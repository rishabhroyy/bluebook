"""Stage cache: data/work/<id>/<stage>.json keyed by input sha256.

Re-running the pipeline recomputes only stages whose input hash changed.
--force-stage NAME invalidates that stage and every stage after it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from bluebook_core.config import config

log = logging.getLogger("bluebook.perception.cache")

STAGES = [
    "ingest",
    "transcribe",
    "outline",
    "detail",
    "keyframes",
    "synthesize",
    "verify",
    "index",
]


def _work_dir(skill_id: str) -> Path:
    return config.data_dir / "work" / skill_id


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class StageCache:
    def __init__(self, skill_id: str, force_stage: str | None = None):
        self._dir = _work_dir(skill_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._invalidated: set[str] = set()
        if force_stage and force_stage in STAGES:
            idx = STAGES.index(force_stage)
            for s in STAGES[idx:]:
                self._invalidated.add(s)
                p = self._dir / f"{s}.json"
                if p.exists():
                    p.unlink()
                    log.info("cache: invalidated %s", s)

    def _path(self, stage: str) -> Path:
        return self._dir / f"{stage}.json"

    def load(self, stage: str, input_hash: str) -> Any | None:
        if stage in self._invalidated:
            return None
        p = self._path(stage)
        if not p.exists():
            return None
        try:
            envelope = json.loads(p.read_text())
            if envelope.get("input_hash") != input_hash:
                log.info("cache: %s hash mismatch, recomputing", stage)
                return None
            return envelope["data"]
        except (OSError, KeyError, ValueError):
            return None

    def save(self, stage: str, input_hash: str, data: Any) -> None:
        envelope = {"input_hash": input_hash, "data": data}
        p = self._path(stage)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(envelope, indent=2))
        tmp.replace(p)
        log.info("cache: saved %s", stage)
