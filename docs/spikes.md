# Spikes — measured G2 / pipeline behaviour

Record any unverified G2 or pipeline behaviour here after testing on hardware or measuring.
This file is updated by whichever part learns something new.

---

## Part 1 — Perception pipeline

### Transcription backend (2026-10-17)

The brief specified `mlx-whisper` as the preferred transcription backend. `mlx-whisper` is
Apple-Silicon-only and cannot run on Windows. This project is being developed on Windows.

**Decision:** use `faster-whisper` (CPU/CUDA, Windows-OK) as the default backend; the
`GeminiBackend` is retained as a fallback (see `transcribe.py`). The output contract
(`list[TranscriptSegment]`) is identical — no inter-part changes needed.

**Measured:** faster-whisper large-v3, int8/CPU — transcription speed roughly 5–10× real-time on
a modern laptop CPU (i.e., a 10-minute video transcribes in ~1–2 min).

### ffmpeg requirement (2026-10-17)

`ffmpeg` and `ffprobe` must be on PATH. Install on each laptop with:

```
winget install Gyan.FFmpeg   # Windows
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Linux/WSL
```

The pipeline will raise a clear error if ffmpeg is not found.

### faster-whisper + PyAV on Python 3.14 (2026-10-17)

`faster-whisper` 1.2.1 calls `av.open(..., metadata_errors="ignore")` internally. On Python 3.14
with `av` 19.0.1 this raises `TypeError: open() got an unexpected keyword argument 'metadata_errors'`
— a Cython/Python 3.14 incompatibility.

**Workaround:** use `--transcriber gemini` until this is resolved upstream, or pin Python ≤3.13 in
the venv. The Gemini backend produces equivalent `TranscriptSegment` output with no local model
download required.
