"""Contract tests. If one of these breaks, a part is about to break another part."""

import json
import os
from pathlib import Path

import pytest
from bluebook_core import display, store
from bluebook_core.protocol import Hello, Input, OverlayFrame, Rect, ShowPage, client_msg, server_msg
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
    from bluebook_core import glasses
    from bluebook_core.server import app

    glasses.set_controller(glasses.EchoController)  # mounted parts (Part 2) replace the default at import
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


def test_show_page_enforces_sdk_limits():
    tb = lambda i, cap=False: display.TextBox(id=i, name=f"t{i}", x=0, y=0, w=10, h=10, capture=cap)
    with pytest.raises(ValidationError, match="capture"):
        ShowPage(boxes=[tb(1), tb(2)])
    with pytest.raises(ValidationError, match="duplicate"):
        ShowPage(boxes=[tb(1, True), tb(1)])
    with pytest.raises(ValidationError, match="8 text"):
        ShowPage(boxes=[tb(i, i == 0) for i in range(9)])
    with pytest.raises(ValidationError, match="canvas"):
        display.TextBox(id=1, name="x", x=500, y=0, w=100, h=10)


def test_store_reloads_after_external_write_and_skips_broken(tmp_path):
    pkg = store.load("fix-leaky-compression-fitting", FIXTURES)
    store.save(pkg, root=tmp_path)
    assert store.load(pkg.id, tmp_path).title == pkg.title  # warm the cache
    p = tmp_path / pkg.id / "skill.json"
    p.write_text(p.read_text().replace(pkg.title, "Edited elsewhere"))  # e.g. Part 1 CLI in another process
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 1_000_000))
    assert store.load(pkg.id, tmp_path).title == "Edited elsewhere"
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "skill.json").write_text("{}")
    assert [s.id for s in store.list_skills(tmp_path)] == [pkg.id]


def test_controller_error_keeps_connection_and_state_survives_reconnect():
    from bluebook_core import glasses
    from bluebook_core.server import app

    class Flaky(glasses.Controller):
        async def on_connect(self, conn):
            conn.state["n"] = conn.state.get("n", 0) + 1
            await conn.show(display.message_page(f"connect {conn.state['n']}"))

        async def on_input(self, conn, ev):
            if ev.kind == "tap":
                raise RuntimeError("part bug")
            await conn.text("msg", "still alive")

    glasses.set_controller(Flaky)
    try:
        with TestClient(app) as c:
            for n in (1, 2):
                with c.websocket_connect("/ws/glasses") as ws:
                    ws.send_text(Hello(device={"sn": "flaky"}).model_dump_json())
                    assert f"connect {n}" in ws.receive_text()
                    ws.send_text(Input(kind="tap", t=0).model_dump_json())
                    ws.send_text(Input(kind="double_tap", t=0).model_dump_json())
                    assert "still alive" in ws.receive_text()
    finally:
        glasses.set_controller(glasses.EchoController)


def test_camera_frames_reach_hub():
    from bluebook_core import hub
    from bluebook_core.protocol import CameraFrame, CameraHello
    from bluebook_core.server import app

    with TestClient(app) as c, c.websocket_connect("/ws/camera") as ws:
        ws.send_text(CameraHello(camera_id="test-cam", width=4, height=3, fps=1).model_dump_json())
        ws.send_bytes(b"\xff\xd8jpeg")
        ws.send_text('{"type":"ping","t":0}')  # text after hello is ignored, not fatal
        ws.send_bytes(b"\xff\xd8jpeg2")
    assert hub.last(CameraFrame, "test-cam").jpeg == b"\xff\xd8jpeg2"


def test_write_env_merges_and_preserves(tmp_path):
    from bluebook_core.cli import write_env
    from bluebook_core.config import read_env

    p = tmp_path / ".env"
    p.write_text("# keep me\nGEMINI_API_KEY=old\nBB_MODEL_VIDEO=custom\n")
    write_env({"GEMINI_API_KEY": "new", "BB_PORT": "9000"}, p)
    assert read_env(p) == {"GEMINI_API_KEY": "new", "BB_MODEL_VIDEO": "custom", "BB_PORT": "9000"}
    assert p.read_text().startswith("# keep me\n")
    assert p.stat().st_mode & 0o777 == 0o600
