"""Stage 6: synthesize — FAQ, prerequisites, est_duration_s."""

from __future__ import annotations

import logging
from pathlib import Path

from bluebook_core import models

from ..cache import StageCache, sha256_str
from ..schemas import SynthResult

log = logging.getLogger("bluebook.perception.stages.synthesize")

_PROMPT = (Path(__file__).parents[1] / "prompts" / "synthesize.md").read_text()


async def run(outline_json: str, detail_json: str, cache: StageCache) -> SynthResult:
    input_hash = sha256_str(outline_json + detail_json)
    cached = cache.load("synthesize", input_hash)
    if cached:
        log.info("synthesize: cache hit")
        return SynthResult.model_validate(cached)

    context = f"<outline>\n{outline_json}\n</outline>\n\n<steps>\n{detail_json}\n</steps>"
    log.info("synthesize: calling synthesizer …")
    result: SynthResult = await models.generate("synthesizer", [context, _PROMPT], schema=SynthResult)

    cache.save("synthesize", input_hash, result.model_dump())
    log.info("synthesize: %d FAQ, %d prerequisites", len(result.faq), len(result.prerequisites))
    return result
