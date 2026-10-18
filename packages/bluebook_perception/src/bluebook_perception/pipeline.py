"""Orchestrates all 9 stages into one call: video paths → SkillPackage saved to disk."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from bluebook_core import models, store
from bluebook_core.skill import (
    QA,
    ObjectRef,
    SafetyNote,
    SkillPackage,
    Source,
    Step,
    StepObject,
    TranscriptSegment,
)

from .cache import StageCache
from .stages import detail, index, ingest, keyframes, outline, synthesize, verify
from .transcribe import Transcriber, default_transcriber

log = logging.getLogger("bluebook.perception.pipeline")

PIPELINE_VERSION = "ingest-v1"


def _clamp_steps(steps: list, duration: float) -> list:
    """Keep outline step times inside the video. Gemini can return times past the end, and a clip
    request past the end fails with 400 INVALID_ARGUMENT."""
    out = [s.model_copy(update={"t_end": min(s.t_end, duration)}) for s in steps if s.t_start < duration]
    if [(s.t_start, s.t_end) for s in out] != [(s.t_start, s.t_end) for s in steps]:
        log.warning(
            "outline: clamped step times to video duration %.1f s (%d -> %d steps)", duration, len(steps), len(out)
        )
    return out


async def run(
    videos: list[Path],
    skill_id: str,
    title: str | None = None,
    force_stage: str | None = None,
    transcriber: Transcriber | None = None,
) -> SkillPackage:
    """Full 9-stage pipeline. Returns the saved SkillPackage."""
    if not videos:
        raise ValueError("at least one video required")

    cache = StageCache(skill_id, force_stage=force_stage)
    if transcriber is None:
        transcriber = default_transcriber()

    # ── Stage 1: ingest (first video; M4 will handle multi-video merge) ──────
    log.info("=== stage 1: ingest ===")
    ingest_result = ingest.run(videos[0], skill_id, cache)
    source_video_data = ingest_result["source_video"]
    normalized_path = ingest_result["normalized_path"]
    audio_path = Path(ingest_result["audio_path"])

    from bluebook_core.skill import SourceVideo

    source_video = SourceVideo.model_validate(source_video_data)

    # ── Stage 2: transcribe ───────────────────────────────────────────────────
    log.info("=== stage 2: transcribe ===")
    from .cache import sha256_file, sha256_str

    audio_hash = sha256_file(audio_path) if audio_path.exists() else sha256_str(str(audio_path))
    cached_tx = cache.load("transcribe", audio_hash)
    if cached_tx:
        log.info("transcribe: cache hit")
        transcript_segs = [TranscriptSegment.model_validate(s) for s in cached_tx]
    else:
        transcript_segs = await transcriber.transcribe(audio_path, video_index=0)
        cache.save("transcribe", audio_hash, [s.model_dump() for s in transcript_segs])

    transcript_json = json.dumps([s.model_dump() for s in transcript_segs], indent=2)

    # ── Stage 3: outline ──────────────────────────────────────────────────────
    log.info("=== stage 3: outline ===")
    uploaded = await models.upload(Path(normalized_path))
    outline_result = await outline.run(uploaded, transcript_json, cache)
    outline_result.steps = _clamp_steps(outline_result.steps, source_video.duration_s)

    # ── Build initial objects from outline ────────────────────────────────────
    objects: dict[str, ObjectRef] = {
        o.id: ObjectRef(
            id=o.id,
            name=o.name,
            aliases=o.aliases,
            category=o.category,
            visual_description=o.visual_description,
        )
        for o in outline_result.objects
    }
    objects_json = json.dumps([o.model_dump() for o in objects.values()], indent=2)

    # ── Stage 4: detail ───────────────────────────────────────────────────────
    log.info("=== stage 4: detail ===")
    detail_steps = await detail.run_all(
        uploaded,
        [s.model_dump() for s in outline_result.steps],
        [s.model_dump() for s in transcript_segs],
        objects_json,
        cache,
        duration=source_video.duration_s,
    )
    detail_json = json.dumps([s.model_dump() for s in detail_steps], indent=2)

    # ── Stage 5: keyframes ────────────────────────────────────────────────────
    log.info("=== stage 5: keyframes ===")
    built_steps: list[Step] = []

    for i, (outline_step, d_step) in enumerate(zip(outline_result.steps, detail_steps)):
        step_id = f"s{i + 1}"
        # resolve object refs from detail pass
        step_objects = [
            StepObject(object_id=so.object_id, role=so.role, action=so.action)
            for so in d_step.objects
            if so.object_id in objects
        ]
        target_ids = {so.object_id for so in step_objects if so.role == "target"}
        target_objs = [objects[oid] for oid in target_ids if oid in objects]

        kf_refs, updated_objs = await keyframes.extract_and_ground(
            normalized_path,
            skill_id,
            step_id,
            # caption clips run to int(t + 1) + 1, so keep t two seconds clear of the end
            [min(t, source_video.duration_s - 2) for t in d_step.keyframe_times],
            target_objs,
            uploaded,
            cache,
        )
        # merge updated object appearances back
        for obj in updated_objs:
            objects[obj.id] = obj

        built_steps.append(
            Step(
                id=step_id,
                index=i,
                title=d_step.title or outline_step.title,
                tooltip=d_step.tooltip,
                instruction=d_step.instruction,
                why=d_step.why,
                substeps=d_step.substeps,
                objects=step_objects,
                success_criteria=d_step.success_criteria,
                common_mistakes=d_step.common_mistakes,
                tips=d_step.tips,
                warnings=d_step.warnings,
                narration=d_step.narration,
                t_start=outline_step.t_start,
                t_end=outline_step.t_end,
                keyframes=kf_refs,
                est_duration_s=d_step.est_duration_s,
                provenance="observed",  # verify stage will correct this
                confidence=1.0,
            )
        )

    # ── Stage 6: synthesize ───────────────────────────────────────────────────
    log.info("=== stage 6: synthesize ===")
    synth = await synthesize.run(outline_result.model_dump_json(), detail_json, cache)

    faq = [QA(q=qa.q, a=qa.a, step_ids=qa.step_ids, provenance="inferred") for qa in synth.faq]
    safety_notes = [SafetyNote(text=sn.text, severity=sn.severity) for sn in outline_result.safety]

    # ── Stage 7: verify (agentic critique loop) ───────────────────────────────
    log.info("=== stage 7: verify ===")
    steps_json = json.dumps([s.model_dump() for s in built_steps], indent=2)
    verify_result = await verify.run(transcript_json, outline_result.model_dump_json(), steps_json, cache)

    # apply verify results
    step_by_id = {s.id: s for s in built_steps}
    for vs in verify_result.steps:
        if vs.step_id in step_by_id:
            s = step_by_id[vs.step_id]
            updated = s.model_copy(
                update={
                    "provenance": vs.provenance,
                    "confidence": vs.confidence,
                }
            )
            if vs.revised_tooltip:
                updated = updated.model_copy(update={"tooltip": vs.revised_tooltip[:120]})
            if vs.revised_instruction:
                updated = updated.model_copy(update={"instruction": vs.revised_instruction})
            step_by_id[vs.step_id] = updated

    # drop or update safety notes from verify
    kept_safety: list[SafetyNote] = []
    for i, sn in enumerate(safety_notes):
        vs = next((v for v in verify_result.safety if v.index == i), None)
        if vs and vs.drop:
            log.info("verify: dropping safety note %d: %s", i, sn.text[:60])
            continue
        if vs:
            kept_safety.append(sn.model_copy(update={"provenance": vs.provenance}))
        else:
            kept_safety.append(sn)

    final_steps = [step_by_id[f"s{i + 1}"] for i in range(len(built_steps))]

    # ── Assemble SkillPackage (pre-index) ─────────────────────────────────────
    pkg = SkillPackage(
        id=skill_id,
        title=title or outline_result.title,
        summary=outline_result.summary,
        tags=outline_result.tags,
        difficulty=outline_result.difficulty,
        est_duration_s=synth.est_duration_s,
        prerequisites=synth.prerequisites,
        safety=kept_safety,
        objects=list(objects.values()),
        steps=final_steps,
        faq=faq,
        transcript=transcript_segs,
        source=Source(
            videos=[source_video],
            created_at=datetime.now(UTC),
            pipeline_version=PIPELINE_VERSION,
            models={role: models.model_id(role) for role in ("video_analyst", "synthesizer", "embedder")},
        ),
    )

    errs = pkg.check()
    if errs:
        log.warning("pre-index package has issues (will attempt to continue): %s", errs)

    # ── Stage 8: index + embed ────────────────────────────────────────────────
    log.info("=== stage 8: index ===")
    pkg, embeddings = await index.run(pkg, cache)

    # ── Stage 9: save ─────────────────────────────────────────────────────────
    log.info("=== stage 9: save ===")
    errs = pkg.check()
    if errs:
        raise ValueError("SkillPackage validation failed:\n" + "\n".join(errs))

    store.save(pkg, embeddings)
    log.info("pipeline: saved skill '%s' — %d steps, %d chunks", skill_id, len(pkg.steps), len(pkg.chunks))
    return pkg
