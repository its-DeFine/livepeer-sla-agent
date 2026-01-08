"""
GPU Performance Profiles for Benchmark Verification.

This module contains expected benchmark timings for various GPU models.
These baselines are used to verify that a GPU performs as expected.

The profiles are organized by:
- GPU model name (as reported by nvidia-smi)
- Benchmark type (matrix_4096, matrix_8192, transcode_720p, etc.)
- Expected timing in milliseconds
- Tolerance (how much variance to allow)

How profiles are used:
1. Agent runs benchmark → reports timing
2. Dashboard looks up expected timing for claimed GPU
3. If timing > expected * (1 + tolerance) → SUSPECT
4. If timing within tolerance → VERIFIED
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class BenchmarkProfile:
    """Expected performance for a specific benchmark on a GPU."""
    expected_ms: float
    tolerance: float = 0.25  # 25% variance allowed by default
    min_ms: float = 0.0  # Floor (can't be faster than this)

    def is_within_tolerance(self, actual_ms: float) -> bool:
        """Check if actual timing is within acceptable range."""
        if actual_ms < self.min_ms:
            return False  # Suspiciously fast
        max_allowed = self.expected_ms * (1 + self.tolerance)
        return actual_ms <= max_allowed

    def get_score(self, actual_ms: float) -> float:
        """
        Calculate a 0-100 score based on timing.

        100 = at or below expected
        0 = at or beyond tolerance limit
        """
        if actual_ms <= self.expected_ms:
            return 100.0
        max_allowed = self.expected_ms * (1 + self.tolerance)
        if actual_ms >= max_allowed:
            return 0.0
        # Linear interpolation
        excess = actual_ms - self.expected_ms
        tolerance_range = max_allowed - self.expected_ms
        return 100.0 * (1 - (excess / tolerance_range))


@dataclass
class GPUProfile:
    """Performance profile for a GPU model."""
    name: str
    family: str  # e.g., "RTX 40", "RTX 30", "Tesla", "Quadro"
    compute_capability: str  # e.g., "8.9", "8.6"
    benchmarks: Dict[str, BenchmarkProfile]

    def get_benchmark(self, benchmark_type: str) -> Optional[BenchmarkProfile]:
        """Get benchmark profile, or None if not defined."""
        return self.benchmarks.get(benchmark_type)


# GPU Profile Database
# These are approximate values - actual values may vary by driver, temperature, etc.
# The tolerance accounts for this variance.

GPU_PROFILES: Dict[str, GPUProfile] = {
    # ===================
    # RTX 40 Series (Ada Lovelace)
    # ===================
    "NVIDIA GeForce RTX 4090": GPUProfile(
        name="NVIDIA GeForce RTX 4090",
        family="RTX 40",
        compute_capability="8.9",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=35, tolerance=0.25, min_ms=10),
            "matrix_8192": BenchmarkProfile(expected_ms=140, tolerance=0.25, min_ms=40),
            "transcode_720p": BenchmarkProfile(expected_ms=800, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1200, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 4080": GPUProfile(
        name="NVIDIA GeForce RTX 4080",
        family="RTX 40",
        compute_capability="8.9",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=50, tolerance=0.25, min_ms=15),
            "matrix_8192": BenchmarkProfile(expected_ms=200, tolerance=0.25, min_ms=60),
            "transcode_720p": BenchmarkProfile(expected_ms=900, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1400, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 4070 Ti": GPUProfile(
        name="NVIDIA GeForce RTX 4070 Ti",
        family="RTX 40",
        compute_capability="8.9",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=60, tolerance=0.25, min_ms=18),
            "matrix_8192": BenchmarkProfile(expected_ms=240, tolerance=0.25, min_ms=70),
            "transcode_720p": BenchmarkProfile(expected_ms=950, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1500, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 4070": GPUProfile(
        name="NVIDIA GeForce RTX 4070",
        family="RTX 40",
        compute_capability="8.9",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=70, tolerance=0.25, min_ms=20),
            "matrix_8192": BenchmarkProfile(expected_ms=280, tolerance=0.25, min_ms=80),
            "transcode_720p": BenchmarkProfile(expected_ms=1000, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1600, tolerance=0.30),
        }
    ),

    # ===================
    # RTX 30 Series (Ampere)
    # ===================
    "NVIDIA GeForce RTX 3090": GPUProfile(
        name="NVIDIA GeForce RTX 3090",
        family="RTX 30",
        compute_capability="8.6",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=55, tolerance=0.25, min_ms=15),
            "matrix_8192": BenchmarkProfile(expected_ms=220, tolerance=0.25, min_ms=65),
            "transcode_720p": BenchmarkProfile(expected_ms=850, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1350, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 3090 Ti": GPUProfile(
        name="NVIDIA GeForce RTX 3090 Ti",
        family="RTX 30",
        compute_capability="8.6",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=50, tolerance=0.25, min_ms=14),
            "matrix_8192": BenchmarkProfile(expected_ms=200, tolerance=0.25, min_ms=60),
            "transcode_720p": BenchmarkProfile(expected_ms=800, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1300, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 3080": GPUProfile(
        name="NVIDIA GeForce RTX 3080",
        family="RTX 30",
        compute_capability="8.6",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=65, tolerance=0.25, min_ms=18),
            "matrix_8192": BenchmarkProfile(expected_ms=260, tolerance=0.25, min_ms=75),
            "transcode_720p": BenchmarkProfile(expected_ms=900, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1450, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 3080 Ti": GPUProfile(
        name="NVIDIA GeForce RTX 3080 Ti",
        family="RTX 30",
        compute_capability="8.6",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=58, tolerance=0.25, min_ms=16),
            "matrix_8192": BenchmarkProfile(expected_ms=235, tolerance=0.25, min_ms=68),
            "transcode_720p": BenchmarkProfile(expected_ms=870, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1380, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 3070": GPUProfile(
        name="NVIDIA GeForce RTX 3070",
        family="RTX 30",
        compute_capability="8.6",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=80, tolerance=0.25, min_ms=22),
            "matrix_8192": BenchmarkProfile(expected_ms=320, tolerance=0.25, min_ms=90),
            "transcode_720p": BenchmarkProfile(expected_ms=1000, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1600, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 3070 Ti": GPUProfile(
        name="NVIDIA GeForce RTX 3070 Ti",
        family="RTX 30",
        compute_capability="8.6",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=75, tolerance=0.25, min_ms=21),
            "matrix_8192": BenchmarkProfile(expected_ms=300, tolerance=0.25, min_ms=85),
            "transcode_720p": BenchmarkProfile(expected_ms=950, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1550, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 3060": GPUProfile(
        name="NVIDIA GeForce RTX 3060",
        family="RTX 30",
        compute_capability="8.6",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=100, tolerance=0.25, min_ms=28),
            "matrix_8192": BenchmarkProfile(expected_ms=400, tolerance=0.25, min_ms=110),
            "transcode_720p": BenchmarkProfile(expected_ms=1100, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1800, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 3060 Ti": GPUProfile(
        name="NVIDIA GeForce RTX 3060 Ti",
        family="RTX 30",
        compute_capability="8.6",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=90, tolerance=0.25, min_ms=25),
            "matrix_8192": BenchmarkProfile(expected_ms=360, tolerance=0.25, min_ms=100),
            "transcode_720p": BenchmarkProfile(expected_ms=1050, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1700, tolerance=0.30),
        }
    ),

    # ===================
    # Data Center GPUs
    # ===================
    "NVIDIA A100-SXM4-80GB": GPUProfile(
        name="NVIDIA A100-SXM4-80GB",
        family="Ampere Data Center",
        compute_capability="8.0",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=25, tolerance=0.20, min_ms=8),
            "matrix_8192": BenchmarkProfile(expected_ms=100, tolerance=0.20, min_ms=30),
            "transcode_720p": BenchmarkProfile(expected_ms=700, tolerance=0.25),
            "transcode_1080p": BenchmarkProfile(expected_ms=1100, tolerance=0.25),
        }
    ),
    "NVIDIA A100-PCIE-40GB": GPUProfile(
        name="NVIDIA A100-PCIE-40GB",
        family="Ampere Data Center",
        compute_capability="8.0",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=28, tolerance=0.20, min_ms=9),
            "matrix_8192": BenchmarkProfile(expected_ms=110, tolerance=0.20, min_ms=33),
            "transcode_720p": BenchmarkProfile(expected_ms=750, tolerance=0.25),
            "transcode_1080p": BenchmarkProfile(expected_ms=1150, tolerance=0.25),
        }
    ),
    "NVIDIA H100 PCIe": GPUProfile(
        name="NVIDIA H100 PCIe",
        family="Hopper Data Center",
        compute_capability="9.0",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=18, tolerance=0.20, min_ms=5),
            "matrix_8192": BenchmarkProfile(expected_ms=70, tolerance=0.20, min_ms=20),
            "transcode_720p": BenchmarkProfile(expected_ms=600, tolerance=0.25),
            "transcode_1080p": BenchmarkProfile(expected_ms=950, tolerance=0.25),
        }
    ),
    "NVIDIA L40": GPUProfile(
        name="NVIDIA L40",
        family="Ada Data Center",
        compute_capability="8.9",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=40, tolerance=0.20, min_ms=12),
            "matrix_8192": BenchmarkProfile(expected_ms=160, tolerance=0.20, min_ms=45),
            "transcode_720p": BenchmarkProfile(expected_ms=750, tolerance=0.25),
            "transcode_1080p": BenchmarkProfile(expected_ms=1200, tolerance=0.25),
        }
    ),
    "Tesla T4": GPUProfile(
        name="Tesla T4",
        family="Turing Data Center",
        compute_capability="7.5",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=120, tolerance=0.25, min_ms=35),
            "matrix_8192": BenchmarkProfile(expected_ms=480, tolerance=0.25, min_ms=130),
            "transcode_720p": BenchmarkProfile(expected_ms=1200, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1900, tolerance=0.30),
        }
    ),

    # ===================
    # RTX 20 Series (Turing)
    # ===================
    "NVIDIA GeForce RTX 2080 Ti": GPUProfile(
        name="NVIDIA GeForce RTX 2080 Ti",
        family="RTX 20",
        compute_capability="7.5",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=85, tolerance=0.25, min_ms=24),
            "matrix_8192": BenchmarkProfile(expected_ms=340, tolerance=0.25, min_ms=95),
            "transcode_720p": BenchmarkProfile(expected_ms=1000, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1600, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 2080": GPUProfile(
        name="NVIDIA GeForce RTX 2080",
        family="RTX 20",
        compute_capability="7.5",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=95, tolerance=0.25, min_ms=27),
            "matrix_8192": BenchmarkProfile(expected_ms=380, tolerance=0.25, min_ms=105),
            "transcode_720p": BenchmarkProfile(expected_ms=1050, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1700, tolerance=0.30),
        }
    ),
    "NVIDIA GeForce RTX 2070": GPUProfile(
        name="NVIDIA GeForce RTX 2070",
        family="RTX 20",
        compute_capability="7.5",
        benchmarks={
            "matrix_4096": BenchmarkProfile(expected_ms=110, tolerance=0.25, min_ms=31),
            "matrix_8192": BenchmarkProfile(expected_ms=440, tolerance=0.25, min_ms=120),
            "transcode_720p": BenchmarkProfile(expected_ms=1100, tolerance=0.30),
            "transcode_1080p": BenchmarkProfile(expected_ms=1800, tolerance=0.30),
        }
    ),
}


def get_gpu_profile(gpu_name: str) -> Optional[GPUProfile]:
    """
    Get profile for a GPU by name.

    Tries exact match first, then partial match.
    """
    # Exact match
    if gpu_name in GPU_PROFILES:
        return GPU_PROFILES[gpu_name]

    # Partial match (GPU name might have extra info like "Laptop GPU")
    for profile_name, profile in GPU_PROFILES.items():
        if profile_name in gpu_name or gpu_name in profile_name:
            return profile

    return None


def estimate_profile_from_timing(
    benchmark_type: str,
    actual_ms: float
) -> Optional[str]:
    """
    Estimate which GPU a timing result corresponds to.

    Useful for detecting if someone is claiming a faster GPU than they have.
    """
    best_match = None
    best_score = -1

    for gpu_name, profile in GPU_PROFILES.items():
        benchmark = profile.get_benchmark(benchmark_type)
        if not benchmark:
            continue

        score = benchmark.get_score(actual_ms)
        if score > best_score:
            best_score = score
            best_match = gpu_name

    return best_match


def is_timing_plausible(
    claimed_gpu: str,
    benchmark_type: str,
    actual_ms: float
) -> tuple[bool, str]:
    """
    Check if a timing result is plausible for the claimed GPU.

    Returns:
        (is_plausible, reason)
    """
    profile = get_gpu_profile(claimed_gpu)

    if not profile:
        return True, f"Unknown GPU model, cannot verify: {claimed_gpu}"

    benchmark = profile.get_benchmark(benchmark_type)

    if not benchmark:
        return True, f"No benchmark profile for {benchmark_type} on {claimed_gpu}"

    if actual_ms < benchmark.min_ms:
        estimated = estimate_profile_from_timing(benchmark_type, actual_ms)
        return False, f"Suspiciously fast ({actual_ms:.1f}ms < {benchmark.min_ms:.1f}ms min). Possible: {estimated}"

    if not benchmark.is_within_tolerance(actual_ms):
        max_allowed = benchmark.expected_ms * (1 + benchmark.tolerance)
        return False, f"Too slow ({actual_ms:.1f}ms > {max_allowed:.1f}ms max)"

    return True, "OK"


# Benchmark type descriptions
BENCHMARK_TYPES = {
    "matrix_4096": {
        "description": "4096x4096 matrix multiplication (FP32)",
        "tests": "Raw GPU compute capability",
        "typical_duration": "35-120ms depending on GPU"
    },
    "matrix_8192": {
        "description": "8192x8192 matrix multiplication (FP32)",
        "tests": "GPU compute + memory bandwidth",
        "typical_duration": "100-500ms depending on GPU"
    },
    "transcode_720p": {
        "description": "5-second 720p video transcode (NVENC)",
        "tests": "Video encoding hardware capability",
        "typical_duration": "700-1200ms depending on GPU"
    },
    "transcode_1080p": {
        "description": "5-second 1080p video transcode (NVENC)",
        "tests": "Video encoding hardware capability",
        "typical_duration": "950-1900ms depending on GPU"
    },
}
