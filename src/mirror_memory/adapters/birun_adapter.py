"""筆潤智談 (Bi Run Zhi Tan) product adapter.

Second product integration for cross-product reuse validation (R15).
Uses independent app_id and data; cannot access Psych data.

Domain rules:
- Language preference extraction
- Format preference extraction
- Task preference extraction
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    ScopeControlRepository,
)
from mirror_memory.application.memory_service import MirrorMemoryService
from mirror_memory.core.types import (
    AuthorizationSnapshot,
    MemoryContext,
    ObserveInput,
    RecallInput,
    RecallOutcome,
    Scope,
    SourceEvent,
    SourceRole,
    SyncAuthorizationInput,
)


class BiRunAdapter:
    """Adapter for 筆潤智談 product.

    Uses app_id="bi_run_zhi_tan" — fully isolated from Psych.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._app_id = "bi_run_zhi_tan"
        self._service = MirrorMemoryService(session)
        self._scope_repo = ScopeControlRepository(session)

    @property
    def app_id(self) -> str:
        return self._app_id

    def authorize_user(self, tenant_id: str, user_id: str, version: int = 1) -> bool:
        scope = Scope(tenant_id=tenant_id, app_id=self._app_id, subject_id=user_id)
        now = datetime.now(UTC)
        snap = AuthorizationSnapshot(
            scope=scope, purpose="memory_management",
            allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
            version=version, issued_at=now, expires_at=now + timedelta(days=365), issuer="birun_backend",
        )
        return self._service.sync_authorization(
            SyncAuthorizationInput(event_type="grant", snapshot=snap)
        ).success

    def observe_message(
        self, tenant_id: str, user_id: str, session_id: str, text: str,
    ) -> dict[str, Any]:
        scope = Scope(tenant_id=tenant_id, app_id=self._app_id, subject_id=user_id)
        ctx = MemoryContext(scope=scope, purpose="memory_management", session_id=session_id)

        import uuid
        result = self._service.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id=f"birun_{uuid.uuid4().hex[:12]}",
                source_role=SourceRole.USER, text=text,
                occurred_at=datetime.now(UTC), session_id=session_id,
            ),
        ))
        return {"success": result.success, "operation_id": result.outcome.operation_id if result.success else None}

    def recall(
        self, tenant_id: str, user_id: str, session_id: str, query: str = "",
    ) -> dict[str, Any]:
        scope = Scope(tenant_id=tenant_id, app_id=self._app_id, subject_id=user_id)
        ctx = MemoryContext(scope=scope, purpose="memory_management", session_id=session_id)

        result = self._service.recall(RecallInput(context=ctx, query=query))
        memories = []
        if result.success and result.outcome.outcome == RecallOutcome.FOUND:
            memories = [
                {"type": i.type.value, "content": i.content, "source": i.source_kind.value}
                for i in result.outcome.items
            ]
        return {"success": result.success, "memories": memories}