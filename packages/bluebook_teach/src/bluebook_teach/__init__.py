"""Part 2 — library + teaching mode. Spec: workstreams/part2-teaching.md"""

from bluebook_core import glasses
from fastapi import FastAPI, HTTPException

from .controller import TeachController
from .spike import SpikeController

__all__ = ["TeachController", "setup"]


def setup(app: FastAPI):
    """Register the teaching Controller and two dev routes. Called once by bluebook_core.server."""
    glasses.set_controller(TeachController)

    @app.get("/teach/sessions")
    def teach_sessions():
        """Live lesson state per connected glasses (debugging in the simulator)."""
        return {sid: conn.state for sid, conn in glasses.sessions.items()}

    @app.post("/teach/spike/{session}")
    async def teach_spike(session: str):
        """Show the hardware spike pages on these glasses (docs/spikes.md). Double taps return to the lesson."""
        if not (conn := glasses.sessions.get(session)):
            raise HTTPException(404, f"no glasses connected as {session!r}; see GET /health")
        await conn.push(SpikeController())
        return {"ok": True}
