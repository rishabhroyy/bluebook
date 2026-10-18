"""TeachController: library -> overview -> steps -> done on the glasses. Spec: workstreams/part2-teaching.md

Everything that must survive a reconnect or Part 3's calibration push/pop lives in conn.state
(plain JSON values) and is mirrored to data/sessions/<session>.json so a lesson also survives a
server restart. on_connect always redraws from that state. Anything per-connection and
transient (tasks, the navigation epoch) lives on the controller instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Coroutine
from functools import lru_cache
from pathlib import Path

from bluebook_core import display, glasses, hub, store
from bluebook_core.config import config
from bluebook_core.glasses import Controller, GlassesConnection
from bluebook_core.protocol import CameraFrame, ImageBox, Input, SetImage, StepChanged, StepTarget
from bluebook_core.skill import SkillPackage, Step
from PIL import Image
from starlette.websockets import WebSocketState

from . import render, tutor

log = logging.getLogger("bluebook.teach")

SCREENS = ("library", "overview", "step", "done")
SCENE_CAMERA = "scene"  # apps/camera ?id=scene
FRESH_S = 2.0  # a scene frame older than this is not shown to a model
SCENE_EVERY_S = 4.0  # scene check cadence while a fresh frame exists
SCENE_CONFIDENT = 0.7  # suggest "looks done" after 2 agreeing checks at this confidence

# Push-to-talk feedback goes in the big "tip" area: the dim status line was easy to miss on glass.
LISTENING = "● Listening... let go when you're done."
THINKING = "Thinking..."
FAILED = "Couldn't answer. Hold to ask again."
# G2 swipes sometimes arrive twice ~170 ms apart (measured 2026-10-17); without this one swipe skips two pages.
SWIPE_DEBOUNCE_S = 0.3
LOOKS_DONE = "★ looks done, tap"
# Steps show text only (owner's call, 2026-10-17). True puts the step keyframe back in the image band.
SHOW_KEYFRAMES = False

# teach_page's image band (img_l + img_r): a step keyframe goes here unless Part 3 owns it.
BAND = tuple(b for b in display.teach_page().boxes if isinstance(b, ImageBox))
BAND_X, BAND_W, BAND_H = BAND[0].x, sum(b.w for b in BAND), max(b.h for b in BAND)


@lru_cache(maxsize=64)
def band_tiles(path: Path | None, _mtime: int = 0) -> tuple[SetImage, ...]:
    """A keyframe letterboxed into the band and cut into its image boxes. No path: black, which is
    see-through on the G2, so the previous step's picture never lingers."""
    band = Image.new("L", (BAND_W, BAND_H))
    if path:
        with Image.open(path) as im:
            band = display.fit(im, BAND_W, BAND_H)
    return tuple(display.image_msg(b, band.crop((b.x - BAND_X, 0, b.x - BAND_X + b.w, b.h))) for b in BAND)


def fresh_state() -> dict:
    return {
        "screen": "library",
        "skill_id": None,
        "step": 0,  # index into pkg.steps
        "view": 0,  # index into the pages currently in "tip" (detail pages, or answer pages)
        "answer": [],  # tutor pages shown in place of detail pages until dismissed
        "library_ids": [],  # what the list showed, so list_select indices map to what the learner saw
        "extra_substeps": {},  # step_id -> session-local substeps added by the tutor
        "history": [],  # last 10 {q, a} turns
    }


def step_targets(pkg: SkillPackage, step: Step) -> list[StepTarget]:
    """Step objects with role "target", joined to their ObjectRef (what Part 3 highlights)."""
    out = []
    for so in step.objects:
        if so.role != "target":
            continue
        try:
            o = pkg.object(so.object_id)
        except KeyError:
            continue
        out.append(
            StepTarget(
                object_id=o.id,
                name=o.name,
                aliases=o.aliases,
                visual_description=o.visual_description,
                action=so.action,
            )
        )
    return out


class TeachController(Controller):
    def __init__(
        self, skills_root: Path | None = None, sessions_dir: Path | None = None, interactive: bool | None = None
    ) -> None:
        self.skills_root = skills_root  # None = config.skills_dir (store's default)
        self.sessions_dir = sessions_dir or config.data_dir / "sessions"
        # With Part 3 on, its compositor is the single writer of the image band.
        self.interactive = config.interactive if interactive is None else interactive
        self.epoch = 0  # bumped on every navigation; async results from an older epoch are dropped
        self.question = 0  # bumped per utterance; an older question's answer is dropped
        self.note = ""  # transient status (listening / thinking / error), shown instead of the step title
        self.heard = ""  # last transcript, shown above its answer in the tip
        self.suggest = False  # scene checks agree the step looks done (a suggestion, never an advance)
        self.streak = 0  # consecutive confident "done" scene checks on this step
        self.flagged: set[int] = set()  # common mistakes already shown on this step
        self.last_check = 0.0
        self._band_key: str | None = None  # keyframe currently in the band ("" = black)
        self._scene_task: asyncio.Task | None = None
        self._last_swipe = ("", 0.0)  # (kind, monotonic time) for SWIPE_DEBOUNCE_S
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle

    async def on_connect(self, conn: GlassesConnection) -> None:
        # Also runs when a pushed controller (P3 calibration, spike) pops: anything asked before the push
        # is dropped, and its "thinking" note must not come back with the redraw.
        self.note = self.heard = ""
        self.question += 1
        if conn.state.get("screen") not in SCREENS:  # first connect of this device since server start
            conn.state.clear()
            conn.state.update(self._restore(conn.session))
        await self._render(conn)
        self._publish(conn)
        if self._scene_task is None or self._scene_task.done():  # on_connect also runs after a push/pop
            self._scene_task = self._spawn(self._scene_loop(conn), name="scene")

    async def on_disconnect(self, conn: GlassesConnection) -> None:
        for t in list(self._tasks):
            t.cancel()
        # A quick reconnect may already have registered a new connection for this device; don't clear its step.
        if glasses.sessions.get(conn.session) is None:
            hub.publish(StepChanged(session=conn.session, skill_id=conn.state.get("skill_id") or "", step_id=None))

    # ---------------------------------------------------------------------- input

    async def on_input(self, conn: GlassesConnection, ev: Input) -> None:
        log.info(
            "session %s: input %s src=%s box=%s idx=%s on %s step=%s answer=%d note=%r",
            conn.session,
            ev.kind,
            ev.source,
            ev.container,
            ev.index,
            conn.state.get("screen"),
            conn.state.get("step"),
            len(conn.state.get("answer", [])),
            self.note,
        )  # fmt: skip  — one line per gesture: the hardware input contract is still being measured (spike S5)
        if ev.kind in ("scroll_up", "scroll_down"):
            now = time.monotonic()
            if self._last_swipe[0] == ev.kind and now - self._last_swipe[1] < SWIPE_DEBOUNCE_S:
                log.info("session %s: dropped duplicate %s", conn.session, ev.kind)
                return
            self._last_swipe = (ev.kind, now)
        try:
            if ev.kind == "long_press":  # push-to-talk: the client is already streaming PCM
                self.note = LISTENING
                await self._update(conn, note_only=True)
            elif ev.kind == "long_press_release":
                if self.note == LISTENING:  # no utterance arrived (too short / no audio); AudioEnd comes first
                    self.note = ""
                    await self._update(conn, note_only=True)
            else:
                await getattr(self, f"_input_{conn.state['screen']}")(conn, ev)
        except (OSError, ValueError, IndexError) as e:  # skill changed on disk mid-lesson: redraw (falls back)
            log.warning("session %s: %s; redrawing", conn.session, e)
            await self._render(conn)

    async def _input_library(self, conn: GlassesConnection, ev: Input) -> None:
        if ev.kind in ("tap", "list_select"):
            ids = conn.state["library_ids"]
            i = ev.index or 0  # client quirk: index 0 often arrives missing
            if 0 <= i < len(ids):
                await self._go(conn, "overview", skill_id=ids[i])
        elif ev.kind == "double_tap":
            await conn.control(exit=True)  # required at the root page

    async def _input_overview(self, conn: GlassesConnection, ev: Input) -> None:
        if ev.kind == "tap" and conn.state["answer"]:
            await self._dismiss(conn)
        elif ev.kind == "tap":
            await self._go(conn, "step", step=0)
        elif ev.kind == "double_tap":
            await self._go(conn, "library")

    async def _input_step(self, conn: GlassesConnection, ev: Input) -> None:
        s = conn.state
        if ev.kind == "tap" and s["answer"]:
            await self._dismiss(conn)
        elif ev.kind == "tap":
            if s["step"] + 1 < len(self._pkg(conn).steps):
                await self._go(conn, "step", step=s["step"] + 1)
            else:
                await self._go(conn, "done")
        elif ev.kind == "double_tap":
            await self._go(conn, "overview")
        elif ev.kind in ("scroll_down", "scroll_up"):
            await self._scroll(conn, 1 if ev.kind == "scroll_down" else -1)

    async def _dismiss(self, conn: GlassesConnection) -> None:
        """First tap after an answer (or a camera warning) closes it and shows the instruction it covered;
        the next tap advances. Deviates from the brief's table, where tap always advances (owner's call, 2026-10-17)."""
        conn.state["answer"], conn.state["view"] = [], 0
        self.heard = ""
        await self._update(conn)

    async def _input_done(self, conn: GlassesConnection, ev: Input) -> None:
        if ev.kind == "tap":
            await self._go(conn, "library")
        elif ev.kind == "double_tap":
            await self._go(conn, "step", step=len(self._pkg(conn).steps) - 1)

    # ---------------------------------------------------------------------- voice

    async def on_utterance(self, conn: GlassesConnection, pcm: bytes) -> None:
        """Runs on the receive loop: flip the status and hand the model call to a task."""
        log.info("session %s: utterance %.1fs of audio on %s", conn.session, len(pcm) / 32000, conn.state["screen"])
        self.note = THINKING if conn.state["screen"] != "done" else ""
        self.question += 1
        await self._update(conn, note_only=True)
        if self.note:
            self._spawn(self._answer(conn, pcm, self.epoch, self.question, time.monotonic()), name="answer")

    async def _answer(self, conn: GlassesConnection, pcm: bytes, epoch: int, q: int, t0: float) -> None:
        s = conn.state
        screen = s["screen"]  # what the question was asked on; s["screen"] may change while we wait
        try:
            if screen == "library":
                skills = [k for k in store.list_skills(self.skills_root) if k.id in s["library_ids"]]
                pick = await tutor.pick_skill(skills, pcm)
            else:
                pkg = self._pkg(conn)
                step = pkg.steps[s["step"]] if screen == "step" else None
                extra = s["extra_substeps"].get(step.id, []) if step else []
                reply = await tutor.ask(pkg, step and step.id, s["history"], extra, pcm, self._fresh_frame())
        except Exception as e:  # noqa: BLE001 — timeout, model/network error, bad JSON: say so, keep the lesson
            log.warning("session %s: answer failed: %r", conn.session, e)
            if not self._stale(conn, epoch, q):
                self.note = FAILED
                await self._update(conn, note_only=True)
            return
        if self._stale(conn, epoch, q):
            log.info("session %s: dropped a stale answer (moved on, asked again, or reconnected)", conn.session)
            return
        self.note = ""
        try:
            if screen == "library":
                await self._apply_pick(conn, pick)
            else:
                await self._apply_reply(conn, pkg, reply)
        except (OSError, ValueError, IndexError, KeyError) as e:  # skill changed on disk meanwhile
            log.warning("session %s: answer not applied: %r; redrawing", conn.session, e)
            await self._render(conn)
            return
        log.info("session %s: answered in %.2fs", conn.session, time.monotonic() - t0)

    async def _apply_pick(self, conn: GlassesConnection, pick: tutor.LibraryPick) -> None:
        if pick.skill_id in conn.state["library_ids"]:
            await self._go(conn, "overview", skill_id=pick.skill_id)
        else:
            self.note = render.sanitize(pick.reply or "No match. Hold to try again.")
            await self._update(conn)

    async def _apply_reply(self, conn: GlassesConnection, pkg: SkillPackage, reply: tutor.TutorReply) -> None:
        s = conn.state
        pages = render.answer_pages(reply.pages)
        s["history"] = [*s["history"], {"q": reply.transcript, "a": " ".join(reply.pages)}][-10:]
        here = s["step"] if s["screen"] == "step" else -1  # overview counts as "before step 1"
        target = None
        if reply.action == "next":
            target = here + 1
        elif reply.action == "prev" and here > 0:
            target = here - 1
        elif reply.action == "goto":
            target = next((st.index for st in pkg.steps if st.id == reply.goto_step), None)  # unknown id: ignore
        elif reply.action == "add_substeps" and here >= 0 and reply.substeps:
            subs = [render.sanitize(x) for x in reply.substeps[:6]]
            s["extra_substeps"][pkg.steps[here].id] = subs
            pages = pages or render.answer_pages([f"Substep {i}/{len(subs)}. {x}" for i, x in enumerate(subs, 1)])
        if target is not None and target != here:
            if target >= len(pkg.steps):
                await self._go(conn, "done")
                return
            await self._go(conn, "step", step=target)
        self.heard = render.sanitize(reply.transcript)
        s["answer"], s["view"] = pages, 0
        await self._update(conn)

    def _fresh_frame(self) -> bytes | None:
        f = hub.last(CameraFrame, SCENE_CAMERA)
        return f.jpeg if f and time.time() - f.t < FRESH_S else None

    def _stale(self, conn: GlassesConnection, epoch: int, q: int | None = None) -> bool:
        """The learner moved on, asked again, a pushed controller (P3 calibration) owns the screen,
        or this socket was replaced by a reconnect."""
        return (
            epoch != self.epoch
            or (q is not None and q != self.question)
            or conn.controller is not self
            or glasses.sessions.get(conn.session, conn) is not conn
        )

    async def _scroll(self, conn: GlassesConnection, d: int) -> None:
        """Page through the answer (if one is showing), then back to the step's own pages."""
        s = conn.state
        n = len(self._pages(conn))
        if 0 <= s["view"] + d < n:
            s["view"] += d
        elif s["answer"]:  # scrolled past either end of an answer: dismiss it
            s["answer"], s["view"] = [], 0
            self.heard = ""
        else:
            return
        await self._refresh(conn)

    # ----------------------------------------------------------------- navigation

    async def _go(
        self, conn: GlassesConnection, screen: str, skill_id: str | None = None, step: int | None = None
    ) -> None:
        s = conn.state
        cheap = screen == "step" and s["screen"] == "step" and conn.page is not None
        s.update(screen=screen, view=0, answer=[])
        if skill_id is not None:
            s.update(skill_id=skill_id, step=0)
        if step is not None:
            s["step"] = step
        self.epoch += 1
        self.note = self.heard = ""
        self.suggest, self.streak, self.flagged = False, 0, set()
        if cheap:  # step -> step stays on teach_page: two text updates, no ~165 ms rebuild
            await self._refresh(conn)
            await self._band(conn)
        else:
            await self._render(conn)
        self._save(conn)
        self._publish(conn)

    async def _render(self, conn: GlassesConnection) -> None:
        """Full redraw of the current screen from conn.state. Falls back to the library if the skill vanished."""
        s = conn.state
        try:
            if s["screen"] == "overview":
                await conn.show(display.message_page(self._overview(conn)))
            elif s["screen"] == "step":
                await conn.show(display.teach_page(self._tip(conn), self._status(conn)))
                self._band_key = None  # a rebuilt page starts with empty image boxes
                await self._band(conn)
            elif s["screen"] == "done":
                await conn.show(display.message_page(render.done_text(self._pkg(conn))))
            else:
                await self._render_library(conn)
        except (OSError, ValueError, IndexError) as e:  # skill deleted/re-ingested/broken under us
            log.warning("session %s: %s; back to library", conn.session, e)
            s.update(screen="library", skill_id=None, step=0, view=0, answer=[])
            self.epoch += 1  # in-flight answers/scene checks were for the old screen
            self.note = self.heard = ""
            await self._render_library(conn)
            self._save(conn)
            self._publish(conn)  # Part 3 must stop highlighting the vanished step

    async def _render_library(self, conn: GlassesConnection) -> None:
        skills = store.list_skills(self.skills_root)[:20]  # native list holds 20 items
        conn.state["library_ids"] = [k.id for k in skills]
        if not skills:
            await conn.show(display.message_page(render.EMPTY_LIBRARY))
        else:
            await conn.show(display.list_page(render.LIBRARY_TITLE, [render.sanitize(k.title) for k in skills]))

    async def _refresh(self, conn: GlassesConnection) -> None:
        """In-place update of the step view (tip + status), ~83 ms each."""
        await conn.text("tip", self._tip(conn))
        await conn.text("status", self._status(conn))

    async def _update(self, conn: GlassesConnection, note_only: bool = False) -> None:
        """In-place update of whatever screen is up, e.g. for the listening/thinking note or an answer.
        note_only: on a step only the tip changed, so skip resending the status line (~83 ms on glass)."""
        screen = conn.state["screen"]
        if screen == "step" and note_only:
            await conn.text("tip", self._tip(conn))
        elif screen == "step":
            await self._refresh(conn)
        elif screen == "overview":
            await conn.text("msg", self._overview(conn))
        elif screen == "library" and conn.page and any(b.name == "title" for b in conn.page.boxes):
            await conn.text("title", self.note or render.LIBRARY_TITLE)

    def _overview(self, conn: GlassesConnection) -> str:
        """Overview text, or the tutor's answer to a question asked on the overview."""
        s = conn.state
        if s["answer"]:
            heard = f'"{self.heard}"\n\n' if self.heard else ""
            body = heard + "\n\n".join(s["answer"]) + "\n\nTap to close. Double tap for the library."
        else:
            body = render.overview_text(self._pkg(conn))
        return f"{self.note}\n\n{body}" if self.note else body

    # ---------------------------------------------------------------- step view

    def _pkg(self, conn: GlassesConnection) -> SkillPackage:
        if not (skill_id := conn.state.get("skill_id")):
            raise ValueError("no skill selected")
        return store.load(skill_id, self.skills_root)

    def _step(self, conn: GlassesConnection) -> Step:
        return self._pkg(conn).steps[conn.state["step"]]

    def _pages(self, conn: GlassesConnection) -> list[str]:
        s = conn.state
        if s["answer"]:
            return s["answer"]
        step = self._step(conn)
        return render.detail_pages(self._pkg(conn), step, s["extra_substeps"].get(step.id, []))

    def _tip(self, conn: GlassesConnection) -> str:
        if self.note:  # listening / thinking / error replace the tooltip until the answer (or release)
            return render.sanitize(self.note)
        pages = self._pages(conn)
        page = pages[min(conn.state["view"], len(pages) - 1)]
        if conn.state["answer"] and self.heard:  # what the learner said, above the answer to it
            return render.with_heard(self.heard, page)
        return page

    def _status(self, conn: GlassesConnection) -> str:
        s = conn.state
        note = LOOKS_DONE if self.suggest else ""
        return render.status_line(self._pkg(conn), s["step"], s["view"], len(self._pages(conn)), note)

    # ------------------------------------------------------------- image band

    async def _band(self, conn: GlassesConnection) -> None:
        """The step's first keyframe in the band, or black. Only sent when it changes (~100 ms per tile)."""
        if self.interactive or not SHOW_KEYFRAMES:
            return
        pkg, step = self._pkg(conn), self._step(conn)
        path, tiles = None, None
        if step.keyframes:
            try:
                path = store.media_path(pkg.id, step.keyframes[0].path, self.skills_root)
                tiles = await asyncio.to_thread(band_tiles, path, path.stat().st_mtime_ns)
            except (OSError, ValueError, Image.DecompressionBombError) as e:  # missing/corrupt media: no picture
                log.warning("keyframe %s/%s: %s", pkg.id, step.keyframes[0].path, e)
                path = None
        key = str(path) if path else ""
        if key == self._band_key:
            return
        for m in tiles or band_tiles(None):
            await conn.image(m)
        self._band_key = key

    # ------------------------------------------------------------ scene checks

    async def _scene_loop(self, conn: GlassesConnection) -> None:
        """Lives as long as the socket. One check in flight at most, since each tick awaits its check."""
        while getattr(conn.ws, "client_state", None) != WebSocketState.DISCONNECTED:
            await asyncio.sleep(1.0)
            try:
                await self._scene_tick(conn)
            except Exception as e:  # noqa: BLE001 — model/network errors: skip this check, keep looping
                log.warning("session %s: scene check failed: %r", conn.session, e)

    async def _scene_tick(self, conn: GlassesConnection, now: float | None = None) -> None:
        """Check the current step against a fresh scene frame every SCENE_EVERY_S. Suggests, never advances."""
        now = time.time() if now is None else now
        s = conn.state
        if s["screen"] != "step" or now - self.last_check < SCENE_EVERY_S or self._stale(conn, self.epoch):
            return
        frame = hub.last(CameraFrame, SCENE_CAMERA)
        if not frame or now - frame.t > FRESH_S:
            return
        pkg = self._pkg(conn)
        step = pkg.steps[s["step"]]
        if not step.success_criteria and not step.common_mistakes:
            return
        self.last_check, epoch = now, self.epoch
        r = await tutor.check_scene(step, frame.jpeg)
        if self._stale(conn, epoch):
            return
        confident = r.confidence >= SCENE_CONFIDENT
        self.streak = self.streak + 1 if r.done and confident else 0
        changed = False
        if self.streak >= 2 and not self.suggest and not render.is_danger(pkg, step):
            self.suggest = changed = True
        m = r.mistake
        known = bool(m and 1 <= m <= len(step.common_mistakes) and m not in self.flagged)
        if r.issue and confident and known and not s["answer"]:  # don't stomp an answer being read
            self.flagged.add(m)
            s["answer"], s["view"] = render.answer_pages([f"Check: {r.issue}"]), 0
            self.heard = ""  # this page isn't the answer to the last question
            changed = True
        if changed:
            await self._refresh(conn)

    # -------------------------------------------------------------- hub + disk

    def _publish(self, conn: GlassesConnection) -> None:
        """StepChanged on every step change; step_id=None outside a lesson. Part 3 depends on this."""
        s = conn.state
        step_id, targets = None, []
        if s["screen"] == "step":
            try:
                pkg = self._pkg(conn)
                step = pkg.steps[s["step"]]
                step_id, targets = step.id, step_targets(pkg, step)
            except (OSError, ValueError, IndexError):
                pass
        hub.publish(
            StepChanged(session=conn.session, skill_id=s.get("skill_id") or "", step_id=step_id, targets=targets)
        )

    def _session_file(self, session: str) -> Path | None:
        if session.startswith("anon-"):  # no device serial: the id changes on every connect
            return None
        return self.sessions_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', session)[:64]}.json"

    def _save(self, conn: GlassesConnection) -> None:
        if not (f := self._session_file(conn.session)):
            return
        s = conn.state
        try:
            step_id = self._step(conn).id if s["screen"] == "step" else None
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps({"screen": s["screen"], "skill_id": s["skill_id"], "step_id": step_id}))
            tmp.replace(f)
        except (OSError, ValueError, IndexError) as e:
            log.warning("session %s not saved: %s", conn.session, e)

    def _restore(self, session: str) -> dict:
        """Last saved position, validated against the skill as it is now on disk."""
        state = fresh_state()
        f = self._session_file(session)
        try:
            saved = json.loads(f.read_text()) if f and f.is_file() else {}
            if (
                not isinstance(saved, dict)  # hand-edited or corrupt file
                or saved.get("screen") not in ("overview", "step", "done")
                or not saved.get("skill_id")
            ):
                return state
            pkg = store.load(saved["skill_id"], self.skills_root)
        except (OSError, ValueError) as e:
            log.warning("session %s not restored: %s", session, e)
            return state
        state.update(skill_id=pkg.id, screen=saved["screen"])
        if saved["screen"] == "step":
            idx = next((s.index for s in pkg.steps if s.id == saved.get("step_id")), None)
            if idx is None:  # step ids changed (re-ingested): start the skill over from its overview
                state["screen"] = "overview"
            else:
                state["step"] = idx
        return state

    # ---------------------------------------------------------------------- tasks

    def _spawn(self, coro: Coroutine, name: str | None = None) -> asyncio.Task:
        """Model calls run off the receive loop; core's _safe() doesn't cover tasks, so log failures here."""
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and (e := t.exception()):
            log.error("teach task %s failed", t.get_name(), exc_info=e)
