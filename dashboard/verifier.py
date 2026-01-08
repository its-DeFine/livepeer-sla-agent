"""
Active verification system - sends challenges to nodes to verify capabilities.

Instead of trusting what nodes report, we test them.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import logging
import os
from datetime import datetime
from typing import Optional
from dataclasses import dataclass
import json
from pathlib import Path

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

    def __init__(
        self,
        test_video_url: str = DEFAULT_TEST_VIDEO,
        persist_path: Optional[Path] = None,
        challenge_token: Optional[str] = None,
    ):
        self.test_video_url = test_video_url
        self._client: Optional[httpx.AsyncClient] = None
        self._challenge_token = challenge_token or os.environ.get("SLA_CHALLENGE_TOKEN") or None

        # Node endpoint registry (in production, nodes would register these)
        self._endpoints: dict[str, NodeEndpoint] = {}
        self._persist_path = persist_path
        self._load_endpoints()

    def _challenge_headers(self) -> dict[str, str]:
        """Headers to send to agent challenge endpoints."""
        if not self._challenge_token:
            return {}
        return {"X-SLA-Token": self._challenge_token}

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=120.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    def register_endpoint(self, node_id: str, url: str):
        """Register a node's agent endpoint."""
        self._endpoints[node_id] = NodeEndpoint(node_id=node_id, url=url.rstrip("/"))
        self._persist_endpoints()

    def get_endpoint(self, node_id: str) -> Optional[str]:
        """Get a node's registered endpoint URL."""
        ep = self._endpoints.get(node_id)
        return ep.url if ep else None

    def _persist_endpoints(self) -> None:
        """Persist endpoint registry to disk (best-effort)."""
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {node_id: ep.url for node_id, ep in self._endpoints.items()}
            self._persist_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load_endpoints(self) -> None:
        """Load persisted endpoints from disk (best-effort)."""
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            if not isinstance(data, dict):
                return
            for node_id, url in data.items():
                if isinstance(node_id, str) and isinstance(url, str) and url:
                    self._endpoints[node_id] = NodeEndpoint(node_id=node_id, url=url.rstrip("/"))
        except Exception:
            return

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
                },
                headers=self._challenge_headers(),
            )
            response.raise_for_status()
            result = response.json()

            duration_ms = int((time.time() - start_time) * 1000)

            # Verify the signature
            from agent.identity import NodeIdentity
            returned_node_id = result.get("node_id")
            returned_challenge_id = result.get("challenge_id")
            returned_challenge = result.get("challenge")
            returned_signature = result.get("signature")

            node_id_match = returned_node_id == node_id
            challenge_id_match = returned_challenge_id == challenge_id
            challenge_match = returned_challenge == challenge
            signature_valid = bool(returned_signature) and NodeIdentity.verify_challenge_response(
                node_id=node_id,
                challenge=challenge,
                signature=returned_signature,
            )

            success = node_id_match and challenge_id_match and challenge_match and signature_valid

            # Record the job
            storage = get_storage()
            job = VerificationJob(
                job_id=challenge_id,
                node_id=node_id,
                job_type="liveness",
                created_at=datetime.utcnow(),
                completed_at=datetime.utcnow(),
                success=success,
                duration_ms=duration_ms,
                result={
                    "node_id_match": node_id_match,
                    "challenge_id_match": challenge_id_match,
                    "challenge_match": challenge_match,
                    "signature_valid": signature_valid,
                }
            )
            storage.record_verification_job(job)

            return {
                "success": success,
                "node_id": node_id,
                "challenge_id": challenge_id,
                "duration_ms": duration_ms,
                "node_id_match": node_id_match,
                "challenge_id_match": challenge_id_match,
                "challenge_match": challenge_match,
                "signature_valid": signature_valid,
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
            trim_duration_seconds = 2.0
            trim_start_seconds = secrets.randbelow(8000) / 1000.0  # 0s..8s (test clip is ~10s)
            response = await self._client.post(
                f"{endpoint}/challenge/transcode",
                json={
                    "job_id": job_id,
                    "input_url": self.test_video_url,
                    "output_profile": profile,
                    "trim_start_seconds": trim_start_seconds,
                    "trim_duration_seconds": trim_duration_seconds,
                    "timeout_seconds": timeout
                },
                headers=self._challenge_headers(),
                timeout=timeout + 10  # Extra buffer
            )
            response.raise_for_status()
            result = response.json()

            total_duration = int((time.time() - start_time) * 1000)

            # If the agent provides an output URL, fetch bytes and hash locally.
            output_hash = result.get("output_hash")
            output_url = result.get("output_url")
            if result.get("success") and output_url:
                try:
                    fetch = await self._client.get(
                        f"{endpoint}{output_url}",
                        headers=self._challenge_headers(),
                        timeout=timeout + 10,
                    )
                    fetch.raise_for_status()
                    output_hash = hashlib.sha256(fetch.content).hexdigest()
                except Exception:
                    pass

            # Update job record
            storage.update_verification_job(
                job_id,
                completed_at=datetime.utcnow(),
                success=result.get("success", False),
                duration_ms=result.get("duration_ms", total_duration),
                result={**result, "output_hash": output_hash}
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
                "output_hash": output_hash,
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

    async def verify_transcode_burst(
        self,
        node_id: str,
        profile: str = "P720p30fps16x9",
        burst_count: int = 2,
        timeout: int = 60,
        deadline_seconds: Optional[float] = None,
    ) -> dict:
        """
        Run multiple transcode verifications concurrently to estimate effective capacity.

        The result includes a concurrency estimate:
          sum(individual_duration_ms) / wall_clock_ms
        """
        burst_count = max(1, min(int(burst_count or 1), 32))

        start_wall = time.time()
        tasks = [
            self.verify_transcode(node_id=node_id, profile=profile, timeout=timeout)
            for _ in range(burst_count)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        wall_clock_ms = int((time.time() - start_wall) * 1000)

        successes = [r for r in results if isinstance(r, dict) and r.get("success")]
        sum_ms = sum(int(r.get("duration_ms") or 0) for r in successes)
        concurrency = round(sum_ms / max(1, wall_clock_ms), 3)

        ok = len(successes) == burst_count
        error = None
        if deadline_seconds is not None:
            deadline_ms = int(float(deadline_seconds) * 1000)
            if wall_clock_ms > deadline_ms:
                ok = False
                error = f"Deadline exceeded ({wall_clock_ms}ms > {deadline_ms}ms)"

        return {
            "success": ok,
            "node_id": node_id,
            "burst_count": burst_count,
            "wall_clock_ms": wall_clock_ms,
            "concurrency_estimate": concurrency,
            "results": results,
            "error": error,
        }

    async def verify_gpu_benchmark(
        self,
        node_id: str,
        benchmark_type: str = "matrix_4096",
        gpu_index: Optional[int] = None,
        timeout: int = 60
    ) -> dict:
        """
        Send a GPU benchmark challenge to verify actual GPU capability.

        This proves GPU capacity through timed compute tasks.
        The timing is compared against known GPU performance profiles.

        Args:
            node_id: Node to verify
            benchmark_type: Type of benchmark (matrix_4096, transcode_720p, etc.)
            gpu_index: Specific GPU to test, or None for all GPUs
            timeout: Maximum time for benchmark

        Returns:
            Verification result with trust tier assignment
        """
        from agent.gpu_profiles import get_gpu_profile, is_timing_plausible, BENCHMARK_TYPES

        endpoint = self.get_endpoint(node_id)
        if not endpoint:
            return {
                "success": False,
                "error": "No endpoint registered for node",
                "node_id": node_id,
                "trust_tier": "UNKNOWN"
            }

        # Generate challenge
        challenge_id = secrets.token_hex(16)
        seed = secrets.randbelow(2**63)  # Random seed for determinism

        start_time = time.time()

        # Record job start
        storage = get_storage()
        job = VerificationJob(
            job_id=challenge_id,
            node_id=node_id,
            job_type="gpu_benchmark",
            created_at=datetime.utcnow()
        )
        storage.record_verification_job(job)

        try:
            response = await self._client.post(
                f"{endpoint}/challenge/gpu-benchmark",
                json={
                    "challenge_id": challenge_id,
                    "seed": seed,
                    "benchmark_type": benchmark_type,
                    "gpu_index": gpu_index,
                    "timeout_seconds": timeout
                },
                headers=self._challenge_headers(),
                timeout=timeout + 10
            )
            response.raise_for_status()
            result = response.json()

            total_duration = int((time.time() - start_time) * 1000)

            # Analyze results
            benchmark_results = result.get("results", [])
            all_passed = True
            all_plausible = True
            gpu_analysis = []

            for bench_result in benchmark_results:
                gpu_model = bench_result.get("gpu_model", "unknown")
                timing_ms = bench_result.get("timing_ms", 0)
                success = bench_result.get("success", False)

                if not success:
                    all_passed = False
                    gpu_analysis.append({
                        "gpu_index": bench_result.get("gpu_index"),
                        "gpu_model": gpu_model,
                        "success": False,
                        "error": bench_result.get("error"),
                        "trust_tier": "FAILED"
                    })
                    continue

                # Check if timing is plausible for claimed GPU
                plausible, reason = is_timing_plausible(gpu_model, benchmark_type, timing_ms)

                if not plausible:
                    all_plausible = False

                # Calculate score
                profile = get_gpu_profile(gpu_model)
                if profile:
                    bench_profile = profile.get_benchmark(benchmark_type)
                    score = bench_profile.get_score(timing_ms) if bench_profile else 50.0
                else:
                    score = 50.0  # Unknown GPU, neutral score

                gpu_analysis.append({
                    "gpu_index": bench_result.get("gpu_index"),
                    "gpu_model": gpu_model,
                    "success": True,
                    "timing_ms": timing_ms,
                    "plausible": plausible,
                    "reason": reason,
                    "score": score,
                    "result_hash": bench_result.get("result_hash"),
                    "trust_tier": "TESTED" if plausible else "SUSPECT"
                })

            # Determine overall trust tier
            if not all_passed:
                trust_tier = "FAILED"
            elif not all_plausible:
                trust_tier = "SUSPECT"
            else:
                trust_tier = "TESTED"

            # Update job record
            storage.update_verification_job(
                challenge_id,
                completed_at=datetime.utcnow(),
                success=all_passed and all_plausible,
                duration_ms=total_duration,
                result={
                    "trust_tier": trust_tier,
                    "benchmark_type": benchmark_type,
                    "seed": seed,
                    "gpu_analysis": gpu_analysis
                }
            )

            # Update node verification score
            avg_score = sum(g.get("score", 0) for g in gpu_analysis) / max(1, len(gpu_analysis))
            storage.update_verification_score(node_id, avg_score)

            return {
                "success": all_passed and all_plausible,
                "node_id": node_id,
                "challenge_id": challenge_id,
                "trust_tier": trust_tier,
                "benchmark_type": benchmark_type,
                "total_duration_ms": total_duration,
                "gpu_count": len(gpu_analysis),
                "gpu_analysis": gpu_analysis,
                "signature": result.get("signature")
            }

        except httpx.HTTPError as e:
            logger.error(f"GPU benchmark verification failed for {node_id}: {e}")
            storage.update_verification_job(
                challenge_id,
                completed_at=datetime.utcnow(),
                success=False,
                result={"error": str(e)}
            )
            return {
                "success": False,
                "error": str(e),
                "node_id": node_id,
                "challenge_id": challenge_id,
                "trust_tier": "FAILED"
            }

    async def verify_gpu_benchmark_burst(
        self,
        node_id: str,
        benchmark_type: str = "matrix_4096",
        burst_count: int = 2,
        timeout: int = 60,
        deadline_seconds: Optional[float] = None,
    ) -> dict:
        """
        Run multiple GPU benchmark verifications concurrently to estimate effective parallel capacity.

        Uses `/gpu-info` to pick GPU indices when possible, but will still attempt index 0..N-1 if not.
        """
        burst_count = max(1, min(int(burst_count or 1), 32))

        endpoint = self.get_endpoint(node_id)
        if not endpoint:
            return {"success": False, "error": "No endpoint registered for node", "node_id": node_id}

        gpu_indices: list[int] = []
        if self._client:
            try:
                resp = await self._client.get(
                    f"{endpoint}/gpu-info",
                    headers=self._challenge_headers(),
                    timeout=10,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    gpus = payload.get("gpus", [])
                    if isinstance(gpus, list):
                        for item in gpus:
                            if not isinstance(item, dict):
                                continue
                            idx = item.get("index")
                            if isinstance(idx, int):
                                gpu_indices.append(idx)
                            elif isinstance(idx, str) and idx.isdigit():
                                gpu_indices.append(int(idx))
            except Exception:
                gpu_indices = []

        if not gpu_indices:
            gpu_indices = list(range(burst_count))

        selected: list[int] = []
        pool = list(gpu_indices)
        while pool and len(selected) < min(burst_count, len(gpu_indices)):
            i = secrets.randbelow(len(pool))
            selected.append(pool.pop(i))
        while len(selected) < burst_count:
            selected.append(gpu_indices[secrets.randbelow(len(gpu_indices))])

        start_wall = time.time()
        tasks = [
            self.verify_gpu_benchmark(
                node_id=node_id,
                benchmark_type=benchmark_type,
                gpu_index=gpu_index,
                timeout=timeout,
            )
            for gpu_index in selected
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        wall_clock_ms = int((time.time() - start_wall) * 1000)

        successes = [r for r in results if isinstance(r, dict) and r.get("success")]
        sum_ms = sum(int(r.get("total_duration_ms") or 0) for r in successes)
        concurrency = round(sum_ms / max(1, wall_clock_ms), 3)

        ok = len(successes) == burst_count
        error = None
        if deadline_seconds is not None:
            deadline_ms = int(float(deadline_seconds) * 1000)
            if wall_clock_ms > deadline_ms:
                ok = False
                error = f"Deadline exceeded ({wall_clock_ms}ms > {deadline_ms}ms)"

        return {
            "success": ok,
            "node_id": node_id,
            "benchmark_type": benchmark_type,
            "burst_count": burst_count,
            "gpu_indices": selected,
            "wall_clock_ms": wall_clock_ms,
            "concurrency_estimate": concurrency,
            "results": results,
            "error": error,
        }

    async def verify_all_nodes(self, challenge_type: str = "liveness") -> list[dict]:
        """Verify all registered nodes."""
        results = []
        for node_id in self._endpoints:
            if challenge_type == "liveness":
                result = await self.verify_liveness(node_id)
            elif challenge_type == "gpu_benchmark":
                result = await self.verify_gpu_benchmark(node_id)
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
        _verifier = Verifier(persist_path=Path("./data/endpoints.json"))
    return _verifier
