"""Hardware spike pages for docs/spikes.md (S5 input + glyphs, S1 image/text occlusion, S6 latency).

Pushed onto a live session with `POST /teach/spike/{session}` (session = glasses serial, see
GET /health). Every input on every page is echoed to the status line and logged with server time.
Double tap moves to the next page; on the last page it pops back to the lesson, which redraws.
Strings here bypass render.sanitize on purpose: finding out which glyphs render is part of S5.
"""

from __future__ import annotations

import logging
import time

from bluebook_core import display
from bluebook_core.glasses import Controller, GlassesConnection
from bluebook_core.protocol import ImageBox, Input, ShowPage, TextBox
from PIL import Image, ImageDraw

log = logging.getLogger("bluebook.teach.spike")

PAGES = ("s5", "s1", "s6")
GLYPH_TEST = "S5 glyphs: ● ★ ▼ ▶ ━ █ | ✓ · … é ° ½"


def stripes(w: int = 288, h: int = 144, period: int = 24) -> Image.Image:
    img = Image.new("L", (w, h))
    d = ImageDraw.Draw(img)
    for x in range(0, w, period * 2):
        d.rectangle((x, 0, x + period - 1, h), fill=255)
    return img


def occlusion_page() -> ShowPage:
    """Left half: image BELOW the text (z). Right half: image ABOVE the text. Both images are striped."""
    return ShowPage(
        boxes=[
            TextBox(id=1, name="bg", x=0, y=0, w=576, h=288, content=" ", capture=True, z=0),
            ImageBox(id=2, name="img_l", x=0, y=88, w=288, h=144, z=1),
            TextBox(id=3, name="txt", x=0, y=88, w=576, h=144, content="TEXT " * 60, z=2),
            ImageBox(id=4, name="img_r", x=288, y=88, w=288, h=144, z=3),
            TextBox(id=5, name="status", x=0, y=240, w=576, h=48, content="S1 occlusion", padding=4, z=4),
            TextBox(id=6, name="tip", x=0, y=0, w=576, h=88, content="S1. Left: image under text. Right: over.", z=5),
        ]
    )


class SpikeController(Controller):
    def __init__(self) -> None:
        self.i = 0
        self.n = 0  # inputs seen

    async def on_connect(self, conn: GlassesConnection) -> None:
        page = PAGES[self.i]
        if page == "s5":
            await conn.show(display.teach_page(GLYPH_TEST, "S5: swipe, hold, tap each temple + ring"))
        elif page == "s1":
            await conn.show(occlusion_page())
            for box in (b for b in conn.page.boxes if isinstance(b, ImageBox)):
                await conn.image(display.image_msg(box, stripes(box.w, box.h)))
        else:
            await conn.show(display.teach_page("S6 latency: film the lens in slow motion, then tap.", "S6 tap 0"))

    async def on_input(self, conn: GlassesConnection, ev: Input) -> None:
        t_recv = time.time()
        self.n += 1
        if ev.kind == "double_tap":
            self.i += 1
            if self.i >= len(PAGES):
                await conn.pop()
                return
            await self.on_connect(conn)
        elif PAGES[self.i] == "s6" and ev.kind == "tap":
            await conn.text("tip", f"TAP {self.n}")
        await conn.text("status", f"{PAGES[self.i]} #{self.n} {ev.kind} src={ev.source} box={ev.container}")
        log.info(
            "spike %s session=%s #%d kind=%s source=%s container=%s client_t=%.3f recv=%.3f sent=%.3f",
            PAGES[self.i], conn.session, self.n, ev.kind, ev.source, ev.container, ev.t, t_recv, time.time(),
        )  # fmt: skip

    async def on_utterance(self, conn: GlassesConnection, pcm: bytes) -> None:
        await conn.text("status", f"{PAGES[self.i]} heard {len(pcm) / 32000:.1f}s of audio")
