"""
Payments-backend integration for paying orchestrators for verified workloads.

This is intentionally optional: the SLA dashboard can run standalone without any
payments backend configured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _parse_positive_decimal(value: str) -> Optional[str]:
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        dec = Decimal(candidate)
    except (InvalidOperation, ValueError):
        return None
    if dec <= 0:
        return None
    # Preserve user precision; normalize only for comparisons.
    return str(dec)


@dataclass
class PaymentsConfig:
    base_url: str
    admin_token: str
    payout_liveness_eth: Optional[str] = None
    payout_transcode_eth: Optional[str] = None
    payout_gpu_benchmark_eth: Optional[str] = None


class PaymentsClient:
    def __init__(self, config: PaymentsConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "PaymentsClient":
        self._client = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def is_enabled(self) -> bool:
        return bool(self.config.base_url and self.config.admin_token)

    def payout_for(self, challenge_type: str) -> Optional[str]:
        if challenge_type == "liveness":
            return self.config.payout_liveness_eth
        if challenge_type == "transcode":
            return self.config.payout_transcode_eth
        if challenge_type == "gpu_benchmark":
            return self.config.payout_gpu_benchmark_eth
        return None

    async def resolve_orchestrator_id(self, eth_address: str) -> Optional[str]:
        """Resolve payments orchestrator_id by ETH address (admin-only)."""
        if not self._client:
            raise RuntimeError("PaymentsClient not started")

        target = (eth_address or "").strip().lower()
        if not target:
            return None

        resp = await self._client.get(
            f"{self.config.base_url}/api/orchestrators",
            headers={"X-Admin-Token": self.config.admin_token},
        )
        resp.raise_for_status()
        payload = resp.json()
        orchestrators = payload.get("orchestrators", [])
        if not isinstance(orchestrators, list):
            return None

        for item in orchestrators:
            if not isinstance(item, dict):
                continue
            addr = str(item.get("address") or "").strip().lower()
            if addr and addr == target:
                orch_id = str(item.get("orchestrator_id") or "").strip()
                return orch_id or None
        return None

    async def credit_verified_workload(
        self,
        *,
        workload_id: str,
        orchestrator_id: str,
        payout_amount_eth: str,
        artifact_hash: str,
        plan_id: Optional[str] = None,
        run_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        """
        Create a workload record then mark it verified to trigger credit.

        This uses admin token auth and is safe to call multiple times for the same workload_id.
        """
        if not self._client:
            raise RuntimeError("PaymentsClient not started")

        payout = _parse_positive_decimal(payout_amount_eth)
        if not payout:
            raise ValueError("payout_amount_eth must be > 0")

        create_payload = {
            "workload_id": workload_id,
            "orchestrator_id": orchestrator_id,
            "plan_id": plan_id,
            "run_id": run_id,
            "artifact_hash": artifact_hash,
            "artifact_uri": None,
            "payout_amount_eth": payout,
            "notes": notes,
        }

        created: Optional[dict] = None
        try:
            resp = await self._client.post(
                f"{self.config.base_url}/api/workloads",
                headers={"X-Admin-Token": self.config.admin_token},
                json=create_payload,
            )
            if resp.status_code == 409:
                created = {"workload_id": workload_id, "status": "exists"}
            else:
                resp.raise_for_status()
                created = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("payments: create workload failed: %s", exc)
            raise

        # Mark verified to trigger credit.
        resp = await self._client.patch(
            f"{self.config.base_url}/api/workloads/{workload_id}",
            headers={"X-Admin-Token": self.config.admin_token},
            json={"status": "verified"},
        )
        resp.raise_for_status()
        updated = resp.json()

        return {
            "created": created,
            "updated": updated,
        }

