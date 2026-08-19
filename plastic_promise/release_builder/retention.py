"""Retention planning only; executing physical deletion stays separately governed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_RETENTION_DAYS = {
    "source-archive": 14,
    "build-cache": 1,
    "receipt": 90,
    "server-backup": 5,
}


@dataclass(frozen=True)
class RetentionEntry:
    id: str
    category: str
    created_at: datetime
    incomplete_request: bool = False
    current_stable: bool = False
    rollback_protected: bool = False


@dataclass(frozen=True)
class RetentionPlan:
    eligible_ids: tuple[str, ...]
    server_backup_retention_days: int


def plan_retention_cleanup(
    entries: tuple[RetentionEntry, ...],
    *,
    now: datetime,
) -> RetentionPlan:
    """Select only expired, unprotected entries; never perform deletion."""

    now_utc = now.astimezone(UTC)
    eligible: list[str] = []
    for entry in entries:
        days = _RETENTION_DAYS.get(entry.category)
        if (
            days is None
            or entry.incomplete_request
            or entry.current_stable
            or entry.rollback_protected
        ):
            continue
        created_at = entry.created_at.astimezone(UTC)
        if created_at < now_utc - timedelta(days=days):
            eligible.append(entry.id)
    return RetentionPlan(tuple(eligible), _RETENTION_DAYS["server-backup"])
