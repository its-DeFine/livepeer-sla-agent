"""
Dashboard server - collects attestations and provides verification API.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from pathlib import Path
import socket
import ipaddress
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agent.identity import NodeIdentity, SignedAttestation, create_endpoint_registration_message
from agent.livepeer import get_livepeer_network, LivepeerNetwork
from agent.eth_link import AddressLink, AddressLinkRegistry, verify_address_link, create_link_for_signing
from agent.onchain import get_subgraph, LivepeerSubgraph
from .models import AttestationPayload
from .storage import get_storage, Storage
from .verifier import get_verifier, Verifier
from .proofs import get_proof_generator, get_proof_storage, ProofGenerator, ProofStorage
from .payments import PaymentsClient, PaymentsConfig, _parse_positive_decimal

logger = logging.getLogger(__name__)


# Request models
class RegisterEndpointRequest(BaseModel):
    node_id: str
    agent_url: str
    timestamp: int
    signature: str
    eth_address: Optional[str] = None  # Optional ETH address to link


class VerifyRequest(BaseModel):
    node_id: str
    challenge_type: str = "liveness"  # or "transcode" or "gpu_benchmark"
    profile: str = "P720p30fps16x9"
    benchmark_type: str = "matrix_4096"  # For GPU benchmark challenges
    gpu_index: Optional[int] = None  # None = test all GPUs


class LinkAddressRequest(BaseModel):
    node_id: str
    eth_address: str
    timestamp: int
    message: str
    eth_signature: str


class VerifyProofRequest(BaseModel):
    proof_json: str


# Global instances
_storage: Optional[Storage] = None
_verifier: Optional[Verifier] = None
_livepeer: Optional[LivepeerNetwork] = None
_link_registry: Optional[AddressLinkRegistry] = None
_proof_generator: Optional[ProofGenerator] = None
_proof_storage: Optional[ProofStorage] = None
_subgraph: Optional[LivepeerSubgraph] = None
_payments: Optional[PaymentsClient] = None
_auto_verify_task: Optional[asyncio.Task] = None


def create_app() -> FastAPI:
    """Create the dashboard FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _storage, _verifier, _livepeer, _link_registry, _proof_generator, _proof_storage, _subgraph, _payments, _auto_verify_task
        _storage = get_storage()
        _verifier = get_verifier()
        _livepeer = get_livepeer_network()
        _link_registry = AddressLinkRegistry(persist_path=Path("./data/links.json"))
        _proof_generator = get_proof_generator()
        _proof_storage = get_proof_storage()
        _subgraph = get_subgraph()

        payments_url = os.environ.get("PAYMENTS_BACKEND_URL", "").strip().rstrip("/")
        payments_admin = os.environ.get("PAYMENTS_ADMIN_TOKEN", "").strip()
        if payments_url and payments_admin:
            config = PaymentsConfig(
                base_url=payments_url,
                admin_token=payments_admin,
                payout_liveness_eth=_parse_positive_decimal(os.environ.get("PAYMENTS_PAYOUT_LIVENESS_ETH", "")),
                payout_transcode_eth=_parse_positive_decimal(os.environ.get("PAYMENTS_PAYOUT_TRANSCODE_ETH", "")),
                payout_gpu_benchmark_eth=_parse_positive_decimal(os.environ.get("PAYMENTS_PAYOUT_GPU_BENCHMARK_ETH", "")),
                offer_id_liveness=(os.environ.get("PAYMENTS_OFFER_ID_LIVENESS", "").strip() or None),
                offer_id_transcode=(os.environ.get("PAYMENTS_OFFER_ID_TRANSCODE", "").strip() or None),
                offer_id_gpu_benchmark=(os.environ.get("PAYMENTS_OFFER_ID_GPU_BENCHMARK", "").strip() or None),
            )
            _payments = PaymentsClient(config)
            await _payments.__aenter__()
            logger.info("Payments integration enabled: %s", payments_url)
        else:
            _payments = None

        await _verifier.__aenter__()
        await _subgraph.__aenter__()

        async def _auto_verify_loop() -> None:
            interval_s = float(os.environ.get("AUTO_VERIFY_INTERVAL_SECONDS", "1800") or 1800)
            jitter_frac = float(os.environ.get("AUTO_VERIFY_JITTER_FRACTION", "0.5") or 0.5)
            timeout_minutes = int(os.environ.get("AUTO_VERIFY_ONLINE_TIMEOUT_MINUTES", "10") or 10)
            only_linked = os.environ.get("AUTO_VERIFY_ONLY_LINKED", "true").strip().lower() in {"1", "true", "yes"}
            cooldown_s = float(os.environ.get("AUTO_VERIFY_NODE_COOLDOWN_SECONDS", "0") or 0)

            challenge_types_raw = os.environ.get("AUTO_VERIFY_CHALLENGE_TYPES", "liveness,transcode,gpu_benchmark")
            challenge_types = [c.strip() for c in challenge_types_raw.split(",") if c.strip()]

            transcode_profiles_raw = os.environ.get("AUTO_VERIFY_TRANSCODE_PROFILES", "P720p30fps16x9")
            transcode_profiles = [p.strip() for p in transcode_profiles_raw.split(",") if p.strip()]

            gpu_benchmarks_raw = os.environ.get("AUTO_VERIFY_GPU_BENCHMARK_TYPES", "matrix_4096")
            gpu_benchmarks = [b.strip() for b in gpu_benchmarks_raw.split(",") if b.strip()]

            last_verified_at: dict[str, float] = {}

            def _next_sleep() -> float:
                base = max(1.0, interval_s)
                frac = max(0.0, min(jitter_frac, 1.0))
                low = base * (1.0 - frac)
                high = base * (1.0 + frac)
                return low + (secrets.randbelow(10_000) / 10_000.0) * (high - low)

            logger.info(
                "Auto-verify enabled: interval=%ss jitter=%s types=%s",
                interval_s,
                jitter_frac,
                ",".join(challenge_types),
            )

            while True:
                try:
                    candidates = _storage.get_online_nodes(timeout_minutes=timeout_minutes)
                    eligible: list[str] = []
                    now = time.time()
                    for node in candidates:
                        if not _verifier.get_endpoint(node.node_id):
                            continue
                        if only_linked and not _link_registry.get_eth_address(node.node_id):
                            continue
                        last = last_verified_at.get(node.node_id)
                        if cooldown_s > 0 and last is not None and (now - last) < cooldown_s:
                            continue
                        eligible.append(node.node_id)

                    if not eligible:
                        await asyncio.sleep(_next_sleep())
                        continue

                    node_id = eligible[secrets.randbelow(len(eligible))]
                    eth_address = _link_registry.get_eth_address(node_id)

                    allowed_types = list(challenge_types)
                    if _payments and eth_address:
                        orchestrator_id = await _payments.resolve_orchestrator_id(eth_address)
                        if orchestrator_id:
                            filtered: list[str] = []
                            for challenge_type in allowed_types:
                                offer_id = _payments.offer_id_for(challenge_type)
                                if not offer_id:
                                    filtered.append(challenge_type)
                                    continue
                                if await _payments.is_subscribed(orchestrator_id, offer_id):
                                    filtered.append(challenge_type)
                            allowed_types = filtered
                        else:
                            allowed_types = [c for c in allowed_types if not _payments.offer_id_for(c)]
                    elif _payments:
                        allowed_types = [c for c in allowed_types if not _payments.offer_id_for(c)]

                    if not allowed_types:
                        await asyncio.sleep(_next_sleep())
                        continue

                    challenge_type = allowed_types[secrets.randbelow(len(allowed_types))]
                    payload = {
                        "node_id": node_id,
                        "challenge_type": challenge_type,
                        "profile": (
                            transcode_profiles[secrets.randbelow(len(transcode_profiles))]
                            if challenge_type == "transcode" and transcode_profiles
                            else "P720p30fps16x9"
                        ),
                        "benchmark_type": (
                            gpu_benchmarks[secrets.randbelow(len(gpu_benchmarks))]
                            if challenge_type == "gpu_benchmark" and gpu_benchmarks
                            else "matrix_4096"
                        ),
                        "gpu_index": None,
                    }

                    result = await _run_verification(VerifyRequest(**payload))
                    last_verified_at[node_id] = time.time()
                    logger.info(
                        "auto-verify: node=%s type=%s success=%s",
                        node_id,
                        challenge_type,
                        bool(result.get("success")),
                    )
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("auto-verify loop error: %s", exc)
                await asyncio.sleep(_next_sleep())

        if os.environ.get("AUTO_VERIFY_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
            _auto_verify_task = asyncio.create_task(_auto_verify_loop())
        logger.info("Dashboard started")
        yield
        if _auto_verify_task is not None:
            _auto_verify_task.cancel()
            try:
                await _auto_verify_task
            except asyncio.CancelledError:
                pass
            _auto_verify_task = None
        await _subgraph.__aexit__(None, None, None)
        await _verifier.__aexit__(None, None, None)
        if _payments is not None:
            await _payments.__aexit__(None, None, None)
        logger.info("Dashboard stopped")

    app = FastAPI(
        title="Livepeer SLA Dashboard",
        description="Collects and verifies node attestations for Livepeer orchestrators",
        version="0.2.0",
        lifespan=lifespan
    )

    async def _run_verification(request: VerifyRequest) -> dict:
        """
        Shared verification implementation used by both the API endpoint and optional auto-verify loop.
        """
        # Get linked ETH address and rank
        eth_address = _link_registry.get_eth_address(request.node_id)
        orchestrator_rank = None

        if eth_address:
            is_top, rank = await _livepeer.is_top_orchestrator(eth_address)
            orchestrator_rank = rank if is_top else None

        # Run verification
        if request.challenge_type == "liveness":
            result = await _verifier.verify_liveness(request.node_id)
        elif request.challenge_type == "transcode":
            result = await _verifier.verify_transcode(request.node_id, profile=request.profile)
        elif request.challenge_type == "gpu_benchmark":
            result = await _verifier.verify_gpu_benchmark(
                request.node_id,
                benchmark_type=request.benchmark_type,
                gpu_index=request.gpu_index
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid challenge type")

        # Create proof of verification
        proof = _proof_generator.create_verification_proof(
            node_id=request.node_id,
            verification_type=request.challenge_type,
            result=result,
            eth_address=eth_address,
            orchestrator_rank=orchestrator_rank
        )

        # Store proof
        _proof_storage.store_proof(proof)

        payments_result = None
        payout_eth = _payments.payout_for(request.challenge_type) if _payments else None
        offer_id = _payments.offer_id_for(request.challenge_type) if _payments else None

        if _payments and offer_id:
            try:
                offer = await _payments.get_offer(offer_id)
                if not offer:
                    payments_result = {"skipped": True, "reason": f"Offer not found: {offer_id}"}
                elif not bool(offer.get("active", False)):
                    payments_result = {"skipped": True, "reason": f"Offer inactive: {offer_id}"}
                else:
                    payout_eth = _parse_positive_decimal(str(offer.get("payout_amount_eth") or "")) or payout_eth
            except Exception as exc:
                payments_result = {"error": str(exc)}

        if payout_eth and result.get("success") and eth_address and _payments and payments_result is None:
            try:
                orchestrator_id = await _payments.resolve_orchestrator_id(eth_address)
                if orchestrator_id:
                    if offer_id:
                        subscribed = await _payments.is_subscribed(orchestrator_id, offer_id)
                        if not subscribed:
                            return {
                                **result,
                                "proof_id": proof.proof_id,
                                "proof": proof.to_dict(),
                                "payments": {
                                    "skipped": True,
                                    "reason": f"Orchestrator not opted into offer {offer_id}",
                                    "offer_id": offer_id,
                                    "orchestrator_id": orchestrator_id,
                                },
                            }

                    artifact_hash = (
                        str(result.get("output_hash") or "").strip()
                        or hashlib.sha256(
                            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                    )
                    run_id = (
                        str(result.get("job_id") or result.get("challenge_id") or "").strip()
                        or proof.proof_id
                    )
                    payments_result = await _payments.credit_verified_workload(
                        workload_id=f"sla-{proof.proof_id}",
                        orchestrator_id=orchestrator_id,
                        payout_amount_eth=payout_eth,
                        artifact_hash=artifact_hash,
                        plan_id=offer_id or request.challenge_type,
                        run_id=run_id,
                        notes=f"sla verification: type={request.challenge_type} node_id={request.node_id}",
                    )
                else:
                    payments_result = {
                        "skipped": True,
                        "reason": "Orchestrator not registered in payments (address not found)",
                    }
            except Exception as exc:
                payments_result = {"error": str(exc)}

        return {
            **result,
            "proof_id": proof.proof_id,
            "proof": proof.to_dict(),
            "payments": payments_result,
        }

    # ==================== Attestation Endpoints ====================

    @app.post("/api/v1/attestations")
    async def submit_attestation(attestation: AttestationPayload):
        """Receive an attestation from a node agent."""
        signed = SignedAttestation(
            node_id=attestation.node_id,
            timestamp=attestation.timestamp,
            payload=attestation.payload,
            signature=attestation.signature
        )

        if not NodeIdentity.verify_attestation(signed):
            raise HTTPException(status_code=400, detail="Invalid signature")

        if abs(time.time() - attestation.timestamp) > 300:
            raise HTTPException(status_code=400, detail="Timestamp too old or in future")

        node = _storage.record_attestation(attestation)

        return {
            "status": "accepted",
            "node_id": node.node_id,
            "attestation_count": node.attestation_count
        }

    @app.get("/api/v1/attestations/{node_id}")
    async def get_node_attestations(node_id: str, limit: int = Query(default=10, le=100)):
        """Get recent attestations for a node."""
        attestations = _storage.get_node_attestations(node_id, limit)
        if not attestations:
            raise HTTPException(status_code=404, detail="Node not found")
        return {"node_id": node_id, "attestations": attestations}

    # ==================== Node Endpoints ====================

    @app.get("/api/v1/nodes")
    async def list_nodes(online_only: bool = False):
        """List all known nodes."""
        _storage.refresh_online_status(timeout_minutes=5)
        nodes = _storage.get_online_nodes() if online_only else _storage.get_all_nodes()

        return {
            "count": len(nodes),
            "nodes": [
                {
                    "node_id": n.node_id,
                    "eth_address": _link_registry.get_eth_address(n.node_id),
                    "first_seen": n.first_seen.isoformat(),
                    "last_seen": n.last_seen.isoformat(),
                    "attestation_count": n.attestation_count,
                    "is_online": n.is_online,
                    "uptime_1h": _storage.get_node_uptime(n.node_id, period_seconds=3600),
                    "verification_1h": _storage.get_node_verification_stats(n.node_id, period_seconds=3600),
                    "verification_score": n.verification_score,
                    "trust_tier": _compute_trust_tier(n, _storage),
                    "capabilities_summary": _summarize_capabilities(n.last_capabilities)
                }
                for n in nodes
            ]
        }

    @app.get("/api/v1/nodes/{node_id}")
    async def get_node(node_id: str):
        """Get details for a specific node."""
        node = _storage.get_node(node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")

        eth_address = _link_registry.get_eth_address(node_id)
        orchestrator_rank = None

        if eth_address:
            is_top, rank = await _livepeer.is_top_orchestrator(eth_address)
            orchestrator_rank = rank

        return {
            "node_id": node.node_id,
            "eth_address": eth_address,
            "orchestrator_rank": orchestrator_rank,
            "first_seen": node.first_seen.isoformat(),
            "last_seen": node.last_seen.isoformat(),
            "attestation_count": node.attestation_count,
            "is_online": node.is_online,
            "uptime_1h": _storage.get_node_uptime(node_id, period_seconds=3600),
            "verification_1h": _storage.get_node_verification_stats(node_id, period_seconds=3600),
            "verification_score": node.verification_score,
            "trust_tier": _compute_trust_tier(node, _storage),
            "capabilities": node.last_capabilities
        }

    # ==================== Address Linking ====================

    @app.post("/api/v1/link/prepare")
    async def prepare_address_link(node_id: str, eth_address: str):
        """
        Prepare an address link request.

        Returns a message that must be signed by the ETH wallet.
        """
        return create_link_for_signing(node_id, eth_address)

    @app.post("/api/v1/link/submit")
    async def submit_address_link(request: LinkAddressRequest):
        """
        Submit a signed address link.

        The ETH signature proves the orchestrator controls both addresses.
        """
        link = AddressLink(
            node_id=request.node_id,
            eth_address=request.eth_address,
            timestamp=request.timestamp,
            message=request.message,
            eth_signature=request.eth_signature
        )

        try:
            if not _link_registry.add_link(link):
                raise HTTPException(status_code=400, detail="Invalid signature or link conflict")
        except ImportError as e:
            raise HTTPException(status_code=500, detail=str(e))

        # Check if this is a top orchestrator
        is_top, rank = await _livepeer.is_top_orchestrator(request.eth_address)

        return {
            "status": "linked",
            "node_id": request.node_id,
            "eth_address": request.eth_address,
            "is_top_100": is_top,
            "rank": rank
        }

    @app.get("/api/v1/link/{node_id}")
    async def get_address_link(node_id: str):
        """Get the address link for a node."""
        link = _link_registry.get_link(node_id)
        if not link:
            raise HTTPException(status_code=404, detail="No link found for node")

        is_top, rank = await _livepeer.is_top_orchestrator(link.eth_address)

        return {
            **link.to_dict(),
            "is_top_100": is_top,
            "rank": rank
        }

    # ==================== Top 100 Orchestrators ====================

    @app.get("/api/v1/orchestrators/top100")
    async def get_top100_orchestrators():
        """
        Get top 100 orchestrators with their verification status.

        This shows which top orchestrators have linked SLA agents and their verification status.
        """
        _storage.refresh_online_status(timeout_minutes=5)
        orchestrators = await _livepeer.get_top_orchestrators(100)

        result = []
        for orch in orchestrators:
            node_id = _link_registry.get_node_id(orch.eth_address)
            node = _storage.get_node(node_id) if node_id else None

            # Get latest proof if available
            proofs = _proof_storage.get_proofs_for_eth(orch.eth_address) if orch.eth_address else []
            latest_proof = proofs[-1] if proofs else None

            uptime_1h = _storage.get_node_uptime(node.node_id, period_seconds=3600) if node else None
            verification_1h = _storage.get_node_verification_stats(node.node_id, period_seconds=3600) if node else None

            trust_tier = _compute_trust_tier(node, _storage) if node else {"tier": "UNKNOWN", "badge": "❓", "description": "Not linked"}

            result.append({
                "rank": orch.rank,
                "eth_address": orch.eth_address,
                "total_stake": orch.total_stake,
                "service_uri": orch.service_uri,
                "node_id": node_id,
                "is_linked": node_id is not None,
                "is_online": node.is_online if node else False,
                "uptime_1h": uptime_1h,
                "verification_1h": verification_1h,
                "verification_score": node.verification_score if node else 0,
                "trust_tier": trust_tier,
                "last_verified": latest_proof.timestamp if latest_proof else None,
                "attestation_count": node.attestation_count if node else 0
            })

        # Calculate summary stats
        linked_count = sum(1 for o in result if o["is_linked"])
        online_count = sum(1 for o in result if o["is_online"])
        verified_count = sum(1 for o in result if o["last_verified"])

        return {
            "timestamp": int(time.time()),
            "summary": {
                "total": len(result),
                "linked": linked_count,
                "online": online_count,
                "verified": verified_count
            },
            "orchestrators": result
        }

    # ==================== On-Chain Ticket Redemptions ====================

    @app.get("/api/v1/orchestrators/{eth_address}/tickets")
    async def get_orchestrator_tickets(
        eth_address: str,
        limit: int = Query(default=50, le=100),
        hours: int = Query(default=6, le=24, description="Hours to look back (max 24 due to RPC limits)")
    ):
        """
        Get on-chain ticket redemptions for an orchestrator.

        Tickets represent verifiable proof of work - each winning ticket
        is a micropayment from a broadcaster for transcoding work done.

        Note: Uses direct RPC queries, limited to ~24 hours on public RPC.
        """
        redemptions = await _subgraph.get_ticket_redemptions(
            orchestrator_address=eth_address,
            limit=limit
        )

        return {
            "eth_address": eth_address,
            "period_hours": hours,
            "ticket_count": len(redemptions),
            "tickets": [r.to_dict() for r in redemptions]
        }

    @app.get("/api/v1/orchestrators/{eth_address}/earnings")
    async def get_orchestrator_earnings(
        eth_address: str,
        days: int = Query(default=1, le=7, description="Days to look back (max 7 due to RPC limits)")
    ):
        """
        Get earnings summary for an orchestrator from ticket redemptions.

        This is on-chain verifiable proof of work performed.
        Note: Uses direct RPC queries, limited lookback on public RPC.
        """
        earnings = await _subgraph.get_orchestrator_earnings(eth_address, days)

        # Check if top 100
        is_top, rank = await _livepeer.is_top_orchestrator(eth_address)

        # Get daily breakdown
        daily_activity = await _subgraph.get_orchestrator_activity(eth_address, min(days, 2))

        return {
            **earnings.to_dict(),
            "is_top_100": is_top,
            "rank": rank,
            "daily_activity": daily_activity
        }

    @app.get("/api/v1/network/redemptions")
    async def get_network_redemptions(
        hours: int = Query(default=6, le=24, description="Hours to look back (max 24 due to RPC limits)")
    ):
        """
        Get network-wide ticket redemption statistics.

        Shows overall network activity and active orchestrators/gateways.
        Note: Uses direct RPC queries to Arbitrum One.
        """
        stats = await _subgraph.get_network_stats(hours)
        recent = await _subgraph.get_recent_network_redemptions(limit=20)

        return {
            **stats,
            "recent_redemptions": [r.to_dict() for r in recent]
        }

    @app.get("/api/v1/gateways")
    async def get_gateways(
        hours: int = Query(default=6, le=24, description="Hours to look back (max 24 due to RPC limits)")
    ):
        """
        Get all active gateways (broadcasters) and their traffic distribution.

        Shows which orchestrators each gateway is routing work to.
        Note: Uses direct RPC queries to Arbitrum One.
        """
        gateways = await _subgraph.get_gateways_summary(hours)

        return {
            "period_hours": hours,
            "gateway_count": len(gateways),
            "gateways": gateways
        }

    @app.get("/api/v1/gateways/{gateway_address}/traffic")
    async def get_gateway_traffic(
        gateway_address: str,
        hours: int = Query(default=6, le=24, description="Hours to look back (max 24 due to RPC limits)")
    ):
        """
        Get traffic breakdown for a specific gateway.

        Shows which orchestrators this gateway sent work to and how much.
        Note: Uses direct RPC queries to Arbitrum One.
        """
        traffic = await _subgraph.get_gateway_traffic(
            gateway_address=gateway_address,
            hours=hours
        )

        # Calculate totals
        total_tickets = sum(t["ticket_count"] for t in traffic)
        total_eth = sum(t["total_eth"] for t in traffic)

        return {
            "gateway": gateway_address,
            "period_hours": hours,
            "total_tickets": total_tickets,
            "total_eth": total_eth,
            "orchestrator_count": len(traffic),
            "traffic": traffic
        }

    # ==================== Verification Endpoints ====================

    @app.post("/api/v1/nodes/register-endpoint")
    async def register_node_endpoint(request: RegisterEndpointRequest):
        """Register a node's agent endpoint for active verification."""
        if abs(time.time() - request.timestamp) > 600:
            raise HTTPException(status_code=400, detail="Timestamp too old or in future")

        agent_url = request.agent_url.rstrip("/")
        unsafe_ok = os.environ.get("ALLOW_PRIVATE_AGENT_URLS", "").strip().lower() in {"1", "true", "yes"}
        if not unsafe_ok:
            ok, reason = _validate_agent_url(agent_url)
            if not ok:
                raise HTTPException(status_code=400, detail=f"Unsafe agent_url: {reason}")
        message = create_endpoint_registration_message(request.node_id, agent_url, request.timestamp)
        if not NodeIdentity.verify_challenge_response(
            node_id=request.node_id,
            challenge=message,
            signature=request.signature,
        ):
            raise HTTPException(status_code=400, detail="Invalid endpoint registration signature")

        _verifier.register_endpoint(request.node_id, agent_url)

        # Optionally link ETH address if provided
        eth_address = request.eth_address
        if not eth_address:
            eth_address = _link_registry.get_eth_address(request.node_id)

        return {
            "status": "registered",
            "node_id": request.node_id,
            "eth_address": eth_address
        }

    @app.post("/api/v1/verify")
    async def verify_node(request: VerifyRequest):
        """
        Send a verification challenge to a node.

        Creates a cryptographic proof of the verification result.
        """
        return await _run_verification(request)

    @app.get("/api/v1/verification-jobs")
    async def list_verification_jobs(node_id: Optional[str] = None, limit: int = 50):
        """List verification jobs."""
        jobs = _storage.get_verification_jobs(node_id, limit)
        return {
            "count": len(jobs),
            "jobs": [
                {
                    "job_id": j.job_id,
                    "node_id": j.node_id,
                    "job_type": j.job_type,
                    "created_at": j.created_at.isoformat(),
                    "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                    "success": j.success,
                    "duration_ms": j.duration_ms
                }
                for j in jobs
            ]
        }

    # ==================== Proof Endpoints ====================

    @app.get("/api/v1/proofs/{proof_id}")
    async def get_proof(proof_id: str):
        """Get a specific verification proof."""
        # Search through stored proofs
        for proofs in [_proof_storage._proofs_by_node.values()]:
            for proof_list in proofs:
                for proof in proof_list:
                    if proof.proof_id == proof_id:
                        return proof.to_dict()

        raise HTTPException(status_code=404, detail="Proof not found")

    @app.get("/api/v1/proofs/node/{node_id}")
    async def get_node_proofs(node_id: str, limit: int = 20):
        """Get all verification proofs for a node."""
        proofs = _proof_storage.get_proofs_for_node(node_id)
        return {
            "node_id": node_id,
            "count": len(proofs),
            "proofs": [p.to_dict() for p in proofs[-limit:]]
        }

    @app.get("/api/v1/proofs/orchestrator/{eth_address}")
    async def get_orchestrator_proofs(eth_address: str, limit: int = 20):
        """Get all verification proofs for an orchestrator (by ETH address)."""
        proofs = _proof_storage.get_proofs_for_eth(eth_address)

        # Check if top 100
        is_top, rank = await _livepeer.is_top_orchestrator(eth_address)

        return {
            "eth_address": eth_address,
            "is_top_100": is_top,
            "rank": rank,
            "count": len(proofs),
            "proofs": [p.to_dict() for p in proofs[-limit:]]
        }

    @app.post("/api/v1/proofs/verify")
    async def verify_proof(request: VerifyProofRequest):
        """
        Verify a proof's signature.

        Anyone can use this to verify that a proof is authentic.
        """
        from .proofs import VerificationProof

        try:
            proof = VerificationProof.from_json(request.proof_json)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid proof JSON: {e}")

        is_valid = ProofGenerator.verify_proof(proof)

        return {
            "valid": is_valid,
            "proof_id": proof.proof_id,
            "node_id": proof.node_id,
            "eth_address": proof.eth_address,
            "verification_type": proof.verification_type,
            "timestamp": proof.timestamp,
            "orchestrator_rank": proof.orchestrator_rank
        }

    @app.get("/api/v1/proofs/was-verified")
    async def was_orchestrator_verified(
        eth_address: str,
        timestamp: int,
        tolerance: int = Query(default=3600, description="Tolerance in seconds")
    ):
        """
        Check if an orchestrator was verified near a specific time.

        Useful for auditing: "Was orchestrator X verified at time Y?"
        """
        proof = _proof_storage.was_verified_at(eth_address, timestamp, tolerance)

        if proof:
            return {
                "verified": True,
                "proof": proof.to_dict(),
                "time_difference": abs(proof.timestamp - timestamp)
            }
        else:
            return {
                "verified": False,
                "message": f"No verification found within {tolerance}s of timestamp {timestamp}"
            }

    # ==================== Stats Endpoints ====================

    @app.get("/api/v1/stats")
    async def get_network_stats():
        """Get network-wide statistics including top 100 coverage."""
        base_stats = _storage.get_network_stats()
        online_agents = _storage.get_online_nodes(timeout_minutes=5)
        uptime_values = [_storage.get_node_uptime(n.node_id, period_seconds=3600)["uptime_percent"] for n in online_agents]
        avg_uptime_1h = round(sum(uptime_values) / len(uptime_values), 1) if uptime_values else 0.0

        # Get top 100 stats
        orchestrators = await _livepeer.get_top_orchestrators(100)
        linked_count = sum(1 for o in orchestrators if _link_registry.get_node_id(o.eth_address))

        return {
            **base_stats,
            "avg_uptime_1h": avg_uptime_1h,
            "top_100_total": len(orchestrators),
            "top_100_linked": linked_count,
            "top_100_coverage": f"{(linked_count / max(len(orchestrators), 1)) * 100:.1f}%"
        }

    # ==================== Dashboard UI ====================

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_ui():
        """Dashboard UI with visual observability for Livepeer network."""
        stats = _storage.get_network_stats()
        nodes = _storage.get_all_nodes()

        # Get top 100 data
        orchestrators = await _livepeer.get_top_orchestrators(100)
        linked_count = sum(1 for o in orchestrators if _link_registry.get_node_id(o.eth_address))
        online_count = sum(1 for o in orchestrators if _link_registry.get_node_id(o.eth_address) and _storage.get_node(_link_registry.get_node_id(o.eth_address)) and _storage.get_node(_link_registry.get_node_id(o.eth_address)).is_online)

        # Get network-wide ticket redemption stats
        try:
            network_redemptions = await _subgraph.get_network_stats(hours=6)
        except Exception as e:
            logger.warning(f"Failed to fetch network stats: {e}")
            network_redemptions = {"total_tickets": 0, "total_eth": 0.0, "active_orchestrators": 0, "active_gateways": 0}

        # Get gateway traffic data
        try:
            gateways = await _subgraph.get_gateways_summary(hours=6)
        except Exception as e:
            logger.warning(f"Failed to fetch gateway data: {e}")
            gateways = []

        # Get recent redemptions for chart
        try:
            recent_redemptions = await _subgraph.get_recent_network_redemptions(limit=100)
        except Exception:
            recent_redemptions = []

        # Build hourly earnings data for chart
        from collections import defaultdict
        hourly_earnings = defaultdict(float)
        for r in recent_redemptions:
            if r.timestamp > 0:
                hour_key = datetime.utcfromtimestamp(r.timestamp).strftime("%H:00")
                hourly_earnings[hour_key] += r.face_value_eth

        # Sort by hour
        sorted_hours = sorted(hourly_earnings.keys())
        chart_labels = sorted_hours[-6:] if len(sorted_hours) > 6 else sorted_hours
        chart_data = [hourly_earnings[h] for h in chart_labels]

        # Build gateway traffic bars
        max_tickets = max((g["total_tickets"] for g in gateways), default=1)
        gateways_html = ""
        for gw in gateways[:6]:
            bar_width = int((gw["total_tickets"] / max_tickets) * 100)
            gateways_html += f"""
            <div class="traffic-row">
                <a href="/gateway/{gw['gateway']}" class="gateway-addr">{gw['gateway'][:10]}...{gw['gateway'][-4:]}</a>
                <div class="traffic-bar-container">
                    <div class="traffic-bar" style="width: {bar_width}%"></div>
                </div>
                <span class="traffic-stats">{gw['total_tickets']} tickets • {gw['total_eth']:.4f} ETH • {gw['orchestrator_count']} orchs</span>
            </div>
            """

        # Build orchestrator table with progress bars
        top100_html = ""
        for orch in orchestrators[:15]:
            node_id = _link_registry.get_node_id(orch.eth_address)
            node = _storage.get_node(node_id) if node_id else None

            if node and node.is_online:
                status = '<span class="status-badge online">● Online</span>'
            elif node_id:
                status = '<span class="status-badge linked">● Linked</span>'
            else:
                status = '<span class="status-badge">○ Not linked</span>'

            # Compute trust tier for GPU verification
            trust_tier = _compute_trust_tier(node, _storage) if node else {"tier": "UNKNOWN", "badge": "❓", "description": "Not linked"}
            tier_badge = trust_tier["badge"]
            tier_name = trust_tier["tier"]
            tier_desc = trust_tier["description"]

            # Color-code trust tier
            tier_colors = {
                "TESTED": "#3fb950",    # Green
                "CLAIMED": "#d29922",   # Yellow
                "SUSPECT": "#f0883e",   # Orange
                "FAILED": "#f85149",    # Red
                "UNKNOWN": "#8b949e"    # Gray
            }
            tier_color = tier_colors.get(tier_name, "#8b949e")

            stake = f"{orch.total_stake / 1000:.1f}k" if orch.total_stake >= 1000 else f"{orch.total_stake:.0f}"
            score = node.verification_score if node else 0
            score_width = int(score)
            uptime_display = "—"
            if node:
                uptime = _storage.get_node_uptime(node.node_id, period_seconds=3600)
                uptime_display = f"{uptime['uptime_percent']:.1f}%"

            top100_html += f"""
            <tr onclick="window.location='/orchestrator/{orch.eth_address}'" style="cursor:pointer;">
                <td><strong>#{orch.rank}</strong></td>
                <td><code class="eth-addr">{orch.eth_address[:8]}...{orch.eth_address[-6:]}</code></td>
                <td>{stake} LPT</td>
                <td>{status}</td>
                <td><span class="trust-badge" style="color:{tier_color};" title="{tier_desc}">{tier_badge} {tier_name}</span></td>
                <td style="color:#8b949e;font-size:12px;">{uptime_display}</td>
                <td>
                    <div class="score-bar-container">
                        <div class="score-bar" style="width: {score_width}%"></div>
                        <span class="score-text">{score:.0f}</span>
                    </div>
                </td>
            </tr>
            """

        # Network health summary with GPU metrics
        gpu_util = stats.get('avg_gpu_utilization', 0)
        gpu_temp = stats.get('avg_gpu_temperature', 0)
        gpu_power = stats.get('total_power_draw_w', 0)
        gpus_reporting = stats.get('gpus_reporting_metrics', 0)

        online_agents = _storage.get_online_nodes(timeout_minutes=5)
        uptime_values = [_storage.get_node_uptime(n.node_id, period_seconds=3600)["uptime_percent"] for n in online_agents]
        avg_uptime_1h = round(sum(uptime_values) / len(uptime_values), 1) if uptime_values else 0.0

        # Color code temperature (green < 70, yellow < 85, red >= 85)
        temp_color = "#3fb950" if gpu_temp < 70 else ("#d29922" if gpu_temp < 85 else "#f85149")

        health_html = f"""
            <div class="health-item">
                <span class="health-label">SLA Coverage</span>
                <span class="health-value">{linked_count}/100 orchestrators linked</span>
            </div>
            <div class="health-item">
                <span class="health-label">Online Agents</span>
                <span class="health-value">{online_count} reporting</span>
            </div>
            <div class="health-item">
                <span class="health-label">Agent Uptime</span>
                <span class="health-value">{avg_uptime_1h:.1f}% avg (1h)</span>
            </div>
            <div class="health-item">
                <span class="health-label">Total GPUs</span>
                <span class="health-value">{stats['total_gpus']} available</span>
            </div>
            <div class="health-item">
                <span class="health-label">GPU Utilization</span>
                <span class="health-value">{gpu_util:.1f}% avg ({gpus_reporting} reporting)</span>
            </div>
            <div class="health-item">
                <span class="health-label">GPU Temperature</span>
                <span class="health-value" style="color: {temp_color}">{gpu_temp:.1f}°C avg</span>
            </div>
            <div class="health-item">
                <span class="health-label">Power Draw</span>
                <span class="health-value">{gpu_power:.0f}W total</span>
            </div>
            <div class="health-item">
                <span class="health-label">Attestations</span>
                <span class="health-value">{stats['total_attestations']} total</span>
            </div>
        """

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Livepeer SLA Dashboard</title>
            <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
            <style>
                * {{ box-sizing: border-box; }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    margin: 0; padding: 24px 32px;
                    background: #0d1117; color: #c9d1d9;
                    line-height: 1.5;
                }}
                h1 {{ color: #fff; margin: 0 0 24px 0; font-size: 24px; font-weight: 600; }}
                h2 {{ color: #c9d1d9; margin: 0 0 16px 0; font-size: 16px; font-weight: 600; }}
                h3 {{ color: #8b949e; margin: 0 0 12px 0; font-size: 13px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }}
                a {{ color: #58a6ff; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}

                /* Hero Stats */
                .hero-stats {{
                    display: grid;
                    grid-template-columns: repeat(4, 1fr);
                    gap: 16px;
                    margin-bottom: 24px;
                }}
                .hero-card {{
                    background: linear-gradient(135deg, #161b22 0%, #1c2128 100%);
                    border: 1px solid #30363d;
                    border-radius: 12px;
                    padding: 20px;
                }}
                .hero-card.primary {{ border-color: #58a6ff; }}
                .hero-icon {{ font-size: 20px; margin-bottom: 8px; }}
                .hero-value {{ font-size: 32px; font-weight: 700; color: #fff; margin-bottom: 4px; }}
                .hero-label {{ font-size: 13px; color: #8b949e; }}
                .hero-sublabel {{ font-size: 11px; color: #6e7681; margin-top: 4px; }}

                /* Main Grid */
                .main-grid {{
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 24px;
                    margin-bottom: 24px;
                }}
                .panel {{
                    background: #161b22;
                    border: 1px solid #30363d;
                    border-radius: 12px;
                    padding: 20px;
                }}

                /* Traffic Flow */
                .traffic-row {{
                    display: flex;
                    align-items: center;
                    gap: 12px;
                    margin-bottom: 12px;
                }}
                .gateway-addr {{
                    font-family: 'SF Mono', Monaco, monospace;
                    font-size: 12px;
                    color: #f0883e;
                    min-width: 140px;
                }}
                .traffic-bar-container {{
                    flex: 1;
                    height: 24px;
                    background: #21262d;
                    border-radius: 4px;
                    overflow: hidden;
                }}
                .traffic-bar {{
                    height: 100%;
                    background: linear-gradient(90deg, #238636 0%, #3fb950 100%);
                    border-radius: 4px;
                    transition: width 0.3s ease;
                }}
                .traffic-stats {{
                    font-size: 11px;
                    color: #8b949e;
                    min-width: 180px;
                    text-align: right;
                }}

                /* Chart */
                .chart-container {{
                    height: 200px;
                    position: relative;
                }}

                /* Table */
                table {{
                    width: 100%;
                    border-collapse: collapse;
                }}
                th, td {{
                    padding: 10px 12px;
                    text-align: left;
                    border-bottom: 1px solid #21262d;
                    font-size: 13px;
                }}
                th {{
                    color: #8b949e;
                    font-weight: 500;
                    font-size: 11px;
                    text-transform: uppercase;
                    letter-spacing: 0.5px;
                }}
                tr:hover {{ background: #1c2128; }}
                code {{ font-family: 'SF Mono', Monaco, monospace; }}
                .eth-addr {{
                    background: #30363d;
                    padding: 2px 6px;
                    border-radius: 4px;
                    font-size: 11px;
                    color: #f0883e;
                }}

                /* Status Badges */
                .status-badge {{
                    font-size: 11px;
                    padding: 2px 8px;
                    border-radius: 12px;
                    background: #30363d;
                    color: #8b949e;
                }}
                .status-badge.online {{
                    background: rgba(35, 134, 54, 0.2);
                    color: #3fb950;
                }}
                .status-badge.linked {{
                    background: rgba(210, 153, 34, 0.2);
                    color: #d29922;
                }}

                /* Score Bar */
                .score-bar-container {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                }}
                .score-bar {{
                    height: 6px;
                    background: #3fb950;
                    border-radius: 3px;
                    min-width: 4px;
                }}
                .score-text {{
                    font-size: 11px;
                    color: #8b949e;
                    min-width: 24px;
                }}

                /* Trust Badge */
                .trust-badge {{
                    font-size: 11px;
                    font-weight: 500;
                    white-space: nowrap;
                }}

                /* Health Panel */
                .health-item {{
                    display: flex;
                    justify-content: space-between;
                    padding: 8px 0;
                    border-bottom: 1px solid #21262d;
                }}
                .health-item:last-child {{ border-bottom: none; }}
                .health-label {{ color: #8b949e; font-size: 13px; }}
                .health-value {{ color: #c9d1d9; font-size: 13px; font-weight: 500; }}

                /* Footer */
                .footer {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    padding-top: 16px;
                    border-top: 1px solid #21262d;
                    font-size: 12px;
                    color: #6e7681;
                }}
                .footer-links {{ display: flex; gap: 16px; }}

                /* Responsive */
                @media (max-width: 1200px) {{
                    .hero-stats {{ grid-template-columns: repeat(2, 1fr); }}
                    .main-grid {{ grid-template-columns: 1fr; }}
                }}
            </style>
            <meta http-equiv="refresh" content="30">
        </head>
        <body>
            <h1>🎬 Livepeer Network Observatory</h1>

            <!-- Hero Stats -->
            <div class="hero-stats">
                <div class="hero-card primary">
                    <div class="hero-icon">🎫</div>
                    <div class="hero-value">{network_redemptions['total_tickets']}</div>
                    <div class="hero-label">Tickets Redeemed</div>
                    <div class="hero-sublabel">Last 6 hours</div>
                </div>
                <div class="hero-card">
                    <div class="hero-icon">💰</div>
                    <div class="hero-value">{network_redemptions['total_eth']:.4f}</div>
                    <div class="hero-label">ETH Earned</div>
                    <div class="hero-sublabel">Last 6 hours</div>
                </div>
                <div class="hero-card">
                    <div class="hero-icon">🎯</div>
                    <div class="hero-value">{network_redemptions.get('active_orchestrators', 0)}</div>
                    <div class="hero-label">Active Orchestrators</div>
                    <div class="hero-sublabel">Receiving work</div>
                </div>
                <div class="hero-card">
                    <div class="hero-icon">🌐</div>
                    <div class="hero-value">{network_redemptions.get('active_gateways', 0)}</div>
                    <div class="hero-label">Active Gateways</div>
                    <div class="hero-sublabel">Sending work</div>
                </div>
            </div>

            <!-- Main Grid -->
            <div class="main-grid">
                <!-- Gateway Traffic -->
                <div class="panel">
                    <h3>Gateway → Orchestrator Traffic</h3>
                    {gateways_html if gateways_html else '<p style="color:#8b949e;text-align:center;padding:20px;">No gateway traffic in last 6 hours</p>'}
                </div>

                <!-- Earnings Chart -->
                <div class="panel">
                    <h3>Earnings Trend (Last 6 Hours)</h3>
                    <div class="chart-container">
                        <canvas id="earningsChart"></canvas>
                    </div>
                </div>
            </div>

            <div class="main-grid">
                <!-- Top Orchestrators -->
                <div class="panel">
                    <h3>Top Orchestrators</h3>
	                    <table>
	                        <thead>
	                            <tr>
	                                <th>Rank</th>
	                                <th>Address</th>
	                                <th>Stake</th>
	                                <th>Status</th>
	                                <th>Trust</th>
	                                <th>Uptime (1h)</th>
	                                <th style="width:100px;">Score</th>
	                            </tr>
	                        </thead>
	                        <tbody>
	                            {top100_html if top100_html else '<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:20px;">Loading...</td></tr>'}
                        </tbody>
                    </table>
                    <p style="color:#6e7681;font-size:11px;margin-top:12px;text-align:center;">
                        Showing top 15 • <a href="/api/v1/orchestrators/top100">View all 100 →</a>
                    </p>
                </div>

                <!-- Network Health -->
                <div class="panel">
                    <h3>Network Health</h3>
                    {health_html}
                </div>
            </div>

            <!-- Footer -->
            <div class="footer">
                <span>Auto-refreshes every 30 seconds • Data from Arbitrum One</span>
                <div class="footer-links">
                    <a href="/api/v1/gateways">Gateways API</a>
                    <a href="/api/v1/network/redemptions">On-Chain API</a>
                    <a href="/docs">API Docs</a>
                </div>
            </div>

            <script>
                // Chart.js config for dark theme
                Chart.defaults.color = '#8b949e';
                Chart.defaults.borderColor = '#30363d';

                const ctx = document.getElementById('earningsChart').getContext('2d');
                new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: {chart_labels},
                        datasets: [{{
                            label: 'ETH Earned',
                            data: {chart_data},
                            borderColor: '#3fb950',
                            backgroundColor: 'rgba(63, 185, 80, 0.1)',
                            fill: true,
                            tension: 0.4,
                            pointRadius: 4,
                            pointBackgroundColor: '#3fb950'
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{
                            legend: {{ display: false }}
                        }},
                        scales: {{
                            y: {{
                                beginAtZero: true,
                                grid: {{ color: '#21262d' }},
                                ticks: {{
                                    callback: function(value) {{ return value.toFixed(3) + ' ETH'; }}
                                }}
                            }},
                            x: {{
                                grid: {{ display: false }}
                            }}
                        }}
                    }}
                }});
            </script>
        </body>
        </html>
        """

    # ==================== Detail Pages ====================

    @app.get("/orchestrator/{eth_address}", response_class=HTMLResponse)
    async def orchestrator_detail_page(eth_address: str):
        """Detail page for a specific orchestrator."""
        # Get orchestrator rank and stake
        orchestrators = await _livepeer.get_top_orchestrators(100)
        orch_data = next((o for o in orchestrators if o.eth_address.lower() == eth_address.lower()), None)

        # Get node info if linked
        node_id = _link_registry.get_node_id(eth_address)
        node = _storage.get_node(node_id) if node_id else None

        # Get earnings and tickets
        try:
            earnings = await _subgraph.get_orchestrator_earnings(eth_address, days=1)
            tickets = await _subgraph.get_ticket_redemptions(orchestrator_address=eth_address, limit=20)
        except Exception as e:
            logger.warning(f"Failed to fetch orchestrator data: {e}")
            earnings = None
            tickets = []

        # Build tickets table
        tickets_html = ""
        for t in tickets:
            time_ago = _format_time_ago(t.timestamp) if t.timestamp > 0 else "Unknown"
            tx_link = f"https://arbiscan.io/tx/{t.tx_hash}" if t.tx_hash else "#"
            tickets_html += f"""
            <tr>
                <td>{time_ago}</td>
                <td><a href="/gateway/{t.sender}" class="eth-addr">{t.sender[:8]}...{t.sender[-6:]}</a></td>
                <td>{t.face_value_eth:.6f} ETH</td>
                <td><a href="{tx_link}" target="_blank" style="font-size:11px;">View →</a></td>
            </tr>
            """

        rank_display = f"Rank #{orch_data.rank}" if orch_data else "Unranked"
        stake_display = f"{orch_data.total_stake / 1000:.1f}k LPT" if orch_data else "N/A"
        status = "🟢 Online" if (node and node.is_online) else ("🟡 Linked" if node_id else "⚪ Not linked")
        score = f"{node.verification_score:.0f}/100" if node else "N/A"
        uptime_display = "—"
        if node:
            uptime = _storage.get_node_uptime(node.node_id, period_seconds=3600)
            uptime_display = f"{uptime['uptime_percent']:.1f}%"

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Orchestrator {eth_address[:10]}... | Livepeer SLA</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 24px 32px; background: #0d1117; color: #c9d1d9; }}
                a {{ color: #58a6ff; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                .back {{ color: #8b949e; font-size: 13px; margin-bottom: 16px; display: block; }}
                h1 {{ color: #fff; margin: 0 0 8px 0; font-size: 20px; }}
                .subtitle {{ color: #8b949e; font-size: 14px; margin-bottom: 24px; }}
                .stats-row {{ display: flex; gap: 32px; margin-bottom: 24px; padding: 16px; background: #161b22; border-radius: 8px; border: 1px solid #30363d; }}
                .stat {{ text-align: center; }}
                .stat-val {{ font-size: 24px; font-weight: 600; color: #fff; }}
                .stat-lbl {{ font-size: 12px; color: #8b949e; }}
                .panel {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
                h3 {{ color: #8b949e; font-size: 12px; text-transform: uppercase; margin: 0 0 12px 0; }}
                table {{ width: 100%; border-collapse: collapse; }}
                th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #21262d; font-size: 13px; }}
                th {{ color: #8b949e; font-size: 11px; text-transform: uppercase; }}
                .eth-addr {{ background: #30363d; padding: 2px 6px; border-radius: 4px; font-size: 11px; color: #f0883e; font-family: monospace; }}
            </style>
        </head>
        <body>
            <a href="/" class="back">← Back to Dashboard</a>
            <h1>Orchestrator <code style="background:#30363d;padding:4px 8px;border-radius:4px;color:#f0883e;">{eth_address}</code></h1>
            <div class="subtitle">{rank_display} • {stake_display} • {status} • Score: {score}</div>

            <div class="stats-row">
                <div class="stat">
                    <div class="stat-val">{earnings.total_tickets if earnings else 0}</div>
                    <div class="stat-lbl">Tickets (24h)</div>
                </div>
                <div class="stat">
                    <div class="stat-val">{(earnings.total_eth if earnings else 0.0):.4f}</div>
                    <div class="stat-lbl">ETH Earned</div>
                </div>
                <div class="stat">
                    <div class="stat-val">{earnings.unique_broadcasters if earnings else 0}</div>
                    <div class="stat-lbl">Unique Gateways</div>
                </div>
                <div class="stat">
                    <div class="stat-val">{uptime_display}</div>
                    <div class="stat-lbl">Uptime (1h)</div>
                </div>
            </div>

            <div class="panel">
                <h3>Recent Ticket Redemptions</h3>
                <table>
                    <thead>
                        <tr><th>Time</th><th>Gateway</th><th>Amount</th><th>Tx</th></tr>
                    </thead>
                    <tbody>
                        {tickets_html if tickets_html else '<tr><td colspan="4" style="text-align:center;color:#8b949e;padding:20px;">No recent tickets</td></tr>'}
                    </tbody>
                </table>
            </div>
        </body>
        </html>
        """

    @app.get("/gateway/{eth_address}", response_class=HTMLResponse)
    async def gateway_detail_page(eth_address: str):
        """Detail page for a specific gateway."""
        try:
            traffic = await _subgraph.get_gateway_traffic(gateway_address=eth_address, hours=6)
        except Exception:
            traffic = []

        total_tickets = sum(t["ticket_count"] for t in traffic)
        total_eth = sum(t["total_eth"] for t in traffic)

        # Build orchestrator distribution
        orchs_html = ""
        max_tickets = max((t["ticket_count"] for t in traffic), default=1)
        for t in traffic:
            bar_width = int((t["ticket_count"] / max_tickets) * 100)
            orchs_html += f"""
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
                <a href="/orchestrator/{t['orchestrator']}" class="eth-addr" style="min-width:160px;">{t['orchestrator'][:8]}...{t['orchestrator'][-6:]}</a>
                <div style="flex:1;height:20px;background:#21262d;border-radius:4px;overflow:hidden;">
                    <div style="height:100%;width:{bar_width}%;background:linear-gradient(90deg,#58a6ff,#3fb950);border-radius:4px;"></div>
                </div>
                <span style="font-size:11px;color:#8b949e;min-width:120px;text-align:right;">{t['ticket_count']} • {t['total_eth']:.4f} ETH</span>
            </div>
            """

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Gateway {eth_address[:10]}... | Livepeer SLA</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 24px 32px; background: #0d1117; color: #c9d1d9; }}
                a {{ color: #58a6ff; text-decoration: none; }}
                .back {{ color: #8b949e; font-size: 13px; margin-bottom: 16px; display: block; }}
                h1 {{ color: #fff; margin: 0 0 8px 0; font-size: 20px; }}
                .subtitle {{ color: #8b949e; font-size: 14px; margin-bottom: 24px; }}
                .stats-row {{ display: flex; gap: 32px; margin-bottom: 24px; padding: 16px; background: #161b22; border-radius: 8px; border: 1px solid #30363d; }}
                .stat {{ text-align: center; }}
                .stat-val {{ font-size: 24px; font-weight: 600; color: #fff; }}
                .stat-lbl {{ font-size: 12px; color: #8b949e; }}
                .panel {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
                h3 {{ color: #8b949e; font-size: 12px; text-transform: uppercase; margin: 0 0 12px 0; }}
                .eth-addr {{ background: #30363d; padding: 2px 6px; border-radius: 4px; font-size: 11px; color: #f0883e; font-family: monospace; }}
            </style>
        </head>
        <body>
            <a href="/" class="back">← Back to Dashboard</a>
            <h1>Gateway <code style="background:#30363d;padding:4px 8px;border-radius:4px;color:#f0883e;">{eth_address}</code></h1>
            <div class="subtitle">Broadcasting work to {len(traffic)} orchestrators</div>

            <div class="stats-row">
                <div class="stat">
                    <div class="stat-val">{total_tickets}</div>
                    <div class="stat-lbl">Tickets Sent (6h)</div>
                </div>
                <div class="stat">
                    <div class="stat-val">{total_eth:.4f}</div>
                    <div class="stat-lbl">ETH Paid</div>
                </div>
                <div class="stat">
                    <div class="stat-val">{len(traffic)}</div>
                    <div class="stat-lbl">Orchestrators</div>
                </div>
            </div>

            <div class="panel">
                <h3>Traffic Distribution by Orchestrator</h3>
                {orchs_html if orchs_html else '<p style="color:#8b949e;text-align:center;padding:20px;">No traffic data</p>'}
            </div>
        </body>
        </html>
        """

    return app


def _format_time_ago(timestamp: int) -> str:
    """Format a Unix timestamp as a human-readable 'time ago' string."""
    now = int(time.time())
    diff = now - timestamp

    if diff < 60:
        return f"{diff}s ago"
    elif diff < 3600:
        return f"{diff // 60}m ago"
    elif diff < 86400:
        return f"{diff // 3600}h ago"
    else:
        return f"{diff // 86400}d ago"


def _compute_trust_tier(node, storage: Storage) -> dict:
    """
    Compute the trust tier for a node based on GPU benchmark verification.

    Trust tiers:
    - TESTED: GPU benchmark passed (timing plausible for claimed GPU)
    - SUSPECT: Benchmark ran but timing doesn't match claimed GPU
    - FAILED: Benchmark failed or node not reachable
    - CLAIMED: Self-reported only (no benchmark verification yet)

    Returns dict with tier, badge emoji, and description.
    """
    if not node:
        return {"tier": "UNKNOWN", "badge": "❓", "description": "Node not found"}

    # Check for recent GPU benchmark jobs
    jobs = storage.get_verification_jobs(node.node_id, limit=10)
    gpu_benchmark_jobs = [j for j in jobs if j.job_type == "gpu_benchmark"]

    if not gpu_benchmark_jobs:
        return {
            "tier": "CLAIMED",
            "badge": "⚠️",
            "description": "Self-reported capabilities only"
        }

    # Check most recent GPU benchmark
    latest_job = gpu_benchmark_jobs[0]

    if not latest_job.success:
        return {
            "tier": "FAILED",
            "badge": "🚫",
            "description": "GPU benchmark failed"
        }

    # Check if result indicates suspect timing
    result = latest_job.result or {}
    trust_tier = result.get("trust_tier", "TESTED")

    if trust_tier == "SUSPECT":
        return {
            "tier": "SUSPECT",
            "badge": "⚡",
            "description": "Timing inconsistent with claimed GPU"
        }

    return {
        "tier": "TESTED",
        "badge": "✅",
        "description": "GPU benchmark verified"
    }


def _summarize_capabilities(caps: Optional[dict]) -> dict:
    """Create a brief summary of capabilities including GPU metrics."""
    if not caps:
        return {}

    summary = {}
    gpus = caps.get("gpus", [])
    if gpus:
        gpu_names = [g.get("name", "Unknown") for g in gpus]
        summary["gpus"] = f"{len(gpus)}x {gpu_names[0]}" if gpu_names else f"{len(gpus)} GPUs"

        # Add real-time GPU metrics from first GPU
        first_gpu = gpus[0]
        gpu_util = first_gpu.get("gpu_utilization_percent", 0)
        gpu_temp = first_gpu.get("temperature_c", 0)
        gpu_power = first_gpu.get("power_draw_w")

        if gpu_util > 0:
            summary["gpu_util"] = f"{gpu_util:.0f}%"
        if gpu_temp > 0:
            summary["gpu_temp"] = f"{gpu_temp:.0f}°C"
        if gpu_power is not None and gpu_power > 0:
            summary["gpu_power"] = f"{gpu_power:.0f}W"
    else:
        summary["gpus"] = "No GPU"

    mem = caps.get("memory", {})
    if mem:
        summary["memory"] = f"{mem.get('total_mb', 0) // 1024} GB"

    cpu = caps.get("cpu", {})
    if cpu:
        summary["cpu"] = f"{cpu.get('cores_physical', '?')} cores"

    net = caps.get("network", {})
    if net:
        tx = net.get("tx_mbps", 0.0) or 0.0
        rx = net.get("rx_mbps", 0.0) or 0.0
        if tx > 0 or rx > 0:
            summary["bandwidth"] = f"↑{tx:.2f} ↓{rx:.2f} Mbps"

    return summary


def _validate_agent_url(url: str) -> tuple[bool, str]:
    """Basic SSRF guard for agent endpoints (public URLs only by default)."""
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
    if parsed.path not in {"", "/"}:
        return False, "path not allowed (use base URL)"
    if parsed.params or parsed.query or parsed.fragment:
        return False, "url must not include params/query/fragment"

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


def main():
    """Run the dashboard server."""
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
