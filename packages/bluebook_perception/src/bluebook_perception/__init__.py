"""Part 1 — perception/learning pipeline. Spec: workstreams/part1-perception.md"""

from fastapi import FastAPI


def setup(app: FastAPI):
    """Mount ingest routes (e.g. POST /ingest). Called once by bluebook_core.server."""
