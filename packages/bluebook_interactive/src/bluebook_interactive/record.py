"""Raw session log for offline replay and tuning (see replay.py): one JSON line per pose-app hello, pose message and
glasses draw ack, stamped with the server receive time. One file per server run (overwritten at start)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("bluebook.interactive")


class Recorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._f = path.open("w", buffering=1)  # line-buffered: a crash loses at most one line
        except OSError:
            log.warning("session recording disabled (%s not writable)", path, exc_info=True)
            self._f = None

    def write(self, kind: str, now: float, data: dict) -> None:
        if self._f is not None:
            self._f.write(json.dumps({"k": kind, "now": round(now, 4), "d": data}) + "\n")
