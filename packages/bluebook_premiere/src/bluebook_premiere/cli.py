"""Render what the wearer sees on the G2 as a transparent PNG, for compositing over footage.

Frames are built with the app's own code (core layouts, Part 3's ring/chevron placement, the
overlay compositor, Part 2's keyframe band and text sanitising), sent to the Even Hub simulator
through the real SDK, and read back from its glasses framebuffer: the real font, real wrapping,
green with alpha. Needs the simulator installed: `cd apps/g2 && pnpm install`. A simulator
window opens for a few seconds per run.

  bluebook-premiere screen s2.png --tip "Step 2/5: Loosen the nut" --status "2/5 | ● Listening..." --scale 3
      # full frame, tip and status where the app puts them, the rest transparent; add --target 300,160 for marks
  bluebook-premiere pack data/premiere/ --scale 3            # ring + 4 chevrons, cropped, to move and keyframe
  bluebook-premiere menu data/premiere/menu.mov --scale 3 --items "Fixing a leaky pipe" "LAHACKS!"
      # ProRes 4444 video with alpha: steps down the library list, taps the last item

Coordinates are display px (576x288, origin top-left). A --target inside the image band
(y 88..232) gets the ring; outside it, the chevron on the band edge pointing at it, like the app.
Off-screen targets need the = form: --target=-200,160. Use the same --scale for screens and the pack.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

from bluebook_core import display
from bluebook_core.protocol import OverlayFrame, ShowPage
from bluebook_interactive.geom import CENTER, place
from bluebook_teach.controller import band_tiles
from bluebook_teach.render import LIBRARY_TITLE, sanitize
from PIL import Image, ImageChops, ImageFilter

REPO = Path(__file__).resolve().parents[4]
SIM = REPO / "apps/g2/node_modules/.bin/evenhub-simulator"
SDK = REPO / "apps/g2/node_modules/@evenrealities/even_hub_sdk/dist/index.js"
CX, CY = CENTER
# --pack: the app's ring at the band centre, and the chevron on each band edge.
PACK = {
    "ring": (CX, CY),
    "chevron-left": (-500, CY),
    "chevron-right": (1000, CY),
    "chevron-up": (CX, -500),
    "chevron-down": (CX, 800),
}

# Mirrors toSdk() in apps/g2/src/main.ts; keep the two in step.
PAGE_HTML = """<!doctype html><meta charset="utf-8"><script type="module">
import { waitForEvenAppBridge, CreateStartUpPageContainer, RebuildPageContainer, TextContainerProperty,
  ListContainerProperty, ListItemContainerProperty, ImageContainerProperty, ImageRawDataUpdate } from './sdk.js'
const bridge = await waitForEvenAppBridge()
function toSdk(page) {
  const textObject = [], listObject = [], imageObject = []
  for (const b of page.boxes) {
    const geo = { xPosition: b.x, yPosition: b.y, width: b.w, height: b.h, containerID: b.id, containerName: b.name,
      ...(b.z != null && { zOrderIndex: b.z }) }
    const deco = { isEventCapture: b.capture ? 1 : 0, borderWidth: b.border, borderColor: b.border_color,
      borderRadius: b.radius, paddingLength: b.padding }
    if (b.kind === 'image') imageObject.push(new ImageContainerProperty(geo))
    else if (b.kind === 'list') listObject.push(new ListContainerProperty({ ...geo, ...deco,
      itemContainer: new ListItemContainerProperty({ itemCount: b.items.length, itemWidth: b.w,
        isItemSelectBorderEn: 1, itemName: b.items }) }))
    else textObject.push(new TextContainerProperty({ ...geo, ...deco, content: b.content ?? '',
      ...(b.brightness != null && { textColor: b.brightness }) }))
  }
  return { containerTotalNum: page.boxes.length, textObject, listObject, imageObject }
}
let seq = -1, started = false
for (;;) {
  const f = await (await fetch('frame.json', { cache: 'no-store' })).json()
  if (f.seq !== seq) {
    seq = f.seq
    const c = toSdk(f.page)
    if (!started) { started = true; await bridge.createStartUpPageContainer(new CreateStartUpPageContainer(c)) }
    else await bridge.rebuildPageContainer(new RebuildPageContainer(c))
    for (const m of f.images) await bridge.updateImageRawData(new ImageRawDataUpdate({ containerID: m.id,
      containerName: m.name, imageData: Uint8Array.from(atob(m.png_b64), (ch) => ch.charCodeAt(0)) }))
    console.log('bb-done ' + seq)
  }
  await new Promise((r) => setTimeout(r, 100))
}
</script>"""


def build(args: argparse.Namespace) -> tuple[ShowPage, list]:
    """The page and image messages the server would send for this screen."""
    if args.page == "list":
        return display.list_page(sanitize(args.title), [sanitize(i) for i in args.items]), []
    if args.page == "message":
        return display.message_page(sanitize(args.message)), []
    page = display.teach_page(sanitize(args.tip), sanitize(args.status))
    if args.page == "none":  # just the image band: overlays without any text
        page = ShowPage(boxes=[b for b in page.boxes if b.name in ("bg", "img_l", "img_r")])
    images = list(band_tiles(args.keyframe)) if args.keyframe else []
    prims = [p for u, v in args.target for p in place(u, v, 1.0)]
    if prims:
        images += display.Compositor().update(page, OverlayFrame(session="", t=0, primitives=prims))
    return page, images


def finish(shot: Image.Image, args: argparse.Namespace) -> Image.Image:
    """Simulator framebuffer (green, alpha = brightness) -> the PNG we save."""
    alpha = shot.getchannel("A")
    if args.crop and (bb := alpha.getbbox()):
        alpha = alpha.crop((bb[0] - 4, bb[1] - 4, bb[2] + 4, bb[3] + 4))
    if args.scale != 1:
        alpha = alpha.resize((alpha.width * args.scale, alpha.height * args.scale), Image.Resampling.NEAREST)
    if args.bold:  # thicken strokes for video (the real display is thinner): grow each lit pixel by `bold` px
        alpha = alpha.filter(ImageFilter.MaxFilter(2 * args.bold + 1))
    out = Image.new("RGBA", alpha.size, args.color or "#00ff00")  # simulator green
    out.putalpha(alpha)
    return out


class Simulator:
    """The Even Hub simulator on a local page that draws whatever frame.json holds."""

    def __init__(self, extra: list[str] = ()) -> None:  # extra simulator flags, e.g. ["--bounce", "spring"]
        if not SIM.exists() or not SDK.exists():
            raise SystemExit("Even Hub simulator not installed: cd apps/g2 && pnpm install")
        self.seq = 0
        files = {"/": (PAGE_HTML.encode(), "text/html"), "/sdk.js": (SDK.read_bytes(), "text/javascript")}
        self.files = files

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body, kind = files.get(self.path.split("?")[0], (None, ""))
                if body is None:
                    return self.send_error(404)
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a) -> None:
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        with socket.socket() as s:  # ponytail: free-port race with other processes; fine on a laptop
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.api = f"http://127.0.0.1:{port}"
        page = f"http://127.0.0.1:{self.http.server_port}/"
        cmd = [SIM, page, "--automation-port", str(port), "--no-glow", *extra]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def _get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.api + path, timeout=5) as r:
            return r.read()

    def shot(self, page: ShowPage, images: list, timeout: float = 30) -> Image.Image:
        self.seq += 1
        frame = {"seq": self.seq, "page": page.model_dump(), "images": [m.model_dump() for m in images]}
        self.files["/frame.json"] = (json.dumps(frame).encode(), "application/json")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if f'"bb-done {self.seq}"' in self._get("/api/console").decode():
                    # ponytail: let the framebuffer settle after the last SDK call; raise if tiles go missing
                    time.sleep(0.3)
                    return self.screen()
            except OSError:
                pass  # simulator still starting
            time.sleep(0.2)
        raise SystemExit(f"simulator did not draw the frame within {timeout:.0f}s")

    def screen(self) -> Image.Image:
        return Image.open(BytesIO(self._get("/api/screenshot/glasses"))).convert("RGBA")

    def press(self, action: str, timeout: float = 5) -> Image.Image:
        """Send a touchpad action ("down", "click", ...) and return the screen once it changed."""
        before = self._get("/api/screenshot/glasses")
        body = json.dumps({"action": action}).encode()
        req = urllib.request.Request(self.api + "/api/input", body, {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._get("/api/screenshot/glasses") != before:
                time.sleep(0.2)
                return self.screen()
            time.sleep(0.02)
        raise SystemExit(f"simulator screen did not change after {action!r}")

    def close(self) -> None:
        os.killpg(self.proc.pid, signal.SIGTERM)  # the npm wrapper spawns the real binary as a child
        self.http.shutdown()


def _ease(t: float) -> float:
    """Ease-out with a small overshoot, so the highlight lands with a little spring."""
    c = 1.4
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


def menu(sim: Simulator, args: argparse.Namespace) -> list[Image.Image]:
    """Library list: hold on each item, slide the highlight down, tap the last one, cut to its overview.

    The simulator snaps the highlight between items, so the states come from it (real font, border,
    dim/bright text) and only the slide between them is synthesised: the border moves on an eased
    curve while the text brightness crossfades. ponytail: assumes the list fits without scrolling
    (<= ~5 items on list_page); a scrolling list needs the scroll offset captured too."""
    items = [sanitize(i) for i in args.items]
    states = [sim.shot(display.list_page(sanitize(args.title), items), []).getchannel("A")]
    for _ in items[1:]:
        states.append(sim.press("down").getchannel("A"))
    lit = [a.point(lambda v: 255 if v else 0) for a in states]
    borders, texts = [], []
    for i, a in enumerate(states):  # border = lit only in this state; text = the rest
        only = lit[i]
        for j, other in enumerate(lit):
            if j != i:
                only = ImageChops.subtract(only, other)
        borders.append(ImageChops.multiply(a, only))
        texts.append(ImageChops.subtract(a, only))
    tops = [b.getbbox()[1] for b in borders]

    fps, hold, slide = args.fps, round(args.hold * args.fps), round(args.slide * args.fps)
    frames = [states[0]] * hold
    for i in range(len(items) - 1):
        for f in range(1, slide + 1):
            e = _ease(f / slide)
            moved = Image.new("L", states[i].size)
            moved.paste(borders[i], (0, round((tops[i + 1] - tops[i]) * e)))
            frames.append(ImageChops.lighter(Image.blend(texts[i], texts[i + 1], min(e, 1.0)), moved))
        frames += [states[i + 1]] * hold
    tap = max(1, fps // 15)  # tap feedback: the highlight blinks off once
    frames += [texts[-1]] * tap + [states[-1]] * (tap * 2)
    then = args.then if args.then is not None else f"{args.items[-1]}\nTap to start. Double tap for the library."
    if then:  # first lines of render.overview_text for the picked skill
        frames += [sim.shot(display.message_page(sanitize(then)), []).getchannel("A")] * round(args.end * fps)
    return frames


def write_video(frames: list[Image.Image], args: argparse.Namespace) -> None:
    """Alpha frames -> ProRes 4444 .mov with alpha (Premiere imports it with transparency)."""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(args.fps), "-i", "-",
           "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(args.out)]  # fmt: skip
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    cache: dict[int, bytes] = {}  # held frames repeat the same image object: encode once
    for a in frames:
        if id(a) not in cache:
            shot = Image.new("RGBA", a.size, "#00ff00")
            shot.putalpha(a)
            buf = BytesIO()
            finish(shot, args).save(buf, "PNG", compress_level=1)
            cache[id(a)] = buf.getvalue()
        ff.stdin.write(cache[id(a)])
    ff.stdin.close()
    if ff.wait():
        raise SystemExit("ffmpeg failed")


def _point(s: str) -> tuple[float, float]:
    try:
        x, y = (float(p) for p in s.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected X,Y, got {s!r}") from None
    return x, y


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bluebook-premiere",
        description="Render G2 glasses-POV graphics through the Even Hub simulator to transparent PNGs.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    screen = sub.add_parser("screen", help="one full 576x288 frame, everything where the app puts it")
    screen.add_argument("out", type=Path, help="output .png")
    screen.add_argument("--page", choices=["teach", "none", "list", "message"], default="teach", help="app screen")
    screen.add_argument("--tip", default="", help="teach: tooltip text (top)")
    screen.add_argument("--status", default="", help="teach: dim status line (bottom)")
    screen.add_argument("--keyframe", type=Path, help="teach/none: photo in the image band, dithered like the app")
    screen.add_argument("--target", type=_point, action="append", default=[], metavar="X,Y", help="ring or chevron")
    screen.add_argument("--title", default="", help="list: title")
    screen.add_argument("--items", nargs="*", default=[], help="list: items")
    screen.add_argument("--message", default="", help="message: full-screen text")
    screen.set_defaults(crop=False)
    pack = sub.add_parser("pack", help="ring + 4 chevrons as cropped PNGs, to move and keyframe")
    pack.add_argument("out", type=Path, help="output directory")
    pack.set_defaults(crop=True, bold=0)
    m = sub.add_parser("menu", help="video: highlight steps down the library list, then picks the last item")
    m.add_argument("out", type=Path, help="output .mov (ProRes 4444 with alpha)")
    m.add_argument("--items", nargs="+", required=True, help="list entries, top to bottom")
    m.add_argument("--title", default=LIBRARY_TITLE, help="list title (default: the app's)")
    m.add_argument("--hold", type=float, default=1.0, help="seconds on each item")
    m.add_argument("--slide", type=float, default=0.25, help="seconds for the highlight to move")
    m.add_argument("--then", help="text after the pick (default: the overview's first lines; '' to end on the tap)")
    m.add_argument("--end", type=float, default=1.5, help="seconds on the --then screen")
    m.add_argument("--fps", type=int, default=30)
    m.set_defaults(crop=False)
    for sp in (screen, m):
        sp.add_argument("--bold", type=int, default=2, help="thicken by N output px (0 = as on glasses)")
    for sp in (screen, pack, m):
        sp.add_argument("--scale", type=int, default=1, help="integer pixel upscale, e.g. 3 for ~1728 wide, 7 for 4K")
        sp.add_argument("--color", help="recolour the display (simulator green #00ff00 by default)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.cmd == "menu":
        sim = Simulator()
        try:
            frames = menu(sim, args)
        finally:
            sim.close()
        write_video(frames, args)
        return print(args.out)
    if args.cmd == "pack":
        jobs = [(args.out / f"{n}.png", build(parser().parse_args(["screen", ".", "--page", "none", f"--target={x},{y}"])))
                for n, (x, y) in PACK.items()]  # fmt: skip
    else:
        jobs = [(args.out, build(args))]
    sim = Simulator()
    try:
        for out, (page, images) in jobs:
            out.parent.mkdir(parents=True, exist_ok=True)
            finish(sim.shot(page, images), args).save(out)
            print(out)
    finally:
        sim.close()
