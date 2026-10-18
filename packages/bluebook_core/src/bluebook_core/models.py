"""The one model client. Every part calls models through these functions so retries,
model ids, and cost logging live in one place.

    from bluebook_core import models
    plan = await models.generate("video_analyst", [models.video(f, fps=2), PROMPT], schema=StepPlan)
    vecs = await models.embed(["turn off the water", img_bytes])

Roles map to model ids in config.Models. Gemini is the only provider for v1 because it is
the mainstream API that natively ingests video *with its audio track* plus timestamps
(see docs/DESIGN.md §4). Add a provider by branching inside generate(), not by subclassing.
Exception: box() asks TypeSafe's Jev for an object's box (Part 3 grounding, ~100 ms; Gemini then replaces it).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel

from .config import config

log = logging.getLogger("bluebook.models")
_client: genai.Client | None = None
_ts: httpx.AsyncClient | None = None  # one pooled connection: a fresh TLS handshake per call would dominate latency
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"


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
    if not items:
        return np.zeros((0, 0), dtype=np.float32)
    contents = [i if isinstance(i, str) else image(i) for i in items]
    out = []
    # One call per item on purpose: multimodal embedding models may merge all parts of one
    # request into a single vector. ponytail: parallelise with asyncio.gather if ingest is slow.
    for c in contents:
        r = await client().aio.models.embed_content(
            model=model_id("embedder"), contents=c, config=types.EmbedContentConfig(task_type=task)
        )
        out.append(r.embeddings[0].values)
    v = np.asarray(out, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


# ---------------------------------------------------------------------- TypeSafe box


def _jev_image(data: bytes, side: int = 1568) -> bytes:
    """Jev never resizes: each side must be 280..1568 px. Shrink to fit `side`; upscale a small crop as far as the
    limits allow (short side to 280, long side at most `side`), then pad (black) what is still under 280. JPEG."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((side, side))
    if (s := min(280 / min(im.size), side / max(im.size))) > 1:
        im = im.resize((round(im.width * s), round(im.height * s)), Image.Resampling.LANCZOS)
    if min(im.size) < 280:
        canvas = Image.new("RGB", (max(im.width, 280), max(im.height, 280)))
        canvas.paste(im, ((canvas.width - im.width) // 2, (canvas.height - im.height) // 2))
        im = canvas
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


async def box(
    role: str, image: bytes, target: str, refs: tuple[bytes, ...] | list[bytes] = (), retries: int = 2
) -> tuple[tuple[float, float, float, float], float]:
    """Box `target` (an instruction naming `frame`, e.g. "the mug in `frame`") in the JPEG `image` with TypeSafe Jev:
    ((x0, y0, x1, y1) normalised, confidence). Box always answers, even when the target is absent. `refs`: reference
    crops, referable as `ref0`, `ref1`, ... in `target`."""
    global _ts
    if _ts is None:
        _ts = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.typesafe_api_key}", "TypeSafe-Danger-Zone-Images": "true"},
            timeout=15,
        )
    req = {
        "model": model_id(role),
        "state": {"frame": "f", **{f"ref{i}": f"r{i}" for i in range(len(refs))}},
        "assets": {k: {"type": "image", "part": k} for k in ["f", *(f"r{i}" for i in range(len(refs)))]},
        "questions": {"box": {"type": "box", "instructions": target}},
    }
    files = {"request": (None, json.dumps(req), "application/json"), "f": ("f.jpg", _jev_image(image), "image/jpeg")}
    files |= {f"r{i}": (f"r{i}.jpg", _jev_image(r, 512), "image/jpeg") for i, r in enumerate(refs)}
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            r = await _ts.post(TYPESAFE_URL, files=files)
            r.raise_for_status()
            a = r.json()["answers"]["box"]
            e = a["box"]
            log.info("%s %s %.2fs conf=%.2f", role, req["model"], time.monotonic() - t0, a["confidence"])
            return (e["left"], e["top"], e["right"], e["bottom"]), float(a["confidence"])
        except (httpx.HTTPError, KeyError, ValueError) as e:
            if attempt == retries - 1:
                raise
            log.warning("%s attempt %d failed: %s", role, attempt + 1, e)
            await asyncio.sleep(0.2)
    raise AssertionError("unreachable")
