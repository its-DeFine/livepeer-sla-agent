"""
Agent HTTP server - exposes endpoints for challenge-response verification.

This runs alongside the heartbeat publisher and allows the dashboard
to actively verify this node's capabilities.
"""
from __future__ import annotations

import asyncio
import logging
import os
import hmac
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

from .identity import NodeIdentity, get_identity, create_endpoint_registration_message
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


class GPUBenchmarkChallenge(BaseModel):
    challenge_id: str
    seed: int
    benchmark_type: str = "matrix_4096"  # matrix_4096, matrix_8192, transcode_720p, etc.
    gpu_index: Optional[int] = None  # None = test all GPUs
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


def _enforce_challenge_token(provided_token: Optional[str]) -> None:
    """Optionally require a shared token for challenge endpoints."""
    expected = os.environ.get("SLA_CHALLENGE_TOKEN")
    if not expected:
        return
    if not provided_token or not hmac.compare_digest(provided_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


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

        # Best-effort endpoint auto-registration (optional)
        agent_public_url = os.environ.get("AGENT_PUBLIC_URL", "").strip().rstrip("/")
        if agent_public_url:
            try:
                import httpx

                ts = int(time.time())
                message = create_endpoint_registration_message(_identity.node_id, agent_public_url, ts)
                signature = _identity.sign_challenge(message)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"{dashboard_url.rstrip('/')}/api/v1/nodes/register-endpoint",
                        json={
                            "node_id": _identity.node_id,
                            "agent_url": agent_public_url,
                            "timestamp": ts,
                            "signature": signature,
                        },
                    )
                    resp.raise_for_status()
                logger.info(f"Registered endpoint with dashboard: {agent_public_url}")
            except Exception as e:
                logger.warning(f"Failed to register endpoint with dashboard: {e}")
        else:
            logger.info("AGENT_PUBLIC_URL not set; skipping endpoint auto-registration")

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
    async def handle_liveness_challenge(
        challenge: LivenessChallenge,
        x_sla_token: Optional[str] = Header(default=None),
    ):
        """
        Handle a liveness challenge from the dashboard.

        The dashboard sends a random challenge, we sign it to prove we're alive.
        """
        _enforce_challenge_token(x_sla_token)
        try:
            response = _challenge_handler.handle_liveness_challenge(
                challenge.challenge_id,
                challenge.challenge
            )
            return response.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/challenge/transcode")
    async def handle_transcode_challenge(
        challenge: TranscodeChallenge,
        x_sla_token: Optional[str] = Header(default=None),
    ):
        """
        Handle a transcoding verification challenge.

        The dashboard sends a test video, we transcode it and prove capability.
        """
        _enforce_challenge_token(x_sla_token)
        result = await _challenge_handler.handle_transcode_challenge(
            job_id=challenge.job_id,
            input_url=challenge.input_url,
            output_profile=challenge.output_profile,
            timeout_seconds=challenge.timeout_seconds
        )
        return result.to_dict()

    @app.post("/challenge/gpu-benchmark")
    async def handle_gpu_benchmark_challenge(
        challenge: GPUBenchmarkChallenge,
        x_sla_token: Optional[str] = Header(default=None),
    ):
        """
        Handle a GPU benchmark challenge.

        This proves actual GPU capability through timed compute tasks.
        The benchmark result includes:
        - Timing (proves GPU speed)
        - Result hash (proves correct computation)
        - Signature (proves authenticity)

        Benchmark types:
        - matrix_4096: 4096x4096 matrix multiply (quick)
        - matrix_8192: 8192x8192 matrix multiply (thorough)
        - transcode_720p: 720p video transcode
        - transcode_1080p: 1080p video transcode
        """
        _enforce_challenge_token(x_sla_token)
        try:
            response = await _challenge_handler.handle_gpu_benchmark_challenge(
                challenge_id=challenge.challenge_id,
                seed=challenge.seed,
                benchmark_type=challenge.benchmark_type,
                gpu_index=challenge.gpu_index,
                timeout_seconds=challenge.timeout_seconds
            )
            return response.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/gpu-info")
    async def get_gpu_info():
        """Get GPU information and benchmark capabilities."""
        from .gpu_benchmark import get_benchmarker
        from .gpu_profiles import GPU_PROFILES, BENCHMARK_TYPES

        benchmarker = get_benchmarker()

        return {
            "gpu_count": benchmarker.gpu_count,
            "gpus": [
                benchmarker.get_gpu_info(i)
                for i in range(benchmarker.gpu_count)
            ],
            "cuda_available": benchmarker._cuda_available,
            "torch_available": benchmarker._torch_available,
            "supported_benchmarks": list(BENCHMARK_TYPES.keys()),
            "known_gpu_profiles": list(GPU_PROFILES.keys())
        }

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
