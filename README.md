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
./scripts/dev.sh          # first run creates .env (add your GEMINI_API_KEY) and copies the sample skill
./scripts/dev.sh --sim    # also opens the Even Hub simulator, so no glasses are needed
```

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

```bash
uv run pytest && (cd apps/g2 && pnpm build)   # checks
```
