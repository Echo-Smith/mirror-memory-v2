"""Frozen core types for Mirror Memory v2.

These types define the interface contract. Changes require a decision record
and regression across all dependent tests.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Identifiers and scope
# ---------------------------------------------------------------------------


class Scope(BaseModel):
    """Immutable triple that isolates all data access.

    Bound by a trusted business backend — end-user JSON cannot become a Scope.
    """

    tenant_id: str
    app_id: str
    subject_id: str

    def key(self) -> str:
        """Deterministic string key for uniqueness constraints."""
        return f"{self.tenant_id}:{self.app_id}:{self.subject_id}"


class MemoryContext(BaseModel):
    """Trusted calling context bound by a business backend.

    agent_id and session_id are optional; omitting them must NOT relax
    app-level isolation.
    """

    scope: Scope
    purpose: str
    agent_id: str | None = None
    session_id: str | None = None


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class AuthorizationSnapshot(BaseModel):
    """Versioned authorization issued by a trusted provider.

    Every protected operation validates allowed_operations, expiry,
    and local latest version before proceeding.
    """

    scope: Scope
    purpose: str
    allowed_operations: list[str]
    version: int
    issued_at: datetime
    expires_at: datetime
    issuer: str


# ---------------------------------------------------------------------------
# Processing states
# ---------------------------------------------------------------------------


class ProcessingState(str, enum.Enum):
    """Job lifecycle states.

    Transitions:
        pending -> running -> ready
                          -> pending (retryable)
                          -> failed  (retries exhausted / permanent error)
                          -> cancelled (revoke / deletion)
                          -> expired  (source retention expired)
    """

    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class RecallOutcome(str, enum.Enum):
    """Distinguishable recall results — never confuse pending with empty."""

    FOUND = "found"
    PENDING = "pending"
    NO_MEMORY = "no_memory"
    DENIED = "denied"
    TIMEOUT = "timeout"


class DeletionStatus(str, enum.Enum):
    """Deletion receipt states."""

    BLOCKED = "blocked"
    PURGING = "purging"
    VERIFIED = "verified"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Source and evidence
# ---------------------------------------------------------------------------


class SourceRole(str, enum.Enum):
    """Who produced the source text."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SourceEvent(BaseModel):
    """An observable event from a conversation session.

    source_event_id is idempotency key within scope.
    """

    source_event_id: str
    source_role: SourceRole
    text: str
    occurred_at: datetime
    session_id: str | None = None
    retention_profile: str | None = None


# ---------------------------------------------------------------------------
# Memory types
# ---------------------------------------------------------------------------


class MemoryType(str, enum.Enum):
    """Categories of remembered information."""

    PREFERENCE = "preference"
    FACT = "fact"
    PLAN = "plan"
    RELATIONSHIP = "relationship"
    EVENT = "event"


class MemoryAtom(BaseModel):
    """A single extracted memory unit with full provenance."""

    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    revision: int = 1
    type: MemoryType
    content: str
    source_kind: SourceRole = SourceRole.USER
    evidence_refs: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    superseded_by: str | None = None
    selection_reason: str | None = None


class RecallItem(BaseModel):
    """A memory returned by recall, with full traceability."""

    memory_id: str
    revision: int
    type: MemoryType
    content: str
    source_kind: SourceRole
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    selection_reason: str | None = None


# ---------------------------------------------------------------------------
# Operation results (unified envelope)
# ---------------------------------------------------------------------------


class ErrorCode(str, enum.Enum):
    """Standard error codes across all operations."""

    AUTH_DENIED = "AUTH_DENIED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    SCOPE_INVALID = "SCOPE_INVALID"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    CAPACITY_LIMIT = "CAPACITY_LIMIT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"


class ResultEnvelope(BaseModel):
    """Unified return envelope for all operations."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    success: bool
    outcome: Any = None
    reason_code: ErrorCode | None = None
    reason: str | None = None
    retryable: bool = False
    retry_after: float | None = None  # seconds


# ---------------------------------------------------------------------------
# Operation-specific input/output types
# ---------------------------------------------------------------------------


class ObserveInput(BaseModel):
    context: MemoryContext
    source_event: SourceEvent


class ObserveOutput(BaseModel):
    operation_id: str
    accepted: bool
    reason: str | None = None


class GetOperationInput(BaseModel):
    context: MemoryContext
    operation_id: str


class GetOperationOutput(BaseModel):
    state: ProcessingState
    reason: str | None = None


class RecallInput(BaseModel):
    context: MemoryContext
    query: str | None = None
    memory_type: MemoryType | None = None
    mode: str = "current"  # "current" | "history"
    deadline_seconds: float = 5.0
    item_budget: int = 20
    token_budget: int = 2000
    after_operation_id: str | None = None


class RecallOutput(BaseModel):
    outcome: RecallOutcome
    items: list[RecallItem] = Field(default_factory=list)
    receipt_id: str | None = None
    view_revision: int | None = None
    freshness: datetime | None = None


class CorrectInput(BaseModel):
    context: MemoryContext
    target_memory_id: str
    expected_revision: int
    correction_text: str
    user_correction_event_id: str


class CorrectOutput(BaseModel):
    old_blocked: bool
    new_operation_id: str | None = None
    receipt_id: str | None = None
    conflict: bool = False


class ForgetSelector(BaseModel):
    """Explicit selector for deletion — empty selector = reject, not 'delete all'."""

    memory_ids: list[str] | None = None
    evidence_ids: list[str] | None = None
    scope_wide: bool = False  # only True when authorized for full subject deletion


class ForgetInput(BaseModel):
    context: MemoryContext
    selector: ForgetSelector
    request_id: str


class ForgetOutput(BaseModel):
    status: DeletionStatus
    receipt_id: str | None = None
    reason: str | None = None


class ExplainInput(BaseModel):
    context: MemoryContext
    memory_id: str | None = None
    receipt_id: str | None = None


class ExplainOutput(BaseModel):
    memory_id: str
    source_chain: list[dict[str, Any]]
    revision_history: list[dict[str, Any]]
    authorization_version: int | None = None
    selection_reason: str | None = None


class ExportInput(BaseModel):
    context: MemoryContext
    scope: str = "all"  # "all" | "evidence" | "memories"
    request_id: str


class ExportOutput(BaseModel):
    status: str  # "pending" | "ready" | "failed"
    download_url: str | None = None
    expires_at: datetime | None = None


class SyncAuthorizationInput(BaseModel):
    event_type: str  # "grant" | "revoke" | "update"
    snapshot: AuthorizationSnapshot


class SyncAuthorizationOutput(BaseModel):
    applied_version: int
    ack: bool
