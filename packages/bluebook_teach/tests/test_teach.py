"""Part 2 tests: the TeachController state machine against a fake glasses socket. No network, no models."""

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from bluebook_core import display, glasses, hub, store
from bluebook_core.glasses import GlassesConnection
from bluebook_core.protocol import Hello, Input, StepChanged
from bluebook_teach import render
from bluebook_teach.controller import TeachController
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/skills"
SKILL = "fix-leaky-compression-fitting"


class FakeWS:
    """Records what the server would send to the G2 app, decoded."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, s: str) -> None:
        self.sent.append(json.loads(s))


@pytest.fixture
def make(tmp_path, monkeypatch):
    """make(root, session) -> (controller, connection) wired the way server.ws_glasses wires them."""

    def _make(root: Path = FIXTURES, session: str | None = None):
        ctrl = TeachController(skills_root=root, sessions_dir=tmp_path / "sessions")
        monkeypatch.setattr(glasses, "_factory", lambda: ctrl)
        return ctrl, GlassesConnection(FakeWS(), session or f"t-{uuid4().hex[:8]}")

    return _make


@pytest.fixture
def skills(tmp_path):
    """A writable copy of the fixture library."""
    root = tmp_path / "skills"
    shutil.copytree(FIXTURES, root)
    return root


async def press(ctrl, conn, kind, **kw):
    n = len(conn.ws.sent)
    await ctrl.on_input(conn, Input(kind=kind, t=0, **kw))
    return conn.ws.sent[n:]


def last_page(conn) -> dict:
    return next(m for m in reversed(conn.ws.sent) if m["type"] == "page")


def box(page: dict, name: str) -> dict:
    return next(b for b in page["boxes"] if b["name"] == name)


def tip(conn) -> str:
    """What the tooltip area shows now: the latest SetText to "tip", else the page's initial content."""
    for m in reversed(conn.ws.sent):
        if m["type"] == "text" and m["name"] == "tip":
            return m["content"]
        if m["type"] == "page":
            return box(m, "tip")["content"]
    raise AssertionError("no tip shown")


def status(conn) -> str:
    for m in reversed(conn.ws.sent):
        if m["type"] == "text" and m["name"] == "status":
            return m["content"]
        if m["type"] == "page":
            return box(m, "status")["content"]
    raise AssertionError("no status shown")


async def to_step(ctrl, conn, n: int = 0):
    await ctrl.on_connect(conn)
    await press(ctrl, conn, "list_select")
    await press(ctrl, conn, "tap")
    for _ in range(n):
        await press(ctrl, conn, "tap")


# ----------------------------------------------------------------- screens + input map


async def test_full_walk_library_to_done_and_back_out(make):
    ctrl, conn = make()
    await ctrl.on_connect(conn)
    assert box(last_page(conn), "list")["items"] == ["Fix a leaky sink supply fitting"]

    await press(ctrl, conn, "list_select")  # index omitted: the client quirk for item 0
    assert "Tap to start" in box(last_page(conn), "msg")["content"]

    await press(ctrl, conn, "tap")
    assert conn.state["screen"] == "step"
    assert tip(conn) == "Turn the shutoff valve clockwise until it stops."
    ev = hub.last(StepChanged, conn.session)
    assert ev.step_id == "s1" and [t.object_id for t in ev.targets] == ["shutoff_valve"]

    for want in ("s2", "s3", "s4", "s5"):
        sent = await press(ctrl, conn, "tap")
        assert [m["type"] for m in sent] == ["text", "text"]  # step -> step never rebuilds the page
        assert hub.last(StepChanged, conn.session).step_id == want
    assert status(conn).startswith("5/5 | Test")

    await press(ctrl, conn, "tap")
    assert box(last_page(conn), "msg")["content"].startswith("Done.")
    assert hub.last(StepChanged, conn.session).step_id is None

    await press(ctrl, conn, "double_tap")  # done -> last step
    assert conn.state["screen"] == "step" and conn.state["step"] == 4
    await press(ctrl, conn, "double_tap")  # step -> overview
    assert conn.state["screen"] == "overview"
    await press(ctrl, conn, "double_tap")  # overview -> library
    assert conn.state["screen"] == "library"
    sent = await press(ctrl, conn, "double_tap")  # library root -> exit dialog (store requirement)
    assert sent == [{"type": "control", "exit": True}]


async def test_scroll_pages_through_step_detail(make):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    pages = render.detail_pages(store.load(SKILL, FIXTURES), store.load(SKILL, FIXTURES).steps[0])
    assert len(pages) > 2 and all(len(p) <= render.TIP_CHARS for p in pages)

    assert await press(ctrl, conn, "scroll_up") == []  # already on the tooltip
    await press(ctrl, conn, "scroll_down")
    assert tip(conn) == pages[1] and status(conn).endswith(f"2/{len(pages)}")
    for _ in range(len(pages) + 3):
        await press(ctrl, conn, "scroll_down")
    assert tip(conn) == pages[-1]
    await press(ctrl, conn, "tap")  # next step resets to its tooltip
    assert tip(conn) == "Open the faucet to drain remaining pressure."


async def test_empty_library_still_exits(make, tmp_path):
    ctrl, conn = make(root=tmp_path / "none")
    await ctrl.on_connect(conn)
    assert "No skills yet" in box(last_page(conn), "msg")["content"]
    assert await press(ctrl, conn, "tap") == []
    assert await press(ctrl, conn, "double_tap") == [{"type": "control", "exit": True}]


async def test_list_index_out_of_range_is_ignored(make):
    ctrl, conn = make()
    await ctrl.on_connect(conn)
    assert await press(ctrl, conn, "list_select", index=7) == []


# ------------------------------------------------------------------------ persistence


async def test_lesson_survives_server_restart(make):
    ctrl, conn = make(session="sn-restart")
    await to_step(ctrl, conn, 2)
    glasses._states.pop("sn-restart")  # a restart loses everything in memory

    ctrl2, conn2 = make(session="sn-restart")
    await ctrl2.on_connect(conn2)
    assert conn2.state["screen"] == "step" and conn2.state["step"] == 2
    assert tip(conn2) == "Put a bucket or towel under the fitting."
    assert hub.last(StepChanged, "sn-restart").step_id == "s3"


async def test_reconnect_resumes_from_memory(make):
    ctrl, conn = make()
    await to_step(ctrl, conn, 1)
    await press(ctrl, conn, "scroll_down")
    ctrl2, conn2 = make(session=conn.session)  # same glasses serial, new socket (e.g. Android WebView killed)
    await ctrl2.on_connect(conn2)
    assert conn2.state["step"] == 1 and conn2.state["view"] == 1


async def test_deleted_skill_falls_back_to_library(make, skills):
    ctrl, conn = make(root=skills, session="sn-deleted")
    await to_step(ctrl, conn, 1)
    store.delete(SKILL, skills)
    await press(ctrl, conn, "tap")  # mid-lesson: no crash, back to the (now empty) library
    assert conn.state["screen"] == "library" and "No skills yet" in box(last_page(conn), "msg")["content"]

    glasses._states.pop("sn-deleted")
    ctrl2, conn2 = make(root=skills, session="sn-deleted")
    await ctrl2.on_connect(conn2)
    assert conn2.state["screen"] == "library"


async def test_reingested_skill_with_new_step_ids_restarts_at_overview(make, skills):
    ctrl, conn = make(root=skills, session="sn-reingest")
    await to_step(ctrl, conn, 2)
    pkg = store.load(SKILL, skills).model_copy(deep=True)
    pkg.steps[2].id = "s3-new"
    store.save(pkg, root=skills)
    p = skills / SKILL / "skill.json"
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 1_000_000))

    glasses._states.pop("sn-reingest")
    ctrl2, conn2 = make(root=skills, session="sn-reingest")
    await ctrl2.on_connect(conn2)
    assert conn2.state["screen"] == "overview"


async def test_anonymous_sessions_are_not_persisted(make, tmp_path):
    ctrl, conn = make(session="anon-123")
    await to_step(ctrl, conn)
    assert not (tmp_path / "sessions").exists()


# ---------------------------------------------------------------------------- render


def test_sanitize_maps_typography_and_drops_unknown_glyphs():
    assert render.sanitize("“Hi” — café \U0001f44d ½ ●━") == '"Hi" - cafe  1/2 ●━'


def test_paginate_respects_limit_and_word_boundaries():
    pages = render.paginate("word " * 60 + "x" * 130, limit=50)
    assert all(len(p) <= 50 for p in pages)
    assert all(not p.startswith(" ") and not p.endswith(" ") for p in pages)
    assert "".join(pages[-3:]).endswith("x" * 130)
    assert render.paginate("") == []


def test_overview_keeps_safety_when_truncated():
    pkg = store.load(SKILL, FIXTURES).model_copy(update={"summary": "long " * 400})
    text = render.overview_text(pkg)
    assert len(text) <= render.MSG_CHARS and "CAUTION. Do not over-tighten" in text


# ------------------------------------------------------------------ through the server


def test_teach_controller_through_the_websocket(monkeypatch, tmp_path):
    from bluebook_core.server import app

    monkeypatch.setattr(glasses, "_factory", lambda: TeachController(FIXTURES, tmp_path))
    with TestClient(app) as c, c.websocket_connect("/ws/glasses") as ws:
        ws.send_text(Hello(device={"sn": f"ws-{uuid4().hex[:6]}"}).model_dump_json())
        page = json.loads(ws.receive_text())
        assert page["type"] == "page" and box(page, "list")["items"]
        ws.send_text(Input(kind="list_select", index=0, t=0).model_dump_json())
        assert "Tap to start" in box(json.loads(ws.receive_text()), "msg")["content"]
        ws.send_text(Input(kind="tap", t=0).model_dump_json())
        page = json.loads(ws.receive_text())
        assert box(page, "tip")["content"].startswith("Turn the shutoff valve")
        assert c.get("/teach/sessions").json()  # debug route shows the live session


def test_layout_names_match_core_contract():
    names = {b.name for b in display.teach_page().boxes}
    assert {"tip", "status", "img_l", "img_r"} <= names
