"""All runtime config, from env (and .env via `uv run --env-file .env`). One place, no per-part config files."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Models:
    """Model id per role. Change models here (env), never hard-code ids in part code.
    Ids verified against ai.google.dev/gemini-api/docs/models on 2026-10-17."""

    video_analyst: str = _env("BB_MODEL_VIDEO", "gemini-3.8-flash")  # Part 1: video+audio understanding
    synthesizer: str = _env("BB_MODEL_SYNTH", "gemini-3.8-flash")  # Part 1: FAQ/safety/consistency passes
    tutor: str = _env("BB_MODEL_TUTOR", "gemini-3.8-flash")  # Part 2: conversation + audio questions
    scene: str = _env("BB_MODEL_SCENE", "gemini-3.5-flash-lite")  # Part 2: periodic scene checks (cheap, frequent)
    detector: str = _env("BB_MODEL_DETECT", "gemini-3.8-flash")  # Part 3: box_2d grounding
    embedder: str = _env("BB_MODEL_EMBED", "gemini-embedding-2-preview")  # multimodal; all parts


@dataclass(frozen=True)
class Config:
    host: str = _env("BB_HOST", "127.0.0.1")  # tailscale serve proxies https -> here
    port: int = int(_env("BB_PORT", "8787"))
    # Public origin as seen by the phone, e.g. https://laptop.tail1234.ts.net — must be in apps/g2/app.json whitelist
    public_url: str = _env("BB_PUBLIC_URL", "http://localhost:8787")
    data_dir: Path = Path(_env("BB_DATA_DIR", "data"))
    google_api_key: str = _env("GEMINI_API_KEY", "")
    interactive: bool = _env("BB_INTERACTIVE", "0") == "1"  # enable Part 3
    models: Models = field(default_factory=Models)

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"


config = Config()
