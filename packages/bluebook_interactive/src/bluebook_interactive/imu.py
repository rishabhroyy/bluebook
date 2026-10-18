"""Glasses IMU <-> head-frame calibration, used to keep the overlay roughly right while the face is not tracked.

The G2 IMU (measured) reports ~ accelerometer in g, |a| ~ 1 at rest, ~10 Hz. Gravity gives pitch and roll, never yaw.
While the face IS tracked in a gravity-aligned world (arkit-world), the head-frame direction of gravity is known from
the face pose, so the fixed rotation R (IMU <- head) can be fitted from (head gravity, IMU vector) pairs (Kabsch with
Huber re-weighting). It is only trusted once the pairs span at least MIN_SPREAD_DEG of distinct tilts (a single tilt
direction leaves the rotation about it undetermined). Spec: workstreams/part3-interactive.md.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from .geom import rot_between

DOWN = np.array([0.0, -1.0, 0.0])  # gravity direction in the (y-up) ARKit world frame

G_GATE = 0.10  # |a| must be within 1 +- this (g) to count as "not accelerating"
LPF_TAU_S = 0.15  # IMU low-pass time constant: ~10 Hz and noisy, but a longer one lags a moving head by tau x speed
GAP_RESET_S = 1.0  # a longer hole in the IMU stream restarts the low-pass
MAX_PAIRS = 300  # stored (head gravity, IMU) pairs; a new pair must differ by MIN_SEP_DEG from all of them
MIN_SEP_DEG = 1.0  # so sitting still for a minute does not drown out the earlier, diverse tilts
REFIT_EVERY = 10  # new pairs between refits
HUBER_DEG = 3.0  # residuals beyond this are down-weighted (bad timing, a bump)
# ok-gate (priors): enough pairs, enough tilt diversity, and a fit that agrees with the data
MIN_PAIRS, MIN_SPREAD_DEG, MAX_RESID_DEG = 20, 4.0, 6.0
STALE_S = 0.5  # the IMU counts as live for the memory if its last sample is younger than this


def _unit(v) -> np.ndarray | None:
    v = np.asarray(v, float)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else None


def _kabsch(a: np.ndarray, b: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Rotation R minimising sum w |b - R a|^2 (rows of a, b)."""
    U, _, Vt = np.linalg.svd((b * w[:, None]).T @ a)
    return U @ np.diag([1.0, 1.0, np.sign(np.linalg.det(U @ Vt)) or 1.0]) @ Vt


class ImuCalib:
    """One glasses session's IMU calibration + low-passed IMU state."""

    def __init__(self) -> None:
        self.lpf: np.ndarray | None = None  # low-passed raw IMU vector (g)
        self.t = -math.inf  # time of the last IMU sample
        self.last_norm = 1.0  # |a| of the last raw sample (g): far from 1 = the head is accelerating
        self.ref: np.ndarray | None = None  # unit IMU direction at the last accepted face pose (see snap)
        self.pairs: deque[tuple[np.ndarray, np.ndarray]] = deque(maxlen=MAX_PAIRS)  # (head gravity, IMU) unit vectors
        self.R: np.ndarray | None = None  # IMU <- head: sign * imu_dir = R @ head_gravity
        self.sign = 1.0  # +1: the IMU vector points where gravity does; -1: it points up (specific force)
        self.residual_deg: float | None = None
        self.spread_deg = 0.0
        self.ok = False
        self._since_fit = 0

    # ------------------------------------------------------------------ input

    def add(self, x: float, y: float, z: float, now: float, g_head: np.ndarray | None) -> None:
        """One IMU sample. g_head: unit gravity direction in the head frame from the tracked face pose, or None when
        it is not trustworthy (face lost, head moving, other pose source) -> the sample only feeds the low-pass."""
        a = np.array([x, y, z], float)
        if self.lpf is None or now - self.t > GAP_RESET_S:
            self.lpf = a.copy()
        else:
            k = 1 - math.exp(-(now - self.t) / LPF_TAU_S)
            self.lpf = self.lpf + k * (a - self.lpf)
        self.t, self.last_norm = now, float(np.linalg.norm(a))
        if g_head is None or abs(float(np.linalg.norm(a)) - 1.0) > G_GATE:
            return
        b, h = _unit(self.lpf), _unit(g_head)
        if b is None or h is None:
            return
        if self.pairs and max(float(h @ p[0]) for p in self.pairs) > math.cos(math.radians(MIN_SEP_DEG)):
            return  # nothing new about the rotation
        self.pairs.append((h, b))
        self._since_fit += 1
        if self._since_fit >= REFIT_EVERY:
            self.fit()

    def snap(self) -> None:
        """A face pose was just accepted: remember the IMU direction that goes with it (the memory works relative to
        it, so a constant calibration error cancels)."""
        self.ref = _unit(self.lpf) if self.lpf is not None else None

    # ------------------------------------------------------------------ fit

    def fit(self) -> None:
        self._since_fit = 0
        if len(self.pairs) < 3:
            self.ok = False
            return
        a = np.array([p[0] for p in self.pairs])
        b = np.array([p[1] for p in self.pairs])
        ev = np.linalg.eigvalsh(a.T @ a / len(a))  # ascending: second moment of the tilt directions
        self.spread_deg = math.degrees(math.asin(min(1.0, math.sqrt(max(float(ev[1]), 0.0)))))
        best = None
        for s in (1.0, -1.0):
            w = np.ones(len(a))
            for _ in range(4):  # IRLS (Huber)
                R = _kabsch(a, s * b, w)
                r = np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", a @ R.T, s * b), -1, 1)))
                w = np.minimum(1.0, HUBER_DEG / np.maximum(r, 1e-6))
            res = float(np.median(r))
            if best is None or res < best[0]:
                best = (res, s, R)
        assert best is not None
        self.residual_deg, self.sign, R = best
        self.R = R  # sign * b = R @ a
        self.ok = len(a) >= MIN_PAIRS and self.spread_deg >= MIN_SPREAD_DEG and self.residual_deg <= MAX_RESID_DEG

    # ------------------------------------------------------------------ use

    def live(self, now: float) -> bool:
        """Calibrated, and the IMU is streaming with a reference taken at the last face pose."""
        return self.ok and self.ref is not None and now - self.t <= STALE_S

    def head_gravity(self, now: float) -> np.ndarray | None:
        """Head-frame gravity (unit) straight from the IMU (sign * b = R @ a), or None: not calibrated, not streaming,
        or the head is accelerating (|a| off 1 g) so the IMU is not measuring gravity alone."""
        if not (self.ok and self.R is not None and self.lpf is not None and now - self.t <= STALE_S):
            return None
        if abs(self.last_norm - 1.0) > G_GATE:
            return None
        b = _unit(self.lpf)
        return None if b is None else self.R.T @ (self.sign * b)

    def tilt_gravity(self, g_old: np.ndarray) -> np.ndarray | None:
        """Head-frame gravity (unit) now, given it was g_old at the last face pose: g_old rotated by the rotation the
        IMU direction has undergone since then (expressed in head coordinates). None if there is no live IMU."""
        b = _unit(self.lpf) if self.lpf is not None else None
        if self.R is None or self.ref is None or b is None:
            return None
        Q = rot_between(self.ref, b)
        return self.R.T @ (Q @ (self.R @ g_old))

    def status(self) -> dict:
        return {
            "ok": self.ok,
            "n": len(self.pairs),
            "residual_deg": None if self.residual_deg is None else round(self.residual_deg, 2),
            "spread_deg": round(self.spread_deg, 1),
            "sign": self.sign if self.R is not None else None,
        }
