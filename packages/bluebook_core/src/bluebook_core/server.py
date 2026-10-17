"""The single dev server. Run:  uv run bluebook-server   (then `tailscale serve --bg 8787`)

Core owns the transport (websockets, REST for the library, static pages) and mounts the parts:
each part package exposes `setup(app: FastAPI)`, called at import if the package is installed.
setup() may return an async function; it is run as a background task for the server's lifetime.
Part 3 is only mounted when BB_INTERACTIVE=1.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import ValidationError

from . import hub, store
from .config import config
from .glasses import GlassesConnection, sessions
from .protocol import (
    AudioEnd,
    AudioStart,
    CameraFrame,
    CameraHello,
    Hello,
    Imu,
    ImuSample,
    Input,
    OverlayFrame,
    Ping,
    Pong,
    client_msg,
)

log = logging.getLogger("bluebook.server")
ROOT = Path(__file__).resolve().parents[4]  # repo root
PARTS = ["bluebook_perception", "bluebook_teach"] + (["bluebook_interactive"] if config.interactive else [])


async def _forward_overlays() -> None:
    async for frame in hub.subscribe(OverlayFrame):
        if conn := sessions.get(frame.session):
            try:
                await conn.overlay(frame)
            except Exception:
                log.exception("overlay send failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(f()) for f in [_forward_overlays, *_background]]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="bluebook", lifespan=lifespan)
# The Even Hub WebView enforces CORS on fetch(); tailnet membership is the auth boundary.
# ponytail: "*" for dev; pin to the WebView origin once known (see DESIGN §8 spike S4).
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_background = []
for name in PARTS:
    try:
        mod = importlib.import_module(name)
    except ImportError as e:
        if e.name != name:  # the part exists but one of *its* imports is broken: fail loud
            raise
        log.warning("part %s not installed, skipping", name)
        continue
    if bg := mod.setup(app):
        _background.append(bg)
    log.info("mounted %s", name)


@app.get("/health")
def health():
    return {"ok": True, "parts": PARTS, "sessions": list(sessions)}


@app.get("/skills")
def skills():
    return [{"id": s.id, "title": s.title, "summary": s.summary, "steps": len(s.steps)} for s in store.list_skills()]


@app.get("/skills/{skill_id}")
def skill(skill_id: str):
    try:
        return store.load(skill_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)


@app.get("/skills/{skill_id}/media/{rel:path}")
def media(skill_id: str, rel: str):
    try:
        return FileResponse(store.media_path(skill_id, rel))
    except ValueError:
        raise HTTPException(404)


@app.get("/camera")
def camera_page():
    return FileResponse(ROOT / "apps/camera/index.html")


@app.websocket("/ws/glasses")
async def ws_glasses(ws: WebSocket):
    await ws.accept()
    hello = Hello.model_validate_json(await ws.receive_text())
    session = str(hello.device.get("sn") or f"anon-{id(ws)}")
    conn = sessions[session] = GlassesConnection(ws, session)
    log.info("glasses connected session=%s", session)
    try:
        await conn.controller.on_connect(conn)
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                break
            if (b := m.get("bytes")) is not None:
                conn.audio_chunk(b)
                continue
            try:
                msg = client_msg.validate_json(m["text"])
            except ValidationError as e:
                log.warning("bad client msg: %s", e)
                continue
            match msg:
                case Input():
                    await conn.controller.on_input(conn, msg)
                case AudioStart():
                    conn.audio_start()
                case AudioEnd():
                    if pcm := conn.audio_end():
                        await conn.controller.on_utterance(conn, pcm)
                case Imu():
                    hub.publish(ImuSample(session=session, x=msg.x, y=msg.y, z=msg.z, t=msg.t))
                    await conn.controller.on_imu(conn, msg)
                case Ping():
                    await conn.send(Pong(t=msg.t))
                case _:
                    log.info("glasses %s: %s", session, msg)
    except WebSocketDisconnect:
        pass
    finally:
        if sessions.get(session) is conn:
            del sessions[session]
        await conn.controller.on_disconnect(conn)
        log.info("glasses disconnected session=%s", session)


@app.websocket("/ws/camera")
async def ws_camera(ws: WebSocket):
    await ws.accept()
    hello = CameraHello.model_validate_json(await ws.receive_text())
    log.info("camera %s connected %dx%d", hello.camera_id, hello.width, hello.height)
    try:
        while True:
            jpeg = await ws.receive_bytes()
            hub.publish(
                CameraFrame(session=hello.camera_id, t=time.time(), jpeg=jpeg, width=hello.width, height=hello.height)
            )
    except WebSocketDisconnect:
        log.info("camera %s disconnected", hello.camera_id)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(app, host=config.host, port=config.port)
