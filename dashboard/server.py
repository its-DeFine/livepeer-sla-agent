"""
Dashboard server - collects attestations and provides verification API.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agent.identity import NodeIdentity, SignedAttestation
from agent.livepeer import get_livepeer_network, LivepeerNetwork
from agent.eth_link import AddressLink, AddressLinkRegistry, verify_address_link, create_link_for_signing
from agent.onchain import get_subgraph, LivepeerSubgraph
from .models import AttestationPayload
from .storage import get_storage, Storage
from .verifier import get_verifier, Verifier
from .proofs import get_proof_generator, get_proof_storage, ProofGenerator, ProofStorage

logger = logging.getLogger(__name__)


# Request models
class RegisterEndpointRequest(BaseModel):
    node_id: str
    agent_url: str
    eth_address: Optional[str] = None  # Optional ETH address to link


class VerifyRequest(BaseModel):
    node_id: str
    challenge_type: str = "liveness"  # or "transcode"
    profile: str = "P720p30fps16x9"


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


def create_app() -> FastAPI:
    """Create the dashboard FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _storage, _verifier, _livepeer, _link_registry, _proof_generator, _proof_storage, _subgraph
        _storage = get_storage()
        _verifier = get_verifier()
        _livepeer = get_livepeer_network()
        _link_registry = AddressLinkRegistry()
        _proof_generator = get_proof_generator()
        _proof_storage = get_proof_storage()
        _subgraph = get_subgraph()
        await _verifier.__aenter__()
        await _subgraph.__aenter__()
        logger.info("Dashboard started")
        yield
        await _subgraph.__aexit__(None, None, None)
        await _verifier.__aexit__(None, None, None)
        logger.info("Dashboard stopped")

    app = FastAPI(
        title="Livepeer SLA Dashboard",
        description="Collects and verifies node attestations for Livepeer orchestrators",
        version="0.2.0",
        lifespan=lifespan
    )

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
                    "verification_score": n.verification_score,
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
            "verification_score": node.verification_score,
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
        orchestrators = await _livepeer.get_top_orchestrators(100)

        result = []
        for orch in orchestrators:
            node_id = _link_registry.get_node_id(orch.eth_address)
            node = _storage.get_node(node_id) if node_id else None

            # Get latest proof if available
            proofs = _proof_storage.get_proofs_for_eth(orch.eth_address) if orch.eth_address else []
            latest_proof = proofs[-1] if proofs else None

            result.append({
                "rank": orch.rank,
                "eth_address": orch.eth_address,
                "total_stake": orch.total_stake,
                "service_uri": orch.service_uri,
                "node_id": node_id,
                "is_linked": node_id is not None,
                "is_online": node.is_online if node else False,
                "verification_score": node.verification_score if node else 0,
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
        days: int = Query(default=30, le=90)
    ):
        """
        Get on-chain ticket redemptions for an orchestrator.

        Tickets represent verifiable proof of work - each winning ticket
        is a micropayment from a broadcaster for transcoding work done.
        """
        from datetime import datetime, timedelta

        since_timestamp = int((datetime.utcnow() - timedelta(days=days)).timestamp())

        redemptions = await _subgraph.get_ticket_redemptions(
            orchestrator_address=eth_address,
            limit=limit,
            since_timestamp=since_timestamp
        )

        return {
            "eth_address": eth_address,
            "period_days": days,
            "ticket_count": len(redemptions),
            "tickets": [r.to_dict() for r in redemptions]
        }

    @app.get("/api/v1/orchestrators/{eth_address}/earnings")
    async def get_orchestrator_earnings(
        eth_address: str,
        days: int = Query(default=30, le=365)
    ):
        """
        Get earnings summary for an orchestrator from ticket redemptions.

        This is on-chain verifiable proof of work performed.
        """
        earnings = await _subgraph.get_orchestrator_earnings(eth_address, days)

        # Check if top 100
        is_top, rank = await _livepeer.is_top_orchestrator(eth_address)

        # Get daily breakdown
        daily_activity = await _subgraph.get_orchestrator_activity(eth_address, min(days, 30))

        return {
            **earnings.to_dict(),
            "is_top_100": is_top,
            "rank": rank,
            "daily_activity": daily_activity
        }

    @app.get("/api/v1/network/redemptions")
    async def get_network_redemptions(
        hours: int = Query(default=24, le=168)
    ):
        """
        Get network-wide ticket redemption statistics.

        Shows overall network activity and active orchestrators/broadcasters.
        """
        stats = await _subgraph.get_network_stats(hours)
        recent = await _subgraph.get_recent_network_redemptions(limit=20)

        return {
            **stats,
            "recent_redemptions": [r.to_dict() for r in recent]
        }

    @app.get("/api/v1/gateways")
    async def get_gateways(
        hours: int = Query(default=24, le=168)
    ):
        """
        Get all active gateways (broadcasters) and their traffic distribution.

        Shows which orchestrators each gateway is routing work to.
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
        hours: int = Query(default=24, le=168)
    ):
        """
        Get traffic breakdown for a specific gateway.

        Shows which orchestrators this gateway sent work to and how much.
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
        _verifier.register_endpoint(request.node_id, request.agent_url)

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

        return {
            **result,
            "proof_id": proof.proof_id,
            "proof": proof.to_dict()
        }

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

        # Get top 100 stats
        orchestrators = await _livepeer.get_top_orchestrators(100)
        linked_count = sum(1 for o in orchestrators if _link_registry.get_node_id(o.eth_address))

        return {
            **base_stats,
            "top_100_total": len(orchestrators),
            "top_100_linked": linked_count,
            "top_100_coverage": f"{(linked_count / max(len(orchestrators), 1)) * 100:.1f}%"
        }

    # ==================== Dashboard UI ====================

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_ui():
        """Dashboard UI with top 100 orchestrator status."""
        stats = _storage.get_network_stats()
        nodes = _storage.get_all_nodes()

        # Get top 100 data
        orchestrators = await _livepeer.get_top_orchestrators(100)
        linked_count = sum(1 for o in orchestrators if _link_registry.get_node_id(o.eth_address))

        # Get network-wide ticket redemption stats
        try:
            network_redemptions = await _subgraph.get_network_stats(hours=24)
        except Exception:
            network_redemptions = {"total_tickets": 0, "total_eth": 0.0, "active_orchestrators": 0}

        # Build nodes table
        nodes_html = ""
        for n in sorted(nodes, key=lambda x: x.last_seen, reverse=True)[:20]:
            status_badge = "🟢" if n.is_online else "🔴"
            eth_addr = _link_registry.get_eth_address(n.node_id)
            eth_display = f"{eth_addr[:10]}..." if eth_addr else "Not linked"
            caps = _summarize_capabilities(n.last_capabilities)
            nodes_html += f"""
            <tr>
                <td><code>{n.node_id[:16]}...</code></td>
                <td><code style="color:#f0883e;">{eth_display}</code></td>
                <td>{status_badge}</td>
                <td>{n.attestation_count}</td>
                <td>{n.verification_score:.1f}</td>
                <td>{caps.get('gpus', 'N/A')}</td>
                <td>{n.last_seen.strftime('%H:%M:%S')}</td>
            </tr>
            """

        # Build top 100 table
        top100_html = ""
        for orch in orchestrators[:20]:
            node_id = _link_registry.get_node_id(orch.eth_address)
            node = _storage.get_node(node_id) if node_id else None
            status = "🟢" if (node and node.is_online) else ("🟡" if node_id else "⚪")
            link_status = "✓ Linked" if node_id else "Not linked"
            stake = f"{orch.total_stake / 1000:.1f}k" if orch.total_stake >= 1000 else f"{orch.total_stake:.0f}"

            top100_html += f"""
            <tr>
                <td><strong>#{orch.rank}</strong></td>
                <td><code>{orch.eth_address[:14]}...</code></td>
                <td>{stake} LPT</td>
                <td>{status} {link_status}</td>
                <td>{node.verification_score:.1f if node else '-'}</td>
            </tr>
            """

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Livepeer SLA Dashboard</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #0d1117; color: #c9d1d9; }}
                h1, h2 {{ color: #58a6ff; }}
                .stats {{ display: flex; gap: 20px; margin-bottom: 30px; flex-wrap: wrap; }}
                .stat-card {{ background: #161b22; padding: 20px; border-radius: 8px; border: 1px solid #30363d; min-width: 120px; }}
                .stat-value {{ font-size: 28px; font-weight: bold; color: #58a6ff; }}
                .stat-label {{ color: #8b949e; font-size: 13px; }}
                .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 30px; }}
                table {{ width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }}
                th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #30363d; font-size: 13px; }}
                th {{ background: #21262d; color: #8b949e; font-weight: 600; }}
                code {{ background: #30363d; padding: 2px 6px; border-radius: 4px; font-size: 11px; }}
                .refresh {{ color: #8b949e; font-size: 12px; margin-top: 20px; }}
                a {{ color: #58a6ff; }}
                .highlight {{ background: #238636; color: white; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
            </style>
            <meta http-equiv="refresh" content="30">
        </head>
        <body>
            <h1>🎬 Livepeer SLA Dashboard</h1>

            <div class="stats">
                <div class="stat-card">
                    <div class="stat-value">{linked_count}/{len(orchestrators)}</div>
                    <div class="stat-label">Top 100 Linked</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['online_nodes']}</div>
                    <div class="stat-label">Online Agents</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{network_redemptions['total_tickets']}</div>
                    <div class="stat-label">Tickets (24h)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{network_redemptions['total_eth']:.4f}</div>
                    <div class="stat-label">ETH Earned (24h)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['total_gpus']}</div>
                    <div class="stat-label">Total GPUs</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['total_attestations']}</div>
                    <div class="stat-label">Attestations</div>
                </div>
            </div>

            <div class="grid">
                <div>
                    <h2>📊 Top 100 Orchestrators</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Rank</th>
                                <th>ETH Address</th>
                                <th>Stake</th>
                                <th>SLA Status</th>
                                <th>Score</th>
                            </tr>
                        </thead>
                        <tbody>
                            {top100_html if top100_html else '<tr><td colspan="5" style="text-align:center;color:#8b949e;">Loading orchestrators...</td></tr>'}
                        </tbody>
                    </table>
                    <p style="color:#8b949e;font-size:12px;">Showing top 20 • <a href="/api/v1/orchestrators/top100">View all 100 →</a></p>
                </div>

                <div>
                    <h2>🖥️ Connected Agents</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Node ID</th>
                                <th>ETH Address</th>
                                <th>Status</th>
                                <th>Atts</th>
                                <th>Score</th>
                                <th>GPUs</th>
                                <th>Last Seen</th>
                            </tr>
                        </thead>
                        <tbody>
                            {nodes_html if nodes_html else '<tr><td colspan="7" style="text-align:center;color:#8b949e;">No agents registered yet</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>

            <p class="refresh">
                Auto-refreshes every 30 seconds |
                <a href="/api/v1/stats">Stats API</a> •
                <a href="/api/v1/orchestrators/top100">Top 100 API</a> •
                <a href="/api/v1/gateways">Gateways</a> •
                <a href="/api/v1/network/redemptions">On-Chain Data</a> •
                <a href="/docs">API Docs</a>
            </p>
        </body>
        </html>
        """

    return app


def _summarize_capabilities(caps: Optional[dict]) -> dict:
    """Create a brief summary of capabilities."""
    if not caps:
        return {}

    summary = {}
    gpus = caps.get("gpus", [])
    if gpus:
        gpu_names = [g.get("name", "Unknown") for g in gpus]
        summary["gpus"] = f"{len(gpus)}x {gpu_names[0]}" if gpu_names else f"{len(gpus)} GPUs"
    else:
        summary["gpus"] = "No GPU"

    mem = caps.get("memory", {})
    if mem:
        summary["memory"] = f"{mem.get('total_mb', 0) // 1024} GB"

    cpu = caps.get("cpu", {})
    if cpu:
        summary["cpu"] = f"{cpu.get('cores_physical', '?')} cores"

    return summary


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
