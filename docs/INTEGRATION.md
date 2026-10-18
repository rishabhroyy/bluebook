# Integration — how the parts join up

All three parts are merged on `main`. Part 3 is mounted only when `BB_INTERACTIVE=1`; with the flag off the
system behaves exactly as if it weren't there. New work still follows the branch and contract rules below.

## Seams

The parts never import each other. They meet only through these core interfaces:

| Seam | Producer → consumer | Mechanism |
|---|---|---|
| Skill package on disk | P1 → P2, P3 | `store.save()` / `store.load()` · `SkillPackage` |
| Glasses UI | P2 → G2 app | `GlassesConnection` + `display.*_page` · WS protocol |
| Current step | P2 → P3 | `hub.publish(StepChanged)` with the step's target objects |
| Overlay | P3 → glasses | `hub.publish(OverlayFrame)` → core `Compositor` (the only writer to the image boxes) |
| Scene frames | camera page / iPhone app → P2, P3 | `/ws/camera` → `hub` `CameraFrame` |
| Scene facts (optional) | P3 → P2 | `hub.publish(SceneObservation)` |
| IMU | glasses → P3 | `hub` `ImuSample` |
| Mounting | each part → server | `setup(app)` in the package `__init__` (may return a background coroutine) |

Each part can be developed against fakes of the others:
- P2 runs on `fixtures/skills/*` or any `data/skills/<id>/` folder.
- P3 fakes P2 with `POST /interactive/step/<session>?name=...`.
- P1 needs nobody. Check its output with `store.load` and `pkg.check()`.

## Branch rules

1. Branch from `main` (suggested names: `part1-perception`, `part2-teaching`, `part3-interactive`).
2. Edit only your own package (plus `fixtures/skills/` additions and `docs/spikes.md`).
3. `uv run pytest` must pass, **including `packages/bluebook_core/tests`** (the contract tests).
4. Don't add a module that duplicates something in core. If two parts need it, it goes in core (below).
5. Rebase on `main` whenever it moves (`git fetch && git rebase origin/main`).

## Contract changes (`packages/bluebook_core`, `apps/g2`, `apps/camera`, `contracts/`)

1. Open a **small PR to `main`** with only the core change, never mixed with part code. Include the model change,
   the output of `uv run python scripts/gen_contracts.py`, new or updated tests in `test_core.py`, and a note
   naming the affected parts.
2. Changes are **additive by default**: new optional fields with defaults. Renames and removals bump
   `SCHEMA_VERSION` major (skill) or `PROTOCOL_VERSION` (wire) and need every affected owner's OK.
3. After merge, every branch rebases. Regenerate skill packages if the `SCHEMA_VERSION` major changed.

Merging into `main` needs the repo owner's explicit approval. Agents open PRs; they don't merge them.

## Smoke checklist (before merging anything user-facing)

- [ ] `./scripts/dev.sh` starts cleanly on a **fresh clone on a second laptop** (no one else's setup needed)
- [ ] `/health` lists the mounted parts, and the printed `https://<host>.ts.net/health` loads on the phone
- [ ] The glasses app connects (scan the QR, or `--sim`) and the library lists every skill in `data/skills/`
- [ ] A skill plays overview → every step → done; double-tap backs out to the exit dialog
- [ ] Hold-to-ask answers in under 3 s; "go back a step" moves back
- [ ] Phone locked 5 min → unlock → the same step is shown
- [ ] A freshly ingested skill (`bluebook-ingest`) plays end to end
- [ ] Scene camera on a tripod: "★ looks done" appears after completing a step
- [ ] Part 3 on: the highlight lands on the step's target; flag off: no overlay traffic

## Demo-day runbook

```bash
./scripts/dev.sh        # on the demo laptop; scan the QR in the Even Realities app
# scene camera: the iPhone app, or Safari → the printed https://<host>.<tailnet>.ts.net/camera?id=scene
# no dev server on the phone: install the beta build (uv run bluebook pack, apps/g2/STORE.md); dev.sh still runs the server
```
