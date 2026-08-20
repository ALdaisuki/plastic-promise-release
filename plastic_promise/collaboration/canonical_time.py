"""Server-authoritative time helpers for the collaboration plane.

The collaboration wire contracts intentionally carry source timestamps so an
operator can diagnose clock skew.  Those values are never authoritative:
ordering, expiry, fences, cursors, and receipts must use a timestamp observed
by the server.  This module keeps the parsing/canonicalisation rules in one
place so the contract and SQLite adapters cannot drift.

The helpers are dependency-free on purpose.  They raise :class:`ValueError`
with stable, machine-readable messages; the public collaboration modules map
those failures to their existing ``CollaborationContractError`` codes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def parse_utc(value: object) -> datetime:
    """Parse an aware ISO-8601 timestamp and return an aware UTC datetime.

    Naive values are rejected rather than silently interpreted in the local
    timezone.  The rejection is important at all client/compute boundaries:
    a caller must not smuggle a local wall clock into a server ordering rule.
    """

    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or value != value.strip() or not value:
            raise ValueError("timestamp_invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def canonical_text(value: object) -> str:
    """Return an aware timestamp in the canonical microsecond ``...Z`` form."""

    parsed = parse_utc(value)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def server_now(clock: Callable[[], datetime] | None = None) -> datetime:
    """Read and validate the server clock once for one authority decision."""

    selected = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(selected, datetime):
        raise ValueError("clock_invalid")
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("timezone_required")
    return selected.astimezone(timezone.utc)


def server_now_text(clock: Callable[[], datetime] | None = None) -> str:
    """Return one canonical server-observation timestamp."""

    selected = server_now(clock)
    return selected.isoformat(timespec="microseconds").replace("+00:00", "Z")


def server_expiry_text(*, observed_at: object, retention_seconds: object | None) -> str | None:
    """Create an expiry from server policy, never from a source timestamp."""

    if retention_seconds is None:
        return None
    if (
        isinstance(retention_seconds, bool)
        or not isinstance(retention_seconds, int)
        or retention_seconds < 1
    ):
        raise ValueError("retention_invalid")
    observed = parse_utc(observed_at)
    try:
        expires = observed + timedelta(seconds=retention_seconds)
    except OverflowError as exc:
        raise ValueError("retention_invalid") from exc
    return canonical_text(expires)
