"""Pluggable transcription backends → list[TranscriptSegment].

Default: FasterWhisperBackend (local, word-level timestamps, Windows-OK).
Fallback: GeminiBackend (via models.generate; timestamps may drift on long videos).

The backend is an implementation detail of stage 2 — the output contract
(list[TranscriptSegment]) is fixed and shared with Parts 2/3 via SkillPackage.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bluebook_core.skill import TranscriptSegment

log = logging.getLogger("bluebook.perception.transcribe")

_FILLERS = {"uh", "um", "er", "ah", "mm", "hmm", "like", "you know"}


def _clean(text: str) -> str:
    return text.strip()


def _is_filler(text: str) -> bool:
    return text.strip().lower() in _FILLERS


class Transcriber:
    """Abstract base — implement transcribe()."""

    async def transcribe(self, audio_path: Path, video_index: int = 0) -> list[TranscriptSegment]:
        raise NotImplementedError


class FasterWhisperBackend(Transcriber):
    """Local faster-whisper large-v3. Downloads model on first use (~3 GB, cached in ~/.cache)."""

    def __init__(self, model_size: str = "large-v3", device: str = "auto", compute_type: str = "auto"):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = None  # lazy load

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]

            device = self._device
            compute_type = self._compute_type
            if device == "auto":
                try:
                    import torch

                    device = "cuda" if torch.cuda.is_available() else "cpu"
                except ImportError:
                    device = "cpu"
            if compute_type == "auto":
                compute_type = "float16" if device == "cuda" else "int8"
            log.info("loading faster-whisper %s on %s/%s", self._model_size, device, compute_type)
            self._model = WhisperModel(self._model_size, device=device, compute_type=compute_type)
        return self._model

    async def transcribe(self, audio_path: Path, video_index: int = 0) -> list[TranscriptSegment]:
        import asyncio

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio_path, video_index)

    def _transcribe_sync(self, audio_path: Path, video_index: int) -> list[TranscriptSegment]:
        model = self._load()
        segments, _ = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            vad_filter=True,
            language="en",
            log_progress=True,  # tqdm bar over audio seconds, on stderr
        )
        out: list[TranscriptSegment] = []
        for seg in segments:
            if not seg.words:
                text = _clean(seg.text)
                if text and not _is_filler(text):
                    out.append(TranscriptSegment(t0=seg.start, t1=seg.end, text=text, video=video_index))
                continue
            # group words into short segments (break on ~5 s silence or 15-word chunks)
            buf: list = []
            for w in seg.words:
                if _is_filler(w.word.strip()):
                    continue
                if buf and (w.start - buf[-1].end > 2.0 or len(buf) >= 15):
                    text = _clean(" ".join(x.word for x in buf))
                    if text:
                        out.append(TranscriptSegment(t0=buf[0].start, t1=buf[-1].end, text=text, video=video_index))
                    buf = []
                buf.append(w)
            if buf:
                text = _clean(" ".join(x.word for x in buf))
                if text:
                    out.append(TranscriptSegment(t0=buf[0].start, t1=buf[-1].end, text=text, video=video_index))
        log.info("transcribed %d segments from %s", len(out), audio_path.name)
        return out


class GeminiBackend(Transcriber):
    """Gemini fallback transcription. Timestamps may drift on long videos (>5 min)."""

    async def transcribe(self, audio_path: Path, video_index: int = 0) -> list[TranscriptSegment]:
        from bluebook_core import models
        from pydantic import BaseModel

        class _Seg(BaseModel):
            t0: float
            t1: float
            text: str

        class _Result(BaseModel):
            segments: list[_Seg]

        audio_bytes = audio_path.read_bytes()
        part = (
            models.audio_pcm16k(audio_bytes) if audio_path.suffix == ".pcm" else models.image(audio_bytes, "audio/wav")
        )
        prompt = (
            "Transcribe this audio. Return word-level segments with start (t0) and end (t1) times in seconds. "
            "Omit filler words (uh, um, er). Each segment should be a short phrase (one sentence or less)."
        )
        result: _Result = await models.generate("synthesizer", [part, prompt], schema=_Result)
        return [
            TranscriptSegment(t0=s.t0, t1=s.t1, text=_clean(s.text), video=video_index)
            for s in result.segments
            if _clean(s.text)
        ]


def default_transcriber() -> Transcriber:
    return FasterWhisperBackend()
