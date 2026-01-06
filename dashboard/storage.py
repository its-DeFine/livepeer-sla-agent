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

        # Verification jobs: job_id -> VerificationJob
        self._jobs: dict[str, VerificationJob] = {}

        # Load from disk if available
        if persist_path and persist_path.exists():
            self._load()

    def record_attestation(self, attestation: AttestationPayload) -> NodeRecord:
        """Record an attestation and update node record."""
        with self._lock:
            now = datetime.utcnow()
            node_id = attestation.node_id

            # Update or create node record
            if node_id in self._nodes:
                node = self._nodes[node_id]
                node.last_seen = now
                node.attestation_count += 1
                node.last_capabilities = attestation.payload.get("capabilities")
                node.is_online = True
            else:
                node = NodeRecord(
                    node_id=node_id,
                    first_seen=now,
                    last_seen=now,
                    attestation_count=1,
                    last_capabilities=attestation.payload.get("capabilities"),
                    is_online=True
                )
                self._nodes[node_id] = node

            # Store attestation
            self._attestations[node_id].append((attestation.timestamp, attestation.payload))

            # Trim old attestations
            if len(self._attestations[node_id]) > self._max_attestations_per_node:
                self._attestations[node_id] = self._attestations[node_id][-self._max_attestations_per_node:]

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
            self._persist()

    def update_verification_job(self, job_id: str, **updates):
        """Update a verification job."""
        with self._lock:
            if job_id in self._jobs:
                job = self._jobs[job_id]
                for key, value in updates.items():
                    setattr(job, key, value)
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

    def get_network_stats(self) -> dict:
        """Get network-wide statistics including GPU metrics."""
        with self._lock:
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
                }
            }

            # Convert datetime to ISO format
            for node_data in data["nodes"].values():
                node_data["first_seen"] = node_data["first_seen"].isoformat()
                node_data["last_seen"] = node_data["last_seen"].isoformat()

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
