"""The Tracker: one object owning everything Part 3 knows at runtime.

Phone pose messages -> noise estimate -> head Kalman filter. Step changes -> targets. Per glasses session a Renderer
(render.py) draws at the display's pace using the filtered pose; the BoresightLearner (optics.py) adapts the display
to the wearer. Loops in run(): steps, grounding, drawn-acks, imu, plus one renderer task per active session.
Spec: workstreams/part3-interactive.md.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import statistics
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np
from bluebook_core import display as core_display
from bluebook_core import glasses, hub
from bluebook_core.protocol import ImageDrawn, ImuSample, OverlayFrame, StepChanged
from fastapi import WebSocket
from PIL import Image, ImageDraw

from .filters import HeadKalman, HeadPose, NoiseEstimator
from .geom import BAND, K_of, rodrigues, rodrigues_inv, rot_between, to_head
from .imu import DOWN, ImuCalib
from .optics import Display
from .record import Recorder
from .render import Renderer
from .targets import PHONE, Cam, Targets
from .wire import PoseHello, PoseMsg

log = logging.getLogger("bluebook.interactive")

FRESH_S = 0.5  # a pose older than this no longer counts as tracking
MIN_FACE_CONF = 0.5  # face conf is app-defined in [0, 1]; below the midpoint the pose is not trusted
MIN_FACE_Z = 0.05  # m; a face closer than this is a tracker failure
ZERO = np.zeros(3)
MX = np.diag([-1.0, 1.0, 1.0])
WORLD = "arkit-world"  # pose source: head pose + anchors in one gravity-aligned ARKit world frame
# Pose sources whose head x axis is mirrored (see Tracker.mirror). The face-only "arkit" mode is known to be; the
# world mode is raw ARKit and UNKNOWN until verified on hardware: flip with POST /interactive/mirror.
MIRROR_SOURCES = {"arkit"}
# Face memory: when the face is lost keep drawing from the last good pose, this long (counted from the last face
# pose). Priors: ~3 s is about how long a wearer looks down at the work / turns away before looking back; without a
# live IMU the held pose cannot follow the head, so it is trusted for much less.
MEMORY_S, MEMORY_HOLD_S = 3.0, 1.0
MEM_SIG_DEG_PER_S = 3.0  # held pose: assumed yaw drift (unobservable without the face), grows the pose's sigma
IMU_MAX_HEAD_DEG_S = 15.0  # only pair IMU with head gravity when the head is slow: pose/IMU timing skew ~0.1 s
# IMU tilt blend (world mode): the G2 IMU's gravity gives pitch/roll far cleaner than rear-camera Vision (2-3 deg per
# frame measured), so the measured pose's tilt is pulled toward it by the noise ratio; yaw is the camera's alone.
# Prior for the IMU tilt error (low-passed accelerometer + calibration), deg: the knob if the blend over/under-trusts.
IMU_TILT_SIG_DEG = 0.5
# Convention self-check: which mirror fix of the app's (R, t) agrees with ARKit's own cam_in_head.
VARIANTS = {
    "as sent": lambda R, t: (R, t),
    "R mirrored": lambda R, t: (MX @ R @ MX, t),
    "t mirrored": lambda R, t: (R, MX @ t),
    "both mirrored": lambda R, t: (MX @ R @ MX, MX @ t),
}


async def _forever(name: str, fn) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s loop crashed, restarting", name)
            await asyncio.sleep(1)


class Tracker:
    def __init__(self, calib_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.calib_dir, self.clock = calib_dir, clock
        self.app: WebSocket | None = None  # the connected phone app (last connection wins)
        self.hello: PoseHello | None = None
        self.recorder = Recorder(calib_dir.parent / "diag" / "poses.jsonl")  # raw session log for replay.py
        self.noises: dict[str, NoiseEstimator] = {}  # per face source: front ARKit vs rear Vision differ >10x
        self.face_src: str | None = None
        self._had_face, self._face_change = False, 0.0  # face lost/regained event log (see _log_face)
        self.head = HeadKalman()
        self.cam: Cam | None = None
        self.last_msg: PoseMsg | None = None
        self.last_recv, self.last_face, self.face_conf = 0.0, -math.inf, None
        self.raw_R: np.ndarray | None = None  # last measured head rotation BEFORE the IMU blend (calibrates the IMU)
        self.imu_w = 0.0  # weight the IMU tilt got in the last pose (0 = camera only)
        self.pose_age: deque[float] = deque(maxlen=50)  # s, server receive time - phone capture time
        self.pose_times: deque[float] = deque(maxlen=30)
        self.variant, self.variant_err = "as sent", dict.fromkeys(VARIANTS, 0.0)  # EMA disagreement, degrees
        self.targets = Targets(self.to_app, lambda: self.world)
        self.mirror_sources = set(MIRROR_SOURCES)
        self.imu_calib: dict[str, ImuCalib] = {}
        self.displays: dict[str, Display] = {}
        self.renderers: dict[str, Renderer] = {}
        self.imu: dict[str, deque] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ per-session objects

    @property
    def noise(self) -> NoiseEstimator:
        """Noise estimator of the face source currently in use."""
        return self.noises.setdefault(self.face_src or "default", NoiseEstimator())

    def display(self, session: str) -> Display:
        if session not in self.displays:
            self.displays[session] = Display(session, self.calib_dir)
        return self.displays[session]

    def renderer(self, session: str) -> Renderer:
        if session not in self.renderers:
            self.renderers[session] = Renderer(session, self)
        return self.renderers[session]

    def set_mirror(self, source: str, on: bool) -> list[str]:
        (self.mirror_sources.add if on else self.mirror_sources.discard)(source)
        self.imu_calib.clear()  # its pairs were made with the old convention
        return sorted(self.mirror_sources)

    def reset_boresight(self, session: str) -> list[str]:
        """Forget what was learned for this glasses + pose source (all sources if no app is connected)."""
        d = self.display(session)
        sources = [self.source] if self.hello else list(d.learners)
        for s in sources:
            d.learner(s).reset()
        d.save()
        return sources

    # ------------------------------------------------------------------ pose

    @property
    def source(self) -> str:
        return self.hello.source if self.hello else "unknown"

    @property
    def mirror(self) -> np.ndarray:
        """ARKit's front-camera world is MIRRORED: self-consistent (cam_in_head agrees with the app's R/t) but its
        face +x is physically the wearer's RIGHT, while our head frame (+x = wearer's left) must be physical for the
        display. So mirror x once, for the sources in `mirror_sources` (default: "arkit" only); the Vision path works in
        the true image frame; "arkit-world" is unverified, hence configurable at runtime."""
        return MX if self.source in self.mirror_sources else np.eye(3)

    @property
    def world(self) -> bool:
        return self.source == WORLD

    @property
    def head_z(self) -> float | None:
        return float(self.head.p[2]) if self.head.R is not None else None

    def tracking(self, now: float) -> bool:
        """A trusted face pose arrived recently."""
        return self.head.R is not None and now - self.last_face <= FRESH_S

    def fix_conventions(self, R: np.ndarray, t: np.ndarray, cam_in_head) -> tuple[np.ndarray, np.ndarray]:
        """Pick the mirror fix of (R, t) whose phone direction matches ARKit's own cam_in_head (running average), so
        a sign slip in the app's ARKit->image conversion can't misplace image-grounded targets. Vision path: no
        truth, keeps the last choice."""
        if cam_in_head is not None:
            c = np.array(cam_in_head) / max(float(np.linalg.norm(cam_in_head)), 1e-9)
            for k, f in VARIANTS.items():
                d = to_head(*f(R, t), ZERO)
                ang = math.degrees(math.acos(max(-1.0, min(1.0, float(d @ c) / max(float(np.linalg.norm(d)), 1e-9)))))
                self.variant_err[k] += 0.1 * (ang - self.variant_err[k])
            self.variant = min(self.variant_err, key=self.variant_err.get)
        return VARIANTS[self.variant](R, t)

    def on_pose(self, m: PoseMsg, now: float) -> None:
        """One pose message -> noise estimate, head filter, tracks. Drawing is the renderers' job."""
        self.recorder.write("pose", now, m.model_dump())
        self.pose_times.append(now)
        if (age := now - m.t) >= 0:  # phone vs server clock: ignore negatives (skew)
            self.pose_age.append(min(age, 0.5))
        self.cam = Cam(K_of(*m.K), m.w, m.h)
        for tr in m.tracks:
            self.targets.update_track(tr, now)
        self.last_msg, self.last_recv = m, now
        self.face_conf = m.face.conf if m.face else None
        if self.world:  # world frame: z has no "in front of the camera" meaning
            self.targets.set_world(m.anchors, m.errors, now)
        has_face = bool(m.face and m.face.conf >= MIN_FACE_CONF and (self.world or m.face.t[2] > MIN_FACE_Z))
        self._log_face(has_face, m, now)
        if has_face:
            R, t = self.fix_conventions(np.array(m.face.R).reshape(3, 3), np.array(m.face.t), m.cam_in_head)
            self.face_src, self.raw_R = m.face.src, R
            r_rot, r_pos = self.noise.update(R, t, m.t)
            R, r_rot = self._imu_blend(R, r_rot, now)
            self.head.update(R, t, m.t, r_rot, r_pos)
            self.last_face = now
            for c in self.imu_calib.values():
                c.snap()
        for sid in list(self.targets.steps):
            self.targets.observe(sid, now, self.tracking(now))

    def _imu_blend(self, R: np.ndarray, r_rot: np.ndarray, now: float) -> tuple[np.ndarray, np.ndarray]:
        """Pull the measured head tilt toward the IMU's gravity, weighted by the noise of each (inverse variance);
        yaw is untouched (minimal world rotation, as in memory()). Body x (pitch) and z (roll) carry the tilt, so their
        noise drops to the blend's. No live calibrated IMU (or not world mode): unchanged."""
        self.imu_w = 0.0
        c = self._best_calib(None, now)
        if c is None or (g := c.head_gravity(now)) is None:
            return R, r_rot
        r_cam, r_imu = float(r_rot[[0, 2]].mean()), IMU_TILT_SIG_DEG**2
        self.imu_w = w = r_cam / (r_cam + r_imu)
        R = rodrigues(w * rodrigues_inv(rot_between(R @ self.mirror @ g, DOWN))) @ R
        r_rot = r_rot.copy()
        r_rot[[0, 2]] = r_cam * r_imu / (r_cam + r_imu)
        return R, r_rot

    def _log_face(self, has_face: bool, m: PoseMsg, now: float) -> None:
        """Append face lost/regained events (with the app's ARKit diagnostics) to <data>/diag/face.jsonl, so a session
        in a bad environment shows WHY the face drops (tracking limited? rear Vision miss? no depth? distance?)."""
        if has_face == self._had_face:
            return
        self._had_face = has_face
        ev = {
            "t": round(now, 3),
            "event": "regained" if has_face else "lost",
            "after_s": round(now - self._face_change, 2) if self._face_change else None,
            "source": self.source,
            "face_src": m.face.src if m.face else None,
            "face_conf": m.face.conf if m.face else None,
            "head_dist_m": round(float(np.linalg.norm(np.array(m.face.t))), 2) if m.face and self.world else None,
            "ar": m.ar,
        }
        self._face_change = now
        try:
            path = self.calib_dir.parent / "diag" / "face.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            log.warning("could not write face diagnostics", exc_info=True)

    # ------------------------------------------------------------------ loops

    async def to_app(self, msg: dict) -> None:
        if self.app is not None:
            try:
                await self.app.send_text(json.dumps(msg))
            except Exception:
                log.warning("send to pose app failed", exc_info=True)

    def page_redrawn(self, session: str) -> None:
        self.renderer(session).reset()

    async def run(self) -> None:
        """Background task for the server's lifetime."""
        try:
            await asyncio.gather(
                *(
                    _forever(n, f)
                    for n, f in [
                        ("steps", self._steps_loop),
                        ("ground", self.targets.ground_loop),
                        ("imu", self._imu_loop),
                        ("drawn", self._drawn_loop),
                    ]
                )
            )
        finally:
            for t in self._tasks.values():
                t.cancel()

    async def _set_imu(self, session: str, pace: int) -> None:
        if conn := glasses.sessions.get(session):
            try:
                await conn.control(imu_pace=pace)
            except Exception:
                log.exception("imu control failed")

    async def _steps_loop(self) -> None:
        async for ev in hub.subscribe(StepChanged):
            s = ev.session
            await self.targets.on_step(ev)
            r = self.renderer(s)
            if ev.targets:
                await self._set_imu(s, 100)
                if (t := self._tasks.get(s)) is None or t.done():
                    self._tasks[s] = asyncio.create_task(_forever(f"render {s}", r.run))
            else:
                if t := self._tasks.pop(s, None):  # only undo what we started: Part 2 publishes target-less steps too
                    t.cancel()
                    await self._set_imu(s, 0)
                r.clear(self.clock())

    async def _drawn_loop(self) -> None:
        async for ev in hub.subscribe(ImageDrawn, maxsize=64):
            now = self.clock()
            self.recorder.write("ack", now, {"session": ev.session, "t": ev.t, "at": ev.at, "ms": ev.ms})
            self.renderer(ev.session).on_ack(ev, now)

    async def _imu_loop(self) -> None:
        async for ev in hub.subscribe(ImuSample, maxsize=64):
            self.on_imu(ev.session, ev.x, ev.y, ev.z)

    def on_imu(self, session: str, x: float, y: float, z: float) -> None:
        now = self.clock()
        self.recorder.write("imu", now, {"session": session, "x": x, "y": y, "z": z})
        self.imu.setdefault(session, deque(maxlen=500)).append((now, x, y, z))
        g = None
        if self.world and self.tracking(now) and self.head.speed_deg_s <= IMU_MAX_HEAD_DEG_S:
            g = self.gravity_in_head(self.raw_R)  # raw camera pose: the blended one would teach the IMU its own errors
        self.imu_calib.setdefault(session, ImuCalib()).add(x, y, z, now, g)

    # ------------------------------------------------------------------ face memory

    def gravity_in_head(self, R: np.ndarray | None = None) -> np.ndarray:
        """Unit gravity direction in the (display-convention) head frame, for a head->world rotation R (default: the
        filtered head). Needs a gravity-aligned world (arkit-world)."""
        return self.mirror @ (self.head.R if R is None else R).T @ DOWN

    def _best_calib(self, session: str | None, now: float) -> ImuCalib | None:
        cs = [self.imu_calib[session]] if session in self.imu_calib else list(self.imu_calib.values())
        return next((c for c in cs if c.live(now)), None) if self.world else None

    def memory_limit(self, session: str | None, now: float) -> float:
        return MEMORY_S if self._best_calib(session, now) else MEMORY_HOLD_S

    def memory(self, session: str, now: float) -> tuple[HeadPose, str] | None:
        """The head pose to draw from while the face is lost, or None (never had one, or it has been too long).
        With a live calibrated IMU (world mode): the last good pose with its pitch/roll rotated by what the IMU says
        the head did since (yaw and position kept). Otherwise the last good pose, held, for a shorter time."""
        age = now - self.last_face
        if self.head.R is None or age <= FRESH_S or age > self.memory_limit(session, now):
            return None
        R, mode = self.head.R, "hold"
        if (c := self._best_calib(session, now)) and (g_new := c.tilt_gravity(self.gravity_in_head())) is not None:
            # R_adj^T (0,-1,0) = mirror @ g_new, via the minimal world rotation that fixes gravity (yaw kept)
            R = rot_between(R @ self.mirror @ g_new, DOWN) @ R
            mode = "imu"
        sig = math.radians(1.0 + MEM_SIG_DEG_PER_S * age)
        return HeadPose(R, self.head.p.copy(), np.full(3, sig), np.full(3, 0.02)), mode

    # ------------------------------------------------------------------ debug

    def pose_hz(self, now: float) -> float:
        if len(self.pose_times) < 2 or now - self.pose_times[-1] > 2:
            return 0.0
        return round((len(self.pose_times) - 1) / (self.pose_times[-1] - self.pose_times[0]), 1)

    def _phone_dir(self, m: PoseMsg | None) -> dict | None:
        """Where the phone (camera origin) is from the wearer's point of view: a sign check.
        Turn your head right -> the phone should read as LEFT."""
        if not (m and m.face):
            return None
        if self.world:  # the phone is the app's "camera" anchor, in the world frame
            if not (cam := self.targets.world_anchors.get("camera")):
                return None
            d = self.mirror @ to_head(np.array(m.face.R).reshape(3, 3), np.array(m.face.t), cam.p)
        elif m.cam_in_head:
            d = np.array(m.cam_in_head)
            d = self.mirror @ d
        else:  # head frame: +x wearer's left, +y up, +z ahead
            R, t = VARIANTS[self.variant](np.array(m.face.R).reshape(3, 3), np.array(m.face.t))
            d = self.mirror @ to_head(R, t, ZERO)
        az, el = math.degrees(math.atan2(d[0], d[2])), math.degrees(math.atan2(d[1], math.hypot(d[0], d[2])))
        return {
            "horizontal": f"{abs(az):.0f}° {'LEFT' if az > 0 else 'RIGHT'}",
            "vertical": f"{abs(el):.0f}° {'UP' if el > 0 else 'DOWN'}",
            "ahead": bool(d[2] > 0),
            "az_el_deg": [round(az, 3), round(el, 3)],  # raw (unfiltered) numbers, for measuring jitter
        }

    def memory_status(self, now: float) -> dict:
        age = now - self.last_face
        lost = self.head.R is not None and age > FRESH_S
        lim = max((self.memory_limit(s, now) for s in self.renderers or [None]), default=MEMORY_HOLD_S)
        active = lost and age <= lim
        return {
            "active": active,
            "age_s": round(age, 2) if lost and age < 3600 else None,
            "limit_s": lim,
            "mode": ("imu" if lim == MEMORY_S else "hold") if active else None,
        }

    def imu_calib_status(self) -> dict:
        per = {s: c.status() for s, c in self.imu_calib.items()}
        top = next((v for v in per.values() if v["ok"]), next(iter(per.values()), None)) or {
            "ok": False, "n": 0, "residual_deg": None, "spread_deg": 0.0, "sign": None}  # fmt: skip
        return {**top, "sessions": per}

    def status(self) -> dict:
        now = self.clock()
        imu = {}
        for s, d in self.imu.items():
            a = np.array(d)
            imu[s] = {
                "n": len(a),
                "hz": round((len(a) - 1) / (a[-1, 0] - a[0, 0]), 1) if len(a) > 1 else 0,
                "last": a[-1, 1:].tolist(),
                "min": a[:, 1:].min(0).tolist(),
                "max": a[:, 1:].max(0).tolist(),
            }
        m = self.last_msg
        rs = {s: r.status(now) for s, r in self.renderers.items()}
        rtts = [x for r in self.renderers.values() for x in r.rtt]
        return {
            "pose": {
                "app_connected": self.app is not None,
                "source": self.hello.source if self.hello else None,
                "device": self.hello.device if self.hello else None,
                "hz": self.pose_hz(now),
                "age_s": round(now - self.last_recv, 1) if m else None,
                "size": [m.w, m.h] if m else None,
                "K": m.K if m else None,
                "face_conf": self.face_conf,
                "head_ok": self.tracking(now),
                "head_z": self.head_z,
                "phone_from_head": self._phone_dir(m),
                "filter": {
                    "moving": not self.head.still,
                    "speed_deg_s": round(self.head.speed_deg_s, 1),
                    "face_src": self.face_src,
                    "noise_by_src_deg": {k: round(n.rot_deg, 2) for k, n in self.noises.items()},
                    "noise_deg": round(self.noise.rot_deg, 2),
                    "noise_cm": round(self.noise.pos_cm, 2),
                    "imu_tilt_weight": round(self.imu_w, 2),
                },
                "arkit_truth": bool(m and m.cam_in_head),
                "mirror": {"using": self.source in self.mirror_sources, "sources": sorted(self.mirror_sources)},
                "conventions": {
                    "using": self.variant,
                    "disagreement_deg": {k: round(v, 1) for k, v in self.variant_err.items()},
                },
            },
            "omega_deg_s": round(self.head.speed_deg_s, 1) if self.head.R is not None else None,
            "render": {
                "pose_age_ms_p50": round(statistics.median(self.pose_age) * 1000) if self.pose_age else None,
                "draw_rtt_ms_p50": round(statistics.median(rtts) * 1000) if rtts else None,
                "sessions": rs,
                "inflight": sorted(s for s, r in self.renderers.items() if r.inflight is not None),
            },
            "face_memory": self.memory_status(now),
            "imu_calib": self.imu_calib_status(),
            "anchors_world": {
                k: {
                    "p": [round(float(v), 3) for v in a.p],
                    "how": a.how,
                    "dist_from_head": round(float(np.linalg.norm(a.p - self.head.p)), 3)
                    if self.head.R is not None
                    else None,
                }
                for k, a in self.targets.world_anchors.items()
            },
            "locate": {"pending": sorted(self.targets.pending), "errors": dict(self.targets.errors)},
            "boresight": {
                s: {
                    "source": src,
                    "az_deg": round(le.az, 2),
                    "el_deg": round(le.el, 2),
                    "confidence": round(le.confidence, 2),
                    "dwell_s": round(le.w, 1),
                    "observations": le.n_obs,
                    "observing": le.still_since is not None,
                    "last_update_age_s": None if le.last_update is None else round(now - le.last_update, 1),
                    "learns_from": PHONE,
                }
                for s in glasses.sessions
                for src, le in [(self.source, self.display(s).learner(self.source))]
            },
            "display": {s: self.display(s).snapshot().model_dump() for s in glasses.sessions},
            "sessions": {
                s: {"step": e.step_id, "targets": [t.object_id for t in e.targets]}
                for s, e in self.targets.steps.items()
            },
            "anchors": {
                f"{s}/{o}": {"age_s": round(now - a.t, 1), "box": a.box} for (s, o), a in self.targets.anchors.items()
            },
            "tracks": {
                k: {"conf": t.conf, "z": t.z, "age_s": round(now - t.t, 1), "box": t.box}
                for k, t in self.targets.tracks.items()
            },
            "imu": imu,
        }

    def glasses_png(self, session: str) -> bytes:
        """What the G2 is showing right now (last published overlay), green on black, visible band outlined."""
        prims = self.renderer(session).shown
        img = core_display.draw(OverlayFrame(session=session, t=0, primitives=prims))
        rgb = Image.merge("RGB", (Image.new("L", img.size), img, Image.new("L", img.size)))
        d = ImageDraw.Draw(rgb)
        x0, y0, x1, y1 = BAND
        d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(0, 70, 0))
        d.text((4, 4), "glasses display (live mirror) · only the outlined band can show overlays", fill=(0, 140, 0))
        buf = io.BytesIO()
        rgb.save(buf, "PNG")
        return buf.getvalue()
