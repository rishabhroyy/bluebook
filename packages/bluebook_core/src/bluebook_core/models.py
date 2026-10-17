"""The one model client. Every part calls models through these functions so retries,
model ids, and cost logging live in one place.

    from bluebook_core import models
    plan = await models.generate("video_analyst", [models.video(f, fps=2), PROMPT], schema=StepPlan)
    vecs = await models.embed(["turn off the water", img_bytes])

Roles map to model ids in config.Models. Gemini is the only provider for v1 because it is
the mainstream API that natively ingests video *with its audio track* plus timestamps
(see docs/DESIGN.md §4). Add a provider by branching inside generate(), not by subclassing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import numpy as np
from google import genai
from google.genai import types
from pydantic import BaseModel

from .config import config

log = logging.getLogger("bluebook.models")
_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.google_api_key or None)
    return _client


def model_id(role: str) -> str:
    return getattr(config.models, role)


# ------------------------------------------------------------------ content parts


def image(data: bytes, mime: str = "image/jpeg") -> types.Part:
    return types.Part.from_bytes(data=data, mime_type=mime)


def audio_pcm16k(pcm: bytes) -> types.Part:
    """Raw G2 mic audio (16 kHz s16le mono) -> WAV part."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav")


async def upload(path: Path) -> types.File:
    """Upload a video/audio file (File API, needed for >~100MB / long video) and wait until ACTIVE."""
    f = await client().aio.files.upload(file=str(path))
    while f.state == types.FileState.PROCESSING:
        await asyncio.sleep(2)
        f = await client().aio.files.get(name=f.name)
    if f.state != types.FileState.ACTIVE:
        raise RuntimeError(f"upload failed: {path} -> {f.state}")
    return f


def video(f: types.File, fps: float | None = None, start: float | None = None, end: float | None = None) -> types.Part:
    """Reference an uploaded video, optionally clipped and resampled. Audio track is included by the API."""
    meta = types.VideoMetadata(
        fps=fps,
        start_offset=f"{start}s" if start is not None else None,
        end_offset=f"{end}s" if end is not None else None,
    )
    return types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=f.mime_type), video_metadata=meta)


# ---------------------------------------------------------------------- calls


async def generate[T: BaseModel](
    role: str,
    contents: list,
    schema: type[T] | None = None,
    system: str | None = None,
    retries: int = 3,
    **cfg,
) -> T | str:
    """One-shot generation. With `schema`, returns a validated instance (structured output)."""
    conf = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json" if schema else None,
        response_schema=schema,
        **cfg,
    )
    mid = model_id(role)
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            r = await client().aio.models.generate_content(model=mid, contents=contents, config=conf)
            u = r.usage_metadata
            log.info(
                "%s %s %.2fs in=%s out=%s",
                role,
                mid,
                time.monotonic() - t0,
                u and u.prompt_token_count,
                u and u.candidates_token_count,
            )
            if schema is None:
                return r.text or ""
            return r.parsed if isinstance(r.parsed, schema) else schema.model_validate_json(r.text or "")
        except Exception as e:  # ponytail: retry everything; narrow to 429/5xx if this masks bugs
            if attempt == retries - 1:
                raise
            log.warning("%s attempt %d failed: %s", role, attempt + 1, e)
            await asyncio.sleep(2**attempt)
    raise AssertionError("unreachable")


async def embed(items: list[str | bytes], task: str = "RETRIEVAL_DOCUMENT") -> np.ndarray:
    """Embed text and/or JPEG bytes. Returns float32 [n, dim], L2-normalised.
    Use task="RETRIEVAL_QUERY" for queries."""
    contents = [i if isinstance(i, str) else image(i) for i in items]
    out = []
    for c in contents:  # ponytail: one call per item; batch when ingest time matters
        r = await client().aio.models.embed_content(
            model=model_id("embedder"), contents=c, config=types.EmbedContentConfig(task_type=task)
        )
        out.append(r.embeddings[0].values)
    v = np.asarray(out, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)
