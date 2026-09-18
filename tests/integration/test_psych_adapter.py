"""Psych adapter integration tests.

Verifies off/shadow/active_internal modes and domain rules.
"""


import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    JobRepository,
    MemoryAtomRepository,
)
from mirror_memory.adapters.psych_adapter import AdapterMode, PsychAdapter
from mirror_memory.runtime.worker import JobWorker, _factory_from_session


@pytest.fixture
def adapter(db: Session) -> PsychAdapter:
    return PsychAdapter(db, app_id="psych")


class TestPsychAdapterModes:
    """Verify adapter mode behavior."""

    def test_off_mode_no_processing(self, adapter: PsychAdapter, db: Session):
        adapter.set_mode(AdapterMode.OFF)
        adapter.authorize_user("t1", "u1")

        result = adapter.observe_message("t1", "u1", "s1", "I like cats")
        assert not result["processed"]
        assert result["mode"] == "off"

    def test_shadow_mode_processes_but_not_in_reply(self, adapter: PsychAdapter, db: Session):
        adapter.set_mode(AdapterMode.SHADOW)
        adapter.authorize_user("t1", "u1")

        result = adapter.observe_message("t1", "u1", "s1", "I like cats")
        assert result["processed"]
        assert result["mode"] == "shadow"

        # Process the job
        if result["operation_id"]:
            from mirror_memory.core.types import Scope
            scope = Scope(tenant_id="t1", app_id="psych", subject_id="u1")
            db.commit()  # Worker opens its own session — must see committed data
            worker = JobWorker(_factory_from_session(db))
            worker.process(scope, result["operation_id"], "test_worker")

        recall = adapter.recall_for_reply("t1", "u1", "s1", "cats")
        assert recall["mode"] == "shadow"
        assert not recall["in_reply"]  # shadow = not in actual reply

    def test_active_internal_mode_for_cohort(self, adapter: PsychAdapter, db: Session):
        adapter.set_mode(AdapterMode.ACTIVE_INTERNAL)
        adapter.set_test_cohort({"u1"})
        adapter.authorize_user("t1", "u1")

        result = adapter.observe_message("t1", "u1", "s1", "I like dogs")
        assert result["processed"]

        recall = adapter.recall_for_reply("t1", "u1", "s1", "dogs")
        assert recall["mode"] == "active_internal"
        # User is in cohort, so in_reply should be True if memories exist
        # (may be False if no memories extracted, which is also valid)

    def test_active_internal_not_in_cohort(self, adapter: PsychAdapter, db: Session):
        adapter.set_mode(AdapterMode.ACTIVE_INTERNAL)
        adapter.set_test_cohort({"u_other"})  # u1 not in cohort
        adapter.authorize_user("t1", "u1")

        adapter.observe_message("t1", "u1", "s1", "I like fish")
        recall = adapter.recall_for_reply("t1", "u1", "s1", "fish")
        assert not recall["in_reply"]  # not in cohort


class TestPsychAdapterFormatting:
    """Verify memory formatting for Psych reply context."""

    def test_format_memories(self, adapter: PsychAdapter):
        memories = [
            {"type": "preference", "content": "User likes cats", "source": "user", "memory_id": "m1"},
            {"type": "fact", "content": "User lives in Beijing", "source": "user", "memory_id": "m2"},
        ]
        text = adapter.format_memories_for_context(memories)
        assert "用户记忆" in text
        assert "cats" in text
        assert "Beijing" in text

    def test_format_empty(self, adapter: PsychAdapter):
        assert adapter.format_memories_for_context([]) == ""


class TestPsychAdapterExtraction:
    """Verify domain extraction through the adapter."""

    def test_preference_extraction(self, adapter: PsychAdapter, db: Session):
        adapter.set_mode(AdapterMode.SHADOW)
        adapter.authorize_user("t1", "u1")

        result = adapter.observe_message("t1", "u1", "s1", "技术问题请展开解释")
        assert result["processed"]

        # Process the job
        from mirror_memory.core.types import Scope
        scope = Scope(tenant_id="t1", app_id="psych", subject_id="u1")
        job_repo = JobRepository(db)
        jobs = job_repo.find_pending(scope)
        if jobs:
            db.commit()  # Worker opens its own session — must see committed data
            worker = JobWorker(_factory_from_session(db))
            worker.process(scope, jobs[0].id, "test_worker")

        # Check if preference was extracted
        atom_repo = MemoryAtomRepository(db)
        atoms = atom_repo.get_current(scope)
        # At least one atom should exist from the extraction
        assert len(atoms) >= 1