"""Signal processing for Part 3: noise estimation, the head-pose Kalman filter, and the on-screen smoother.

Everything is time-step aware (variable dt, dropouts) and takes its scales from measured noise or from the
display, not from constants tuned for one sensor. No I/O, no globals.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from .geom import rodrigues, rodrigues_inv, rot_angle

MEDIAN_CHI2_1 = 0.4549  # median of a chi-square variable with 1 dof: median(x^2) = 0.4549 sigma^2 for Gaussian x


# ------------------------------------------------------------------------------------------ measurement noise


class NoiseEstimator:
    """Per-axis measurement noise of a raw pose stream, learned live from the second difference of the samples.

    A smooth motion (constant velocity) cancels in x2 - x1 - rho (x1 - x0), so what is left is noise (plus a little
    acceleration), which makes this work while the head moves. The variance is the MEDIAN of the recent squared
    second differences / 0.4549 (the chi-square(1) median): robust to glitches, dropouts and fast moves without
    any hand-set cap. Rotation uses body-frame increments (deg), position metres. Until MIN_N samples exist the
    priors are used (a typical phone-vision pose: 1 deg, 2 cm), so the first second is conservative.
    """

    PRIOR_ROT_DEG, PRIOR_POS_M = 1.0, 0.02
    FLOOR_ROT_DEG, FLOOR_POS_M = 0.05, 1e-3  # sensor quantisation: a perfect-looking stream is not noiseless
    WINDOW, MIN_N, MAX_DT = 300, 30, 0.15  # 10 s of history: the median's own error is ~15%

    def __init__(self) -> None:
        self._raw: deque[tuple[np.ndarray, np.ndarray, float]] = deque(maxlen=3)
        self._rot: deque[np.ndarray] = deque(maxlen=self.WINDOW)
        self._pos: deque[np.ndarray] = deque(maxlen=self.WINDOW)

    def update(self, R: np.ndarray, t: np.ndarray, ts: float) -> tuple[np.ndarray, np.ndarray]:
        """Feed one raw pose; returns (rotation variance deg^2 per body axis, position variance m^2 per axis)."""
        if self._raw and ts <= self._raw[-1][2]:
            return self.variances()
        self._raw.append((R, np.asarray(t, float), ts))
        if len(self._raw) == 3:
            (R0, t0, s0), (R1, t1, s1), (R2, t2, s2) = self._raw
            dt1, dt2 = s1 - s0, s2 - s1
            rho = dt2 / dt1
            if max(dt1, dt2) <= self.MAX_DT and 0.5 <= rho <= 2.0:
                c = 1 + (1 + rho) ** 2 + rho**2  # var(second difference) = c sigma^2 (6 for uniform steps)
                d2r = np.degrees(rodrigues_inv(R1.T @ R2) - rho * rodrigues_inv(R0.T @ R1))
                d2p = (t2 - t1) - rho * (t1 - t0)
                self._rot.append(d2r**2 / c)
                self._pos.append(d2p**2 / c)
        return self.variances()

    def variances(self) -> tuple[np.ndarray, np.ndarray]:
        if len(self._rot) < self.MIN_N:
            return np.full(3, self.PRIOR_ROT_DEG**2), np.full(3, self.PRIOR_POS_M**2)
        r = np.median(self._rot, axis=0) / MEDIAN_CHI2_1
        p = np.median(self._pos, axis=0) / MEDIAN_CHI2_1
        return np.maximum(r, self.FLOOR_ROT_DEG**2), np.maximum(p, self.FLOOR_POS_M**2)

    @property
    def rot_deg(self) -> float:
        return float(math.sqrt(self.variances()[0].mean()))

    @property
    def pos_cm(self) -> float:
        return float(math.sqrt(self.variances()[1].mean()) * 100)


# ----------------------------------------------------------------------------------------------- head Kalman


@dataclass
class HeadPose:
    """A head pose (cam <- head) with the uncertainty the filter has about it."""

    R: np.ndarray
    p: np.ndarray
    sig_rot: np.ndarray  # rad, per body axis
    sig_pos: np.ndarray  # m, per camera axis

    def ang_sigma(self, X_cam: np.ndarray) -> float:
        """1-sigma angular error (rad) of the direction to X_cam seen from the head. Rotation about the line of
        sight and position error along it do not move the direction, so each axis counts by (1 - l_i^2)."""
        rel = np.asarray(X_cam, float) - self.p
        dist = max(float(np.linalg.norm(rel)), 0.2)
        l_cam = rel / dist
        l_body = self.R.T @ l_cam
        s_rot2 = float(np.sum(self.sig_rot**2 * (1 - l_body**2)))
        s_pos2 = float(np.sum(self.sig_pos**2 * (1 - l_cam**2))) / dist**2
        return math.sqrt(s_rot2 + s_pos2)


class HeadKalman:
    """Constant-velocity Kalman filter on the head pose: rotation on SO(3) (body-frame error state, deg) and
    position (m), one [value, rate] channel per axis, dt from the measurement timestamps. Measurement noise comes
    in with every update (NoiseEstimator), so nothing about the sensor is baked in.

    Robustness: (1) adaptive manoeuvre detection - when the normalised innovation says a real move started, the
    process noise is opened up at once so the filter follows without lag; (2) gross outliers (innovation far
    beyond what noise or a real manoeuvre explains, e.g. a 30 deg single-frame glitch) are not applied: the
    filter coasts, and only accepts the jump if the NEXT measurement agrees with it; (3) a gap longer than
    reset_s re-initialises. predict() extrapolates with a velocity that has been soft-thresholded by its own
    uncertainty, so a still head predicts a still pose instead of extrapolating velocity noise.
    """

    def __init__(
        self,
        q_rot: tuple[float, float] = (10.0, 3000.0),  # (still, manoeuvre) angular-acceleration noise, deg^2/s^3
        q_pos: tuple[float, float] = (1e-5, 0.5),  # m^2/s^3
        nis_gate: float = 3.0,  # innovation beyond this many sigma = manoeuvre (6 smoothed more but lagged on hardware)
        nis_tau: float = 0.4,  # s, averaging time of the innovation statistic
        bias_tau: float = 0.15,  # s, averaging time of the signed (drift) innovation
        bias_gate: float = 2.0,  # averaged signed innovation beyond this many sigma = drift = manoeuvre
        out_sigma: float = 3.5,  # ...beyond this x the 3-D rms innovation (~6 per-axis sigma) (and out_min) = gross outlier
        out_min: tuple[float, float] = (3.0, 0.1),  # deg, m: no outlier below these, however small the noise
        vel_prior: tuple[float, float] = (30.0, 0.3),  # deg/s, m/s: typical speeds -> initial rate variance
        zupt_sigma: tuple[float, float] = (0.5, 0.01),  # deg/s, m/s: what 'still' means (micro-sway) for the lock
        vel_shrink: float = 3.0,  # velocity used for prediction is shrunk by this many of its own sigma
        # s: extrapolated velocity decays with this time constant (bounds overshoot when the head stops). 0.1 s
        # capped every prediction at ~0.1 s of motion -- far short of the ~0.35 s display latency -> the ring lagged.
        motion_tau: float = 0.25,
        reset_s: float = 0.5,
    ) -> None:
        self.q_rot, self.q_pos, self.nis_gate, self.nis_tau = q_rot, q_pos, nis_gate, nis_tau
        self.bias_tau, self.bias_gate = bias_tau, bias_gate
        self.out_sigma, self.out_min, self.vel_prior, self.vel_shrink, self.reset_s = (
            out_sigma,
            out_min,
            vel_prior,
            vel_shrink,
            reset_s,
        )
        self.zupt_sigma, self.motion_tau = zupt_sigma, motion_tau
        self.R: np.ndarray | None = None
        self.p = np.zeros(3)
        self.w = np.zeros(3)  # deg/s, body frame
        self.v = np.zeros(3)  # m/s
        self.moving = False
        self.ts = self.ok_ts = 0.0
        self._pend: list = [None, None]

    # ------------------------------------------------------------------ update

    @staticmethod
    def _Q(q: float, dt: float) -> np.ndarray:
        return q * np.array([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]])

    def _snap(self, c: int, m, r, axes=None) -> None:
        """(Re)initialise a channel (0 = rotation, 1 = position; position per axis) on a measurement: the rate is
        unknown again, the uncertainty is the measurement noise r."""
        P = np.stack([np.diag([ri, self.vel_prior[c] ** 2]) for ri in r])
        if c == 0:
            self.R, self.w, self.Pr = m, np.zeros(3), P
        elif axes is None:
            self.p, self.v, self.Pp = np.array(m, float), np.zeros(3), P
        else:
            self.p, self.v = np.where(axes, m, self.p), np.where(axes, 0.0, self.v)
            self.Pp = np.where(axes[:, None, None], P, self.Pp)
        if axes is None or axes.all():
            self.nis_avg[c], self.bias[c], self.zupt[c] = 1.0, 0.0, False

    def _init(self, R, t, ts, r_rot, r_pos) -> None:
        self.nis_avg, self.bias, self.zupt, self.n = np.ones(2), np.zeros((2, 3)), [False, False], 0
        self.moving, self._pend = False, [None, None]
        self._snap(0, R, r_rot)
        self._snap(1, t, r_pos)
        self.ts = self.ok_ts = ts

    def _gate(self, c: int, m, r, e, S, ts: float) -> np.ndarray:
        """Which axes of this channel's measurement are usable. Gross outliers (a 30 deg single-frame glitch, a
        metre of distance jump) are held back, and only accepted - by re-initialising that axis - if the next
        measurement agrees with them. Rotation is judged as a whole, position per axis (the distance axis is far
        noisier than the others and must not drag them along)."""
        if c == 0:
            bad = np.full(3, np.linalg.norm(e) > max(self.out_sigma * math.sqrt(S.sum()), self.out_min[0]))
        else:
            bad = np.abs(e) > np.maximum(self.out_sigma * math.sqrt(3) * np.sqrt(S), self.out_min[1])
        pend, self._pend[c] = self._pend[c], None
        if not bad.any():
            return np.ones(3, bool)
        agree = np.zeros(3, bool)
        if pend is not None and ts - pend[1] <= 0.2:
            if c == 0:
                agree[:] = math.degrees(rot_angle(pend[0], m)) <= max(
                    4 * math.sqrt(2 * float(r.sum())), self.out_min[0]
                )
            else:
                agree = pend[2] & (np.abs(pend[0] - m) <= np.maximum(4 * np.sqrt(2 * r), self.out_min[1]))
            agree &= bad
        if agree.any():
            self._snap(c, m, r, agree if c == 1 else None)
        if (held := bad & ~agree).any():
            self._pend[c] = (m, ts, held)
        return ~bad

    def update(self, R: np.ndarray, t: np.ndarray, ts: float, r_rot: np.ndarray, r_pos: np.ndarray) -> bool:
        """Fuse one measurement (r_* = per-axis noise variances, deg^2 / m^2). False if it was held back."""
        t = np.asarray(t, float)
        if self.R is None or not 0 < ts - self.ok_ts <= self.reset_s:
            self._init(R, t, ts, r_rot, r_pos)
            return True
        dt = ts - self.ts
        if dt <= 0:
            return False
        self.ts = ts
        F = np.array([[1.0, dt], [0.0, 1.0]])
        self.R = self.R @ rodrigues(np.radians(self.w) * dt)
        self.p = self.p + self.v * dt
        self.Pr = F @ self.Pr @ F.T + self._Q(self.q_rot[0], dt)
        self.Pp = F @ self.Pp @ F.T + self._Q(self.q_pos[0], dt)
        meas, rs = (R, t), (r_rot, r_pos)
        e = (np.degrees(rodrigues_inv(self.R.T @ R)), t - self.p)  # innovations: body-frame rotation (deg), metres
        S = (self.Pr[:, 0, 0] + r_rot, self.Pp[:, 0, 0] + r_pos)
        used = [self._gate(c, meas[c], rs[c], e[c], S[c], ts) for c in (0, 1)]
        if not (used[0].any() or used[1].any()):
            return False  # nothing usable in this measurement
        self.ok_ts = ts
        s = [0.0, 0.0]
        for c in (0, 1):
            if not used[c].any():
                continue
            u = used[c]
            nu = e[c][u] / np.sqrt(S[c][u])  # normalised innovation: N(0, 1) per axis if the model fits
            nis = float(np.mean(nu**2))
            self.nis_avg[c] += (1 - math.exp(-dt / self.nis_tau)) * (nis - self.nis_avg[c])
            self.bias[c][u] += (1 - math.exp(-dt / self.bias_tau)) * (nu - self.bias[c][u])
            # a gross innovation, or a consistent one-sided drift (what a turn the still model lags behind looks
            # like), means a manoeuvre: open the process noise up at once. A milder persistent excess opens it
            # proportionally.
            hard = nis > self.nis_gate**2 or float(np.abs(self.bias[c]).max()) > self.bias_gate
            s[c] = 1.0 if hard else min(max((self.nis_avg[c] - 1.5) / (self.nis_gate**2 - 1.5), 0.0), 1.0)
            q = (self.q_rot, self.q_pos)[c]
            P = (self.Pr, self.Pp)[c]
            P += s[c] * self._Q(q[1] - q[0], dt)
            K = P[:, :, 0] / (P[:, 0, 0] + rs[c])[:, None] * u[:, None]  # (3, 2); zero for held-back axes
            corr = K * e[c][:, None]
            P -= K[:, :, None] * P[:, None, 0, :]
            self._apply(c, corr)
            # Still-lock (zero-velocity update): while the innovations say "no motion" (Schmitt trigger on their
            # average), tell the filter the rate is ~0. It then averages like a random walk instead of paying for
            # a velocity it does not have: much less noise on a still head. Any real motion ends it at once.
            rate = (self.w, self.v)[c]
            if self.zupt[c] and (self.nis_avg[c] > 2.2 or hard):
                self.zupt[c] = False
            elif (
                not self.zupt[c]
                and self.nis_avg[c] < 1.5
                and self.n >= 10
                and float(np.linalg.norm(rate)) <= 3 * self.zupt_sigma[c]  # already near rest
            ):
                self.zupt[c] = True
            if self.zupt[c]:
                K = P[:, :, 1] / (P[:, 1, 1] + self.zupt_sigma[c] ** 2)[:, None]
                self._apply(c, -K * rate[:, None])
                P -= K[:, :, None] * P[:, None, 1, :]
        self.n += 1
        self.moving = (
            s[0] > 0.5
        )  # rotation only: the position channel is noisier and its alarms are not the head turning
        return True

    def _apply(self, c: int, corr: np.ndarray) -> None:
        """Add a (3, 2) state correction [value, rate] to channel c."""
        if c == 0:
            self.R = self.R @ rodrigues(np.radians(corr[:, 0]))
            self.w = self.w + corr[:, 1]
        else:
            self.p = self.p + corr[:, 0]
            self.v = self.v + corr[:, 1]

    # ------------------------------------------------------------------ readout

    @property
    def sig_w(self) -> np.ndarray:
        """1-sigma of the angular velocity estimate, deg/s per axis."""
        return np.sqrt(self.Pr[:, 1, 1]) if self.R is not None else np.zeros(3)

    def motion(self, sway_deg_s: float, turn_deg_s: float) -> float:
        """0 = still / natural sway / noise, 1 = a deliberate turn; linear between. The floor is the LARGER of the
        physiological sway speed and the rate estimate's own noise (vel_shrink sigma), not their sum: a noisy source
        (rear-camera Vision) must not need a faster turn before its motion counts."""
        if self.R is None:
            return 0.0
        floor = max(sway_deg_s, self.vel_shrink * float(np.linalg.norm(self.sig_w)))
        return min(1.0, max(0.0, (float(np.linalg.norm(self.w)) - floor) / max(turn_deg_s - floor, 1e-6)))

    def _shrunk(self, x: np.ndarray, sig: np.ndarray) -> np.ndarray:
        return np.sign(x) * np.maximum(np.abs(x) - self.vel_shrink * sig, 0.0)

    @property
    def speed_deg_s(self) -> float:
        """Angular speed with the estimate's own noise removed (0 for a still head)."""
        return float(np.linalg.norm(self._shrunk(self.w, self.sig_w))) if self.R is not None else 0.0

    @property
    def calm(self) -> bool:
        """No evidence of motion (no manoeuvre flagged, no significant rate): whatever the pose does is noise."""
        return self.R is not None and not self.moving and self.speed_deg_s == 0.0

    @property
    def still(self) -> bool:
        """Raw speed within the noise floor of the velocity estimate (3-dof chi: 2.5 x rms sigma covers ~99%)."""
        return (
            self.R is not None
            and self.zupt[0]
            and float(np.linalg.norm(self.w)) <= 2.5 * float(np.linalg.norm(self.sig_w))
        )

    def predict(self, lead: float) -> HeadPose:
        """The pose `lead` seconds after the last update, with its uncertainty. The rate is extrapolated with an
        exponential decay (motion_tau): linear extrapolation over a display latency overshoots every time the head
        stops, a decaying one is exact for constant velocity at short leads and bounded at long ones."""
        assert self.R is not None
        h = self.motion_tau * (1 - math.exp(-lead / self.motion_tau))  # effective extrapolation time, <= lead
        w, v = self._shrunk(self.w, self.sig_w), self._shrunk(self.v, np.sqrt(self.Pp[:, 1, 1]))
        # rate uncertainty only matters where a rate is actually being extrapolated
        sw = np.minimum(self.sig_w, np.abs(w))
        sv = np.minimum(np.sqrt(self.Pp[:, 1, 1]), np.abs(v))
        sig_rot = np.sqrt(self.Pr[:, 0, 0] + (sw * h) ** 2)  # estimation noise, not the future's own motion
        sig_pos = np.sqrt(self.Pp[:, 0, 0] + (sv * h) ** 2)
        return HeadPose(self.R @ rodrigues(np.radians(w) * h), self.p + v * h, np.radians(sig_rot), sig_pos)


# --------------------------------------------------------------------------------------------- screen smoother


class ScreenSmoother:
    """Critically damped second-order (spring-damper) follower for one on-screen point, in px.

    Closed-form step, so it is exact and stable for any dt (dropped ticks, late ticks). `omega` (rad/s) sets the
    speed: the caller derives it from the display's update interval so the mark settles in a few updates.
    Dead-band with hysteresis (radius `deadband` px, from the target's predicted noise): the output is frozen while
    the target stays inside it, so a still head gives a perfectly still mark; once the target leaves it the mark
    follows the target itself (no lag) until it is back within a quarter of the band, then freezes again.
    `vmax` (px/s) caps speed so any jump glides.
    """

    REARM = 0.25  # freeze again when the error is below this fraction of the dead-band

    def __init__(self) -> None:
        self.x: np.ndarray | None = None
        self.v = np.zeros(2)
        self.active = False

    def reset(self, p) -> None:
        self.x, self.v, self.active = np.array(p, float), np.zeros(2), False

    def step(self, g, dt: float, omega: float, deadband: float, vmax: float) -> np.ndarray:
        g = np.asarray(g, float)
        if self.x is None:
            self.reset(g)
            return self.x
        n = float(np.linalg.norm(g - self.x))
        self.active = n > deadband or (self.active and n > self.REARM * deadband) or deadband <= 0
        g_eff = g if self.active else self.x
        c1 = self.x - g_eff
        c2 = self.v + omega * c1
        k = math.exp(-omega * dt)
        x1 = g_eff + (c1 + c2 * dt) * k
        v1 = (c2 - omega * (c1 + c2 * dt)) * k
        if (s := float(np.linalg.norm(x1 - self.x))) > vmax * dt:
            x1 = self.x + (x1 - self.x) * (vmax * dt / s)
        if (s := float(np.linalg.norm(v1))) > vmax:
            v1 = v1 * (vmax / s)
        if not self.active and float(np.linalg.norm(v1)) < 0.5:  # settled: stop creeping (sub-pixel)
            v1 = np.zeros(2)
        self.x, self.v = x1, v1
        return self.x


class StaticPoint:
    """World position of an object that does not move (the phone on its stand, a bottle on a table).

    ARKit re-estimates anchors and the camera pose every frame as SLAM refines, so the raw point wobbles by mm-cm.
    The object did not move, so neither should its estimate: output the median of the last WINDOW_S of samples, and
    HOLD it until that median has moved by more than the measured jitter (K x robust sigma, at least MIN_STEP_M).
    A jump larger than REPLACE_M is a re-placement (new tap/locate), adopted at once."""

    WINDOW_S = 2.0
    K = 4.0
    MIN_STEP_M = 0.01  # 1 cm: below what the G2 ring (40 px ~ 1.5 deg ~ 2.6 cm at 1 m) can show anyway
    REPLACE_M = 0.10  # 10 cm in one go is not SLAM drift: the app placed the anchor somewhere new

    def __init__(self) -> None:
        self.samples: deque[tuple[float, np.ndarray]] = deque()
        self.held: np.ndarray | None = None
        self.window_s = self.WINDOW_S  # a coupled (moving) object uses TRACKED_WINDOW_S instead
        self.hold = True  # static object: hold until the median really moves. Coupled (moving) object: no hold

    TRACKED_WINDOW_S = 0.4  # app couples the anchor to a moving object: follow it, only de-jitter

    def update(self, p, ts: float) -> np.ndarray:
        p = np.asarray(p, float)
        if self.held is not None and float(np.linalg.norm(p - self.held)) > self.REPLACE_M:
            self.samples.clear()
            self.held = p
        self.samples.append((ts, p))
        while self.samples and ts - self.samples[0][0] > self.window_s:
            self.samples.popleft()
        pts = np.array([s[1] for s in self.samples])
        med = np.median(pts, axis=0)
        sigma = 1.4826 * float(np.median(np.linalg.norm(pts - med, axis=1)))  # robust jitter (MAD -> sigma)
        if (
            not self.hold
            or self.held is None
            or float(np.linalg.norm(med - self.held)) > max(self.MIN_STEP_M, self.K * sigma)
        ):
            self.held = med
        return self.held
