"""Stage 5: keyframes — select sharpest frames, save JPEGs, optional box_2d grounding."""

from __future__ import annotations

import logging
from pathlib import Path

from bluebook_core import models
from bluebook_core.skill import MediaRef, ObjectAppearance, ObjectRef
from bluebook_core.store import skill_dir
from google.genai import types

from ..cache import StageCache, sha256_str
from ..media import gemini_box_to_box2d, save_keyframe, sharpest_frame
from ..schemas import GroundingResult

log = logging.getLogger("bluebook.perception.stages.keyframes")


async def extract_and_ground(
    normalized_path: str,
    skill_id: str,
    step_id: str,
    keyframe_times: list[float],
    target_objects: list[ObjectRef],
    uploaded_file: types.File,
    cache: StageCache,
) -> tuple[list[MediaRef], list[ObjectRef]]:
    """Returns (keyframe MediaRefs for the step, updated ObjectRef list with appearances)."""
    video_path = Path(normalized_path)
    pkg_dir = skill_dir(skill_id)
    media_dir = pkg_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    refs: list[MediaRef] = []
    updated_objects = {o.id: o.model_copy(deep=True) for o in target_objects}

    for i, t in enumerate(keyframe_times[:3]):  # cap at 3 per step
        kf_path = media_dir / f"{step_id}_k{i + 1}.jpg"
        input_hash = sha256_str(f"{normalized_path}:{step_id}:{t}")
        cached = cache.load(f"kf_{step_id}_{i}", input_hash)

        if cached and kf_path.exists():
            ref = MediaRef(**cached["ref"])
        else:
            img = sharpest_frame(video_path, t)
            if img is None:
                log.warning("keyframes: no frame found at t=%.1f for %s", t, step_id)
                continue

            # generate a caption for multimodal embedding quality
            caption = await _caption_frame(img, uploaded_file, t)
            save_keyframe(img, kf_path)
            ref = MediaRef(path=f"media/{kf_path.name}", t=t, caption=caption)
            cache.save(f"kf_{step_id}_{i}", input_hash, {"ref": ref.model_dump()})

        refs.append(ref)

        # grounding: find target objects in this keyframe (Balanced: 1–3 per target)
        if target_objects and kf_path.exists():
            await _ground_objects(kf_path, ref, target_objects, updated_objects, cache, step_id, i)

    return refs, list(updated_objects.values())


async def _caption_frame(img, uploaded_file: types.File, t: float) -> str:
    """Ask video_analyst for a brief caption of what's visible at this moment."""
    import io

    buf = io.BytesIO()
    img.save(buf, "JPEG")
    img_part = models.image(buf.getvalue())
    clip = models.video(uploaded_file, fps=1, start=int(max(0, t - 1.0)), end=int(t + 1.0) + 1)
    try:
        caption = await models.generate(
            "video_analyst",
            [
                img_part,
                clip,
                "Describe what is shown in this keyframe in one sentence. Focus on what the person's hands are doing and which object they are acting on.",
            ],
        )
        return str(caption).strip()[:200]
    except (ValueError, RuntimeError, OSError) as e:
        log.warning("keyframes: caption failed: %s", e)
        return ""


async def _ground_objects(
    kf_path: Path,
    ref: MediaRef,
    target_objects: list[ObjectRef],
    updated_objects: dict[str, ObjectRef],
    cache: StageCache,
    step_id: str,
    frame_idx: int,
) -> None:
    """Ask Gemini for box_2d of each target object in this keyframe."""
    from pydantic import BaseModel as PBM

    class _GroundMany(PBM):
        groundings: list[GroundingResult]

    obj_desc = "\n".join(f"- id={o.id}: {o.visual_description or o.name}" for o in target_objects)
    img_bytes = kf_path.read_bytes()
    img_part = models.image(img_bytes)
    prompt = (
        f"For each object listed below, provide its bounding box in this image as [ymin, xmin, ymax, xmax] "
        f"where each value is 0–1000 (normalised). If an object is not visible, set box to null.\n\nObjects:\n{obj_desc}"
    )

    cache_key = f"ground_{step_id}_{frame_idx}"
    cache_hash = sha256_str(f"{kf_path}:{obj_desc}")
    cached_g = cache.load(cache_key, cache_hash)
    if cached_g:
        groundings = [GroundingResult(**g) for g in cached_g]
    else:
        try:
            result: _GroundMany = await models.generate("video_analyst", [img_part, prompt], schema=_GroundMany)
            groundings = result.groundings
            cache.save(cache_key, cache_hash, [g.model_dump() for g in groundings])
        except (ValueError, RuntimeError, OSError) as e:
            log.warning("keyframes: grounding failed: %s", e)
            return

    for g in groundings:
        if g.object_id in updated_objects and g.box:
            box = gemini_box_to_box2d(g.box)
            appearance = ObjectAppearance(media=ref, box=box)
            updated_objects[g.object_id].appearances.append(appearance)
