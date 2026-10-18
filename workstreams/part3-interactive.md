# Part 3 — Interactive AR highlighting (experimental)

Suggested branch: `part3-interactive` · Package: `packages/bluebook_interactive/` · App: `apps/camera-ios/` · Enabled only with `BB_INTERACTIVE=1` (set by `uv run bluebook bootstrap`)
Read first: `AGENTS.md`, `docs/DESIGN.md` §1, §5, §6.3, §8, then `packages/bluebook_interactive/HANDOFF.md` (current state)

## Goal

While the learner is on a step, highlight the step's **target object** (e.g. the water shutoff valve)
in the glasses so that the highlight stays on the real object as they move their head. A native iPhone
app watches the scene and the wearer.

This part is experimental. If you don't have it, Part 2 still works fully. Optimise for learning what
works, and record measurements in `docs/spikes.md`.

## Hard constraints (verified; see DESIGN §1)

- The G2 has **no camera**. Track the wearer's head from outside, from the iPhone.
- **No markers of any kind:** no AprilTags, ArUco/ChArUco, stickers, or clips on the glasses or in the
  scene. This is a product decision, not open for debate. If accuracy falls short, report measurements.
- Display: 576×288, overlays only through `OverlayFrame` → core `Compositor` → image boxes. Updates
  are ~105–185 ms each and sequential, so you get **~5 fps** for one 288×144 tile and fewer for two.
- G2 optics are not published: we use the spec prior and learn only a boresight offset.
- Dropped for good (owner's decision): MediaPipe, Depth Pro, SPAAM / fingertip calibration, a web debug page.

## You own

- `packages/bluebook_interactive/**`, `apps/camera-ios/` (new), spikes S2, S3, S7.

Not yours: core (contract PR), `apps/g2`, P2's controller. **Never** send `SetImage` directly. Publish
`OverlayFrame` on the hub. The compositor is the single writer to the image boxes. Shared-file changes
(glasses `drawn` acks, `ImageDrawn`, tile order, `SetImage.t`, `apps/camera`) need their own PR to `main`.

## Architecture (as built)

**iPhone app** (SwiftUI, xcodegen + xcodebuild; run steps in the README, details in `apps/camera-ios/README.md`):
- Default **World mode**: ARKit `ARWorldTrackingConfiguration` (rear camera + LiDAR) with
  `userFaceTrackingEnabled` (front TrueDepth), all in one gravity-aligned world frame. If the front face is
  not tracked it falls back to rear-camera Vision face angles + LiDAR depth; each pose says `face.src = front|rear`.
- Objects: Gemini box → server sends `locate` → LiDAR raycast (5×5 grid median) → world anchor, then followed
  with `VNTrackObjectRequest` + LiDAR re-sampling (`how = tracked|held`). A located object is a fixed world point:
  the tracker may only refine it by 8 cm; a disagreeing measurement is ignored and reported as `moved` (or `lost`),
  and the server re-locates (errors are sticky until then). Tap-to-place too. The app also streams JPEG to core `/ws/camera`, so Part 2 sees it as the scene camera.
- The app is the dev console (settings, schematic, tuning, status). There is no web debug page.

**Server** (`bluebook_interactive`; wire protocol in `wire.py` docstrings):
pose WS `/interactive/ws/pose` → `Tracker` (per-face-source noise estimate, IMU tilt blend, `HeadKalman`, face memory)
→ `Targets` → `Renderer` (ticks at the glasses' ack pace, predicts by measured latency, spring smoother, ring
with hold area / chevron at the edge, never hides, freezes on face loss) → `OverlayFrame`. The **IMU tilt blend**
replaces most of the camera's pitch/roll with the glasses' gravity (accelerometer only, ~10 Hz), weighted by noise;
yaw stays from the camera.
`optics.DisplayModel` = G2 spec prior; `BoresightLearner` learns only from the phone target (`name=camera`).

**Interfaces.** Consumes `StepChanged` (targets with name/aliases/visual_description). Publishes
`OverlayFrame(session, t, primitives)` in display px (visible band y=88..232 on `teach_page`); empty clears.
Routes under `/interactive/`: `ws/pose`, `step/{session}` (fake Part-2 `StepChanged`: `name=`, `anchor=`,
`step_id=`; none clears), `status`, `display/{session}`, `align/{session}`, `imu/{session}`, `mirror`,
`glasses.png`, `eye.jpg`. `setup(app)` returns the async tracking loop core runs for the server's lifetime.

## Diagnostics

`/interactive/status` has everything (`render.sessions.<sn>.projection` is the full chain). The raw session
(hello, pose, ack) is written to `data/diag/poses.jsonl` (overwritten each server start) and replayed with
`uv run python -m bluebook_interactive.replay data/diag/poses.jsonl`. `replay --record` captures a fresh
run (poses, IMU, draw acks) to `data/diag/run-<time>.jsonl` and prints metrics (jitter, lag, err, flips, max step).

## Camera placement guidance

Phone across the work area, screen toward the wearer, rear camera on the fixture, so the front TrueDepth
camera sees the face. Standing behind the phone degrades to the noisy rear-Vision face path (~1° vs ~0.08°).

## Open items

1. **Rear-camera yaw wander** (3-5° at ~1.3 m) is the main accuracy limit; replayed lag ~350 ms. Tune on recordings.
2. The overlay band is only ±2.7° vertical (144 px); using the full display height needs a shared `teach_page`/compositor change.
3. Tap-to-place tracks a tiny box (consider Vision saliency); the phone target is the rear lens, ~7 cm from the
   screen centre; moving a located object needs ~5 s to re-locate.
4. Tests that mount via core write into real `data/` when `.env` has `BB_INTERACTIVE=1`.
5. Spikes S2/S7, IMU pace codes (S3) and a full motion-to-photon breakdown still to write up in `docs/spikes.md`.

## Acceptance criteria (demo-grade)

- Static head: the target is inside the highlight ring (radius ≤ 80 px) as judged by the wearer. Region-level by design (DESIGN §6.3).
- Slow head turn (~20°/s): the highlight stays on the target or the chevron points the right way. It never confidently marks the wrong object.
- Measured motion-to-photon latency logged and written up in `docs/spikes.md`.
- Tests: `uv run pytest` green (no network or model calls); app: `xcodebuild test -scheme BluebookCamera -destination 'platform=iOS Simulator,name=iPhone 17 Pro'`.

## References

Gemini image understanding (`box_2d`): <https://ai.google.dev/gemini-api/docs/image-understanding>.
Grubert et al., *A Survey of Calibration Methods for OST-HMDs*: <https://arxiv.org/abs/1709.04299>.
