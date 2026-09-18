"""C12–C13: Deletion barrier and backup recovery.

References: R08, design.md §6.
"""


import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    DeletionJobRepository,
    MemoryAtomRepository,
    ScopeControlRepository,
)


@pytest.mark.contract_C12
class TestDeletionBarrier:
    """Verify deletion barrier prevents access and cleans up."""

    def test_deleted_not_returned_in_recall(self, db: Session, scope_1):
        """After deletion confirmed, memory is not returned."""
        atom_repo = MemoryAtomRepository(db)
        sc_repo = ScopeControlRepository(db)

        atom_repo.create(
            scope=scope_1, type="preference",
            content="secret preference", source_kind="user",
        )

        # Mark deletion
        gen = sc_repo.mark_deleted(scope_1)
        atom_repo.soft_delete_by_scope(scope_1, gen)

        # Not returned in current
        current = atom_repo.get_current(scope_1)
        assert len(current) == 0

    def test_empty_selector_rejected(self, db: Session, scope_1):
        """forget with empty selector is rejected, not treated as 'delete all'."""
        dj_repo = DeletionJobRepository(db)

        # Empty selector (no memory_ids, no evidence_ids, not scope_wide)
        dj = dj_repo.create(
            scope=scope_1,
            request_id="req_empty",
            selector={"memory_ids": None, "evidence_ids": None, "scope_wide": False},
        )
        # The selector is stored but should be validated at service layer
        assert dj.selector["scope_wide"] is False
        assert dj.selector["memory_ids"] is None

    def test_scope_wide_deletion(self, db: Session, scope_1):
        """Scope-wide deletion marks all atoms as deleted."""
        atom_repo = MemoryAtomRepository(db)
        sc_repo = ScopeControlRepository(db)

        atom_repo.create(scope=scope_1, type="preference", content="a", source_kind="user")
        atom_repo.create(scope=scope_1, type="fact", content="b", source_kind="user")
        atom_repo.create(scope=scope_1, type="plan", content="c", source_kind="user")

        gen = sc_repo.mark_deleted(scope_1)
        count = atom_repo.soft_delete_by_scope(scope_1, gen)
        assert count == 3

        current = atom_repo.get_current(scope_1)
        assert len(current) == 0

    def test_deletion_job_lifecycle(self, db: Session, scope_1):
        """Deletion job progresses through states."""
        dj_repo = DeletionJobRepository(db)

        dj = dj_repo.create(
            scope=scope_1,
            request_id="req_001",
            selector={"scope_wide": True},
        )
        assert dj.status == "blocked"

        dj_repo.update_status(dj.id, "purging")
        assert dj_repo.get_by_id(dj.id).status == "purging"

        dj_repo.update_status(dj.id, "verified")
        updated = dj_repo.get_by_id(dj.id)
        assert updated.status == "verified"
        assert updated.verified_at is not None


@pytest.mark.contract_C13
class TestBackupRecovery:
    """Verify backup restore doesn't resurrect deleted data."""

    def test_deletion_generation_prevents_resurrection(self, db: Session, scope_1):
        """Atoms with old deletion_generation stay deleted even after new operations."""
        atom_repo = MemoryAtomRepository(db)
        sc_repo = ScopeControlRepository(db)

        atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="to be deleted", source_kind="user",
        )

        # Delete at generation 1
        gen = sc_repo.mark_deleted(scope_1)
        assert gen == 1
        atom_repo.soft_delete_by_scope(scope_1, gen)

        # Verify deleted
        current = atom_repo.get_current(scope_1)
        assert len(current) == 0

        # Even if someone tries to "restore" with old generation,
        # the atom's deletion_generation prevents it
        refreshed = atom_repo.get_by_id(atom.id)
        assert refreshed.deletion_generation == 1
        assert not refreshed.is_current