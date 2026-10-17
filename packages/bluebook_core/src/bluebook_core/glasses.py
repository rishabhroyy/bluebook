"""Server side of /ws/glasses: one GlassesConnection per connected G2 app, driven by a Controller.

Part 2 provides the real Controller (library + teaching) via set_controller().
Part 3 may push() a temporary controller (calibration) and pop() it when done,
and sends overlays by publishing OverlayFrame on the hub — never by touching
image boxes directly, so the compositor stays the single writer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import WebSocket

from . import display
from .protocol import Control, Imu, Input, OverlayFrame, ServerMsg, SetImage, SetText, ShowPage, TextBox

log = logging.getLogger("bluebook.glasses")


class Controller:
    """Override what you need. All handlers run on the connection's receive loop:
    keep them fast; spawn tasks for model calls (asyncio.create_task). An exception
    in a handler is logged and the connection stays up."""

    async def on_connect(self, conn: GlassesConnection) -> None:
        """Called on every (re)connect AND when a pushed controller pops: redraw from conn.state, don't reset it."""

    async def on_input(self, conn: GlassesConnection, ev: Input) -> None:
        pass

    async def on_utterance(self, conn: GlassesConnection, pcm: bytes) -> None:
        """Complete push-to-talk utterance, 16 kHz s16le mono."""

    async def on_imu(self, conn: GlassesConnection, ev: Imu) -> None:
        pass

    async def on_disconnect(self, conn: GlassesConnection) -> None:
        pass


class EchoController(Controller):
    """Default when Part 2 isn't installed: proves the glasses <-> server path end to end."""

    async def on_connect(self, conn):
        await conn.show(
            display.message_page(
                "bluebook connected\nno teaching module installed\n\ntap / scroll / hold to test input"
            )
        )

    async def on_input(self, conn, ev):
        await conn.text("msg", f"input: {ev.kind} from {ev.source}")

    async def on_utterance(self, conn, pcm):
        await conn.text("msg", f"heard {len(pcm) / 32000:.1f}s of audio")


_factory: Callable[[], Controller] = EchoController
sessions: dict[str, GlassesConnection] = {}
_states: dict[str, dict] = {}  # session -> conn.state, kept across reconnects (ponytail: in-memory; lost on restart)


def set_controller(factory: Callable[[], Controller]) -> None:
    global _factory
    _factory = factory


class GlassesConnection:
    def __init__(self, ws: WebSocket, session: str) -> None:
        self.ws = ws
        self.session = session  # stable per device (serial) so a reconnect resumes the same session
        self.page: ShowPage | None = None
        # Controller-owned scratch space. Survives controller push/pop and reconnects of the same device.
        self.state: dict = _states.setdefault(session, {})
        self._stack: list[Controller] = [_factory()]
        self._send_lock = asyncio.Lock()
        self._compositor = display.Compositor()
        self._audio: bytearray | None = None

    @property
    def controller(self) -> Controller:
        return self._stack[-1]

    async def push(self, c: Controller) -> None:
        self._stack.append(c)
        await c.on_connect(self)

    async def pop(self) -> None:
        if len(self._stack) > 1:
            await self._stack.pop().on_disconnect(self)
            await self.controller.on_connect(self)  # let the previous controller redraw

    # ---------------------------------------------------------------- sending

    async def send(self, msg: ServerMsg) -> None:
        async with self._send_lock:
            await self.ws.send_text(msg.model_dump_json(exclude_none=True))

    async def show(self, page: ShowPage) -> None:
        self.page = page
        self._compositor.reset()
        await self.send(page)

    async def text(self, name: str, content: str, brightness: int | None = None) -> None:
        box = next((b for b in (self.page.boxes if self.page else []) if b.name == name), None)
        if not isinstance(box, TextBox):
            raise KeyError(f"no text box {name!r} on current page")
        await self.send(SetText(id=box.id, name=name, content=content, brightness=brightness))

    async def image(self, msg: SetImage) -> None:
        await self.send(msg)

    async def control(self, **kw) -> None:
        await self.send(Control(**kw))

    async def overlay(self, frame: OverlayFrame) -> None:
        if self.page:
            for m in self._compositor.update(self.page, frame):
                await self.send(m)

    # -------------------------------------------------------------- receiving

    def audio_start(self) -> None:
        self._audio = bytearray()

    def audio_chunk(self, b: bytes) -> None:
        if self._audio is not None and len(self._audio) < 16000 * 2 * 60:  # cap 60 s
            self._audio += b

    def audio_end(self) -> bytes | None:
        pcm, self._audio = self._audio, None
        return bytes(pcm) if pcm else None
