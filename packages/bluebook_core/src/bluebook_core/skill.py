"""SkillPackage: the on-disk artifact Part 1 produces and Parts 2/3 consume.

Layout of one package (directory = skill id):

    <skills_dir>/<skill_id>/
        skill.json          # SkillPackage (this schema)
        embeddings.npy      # float32 [len(chunks), dim], L2-normalised, row i <-> chunks[i] (optional)
        media/              # keyframes (jpg), step clips (mp4), source audio (wav)
        source/             # original uploaded video(s), untouched

All paths inside skill.json are relative to the package directory.
All times are seconds from the start of the referenced source video.
Bump SCHEMA_VERSION on any breaking change and follow docs/INTEGRATION.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

# Where a piece of knowledge came from. Tutors must treat "inferred" as lower trust
# and say so to the user when it matters (safety, measurements).
Provenance = Literal["observed", "narrated", "inferred"]


class MediaRef(BaseModel):
    path: str  # relative to package dir, e.g. "media/s3_k1.jpg"
    t: float | None = None  # timestamp in source video
    video: int = 0  # index into SkillPackage.source.videos
    caption: str = ""


class Box2D(BaseModel):
    """Normalised [0,1] box in the image referenced by `media`. Origin top-left."""

    x0: float
    y0: float
    x1: float
    y1: float


class ObjectAppearance(BaseModel):
    media: MediaRef
    box: Box2D | None = None


class ObjectRef(BaseModel):
    """A physical thing that matters for the task. Part 3 uses `name`, `aliases`,
    `visual_description` and `appearances` as the detection prompt / exemplars."""

    id: str  # stable slug, e.g. "shutoff_valve"
    name: str
    aliases: list[str] = []
    category: Literal["tool", "material", "part", "fixture", "safety_gear", "other"] = "other"
    visual_description: str = ""  # colour, shape, size, typical location; for VLM grounding
    appearances: list[ObjectAppearance] = []


class StepObject(BaseModel):
    object_id: str
    # target = the thing the user must act on in this step (Part 3 highlights these)
    role: Literal["target", "tool", "material", "context"]
    action: str = ""  # e.g. "turn clockwise until it stops"


class Step(BaseModel):
    id: str  # "s1", "s2", ... stable across re-runs where possible
    index: int  # 0-based order
    title: str
    tooltip: str = Field(max_length=120)  # what the glasses show first; imperative, <=120 chars
    instruction: str  # full detail, used by the tutor when the user asks for more
    why: str = ""  # rationale; helps the tutor answer "why do I need to..."
    substeps: list[str] = []
    objects: list[StepObject] = []
    success_criteria: list[str] = []  # observable checks ("handle is perpendicular to pipe"); scene VLM uses these
    common_mistakes: list[str] = []
    tips: list[str] = []
    warnings: list[str] = []
    narration: str = ""  # what the expert actually said during this step (from transcript)
    t_start: float
    t_end: float
    video: int = 0
    keyframes: list[MediaRef] = []
    clip: MediaRef | None = None
    depends_on: list[str] = []  # step ids; empty = previous step. Enables optional / reorderable steps
    optional: bool = False
    est_duration_s: float | None = None
    provenance: Provenance = "observed"
    confidence: float = Field(default=1.0, ge=0, le=1)


class SafetyNote(BaseModel):
    text: str
    severity: Literal["info", "caution", "danger"] = "caution"
    step_ids: list[str] = []  # empty = whole task
    provenance: Provenance = "observed"


class QA(BaseModel):
    q: str
    a: str
    step_ids: list[str] = []
    provenance: Provenance = "inferred"


class TranscriptSegment(BaseModel):
    t0: float
    t1: float
    text: str
    speaker: str = ""
    video: int = 0


class Chunk(BaseModel):
    """Unit of retrieval. Row i of embeddings.npy embeds chunks[i]."""

    id: str
    kind: Literal["step", "transcript", "faq", "object", "keyframe", "safety", "overview"]
    text: str  # always present (for keyframes: a caption) so keyword fallback works
    step_id: str | None = None
    t0: float | None = None
    t1: float | None = None
    media: MediaRef | None = None


class SourceVideo(BaseModel):
    path: str  # relative, under source/
    sha256: str
    duration_s: float
    width: int
    height: int
    fps: float
    has_audio: bool


class Source(BaseModel):
    videos: list[SourceVideo]
    created_at: datetime
    pipeline_version: str
    models: dict[str, str] = {}  # role -> model id actually used


class EmbeddingInfo(BaseModel):
    model: str  # queries MUST be embedded with this same model
    dim: int
    file: str = "embeddings.npy"


class SkillPackage(BaseModel):
    schema_version: str = SCHEMA_VERSION
    id: str  # slug, == directory name
    title: str
    summary: str
    tags: list[str] = []
    difficulty: Literal["beginner", "intermediate", "advanced"] = "beginner"
    est_duration_s: float | None = None
    prerequisites: list[str] = []
    safety: list[SafetyNote] = []
    objects: list[ObjectRef] = []  # every tool/material/part referenced by any step
    steps: list[Step]
    faq: list[QA] = []
    transcript: list[TranscriptSegment] = []
    chunks: list[Chunk] = []
    embedding: EmbeddingInfo | None = None
    source: Source

    def step(self, step_id: str) -> Step:
        return next(s for s in self.steps if s.id == step_id)

    def object(self, object_id: str) -> ObjectRef:
        return next(o for o in self.objects if o.id == object_id)

    def check(self) -> list[str]:
        """Referential-integrity problems pydantic can't see. Empty list = valid."""
        errs = []
        obj_ids = {o.id for o in self.objects}
        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            errs.append("duplicate step ids")
        if [s.index for s in self.steps] != list(range(len(self.steps))):
            errs.append("step indices must be 0..n-1 in order")
        for s in self.steps:
            errs += [f"{s.id}: unknown object {o.object_id}" for o in s.objects if o.object_id not in obj_ids]
            errs += [f"{s.id}: unknown dependency {d}" for d in s.depends_on if d not in step_ids]
            if s.t_end < s.t_start:
                errs.append(f"{s.id}: t_end < t_start")
        for c in self.chunks:
            if c.step_id and c.step_id not in step_ids:
                errs.append(f"chunk {c.id}: unknown step {c.step_id}")
        return errs
