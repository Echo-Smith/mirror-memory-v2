"""C16–C17: Budget and capacity guardrails.

C16: Reserve/settle/release lifecycle. Concurrent limits. Idempotent settlement.
C17: Daily usage tracking. Soft/hard thresholds. Control operations exempt.

References: R11.
"""


import pytest

from mirror_memory.adapters.postgresql.budget_repo import (
    BudgetProfileRepository,
    BudgetReservationRepository,
    UsageLedgerRepository,
)
from mirror_memory.adapters.postgresql.repositories import ScopeControlRepository
from mirror_memory.application.budget_service import BudgetService


@pytest.mark.contract_C16
class TestBudgetReservation:
    """Verify budget reservation lifecycle."""

    def test_reserve_and_settle(self, db, scope_1):
        svc = BudgetService(db)
        ScopeControlRepository(db).get_or_create(scope_1)
        check = svc.check_and_reserve(scope_1, "cost", 0.5, operation="observe")
        assert check.allowed
        assert svc.settle(check.reservation_id, 0.3)

    def test_reserve_and_release(self, db, scope_1):
        svc = BudgetService(db)
        ScopeControlRepository(db).get_or_create(scope_1)
        check = svc.check_and_reserve(scope_1, "cost", 0.5, operation="observe")
        assert svc.release(check.reservation_id)

    def test_no_double_settlement(self, db, scope_1):
        svc = BudgetService(db)
        ScopeControlRepository(db).get_or_create(scope_1)
        BudgetProfileRepository(db).create_or_update(scope_1.app_id, scope_daily_cost_limit=10.0)
        check = svc.check_and_reserve(scope_1, "cost", 0.5, operation="observe")
        assert check.reservation_id is not None
        assert svc.settle(check.reservation_id, 0.3)
        assert not svc.settle(check.reservation_id, 0.2)

    def test_concurrent_limit_enforced(self, db, scope_1):
        svc = BudgetService(db)
        ScopeControlRepository(db).get_or_create(scope_1)
        BudgetProfileRepository(db).create_or_update(scope_1.app_id, scope_concurrent_jobs=2)
        c1 = svc.check_and_reserve(scope_1, "call", 1.0)
        c2 = svc.check_and_reserve(scope_1, "call", 1.0)
        c3 = svc.check_and_reserve(scope_1, "call", 1.0)
        assert c1.allowed and c2.allowed and not c3.allowed

    def test_control_ops_exempt(self, db, scope_1):
        svc = BudgetService(db)
        ScopeControlRepository(db).get_or_create(scope_1)
        BudgetProfileRepository(db).create_or_update(scope_1.app_id, scope_concurrent_jobs=0)
        for op in ("forget", "correct", "sync_authorization"):
            check = svc.check_and_reserve(scope_1, "call", 1.0, operation=op)
            assert check.allowed

    def test_expired_cleanup(self, db, scope_1):
        repo = BudgetReservationRepository(db)
        sc = ScopeControlRepository(db).get_or_create(scope_1)
        repo.reserve(sc.id, "call", 1.0, ttl_seconds=0)
        assert repo.cleanup_expired() >= 1


@pytest.mark.contract_C17
class TestCapacityGuardrails:
    """Verify capacity guardrails."""

    def test_daily_cost_limit(self, db, scope_1):
        svc = BudgetService(db)
        sc_repo = ScopeControlRepository(db)
        sc_repo.get_or_create(scope_1)
        BudgetProfileRepository(db).create_or_update(scope_1.app_id, scope_daily_cost_limit=1.0)
        sc = sc_repo.get(scope_1)
        UsageLedgerRepository(db).record(sc.id, "cost", 0.95)
        assert not svc.check_and_reserve(scope_1, "cost", 0.1, operation="observe").allowed

    def test_soft_threshold_warning(self, db, scope_1):
        svc = BudgetService(db)
        sc_repo = ScopeControlRepository(db)
        sc_repo.get_or_create(scope_1)
        BudgetProfileRepository(db).create_or_update(scope_1.app_id, scope_daily_cost_limit=1.0, soft_threshold_ratio=0.8)
        UsageLedgerRepository(db).record(sc_repo.get(scope_1).id, "cost", 0.85)
        check = svc.check_and_reserve(scope_1, "cost", 0.01, operation="observe")
        assert check.allowed and check.soft_warning

    def test_usage_summary(self, db, scope_1):
        svc = BudgetService(db)
        ScopeControlRepository(db).get_or_create(scope_1)
        s = svc.get_usage_summary(scope_1)
        assert "scope" in s and "period_cost" in s

    def test_no_profile_allows_all(self, db, scope_1):
        svc = BudgetService(db)
        ScopeControlRepository(db).get_or_create(scope_1)
        assert svc.check_and_reserve(scope_1, "cost", 999.0).allowed

    def test_usage_persists(self, db, scope_1):
        repo = UsageLedgerRepository(db)
        sc = ScopeControlRepository(db).get_or_create(scope_1)
        for i in range(5):
            repo.record(sc.id, "cost", 0.1 * i)
        assert repo.get_period_usage(sc.id, "cost") > 0