"""Immutable, secret-free contracts for maintainer release requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Any

_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STABLE_VERSION_RE = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "release_version",
        "source_commit",
        "source_channel",
        "source_archive_sha256",
        "actions",
    }
)
_ACTION_FIELDS = frozenset(
    {
        "push_ghcr_stable",
        "publish_pypi",
        "deploy_server",
        "sqlite_migration",
        "lancedb_promotion",
        "start_maintenance",
    }
)


class ReleaseBuilderError(ValueError):
    """Stable, operator-facing rejection code for release contract failures."""


class BuilderMode(StrEnum):
    DESKTOP_INTERACTIVE = "desktop-interactive"
    HEADLESS_BUILDER = "headless-builder"


@dataclass(frozen=True)
class ReleaseActions:
    """Explicit release operations; no action is inferred from another action."""

    push_ghcr_stable: bool = False
    publish_pypi: bool = False
    deploy_server: bool = False
    sqlite_migration: bool = False
    lancedb_promotion: bool = False
    # The maintainer explicitly selected default-on Maintenance after deploy.
    start_maintenance: bool = True

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> ReleaseActions:
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise ReleaseBuilderError("release_actions_invalid")
        unknown = set(payload) - _ACTION_FIELDS
        if unknown:
            raise ReleaseBuilderError("release_action_field_not_allowed")
        values: dict[str, bool] = {}
        for field in _ACTION_FIELDS:
            value = payload.get(field, getattr(cls(), field))
            if not isinstance(value, bool):
                raise ReleaseBuilderError("release_action_boolean_required")
            values[field] = value
        return cls(**values)

    def to_mapping(self) -> dict[str, bool]:
        return {
            "push_ghcr_stable": self.push_ghcr_stable,
            "publish_pypi": self.publish_pypi,
            "deploy_server": self.deploy_server,
            "sqlite_migration": self.sqlite_migration,
            "lancedb_promotion": self.lancedb_promotion,
            "start_maintenance": self.start_maintenance,
        }


@dataclass(frozen=True)
class ReleaseRequest:
    """Exact release intent that contains no secret, transport, or command data."""

    release_version: str
    source_commit: str
    source_channel: str
    source_archive_sha256: str | None = None
    actions: ReleaseActions = ReleaseActions()
    schema_version: str = "plastic-promise-release-request/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "plastic-promise-release-request/v1":
            raise ReleaseBuilderError("release_request_schema_invalid")
        if not isinstance(self.release_version, str) or not _STABLE_VERSION_RE.fullmatch(
            self.release_version
        ):
            raise ReleaseBuilderError("release_version_invalid")
        if not isinstance(self.source_commit, str) or not _SOURCE_COMMIT_RE.fullmatch(
            self.source_commit
        ):
            raise ReleaseBuilderError("release_source_commit_invalid")
        if self.source_channel not in {"github-commit", "verified-archive"}:
            raise ReleaseBuilderError("release_source_channel_invalid")
        if self.source_channel == "verified-archive":
            if not isinstance(self.source_archive_sha256, str) or not _SHA256_RE.fullmatch(
                self.source_archive_sha256
            ):
                raise ReleaseBuilderError("release_source_archive_sha256_required")
        elif self.source_archive_sha256 is not None:
            raise ReleaseBuilderError("release_source_archive_sha256_not_allowed")
        if not isinstance(self.actions, ReleaseActions):
            raise ReleaseBuilderError("release_actions_invalid")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReleaseRequest:
        if not isinstance(payload, Mapping):
            raise ReleaseBuilderError("release_request_invalid")
        unknown = set(payload) - _REQUEST_FIELDS
        if unknown:
            raise ReleaseBuilderError("release_request_field_not_allowed")
        return cls(
            schema_version=payload.get("schema_version", "plastic-promise-release-request/v1"),
            release_version=payload.get("release_version"),
            source_commit=payload.get("source_commit"),
            source_channel=payload.get("source_channel"),
            source_archive_sha256=payload.get("source_archive_sha256"),
            actions=ReleaseActions.from_mapping(payload.get("actions")),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "source_commit": self.source_commit,
            "source_channel": self.source_channel,
            "source_archive_sha256": self.source_archive_sha256,
            "actions": self.actions.to_mapping(),
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @property
    def request_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReleaseConfirmation:
    """Local confirmation bound to exactly one release-request hash."""

    request_hash: str
    confirmed_at: datetime
    expires_at: datetime


def confirm_request(request: ReleaseRequest, *, now: datetime | None = None) -> ReleaseConfirmation:
    """Issue the 30-minute confirmation token representation, without persistence."""

    confirmed_at = _normalise_utc(now or datetime.now(UTC))
    return ReleaseConfirmation(
        request_hash=request.request_hash,
        confirmed_at=confirmed_at,
        expires_at=confirmed_at + timedelta(minutes=30),
    )


def validate_confirmation(
    request: ReleaseRequest,
    confirmation: ReleaseConfirmation | None,
    *,
    mode: BuilderMode,
    now: datetime | None = None,
) -> None:
    """Fail closed before a stable action can use desktop credentials."""

    if mode is not BuilderMode.DESKTOP_INTERACTIVE:
        raise ReleaseBuilderError("release_credentials_interactive_required")
    if confirmation is None:
        raise ReleaseBuilderError("release_confirmation_required")
    if confirmation.request_hash != request.request_hash:
        raise ReleaseBuilderError("release_confirmation_hash_mismatch")
    current_time = _normalise_utc(now or datetime.now(UTC))
    if current_time > _normalise_utc(confirmation.expires_at):
        raise ReleaseBuilderError("release_confirmation_expired")


def validate_windows_source_root(source_root: str, source_commit: str) -> str:
    """Permit exactly the immutable, non-C-drive Builder workspace layout."""

    if not _SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise ReleaseBuilderError("release_source_commit_invalid")
    path = PureWindowsPath(source_root)
    expected = PureWindowsPath(r"D:\PlasticPromise\remote-builds") / source_commit / "source"
    if path.drive.casefold() != "d:" or path != expected:
        raise ReleaseBuilderError("release_source_workspace_invalid")
    return str(expected)


def _normalise_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ReleaseBuilderError("release_time_timezone_required")
    return value.astimezone(UTC)
