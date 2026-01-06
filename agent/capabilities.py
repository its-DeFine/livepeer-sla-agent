"""
Capability detection module.

Probes the system to detect hardware capabilities relevant for transcoding:
- GPU models and VRAM
- CPU cores and model
- Available memory
- Network bandwidth (estimated)
- Livepeer-specific capabilities (if go-livepeer is running)
"""
from __future__ import annotations

import os
import platform
import subprocess
import socket
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

import psutil


@dataclass
class GPUInfo:
    """Information about a detected GPU."""
    index: int
    name: str
    memory_total_mb: int
    memory_free_mb: int
    driver_version: str = ""
    cuda_version: str = ""
    nvenc_supported: bool = False
    nvdec_supported: bool = False
    # Real-time metrics (updated each heartbeat)
    gpu_utilization_percent: float = 0.0
    memory_utilization_percent: float = 0.0
    temperature_c: float = 0.0
    power_draw_w: Optional[float] = None


@dataclass
class CPUInfo:
    """CPU information."""
    model: str
    cores_physical: int
    cores_logical: int
    frequency_mhz: float
    architecture: str


@dataclass
class MemoryInfo:
    """System memory information."""
    total_mb: int
    available_mb: int
    swap_total_mb: int


@dataclass
class NetworkInfo:
    """Network capability information."""
    hostname: str
    primary_ip: str
    bandwidth_estimate_mbps: Optional[float] = None  # From benchmark if available


@dataclass
class LivepeerInfo:
    """Livepeer-specific capability info."""
    orchestrator_address: Optional[str] = None
    transcoder_available: bool = False
    supported_codecs: list[str] = field(default_factory=list)
    supported_profiles: list[str] = field(default_factory=list)


@dataclass
class NodeCapabilities:
    """Complete capability snapshot of this node."""
    cpu: CPUInfo
    memory: MemoryInfo
    gpus: list[GPUInfo]
    network: NetworkInfo
    livepeer: LivepeerInfo
    os_info: str
    container_runtime: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return asdict(self)


class CapabilityProber:
    """Probes system for hardware and software capabilities."""

    def __init__(self):
        self._gpu_cache: Optional[list[GPUInfo]] = None

    def probe_all(self) -> NodeCapabilities:
        """Run all capability probes and return complete snapshot."""
        return NodeCapabilities(
            cpu=self.probe_cpu(),
            memory=self.probe_memory(),
            gpus=self.probe_gpus(),
            network=self.probe_network(),
            livepeer=self.probe_livepeer(),
            os_info=self._get_os_info(),
            container_runtime=self._detect_container_runtime()
        )

    def probe_cpu(self) -> CPUInfo:
        """Detect CPU information."""
        try:
            freq = psutil.cpu_freq()
            frequency = freq.current if freq else 0.0
        except Exception:
            frequency = 0.0

        # Try to get CPU model
        model = platform.processor() or "Unknown"

        # On Linux, try to get more detailed info
        if platform.system() == "Linux":
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.startswith("model name"):
                            model = line.split(":")[1].strip()
                            break
            except Exception:
                pass

        # On macOS, use sysctl
        if platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    model = result.stdout.strip()
            except Exception:
                pass

        return CPUInfo(
            model=model,
            cores_physical=psutil.cpu_count(logical=False) or 1,
            cores_logical=psutil.cpu_count(logical=True) or 1,
            frequency_mhz=frequency,
            architecture=platform.machine()
        )

    def probe_memory(self) -> MemoryInfo:
        """Detect system memory."""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        return MemoryInfo(
            total_mb=mem.total // (1024 * 1024),
            available_mb=mem.available // (1024 * 1024),
            swap_total_mb=swap.total // (1024 * 1024)
        )

    def probe_gpus(self) -> list[GPUInfo]:
        """Detect NVIDIA GPUs using nvidia-smi with real-time metrics."""
        # Note: No caching - we want fresh metrics each heartbeat
        gpus = []

        # Try nvidia-smi for NVIDIA GPUs with real-time metrics
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.total,memory.free,driver_version,utilization.gpu,utilization.memory,temperature.gpu,power.draw",
                    "--format=csv,noheader,nounits"
                ],
                capture_output=True, text=True, timeout=10
            )

            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 5:
                        # Parse real-time metrics (may be [N/A] on some systems)
                        gpu_util = 0.0
                        mem_util = 0.0
                        temp = 0.0
                        power = None

                        if len(parts) >= 9:
                            try:
                                gpu_util = float(parts[5]) if parts[5] not in ["[N/A]", "[Not Supported]"] else 0.0
                            except ValueError:
                                gpu_util = 0.0
                            try:
                                mem_util = float(parts[6]) if parts[6] not in ["[N/A]", "[Not Supported]"] else 0.0
                            except ValueError:
                                mem_util = 0.0
                            try:
                                temp = float(parts[7]) if parts[7] not in ["[N/A]", "[Not Supported]"] else 0.0
                            except ValueError:
                                temp = 0.0
                            try:
                                power = float(parts[8]) if parts[8] not in ["[N/A]", "[Not Supported]"] else None
                            except ValueError:
                                power = None

                        gpu = GPUInfo(
                            index=int(parts[0]),
                            name=parts[1],
                            memory_total_mb=int(float(parts[2])),
                            memory_free_mb=int(float(parts[3])),
                            driver_version=parts[4],
                            nvenc_supported=self._check_nvenc_support(parts[1]),
                            nvdec_supported=self._check_nvenc_support(parts[1]),
                            gpu_utilization_percent=gpu_util,
                            memory_utilization_percent=mem_util,
                            temperature_c=temp,
                            power_draw_w=power
                        )
                        gpus.append(gpu)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Try detecting CUDA version
        if gpus:
            cuda_version = self._get_cuda_version()
            for gpu in gpus:
                gpu.cuda_version = cuda_version

        return gpus

    def _check_nvenc_support(self, gpu_name: str) -> bool:
        """Check if GPU likely supports NVENC (simplified heuristic)."""
        # Most modern NVIDIA GPUs support NVENC
        nvenc_families = ["RTX", "GTX 16", "GTX 10", "Quadro", "Tesla", "A100", "A10", "H100", "L40"]
        return any(fam in gpu_name for fam in nvenc_families)

    def _get_cuda_version(self) -> str:
        """Get CUDA version if available."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5
            )
            # nvidia-smi shows CUDA version in header
            result2 = subprocess.run(
                ["nvidia-smi"],
                capture_output=True, text=True, timeout=5
            )
            for line in result2.stdout.split("\n"):
                if "CUDA Version" in line:
                    # Extract version like "CUDA Version: 12.2"
                    parts = line.split("CUDA Version:")
                    if len(parts) > 1:
                        return parts[1].strip().split()[0]
        except Exception:
            pass
        return ""

    def probe_network(self) -> NetworkInfo:
        """Detect network information."""
        hostname = socket.gethostname()

        # Get primary IP
        primary_ip = "127.0.0.1"
        try:
            # Connect to a public DNS to determine our primary IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            primary_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass

        return NetworkInfo(
            hostname=hostname,
            primary_ip=primary_ip,
            bandwidth_estimate_mbps=None  # Set by benchmark if run
        )

    def probe_livepeer(self) -> LivepeerInfo:
        """Detect Livepeer-specific capabilities."""
        info = LivepeerInfo()

        # Check if livepeer is running
        try:
            for proc in psutil.process_iter(['name', 'cmdline']):
                if 'livepeer' in proc.info['name'].lower():
                    info.transcoder_available = True

                    # Try to extract orchestrator address from cmdline
                    cmdline = proc.info.get('cmdline', [])
                    for i, arg in enumerate(cmdline):
                        if arg in ['-orchAddr', '-orch']:
                            if i + 1 < len(cmdline):
                                info.orchestrator_address = cmdline[i + 1]
                    break
        except Exception:
            pass

        # Default supported codecs for NVIDIA transcoding
        if info.transcoder_available or self.probe_gpus():
            info.supported_codecs = ["h264", "hevc", "vp8", "vp9"]
            info.supported_profiles = ["P720p60fps16x9", "P720p30fps16x9", "P1080p30fps16x9"]

        return info

    def _get_os_info(self) -> str:
        """Get OS version string."""
        return f"{platform.system()} {platform.release()}"

    def _detect_container_runtime(self) -> Optional[str]:
        """Detect if running in a container."""
        # Check for Docker
        if Path("/.dockerenv").exists():
            return "docker"

        # Check cgroup for container indicators
        try:
            with open("/proc/1/cgroup") as f:
                content = f.read()
                if "docker" in content:
                    return "docker"
                if "kubepods" in content:
                    return "kubernetes"
                if "lxc" in content:
                    return "lxc"
        except Exception:
            pass

        return None


# Convenience function
def probe_capabilities() -> NodeCapabilities:
    """Probe all system capabilities."""
    return CapabilityProber().probe_all()
