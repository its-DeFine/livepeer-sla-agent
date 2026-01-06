"""
On-chain data client for Livepeer.

Queries the Livepeer subgraph for verifiable on-chain data:
- Winning ticket redemptions (proof of work)
- Orchestrator earnings
- Broadcaster activity

This provides trustless verification of orchestrator work.
"""
from __future__ import annotations

import os
import logging
from typing import Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

# Subgraph endpoints
SUBGRAPH_FREE = "https://api.thegraph.com/subgraphs/name/livepeer/arbitrum-one"
SUBGRAPH_GATEWAY = "https://gateway.thegraph.com/api/{api_key}/subgraphs/id/Pkp6kB2N3QrdoWkcXgAHxQW8x9xLhiKHUL84B2aYk9X8"


@dataclass
class TicketRedemption:
    """A winning ticket redeemed on-chain."""
    id: str
    recipient: str          # Orchestrator ETH address
    sender: str             # Broadcaster ETH address
    face_value_wei: int     # Payment amount in wei
    face_value_eth: float   # Payment amount in ETH
    win_prob: int           # Winning probability
    creation_round: int     # Livepeer round
    timestamp: int          # Unix timestamp
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


class LivepeerSubgraph:
    """
    Client for querying the Livepeer subgraph.

    Uses free tier by default, or gateway with API key for higher limits.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GRAPH_API_KEY")

        if self.api_key:
            self.endpoint = SUBGRAPH_GATEWAY.format(api_key=self.api_key)
            logger.info("Using The Graph gateway (API key provided)")
        else:
            self.endpoint = SUBGRAPH_FREE
            logger.info("Using The Graph free tier (no API key)")

        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def _query(self, query: str, variables: dict = None) -> dict:
        """Execute a GraphQL query."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

        response = await self._client.post(
            self.endpoint,
            json={"query": query, "variables": variables or {}}
        )
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            logger.error(f"GraphQL errors: {data['errors']}")
            raise Exception(f"GraphQL error: {data['errors'][0]['message']}")

        return data.get("data", {})

    async def get_ticket_redemptions(
        self,
        orchestrator_address: str,
        limit: int = 100,
        skip: int = 0,
        since_timestamp: Optional[int] = None
    ) -> list[TicketRedemption]:
        """
        Get winning ticket redemptions for an orchestrator.

        Args:
            orchestrator_address: ETH address of the orchestrator
            limit: Maximum number of results
            skip: Number of results to skip (pagination)
            since_timestamp: Only return redemptions after this time
        """
        where_clause = f'recipient: "{orchestrator_address.lower()}"'
        if since_timestamp:
            where_clause += f", timestamp_gte: {since_timestamp}"

        query = f"""
        query GetRedemptions($limit: Int!, $skip: Int!) {{
            winningTicketRedeemedEvents(
                where: {{ {where_clause} }}
                orderBy: timestamp
                orderDirection: desc
                first: $limit
                skip: $skip
            ) {{
                id
                recipient
                sender
                faceValue
                winProb
                creationRound
                timestamp
                transaction {{
                    id
                }}
            }}
        }}
        """

        data = await self._query(query, {"limit": limit, "skip": skip})
        events = data.get("winningTicketRedeemedEvents", [])

        return [
            TicketRedemption(
                id=e["id"],
                recipient=e["recipient"],
                sender=e["sender"],
                face_value_wei=int(e["faceValue"]),
                face_value_eth=int(e["faceValue"]) / 1e18,
                win_prob=int(e["winProb"]),
                creation_round=int(e["creationRound"]),
                timestamp=int(e["timestamp"]),
                tx_hash=e.get("transaction", {}).get("id")
            )
            for e in events
        ]

    async def get_orchestrator_earnings(
        self,
        orchestrator_address: str,
        days: int = 30
    ) -> OrchestratorEarnings:
        """
        Get earnings summary for an orchestrator.

        Args:
            orchestrator_address: ETH address of the orchestrator
            days: Number of days to look back
        """
        since = int((datetime.utcnow() - timedelta(days=days)).timestamp())

        # Get all redemptions in period
        all_redemptions = []
        skip = 0
        while True:
            batch = await self.get_ticket_redemptions(
                orchestrator_address,
                limit=1000,
                skip=skip,
                since_timestamp=since
            )
            if not batch:
                break
            all_redemptions.extend(batch)
            skip += len(batch)
            if len(batch) < 1000:
                break

        if not all_redemptions:
            return OrchestratorEarnings(
                eth_address=orchestrator_address,
                total_tickets=0,
                total_eth=0.0,
                unique_broadcasters=0,
                first_redemption=None,
                last_redemption=None,
                period_days=days
            )

        total_eth = sum(r.face_value_eth for r in all_redemptions)
        unique_broadcasters = len(set(r.sender for r in all_redemptions))
        timestamps = [r.timestamp for r in all_redemptions]

        return OrchestratorEarnings(
            eth_address=orchestrator_address,
            total_tickets=len(all_redemptions),
            total_eth=total_eth,
            unique_broadcasters=unique_broadcasters,
            first_redemption=min(timestamps),
            last_redemption=max(timestamps),
            period_days=days
        )

    async def get_recent_network_redemptions(
        self,
        limit: int = 100
    ) -> list[TicketRedemption]:
        """Get recent ticket redemptions across the entire network."""
        query = """
        query GetRecentRedemptions($limit: Int!) {
            winningTicketRedeemedEvents(
                orderBy: timestamp
                orderDirection: desc
                first: $limit
            ) {
                id
                recipient
                sender
                faceValue
                winProb
                creationRound
                timestamp
                transaction {
                    id
                }
            }
        }
        """

        data = await self._query(query, {"limit": limit})
        events = data.get("winningTicketRedeemedEvents", [])

        return [
            TicketRedemption(
                id=e["id"],
                recipient=e["recipient"],
                sender=e["sender"],
                face_value_wei=int(e["faceValue"]),
                face_value_eth=int(e["faceValue"]) / 1e18,
                win_prob=int(e["winProb"]),
                creation_round=int(e["creationRound"]),
                timestamp=int(e["timestamp"]),
                tx_hash=e.get("transaction", {}).get("id")
            )
            for e in events
        ]

    async def get_network_stats(self, hours: int = 24) -> dict:
        """Get network-wide redemption statistics."""
        since = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())

        query = f"""
        query GetNetworkStats {{
            winningTicketRedeemedEvents(
                where: {{ timestamp_gte: {since} }}
                first: 1000
            ) {{
                recipient
                sender
                faceValue
            }}
        }}
        """

        data = await self._query(query)
        events = data.get("winningTicketRedeemedEvents", [])

        if not events:
            return {
                "period_hours": hours,
                "total_tickets": 0,
                "total_eth": 0.0,
                "active_orchestrators": 0,
                "active_broadcasters": 0
            }

        total_eth = sum(int(e["faceValue"]) / 1e18 for e in events)
        orchestrators = set(e["recipient"] for e in events)
        broadcasters = set(e["sender"] for e in events)

        return {
            "period_hours": hours,
            "total_tickets": len(events),
            "total_eth": total_eth,
            "active_orchestrators": len(orchestrators),
            "active_broadcasters": len(broadcasters)
        }

    async def get_orchestrator_activity(
        self,
        orchestrator_address: str,
        days: int = 7
    ) -> list[dict]:
        """
        Get daily activity breakdown for an orchestrator.

        Returns list of {date, tickets, eth} for each day.
        """
        since = int((datetime.utcnow() - timedelta(days=days)).timestamp())
        redemptions = await self.get_ticket_redemptions(
            orchestrator_address,
            limit=1000,
            since_timestamp=since
        )

        # Group by day
        daily: dict[str, dict] = {}
        for r in redemptions:
            date = datetime.utcfromtimestamp(r.timestamp).strftime("%Y-%m-%d")
            if date not in daily:
                daily[date] = {"date": date, "tickets": 0, "eth": 0.0}
            daily[date]["tickets"] += 1
            daily[date]["eth"] += r.face_value_eth

        # Sort by date
        return sorted(daily.values(), key=lambda x: x["date"])


    async def get_gateway_traffic(
        self,
        gateway_address: Optional[str] = None,
        hours: int = 24
    ) -> list[dict]:
        """
        Get traffic breakdown by gateway (broadcaster).

        Shows which orchestrators each gateway is sending work to.
        If gateway_address is provided, filter to that specific gateway.
        """
        since = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())

        where_clause = f"timestamp_gte: {since}"
        if gateway_address:
            where_clause += f', sender: "{gateway_address.lower()}"'

        query = f"""
        query GetGatewayTraffic {{
            winningTicketRedeemedEvents(
                where: {{ {where_clause} }}
                first: 1000
                orderBy: timestamp
                orderDirection: desc
            ) {{
                sender
                recipient
                faceValue
                timestamp
            }}
        }}
        """

        data = await self._query(query)
        events = data.get("winningTicketRedeemedEvents", [])

        # Aggregate by gateway -> orchestrator pairs
        traffic: dict[str, dict] = {}
        for e in events:
            gateway = e["sender"]
            orch = e["recipient"]
            key = f"{gateway}:{orch}"

            if key not in traffic:
                traffic[key] = {
                    "gateway": gateway,
                    "orchestrator": orch,
                    "ticket_count": 0,
                    "total_eth": 0.0,
                    "first_ticket": int(e["timestamp"]),
                    "last_ticket": int(e["timestamp"])
                }

            traffic[key]["ticket_count"] += 1
            traffic[key]["total_eth"] += int(e["faceValue"]) / 1e18
            traffic[key]["first_ticket"] = min(traffic[key]["first_ticket"], int(e["timestamp"]))
            traffic[key]["last_ticket"] = max(traffic[key]["last_ticket"], int(e["timestamp"]))

        # Sort by total ETH (most active pairs first)
        result = sorted(traffic.values(), key=lambda x: x["total_eth"], reverse=True)

        return result

    async def get_gateways_summary(self, hours: int = 24) -> list[dict]:
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
            gw["orchestrators"] = sorted(gw["orchestrators"], key=lambda x: x["eth"], reverse=True)

        return sorted(gateways.values(), key=lambda x: x["total_eth"], reverse=True)


# Global instance
_subgraph: Optional[LivepeerSubgraph] = None


def get_subgraph(api_key: Optional[str] = None) -> LivepeerSubgraph:
    """Get global subgraph client instance."""
    global _subgraph
    if _subgraph is None:
        _subgraph = LivepeerSubgraph(api_key)
    return _subgraph
