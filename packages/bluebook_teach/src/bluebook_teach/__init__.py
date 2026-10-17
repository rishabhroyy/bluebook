"""Part 2 — library + teaching mode. Spec: workstreams/part2-teaching.md"""

from fastapi import FastAPI


def setup(app: FastAPI):
    """Register the teaching Controller: bluebook_core.glasses.set_controller(TeachController)."""
