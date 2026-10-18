"""Per-glasses renderer: turns the head pose + targets into overlay frames at the glasses' own pace.

The 30 Hz pose stream never drives drawing. Each session's Renderer ticks when the previous publish has been acked
(ImageDrawn) or after a timeout derived from the measured draw round trip, never faster. A tick:
  predicted head pose at (now + measured latency) -> target screen position -> ScreenSmoother -> primitives -> publish.
Every scale here (spring speed, dead-band, hide speed, velocity cap, timeouts) is derived from the measured display
update interval, the filter's own covariance, or the ring size; the few remaining constants are priors, marked.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from bluebook_core import glasses, hub
from bluebook_core.protocol import Arrow, ImageDrawn, OverlayFrame, Primitive, Ring

from .filters import ScreenSmoother
from .geom import BAND, RING_R, edge_point, in_band, place, project
from .targets import PHONE

if TYPE_CHECKING:
    from .tracker import Tracker

SETTLE_TICKS = 3  # the spring settles (5%) in this many display updates (4 lagged on hardware; the ring hold steadies)
K_SIGMA = 3.0  # chevron dead-band = this many predicted screen-space sigmas (5 froze it, then it lurched)
MIN_DEADBAND_PX = 1.0  # display resolution
HYST_MIN_PX = 1.5 * RING_R  # ring <-> chevron hysteresis around the activation area (at least; see tick): replayed
# rear-camera sessions flip-flopped less with 60 than 20 px once the area was enlarged
# Activation area: the ring shows while the target is within this far OUTSIDE the band too (drawn clamped to the band
# edge), so it spawns earlier and does not turn into a chevron the moment the target grazes the edge.
ACTIVATE_PX = RING_R
# Once spawned, the ring holds still while the target stays inside its inner part (so the target is always visibly in
# the circle: stable AND accurate); leaving that, it glides after it. Replaces the noise-scaled dead-band for rings,
# which on a noisy pose grew past the ring itself.
RING_HOLD_PX = 0.6 * RING_R
# The mark is NEVER hidden (owner: "the cursor should have memory of wherever it was pointing"): out of view it is
# clamped to the band edge pointing at the target (minimap-style); with no fresh pose it stays frozen where it was.
# Speed cap per display update: a third of the band width, so even fast turns glide instead of teleporting.
VMAX_PX_PER_UPDATE = (BAND[2] - BAND[0]) / 3
# Head speed (noise already removed by the Kalman) below SWAY is natural sway while looking at something: no
# prediction, dead-band on. Above TURN it is a deliberate turn: full latency prediction. Linear ramp between.
SWAY_DEG_S, TURN_DEG_S = 4.0, 15.0
# Priors, used until enough draws/poses were measured (G2 over BLE: ~90 ms/tile, ~160 ms round trip; phone->server ~50 ms)
PRIOR_RTT_S, PRIOR_AGE_S, MIN_SAMPLES = 0.16, 0.05, 8
PERIOD_S = (0.05, 0.4)  # bounds on the display update interval
TIMEOUT_S = (0.2, 0.8)  # bounds on the ack timeout (2 x p90 round trip)
LEAD_MAX_S = 0.6  # never predict further ahead than this
KEEPALIVE_S = 2.0  # republish an unchanged frame this often (a page redraw clears the compositor's tiles)


@dataclass
class Mark:
    sm: ScreenSmoother = field(default_factory=ScreenSmoother)
    chevron: bool | None = None
    debug: dict = field(default_factory=dict)  # projection chain of the last tick, for checking it on hardware


def _in_band(u: float, v: float) -> tuple[float, float]:
    """Ring centre kept inside the band (inset half a ring radius, so most of the ring stays visible)."""
    m = RING_R / 2
    return min(max(u, BAND[0] + m), BAND[2] - m), min(max(v, BAND[1] + m), BAND[3] - m)


def _round(p: Primitive) -> Primitive:
    """Whole pixels, so equal primitives are pixel-identical."""
    if isinstance(p, Ring):
        return Ring(cx=round(p.cx), cy=round(p.cy), r=p.r)
    if isinstance(p, Arrow):
        return Arrow(x0=round(p.x0), y0=round(p.y0), x1=round(p.x1), y1=round(p.y1))
    return p


class Renderer:
    def __init__(self, session: str, tk: Tracker) -> None:
        self.session, self.tk = session, tk
        self.rtt: deque[float] = deque(maxlen=50)  # s, SetImage created -> ImageDrawn received
        self.draw_ms: deque[float] = deque(maxlen=50)  # SDK draw time on the phone
        self.ack_times: deque[float] = deque(maxlen=50)
        self.tick_times: deque[float] = deque(maxlen=50)
        self.marks: dict[str, Mark] = {}
        self.visible = False
        self.motion = 0.0  # 0 = sway/still (no prediction, dead-band on) .. 1 = deliberate turn

        self.shown: list[Primitive] = []
        self.inflight: float | None = None
        self.pub_t = self.last_tick = 0.0
        self.ack_pending = False
        self.lead = self.deadband = 0.0
        self.mem_mode: str | None = None
        self._ack = asyncio.Event()

    # ------------------------------------------------------------------ pacing

    def period(self) -> float:
        """Idle update interval: the measured draw round trip."""
        rtt = statistics.median(self.rtt) if len(self.rtt) >= MIN_SAMPLES else PRIOR_RTT_S
        return min(max(rtt, PERIOD_S[0]), PERIOD_S[1])

    def interval(self) -> float:
        """The display update interval the spring and the speed limit are scaled to (= period)."""
        return self.period()

    def timeout(self) -> float:
        """How long to wait for an ack before drawing anyway: 2 x p90 of the measured round trip."""
        p90 = float(np.percentile(self.rtt, 90)) if len(self.rtt) >= MIN_SAMPLES else PRIOR_RTT_S
        return min(max(2 * p90, TIMEOUT_S[0]), TIMEOUT_S[1])

    def wait_time(self, now: float) -> float:
        """Seconds until the next tick if no ack arrives."""
        if self.inflight is None:
            return max(0.0, self.last_tick + self.period() - now)
        return max(0.0, self.inflight + self.timeout() - now)

    def due(self, now: float) -> bool:
        return self.ack_pending or self.wait_time(now) <= 0

    def on_ack(self, ev: ImageDrawn, now: float) -> None:
        self.draw_ms.append(ev.ms)
        self.ack_times.append(now)
        if ev.t is not None and ev.at >= ev.t:
            self.rtt.append(min(ev.at - ev.t, 1.0))
        # an ack of an older publish (that already timed out) must not release the newer one
        if self.inflight is not None and (ev.t is None or ev.t >= self.inflight - 0.01):
            self.inflight, self.ack_pending = None, True
            self._ack.set()

    async def run(self) -> None:
        """Tick at the glasses' pace for as long as the session has a step with targets."""
        while True:
            now = self.tk.clock()
            if self.due(now):
                if self.session in glasses.sessions:
                    self.tick(now)
                else:  # nobody to draw for
                    self.last_tick, self.inflight = now, None
                continue
            self._ack.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ack.wait(), max(self.wait_time(now), 0.001))

    # ------------------------------------------------------------------ publishing

    def reset(self) -> None:
        """The page was redrawn (compositor cleared): the next tick must draw again."""
        self.shown, self.inflight, self.ack_pending = [], None, False

    def _publish(self, prims: list[Primitive], now: float, wait_ack: bool = True) -> None:
        hub.publish(OverlayFrame(session=self.session, t=now, primitives=prims))
        if wait_ack and prims != self.shown:
            self.inflight = now
        self.shown, self.pub_t = prims, now

    def _emit(self, prims: list[Primitive], now: float) -> None:
        if not prims:  # an empty frame would wipe Part 2's keyframe tiles: only to take our own mark away
            if self.shown:
                self._publish([], now)
        elif prims != self.shown:
            self._publish(prims, now)
        elif now - self.pub_t >= KEEPALIVE_S:
            self._publish(prims, now, wait_ack=False)

    def clear(self, now: float) -> None:
        self.visible = False
        self.marks.clear()
        self._emit([], now)

    # ------------------------------------------------------------------ the tick

    def tick(self, now: float) -> None:
        tk = self.tk
        self.ack_pending = False
        self.inflight = None  # a tick is due only after the ack or its timeout: either way nothing is awaited now
        dt = min(now - self.last_tick, 1.0) if self.last_tick else self.interval()
        self.last_tick = now
        self.tick_times.append(now)
        disp = tk.display(self.session)
        head = tk.head
        f, T = disp.model.f, self.interval()
        tracking = tk.tracking(now)
        mem = None if tracking else tk.memory(self.session, now)  # face lost: (held pose, "imu" | "hold")
        self.mem_mode = mem[1] if mem else None

        pts = tk.targets.resolve(self.session, now, tk.cam, tk.head_z) if head.R is not None else []
        if not pts:  # no target (step cleared) or no head pose ever: take our mark away
            self.visible = False
            self.marks.clear()
            self._emit([], now)
            return
        self.visible = True
        if not (tracking or mem):  # face lost beyond the memory window: the mark stays frozen where it was
            if self.shown:
                self._emit(self.shown, now)
            return

        source = tk.source
        e = disp.model.eye()
        # learn the boresight from where the wearer holds the head still (see BoresightLearner), ONLY from the
        # ground-truth target (the phone): Gemini/tapped objects may be mislocated and must never teach the display
        if tracking and any(oid == PHONE for oid, _ in pts):
            now_pose = head.predict(0.0)
            X = next(X for oid, X in pts if oid == PHONE)
            disp.learner(source).observe(tk.mirror @ now_pose.R.T @ (X - now_pose.p) - e, head.still, now, dt)
            disp.maybe_save(now)
        P = disp.P(source)

        # predicted pose at the moment the drawing becomes visible: time since the last pose + pose age + draw RTT
        age = statistics.median(tk.pose_age) if len(tk.pose_age) >= MIN_SAMPLES else PRIOR_AGE_S
        rtt = statistics.median(self.rtt) if len(self.rtt) >= MIN_SAMPLES else PRIOR_RTT_S
        omega = 4.74 / (SETTLE_TICKS * T)  # rad/s: 5%-settling time of a critically damped spring is 4.74/omega
        # Prediction cancels latency during REAL turns only. Extrapolating natural micro-sway (a few deg/s even when
        # "holding still") made the cursor bounce, so the lead ramps in with the noise-free head speed.
        self.motion = motion = head.motion(SWAY_DEG_S, TURN_DEG_S)
        latency = max(0.0, now - tk.last_face) + age + rtt + 1 / (2 * omega)
        self.lead = lead = min(LEAD_MAX_S, latency * motion)
        pred = mem[0] if mem else head.predict(lead)
        vmax = VMAX_PX_PER_UPDATE / T  # px/s

        prims: list[Primitive] = []
        self.deadband = 0.0
        for oid, X in pts:
            Xh = tk.mirror @ pred.R.T @ (X - pred.p)  # target in the head frame: +x wearer's left, +y up, +z ahead
            u, v, w = project(P, Xh)[0]
            mark = self.marks.setdefault(oid, Mark())
            mark.debug = {
                "world": [round(float(c), 3) for c in X],
                "head": [round(float(c), 3) for c in Xh],
                "az_left_deg": round(math.degrees(math.atan2(Xh[0], Xh[2])), 1),
                "el_up_deg": round(math.degrees(math.atan2(Xh[1], math.hypot(Xh[0], Xh[2]))), 1),
                "uv": [round(float(u)), round(float(v))] if w > 0 else None,
                "behind": bool(w <= 0),
            }
            # ring <-> chevron only when clearly past the band edge: the margin covers the target's own noise
            # (a ~1 deg noisy rear-camera pose flip-flopped across a 10 px margin)
            sig_px = f * pred.ang_sigma(X)
            hyst = max(HYST_MIN_PX, 3 * sig_px)
            chevron = w <= 0 or not in_band(u, v, w, ACTIVATE_PX + (-hyst if mark.chevron else hyst))
            g = edge_point(u, v, w) if chevron else _in_band(u, v)
            if chevron != mark.chevron:  # snap: gliding a ring<->chevron switch through the spring + speed cap lagged
                mark.chevron = chevron
                mark.sm.reset(g)
            # hold through noise and micro-sway (ring: geometric, see RING_HOLD_PX); a real turn (motion > 0) is
            # followed without one
            if not tracking or motion > 0.0:
                db = 0.0
            else:
                db = max(MIN_DEADBAND_PX, K_SIGMA * sig_px) if chevron else RING_HOLD_PX
            self.deadband = max(self.deadband, db)
            x = mark.sm.step(g, dt, omega, db, vmax)
            prims += [_round(p) for p in place(float(x[0]), float(x[1]), 1.0, chevron=chevron)]
        self._emit(prims, now)

    # ------------------------------------------------------------------ debug

    def status(self, now: float) -> dict:
        tt = [t for t in self.tick_times if now - t < 3.0]
        acks = [t for t in self.ack_times if now - t < 3.0]
        return {
            "visible": self.visible,
            "tick_hz": round((len(tt) - 1) / (tt[-1] - tt[0]), 1) if len(tt) > 1 and tt[-1] > tt[0] else 0.0,
            "interval_ms": round(self.interval() * 1000),
            "latency_ms": round(self.lead * 1000),
            "deadband_px": round(self.deadband, 1),
            "draw_ms_p50": round(statistics.median(self.draw_ms)) if self.draw_ms else None,
            "draw_rtt_ms_p50": round(statistics.median(self.rtt) * 1000) if self.rtt else None,
            "timeout_ms": round(self.timeout() * 1000),
            "acks_per_s": round((len(acks) - 1) / (acks[-1] - acks[0]), 1) if len(acks) > 1 else 0.0,
            "inflight": self.inflight is not None,
            "face_memory": self.mem_mode,
            "motion": round(self.motion, 2),
            "marks": {k: ("chevron" if m.chevron else "ring") for k, m in self.marks.items()},
            "projection": {k: m.debug for k, m in self.marks.items()},
        }
