"""
On-chain data client for Livepeer.

Queries Livepeer smart contracts directly on Arbitrum One via JSON-RPC.
No external indexer (The Graph) required - fully trustless.

Data tracked:
- Winning ticket redemptions (proof of work)
- Orchestrator earnings
- Gateway (broadcaster) activity
"""
from __future__ import annotations

import os
import logging
from typing import Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from eth_abi import decode
from eth_utils import keccak

import httpx

logger = logging.getLogger(__name__)

# Arbitrum One RPC endpoints
# Public RPC (rate limited but free, no API key needed)
ARBITRUM_PUBLIC_RPC = "https://arb1.arbitrum.io/rpc"

# Alternative RPCs (can set via ARBITRUM_RPC_URL env var):
# - Alchemy: https://arb-mainnet.g.alchemy.com/v2/{API_KEY}
# - Infura: https://arbitrum-mainnet.infura.io/v3/{API_KEY}
# - Ankr: https://rpc.ankr.com/arbitrum

# Livepeer contract addresses on Arbitrum One
# Source: https://github.com/livepeer/protocol/blob/master/deployments/arbitrumOne.json
TICKET_BROKER_ADDRESS = "0xa8bb618B1520E284046F3dFc448851A1Ff26e41B"

# WinningTicketRedeemed event signature
# event WinningTicketRedeemed(
#     address indexed sender,
#     address indexed recipient,
#     uint256 faceValue,
#     uint256 winProb,
#     uint256 senderNonce,
#     uint256 recipientRand,
#     bytes auxData
# )
WINNING_TICKET_REDEEMED_TOPIC = "0x" + keccak(
    text="WinningTicketRedeemed(address,address,uint256,uint256,uint256,uint256,bytes)"
).hex()


@dataclass
class TicketRedemption:
    """A winning ticket redeemed on-chain."""
    id: str
    recipient: str          # Orchestrator ETH address
    sender: str             # Gateway/Broadcaster ETH address
    face_value_wei: int     # Payment amount in wei
    face_value_eth: float   # Payment amount in ETH
    win_prob: int           # Winning probability
    block_number: int       # Block number
    timestamp: int          # Unix timestamp (estimated from block)
    tx_hash: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrchestratorEarnings:
    """Summary of orchestrator earnings from ticket redemptions."""
    eth_address: str
    total_tickets: int
    total_eth: float
    unique_broadcasters: int
    first_redemption: Optional[int]  # Unix timestamp
    last_redemption: Optional[int]   # Unix timestamp
    period_days: int

    def to_dict(self) -> dict:
        return asdict(self)


class LivepeerOnChain:
    """
    Client for querying Livepeer contracts directly on Arbitrum One.

    Uses JSON-RPC to query event logs - no external indexer required.
    """

    def __init__(self, rpc_url: Optional[str] = None):
        self.rpc_url = rpc_url or os.environ.get("ARBITRUM_RPC_URL", ARBITRUM_PUBLIC_RPC)
        self._client: Optional[httpx.AsyncClient] = None
        self._block_timestamps: dict[int, int] = {}  # Cache block -> timestamp
        logger.info(f"Using Arbitrum RPC: {self.rpc_url}")

    @property
    def enabled(self) -> bool:
        """Always enabled - uses public RPC by default."""
        return True

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def _rpc_call(self, method: str, params: list) -> dict:
        """Execute a JSON-RPC call."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1
        }

        response = await self._client.post(self.rpc_url, json=payload)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            logger.error(f"RPC error: {data['error']}")
            raise Exception(f"RPC error: {data['error'].get('message', data['error'])}")

        return data.get("result")

    async def _get_block_timestamp(self, block_number: int, fast_estimate: bool = False) -> int:
        """Get timestamp for a block (with caching).

        Args:
            block_number: The block number
            fast_estimate: If True, estimate timestamp from block number instead of RPC call
        """
        if block_number in self._block_timestamps:
            return self._block_timestamps[block_number]

        if fast_estimate:
            # Fast path: estimate timestamp from block number
            # Arbitrum has ~0.25s blocks, so we estimate from current time
            if not hasattr(self, '_reference_block') or not hasattr(self, '_reference_time'):
                self._reference_block = await self._get_current_block()
                self._reference_time = int(datetime.utcnow().timestamp())

            # Arbitrum: ~4 blocks per second = 0.25s per block
            block_diff = self._reference_block - block_number
            time_diff = int(block_diff * 0.25)  # 0.25 seconds per block
            estimated_timestamp = self._reference_time - time_diff
            return estimated_timestamp

        block = await self._rpc_call("eth_getBlockByNumber", [hex(block_number), False])
        if block:
            timestamp = int(block["timestamp"], 16)
            self._block_timestamps[block_number] = timestamp
            return timestamp
        return 0

    async def _get_current_block(self) -> int:
        """Get current block number."""
        result = await self._rpc_call("eth_blockNumber", [])
        return int(result, 16)

    async def _estimate_block_from_timestamp(self, timestamp: int) -> int:
        """
        Estimate block number from timestamp.
        Arbitrum has ~0.25s block time, but we use 1 block/sec as conservative estimate.
        """
        current_block = await self._get_current_block()
        current_time = int(datetime.utcnow().timestamp())
        time_diff = current_time - timestamp
        # Conservative: assume 1 block per second (actual is faster)
        block_diff = time_diff
        estimated_block = max(0, current_block - block_diff)
        return estimated_block

    async def get_ticket_redemptions(
        self,
        orchestrator_address: Optional[str] = None,
        gateway_address: Optional[str] = None,
        from_block: Optional[int] = None,
        to_block: Optional[int] = None,
        limit: int = 100
    ) -> list[TicketRedemption]:
        """
        Get winning ticket redemptions from the TicketBroker contract.

        Args:
            orchestrator_address: Filter by recipient (orchestrator)
            gateway_address: Filter by sender (gateway/broadcaster)
            from_block: Start block (default: last ~24h)
            to_block: End block (default: latest)
            limit: Maximum results to return
        """
        # Build topics filter
        topics = [WINNING_TICKET_REDEEMED_TOPIC]

        # Topic 1: sender (gateway) - indexed
        if gateway_address:
            topics.append("0x000000000000000000000000" + gateway_address.lower()[2:])
        else:
            topics.append(None)

        # Topic 2: recipient (orchestrator) - indexed
        if orchestrator_address:
            topics.append("0x000000000000000000000000" + orchestrator_address.lower()[2:])
        else:
            topics.append(None)

        # Default to last ~24 hours if no from_block specified
        if from_block is None:
            # Arbitrum: ~4 blocks/sec = ~345,600 blocks/day
            # Use conservative 100k blocks (~7 hours) to avoid rate limits
            current = await self._get_current_block()
            from_block = current - 100000

        params = {
            "address": TICKET_BROKER_ADDRESS,
            "topics": topics,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block) if to_block else "latest"
        }

        logs = await self._rpc_call("eth_getLogs", [params])

        if not logs:
            return []

        redemptions = []
        for log in logs[-limit:]:  # Take last N (most recent)
            try:
                redemption = await self._parse_redemption_log(log)
                if redemption:
                    redemptions.append(redemption)
            except Exception as e:
                logger.warning(f"Failed to parse log: {e}")
                continue

        # Sort by block number descending (most recent first)
        redemptions.sort(key=lambda x: x.block_number, reverse=True)
        return redemptions[:limit]

    async def _parse_redemption_log(self, log: dict) -> Optional[TicketRedemption]:
        """Parse a WinningTicketRedeemed event log."""
        topics = log.get("topics", [])
        data = log.get("data", "0x")

        if len(topics) < 3:
            return None

        # Indexed params from topics
        sender = "0x" + topics[1][-40:]  # Gateway/Broadcaster
        recipient = "0x" + topics[2][-40:]  # Orchestrator

        # Non-indexed params from data
        # (uint256 faceValue, uint256 winProb, uint256 senderNonce, uint256 recipientRand, bytes auxData)
        try:
            decoded = decode(
                ["uint256", "uint256", "uint256", "uint256", "bytes"],
                bytes.fromhex(data[2:])
            )
            face_value_wei = decoded[0]
            win_prob = decoded[1]
        except Exception as e:
            logger.debug(f"Failed to decode event data: {e}")
            face_value_wei = 0
            win_prob = 0

        block_number = int(log["blockNumber"], 16)
        tx_hash = log.get("transactionHash")

        # Get block timestamp (use fast estimation to avoid RPC calls for each ticket)
        timestamp = await self._get_block_timestamp(block_number, fast_estimate=True)

        return TicketRedemption(
            id=f"{tx_hash}-{log.get('logIndex', '0')}",
            recipient=recipient,
            sender=sender,
            face_value_wei=face_value_wei,
            face_value_eth=face_value_wei / 1e18,
            win_prob=win_prob,
            block_number=block_number,
            timestamp=timestamp,
            tx_hash=tx_hash
        )

    async def get_orchestrator_earnings(
        self,
        orchestrator_address: str,
        days: int = 7
    ) -> OrchestratorEarnings:
        """
        Get earnings summary for an orchestrator.

        Args:
            orchestrator_address: ETH address of the orchestrator
            days: Number of days to look back (max ~7 due to RPC limits)
        """
        # Calculate from_block based on days
        # Arbitrum: ~345,600 blocks/day, but limit to avoid RPC issues
        blocks_per_day = 345600
        blocks_to_query = min(days * blocks_per_day, 500000)  # Cap at ~1.5 days

        current = await self._get_current_block()
        from_block = current - blocks_to_query

        redemptions = await self.get_ticket_redemptions(
            orchestrator_address=orchestrator_address,
            from_block=from_block,
            limit=1000
        )

        if not redemptions:
            return OrchestratorEarnings(
                eth_address=orchestrator_address,
                total_tickets=0,
                total_eth=0.0,
                unique_broadcasters=0,
                first_redemption=None,
                last_redemption=None,
                period_days=days
            )

        total_eth = sum(r.face_value_eth for r in redemptions)
        unique_broadcasters = len(set(r.sender for r in redemptions))
        timestamps = [r.timestamp for r in redemptions if r.timestamp > 0]

        return OrchestratorEarnings(
            eth_address=orchestrator_address,
            total_tickets=len(redemptions),
            total_eth=total_eth,
            unique_broadcasters=unique_broadcasters,
            first_redemption=min(timestamps) if timestamps else None,
            last_redemption=max(timestamps) if timestamps else None,
            period_days=days
        )

    async def get_recent_network_redemptions(
        self,
        limit: int = 100
    ) -> list[TicketRedemption]:
        """Get recent ticket redemptions across the entire network."""
        return await self.get_ticket_redemptions(limit=limit)

    async def get_network_stats(self, hours: int = 6) -> dict:
        """
        Get network-wide redemption statistics.

        Note: Limited to ~6 hours due to RPC query limits on public endpoints.
        """
        # ~4 blocks/sec * 3600 sec/hr = ~14,400 blocks/hr
        blocks_per_hour = 14400
        blocks_to_query = min(hours * blocks_per_hour, 100000)

        current = await self._get_current_block()
        from_block = current - blocks_to_query

        redemptions = await self.get_ticket_redemptions(
            from_block=from_block,
            limit=1000
        )

        if not redemptions:
            return {
                "period_hours": hours,
                "total_tickets": 0,
                "total_eth": 0.0,
                "active_orchestrators": 0,
                "active_gateways": 0
            }

        total_eth = sum(r.face_value_eth for r in redemptions)
        orchestrators = set(r.recipient for r in redemptions)
        gateways = set(r.sender for r in redemptions)

        return {
            "period_hours": hours,
            "total_tickets": len(redemptions),
            "total_eth": total_eth,
            "active_orchestrators": len(orchestrators),
            "active_gateways": len(gateways)
        }

    async def get_gateway_traffic(
        self,
        gateway_address: Optional[str] = None,
        hours: int = 6
    ) -> list[dict]:
        """
        Get traffic breakdown by gateway (broadcaster).

        Shows which orchestrators each gateway is sending work to.
        """
        blocks_per_hour = 14400
        blocks_to_query = min(hours * blocks_per_hour, 100000)

        current = await self._get_current_block()
        from_block = current - blocks_to_query

        redemptions = await self.get_ticket_redemptions(
            gateway_address=gateway_address,
            from_block=from_block,
            limit=1000
        )

        # Aggregate by gateway -> orchestrator pairs
        traffic: dict[str, dict] = {}
        for r in redemptions:
            key = f"{r.sender}:{r.recipient}"

            if key not in traffic:
                traffic[key] = {
                    "gateway": r.sender,
                    "orchestrator": r.recipient,
                    "ticket_count": 0,
                    "total_eth": 0.0,
                    "first_ticket": r.timestamp,
                    "last_ticket": r.timestamp
                }

            traffic[key]["ticket_count"] += 1
            traffic[key]["total_eth"] += r.face_value_eth
            if r.timestamp > 0:
                traffic[key]["first_ticket"] = min(traffic[key]["first_ticket"], r.timestamp)
                traffic[key]["last_ticket"] = max(traffic[key]["last_ticket"], r.timestamp)

        # Sort by total ETH (most active pairs first)
        return sorted(traffic.values(), key=lambda x: x["total_eth"], reverse=True)

    async def get_orchestrator_activity(
        self,
        orchestrator_address: str,
        days: int = 7
    ) -> list[dict]:
        """
        Get daily activity breakdown for an orchestrator.

        Returns list of {date, tickets, eth} for each day.
        Note: Limited by RPC query depth (~1-2 days on public RPC).
        """
        blocks_per_day = 345600
        blocks_to_query = min(days * blocks_per_day, 500000)

        current = await self._get_current_block()
        from_block = current - blocks_to_query

        redemptions = await self.get_ticket_redemptions(
            orchestrator_address=orchestrator_address,
            from_block=from_block,
            limit=1000
        )

        # Group by day
        daily: dict[str, dict] = {}
        for r in redemptions:
            if r.timestamp > 0:
                date = datetime.utcfromtimestamp(r.timestamp).strftime("%Y-%m-%d")
                if date not in daily:
                    daily[date] = {"date": date, "tickets": 0, "eth": 0.0}
                daily[date]["tickets"] += 1
                daily[date]["eth"] += r.face_value_eth

        # Sort by date
        return sorted(daily.values(), key=lambda x: x["date"])

    async def get_gateways_summary(self, hours: int = 6) -> list[dict]:
        """
        Get summary of all active gateways (broadcasters).

        Shows each gateway's total traffic and which orchestrators they use.
        """
        traffic = await self.get_gateway_traffic(hours=hours)

        # Aggregate by gateway
        gateways: dict[str, dict] = {}
        for t in traffic:
            gw = t["gateway"]
            if gw not in gateways:
                gateways[gw] = {
                    "gateway": gw,
                    "total_tickets": 0,
                    "total_eth": 0.0,
                    "orchestrators": [],
                    "orchestrator_count": 0
                }

            gateways[gw]["total_tickets"] += t["ticket_count"]
            gateways[gw]["total_eth"] += t["total_eth"]
            gateways[gw]["orchestrators"].append({
                "address": t["orchestrator"],
                "tickets": t["ticket_count"],
                "eth": t["total_eth"]
            })

        # Calculate orchestrator counts and sort
        for gw in gateways.values():
            gw["orchestrator_count"] = len(gw["orchestrators"])
            gw["orchestrators"] = sorted(
                gw["orchestrators"],
                key=lambda x: x["eth"],
                reverse=True
            )

        return sorted(gateways.values(), key=lambda x: x["total_eth"], reverse=True)


# Global instance
_onchain: Optional[LivepeerOnChain] = None


def get_onchain_client(rpc_url: Optional[str] = None) -> LivepeerOnChain:
    """Get global on-chain client instance."""
    global _onchain
    if _onchain is None:
        _onchain = LivepeerOnChain(rpc_url)
    return _onchain


# Backwards compatibility alias
LivepeerSubgraph = LivepeerOnChain
get_subgraph = get_onchain_client
