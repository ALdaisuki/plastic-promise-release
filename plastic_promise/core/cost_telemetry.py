"""Explicit currency contract for token-cost telemetry."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

SUPPORTED_COST_CURRENCIES = frozenset({"USD", "CNY"})
DISTINCT_TOKEN_RATE_LIMITATION = "distinct-input-output-rates-not-modeled"
TOKEN_COUNT_UNAVAILABLE = "token-count-unavailable"


@dataclass(frozen=True)
class TokenCostPolicy:
    """One immutable provider pricing schedule captured at construction."""

    cost_per_million_tokens: float | None
    currency: str
    pricing_revision: str
    configured: bool

    @classmethod
    def from_environment(cls, env_prefix: str, *, reason_prefix: str) -> TokenCostPolicy:
        rate_name = f"{env_prefix}_COST_PER_MILLION_TOKENS"
        currency_name = f"{env_prefix}_COST_CURRENCY"
        revision_name = f"{env_prefix}_PRICING_REVISION"
        raw_rate = os.getenv(rate_name, "").strip()
        raw_currency = os.getenv(currency_name, "").strip()
        pricing_revision = os.getenv(revision_name, "").strip()

        rate: float | None = None
        if raw_rate:
            try:
                rate = float(raw_rate)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{reason_prefix}_cost_per_million_tokens_invalid") from None
            if not math.isfinite(rate) or rate < 0.0:
                raise ValueError(f"{reason_prefix}_cost_per_million_tokens_invalid")

        currency = (raw_currency or "USD").upper()
        if currency not in SUPPORTED_COST_CURRENCIES:
            raise ValueError(f"{reason_prefix}_cost_currency_invalid")
        if len(pricing_revision) > 256 or any(
            ord(character) < 32 or ord(character) == 127 for character in pricing_revision
        ):
            raise ValueError(f"{reason_prefix}_pricing_revision_invalid")
        return cls(
            cost_per_million_tokens=rate,
            currency=currency,
            pricing_revision=pricing_revision,
            configured=bool(raw_rate or raw_currency or pricing_revision),
        )

    def estimate(self, token_count: int | None) -> float | None:
        if token_count is None or self.cost_per_million_tokens is None:
            return None
        return round(token_count * self.cost_per_million_tokens / 1_000_000, 8)

    def telemetry(self, token_count: int | None, *, cost_basis: str) -> dict[str, object]:
        if token_count is None:
            return {
                "estimated_cost": None,
                "cost_currency": self.currency,
                "estimated_cost_usd": None,
                "pricing_revision": self.pricing_revision,
                "cost_basis": "unknown",
                "cost_limitation": TOKEN_COUNT_UNAVAILABLE,
            }
        cost = self.estimate(token_count)
        telemetry = {
            "estimated_cost": cost,
            "cost_currency": self.currency,
            "estimated_cost_usd": cost if self.currency == "USD" else None,
            "pricing_revision": self.pricing_revision,
            "cost_basis": cost_basis,
        }
        if cost_basis == "total_tokens_single_blended_rate":
            telemetry["cost_limitation"] = DISTINCT_TOKEN_RATE_LIMITATION
        return telemetry


__all__ = [
    "DISTINCT_TOKEN_RATE_LIMITATION",
    "SUPPORTED_COST_CURRENCIES",
    "TOKEN_COUNT_UNAVAILABLE",
    "TokenCostPolicy",
]
