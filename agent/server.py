"""
Agent HTTP server - exposes endpoints for challenge-response verification.

This runs alongside the heartbeat publisher and allows the dashboard
to actively verify this node's capabilities.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .identity import NodeIdentity, get_identity
from .capabilities import probe_capabilities
from .heartbeat import HeartbeatPublisher
from .challenge import ChallengeHandler

logger = logging.getLogger(__name__)


# Request/Response models
class LivenessChallenge(BaseModel):
    challenge_id: str
    challenge: str


class TranscodeChallenge(BaseModel):
    job_id: str
    input_url: str
    output_profile: str = "P720p30fps16x9"
    timeout_seconds: int = 60


class StatusResponse(BaseModel):
    node_id: str
    status: str
    heartbeats_sent: int
    last_heartbeat: Optional[str]
    uptime_seconds: int


# Global state (initialized in lifespan)
_identity: Optional[NodeIdentity] = None
_heartbeat_publisher: Optional[HeartbeatPublisher] = None
_challenge_handler: Optional[ChallengeHandler] = None
_start_time: float = 0


def create_app(
    dashboard_url: str = "http://localhost:8080",
    heartbeat_interval: int = 60,
    identity: Optional[NodeIdentity] = None
) -> FastAPI:
    """Create the agent FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _identity, _heartbeat_publisher, _challenge_handler, _start_time
        import time

        _start_time = time.time()

        # Initialize identity
        _identity = identity or get_identity()
        logger.info(f"Node ID: {_identity.node_id}")

        # Initialize challenge handler
        _challenge_handler = ChallengeHandler(_identity)

        # Initialize and start heartbeat publisher
        _heartbeat_publisher = HeartbeatPublisher(
            identity=_identity,
            dashboard_url=dashboard_url,
            interval_seconds=heartbeat_interval
        )

        # Start heartbeat in background
        await _heartbeat_publisher.start()

        yield

        # Cleanup
        await _heartbeat_publisher.stop()

    app = FastAPI(
        title="Livepeer SLA Agent",
        description="Attestation agent for Livepeer orchestrators",
        version="0.1.0",
        lifespan=lifespan
    )

    @app.get("/")
    async def root():
        """Health check endpoint."""
        return {"status": "ok", "agent": "livepeer-sla-agent"}

    @app.get("/status", response_model=StatusResponse)
    async def get_status():
        """Get agent status."""
        import time
        return StatusResponse(
            node_id=_identity.node_id,
            status="running",
            heartbeats_sent=_heartbeat_publisher.heartbeats_sent,
            last_heartbeat=_heartbeat_publisher.last_heartbeat.isoformat() if _heartbeat_publisher.last_heartbeat else None,
            uptime_seconds=int(time.time() - _start_time)
        )

    @app.get("/capabilities")
    async def get_capabilities():
        """Get current node capabilities (unsigned, for debugging)."""
        caps = probe_capabilities()
        return caps.to_dict()

    @app.get("/attestation")
    async def get_signed_attestation():
        """Get a fresh signed capability attestation."""
        caps = probe_capabilities()
        attestation = _identity.sign_attestation({
            "type": "capability_snapshot",
            "capabilities": caps.to_dict()
        })
        return {
            "node_id": attestation.node_id,
            "timestamp": attestation.timestamp,
            "payload": attestation.payload,
            "signature": attestation.signature
        }

    @app.post("/challenge/liveness")
    async def handle_liveness_challenge(challenge: LivenessChallenge):
        """
        Handle a liveness challenge from the dashboard.

        The dashboard sends a random challenge, we sign it to prove we're alive.
        """
        try:
            response = _challenge_handler.handle_liveness_challenge(
                challenge.challenge_id,
                challenge.challenge
            )
            return response.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/challenge/transcode")
    async def handle_transcode_challenge(challenge: TranscodeChallenge):
        """
        Handle a transcoding verification challenge.

        The dashboard sends a test video, we transcode it and prove capability.
        """
        result = await _challenge_handler.handle_transcode_challenge(
            job_id=challenge.job_id,
            input_url=challenge.input_url,
            output_profile=challenge.output_profile,
            timeout_seconds=challenge.timeout_seconds
        )
        return result.to_dict()

    @app.post("/heartbeat/force")
    async def force_heartbeat():
        """Force an immediate heartbeat (for testing)."""
        attestation = await _heartbeat_publisher.send_heartbeat()
        return {
            "success": True,
            "attestation": {
                "node_id": attestation.node_id,
                "timestamp": attestation.timestamp,
                "signature": attestation.signature[:32] + "..."
            }
        }

    return app


def run_server(
    host: str = "0.0.0.0",
    port: int = 9090,
    dashboard_url: str = "http://localhost:8080",
    heartbeat_interval: int = 60
):
    """Run the agent server."""
    import uvicorn

    app = create_app(
        dashboard_url=dashboard_url,
        heartbeat_interval=heartbeat_interval
    )

    uvicorn.run(app, host=host, port=port)
