"""Part 3 (experimental) — interactive AR highlighting. Spec: workstreams/part3-interactive.md
Mounted only when BB_INTERACTIVE=1."""

import logging
import re
from pathlib import Path

from bluebook_core import glasses, hub, store
from bluebook_core.config import config
from bluebook_core.protocol import CameraFrame, StepChanged, StepTarget
from fastapi import FastAPI, HTTPException, Response, WebSocket
from pydantic import ValidationError

from .controllers import ViewController
from .targets import PHONE
from .tracker import Tracker
from .wire import pose_session

tracker: Tracker | None = None  # the one Tracker of this process, made by setup()


def setup(app: FastAPI, calib_dir: Path | None = None):
    """Add the pose WebSocket + the JSON/PNG API the phone app uses as its dev console; return the async tracking loop (core runs it as a background task)."""
    global tracker
    tk = tracker = Tracker(calib_dir or config.data_dir / "calib")
    # Warnings + tracebacks from the whole server, so a crash can be read after the fact.
    config.data_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.data_dir / "server.log")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    def conn_of(session: str) -> glasses.GlassesConnection:
        if session not in glasses.sessions:
            raise HTTPException(404, f"glasses {session!r} not connected")
        return glasses.sessions[session]

    @app.websocket("/interactive/ws/pose")
    async def pose_ws(ws: WebSocket):
        await pose_session(ws, tk)

    @app.get("/interactive/display/{session}")
    def display_get(session: str):
        return tk.display(session).snapshot()

    @app.post("/interactive/display/{session}")
    def display_set(
        session: str,
        fov_deg: float | None = None,
        dx: float | None = None,
        dy: float | None = None,
        dz: float | None = None,
        cx: float | None = None,
        cy: float | None = None,
    ):
        """Tune the display model live (dx/dy/dz in metres, head frame); saved to data/calib/display_<session>.json."""
        try:
            return tk.display(session).set(fov_deg=fov_deg, dx=dx, dy=dy, dz=dz, cx=cx, cy=cy)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False, include_input=False))

    @app.post("/interactive/step/{session}")
    async def step(
        session: str,
        skill_id: str = "adhoc",
        step_id: str | None = None,
        name: str | None = None,
        anchor: str | None = None,
    ):
        """Fake Part-2 publisher: StepChanged with the step's target objects; no step_id clears.
        `name` instead highlights any object by name (e.g. "hydro flask") for testing without a skill.
        `anchor` (arkit-world): target the app's own anchor with that id (tap-to-place), labelled `name`."""
        targets = []
        if anchor:
            step_id = "adhoc"
            targets = [StepTarget(object_id=anchor, name=name or anchor)]
            tk.targets.app_owned.add(anchor)  # the app placed it: never ground or forget it
        elif name == PHONE:
            step_id, targets = "adhoc", [StepTarget(object_id=PHONE, name="phone")]
        elif name:
            step_id = "adhoc"
            targets = [StepTarget(object_id=re.sub(r"\W+", "_", name.lower()).strip("_") or "obj", name=name)]
        elif step_id:
            try:
                pkg = store.load(skill_id)
                targets = [
                    StepTarget(
                        object_id=o.object_id,
                        name=ob.name,
                        aliases=ob.aliases,
                        visual_description=ob.visual_description,
                        action=o.action,
                    )
                    for o in pkg.step(step_id).objects
                    if o.role == "target"
                    for ob in [pkg.object(o.object_id)]
                ]
            except (FileNotFoundError, ValueError, KeyError):
                raise HTTPException(404, f"no skill {skill_id!r} / step {step_id!r}")
        hub.publish(StepChanged(session=session, skill_id=skill_id, step_id=step_id, targets=targets))
        if conn := glasses.sessions.get(session):  # show a teach_page so the overlay has image boxes
            while isinstance(conn.controller, ViewController):
                await conn.pop()
            if targets:
                await conn.push(ViewController([t.name for t in targets], tk.page_redrawn))
        return {"targets": [t.object_id for t in targets]}

    @app.post("/interactive/align/{session}")
    def align(session: str):
        """Forget the learned boresight for this glasses + pose source (it is re-learned from use)."""
        return {"reset": tk.reset_boresight(session), "session": session}

    @app.post("/interactive/mirror")
    def mirror(source: str, on: bool):
        """Does the head x axis of this pose source need mirroring? (for verifying arkit-world on hardware)"""
        return {"mirror_sources": tk.set_mirror(source, on)}

    @app.post("/interactive/imu/{session}")
    async def imu(session: str, pace: int = 100):
        try:
            await conn_of(session).control(imu_pace=pace)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"imu_pace": pace}

    @app.get("/interactive/status")
    def status():
        return tk.status()

    @app.get("/interactive/glasses.png")
    def glasses_png(session: str):
        return Response(tk.glasses_png(session), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/interactive/eye.jpg")
    def eye_jpg():
        """Through-the-lens camera (camera id "eye"): what the wearer actually sees, for dev/polish."""
        if not (f := hub.last(CameraFrame, "eye")):
            raise HTTPException(404, "no eye camera")
        return Response(f.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    return tk.run
