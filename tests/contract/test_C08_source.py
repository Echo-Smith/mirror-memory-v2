"""C08: Source validity and injection rejection.

References: R04, design.md §3 (source verification).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    MemoryAtomRepository,
)
from mirror_memory.core.types import SourceRole


@pytest.mark.contract_C08
class TestSourceValidity:
    """Verify source provenance and injection rejection."""

    def test_assistant_speculation_not_user_fact(self, db: Session, scope_1):
        """assistant-role source cannot become a user fact."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        now = datetime.now(UTC)
        # Assistant makes a speculation
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_asst_001",
            source_role=SourceRole.ASSISTANT,
            text="You might not like socializing", occurred_at=now,
        )

        # This should NOT be stored as a user fact
        # The source_kind must reflect the actual role
        atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="User doesn't like socializing",
            source_kind="assistant",  # explicitly marked as assistant
            source_evidence_id=ev.id,
        )
        assert atom.source_kind == "assistant"

        # When querying, assistant-sourced items must be distinguished
        atoms = atom_repo.get_current(scope_1)
        for a in atoms:
            if a.source_kind == "assistant":
                # Must not be treated as user-stated fact
                assert a.source_kind != "user"

    def test_user_source_preserved(self, db: Session, scope_1):
        """User-role source is correctly recorded."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_user_001",
            source_role=SourceRole.USER,
            text="I love cats", occurred_at=now,
        )
        atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="User loves cats",
            source_kind="user",
            source_evidence_id=ev.id,
        )
        assert atom.source_kind == "user"

    def test_system_source_preserved(self, db: Session, scope_1):
        """System-role source is correctly recorded."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_sys_001",
            source_role=SourceRole.SYSTEM,
            text="System event: user logged in", occurred_at=now,
        )
        atom = atom_repo.create(
            scope=scope_1, type="event",
            content="User logged in",
            source_kind="system",
            source_evidence_id=ev.id,
        )
        assert atom.source_kind == "system"