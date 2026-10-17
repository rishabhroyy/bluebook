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
from collections.abc import Coroutine
from pathlib import Path

from bluebook_core import display, glasses, hub, store
from bluebook_core.config import config
from bluebook_core.glasses import Controller, GlassesConnection
from bluebook_core.protocol import Input, StepChanged, StepTarget
from bluebook_core.skill import SkillPackage, Step

from . import render

log = logging.getLogger("bluebook.teach")

SCREENS = ("library", "overview", "step", "done")


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
    def __init__(self, skills_root: Path | None = None, sessions_dir: Path | None = None) -> None:
        self.skills_root = skills_root  # None = config.skills_dir (store's default)
        self.sessions_dir = sessions_dir or config.data_dir / "sessions"
        self.epoch = 0  # bumped on every navigation; async results from an older epoch are dropped
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle

    async def on_connect(self, conn: GlassesConnection) -> None:
        if conn.state.get("screen") not in SCREENS:  # first connect of this device since server start
            conn.state.clear()
            conn.state.update(self._restore(conn.session))
        await self._render(conn)
        self._publish(conn)

    async def on_disconnect(self, conn: GlassesConnection) -> None:
        for t in self._tasks:
            t.cancel()
        # A quick reconnect may already have registered a new connection for this device; don't clear its step.
        if glasses.sessions.get(conn.session) is None:
            hub.publish(StepChanged(session=conn.session, skill_id=conn.state.get("skill_id") or "", step_id=None))

    # ---------------------------------------------------------------------- input

    async def on_input(self, conn: GlassesConnection, ev: Input) -> None:
        try:
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
        if ev.kind == "tap":
            await self._go(conn, "step", step=0)
        elif ev.kind == "double_tap":
            await self._go(conn, "library")

    async def _input_step(self, conn: GlassesConnection, ev: Input) -> None:
        s = conn.state
        if ev.kind == "tap":
            if s["step"] + 1 < len(self._pkg(conn).steps):
                await self._go(conn, "step", step=s["step"] + 1)
            else:
                await self._go(conn, "done")
        elif ev.kind == "double_tap":
            await self._go(conn, "overview")
        elif ev.kind in ("scroll_down", "scroll_up"):
            await self._scroll(conn, 1 if ev.kind == "scroll_down" else -1)

    async def _input_done(self, conn: GlassesConnection, ev: Input) -> None:
        if ev.kind == "tap":
            await self._go(conn, "library")
        elif ev.kind == "double_tap":
            await self._go(conn, "step", step=len(self._pkg(conn).steps) - 1)

    async def _scroll(self, conn: GlassesConnection, d: int) -> None:
        """Page through the answer (if one is showing), then back to the step's own pages."""
        s = conn.state
        n = len(self._pages(conn))
        if 0 <= s["view"] + d < n:
            s["view"] += d
        elif s["answer"]:  # scrolled past either end of an answer: dismiss it
            s["answer"], s["view"] = [], 0
        else:
            return
        await self._refresh(conn)

    # ----------------------------------------------------------------- navigation

    async def _go(self, conn: GlassesConnection, screen: str, skill_id: str | None = None, step: int = 0) -> None:
        s = conn.state
        cheap = screen == "step" and s["screen"] == "step" and conn.page is not None
        s.update(screen=screen, step=step, view=0, answer=[])
        if skill_id is not None:
            s["skill_id"] = skill_id
        self.epoch += 1
        if cheap:  # step -> step stays on teach_page: two text updates, no ~165 ms rebuild
            await self._refresh(conn)
        else:
            await self._render(conn)
        self._save(conn)
        self._publish(conn)

    async def _render(self, conn: GlassesConnection) -> None:
        """Full redraw of the current screen from conn.state. Falls back to the library if the skill vanished."""
        s = conn.state
        try:
            if s["screen"] == "overview":
                await conn.show(display.message_page(render.overview_text(self._pkg(conn))))
            elif s["screen"] == "step":
                await conn.show(display.teach_page(self._tip(conn), self._status(conn)))
            elif s["screen"] == "done":
                await conn.show(display.message_page(render.done_text(self._pkg(conn))))
            else:
                await self._render_library(conn)
        except (OSError, ValueError, IndexError) as e:  # skill deleted/re-ingested/broken under us
            log.warning("session %s: %s; back to library", conn.session, e)
            s.update(screen="library", skill_id=None, step=0, view=0, answer=[])
            await self._render_library(conn)

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
        pages = self._pages(conn)
        return pages[min(conn.state["view"], len(pages) - 1)]

    def _status(self, conn: GlassesConnection) -> str:
        s = conn.state
        return render.status_line(self._pkg(conn), s["step"], s["view"], len(self._pages(conn)))

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
            if saved.get("screen") not in ("overview", "step", "done") or not saved.get("skill_id"):
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
