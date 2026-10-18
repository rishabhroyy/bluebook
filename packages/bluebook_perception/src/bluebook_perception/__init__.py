"""Part 1 — perception/learning pipeline. Spec: workstreams/part1-perception.md"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from .pipeline import run as _run


async def ingest(videos: list[Path], skill_id: str, title: str | None = None):
    """Python API: video paths → saved SkillPackage. Called by CLI and optionally by Part 2."""
    return await _run(videos=videos, skill_id=skill_id, title=title)


def setup(app: FastAPI) -> None:
    """Mount ingest HTTP routes. Called once by bluebook_core.server."""
    from .http import router

    app.include_router(router)
