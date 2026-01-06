"""
Challenge-Response handler for active verification.

The dashboard can send challenges that the node must sign to prove:
1. Liveness (node is online right now)
2. Identity (node controls the private key)

Additionally, the dashboard can send verification jobs (e.g., transcode tasks)
to test actual capabilities.
"""
from __future__ import annotations

import asyncio
import logging
import time
import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

from .identity import NodeIdentity

logger = logging.getLogger(__name__)


@dataclass
class ChallengeResponse:
    """Response to a challenge from the dashboard."""
    challenge_id: str
    node_id: str
    challenge: str
    signature: str
    timestamp: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationResult:
    """Result of a verification job (e.g., transcoding test)."""
    job_id: str
    node_id: str
    success: bool
    duration_ms: int
    output_hash: Optional[str] = None
    error: Optional[str] = None
    metrics: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)


class ChallengeHandler:
    """
    Handles challenges from the dashboard to prove liveness and capabilities.
    """

    def __init__(self, identity: NodeIdentity):
        self.identity = identity

        # Challenge nonce tracking to prevent replay
        self._seen_challenges: set[str] = set()
        self._challenge_ttl = 300  # 5 minutes

    def handle_liveness_challenge(self, challenge_id: str, challenge: str) -> ChallengeResponse:
        """
        Sign a challenge to prove we're alive and control this key.

        The challenge is typically: challenge_id + timestamp + random_bytes
        """
        # Prevent replay attacks
        if challenge_id in self._seen_challenges:
            raise ValueError(f"Challenge {challenge_id} already seen (replay attack?)")

        self._seen_challenges.add(challenge_id)

        # Sign the challenge
        signature = self.identity.sign_challenge(challenge)

        return ChallengeResponse(
            challenge_id=challenge_id,
            node_id=self.identity.node_id,
            challenge=challenge,
            signature=signature,
            timestamp=int(time.time())
        )

    async def handle_transcode_challenge(
        self,
        job_id: str,
        input_url: str,
        output_profile: str = "P720p30fps16x9",
        timeout_seconds: int = 60
    ) -> VerificationResult:
        """
        Handle a transcoding verification challenge.

        The dashboard sends a test video URL, we transcode it and return:
        - Duration (proves speed)
        - Output hash (proves we did the work)
        """
        start_time = time.time()

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                input_path = Path(tmpdir) / "input.mp4"
                output_path = Path(tmpdir) / "output.mp4"

                # Download input
                await self._download_file(input_url, input_path, timeout_seconds // 2)

                # Transcode using ffmpeg (similar to what livepeer uses)
                transcode_result = await self._transcode(
                    input_path,
                    output_path,
                    output_profile,
                    timeout_seconds // 2
                )

                if not transcode_result["success"]:
                    return VerificationResult(
                        job_id=job_id,
                        node_id=self.identity.node_id,
                        success=False,
                        duration_ms=int((time.time() - start_time) * 1000),
                        error=transcode_result.get("error", "Transcode failed")
                    )

                # Calculate output hash
                output_hash = self._hash_file(output_path)

                duration_ms = int((time.time() - start_time) * 1000)

                return VerificationResult(
                    job_id=job_id,
                    node_id=self.identity.node_id,
                    success=True,
                    duration_ms=duration_ms,
                    output_hash=output_hash,
                    metrics={
                        "input_size": input_path.stat().st_size,
                        "output_size": output_path.stat().st_size,
                        "transcode_time_ms": transcode_result.get("duration_ms", 0)
                    }
                )

        except Exception as e:
            logger.error(f"Transcode challenge failed: {e}")
            return VerificationResult(
                job_id=job_id,
                node_id=self.identity.node_id,
                success=False,
                duration_ms=int((time.time() - start_time) * 1000),
                error=str(e)
            )

    async def _download_file(self, url: str, path: Path, timeout: int):
        """Download a file from URL."""
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            path.write_bytes(response.content)

    async def _transcode(
        self,
        input_path: Path,
        output_path: Path,
        profile: str,
        timeout: int
    ) -> dict:
        """Run ffmpeg transcode."""
        # Profile to ffmpeg settings mapping
        profiles = {
            "P720p30fps16x9": ["-vf", "scale=1280:720", "-r", "30", "-c:v", "libx264", "-preset", "fast"],
            "P720p60fps16x9": ["-vf", "scale=1280:720", "-r", "60", "-c:v", "libx264", "-preset", "fast"],
            "P1080p30fps16x9": ["-vf", "scale=1920:1080", "-r", "30", "-c:v", "libx264", "-preset", "fast"],
            "P360p30fps16x9": ["-vf", "scale=640:360", "-r", "30", "-c:v", "libx264", "-preset", "fast"],
        }

        ffmpeg_args = profiles.get(profile, profiles["P720p30fps16x9"])

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            *ffmpeg_args,
            "-an",  # No audio for speed test
            str(output_path)
        ]

        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout
            )

            duration_ms = int((time.time() - start) * 1000)

            if proc.returncode != 0:
                return {
                    "success": False,
                    "error": stderr.decode()[:500],
                    "duration_ms": duration_ms
                }

            return {"success": True, "duration_ms": duration_ms}

        except asyncio.TimeoutError:
            return {"success": False, "error": "Transcode timeout"}

    def _hash_file(self, path: Path) -> str:
        """Calculate SHA256 hash of file."""
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def cleanup_old_challenges(self):
        """Remove old challenge IDs to prevent memory leak."""
        # In production, you'd track timestamps and remove old ones
        if len(self._seen_challenges) > 10000:
            self._seen_challenges.clear()
