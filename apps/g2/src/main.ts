// bluebook G2 thin client. Renders what the server says, forwards input/audio/IMU.
// All product logic lives server-side (packages/*). Only touch this file for
// transport/rendering changes, and keep it in step with bluebook_core/protocol.py.
import {
  AudioInputSource,
  CreateStartUpPageContainer,
  EventSourceType,
  ImageContainerProperty,
  ImageRawDataUpdate,
  ListContainerProperty,
  ListItemContainerProperty,
  OsEventTypeList,
  RebuildPageContainer,
  StartUpPageCreateResult,
  TextContainerProperty,
  TextContainerUpgrade,
  waitForEvenAppBridge,
  type EvenAppBridge,
  type EvenHubEvent,
} from '@evenrealities/even_hub_sdk'
import type { Hello, Input, Protocol, ShowPage } from './protocol'

type ServerMsg = Protocol['server']
type ClientMsg = Protocol['client']

// Dev (QR sideload of the Vite server): same origin, Vite proxies /ws to the bluebook server.
// Packaged .ehpk: set VITE_BB_SERVER=wss://<host>.ts.net/ws/glasses at build time.
const SERVER = (import.meta.env.VITE_BB_SERVER as string | undefined) || `${location.origin.replace(/^http/, 'ws')}/ws/glasses`
const logEl = document.getElementById('log')!
const log = (s: string) => {
  logEl.textContent = `${new Date().toISOString().slice(11, 19)} ${s}\n${logEl.textContent}`.slice(0, 4000)
}

let bridge: EvenAppBridge
let ws: WebSocket | null = null
let pageCreated = false
// crypto.randomUUID exists only in secure contexts; the dev sideload is plain http://<tailnet-ip>:5173,
// where it is undefined in the Even app's WebView (measured on G2, 2026-10-17). Ids only need to be unique per app.
const uid = () => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
let talking: string | null = null // utterance id while push-to-talk is held

// ------------------------------------------------------------------ rendering

function toSdk(page: ShowPage) {
  const textObject: TextContainerProperty[] = []
  const listObject: ListContainerProperty[] = []
  const imageObject: ImageContainerProperty[] = []
  for (const b of page.boxes) {
    const geo = { xPosition: b.x, yPosition: b.y, width: b.w, height: b.h, containerID: b.id, containerName: b.name, ...(b.z != null && { zOrderIndex: b.z }) }
    if (b.kind === 'image') imageObject.push(new ImageContainerProperty(geo))
    else if (b.kind === 'list')
      listObject.push(new ListContainerProperty({
        ...geo, isEventCapture: b.capture ? 1 : 0, borderWidth: b.border, borderColor: b.border_color, borderRadius: b.radius, paddingLength: b.padding,
        itemContainer: new ListItemContainerProperty({ itemCount: b.items.length, itemWidth: b.w, isItemSelectBorderEn: 1, itemName: b.items }),
      }))
    else
      textObject.push(new TextContainerProperty({
        ...geo, isEventCapture: b.capture ? 1 : 0, borderWidth: b.border, borderColor: b.border_color, borderRadius: b.radius, paddingLength: b.padding,
        content: b.content ?? '', ...(b.brightness != null && { textColor: b.brightness }),
      }))
  }
  return { containerTotalNum: page.boxes.length, textObject, listObject, imageObject }
}

// The SDK forbids concurrent image sends and each call costs ~100 ms, so all display
// writes go through one queue, and pending writes to the same container coalesce (latest wins).
const pending = new Map<string, () => Promise<unknown>>()
let draining = false
function enqueue(key: string, op: () => Promise<unknown>) {
  if (key === 'page') pending.clear() // a rebuild supersedes queued updates for the old page
  pending.set(key, op)
  if (!draining) void drain()
}
async function drain() {
  draining = true
  while (pending.size) {
    const [key, op] = pending.entries().next().value!
    pending.delete(key)
    try { await op() } catch (e) { log(`display ${key} failed: ${e}`) }
  }
  draining = false
}

function b64ToBytes(b64: string) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
}

async function handle(msg: ServerMsg) {
  switch (msg.type) {
    case 'page':
      return enqueue('page', async () => {
        const c = toSdk(msg)
        if (!pageCreated) {
          const r = await bridge.createStartUpPageContainer(new CreateStartUpPageContainer(c))
          if (r !== StartUpPageCreateResult.success) throw new Error(`createStartUpPageContainer=${r}`)
          pageCreated = true
        } else if (!(await bridge.rebuildPageContainer(new RebuildPageContainer(c)))) throw new Error('rebuild failed')
      })
    case 'text':
      return enqueue(`t${msg.id}`, () => bridge.textContainerUpgrade(new TextContainerUpgrade({
        containerID: msg.id, containerName: msg.name, content: msg.content, ...(msg.brightness != null && { textColor: msg.brightness }),
      })))
    case 'image':
      return enqueue(`i${msg.id}`, () => bridge.updateImageRawData(new ImageRawDataUpdate({
        containerID: msg.id, containerName: msg.name, imageData: b64ToBytes(msg.png_b64),
      })))
    case 'control':
      if (msg.mic != null) await bridge.audioControl(msg.mic, AudioInputSource.Glasses)
      if (msg.imu_pace != null) await bridge.imuControl(msg.imu_pace > 0, msg.imu_pace || undefined)
      if (msg.exit) await bridge.shutDownPageContainer(1)
      return
    case 'pong':
      return log(`rtt ${((Date.now() / 1000 - msg.t) * 1000).toFixed(0)} ms`)
  }
}

// -------------------------------------------------------------------- input

const now = () => Date.now() / 1000
const send = (m: ClientMsg) => ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify(m))

const SOURCE: Record<number, Input['source']> = {
  [EventSourceType.TOUCH_EVENT_FROM_GLASSES_R]: 'right',
  [EventSourceType.TOUCH_EVENT_FROM_GLASSES_L]: 'left',
  [EventSourceType.TOUCH_EVENT_FROM_RING]: 'ring',
}

function onEvent(ev: EvenHubEvent) {
  if (ev.audioEvent) {
    if (talking && ws?.readyState === WebSocket.OPEN) ws.send(ev.audioEvent.audioPcm)
    return
  }
  const sys = ev.sysEvent
  if (sys?.eventType === OsEventTypeList.IMU_DATA_REPORT && sys.imuData) {
    const { x = 0, y = 0, z = 0 } = sys.imuData
    return send({ type: 'imu', x, y, z, t: now() })
  }
  const lifecycle = { [OsEventTypeList.FOREGROUND_ENTER_EVENT]: 'foreground', [OsEventTypeList.FOREGROUND_EXIT_EVENT]: 'background',
    [OsEventTypeList.ABNORMAL_EXIT_EVENT]: 'abnormal_exit', [OsEventTypeList.SYSTEM_EXIT_EVENT]: 'system_exit' } as const
  const e = ev.listEvent ?? ev.textEvent ?? sys
  if (!e) return
  // Quirk: CLICK_EVENT (0) often arrives as undefined after SDK fromJson.
  const type = e.eventType ?? OsEventTypeList.CLICK_EVENT
  if (type in lifecycle) {
    return send({ type: 'lifecycle', event: lifecycle[type as keyof typeof lifecycle], t: now() })
  }
  const kind = ({
    [OsEventTypeList.CLICK_EVENT]: ev.listEvent ? 'list_select' : 'tap',
    [OsEventTypeList.DOUBLE_CLICK_EVENT]: 'double_tap',
    [OsEventTypeList.SCROLL_TOP_EVENT]: 'scroll_up',
    [OsEventTypeList.SCROLL_BOTTOM_EVENT]: 'scroll_down',
    [OsEventTypeList.LONG_PRESS_EVENT]: 'long_press',
    [OsEventTypeList.LONG_PRESS_RELEASE_EVENT]: 'long_press_release',
  } as Record<number, Input['kind']>)[type]
  if (!kind) return
  // Long press is push-to-talk on every screen (contract, see DESIGN §6.2).
  if (kind === 'long_press' && !talking) {
    talking = uid()
    send({ type: 'audio_start', utterance: talking })
    void bridge.audioControl(true, AudioInputSource.Glasses)
  } else if (kind === 'long_press_release' && talking) {
    void bridge.audioControl(false)
    send({ type: 'audio_end', utterance: talking })
    talking = null
  }
  send({
    type: 'input', kind, t: now(),
    source: SOURCE[(sys?.eventSource as number) ?? -1] ?? 'unknown',
    container: ev.listEvent?.containerName ?? ev.textEvent?.containerName ?? null,
    // Quirk: index 0 is often omitted.
    index: kind === 'list_select' ? (ev.listEvent?.currentSelectItemIndex ?? 0) : null,
  })
}

// ---------------------------------------------------------------- transport

let backoff = 500
function connect() {
  log(`connecting ${SERVER}`)
  ws = new WebSocket(SERVER)
  ws.binaryType = 'arraybuffer'
  ws.onopen = async () => {
    backoff = 500
    const info = await bridge.getDeviceInfo()
    send({ type: 'hello', protocol: 1, client: 'g2', device: info ? JSON.parse(JSON.stringify(info)) : {} } satisfies Hello)
    log('connected')
  }
  ws.onmessage = (m) => void handle(JSON.parse(m.data as string) as ServerMsg).catch((e) => log(`handle: ${e}`))
  ws.onclose = () => {
    log(`closed, retry in ${backoff} ms`)
    setTimeout(connect, backoff)
    backoff = Math.min(backoff * 2, 10_000)
  }
}

async function main() {
  bridge = await waitForEvenAppBridge()
  bridge.onEvenHubEvent(onEvent)
  bridge.onDeviceStatusChanged((s) => send({ type: 'status', battery: s.batteryLevel ?? null, wearing: s.isWearing ?? null, charging: s.isCharging ?? null }))
  setInterval(() => send({ type: 'ping', t: now() }), 15_000)
  connect()
}

void main()
