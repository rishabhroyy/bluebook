"""Stage 8: index — build chunks with Contextual Retrieval, embed everything."""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from bluebook_core import models
from bluebook_core.skill import Chunk, EmbeddingInfo, SkillPackage

from ..cache import StageCache, sha256_str

log = logging.getLogger("bluebook.perception.stages.index")

_CONTEXT_BATCH = 8  # how many chunks to contextualize in one prompt


async def _situating_context(pkg: SkillPackage, chunk: Chunk) -> str:
    """Generate a 1–2 sentence context sentence for Contextual Retrieval."""
    try:
        skill_summary = f"Task: {pkg.title}. {pkg.summary}"
        step_ref = f" (step: {pkg.step(chunk.step_id).title})" if chunk.step_id else ""
        prompt = (
            f"Skill: {skill_summary}\n\n"
            f"Here is a chunk of content from that skill ({chunk.kind}{step_ref}):\n{chunk.text[:500]}\n\n"
            "Write 1–2 sentences that situate this chunk within the skill — what step it belongs to, "
            "what comes before and after, and what makes it distinctive. Be concise."
        )
        ctx = await models.generate("synthesizer", [prompt])
        return str(ctx).strip()
    except (ValueError, RuntimeError, OSError, KeyError) as e:
        log.warning("index: context generation failed for %s: %s", chunk.id, e)
        return ""


def _transcript_windows(pkg: SkillPackage, window_s: float = 30.0, step_s: float = 5.0) -> list[Chunk]:
    chunks = []
    segs = pkg.transcript
    if not segs:
        return chunks
    t_max = max(s.t1 for s in segs)
    t = 0.0
    i = 0
    while t < t_max:
        t1 = t + window_s
        window_segs = [s for s in segs if s.t1 >= t and s.t0 <= t1]
        if window_segs:
            text = " ".join(s.text for s in window_segs)
            chunks.append(Chunk(id=f"tr_{i}", kind="transcript", text=text, t0=t, t1=t1))
            i += 1
        t += step_s
    return chunks


def _step_chunks(pkg: SkillPackage) -> list[Chunk]:
    out = []
    for s in pkg.steps:
        text = f"{s.title}. {s.tooltip} {s.instruction} " + " ".join(s.tips)
        out.append(Chunk(id=f"c_{s.id}", kind="step", text=text.strip(), step_id=s.id, t0=s.t_start, t1=s.t_end))
        # proposition-level: each success criterion and safety warning as its own chunk
        for j, sc in enumerate(s.success_criteria):
            out.append(
                Chunk(
                    id=f"c_{s.id}_sc{j}",
                    kind="step",
                    text=f"Success check for '{s.title}': {sc}",
                    step_id=s.id,
                    t0=s.t_start,
                    t1=s.t_end,
                )
            )
    return out


def _faq_chunks(pkg: SkillPackage) -> list[Chunk]:
    valid_ids = {s.id for s in pkg.steps}
    return [
        Chunk(
            id=f"c_faq{i}",
            kind="faq",
            text=f"Q: {qa.q}\nA: {qa.a}",
            step_id=next((sid for sid in qa.step_ids if sid in valid_ids), None),
        )
        for i, qa in enumerate(pkg.faq)
    ]


def _object_chunks(pkg: SkillPackage) -> list[Chunk]:
    return [Chunk(id=f"c_obj_{o.id}", kind="object", text=f"{o.name}: {o.visual_description}") for o in pkg.objects]


def _keyframe_chunks(pkg: SkillPackage) -> list[tuple[Chunk, bytes | None]]:
    """Returns (chunk, jpeg_bytes_or_None) pairs — keyframes embed image+text."""
    from bluebook_core.store import skill_dir

    pkg_dir = skill_dir(pkg.id)
    out = []
    for s in pkg.steps:
        for j, kf in enumerate(s.keyframes):
            text = kf.caption or f"Keyframe from step '{s.title}' at {kf.t:.1f}s"
            chunk = Chunk(id=f"c_{s.id}_kf{j}", kind="keyframe", text=text, step_id=s.id, t0=kf.t, media=kf)
            jpeg = None
            kf_path = pkg_dir / kf.path
            if kf_path.exists():
                jpeg = kf_path.read_bytes()
            out.append((chunk, jpeg))
    return out


def _safety_chunks(pkg: SkillPackage) -> list[Chunk]:
    valid_ids = {s.id for s in pkg.steps}
    return [
        Chunk(
            id=f"c_safety{i}",
            kind="safety",
            text=f"[{sn.severity.upper()}] {sn.text}",
            step_id=next((sid for sid in sn.step_ids if sid in valid_ids), None),
        )
        for i, sn in enumerate(pkg.safety)
    ]


def _overview_chunk(pkg: SkillPackage) -> Chunk:
    prereqs = ", ".join(pkg.prerequisites) or "none"
    tags = ", ".join(pkg.tags)
    return Chunk(
        id="c_overview",
        kind="overview",
        text=f"{pkg.title}: {pkg.summary} Difficulty: {pkg.difficulty}. Tags: {tags}. Prerequisites: {prereqs}.",
    )


async def run(pkg: SkillPackage, cache: StageCache) -> tuple[SkillPackage, np.ndarray]:
    """Build chunks with Contextual Retrieval and embed. Returns updated pkg + embeddings array."""
    pkg_hash = sha256_str(pkg.model_dump_json())
    cached = cache.load("index", pkg_hash)
    if cached:
        log.info("index: cache hit")
        chunks = [Chunk.model_validate(c) for c in cached["chunks"]]
        emb = np.array(cached["embeddings"], dtype=np.float32)
        pkg = pkg.model_copy(update={"chunks": chunks, "embedding": EmbeddingInfo(**cached["embedding_info"])})
        return pkg, emb

    log.info("index: building chunks …")

    # build text chunks
    text_chunks: list[Chunk] = [_overview_chunk(pkg)]
    text_chunks += _step_chunks(pkg)
    text_chunks += _transcript_windows(pkg)
    text_chunks += _faq_chunks(pkg)
    text_chunks += _object_chunks(pkg)
    text_chunks += _safety_chunks(pkg)

    # add Contextual Retrieval: prepend situating context to each chunk's text
    log.info("index: generating situating context for %d text chunks …", len(text_chunks))
    contexts = await asyncio.gather(*[_situating_context(pkg, c) for c in text_chunks])
    for chunk, ctx in zip(text_chunks, contexts):
        if ctx:
            chunk.text = f"{ctx}\n\n{chunk.text}"

    # keyframe chunks (image + text)
    kf_pairs = _keyframe_chunks(pkg)
    kf_chunks = [pair[0] for pair in kf_pairs]
    kf_images = [pair[1] for pair in kf_pairs]

    all_chunks = text_chunks + kf_chunks
    log.info("index: embedding %d chunks (%d with images) …", len(all_chunks), sum(1 for b in kf_images if b))

    # embed text chunks
    text_items: list[str | bytes] = [c.text for c in text_chunks]
    text_vecs = await models.embed(text_items, task="RETRIEVAL_DOCUMENT")

    # embed keyframe chunks (image bytes when available, else caption text)
    kf_items: list[str | bytes] = [img if img else c.text for c, img in zip(kf_chunks, kf_images)]
    kf_vecs = (
        await models.embed(kf_items, task="RETRIEVAL_DOCUMENT")
        if kf_items
        else np.zeros((0, text_vecs.shape[1] if text_vecs.size else 0), dtype=np.float32)
    )

    embeddings = np.vstack([text_vecs, kf_vecs]) if kf_vecs.size else text_vecs
    dim = int(embeddings.shape[1])
    embedding_info = EmbeddingInfo(model=models.model_id("embedder"), dim=dim)

    pkg = pkg.model_copy(update={"chunks": all_chunks, "embedding": embedding_info})

    cache.save(
        "index",
        pkg_hash,
        {
            "chunks": [c.model_dump() for c in all_chunks],
            "embeddings": embeddings.tolist(),
            "embedding_info": embedding_info.model_dump(),
        },
    )
    log.info("index: done — %d chunks, dim=%d", len(all_chunks), dim)
    return pkg, embeddings
