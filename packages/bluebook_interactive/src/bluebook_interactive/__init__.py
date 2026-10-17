"""Part 3 (experimental) — interactive AR highlighting. Spec: workstreams/part3-interactive.md
Mounted only when BB_INTERACTIVE=1."""

from fastapi import FastAPI


def setup(app: FastAPI):
    """Add calibration/debug routes; return the async tracking loop (core runs it as a background task)."""
