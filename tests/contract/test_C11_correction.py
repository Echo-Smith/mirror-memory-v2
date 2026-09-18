"""C11: Correction semantics.

References: R07, design.md §2 (correct operation).
"""


import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    MemoryAtomRepository,
)


@pytest.mark.contract_C11
class TestCorrection:
    """Verify correction stops old version and handles conflicts."""

    def test_correction_stops_old_current(self, db: Session, scope_1):
        """After correction, old revision is no longer returned in current recall."""
        atom_repo = MemoryAtomRepository(db)

        atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="likes Python", source_kind="user",
        )
        assert atom.is_current

        # Correct: mark old as not current
        success, rev = atom_repo.mark_corrected(atom.id, atom.revision)
        assert success

        # No longer current
        current = atom_repo.get_current(scope_1)
        assert len(current) == 0

    def test_revision_mismatch_requires_reread(self, db: Session, scope_1):
        """Correcting with wrong expected_revision → revision conflict."""
        atom_repo = MemoryAtomRepository(db)

        atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="likes Python", source_kind="user",
        )

        # Wrong revision
        success, actual = atom_repo.mark_corrected(atom.id, 999)
        assert not success
        assert actual == atom.revision

        # Still current
        current = atom_repo.get_current(scope_1)
        assert len(current) == 1

    def test_conflict_on_concurrent_publish(self, db: Session, scope_1):
        """Correcting while worker publishes → explicit conflict, not silent overwrite."""
        atom_repo = MemoryAtomRepository(db)

        atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="old value", source_kind="user",
        )

        # Simulate concurrent update: revision changes
        atom.revision = 2
        db.flush()

        # Correction with stale revision fails
        success, actual = atom_repo.mark_corrected(atom.id, 1)
        assert not success
        assert actual == 2

    def test_new_value_can_be_created_after_correction(self, db: Session, scope_1):
        """After correction, a new atom can replace the old one."""
        atom_repo = MemoryAtomRepository(db)

        old_atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="likes Python", source_kind="user",
        )
        atom_repo.mark_corrected(old_atom.id, old_atom.revision)

        # Create new atom
        new_atom = atom_repo.create(
            scope=scope_1, type="preference",
            content="likes Rust", source_kind="user",
        )
        atom_repo.supersede(old_atom.id, new_atom.id)

        current = atom_repo.get_current(scope_1)
        assert len(current) == 1
        assert current[0].content == "likes Rust"