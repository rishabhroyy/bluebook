"""Part 3 perception: VLM grounding of step targets in the scene camera frame (Gemini).
Head pose and box tracking run on the phone (Apple ARKit/Vision) and arrive over the pose WebSocket (track.py)."""

from __future__ import annotations

import io

from bluebook_core import models, store
from bluebook_core.protocol import StepTarget
from PIL import Image
from pydantic import BaseModel

MAX_EXEMPLAR_SIDE = 512  # exemplar crops are downscaled to this


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


class _Found(BaseModel):
    found: bool
    box_2d: list[int] = []  # [ymin, xmin, ymax, xmax], 0..1000


async def ground(jpeg: bytes, target: StepTarget, ex: list[bytes] = []) -> tuple[float, float, float, float] | None:  # noqa: B006
    """VLM box for the target in the frame, as normalised (x0, y0, x1, y1). None if not found."""
    aka = f" (aka {', '.join(target.aliases)})" if target.aliases else ""
    prompt = (
        f"Find: {target.name}{aka}. Looks like: {target.visual_description}. Box the whole object in the first image."
    )
    parts: list = [models.image(jpeg)]
    for e in ex:
        parts += ["Reference crop of the object:", models.image(e)]
    r = await models.generate("detector", [*parts, prompt], schema=_Found)
    if not r.found or len(r.box_2d) != 4:
        return None
    y0, x0, y1, x1 = (min(max(v / 1000.0, 0.0), 1.0) for v in r.box_2d)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1
