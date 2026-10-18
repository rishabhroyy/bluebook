"""bluebook_premiere: frames are built from the app's own code; framebuffer -> PNG. No simulator here."""

from bluebook_premiere.cli import build, finish, parser
from PIL import Image


def test_build_uses_app_marks():
    page, images = build(
        parser().parse_args(["screen", "x.png", "--tip", "hi ✓", "--target", "300,160", "--target=900,160"])
    )
    assert next(b for b in page.boxes if b.name == "tip").content == "hi "  # sanitised like Part 2: no ✓ glyph
    assert {m.name for m in images} == {"img_l", "img_r"}  # ring + chevron drawn into the band tiles


def test_finish_crops_scales_tints():
    shot = Image.new("RGBA", (576, 288), (0, 255, 0, 0))
    shot.putpixel((100, 100), (0, 255, 0, 255))
    out = finish(shot, parser().parse_args(["pack", "d", "--scale", "2", "--color", "#ff0000"]))
    assert out.size == (18, 18) and out.getpixel((8, 8)) == (255, 0, 0, 255) and out.getpixel((0, 0))[3] == 0
