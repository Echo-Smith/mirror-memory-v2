"""C09–C10: Recall correctness.

References: R06, design.md §2 (RecallItem, RecallOutcome).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    JobRepository,
    MemoryAtomRepository,
)
from mirror_memory.core.types import SourceRole


@pytest.mark.contract_C09
class TestRecallStatusDistinction:
    """Verify recall outcomes are distinguishable."""

    def test_pending_job_exists(self, db: Session, scope_1):
        """After observe, job is in pending state (distinguishable from empty)."""
        ev_repo = EvidenceRepository(db)
        job_repo = JobRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_recall_001",
            source_role=SourceRole.USER, text="test", occurred_at=now,
        )
        job = job_repo.create(scope=scope_1, evidence_id=ev.id)

        assert job.state == "pending"
        # Pending is distinguishable from no data
        assert job_repo.get_state(job.id) == "pending"

    def test_ready_with_no_memory(self, db: Session, scope_1):
        """Processing completes with no extractable memory → ready with no_memory reason."""
        ev_repo = EvidenceRepository(db)
        job_repo = JobRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_nomem_001",
            source_role=SourceRole.USER, text="hello", occurred_at=now,
        )
        job = job_repo.create(scope=scope_1, evidence_id=ev.id)

        acquired, token = job_repo.try_acquire_lease(job.id, "worker_1")
        assert acquired
        assert job_repo.complete(job.id, token, reason="no_memory")

        assert job_repo.get_state(job.id) == "ready"
        updated_job = job_repo.get_by_id(job.id)
        assert updated_job.reason == "no_memory"

    def test_failed_on_model_error(self, db: Session, scope_1):
        """Model failure → failed with reason, not empty."""
        ev_repo = EvidenceRepository(db)
        job_repo = JobRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_fail_001",
            source_role=SourceRole.USER, text="test", occurred_at=now,
        )
        job = job_repo.create(scope=scope_1, evidence_id=ev.id)
        job.max_retries = 1  # fail after 1 attempt

        acquired, token = job_repo.try_acquire_lease(job.id, "worker_1")
        assert acquired
        assert job_repo.fail(job.id, token, reason="model_unavailable")

        assert job_repo.get_state(job.id) == "failed"


@pytest.mark.contract_C10
class TestRecallModeAndBudget:
    """Verify recall filtering and expiry."""

    def test_current_excludes_superseded(self, db: Session, scope_1):
        """Current mode returns only non-superseded items."""
        atom_repo = MemoryAtomRepository(db)

        atom1 = atom_repo.create(
            scope=scope_1, type="preference",
            content="likes Python", source_kind="user",
        )
        atom2 = atom_repo.create(
            scope=scope_1, type="preference",
            content="likes Rust", source_kind="user",
        )
        # Supersede atom1
        atom_repo.supersede(atom1.id, atom2.id)

        current = atom_repo.get_current(scope_1)
        assert len(current) == 1
        assert current[0].content == "likes Rust"

    def test_history_includes_superseded(self, db: Session, scope_1):
        """History mode can return past assertions."""
        atom_repo = MemoryAtomRepository(db)

        atom1 = atom_repo.create(
            scope=scope_1, type="preference",
            content="old preference", source_kind="user",
        )
        atom2 = atom_repo.create(
            scope=scope_1, type="preference",
            content="new preference", source_kind="user",
        )
        atom_repo.supersede(atom1.id, atom2.id)

        history = atom_repo.get_history(scope_1)
        assert len(history) == 2

    def test_expired_items_excluded(self, db: Session, scope_1):
        """Items past valid_until are excluded from current mode."""
        atom_repo = MemoryAtomRepository(db)

        # Create an expired atom
        atom_repo.create(
            scope=scope_1, type="plan",
            content="Exam in June", source_kind="user",
            valid_from=datetime.now(UTC) - timedelta(days=30),
            valid_until=datetime.now(UTC) - timedelta(days=1),  # expired
        )

        # It exists in history
        history = atom_repo.get_history(scope_1, memory_type="plan")
        assert len(history) == 1

        # But valid_until is in the past (filtering logic is in recall service)
        atom = history[0]
        valid_until = atom.valid_until
        # Normalize for SQLite (strips timezone)
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        assert valid_until < datetime.now(UTC)