"""Side-effect-free phase ordering and partial-success resume planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import ReleaseRequest


class ReleasePhase(StrEnum):
    RESOURCE_GATE = "resource-gate"
    LOCAL_BUILD = "local-build"
    GHCR_EVIDENCE = "ghcr-evidence"
    SERVER_DEPLOYMENT = "server-deployment"
    MCP_E2E = "mcp-e2e"
    SQLITE_MIGRATION = "sqlite-migration"
    LANCEDB_PROMOTION = "lancedb-promotion"
    MAINTENANCE_START = "maintenance-start"
    PYPI_PUBLICATION = "pypi-publication"
    RELEASE_SYNC = "release-sync"
    RELEASE_LEARNING = "release-learning"


_ORDERED_PHASES = (
    ReleasePhase.RESOURCE_GATE,
    ReleasePhase.LOCAL_BUILD,
    ReleasePhase.GHCR_EVIDENCE,
    ReleasePhase.SERVER_DEPLOYMENT,
    ReleasePhase.MCP_E2E,
    ReleasePhase.SQLITE_MIGRATION,
    ReleasePhase.LANCEDB_PROMOTION,
    ReleasePhase.MAINTENANCE_START,
    ReleasePhase.PYPI_PUBLICATION,
    ReleasePhase.RELEASE_SYNC,
    ReleasePhase.RELEASE_LEARNING,
)


@dataclass
class ReleaseLedger:
    """Minimal terminal phase projection; receipt persistence belongs to a later slice."""

    outcomes: dict[ReleasePhase, str] = field(default_factory=dict)

    def record(self, phase: ReleasePhase, outcome: str) -> None:
        if outcome not in {"passed", "failed"}:
            raise ValueError("release_phase_outcome_invalid")
        self.outcomes[phase] = outcome


def remaining_phases(request: ReleaseRequest, ledger: ReleaseLedger) -> tuple[ReleasePhase, ...]:
    """Return the ordered remaining work, or nothing after any terminal failure."""

    if any(outcome == "failed" for outcome in ledger.outcomes.values()):
        return ()
    selected = _selected_phases(request)
    return tuple(phase for phase in selected if ledger.outcomes.get(phase) != "passed")


def _selected_phases(request: ReleaseRequest) -> tuple[ReleasePhase, ...]:
    """Project only actions explicitly selected by a signed release request.

    The resource gate and the local, non-publishing build are always required.
    Every production mutation remains opt-in: a planner may not infer migration,
    index promotion, publication, or maintenance from a successful build.
    """

    actions = request.actions
    selected = [ReleasePhase.RESOURCE_GATE, ReleasePhase.LOCAL_BUILD]
    if actions.push_ghcr_stable or actions.deploy_server or actions.publish_pypi:
        selected.append(ReleasePhase.GHCR_EVIDENCE)
    if actions.deploy_server:
        selected.extend((ReleasePhase.SERVER_DEPLOYMENT, ReleasePhase.MCP_E2E))
    if actions.sqlite_migration:
        selected.append(ReleasePhase.SQLITE_MIGRATION)
    if actions.lancedb_promotion:
        selected.append(ReleasePhase.LANCEDB_PROMOTION)
    if actions.deploy_server and actions.start_maintenance:
        selected.append(ReleasePhase.MAINTENANCE_START)
    if actions.publish_pypi:
        selected.append(ReleasePhase.PYPI_PUBLICATION)
    if actions.push_ghcr_stable or actions.publish_pypi:
        selected.append(ReleasePhase.RELEASE_SYNC)
    if len(selected) > 2:
        selected.append(ReleasePhase.RELEASE_LEARNING)
    return tuple(selected)
