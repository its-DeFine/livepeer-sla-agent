"""
Livepeer network integration.

Queries the Livepeer subgraph to get orchestrator information,
and links our node identity to the orchestrator's ETH address.
"""
from __future__ import annotations

import logging
from typing import Optional
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Livepeer Subgraph endpoints
LIVEPEER_SUBGRAPH_MAINNET = "https://gateway.thegraph.com/api/subgraphs/id/FP5nt9xyQTLgEPEhkFzJMGaNbGgHd9vQbzkxBWDUZqMs"
LIVEPEER_SUBGRAPH_ARBITRUM = "https://gateway.thegraph.com/api/subgraphs/id/Pkp6kB2N3QrdoWkcXgAHxQW8x9xLhiKHUL84B2aYk9X8"

# Alternative: Livepeer's public API
LIVEPEER_EXPLORER_API = "https://explorer.livepeer.org/api"


@dataclass
class Orchestrator:
    """Livepeer orchestrator information."""
    eth_address: str
    total_stake: float  # In LPT
    reward_cut: float   # Percentage
    fee_cut: float      # Percentage
    activation_round: int
    active: bool
    service_uri: Optional[str] = None
    rank: Optional[int] = None


class LivepeerNetwork:
    """
    Interface to the Livepeer network.

    Queries orchestrator data from the subgraph/explorer API.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self._cache: dict[str, list[Orchestrator]] = {}
        self._cache_timestamp: float = 0
        self._cache_ttl = 300  # 5 minutes

    async def get_top_orchestrators(self, limit: int = 100) -> list[Orchestrator]:
        """
        Get top orchestrators by stake.

        Returns list sorted by total stake (descending).
        """
        import time

        # Check cache
        cache_key = f"top_{limit}"
        if cache_key in self._cache and (time.time() - self._cache_timestamp) < self._cache_ttl:
            return self._cache[cache_key]

        orchestrators = await self._query_orchestrators(limit)

        # Update cache
        self._cache[cache_key] = orchestrators
        self._cache_timestamp = time.time()

        return orchestrators

    async def _query_orchestrators(self, limit: int) -> list[Orchestrator]:
        """Query orchestrators from the Livepeer explorer API."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Try the explorer API first (simpler, no API key needed)
                response = await client.get(
                    f"{LIVEPEER_EXPLORER_API}/orchestrators",
                    params={"limit": limit, "order": "totalStake", "sort": "desc"}
                )

                if response.status_code == 200:
                    data = response.json()
                    return self._parse_explorer_response(data, limit)

                # Fallback to subgraph
                return await self._query_subgraph(client, limit)

        except Exception as e:
            logger.error(f"Failed to query orchestrators: {e}")
            return []

    async def _query_subgraph(self, client: httpx.AsyncClient, limit: int) -> list[Orchestrator]:
        """Query the Livepeer subgraph directly."""
        query = """
        query GetOrchestrators($limit: Int!) {
            transcoders(
                first: $limit
                orderBy: totalStake
                orderDirection: desc
                where: { active: true }
            ) {
                id
                totalStake
                rewardCut
                feeShare
                activationRound
                active
                serviceURI
            }
        }
        """

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        response = await client.post(
            LIVEPEER_SUBGRAPH_ARBITRUM,
            json={"query": query, "variables": {"limit": limit}},
            headers=headers
        )

        if response.status_code != 200:
            logger.error(f"Subgraph query failed: {response.status_code}")
            return []

        data = response.json()
        transcoders = data.get("data", {}).get("transcoders", [])

        orchestrators = []
        for i, t in enumerate(transcoders):
            orchestrators.append(Orchestrator(
                eth_address=t["id"],
                total_stake=float(t["totalStake"]) / 1e18,  # Convert from wei
                reward_cut=float(t.get("rewardCut", 0)) / 10000,  # Basis points to %
                fee_cut=float(t.get("feeShare", 0)) / 10000,
                activation_round=int(t.get("activationRound", 0)),
                active=t.get("active", False),
                service_uri=t.get("serviceURI"),
                rank=i + 1
            ))

        return orchestrators

    def _parse_explorer_response(self, data: list | dict, limit: int) -> list[Orchestrator]:
        """Parse response from Livepeer explorer API."""
        if isinstance(data, dict):
            data = data.get("data", data.get("orchestrators", []))

        orchestrators = []
        for i, item in enumerate(data[:limit]):
            try:
                orchestrators.append(Orchestrator(
                    eth_address=item.get("id", item.get("address", "")),
                    total_stake=float(item.get("totalStake", 0)) / 1e18,
                    reward_cut=float(item.get("rewardCut", 0)) / 10000,
                    fee_cut=float(item.get("feeShare", item.get("feeCut", 0))) / 10000,
                    activation_round=int(item.get("activationRound", 0)),
                    active=item.get("active", True),
                    service_uri=item.get("serviceURI", item.get("serviceUri")),
                    rank=i + 1
                ))
            except (KeyError, ValueError) as e:
                logger.warning(f"Failed to parse orchestrator: {e}")
                continue

        return orchestrators

    async def is_top_orchestrator(self, eth_address: str, top_n: int = 100) -> tuple[bool, Optional[int]]:
        """
        Check if an address is in the top N orchestrators.

        Returns (is_top, rank) where rank is 1-indexed position or None.
        """
        orchestrators = await self.get_top_orchestrators(top_n)

        eth_address_lower = eth_address.lower()
        for orch in orchestrators:
            if orch.eth_address.lower() == eth_address_lower:
                return True, orch.rank

        return False, None

    async def get_orchestrator(self, eth_address: str) -> Optional[Orchestrator]:
        """Get details for a specific orchestrator."""
        orchestrators = await self.get_top_orchestrators(200)  # Get more to find it

        eth_address_lower = eth_address.lower()
        for orch in orchestrators:
            if orch.eth_address.lower() == eth_address_lower:
                return orch

        return None


# Global instance
_network: Optional[LivepeerNetwork] = None


def get_livepeer_network(api_key: Optional[str] = None) -> LivepeerNetwork:
    """Get global Livepeer network instance."""
    global _network
    if _network is None:
        _network = LivepeerNetwork(api_key)
    return _network
