"""Optional HTTP routes: POST /ingest (multipart) → {job_id}, GET /ingest/{job_id} → status."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from .pipeline import run as pipeline_run

log = logging.getLogger("bluebook.perception.http")
router = APIRouter(prefix="/ingest", tags=["ingest"])

# In-memory job store (per-process, per-laptop; no queue needed per spec)
_jobs: dict[str, dict] = {}


class JobStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "done", "error"]
    skill_id: str
    error: str | None = None


@router.post("", response_model=dict)
async def start_ingest(
    file: UploadFile,
    skill_id: str,
    title: str | None = None,
    force_stage: str | None = None,
) -> dict:
    """Accept a multipart video upload, start ingest pipeline as a background task."""
    job_id = str(uuid.uuid4())[:8]
    suffix = Path(file.filename or "upload").suffix or ".mp4"
    tmp = Path(tempfile.gettempdir()) / f"bb_ingest_{job_id}{suffix}"
    contents = await file.read()
    tmp.write_bytes(contents)

    _jobs[job_id] = {"status": "pending", "skill_id": skill_id, "error": None}
    asyncio.create_task(_run_job(job_id, tmp, skill_id, title, force_stage))
    log.info("ingest job %s started for skill '%s'", job_id, skill_id)
    return {"job_id": job_id}


async def _run_job(job_id: str, video_path: Path, skill_id: str, title: str | None, force_stage: str | None) -> None:
    _jobs[job_id]["status"] = "running"
    try:
        await pipeline_run(videos=[video_path], skill_id=skill_id, title=title, force_stage=force_stage)
        _jobs[job_id]["status"] = "done"
        log.info("ingest job %s done", job_id)
    except (ValueError, OSError, RuntimeError) as e:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(e)
        log.error("ingest job %s failed: %s", job_id, e)
    finally:
        if video_path.exists():
            video_path.unlink()


@router.get("/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str) -> JobStatus:
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    j = _jobs[job_id]
    return JobStatus(job_id=job_id, status=j["status"], skill_id=j["skill_id"], error=j["error"])
