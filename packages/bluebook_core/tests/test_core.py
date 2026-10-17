"""Contract tests. If one of these breaks, a part is about to break another part."""

import json
from pathlib import Path

import pytest
from bluebook_core import display, store
from bluebook_core.protocol import Hello, Input, OverlayFrame, Rect, client_msg, server_msg
from fastapi.testclient import TestClient
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/skills"


def test_fixture_is_valid():
    pkg = store.load("fix-leaky-compression-fitting", FIXTURES)
    assert pkg.check() == []
    assert pkg.step("s1").objects[0].role == "target"


def test_check_catches_bad_refs():
    pkg = store.load("fix-leaky-compression-fitting", FIXTURES).model_copy(deep=True)
    pkg.steps[0].objects[0].object_id = "nope"
    assert any("unknown object" in e for e in pkg.check())


def test_skill_id_rejects_traversal():
    with pytest.raises(ValueError):
        store.skill_dir("../etc")
    with pytest.raises(ValueError):
        store.media_path("fix-leaky-compression-fitting", "../../x", FIXTURES)


async def test_keyword_search():
    pkg = store.load("fix-leaky-compression-fitting", FIXTURES)
    hits = await store.search(pkg, "which way closes the valve", root=FIXTURES)
    assert hits and hits[0][0].id in {"c_faq1", "c_s1"}


def test_save_roundtrip(tmp_path):
    pkg = store.load("fix-leaky-compression-fitting", FIXTURES)
    store.save(pkg, root=tmp_path)
    assert store.load(pkg.id, tmp_path) == pkg


def test_protocol_roundtrip_and_limits():
    page = display.teach_page("tip", "status")
    assert server_msg.validate_json(page.model_dump_json()) == page
    assert isinstance(client_msg.validate_json('{"type":"input","kind":"tap","t":1}'), Input)
    with pytest.raises(ValidationError):
        display.ImageBox(id=9, name="x", x=0, y=0, w=300, h=100)  # >288 wide is rejected by firmware


def test_compositor_sends_only_changed_tiles():
    page, comp = display.teach_page(), display.Compositor()
    left = OverlayFrame(session="s", t=0, primitives=[Rect(x0=20, y0=100, x1=120, y1=200)])
    assert [m.name for m in comp.update(page, left)] == ["img_l", "img_r"]  # first frame: both
    assert comp.update(page, left) == []  # unchanged: nothing
    moved = OverlayFrame(session="s", t=1, primitives=[Rect(x0=30, y0=100, x1=130, y1=200)])
    assert [m.name for m in comp.update(page, moved)] == ["img_l"]


def test_glasses_ws_echo():
    from bluebook_core.server import app

    with TestClient(app) as c, c.websocket_connect("/ws/glasses") as ws:
        ws.send_text(Hello(device={"sn": "test"}).model_dump_json())
        assert json.loads(ws.receive_text())["type"] == "page"
        ws.send_text(Input(kind="tap", t=0).model_dump_json())
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "text" and "tap" in msg["content"]
        ws.send_text('{"type":"audio_start","utterance":"u1"}')
        ws.send_bytes(b"\0" * 32000)
        ws.send_text('{"type":"audio_end","utterance":"u1"}')
        assert "1.0s" in json.loads(ws.receive_text())["content"]
