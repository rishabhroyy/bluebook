"""What to highlight: step targets -> points in the scene-camera frame.

Targets are fixed points in a static frame. Two modes, by the app's pose source:
- "arkit" / "vision": the scene camera's frame. The phone itself is the origin; anything else is grounded by Gemini in
  a camera frame (slow, re-run every REGROUND_S), then followed by the phone's own box tracker.
- "arkit-world" (rear) / "arkit-world-front": the ARKit world frame. The phone is the app's anchor "camera"; anything
  else is an app anchor with the target's object_id (placed by tapping, or by `locate`: Jev boxes the object in the
  newest JPEG, constantly; once boxes agree the app samples depth in that box and returns the world point).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np
from bluebook_core import hub
from bluebook_core.protocol import CameraFrame, SceneObservation, StepChanged

from . import vision
from .filters import StaticPoint
from .geom import backproject
from .wire import CAM, TrackIn

log = logging.getLogger("bluebook.interactive")

PHONE = "camera"  # object_id of the built-in test target: the scene camera lens, X_cam = 0
REGROUND_S = 10.0  # re-run the VLM on a target at most this often...
RETRY_S = 3.0  # ...and no more often than this when the track is lost or grounding failed
TRACK_LOW_CONF = 0.3  # phone tracker confidence below this triggers a re-ground
TRACK_STALE_S = 1.0  # ignore a tracked box older than this and fall back to the grounded box
OBSERVE_S = 1.0  # SceneObservation cadence
# Depth prior for a target the phone could not measure: as far from the camera as the wearer's head (the wearer
# is within arm's reach of the thing they are working on). Wrong for far fixtures; the phone sends `z` when it can.
FALLBACK_Z = 1.0  # metres, only when there is no track z and no head pose yet
# arkit-world locate flow. Jev looks constantly (~100 ms a call, one in flight per target, only on new frames), so an
# object that appears is found within a few frames. One Box misses ~30% of the time (2026-10-18, skill keyframes), so a
# box is acted on only when 2 of the last 3 agree. A placed anchor is trusted and not re-sent for the same box
# (re-locating a correctly placed object made it visibly jump); it is re-located when the app reports an error for it
# or when the agreed box has moved away from where we last located it.
LOOK_S = 0.15  # no good anchor yet (missing / app error): ground the newest frame this often
WATCH_S = 1.0  # good anchor: keep checking that it is still where we placed it
VOTES, VOTE_WINDOW_S = 3, 2.0  # last boxes per target that may agree, and how old they may be
AGREE = 0.05  # box centres this close (normalised image units) are the same place
MOVED = 0.08  # agreed box this far from the last located one: the object moved, locate again
LOCATE_WAIT_S = 4.0  # an unanswered locate is dropped after this


@dataclass
class Cam:
    """The scene camera as last reported by a pose message."""

    K: np.ndarray
    w: int
    h: int


@dataclass
class Anchor:
    box: tuple[float, float, float, float]  # grounded box, normalised x0,y0,x1,y1
    t: float  # when grounded
    world: bool = False  # located through the app (arkit-world), not a phone box tracker


@dataclass
class Track:
    box: tuple[float, float, float, float]  # normalised
    conf: float
    z: float | None
    t: float  # receive time


@dataclass
class WorldAnchor:
    p: np.ndarray  # metres, ARKit world frame
    how: str


def _centre(b) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def consensus(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    """The box at least two of `boxes` agree on (centres within AGREE), as their per-edge median; None if none agree."""
    best: list = []
    for b in boxes:
        near = [o for o in boxes if math.dist(_centre(o), _centre(b)) <= AGREE]
        if len(near) > len(best):
            best = near
    if len(best) < 2:
        return None
    a = np.median(np.array(best, float), axis=0)
    return (float(a[0]), float(a[1]), float(a[2]), float(a[3]))


def tid(session: str, object_id: str) -> str:
    return f"{session}/{object_id}"


class Targets:
    def __init__(self, send: Callable[[dict], Awaitable[None]], world: Callable[[], bool] = lambda: False) -> None:
        self.send = send  # to the phone app
        self.world = world  # is the app in arkit-world mode?
        self.world_anchors: dict[str, WorldAnchor] = {}  # the app's anchors by id, as of its last pose message
        self.stable: dict[str, StaticPoint] = {}  # held (de-jittered) position of each anchor: what gets drawn
        # the app's per-anchor errors. The app sends each one ONCE, so they stick until that anchor is located again
        # (the 1 Hz ground loop missed a one-message error almost always: re-locates never fired).
        self.errors: dict[str, str] = {}
        self.located: dict[str, float] = {}  # ids WE asked the app to locate -> when
        self.pending: dict[str, tuple[float, np.ndarray | None]] = {}  # locate sent: (when, anchor point before it)
        self.app_owned: set[str] = set()  # target ids the app placed itself (tap): never grounded, never forgotten
        self.steps: dict[str, StepChanged] = {}
        self.anchors: dict[tuple[str, str], Anchor] = {}
        self.tracks: dict[str, Track] = {}
        self._tried: dict[tuple[str, str], float] = {}
        self._obs_t: dict[str, float] = {}
        self._tried_w: dict[str, float] = {}
        self._frame_w: dict[str, float] = {}  # newest frame time grounded per target: never ground a frame twice
        self.votes: dict[str, deque] = {}  # recent (time, box) per target, for consensus()
        self.located_box: dict[str, tuple[float, float, float, float]] = {}  # the box we last asked the app to place
        self.ground_now: asyncio.Event | None = None  # made in the running loop
        self.rec: Callable[[str, dict], None] = lambda kind, d: None  # session log (Tracker sets it)
        self._inflight: dict[str, asyncio.Task] = {}  # one grounding call per target at a time, run in the background

    def active(self, session: str) -> list[str]:
        return [t.object_id for t in (self.steps[session].targets if session in self.steps else [])]

    def update_track(self, tr: TrackIn, now: float) -> None:
        self.tracks[tr.id] = Track((tr.box[0], tr.box[1], tr.box[2], tr.box[3]), tr.conf, tr.z, now)

    def _box_of(self, sid: str, oid: str, now: float):
        """Current normalised box + track z: a fresh phone track, else the grounded box."""
        if (tr := self.tracks.get(tid(sid, oid))) and now - tr.t <= TRACK_STALE_S:
            return tr.box, tr.z
        return (a.box, None) if (a := self.anchors.get((sid, oid))) else None

    def set_world(self, anchors, errors: dict[str, str] | None, now: float = 0.0) -> None:
        """The app's anchors/errors from a pose message. A pending locate completes when its anchor shows up (new, or
        moved from where it was). Every anchor also feeds a StaticPoint: targets are drawn at the held position."""
        if anchors is not None:
            self.world_anchors = {a.id: WorldAnchor(np.array(a.p, float), a.how) for a in anchors}
            for oid, w in self.world_anchors.items():
                sp = self.stable.setdefault(oid, StaticPoint())
                moving = w.how == "tracked"  # the app couples this anchor to a (possibly moving) object
                sp.window_s, sp.hold = (StaticPoint.TRACKED_WINDOW_S, False) if moving else (StaticPoint.WINDOW_S, True)
                sp.update(w.p, now)
            for oid in set(self.stable) - set(self.world_anchors):
                del self.stable[oid]
            for oid, (_, old) in list(self.pending.items()):
                if (w := self.world_anchors.get(oid)) and (old is None or not np.allclose(w.p, old)):
                    del self.pending[oid]
                    self.errors.pop(oid, None)
        self.errors.update(errors or {})
        for oid in errors or {}:  # answered (it could not place it): free the target for the next agreed box now
            self.pending.pop(oid, None)

    def resolve(self, session: str, now: float, cam: Cam | None, head_z: float | None) -> list[tuple[str, np.ndarray]]:
        """(object_id, point in the static frame) for every target of the session that is currently locatable.
        World mode: the app's anchor of that id. Otherwise the camera frame; depth: the track's own z, else the
        (filtered) head distance, else FALLBACK_Z."""
        out = []
        if self.world():
            return [(oid, sp.held) for oid in self.active(session) if (sp := self.stable.get(oid)) is not None]
        for oid in self.active(session):
            if oid == PHONE:
                out.append((oid, np.zeros(3)))
            elif cam is not None and (b := self._box_of(session, oid, now)):
                (x0, y0, x1, y1), z = b
                out.append(
                    (oid, backproject(cam.K, (x0 + x1) / 2 * cam.w, (y0 + y1) / 2 * cam.h, z or head_z or FALLBACK_Z))
                )
        return out

    def observe(self, session: str, now: float, head_ok: bool) -> None:
        """Tell Part 2 (at 1 Hz) where the targets are and whether the wearer's head is tracked."""
        if now - self._obs_t.get(session, 0) < OBSERVE_S:
            return
        self._obs_t[session] = now
        boxes = {oid: b[0] for oid in self.active(session) if oid != PHONE and (b := self._box_of(session, oid, now))}
        hub.publish(SceneObservation(session=session, camera_id=CAM, t=now, objects=boxes, head_visible=head_ok))

    # ------------------------------------------------------------------ phone tracker control

    async def track(self, sid: str, oid: str, box: tuple[float, float, float, float]) -> None:
        """Start/replace the phone's tracker on this target (box normalised 0..1)."""
        self.tracks.pop(tid(sid, oid), None)
        await self.send({"type": "track", "id": tid(sid, oid), "box": list(box)})

    async def resend(self) -> None:
        for (sid, oid), a in list(self.anchors.items()):
            if not a.world:
                await self.track(sid, oid, a.box)

    async def on_step(self, ev: StepChanged) -> None:
        """Drop the old step's camera-frame trackers/anchors and adopt the new one. World anchors are kept: objects stay
        where they are between steps (s1 and s4 share the camera), and the constant watch re-locates any that move."""
        s = ev.session
        old = self.steps.get(s)
        ids = {o for (ss, o) in self.anchors if ss == s} | {t.object_id for t in (old.targets if old else [])}
        for oid in ids - {PHONE}:
            self.tracks.pop(tid(s, oid), None)
            await self.send({"type": "untrack", "id": tid(s, oid)})
        self.app_owned &= {t.object_id for t in ev.targets}
        self._tried_w.clear()
        self.steps[s] = ev
        for d in (self.anchors, self._tried):
            for k in [k for k in d if k[0] == s]:
                del d[k]
        if ev.targets and self.ground_now:
            self.ground_now.set()

    # ------------------------------------------------------------------ grounding

    def _due(self, sid: str, oid: str, now: float) -> bool:
        if now - self._tried.get((sid, oid), 0) < RETRY_S:
            return False
        a, tr = self.anchors.get((sid, oid)), self.tracks.get(tid(sid, oid))
        low = bool(tr and now - tr.t <= TRACK_STALE_S and tr.conf < TRACK_LOW_CONF)
        return a is None or now - a.t >= REGROUND_S or low

    def _due_world(self, oid: str, now: float) -> bool:
        if oid == PHONE or oid in self.app_owned:
            return False
        if (p := self.pending.get(oid)) and now - p[0] < LOCATE_WAIT_S:
            return False
        good = oid in self.world_anchors and oid not in self.errors
        return now - self._tried_w.get(oid, 0) >= (WATCH_S if good else LOOK_S)

    async def _ground(self, f: CameraFrame, sid: str, st: StepChanged, tg) -> tuple[float, float, float, float] | None:
        t0 = time.time()
        d = {"session": sid, "step_id": st.step_id, "object_id": tg.object_id, "name": tg.name, "frame_t": f.t}
        try:
            box = await vision.ground(f.jpeg, tg)
        except Exception as e:
            self.rec("ground", d | {"error": repr(e)[:200], "s": round(time.time() - t0, 2)})
            raise
        self.rec("ground", d | {"box": box, "s": round(time.time() - t0, 2)})
        return box

    def _spawn(self, key: str, coro) -> None:
        """Run a grounding call in the background, so a new step never waits behind an older step's call (measured:
        median 3 s, up to 50 s of queueing when they ran one after another)."""
        if (t := self._inflight.get(key)) and not t.done():
            coro.close()
            return
        self._inflight[key] = asyncio.create_task(coro)

    async def _ground_world(self, f: CameraFrame, now: float) -> None:
        for sid, st in list(self.steps.items()):
            for tg in [t for t in st.targets if self._due_world(t.object_id, now)]:
                if self._frame_w.get(tg.object_id) == f.t:  # nothing new to look at
                    continue
                self._frame_w[tg.object_id], self._tried_w[tg.object_id] = f.t, now
                self._spawn(tg.object_id, self._locate_world(f, sid, st, tg))

    async def _locate_world(self, f: CameraFrame, sid: str, st: StepChanged, tg) -> None:
        oid = tg.object_id
        try:
            box = await self._ground(f, sid, st, tg)
        except Exception:
            log.exception("grounding %s failed", oid)
            return
        if box is None or self.steps.get(sid) is not st:
            return
        now = time.time()
        votes = self.votes.setdefault(oid, deque(maxlen=VOTES))
        votes.append((now, box))
        if (agreed := consensus([b for t, b in votes if now - t <= VOTE_WINDOW_S])) is None:
            return
        last = self.located_box.get(oid)
        if (
            oid in self.world_anchors
            and oid not in self.errors
            and last
            and math.dist(_centre(agreed), _centre(last)) <= MOVED
        ):
            return  # still where we placed it
        if (p := self.pending.get(oid)) and now - p[0] < LOCATE_WAIT_S:
            return
        old = self.world_anchors.get(oid)
        self.anchors[(sid, oid)] = Anchor(agreed, now, world=True)
        self.located[oid], self.located_box[oid] = now, agreed
        self.pending[oid] = (now, old.p.copy() if old else None)
        await self.send({"type": "locate", "id": oid, "box": list(agreed)})
        self.rec("locate", {"id": oid, "box": list(agreed)})

    async def _ground_cam(self, f: CameraFrame, sid: str, st: StepChanged, tg) -> None:
        try:
            box = await self._ground(f, sid, st, tg)
        except Exception:
            log.exception("grounding %s failed", tg.object_id)
            return
        if box is None or self.steps.get(sid) is not st:  # not found, or the step moved on meanwhile
            return
        self.anchors[(sid, tg.object_id)] = Anchor(box, time.time())
        await self.track(sid, tg.object_id, box)

    async def ground_loop(self) -> None:
        self.ground_now = asyncio.Event()
        while True:
            try:
                await asyncio.wait_for(self.ground_now.wait(), LOOK_S)
            except TimeoutError:
                pass
            self.ground_now.clear()
            now = time.time()
            if not (f := hub.last(CameraFrame, CAM)):
                continue
            if self.world():
                await self._ground_world(f, now)
                continue
            for sid, st in list(self.steps.items()):
                for tg in [t for t in st.targets if t.object_id != PHONE and self._due(sid, t.object_id, now)]:
                    self._tried[(sid, tg.object_id)] = now
                    self._spawn(tid(sid, tg.object_id), self._ground_cam(f, sid, st, tg))
