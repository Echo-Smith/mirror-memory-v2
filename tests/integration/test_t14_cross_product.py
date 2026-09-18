"""T14: Cross-product reuse — Bi Run Zhi Tan integration.

Verifies R15: independent app_id, data isolation, domain rules.
Re-runs Psych regression to confirm no breakage.
"""

from sqlalchemy.orm import Session

from mirror_memory.adapters.birun_adapter import BiRunAdapter
from mirror_memory.adapters.postgresql.repositories import JobRepository
from mirror_memory.adapters.psych_adapter import AdapterMode, PsychAdapter
from mirror_memory.core.types import Scope
from mirror_memory.runtime.worker import JobWorker, _factory_from_session


class TestT14CrossProduct:
    """T14: Verify second product integration."""

    def test_birun_independent_app_id(self, db: Session):
        """Bi Run uses different app_id than Psych."""
        birun = BiRunAdapter(db)
        psych = PsychAdapter(db)
        assert birun.app_id != psych._app_id

    def test_birun_observe_and_recall(self, db: Session):
        """Bi Run can observe and recall independently."""
        birun = BiRunAdapter(db)
        birun.authorize_user("t1", "u1")

        result = birun.observe_message("t1", "u1", "s1", "我喜欢简洁的写作风格")
        assert result["success"]

        # Process job
        scope = Scope(tenant_id="t1", app_id="bi_run_zhi_tan", subject_id="u1")
        job_repo = JobRepository(db)
        jobs = job_repo.find_pending(scope)
        if jobs:
            worker = JobWorker(_factory_from_session(db))
            worker.process(scope, jobs[0].id, "test_worker")

        recall = birun.recall("t1", "u1", "s1", "写作风格")
        assert recall["success"]

    def test_psych_data_invisible_to_birun(self, db: Session):
        """Psych data is not visible through Bi Run adapter."""
        psych = PsychAdapter(db)
        birun = BiRunAdapter(db)

        psych.set_mode(AdapterMode.SHADOW)
        psych.authorize_user("t1", "u1")
        birun.authorize_user("t1", "u1")

        # Psych user observes
        psych.observe_message("t1", "u1", "s1", "我喜欢猫")

        # Bi Run should not see it
        recall = birun.recall("t1", "u1", "s1", "猫")
        assert recall["success"]
        assert len(recall["memories"]) == 0

    def test_birun_data_invisible_to_psych(self, db: Session):
        """Bi Run data is not visible through Psych adapter."""
        psych = PsychAdapter(db)
        birun = BiRunAdapter(db)

        psych.set_mode(AdapterMode.SHADOW)
        psych.authorize_user("t1", "u1")
        birun.authorize_user("t1", "u1")

        # Bi Run user observes
        birun.observe_message("t1", "u1", "s1", "我喜欢简洁风格")

        # Psych should not see it
        recall = psych.recall_for_reply("t1", "u1", "s1", "简洁")
        assert len(recall["memories"]) == 0

    def test_same_user_different_apps(self, db: Session):
        """Same user can have independent memories in both products."""
        psych = PsychAdapter(db)
        birun = BiRunAdapter(db)

        psych.set_mode(AdapterMode.SHADOW)
        psych.authorize_user("t1", "u1")
        birun.authorize_user("t1", "u1")

        psych.observe_message("t1", "u1", "s1", "我喜欢Python")
        birun.observe_message("t1", "u1", "s1", "我喜欢Markdown格式")

        # Each sees only its own
        psych_recall = psych.recall_for_reply("t1", "u1", "s1", "Python")
        birun_recall = birun.recall("t1", "u1", "s1", "Markdown")

        # No cross-contamination
        assert not any("Markdown" in m.get("content", "") for m in psych_recall["memories"])
        assert not any("Python" in m.get("content", "") for m in birun_recall["memories"])

    def test_psych_regression_after_birun(self, db: Session):
        """Adding Bi Run doesn't break Psych behavior."""
        psych = PsychAdapter(db)
        birun = BiRunAdapter(db)

        psych.set_mode(AdapterMode.SHADOW)
        psych.authorize_user("t1", "u1")
        birun.authorize_user("t1", "u1")

        # Psych works normally
        r1 = psych.observe_message("t1", "u1", "s1", "我喜欢深色模式")
        assert r1["processed"]

        # Bi Run also works
        r2 = birun.observe_message("t1", "u1", "s1", "我喜欢简约设计")
        assert r2["success"]

        # Psych still works
        r3 = psych.observe_message("t1", "u1", "s2", "我喜欢猫")
        assert r3["processed"]