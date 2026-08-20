"""Shared token-budget contract for structured JSON inference.

Token counts are request hints, not a local memory-safety boundary.  A value of
``0`` in a configured limit means that the caller does not impose an additional
token ceiling.  The request itself must still contain a positive integer, while
prompt/payload/output byte limits, provider limits, timeouts, retries, and queue
capacity remain authoritative safeguards.
"""

from __future__ import annotations

DEFAULT_STRUCTURED_REQUEST_TOKENS = 32 * 1024
UNBOUNDED_STRUCTURED_TOKEN_LIMIT = 0


def structured_tokens_allowed(requested: int, configured_limit: int) -> bool:
    """Return whether a positive request fits the configured local policy."""

    if (
        not isinstance(requested, int)
        or isinstance(requested, bool)
        or requested < 1
        or not isinstance(configured_limit, int)
        or isinstance(configured_limit, bool)
        or configured_limit < 0
    ):
        return False
    return configured_limit == UNBOUNDED_STRUCTURED_TOKEN_LIMIT or requested <= configured_limit


def validate_structured_token_limit(value: int, *, allow_unbounded: bool = True) -> int:
    """Validate a configured limit, preserving ``0`` as the unbounded sentinel."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("structured_token_limit_invalid")
    minimum = 0 if allow_unbounded else 1
    if value < minimum:
        raise ValueError("structured_token_limit_invalid")
    return value
