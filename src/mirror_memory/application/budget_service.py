"""Budget service — reserve/settle/release with threshold enforcement.

Implements R11 budget controls:
- Profile-driven limits per app
- Pre-operation reservation with threshold checks
- Post-operation settlement (actual vs reserved)
- Control operations (delete/revoke/forget) are exempt
- Soft threshold → warn, hard threshold → reject
- Cooldown with recovery threshold
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.budget_repo import (
    BudgetProfileRepository,
    BudgetReservationRepository,
    UsageLedgerRepository,
)
from mirror_memory.adapters.postgresql.repositories import ScopeControlRepository
from mirror_memory.core.types import Scope
from mirror_memory.core.utils import utcnow


@dataclass
class BudgetCheckResult:
    """Result of a budget pre-check."""

    allowed: bool
    reason: str = ""
    soft_warning: bool = False  # True if approaching limit
    reservation_id: str | None = None


class BudgetService:
    """Orchestrates budget reservation, settlement, and enforcement.

    Usage:
        svc = BudgetService(session)
        check = svc.check_and_reserve(scope, "cost", estimated_cost)
        if not check.allowed:
            # reject
        # ... do work ...
        svc.settle(check.reservation_id, actual_cost)
    """

    # Control operations that are exempt from budget limits
    CONTROL_OPERATIONS: ClassVar[set[str]] = {"forget", "correct", "sync_authorization"}

    def __init__(self, session: Session) -> None:
        self._session = session
        self._profile_repo = BudgetProfileRepository(session)
        self._res_repo = BudgetReservationRepository(session)
        self._ledger_repo = UsageLedgerRepository(session)
        self._scope_repo = ScopeControlRepository(session)

    def get_or_create_profile(self, app_id: str):
        """Get or create a default profile for an app."""
        profile = self._profile_repo.get_by_app(app_id)
        if profile is None:
            profile = self._profile_repo.create_or_update(app_id)
        return profile

    def check_and_reserve(
        self,
        scope: Scope,
        resource_type: str,
        estimated_amount: float,
        operation: str = "observe",
        idempotency_key: str | None = None,
    ) -> BudgetCheckResult:
        """Check budget limits and create a reservation if allowed.

        Control operations (delete/revoke/forget) bypass budget checks.
        """
        # Control operations are exempt
        if operation in self.CONTROL_OPERATIONS:
            return BudgetCheckResult(allowed=True, reason="control_operation_exempt")

        # Get scope
        sc = self._scope_repo.get(scope)
        if sc is None:
            return BudgetCheckResult(allowed=False, reason="scope_not_found")

        # Get profile
        profile = self._profile_repo.get_by_app(scope.app_id)
        if profile is None:
            # No profile = no limits = allow
            return BudgetCheckResult(allowed=True, reason="no_profile")

        utcnow()

        # Check concurrent job limit
        if resource_type == "call":
            active = self._res_repo.get_active_count(sc.id, "call")
            if active >= profile.scope_concurrent_jobs:
                return BudgetCheckResult(
                    allowed=False,
                    reason=f"concurrent_limit_reached ({active}/{profile.scope_concurrent_jobs})",
                )

        # Check daily usage
        period_usage = self._ledger_repo.get_period_usage(sc.id, resource_type)

        # Determine limits
        if resource_type == "token":
            hard_limit = float(profile.scope_daily_token_limit)
        elif resource_type == "cost":
            hard_limit = float(profile.scope_daily_cost_limit)
        else:
            hard_limit = float("inf")

        # Include pending reservations in the total
        pending_reservations = self._res_repo.get_active_count(sc.id, resource_type)
        # Estimate pending amount (conservative: assume each pending reservation is at the hard limit / 10)
        pending_estimate = pending_reservations * (hard_limit * 0.1) if hard_limit != float("inf") else 0
        projected_total = period_usage + pending_estimate + estimated_amount

        # Hard threshold check
        hard_threshold = hard_limit * profile.hard_threshold_ratio
        if projected_total > hard_threshold:
            return BudgetCheckResult(
                allowed=False,
                reason=f"daily_{resource_type}_limit_reached ({projected_total:.2f}/{hard_threshold:.2f})",
            )

        # Soft threshold warning
        soft_warning = False
        soft_threshold = hard_limit * profile.soft_threshold_ratio
        if projected_total > soft_threshold:
            soft_warning = True

        # Create reservation
        res = self._res_repo.reserve(
            scope_id=sc.id,
            resource_type=resource_type,
            amount=estimated_amount,
            ttl_seconds=300,
            idempotency_key=idempotency_key,
        )

        return BudgetCheckResult(
            allowed=True,
            soft_warning=soft_warning,
            reservation_id=res.id,
        )

    def settle(self, reservation_id: str | None, actual_amount: float) -> bool:
        """Settle a reservation with actual usage."""
        if reservation_id is None:
            return True
        return self._res_repo.settle(reservation_id, actual_amount)

    def release(self, reservation_id: str | None) -> bool:
        """Release a reservation without settling (e.g., operation cancelled)."""
        if reservation_id is None:
            return True
        return self._res_repo.release(reservation_id)

    def get_usage_summary(self, scope: Scope) -> dict:
        """Get current usage summary for a scope."""
        sc = self._scope_repo.get(scope)
        if sc is None:
            return {"error": "scope_not_found"}

        profile = self._profile_repo.get_by_app(scope.app_id)

        cost_usage = self._ledger_repo.get_period_usage(sc.id, "cost")
        token_usage = self._ledger_repo.get_period_usage(sc.id, "token")
        active_calls = self._res_repo.get_active_count(sc.id, "call")

        return {
            "scope": scope.key(),
            "period_cost": cost_usage,
            "period_tokens": token_usage,
            "active_calls": active_calls,
            "profile": {
                "app_id": profile.app_id if profile else None,
                "daily_cost_limit": profile.scope_daily_cost_limit if profile else None,
                "daily_token_limit": profile.scope_daily_token_limit if profile else None,
                "concurrent_jobs_limit": profile.scope_concurrent_jobs if profile else None,
            } if profile else None,
        }

    def cleanup_expired(self) -> int:
        """Release expired reservations."""
        return self._res_repo.cleanup_expired()