# AGENTS.md — rules for every agent working in this repo

bluebook teaches hands-on tasks on Even Realities G2 glasses. Read `docs/DESIGN.md` first, then your
brief in `workstreams/`, then `docs/INTEGRATION.md`.

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
uv sync                                   # Python workspace (uv; Python ≥3.12)
uv run pytest                             # all tests incl. core contract tests — must stay green
uv run ruff check . && uv run ruff format --check .
uv run bluebook-server                    # dev server on 127.0.0.1:8787
uv run python scripts/gen_contracts.py    # after any protocol/skill model change (main only)
cd apps/g2 && pnpm install && pnpm dev    # glasses app (pnpm); `pnpm sim` simulator, `pnpm qr` sideload
```

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
