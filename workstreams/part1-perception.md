# Part 1 — Perception / learning pipeline

Branch: `part1-perception` · Package: `packages/bluebook_perception/` · Read first: `AGENTS.md`, `docs/DESIGN.md` §1, §4, §5.1, §6.1

## Goal

Turn one iPhone recording of an expert doing a task, with **video and audio**, into a `SkillPackage`
rich enough that a tutor agent can teach the task step by step and answer follow-up questions without
going back to the video.

## You own

- `packages/bluebook_perception/**` (code, prompts, tests, this package's deps in its `pyproject.toml`)
- New fixtures in `fixtures/skills/` (small ones only; no videos in git)

You do **not** edit `packages/bluebook_core/**`, `apps/**`, or other parts. If you need a contract
change (e.g. a new `Step` field), follow `docs/INTEGRATION.md` §Contract changes.

## Interfaces

**Output (contract):** `bluebook_core.store.save(pkg, embeddings)` → `data/skills/<id>/`. `save()`
validates with `SkillPackage.check()`. A package that doesn't validate isn't done.

**Entry points you must provide:**
- CLI: `uv run bluebook-ingest VIDEO [VIDEO...] --id SLUG --title "..." [--force-stage NAME]` (stub in `cli.py`).
- Python: `async def ingest(videos: list[Path], skill_id: str, title: str | None = None) -> SkillPackage`
  exported from `bluebook_perception` (P2 may call it for "record a new skill" later).
- Optional HTTP in `setup(app)`: `POST /ingest` (multipart upload) → `{job_id}`, `GET /ingest/{job_id}` → status.
  Run jobs as asyncio tasks; no queue system.

**Use from core, don't re-implement:** `models.upload/video/generate/embed`, `store.save/skill_dir`,
`config` (model ids via role names `video_analyst`, `synthesizer`, `embedder`).

## Pipeline (recommended; deviate if measurements say so, and write down why)

Cache every stage under `data/work/<id>/<stage>.json` keyed by input hash, so re-running only
recomputes what changed (cheap prompt iteration).

1. **ingest**: sha256, `ffprobe` metadata, normalise with `ffmpeg` (H.264, ≤1080p, keep AAC audio),
   extract `media/audio.wav` (16 kHz mono), and copy the original to `source/`. Reject inputs with no audio
   track loudly (warn, continue): narration is a main source of knowledge.
2. **transcribe**: word-level timestamps. Prefer local `mlx-whisper` (Apple Silicon) or `faster-whisper`
   large-v3, because Gemini timestamps can drift on long videos. Output `TranscriptSegment`s. Keep fillers out.
3. **outline** (`video_analyst`, whole video incl. audio, default 1 fps, structured output): title,
   summary, tags, difficulty, ordered steps with `t_start/t_end`, tools/materials/parts with visual
   descriptions, safety notes, and **non-speech audio cues** (hiss, click, motor change). Give the
   transcript in the prompt *as well as* the video. The model gets both channels, and the
   timestamps line up.
4. **detail** (per step, `models.video(f, fps=3, start=t0-2, end=t1+2)`, high media resolution): fill every
   `Step` field. `tooltip` is imperative, ≤120 chars, and names the object ("Turn the shutoff valve
   clockwise until it stops."). Also: `success_criteria` as *observable* checks a camera could verify,
   `common_mistakes`, `substeps`, `objects[]` with roles (exactly the thing(s) acted on are `target`),
   `narration` (verbatim transcript slice), and 1–3 keyframe timestamps.
5. **keyframes**: for each requested t, decode ±0.5 s and keep the sharpest frame (variance of
   Laplacian; handheld footage blurs). Save JPEG ≤1280 px to `media/`. Optional: ground each
   `target` object in its keyframe (`box_2d` from Gemini, which is `[ymin,xmin,ymax,xmax]`/1000; convert to
   `Box2D` x0,y0,x1,y1 in [0,1]) → `ObjectRef.appearances`. Part 3 uses these as visual exemplars.
6. **synthesize** (`synthesizer`): FAQ (questions a novice would ask at each step), prerequisites,
   dedupe objects, `est_duration_s`.
7. **verify** (`synthesizer`, a separate call): for each step and safety note, check it against
   transcript + outline. Set `provenance` (`observed`/`narrated`/`inferred`) and `confidence`, and drop or
   flag unsupported claims. **Never invent torque values, measurements, or part numbers.** If the
   expert didn't say it or show it, it's `inferred` and the text says "typically".
8. **index**: chunks = one per step (title+instruction+tips), transcript windows (~30 s, 5 s overlap),
   each FAQ, each object, each keyframe (caption text + image), each safety note. Embed with
   `models.embed` (images as JPEG bytes). Set `pkg.embedding = EmbeddingInfo(model=..., dim=...)`.
9. **save**: `store.save(pkg, embeddings)`.

Keep prompts in `prompts/*.md` files, not inline strings, so they're easy to diff while you iterate.
Use pydantic response schemas (subsets of the `skill.py` models) with `models.generate(schema=...)`.

## Milestones

1. **M1 (day 1–2):** CLI runs stages 1–3 on one real video and writes a valid package with steps,
   tooltips, and the transcript. Hand it to P2 (push the package dir to a shared drive, not git).
2. **M2:** stages 4–7, so you have full step detail, keyframes, and provenance.
3. **M3:** index + embeddings, plus the eval harness.
4. **M4 (stretch):** multiple videos per skill (different angles/takes) merged into one step list.

## Acceptance criteria

- `store.load(id)` works and `pkg.check() == []` for every eval video.
- Eval set: 3–5 real tasks (≥1 with background noise, ≥1 with silent stretches, ≥1 handheld with
  lots of motion). Hand-label step boundaries in `packages/bluebook_perception/eval/<id>.labels.json`.
  Report step-boundary F1 at ±3 s, and have a human rate tooltip actionability (1–5) for every step.
  Target: F1 ≥ 0.7 and mean actionability ≥ 4.
- No safety note or numeric spec marked `observed`/`narrated` that isn't in the transcript or visible.
- A 10-minute video ingests in under 15 min, and cost is logged per run (token counts are in the `models` logs).
- Unit tests for the pure parts (chunking, keyframe selection, box conversion, stage caching).
  `uv run pytest` is green, including core's contract tests.

## Gotchas

- Gemini samples video at **1 fps by default**, which misses fast hand actions. That's why the per-step
  pass uses higher fps on short clips.
- iPhone `.mov` files can be HEVC with variable frame rate. Normalise first, or offsets drift.
- The File API needs files to be `ACTIVE` before use (`models.upload` waits for this), and deletes
  them after ~48 h. Re-upload from `source/` on cache miss.
- Keep step granularity at roughly 1 tooltip = 1 physical action, about 10–60 s. Split anything longer.
