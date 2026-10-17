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

- The G2 has **no camera**. Track the glasses from outside (the external camera sees fiducials on the glasses).
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
  camera frame annotated with tag poses, boxes, and projected points). The debug view is the main dev tool.
- **Background loop:** `setup(app)` returns your `async def run()`. Core runs it as a task for the
  server's lifetime (see `server.py`).

## Pipeline (recommended)

1. **Camera intrinsics:** script `bluebook-calib-camera` that captures ~20 ChArUco views from the
   `apps/camera` stream (`?w=1280`, fixed) and runs `cv2.calibrateCamera` →
   `data/calib/camera_<id>.json {K, dist, width, height, rms}`. Accept if RMS < 1 px.
2. **Fiducials:** tag36h11 AprilTags (`pupil-apriltags` or OpenCV ArUco/AprilTag) on a light rigid clip on
   the G2 (both temples, or a brow bar if the camera is overhead). A world tag on the work surface or
   wall defines the anchor plane. Pose via `solvePnP` (IPPE_SQUARE). Smooth with a short constant-velocity
   filter on SE(3) (or a One-Euro filter per component).
3. **Grounding:** on `StepChanged`, run the detector VLM on the newest frame: "Find: {name} (aka
   {aliases}). Looks like: {visual_description}." Return `box_2d`, `[ymin,xmin,ymax,xmax]`/1000. Include
   exemplar crops from `appearances` as extra images. Re-ground every ~3 s, or when tracking confidence
   drops. Targets are usually static fixtures, so a 3D point (box centre ray ∩ anchor plane, offset by
   estimated depth) is enough for v1. Track between VLM calls with a cheap 2D tracker only if moving
   targets matter.
4. **Display calibration (SPAAM):** the calibration controller shows a crosshair at known display px
   (via `OverlayFrame` → compositor; the image band limits y to 88..232, so put text instructions
   in `tip`). The user aligns it with the world tag's centre and taps. Collect ≥12 points across 3 depths
   (Moser et al. found that depth variance improves accuracy), solve a 3×4 DLT `P` (tag-frame → display px),
   report reprojection error, and save `data/calib/display_<glasses_sn>.json`. Re-run if the clip moves.
5. **Render loop:** per camera frame: tag pose → `T_cam←tag` → transform the target point into the tag frame → `P` →
   pixel. If inside the visible band: `Ring` with radius ≥ max(24 px, 2σ of the reprojection error).
   If outside: `Arrow` at the band edge pointing towards it, plus a `Label`. Publish only if moved > 4 px or the
   state changed. Hide during fast head motion (tag angular velocity > threshold, or IMU magnitude
   if S3 allows).
6. **Fallback (no tag visible / not calibrated):** publish nothing, and publish `SceneObservation` so P2
   can say "valve is to your left" as text.

## Milestones

1. **M1:** spikes S2/S3/S7 plus the camera intrinsics script. On the debug page: tag pose axes drawn live.
2. **M2:** grounding of fixture targets on the debug page (boxes), and the 3D anchor point.
3. **M3:** SPAAM calibration on glasses, with a static crosshair test (does the projected point sit on the
   world tag when the head is still?).
4. **M4:** a live highlight following the target with the fixture skill driven by P2 (or a fake
   `StepChanged` publisher script before P2 is merged).

## Acceptance criteria (demo-grade)

- Static head: the highlight centre is within 30 px (~1/20 of the display width) of the target, as
  judged by the wearer, after calibration.
- Slow head turn (~20°/s): the highlight stays on the target or hides itself. It never confidently
  points at the wrong object.
- Measured motion-to-photon latency logged (camera frame t → `OverlayFrame` → `SetImage` sent) and
  written up in `docs/spikes.md`.
- Tests: DLT solve on synthetic correspondences (known P recovered), projection/box maths, and arrow
  placement for off-screen targets. `uv run pytest` green.

## References

Grubert et al., *A Survey of Calibration Methods for OST-HMDs* (SPAAM and variants, error sources),
<https://arxiv.org/abs/1709.04299>. Gemini image understanding (`box_2d`):
<https://ai.google.dev/gemini-api/docs/image-understanding>. SAM 3 for local text-prompted
segmentation and tracking if VLM latency is too high: <https://github.com/facebookresearch/sam3>.
