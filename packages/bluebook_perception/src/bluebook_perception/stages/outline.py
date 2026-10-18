"""Stage 3: outline — whole-video pass to get steps, objects, safety notes."""

from __future__ import annotations

import logging
from pathlib import Path

from bluebook_core import models
from google.genai import types

from ..cache import StageCache, sha256_str
from ..schemas import OutlineResult

log = logging.getLogger("bluebook.perception.stages.outline")

_PROMPT = (Path(__file__).parents[1] / "prompts" / "outline.md").read_text()


async def run(
    uploaded_file: types.File,
    transcript_json: str,
    cache: StageCache,
) -> OutlineResult:
    input_hash = sha256_str(f"{uploaded_file.uri}:{transcript_json}")
    cached = cache.load("outline", input_hash)
    if cached:
        log.info("outline: cache hit")
        return OutlineResult.model_validate(cached)

    transcript_block = f"<transcript>\n{transcript_json}\n</transcript>"
    video_part = models.video(uploaded_file, fps=1)

    log.info("outline: calling video_analyst …")
    result: OutlineResult = await models.generate(
        "video_analyst",
        [video_part, transcript_block, _PROMPT],
        schema=OutlineResult,
    )

    cache.save("outline", input_hash, result.model_dump())
    log.info("outline: %d steps, %d objects, %d safety", len(result.steps), len(result.objects), len(result.safety))
    return result
