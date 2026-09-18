"""Budget repository — profiles, reservations, and usage ledger.

Implements the reserve/settle/release lifecycle for R11 budget controls.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.models import (
    BudgetProfile,
    BudgetReservation,
    UsageLedger,
)
from mirror_memory.core.utils import utcnow

# ---------------------------------------------------------------------------
# Profile Repository
# ---------------------------------------------------------------------------


class BudgetProfileRepository:
    """Manages per-app budget profiles."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_app(self, app_id: str) -> BudgetProfile | None:
        return self._session.query(BudgetProfile).filter(
            BudgetProfile.app_id == app_id
        ).first()

    def create_or_update(
        self,
        app_id: str,
        scope_daily_token_limit: int = 10000,
        scope_daily_cost_limit: float = 1.0,
        scope_concurrent_jobs: int = 2,
        app_daily_cost_limit: float = 100.0,
        soft_threshold_ratio: float = 0.8,
        hard_threshold_ratio: float = 1.0,
        recovery_threshold_ratio: float = 0.6,
        cooldown_seconds: int = 300,
    ) -> BudgetProfile:
        existing = self.get_by_app(app_id)
        if existing:
            existing.scope_daily_token_limit = scope_daily_token_limit
            existing.scope_daily_cost_limit = scope_daily_cost_limit
            existing.scope_concurrent_jobs = scope_concurrent_jobs
            existing.app_daily_cost_limit = app_daily_cost_limit
            existing.soft_threshold_ratio = soft_threshold_ratio
            existing.hard_threshold_ratio = hard_threshold_ratio
            existing.recovery_threshold_ratio = recovery_threshold_ratio
            existing.cooldown_seconds = cooldown_seconds
            existing.version += 1
            self._session.flush()
            return existing
        profile = BudgetProfile(
            app_id=app_id,
            scope_daily_token_limit=scope_daily_token_limit,
            scope_daily_cost_limit=scope_daily_cost_limit,
            scope_concurrent_jobs=scope_concurrent_jobs,
            app_daily_cost_limit=app_daily_cost_limit,
            soft_threshold_ratio=soft_threshold_ratio,
            hard_threshold_ratio=hard_threshold_ratio,
            recovery_threshold_ratio=recovery_threshold_ratio,
            cooldown_seconds=cooldown_seconds,
        )
        self._session.add(profile)
        self._session.flush()
        return profile


# ---------------------------------------------------------------------------
# Reservation Repository
# ---------------------------------------------------------------------------


class BudgetReservationRepository:
    """Manages budget reservations with atomicity."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def reserve(
        self,
        scope_id: str,
        resource_type: str,
        amount: float,
        ttl_seconds: int = 300,
        idempotency_key: str | None = None,
    ) -> BudgetReservation:
        """Create a reservation. Caller must have already checked limits."""
        now = utcnow()
        res = BudgetReservation(
            scope_id=scope_id,
            resource_type=resource_type,
            amount=amount,
            status="reserved",
            idempotency_key=idempotency_key,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self._session.add(res)
        self._session.flush()
        return res

    def settle(self, reservation_id: str, actual_amount: float) -> bool:
        """Settle a reservation with actual usage. Idempotent per reservation."""
        res = self._session.get(BudgetReservation, reservation_id)
        if res is None or res.status != "reserved":
            return False
        res.status = "settled"
        res.settled_amount = actual_amount
        ledger = UsageLedger(
            scope_id=res.scope_id,
            resource_type=res.resource_type,
            amount=actual_amount,
            source=reservation_id,
            idempotency_key=res.idempotency_key,
        )
        self._session.add(ledger)
        self._session.flush()
        return True

    def release(self, reservation_id: str) -> bool:
        """Release a reservation without settling."""
        res = self._session.get(BudgetReservation, reservation_id)
        if res is None or res.status != "reserved":
            return False
        res.status = "released"
        self._session.flush()
        return True

    def get_active_count(self, scope_id: str, resource_type: str) -> int:
        """Count active (reserved, non-expired) reservations."""
        now = utcnow()
        return self._session.query(BudgetReservation).filter(
            BudgetReservation.scope_id == scope_id,
            BudgetReservation.resource_type == resource_type,
            BudgetReservation.status == "reserved",
            BudgetReservation.expires_at > now,
        ).count()

    def cleanup_expired(self) -> int:
        """Release expired reservations. Returns count."""
        now = utcnow()
        expired = self._session.query(BudgetReservation).filter(
            BudgetReservation.status == "reserved",
            BudgetReservation.expires_at <= now,
        ).all()
        for res in expired:
            res.status = "released"
        self._session.flush()
        return len(expired)


# ---------------------------------------------------------------------------
# Usage Ledger Repository
# ---------------------------------------------------------------------------


class UsageLedgerRepository:
    """Immutable usage records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_period_usage(
        self, scope_id: str, resource_type: str, since: datetime | None = None,
    ) -> float:
        """Sum usage for a scope since a given time (default: start of today)."""
        if since is None:
            now = utcnow()
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        entries = self._session.query(UsageLedger).filter(
            UsageLedger.scope_id == scope_id,
            UsageLedger.resource_type == resource_type,
            UsageLedger.created_at >= since,
        ).all()
        return sum(e.amount for e in entries)

    def get_app_period_usage(
        self, scope_ids: list[str], resource_type: str, since: datetime | None = None,
    ) -> float:
        """Sum usage across all scopes in an app."""
        if not scope_ids:
            return 0.0
        if since is None:
            now = utcnow()
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        entries = self._session.query(UsageLedger).filter(
            UsageLedger.scope_id.in_(scope_ids),
            UsageLedger.resource_type == resource_type,
            UsageLedger.created_at >= since,
        ).all()
        return sum(e.amount for e in entries)

    def record(
        self,
        scope_id: str,
        resource_type: str,
        amount: float,
        source: str = "direct",
        idempotency_key: str | None = None,
    ) -> UsageLedger:
        """Record a direct usage entry."""
        entry = UsageLedger(
            scope_id=scope_id,
            resource_type=resource_type,
            amount=amount,
            source=source,
            idempotency_key=idempotency_key,
        )
        self._session.add(entry)
        self._session.flush()
        return entry