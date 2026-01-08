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
import os
import socket
import ipaddress
from urllib.parse import urlparse
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

from .identity import NodeIdentity
from .gpu_benchmark import get_benchmarker, GPUBenchmarkChallenge, GPUBenchmarkResponse, BenchmarkResult

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
            unsafe_ok = os.environ.get("ALLOW_UNSAFE_INPUT_URLS", "").strip().lower() in {"1", "true", "yes"}
            if not unsafe_ok:
                ok, reason = self._validate_input_url(input_url)
                if not ok:
                    return VerificationResult(
                        job_id=job_id,
                        node_id=self.identity.node_id,
                        success=False,
                        duration_ms=int((time.time() - start_time) * 1000),
                        error=f"Unsafe input_url: {reason}",
                    )

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

    @staticmethod
    def _validate_input_url(url: str) -> tuple[bool, str]:
        """Basic SSRF guard for transcode input URLs."""
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "invalid url"

        if parsed.scheme not in {"http", "https"}:
            return False, "unsupported scheme"
        if not parsed.hostname:
            return False, "missing hostname"
        if parsed.username or parsed.password:
            return False, "userinfo not allowed"

        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception:
            return False, "dns resolution failed"

        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return False, f"disallowed ip {ip}"

        return True, "ok"

    async def _download_file(self, url: str, path: Path, timeout: int):
        """Download a file from URL."""
        import httpx
        max_bytes = int(os.environ.get("MAX_CHALLENGE_DOWNLOAD_BYTES", str(50 * 1024 * 1024)))
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_len = response.headers.get("content-length")
                if content_len and content_len.isdigit() and int(content_len) > max_bytes:
                    raise ValueError(f"Input file too large (content-length={content_len})")

                total = 0
                with open(path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Input file too large")
                        f.write(chunk)

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

    async def handle_gpu_benchmark_challenge(
        self,
        challenge_id: str,
        seed: int,
        benchmark_type: str = "matrix_4096",
        gpu_index: Optional[int] = None,
        timeout_seconds: int = 60
    ) -> GPUBenchmarkResponse:
        """
        Handle a GPU benchmark challenge.

        This proves actual GPU capability through timed compute tasks.
        The result hash proves correct computation, timing proves GPU speed.

        Args:
            challenge_id: Unique challenge identifier
            seed: Random seed for deterministic benchmark
            benchmark_type: Type of benchmark (matrix_4096, transcode_720p, etc.)
            gpu_index: Specific GPU to test, or None to test all GPUs
            timeout_seconds: Maximum time for benchmark

        Returns:
            GPUBenchmarkResponse with results and signature
        """
        # Prevent replay attacks
        if challenge_id in self._seen_challenges:
            raise ValueError(f"Challenge {challenge_id} already seen (replay attack?)")

        self._seen_challenges.add(challenge_id)

        start_time = time.time()
        benchmarker = get_benchmarker()

        results: list[BenchmarkResult] = []

        if gpu_index is not None:
            # Test specific GPU
            result = await benchmarker.run_benchmark(
                benchmark_type=benchmark_type,
                seed=seed,
                gpu_index=gpu_index,
                timeout_seconds=timeout_seconds
            )
            results.append(result)
        else:
            # Test all GPUs sequentially
            results = await benchmarker.run_multi_gpu_benchmark(
                benchmark_type=benchmark_type,
                seed=seed,
                timeout_seconds=timeout_seconds
            )

        total_timing_ms = (time.time() - start_time) * 1000

        # Sign the benchmark results
        results_payload = {
            "challenge_id": challenge_id,
            "benchmark_type": benchmark_type,
            "seed": seed,
            "results": [r.to_dict() for r in results],
            "total_timing_ms": total_timing_ms,
            "timestamp": int(time.time())
        }

        import json
        signature = self.identity.sign_challenge(json.dumps(results_payload, sort_keys=True))

        return GPUBenchmarkResponse(
            challenge_id=challenge_id,
            node_id=self.identity.node_id,
            results=results,
            total_timing_ms=total_timing_ms,
            signature=signature,
            timestamp=int(time.time())
        )

    def cleanup_old_challenges(self):
        """Remove old challenge IDs to prevent memory leak."""
        # In production, you'd track timestamps and remove old ones
        if len(self._seen_challenges) > 10000:
            self._seen_challenges.clear()
