"""Pydantic response schemas for models.generate(schema=...) calls.

These are intentionally minimal subsets of skill.py models — only the fields
the model needs to fill in each pass. The pipeline assembles the final SkillPackage
from these fragments.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------------ outline (stage 3)


class OutlineObject(BaseModel):
    id: str
    name: str
    aliases: list[str] = []
    category: Literal["tool", "material", "part", "fixture", "safety_gear", "other"] = "other"
    visual_description: str = ""


class OutlineSafetyNote(BaseModel):
    text: str
    severity: Literal["info", "caution", "danger"] = "caution"


class OutlineStep(BaseModel):
    title: str
    t_start: float
    t_end: float
    summary: str = ""


class OutlineResult(BaseModel):
    title: str
    summary: str
    tags: list[str] = []
    difficulty: Literal["beginner", "intermediate", "advanced"] = "beginner"
    steps: list[OutlineStep]
    objects: list[OutlineObject] = []
    safety: list[OutlineSafetyNote] = []
    audio_cues: list[str] = []  # non-speech audio cues


# ------------------------------------------------------------------ detail (stage 4)


class DetailObject(BaseModel):
    object_id: str
    role: Literal["target", "tool", "material", "context"]
    action: str = ""


class DetailStep(BaseModel):
    title: str
    tooltip: str = Field(max_length=120)
    instruction: str
    why: str = ""
    substeps: list[str] = []
    objects: list[DetailObject] = []
    success_criteria: list[str] = []
    common_mistakes: list[str] = []
    tips: list[str] = []
    warnings: list[str] = []
    narration: str = ""
    keyframe_times: list[float] = []  # 1–3 timestamps for best keyframes
    est_duration_s: float | None = None


# ------------------------------------------------------------------ keyframe grounding (stage 5)


class GroundingResult(BaseModel):
    """Gemini box_2d grounding for one object in one keyframe."""

    object_id: str
    box: list[float] | None = None  # [ymin, xmin, ymax, xmax] / 1000, or null if not visible


# ------------------------------------------------------------------ synthesize (stage 6)


class SynthQA(BaseModel):
    q: str
    a: str
    step_ids: list[str] = []


class SynthResult(BaseModel):
    prerequisites: list[str] = []
    faq: list[SynthQA] = []
    est_duration_s: float | None = None


# ------------------------------------------------------------------ verify (stage 7)


class VerifyStep(BaseModel):
    step_id: str
    provenance: Literal["observed", "narrated", "inferred"] = "observed"
    confidence: float = Field(default=1.0, ge=0, le=1)
    issues: list[str] = []  # claims to flag or drop
    revised_tooltip: str | None = None  # if tooltip needs correction
    revised_instruction: str | None = None


class VerifySafety(BaseModel):
    index: int  # position in safety list
    provenance: Literal["observed", "narrated", "inferred"] = "observed"
    confidence: float = Field(default=1.0, ge=0, le=1)
    drop: bool = False  # true = unsupported, remove from package


class VerifyResult(BaseModel):
    steps: list[VerifyStep] = []
    safety: list[VerifySafety] = []
    critique: str = ""  # summary of issues found


# ------------------------------------------------------------------ contextual index (stage 8)


class ChunkContext(BaseModel):
    """LLM-generated 1–2 sentence situating context for one chunk."""

    chunk_id: str
    context: str
