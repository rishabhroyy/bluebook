"""Stage 7: verify — agentic critique loop to assign provenance and drop unsupported claims."""

from __future__ import annotations

import logging
from pathlib import Path

from bluebook_core import models

from ..cache import StageCache, sha256_str
from ..schemas import VerifyResult

log = logging.getLogger("bluebook.perception.stages.verify")

_PROMPT = (Path(__file__).parents[1] / "prompts" / "verify.md").read_text()
_MAX_ROUNDS = 2  # generate → critique → regenerate (one correction pass is enough for Balanced depth)


async def run(transcript_json: str, outline_json: str, detail_json: str, cache: StageCache) -> VerifyResult:
    input_hash = sha256_str(transcript_json + outline_json + detail_json)
    cached = cache.load("verify", input_hash)
    if cached:
        log.info("verify: cache hit")
        return VerifyResult.model_validate(cached)

    context = (
        f"<transcript>\n{transcript_json}\n</transcript>\n\n"
        f"<outline>\n{outline_json}\n</outline>\n\n"
        f"<steps>\n{detail_json}\n</steps>"
    )

    result: VerifyResult | None = None
    for round_n in range(_MAX_ROUNDS):
        prompt_parts = [context, _PROMPT]
        if result and result.critique:
            # agentic loop: feed previous critique back for a correction pass
            prompt_parts.append(
                f"\n\nPrevious critique round identified these issues:\n{result.critique}\n"
                "Please revise your assessment addressing these points."
            )

        log.info("verify: round %d/%d …", round_n + 1, _MAX_ROUNDS)
        result = await models.generate("synthesizer", prompt_parts, schema=VerifyResult)

        issues_found = sum(len(s.issues) for s in result.steps) + sum(1 for s in result.safety if s.drop)
        log.info(
            "verify: round %d — %d step issues, critique=%r",
            round_n + 1,
            issues_found,
            result.critique[:80] if result.critique else "",
        )

        # if no issues found, no need for another round
        if not result.critique and issues_found == 0:
            break

    cache.save("verify", input_hash, result.model_dump())
    return result
