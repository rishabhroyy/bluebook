"""The G2 display as a camera: optics prior, per-glasses persistence, and the online boresight learner."""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Annotated

import numpy as np
from pydantic import BaseModel, Field, ValidationError

from .geom import K_of, az_el, dir_of, rot_between

log = logging.getLogger("bluebook.interactive")

R0 = np.diag([-1.0, -1.0, 1.0])  # head +x (wearer's left) is display -u, head +y (up) is display -v
F = Annotated[float, Field(allow_inf_nan=False)]


class DisplayModel(BaseModel):
    """Display optics + eye offset in the head frame (metres) + the learned boresight per pose source.

    Optics prior from the published G2 spec: 27.5 deg diagonal FOV over the 640x350 panel = 0.0377 deg/px, i.e.
    f ~ 1521 px on our 576x288 canvas = 21.4 deg across it; principal point = canvas centre; eye offset 0; the
    display axis is assumed to be the head's +z (rot = identity). Everything wearer- or fit-specific beyond this is
    learned (BoresightLearner), not hard-coded.
    """

    fov_deg: float = Field(21.4, gt=5, lt=120)
    dx: float = Field(0.0, ge=-0.5, le=0.5)
    dy: float = Field(0.0, ge=-0.5, le=0.5)
    dz: float = Field(0.0, ge=-0.5, le=0.5)
    cx: float = Field(288.0, ge=0, le=576)
    cy: float = Field(144.0, ge=0, le=288)
    boresight: dict[str, Annotated[list[F], Field(min_length=3, max_length=3)]] = {}  # source -> [az, el, dwell_s]

    @property
    def f(self) -> float:
        return 288 / math.tan(math.radians(self.fov_deg / 2))

    def K(self) -> np.ndarray:
        return K_of(self.f, self.f, self.cx, self.cy)

    def eye(self) -> np.ndarray:
        return np.array([self.dx, self.dy, self.dz])

    def R_d(self, bore: tuple[float, float, float] | None = None) -> np.ndarray:
        """Head frame -> display rays. With a learned boresight (az, el deg, confidence), the direction the wearer
        habitually looks along is rotated onto the principal point (the display centre they look through) — the same
        point the un-learned prior uses for head-forward, so learning never makes the mark slide to a new aim."""
        # No stored manual rotation any more: an old file's "rot" (an 8.7 deg tilt measured under a since-fixed
        # convention) silently pushed every mark ~230 px down. Unknown keys in old files are ignored on load.
        R = R0
        if bore is not None and bore[2] > 0:
            R = (
                rot_between(R @ dir_of(bore[0], bore[1]), np.linalg.inv(self.K()) @ np.array([self.cx, self.cy, 1.0]))
                @ R
            )
        return R

    def P(self, bore: tuple[float, float, float] | None = None) -> np.ndarray:
        """3x4 head frame (origin = eye midpoint) -> display px: K [R_d | -R_d e]."""
        Rd = self.R_d(bore)
        return self.K() @ np.c_[Rd, -Rd @ self.eye()]


class BoresightLearner:
    """Learns where the wearer's gaze axis sits in the head frame, from how they actually use the glasses.

    Observations must come from GROUND-TRUTH targets only (the phone, whose position is known exactly): a Gemini- or
    tap-placed object may be in the wrong place, and learning from it would corrupt the boresight (the caller,
    Renderer.tick, enforces this).

    ASSUMPTION (the one thing this relies on): when the wearer holds their head still on the target for a while,
    they are looking at it through the middle of the display. So while the head is STILL for >= DWELL_S and the
    target is within +-GATE_DEG of head-forward, the target's (azimuth, elevation) in the head frame is an
    observation of the display's angular offset. Different wearers / fits / pose sources (ARKit vs Vision
    reference "straight" differently) all show up as a different offset, so it is kept per (glasses, source).

    Estimate = running mean with a prior of TAU0_S seconds of dwell at offset 0 (so the first seconds are shrunk
    toward "no offset"), observations Huber-clipped around the estimate (the clip is wide while unsure and tight
    once confident, so a few seconds of looking elsewhere cannot wreck it), weight capped at W_MAX_S so it stays
    adaptive, result bounded to +-LIMIT_DEG. Converges in ~10-20 s of dwell; confidence = w / (w + TAU0_S).
    """

    # Conservative priors (the owner saw the set point shift a lot with the first, faster settings): a longer dwell,
    # a narrower gate, a stronger pull toward "no offset" (TAU0_S s of virtual dwell at 0), tighter outlier clipping.
    # Converges over a minute or two of looking at the phone, not seconds.
    DWELL_S, GATE_DEG, LIMIT_DEG = 1.5, 12.0, 15.0
    TAU0_S, W_MAX_S = 10.0, 120.0
    CLIP_DEG = (2.5, 8.0)  # Huber threshold when confident / when unsure

    def __init__(self, state: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        self.az, self.el, self.w = state
        self.still_since: float | None = None
        self.dirty = False
        self.last_update: float | None = None  # clock time of the last accepted observation
        self.n_obs = 0

    @property
    def confidence(self) -> float:
        return self.w / (self.w + self.TAU0_S)

    @property
    def bore(self) -> tuple[float, float, float]:
        return self.az, self.el, self.confidence

    def reset(self) -> None:
        self.az = self.el = self.w = 0.0
        self.still_since, self.dirty = None, True
        self.last_update, self.n_obs = None, 0

    def observe(self, d_eye, still: bool, now: float, dt: float) -> None:
        """d_eye = direction from the eye to the target in the head frame; dt = time since the last call."""
        az, el = az_el(d_eye)
        if not still or abs(az) > self.GATE_DEG or abs(el) > self.GATE_DEG:
            self.still_since = None
            return
        if self.still_since is None:
            self.still_since = now
        if now - self.still_since < self.DWELL_S:
            return
        dt = min(dt, 0.5)
        e = np.array([az - self.az, el - self.el])
        clip = self.CLIP_DEG[0] + (self.CLIP_DEG[1] - self.CLIP_DEG[0]) * self.TAU0_S / (self.TAU0_S + self.w)
        if (n := float(np.linalg.norm(e))) > clip:
            e *= clip / n
        k = dt / (self.TAU0_S + self.w + dt)
        self.az = float(np.clip(self.az + k * e[0], -self.LIMIT_DEG, self.LIMIT_DEG))
        self.el = float(np.clip(self.el + k * e[1], -self.LIMIT_DEG, self.LIMIT_DEG))
        self.w = min(self.w + dt, self.W_MAX_S)
        self.dirty = True
        self.last_update, self.n_obs = now, self.n_obs + 1


class Display:
    """One glasses session's display model + its boresight learners (one per pose source), persisted together
    in <calib_dir>/display_<session>.json."""

    SAVE_EVERY_S = 5.0

    def __init__(self, session: str, calib_dir: Path) -> None:
        self.path = calib_dir / f"display_{re.sub(r'[^A-Za-z0-9_.-]', '_', session)}.json"
        try:
            self.model = DisplayModel.model_validate_json(self.path.read_text())
        except (OSError, ValidationError):
            self.model = DisplayModel()
        self.learners = {s: BoresightLearner(tuple(v)) for s, v in self.model.boresight.items()}  # type: ignore[arg-type]
        self._saved = 0.0

    def learner(self, source: str) -> BoresightLearner:
        return self.learners.setdefault(source, BoresightLearner())

    def P(self, source: str) -> np.ndarray:
        return self.model.P(self.learner(source).bore)

    def set(self, **kw) -> DisplayModel:
        """Update the given fields and persist. Raises ValidationError on out-of-range values."""
        self.model = DisplayModel(**{**self.model.model_dump(), **{k: v for k, v in kw.items() if v is not None}})
        self.save()
        return self.model

    def snapshot(self) -> DisplayModel:
        self.model.boresight = {s: [l.az, l.el, l.w] for s, l in self.learners.items()}
        return self.model

    def save(self) -> None:
        self.snapshot()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.model.model_dump_json())
        except OSError:
            log.warning("could not save %s", self.path, exc_info=True)
        for learner in self.learners.values():
            learner.dirty = False

    def maybe_save(self, now: float) -> None:
        if now - self._saved >= self.SAVE_EVERY_S and any(l.dirty for l in self.learners.values()):
            self._saved = now
            self.save()
