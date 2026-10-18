"""Part 3 perception: grounding of step targets in the scene camera frame (TypeSafe Jev Box).
Head pose and box tracking run on the phone (Apple ARKit/Vision) and arrive over the pose WebSocket (track.py)."""

from __future__ import annotations

import io

from bluebook_core import models, store
from bluebook_core.config import config
from bluebook_core.protocol import StepTarget
from PIL import Image

MAX_EXEMPLAR_SIDE = 512  # exemplar crops are downscaled to this (Gemini labelling / evals; Jev does not use them)


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
    """Jev Box for the target in the frame, as normalised (x0, y0, x1, y1). Jev only, never Gemini (owner's call).
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
