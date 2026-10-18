# bluebook

Learn hands-on tasks from short, glanceable steps on Even Realities G2 glasses instead of pausing a video.

An expert records a task once on an iPhone, talking through it as they work. bluebook turns that video and its
audio into a structured **skill**: ordered steps, the objects involved, tips, common mistakes and safety notes.
The glasses then teach it back one step at a time. The learner keeps their hands on the work, holds the touchpad
to ask a question out loud, and an optional scene camera lets the tutor check their progress.

## How it works

| Part | What it does | Package |
|---|---|---|
| 1. Perception | Video + audio → skill. Local word-level transcription, a whole-video outline pass, per-step clip passes at higher fps, sharpest-frame keyframes, a verification pass that tags every claim as observed, narrated or inferred, then embeddings for retrieval. | `bluebook_perception` |
| 2. Teaching | The glasses UI: skill library → overview → one tooltip per step. Voice questions go straight to the tutor model as audio. A scene camera, if connected, checks each step against its success criteria and suggests "looks done"; it never advances on its own. | `bluebook_teach` |
| 3. AR highlights (experimental, off by default) | Keeps a highlight on the step's target object as the wearer moves their head. The G2 has no camera, so an iPhone tracks the wearer's head from outside (ARKit face + LiDAR world tracking, no markers) and finds the object in the scene. | `bluebook_interactive`, `apps/camera-ios` |

The glasses app is a thin client: the server sends page layouts and text, the app renders them and sends input and
microphone audio back. All product logic is Python, in one process on the laptop, reached over Tailscale.

```
iPhone (expert video) ──▶ Part 1 ──▶ data/skills/<id>/ ──▶ Part 2 ◀──WebSocket──▶ Even app WebView ◀─BLE─▶ G2
                                                             ▲  StepChanged  │
iPhone (scene camera) ──JPEG / head pose────────────────────▶ Part 3 ──overlay┘
```

Design, measurements and the reasons behind each choice: [docs/DESIGN.md](docs/DESIGN.md).

## Stack

| Layer | Technology |
|---|---|
| Server | Python 3.12+, FastAPI + uvicorn, asyncio, pydantic v2, one in-process pub/sub hub |
| Models | Gemini via `google-genai`: video + audio understanding, tutor (audio in, structured JSON out), scene checks, multimodal embeddings, object boxes. TypeSafe Jev for fast (~100 ms) object boxes in Part 3. Every call goes through one client with a role name; model ids live only in config. |
| Transcription | faster-whisper large-v3, local, word timestamps (Gemini as fallback) |
| Media | ffmpeg / ffprobe, Pillow, NumPy |
| Glasses app | TypeScript + Vite on the Even Hub SDK (`@evenrealities/even_hub_sdk`), packaged as `.ehpk` for Even Hub |
| iPhone app | Swift / SwiftUI, ARKit (world tracking, TrueDepth face, LiDAR depth), Vision object tracking, XcodeGen |
| Networking | Tailscale (`tailscale serve` for HTTPS/WSS on the tailnet) |
| Tooling | uv workspace, pnpm, ruff, pytest. Wire and skill schemas are generated from the pydantic models into JSON Schema and TypeScript. |

## Run it

Every developer runs a complete private copy (their own server, API keys, skills and tailnet name), so nobody
depends on anyone else's machine.

```bash
uv run bluebook bootstrap   # once: checks prerequisites, asks for your Gemini key, writes .env, installs app deps
./scripts/dev.sh            # server + glasses app dev server + a QR code to load the app on your phone
./scripts/dev.sh --sim      # the same, plus the Even Hub simulator (no glasses needed)
```

Scan the QR code in the Even Realities app to load the glasses app from your laptop. Re-run `bootstrap` any time
to change settings; current values are offered as defaults and keys it doesn't ask about are kept.

| Task | How |
|---|---|
| Prerequisites | `uv`, `pnpm`, `ffmpeg`, a Gemini API key. Tailscale is recommended (HTTPS certificates turned on once in the admin console). |
| Record a skill | `uv run bluebook-ingest VIDEO --id fix-leaky-tap --title "Fix a leaky tap"` → `data/skills/<id>/` (run `uv run bluebook whisper` once first for the ~3 GB transcription model) |
| Scene camera | iPhone Safari: `https://<laptop>.<tailnet>.ts.net/camera?id=scene` (printed by `dev.sh`), or the laptop webcam at `http://localhost:8787/camera?id=scene` |
| Installable build (Even Hub beta) | `uv run bluebook pack [--check]` → `apps/g2/bluebook-<host>-<version>.ehpk`. Upload steps and the release checklist: [apps/g2/STORE.md](apps/g2/STORE.md) |
| Share skills | copy a `data/skills/<id>/` folder between laptops |
| Glasses-POV graphics for video editing | `uv run bluebook-premiere --help` (transparent PNGs / ProRes 4444 rendered through the simulator) |
| Checks | `uv run pytest`, `uv run ruff check . && uv run ruff format --check .`, `cd apps/g2 && pnpm build` |

### Part 3: the iPhone app

Off by default. Turn it on with `uv run bluebook bootstrap` (answer yes to AR mode; it also asks for a TypeSafe
key), then restart `dev.sh`. It needs an iPhone with LiDAR and TrueDepth (Pro models), Xcode, `brew install xcodegen`
and an Apple developer team (`DEVELOPMENT_TEAM` in `apps/camera-ios/project.yml`).

```bash
cd apps/camera-ios && xcodegen
xcodebuild -scheme BluebookCamera -destination 'id=<UDID>' -allowProvisioningUpdates -derivedDataPath build build
xcrun devicectl device install app --device <UDID> build/Build/Products/Debug-iphoneos/BluebookCamera.app
```

`<UDID>` comes from `xcrun devicectl list devices`. In the app, tap the gear, enter the server URL `dev.sh` printed,
and press Start. To try a highlight without a lesson (the session id is the glasses serial, see
`/interactive/status`):

```bash
curl -X POST "http://localhost:8787/interactive/step/<session>?name=camera"        # the phone's own lens
curl -X POST "http://localhost:8787/interactive/step/<session>?name=hydro%20flask" # any object by name
curl -X POST "http://localhost:8787/interactive/step/<session>"                    # clear
```

`/interactive/status` shows tracking and the full projection chain. Each server run records the session to
`data/diag/poses.jsonl`; `uv run python -m bluebook_interactive.replay data/diag/poses.jsonl` replays it offline,
and `--record` captures a fresh run and prints jitter and lag. More in
[apps/camera-ios/README.md](apps/camera-ios/README.md).

## Repository

```
packages/bluebook_core/          shared contracts, server, model client, skill store, glasses display layouts
packages/bluebook_perception/    Part 1: video → skill pipeline (prompts in prompts/*.md)
packages/bluebook_teach/         Part 2: teaching controller, tutor, scene checks
packages/bluebook_interactive/   Part 3: head pose, object grounding, overlay renderer, replay tools
packages/bluebook_premiere/      renders glasses-POV graphics for demo videos
apps/g2/                         glasses app (thin client) + Even Hub packaging
apps/camera/                     scene-camera web page
apps/camera-ios/                 native iPhone scene camera and Part 3 pose app
contracts/                       JSON Schemas generated from the pydantic models
fixtures/skills/                 a hand-written skill used by the tests
docs/                            design, integration rules, hardware measurements
workstreams/                     the original brief for each part
```

| Doc | Read it for |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | platform constraints, architecture, model choices, latency and accuracy budgets |
| [docs/spikes.md](docs/spikes.md) | what was measured on real hardware |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | how the parts connect and how shared contracts change |
| [AGENTS.md](AGENTS.md) | working rules for contributors and coding agents |
| [packages/bluebook_interactive/HANDOFF.md](packages/bluebook_interactive/HANDOFF.md) | Part 3 as built, and its open items |
