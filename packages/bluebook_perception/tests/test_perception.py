"""Unit tests for bluebook_perception pure components.

No network, no model calls, no ffmpeg — all external calls are faked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import numpy as np
from bluebook_core import store
from bluebook_core.skill import (
    QA,
    Box2D,
    SafetyNote,
    SkillPackage,
    Source,
    SourceVideo,
    Step,
    StepObject,
    TranscriptSegment,
)

# ─────────────────────────── helpers ────────────────────────────────────────


def _make_pkg(skill_id: str = "test-skill", n_steps: int = 2) -> SkillPackage:
    steps = [
        Step(
            id=f"s{i + 1}",
            index=i,
            title=f"Step {i + 1}",
            tooltip=f"Do step {i + 1}.",
            instruction=f"Full instruction for step {i + 1}.",
            t_start=float(i * 30),
            t_end=float((i + 1) * 30),
            provenance="narrated",
        )
        for i in range(n_steps)
    ]
    return SkillPackage(
        id=skill_id,
        title="Test Skill",
        summary="A test skill.",
        steps=steps,
        source=Source(
            videos=[
                SourceVideo(
                    path="source/v.mp4",
                    sha256="abc",
                    duration_s=60.0,
                    width=1920,
                    height=1080,
                    fps=30.0,
                    has_audio=True,
                )
            ],
            created_at=datetime.now(UTC),
            pipeline_version="ingest-v1",
        ),
    )


# ─────────────────────── cache tests ────────────────────────────────────────


def test_cache_save_and_load(tmp_path):
    from bluebook_perception.cache import StageCache

    with patch("bluebook_perception.cache.config") as mock_cfg:
        mock_cfg.data_dir = tmp_path
        cache = StageCache("test-skill")
        cache.save("ingest", "hash123", {"foo": "bar"})
        result = cache.load("ingest", "hash123")
        assert result == {"foo": "bar"}


def test_cache_hash_mismatch_returns_none(tmp_path):
    from bluebook_perception.cache import StageCache

    with patch("bluebook_perception.cache.config") as mock_cfg:
        mock_cfg.data_dir = tmp_path
        cache = StageCache("test-skill")
        cache.save("ingest", "hash_old", {"data": 1})
        assert cache.load("ingest", "hash_new") is None


def test_force_stage_invalidates_downstream(tmp_path):
    from bluebook_perception.cache import StageCache

    with patch("bluebook_perception.cache.config") as mock_cfg:
        mock_cfg.data_dir = tmp_path
        # pre-populate several stages
        setup_cache = StageCache("test-skill")
        for stage in ("ingest", "transcribe", "outline", "detail"):
            setup_cache.save(stage, "h", {"stage": stage})

        # force from outline onwards
        cache = StageCache("test-skill", force_stage="outline")
        assert cache.load("ingest", "h") == {"stage": "ingest"}  # untouched
        assert cache.load("transcribe", "h") == {"stage": "transcribe"}  # untouched
        assert cache.load("outline", "h") is None  # invalidated
        assert cache.load("detail", "h") is None  # invalidated (downstream)


# ─────────────────────── box conversion tests ────────────────────────────────


def test_gemini_box_to_box2d_converts_correctly():
    from bluebook_perception.media import gemini_box_to_box2d

    # Gemini returns [ymin, xmin, ymax, xmax] / 1000
    box = gemini_box_to_box2d([100, 200, 400, 600])
    assert abs(box.x0 - 0.2) < 1e-6
    assert abs(box.y0 - 0.1) < 1e-6
    assert abs(box.x1 - 0.6) < 1e-6
    assert abs(box.y1 - 0.4) < 1e-6


def test_gemini_box_to_box2d_full_frame():
    from bluebook_perception.media import gemini_box_to_box2d

    box = gemini_box_to_box2d([0, 0, 1000, 1000])
    assert box == Box2D(x0=0.0, y0=0.0, x1=1.0, y1=1.0)


# ─────────────────────── keyframe sharpness tests ────────────────────────────


def test_laplacian_variance_prefers_sharp_image():
    from bluebook_perception.media import _laplacian_variance
    from PIL import Image

    # sharp: checkerboard pattern
    sharp = Image.fromarray(np.tile(np.array([[0, 255], [255, 0]], dtype=np.uint8), (50, 50)))
    # blurry: uniform grey
    blurry = Image.fromarray(np.full((100, 100), 128, dtype=np.uint8))
    assert _laplacian_variance(sharp) > _laplacian_variance(blurry)


# ─────────────────────── chunking tests ──────────────────────────────────────


def test_step_chunks_one_per_step():
    from bluebook_perception.stages.index import _step_chunks

    pkg = _make_pkg(n_steps=3)
    chunks = _step_chunks(pkg)
    step_chunks = [c for c in chunks if c.kind == "step" and "_sc" not in c.id]
    assert len(step_chunks) == 3
    assert all(c.kind == "step" for c in step_chunks)


def test_overview_chunk_contains_title():
    from bluebook_perception.stages.index import _overview_chunk

    pkg = _make_pkg()
    chunk = _overview_chunk(pkg)
    assert chunk.kind == "overview"
    assert "Test Skill" in chunk.text


def test_transcript_windows_coverage():
    from bluebook_perception.stages.index import _transcript_windows

    pkg = _make_pkg()
    pkg = pkg.model_copy(
        update={
            "transcript": [
                TranscriptSegment(t0=0.0, t1=5.0, text="First words."),
                TranscriptSegment(t0=5.0, t1=35.0, text="More words."),
                TranscriptSegment(t0=35.0, t1=65.0, text="Final words."),
            ]
        }
    )
    chunks = _transcript_windows(pkg, window_s=30.0, step_s=15.0)
    assert len(chunks) >= 2
    assert all(c.kind == "transcript" for c in chunks)


def test_faq_chunks():
    from bluebook_perception.stages.index import _faq_chunks

    pkg = _make_pkg()
    pkg = pkg.model_copy(
        update={
            "faq": [
                QA(q="Why?", a="Because.", step_ids=["s1"]),
                QA(q="How?", a="Like this."),
            ]
        }
    )
    chunks = _faq_chunks(pkg)
    assert len(chunks) == 2
    assert all(c.kind == "faq" for c in chunks)
    assert "Why?" in chunks[0].text


def test_safety_chunks():
    from bluebook_perception.stages.index import _safety_chunks

    pkg = _make_pkg()
    pkg = pkg.model_copy(
        update={
            "safety": [
                SafetyNote(text="Don't over-tighten.", severity="caution", step_ids=["s1"]),
            ]
        }
    )
    chunks = _safety_chunks(pkg)
    assert len(chunks) == 1
    assert "CAUTION" in chunks[0].text


# ─────────────────────── eval harness tests ──────────────────────────────────


def test_boundary_f1_perfect():
    from bluebook_perception.eval.harness import boundary_f1

    pred = [0.0, 30.0, 60.0]
    ref = [0.0, 30.0, 60.0]
    result = boundary_f1(pred, ref)
    assert result["f1"] == 1.0


def test_boundary_f1_within_tolerance():
    from bluebook_perception.eval.harness import boundary_f1

    pred = [1.5, 31.0]  # within ±3s of ref
    ref = [0.0, 30.0]
    result = boundary_f1(pred, ref, tol=3.0)
    assert result["f1"] == 1.0


def test_boundary_f1_no_match():
    from bluebook_perception.eval.harness import boundary_f1

    pred = [100.0, 200.0]
    ref = [0.0, 30.0]
    result = boundary_f1(pred, ref)
    assert result["f1"] == 0.0


def test_boundary_f1_empty_predicted():
    from bluebook_perception.eval.harness import boundary_f1

    result = boundary_f1([], [0.0, 30.0])
    assert result["f1"] == 0.0


# ─────────────────────── step times vs video length ──────────────────────────


def test_clamp_steps_keeps_times_inside_the_video():
    from bluebook_perception.pipeline import _clamp_steps
    from bluebook_perception.schemas import OutlineStep

    steps = [
        OutlineStep(title="a", t_start=0, t_end=60),
        OutlineStep(title="b", t_start=60.5, t_end=140),
        OutlineStep(title="c", t_start=141.5, t_end=157),
    ]
    out = _clamp_steps(steps, 127.5)
    assert [(s.title, s.t_start, s.t_end) for s in out] == [("a", 0, 60), ("b", 60.5, 127.5)]


# ─────────────────────── package validity ────────────────────────────────────


def test_assembled_package_check_passes():
    pkg = _make_pkg()
    assert pkg.check() == []


def test_package_check_catches_bad_object_ref():
    pkg = _make_pkg()
    steps = list(pkg.steps)
    steps[0] = steps[0].model_copy(update={"objects": [StepObject(object_id="nonexistent", role="target")]})
    pkg2 = pkg.model_copy(update={"steps": steps})
    assert any("unknown object" in e for e in pkg2.check())


def test_package_save_roundtrip(tmp_path):
    pkg = _make_pkg()
    store.save(pkg, root=tmp_path)
    loaded = store.load(pkg.id, tmp_path)
    assert loaded.id == pkg.id
    assert len(loaded.steps) == len(pkg.steps)
    assert loaded.check() == []
