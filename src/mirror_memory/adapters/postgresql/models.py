"""SQLAlchemy ORM models for Mirror Memory v2.

Logical collections: scope_control, evidence, jobs, generation_runs,
atoms, relations, view_revisions, view_heads, lifecycle_events,
receipts, budget_reservations, usage_ledger, deletion_jobs.

Compatible with both PostgreSQL and SQLite (for development/testing).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


from mirror_memory.core.utils import utcnow as _utcnow

# ---------------------------------------------------------------------------
# Scope control — authorization barrier
# ---------------------------------------------------------------------------


class ScopeControl(Base):
    """Per-scope authorization state and deletion barrier.

    References:
    - design.md §4: scope_control saves authorization version and deletion generation
    - design.md §5: authorization handshake and update
    """

    __tablename__ = "scope_control"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    app_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Current authorization state
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auth_purpose: Mapped[str | None] = mapped_column(String(256))
    auth_allowed_operations: Mapped[list | None] = mapped_column(JSON)
    auth_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_issuer: Mapped[str | None] = mapped_column(String(256))

    # Deletion barrier
    deletion_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "app_id", "subject_id", name="uq_scope_identity"),
        Index("ix_scope_tenant_app", "tenant_id", "app_id"),
    )


# ---------------------------------------------------------------------------
# Evidence — received source events
# ---------------------------------------------------------------------------


class Evidence(Base):
    """A received source event, bound to scope.

    References:
    - design.md §3: observe receives Evidence and job in same transaction
    - requirements.md R03: reliable receipt with idempotency
    """

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_role: Mapped[str] = mapped_column(String(32), nullable=False)  # user|assistant|system
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 of normalized text
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(128))
    retention_profile: Mapped[str | None] = mapped_column(String(64))
    deletion_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("scope_id", "source_event_id", name="uq_evidence_scope_event"),
        Index("ix_evidence_scope", "scope_id"),
    )


# ---------------------------------------------------------------------------
# Jobs — processing pipeline
# ---------------------------------------------------------------------------


class Job(Base):
    """A processing job linked to evidence.

    References:
    - design.md §3: pending -> running -> ready (+ failed/cancelled/expired)
    - requirements.md R03: accepted means Evidence + job co-committed
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(64), ForeignKey("evidence.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reason: Mapped[str | None] = mapped_column(String(256))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # Lease / fencing
    lease_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Authorization snapshot at Observe time (immutable after creation)
    purpose: Mapped[str | None] = mapped_column(String(256))
    accepted_auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_deletion_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_jobs_scope_state", "scope_id", "state"),
        Index("ix_jobs_pending", "state", "lease_expires_at"),
    )


# ---------------------------------------------------------------------------
# Generation runs — model dispatch tracking
# ---------------------------------------------------------------------------


class GenerationRun(Base):
    """Tracks a model dispatch attempt for a job.

    References:
    - design.md §4: model network requests execute outside transaction
    """

    __tablename__ = "generation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.id"), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="dispatched")
    model_name: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)

    # Cost tracking
    cost_estimate: Mapped[float | None] = mapped_column()
    tokens_used: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Memory atoms — extracted knowledge units
# ---------------------------------------------------------------------------


class MemoryAtomModel(Base):
    """A single extracted memory unit.

    References:
    - design.md §4: atoms with revision, source, validity
    - requirements.md R04: source provenance
    """

    __tablename__ = "memory_atoms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # preference|fact|plan|relationship|event
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_evidence_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("evidence.id"))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[str | None] = mapped_column(String(64))
    selection_reason: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Deletion coordination
    deletion_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_atoms_scope_current", "scope_id", "is_current"),
        Index("ix_atoms_scope_type", "scope_id", "type"),
    )


# ---------------------------------------------------------------------------
# View heads — current materialized view with optimistic locking
# ---------------------------------------------------------------------------


class ViewHead(Base):
    """Current view revision per scope, with compare-and-swap updates.

    References:
    - design.md §4: head uses expected_revision compare-and-swap
    """

    __tablename__ = "view_heads"

    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ViewRevision(Base):
    """Historical view revision log."""

    __tablename__ = "view_revisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict | None] = mapped_column(JSON)
    caused_by: Mapped[str | None] = mapped_column(String(128))  # job_id or correction_id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("scope_id", "revision", name="uq_view_revision"),
        Index("ix_view_rev_scope", "scope_id", "revision"),
    )


# ---------------------------------------------------------------------------
# Receipts — audit trail
# ---------------------------------------------------------------------------


class Receipt(Base):
    """Operation receipt for audit and explain.

    References:
    - requirements.md R10: explainability with traceable source chain
    """

    __tablename__ = "receipts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_ids: Mapped[list | None] = mapped_column(JSON)  # list of memory/evidence IDs
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_receipts_scope", "scope_id"),)


# ---------------------------------------------------------------------------
# Deletion jobs — explicit deletion tracking
# ---------------------------------------------------------------------------


class DeletionJob(Base):
    """Tracks deletion progress with barrier semantics.

    References:
    - design.md §6: deletion selector, barrier, cleanup verification
    - requirements.md R08: deletion guarantees
    """

    __tablename__ = "deletion_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    selector: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="blocked")
    reason: Mapped[str | None] = mapped_column(String(256))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("scope_id", "request_id", name="uq_deletion_request"),
        Index("ix_deletion_scope", "scope_id"),
    )


# ---------------------------------------------------------------------------
# Budget profiles, reservations, and usage ledger
# ---------------------------------------------------------------------------


class BudgetProfile(Base):
    """Per-app budget configuration.

    Each product has its own profile defining limits.
    Thresholds are ratios of the hard limit:
    - soft_threshold_ratio: warn/delay
    - hard_threshold_ratio: reject
    - recovery_threshold_ratio: resume after cooldown
    """

    __tablename__ = "budget_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Per-scope (per-user) limits
    scope_daily_token_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    scope_daily_cost_limit: Mapped[float] = mapped_column(nullable=False, default=1.0)
    scope_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    # Per-app (product-wide) limits
    app_daily_cost_limit: Mapped[float] = mapped_column(nullable=False, default=100.0)

    # Threshold ratios
    soft_threshold_ratio: Mapped[float] = mapped_column(nullable=False, default=0.8)
    hard_threshold_ratio: Mapped[float] = mapped_column(nullable=False, default=1.0)
    recovery_threshold_ratio: Mapped[float] = mapped_column(nullable=False, default=0.6)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class BudgetReservation(Base):
    """Short-lived reservation for cost/token/call budgets.

    References:
    - requirements.md R11: usage controls with concurrent reservations
    """

    __tablename__ = "budget_reservations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)  # cost|token|call
    amount: Mapped[float] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    settled_amount: Mapped[float | None] = mapped_column()
    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_budget_scope", "scope_id", "resource_type"),
    )


class UsageLedger(Base):
    """Immutable usage records for cost accounting."""

    __tablename__ = "usage_ledger"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[float] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # reservation_id or "direct"
    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("scope_id", "idempotency_key", name="uq_ledger_idempotent"),
        Index("ix_ledger_scope", "scope_id"),
    )


# ---------------------------------------------------------------------------
# Lifecycle events — for audit and explain
# ---------------------------------------------------------------------------


class LifecycleEvent(Base):
    """Records significant lifecycle events (authorize, revoke, delete, correct)."""

    __tablename__ = "lifecycle_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(String(64), ForeignKey("scope_control.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)
    auth_version: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_lifecycle_scope", "scope_id"),)