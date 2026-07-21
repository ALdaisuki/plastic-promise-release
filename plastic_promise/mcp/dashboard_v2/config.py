"""Fail-closed configuration and local authority for Dashboard V2."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


class DashboardConfigurationError(ValueError):
    """Dashboard startup configuration is unsafe or unsupported."""


class DashboardAccessError(PermissionError):
    """A dashboard request cannot be admitted to a local project scope."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _exact_one(value: object) -> bool:
    return isinstance(value, str) and value == "1"


def _normalize_project_id(value: object) -> str:
    project_id = str(value or "").strip()
    if not project_id:
        return ""
    if project_id.startswith("project:"):
        return project_id
    return f"project:{project_id}"


def _loopback_host(host: object) -> bool:
    value = str(host or "").strip().casefold()
    if value == "localhost":
        return True
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _loopback_authority(authority: object) -> bool:
    """Validate an HTTP Host authority without resolving attacker-controlled DNS."""
    value = str(authority or "").strip()
    if not value or any(character in value for character in "\r\n\t/@,\\"):
        return False
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return False
        host = value[1:closing]
        suffix = value[closing + 1 :]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            return False
    elif value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if not port.isdigit():
            return False
    else:
        host = value
    return _loopback_host(host)


@dataclass(frozen=True)
class DashboardScope:
    project_id: str
    auth_mode: str = "local"

    @property
    def fingerprint(self) -> str:
        material = json.dumps(
            {"auth_mode": self.auth_mode, "project_id": self.project_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {"project_id": self.project_id, "auth_mode": self.auth_mode}


@dataclass(frozen=True)
class DashboardSettings:
    enabled: bool
    explain_enabled: bool
    auth_mode: str
    project_id: str
    bind_host: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, Any] | None = None,
        *,
        bind_host: str = "127.0.0.1",
    ) -> DashboardSettings:
        env = os.environ if environ is None else environ
        enabled = _exact_one(env.get("PP_DASHBOARD_V2"))
        explain_enabled = _exact_one(env.get("PP_RETRIEVAL_EXPLAIN"))
        raw_auth_mode = str(env.get("PP_DASHBOARD_AUTH") or "local").strip().casefold()
        auth_mode = raw_auth_mode if raw_auth_mode in {"local", "token", "required"} else "local"

        if enabled:
            if raw_auth_mode not in {"local", "token", "required"}:
                raise DashboardConfigurationError("dashboard_auth_mode_invalid")
            if raw_auth_mode in {"token", "required"}:
                raise DashboardConfigurationError("dashboard_auth_backend_unavailable")
            if not _loopback_host(bind_host):
                raise DashboardConfigurationError("dashboard_local_bind_not_loopback")

        project_id = ""
        for key in ("PP_DASHBOARD_PROJECT_ID", "PLASTIC_PROJECT_ID", "PP_PROJECT_ID"):
            candidate = _normalize_project_id(env.get(key))
            if candidate:
                project_id = candidate
                break

        return cls(
            enabled=enabled,
            explain_enabled=enabled and explain_enabled,
            auth_mode=auth_mode,
            project_id=project_id,
            bind_host=str(bind_host or ""),
        )


def resolve_local_scope(
    settings: DashboardSettings,
    *,
    client_host: str,
    request_host: str | None = None,
    requested_project_id: str | None = None,
) -> DashboardScope:
    """Resolve one server-owned project; request input may only confirm it."""
    if not settings.enabled:
        raise DashboardAccessError(404, "dashboard_v2_disabled")
    if settings.auth_mode != "local":
        raise DashboardAccessError(503, "dashboard_auth_backend_unavailable")
    if not _loopback_host(client_host):
        raise DashboardAccessError(403, "dashboard_loopback_required")
    if request_host is not None and not _loopback_authority(request_host):
        raise DashboardAccessError(403, "dashboard_loopback_host_required")

    project_id = _normalize_project_id(settings.project_id)
    if not project_id or project_id == "project:unknown":
        raise DashboardAccessError(503, "dashboard_project_unavailable")

    if requested_project_id is not None:
        requested = _normalize_project_id(requested_project_id)
        if not requested or requested != project_id:
            raise DashboardAccessError(403, "dashboard_scope_denied")

    return DashboardScope(project_id=project_id, auth_mode="local")


__all__ = [
    "DashboardAccessError",
    "DashboardConfigurationError",
    "DashboardScope",
    "DashboardSettings",
    "resolve_local_scope",
]
