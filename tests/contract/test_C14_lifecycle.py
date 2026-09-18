"""C14: Lifecycle and self-reinforcement prevention.

References: R09, design.md §3 (lifecycle).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    MemoryAtomRepository,
)
from mirror_memory.core.types import SourceRole


@pytest.mark.contract_C14
class TestLifecycle:
    """Verify lifecycle management prevents self-reinforcement."""

    def test_repeated_source_creates_single_evidence(self, db: Session, scope_1):
        """Same source_event_id sent twice is idempotent (single evidence)."""
        ev_repo = EvidenceRepository(db)

        now = datetime.now(UTC)
        text = "I like cats"

        ev_repo.create(
            scope=scope_1, source_event_id="evt_repeat_001",
            source_role=SourceRole.USER, text=text, occurred_at=now,
        )

        # Idempotency check
        status, existing = ev_repo.check_idempotency(scope_1, "evt_repeat_001", text)
        assert status == "idempotent"
        assert existing is not None

        # Only one evidence record
        evs = ev_repo.list_by_scope(scope_1)
        assert len(evs) == 1

    def test_different_source_events_create_separate_evidence(self, db: Session, scope_1):
        """Different source_event_ids create separate evidence records."""
        ev_repo = EvidenceRepository(db)

        now = datetime.now(UTC)
        ev_repo.create(
            scope=scope_1, source_event_id="evt_diff_001",
            source_role=SourceRole.USER, text="I like cats", occurred_at=now,
        )
        ev_repo.create(
            scope=scope_1, source_event_id="evt_diff_002",
            source_role=SourceRole.USER, text="I like cats", occurred_at=now,
        )

        evs = ev_repo.list_by_scope(scope_1)
        assert len(evs) == 2

    def test_supersession_not_silent_replacement(self, db: Session, scope_1):
        """Superseding an atom preserves both old and new."""
        atom_repo = MemoryAtomRepository(db)

        old = atom_repo.create(
            scope=scope_1, type="preference",
            content="old preference", source_kind="user",
        )
        new = atom_repo.create(
            scope=scope_1, type="preference",
            content="new preference", source_kind="user",
        )
        atom_repo.supersede(old.id, new.id)

        # Current only shows new
        current = atom_repo.get_current(scope_1)
        assert len(current) == 1
        assert current[0].content == "new preference"

        # History shows both
        history = atom_repo.get_history(scope_1)
        assert len(history) == 2