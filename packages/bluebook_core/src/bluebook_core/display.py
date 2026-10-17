"""G2 display: shared layouts, PNG rasterising, and the overlay compositor.

Canvas 576x288, 16 greys of green, black = off (see-through). Measured costs
(even-g2-notes/performance.md): text upgrade ~83 ms, rebuild ~165 ms, image ~104 ms + 3.9 ms/KB.
Layout geometry here is a v1 guess; Part 2 owns tuning it on hardware (docs/DESIGN.md §6.1).
"""

from __future__ import annotations

import base64
import hashlib
import io
import math

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .protocol import Arrow, ImageBox, Label, ListBox, OverlayFrame, Rect, Ring, SetImage, ShowPage, TextBox

W, H = 576, 288

# Container ids/names are part of the contract: Part 2 updates "tip"/"status" by these,
# the compositor fills "img_l"/"img_r". Same id+name must be used in SetText or the SDK silently no-ops.
BG = TextBox(id=1, name="bg", x=0, y=0, w=W, h=H, content=" ", capture=True, z=0)


def teach_page(tip: str = "", status: str = "") -> ShowPage:
    """Step view: tooltip on top, 576x144 image band (keyframe or AR overlay), status line at bottom."""
    return ShowPage(
        boxes=[
            BG,
            ImageBox(id=2, name="img_l", x=0, y=88, w=288, h=144, z=1),
            ImageBox(id=3, name="img_r", x=288, y=88, w=288, h=144, z=2),
            TextBox(id=4, name="tip", x=0, y=0, w=W, h=88, content=tip, padding=4, z=3),
            TextBox(id=5, name="status", x=0, y=240, w=W, h=48, content=status, padding=4, brightness=2, z=4),
        ]
    )


def list_page(title: str, items: list[str]) -> ShowPage:
    """Library / menus. Native list: <=20 items, <=64 chars; firmware handles scroll highlight."""
    return ShowPage(
        boxes=[
            TextBox(id=1, name="title", x=0, y=0, w=W, h=40, content=title, padding=4),
            ListBox(id=2, name="list", x=0, y=40, w=W, h=H - 40, items=[i[:64] for i in items[:20]], capture=True),
        ]
    )


def message_page(text: str) -> ShowPage:
    """Full-screen text (overview, long answers, errors). Overflow scrolls in firmware."""
    return ShowPage(boxes=[TextBox(id=1, name="msg", x=0, y=0, w=W, h=H, content=text[:1000], capture=True, padding=6)])


# ------------------------------------------------------------------ rasterising


def to_png(img: Image.Image) -> bytes:
    """Any PIL image -> 16-level greyscale PNG. Host converts to gray4; we pre-quantise so what we test is what shows."""
    g = img.convert("L").point(lambda v: (v >> 4) * 17)
    buf = io.BytesIO()
    g.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Letterbox a photo (e.g. a step keyframe) into a container, contrast-stretched, Floyd-Steinberg dithered."""
    src = ImageOps.autocontrast(img.convert("L"))
    src.thumbnail((w, h))
    out = Image.new("L", (w, h))
    out.paste(src, ((w - src.width) // 2, (h - src.height) // 2))
    return out.quantize(16, dither=Image.Dither.FLOYDSTEINBERG).convert("L")


def image_msg(box: ImageBox, img: Image.Image) -> SetImage:
    assert img.size == (box.w, box.h), f"{box.name}: image {img.size} != box {(box.w, box.h)}"
    return SetImage(id=box.id, name=box.name, png_b64=base64.b64encode(to_png(img)).decode())


def draw(frame: OverlayFrame, stroke: int = 3) -> Image.Image:
    img = Image.new("L", (W, H))
    d = ImageDraw.Draw(img)
    for p in frame.primitives:
        if isinstance(p, Rect):
            d.rounded_rectangle((p.x0, p.y0, p.x1, p.y1), radius=6, outline=255, width=stroke)
        elif isinstance(p, Ring):
            d.ellipse((p.cx - p.r, p.cy - p.r, p.cx + p.r, p.cy + p.r), outline=255, width=stroke)
        elif isinstance(p, Arrow):
            d.line((p.x0, p.y0, p.x1, p.y1), fill=255, width=stroke)
            a = math.atan2(p.y1 - p.y0, p.x1 - p.x0)
            for da in (2.6, -2.6):
                d.line((p.x1, p.y1, p.x1 + 14 * math.cos(a + da), p.y1 + 14 * math.sin(a + da)), fill=255, width=stroke)
        elif isinstance(p, Label):
            d.text((p.x, p.y), p.text, fill=255, font=ImageFont.load_default(18))
    return img


class Compositor:
    """Draws OverlayFrames onto the full canvas and returns SetImage msgs only for image
    boxes whose pixels changed. Anything outside the page's image boxes is clipped —
    on teach_page that is the y=88..232 band. ponytail: fixed boxes; Part 3 may move a
    single box to follow the target via rebuild if hardware tests favour it."""

    def __init__(self) -> None:
        self._sent: dict[int, bytes] = {}

    def reset(self) -> None:
        self._sent.clear()

    def update(self, page: ShowPage, frame: OverlayFrame) -> list[SetImage]:
        canvas = draw(frame)
        out = []
        for b in page.boxes:
            if not isinstance(b, ImageBox):
                continue
            tile = canvas.crop((b.x, b.y, b.x + b.w, b.y + b.h))
            digest = hashlib.blake2b(tile.tobytes(), digest_size=8).digest()
            if self._sent.get(b.id) != digest:
                self._sent[b.id] = digest
                out.append(image_msg(b, tile))
        return out
