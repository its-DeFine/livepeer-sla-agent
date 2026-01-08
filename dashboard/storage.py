"""
Simple in-memory storage for the dashboard.

In production, you'd swap this for PostgreSQL/Redis/etc.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from collections import defaultdict
import threading
import time

from .models import NodeRecord, AttestationPayload, VerificationJob


class Storage:
    """
    In-memory storage with optional file persistence.

    Thread-safe for concurrent access from API handlers.
    """

    def __init__(self, persist_path: Optional[Path] = None):
        self.persist_path = persist_path
        self._lock = threading.RLock()

        # Node registry: node_id -> NodeRecord
        self._nodes: dict[str, NodeRecord] = {}

        # Recent attestations: node_id -> list of (timestamp, payload)
        self._attestations: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        self._max_attestations_per_node = 100

        # Heartbeat timestamps (for uptime calculations): node_id -> list[timestamp]
        self._heartbeats: dict[str, list[int]] = defaultdict(list)
        self._max_heartbeats_per_node = 10_000

        # Verification jobs: job_id -> VerificationJob
        self._jobs: dict[str, VerificationJob] = {}
        self._max_jobs = 2_000

        # Load from disk if available
        if persist_path and persist_path.exists():
            self._load()

    def refresh_online_status(self, timeout_minutes: int = 5) -> None:
        """Refresh in-memory online/offline status for all nodes."""
        with self._lock:
            cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
            for node in self._nodes.values():
                node.is_online = node.last_seen > cutoff

    def record_attestation(self, attestation: AttestationPayload) -> NodeRecord:
        """Record an attestation and update node record."""
        with self._lock:
            now = datetime.utcnow()
            node_id = attestation.node_id
            payload_type = attestation.payload.get("type")
            heartbeat_interval = attestation.payload.get("heartbeat_interval_seconds")

            # Update or create node record
            if node_id in self._nodes:
                node = self._nodes[node_id]
                node.last_seen = now
                node.attestation_count += 1
                node.last_capabilities = attestation.payload.get("capabilities")
                if isinstance(heartbeat_interval, int):
                    node.last_heartbeat_interval_seconds = heartbeat_interval
                node.is_online = True
            else:
                node = NodeRecord(
                    node_id=node_id,
                    first_seen=now,
                    last_seen=now,
                    attestation_count=1,
                    last_capabilities=attestation.payload.get("capabilities"),
                    last_heartbeat_interval_seconds=heartbeat_interval if isinstance(heartbeat_interval, int) else None,
                    is_online=True
                )
                self._nodes[node_id] = node

            # Store attestation
            self._attestations[node_id].append((attestation.timestamp, attestation.payload))

            # Trim old attestations
            if len(self._attestations[node_id]) > self._max_attestations_per_node:
                self._attestations[node_id] = self._attestations[node_id][-self._max_attestations_per_node:]

            # Track heartbeat timestamps for uptime calculations
            if payload_type == "heartbeat" and isinstance(attestation.timestamp, int):
                self._heartbeats[node_id].append(attestation.timestamp)
                if len(self._heartbeats[node_id]) > self._max_heartbeats_per_node:
                    self._heartbeats[node_id] = self._heartbeats[node_id][-self._max_heartbeats_per_node:]

            self._persist()
            return node

    def get_node(self, node_id: str) -> Optional[NodeRecord]:
        """Get a node record."""
        with self._lock:
            return self._nodes.get(node_id)

    def get_all_nodes(self) -> list[NodeRecord]:
        """Get all known nodes."""
        with self._lock:
            return list(self._nodes.values())

    def get_online_nodes(self, timeout_minutes: int = 5) -> list[NodeRecord]:
        """Get nodes that have reported recently."""
        self.refresh_online_status(timeout_minutes=timeout_minutes)
        with self._lock:
            cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
            online = []
            for node in self._nodes.values():
                if node.last_seen > cutoff:
                    node.is_online = True
                    online.append(node)
                else:
                    node.is_online = False
            return online

    def get_node_attestations(self, node_id: str, limit: int = 10) -> list[dict]:
        """Get recent attestations for a node."""
        with self._lock:
            attestations = self._attestations.get(node_id, [])
            return [
                {"timestamp": ts, "payload": payload}
                for ts, payload in attestations[-limit:]
            ]

    def record_verification_job(self, job: VerificationJob):
        """Record a verification job."""
        with self._lock:
            self._jobs[job.job_id] = job
            self._trim_jobs_unlocked()
            self._persist()

    def update_verification_job(self, job_id: str, **updates):
        """Update a verification job."""
        with self._lock:
            if job_id in self._jobs:
                job = self._jobs[job_id]
                for key, value in updates.items():
                    setattr(job, key, value)
                self._trim_jobs_unlocked()
                self._persist()

    def get_verification_jobs(self, node_id: Optional[str] = None, limit: int = 50) -> list[VerificationJob]:
        """Get verification jobs, optionally filtered by node."""
        with self._lock:
            jobs = list(self._jobs.values())
            if node_id:
                jobs = [j for j in jobs if j.node_id == node_id]
            return sorted(jobs, key=lambda j: j.created_at, reverse=True)[:limit]

    def update_verification_score(self, node_id: str, score: float):
        """Update a node's verification score."""
        with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id].verification_score = score
                self._persist()

    def get_node_uptime(self, node_id: str, period_seconds: int = 3600, grace_multiplier: float = 2.5) -> dict:
        """Compute uptime over a rolling window from heartbeat timestamps."""
        with self._lock:
            node = self._nodes.get(node_id)
            interval = node.last_heartbeat_interval_seconds if node else None
            timestamps = list(self._heartbeats.get(node_id, []))

        now = int(time.time())
        window_start = now - period_seconds

        if not timestamps:
            return {
                "period_seconds": period_seconds,
                "uptime_percent": 0.0,
                "downtime_seconds": period_seconds,
                "samples": 0,
                "heartbeat_interval_seconds": interval,
            }

        timestamps.sort()
        in_window = [t for t in timestamps if t >= window_start and t <= now]
        prev_before = None
        for t in reversed(timestamps):
            if t < window_start:
                prev_before = t
                break

        relevant = ([prev_before] if prev_before is not None else []) + in_window
        if not relevant:
            return {
                "period_seconds": period_seconds,
                "uptime_percent": 0.0,
                "downtime_seconds": period_seconds,
                "samples": 0,
                "heartbeat_interval_seconds": interval,
            }

        if interval is None:
            diffs = [b - a for a, b in zip(relevant, relevant[1:]) if (b - a) > 0]
            if diffs:
                diffs.sort()
                interval = diffs[len(diffs) // 2]
        if not interval or interval <= 0:
            interval = 60

        grace_seconds = max(1, int(interval * grace_multiplier))
        downtime = 0

        last = relevant[0]
        coverage_end = last + grace_seconds
        if window_start > coverage_end:
            first = relevant[1] if len(relevant) > 1 else None
            if first is None:
                downtime = period_seconds
            else:
                downtime += max(0, min(first, now) - window_start)

        for prev_t, t in zip(relevant, relevant[1:]):
            gap = t - prev_t
            if gap <= grace_seconds:
                continue
            offline_start = prev_t + grace_seconds
            offline_end = t
            downtime += max(0, min(offline_end, now) - max(offline_start, window_start))

        last_t = relevant[-1]
        tail_offline_start = last_t + grace_seconds
        if now > tail_offline_start:
            downtime += max(0, now - max(tail_offline_start, window_start))

        downtime = min(max(downtime, 0), period_seconds)
        uptime_percent = round(max(0.0, 100.0 * (1.0 - (downtime / period_seconds))), 2)

        return {
            "period_seconds": period_seconds,
            "uptime_percent": uptime_percent,
            "downtime_seconds": int(downtime),
            "samples": len(in_window),
            "heartbeat_interval_seconds": int(interval),
        }

    def get_node_verification_stats(self, node_id: str, period_seconds: int = 3600) -> dict:
        """Compute verification success/error rates over a rolling window."""
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=period_seconds)

        with self._lock:
            jobs = [j for j in self._jobs.values() if j.node_id == node_id and j.created_at >= cutoff]

        total = len(jobs)
        completed = [j for j in jobs if j.completed_at is not None and j.success is not None]
        successes = sum(1 for j in completed if j.success)
        failures = sum(1 for j in completed if j.success is False)
        success_rate = (successes / len(completed)) if completed else 0.0

        return {
            "period_seconds": period_seconds,
            "total_jobs": total,
            "completed_jobs": len(completed),
            "successes": successes,
            "failures": failures,
            "success_rate": round(success_rate, 3),
        }

    def get_network_stats(self) -> dict:
        """Get network-wide statistics including GPU metrics."""
        with self._lock:
            self.refresh_online_status(timeout_minutes=5)
            nodes = list(self._nodes.values())
            online = [n for n in nodes if n.is_online]

            total_gpus = 0
            total_memory_gb = 0
            gpu_types: dict[str, int] = defaultdict(int)

            # GPU real-time metrics aggregation
            gpu_utilizations: list[float] = []
            gpu_temperatures: list[float] = []
            total_power_draw = 0.0
            gpus_with_power = 0

            for node in online:
                caps = node.last_capabilities or {}
                gpus = caps.get("gpus", [])
                total_gpus += len(gpus)
                for gpu in gpus:
                    gpu_types[gpu.get("name", "Unknown")] += 1

                    # Collect real-time GPU metrics
                    util = gpu.get("gpu_utilization_percent", 0)
                    if util > 0:
                        gpu_utilizations.append(util)

                    temp = gpu.get("temperature_c", 0)
                    if temp > 0:
                        gpu_temperatures.append(temp)

                    power = gpu.get("power_draw_w")
                    if power is not None and power > 0:
                        total_power_draw += power
                        gpus_with_power += 1

                mem = caps.get("memory", {})
                total_memory_gb += mem.get("total_mb", 0) / 1024

            # Calculate averages
            avg_gpu_utilization = round(sum(gpu_utilizations) / len(gpu_utilizations), 1) if gpu_utilizations else 0.0
            avg_gpu_temperature = round(sum(gpu_temperatures) / len(gpu_temperatures), 1) if gpu_temperatures else 0.0

            return {
                "total_nodes": len(nodes),
                "online_nodes": len(online),
                "total_gpus": total_gpus,
                "total_memory_gb": round(total_memory_gb, 1),
                "gpu_types": dict(gpu_types),
                "total_attestations": sum(n.attestation_count for n in nodes),
                # GPU real-time metrics
                "avg_gpu_utilization": avg_gpu_utilization,
                "avg_gpu_temperature": avg_gpu_temperature,
                "total_power_draw_w": round(total_power_draw, 1),
                "gpus_reporting_metrics": len(gpu_utilizations)
            }

    def _trim_jobs_unlocked(self) -> None:
        """Trim verification jobs to avoid unbounded growth (lock must be held)."""
        if len(self._jobs) <= self._max_jobs:
            return
        jobs_sorted = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        keep = {j.job_id for j in jobs_sorted[: self._max_jobs]}
        for job_id in list(self._jobs.keys()):
            if job_id not in keep:
                self._jobs.pop(job_id, None)

    def _persist(self):
        """Persist to disk (if configured)."""
        if not self.persist_path:
            return

        try:
            data = {
                "nodes": {
                    nid: node.model_dump()
                    for nid, node in self._nodes.items()
                },
                "attestations": {
                    nid: atts
                    for nid, atts in self._attestations.items()
                },
                "heartbeats": dict(self._heartbeats),
                "jobs": {
                    jid: job.model_dump()
                    for jid, job in self._jobs.items()
                },
            }

            # Convert datetime to ISO format
            for node_data in data["nodes"].values():
                node_data["first_seen"] = node_data["first_seen"].isoformat()
                node_data["last_seen"] = node_data["last_seen"].isoformat()

            for job_data in data["jobs"].values():
                job_data["created_at"] = job_data["created_at"].isoformat()
                if job_data.get("completed_at"):
                    job_data["completed_at"] = job_data["completed_at"].isoformat()

            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            self.persist_path.write_text(json.dumps(data, indent=2, default=str))
        except Exception:
            pass  # Don't fail on persistence errors

    def _load(self):
        """Load from disk."""
        try:
            data = json.loads(self.persist_path.read_text())

            for nid, node_data in data.get("nodes", {}).items():
                node_data["first_seen"] = datetime.fromisoformat(node_data["first_seen"])
                node_data["last_seen"] = datetime.fromisoformat(node_data["last_seen"])
                self._nodes[nid] = NodeRecord(**node_data)

            for nid, atts in data.get("attestations", {}).items():
                self._attestations[nid] = atts

            for nid, ts_list in data.get("heartbeats", {}).items():
                if isinstance(ts_list, list):
                    self._heartbeats[nid] = [int(t) for t in ts_list if isinstance(t, int) or (isinstance(t, str) and t.isdigit())]

            for jid, job_data in data.get("jobs", {}).items():
                try:
                    job_data["created_at"] = datetime.fromisoformat(job_data["created_at"])
                    if job_data.get("completed_at"):
                        job_data["completed_at"] = datetime.fromisoformat(job_data["completed_at"])
                    self._jobs[jid] = VerificationJob(**job_data)
                except Exception:
                    continue
        except Exception:
            pass  # Start fresh on load errors


# Global storage instance
_storage: Optional[Storage] = None


def get_storage() -> Storage:
    """Get the global storage instance."""
    global _storage
    if _storage is None:
        _storage = Storage(persist_path=Path("./data/dashboard.json"))
    return _storage
