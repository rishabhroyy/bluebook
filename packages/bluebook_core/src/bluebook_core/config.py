"""All runtime config, from env vars and ./.env. One place, no per-part config files."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def read_env(path: Path = Path(".env")) -> dict[str, str]:
    """Parse KEY=VALUE lines. ponytail: no quoting/expansion; `bluebook bootstrap` writes it that way."""
    out = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() and not k.lstrip().startswith("#"):
                out[k.strip()] = v.strip()
    return out


for _k, _v in read_env().items():  # ./.env never overrides real env vars
    os.environ.setdefault(_k, _v)


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
    boxer: str = _env("BB_MODEL_BOX", "jev-preview")  # Part 3: TypeSafe Jev Box grounding (needs TYPESAFE_API_KEY)
    embedder: str = _env("BB_MODEL_EMBED", "gemini-embedding-2-preview")  # multimodal; all parts


@dataclass(frozen=True)
class Config:
    host: str = _env("BB_HOST", "127.0.0.1")  # tailscale serve proxies https -> here
    port: int = int(_env("BB_PORT", "8787"))
    data_dir: Path = Path(_env("BB_DATA_DIR", "data"))
    google_api_key: str = _env("GEMINI_API_KEY", "")
    typesafe_api_key: str = _env("TYPESAFE_API_KEY", "")  # Part 3 object boxes (Jev); required when BB_INTERACTIVE=1
    interactive: bool = _env("BB_INTERACTIVE", "0") == "1"  # enable Part 3
    models: Models = field(default_factory=Models)

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"


config = Config()
