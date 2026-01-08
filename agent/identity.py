"""
Identity module - Ed25519 key management and message signing.

This provides cryptographic proof that attestations come from a specific node.
The orchestrator's key serves as their identity on the network.
"""
from __future__ import annotations

import json
import time
import hashlib
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import HexEncoder
from nacl.exceptions import BadSignatureError


@dataclass
class SignedAttestation:
    """A signed attestation that can be verified by anyone with the public key."""
    node_id: str           # Public key (hex)
    timestamp: int         # Unix timestamp
    payload: dict          # The actual data being attested
    signature: str         # Ed25519 signature (hex)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str) -> "SignedAttestation":
        return cls(**json.loads(data))


class NodeIdentity:
    """
    Manages the node's cryptographic identity.

    Key storage: By default, keys are stored in ~/.livepeer-sla/identity.key
    The private key never leaves this machine - only signatures are published.
    """

    def __init__(self, key_path: Optional[Path] = None):
        self.key_path = key_path or Path.home() / ".livepeer-sla" / "identity.key"
        self._signing_key: Optional[SigningKey] = None
        self._verify_key: Optional[VerifyKey] = None

    @property
    def node_id(self) -> str:
        """Public key as hex string - this is the node's identity."""
        if self._verify_key is None:
            raise ValueError("Identity not initialized. Call load() or generate() first.")
        return self._verify_key.encode(encoder=HexEncoder).decode()

    @property
    def signing_key(self) -> SigningKey:
        if self._signing_key is None:
            raise ValueError("Identity not initialized. Call load() or generate() first.")
        return self._signing_key

    def generate(self, force: bool = False) -> str:
        """
        Generate a new Ed25519 keypair.

        Returns the node_id (public key).
        """
        if self.key_path.exists() and not force:
            raise FileExistsError(
                f"Key already exists at {self.key_path}. Use force=True to overwrite."
            )

        self._signing_key = SigningKey.generate()
        self._verify_key = self._signing_key.verify_key

        # Save private key securely
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(self._signing_key.encode())
        self.key_path.chmod(0o600)  # Owner read/write only

        return self.node_id

    def load(self) -> str:
        """Load existing keypair from disk. Returns node_id."""
        if not self.key_path.exists():
            raise FileNotFoundError(
                f"No identity found at {self.key_path}. Run 'sla-agent init' first."
            )

        key_bytes = self.key_path.read_bytes()
        self._signing_key = SigningKey(key_bytes)
        self._verify_key = self._signing_key.verify_key

        return self.node_id

    def load_or_generate(self) -> str:
        """Load existing identity or generate new one."""
        try:
            return self.load()
        except FileNotFoundError:
            return self.generate()

    def sign_attestation(self, payload: dict) -> SignedAttestation:
        """
        Sign a payload and create a verifiable attestation.

        The attestation includes:
        - node_id: Who made this attestation
        - timestamp: When it was made
        - payload: The actual data
        - signature: Cryptographic proof
        """
        timestamp = int(time.time())

        # Create canonical message to sign
        message = self._create_signing_message(self.node_id, timestamp, payload)

        # Sign with Ed25519
        signed = self.signing_key.sign(message)
        signature = signed.signature.hex()

        return SignedAttestation(
            node_id=self.node_id,
            timestamp=timestamp,
            payload=payload,
            signature=signature
        )

    def sign_challenge(self, challenge: str) -> str:
        """
        Sign a challenge string (for liveness proofs).

        Returns hex-encoded signature.
        """
        message = challenge.encode('utf-8')
        signed = self.signing_key.sign(message)
        return signed.signature.hex()

    @staticmethod
    def _create_signing_message(node_id: str, timestamp: int, payload: dict) -> bytes:
        """Create deterministic message for signing."""
        canonical = json.dumps({
            "node_id": node_id,
            "timestamp": timestamp,
            "payload": payload
        }, sort_keys=True, separators=(',', ':'))
        return canonical.encode('utf-8')

    @staticmethod
    def verify_attestation(attestation: SignedAttestation) -> bool:
        """
        Verify that an attestation was signed by the claimed node_id.

        This can be called by anyone - no private key needed.
        """
        try:
            # Reconstruct the verify key from node_id
            verify_key = VerifyKey(bytes.fromhex(attestation.node_id))

            # Reconstruct the message that was signed
            message = NodeIdentity._create_signing_message(
                attestation.node_id,
                attestation.timestamp,
                attestation.payload
            )

            # Verify signature
            signature = bytes.fromhex(attestation.signature)
            verify_key.verify(message, signature)
            return True

        except (BadSignatureError, ValueError):
            return False

    @staticmethod
    def verify_challenge_response(node_id: str, challenge: str, signature: str) -> bool:
        """Verify a challenge-response signature."""
        try:
            verify_key = VerifyKey(bytes.fromhex(node_id))
            verify_key.verify(challenge.encode('utf-8'), bytes.fromhex(signature))
            return True
        except (BadSignatureError, ValueError):
            return False


# Convenience functions
def get_identity(key_path: Optional[Path] = None) -> NodeIdentity:
    """Get initialized identity, loading from disk."""
    identity = NodeIdentity(key_path)
    identity.load_or_generate()
    return identity


def create_endpoint_registration_message(node_id: str, agent_url: str, timestamp: int) -> str:
    """
    Create a deterministic message for endpoint registration signatures.

    This binds (node_id, agent_url, timestamp) so the dashboard can verify that the
    node owner authorized the endpoint mapping.
    """
    agent_url = agent_url.rstrip("/")
    return json.dumps(
        {
            "type": "register_endpoint",
            "node_id": node_id,
            "agent_url": agent_url,
            "timestamp": timestamp,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
