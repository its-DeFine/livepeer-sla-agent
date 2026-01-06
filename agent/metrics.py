"""
Job-level metrics collection.

Captures real-time performance data from transcoding jobs.
Focused on transcode metrics and SLA scores.
"""
from __future__ import annotations

import time
import asyncio
import subprocess
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path
from enum import Enum

logger = logging.getLogger(__name__)


class JobType(str, Enum):
    TRANSCODE = "transcode"


@dataclass
class TranscodeMetrics:
    """Metrics from a transcoding job."""
    job_id: str
    timestamp: int

    # Input
    input_duration_ms: int
    input_resolution: str  # e.g., "1920x1080"
    input_codec: str       # e.g., "h264"
    input_size_bytes: int

    # Output
    output_resolution: str
    output_codec: str
    output_size_bytes: int
    output_profile: str    # e.g., "P720p30fps16x9"

    # Performance
    transcode_time_ms: int
    realtime_ratio: float  # < 1.0 means faster than realtime
    gpu_used: bool
    gpu_index: Optional[int] = None

    # Quality (if measured)
    psnr: Optional[float] = None
    ssim: Optional[float] = None
    vmaf: Optional[float] = None

    # Errors
    success: bool = True
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SLAScore:
    """
    Computed SLA score for a node.

    Based on multiple factors weighted together.
    """
    node_id: str
    timestamp: int

    # Component scores (0-100)
    availability_score: float      # Uptime / responsiveness
    latency_score: float           # Response time to challenges
    throughput_score: float        # Transcode speed
    quality_score: float           # Output quality (VMAF, etc.)
    reliability_score: float       # Success rate

    # Weighted total
    total_score: float

    # Metadata
    sample_count: int              # Number of measurements
    period_hours: int              # Time period for this score

    def to_dict(self) -> dict:
        return asdict(self)


class MetricsCollector:
    """
    Collects and aggregates transcode job metrics.

    This runs on the agent and reports metrics with each heartbeat.
    """

    def __init__(self, max_history: int = 1000):
        self.max_history = max_history
        self._transcode_metrics: list[TranscodeMetrics] = []
        self._start_time = time.time()

    def record_transcode(self, metrics: TranscodeMetrics):
        """Record a transcode job's metrics."""
        self._transcode_metrics.append(metrics)
        if len(self._transcode_metrics) > self.max_history:
            self._transcode_metrics = self._transcode_metrics[-self.max_history:]

    def get_summary(self, last_n_minutes: int = 60) -> dict:
        """Get aggregated metrics summary for reporting."""
        cutoff = time.time() - (last_n_minutes * 60)
        recent_transcode = [m for m in self._transcode_metrics if m.timestamp > cutoff]

        return {
            "period_minutes": last_n_minutes,
            "uptime_seconds": int(time.time() - self._start_time),
            "transcode": self._summarize_transcode(recent_transcode)
        }

    def _summarize_transcode(self, metrics: list[TranscodeMetrics]) -> dict:
        """Summarize transcode metrics."""
        if not metrics:
            return {"job_count": 0}

        success_count = sum(1 for m in metrics if m.success)
        times = [m.transcode_time_ms for m in metrics if m.success]
        ratios = [m.realtime_ratio for m in metrics if m.success]

        return {
            "job_count": len(metrics),
            "success_rate": success_count / len(metrics) if metrics else 0,
            "avg_transcode_ms": sum(times) / len(times) if times else 0,
            "avg_realtime_ratio": sum(ratios) / len(ratios) if ratios else 0,
            "min_realtime_ratio": min(ratios) if ratios else 0,
            "gpu_job_percent": sum(1 for m in metrics if m.gpu_used) / len(metrics) if metrics else 0
        }


def compute_sla_score(
    node_id: str,
    availability_samples: list[bool],      # Was node responsive?
    latency_samples: list[int],             # Response times in ms
    transcode_ratios: list[float],          # Realtime ratios
    success_samples: list[bool],            # Did jobs succeed?
    quality_scores: list[float] = None      # VMAF scores if available
) -> SLAScore:
    """
    Compute an SLA score from raw samples.

    Each component is scored 0-100, then weighted.
    """
    timestamp = int(time.time())

    # Availability: % of successful pings
    availability = (sum(availability_samples) / len(availability_samples) * 100) if availability_samples else 0

    # Latency: Score based on response time (lower is better)
    # 100 = <50ms, 0 = >5000ms
    if latency_samples:
        avg_latency = sum(latency_samples) / len(latency_samples)
        latency_score = max(0, min(100, 100 - (avg_latency - 50) / 49.5))
    else:
        latency_score = 0

    # Throughput: Based on realtime ratio (lower is better = faster)
    # 100 = 0.1x realtime, 50 = 1.0x realtime, 0 = >2x realtime
    if transcode_ratios:
        avg_ratio = sum(transcode_ratios) / len(transcode_ratios)
        throughput_score = max(0, min(100, (2.0 - avg_ratio) * 50))
    else:
        throughput_score = 0

    # Quality: Average VMAF or default to 80 if not measured
    if quality_scores:
        quality_score = sum(quality_scores) / len(quality_scores)
    else:
        quality_score = 80  # Assume good quality if not measured

    # Reliability: % of successful jobs
    reliability = (sum(success_samples) / len(success_samples) * 100) if success_samples else 0

    # Weighted total
    weights = {
        "availability": 0.25,
        "latency": 0.15,
        "throughput": 0.25,
        "quality": 0.15,
        "reliability": 0.20
    }

    total = (
        availability * weights["availability"] +
        latency_score * weights["latency"] +
        throughput_score * weights["throughput"] +
        quality_score * weights["quality"] +
        reliability * weights["reliability"]
    )

    return SLAScore(
        node_id=node_id,
        timestamp=timestamp,
        availability_score=availability,
        latency_score=latency_score,
        throughput_score=throughput_score,
        quality_score=quality_score,
        reliability_score=reliability,
        total_score=total,
        sample_count=len(availability_samples) + len(latency_samples),
        period_hours=24  # Default to daily
    )


async def measure_gpu_utilization() -> Optional[dict]:
    """
    Measure current GPU utilization.

    This is the "CUDA Utilization Efficiency" metric they mentioned.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits"
            ],
            capture_output=True, text=True, timeout=10
        )

        if result.returncode != 0:
            return None

        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                gpus.append({
                    "index": int(parts[0]),
                    "gpu_utilization_percent": float(parts[1]),
                    "memory_utilization_percent": float(parts[2]),
                    "memory_used_mb": int(float(parts[3])),
                    "memory_total_mb": int(float(parts[4])),
                    "temperature_c": float(parts[5]),
                    "power_draw_w": float(parts[6]) if parts[6] != "[N/A]" else None
                })

        return {"gpus": gpus, "timestamp": int(time.time())}

    except Exception as e:
        logger.debug(f"GPU utilization measurement failed: {e}")
        return None


async def benchmark_transcode_speed(
    test_video_url: str = "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4",
    profile: str = "P720p30fps16x9"
) -> Optional[TranscodeMetrics]:
    """
    Run a transcode benchmark and return metrics.

    This actively tests transcode capability.
    """
    import tempfile
    import hashlib
    import httpx

    job_id = hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:16]
    timestamp = int(time.time())

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.mp4"
            output_path = Path(tmpdir) / "output.mp4"

            # Download test video
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(test_video_url)
                response.raise_for_status()
                input_path.write_bytes(response.content)

            input_size = input_path.stat().st_size

            # Get input info
            probe_cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(input_path)
            ]
            probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)

            input_duration_ms = 10000  # Default 10s
            input_resolution = "640x360"
            input_codec = "h264"

            if probe_result.returncode == 0:
                import json
                try:
                    probe_data = json.loads(probe_result.stdout)
                    if "format" in probe_data:
                        input_duration_ms = int(float(probe_data["format"].get("duration", 10)) * 1000)
                    for stream in probe_data.get("streams", []):
                        if stream.get("codec_type") == "video":
                            input_resolution = f"{stream.get('width', 640)}x{stream.get('height', 360)}"
                            input_codec = stream.get("codec_name", "h264")
                            break
                except:
                    pass

            # Transcode
            profile_settings = {
                "P720p30fps16x9": ["-vf", "scale=1280:720", "-r", "30"],
                "P720p60fps16x9": ["-vf", "scale=1280:720", "-r", "60"],
                "P1080p30fps16x9": ["-vf", "scale=1920:1080", "-r", "30"],
            }

            ffmpeg_args = profile_settings.get(profile, profile_settings["P720p30fps16x9"])

            # Check if GPU available
            gpu_available = subprocess.run(
                ["nvidia-smi"], capture_output=True
            ).returncode == 0

            if gpu_available:
                # Use NVENC
                cmd = [
                    "ffmpeg", "-y", "-hwaccel", "cuda",
                    "-i", str(input_path),
                    *ffmpeg_args,
                    "-c:v", "h264_nvenc", "-preset", "p4",
                    "-an", str(output_path)
                ]
            else:
                cmd = [
                    "ffmpeg", "-y",
                    "-i", str(input_path),
                    *ffmpeg_args,
                    "-c:v", "libx264", "-preset", "fast",
                    "-an", str(output_path)
                ]

            start = time.time()
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            transcode_time_ms = int((time.time() - start) * 1000)

            if proc.returncode != 0:
                return TranscodeMetrics(
                    job_id=job_id,
                    timestamp=timestamp,
                    input_duration_ms=input_duration_ms,
                    input_resolution=input_resolution,
                    input_codec=input_codec,
                    input_size_bytes=input_size,
                    output_resolution="1280x720",
                    output_codec="h264",
                    output_size_bytes=0,
                    output_profile=profile,
                    transcode_time_ms=transcode_time_ms,
                    realtime_ratio=transcode_time_ms / input_duration_ms,
                    gpu_used=gpu_available,
                    success=False,
                    error_message=proc.stderr.decode()[:200]
                )

            output_size = output_path.stat().st_size

            return TranscodeMetrics(
                job_id=job_id,
                timestamp=timestamp,
                input_duration_ms=input_duration_ms,
                input_resolution=input_resolution,
                input_codec=input_codec,
                input_size_bytes=input_size,
                output_resolution="1280x720",
                output_codec="h264",
                output_size_bytes=output_size,
                output_profile=profile,
                transcode_time_ms=transcode_time_ms,
                realtime_ratio=transcode_time_ms / input_duration_ms,
                gpu_used=gpu_available,
                gpu_index=0 if gpu_available else None,
                success=True
            )

    except Exception as e:
        logger.error(f"Benchmark failed: {e}")
        return None
