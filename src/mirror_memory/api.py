"""FastAPI HTTP API layer for Mirror Memory v2.

Exposes the 8 Mirror Memory operations as HTTP endpoints.
M1: In-process API (no external HTTP server required for testing).
M2+: Deploy behind a reverse proxy with authentication.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mirror_memory.adapters.postgresql.models import Base
from mirror_memory.application.memory_service import MirrorMemoryService
from mirror_memory.core.types import (
    AuthorizationSnapshot,
    CorrectInput,
    ExplainInput,
    ForgetInput,
    ForgetSelector,
    GetOperationInput,
    MemoryContext,
    ObserveInput,
    RecallInput,
    Scope,
    SourceEvent,
    SourceRole,
    SyncAuthorizationInput,
)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

_engine = None
_session_factory = None


def init_db(url: str | None = None) -> None:
    """Initialize database engine and create tables."""
    global _engine, _session_factory
    db_url = url or os.environ.get("DATABASE_URL", "sqlite:///./mirror_memory.db")
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    _engine = create_engine(db_url, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)


def get_session():
    """Get a new database session."""
    if _session_factory is None:
        init_db()
    return _session_factory()  # type: ignore[misc]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    if _engine:
        _engine.dispose()


app = FastAPI(
    title="Mirror Memory v2",
    description="Long-term memory infrastructure for AI conversational products",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class ScopeModel(BaseModel):
    tenant_id: str
    app_id: str
    subject_id: str


class AuthGrantRequest(BaseModel):
    scope: ScopeModel
    version: int = 1
    operations: list[str] = Field(default_factory=lambda: ["observe", "recall", "correct", "forget", "explain", "export"])
    purpose: str = "memory_management"
    expires_hours: float = 24.0


class ObserveRequest(BaseModel):
    scope: ScopeModel
    event_id: str
    text: str
    role: str = "user"
    session_id: str | None = None
    purpose: str = "memory_management"


class RecallRequest(BaseModel):
    scope: ScopeModel
    query: str = ""
    mode: str = "current"
    memory_type: str | None = None
    session_id: str | None = None
    purpose: str = "memory_management"


class CorrectRequest(BaseModel):
    scope: ScopeModel
    memory_id: str
    expected_revision: int
    correction_text: str
    event_id: str | None = None
    purpose: str = "memory_management"


class ForgetRequest(BaseModel):
    scope: ScopeModel
    memory_ids: list[str] | None = None
    evidence_ids: list[str] | None = None
    scope_wide: bool = False
    request_id: str | None = None
    purpose: str = "memory_management"


class ExplainRequest(BaseModel):
    scope: ScopeModel
    memory_id: str
    purpose: str = "memory_management"


class ExportRequest(BaseModel):
    scope: ScopeModel
    scope_type: str = "all"
    request_id: str | None = None
    purpose: str = "memory_management"


class OperationStatusRequest(BaseModel):
    scope: ScopeModel
    operation_id: str
    purpose: str = "memory_management"


class Envelope(BaseModel):
    success: bool
    outcome: Any = None
    reason: str | None = None
    reason_code: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(m: ScopeModel) -> Scope:
    return Scope(tenant_id=m.tenant_id, app_id=m.app_id, subject_id=m.subject_id)


def _ctx(scope: Scope, purpose: str, session_id: str | None = None) -> MemoryContext:
    return MemoryContext(scope=scope, purpose=purpose, session_id=session_id)


def _wrap(result) -> Envelope:
    return Envelope(
        success=result.success,
        outcome=result.outcome.model_dump() if hasattr(result.outcome, "model_dump") else result.outcome,
        reason=result.reason,
        reason_code=result.reason_code.value if result.reason_code else None,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/v1/auth/grant", response_model=Envelope)
def auth_grant(req: AuthGrantRequest):
    """Grant or update authorization for a scope."""
    session = get_session()
    try:
        svc = MirrorMemoryService(session)
        now = datetime.now(UTC)
        snap = AuthorizationSnapshot(
            scope=_scope(req.scope),
            purpose=req.purpose,
            allowed_operations=req.operations,
            version=req.version,
            issued_at=now,
            expires_at=now + timedelta(hours=req.expires_hours),
            issuer="http_api",
        )
        result = svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=snap))
        session.commit()
        return _wrap(result)
    finally:
        session.close()


@app.post("/v1/auth/revoke", response_model=Envelope)
def auth_revoke(req: AuthGrantRequest):
    """Revoke authorization for a scope."""
    session = get_session()
    try:
        svc = MirrorMemoryService(session)
        now = datetime.now(UTC)
        snap = AuthorizationSnapshot(
            scope=_scope(req.scope),
            purpose=req.purpose,
            allowed_operations=[],
            version=req.version,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            issuer="http_api",
        )
        result = svc.sync_authorization(SyncAuthorizationInput(event_type="revoke", snapshot=snap))
        session.commit()
        return _wrap(result)
    finally:
        session.close()


@app.post("/v1/observe", response_model=Envelope)
def observe(req: ObserveRequest):
    """Accept a user event for memory processing."""
    session = get_session()
    try:
        svc = MirrorMemoryService(session)
        result = svc.observe(ObserveInput(
            context=_ctx(_scope(req.scope), req.purpose, req.session_id),
            source_event=SourceEvent(
                source_event_id=req.event_id,
                source_role=SourceRole(req.role),
                text=req.text,
                occurred_at=datetime.now(UTC),
                session_id=req.session_id,
            ),
        ))
        session.commit()
        return _wrap(result)
    finally:
        session.close()


@app.post("/v1/recall", response_model=Envelope)
def recall(req: RecallRequest):
    """Recall memories for a scope."""
    session = get_session()
    try:
        svc = MirrorMemoryService(session)
        result = svc.recall(RecallInput(
            context=_ctx(_scope(req.scope), req.purpose, req.session_id),
            query=req.query,
            mode=req.mode,
        ))
        return _wrap(result)
    finally:
        session.close()


@app.post("/v1/correct", response_model=Envelope)
def correct(req: CorrectRequest):
    """Correct a memory atom."""
    session = get_session()
    try:
        svc = MirrorMemoryService(session)
        result = svc.correct(CorrectInput(
            context=_ctx(_scope(req.scope), req.purpose),
            target_memory_id=req.memory_id,
            expected_revision=req.expected_revision,
            correction_text=req.correction_text,
            user_correction_event_id=req.event_id or f"http_correct_{req.memory_id}",
        ))
        session.commit()
        return _wrap(result)
    finally:
        session.close()


@app.post("/v1/forget", response_model=Envelope)
def forget(req: ForgetRequest):
    """Delete memories."""
    session = get_session()
    try:
        svc = MirrorMemoryService(session)
        result = svc.forget(ForgetInput(
            context=_ctx(_scope(req.scope), req.purpose),
            selector=ForgetSelector(
                memory_ids=req.memory_ids,
                evidence_ids=req.evidence_ids,
                scope_wide=req.scope_wide,
            ),
            request_id=req.request_id or f"http_forget_{datetime.now(UTC).timestamp()}",
        ))
        session.commit()
        return _wrap(result)
    finally:
        session.close()


@app.post("/v1/explain", response_model=Envelope)
def explain(req: ExplainRequest):
    """Explain a memory's source chain."""
    session = get_session()
    try:
        svc = MirrorMemoryService(session)
        result = svc.explain(ExplainInput(
            context=_ctx(_scope(req.scope), req.purpose),
            memory_id=req.memory_id,
        ))
        return _wrap(result)
    finally:
        session.close()


@app.post("/v1/export", response_model=Envelope)
def export_data(req: ExportRequest):
    """Export user data."""
    session = get_session()
    try:
        from mirror_memory.core.types import ExportInput
        svc = MirrorMemoryService(session)
        result = svc.export(ExportInput(
            context=_ctx(_scope(req.scope), req.purpose),
            scope=req.scope_type,
            request_id=req.request_id or f"http_export_{datetime.now(UTC).timestamp()}",
        ))
        session.commit()
        return _wrap(result)
    finally:
        session.close()


@app.post("/v1/operation", response_model=Envelope)
def get_operation(req: OperationStatusRequest):
    """Query operation status."""
    session = get_session()
    try:
        svc = MirrorMemoryService(session)
        result = svc.get_operation(GetOperationInput(
            context=_ctx(_scope(req.scope), req.purpose),
            operation_id=req.operation_id,
        ))
        return _wrap(result)
    finally:
        session.close()


@app.get("/v1/health")
def health():
    """Health check."""
    return {"status": "ok", "version": "0.1.0"}