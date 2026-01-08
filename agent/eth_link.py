"""
Ethereum address linking.

Allows orchestrators to prove they control an ETH address by signing a message.
This links their Ed25519 node ID to their Livepeer orchestrator address.
"""
from __future__ import annotations

import json
import time
import hashlib
from typing import Optional
from dataclasses import dataclass, asdict
from pathlib import Path

# Note: For ETH signature verification, we use eth_account
# pip install eth-account
try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    HAS_ETH_ACCOUNT = True
except ImportError:
    HAS_ETH_ACCOUNT = False


@dataclass
class AddressLink:
    """
    Links an Ed25519 node ID to an Ethereum address.

    The orchestrator proves control by signing a message with their ETH key.
    """
    node_id: str           # Ed25519 public key (hex)
    eth_address: str       # Ethereum address (0x...)
    timestamp: int         # When the link was created
    message: str           # The message that was signed
    eth_signature: str     # Ethereum signature (hex)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "AddressLink":
        return cls(**data)


def create_link_message(node_id: str, eth_address: str, timestamp: int) -> str:
    """
    Create the message to be signed for address linking.

    Format is human-readable for wallet signing UIs.
    """
    return (
        f"Livepeer SLA Agent Address Link\n"
        f"\n"
        f"I am linking my Livepeer orchestrator address to this SLA agent node.\n"
        f"\n"
        f"Node ID: {node_id}\n"
        f"ETH Address: {eth_address}\n"
        f"Timestamp: {timestamp}\n"
        f"\n"
        f"This signature proves I control both the orchestrator and this node."
    )


def verify_address_link(link: AddressLink) -> bool:
    """
    Verify that an address link is valid.

    Checks that the ETH signature matches the claimed address.
    """
    if not HAS_ETH_ACCOUNT:
        raise ImportError(
            "eth-account package required for ETH signature verification. "
            "Install with: pip install eth-account"
        )

    # Recreate the expected message
    expected_message = create_link_message(link.node_id, link.eth_address, link.timestamp)

    if expected_message != link.message:
        return False

    try:
        # Recover signer address from signature
        message_hash = encode_defunct(text=link.message)
        recovered_address = Account.recover_message(message_hash, signature=link.eth_signature)

        # Check if recovered address matches claimed address
        return recovered_address.lower() == link.eth_address.lower()

    except Exception:
        return False


def create_link_for_signing(node_id: str, eth_address: str) -> dict:
    """
    Create a link request that the orchestrator needs to sign.

    Returns dict with message to sign and other details.
    The orchestrator signs this with their ETH wallet.
    """
    timestamp = int(time.time())
    message = create_link_message(node_id, eth_address, timestamp)

    return {
        "node_id": node_id,
        "eth_address": eth_address,
        "timestamp": timestamp,
        "message": message,
        "instructions": (
            "Sign this message with your orchestrator's ETH wallet. "
            "In MetaMask: Settings -> Security -> Sign Message. "
            "Then submit the signature to complete the link."
        )
    }


def sign_link_locally(node_id: str, eth_private_key: str) -> AddressLink:
    """
    Sign an address link locally (for testing or automated setups).

    WARNING: This requires the ETH private key. In production,
    orchestrators should sign via their wallet (MetaMask, etc).
    """
    if not HAS_ETH_ACCOUNT:
        raise ImportError("eth-account package required")

    account = Account.from_key(eth_private_key)
    eth_address = account.address
    timestamp = int(time.time())
    message = create_link_message(node_id, eth_address, timestamp)

    # Sign the message
    message_hash = encode_defunct(text=message)
    signed = account.sign_message(message_hash)

    return AddressLink(
        node_id=node_id,
        eth_address=eth_address,
        timestamp=timestamp,
        message=message,
        eth_signature=signed.signature.hex()
    )


class AddressLinkRegistry:
    """
    Stores verified address links.

    Maps node IDs to their linked ETH addresses.
    """

    def __init__(self, persist_path: Optional[Path] = None):
        self.persist_path = persist_path
        self._links: dict[str, AddressLink] = {}  # node_id -> link
        self._by_eth: dict[str, str] = {}  # eth_address -> node_id

        if self.persist_path and self.persist_path.exists():
            self._load()

    def add_link(self, link: AddressLink) -> bool:
        """
        Add a verified link to the registry.

        Returns True if link was valid and added.
        """
        if not verify_address_link(link):
            return False

        # Check for existing links (one-to-one mapping)
        eth_lower = link.eth_address.lower()

        # Allow updating if same node is re-linking
        if eth_lower in self._by_eth:
            old_node = self._by_eth[eth_lower]
            if old_node != link.node_id:
                # Different node trying to claim same ETH address
                # Only allow if new link is more recent
                old_link = self._links.get(old_node)
                if old_link and old_link.timestamp > link.timestamp:
                    return False
                # Remove old link
                del self._links[old_node]

        self._links[link.node_id] = link
        self._by_eth[eth_lower] = link.node_id
        self._persist()
        return True

    def get_eth_address(self, node_id: str) -> Optional[str]:
        """Get the ETH address linked to a node ID."""
        link = self._links.get(node_id)
        return link.eth_address if link else None

    def get_node_id(self, eth_address: str) -> Optional[str]:
        """Get the node ID linked to an ETH address."""
        return self._by_eth.get(eth_address.lower())

    def get_link(self, node_id: str) -> Optional[AddressLink]:
        """Get the full link details for a node."""
        return self._links.get(node_id)

    def is_linked(self, node_id: str) -> bool:
        """Check if a node has a linked ETH address."""
        return node_id in self._links

    def all_links(self) -> list[AddressLink]:
        """Get all registered links."""
        return list(self._links.values())

    def _persist(self) -> None:
        """Persist links to disk (best-effort)."""
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [link.to_dict() for link in self._links.values()]
            self.persist_path.write_text(json.dumps(payload, indent=2))
        except Exception:
            pass

    def _load(self) -> None:
        """Load persisted links from disk (best-effort)."""
        if not self.persist_path:
            return
        try:
            data = json.loads(self.persist_path.read_text())
            if not isinstance(data, list):
                return
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    link = AddressLink.from_dict(item)
                except Exception:
                    continue
                self._links[link.node_id] = link
                self._by_eth[link.eth_address.lower()] = link.node_id
        except Exception:
            return
