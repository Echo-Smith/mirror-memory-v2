"""Shared utilities for Mirror Memory."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current UTC time. Single source of truth for all modules."""
    return datetime.now(UTC)