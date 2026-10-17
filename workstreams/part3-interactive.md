# Part 3 — Interactive AR highlighting (experimental)

Branch: `part3-interactive` · Package: `packages/bluebook_interactive/` · Enabled only with `BB_INTERACTIVE=1`
Read first: `AGENTS.md`, `docs/DESIGN.md` §1, §5, §6.3, §8

## Goal

While the learner is on a step, highlight the step's **target object** (e.g. the water shutoff valve)
in the glasses so that the highlight stays on the real object as they move their head. A fixed iPhone
camera (tripod or bird's-eye) watches the scene.

This part is experimental. If you don't have it, Part 2 still works fully. Optimise for learning what
works, and record measurements in `docs/spikes.md`.

## Hard constraints (verified; see DESIGN §1)

- The G2 has **no camera**. Track the wearer's head from outside, from the scene camera.
- **No markers of any kind:** no AprilTags, ArUco/ChArUco, stickers, or clips on the glasses or in the
  scene. Every pose and depth estimate comes from plain video with learned models. This is a product
  decision, not open for debate. If accuracy falls short, report the measurements rather than adding markers.
- The IMU reports `x,y,z` with **undocumented units/axes**. Characterise it before relying on it (spike S3).
- Display: 576×288, overlays only through `OverlayFrame` → core `Compositor` → image boxes. Updates
  are ~105–185 ms each and sequential, so you get **~5 fps** for one 288×144 tile and fewer for two.
- G2 optics (FOV, virtual image distance, eye placement) are **not published**, so calibrate them.

## You own

- `packages/bluebook_interactive/**`, `data/calib/*` formats, spikes S2, S3, S7.
- The calibration UX (a temporary `Controller` you `conn.push()` and `conn.pop()`).

Not yours: core (contract PR), `apps/**`, P2's controller. **Never** send `SetImage` directly. Publish
`OverlayFrame` on the hub. The compositor is the single writer to the image boxes.

## Interfaces

- **Consume:** `hub.subscribe(StepChanged)` (targets with name/aliases/visual_description/action),
  `hub.subscribe(CameraFrame)` / `hub.last(CameraFrame, "scene")`, `hub.subscribe(ImuSample)` (IMU is off
  until you send `conn.control(imu_pace=100)`; coordinate with P2, who owns the session, by turning
  it on when you get `StepChanged` with targets and off when they're cleared), plus
  `store.load(skill_id).object(id).appearances` for exemplar crops.
- **Produce:** `hub.publish(OverlayFrame(session, t=frame.t, primitives=[Ring|Rect|Arrow|Label]))` in display px
  (only y=88..232 is visible on `teach_page`). Publish an empty `primitives` to clear.
  Optional: `hub.publish(SceneObservation(...))`, which P2 may use for "it's on your left".
- **Routes in `setup(app)`:** `POST /interactive/calibrate/{session}` (starts SPAAM on that session's
  glasses via `glasses.sessions[session].push(CalibController())`), and `GET /interactive/debug` (latest
  camera frame annotated with face landmarks, head-pose axes, target boxes + depth, and projected points). The debug view is the main dev tool.
- **Background loop:** `setup(app)` returns your `async def run()`. Core runs it as a task for the
  server's lifetime (see `server.py`).

## Pipeline (recommended)

1. **Camera intrinsics (marker-free):** script `bluebook-calib-camera` that runs Apple Depth Pro
   (`apple/ml-depth-pro`, which estimates focal length and metric depth) on ~30 frames of the
   `apps/camera` stream (`?w=1280`, fixed) and stores the median focal length →
   `data/calib/camera_<id>.json {fx, fy, cx, cy, width, height, n, focal_std}`. Assume principal point
   = image centre and no distortion. Re-check `focal_std` and warn if > 3%.
2. **Head pose** (the glasses are rigid on the head, so head pose stands in for glasses pose):
   - MediaPipe Face Landmarker (`output_facial_transformation_matrixes=True`) for 478 landmarks →
     `solvePnP` against the canonical face model with the intrinsics → `T_cam←head`. Rescale the
     translation using Depth Pro's median depth over the face landmarks. Run Depth Pro at a low rate
     (~1 Hz) and cache a scale factor, because it's ~0.3 s/frame on GPU and slower on a laptop.
   - When the face isn't detected (turned away), use 6DRepNet360 (`thohemp/6DRepNet360`) for rotation,
     plus MediaPipe Pose head keypoints and depth for position. Mark the pose `low_confidence`.
   - Measure how much the G2 frames degrade face landmarks (spike S7). Expect partial occlusion
     around the eyes, so prefer the nose/jaw/cheek landmarks in PnP.
   - Filtering: constant-velocity / One-Euro on the pose. If S3 shows the IMU gives orientation or
     gyro data, run a complementary filter (IMU fast rotation, camera absolute correction) and estimate
     the camera↔IMU rotation offset during calibration.
3. **Grounding:** on `StepChanged`, run the detector VLM on the newest frame: "Find: {name} (aka
   {aliases}). Looks like: {visual_description}." Return `box_2d`, `[ymin,xmin,ymax,xmax]`/1000. Include
   exemplar crops from `appearances` as extra images. Re-ground every ~3 s, or when the box moves.
   3D point = box centre back-projected at the **median Depth Pro depth inside the box** (targets are
   usually static fixtures, so cache it). Track between VLM calls with a cheap 2D tracker only if moving
   targets matter.
4. **Display calibration (SPAAM, markerless):** the calibration controller shows a crosshair at known
   display px (via `OverlayFrame` → compositor; the visible band is y 88..232, so put instructions in
   `tip`). The user puts their **index fingertip** exactly behind the crosshair and taps (the other
   hand or the ring). Fingertip 3D = MediaPipe Hand Landmarker tip pixel + Depth Pro depth, using
   the median of a few frames around the tap. Collect ≥12 points: 4 crosshair positions × 3 arm
   extensions, for depth variance (Moser et al.). Then solve a 3×4 DLT `P` (head frame → display px),
   report reprojection error, and save `data/calib/display_<glasses_sn>.json`. Alternative point
   source: a VLM-grounded static object point. Re-run when the glasses are re-seated or a different
   person wears them.
5. **Render loop:** per camera frame: head pose → transform the cached target point into the head frame →
   `P` → pixel. If inside the visible band: `Ring` with radius ≥ max(60 px, 2σ of the current error
   estimate), plus a short `Label`. If outside: `Arrow` at the band edge pointing towards it. Publish only if
   it moved > 6 px or the state changed. Hide while head angular velocity is high or the pose is
   `low_confidence`.
6. **Fallback (no face/head pose, or not calibrated):** publish nothing, and publish `SceneObservation`
   so P2 can say "valve is to your left" as text.

## Camera placement guidance (put in the setup instructions)

The camera has to see **the target area and the wearer's face**, within ~±60° yaw: e.g. a tripod 1–2 m
away, across the work area, angled to cover both. A pure top-down bird's-eye view doesn't see the face.
It still works for Part 2 scene checks but degrades Part 3 to the 6DRepNet360 path.

## Milestones

1. **M1:** spikes S2/S3/S7 plus the marker-free intrinsics script. Debug page shows live face landmarks
   and head-pose axes with G2 worn, and logs per-frame pose confidence.
2. **M2:** grounding of fixture targets on the debug page (boxes + depth), and the cached 3D anchor point.
3. **M3:** fingertip SPAAM calibration on glasses, with a static check: does a projected fingertip
   or object point land under the crosshair when the head is still?
4. **M4:** a live highlight following the target with the fixture skill driven by P2 (or a fake
   `StepChanged` publisher script before P2 is merged).

## Acceptance criteria (demo-grade)

- Static head: the target is inside the highlight ring (radius ≤ 80 px) as judged by the wearer after
  calibration. Report median error in px and the head-pose rotation error against the IMU, if S3
  makes that possible. Markerless head pose is ~3–5° error on benchmarks, so this is region-level by
  design (DESIGN §6.3).
- Slow head turn (~20°/s): the highlight stays on the target or hides itself. It never confidently
  points at the wrong object.
- Measured motion-to-photon latency logged (camera frame t → `OverlayFrame` → `SetImage` sent) and
  written up in `docs/spikes.md`.
- Tests: DLT solve on synthetic correspondences (known P recovered, including with 3° injected
  rotation noise), back-projection with depth, and arrow placement for off-screen targets. `uv run pytest` green.

## References

Grubert et al., *A Survey of Calibration Methods for OST-HMDs* (SPAAM and variants, error sources),
<https://arxiv.org/abs/1709.04299>. Gemini image understanding (`box_2d`):
<https://ai.google.dev/gemini-api/docs/image-understanding>. SAM 3 for local text-prompted
segmentation and tracking if VLM latency is too high: <https://github.com/facebookresearch/sam3>.
MediaPipe Face/Hand Landmarker: <https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker>.
6DRepNet360 (full-range head pose): <https://arxiv.org/abs/2309.07654>, <https://github.com/thohemp/6DRepNet360>.
Depth Pro (metric depth + focal length from one image): <https://arxiv.org/abs/2410.02073>, <https://github.com/apple/ml-depth-pro>.
