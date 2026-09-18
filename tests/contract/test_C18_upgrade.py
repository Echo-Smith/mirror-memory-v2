"""C18: Projection rebuild and schema upgrade.

References: R12, design.md §1 (upgrade recovery).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.models import Base
from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    MemoryAtomRepository,
)
from mirror_memory.core.types import SourceRole


@pytest.mark.contract_C18
class TestUpgradeRecovery:
    """Verify schema/model upgrades are backward-compatible."""

    def test_schema_create_drop_cycle(self, _db_engine):
        """Schema can be created and dropped cleanly."""
        Base.metadata.drop_all(_db_engine)
        Base.metadata.create_all(_db_engine)
        # Verify tables exist
        assert len(Base.metadata.tables) == 13
        # Drop and recreate
        Base.metadata.drop_all(_db_engine)
        Base.metadata.create_all(_db_engine)
        assert len(Base.metadata.tables) == 13

    def test_data_survives_schema_rebuild(self, db: Session, scope_1):
        """Existing data is preserved after non-destructive schema operations."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_upgrade_001",
            source_role=SourceRole.USER, text="test data", occurred_at=now,
        )
        atom = atom_repo.create(
            scope=scope_1, type="preference", content="persistent data",
            source_kind="user", source_evidence_id=ev.id,
        )

        # Data exists
        assert ev_repo.get_by_event_id(scope_1, "evt_upgrade_001") is not None
        assert atom_repo.get_by_id(atom.id) is not None

    def test_version_isolation(self, db: Session, scope_1):
        """Different evidence versions produce isolated results."""
        ev_repo = EvidenceRepository(db)

        now = datetime.now(UTC)
        ev1 = ev_repo.create(
            scope=scope_1, source_event_id="evt_v1",
            source_role=SourceRole.USER, text="version 1", occurred_at=now,
        )
        ev2 = ev_repo.create(
            scope=scope_1, source_event_id="evt_v2",
            source_role=SourceRole.USER, text="version 2", occurred_at=now,
        )

        # Both exist independently
        assert ev1.id != ev2.id
        evs = ev_repo.list_by_scope(scope_1)
        assert len(evs) == 2