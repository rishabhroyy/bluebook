"""Part 2 — library + teaching mode. Spec: workstreams/part2-teaching.md"""

from bluebook_core import glasses
from fastapi import FastAPI

from .controller import TeachController

__all__ = ["TeachController", "setup"]


def setup(app: FastAPI):
    """Register the teaching Controller and a debug route. Called once by bluebook_core.server."""
    glasses.set_controller(TeachController)

    @app.get("/teach/sessions")
    def teach_sessions():
        """Live lesson state per connected glasses (debugging in the simulator)."""
        return {sid: conn.state for sid, conn in glasses.sessions.items()}
