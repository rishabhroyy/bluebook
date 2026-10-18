# Part 3 handoff (2026-10-18)

State of `part-3-3d` for the next agent. Nothing is committed. Read AGENTS.md and workstreams/part3-interactive.md
first; this file records the implementation as built, what changed last, and what is open.

## Architecture as built (differs from the original brief: owner's decisions)
- **No MediaPipe, Depth Pro, SPAAM or web debug page**: all removed at the owner's request.
- **Native iPhone app `apps/camera-ios/`** (SwiftUI, xcodegen, team 3Y435S5YAR) is the scene camera and the dev console.
  - Default **World mode**: ARKit `ARWorldTrackingConfiguration` (rear camera + LiDAR) with `userFaceTrackingEnabled`
    (front TrueDepth face), everything in one gravity-aligned ARKit world frame. If the front face is not tracked it
    falls back to rear-camera Vision rev-3 face angles + LiDAR depth (`RearFace.swift`), `face.src = front|rear`.
  - Objects: Gemini box → `locate` → LiDAR 5×5 grid median → world anchor. A located object is a **fixed world
    point**: the Vision tracker (`VNTrackObjectRequest`, `Tracking.swift`) may only refine it by `Coupling.refineM`
    (8 cm); a disagreeing measurement is ignored and reported as error `moved` (`lost` as before). Tap-to-place too.
  - Also streams JPEG to core `/ws/camera` (Part 2 compatible). Pose JSON protocol: `wire.py` docstrings.
  - Install: `cd apps/camera-ios && xcodegen && xcodebuild -scheme BluebookCamera -destination 'id=<UDID>'
    -allowProvisioningUpdates -derivedDataPath build build && xcrun devicectl device install app --device <UDID>
    build/Build/Products/Debug-iphoneos/BluebookCamera.app` (owner's UDID 00008150-001A4CD12130401C).
- **Server package** (`src/bluebook_interactive/`): `wire` (pose WS) → `tracker.Tracker` (conventions check, x-mirror only
  for source `arkit`, per-face-source `NoiseEstimator`, IMU tilt blend, `HeadKalman` (`nis_gate` 3, `motion_tau` 0.25),
  face memory) → `targets` (world anchors → `StaticPoint` hold; Gemini locate flow; app errors sticky until that
  anchor is re-located, retried every `ERROR_RETRY_S` = 5 s; tapped anchors are never re-located)
  → `render.Renderer` (ticks at the glasses' ack pace, predicts by measured latency, `ScreenSmoother` spring
  (`SETTLE_TICKS` 3), ring/chevron, never hides, freezes on face loss) → `OverlayFrame`.
  `optics.DisplayModel` = G2 spec prior (f≈1523 px), `BoresightLearner` learns only from the phone target.
- **IMU tilt blend** (`tracker._imu_blend`, `IMU_TILT_SIG_DEG` 0.5): in world mode the glasses IMU gravity
  (accelerometer only, ~10 Hz, no gyro) replaces most of the head pose's pitch/roll, inverse-variance weighted (rear
  camera ~2-3° pitch noise → IMU weight ~0.8-0.96); yaw stays from the camera. IMU calibration (`imu.py`) learns only
  from raw camera poses. Status: `pose.filter.imu_tilt_weight`.
- **Renderer marks**: ring shows up to one ring radius outside the band (`ACTIVATE_PX`, drawn clamped to the band
  edge); `RING_HOLD_PX` (0.6 ring radius): the ring holds still while the target is inside, then glides; chevron uses a
  noise dead-band (`K_SIGMA` 3). Ring↔chevron hysteresis `HYST_MIN_PX` 60; the switch snaps (no glide).
- Status JSON `/interactive/status` has everything (render.sessions.<sn>.projection is the full projection chain).

## Fixed in the latest session
1. Delay/jitter/flip-flop on the rear-camera path: IMU tilt blend and the renderer changes above, tuned by replay.
2. Object tracking drift (recorded 2026-10-18): the Vision tracker re-acquired the background and moved the anchor
   1.8 m; a box stuck in the image dragged the point with the phone. Now bounded by `refineM`, errors are sticky on the
   server, so lost/moved objects get a fresh Gemini locate.
3. Tooling: `uv run python -m bluebook_interactive.replay --record` records until Ctrl-C (spoken prompts), writes
   `data/diag/run-<time>.jsonl` (poses, IMU, draw acks) and prints metrics. `replay <file>` replays any recording
   through Tracker+Renderer on a fake clock: jitter, lag, err, flips, max step per face source. A regression test
   replays a 45 s real rear-camera clip (`tests/rear_imu_clip.jsonl.gz`).
4. Measured (docs/spikes.md): rear Vision face at ~1.3 m wanders 3-5° yaw (~1°/frame); draw RTT ~130 ms; replayed lag
   ~350 ms on the clip. The owner uses the rear camera (behind the phone); front TrueDepth works too.

## Shared files changed (need separate PRs to main per AGENTS.md)
`packages/bluebook_core/{protocol.py,server.py,display.py,glasses.py}` + `apps/g2/src/main.ts` + generated
`protocol.ts`/`contracts/` (glasses `drawn` acks + `ImageDrawn` hub event + content-first tile order + `SetImage.t`),
`apps/camera/index.html` (front/back buttons, 10 fps), `apps/camera-ios/` (new app). `docs/spikes.md` is new.

## Open items
- **Rear-camera yaw wander** (3-5° at ~1.3 m) is the main accuracy limit; the IMU fixes pitch/roll only.
- The overlay band is only ±2.7° vertical (144 px of the 288 px display). Using the full display height needs a shared
  `teach_page`/compositor change (contract PR to `main`).
- Tap-to-place tracks a tiny box; consider Vision saliency to get the object box.
- The phone target is the rear lens, ~7 cm from the screen centre.
- Moving a located object takes ~5 s to re-locate (`ERROR_RETRY_S` + Gemini).
- IMU pace codes other than 100 are untested (spike S3); spikes S2/S7 and motion-to-photon still to write up.
- Tests that mount via core write into real `data/` when `.env` has `BB_INTERACTIVE=1` (core mounts first).

Checks: `uv run pytest -q` (61 pass), `uv run ruff check . && uv run ruff format --check .`; app: `xcodebuild test
-scheme BluebookCamera -destination 'platform=iOS Simulator,name=iPhone 17 Pro'` (35 pass).
