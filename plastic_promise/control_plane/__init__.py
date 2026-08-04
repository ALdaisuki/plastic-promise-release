"""Loopback-only, role-separated remote configuration control plane."""

from plastic_promise.control_plane.auth import (
    ControlPlaneAuthenticationError,
    ControlPlaneAuthenticator,
    ControlPlaneCredential,
    ControlPlanePrincipal,
)
from plastic_promise.control_plane.config_schema import (
    CONFIG_CONTRACT,
    ControlPlaneError,
    ControlPlaneValidationError,
    default_safe_config,
)
from plastic_promise.control_plane.store import (
    ActivationResult,
    AuditEvent,
    ConfigRevision,
    ControlPlaneAuthorizationError,
    ControlPlaneConfigStore,
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlanePreconditionError,
    ControlPlaneStorageError,
    SafeConfigSnapshot,
    ValidationResult,
)

__all__ = [
    "CONFIG_CONTRACT",
    "ActivationResult",
    "AuditEvent",
    "ConfigRevision",
    "ControlPlaneAuthenticationError",
    "ControlPlaneAuthenticator",
    "ControlPlaneAuthorizationError",
    "ControlPlaneConfigStore",
    "ControlPlaneConflictError",
    "ControlPlaneCredential",
    "ControlPlaneError",
    "ControlPlaneNotFoundError",
    "ControlPlanePreconditionError",
    "ControlPlanePrincipal",
    "ControlPlaneStorageError",
    "ControlPlaneValidationError",
    "SafeConfigSnapshot",
    "ValidationResult",
    "default_safe_config",
]
