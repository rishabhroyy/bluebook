"""ffprobe/ffmpeg wrappers, audio extraction, keyframe decode + Laplacian sharpness, box conversion."""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
from pathlib import Path

import numpy as np
from bluebook_core.skill import Box2D
from PIL import Image

log = logging.getLogger("bluebook.perception.media")

# Common winget install location as fallback when ffmpeg isn't on PATH
_WINGET_FFMPEG = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"


def _find_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    # Search winget packages directory (Windows)
    for candidate in _WINGET_FFMPEG.glob(f"Gyan.FFmpeg*/**/bin/{name}.exe"):
        return str(candidate)
    return name  # fall back to bare name and let subprocess raise a clear error


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


# ------------------------------------------------------------------ ffprobe


def probe(path: Path) -> dict:
    """Return ffprobe JSON for a video file."""
    cmd = [
        FFPROBE,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def video_metadata(path: Path) -> dict:
    """Return dict with duration_s, width, height, fps, has_audio."""
    info = probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = any(s.get("codec_type") == "audio" for s in streams)
    if not video:
        raise ValueError(f"no video stream in {path}")
    w = int(video.get("width", 0))
    h = int(video.get("height", 0))
    # fps from avg_frame_rate "num/den"
    fr = video.get("avg_frame_rate", "0/1")
    num, _, den = fr.partition("/")
    fps = float(num) / float(den) if float(den) else 0.0
    duration = float(video.get("duration") or info["format"].get("duration", 0))
    return {"duration_s": duration, "width": w, "height": h, "fps": fps, "has_audio": audio}


# ------------------------------------------------------------------ ffmpeg normalize


def normalize(src: Path, dst: Path) -> None:
    """Re-encode to H.264 ≤1080p, keep AAC audio. dst must not exist."""
    cmd = [
        FFMPEG,
        "-y",
        "-i",
        str(src),
        "-vf",
        "scale='min(iw,1920)':'min(ih,1080)':force_original_aspect_ratio=decrease",
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "fast",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def extract_audio_wav(src: Path, dst: Path) -> None:
    """Extract 16 kHz mono WAV from src into dst."""
    cmd = [
        FFMPEG,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(dst),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


# ------------------------------------------------------------------ keyframes


def _laplacian_variance(img: Image.Image) -> float:
    """Measure sharpness via variance of Laplacian (higher = sharper)."""
    grey = np.array(img.convert("L"), dtype=np.float32)
    # 3×3 Laplacian kernel
    kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    from numpy.lib.stride_tricks import sliding_window_view

    windows = sliding_window_view(grey, (3, 3))
    conv = (windows * kernel).sum(axis=(-2, -1))
    return float(conv.var())


def _frames_around(video_path: Path, t: float, window: float = 0.5, max_frames: int = 15) -> list[Image.Image]:
    """Extract up to max_frames from [t-window, t+window] using ffmpeg pipe."""
    t0 = max(0.0, t - window)
    duration = window * 2
    cmd = [
        FFMPEG,
        "-ss",
        str(t0),
        "-t",
        str(duration),
        "-i",
        str(video_path),
        "-vf",
        f"fps={max_frames / duration:.2f}",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    r = subprocess.run(cmd, check=False, capture_output=True)
    frames: list[Image.Image] = []
    buf = r.stdout
    # split JPEG stream on SOI markers
    idx = 0
    while True:
        start = buf.find(b"\xff\xd8", idx)
        if start == -1:
            break
        end = buf.find(b"\xff\xd8", start + 2)
        jpeg = buf[start:end] if end != -1 else buf[start:]
        try:
            frames.append(Image.open(io.BytesIO(jpeg)))
        except OSError:
            pass
        if end == -1:
            break
        idx = end
    return frames


def sharpest_frame(video_path: Path, t: float) -> Image.Image | None:
    """Return the sharpest JPEG frame in a ±0.5 s window around t."""
    frames = _frames_around(video_path, t)
    if not frames:
        return None
    return max(frames, key=_laplacian_variance)


def save_keyframe(img: Image.Image, dst: Path, max_px: int = 1280) -> None:
    """Resize if needed and save as JPEG."""
    if max(img.width, img.height) > max_px:
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, "JPEG", quality=85)


# ------------------------------------------------------------------ box conversion


def gemini_box_to_box2d(gemini_box: list[float]) -> Box2D:
    """Convert Gemini [ymin, xmin, ymax, xmax] / 1000 → Box2D x0,y0,x1,y1 in [0,1]."""
    ymin, xmin, ymax, xmax = [v / 1000.0 for v in gemini_box]
    return Box2D(x0=xmin, y0=ymin, x1=xmax, y1=ymax)
