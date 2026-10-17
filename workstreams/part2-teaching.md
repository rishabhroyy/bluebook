# Part 2 — Playback / teaching mode

Suggested branch: `part2-teaching` · Package: `packages/bluebook_teach/` · Read first: `AGENTS.md`, `docs/DESIGN.md` §1, §2, §5.2, §6.2, §8

## Goal

A learner wearing G2 glasses picks a skill and is taught it step by step with short tooltips at the
top of the view. They can ask questions by voice at any point, and the tutor adapts: more detail, a
different explanation, going back, or extra substeps. When a scene camera is connected, the tutor uses
what it sees.

## You own

- `packages/bluebook_teach/**`
- Week-1 hardware spikes S1, S4, S5, S6 → write results in `docs/spikes.md` (new file; send it to main in a PR)
- Tuning the `teach_page` geometry. The layout lives in core (`display.py`), so changes go through a
  contract PR (it's shared with P3).

Not yours: `bluebook_core` (contract PR only), `apps/g2` (PR to main for transport bugs only; never add
product logic there), other parts.

## Interfaces

- **Register:** in `setup(app)`, call `bluebook_core.glasses.set_controller(TeachController)`.
- **Implement** `TeachController(Controller)`: `on_connect`, `on_input`, `on_utterance`, `on_disconnect`.
  Draw only with `conn.show(display.*_page(...))`, `conn.text("tip"|"status", ...)`,
  `conn.image(display.image_msg(box, display.fit(img, w, h)))`, `conn.control(...)`.
- **Publish** `hub.publish(StepChanged(session=conn.session, skill_id=..., step_id=..., targets=[...]))` on
  every step change, and with `step_id=None` when leaving a lesson. Fill `targets` from step objects with
  `role == "target"` joined to `ObjectRef`. **P3 depends on this.**
- **Consume:** `store.list_skills/load/search`, `hub.last(CameraFrame, "scene")` (scene camera; may be
  absent), and optionally `hub.subscribe(SceneObservation)` from P3.
- **Image band ownership:** when `config.interactive` is on and P3 is producing overlays, don't write
  `img_l/img_r` yourself (P3's compositor owns them). Otherwise show the step keyframe there.
- Session state goes in `conn.state`. Core keeps it in memory across P3's calibration controller
  push/pop **and** across reconnects of the same glasses (Android WebView kills, phone lock), and
  `on_connect` is called again each time, so redraw from it. Also persist `{skill_id, step_id}` to
  `data/sessions/<session>.json` so a lesson survives a server restart.

## UX contract (from DESIGN §6.2; change only with the owner)

| Gesture | Library (`list_page`) | Overview (`message_page`) | Step (`teach_page`) |
|---|---|---|---|
| tap / list_select | open skill overview | start step 1 | next step / accept suggestion |
| double_tap | `conn.control(exit=True)` (root, required) | back to library | back to overview |
| scroll_down / up | firmware scroll | firmware scroll | next / prev detail page: instruction → substeps → tips/warnings → why |
| long_press (hold) + release | ask "what do you want to learn?" → search titles/summaries | ask | ask (push-to-talk; client streams PCM, you get `on_utterance`) |

- `tip` (3 lines, ~120 chars): the tooltip, or the current detail/answer page. Dim `status` line:
  `2/5 · Turn off water`, then `● listening` while held, `… thinking`, and `✓ looks done — tap` when
  the scene check suggests it.
- Text measurement: the font is proportional with no size control. Paginate at ~120 chars for `tip`
  and ~450 for full-screen pages. Measure on hardware and adjust (even-toolkit has pixel-accurate
  metrics if you need them). Supported glyphs: `━ ─ █ ▲△▶▷▼▽◀◁ ●○ ■□ ★☆` and box-drawing characters.
  Anything outside the font set is **silently dropped**, so avoid emoji.

## Tutor design (recommended)

- **Context:** the skill is small. Put a compact rendering of the whole `SkillPackage` (steps with all
  fields, objects, safety, FAQ, no transcript/chunks) in `system`, plus the current step id and the
  last ~10 turns. Use `store.search(pkg, query)` for transcript snippets when the question asks
  "what did they say about…". Use Gemini context caching if cost or latency shows up.
- **One call per utterance:** `models.generate("tutor", [models.audio_pcm16k(pcm), state_text], schema=TutorReply)`:
  ```python
  class TutorReply(BaseModel):
      transcript: str            # what the learner said (show it briefly; it builds trust)
      pages: list[str]           # each ≤120 chars, ≤4 pages
      action: Literal["none", "next", "prev", "goto", "add_substeps"]
      goto_step: str | None = None
      substeps: list[str] = []   # session-local extra breakdown when the learner is struggling
  ```
- **Teaching behaviour** (system prompt): one action per tooltip; name the object; answer the question
  asked, then point back to the step; when the learner is stuck, break the step into smaller substeps;
  flag `inferred` safety info ("typically…, check your manual"); never invent specs. If the question
  is outside the skill, say so briefly.
- **Scene check** (when a fresh scene frame exists, age < 2 s): every ~4 s and on "is this right?",
  call `models.generate("scene", [models.image(frame.jpeg), step criteria + mistakes], schema=SceneCheck{done, confidence, issue})`.
  Suggest on 2 consecutive `done` with confidence ≥ 0.7. Show an `issue` immediately if it matches a
  `common_mistake`. Never auto-advance. Never confirm steps that carry `danger` safety notes.
- Run model calls with `asyncio.create_task`, and drop stale replies (the step changed while waiting).

## Milestones

1. **M1:** spikes S1/S4/S5/S6 on real glasses through `EchoController`, which is already in core. Run the
   app: `cd apps/g2 && pnpm dev`, `pnpm qr`, then scan in the Even app.
2. **M2:** library → overview → steps with tap/double-tap/scroll on the fixture skill. Publish `StepChanged`.
3. **M3:** voice Q&A with `TutorReply` actions, plus session persistence.
4. **M4:** keyframes in the image band, and scene checks with the `apps/camera` page.

## Acceptance criteria

- A full run of the fixture skill on hardware, plus one P1-generated skill once available, with no
  dead ends (every screen exits via double-tap).
- Voice question p50 < 3 s from release to first page (log timings).
- Locking the phone for 5 min and coming back resumes the same step (Even beta-test requirement).
- Tests: controller state machine driven by fake `Input`s against a fake connection (no network or
  models), and `TutorReply` action handling. `uv run pytest` green.
