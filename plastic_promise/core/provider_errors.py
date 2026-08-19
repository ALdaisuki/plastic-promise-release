"""Provider-neutral error types shared by server-safe runtime code.

The HTTP transport and concrete provider implementations live in the compute
package.  These bounded diagnostic types intentionally live in the common
runtime so server-only modules can classify a provider failure without
importing the provider transport (which is excluded from the server image).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderHTTPDiagnostics:
    """Bounded, non-content diagnostics suitable for logs and metrics."""

    attempts: int = 0
    latency_ms: float = 0.0
    status_code: int | None = None
    request_id: str | None = None
    usage: dict[str, int | float] = field(default_factory=dict)
    circuit_state: str = "closed"


@dataclass(frozen=True)
class ProviderHTTPResult:
    """Validated provider payload and bounded request metadata."""

    payload: dict[str, Any]
    attempts: int
    latency_ms: float
    request_id: str | None
    status_code: int | None = None
    usage: dict[str, int | float] = field(default_factory=dict)
    circuit_state: str = "closed"

    @property
    def diagnostics(self) -> ProviderHTTPDiagnostics:
        """Compatibility view for callers that prefer grouped diagnostics."""

        return ProviderHTTPDiagnostics(
            attempts=self.attempts,
            latency_ms=self.latency_ms,
            status_code=self.status_code,
            request_id=self.request_id,
            usage=dict(self.usage),
            circuit_state=self.circuit_state,
        )


class ProviderHTTPError(RuntimeError):
    """Provider transport failure exposing only a stable reason code."""

    def __init__(
        self,
        reason: str,
        diagnostics: ProviderHTTPDiagnostics | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics or ProviderHTTPDiagnostics()

    @property
    def attempts(self) -> int:
        return self.diagnostics.attempts

    @property
    def latency_ms(self) -> float:
        return self.diagnostics.latency_ms

    @property
    def request_id(self) -> str | None:
        return self.diagnostics.request_id

    @property
    def status_code(self) -> int | None:
        return self.diagnostics.status_code

    @property
    def usage(self) -> dict[str, int | float]:
        return dict(self.diagnostics.usage)

    @property
    def circuit_state(self) -> str:
        return self.diagnostics.circuit_state
