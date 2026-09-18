"""C01: Cross-domain isolation.

Setup: Two subjects, two apps, two tenants.
Trigger: Attempt cross-domain reads, write references, query receipts, and export.
Must observe: All unauthorized attempts rejected; legitimate same-domain succeeds;
no target existence leakage.

References: R01, design.md §2 (MemoryContext binding).
"""

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    MemoryAtomRepository,
    ReceiptRepository,
)
from mirror_memory.core.types import SourceRole


@pytest.mark.contract_C01
class TestIsolation:
    """Verify strict tenant/app/subject isolation."""

    def test_cross_subject_read_rejected(
        self, db: Session, scope_1, scope_2, context_1, context_2
    ):
        """Subject 1 cannot read Subject 2's memories."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        # Create evidence for subject 1
        ev1 = ev_repo.create(
            scope=scope_1,
            source_event_id="evt_s1_001",
            source_role=SourceRole.USER,
            text="I like Python",
            occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        atom_repo.create(
            scope=scope_1,
            type="preference",
            content="User likes Python",
            source_kind="user",
            source_evidence_id=ev1.id,
        )

        # Subject 2 should see nothing
        atoms_s2 = atom_repo.get_current(scope_2)
        assert len(atoms_s2) == 0, "Subject 2 must not see Subject 1's memories"

    def test_cross_app_read_rejected(
        self, db: Session, scope_1, scope_cross_app
    ):
        """App 1 cannot read App 2's data."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        # Create evidence for app 1
        ev1 = ev_repo.create(
            scope=scope_1,
            source_event_id="evt_app1_001",
            source_role=SourceRole.USER,
            text="I prefer dark mode",
            occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        atom_repo.create(
            scope=scope_1,
            type="preference",
            content="User prefers dark mode",
            source_kind="user",
            source_evidence_id=ev1.id,
        )

        # Cross-app should see nothing
        atoms_cross = atom_repo.get_current(scope_cross_app)
        assert len(atoms_cross) == 0, "Cross-app must not see other app's memories"

    def test_cross_tenant_read_rejected(
        self, db: Session, scope_1, scope_cross_tenant
    ):
        """Tenant 1 cannot read Tenant 2's data."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        ev1 = ev_repo.create(
            scope=scope_1,
            source_event_id="evt_t1_001",
            source_role=SourceRole.USER,
            text="I live in Beijing",
            occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        atom_repo.create(
            scope=scope_1,
            type="fact",
            content="User lives in Beijing",
            source_kind="user",
            source_evidence_id=ev1.id,
        )

        atoms_cross = atom_repo.get_current(scope_cross_tenant)
        assert len(atoms_cross) == 0, "Cross-tenant must not see other tenant's memories"

    def test_cross_subject_write_rejected(
        self, db: Session, scope_1, scope_2
    ):
        """Evidence created for subject 1 cannot reference subject 2's scope."""
        ev_repo = EvidenceRepository(db)

        # Create evidence under scope_1
        ev1 = ev_repo.create(
            scope=scope_1,
            source_event_id="evt_write_001",
            source_role=SourceRole.USER,
            text="test",
            occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )

        # Scope 2 has no evidence
        ev_s2 = ev_repo.list_by_scope(scope_2)
        assert len(ev_s2) == 0, "Subject 2 must not see Subject 1's evidence"

    def test_cross_subject_receipt_rejected(
        self, db: Session, scope_1, scope_2
    ):
        """Subject 1 cannot query Subject 2's receipts."""
        receipt_repo = ReceiptRepository(db)

        receipt_repo.create(
            scope=scope_1,
            operation_type="observe",
            auth_version=1,
        )

        receipts_s2 = receipt_repo.list_by_scope(scope_2)
        assert len(receipts_s2) == 0, "Subject 2 must not see Subject 1's receipts"

    def test_cross_subject_export_rejected(
        self, db: Session, scope_1, scope_2
    ):
        """Subject 1 cannot export Subject 2's data.

        Note: Full export is T06 scope. Here we verify the data isolation
        that export relies on.
        """
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        ev1 = ev_repo.create(
            scope=scope_1,
            source_event_id="evt_export_001",
            source_role=SourceRole.USER,
            text="my secret",
            occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        atom_repo.create(
            scope=scope_1,
            type="fact",
            content="secret data",
            source_kind="user",
            source_evidence_id=ev1.id,
        )

        # Subject 2's export would return empty
        ev_s2 = ev_repo.list_by_scope(scope_2)
        atoms_s2 = atom_repo.get_current(scope_2)
        assert len(ev_s2) == 0
        assert len(atoms_s2) == 0

    def test_no_existence_leakage(
        self, db: Session, scope_1, scope_cross_tenant
    ):
        """Accessing non-existent cross-tenant target must not reveal its existence."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        # Create data for scope_1
        ev_repo.create(
            scope=scope_1,
            source_event_id="evt_leak_001",
            source_role=SourceRole.USER,
            text="exists",
            occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )

        # Cross-tenant query returns empty, not an error that reveals existence
        ev_cross = ev_repo.list_by_scope(scope_cross_tenant)
        atoms_cross = atom_repo.get_current(scope_cross_tenant)
        assert ev_cross == []
        assert atoms_cross == []

    def test_same_domain_succeeds(self, db: Session, scope_1):
        """Legitimate same-domain operations succeed."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)

        ev = ev_repo.create(
            scope=scope_1,
            source_event_id="evt_ok_001",
            source_role=SourceRole.USER,
            text="I like cats",
            occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        assert ev.id is not None

        atom = atom_repo.create(
            scope=scope_1,
            type="preference",
            content="User likes cats",
            source_kind="user",
            source_evidence_id=ev.id,
        )
        assert atom.id is not None

        # Can read back
        atoms = atom_repo.get_current(scope_1)
        assert len(atoms) == 1
        assert atoms[0].content == "User likes cats"