"""The pose WebSocket protocol between the phone app and the server, and the session handler."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

if TYPE_CHECKING:
    from .tracker import Tracker

log = logging.getLogger("bluebook.interactive")
CAM = "scene"  # camera id of the phone's scene camera

F = Annotated[float, Field(allow_inf_nan=False)]


class PoseHello(BaseModel):
    type: Literal["pose_hello"]
    camera_id: str = CAM
    source: Literal["arkit", "arkit-world", "arkit-world-front", "vision"]
    device: str = ""


class Face(BaseModel):
    R: Annotated[list[F], Field(min_length=9, max_length=9)]  # row-major, X_cam = R @ X_head + t
    t: Annotated[list[F], Field(min_length=3, max_length=3)]
    conf: Annotated[float, Field(ge=0, le=1)] = 1.0
    # World mode: which camera saw the face ("front" = ARKit TrueDepth, "rear" = Vision + LiDAR). Their noise differs
    # by >10x, so each source gets its own noise estimate.
    src: str | None = None


class TrackIn(BaseModel):
    id: str
    box: Annotated[list[F], Field(min_length=4, max_length=4)]  # x0,y0,x1,y1 normalised 0..1
    conf: Annotated[float, Field(ge=0, le=1)] = 1.0
    z: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None  # metres


class AnchorIn(BaseModel):
    """An app-owned world point (arkit-world mode): metres in the ARKit world frame (gravity-aligned, y up)."""

    id: str
    p: Annotated[list[F], Field(min_length=3, max_length=3)]
    how: str = (
        "estimate"  # "lidar" | "mesh" | "plane" | "estimate" | "camera" | "tracked" | "held" (free text: never drops)
    )
    conf: float | None = None  # coupled-tracker confidence (1 for static/camera)
    age_s: float | None = None  # seconds since this position was last measured


class PoseMsg(BaseModel):
    type: Literal["pose"]
    t: F  # phone capture time (unix epoch s)
    w: Annotated[int, Field(gt=0)]
    h: Annotated[int, Field(gt=0)]
    K: Annotated[list[F], Field(min_length=4, max_length=4)]  # fx, fy, cx, cy
    face: Face | None = None
    tracks: list[TrackIn] = []
    # ARKit only: the phone camera in the head frame, computed inside ARKit with no axis conversions (ground truth)
    cam_in_head: Annotated[list[F], Field(min_length=3, max_length=3)] | None = None
    # arkit-world only. face.R/t are then head -> WORLD; anchors always include id "camera" (the phone itself);
    # errors: anchor id -> reason (e.g. "no depth"); cam_T: rear camera -> world, row-major (ARKit convention).
    anchors: list[AnchorIn] | None = None
    errors: dict[str, str] | None = None
    cam_T: Annotated[list[F], Field(min_length=16, max_length=16)] | None = None
    ar: dict | None = None  # ARKit diagnostics from the app (tracking state, face source, lost reason, lux, ...)

    @model_validator(mode="after")
    def _k(self):
        if self.K[0] <= 0 or self.K[1] <= 0:
            raise ValueError("K focal lengths must be > 0")
        return self


# server -> app: {"type": "track", "id": str, "box": [x0, y0, x1, y1]} and {"type": "untrack", "id": str} (vision mode);
# arkit-world: {"type": "locate", "id": str, "box": [...]} (raycast the box in the last JPEG, answer with an anchor of
# that id) and {"type": "forget", "id": str}.
_pose_in = TypeAdapter(Annotated[PoseHello | PoseMsg, Field(discriminator="type")])


async def pose_session(ws: WebSocket, tk: Tracker) -> None:
    """One phone-app connection: validate each text message, drop bad ones, feed the tracker."""
    await ws.accept()
    if tk.app is not None:  # last connection wins
        with contextlib.suppress(RuntimeError, OSError):
            await tk.app.close(1012)
    tk.app, tk.hello = ws, None
    try:
        await tk.targets.resend()  # a reconnected app resumes its trackers
        while True:
            try:
                msg = _pose_in.validate_json(await ws.receive_text())
            except ValidationError as e:
                log.warning("bad pose msg: %s", e.errors(include_url=False, include_input=False)[:2])
                continue
            if isinstance(msg, PoseHello):
                tk.hello = msg
                tk.recorder.write("hello", tk.clock(), msg.model_dump())
            else:
                tk.on_pose(msg, time.time())
    except WebSocketDisconnect:
        pass
    finally:
        if tk.app is ws:
            tk.app = None
