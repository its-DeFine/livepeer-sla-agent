"""
Data models for the dashboard.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class AttestationPayload(BaseModel):
    """Incoming attestation from an agent."""
    node_id: str
    timestamp: int
    payload: dict
    signature: str


class NodeRecord(BaseModel):
    """Stored record of a node."""
    node_id: str
    first_seen: datetime
    last_seen: datetime
    attestation_count: int = 0
    last_capabilities: Optional[dict] = None
    verification_score: float = 0.0  # 0-100 based on challenge responses
    is_online: bool = True


class ChallengeRequest(BaseModel):
    """Request to send a challenge to a node."""
    node_id: str
    challenge_type: str = "liveness"  # or "transcode"


class VerificationJob(BaseModel):
    """A verification job sent to a node."""
    job_id: str
    node_id: str
    job_type: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    success: Optional[bool] = None
    duration_ms: Optional[int] = None
    result: Optional[dict] = None
