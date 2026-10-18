"""Stage 4: detail — per-step high-fps clip pass to fill every Step field."""

from __future__ import annotations

import logging
from pathlib import Path

from bluebook_core import models
from google.genai import types

from ..cache import StageCache, sha256_str
from ..schemas import DetailStep

log = logging.getLogger("bluebook.perception.stages.detail")

_PROMPT = (Path(__file__).parents[1] / "prompts" / "detail.md").read_text()


async def run_step(
    uploaded_file: types.File,
    step_title: str,
    t_start: float,
    t_end: float,
    transcript_slice: str,
    objects_json: str,
    cache: StageCache,
) -> DetailStep:
    input_hash = sha256_str(f"{uploaded_file.uri}:{step_title}:{t_start}:{t_end}:{transcript_slice}")
    cache_key = f"detail_{sha256_str(step_title)[:8]}"
    cached = cache.load(cache_key, input_hash)
    if cached:
        return DetailStep.model_validate(cached)

    # extend clip slightly for context; Gemini Duration requires integer seconds
    clip_start = int(max(0.0, t_start - 2.0))
    clip_end = int(t_end + 2.0) + 1
    clip = models.video(uploaded_file, fps=3, start=clip_start, end=clip_end)

    context = (
        f"Step title: {step_title}\n"
        f"Time range: {t_start:.1f}s – {t_end:.1f}s\n"
        f"Transcript for this step:\n{transcript_slice}\n\n"
        f"Task objects already identified:\n{objects_json}"
    )

    log.info("detail: step '%s' (%.1f–%.1f s) …", step_title, t_start, t_end)
    result: DetailStep = await models.generate(
        "video_analyst",
        [clip, context, _PROMPT],
        schema=DetailStep,
    )

    cache.save(cache_key, input_hash, result.model_dump())
    return result


async def run_all(
    uploaded_file: types.File,
    outline_steps: list,
    transcript_segments: list,
    objects_json: str,
    cache: StageCache,
) -> list[DetailStep]:
    import asyncio

    def transcript_slice(t0: float, t1: float) -> str:
        segs = [s for s in transcript_segments if s.get("t1", 0) >= t0 and s.get("t0", 9e9) <= t1]
        return " ".join(s["text"] for s in segs)

    tasks = [
        run_step(
            uploaded_file,
            step["title"],
            step["t_start"],
            step["t_end"],
            transcript_slice(step["t_start"], step["t_end"]),
            objects_json,
            cache,
        )
        for step in outline_steps
    ]
    return list(await asyncio.gather(*tasks))
