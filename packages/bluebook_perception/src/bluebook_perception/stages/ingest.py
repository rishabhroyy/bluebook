"""Stage 1: ingest — normalise video, extract audio, record SourceVideo metadata."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from bluebook_core.skill import SourceVideo
from bluebook_core.store import skill_dir

from ..cache import StageCache, sha256_file
from ..media import extract_audio_wav, normalize, video_metadata

log = logging.getLogger("bluebook.perception.stages.ingest")


def run(video_path: Path, skill_id: str, cache: StageCache) -> dict:
    """Return dict with keys: source_video (SourceVideo dict), normalized_path str, audio_path str."""
    input_hash = sha256_file(video_path)
    cached = cache.load("ingest", input_hash)
    if cached:
        log.info("ingest: cache hit for %s", video_path.name)
        return cached

    pkg_dir = skill_dir(skill_id)
    source_dir = pkg_dir / "source"
    media_dir = pkg_dir / "media"
    source_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    # copy original untouched
    source_copy = source_dir / video_path.name
    if not source_copy.exists():
        shutil.copy2(video_path, source_copy)
    log.info("ingest: source saved to %s", source_copy)

    # probe original (before normalize, preserves original metadata)
    meta = video_metadata(video_path)
    if not meta["has_audio"]:
        log.warning("ingest: %s has NO audio track — narration quality will be zero", video_path.name)

    # normalize
    normalized = media_dir / f"normalized_{video_path.stem}.mp4"
    if not normalized.exists():
        log.info("ingest: normalizing %s …", video_path.name)
        normalize(video_path, normalized)

    # extract audio
    audio_path = media_dir / "audio.wav"
    if not audio_path.exists():
        log.info("ingest: extracting audio …")
        extract_audio_wav(normalized, audio_path)

    source_video = SourceVideo(
        path=str(source_copy.relative_to(pkg_dir)),
        sha256=input_hash,
        duration_s=meta["duration_s"],
        width=meta["width"],
        height=meta["height"],
        fps=meta["fps"],
        has_audio=meta["has_audio"],
    )

    result = {
        "source_video": source_video.model_dump(),
        "normalized_path": str(normalized),
        "audio_path": str(audio_path),
    }
    cache.save("ingest", input_hash, result)
    log.info(
        "ingest: done — %.1f s, %dx%d, audio=%s", meta["duration_s"], meta["width"], meta["height"], meta["has_audio"]
    )
    return result
