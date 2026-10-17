# Integration plan — how the three parts join up

## Branches

Each developer creates their own branch from `main`. Suggested names:

```
main ─┬─ part1-perception   (packages/bluebook_perception)
      ├─ part2-teaching     (packages/bluebook_teach)
      └─ part3-interactive  (packages/bluebook_interactive)
```

All branches start from `main`, which already has the shared core, the server,
the G2 thin client, the camera page, the contracts, and a fixture skill. No glue code needs writing
later. The seams already exist:

| Seam | Producer → consumer | Mechanism |
|---|---|---|
| Skill package on disk | P1 → P2, P3 | `store.save()` / `store.load()` · `SkillPackage` |
| Glasses UI | P2 (and P3 calibration) → G2 app | `GlassesConnection` + `display.*_page` · WS protocol |
| Current step | P2 → P3 | `hub.publish(StepChanged)` |
| Overlay | P3 → glasses | `hub.publish(OverlayFrame)` → core `Compositor` |
| Scene frames | camera page → P2, P3 | `/ws/camera` → `hub` `CameraFrame` |
| Scene facts (optional) | P3 → P2 | `hub.publish(SceneObservation)` |
| IMU | glasses → P3 | `hub` `ImuSample` |
| Mounting | each part → server | `setup(app)` in the package `__init__` (may return a background coroutine) |

Each part develops against **fakes of the others**:
- P2 uses `fixtures/skills/*` instead of P1 output.
- P3 publishes `StepChanged` from a small script (or the fixture + P2 once merged).
- P1 needs nobody. Check output with `store.load` and `pkg.check()`.

## Rules for every branch

1. Edit only your own package (plus `fixtures/skills/` additions and `docs/spikes.md`).
2. `uv run pytest` must pass, **including `packages/bluebook_core/tests`** (the contract tests).
3. Don't add a module to your package that duplicates something in core. If two parts need a thing,
   it goes in core (see below).
4. Keep your branch rebased on `main` (`git fetch && git rebase origin/main`) at least whenever main moves.

## Contract changes (anything in `packages/bluebook_core`, `apps/`, `contracts/`)

1. Open a **small PR to `main`** with only the core change, never mixed with part code. Include:
   the model change, `uv run python scripts/gen_contracts.py` output, updated/added tests in
   `test_core.py`, and a note in the PR body naming which parts are affected.
2. Changes are **additive by default**: new optional fields with defaults. Renames/removals bump
   `SCHEMA_VERSION` major (skill) or `PROTOCOL_VERSION` (wire) and need all affected owners' OK.
3. After merge, every branch rebases. Regenerate any skill packages if `SCHEMA_VERSION` major changed.

Merging into `main` needs the repo owner's explicit approval. Agents open PRs and don't merge them.

## Merge order

1. **Part 2 first.** It replaces `EchoController` and makes the system demoable with fixture skills.
2. **Part 1.** Real skills appear in the library with no code change on P2's side.
3. **Part 3**, behind `BB_INTERACTIVE=1`. With the flag off, the system must behave exactly as before.

Before each merge: rebase on main, run the full test suite, then run the smoke checklist below on real glasses.

## End-to-end smoke checklist (after each merge)

- [ ] `uv run bluebook-server` starts; `/health` lists mounted parts
- [ ] `tailscale serve --bg 8787`; `https://<host>.ts.net/health` loads on the phone
- [ ] G2 app connects (sideload via `pnpm qr`); library lists all skills in `data/skills/`
- [ ] Fixture skill: overview → all steps → done; double-tap backs out to exit dialog
- [ ] Hold-to-ask returns an answer in < 3 s; "go back a step" moves back
- [ ] Phone locked 5 min → unlock → same step shown
- [ ] (P1 merged) a freshly ingested skill plays end to end
- [ ] (camera) `/camera?id=scene` on tripod phone; "✓ looks done" appears after completing a step
- [ ] (P3, flag on) calibrate; highlight appears on step-1 target; flag off → no overlay traffic

## Demo-day runbook

```bash
cp .env.example .env               # set GEMINI_API_KEY; put your ts.net origin in apps/g2/app.json
uv sync && uv run bluebook-server  # laptop
tailscale serve --bg 8787          # https://<host>.<tailnet>.ts.net
cd apps/g2 && pnpm dev && pnpm qr  # scan with Even Realities app (phone on tailnet)
# tripod phone: Safari → https://<host>.<tailnet>.ts.net/camera?id=scene
```
