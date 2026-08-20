"""Maintainer-only, request-triggered Plastic Promise release contracts.

The package contains secret-free validation, planning, and read-only resource
observation primitives. It deliberately has no publishing, credential-store,
SSH, database, MCP, or Docker-mutation capability; side-effecting adapters must
consume these contracts rather than bypass them.
"""

from .contracts import (
    BuilderMode,
    ReleaseActions,
    ReleaseBuilderError,
    ReleaseConfirmation,
    ReleaseRequest,
    confirm_request,
    validate_confirmation,
    validate_windows_source_root,
)
from .ledger import ReleaseLedger, ReleasePhase, remaining_phases
from .persistence import (
    load_confirmation,
    load_request,
    write_confirmation,
    write_receipt,
    write_request,
)
from .resource_gate import ResourceGateDecision, ResourceSnapshot, evaluate_resource_gate
from .resource_probe import (
    ResourceProbeError,
    ResourceSample,
    aggregate_resource_samples,
    observe_resource_window,
)
from .retention import RetentionEntry, RetentionPlan, plan_retention_cleanup

__all__ = [
    "BuilderMode",
    "ReleaseActions",
    "ReleaseBuilderError",
    "ReleaseConfirmation",
    "ReleaseLedger",
    "ReleasePhase",
    "ReleaseRequest",
    "ResourceGateDecision",
    "ResourceProbeError",
    "ResourceSample",
    "ResourceSnapshot",
    "RetentionEntry",
    "RetentionPlan",
    "aggregate_resource_samples",
    "confirm_request",
    "evaluate_resource_gate",
    "load_confirmation",
    "load_request",
    "plan_retention_cleanup",
    "observe_resource_window",
    "remaining_phases",
    "validate_confirmation",
    "validate_windows_source_root",
    "write_confirmation",
    "write_receipt",
    "write_request",
]
