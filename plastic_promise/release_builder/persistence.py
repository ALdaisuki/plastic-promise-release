"""Bounded, atomic on-disk persistence for immutable release intent and receipts."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import ReleaseBuilderError, ReleaseConfirmation, ReleaseRequest

_RECEIPT_OUTCOMES = frozenset({"passed", "failed", "deferred"})
_HEX_SHA256_LENGTH = 64
_RECEIPT_PHASES = frozenset(
    {
        "resource-gate",
        "local-build",
        "ghcr-evidence",
        "server-deployment",
        "mcp-e2e",
        "sqlite-migration",
        "lancedb-promotion",
        "maintenance-start",
        "pypi-publication",
        "release-sync",
        "release-learning",
    }
)


def write_request(state_root: Path, request: ReleaseRequest) -> Path:
    """Persist one request at its immutable content-addressed path."""

    path = state_root / "requests" / f"{request.request_hash}.json"
    _write_immutable_json(path, request.to_mapping(), conflict="release_request_immutable_conflict")
    return path


def load_request(path: Path) -> ReleaseRequest:
    """Load a regular bounded request file and revalidate its full contract."""

    payload = _load_json_object(path, label="release_request")
    return ReleaseRequest.from_mapping(payload)


def write_confirmation(state_root: Path, confirmation: ReleaseConfirmation) -> Path:
    """Persist a confirmation separately from its request and without credentials."""

    if not _is_sha256(confirmation.request_hash):
        raise ReleaseBuilderError("release_confirmation_hash_invalid")
    payload = {
        "schema_version": "plastic-promise-release-confirmation/v1",
        "request_hash": confirmation.request_hash,
        "confirmed_at": _datetime_string(confirmation.confirmed_at),
        "expires_at": _datetime_string(confirmation.expires_at),
    }
    path = state_root / "confirmations" / f"{confirmation.request_hash}.json"
    _write_immutable_json(path, payload, conflict="release_confirmation_immutable_conflict")
    return path


def load_confirmation(path: Path) -> ReleaseConfirmation:
    """Load one confirmation and retain timezone-aware timestamps."""

    payload = _load_json_object(path, label="release_confirmation")
    if set(payload) != {"schema_version", "request_hash", "confirmed_at", "expires_at"}:
        raise ReleaseBuilderError("release_confirmation_schema_invalid")
    if payload["schema_version"] != "plastic-promise-release-confirmation/v1":
        raise ReleaseBuilderError("release_confirmation_schema_invalid")
    request_hash = payload["request_hash"]
    if not isinstance(request_hash, str) or not _is_sha256(request_hash):
        raise ReleaseBuilderError("release_confirmation_hash_invalid")
    confirmed_at = _parse_datetime(
        payload["confirmed_at"], code="release_confirmation_time_invalid"
    )
    expires_at = _parse_datetime(payload["expires_at"], code="release_confirmation_time_invalid")
    if expires_at < confirmed_at:
        raise ReleaseBuilderError("release_confirmation_time_invalid")
    return ReleaseConfirmation(request_hash, confirmed_at, expires_at)


def write_receipt(
    state_root: Path,
    *,
    request_hash: str,
    phase: str,
    attempt: int,
    outcome: str,
    created_at: datetime,
    evidence_hashes: Mapping[str, str],
    reason: str | None = None,
) -> Path:
    """Persist one immutable phase attempt; later attempts retain prior evidence."""

    if not _is_sha256(request_hash):
        raise ReleaseBuilderError("release_receipt_request_hash_invalid")
    if outcome not in _RECEIPT_OUTCOMES:
        raise ReleaseBuilderError("release_receipt_outcome_invalid")
    phase_value = str(phase)
    if phase_value not in _RECEIPT_PHASES:
        raise ReleaseBuilderError("release_receipt_phase_invalid")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ReleaseBuilderError("release_receipt_attempt_invalid")
    if reason is not None and (not isinstance(reason, str) or not reason or len(reason) > 128):
        raise ReleaseBuilderError("release_receipt_reason_invalid")
    normalized_evidence = _validate_evidence_hashes(evidence_hashes)
    payload: dict[str, Any] = {
        "schema_version": "plastic-promise-release-receipt/v1",
        "request_hash": request_hash,
        "phase": phase_value,
        "attempt": attempt,
        "outcome": outcome,
        "created_at": _datetime_string(created_at),
        "evidence_hashes": normalized_evidence,
        "reason": reason,
    }
    path = state_root / "receipts" / request_hash / phase_value / f"{attempt}.json"
    _write_immutable_json(path, payload, conflict="release_receipt_immutable_conflict")
    return path


def _validate_evidence_hashes(evidence_hashes: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(evidence_hashes, Mapping) or not evidence_hashes:
        raise ReleaseBuilderError("release_receipt_evidence_required")
    result: dict[str, str] = {}
    for label, value in evidence_hashes.items():
        if (
            not isinstance(label, str)
            or not label.replace("_", "").replace("-", "").isalnum()
            or not isinstance(value, str)
            or not _is_sha256(value)
        ):
            raise ReleaseBuilderError("release_receipt_evidence_invalid")
        result[label] = value
    return dict(sorted(result.items()))


def _write_immutable_json(path: Path, payload: Mapping[str, Any], *, conflict: str) -> None:
    encoded = _canonical_json(payload)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise ReleaseBuilderError("release_state_read_failed") from exc
        if existing != encoded:
            raise ReleaseBuilderError(conflict)
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != encoded:
                raise ReleaseBuilderError(conflict) from None
        else:
            temporary_path.unlink()
    except OSError as exc:
        raise ReleaseBuilderError("release_state_write_failed") from exc
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file():
        raise ReleaseBuilderError(f"{label}_not_regular_file")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ReleaseBuilderError(f"{label}_read_failed") from exc
    if not raw or len(raw) > 64 * 1024:
        raise ReleaseBuilderError(f"{label}_size_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuilderError(f"{label}_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ReleaseBuilderError(f"{label}_not_object")
    return payload


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _datetime_string(value: datetime) -> str:
    if value.tzinfo is None:
        raise ReleaseBuilderError("release_time_timezone_required")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, *, code: str) -> datetime:
    if not isinstance(value, str):
        raise ReleaseBuilderError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseBuilderError(code) from exc
    if parsed.tzinfo is None:
        raise ReleaseBuilderError(code)
    return parsed.astimezone(UTC)


def _is_sha256(value: str) -> bool:
    return len(value) == _HEX_SHA256_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )
