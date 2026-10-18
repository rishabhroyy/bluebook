"""Glasses controllers owned by Part 3."""

from __future__ import annotations

from bluebook_core import display, glasses
from bluebook_core.protocol import Input


class ViewController(glasses.Controller):
    """Test screen for the fake step route: a teach_page so overlays have image boxes to draw in.
    ponytail: only needed until Part 2's TeachController is merged; it shows teach_page itself."""

    def __init__(self, names: list[str], on_page) -> None:
        self.names = names
        self.on_page = on_page  # called with the session after the page is (re)drawn: the overlay must be redrawn

    async def on_connect(self, conn) -> None:
        await conn.show(display.teach_page(f"highlighting: {', '.join(self.names)}", "double tap: exit"))
        self.on_page(conn.session)

    async def on_input(self, conn, ev: Input) -> None:
        if ev.kind == "double_tap":
            await conn.pop()
