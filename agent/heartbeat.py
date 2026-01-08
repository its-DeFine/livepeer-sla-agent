"""
Heartbeat publisher - sends signed attestations to dashboard at regular intervals.

Each heartbeat includes:
- Node identity (public key)
- Timestamp (proves liveness)
- Current capabilities
- Signature (proves authenticity)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Callable
from datetime import datetime

import httpx

from .identity import NodeIdentity, SignedAttestation
from .capabilities import CapabilityProber, NodeCapabilities

logger = logging.getLogger(__name__)


class HeartbeatPublisher:
    """
    Publishes signed capability attestations to a dashboard endpoint.

    The dashboard can verify signatures without needing our private key.
    """

    def __init__(
        self,
        identity: NodeIdentity,
        dashboard_url: str,
        interval_seconds: int = 60,
        capability_prober: Optional[CapabilityProber] = None,
    ):
        self.identity = identity
        self.dashboard_url = dashboard_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.prober = capability_prober or CapabilityProber()

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None

        # Callbacks
        self._on_heartbeat: Optional[Callable[[SignedAttestation], None]] = None
        self._on_error: Optional[Callable[[Exception], None]] = None

        # Stats
        self.heartbeats_sent = 0
        self.heartbeats_failed = 0
        self.last_heartbeat: Optional[datetime] = None

    def on_heartbeat(self, callback: Callable[[SignedAttestation], None]):
        """Register callback for successful heartbeats."""
        self._on_heartbeat = callback

    def on_error(self, callback: Callable[[Exception], None]):
        """Register callback for errors."""
        self._on_error = callback

    async def start(self):
        """Start the heartbeat loop."""
        if self._running:
            return

        self._running = True
        self._client = httpx.AsyncClient(timeout=30.0)
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Heartbeat publisher started (interval: {self.interval_seconds}s)")

    async def stop(self):
        """Stop the heartbeat loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        logger.info("Heartbeat publisher stopped")

    async def _heartbeat_loop(self):
        """Main heartbeat loop."""
        while self._running:
            try:
                await self.send_heartbeat()
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
                self.heartbeats_failed += 1
                if self._on_error:
                    self._on_error(e)

            await asyncio.sleep(self.interval_seconds)

    async def send_heartbeat(self) -> SignedAttestation:
        """Send a single heartbeat with current capabilities."""
        # Probe current capabilities
        capabilities = self.prober.probe_all()

        # Create signed attestation
        attestation = self.identity.sign_attestation({
            "type": "heartbeat",
            "heartbeat_interval_seconds": self.interval_seconds,
            "capabilities": capabilities.to_dict()
        })

        # Send to dashboard
        response = await self._client.post(
            f"{self.dashboard_url}/api/v1/attestations",
            json={
                "node_id": attestation.node_id,
                "timestamp": attestation.timestamp,
                "payload": attestation.payload,
                "signature": attestation.signature
            }
        )
        response.raise_for_status()

        # Update stats
        self.heartbeats_sent += 1
        self.last_heartbeat = datetime.now()

        logger.info(f"Heartbeat sent (total: {self.heartbeats_sent})")

        if self._on_heartbeat:
            self._on_heartbeat(attestation)

        return attestation

    async def send_single(self) -> SignedAttestation:
        """Send a single heartbeat without starting the loop."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            self._client = client
            return await self.send_heartbeat()


class LocalHeartbeatPublisher(HeartbeatPublisher):
    """
    Heartbeat publisher that stores attestations locally (for testing/offline).
    """

    def __init__(
        self,
        identity: NodeIdentity,
        storage_path: str = "./attestations",
        **kwargs
    ):
        super().__init__(identity, dashboard_url="http://localhost", **kwargs)
        self.storage_path = storage_path

    async def send_heartbeat(self) -> SignedAttestation:
        """Store attestation locally instead of sending to dashboard."""
        import json
        from pathlib import Path

        capabilities = self.prober.probe_all()
        attestation = self.identity.sign_attestation({
            "type": "heartbeat",
            "capabilities": capabilities.to_dict()
        })

        # Store locally
        storage = Path(self.storage_path)
        storage.mkdir(parents=True, exist_ok=True)

        filename = f"{attestation.timestamp}_{attestation.node_id[:16]}.json"
        (storage / filename).write_text(attestation.to_json())

        self.heartbeats_sent += 1
        self.last_heartbeat = datetime.now()

        logger.info(f"Attestation stored locally: {filename}")

        if self._on_heartbeat:
            self._on_heartbeat(attestation)

        return attestation
