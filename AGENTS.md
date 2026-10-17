# AGENTS.md — rules for every agent working in this repo

bluebook teaches hands-on tasks on Even Realities G2 glasses. Read `docs/DESIGN.md` first, then your
brief in `workstreams/`, then `docs/INTEGRATION.md`, then the "Run it on your own laptop" section of `README.md`.

## Which part am I?

Your human will tell you, or the branch name will (suggested names below). If neither does, ask
before editing anything.

| Part (suggested branch) | Your brief | You may edit |
|---|---|---|
| `part1-perception` | `workstreams/part1-perception.md` | `packages/bluebook_perception/**`, `fixtures/skills/` (add) |
| `part2-teaching` | `workstreams/part2-teaching.md` | `packages/bluebook_teach/**`, `docs/spikes.md`, `fixtures/skills/` (add) |
| `part3-interactive` | `workstreams/part3-interactive.md` | `packages/bluebook_interactive/**`, `docs/spikes.md`, `fixtures/skills/` (add) |
| shared / `main` | — | core/contracts only, via small PRs (see INTEGRATION §Contract changes) |

Everything else (`packages/bluebook_core`, `apps/`, `contracts/`, `scripts/`) is **shared**. Don't
change it on a part branch. If you need a change there, stop and make a separate contract PR to `main`.

## Use the shared core; never duplicate it

- Model calls: `bluebook_core.models.generate/embed/upload/video/image/audio_pcm16k`, with a role name, never a model id.
- Skills: `bluebook_core.store` + `bluebook_core.skill`.
- Glasses output: `GlassesConnection` + `bluebook_core.display` layouts. Glasses input: `Controller` methods.
- Cross-part events: `bluebook_core.hub` with the event models in `protocol.py`.
- Config/flags/secrets: `bluebook_core.config` (env vars `BB_*`, `GEMINI_API_KEY`).

## Commands (run from the repo root; config reads `./.env`)

```bash
uv run bluebook bootstrap --yes [--api-key K]  # set up this laptop non-interactively (humans: drop --yes)
uv sync                                   # Python workspace (uv; Python ≥3.12)
uv run pytest                             # all tests incl. core contract tests — must stay green
uv run ruff check . && uv run ruff format --check .
./scripts/dev.sh [--sim]                  # everything on this laptop: server + glasses app + QR (+ simulator)
uv run bluebook-server                    # just the server, on 127.0.0.1:8787
uv run python scripts/gen_contracts.py    # after any protocol/skill model change (main only)
cd apps/g2 && pnpm dev                    # glasses app only (dev.sh does this); `pnpm sim` simulator
```

## Running and testing (every laptop is independent)

- Each developer runs a **full private copy** on their own laptop with `./scripts/dev.sh`: their own
  server, `.env` (own `GEMINI_API_KEY`), `data/skills/`, and tailnet hostname. Never assume another
  person's laptop, server, URL, or data exists. Never hard-code a hostname, IP, or `ts.net` address.
- Configure a laptop with `uv run bluebook bootstrap` (it writes `.env`); never hand a human a list of
  env vars to set instead. New settings go into `FIELDS` in `bluebook_core/cli.py` **and** `config.py`.
- `.env`, `data/`, `apps/g2/app.local.json`, and `*.ehpk` are per-laptop and gitignored. Don't commit
  them, and don't put a real origin into `apps/g2/app.json` (it keeps the `REPLACE` placeholder;
  `scripts/pack-g2.sh` fills it in per laptop).
- Order of preference for checking your work: (1) `uv run pytest` with fakes, (2) the running server
  with the simulator (`./scripts/dev.sh --sim`; no glasses needed), (3) real glasses, which need a
  human to scan the QR code. Ask the human for (3); don't block on it.
- Scene camera without a phone: the laptop webcam at `http://localhost:8787/camera?id=scene`.
- Need another part's output (e.g. a real skill from Part 1)? Use `fixtures/skills/`, or ask the human
  to copy a `data/skills/<id>/` folder over. Don't add network calls to someone else's machine.
- If you change how the system is started or configured, update `scripts/dev.sh`, `bluebook bootstrap`,
  the README section, and this file in the same PR to `main`.

## Conventions

- Python 3.12+, pydantic v2, asyncio, FastAPI. Type hints throughout. Ruff, line length 120.
- Keep it small: no new abstraction layers, no new services or queues, no new dependency without saying
  why in the PR. One provider (Gemini) for v1.
- Tests: one focused test file per package (`tests/test_<part>.py`). No network or model calls in tests;
  fake them. Real-model runs are scripts or evals, not tests.
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`). Don't push to or merge into `main`
  without the owner's explicit OK.
- Measurements beat guesses. G2 behaviour that isn't in `docs/DESIGN.md` §1 is unverified: test it on
  hardware and record it in `docs/spikes.md`.
