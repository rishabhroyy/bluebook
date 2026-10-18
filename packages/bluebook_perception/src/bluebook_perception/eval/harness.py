"""Eval harness: step-boundary F1 @ ±3s and tooltip actionability report.

Usage:
    uv run python -m bluebook_perception.eval.harness <skill_id> [skill_id ...]

Labels file: packages/bluebook_perception/eval/<id>.labels.json
Format: {"steps": [{"t_start": 0.0, "t_end": 20.0, "title": "..."}]}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bluebook_core import store

LABELS_DIR = Path(__file__).parent
TOLERANCE = 3.0  # seconds


def boundary_f1(predicted: list[float], reference: list[float], tol: float = TOLERANCE) -> dict:
    """Compute F1 for step boundary detection at ±tol seconds."""
    tp = 0
    matched_ref = set()
    for p in predicted:
        for i, r in enumerate(reference):
            if i not in matched_ref and abs(p - r) <= tol:
                tp += 1
                matched_ref.add(i)
                break
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(reference) if reference else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def evaluate(skill_id: str) -> dict:
    labels_path = LABELS_DIR / f"{skill_id}.labels.json"
    if not labels_path.exists():
        return {"skill_id": skill_id, "error": f"no labels at {labels_path}"}

    labels = json.loads(labels_path.read_text())
    ref_steps = labels.get("steps", [])
    ref_starts = [s["t_start"] for s in ref_steps]
    ref_ends = [s["t_end"] for s in ref_steps]

    pkg = store.load(skill_id)
    pred_starts = [s.t_start for s in pkg.steps]
    pred_ends = [s.t_end for s in pkg.steps]

    start_metrics = boundary_f1(pred_starts, ref_starts)
    end_metrics = boundary_f1(pred_ends, ref_ends)
    avg_f1 = (start_metrics["f1"] + end_metrics["f1"]) / 2

    # tooltip quality: check length and imperative form (starts with verb)
    tooltip_issues = []
    for s in pkg.steps:
        if len(s.tooltip) > 120:
            tooltip_issues.append(f"{s.id}: tooltip too long ({len(s.tooltip)} chars)")
        if s.tooltip and not s.tooltip[0].isupper():
            tooltip_issues.append(f"{s.id}: tooltip not capitalized")

    return {
        "skill_id": skill_id,
        "n_steps_predicted": len(pkg.steps),
        "n_steps_reference": len(ref_steps),
        "boundary_f1_starts": start_metrics,
        "boundary_f1_ends": end_metrics,
        "avg_boundary_f1": round(avg_f1, 3),
        "target_met": avg_f1 >= 0.7,
        "tooltip_issues": tooltip_issues,
        "n_chunks": len(pkg.chunks),
        "has_embeddings": pkg.embedding is not None,
    }


def main() -> None:
    skill_ids = sys.argv[1:]
    if not skill_ids:
        print("Usage: python -m bluebook_perception.eval.harness <skill_id> [...]")
        sys.exit(1)

    all_pass = True
    for sid in skill_ids:
        result = evaluate(sid)
        print(json.dumps(result, indent=2))
        if not result.get("target_met", False):
            all_pass = False

    if not all_pass:
        print("\nWARNING: one or more skills did not meet F1 >= 0.7 target")
        sys.exit(1)
    else:
        print("\nAll skills met the F1 >= 0.7 target")


if __name__ == "__main__":
    main()
