"""C15: Explain traceability.

References: R10, design.md §2 (ExplainOutput).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    MemoryAtomRepository,
    ReceiptRepository,
)
from mirror_memory.core.types import SourceRole


@pytest.mark.contract_C15
class TestExplain:
    """Verify explain provides traceable provenance."""

    def test_explain_returns_source_chain(self, db: Session, scope_1):
        """explain returns valid source chain and revision history."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)
        receipt_repo = ReceiptRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_explain_001",
            source_role=SourceRole.USER, text="I prefer dark mode",
            occurred_at=now,
        )
        atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="User prefers dark mode",
            source_kind="user", source_evidence_id=ev.id,
        )

        # Create receipt
        receipt = receipt_repo.create(
            scope=scope_1, operation_type="observe",
            auth_version=1, target_ids=[ev.id],
            detail={"memory_id": atom.id},
        )

        # Explain: trace back to source
        stored_atom = atom_repo.get_by_id(atom.id)
        assert stored_atom.source_evidence_id == ev.id

        stored_ev = ev_repo.get_by_id(ev.id)
        assert stored_ev.source_role == "user"
        assert stored_ev.text == "I prefer dark mode"

        # Receipt links everything
        stored_receipt = receipt_repo.get_by_id(receipt.id)
        assert ev.id in stored_receipt.target_ids

    def test_explain_after_delete_no_content_leak(self, db: Session, scope_1):
        """After deletion, the atom's content is marked as deleted."""
        ev_repo = EvidenceRepository(db)
        atom_repo = MemoryAtomRepository(db)
        sc_repo = __import__(
            "mirror_memory.adapters.postgresql.repositories",
            fromlist=["ScopeControlRepository"],
        ).ScopeControlRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_del_explain_001",
            source_role=SourceRole.USER, text="secret", occurred_at=now,
        )
        atom = atom_repo.create(
            scope=scope_1, type="fact",
            content="secret fact", source_kind="user",
            source_evidence_id=ev.id,
        )

        # Delete
        gen = sc_repo.mark_deleted(scope_1)
        atom_repo.soft_delete_by_scope(scope_1, gen)

        # Atom still exists in DB but is_current=False
        stored = atom_repo.get_by_id(atom.id)
        assert stored is not None
        assert not stored.is_current
        assert stored.deletion_generation == gen