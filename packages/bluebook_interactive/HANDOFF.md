# Part 3 — as built (2026-10-18)

The current state of AR highlighting, for whoever picks it up next. The original plan is in
`workstreams/part3-interactive.md`; this file is what exists now and what's open. Measurements: `docs/spikes.md`.

## Pieces

**iPhone app, `apps/camera-ios/`** (SwiftUI, XcodeGen). It is the scene camera and the dev console.
- **World mode** (default): ARKit world tracking on the rear camera + LiDAR, with the front TrueDepth face in the same
  gravity-aligned metric frame. If the front face isn't tracked, it falls back to rear-camera Vision face angles plus
  LiDAR depth, which is about 10× noisier. Every pose says which (`face.src = front|rear`).
- **Objects:** the server sends `locate {id, box}`. The app samples LiDAR in a 5×5 grid inside the box and places a world
  anchor at the median. A located object is a fixed world point: the Vision tracker may refine it by at most
  `Coupling.refineM` (8 cm). A measurement that disagrees is ignored and reported as `moved` or `lost`, and the server
  locates it again. Tap-to-place works too, and tapped anchors are never re-located.
- Also streams JPEG frames to core `/ws/camera`, so Part 2's scene checks work with it.

**Server, `src/bluebook_interactive/`**, wire protocol in `wire.py` docstrings:

| Module | Job |
|---|---|
| `tracker.py` | Head pose: convention check, per-face-source noise estimate, IMU tilt blend, `HeadKalman`, face memory |
| `targets.py` | Step targets → points: grounding (Jev + Gemini, below), voting, world anchors held by `StaticPoint` |
| `vision.py` | The two grounding calls |
| `render.py` | Ticks at the glasses' draw-ack pace, predicts by measured latency, spring smoothing, ring or edge chevron |
| `optics.py` | `DisplayModel` from the G2 spec (f ≈ 1523 px); `BoresightLearner` learns only from the phone target |
| `replay.py` | Records sessions and replays them on a fake clock: jitter, lag, flips, max step |

**Grounding.** Every target is boxed by Jev (~100 ms, on every new frame; a box counts once 2 of the last 3 agree) and
by Gemini flash-lite (~1 s), whose answer replaces Jev's when it lands, for `GEMINI_HOLD_S`. Gemini can say "not
visible", which takes the highlight down (`untrack`, or `forget` in world mode). Jev always returns a box, so with the
part in a hand or inside the camera it boxed the nearest lookalike. If Gemini errors or has no key, Jev's answer
stands. Objects in a hand don't need highlighting.

**IMU tilt blend.** The glasses IMU is accelerometer only (~10 Hz, no gyro). Its gravity replaces most of the
camera's pitch and roll, weighted by measured noise (`IMU_TILT_SIG_DEG`); yaw stays from the camera.
`/interactive/status` shows the weight (`pose.filter.imu_tilt_weight`) and the full projection chain.

## Measured

- Rear Vision face at ~1.3 m: yaw ~1°/frame, wandering 3–5°; front TrueDepth ~0.08°.
- Draw round trip ~130 ms; replayed motion-to-photon lag ~350 ms on the 45 s regression clip
  (`tests/rear_imu_clip.jsonl.gz`).
- Overlay band: 144 px, about ±2.7° vertically.

## Open

- **Rear-camera yaw wander** is the main accuracy limit; the IMU only fixes pitch and roll.
- The overlay band is half the display height. Using all of it needs a shared `teach_page`/compositor change.
- Tap-to-place tracks a tiny box (Vision saliency could find the object's extent).
- The phone target is the rear lens, about 7 cm from the screen centre.
- IMU pace codes other than 100 are unmeasured (spike S3); spikes S2 and S7 still need writing up.
- Tests that mount through core write into the real `data/` when `.env` has `BB_INTERACTIVE=1`.

## Checks

`uv run pytest packages/bluebook_interactive`; app: `xcodebuild test -scheme BluebookCamera -destination
'platform=iOS Simulator,name=iPhone 17 Pro'`.
