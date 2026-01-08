"""
GPU Benchmark Framework for Proof-of-Work Verification.

This module provides deterministic GPU benchmarks that prove actual GPU capacity
through timed compute tasks. The key insight: you can fake nvidia-smi output,
but you can't fake physics - a GPU can only compute so fast.

Benchmarks:
1. Matrix Multiply - Tests raw GPU compute (FP32 TFLOPS)
2. Transcode - Tests real video workload (NVENC capability)

Design principles:
- Same seed → same task → same result (deterministic)
- Dashboard can verify result hash without re-running
- Timing reveals actual GPU capability
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkResult:
    """Result of a GPU benchmark."""
    benchmark_type: str
    gpu_index: int
    gpu_model: str
    seed: int
    timing_ms: float
    result_hash: str
    success: bool
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GPUBenchmarkChallenge:
    """A benchmark challenge from the dashboard."""
    challenge_id: str
    seed: int
    benchmark_type: str  # "matrix_4096", "matrix_8192", "transcode_720p", etc.
    gpu_index: int = 0  # Which GPU to test (for multi-GPU)


@dataclass
class GPUBenchmarkResponse:
    """Response to a GPU benchmark challenge."""
    challenge_id: str
    node_id: str
    results: List[BenchmarkResult]
    total_timing_ms: float
    signature: str
    timestamp: int

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "results": [r.to_dict() for r in self.results]
        }


class GPUBenchmarker:
    """
    Runs deterministic GPU benchmarks for proof-of-work verification.

    The benchmarks are designed to:
    1. Be deterministic (same seed = same result)
    2. Measure actual GPU compute time
    3. Produce verifiable output hashes
    """

    def __init__(self):
        self._torch_available = self._check_torch()
        self._cuda_available = self._check_cuda()
        self._gpu_info = self._detect_gpus()

    def _check_torch(self) -> bool:
        """Check if PyTorch is available."""
        try:
            import torch
            return True
        except ImportError:
            logger.warning("PyTorch not available - matrix benchmarks disabled")
            return False

    def _check_cuda(self) -> bool:
        """Check if CUDA is available."""
        if not self._torch_available:
            return False
        try:
            import torch
            return torch.cuda.is_available()
        except Exception:
            return False

    def _detect_gpus(self) -> List[dict]:
        """Detect available GPUs using nvidia-smi."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return []

            gpus = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    gpus.append({
                        "index": int(parts[0]),
                        "name": parts[1],
                        "memory_mb": int(float(parts[2]))
                    })
            return gpus
        except Exception as e:
            logger.warning(f"Failed to detect GPUs: {e}")
            return []

    @property
    def gpu_count(self) -> int:
        return len(self._gpu_info)

    def get_gpu_info(self, index: int = 0) -> Optional[dict]:
        """Get info for a specific GPU."""
        for gpu in self._gpu_info:
            if gpu["index"] == index:
                return gpu
        return None

    async def run_benchmark(
        self,
        benchmark_type: str,
        seed: int,
        gpu_index: int = 0,
        timeout_seconds: int = 60
    ) -> BenchmarkResult:
        """
        Run a single benchmark.

        Args:
            benchmark_type: Type of benchmark (e.g., "matrix_4096", "transcode_720p")
            seed: Random seed for determinism
            gpu_index: Which GPU to use
            timeout_seconds: Maximum time allowed

        Returns:
            BenchmarkResult with timing and output hash
        """
        gpu_info = self.get_gpu_info(gpu_index)
        if not gpu_info:
            return BenchmarkResult(
                benchmark_type=benchmark_type,
                gpu_index=gpu_index,
                gpu_model="unknown",
                seed=seed,
                timing_ms=0,
                result_hash="",
                success=False,
                error=f"GPU {gpu_index} not found"
            )

        try:
            if benchmark_type.startswith("matrix_"):
                size = int(benchmark_type.split("_")[1])
                return await self._matrix_benchmark(seed, size, gpu_index, gpu_info, timeout_seconds)
            elif benchmark_type.startswith("transcode_"):
                profile = benchmark_type.split("_")[1]
                return await self._transcode_benchmark(seed, profile, gpu_index, gpu_info, timeout_seconds)
            else:
                return BenchmarkResult(
                    benchmark_type=benchmark_type,
                    gpu_index=gpu_index,
                    gpu_model=gpu_info["name"],
                    seed=seed,
                    timing_ms=0,
                    result_hash="",
                    success=False,
                    error=f"Unknown benchmark type: {benchmark_type}"
                )
        except Exception as e:
            logger.error(f"Benchmark {benchmark_type} failed: {e}")
            return BenchmarkResult(
                benchmark_type=benchmark_type,
                gpu_index=gpu_index,
                gpu_model=gpu_info["name"],
                seed=seed,
                timing_ms=0,
                result_hash="",
                success=False,
                error=str(e)
            )

    async def _matrix_benchmark(
        self,
        seed: int,
        size: int,
        gpu_index: int,
        gpu_info: dict,
        timeout_seconds: int
    ) -> BenchmarkResult:
        """
        Matrix multiplication benchmark.

        Creates two random matrices of size NxN and multiplies them.
        The result hash proves the computation was done correctly.
        The timing proves GPU capability.
        """
        if not self._cuda_available:
            return BenchmarkResult(
                benchmark_type=f"matrix_{size}",
                gpu_index=gpu_index,
                gpu_model=gpu_info["name"],
                seed=seed,
                timing_ms=0,
                result_hash="",
                success=False,
                error="CUDA not available"
            )

        import torch

        # Set device
        device = torch.device(f"cuda:{gpu_index}")

        # Ensure determinism
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)

        # These settings ensure reproducible results
        torch.use_deterministic_algorithms(True, warn_only=True)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        # Generate deterministic matrices
        A = torch.randn(size, size, device=device, dtype=torch.float32)
        B = torch.randn(size, size, device=device, dtype=torch.float32)

        # Warmup run (not timed)
        _ = torch.matmul(A, B)
        torch.cuda.synchronize(device)

        # Timed execution
        start_time = time.perf_counter()
        C = torch.matmul(A, B)
        torch.cuda.synchronize(device)  # Wait for GPU to finish
        end_time = time.perf_counter()

        timing_ms = (end_time - start_time) * 1000

        # Calculate result hash (proves correct computation)
        # Use a subset of values to avoid huge data transfer
        checksum_data = C[::max(1, size//64), ::max(1, size//64)].cpu().numpy().tobytes()
        result_hash = hashlib.sha256(checksum_data).hexdigest()

        # Cleanup
        del A, B, C
        torch.cuda.empty_cache()

        return BenchmarkResult(
            benchmark_type=f"matrix_{size}",
            gpu_index=gpu_index,
            gpu_model=gpu_info["name"],
            seed=seed,
            timing_ms=timing_ms,
            result_hash=result_hash,
            success=True,
            metadata={
                "matrix_size": size,
                "flops": 2 * size * size * size,  # Approximate FLOPS for matmul
                "gflops": (2 * size * size * size) / (timing_ms / 1000) / 1e9
            }
        )

    async def _transcode_benchmark(
        self,
        seed: int,
        profile: str,
        gpu_index: int,
        gpu_info: dict,
        timeout_seconds: int
    ) -> BenchmarkResult:
        """
        FFmpeg transcode benchmark.

        Generates a deterministic test pattern video and transcodes it using NVENC.
        This tests real video transcoding capability.
        """
        profile_settings = {
            "720p": {"width": 1280, "height": 720, "fps": 30, "duration": 5},
            "1080p": {"width": 1920, "height": 1080, "fps": 30, "duration": 5},
            "4k": {"width": 3840, "height": 2160, "fps": 30, "duration": 3},
        }

        settings = profile_settings.get(profile, profile_settings["720p"])

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.mp4"
            output_path = Path(tmpdir) / "output.mp4"

            # Generate deterministic test pattern
            # The seed determines the pattern parameters
            pattern_seed = seed % 1000

            # Create test video with ffmpeg
            generate_cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"testsrc=size={settings['width']}x{settings['height']}:rate={settings['fps']}:duration={settings['duration']}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-pix_fmt", "yuv420p",
                str(input_path)
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *generate_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)

                if proc.returncode != 0 or not input_path.exists():
                    return BenchmarkResult(
                        benchmark_type=f"transcode_{profile}",
                        gpu_index=gpu_index,
                        gpu_model=gpu_info["name"],
                        seed=seed,
                        timing_ms=0,
                        result_hash="",
                        success=False,
                        error="Failed to generate test video"
                    )
            except asyncio.TimeoutError:
                return BenchmarkResult(
                    benchmark_type=f"transcode_{profile}",
                    gpu_index=gpu_index,
                    gpu_model=gpu_info["name"],
                    seed=seed,
                    timing_ms=0,
                    result_hash="",
                    success=False,
                    error="Test video generation timeout"
                )

            # Transcode with NVENC (or fallback to CPU)
            # Try NVENC first
            nvenc_cmd = [
                "ffmpeg", "-y",
                "-hwaccel", "cuda",
                "-hwaccel_device", str(gpu_index),
                "-i", str(input_path),
                "-c:v", "h264_nvenc",
                "-preset", "p4",  # Balanced preset
                "-gpu", str(gpu_index),
                "-an",
                str(output_path)
            ]

            # Fallback to CPU if NVENC fails
            cpu_cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-c:v", "libx264",
                "-preset", "fast",
                "-an",
                str(output_path)
            ]

            start_time = time.perf_counter()
            used_nvenc = True

            try:
                proc = await asyncio.create_subprocess_exec(
                    *nvenc_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_seconds
                )

                if proc.returncode != 0:
                    # Fallback to CPU
                    used_nvenc = False
                    logger.info("NVENC failed, falling back to CPU encoding")
                    start_time = time.perf_counter()

                    proc = await asyncio.create_subprocess_exec(
                        *cpu_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=timeout_seconds
                    )

                    if proc.returncode != 0:
                        return BenchmarkResult(
                            benchmark_type=f"transcode_{profile}",
                            gpu_index=gpu_index,
                            gpu_model=gpu_info["name"],
                            seed=seed,
                            timing_ms=0,
                            result_hash="",
                            success=False,
                            error=f"Transcode failed: {stderr.decode()[:200]}"
                        )

            except asyncio.TimeoutError:
                return BenchmarkResult(
                    benchmark_type=f"transcode_{profile}",
                    gpu_index=gpu_index,
                    gpu_model=gpu_info["name"],
                    seed=seed,
                    timing_ms=0,
                    result_hash="",
                    success=False,
                    error="Transcode timeout"
                )

            end_time = time.perf_counter()
            timing_ms = (end_time - start_time) * 1000

            # Calculate output hash
            result_hash = self._hash_file(output_path)

            return BenchmarkResult(
                benchmark_type=f"transcode_{profile}",
                gpu_index=gpu_index,
                gpu_model=gpu_info["name"],
                seed=seed,
                timing_ms=timing_ms,
                result_hash=result_hash,
                success=True,
                metadata={
                    "profile": profile,
                    "width": settings["width"],
                    "height": settings["height"],
                    "fps": settings["fps"],
                    "duration_s": settings["duration"],
                    "used_nvenc": used_nvenc,
                    "output_size": output_path.stat().st_size if output_path.exists() else 0
                }
            )

    def _hash_file(self, path: Path) -> str:
        """Calculate SHA256 hash of file."""
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    async def run_multi_gpu_benchmark(
        self,
        benchmark_type: str,
        seed: int,
        timeout_seconds: int = 120
    ) -> List[BenchmarkResult]:
        """
        Run benchmark on all detected GPUs sequentially.

        This verifies multi-GPU capacity by testing each GPU individually.
        """
        results = []

        for gpu in self._gpu_info:
            result = await self.run_benchmark(
                benchmark_type=benchmark_type,
                seed=seed + gpu["index"],  # Different seed per GPU for variety
                gpu_index=gpu["index"],
                timeout_seconds=timeout_seconds // max(1, len(self._gpu_info))
            )
            results.append(result)

        return results


# Singleton instance
_benchmarker: Optional[GPUBenchmarker] = None


def get_benchmarker() -> GPUBenchmarker:
    """Get or create the GPU benchmarker singleton."""
    global _benchmarker
    if _benchmarker is None:
        _benchmarker = GPUBenchmarker()
    return _benchmarker
