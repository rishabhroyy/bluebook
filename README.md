# bluebook

Learn hands-on tasks from short tooltips on Even Realities G2 glasses instead of long videos.
An expert records a task once on an iPhone, bluebook turns it into a structured skill, and the
glasses teach it back one step at a time. You can ask questions by voice, and an optional scene
camera lets the AI see your progress (plus experimental AR highlights).

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- How the parts fit together: [docs/INTEGRATION.md](docs/INTEGRATION.md)
- Agent rules: [AGENTS.md](AGENTS.md) · per-part briefs: [workstreams/](workstreams/)

## Run it on your own laptop

Each person runs their own complete copy (their own server, API key, skills and tailnet hostname),
so nobody waits on anyone else's machine.

```bash
uv run bluebook bootstrap # once per laptop: checks prereqs, asks for your Gemini key (hidden) and settings,
                          # tests the key, writes .env, copies the sample skill, installs app deps
./scripts/dev.sh          # run everything (it runs bootstrap for you if .env is missing)
./scripts/dev.sh --sim    # also opens the Even Hub simulator, so no glasses are needed
```

Re-run `bluebook bootstrap` any time to change settings: current values are the defaults, and keys it
doesn't ask about (e.g. `BB_MODEL_*` overrides) are kept. Non-interactive:
`uv run bluebook bootstrap --yes --api-key "$GEMINI_API_KEY"`.

`dev.sh` starts the server and the glasses-app dev server, runs `tailscale serve` if Tailscale is
up, and prints a QR code. Scan it in the Even Realities app to load the glasses app **from your
laptop**. Whoever's QR was scanned last is the laptop the glasses talk to, so a shared pair of glasses
(or a shared phone) just rescans to switch between people.

| Need | How |
|---|---|
| Prerequisites | `uv`, `pnpm`, a Gemini API key. Optional: Tailscale (same team tailnet, with HTTPS certificates turned on once in the admin console) |
| No glasses | `--sim`: the simulator renders the 576×288 display and uses your laptop mic |
| Scene camera, no phone | laptop webcam at `http://localhost:8787/camera?id=scene` (localhost counts as a secure context) |
| Scene camera, iPhone | `https://<your-laptop>.<tailnet>.ts.net/camera?id=scene` (printed by `dev.sh`; needs Tailscale) |
| Phone on the same Wi-Fi, no Tailscale | works for the glasses app (QR uses your LAN IP); the iPhone camera page needs Tailscale for HTTPS |
| Installable build for one laptop | `./scripts/pack-g2.sh` → `apps/g2/bluebook-<laptop>.ehpk`, whitelisted for that laptop only |
| Someone else's skills | copy their `data/skills/<id>/` folder into yours (zip / AirDrop). Small ones can be committed to `fixtures/skills/` |

The glasses run `EchoController`, which echoes your input back to you, until Part 2 is merged.

## Part 3 (AR highlights): the iPhone app

Part 3 is experimental and off by default. It uses a native iPhone app, `apps/camera-ios/`, as the scene
camera and dev console: ARKit world tracking on the rear camera + LiDAR, plus front TrueDepth face
tracking for the wearer's head pose.

| Need | How |
|---|---|
| Prerequisites | Xcode, `brew install xcodegen`, an iPhone with LiDAR and TrueDepth (Pro models), an Apple developer team (set `DEVELOPMENT_TEAM` in `apps/camera-ios/project.yml`), Developer Mode on the phone |
| Enable on the server | `uv run bluebook bootstrap` and answer yes to the AR question (sets `BB_INTERACTIVE=1`), then restart `./scripts/dev.sh` |
| Find your phone's id | plug it in, unlock, trust, then `xcrun devicectl list devices` (the Identifier column is `<UDID>`) |
| Build + install | see below |
| Point the app at your laptop | in the app: gear, then the server URL `./scripts/dev.sh` prints (`https://<your-laptop>.<tailnet>.ts.net`; an `http://<lan-ip>:8787` URL works on the same Wi-Fi), then Start. Allow Camera and Local Network |

```bash
cd apps/camera-ios && xcodegen
xcodebuild -scheme BluebookCamera -destination 'id=<UDID>' -allowProvisioningUpdates -derivedDataPath build build
xcrun devicectl device install app --device <UDID> build/Build/Products/Debug-iphoneos/BluebookCamera.app
```

First launch: trust the developer in Settings > General > VPN & Device Management. More in
`apps/camera-ios/README.md`.

Try a highlight with the glasses connected (the session is the glasses' serial number; see `/interactive/status`):

```bash
curl -X POST "http://localhost:8787/interactive/step/<session>?name=camera"        # test target: the phone's own lens
curl -X POST "http://localhost:8787/interactive/step/<session>?name=hydro%20flask" # Gemini finds it in the phone's view
curl -X POST "http://localhost:8787/interactive/step/<session>?anchor=tapped&name=thing"  # after tapping the preview
curl -X POST "http://localhost:8787/interactive/step/<session>"                    # clear
```

`http://localhost:8787/interactive/status` shows tracking and the full projection chain. Each server
run records the raw session to `data/diag/poses.jsonl` (overwritten at start); replay it with
`uv run python -m bluebook_interactive.replay data/diag/poses.jsonl` to tune offline. To capture a fresh run
(spoken prompts, until Ctrl-C) and print jitter/lag metrics: `uv run python -m bluebook_interactive.replay --record`.

```bash
uv run pytest && (cd apps/g2 && pnpm build)   # checks
```
