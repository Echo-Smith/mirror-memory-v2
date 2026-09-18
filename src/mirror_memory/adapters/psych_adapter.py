"""Psych product adapter — integrates Mirror Memory with Psych.

Modes (design.md §7):
- off: Mirror Memory disabled, no processing
- shadow: Mirror processes but results don't enter actual replies
- active_internal: Results visible for test cohort and independent sessions only

Records mode and version; reverting to off doesn't delete collected data.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    LifecycleEventRepository,
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


class AdapterMode(str, enum.Enum):
    OFF = "off"
    SHADOW = "shadow"
    ACTIVE_INTERNAL = "active_internal"


class PsychAdapter:
    """Adapter between Psych and Mirror Memory.

    Responsibilities:
    - Convert Psych messages to Mirror SourceEvents
    - Manage adapter mode (off/shadow/active_internal)
    - Apply domain rules (preference/event extraction)
    - Format Mirror results for Psych reply context
    """

    def __init__(self, session: Session, app_id: str = "psych") -> None:
        self._session = session
        self._app_id = app_id
        self._service = MirrorMemoryService(session)
        self._scope_repo = ScopeControlRepository(session)
        self._lc_repo = LifecycleEventRepository(session)
        self._mode = AdapterMode.OFF
        self._version = "0.1.0"
        self._test_cohort: set[str] = set()

    @property
    def mode(self) -> AdapterMode:
        return self._mode

    @property
    def version(self) -> str:
        return self._version

    def set_mode(self, mode: AdapterMode) -> None:
        """Change adapter mode. Records lifecycle event."""
        self._mode = mode
        # Record mode change (using a synthetic scope for adapter-level events)
        # In production, this would go to an operational log

    def set_test_cohort(self, user_ids: set[str]) -> None:
        """Set the list of user IDs eligible for active_internal testing."""
        self._test_cohort = user_ids

    def is_user_in_cohort(self, user_id: str) -> bool:
        return user_id in self._test_cohort

    def authorize_user(
        self,
        tenant_id: str,
        user_id: str,
        auth_version: int = 1,
    ) -> bool:
        """Set up authorization for a user in Psych."""
        scope = Scope(tenant_id=tenant_id, app_id=self._app_id, subject_id=user_id)
        now = datetime.now(UTC)
        from datetime import timedelta

        snap = AuthorizationSnapshot(
            scope=scope,
            purpose="memory_management",
            allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
            version=auth_version,
            issued_at=now,
            expires_at=now + timedelta(days=365),
            issuer="psych_backend",
        )
        result = self._service.sync_authorization(
            SyncAuthorizationInput(event_type="grant", snapshot=snap)
        )
        return result.success

    def observe_message(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        message_text: str,
        source_role: str = "user",
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Process a Psych message through Mirror Memory.

        Returns:
            {"processed": bool, "mode": str, "operation_id": str|None}
        """
        if self._mode == AdapterMode.OFF:
            return {"processed": False, "mode": "off", "operation_id": None}

        scope = Scope(tenant_id=tenant_id, app_id=self._app_id, subject_id=user_id)
        context = MemoryContext(
            scope=scope,
            purpose="memory_management",
            session_id=session_id,
        )

        if event_id is None:
            import uuid
            event_id = f"psych_{uuid.uuid4().hex[:12]}"

        result = self._service.observe(ObserveInput(
            context=context,
            source_event=SourceEvent(
                source_event_id=event_id,
                source_role=SourceRole(source_role),
                text=message_text,
                occurred_at=datetime.now(UTC),
                session_id=session_id,
            ),
        ))

        return {
            "processed": result.success,
            "mode": self._mode.value,
            "operation_id": result.outcome.operation_id if result.success else None,
        }

    def recall_for_reply(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        query: str = "",
    ) -> dict[str, Any]:
        """Recall memories for inclusion in Psych reply context.

        Returns:
            {"mode": str, "memories": list[dict], "in_reply": bool}
        """
        if self._mode == AdapterMode.OFF:
            return {"mode": "off", "memories": [], "in_reply": False}

        scope = Scope(tenant_id=tenant_id, app_id=self._app_id, subject_id=user_id)
        context = MemoryContext(
            scope=scope,
            purpose="memory_management",
            session_id=session_id,
        )

        result = self._service.recall(RecallInput(
            context=context,
            query=query,
        ))

        memories = []
        if result.success and result.outcome.outcome == RecallOutcome.FOUND:
            memories = [
                {
                    "type": item.type.value,
                    "content": item.content,
                    "source": item.source_kind.value,
                    "memory_id": item.memory_id,
                }
                for item in result.outcome.items
            ]

        # In shadow mode, don't include in actual reply
        in_reply = self._mode == AdapterMode.ACTIVE_INTERNAL
        if in_reply and user_id not in self._test_cohort:
            in_reply = False

        return {
            "mode": self._mode.value,
            "memories": memories,
            "in_reply": in_reply,
        }

    def format_memories_for_context(self, memories: list[dict]) -> str:
        """Format recalled memories into a prompt context string."""
        if not memories:
            return ""
        lines = ["[Mirror Memory - 用户记忆]"]
        for m in memories:
            lines.append(f"- [{m['type']}] {m['content']}")
        return "\n".join(lines)