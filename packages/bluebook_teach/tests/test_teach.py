"""Part 2 tests: the TeachController state machine against a fake glasses socket. No network, no models."""

import asyncio
import base64
import io
import json
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

import pytest
from bluebook_core import display, glasses, hub, store
from bluebook_core.glasses import GlassesConnection
from bluebook_core.protocol import CameraFrame, Hello, Input, StepChanged
from bluebook_core.skill import MediaRef, SafetyNote
from bluebook_teach import render, tutor
from bluebook_teach.controller import FAILED, LISTENING, LOOKS_DONE, THINKING, TeachController
from bluebook_teach.spike import SpikeController
from bluebook_teach.tutor import LibraryPick, SceneCheck, TutorReply
from fastapi.testclient import TestClient
from PIL import Image

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/skills"
SKILL = "fix-leaky-compression-fitting"


class FakeWS:
    """Records what the server would send to the G2 app, decoded."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, s: str) -> None:
        self.sent.append(json.loads(s))


@pytest.fixture
async def make(tmp_path, monkeypatch):
    """make(root, session, interactive) -> (controller, connection) wired the way server.ws_glasses wires them."""
    made: list[TeachController] = []

    def _make(root: Path = FIXTURES, session: str | None = None, interactive: bool = False):
        ctrl = TeachController(skills_root=root, sessions_dir=tmp_path / "sessions", interactive=interactive)
        made.append(ctrl)
        monkeypatch.setattr(glasses, "_factory", lambda: ctrl)
        return ctrl, GlassesConnection(FakeWS(), session or f"t-{uuid4().hex[:8]}")

    yield _make
    for ctrl in made:  # stop scene loops (a real socket stops them on disconnect)
        for t in list(ctrl._tasks):
            t.cancel()
        await asyncio.gather(*ctrl._tasks, return_exceptions=True)


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


# ------------------------------------------------------------------------------ voice

PCM = b"\0" * 3200


class Replies(list):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []


@pytest.fixture
def replies(monkeypatch):
    """Queue TutorReply/LibraryPick/Exception results for the faked model calls; records call args."""
    queue = Replies()
    calls = queue.calls

    async def fake(*args, **kw):
        calls.append({"args": args, **kw})
        r = queue.pop(0)
        if isinstance(r, asyncio.Event):  # block until the test releases it
            await r.wait()
            r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def ask(pkg, step_id, history, extra, pcm, frame=None):
        return await fake(step_id=step_id, history=history, frame=frame)

    async def pick(skills, pcm):
        return await fake(skills=[k.id for k in skills])

    monkeypatch.setattr(tutor, "ask", ask)
    monkeypatch.setattr(tutor, "pick_skill", pick)
    return queue


async def settle(ctrl):
    """Wait for answer tasks (not the scene loop, which lives as long as the connection)."""
    while pending := [t for t in ctrl._tasks if t.get_name() != "scene"]:
        await asyncio.gather(*pending, return_exceptions=True)


async def say(ctrl, conn, settle_after=True):
    await press(ctrl, conn, "long_press")
    await ctrl.on_utterance(conn, PCM)
    await press(ctrl, conn, "long_press_release")
    if settle_after:
        await settle(ctrl)


async def test_question_answer_pages_then_back_to_step(make, replies):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    replies.append(TutorReply(transcript="which way is closed?", pages=["Clockwise. " + "turn " * 30, "Done."]))
    await press(ctrl, conn, "long_press")
    assert status(conn) == f"1/5 | {LISTENING}"
    await ctrl.on_utterance(conn, PCM)
    assert status(conn) == f"1/5 | {THINKING}"
    await settle(ctrl)

    answer = conn.state["answer"]
    assert len(answer) == 3 and all(len(p) <= render.TIP_CHARS for p in answer)  # long page re-paginated
    assert tip(conn) == answer[0] and '"which way is closed?"' in status(conn)
    assert replies.calls[0]["step_id"] == "s1"
    for _ in range(3):
        await press(ctrl, conn, "scroll_down")
    assert tip(conn) == "Turn the shutoff valve clockwise until it stops."  # scrolled off the end: dismissed
    assert conn.state["history"][-1]["q"] == "which way is closed?"


@pytest.mark.parametrize(
    ("reply", "start", "want_screen", "want_step"),
    [
        (TutorReply(action="next", pages=["Next."]), 0, "step", 1),
        (TutorReply(action="prev", pages=["Back."]), 2, "step", 1),
        (TutorReply(action="prev"), 0, "step", 0),
        (TutorReply(action="goto", goto_step="s4"), 0, "step", 3),
        (TutorReply(action="goto", goto_step="s99", pages=["?"]), 1, "step", 1),  # unknown id ignored
        (TutorReply(action="next"), 4, "done", 4),
    ],
)
async def test_tutor_actions(make, replies, reply, start, want_screen, want_step):
    ctrl, conn = make()
    await to_step(ctrl, conn, start)
    replies.append(reply)
    await say(ctrl, conn)
    assert conn.state["screen"] == want_screen and conn.state["step"] == want_step
    if want_screen == "step":
        assert hub.last(StepChanged, conn.session).step_id == f"s{want_step + 1}"
        assert tip(conn) == (render.answer_pages(reply.pages) or [tip(conn)])[0]


async def test_add_substeps_extends_the_step(make, replies):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    replies.append(
        TutorReply(action="add_substeps", substeps=["Find the cold valve, on the right.", "Grip the oval handle."])
    )
    await say(ctrl, conn)
    assert tip(conn) == "Substep 1/2. Find the cold valve, on the right."
    assert conn.state["extra_substeps"] == {"s1": ["Find the cold valve, on the right.", "Grip the oval handle."]}
    pages = render.detail_pages(store.load(SKILL, FIXTURES), store.load(SKILL, FIXTURES).steps[0], ["x"])
    assert any(p.startswith("Substep 1/1. x") for p in pages)


async def test_question_on_overview(make, replies):
    ctrl, conn = make()
    await ctrl.on_connect(conn)
    await press(ctrl, conn, "list_select")
    replies.append(TutorReply(pages=["You need an adjustable wrench."]))
    await say(ctrl, conn)
    msg = next(m for m in reversed(conn.ws.sent) if m["type"] == "text")
    assert msg["name"] == "msg" and msg["content"].startswith("You need an adjustable wrench.")
    assert replies.calls[0]["step_id"] is None
    replies.append(TutorReply(action="next", pages=["Let's go."]))
    await say(ctrl, conn)
    assert conn.state["screen"] == "step" and conn.state["step"] == 0 and tip(conn) == "Let's go."


async def test_answer_for_an_old_step_is_dropped(make, replies):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    gate = asyncio.Event()
    replies.extend([gate, TutorReply(action="next", pages=["stale"])])
    await say(ctrl, conn, settle_after=False)
    await press(ctrl, conn, "tap")  # learner moves on while the model is thinking
    gate.set()
    await settle(ctrl)
    assert conn.state["step"] == 1 and conn.state["answer"] == [] and "stale" not in tip(conn)


async def test_newer_question_wins(make, replies):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    gate = asyncio.Event()
    replies.extend([gate, TutorReply(pages=["first"])])
    await say(ctrl, conn, settle_after=False)
    await asyncio.sleep(0)  # first call starts and blocks on the gate
    replies.insert(0, TutorReply(pages=["second"]))  # served to the second call while the first waits
    await say(ctrl, conn, settle_after=False)
    await asyncio.sleep(0)
    gate.set()
    await settle(ctrl)
    assert tip(conn) == "second"


async def test_model_failure_shows_retry_and_lesson_continues(make, replies):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    replies.append(TimeoutError())
    await say(ctrl, conn)
    assert status(conn) == f"1/5 | {FAILED}"
    await press(ctrl, conn, "tap")
    assert tip(conn) == "Open the faucet to drain remaining pressure." and FAILED not in status(conn)


async def test_release_without_audio_clears_listening(make):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    await press(ctrl, conn, "long_press")
    await press(ctrl, conn, "long_press_release")  # too short: core never calls on_utterance
    assert status(conn).startswith("1/5 | Turn off the water")


async def test_pushed_controller_owns_the_screen(make, replies):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    gate = asyncio.Event()
    replies.extend([gate, TutorReply(pages=["late"])])
    await say(ctrl, conn, settle_after=False)
    await conn.push(glasses.Controller())  # e.g. Part 3 calibration takes over
    n = len(conn.ws.sent)
    gate.set()
    await settle(ctrl)
    assert len(conn.ws.sent) == n


async def test_library_voice_pick(make, replies):
    ctrl, conn = make()
    await ctrl.on_connect(conn)
    replies.append(LibraryPick(transcript="fix my leaking sink", skill_id=SKILL))
    await say(ctrl, conn)
    assert conn.state["screen"] == "overview" and replies.calls[0]["skills"] == [SKILL]

    await press(ctrl, conn, "double_tap")
    replies.append(LibraryPick(transcript="rewire a panel", skill_id="made-up", reply="No skill for that yet."))
    await say(ctrl, conn)
    assert conn.state["screen"] == "library"
    assert conn.ws.sent[-1] == {"type": "text", "id": 1, "name": "title", "content": "No skill for that yet."}


async def test_tutor_sees_only_a_fresh_scene_frame(make, replies):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    hub.publish(CameraFrame(session="scene", t=time.time() - 30, jpeg=b"old", width=4, height=3))
    replies.append(TutorReply(pages=["a"]))
    await say(ctrl, conn)
    hub.publish(CameraFrame(session="scene", t=time.time(), jpeg=b"new", width=4, height=3))
    replies.append(TutorReply(pages=["b"]))
    await say(ctrl, conn)
    assert [c["frame"] for c in replies.calls] == [None, b"new"]


async def test_history_is_capped(make, replies):
    ctrl, conn = make()
    await to_step(ctrl, conn)
    for i in range(12):
        replies.append(TutorReply(transcript=f"q{i}", pages=["ok"]))
        await say(ctrl, conn)
    assert [h["q"] for h in conn.state["history"]] == [f"q{i}" for i in range(2, 12)]


def test_skill_context_drops_what_the_tutor_cant_use():
    ctx = json.loads(tutor.skill_context(store.load(SKILL, FIXTURES)))
    assert {"transcript", "chunks", "source"}.isdisjoint(ctx)
    assert ctx["steps"][0]["narration"] and "t_start" not in ctx["steps"][0]
    assert ctx["steps"][1]["provenance"] == "inferred"  # the tutor must see provenance to hedge


# ------------------------------------------------------------------------- keyframes


@pytest.fixture
def keyframed(skills):
    """Fixture copy where s1 has a real keyframe and s4 points at a missing file."""
    pkg = store.load(SKILL, skills).model_copy(deep=True)
    (skills / SKILL / "media").mkdir()
    Image.linear_gradient("L").resize((640, 360)).convert("RGB").save(skills / SKILL / "media/s1.jpg")
    pkg.steps[0].keyframes = [MediaRef(path="media/s1.jpg", t=3)]
    pkg.steps[3].keyframes = [MediaRef(path="media/gone.jpg", t=50)]
    store.save(pkg, root=skills)
    return skills


def images(sent) -> list[tuple[str, Image.Image]]:
    return [(m["name"], Image.open(io.BytesIO(base64.b64decode(m["png_b64"])))) for m in sent if m["type"] == "image"]


async def test_keyframe_fills_the_band_and_is_cleared_after(make, keyframed):
    ctrl, conn = make(root=keyframed)
    await ctrl.on_connect(conn)
    await press(ctrl, conn, "list_select")
    sent = await press(ctrl, conn, "tap")
    assert [m["type"] for m in sent] == ["page", "image", "image"]
    tiles = images(sent)
    assert [n for n, _ in tiles] == ["img_l", "img_r"] and all(im.size == (288, 144) for _, im in tiles)
    assert all(im.getextrema()[1] > 0 for _, im in tiles)  # a picture, not black

    sent = await press(ctrl, conn, "tap")  # s2 has no keyframe: black (see-through) tiles replace s1's picture
    assert [im.getextrema() for _, im in images(sent)] == [(0, 0), (0, 0)]
    assert images(await press(ctrl, conn, "tap")) == []  # s3: still black, nothing resent
    assert images(await press(ctrl, conn, "tap")) == []  # s4: missing file degrades to black, no crash
    assert conn.state["step"] == 3


async def test_part3_owns_the_band_when_interactive(make, keyframed):
    ctrl, conn = make(root=keyframed, interactive=True)
    await to_step(ctrl, conn, 1)
    assert images(conn.ws.sent) == []


# ---------------------------------------------------------------------- scene checks


@pytest.fixture
def scene(monkeypatch):
    """Queue SceneCheck results; publish a fresh scene frame; returns (queue, calls, frame_time)."""
    queue: list = []
    calls: list = []

    async def check(step, jpeg):
        calls.append(step.id)
        return queue.pop(0)

    monkeypatch.setattr(tutor, "check_scene", check)
    t = time.time()
    hub.publish(CameraFrame(session="scene", t=t, jpeg=b"\xff\xd8", width=4, height=3))
    return queue, calls, t


DONE = SceneCheck(done=True, confidence=0.9)


async def test_scene_suggests_after_two_agreeing_checks_and_never_advances(make, scene):
    queue, calls, t = scene
    ctrl, conn = make()
    await to_step(ctrl, conn)
    queue += [DONE, DONE]
    await ctrl._scene_tick(conn, now=t)
    await ctrl._scene_tick(conn, now=t + 1)  # under the 4 s cadence: skipped
    assert calls == ["s1"] and LOOKS_DONE not in status(conn)
    ctrl.last_check -= 4  # pretend 4 s passed without making the frame stale
    await ctrl._scene_tick(conn, now=t)
    assert status(conn) == f"1/5 | {LOOKS_DONE} | pg 1/5"
    assert conn.state["step"] == 0
    await press(ctrl, conn, "tap")  # accepting the suggestion is just the normal tap
    assert conn.state["step"] == 1 and LOOKS_DONE not in status(conn)


async def test_scene_streak_breaks_on_a_not_done_check(make, scene):
    queue, calls, t = scene
    ctrl, conn = make()
    await to_step(ctrl, conn)
    queue += [DONE, SceneCheck(done=True, confidence=0.4), DONE]
    for _ in range(3):
        ctrl.last_check = 0
        await ctrl._scene_tick(conn, now=t)
    assert len(calls) == 3 and not ctrl.suggest


async def test_scene_never_confirms_a_danger_step(make, skills, scene):
    queue, calls, t = scene
    pkg = store.load(SKILL, skills).model_copy(deep=True)
    pkg.safety.append(SafetyNote(text="Live wires.", severity="danger", step_ids=["s1"]))
    store.save(pkg, root=skills)
    ctrl, conn = make(root=skills)
    await to_step(ctrl, conn)
    queue += [DONE] * 3
    for _ in range(3):
        ctrl.last_check = 0
        await ctrl._scene_tick(conn, now=t)
    assert len(calls) == 3 and not ctrl.suggest and LOOKS_DONE not in status(conn)


async def test_scene_flags_a_known_mistake_once(make, scene):
    queue, _calls, t = scene
    ctrl, conn = make()
    await to_step(ctrl, conn)
    wrong = SceneCheck(done=False, confidence=0.8, mistake=1, issue="That's the hot valve. Use the cold one.")
    queue += [wrong, wrong, SceneCheck(confidence=0.9, mistake=7, issue="made up")]
    ctrl.last_check = 0
    await ctrl._scene_tick(conn, now=t)
    assert tip(conn) == "Check: That's the hot valve. Use the cold one."
    await press(ctrl, conn, "scroll_down")  # dismiss
    for _ in range(2):  # same mistake again, then a mistake number that isn't in the step: ignored
        ctrl.last_check = 0
        await ctrl._scene_tick(conn, now=t)
    assert tip(conn) == "Turn the shutoff valve clockwise until it stops."


async def test_scene_skips_stale_frames_and_other_screens(make, scene):
    _queue, calls, t = scene
    ctrl, conn = make()
    await ctrl.on_connect(conn)
    await ctrl._scene_tick(conn, now=t)  # library: no checks
    await press(ctrl, conn, "list_select")
    await press(ctrl, conn, "tap")
    await ctrl._scene_tick(conn, now=t + 10)  # frame is 10 s old: camera gone
    assert calls == []


async def test_scene_result_for_an_old_step_is_dropped(make, scene, monkeypatch):
    _queue, _calls, t = scene
    ctrl, conn = make()
    await to_step(ctrl, conn)
    gate = asyncio.Event()

    async def slow(step, jpeg):
        await gate.wait()
        return SceneCheck(confidence=0.9, mistake=1, issue="stale")

    monkeypatch.setattr(tutor, "check_scene", slow)
    tick = asyncio.create_task(ctrl._scene_tick(conn, now=t))
    await asyncio.sleep(0)
    await press(ctrl, conn, "tap")
    gate.set()
    await tick
    assert "stale" not in tip(conn)


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


def test_is_danger_covers_step_and_whole_task_notes():
    pkg = store.load(SKILL, FIXTURES).model_copy(deep=True)
    assert not render.is_danger(pkg, pkg.steps[3])  # "caution" only
    pkg.safety.append(SafetyNote(text="Gas line nearby.", severity="danger"))  # no step_ids: whole task
    assert all(render.is_danger(pkg, st) for st in pkg.steps)


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


async def test_spike_pages_then_back_to_the_same_step(make):
    ctrl, conn = make()
    await to_step(ctrl, conn, 2)
    await conn.push(SpikeController())
    spike = conn.controller
    assert box(last_page(conn), "tip")["content"].startswith("S5 glyphs: ● ★")  # deliberately unsanitized
    await spike.on_input(conn, Input(kind="scroll_down", source="right", container="bg", t=0))
    assert status(conn) == "s5 #1 scroll_down src=right box=bg"

    await spike.on_input(conn, Input(kind="double_tap", t=0))  # -> S1 occlusion page with both stripe images
    assert {b["name"] for b in last_page(conn)["boxes"]} >= {"img_l", "img_r", "txt"}
    assert [m["name"] for m in conn.ws.sent[-3:-1]] == ["img_l", "img_r"]
    await spike.on_input(conn, Input(kind="double_tap", t=0))  # -> S6
    await spike.on_input(conn, Input(kind="tap", t=0))
    assert tip(conn) == "TAP 4"

    await spike.on_input(conn, Input(kind="double_tap", t=0))  # last page: pop back to teaching
    assert conn.controller is ctrl and tip(conn) == "Put a bucket or towel under the fitting."
    assert sum(t.get_name() == "scene" for t in ctrl._tasks) == 1  # redraw didn't start a second loop


def test_spike_route(monkeypatch, tmp_path):
    from bluebook_core.server import app

    monkeypatch.setattr(glasses, "_factory", lambda: TeachController(FIXTURES, tmp_path))
    sn = f"ws-{uuid4().hex[:6]}"
    with TestClient(app) as c, c.websocket_connect("/ws/glasses") as ws:
        ws.send_text(Hello(device={"sn": sn}).model_dump_json())
        ws.receive_text()
        assert c.post("/teach/spike/nobody").status_code == 404
        assert c.post(f"/teach/spike/{sn}").json() == {"ok": True}
        assert box(json.loads(ws.receive_text()), "tip")["content"].startswith("S5 glyphs")


def test_layout_names_match_core_contract():
    names = {b.name for b in display.teach_page().boxes}
    assert {"tip", "status", "img_l", "img_r"} <= names
