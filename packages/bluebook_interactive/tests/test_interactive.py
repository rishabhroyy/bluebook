"""Part 3: geometry, filters, optics, renderer, pose WebSocket, and closed-loop simulations of the behaviour
the wearer sees (noisy 30 Hz phone poses -> Tracker -> Renderer -> fake glasses that ack each draw). No network."""

import itertools
import json
import math
import time

import numpy as np
import pytest
from bluebook_core import hub
from bluebook_core.protocol import Arrow, ImageDrawn, Ring, StepChanged, StepTarget
from bluebook_interactive import render
from bluebook_interactive.filters import HeadKalman, NoiseEstimator, ScreenSmoother
from bluebook_interactive.geom import (
    BAND,
    CENTER,
    RING_R,
    K_of,
    az_el,
    backproject,
    dir_of,
    place,
    project,
    rodrigues,
    rot_angle,
    rot_between,
    to_head,
)
from bluebook_interactive.optics import BoresightLearner, Display, DisplayModel
from bluebook_interactive.tracker import MX, VARIANTS, Tracker
from bluebook_interactive.wire import PoseHello, PoseMsg
from pydantic import ValidationError

K = K_of(1100, 1100, 288, 160)
HEAD_ON = np.diag([1.0, -1.0, -1.0])  # head +x = cam +x, up = cam -y, out of the face = cam -z (looking at the camera)


def rot(axis, deg):
    return rodrigues(np.asarray(axis, float) / np.linalg.norm(axis) * math.radians(deg))


# ------------------------------------------------------------------------------------------------ geometry


def test_backproject_and_to_head():
    X = backproject(K, 100.0, 250.0, 0.5)
    assert X[2] == pytest.approx(0.5)
    np.testing.assert_allclose((K @ X)[:2] / (K @ X)[2], [100, 250], atol=1e-9)
    R, t, Xh = rot([0, 1, 0], 20), np.array([0.1, 0.2, 0.3]), np.array([0.01, -0.02, 0.05])
    np.testing.assert_allclose(to_head(R, t, R @ Xh + t), Xh, atol=1e-12)


def test_rotations_and_directions():
    assert rot_angle(np.eye(3), rot([0, 0, 1], 30)) == pytest.approx(math.radians(30))
    a, b = np.array([0.2, -0.3, 1.0]), np.array([-0.5, 0.1, 0.8])
    R = rot_between(a, b)
    np.testing.assert_allclose(R @ a / np.linalg.norm(a), b / np.linalg.norm(b), atol=1e-9)
    assert np.isfinite(rot_between([0, 0, 1], [0, 0, -1])).all()  # antiparallel
    np.testing.assert_allclose(rot_between([0, 0, 1], [0, 0, 1]), np.eye(3))
    assert az_el(dir_of(-7, 4)) == pytest.approx((-7, 4))


def test_place_ring_in_band_and_chevron_outside():
    (ring,) = place(288, 160, 1.0)
    assert isinstance(ring, Ring) and ring.r == RING_R and (ring.cx, ring.cy) == (288, 160)
    for u, v, w in [(-300, 160, 1), (900, 160, 1), (288, -100, 1), (288, 500, 1), (-300, 160, -1), (900, 160, -1)]:
        (arrow,) = place(u, v, w)
        assert isinstance(arrow, Arrow)
        x0, y0, x1, y1 = BAND
        assert x0 <= arrow.x1 <= x1 and y0 <= arrow.y1 <= y1
        dx, dy = (u - CENTER[0], v - CENTER[1]) if w > 0 else (CENTER[0] - u, CENTER[1] - v)  # behind: mirrored
        assert (arrow.x1 - arrow.x0) * dx + (arrow.y1 - arrow.y0) * dy > 0  # points toward the target
    (a,) = place(*CENTER, 1.0, chevron=True)  # forced glyph, and a point already on the edge stays put
    (b,) = place(a.x1, a.y1, 1.0, chevron=True)
    assert (a.x1, a.y1) == pytest.approx((b.x1, b.y1))


# ----------------------------------------------------------------------------------------------- optics


def test_default_display_has_no_hardcoded_tilt():
    m = DisplayModel()
    assert m.f == pytest.approx(1523, abs=5)
    # a legacy file's stale manual "rot" (the 8.7 deg tilt that pushed marks ~230 px down) is ignored on load
    old = DisplayModel.model_validate({"fov_deg": 21.4, "rot": [-0.1523, -0.0405, 0.0], "aligned_t": 1.0})
    np.testing.assert_allclose(old.P(), m.P())
    P = m.P()
    ahead, left, up = (project(P, p)[0] for p in ([0, 0, 1.0], [0.2, 0, 1.0], [0, 0.2, 1.0]))
    assert np.allclose(ahead[:2], [288, 144]) and ahead[2] > 0  # panel centre, straight ahead
    assert left[0] < 288 and up[1] < 144  # +x = wearer's left -> display left; up -> up
    assert project(DisplayModel(dx=0.01).P(), [0, 0, 1.0])[0][0] > 288  # eye moved left: point shifts right


def test_boresight_maps_learned_direction_to_display_centre():
    m = DisplayModel()
    az, el = -7.0, 5.0
    for conf in (0.2, 1.0):  # same aim as the un-learned prior (head-forward -> principal point): no slide
        uv = project(m.P((az, el, conf)), dir_of(az, el))[0][:2]
        np.testing.assert_allclose(uv, [m.cx, m.cy], atol=1e-6)
    assert np.allclose(project(m.P((az, el, 0.0)), dir_of(az, el))[0][:2], project(m.P(), dir_of(az, el))[0][:2])
    np.testing.assert_allclose(project(m.P(), dir_of(0, 0))[0][:2], [m.cx, m.cy], atol=1e-6)


def test_display_persist_validate_and_boresight_roundtrip(tmp_path):
    d = Display("sn/1", tmp_path)
    m = d.set(fov_deg=30, dz=0.02)
    assert (m.fov_deg, m.dz, m.cx) == (30, 0.02, 288)
    d.learner("arkit").az, d.learner("arkit").el, d.learner("arkit").w = 3.0, -4.0, 12.0
    d.save()
    again = Display("sn/1", tmp_path)
    assert again.model.fov_deg == 30 and again.learner("arkit").bore[:2] == (3.0, -4.0)
    assert again.learner("vision").bore == (0.0, 0.0, 0.0)  # per source
    with pytest.raises(ValidationError):
        d.set(fov_deg=500)
    (tmp_path / "display_bad.json").write_text("{nope")
    assert Display("bad", tmp_path).model == DisplayModel()


def test_boresight_learner_gates_and_bounds():
    L = BoresightLearner()
    d = dir_of(8.0, -3.0)
    for i in range(30):  # moving: nothing learned
        L.observe(d, False, i * 0.1, 0.1)
    assert L.bore == (0.0, 0.0, 0.0)
    for i in range(6):  # still but under the dwell time: nothing yet
        L.observe(d, True, 10 + i * 0.1, 0.1)
    assert L.w == 0
    for i in range(6, 1500):  # slow and conservative: minutes of dwell, not seconds
        L.observe(d, True, 10 + i * 0.1, 0.1)
    assert (L.az, L.el) == pytest.approx((8.0, -3.0), abs=0.8) and L.confidence > 0.9
    L2 = BoresightLearner()
    for i in range(200):  # target outside the +-12 deg gate is not the wearer looking at it
        L2.observe(dir_of(25, 0), True, i * 0.1, 0.1)
    assert L2.w == 0
    L.reset()
    assert L.bore == (0.0, 0.0, 0.0)


# ------------------------------------------------------------------------------------------------ filters


@pytest.mark.parametrize("sigma", [0.3, 1.0, 1.5])
def test_noise_estimator_recovers_noise_while_moving_and_with_glitches(sigma):
    rng, ne = np.random.default_rng(0), NoiseEstimator()
    for i in range(300):  # 10 s: 20 deg/s sweep, 2 cm position noise, a 30 deg glitch every 40 frames
        ts = i / 30
        R = HEAD_ON @ rodrigues([0, math.radians(20 * ts), 0]) @ rodrigues(np.radians(rng.normal(0, sigma, 3)))
        if i % 40 == 7:
            R = R @ rodrigues(np.radians([30.0, 0, 0]))
        r_rot, r_pos = ne.update(R, np.array([0, 0, 1.0]) + rng.normal(0, 0.02, 3), ts)
    assert np.sqrt(r_rot) == pytest.approx([sigma] * 3, rel=0.35)
    assert np.sqrt(r_pos) == pytest.approx([0.02] * 3, rel=0.35)
    assert NoiseEstimator().rot_deg == 1.0  # prior before any data


def test_head_kalman_still_is_still_and_turn_is_followed():
    rng, ne, k = np.random.default_rng(0), NoiseEstimator(), HeadKalman()
    still, errs, calm = [], [], []
    for i in range(330):  # still 5 s, 20 deg/s for 2 s, still
        ts = i / 30
        yaw = 0 if ts < 5 else (20 * (ts - 5) if ts < 7 else 40)
        R_true = HEAD_ON @ rodrigues([0, math.radians(yaw), 0])
        R = R_true @ rodrigues(np.radians(rng.normal(0, 1.0, 3)))
        t = np.array([0, 0, 1.0]) + rng.normal(0, 0.02, 3)
        k.update(R, t, ts, *ne.update(R, t, ts))
        if 90 <= i < 150:
            still.append(math.degrees(rot_angle(k.R, HEAD_ON)))
            calm.append(k.still and k.speed_deg_s < 1.0)
        if 180 <= i < 210:
            errs.append(math.degrees(rot_angle(k.R, R_true)))
    assert np.mean(calm) > 0.9  # a still head reads as still: no false motion
    assert np.sqrt(np.mean(np.square(still))) < 0.6  # 3-D error; raw is ~1.7 deg
    assert np.mean(errs) < 2.5
    assert math.degrees(rot_angle(k.R, HEAD_ON @ rodrigues([0, math.radians(40), 0]))) < 1.0


def test_head_kalman_ignores_single_frame_glitch_but_accepts_a_real_jump():
    k, ne = HeadKalman(), NoiseEstimator()
    r = (np.full(3, 0.1), np.full(3, 1e-4))
    t = np.array([0, 0, 1.0])
    for i in range(60):
        k.update(HEAD_ON, t, i / 30, *r)
    k.update(HEAD_ON @ rot([1, 0, 0], 30), t, 2.0, *r)  # glitch
    k.update(HEAD_ON, t, 2.0 + 1 / 30, *r)
    assert math.degrees(rot_angle(k.R, HEAD_ON)) < 0.5
    jump = HEAD_ON @ rot([0, 1, 0], 40)
    for i in range(2):  # two consistent frames = a real jump
        k.update(jump, t, 2.1 + i / 30, *r)
    assert math.degrees(rot_angle(k.R, jump)) < 0.5
    assert ne is not None


def test_screen_smoother_dt_independent_no_overshoot_deadband_and_speed_cap():
    def settle(dt, n_total=2.0, omega=20.0):
        s = ScreenSmoother()
        s.reset([0, 0])
        xs = []
        for _ in range(int(n_total / dt)):
            xs.append(s.step([100, 50], dt, omega, 0.0, 1e9).copy())
        return np.array(xs)

    a, b = settle(0.01), settle(0.1)
    np.testing.assert_allclose(a[9::10][:10], b[:10], atol=1e-6)  # same trajectory whatever the step
    assert a[:, 0].max() <= 100 + 1e-9  # critically damped: never overshoots
    assert np.abs(settle(5.0, 10.0)[-1] - [100, 50]).max() < 1e-6  # absurd dt stays stable
    s = ScreenSmoother()
    s.reset([10, 10])
    for _ in range(50):  # target wanders inside the dead-band: exactly still
        s.step([10 + 3 * math.sin(_), 10], 0.1, 20.0, 5.0, 1e9)
        assert tuple(s.x) == (10, 10)
    s.step([60, 10], 0.1, 20.0, 5.0, 1e9)  # leaves the band: follows the target itself (no lag of a band width)
    for _ in range(40):
        s.step([60, 10], 0.1, 20.0, 5.0, 1e9)
    assert abs(s.x[0] - 60) < 0.25 * 5.0 + 1e-6
    s.reset([0, 0])
    prev = s.x.copy()
    for _ in range(10):  # a huge jump glides at the speed cap
        cur = s.step([5000, 0], 0.1, 20.0, 0.0, 1000.0).copy()
        assert np.linalg.norm(cur - prev) <= 1000 * 0.1 + 1e-9
        prev = cur


# ---------------------------------------------------------------------------------------------- renderer


def make_tracker(tmp_path, clock=lambda: 0.0, source="vision"):
    tk = Tracker(tmp_path, clock)
    tk.hello = PoseHello(type="pose_hello", source=source)
    tk.targets.steps["s"] = StepChanged(
        session="s", skill_id="x", step_id="a", targets=[StepTarget(object_id="camera", name="phone")]
    )
    return tk


def test_renderer_publish_rules(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(render.hub, "publish", sent.append)
    r = make_tracker(tmp_path).renderer("s")
    ring = lambda x: [Ring(cx=x, cy=160, r=RING_R)]
    r._emit([], 0.0)
    assert not sent  # never an empty frame over Part 2's keyframes
    r._emit(ring(100), 1.0)
    assert len(sent) == 1 and r.inflight == 1.0
    r._emit(ring(100), 1.5)  # pixel-identical: skipped
    assert len(sent) == 1
    r._emit(ring(100), 1.0 + render.KEEPALIVE_S)  # slow keepalive heals a page redrawn by someone else, no ack awaited
    assert len(sent) == 2
    r.inflight = None
    r._emit([], 5.0)  # our own mark taken away: allowed once
    r._emit([], 5.1)
    assert len(sent) == 3 and sent[-1].primitives == []
    r._emit(ring(100), 6.0)
    r.reset()  # page redrawn -> compositor is blank -> draw again even if identical
    r._emit(ring(100), 6.1)
    assert len(sent) == 5


def test_renderer_pacing_ack_timeout_and_late_ack(tmp_path):
    r = make_tracker(tmp_path).renderer("s")
    assert r.timeout() == pytest.approx(2 * render.PRIOR_RTT_S)
    r.inflight = 10.0
    assert not r.due(10.1) and r.due(10.0 + r.timeout() + 0.01)  # nothing faster than the timeout without an ack
    ack = lambda t: ImageDrawn(session="s", id=1, name="i", t=t, ms=90, at=t + 0.16)
    r.on_ack(ack(9.0), 10.2)  # ack of an OLDER publish must not release the newer one
    assert r.inflight == 10.0 and not r.ack_pending
    r.on_ack(ack(10.0), 10.16)
    assert r.inflight is None and r.ack_pending and r.due(10.16)
    for i in range(20):
        r.on_ack(ack(20.0 + i), 21.0 + i)
    assert r.period() == pytest.approx(0.16) and r.timeout() == pytest.approx(0.32)
    r.rtt.extend([0.6] * 50)
    assert r.timeout() == render.TIMEOUT_S[1] and r.period() == render.PERIOD_S[1]


# ---------------------------------------------------------------------------------------- simulation


def pose_looking_at(d_head, dist=1.0):
    """True (R, t) of a head that sees the camera (the phone) in head-frame direction d_head."""
    return HEAD_ON @ rot_between(d_head, [0, 0, 1]), np.array([0, 0, dist])


def yaw_pitch(yaw_deg, pitch_deg=0.0, dist=1.0):
    return HEAD_ON @ rodrigues([math.radians(pitch_deg), math.radians(yaw_deg), 0]), np.array([0, 0, dist])


def min_jerk(t, t0, dur):
    s = min(max((t - t0) / dur, 0.0), 1.0)
    return 10 * s**3 - 15 * s**4 + 6 * s**5


class Sim:
    """Noisy 30 Hz poses (50 ms old on arrival) -> Tracker -> Renderer; fake glasses ack every publish `rtt` later."""

    def __init__(self, tmp, truth, noise_deg=1.0, pos_m=0.02, rtt=0.05, source="vision", seed=0, dropouts=False,
                 glitches=False, z_jumps=False, bore=(0.0, 0.0, 60.0)):  # fmt: skip
        self.now = 0.0
        self.tk = make_tracker(tmp, lambda: self.now, source)
        self.truth, self.noise_deg, self.pos_m, self.rtt, self.seed = truth, noise_deg, pos_m, rtt, seed
        self.dropouts, self.glitches, self.z_jumps = dropouts, glitches, z_jumps
        self.r = self.tk.renderer("s")
        self.r.rtt.extend([rtt] * 12)  # a display whose speed is already known
        if bore:  # a converged boresight, so tests that are not about learning see a fixed display
            self.tk.display("s").learners[source] = BoresightLearner(bore)
        self.log: list[tuple[float, tuple | None, bool]] = []  # (time, ring (x, y) / chevron, visible)
        self.k = 0
        self._skip = 0
        self._ack_at = self._pub_t = None

    def _frame(self, k):
        ts = k / 30
        rng = np.random.default_rng([self.seed, k])  # per-frame noise, identical across runs that differ otherwise
        R, t = self.truth(ts)
        R = R @ rodrigues(np.radians(rng.normal(0, self.noise_deg, 3)))
        t = t + rng.normal(0, self.pos_m, 3)
        ev = np.random.default_rng([self.seed + 1, k])
        if self.glitches and ev.random() < 1 / 100:
            R = R @ rodrigues(np.radians([30.0, 0, 0]))  # single-frame orientation glitch
        if self.z_jumps and ev.random() < 0.15:
            t = t + [0, 0, ev.uniform(-0.3, 0.5)]  # IPD-derived distance jumps
        if self.dropouts and self._skip == 0 and ev.random() < 1 / 40:
            self._skip = int(ev.integers(1, 6))
        if self._skip > 0:
            self._skip -= 1
            return
        m = PoseMsg.model_validate(
            {"type": "pose", "t": ts, "w": 1280, "h": 720, "K": [1000, 1000, 640, 360],
             "face": {"R": R.ravel().tolist(), "t": t.tolist(), "conf": 0.9}}
        )  # fmt: skip
        self.tk.on_pose(m, ts + 0.05)

    def run(self, seconds, dt=0.005):
        end = self.now + seconds
        while self.now < end:
            self.now += dt
            while self.k / 30 + 0.05 <= self.now:
                self._frame(self.k)
                self.k += 1
            if self._ack_at is not None and self.now >= self._ack_at:
                self.r.on_ack(ImageDrawn(session="s", id=1, name="i", t=self._pub_t, ms=90, at=self.now), self.now)
                self._ack_at = None
            if self.r.due(self.now):
                before = self.r.inflight
                self.r.tick(self.now)
                if self.r.inflight is not None and self.r.inflight != before:
                    self._pub_t, self._ack_at = self.now, self.now + self.rtt
                self.log.append((self.now, self.ring(), self.r.visible))

    def ring(self):
        p = self.r.shown
        if not p:
            return None
        return (p[0].cx, p[0].cy) if p[0].kind == "ring" else ("chevron", p[0].x1, p[0].y1)

    def screen_of_truth(self, ts):
        R, t = self.truth(ts)
        return project(self.tk.display("s").P(self.tk.source), self.tk.mirror @ R.T @ -t)[0][:2]

    def after(self, t0):
        return [(t, r, v) for t, r, v in self.log if t >= t0]


@pytest.mark.parametrize("noise", [0.3, 1.0, 1.5])
@pytest.mark.parametrize("seed", [1, 2])
def test_sim_still_head_gives_a_perfectly_still_ring(tmp_path, noise, seed):
    """A still head gives a ring at rest, whatever the input noise (dropouts and glitches thrown in): it may glide
    once to re-centre when the learned display offset moves the target out of the hold zone, never jitter."""
    R0_, t0_ = yaw_pitch(3.0)
    bore = (*az_el(R0_.T @ -t0_), 60.0)  # the wearer's learned boresight already points at the phone
    s = Sim(tmp_path, lambda t: yaw_pitch(3.0), noise_deg=noise, seed=seed, dropouts=True, glitches=True, bore=bore)
    s.run(60)
    late = s.after(25)
    assert len(late) > 100 and all(v and r and r[0] != "chevron" for _, r, v in late)
    moved = sum(a[1] != b[1] for a, b in itertools.pairwise(late))
    assert moved <= 0.05 * len(late), moved  # at rest; at most one short glide
    first = s.after(0.0)
    ring = late[-1][1]
    assert np.hypot(*(np.array(ring) - s.screen_of_truth(late[-1][0]))) <= render.RING_HOLD_PX + 1  # target in ring
    assert any(r for _, r, _ in first)


def test_sim_slow_turn_then_stop_is_smooth_and_settles(tmp_path):
    def yawof(t):
        return -4 + 8 * min_jerk(t, 8.0, 0.8)  # 8 deg in 0.8 s, peak ~19 deg/s

    for noise in (0.3, 1.0):
        s = Sim(tmp_path / str(noise), lambda t: yaw_pitch(yawof(t)), noise_deg=noise, seed=5)
        s.run(14)
        seg = s.after(7.9)
        assert all(v and r and r[0] != "chevron" for _, r, v in seg)  # never hides, never leaves the band
        T = s.r.interval()
        vmax = render.VMAX_PX_PER_UPDATE / T
        for (t0, a, _), (t1, b, _) in itertools.pairwise(seg):
            assert np.hypot(b[0] - a[0], b[1] - a[1]) <= vmax * (t1 - t0) * 1.01 + 1e-6  # glides within the speed cap
        end = s.screen_of_truth(20.0)
        xs = np.array([r[0] for _, r, _ in seg])
        assert (xs.max() - end[0]) < (RING_R if noise < 0.5 else 2 * RING_R)  # overshoot bounded by the noise
        assert (
            np.hypot(*(np.array(seg[-1][1]) - end)) < s.r.deadband + 5
        )  # settles in the right place (to the dead-band)
        mid = [(t, r) for t, r, _ in seg if 8.4 < t < 8.6]
        assert mid and all(50 < r[0] < 520 for _, r in mid)  # and followed along the way


def test_sim_glitches_dropouts_and_distance_jumps_do_not_flicker_or_jump(tmp_path):
    def truth(t):
        return yaw_pitch(-3 + 6 * min_jerk(t, 6.0, 1.0), pitch_deg=1.0)

    clean = Sim(tmp_path / "a", truth, noise_deg=1.0, seed=7)
    dirty = Sim(tmp_path / "b", truth, noise_deg=1.0, seed=7, dropouts=True, glitches=True)
    z = Sim(tmp_path / "c", truth, noise_deg=1.0, seed=7, z_jumps=True)
    for s in (clean, dirty, z):
        s.run(12)
    for s in (dirty, z):
        seg = s.after(3.0)
        assert all(v and r and r[0] != "chevron" for _, r, v in seg)  # visibility never toggles, no chevron flicker
        ref = {round(t, 3): r for t, r, _ in clean.after(3.0)}
        diffs = []
        for t, r, _ in seg:
            if (c := ref.get(round(t, 3))) is not None:
                diffs.append(np.hypot(r[0] - c[0], r[1] - c[1]))
        assert diffs and max(diffs) < 2 * RING_R  # at worst a different timing of the same smooth motion
    nz = [t for t, r, v in dirty.log if r is None and t > 3.0]
    assert not nz


def test_sim_boresight_learner_converges_and_ring_lands_at_band_centre(tmp_path):
    """A wearer who, when looking at the phone, has it 7 deg down and 2 deg to the right of head-forward."""
    az0, el0 = -2.0, -7.0
    # (start time, az, el): dwell on the phone, glance far away (outside the +-15 deg gate), dwell, a short look at a
    # place 11 deg off (inside the gate), dwell again; every change is a 0.5 s min-jerk glide
    legs = [(5.0, -40.0, el0), (6.5, az0, el0), (12.0, 9.0, -2.0), (13.5, az0, el0)]

    def truth(t):
        a, e = az0, el0
        for t0, a1, e1 in legs:
            s = min_jerk(t, t0, 0.5)
            a, e = a + (a1 - a) * s, e + (e1 - e) * s
        return pose_looking_at(dir_of(a, e))

    s = Sim(tmp_path, truth, noise_deg=1.0, seed=3, bore=None)
    L = s.tk.display("s").learner("vision")
    assert L.bore == (0.0, 0.0, 0.0)
    s.run(60)
    assert 0 < L.confidence < 0.95 and np.hypot(L.az - az0, L.el - el0) < 5.0  # on its way: slow on purpose
    s.run(180)
    assert abs(L.az - az0) < 1.2 and abs(L.el - el0) < 1.2
    ring = np.array(s.ring())
    assert np.hypot(*(ring - CENTER)) < 25  # looking at the phone -> the ring is at the middle of the band
    assert L.confidence > 0.8
    st = s.tk.status()
    assert set(st["boresight"].get("s", {"az_deg": 0, "el_deg": 0, "confidence": 0})) >= {
        "az_deg",
        "el_deg",
        "confidence",
    }


def test_sim_brief_looking_away_does_not_wreck_the_learned_boresight(tmp_path):
    az0, el0 = 3.0, -4.0

    def truth(t):  # holds the phone at (az0, el0), except 2 s at 10 deg off (still inside the +-15 deg gate)
        away = 40.0 <= t < 42.0
        return pose_looking_at(dir_of(az0 + (10.0 if away else 0.0), el0))

    s = Sim(tmp_path, truth, noise_deg=1.0, seed=4, bore=(az0, el0, 30.0))
    s.run(39.9)
    before = np.array([s.tk.display("s").learner("vision").az, s.tk.display("s").learner("vision").el])
    s.run(6)
    after = np.array([s.tk.display("s").learner("vision").az, s.tk.display("s").learner("vision").el])
    assert np.linalg.norm(after - before) < 0.8


@pytest.mark.parametrize("source,mirrored", [("arkit", True), ("vision", False)])
def test_head_x_mirror_only_for_arkit(tmp_path, source, mirrored):
    tk = make_tracker(tmp_path, source=source)
    np.testing.assert_array_equal(tk.mirror, MX if mirrored else np.eye(3))
    xs = {}
    for src in ("arkit", "vision"):
        s = Sim(tmp_path / src, lambda t: yaw_pitch(5.0), noise_deg=0.0, pos_m=0.0, source=src)
        s.run(4)
        xs[src] = s.ring()[0]
    assert xs["arkit"] + xs["vision"] == pytest.approx(2 * 288, abs=1.5)  # mirror images of each other
    assert xs["vision"] < 288 + 0 or xs["vision"] > 288  # and neither is the centre


def test_conventions_self_correct_from_arkit_truth(tmp_path):
    tk = make_tracker(tmp_path, source="arkit")
    R_true = np.diag([1.0, -1.0, -1.0]) @ rot([0, 1, 0], 25)  # head 0.2 m to camera-right, turned 25 deg
    t_true = np.array([0.2, 0.05, 0.9])
    truth = to_head(R_true, t_true, np.zeros(3))
    for _ in range(40):  # the app sends R left-right mirrored
        R, t = tk.fix_conventions(MX @ R_true @ MX, t_true, truth.tolist())
    assert tk.variant == "R mirrored" and set(VARIANTS) == set(tk.variant_err)
    np.testing.assert_allclose(R, R_true, atol=1e-9)
    np.testing.assert_allclose(t, t_true, atol=1e-9)


# ---------------------------------------------------------------------------------- pose WebSocket


def test_pose_ws_head_on_face_gets_a_ring_near_centre(tmp_path):
    from bluebook_core import server
    from bluebook_core.protocol import Hello, OverlayFrame
    from bluebook_interactive import setup
    from fastapi.testclient import TestClient

    n_routes = len(server.app.routes)
    bg = setup(server.app, calib_dir=tmp_path)
    server._background.append(bg)
    try:
        with TestClient(server.app) as c, c.websocket_connect("/ws/glasses") as g:
            g.send_text(Hello(device={"sn": "t1"}).model_dump_json())
            g.receive_text()  # echo page
            for _ in range(100):  # the steps loop must be subscribed before a publish
                if hub._subs[StepChanged]:
                    break
                time.sleep(0.02)
            assert c.post("/interactive/step/t1?name=camera").json() == {"targets": ["camera"]}
            g.receive_text()  # teach page
            with c.websocket_connect("/interactive/ws/pose") as p:
                p.send_text('{"type":"pose_hello","camera_id":"scene","source":"vision","device":"test"}')
                p.send_text("garbage")  # dropped, not fatal
                p.send_text('{"type":"pose","t":1,"w":10,"h":10,"K":[1,1,5,5],"face":{"R":[1],"t":[0,0,1]}}')
                # head +x (wearer's left) = cam +x, up = cam -y, out of the face = cam -z, 1 m away
                msg = {"type": "pose", "w": 1280, "h": 720, "K": [1000, 1000, 640, 360],
                       "face": {"R": [1, 0, 0, 0, -1, 0, 0, 0, -1], "t": [0, 0, 1], "conf": 0.9}}  # fmt: skip
                for _ in range(300):
                    p.send_text(json.dumps({**msg, "t": time.time()}))
                    time.sleep(0.02)
                    if hub.last(OverlayFrame, "t1") and hub.last(OverlayFrame, "t1").primitives:
                        break
                st = c.get("/interactive/status").json()
                png = c.get("/interactive/glasses.png?session=t1")
            fr = hub.last(OverlayFrame, "t1")
            assert fr and [q.kind for q in fr.primitives] == ["ring"] and fr.primitives[0].r == RING_R
            # no learned offset yet: straight ahead is the panel centre (the learner only starts blending toward
            # the band centre after a dwell)
            assert abs(fr.primitives[0].cx - 288) < 1 and 143 <= fr.primitives[0].cy <= 161
            assert st["pose"]["source"] == "vision" and st["pose"]["head_ok"] and st["pose"]["app_connected"]
            assert {"noise_deg", "noise_cm", "speed_deg_s", "moving"} <= set(st["pose"]["filter"])
            assert {"horizontal", "vertical", "ahead", "az_el_deg"} <= set(st["pose"]["phone_from_head"])
            assert {"conventions", "arkit_truth", "K", "size", "hz", "age_s", "face_conf", "head_z"} <= set(st["pose"])
            assert {"omega_deg_s", "display", "sessions", "anchors", "tracks", "imu", "render", "boresight"} <= set(st)
            assert st["sessions"]["t1"]["targets"] == ["camera"] and st["boresight"]["t1"]["confidence"] >= 0
            rs = st["render"]["sessions"]["t1"]
            assert {"tick_hz", "latency_ms", "visible", "deadband_px"} <= set(rs)
            assert png.status_code == 200 and png.headers["content-type"] == "image/png"
            assert "rot" not in c.get("/interactive/display/t1").json()
            assert c.post("/interactive/display/t1?fov_deg=999").status_code == 422
            assert c.post("/interactive/display/t1?fov_deg=22").json()["fov_deg"] == 22
            assert c.post("/interactive/align/t1").json()["session"] == "t1"
            assert c.post("/interactive/step/t1").json() == {"targets": []}  # clear
    finally:
        server._background.remove(bg)
        del server.app.router.routes[n_routes:]


# ------------------------------------------------------------------------------------ arkit-world mode

WORLD_R = np.diag([-1.0, 1.0, -1.0])  # head faces world -z: head +y = world up, head +x = world -x, +z out of the face


def world_pose(R, t, anchors, t_s=None, conf=0.9):
    return {"type": "pose", "t": t_s if t_s is not None else time.time(), "w": 1280, "h": 720,
            "K": [1000, 1000, 640, 360], "face": {"R": np.asarray(R).ravel().tolist(), "t": list(t), "conf": conf},
            "anchors": anchors}  # fmt: skip


def test_pose_ws_world_mode_phone_and_tapped_anchor_targets(tmp_path):
    from bluebook_core import server
    from bluebook_core.protocol import Hello, OverlayFrame
    from bluebook_interactive import setup
    from fastapi.testclient import TestClient

    n_routes = len(server.app.routes)
    bg = setup(server.app, calib_dir=tmp_path)
    server._background.append(bg)

    def ring_after(c, p, msg, sid):
        hub._last.pop((OverlayFrame, sid), None)
        for _ in range(300):
            p.send_text(json.dumps({**msg, "t": time.time()}))
            time.sleep(0.02)
            if (fr := hub.last(OverlayFrame, sid)) and fr.primitives:
                return fr
        return None

    try:
        with TestClient(server.app) as c, c.websocket_connect("/ws/glasses") as g:
            g.send_text(Hello(device={"sn": "w1"}).model_dump_json())
            g.receive_text()
            for _ in range(100):
                if hub._subs[StepChanged]:
                    break
                time.sleep(0.02)
            c.post("/interactive/step/w1?name=camera")
            g.receive_text()
            with c.websocket_connect("/interactive/ws/pose") as p:
                p.send_text('{"type":"pose_hello","camera_id":"scene","source":"arkit-world","device":"test"}')
                # the head is 1 m in front of the phone, in world coordinates far from the origin (z may be negative)
                cam = {"id": "camera", "p": [0.5, 1.0, -2.0], "how": "camera"}
                cup = {"id": "cup", "p": [0.6, 1.0, -2.0], "how": "lidar"}  # 0.1 m world +x = head -x = display right
                msg = world_pose(WORLD_R, [0.5, 1.0, -1.0], [cam, cup])
                fr = ring_after(c, p, msg, "w1")
                assert fr and [q.kind for q in fr.primitives] == ["ring"]
                assert abs(fr.primitives[0].cx - 288) < 1 and 143 <= fr.primitives[0].cy <= 161  # not mirrored
                st = c.get("/interactive/status").json()
                assert st["pose"]["source"] == "arkit-world" and st["pose"]["head_ok"]
                assert st["pose"]["mirror"]["using"] is False
                assert st["anchors_world"]["cup"]["how"] == "lidar"
                assert st["anchors_world"]["cup"]["dist_from_head"] == pytest.approx(math.hypot(1.0, 0.1), abs=0.01)
                assert st["locate"] == {"pending": [], "errors": {}}
                assert st["face_memory"]["active"] is False and "ok" in st["imu_calib"]
                assert st["pose"]["phone_from_head"]["ahead"] is True
                assert c.post("/interactive/step/w1?anchor=cup&name=mug").json() == {"targets": ["cup"]}
                g.receive_text()
                fr = ring_after(c, p, msg, "w1")  # a tapped anchor renders; 0.1 m right of the phone -> right of centre
                assert fr and fr.primitives[0].kind == "ring" and fr.primitives[0].cx > 288 + 40
                assert c.get("/interactive/status").json()["sessions"]["w1"]["targets"] == ["cup"]
                # mirror switch is per source and flips the side
                assert c.post("/interactive/mirror?source=arkit-world&on=true").json()["mirror_sources"] == [
                    "arkit",
                    "arkit-world",
                ]
                fr = ring_after(c, p, msg, "w1")
                assert fr and fr.primitives[0].cx < 288 - 40
                c.post("/interactive/mirror?source=arkit-world&on=false")
            assert c.post("/interactive/step/w1").json() == {"targets": []}
    finally:
        server._background.remove(bg)
        del server.app.router.routes[n_routes:]


class FakeApp:
    def __init__(self):
        self.sent = []

    async def send_text(self, s):
        self.sent.append(json.loads(s))


def test_locate_flow_grounds_then_uses_the_apps_anchor(tmp_path, monkeypatch):
    import asyncio

    from bluebook_core.protocol import CameraFrame
    from bluebook_interactive import targets as T

    calls = []

    async def fake_ground(jpeg, tg, ex=()):
        calls.append(tg.name)
        return (0.2, 0.3, 0.4, 0.5)

    monkeypatch.setattr(T.vision, "ground", fake_ground)
    monkeypatch.setattr(T.vision, "exemplars", lambda *a, **k: [])

    async def go():
        tk = Tracker(tmp_path)
        tk.app = app = FakeApp()
        tk.hello = PoseHello(type="pose_hello", source="arkit-world")
        hub.publish(CameraFrame(session="scene", t=time.time(), jpeg=b"x", width=10, height=10))
        mug = StepTarget(object_id="mug", name="mug")
        await tk.targets.on_step(StepChanged(session="s", skill_id="x", step_id="a", targets=[mug]))
        task = asyncio.create_task(tk.targets.ground_loop())
        for _ in range(100):
            if app.sent:
                break
            await asyncio.sleep(0.02)
        assert app.sent == [{"type": "locate", "id": "mug", "box": [0.2, 0.3, 0.4, 0.5]}]
        assert tk.targets.resolve("s", 0, None, None) == [] and tk.status()["locate"]["pending"] == ["mug"]
        await asyncio.sleep(1.3)  # another loop pass: a pending locate is not re-sent, and Gemini is not re-asked
        assert len(app.sent) == 1 and calls == ["mug"]
        cam = {"id": "camera", "p": [0, 1, 0], "how": "camera"}
        m = PoseMsg.model_validate({**world_pose(np.eye(3), [0, 1, 1], [cam, {"id": "mug", "p": [1, 2, 3]}]), "t": 1})
        tk.on_pose(m, 1.0)
        ((oid, X),) = tk.targets.resolve("s", 0, None, None)
        assert oid == "mug" and X.tolist() == [1, 2, 3] and not tk.targets.pending
        await asyncio.sleep(1.3)
        assert len(app.sent) == 1  # placed anchor is trusted: not moved again within RELOCATE_S
        # the app reports it could not place it: ground again, from a fresh frame
        tk.on_pose(PoseMsg.model_validate({**m.model_dump(), "errors": {"mug": "moved"}}), 2.0)
        tk.on_pose(m, 2.05)  # the app sends each error once: it must stick until the next pass sees it
        tk.targets.located["mug"] -= T.ERROR_RETRY_S
        tk.targets._tried_w.clear()
        for _ in range(100):
            if len(app.sent) > 1:
                break
            await asyncio.sleep(0.02)
        assert app.sent[1]["type"] == "locate" and calls == ["mug", "mug"]
        moved = {"id": "mug", "p": [1.0, 2.0, 2.5]}
        tk.on_pose(PoseMsg.model_validate({**world_pose(np.eye(3), [0, 1, 1], [cam, moved]), "t": 3}), 3.0)
        assert "mug" not in tk.targets.errors and not tk.targets.pending  # re-placed: error cleared
        # next step: what we placed is forgotten
        await tk.targets.on_step(StepChanged(session="s", skill_id="x", step_id="b", targets=[]))
        assert app.sent[-1] == {"type": "forget", "id": "mug"} and "mug" not in tk.targets.world_anchors
        task.cancel()

    asyncio.run(go())


def test_app_owned_anchor_is_never_grounded(tmp_path):
    tk = Tracker(tmp_path)
    tk.hello = PoseHello(type="pose_hello", source="arkit-world")
    tk.targets.app_owned.add("cup")
    assert not tk.targets._due_world("cup", 1e9) and tk.targets._due_world("mug", 1e9)


def test_boresight_learns_only_from_the_phone_target(tmp_path):
    from bluebook_interactive.targets import Anchor

    def run(oid):
        s = Sim(tmp_path / oid, lambda t: pose_looking_at(dir_of(-3.0, -2.0)), noise_deg=0.5, seed=1, bore=None)
        if oid != "camera":  # a grounded object, "found" in the middle of the scene image, 1 m away
            s.tk.targets.steps["s"] = StepChanged(
                session="s", skill_id="x", step_id="a", targets=[StepTarget(object_id=oid, name=oid)]
            )
            s.tk.targets.anchors[("s", oid)] = Anchor((0.45, 0.45, 0.55, 0.55), 0.0)
        s.run(30)
        return s, s.tk.display("s").learner("vision")

    s, L = run("mug")
    assert s.r.shown and L.w == 0 and L.n_obs == 0  # drawn, but never learned from
    _, L = run("camera")
    assert L.w > 10 and L.n_obs > 0


def test_boresight_status_exposes_its_state(tmp_path, monkeypatch):
    from bluebook_core import glasses

    tk = make_tracker(tmp_path)
    monkeypatch.setitem(glasses.sessions, "s", object())  # type: ignore[arg-type]
    b = tk.status()["boresight"]["s"]
    assert {"dwell_s", "observations", "observing", "last_update_age_s", "learns_from", "confidence"} <= set(b)


# ---- IMU calibration + face memory

DOWN = np.array([0.0, -1.0, 0.0])


def wh(yaw, pitch, roll):
    """Head -> world for a head tilted by these angles (deg)."""
    return rot([0, 1, 0], yaw) @ rot([1, 0, 0], pitch) @ rot([0, 0, 1], roll)


def test_imu_calib_recovers_a_known_rotation_and_gates_on_conditioning():
    from bluebook_interactive.imu import ImuCalib

    rng = np.random.default_rng(0)
    R_true = rot([0.3, -0.5, 0.8], 70)
    for sign in (1.0, -1.0):  # accelerometer pointing along gravity, or up (measured specific force)
        c = ImuCalib()
        for i in range(120):
            g = wh(rng.uniform(-180, 180), rng.uniform(-25, 25), rng.uniform(-20, 20)).T @ DOWN
            imu = sign * R_true @ g
            imu = rodrigues(np.radians(rng.normal(0, 1.5, 3))) @ imu * rng.normal(1, 0.01)
            c.add(*imu, now=i * 2.0, g_head=g)  # 2 s apart: no low-pass carry-over between these independent tilts
        c.fit()
        assert c.ok and c.sign == sign
        assert rot_angle(c.R, R_true) < math.radians(1.5)  # sign * imu = R @ gravity_in_head
        probe = wh(40, 10, -5).T @ DOWN
        np.testing.assert_allclose(c.R @ probe, R_true @ probe, atol=0.03)
        assert c.residual_deg < 3.0 and c.spread_deg > 8
    # every pair from (nearly) the same tilt: rotation about it is undetermined -> not ok
    c = ImuCalib()
    g0 = wh(0, 10, 0).T @ DOWN
    for i in range(100):
        g = rodrigues(np.radians(rng.normal(0, 0.3, 3))) @ g0
        c.add(*(R_true @ g), now=i * 2.0, g_head=g)
    c.fit()
    assert not c.ok and c.spread_deg < 4
    # not at 1 g (accelerating) -> sample not used
    c2 = ImuCalib()
    c2.add(*(2 * (R_true @ g0)), now=0.0, g_head=g0)
    assert len(c2.pairs) == 0


class World:
    """A world-mode tracker on a fake clock with a head that tilts around, a glasses IMU rigidly attached to it."""

    def __init__(self, tmp, calibrate=True, sign=1.0, noise_deg=0.0):
        self.now = 100.0
        self.rng = np.random.default_rng(0)
        self.noise_deg = noise_deg  # camera pose noise (pitch/roll/yaw, deg): rear-camera Vision is ~1-3
        self.tk = Tracker(tmp, lambda: self.now)
        self.tk.hello = PoseHello(type="pose_hello", source="arkit-world")
        self.tk.targets.steps["s"] = StepChanged(
            session="s", skill_id="x", step_id="a", targets=[StepTarget(object_id="camera", name="phone")]
        )
        self.R_imu = rot([0.2, 1.0, -0.4], 110)
        self.sign = sign
        self.att = (10.0, 5.0, 0.0)  # yaw, pitch, roll of the head
        self.t0 = self.now
        if calibrate:
            self.sweep(25.0)

    def pose(self):
        R = wh(*(a + n for a, n in zip(self.att, self.rng.normal(0, self.noise_deg, 3), strict=True)))
        cam = {"id": "camera", "p": [0.0, 1.0, -1.0], "how": "camera"}
        m = world_pose(R, [0.0, 1.0, 0.0], [cam], t_s=self.now - 0.05)
        self.tk.on_pose(PoseMsg.model_validate(m), self.now)

    def imu(self, att=None):
        g = wh(*(att or self.att)).T @ DOWN
        self.tk.on_imu("s", *(self.sign * self.R_imu @ g))

    def sweep(self, seconds):
        end = self.now + seconds
        k = 0
        while self.now < end:
            u = self.now - self.t0
            self.att = (
                10 + 6 * math.sin(u / 5),
                5 + 15 * math.sin(2 * math.pi * u / 10),
                12 * math.sin(2 * math.pi * u / 7.3),
            )
            self.pose()
            if k % 3 == 0:
                self.imu()
            self.now += 1 / 30
            k += 1


def gravity_angle(R1, R2):
    return math.degrees(math.acos(np.clip((R1.T @ DOWN) @ (R2.T @ DOWN), -1, 1)))


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_world_face_memory_follows_imu_pitch_roll_then_times_out(tmp_path, sign):
    w = World(tmp_path, sign=sign)
    tk = w.tk
    cal = tk.imu_calib["s"]
    assert cal.ok and cal.residual_deg < 4 and tk.status()["imu_calib"]["ok"]
    assert tk.memory("s", w.now) is None  # face is tracked: no memory in use
    R0 = tk.head.R.copy()
    t_lost = tk.last_face
    # face lost; the head pitches 15 deg further down while the IMU keeps streaming (yaw unobservable)
    new = (w.att[0], w.att[1] + 15, w.att[2])
    while w.now < t_lost + 1.2:
        w.now += 0.1
        w.imu(new)
    assert not tk.tracking(w.now)
    held, mode = tk.memory("s", w.now)
    assert mode == "imu" and tk.status()["face_memory"]["active"] and tk.status()["face_memory"]["mode"] == "imu"
    assert gravity_angle(held.R, wh(*new)) < 3.0  # pitch/roll followed the IMU...
    assert gravity_angle(held.R, R0) > 10.0
    heading = lambda R: math.degrees(math.atan2((R @ [0, 0, 1])[0], (R @ [0, 0, 1])[2]))
    assert abs(heading(held.R) - heading(R0)) < 8.0  # ...yaw kept (minimal rotation)
    np.testing.assert_allclose(held.p, tk.head.p)
    while w.now < t_lost + 2.9:
        w.now += 0.1
        w.imu(new)
    assert tk.memory("s", w.now) is not None
    while w.now < t_lost + 3.2:
        w.now += 0.1
        w.imu(new)
    assert tk.memory("s", w.now) is None and not tk.status()["face_memory"]["active"]  # MEMORY_S is over


def test_imu_blend_removes_most_of_a_noisy_cameras_tilt_error_and_keeps_yaw(tmp_path):
    def run(calibrate):
        w = World(tmp_path / str(calibrate), calibrate=calibrate, noise_deg=2.5)
        if not calibrate:
            w.sweep(25.0)  # same history, no IMU samples
        w.att, errs, yaws = (20.0, 8.0, 3.0), [], []
        for k in range(150):  # 5 s holding still
            w.pose()
            if k % 3 == 0 and calibrate:
                w.imu()
            w.now += 1 / 30
            if k >= 60:
                errs.append(gravity_angle(w.tk.head.R, wh(*w.att)))
                yaws.append(math.degrees(math.atan2((w.tk.head.R @ [0, 0, 1])[0], (w.tk.head.R @ [0, 0, 1])[2])))
        return w.tk, float(np.mean(errs)), float(np.mean(yaws))

    tk, blended, yaw_b = run(True)
    assert tk.imu_calib["s"].ok and tk.imu_w > 0.8  # a noisy camera hands the tilt to the IMU
    _, camera_only, yaw_c = run(False)
    assert blended < 0.5 * camera_only, (blended, camera_only)
    assert abs(yaw_b - yaw_c) < 1.5  # yaw is the camera's alone


def test_world_face_memory_without_a_calibrated_imu_just_holds_for_less(tmp_path):
    from bluebook_interactive.tracker import MEMORY_HOLD_S

    w = World(tmp_path, calibrate=False)
    for _ in range(30):
        w.pose()
        w.now += 1 / 30
    R0, t_lost = w.tk.head.R.copy(), w.tk.last_face
    w.now = t_lost + 0.8
    held, mode = w.tk.memory("s", w.now)
    assert mode == "hold" and rot_angle(held.R, R0) < 1e-9
    w.now = t_lost + MEMORY_HOLD_S + 0.1
    assert w.tk.memory("s", w.now) is None
    # other pose sources have no gravity reference at all: same short hold
    tk = make_tracker(tmp_path / "v", source="vision")
    assert tk.memory("s", 5.0) is None and tk.imu_calib_status()["ok"] is False


def test_renderer_draws_from_memory_then_freezes_never_hides(tmp_path):
    w = World(tmp_path, calibrate=False)
    r = w.tk.renderer("s")
    r.rtt.extend([0.05] * 12)
    w.tk.display("s").learners["arkit-world"] = BoresightLearner((0.0, 0.0, 60.0))
    for _ in range(60):
        w.pose()
        w.now += 1 / 30
        r.tick(w.now)
    assert r.visible and r.shown and r.mem_mode is None
    t_lost = w.tk.last_face
    seen = []
    while w.now < t_lost + 6.0:
        w.now += 0.05
        r.tick(w.now)
        seen.append((w.now - t_lost, list(r.shown), r.mem_mode))
    assert all(sh and m == "hold" for dt, sh, m in seen if 0.55 < dt < 0.95)  # drawn from the held pose
    late = [sh for dt, sh, _ in seen if dt > 1.5]
    assert all(sh for sh in late) and all(sh == late[0] for sh in late)  # never hidden: frozen where it was


def test_renderer_does_not_spin_when_acks_never_come(tmp_path):
    """An unchanged frame publishes nothing, so a stale `inflight` must not keep the run loop 'due' forever."""
    r = make_tracker(tmp_path).renderer("s")
    r.inflight = 10.0
    t = 10.0 + r.timeout() + 0.01
    assert r.due(t)
    r.tick(t)
    assert not r.due(t) and r.inflight is None


def test_noise_is_learned_per_face_source(tmp_path):
    """Front ARKit (~0.1 deg) and rear Vision (~1 deg) must not share one noise estimate."""
    from bluebook_interactive.geom import rodrigues

    tk, rng = make_tracker(tmp_path), np.random.default_rng(0)
    head_on = np.diag([1.0, -1.0, -1.0])
    for src, sd in (("front", 0.1), ("rear", 1.0)):
        tk.face_src = src
        for i in range(120):
            R = head_on @ rodrigues(np.radians(rng.normal(0, sd, 3)))
            tk.noise.update(R, np.array([0, 0, 1.0]), 100 + i / 30 + (0 if src == "front" else 10))
    assert tk.noises["front"].rot_deg < 0.3
    assert tk.noises["rear"].rot_deg > 0.6


def test_static_point_holds_through_jitter_and_follows_real_moves():
    from bluebook_interactive.filters import StaticPoint

    rng, sp, out = np.random.default_rng(0), StaticPoint(), []
    base = np.array([0.1, -0.2, -0.8])
    for i in range(300):  # 10 s @ 30 Hz of SLAM-like jitter (5 mm) on a static object
        out.append(sp.update(base + rng.normal(0, 0.005, 3), i / 30).copy())
    settled = np.array(out[60:])
    assert np.ptp(settled, axis=0).max() == 0.0  # perfectly still once settled
    assert np.linalg.norm(settled[-1] - base) < 0.01
    moved = base + [0.05, 0, 0]  # a real 5 cm shift (re-localisation) is adopted within the window
    for i in range(300, 390):
        last = sp.update(moved + rng.normal(0, 0.005, 3), i / 30)
    assert np.linalg.norm(last - moved) < 0.01
    replaced = base + [0.5, 0, 0]  # a new tap/locate 50 cm away is adopted at once
    assert np.linalg.norm(sp.update(replaced, 13.1) - replaced) < 1e-9


def test_tracked_object_point_follows_motion():
    """A coupled (moving) object: follow it with only a short de-jitter window, unlike the static hold."""
    from bluebook_interactive.filters import StaticPoint

    rng, sp = np.random.default_rng(1), StaticPoint()
    sp.window_s, sp.hold = StaticPoint.TRACKED_WINDOW_S, False
    for i in range(150):  # object slides 10 cm/s for 5 s with 5 mm jitter
        true = np.array([0.1 * i / 30, 0.0, -0.8])
        out = sp.update(true + rng.normal(0, 0.005, 3), i / 30)
    assert np.linalg.norm(out - true) < 0.03  # within ~3 cm of a 10 cm/s object (window lag ~2 cm)


def test_slow_pan_is_followed_not_averaged(tmp_path):
    """A slow deliberate pan (8 deg/s) must be followed promptly (an earlier fixation lock averaged it and lagged)."""
    w = World(tmp_path, calibrate=False)
    r = w.tk.renderer("s")
    r.rtt.extend([0.1] * 12)
    for _ in range(60):  # settle on target (yaw 180 faces the phone in this world)
        w.att = (180.0, 0.0, 0.0)
        w.pose()
        w.now += 1 / 30
        r.tick(w.now)
    t0, xs = w.now, []
    while w.now < t0 + 1.2:  # pan 8 deg/s: the phone drifts across the view
        w.att = (180.0 + 8.0 * (w.now - t0), 0.0, 0.0)
        w.pose()
        w.now += 1 / 30
        r.tick(w.now)
        xs.append((r.shown[0].cx, r.marks["camera"].debug["uv"][0]))
    shown, true = xs[-1]
    assert abs(shown - true) < 40  # following within a ring radius, not lagging behind


def test_replay_of_a_real_rear_camera_clip_is_smooth_and_does_not_flip_flop():
    """45 s of a real session (rear-camera Vision face + LiDAR, glasses IMU on, highlighting the phone), replayed
    through Tracker + Renderer. Guards both ways: a smoothing change that brings back the lag (one gave 570 ms here) or
    one that brings back big jumps (~131 px) fails."""
    import gzip
    from pathlib import Path

    from bluebook_interactive import replay

    path = Path(__file__).parent / "rear_imu_clip.jsonl.gz"
    recs = [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines() if line]
    cap = {}
    m = replay.replay(recs, metrics=lambda rows, raw: cap.setdefault("rows", rows) and replay._metrics(rows, raw))
    rear = m["rear"]
    assert rear["lag_ms"] <= 450 and rear["max_step_px"] < 130.0 and rear["jitter_px"] < 16.0, rear
    ok = [r for r in cap["rows"] if r["ok"]]
    switches = [r["t"] for a, r in itertools.pairwise(ok) if a["chev"] != r["chev"]]
    # no ring<->chevron ping-pong (a real nod through the 144 px band switches twice ~1 s apart; flapping is < 0.5 s)
    assert all(b - a >= 0.5 for a, b in itertools.pairwise(switches)), switches


def test_imu_is_only_switched_off_by_us_after_we_switched_it_on(tmp_path):
    """Part 2 publishes target-less steps too: Part 3 must not send the glasses anything for those."""
    import asyncio

    async def go():
        tk, calls = Tracker(tmp_path), []

        async def set_imu(s, pace):
            calls.append(pace)

        tk._set_imu = set_imu
        task = asyncio.create_task(tk._steps_loop())
        await asyncio.sleep(0)
        step = lambda targets: hub.publish(StepChanged(session="g", skill_id="x", step_id="a", targets=targets))
        step([])
        await asyncio.sleep(0.05)
        assert calls == []
        step([StepTarget(object_id="camera", name="phone")])
        await asyncio.sleep(0.05)
        step([])
        await asyncio.sleep(0.05)
        assert calls == [100, 0]
        task.cancel()
        for t in tk._tasks.values():
            t.cancel()

    asyncio.run(go())
