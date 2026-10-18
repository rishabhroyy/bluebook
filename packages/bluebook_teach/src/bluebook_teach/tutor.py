"""Model calls for teaching: voice Q&A, voice skill pick, scene checks.

All calls go through bluebook_core.models with a role name ("tutor", "scene"), so model ids and
retries stay in core. Each call is bounded by a timeout here because a stuck request would leave
the glasses showing "thinking" forever. Prompts live in prompts/*.md so changes diff cleanly.
"""

from __future__ import annotations

import asyncio
import json
from functools import cache
from pathlib import Path
from typing import Literal

from bluebook_core import models
from bluebook_core.skill import SkillPackage, Step
from pydantic import BaseModel

PROMPTS = Path(__file__).parent / "prompts"
ASK_TIMEOUT_S = 15.0  # voice p50 target is < 3 s; this only bounds the stuck case
SCENE_TIMEOUT_S = 10.0


class TutorReply(BaseModel):
    transcript: str = ""  # what the learner said (shown briefly; it builds trust)
    pages: list[str] = []  # each <= 120 chars, <= 4 pages (re-paginated anyway)
    action: Literal["none", "next", "prev", "goto", "add_substeps"] = "none"
    goto_step: str | None = None
    substeps: list[str] = []  # session-local extra breakdown when the learner is struggling


class LibraryPick(BaseModel):
    transcript: str = ""
    skill_id: str | None = None
    reply: str = ""


class SceneCheck(BaseModel):
    done: bool = False
    confidence: float = 0.0
    mistake: int | None = None  # 1-based number of a listed common mistake seen in the photo
    issue: str | None = None


@cache
def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def skill_context(pkg: SkillPackage) -> str:
    """The whole package minus what the tutor can't use (raw transcript, retrieval chunks, media paths).
    Steps keep the expert's narration, which covers most "what did they say about..." questions."""
    return pkg.model_dump_json(
        exclude={
            "transcript": True,
            "chunks": True,
            "embedding": True,
            "source": True,
            "steps": {"__all__": {"keyframes", "clip", "video", "t_start", "t_end"}},
            "objects": {"__all__": {"appearances"}},
        },
        exclude_none=True,
    )


async def ask(
    pkg: SkillPackage,
    step_id: str | None,
    history: list[dict],
    extra_substeps: list[str],
    pcm: bytes,
    frame: bytes | None = None,
) -> TutorReply:
    """One call per utterance: the audio goes straight in (no separate STT hop)."""
    state = {"current_step": step_id, "extra_substeps": extra_substeps, "recent_turns": history}
    note = "A scene camera photo of the work area is attached.\n" if frame else ""
    contents: list = [models.audio_pcm16k(pcm)]
    if frame:
        contents.append(models.image(frame))
    contents.append(f"{note}Learner state: {json.dumps(state)}\nThe audio is the learner's question.")
    system = f"{prompt('tutor')}\n\nSKILL PACKAGE (JSON):\n{skill_context(pkg)}"
    return await asyncio.wait_for(
        models.generate("tutor", contents, schema=TutorReply, system=system, retries=2), ASK_TIMEOUT_S
    )


async def pick_skill(skills: list[SkillPackage], pcm: bytes) -> LibraryPick:
    catalog = [{"id": s.id, "title": s.title, "summary": s.summary, "tags": s.tags} for s in skills]
    contents = [models.audio_pcm16k(pcm), f"Skills: {json.dumps(catalog)}"]
    return await asyncio.wait_for(
        models.generate("tutor", contents, schema=LibraryPick, system=prompt("library"), retries=2), ASK_TIMEOUT_S
    )


def object_lines(pkg: SkillPackage, step: Step) -> list[str]:
    """Every object in the task with what it looks like, so the VLM can tell lookalikes apart
    (a battery vs an SD card) instead of matching "small rectangle in hand" to whatever the step names."""
    roles = {o.object_id: f"{o.role}, {o.action}".rstrip(", ") for o in step.objects}
    lines = ["Objects in this task (tell them apart by these looks):"]
    for o in pkg.objects:
        aka = f" (also: {', '.join(o.aliases)})" if o.aliases else ""
        used = f" [this step: {roles[o.id]}]" if o.id in roles else " [not in this step]"
        lines.append(f"- {o.name}{aka}: {o.visual_description or 'no description'}{used}")
    return lines


async def check_scene(pkg: SkillPackage, step: Step, jpeg: bytes) -> SceneCheck:
    lines = object_lines(pkg, step) if pkg.objects else []
    lines += [f"Step: {step.title}", f"Instruction: {step.instruction}", "Success criteria:"]
    lines += [f"- {c}" for c in step.success_criteria]
    lines += ["Common mistakes:"] + [f"{i}. {m}" for i, m in enumerate(step.common_mistakes, 1)]
    return await asyncio.wait_for(
        models.generate("scene", [models.image(jpeg), "\n".join(lines)], schema=SceneCheck, system=prompt("scene")),
        SCENE_TIMEOUT_S,
    )
