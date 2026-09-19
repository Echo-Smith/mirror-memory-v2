"""Mirror Memory Client adapter for memory-benchmarks.

Implements the same async interface as Mem0Client (add/search/delete_user)
so the LoCoMo/LongMemEval/BEAM benchmarks can use Mirror Memory as a backend.

Uses the synchronous Mirror Memory service layer wrapped in asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mirror_memory.adapters.postgresql.models import Base
from mirror_memory.adapters.postgresql.repositories import (
    JobRepository,
    MemoryAtomRepository,
    ScopeControlRepository,
)
from mirror_memory.application.memory_service import MirrorMemoryService
from mirror_memory.core.types import (
    AuthorizationSnapshot,
    ForgetInput,
    ForgetSelector,
    MemoryContext,
    MemoryType,
    ObserveInput,
    RecallInput,
    RecallOutcome,
    Scope,
    SourceEvent,
    SourceRole,
    SyncAuthorizationInput,
)
from mirror_memory.runtime.worker import JobWorker, _factory_from_session

logger = logging.getLogger(__name__)


class MirrorMemoryClient:
    """Async adapter for Mirror Memory matching the Mem0Client interface.

    Args:
        db_url: SQLAlchemy database URL.
        app_id: Application identifier for Mirror Memory scopes.
    """

    def __init__(self, db_url: str = "sqlite:///./benchmark_mirror.db", app_id: str = "locomo_bench"):
        self._db_url = db_url
        self._app_id = app_id
        self._engine = None
        self._session_factory = None
        self._initialized = False

    def _ensure_init(self) -> None:
        """Lazy initialization of database engine and schema."""
        if self._initialized:
            return
        connect_args = {}
        if self._db_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        self._engine = create_engine(self._db_url, connect_args=connect_args)
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
        self._initialized = True

    def _get_session(self):
        self._ensure_init()
        return self._session_factory()

    async def close(self) -> None:
        if self._engine:
            self._engine.dispose()

    async def __aenter__(self) -> MirrorMemoryClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # =========================================================================
    # Add (observe + process)
    # =========================================================================

    async def add(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        observation_date: str | None = None,
        timestamp: int | None = None,
        custom_instructions: str | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        """Add memories from a conversation turn.

        Converts messages to SourceEvents and processes through Mirror Memory.
        Returns dict with "results" key matching Mem0 format.
        """
        return await asyncio.to_thread(
            self._add_sync, messages, user_id, timestamp,
        )

    def _add_sync(self, messages: list[dict[str, str]], user_id: str, timestamp: int | None) -> dict | None:
        session = self._get_session()
        try:
            svc = MirrorMemoryService(session)
            scope = Scope(tenant_id="bench", app_id=self._app_id, subject_id=user_id)
            ctx = MemoryContext(scope=scope, purpose="memory_management")

            # Ensure authorization
            self._ensure_auth(session, scope)

            results = []
            for msg in messages:
                content = msg.get("content", "").strip()
                if not content:
                    continue

                role = msg.get("role", "user")
                source_role = SourceRole.ASSISTANT if role == "assistant" else SourceRole.USER

                event_id = f"bench_{uuid.uuid4().hex[:12]}"
                occurred_at = datetime.fromtimestamp(timestamp, tz=UTC) if timestamp else datetime.now(UTC)

                observe_result = svc.observe(ObserveInput(
                    context=ctx,
                    source_event=SourceEvent(
                        source_event_id=event_id,
                        source_role=source_role,
                        text=content,
                        occurred_at=occurred_at,
                    ),
                ))

                if observe_result.success:
                    # Process through worker
                    worker = JobWorker(_factory_from_session(session))
                    worker_result = worker.process(scope, observe_result.outcome.operation_id, "bench_worker")
                    results.append({
                        "event": "ADD",
                        "memory": content[:200],
                        "id": observe_result.outcome.operation_id,
                    })

            session.commit()
            return {"results": results}

        except Exception as e:
            logger.warning("ADD failed for user %s: %s", user_id, e)
            session.rollback()
            return None
        finally:
            session.close()

    # =========================================================================
    # Search (recall)
    # =========================================================================

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 200,
        rerank: bool = False,
        score_debug: bool = False,
    ) -> list[dict]:
        """Search memories. Returns list of results sorted by relevance."""
        return await asyncio.to_thread(self._search_sync, query, user_id, top_k)

    def _search_sync(self, query: str, user_id: str, top_k: int) -> list[dict]:
        session = self._get_session()
        try:
            svc = MirrorMemoryService(session)
            scope = Scope(tenant_id="bench", app_id=self._app_id, subject_id=user_id)
            ctx = MemoryContext(scope=scope, purpose="memory_management")

            result = svc.recall(RecallInput(
                context=ctx,
                query=query,
                mode="current",
                item_budget=top_k,
            ))

            if not result.success or result.outcome.outcome != RecallOutcome.FOUND:
                return []

            formatted = []
            for i, item in enumerate(result.outcome.items):
                formatted.append({
                    "memory": item.content,
                    "score": 1.0 - (i * 0.01),  # Mirror doesn't have scores, use position
                    "id": item.memory_id,
                })

            return formatted

        except Exception as e:
            logger.warning("SEARCH failed for user %s: %s", user_id, e)
            return []
        finally:
            session.close()

    # =========================================================================
    # Delete
    # =========================================================================

    async def delete_user(self, user_id: str) -> bool:
        """Delete all memories for a user."""
        return await asyncio.to_thread(self._delete_user_sync, user_id)

    def _delete_user_sync(self, user_id: str) -> bool:
        session = self._get_session()
        try:
            svc = MirrorMemoryService(session)
            scope = Scope(tenant_id="bench", app_id=self._app_id, subject_id=user_id)
            ctx = MemoryContext(scope=scope, purpose="memory_management")

            result = svc.forget(ForgetInput(
                context=ctx,
                selector=ForgetSelector(scope_wide=True),
                request_id=f"bench_delete_{uuid.uuid4().hex[:8]}",
            ))
            session.commit()
            return result.success

        except Exception as e:
            logger.warning("DELETE failed for user %s: %s", user_id, e)
            session.rollback()
            return False
        finally:
            session.close()

    # =========================================================================
    # Internal: authorization
    # =========================================================================

    def _ensure_auth(self, session: Session, scope: Scope) -> None:
        """Ensure authorization exists for a scope."""
        sc_repo = ScopeControlRepository(session)
        sc = sc_repo.get(scope)
        if sc and sc.auth_version > 0:
            return

        now = datetime.now(UTC)
        auth = AuthorizationSnapshot(
            scope=scope,
            purpose="memory_management",
            allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
            version=1,
            issued_at=now,
            expires_at=now + timedelta(days=365),
            issuer="benchmark",
        )
        svc = MirrorMemoryService(session)
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=auth))