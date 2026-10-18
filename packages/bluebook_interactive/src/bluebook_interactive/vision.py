"""Part 3 perception: grounding of step targets in the scene camera frame: TypeSafe Jev Box (fast), Gemini (slower,
run in parallel; its answer replaces Jev's, including "not visible", which Jev's Box can't say).
Head pose and box tracking run on the phone (Apple ARKit/Vision) and arrive over the pose WebSocket (track.py)."""

from __future__ import annotations

import asyncio
import io

from bluebook_core import models, store
from bluebook_core.config import config
from bluebook_core.protocol import StepTarget
from PIL import Image
from pydantic import BaseModel

MAX_EXEMPLAR_SIDE = 512  # exemplar crops are downscaled to this (Gemini labelling / evals; Jev does not use them)
GEMINI_TIMEOUT_S = 15.0  # bounds a stuck call; the target's Jev box stands meanwhile


# ------------------------------------------------------------------ grounding


def exemplars(skill_id: str, object_id: str, n: int = 2) -> list[bytes]:
    """Up to n JPEG crops of the object from the skill's recorded appearances. Missing files are skipped."""
    out: list[bytes] = []
    try:
        obj = store.load(skill_id).object(object_id)
    except (KeyError, OSError, ValueError):
        return out
    for ap in obj.appearances:
        if len(out) >= n:
            break
        try:
            im = Image.open(store.media_path(skill_id, ap.media.path)).convert("RGB")
            if ap.box:
                b = ap.box
                W, H = im.size
                im = im.crop(
                    (
                        int(b.x0 * W),
                        int(b.y0 * H),
                        max(int(b.x1 * W), int(b.x0 * W) + 1),
                        max(int(b.y1 * H), int(b.y0 * H) + 1),
                    )
                )
            im.thumbnail((MAX_EXEMPLAR_SIDE, MAX_EXEMPLAR_SIDE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            out.append(buf.getvalue())
        except (OSError, ValueError):
            continue
    return out


async def ground(jpeg: bytes, target: StepTarget, ex: list[bytes] = []) -> tuple[float, float, float, float] | None:  # noqa: B006
    """Jev Box for the target in the frame, as normalised (x0, y0, x1, y1). Gemini (gemini_ground) runs alongside.
    Reference crops (`ex`) are not sent: they doubled latency for no accuracy gain (2026-10-18 eval), nor is a presence
    question (it disagreed with the labels half the time). Box always answers; Targets votes over repeated calls."""
    if not config.typesafe_api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not set: Part 3 detection needs Jev (uv run bluebook bootstrap)")
    aka = f" (aka {', '.join(target.aliases)})" if target.aliases else ""
    desc = f"the {target.name}{aka} in `frame`" + (
        f", which looks like: {target.visual_description}" if target.visual_description else ""
    )
    b, _ = await models.box("boxer", jpeg, desc)
    return b if b[2] > b[0] and b[3] > b[1] else None


class _Found(BaseModel):
    found: bool
    box_2d: list[int] = []  # [ymin, xmin, ymax, xmax], 0..1000


async def gemini_ground(jpeg: bytes, target: StepTarget) -> tuple[float, float, float, float] | None:
    """Gemini box for the target, normalised (x0, y0, x1, y1); None when Gemini says it isn't visible."""
    aka = f" (aka {', '.join(target.aliases)})" if target.aliases else ""
    look = f" It looks like: {target.visual_description}." if target.visual_description else ""
    prompt = (
        f"Find the {target.name}{aka} in the image.{look} A part held in a hand counts when you can see it. Set found to"
        " false only when it is not visible at all (for example already inside something), and never box a different"
        " object instead. Otherwise box_2d is [ymin, xmin, ymax, xmax] of the visible object, scaled 0 to 1000."
    )
    r = await asyncio.wait_for(
        models.generate("detector", [models.image(jpeg), prompt], schema=_Found, retries=1), GEMINI_TIMEOUT_S
    )
    if not r.found or len(r.box_2d) != 4:
        return None
    y0, x0, y1, x1 = (min(max(v / 1000.0, 0.0), 1.0) for v in r.box_2d)
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None
