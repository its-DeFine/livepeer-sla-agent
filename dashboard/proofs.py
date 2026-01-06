"""
Verification proofs - cryptographic records that prove verification happened.

When the dashboard verifies an orchestrator, it creates a signed proof that can
be independently verified by anyone. This provides:

1. Proof that node X was verified at time T
2. Proof that node X was in top N orchestrators at time T
3. Audit trail of all verifications
"""
from __future__ import annotations

import json
import time
import hashlib
from typing import Optional
from dataclasses import dataclass, asdict
from pathlib import Path

from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import HexEncoder


@dataclass
class VerificationProof:
    """
    Cryptographic proof that an orchestrator was verified.

    This proof can be verified by anyone with the dashboard's public key.
    """
    proof_id: str              # Unique proof identifier
    node_id: str               # Ed25519 node ID that was verified
    eth_address: Optional[str]  # Linked ETH address (if any)
    verification_type: str      # "liveness", "transcode", "capability"
    timestamp: int             # Unix timestamp of verification
    result: dict               # Verification result details
    orchestrator_rank: Optional[int]  # Rank at time of verification (if top 100)
    dashboard_id: str          # Dashboard's public key
    signature: str             # Dashboard's signature on this proof

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "VerificationProof":
        return cls(**data)

    @classmethod
    def from_json(cls, data: str) -> "VerificationProof":
        return cls.from_dict(json.loads(data))


@dataclass
class Top100Attestation:
    """
    Attestation that a set of orchestrators were verified as top 100 at a specific time.

    This is a batch proof that can be published periodically (e.g., hourly).
    """
    attestation_id: str
    timestamp: int
    orchestrators: list[dict]  # List of {rank, eth_address, node_id, verified, verification_time}
    total_verified: int
    dashboard_id: str
    signature: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class ProofGenerator:
    """
    Generates and verifies cryptographic proofs of verification.

    The dashboard signs proofs with its own Ed25519 key.
    """

    def __init__(self, key_path: Optional[Path] = None):
        self.key_path = key_path or Path("./data/dashboard.key")
        self._signing_key: Optional[SigningKey] = None
        self._verify_key: Optional[VerifyKey] = None

    @property
    def dashboard_id(self) -> str:
        """Dashboard's public key (its identity)."""
        if self._verify_key is None:
            self._load_or_generate_key()
        return self._verify_key.encode(encoder=HexEncoder).decode()

    def _load_or_generate_key(self):
        """Load or generate the dashboard's signing key."""
        if self.key_path.exists():
            key_bytes = self.key_path.read_bytes()
            self._signing_key = SigningKey(key_bytes)
        else:
            self._signing_key = SigningKey.generate()
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(self._signing_key.encode())
            self.key_path.chmod(0o600)

        self._verify_key = self._signing_key.verify_key

    def create_verification_proof(
        self,
        node_id: str,
        verification_type: str,
        result: dict,
        eth_address: Optional[str] = None,
        orchestrator_rank: Optional[int] = None
    ) -> VerificationProof:
        """
        Create a signed proof of verification.

        This proof attests that the dashboard verified node X at time T.
        """
        if self._signing_key is None:
            self._load_or_generate_key()

        timestamp = int(time.time())
        proof_id = hashlib.sha256(
            f"{node_id}:{timestamp}:{verification_type}".encode()
        ).hexdigest()[:32]

        # Create the proof without signature
        proof_data = {
            "proof_id": proof_id,
            "node_id": node_id,
            "eth_address": eth_address,
            "verification_type": verification_type,
            "timestamp": timestamp,
            "result": result,
            "orchestrator_rank": orchestrator_rank,
            "dashboard_id": self.dashboard_id,
        }

        # Sign it
        message = self._create_signing_message(proof_data)
        signed = self._signing_key.sign(message)
        signature = signed.signature.hex()

        return VerificationProof(
            **proof_data,
            signature=signature
        )

    def create_top100_attestation(
        self,
        orchestrators: list[dict]
    ) -> Top100Attestation:
        """
        Create a batch attestation of top 100 orchestrator verification status.

        Each orchestrator entry should have:
        - rank: int
        - eth_address: str
        - node_id: str (if linked)
        - verified: bool
        - last_verification: int (timestamp)
        """
        if self._signing_key is None:
            self._load_or_generate_key()

        timestamp = int(time.time())
        attestation_id = hashlib.sha256(
            f"top100:{timestamp}".encode()
        ).hexdigest()[:32]

        total_verified = sum(1 for o in orchestrators if o.get("verified", False))

        attestation_data = {
            "attestation_id": attestation_id,
            "timestamp": timestamp,
            "orchestrators": orchestrators,
            "total_verified": total_verified,
            "dashboard_id": self.dashboard_id,
        }

        # Sign it
        message = self._create_signing_message(attestation_data)
        signed = self._signing_key.sign(message)
        signature = signed.signature.hex()

        return Top100Attestation(
            **attestation_data,
            signature=signature
        )

    def _create_signing_message(self, data: dict) -> bytes:
        """Create deterministic message for signing."""
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return canonical.encode('utf-8')

    @staticmethod
    def verify_proof(proof: VerificationProof) -> bool:
        """
        Verify a verification proof's signature.

        Anyone can call this to verify the proof is authentic.
        """
        try:
            verify_key = VerifyKey(bytes.fromhex(proof.dashboard_id))

            # Reconstruct the data that was signed
            proof_data = {
                "proof_id": proof.proof_id,
                "node_id": proof.node_id,
                "eth_address": proof.eth_address,
                "verification_type": proof.verification_type,
                "timestamp": proof.timestamp,
                "result": proof.result,
                "orchestrator_rank": proof.orchestrator_rank,
                "dashboard_id": proof.dashboard_id,
            }

            message = json.dumps(proof_data, sort_keys=True, separators=(',', ':')).encode()
            signature = bytes.fromhex(proof.signature)

            verify_key.verify(message, signature)
            return True

        except Exception:
            return False

    @staticmethod
    def verify_top100_attestation(attestation: Top100Attestation) -> bool:
        """Verify a top 100 attestation's signature."""
        try:
            verify_key = VerifyKey(bytes.fromhex(attestation.dashboard_id))

            attestation_data = {
                "attestation_id": attestation.attestation_id,
                "timestamp": attestation.timestamp,
                "orchestrators": attestation.orchestrators,
                "total_verified": attestation.total_verified,
                "dashboard_id": attestation.dashboard_id,
            }

            message = json.dumps(attestation_data, sort_keys=True, separators=(',', ':')).encode()
            signature = bytes.fromhex(attestation.signature)

            verify_key.verify(message, signature)
            return True

        except Exception:
            return False


class ProofStorage:
    """
    Stores and indexes verification proofs.
    """

    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or Path("./data/proofs")
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # In-memory indexes
        self._proofs_by_node: dict[str, list[VerificationProof]] = {}
        self._proofs_by_eth: dict[str, list[VerificationProof]] = {}
        self._top100_attestations: list[Top100Attestation] = []

    def store_proof(self, proof: VerificationProof):
        """Store a verification proof."""
        # Index by node
        if proof.node_id not in self._proofs_by_node:
            self._proofs_by_node[proof.node_id] = []
        self._proofs_by_node[proof.node_id].append(proof)

        # Index by ETH address
        if proof.eth_address:
            eth_lower = proof.eth_address.lower()
            if eth_lower not in self._proofs_by_eth:
                self._proofs_by_eth[eth_lower] = []
            self._proofs_by_eth[eth_lower].append(proof)

        # Persist to disk
        proof_file = self.storage_path / f"proof_{proof.proof_id}.json"
        proof_file.write_text(proof.to_json())

    def store_top100_attestation(self, attestation: Top100Attestation):
        """Store a top 100 attestation."""
        self._top100_attestations.append(attestation)

        # Persist to disk
        att_file = self.storage_path / f"top100_{attestation.attestation_id}.json"
        att_file.write_text(attestation.to_json())

    def get_proofs_for_node(self, node_id: str) -> list[VerificationProof]:
        """Get all proofs for a node."""
        return self._proofs_by_node.get(node_id, [])

    def get_proofs_for_eth(self, eth_address: str) -> list[VerificationProof]:
        """Get all proofs for an ETH address."""
        return self._proofs_by_eth.get(eth_address.lower(), [])

    def get_latest_top100(self) -> Optional[Top100Attestation]:
        """Get the most recent top 100 attestation."""
        if not self._top100_attestations:
            return None
        return max(self._top100_attestations, key=lambda a: a.timestamp)

    def was_verified_at(
        self,
        eth_address: str,
        timestamp: int,
        tolerance_seconds: int = 3600
    ) -> Optional[VerificationProof]:
        """
        Check if an orchestrator was verified near a specific time.

        Returns the proof if found within tolerance, None otherwise.
        """
        proofs = self.get_proofs_for_eth(eth_address)

        for proof in proofs:
            if abs(proof.timestamp - timestamp) <= tolerance_seconds:
                return proof

        return None

    def get_verification_history(
        self,
        eth_address: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None
    ) -> list[VerificationProof]:
        """Get verification history for an orchestrator within a time range."""
        proofs = self.get_proofs_for_eth(eth_address)

        if start_time:
            proofs = [p for p in proofs if p.timestamp >= start_time]
        if end_time:
            proofs = [p for p in proofs if p.timestamp <= end_time]

        return sorted(proofs, key=lambda p: p.timestamp)


# Global instances
_proof_generator: Optional[ProofGenerator] = None
_proof_storage: Optional[ProofStorage] = None


def get_proof_generator() -> ProofGenerator:
    global _proof_generator
    if _proof_generator is None:
        _proof_generator = ProofGenerator()
    return _proof_generator


def get_proof_storage() -> ProofStorage:
    global _proof_storage
    if _proof_storage is None:
        _proof_storage = ProofStorage()
    return _proof_storage
