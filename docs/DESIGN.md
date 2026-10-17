# bluebook — Design Document (cycle 1)

Status: draft for cycle 1 · Owner: Rishabh · Last updated 2026-10-17

bluebook lets someone learn a hands-on task (first target: blue-collar / trades tasks) through
short, glanceable instructions on **Even Realities G2** glasses instead of a long video. An expert
records the task once on an iPhone. bluebook turns that recording into a structured **skill**, then
teaches it back step by step on the glasses. The learner can ask questions by voice, and an optional
scene camera lets the AI see what they're doing.

Cycle 1 has three parts, built in parallel on separate branches and joined through the contracts
in `packages/bluebook_core`. Each developer creates their own branch from `main`; the names below
are suggestions.

| Part | Suggested branch | What it does | Status |
|---|---|---|---|
| 1. Perception / learning | `part1-perception` | iPhone video **and its audio** → `SkillPackage` on disk | core |
| 2. Playback / teaching | `part2-teaching` | Skill library on glasses, step-by-step tooltips, voice Q&A, optional scene camera | core |
| 3. Interactive (experimental) | `part3-interactive` | Scene camera + glasses pose → AR highlights that stay on the target object | flag `BB_INTERACTIVE=1` |

Per-part briefs for implementing agents: `workstreams/part{1,2,3}-*.md`. How the parts join up:
`docs/INTEGRATION.md`.

---

## 1. Platform facts that drive the design

Everything below was checked against official docs or the SDK typings (`@evenrealities/even_hub_sdk@0.0.16`)
on 2026-10-17, unless marked *community*. Sources are in §13.

| Fact | Consequence for bluebook |
|---|---|
| **The G2 has no camera and no speaker.** Input: 4-mic array (one 16 kHz s16le mono stream), touchpads on both temples, optional R1 ring, IMU. | Every "seeing" job uses an **external iPhone camera**. All output is visual. Part 3 can't track from the glasses' own view, so it tracks the **wearer's head** from outside, **marker-free** (no AprilTags or other fiducials). |
| **The app runs in a WebView inside the Even Realities phone app.** The glasses only render containers and send input over BLE. Apps are HTML/TS built with Vite. | The "G2 app" is a web page on the phone. Heavy work happens on the laptop server. The phone needs Tailscale to reach it. |
| **Display: 576×288, 4-bit greyscale (green), black is see-through.** No DOM: at most **4 image + 8 text/list containers**, absolute positions, exactly one input-capture container. Images go up to **288×144** per container. Text is up to 1000 chars on build and 2000 on update. One built-in proportional font with no size control. | We use **server-driven UI**: the server sends container layouts and the app only renders them (§5.2). Images can't cover the whole screen except as 4 tiles. Overlays live in fixed image "bands". |
| **Update costs (community, measured):** text update ~83 ms, page rebuild ~165 ms (may flicker), image ~104 ms + 3.9 ms/KB, ~9.5 fps ceiling for one small image, ~5.4 fps for 288×144. Image sends can't overlap. | Tooltips use in-place text updates. Rebuilds happen only on screen changes. AR overlays get **~5 updates/s at best**, so registration has to tolerate latency (§6.3). The client serialises writes and drops stale ones. |
| **Network:** `fetch`/WebSocket from the WebView. Destinations must be on the `app.json` `network` whitelist as full origins (no wildcards), **and** CORS applies. HTTPS is recommended. Plain http only works for LAN dev. | One stable origin: `https://<laptop>.<tailnet>.ts.net` via `tailscale serve`, with a valid cert. CORS is enabled on the server. Whitelisting `wss://` isn't documented → spike S4. |
| **Lifecycle:** on iOS the WebView keeps running while the phone is locked. On Android it may be killed, which drops sockets and stops the mic. The root page must exit with `shutDownPageContainer(1)` on double-tap or the store rejects the app. | The client reconnects with backoff and resends `hello`. The session is keyed by glasses serial, so a reconnect resumes the same lesson. The server decides when to exit. |
| **IMU:** `imuControl(on, P100..P1000)` gives `x,y,z` floats. **Units and axes are undocumented**, and the pace values are "protocol codes, not Hz". | We can't count on the IMU for head pose on its own. Part 3 characterises it (spike S3) and, if it's usable, fuses it with camera head pose for motion gating and smoothing. |
| **Gestures (SDK 0.0.16):** click, double click, scroll top/bottom boundary, **long press + release**. Quirks: click can arrive as `undefined`, list index 0 can be missing, the simulator and hardware send different event shapes. | Long press is push-to-talk everywhere. The client normalises the quirks once (`apps/g2/src/main.ts`). |

## 2. Users and core flows

**Expert (author):** records the task on an iPhone, handheld or head-mounted, talking through it as
they work → uploads it with `bluebook-ingest` (or a web upload later) → reviews the generated steps
(optional, by editing the JSON in cycle 1).

**Learner (wearer):**
1. Opens bluebook on the glasses and picks a skill from the library list.
2. Sees an overview: tools needed and safety notes. Tap to start.
3. For each step, a **tooltip** sits at the top of the view ("Turn the shutoff valve clockwise until it
   stops."), with the step number and status at the bottom. The middle band shows a reference keyframe
   from the expert's video, or AR highlights in Part 3.
4. Tap → next step. Double-tap → back (exits at the root). Swipe → more detail for this step
   (instruction, substeps, tips, warnings).
5. Hold the touchpad and speak ("which way is closed?"). The tutor answers in a few lines and can
   change course: go back, add a substep, or explain a different way.
6. With a scene camera on a tripod, the tutor can check the step ("looks done — tap to continue") or
   flag a likely mistake ("that's the hot valve; the cold line is leaking").

## 3. Architecture

```
   iPhone A (expert)                     Laptop  (dev server, behind `tailscale serve` → https://<host>.ts.net)
   ┌──────────────┐  video file  ┌───────────────────────────────────────────────────────────────────────────┐
   │ Camera app   │─────────────▶│  bluebook_perception (Part 1)  ──writes──▶  data/skills/<id>/skill.json   │
   └──────────────┘ (AirDrop/CLI)│        │ Gemini File API (video+audio)            embeddings.npy, media/  │
                                 │        ▼                                                ▲ read            │
                                 │  bluebook_core: config · models · store · hub · display · protocol        │
   iPhone B (wearer's phone)     │        ▲ WS /ws/glasses                     ▲ hub events                   │
   ┌──────────────────────────┐  │  ┌─────┴──────────────┐   StepChanged  ┌─────┴──────────────────────┐      │
   │ Even Realities app       │◀─┼─▶│ GlassesConnection   │──────────────▶│ bluebook_interactive (P3)   │      │
   │  └ WebView: apps/g2      │  │  │  └ Controller       │◀──────────────│  pose + grounding → overlay │      │
   │     (thin client)        │  │  │     = bluebook_teach│  OverlayFrame │                             │      │
   └──────────┬───────────────┘  │  │       (Part 2)      │  (compositor) └─────▲───────────────────────┘      │
              │ BLE              │  └─────▲──────────────┘                     │ CameraFrame                   │
        ┌─────▼─────┐            │        │ CameraFrame (hub)                  │                               │
        │  Even G2  │            │  WS /ws/camera ◀────────────────────────────┘                               │
        └───────────┘            └──────────▲────────────────────────────────────────────────────────────────┘
                                            │ JPEG frames (wss)
                                 iPhone C (tripod / bird's-eye) — Safari: https://<host>.ts.net/camera
```

**Deployment (cycle 1):** one Python process (`uv run bluebook-server`) on the laptop. `tailscale serve`
terminates TLS for `https://<host>.<tailnet>.ts.net`. The wearer's phone and the scene-camera phone are
both on the tailnet. Models run on cloud inference (Gemini API). Nothing runs on the glasses beyond the
firmware.

**Why one process:** the parts exchange high-rate in-memory data (camera frames, overlays). A single
asyncio process with an in-memory hub (`bluebook_core.hub`) is simplest and gives the lowest latency.
The hub's API (`publish`/`subscribe`/`last`) was chosen so it can be backed by Redis later without
touching the parts.

## 4. Model choices

| Role (`config.Models`) | Default | Why |
|---|---|---|
| `video_analyst` (P1) | `gemini-3.8-flash` | Gemini is the mainstream API that takes **video with its audio track** in one request, can refer to timestamps, and supports per-request fps, clipping (`video_metadata.start/end_offset`) and `media_resolution`. Default sampling is 1 fps (too coarse for hand work), so P1 re-runs per-step clips at a higher fps. |
| `synthesizer` (P1) | `gemini-3.8-flash` | Text-heavy passes: FAQ, safety, consistency checks. |
| `tutor` (P2) | `gemini-3.8-flash` | Takes the learner's **audio question directly** (no separate STT hop) plus the skill context, and returns structured JSON. |
| `scene` (P2) | `gemini-3.5-flash-lite` | Frequent cheap frame checks against `success_criteria`. |
| `detector` (P3) | `gemini-3.8-flash` | Open-vocabulary 2D boxes (`box_2d` = `[ymin,xmin,ymax,xmax]` normalised 0–1000) from name + visual description. SAM 3 (open weights, text-prompted segmentation + video tracking) is the local alternative if latency or cost needs it. |
| `embedder` (all) | `gemini-embedding-2-preview` | Multimodal embeddings (text, image, video, audio), so keyframes and text share one space. |

- Model ids change often (they did several times this year). They live **only** in `config.py`/env.
  Every call goes through `bluebook_core.models.generate/embed`, which handles retries and logs
  latency and tokens.
- Claude (`claude-opus-5-5` / `claude-sonnet-5-5`) is a reasonable swap for the **text-only** tutor
  reasoning if it proves better at teaching dialogue. It can't take raw video or audio, so the
  perception roles stay on Gemini. To add it, branch inside `models.generate()`; don't add a provider
  abstraction.
- Realtime voice: a Live API session (`gemini-3.8-live`) is the upgrade path if push-to-talk round
  trips feel slow. The glasses' 16 kHz PCM matches the format Live expects. Not in cycle 1: the G2
  has no speaker, so audio-out is wasted, and one request per utterance is simpler.
- Gemini File API uploads are temporary (auto-deleted after about 48 h). `source/` in the package
  is the durable copy.

## 5. Shared core (`packages/bluebook_core`) — already written on `main`

Rule: **anything used by two or more parts lives here and nowhere else.** Parts import from it and
never copy from it.

| Module | Contents | Main users |
|---|---|---|
| `skill.py` | `SkillPackage` schema (pydantic) + `check()` referential integrity | P1 writes; P2, P3 read |
| `store.py` | `save/load/list_skills/search/media_path` for `data/skills/<id>/`; cosine search over `embeddings.npy`, keyword fallback | all |
| `models.py` | `generate(role, contents, schema=...)`, `embed(...)`, `upload(path)`, `video(file, fps, start, end)`, `image()`, `audio_pcm16k()` | all |
| `config.py` | env vars + `./.env` (run from repo root), model id per role, feature flags | all |
| `protocol.py` | wire messages (glasses + camera WS), hub event models, `ShowPage` validation against SDK limits | all + G2 app |
| `hub.py` | in-process pub/sub, `last()` for late joiners; events: `StepChanged`, `OverlayFrame`, `CameraFrame`, `ImuSample`, `SceneObservation` | P2, P3 |
| `display.py` | shared page layouts (`teach_page`, `list_page`, `message_page`), PNG quantising, keyframe `fit()`, overlay `Compositor` | P2, P3 |
| `glasses.py` | `GlassesConnection` (send page/text/image/control, push/pop controller), `Controller` base, `EchoController` default | P2, P3 |
| `server.py` | FastAPI app: `/ws/glasses`, `/ws/camera`, `/skills…`, `/camera`, mounts each part's `setup(app)` | all |

Also shared:
- `apps/g2/` — the **only** glasses app: a thin client (~200 lines) that renders `ShowPage/SetText/SetImage`
  and forwards `Input/Audio/Imu/Lifecycle`. P2 and P3 change glasses behaviour **from Python**.
- `apps/camera/index.html` — the **only** scene-camera client (Safari `getUserMedia` → JPEG over WSS).
- `contracts/*.schema.json`, `apps/g2/src/protocol.ts` — generated by `scripts/gen_contracts.py`.
- `fixtures/skills/fix-leaky-compression-fitting/` — a hand-written skill so P2 and P3 can start
  before P1 ships.

### 5.1 Skill package (P1 → P2/P3 contract)

See `skill.py` for the authoritative version. Highlights:

- **Steps are the spine.** Each `Step` has a ≤120-char `tooltip` (the glasses line), a full
  `instruction`, `why`, `substeps`, `success_criteria` (observable checks for the scene VLM),
  `common_mistakes`, `tips`, `warnings`, verbatim expert `narration`, source-video `t_start/t_end`,
  `keyframes`, and an optional `clip`.
- **Objects are first-class.** `ObjectRef` has `name`, `aliases`, `visual_description`, and
  `appearances` (keyframe + box). Steps reference objects with a **role**: `target` is what the user
  acts on (Part 3 highlights it), plus `tool`, `material`, `context`.
- **Provenance on every claim:** `observed` (seen in video), `narrated` (expert said it), `inferred`
  (model added it). The tutor must flag `inferred` safety or measurement info.
- **Task graph light:** `depends_on` + `optional` allow non-linear procedures without a graph engine.
- **Retrieval:** `chunks[]` (step, transcript window, FAQ, object, keyframe caption, safety). Row *i* of
  `embeddings.npy` is the vector for `chunks[i]`. `embedding.model` must equal the configured
  embedder, otherwise `search` raises.
- Versioning: `schema_version` major must match. Changes follow `docs/INTEGRATION.md`.

### 5.2 Server-driven glasses UI (P2/P3 ↔ G2 contract)

The G2 app has no product logic. The server sends:

- `ShowPage{boxes}` → first `createStartUpPageContainer`, then `rebuildPageContainer` (expensive; only on
  screen change)
- `SetText{id,name,content}` → `textContainerUpgrade` (cheap; for tooltips and status)
- `SetImage{id,name,png_b64}` → `updateImageRawData` (serialised, coalesced per container)
- `Control{mic, imu_pace, exit}`

The app sends `Hello`, `Input{kind: tap|double_tap|scroll_up|scroll_down|long_press|long_press_release|list_select}`,
binary PCM between `AudioStart`/`AudioEnd` (push-to-talk on long press, handled client-side), `Imu`,
`Lifecycle`, `DeviceStatus`, and `Ping`.

Why server-driven: there's one place to change UX (Python), and P3 overlays and P2 tooltips share one
render path. The app hardly changes, so we rarely re-sideload or re-submit it. The cost is a network
round trip per interaction, a few tens of ms on a tailnet, which is small next to the 83–165 ms
display costs.

**Layouts** (`display.py`, ids and names are part of the contract):

```
teach_page   y=0   ┌──────────────── tip (text, id 4) ─────────────────┐  tooltip, ≤3 lines
             y=88  ├──── img_l (image 288×144, id 2) ─┬─ img_r (id 3) ─┤  keyframe (P2) or AR overlay (P3)
             y=240 ├────────────── status (text, id 5, dim) ───────────┤  "Step 2/5 · ● listening · ✓ looks done"
             y=288 └───────────────────────────────────────────────────┘  bg (text, id 1, full screen, capture, z=0)
list_page    title (id 1) + native list (id 2, capture, ≤20 items × 64 chars)
message_page one full-screen capture text (id 1), firmware scrolls overflow
```

Image boxes have the highest z below the text, and overlays draw only in the y = 88..232 band.
Whether image black pixels hide the text underneath is unknown (spike S1), so text and images don't
overlap in v1.

## 6. Part summaries (full briefs in `workstreams/`)

### 6.1 Part 1 — Perception / learning pipeline

Input: one or more iPhone videos (handheld/moving, with narration and ambient audio). Output: a
validated `SkillPackage`.

Pipeline (each stage caches its output under `data/work/<id>/` so it can be re-run):

1. **Ingest:** hash; ffprobe; normalise to H.264 ≤1080p and keep audio; extract 16 kHz WAV.
2. **Transcribe** with word timestamps (local `mlx-whisper`/`faster-whisper` large-v3 for exact
   timing, or Gemini). This gives the narration that's later aligned to steps.
3. **Global pass** (whole video + audio, 1 fps): task title/summary, step outline with
   `t_start/t_end`, tools, materials, safety, and **audio cues** (the hiss stops, the click of a
   fitting).
4. **Per-step pass** (clip with `start/end_offset`, 2–5 fps, higher `media_resolution`): full step
   fields, success criteria, mistakes, substeps, object roles and visual descriptions, and candidate
   keyframe timestamps.
5. **Keyframes:** extract frames near the model-picked timestamps. Choose the sharpest in ±0.5 s
   (variance of Laplacian), because handheld footage blurs. Optionally ground objects in keyframes
   (`box_2d`) to fill `ObjectRef.appearances` (P3 uses these as exemplars).
6. **Synthesis + verification:** FAQ (anticipated learner questions), prerequisites, and a checker
   pass that confirms each claim against transcript/timestamps and sets `provenance`/`confidence`.
7. **Index:** build `chunks[]` and embed (text + keyframe images). Then `store.save()`, which
   validates.

Quality bar: tooltips that a novice can act on without other context, and no invented
safety-critical numbers. Eval set: 3–5 recorded tasks with hand-labelled step boundaries (see brief).

### 6.2 Part 2 — Playback / teaching

A `TeachController` (subclass of `bluebook_core.glasses.Controller`) registered in `setup()`.

- **Screens:** library (`list_page`) → overview (`message_page`: tools, safety) → step (`teach_page`) →
  done.
- **Input map** (contract for all controllers; P3 calibration follows it too):

  | Gesture | Library | Step view |
  |---|---|---|
  | tap / list select | open skill | next step (or confirm suggestion) |
  | double tap | **exit app** (`Control(exit=True)`; required at root) | back to previous screen |
  | scroll down / up | (firmware list scroll) | next / previous detail page for this step |
  | long press (hold) | ask ("find a skill for…") | ask a question (push-to-talk) |

- **Tutor:** the skill is small (≈5–30k tokens), so the whole package (minus raw transcript/chunks) goes
  into the system context, and `store.search` pulls transcript snippets on demand. One structured call
  per utterance: `{transcript, reply_pages[], action: none|next|prev|goto|add_substep, goto_step}`. The
  reply is written for a ~3-line tooltip, with extra pages available by swiping.
- **Scene awareness (optional):** when `hub.last(CameraFrame, "scene")` is fresh, it runs a cheap
  scene check every ~4 s and on demand ("is this right?"), using the step's `success_criteria` and
  `common_mistakes`. It debounces (2 agreeing checks) and **suggests**, never auto-advances.
  Steps with `danger` safety notes are never auto-confirmed.
- Publishes `StepChanged` on every step change (P3 depends on it).

### 6.3 Part 3 — Interactive AR highlighting (experimental)

The G2 has no camera, so registration is **outside-in**: a fixed iPhone camera sees both the work
area and the wearer. **Constraint: no fiducials.** Nothing is attached to the glasses or the scene
(no AprilTags or ArUco markers), so every pose comes from learned estimators on plain video.

1. **Camera intrinsics, marker-free:** Apple Depth Pro estimates focal length from a single image and
   returns metric depth. Take the median focal length over ~30 frames at the fixed stream resolution
   and save it to `data/calib/camera_<id>.json`. Lens distortion on iPhone main cameras is small and
   ignored in v1.
2. **Head pose (stands in for glasses pose):** the glasses sit rigidly on the head, so we estimate
   head pose from the scene camera:
   - **Primary:** MediaPipe Face Landmarker (478 3D landmarks + facial transformation matrix). Fit the
     landmarks to the canonical face model with `solvePnP` using the intrinsics → `T_cam←head`.
     Correct the absolute distance with Depth Pro depth sampled on the face, since face-size priors
     alone are only good to a few cm.
   - **Wide angles:** 6DRepNet360 gives full-range head **rotation** (it's trained for the whole yaw
     range) for when the face is turned away. Position then comes from head keypoints (MediaPipe Pose
     nose/ears/eyes) plus Depth Pro.
   - **Fusion:** if spike S3 shows the G2 IMU gives orientation or gyro data, fuse it: the IMU gives
     fast relative rotation and the camera corrects drift (complementary filter). Without it, a
     constant-velocity filter on the camera pose.
3. **Target localisation:** on `StepChanged`, ground the `target` objects in the latest frame with the
   detector VLM (name + aliases + visual description + exemplar crops). Most targets in trade tasks are
   **static fixtures** (valves, nuts, breakers), so the 3D point is computed once and refreshed every
   few seconds: box centre back-projected with the **median Depth Pro depth inside the box**. Upgrade
   path: a native ARKit/LiDAR camera app.
4. **Display calibration (head frame → display):** G2 optics (FOV, virtual image distance, per-eye
   placement) aren't published, and where the glasses sit relative to the face-model origin varies by
   user and by how they're worn. Both get absorbed into one 3×4 projection `P: head-frame → display px`,
   estimated with a **SPAAM**-style procedure. The glasses show a crosshair, the user lines it up with a
   **markerless world point** and taps, ≥12 times at varied depths, then a DLT solves `P`. The world
   point is one the camera can localise without markers: the user's **index fingertip** (MediaPipe Hand
   Landmarker + Depth Pro), held at different arm extensions for depth variety, or a VLM-grounded
   static object point. The literature (Grubert et al. survey) finds varied-depth point sets most
   accurate. Re-calibrate whenever the glasses are re-seated.
5. **Render loop:** each camera frame gives head pose → project target → `OverlayFrame` (ring/rect at the
   target; an edge arrow if it's off-display) → core `Compositor` → only changed image tiles. Expected
   motion-to-photon is ~250–400 ms (camera + pose ~30–60 ms + BLE image ~150 ms).

**Accuracy budget, stated honestly.** Markerless head-pose estimators report roughly 3–5° mean
rotation error on benchmarks (6DRepNet360 on BIWI/AFLW2000). One review gives the G2 field of view as
~27.5°, which is unverified. At that FOV, 4° is about 80 px of the 576 px width. So v1 aims for
**region-level** highlights: large rings (≥ 60 px), plus arrows and labels ("valve ▼"), rather than
pixel-tight boxes. SPAAM per session removes the systematic part of the error, IMU fusion reduces
jitter, and the overlay hides during fast head motion or when the pose is low-confidence. When there's
no registration, it falls back to text ("◀ valve is to your left").

**Camera placement.** Face-based pose needs the camera to see the wearer's face within about ±60°
yaw, e.g. a tripod across the work area angled at both the fixture and the person. A pure top-down
bird's-eye view still works for Part 2 scene checks, but Part 3 then only has 6DRepNet360 rotation and
keypoints, so expect worse registration.

Success for cycle 1 is a demo-grade region highlight on a static target during slow head motion. If
spikes S1–S3/S7 fail, Part 3 falls back to camera-only directional hints, which still help.

## 7. Contracts and change control

- Python models in `bluebook_core` are the source of truth. JSON Schema and TS are generated
  (`uv run python scripts/gen_contracts.py`) and committed.
- `packages/bluebook_core/tests/test_core.py` is the contract test suite. Every branch must keep it green.
- Contract changes land on `main` **first** in a small PR. Then each branch rebases. Details in
  `docs/INTEGRATION.md`.

## 8. Hardware spikes (do in week 1; results go in `docs/spikes.md`)

| # | Question | Owner | If it fails |
|---|---|---|---|
| S1 | Do black pixels of an image container occlude a text container beneath/above it? | P2 | keep the band layout (current default) |
| S2 | Is the image identical in both eyes? How does it behave with near objects (parallax)? | P3 | calibrate for the dominant eye; bigger rings |
| S3 | IMU: units, axes, real rate per `ImuReportPace` (orientation vs raw accel/gyro?) | P3 | no fusion; motion gating via camera head-pose velocity |
| S4 | Does the whitelist entry `https://<host>.ts.net` also allow `wss://` to the same host? Is the Tailscale VPN on iOS stable while the Even app is backgrounded? | P2 | long-poll HTTP fallback; add `wss://` entry |
| S5 | Does a full-screen `" "` text capture container emit scroll boundary events on swipe? Long-press/release on temples and ring? | P2 | use list capture for step view |
| S6 | Real end-to-end display latency (input → text visible), image fps on glass | P2 | adjust tooltip/overlay pacing |
| S7 | Markerless head pose with G2 worn, at 1–2 m on a 1280-px iPhone stream: face-landmark dropout from the frames, rotation error vs IMU, usable yaw range | P3 | higher resolution, closer camera, or 2 cameras |

## 9. Latency budgets (p50 targets)

| Interaction | Budget | Path |
|---|---|---|
| Tap → next tooltip | < 300 ms | WS + `textContainerUpgrade` (~83 ms) |
| Screen change | < 500 ms | rebuild ~165 ms + image band |
| Voice question → first answer page | < 3 s after release | PCM already streamed; one tutor call |
| Scene check suggestion | < 6 s after the event happens | 4 s cadence + flash-lite |
| AR highlight follow (P3) | < 400 ms motion-to-photon | §6.3 |
| Ingest 10-min video | < 15 min | fine to run in batch |

## 10. Security and privacy

- Tailnet membership is the auth boundary in cycle 1. The server binds to 127.0.0.1 and is exposed
  only by `tailscale serve`. Never use `tailscale funnel`.
- Validate at the trust boundaries: all WS messages are parsed with pydantic (bad messages are
  dropped), skill ids are slug-checked, media paths are confined to the package dir, utterances are
  capped at 60 s.
- Videos may contain faces or customer premises. Packages stay on the laptop. Uploads to Gemini are
  temporary but still third-party processing, so tell experts. `GEMINI_API_KEY` stays in `.env`
  (gitignored).

## 11. Repo layout and branches

```
packages/bluebook_core/          shared lib + server     (main only; changes via contract PR)
packages/bluebook_perception/    Part 1                  (its own branch, e.g. part1-perception)
packages/bluebook_teach/         Part 2                  (e.g. part2-teaching)
packages/bluebook_interactive/   Part 3                  (e.g. part3-interactive)
apps/g2/                         shared glasses thin client (main; P2 may PR transport fixes)
apps/camera/                     shared scene-camera page   (main)
contracts/                       generated schemas
fixtures/skills/                 hand-written test skills (any branch may ADD fixtures)
docs/  workstreams/  scripts/
data/                            runtime (gitignored): skills/, work/, calib/
```

Each part edits **only its own package**. That keeps merges conflict-free.

## 12. Out of scope for cycle 1

Multi-user/auth, cloud deploy, skill editor UI, Live API streaming voice, multiple simultaneous
learners, native iOS camera app (ARKit), offline/on-device models, Even Hub store submission
(sideload only), G1 support.

## 13. References

- Even Hub docs: overview/specs <https://hub.evenrealities.com/docs/get-started/overview>, architecture
  <https://hub.evenrealities.com/docs/get-started/architecture>, display
  <https://hub.evenrealities.com/docs/build/display>, device APIs
  <https://hub.evenrealities.com/docs/build/device-apis>, networking
  <https://hub.evenrealities.com/docs/build/networking>, lifecycle
  <https://hub.evenrealities.com/docs/build/background-lifecycle>, page lifecycle
  <https://hub.evenrealities.com/docs/build/page-lifecycle>, packaging
  <https://hub.evenrealities.com/docs/ship/packaging>, design guidelines
  <https://hub.evenrealities.com/docs/build/design-guidelines>
- SDK: `@evenrealities/even_hub_sdk@0.0.16` typings (`dist/index.d.ts`) — events incl. LONG_PRESS, ImuReportPace, container limits
- Community: even-g2-notes (performance, input quirks) <https://github.com/nickustinov/even-g2-notes>;
  SDK verification notes <https://zenn.dev/bigdra/articles/eveng2-sdk-features?locale=en>; resource list
  <https://github.com/pangoleen/awesome-even-realities-g2>
- Gemini: models <https://ai.google.dev/gemini-api/docs/models>, video understanding
  <https://ai.google.dev/gemini-api/docs/interactions/video-understanding>, media resolution
  <https://ai.google.dev/gemini-api/docs/media-resolution>, image understanding / box_2d
  <https://ai.google.dev/gemini-api/docs/image-understanding>, Live API
  <https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api>
- SAM 3: <https://ai.meta.com/research/publications/sam-3-segment-anything-with-concepts/>
- Markerless pose/depth: MediaPipe Face Landmarker <https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker>;
  6DRepNet360 (full-range head pose) <https://arxiv.org/abs/2309.07654>; Depth Pro (metric depth + focal
  length) <https://arxiv.org/abs/2410.02073>
- OST-HMD calibration: Grubert, Itoh, Moser, Swan, "A Survey of Calibration Methods for Optical
  See-Through Head-Mounted Displays", IEEE TVCG 2018 <https://arxiv.org/abs/1709.04299>
- Procedural video understanding: Ego-Exo4D <https://arxiv.org/abs/2311.18259>; task graphs
  <https://arxiv.org/abs/2502.17753>; EgoInstruct <https://arxiv.org/abs/2509.22019>
- Tailscale Serve HTTPS + WSS pattern: <https://docs.openwebui.com/reference/https/tailscale>
