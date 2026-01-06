"""
Active verification system - sends challenges to nodes to verify capabilities.

Instead of trusting what nodes report, we test them.
"""
from __future__ import annotations

import asyncio
import secrets
import time
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

import httpx

from .storage import get_storage
from .models import VerificationJob

logger = logging.getLogger(__name__)

# Test video URL for transcode verification (small, public domain clip)
DEFAULT_TEST_VIDEO = "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4"


@dataclass
class NodeEndpoint:
    """Known endpoint for a node agent."""
    node_id: str
    url: str  # e.g., "http://192.168.1.100:9090"


class Verifier:
    """
    Sends verification challenges to nodes to test their capabilities.

    Two types of challenges:
    1. Liveness: Sign a random challenge to prove the node is online
    2. Transcode: Actually transcode a video to prove capability
    """

    def __init__(self, test_video_url: str = DEFAULT_TEST_VIDEO):
        self.test_video_url = test_video_url
        self._client: Optional[httpx.AsyncClient] = None

        # Node endpoint registry (in production, nodes would register these)
        self._endpoints: dict[str, NodeEndpoint] = {}

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=120.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    def register_endpoint(self, node_id: str, url: str):
        """Register a node's agent endpoint."""
        self._endpoints[node_id] = NodeEndpoint(node_id=node_id, url=url.rstrip("/"))

    def get_endpoint(self, node_id: str) -> Optional[str]:
        """Get a node's registered endpoint URL."""
        ep = self._endpoints.get(node_id)
        return ep.url if ep else None

    async def verify_liveness(self, node_id: str) -> dict:
        """
        Send a liveness challenge to a node.

        Returns verification result with timing info.
        """
        endpoint = self.get_endpoint(node_id)
        if not endpoint:
            return {
                "success": False,
                "error": "No endpoint registered for node",
                "node_id": node_id
            }

        # Generate random challenge
        challenge_id = secrets.token_hex(16)
        challenge = f"{challenge_id}:{int(time.time())}:{secrets.token_hex(32)}"

        start_time = time.time()

        try:
            response = await self._client.post(
                f"{endpoint}/challenge/liveness",
                json={
                    "challenge_id": challenge_id,
                    "challenge": challenge
                }
            )
            response.raise_for_status()
            result = response.json()

            duration_ms = int((time.time() - start_time) * 1000)

            # Verify the signature
            from agent.identity import NodeIdentity
            signature_valid = NodeIdentity.verify_challenge_response(
                node_id=result["node_id"],
                challenge=challenge,
                signature=result["signature"]
            )

            # Record the job
            storage = get_storage()
            job = VerificationJob(
                job_id=challenge_id,
                node_id=node_id,
                job_type="liveness",
                created_at=datetime.utcnow(),
                completed_at=datetime.utcnow(),
                success=signature_valid,
                duration_ms=duration_ms,
                result={"signature_valid": signature_valid}
            )
            storage.record_verification_job(job)

            return {
                "success": signature_valid,
                "node_id": node_id,
                "challenge_id": challenge_id,
                "duration_ms": duration_ms,
                "signature_valid": signature_valid
            }

        except httpx.HTTPError as e:
            logger.error(f"Liveness check failed for {node_id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "node_id": node_id,
                "duration_ms": int((time.time() - start_time) * 1000)
            }

    async def verify_transcode(
        self,
        node_id: str,
        profile: str = "P720p30fps16x9",
        timeout: int = 60
    ) -> dict:
        """
        Send a transcoding challenge to a node.

        This actually tests their capability by having them transcode a video.
        """
        endpoint = self.get_endpoint(node_id)
        if not endpoint:
            return {
                "success": False,
                "error": "No endpoint registered for node",
                "node_id": node_id
            }

        job_id = secrets.token_hex(16)
        start_time = time.time()

        # Record job start
        storage = get_storage()
        job = VerificationJob(
            job_id=job_id,
            node_id=node_id,
            job_type="transcode",
            created_at=datetime.utcnow()
        )
        storage.record_verification_job(job)

        try:
            response = await self._client.post(
                f"{endpoint}/challenge/transcode",
                json={
                    "job_id": job_id,
                    "input_url": self.test_video_url,
                    "output_profile": profile,
                    "timeout_seconds": timeout
                },
                timeout=timeout + 10  # Extra buffer
            )
            response.raise_for_status()
            result = response.json()

            total_duration = int((time.time() - start_time) * 1000)

            # Update job record
            storage.update_verification_job(
                job_id,
                completed_at=datetime.utcnow(),
                success=result.get("success", False),
                duration_ms=result.get("duration_ms", total_duration),
                result=result
            )

            # Update node verification score based on results
            if result.get("success"):
                # Score based on speed (faster = better)
                transcode_ms = result.get("duration_ms", 10000)
                speed_score = max(0, 100 - (transcode_ms / 100))  # Rough scoring
                storage.update_verification_score(node_id, speed_score)

            return {
                "success": result.get("success", False),
                "node_id": node_id,
                "job_id": job_id,
                "duration_ms": result.get("duration_ms"),
                "output_hash": result.get("output_hash"),
                "metrics": result.get("metrics"),
                "error": result.get("error")
            }

        except httpx.HTTPError as e:
            logger.error(f"Transcode verification failed for {node_id}: {e}")
            storage.update_verification_job(
                job_id,
                completed_at=datetime.utcnow(),
                success=False,
                result={"error": str(e)}
            )
            return {
                "success": False,
                "error": str(e),
                "node_id": node_id,
                "job_id": job_id
            }

    async def verify_all_nodes(self, challenge_type: str = "liveness") -> list[dict]:
        """Verify all registered nodes."""
        results = []
        for node_id in self._endpoints:
            if challenge_type == "liveness":
                result = await self.verify_liveness(node_id)
            else:
                result = await self.verify_transcode(node_id)
            results.append(result)
        return results


# Global verifier instance
_verifier: Optional[Verifier] = None


def get_verifier() -> Verifier:
    """Get global verifier instance."""
    global _verifier
    if _verifier is None:
        _verifier = Verifier()
    return _verifier
