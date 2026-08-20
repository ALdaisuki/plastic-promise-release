"""Immutable revision store for the loopback-only configuration control plane."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from plastic_promise.control_plane.config_schema import (
    BOOTSTRAP_ONLY_ENV_NAMES,
    CONFIG_CONTRACT,
    ControlPlaneError,
    ControlPlaneValidationError,
    bootstrap_boundary_sha256,
    canonical_json,
    normalize_safe_config,
    prepare_configuration,
    runtime_embedding_index_identity,
    safe_config_from_environment,
    secret_values_from_environment,
)

_ROLE_RANK = {"viewer": 0, "operator": 1, "secret-admin": 2}
_ACTOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}\Z")
_REVISION_RE = re.compile(r"cfg-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\Z")
_EVIDENCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_MANAGED_ENV_TEMP_PREFIX = ".managed.env."
_UNBOUND_BOOTSTRAP_BOUNDARY = "sha256:" + "0" * 64
_UNBOUND_RUNTIME_EMBEDDING_INDEX_IDENTITY = "unbound:legacy-runtime-index-identity"


class ControlPlaneAuthorizationError(ControlPlaneError):
    """The authenticated role cannot perform this mutation."""

    def __init__(self, code: str = "control_role_insufficient") -> None:
        super().__init__(code, status_code=403)


class ControlPlaneConflictError(ControlPlaneError):
    """A compare-and-swap or idempotency invariant was violated."""

    def __init__(self, code: str) -> None:
        super().__init__(code, status_code=409)


class ControlPlaneNotFoundError(ControlPlaneError):
    """A requested immutable revision does not exist."""

    def __init__(self, code: str = "control_revision_not_found") -> None:
        super().__init__(code, status_code=404)


class ControlPlanePreconditionError(ControlPlaneError):
    """A required If-Match or Idempotency-Key precondition is absent or stale."""

    def __init__(self, code: str, *, status_code: int = 428) -> None:
        super().__init__(code, status_code=status_code)


class ControlPlaneStorageError(ControlPlaneError):
    """Private revision material is unavailable or failed integrity checks."""

    def __init__(self, code: str) -> None:
        super().__init__(code, status_code=503)


@dataclass(frozen=True)
class SafeConfigSnapshot:
    contract_version: str
    revision_id: str | None
    source: str
    etag: str
    config: dict[str, object]
    secrets: dict[str, bool]
    embedding_identity: str
    desired_generation_id: str | None = None
    desired_generation_manifest_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return _json_copy(
            {
                "contract_version": self.contract_version,
                "revision_id": self.revision_id,
                "active_revision_id": self.revision_id,
                "source": self.source,
                "etag": self.etag,
                "config": self.config,
                "secrets": self.secrets,
                "embedding_identity": self.embedding_identity,
                "desired_generation_id": self.desired_generation_id,
                "desired_generation_manifest_sha256": self.desired_generation_manifest_sha256,
            }
        )


@dataclass(frozen=True)
class ValidationResult:
    contract_version: str
    current_etag: str
    config: dict[str, object]
    secrets: dict[str, bool]
    embedding_identity: str
    runtime_embedding_index_identity: str
    requires_embedding_evidence: bool

    def to_dict(self) -> dict[str, object]:
        return _json_copy(
            {
                "contract_version": self.contract_version,
                "current_etag": self.current_etag,
                "config": self.config,
                "secrets": self.secrets,
                "embedding_identity": self.embedding_identity,
                "runtime_embedding_index_identity": self.runtime_embedding_index_identity,
                "requires_embedding_evidence": self.requires_embedding_evidence,
                "valid": True,
            }
        )


@dataclass(frozen=True)
class ConfigRevision:
    contract_version: str
    revision_id: str
    created_at: str
    actor: str
    base_etag: str
    etag: str
    config: dict[str, object]
    secrets: dict[str, bool]
    embedding_identity: str
    runtime_embedding_index_identity: str
    requires_embedding_evidence: bool
    contains_secret_changes: bool = True

    def to_dict(self) -> dict[str, object]:
        return _json_copy(
            {
                "contract_version": self.contract_version,
                "revision_id": self.revision_id,
                "created_at": self.created_at,
                "actor": self.actor,
                "base_etag": self.base_etag,
                "etag": self.etag,
                "config": self.config,
                "secrets": self.secrets,
                "embedding_identity": self.embedding_identity,
                "runtime_embedding_index_identity": self.runtime_embedding_index_identity,
                "requires_embedding_evidence": self.requires_embedding_evidence,
                "contains_secret_changes": self.contains_secret_changes,
                "status": "staged",
            }
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        environ: Mapping[str, object] | None = None,
    ) -> ConfigRevision:
        config = normalize_safe_config(_json_object(payload["config"]))
        runtime_identity = payload.get("runtime_embedding_index_identity")
        if runtime_identity is None:
            runtime_identity = _UNBOUND_RUNTIME_EMBEDDING_INDEX_IDENTITY
        return cls(
            contract_version=str(payload["contract_version"]),
            revision_id=str(payload["revision_id"]),
            created_at=str(payload["created_at"]),
            actor=str(payload["actor"]),
            base_etag=str(payload["base_etag"]),
            etag=str(payload["etag"]),
            config=config,
            secrets=_bool_object(payload["secrets"]),
            embedding_identity=str(payload["embedding_identity"]),
            runtime_embedding_index_identity=_required_text(
                runtime_identity,
                "control_metadata_invalid",
            ),
            requires_embedding_evidence=bool(payload["requires_embedding_evidence"]),
            contains_secret_changes=bool(payload.get("contains_secret_changes", True)),
        )


@dataclass(frozen=True)
class ActivationResult:
    contract_version: str
    revision_id: str
    previous_revision_id: str | None
    activated_at: str
    actor: str
    etag: str
    embedding_identity: str
    desired_generation_id: str | None = None
    desired_generation_manifest_sha256: str | None = None
    restart_required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "revision_id": self.revision_id,
            "previous_revision_id": self.previous_revision_id,
            "activated_at": self.activated_at,
            "actor": self.actor,
            "etag": self.etag,
            "embedding_identity": self.embedding_identity,
            "desired_generation_id": self.desired_generation_id,
            "desired_generation_manifest_sha256": self.desired_generation_manifest_sha256,
            "restart_required": self.restart_required,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ActivationResult:
        previous = payload.get("previous_revision_id")
        return cls(
            contract_version=str(payload["contract_version"]),
            revision_id=str(payload["revision_id"]),
            previous_revision_id=None if previous is None else str(previous),
            activated_at=str(payload["activated_at"]),
            actor=str(payload["actor"]),
            etag=str(payload["etag"]),
            embedding_identity=str(payload["embedding_identity"]),
            desired_generation_id=_optional_text(payload.get("desired_generation_id")),
            desired_generation_manifest_sha256=_optional_text(
                payload.get("desired_generation_manifest_sha256")
            ),
            restart_required=bool(payload["restart_required"]),
        )


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    occurred_at: str
    action: str
    actor: str
    role: str
    revision_id: str | None
    details: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return _json_copy(
            {
                "sequence": self.sequence,
                "occurred_at": self.occurred_at,
                "action": self.action,
                "actor": self.actor,
                "role": self.role,
                "revision_id": self.revision_id,
                "details": self.details,
            }
        )


class ControlPlaneConfigStore:
    """Store immutable revisions and atomically activate one managed EnvironmentFile.

    Construction initializes private metadata.  Read methods perform only
    read-only SQLite statements and never make provider calls or update access
    timestamps.
    """

    def __init__(
        self,
        root: str | Path,
        base_env: Mapping[str, object] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        generation_evidence_verifier: Callable[
            [ConfigRevision, Mapping[str, object] | None], Mapping[str, object]
        ]
        | None = None,
        _read_only_existing: bool = False,
    ) -> None:
        self.root = Path(root).expanduser()
        self.revisions_dir = self.root / "revisions"
        self.compute_revisions_dir = self.root / "compute-revisions"
        self.database_path = self.root / "control-plane.sqlite3"
        self.managed_env_path = self.root / "managed.env"
        self.compute_managed_env_path = self.root / "compute.managed.env"
        self._activation_lock_path = self.root / ".activation.lock"
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._generation_evidence_verifier = (
            generation_evidence_verifier or self._verify_generation_evidence
        )
        self._thread_lock = threading.RLock()
        if _read_only_existing:
            self._validate_existing_readonly()
            return
        self._initialize()
        with self._thread_lock, self._activation_file_lock():
            self._recover_activation_locked()

    @classmethod
    def open_existing_readonly(
        cls,
        root: str | Path,
        base_env: Mapping[str, object] | None = None,
    ) -> ControlPlaneConfigStore:
        """Open existing control metadata without creating or recovering state.

        Runtime bootstrap needs only the active safe revision.  It must never
        create control-plane files, complete a pending activation, or repair a
        deployment implicitly while constructing an inference-node runtime.
        """

        return cls(root, base_env, _read_only_existing=True)

    def safe_config(self) -> SafeConfigSnapshot:
        """Return the active safe config without touching provider or mutable state."""

        with self._connect(read_only=True) as connection:
            return self._current_snapshot(connection)

    def compute_profile_digest(self, revision_id: str | None = None) -> str:
        """Return a secret-free digest for the active private compute profile."""

        with self._connect(read_only=True) as connection:
            current = self._current_snapshot(connection)
            selected = revision_id or current.revision_id
            if not isinstance(selected, str) or not selected:
                raise ControlPlaneStorageError("control_compute_profile_unavailable")
            row = self._revision_row(connection, selected)
            content = self._verified_compute_revision_content(row)
        return "sha256:" + hashlib.sha256(content).hexdigest()

    def validate(
        self,
        candidate: Mapping[str, object],
        secret_ops: Mapping[str, object] | None = None,
        *,
        expected_etag: str | None = None,
    ) -> ValidationResult:
        """Validate a patch and secret operations without persisting anything."""

        with self._connect(read_only=True) as connection:
            current = self._current_snapshot(connection)
            if expected_etag is not None:
                _compare_etag(_required_etag(expected_etag), current.etag)
            values = self._current_secret_values(connection, current)
            current_runtime_index_identity = self._current_runtime_embedding_index_identity(
                connection
            )
        prepared = prepare_configuration(current.config, values, candidate, secret_ops)
        runtime_index_identity = runtime_embedding_index_identity(
            prepared.safe_config,
            self._base_env,
        )
        return ValidationResult(
            contract_version=CONFIG_CONTRACT,
            current_etag=current.etag,
            config=_json_object(prepared.safe_config),
            secrets=dict(prepared.secret_state),
            embedding_identity=prepared.embedding_identity,
            runtime_embedding_index_identity=runtime_index_identity,
            requires_embedding_evidence=(runtime_index_identity != current_runtime_index_identity),
        )

    def stage(
        self,
        candidate: Mapping[str, object],
        secret_ops: Mapping[str, object] | None = None,
        *,
        expected_etag: str,
        idempotency_key: str,
        actor: str,
        role: str,
    ) -> ConfigRevision:
        """Persist one immutable private revision under ETag and idempotency CAS."""

        _require_role(actor, role, "operator")
        if secret_ops:
            _require_role(actor, role, "secret-admin")
        expected = _required_etag(expected_etag)
        key_hash = _idempotency_key_hash(idempotency_key)
        request_hash = _request_hash(
            {
                "operation": "stage",
                "candidate": candidate,
                "secret_ops": secret_ops or {},
                "expected_etag": expected,
                "actor": actor,
                "role": role,
            }
        )
        created_path: Path | None = None
        with self._thread_lock, self._activation_file_lock():
            self._recover_activation_locked()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    replay = self._idempotency_replay(
                        connection,
                        operation="stage",
                        key_hash=key_hash,
                        request_hash=request_hash,
                    )
                    if replay is not None:
                        replay_revision_id = _stored_revision_id(replay.get("revision_id"))
                        replay_row = self._revision_row(connection, replay_revision_id)
                        replay = {
                            **replay,
                            "runtime_embedding_index_identity": replay_row[
                                "runtime_embedding_index_identity"
                            ],
                            "requires_embedding_evidence": bool(
                                replay_row["requires_embedding_evidence"]
                            ),
                            "contains_secret_changes": _row_contains_secret_changes(replay_row),
                        }
                        connection.commit()
                        return ConfigRevision.from_dict(replay, environ=self._base_env)

                    current = self._current_snapshot(connection)
                    _compare_etag(expected, current.etag)
                    if current.revision_id is not None:
                        current_row = self._revision_row(connection, current.revision_id)
                        if _row_secret_metadata_unbound(current_row):
                            # A legacy active revision can carry private values
                            # whose change metadata was never recorded. Do not
                            # let an operator copy those values into a newly
                            # bound revision by staging an otherwise safe patch.
                            _require_role(actor, role, "secret-admin")
                    values = self._current_secret_values(connection, current)
                    prepared = prepare_configuration(current.config, values, candidate, secret_ops)
                    revision_id = self._new_revision_id()
                    created_at = self._now()
                    target_etag = _opaque_etag()
                    env_content = _environment_file(revision_id, prepared.environment)
                    env_sha256 = _bytes_sha256(env_content)
                    created_path = self._write_new_revision(revision_id, env_content)
                    self._write_compute_revision(
                        revision_id,
                        _environment_file(revision_id, prepared.compute_environment),
                    )
                    runtime_index_identity = runtime_embedding_index_identity(
                        prepared.safe_config,
                        self._base_env,
                    )
                    current_runtime_index_identity = self._current_runtime_embedding_index_identity(
                        connection
                    )
                    requires_evidence = runtime_index_identity != current_runtime_index_identity
                    bootstrap_boundary = bootstrap_boundary_sha256(self._base_env)
                    revision = ConfigRevision(
                        contract_version=CONFIG_CONTRACT,
                        revision_id=revision_id,
                        created_at=created_at,
                        actor=actor,
                        base_etag=current.etag,
                        etag=target_etag,
                        config=_json_object(prepared.safe_config),
                        secrets=dict(prepared.secret_state),
                        embedding_identity=prepared.embedding_identity,
                        runtime_embedding_index_identity=runtime_index_identity,
                        requires_embedding_evidence=requires_evidence,
                        contains_secret_changes=bool(prepared.secret_operations),
                    )
                    connection.execute(
                        """
                        INSERT INTO revisions(
                            revision_id, created_at, actor, role, base_etag, target_etag,
                            safe_json, secret_state_json, embedding_identity,
                            runtime_embedding_index_identity,
                            bootstrap_boundary_sha256,
                            requires_embedding_evidence, contains_secret_changes,
                            secret_change_metadata_bound, env_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            created_at,
                            actor,
                            role,
                            current.etag,
                            target_etag,
                            canonical_json(prepared.safe_config),
                            canonical_json(prepared.secret_state),
                            prepared.embedding_identity,
                            runtime_index_identity,
                            bootstrap_boundary,
                            int(requires_evidence),
                            int(bool(prepared.secret_operations)),
                            1,
                            env_sha256,
                        ),
                    )
                    public_result = revision.to_dict()
                    self._store_idempotency(
                        connection,
                        operation="stage",
                        key_hash=key_hash,
                        request_hash=request_hash,
                        result=public_result,
                        created_at=created_at,
                    )
                    self._append_audit(
                        connection,
                        occurred_at=created_at,
                        action="config.stage",
                        actor=actor,
                        role=role,
                        revision_id=revision_id,
                        details={
                            "base_etag": current.etag,
                            "target_etag": target_etag,
                            "embedding_identity_changed": (
                                prepared.embedding_identity != current.embedding_identity
                            ),
                            "runtime_embedding_index_identity_changed": requires_evidence,
                            "secret_fields_changed": sorted(prepared.secret_operations),
                        },
                    )
                    connection.commit()
                    return revision
                except BaseException:
                    connection.rollback()
                    if created_path is not None:
                        created_path.unlink(missing_ok=True)
                    raise

    def get_revision(self, revision_id: str) -> ConfigRevision:
        """Read one immutable revision without reading its private EnvironmentFile."""

        _validate_revision_id(revision_id)
        with self._connect(read_only=True) as connection:
            row = self._revision_row(connection, revision_id)
        return _revision_from_row(row)

    def list_revisions(self, limit: int = 100) -> tuple[ConfigRevision, ...]:
        """List recent immutable revisions without any read-side writes."""

        bounded = _bounded_limit(limit)
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM revisions ORDER BY created_at DESC, revision_id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return tuple(_revision_from_row(row) for row in rows)

    def activate(
        self,
        revision_id: str,
        *,
        expected_etag: str,
        idempotency_key: str,
        actor: str,
        role: str,
        evidence: Mapping[str, object] | None = None,
    ) -> ActivationResult:
        """Atomically replace managed.env and return restart_required.

        This method intentionally has no service-manager integration.  The
        caller must arrange a separately authorized restart after activation.
        """

        _require_role(actor, role, "operator")
        _validate_revision_id(revision_id)
        expected = _required_etag(expected_etag)
        key_hash = _idempotency_key_hash(idempotency_key)
        request_hash = _request_hash(
            {
                "operation": "activate",
                "revision_id": revision_id,
                "expected_etag": expected,
                "evidence": evidence,
                "actor": actor,
                "role": role,
            }
        )
        with self._thread_lock, self._activation_file_lock():
            self._recover_activation_locked()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    replay = self._idempotency_replay(
                        connection,
                        operation="activate",
                        key_hash=key_hash,
                        request_hash=request_hash,
                    )
                    if replay is not None:
                        connection.commit()
                        return ActivationResult.from_dict(replay)

                    current = self._current_snapshot(connection)
                    _compare_etag(expected, current.etag)
                    row = self._revision_row(connection, revision_id)
                    revision = _revision_from_row(row)
                    if revision.contains_secret_changes:
                        _require_role(actor, role, "secret-admin")
                    if revision.base_etag != current.etag:
                        raise ControlPlaneConflictError("control_revision_stale")
                    self._require_current_bootstrap_boundary(row)
                    runtime_index_identity_changed = (
                        revision.runtime_embedding_index_identity
                        != self._current_runtime_embedding_index_identity(connection)
                    )
                    evidence_summary: dict[str, object] = {}
                    if runtime_index_identity_changed:
                        evidence_summary = _validated_evidence_summary(
                            self._generation_evidence_verifier(revision, evidence)
                        )
                    elif evidence is not None:
                        raise ControlPlaneValidationError(
                            "control_embedding_evidence_not_applicable"
                        )

                    private_content = self._verified_revision_content(row)
                    activated_at = self._now()
                    state = self._state_row(connection)
                    previous_revision = state["active_revision_id"]
                    desired_generation_id = current.desired_generation_id
                    desired_manifest_sha256 = current.desired_generation_manifest_sha256
                    if runtime_index_identity_changed:
                        desired_generation_id = _required_text(
                            evidence_summary.get("shadow_generation_id"),
                            "control_embedding_evidence_verifier_invalid",
                        )
                        desired_manifest_sha256 = _required_sha256(
                            evidence_summary.get("manifest_sha256"),
                            "control_embedding_evidence_verifier_invalid",
                        )
                    result = ActivationResult(
                        contract_version=CONFIG_CONTRACT,
                        revision_id=revision_id,
                        previous_revision_id=previous_revision,
                        activated_at=activated_at,
                        actor=actor,
                        etag=revision.etag,
                        embedding_identity=revision.embedding_identity,
                        desired_generation_id=desired_generation_id,
                        desired_generation_manifest_sha256=desired_manifest_sha256,
                        restart_required=True,
                    )
                    audit_details = {
                        "previous_revision_id": previous_revision,
                        "embedding_identity_changed": (
                            revision.embedding_identity != current.embedding_identity
                        ),
                        "runtime_embedding_index_identity_changed": (
                            runtime_index_identity_changed
                        ),
                        "evidence": evidence_summary,
                        "desired_generation_id": desired_generation_id,
                        "desired_generation_manifest_sha256": desired_manifest_sha256,
                        "restart_required": True,
                    }
                    connection.execute(
                        """
                        INSERT INTO activation_intent(
                            singleton, revision_id, previous_revision_id, previous_etag,
                            target_etag, target_env_sha256, key_hash, request_hash,
                            actor, role, prepared_at, audit_details_json, result_json
                        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            previous_revision,
                            current.etag,
                            revision.etag,
                            str(row["env_sha256"]),
                            key_hash,
                            request_hash,
                            actor,
                            role,
                            activated_at,
                            canonical_json(audit_details),
                            canonical_json(result.to_dict()),
                        ),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

            self._activation_checkpoint("intent_committed")
            self._atomic_write_managed_env(private_content)
            self._atomic_write_compute_env(self._verified_compute_revision_content(row))
            return self._finalize_activation_locked(recovered=False)

    def retarget_current_generation(
        self,
        generation_id: object,
        *,
        manifest_sha256: object,
        expected_etag: str,
        idempotency_key: str,
        actor: str,
        role: str,
    ) -> dict[str, object]:
        """Bind desired state to the already-promoted, verified generation.

        Rerank and other runtime-only revisions do not change the embedding
        index identity, so normal config activation intentionally preserves
        the previous desired generation.  Host promotion is a separate
        operation; this endpoint closes that gap without editing control
        SQLite out of band.  It only accepts the verified current generation,
        checks its manifest and runtime embedding identity, and records an
        auditable CAS/idempotent state transition.
        """

        _require_role(actor, role, "operator")
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ControlPlaneValidationError("control_generation_id_invalid")
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,255}", generation_id):
            raise ControlPlaneValidationError("control_generation_id_invalid")
        expected = _required_etag(expected_etag)
        manifest = _required_sha256(manifest_sha256, "control_manifest_sha256_invalid")
        key_hash = _idempotency_key_hash(idempotency_key)
        request_hash = _request_hash(
            {
                "operation": "generation_retarget",
                "generation_id": generation_id,
                "manifest_sha256": manifest,
                "expected_etag": expected,
                "actor": actor,
                "role": role,
            }
        )

        with self._thread_lock, self._activation_file_lock():
            self._recover_activation_locked()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    replay = self._idempotency_replay(
                        connection,
                        operation="generation_retarget",
                        key_hash=key_hash,
                        request_hash=request_hash,
                    )
                    if replay is not None:
                        connection.commit()
                        return replay

                    current = self._current_snapshot(connection)
                    _compare_etag(expected, current.etag)
                    raw_root = str(
                        self._base_env.get("PLASTIC_LANCEDB_GENERATION_ROOT") or ""
                    ).strip()
                    if not raw_root or "\x00" in raw_root:
                        raise ControlPlanePreconditionError("control_generation_unavailable")
                    try:
                        from plastic_promise.core.lancedb_artifact import verify_lancedb_artifact
                        from plastic_promise.core.lancedb_generation import GenerationManager

                        with GenerationManager(
                            Path(raw_root).expanduser(),
                            create=False,
                            artifact_verifier=verify_lancedb_artifact,
                        ) as manager:
                            selected, _index_path, _selection = (
                                manager.resolve_verified_current_selection()
                            )
                    except Exception as exc:
                        raise ControlPlanePreconditionError(
                            "control_generation_unavailable"
                        ) from exc

                    if selected.generation_id != generation_id:
                        raise ControlPlaneConflictError("control_generation_not_current")
                    if selected.manifest_sha256 != manifest:
                        raise ControlPlaneConflictError("control_generation_manifest_mismatch")
                    expected_identity = self._current_runtime_embedding_index_identity(connection)
                    if selected.spec.embedding_index_identity != expected_identity:
                        raise ControlPlaneConflictError("control_generation_identity_mismatch")

                    quality = selected.quality_report
                    gate = quality.get("gate") if isinstance(quality, Mapping) else None
                    smoke = quality.get("smoke") if isinstance(quality, Mapping) else None
                    backend = quality.get("backend") if isinstance(quality, Mapping) else None
                    if (
                        not isinstance(gate, Mapping)
                        or gate.get("status") != "pass"
                        or not isinstance(smoke, Mapping)
                        or smoke.get("passed") is not True
                        or not isinstance(backend, Mapping)
                        or backend.get("fallback_used") is not False
                        or backend.get("degraded_used") is not False
                    ):
                        raise ControlPlaneConflictError("control_generation_quality_mismatch")

                    state = self._state_row(connection)
                    next_etag = _opaque_etag()
                    now = self._now()
                    result: dict[str, object] = {
                        "contract_version": CONFIG_CONTRACT,
                        "active_revision_id": _optional_text(state["active_revision_id"]),
                        "previous_generation_id": _optional_text(state["desired_generation_id"]),
                        "previous_generation_manifest_sha256": _optional_text(
                            state["desired_generation_manifest_sha256"]
                        ),
                        "desired_generation_id": generation_id,
                        "desired_generation_manifest_sha256": manifest,
                        "etag": next_etag,
                        "restart_required": False,
                        "retargeted_at": now,
                        "actor": actor,
                    }
                    connection.execute(
                        """
                        UPDATE control_state
                        SET etag = ?, desired_generation_id = ?,
                            desired_generation_manifest_sha256 = ?
                        WHERE singleton = 1
                        """,
                        (next_etag, generation_id, manifest),
                    )
                    self._store_idempotency(
                        connection,
                        operation="generation_retarget",
                        key_hash=key_hash,
                        request_hash=request_hash,
                        result=result,
                        created_at=now,
                    )
                    self._append_audit(
                        connection,
                        occurred_at=now,
                        action="config.generation_retarget",
                        actor=actor,
                        role=role,
                        revision_id=_optional_text(state["active_revision_id"]),
                        details={
                            "previous_generation_id": result["previous_generation_id"],
                            "previous_generation_manifest_sha256": result[
                                "previous_generation_manifest_sha256"
                            ],
                            "desired_generation_id": generation_id,
                            "desired_generation_manifest_sha256": manifest,
                            "verified_current": True,
                            "restart_required": False,
                        },
                    )
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise

    def audit(self, limit: int = 100) -> tuple[AuditEvent, ...]:
        """Return recent mutation audit events with no read-side mutation."""

        bounded = _bounded_limit(limit)
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT sequence, occurred_at, action, actor, role, revision_id, details_json
                FROM audit_events ORDER BY sequence DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return tuple(
            AuditEvent(
                sequence=int(row["sequence"]),
                occurred_at=str(row["occurred_at"]),
                action=str(row["action"]),
                actor=str(row["actor"]),
                role=str(row["role"]),
                revision_id=row["revision_id"],
                details=_json_object(json.loads(row["details_json"])),
            )
            for row in rows
        )

    def _initialize(self) -> None:
        _private_directory(self.root)
        _private_directory(self.revisions_dir)
        _private_directory(self.compute_revisions_dir)
        _private_file(self._activation_lock_path)
        if self.database_path.is_symlink():
            raise ControlPlaneStorageError("control_metadata_path_unsafe")

        prepared = None
        base_etag = None
        if not self._existing_control_state():
            base_config = safe_config_from_environment(self._base_env)
            base_values = secret_values_from_environment(self._base_env)
            prepared = prepare_configuration(base_config, base_values, {}, {})
            base_etag = _opaque_etag()

        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS control_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    active_revision_id TEXT,
                    etag TEXT NOT NULL,
                    base_safe_json TEXT NOT NULL,
                    base_secret_state_json TEXT NOT NULL,
                    base_embedding_identity TEXT NOT NULL,
                    desired_generation_id TEXT,
                    desired_generation_manifest_sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    revision_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    base_etag TEXT NOT NULL,
                    target_etag TEXT NOT NULL,
                    safe_json TEXT NOT NULL,
                    secret_state_json TEXT NOT NULL,
                    embedding_identity TEXT NOT NULL,
                    runtime_embedding_index_identity TEXT NOT NULL,
                    bootstrap_boundary_sha256 TEXT NOT NULL,
                    requires_embedding_evidence INTEGER NOT NULL CHECK (
                        requires_embedding_evidence IN (0, 1)
                    ),
                    contains_secret_changes INTEGER NOT NULL DEFAULT 1 CHECK (
                        contains_secret_changes IN (0, 1)
                    ),
                    secret_change_metadata_bound INTEGER NOT NULL DEFAULT 1 CHECK (
                        secret_change_metadata_bound IN (0, 1)
                    ),
                    env_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activations (
                    activation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activated_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
                    previous_revision_id TEXT,
                    etag TEXT NOT NULL,
                    desired_generation_id TEXT,
                    desired_generation_manifest_sha256 TEXT,
                    restart_required INTEGER NOT NULL CHECK (restart_required = 1)
                );
                CREATE TABLE IF NOT EXISTS activation_intent (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
                    previous_revision_id TEXT REFERENCES revisions(revision_id),
                    previous_etag TEXT NOT NULL,
                    target_etag TEXT NOT NULL,
                    target_env_sha256 TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('operator', 'secret-admin')),
                    prepared_at TEXT NOT NULL,
                    audit_details_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    operation TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (operation, key_hash)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    revision_id TEXT,
                    details_json TEXT NOT NULL
                );
                """
            )
            revision_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(revisions)")
            }
            if "contains_secret_changes" not in revision_columns:
                connection.execute(
                    "ALTER TABLE revisions ADD COLUMN "
                    "contains_secret_changes INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (contains_secret_changes IN (0, 1))"
                )
            if "secret_change_metadata_bound" not in revision_columns:
                connection.execute(
                    "ALTER TABLE revisions ADD COLUMN "
                    "secret_change_metadata_bound INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (secret_change_metadata_bound IN (0, 1))"
                )
            if "runtime_embedding_index_identity" not in revision_columns:
                connection.execute(
                    "ALTER TABLE revisions ADD COLUMN runtime_embedding_index_identity TEXT"
                )
            if "bootstrap_boundary_sha256" not in revision_columns:
                connection.execute(
                    "ALTER TABLE revisions ADD COLUMN bootstrap_boundary_sha256 TEXT"
                )
            legacy_revisions = connection.execute(
                """
                SELECT revision_id, safe_json FROM revisions
                WHERE runtime_embedding_index_identity IS NULL
                   OR runtime_embedding_index_identity = ''
                """
            ).fetchall()
            for legacy_revision in legacy_revisions:
                connection.execute(
                    """
                    UPDATE revisions SET runtime_embedding_index_identity = ?
                    WHERE revision_id = ?
                    """,
                    (
                        _UNBOUND_RUNTIME_EMBEDDING_INDEX_IDENTITY,
                        legacy_revision["revision_id"],
                    ),
                )
            legacy_boundaries = connection.execute(
                """
                SELECT revision_id FROM revisions
                WHERE bootstrap_boundary_sha256 IS NULL
                   OR bootstrap_boundary_sha256 = ''
                """
            ).fetchall()
            for legacy_revision in legacy_boundaries:
                connection.execute(
                    """
                    UPDATE revisions SET bootstrap_boundary_sha256 = ?
                    WHERE revision_id = ?
                    """,
                    (_UNBOUND_BOOTSTRAP_BOUNDARY, legacy_revision["revision_id"]),
                )
            state_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(control_state)")
            }
            if "desired_generation_id" not in state_columns:
                connection.execute(
                    "ALTER TABLE control_state ADD COLUMN desired_generation_id TEXT"
                )
            if "desired_generation_manifest_sha256" not in state_columns:
                connection.execute(
                    "ALTER TABLE control_state ADD COLUMN desired_generation_manifest_sha256 TEXT"
                )
            activation_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(activations)")
            }
            if "desired_generation_id" not in activation_columns:
                connection.execute("ALTER TABLE activations ADD COLUMN desired_generation_id TEXT")
            if "desired_generation_manifest_sha256" not in activation_columns:
                connection.execute(
                    "ALTER TABLE activations ADD COLUMN desired_generation_manifest_sha256 TEXT"
                )
            state_exists = connection.execute(
                "SELECT 1 FROM control_state WHERE singleton = 1"
            ).fetchone()
            if state_exists is None:
                # Only a brand-new control store may derive its base safe
                # configuration from the process environment.  Once the
                # canonical state exists, the server-only managed projection
                # is intentionally incomplete (provider fields are owned by
                # pp-compute-node) and must never be reparsed as a full safe
                # configuration during restart.
                if prepared is None or base_etag is None:
                    raise ControlPlaneStorageError("control_state_missing")
                connection.execute(
                    """
                    INSERT INTO control_state(
                        singleton, active_revision_id, etag, base_safe_json,
                        base_secret_state_json, base_embedding_identity
                    ) VALUES (1, NULL, ?, ?, ?, ?)
                    """,
                    (
                        base_etag,
                        canonical_json(prepared.safe_config),
                        canonical_json(prepared.secret_state),
                        prepared.embedding_identity,
                    ),
                )
            connection.commit()
        os.chmod(self.database_path, 0o600)

    def _existing_control_state(self) -> bool:
        """Check the durable initialization marker without mutating SQLite."""

        if not self.database_path.is_file():
            return False
        try:
            uri = f"file:{self.database_path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'control_state'"
                ).fetchone()
                if table is None:
                    return False
                return (
                    connection.execute("SELECT 1 FROM control_state WHERE singleton = 1").fetchone()
                    is not None
                )
        except sqlite3.Error as exc:
            raise ControlPlaneStorageError("control_metadata_invalid") from exc

    def _validate_existing_readonly(self) -> None:
        if self.root.is_symlink() or self.database_path.is_symlink():
            raise ControlPlaneStorageError("control_metadata_path_unsafe")
        if not self.root.is_dir() or not self.database_path.is_file():
            raise ControlPlaneStorageError("control_metadata_unavailable")

    def _recover_activation_locked(self) -> None:
        """Roll forward a durable activation intent before serving or mutating."""

        self._retire_managed_env_temporaries_locked()
        with self._connect() as connection:
            intent = self._activation_intent_row(connection)
            state = self._state_row(connection)
            if intent is None:
                self._verify_committed_managed_env_locked(connection, state)
                self._retire_unusable_revision_material_locked(connection, state)
                return

            revision_id, previous_revision_id = self._validate_intent_state(
                connection, intent, state
            )
            target_row = self._revision_row(connection, revision_id)
            target_content = self._verified_revision_content(target_row)
            target_compute_content = self._verified_compute_revision_content(target_row)
            observed = self._managed_env_state()
            if not self._managed_state_matches(observed, target_row):
                if previous_revision_id is None:
                    previous_matches = observed is None
                else:
                    previous_row = self._revision_row(connection, previous_revision_id)
                    previous_matches = self._managed_state_matches(observed, previous_row)
                if not previous_matches:
                    raise ControlPlaneStorageError("control_activation_recovery_conflict")
                self._atomic_write_managed_env(target_content)
            else:
                # A crash may have happened after rename but before directory fsync.
                _fsync_directory(self.root)

            self._atomic_write_compute_env(target_compute_content)

        self._finalize_activation_locked(recovered=True)

    def _retire_managed_env_temporaries_locked(self) -> None:
        """Remove non-authoritative secret files left by a hard process crash."""

        try:
            entries = sorted(self.root.iterdir(), key=lambda path: path.name)
        except OSError:
            raise ControlPlaneStorageError("control_managed_env_temp_retirement_failed") from None
        retirement_paths = [
            path for path in entries if path.name.startswith(_MANAGED_ENV_TEMP_PREFIX)
        ]
        removed = False
        for path in retirement_paths:
            if not path.is_symlink() and not path.is_file():
                raise ControlPlaneStorageError("control_managed_env_temp_path_unsafe")
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                raise ControlPlaneStorageError(
                    "control_managed_env_temp_retirement_failed"
                ) from None
            removed = True
        if removed:
            try:
                _fsync_directory(self.root)
            except OSError:
                raise ControlPlaneStorageError(
                    "control_managed_env_temp_retirement_failed"
                ) from None

    def _finalize_activation_locked(self, *, recovered: bool) -> ActivationResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                intent = self._activation_intent_row(connection)
                if intent is None:
                    raise ControlPlaneStorageError("control_activation_intent_missing")
                state = self._state_row(connection)
                revision_id, _previous_revision_id = self._validate_intent_state(
                    connection, intent, state
                )
                target_row = self._revision_row(connection, revision_id)
                self._verified_revision_content(target_row)
                if not self._managed_state_matches(self._managed_env_state(), target_row):
                    raise ControlPlaneStorageError("control_managed_env_integrity_failed")

                result_payload = _json_object(json.loads(str(intent["result_json"])))
                result = ActivationResult.from_dict(result_payload)
                audit_details = _json_object(json.loads(str(intent["audit_details_json"])))
                self._validate_intent_result(intent, target_row, result)
                if (
                    audit_details.get("desired_generation_id") != result.desired_generation_id
                    or audit_details.get("desired_generation_manifest_sha256")
                    != result.desired_generation_manifest_sha256
                    or audit_details.get("restart_required") is not True
                ):
                    raise ControlPlaneStorageError("control_activation_intent_invalid")
                connection.execute(
                    """
                    UPDATE control_state
                    SET active_revision_id = ?, etag = ?, desired_generation_id = ?,
                        desired_generation_manifest_sha256 = ?
                    WHERE singleton = 1
                    """,
                    (
                        revision_id,
                        result.etag,
                        result.desired_generation_id,
                        result.desired_generation_manifest_sha256,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO activations(
                        activated_at, actor, role, revision_id, previous_revision_id,
                        etag, desired_generation_id,
                        desired_generation_manifest_sha256, restart_required
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        result.activated_at,
                        result.actor,
                        str(intent["role"]),
                        revision_id,
                        result.previous_revision_id,
                        result.etag,
                        result.desired_generation_id,
                        result.desired_generation_manifest_sha256,
                    ),
                )
                self._store_idempotency(
                    connection,
                    operation="activate",
                    key_hash=str(intent["key_hash"]),
                    request_hash=str(intent["request_hash"]),
                    result=result_payload,
                    created_at=result.activated_at,
                )
                self._append_audit(
                    connection,
                    occurred_at=result.activated_at,
                    action="config.activate",
                    actor=result.actor,
                    role=str(intent["role"]),
                    revision_id=revision_id,
                    details={**audit_details, "recovered": recovered},
                )
                connection.execute("DELETE FROM activation_intent WHERE singleton = 1")
                self._activation_checkpoint("finalize_precommit")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        self._activation_checkpoint("finalize_committed")
        with self._connect(read_only=True) as connection:
            state = self._state_row(connection)
            self._retire_unusable_revision_material_locked(connection, state)
        return result

    def _retire_unusable_revision_material_locked(
        self,
        connection: sqlite3.Connection,
        state: sqlite3.Row,
    ) -> None:
        """Remove private material that the current CAS state can never activate."""

        if self._activation_intent_row(connection) is not None:
            raise ControlPlaneStorageError("control_activation_recovery_required")

        active_revision_id = _stored_optional_revision_id(state["active_revision_id"])
        current_etag = str(state["etag"])
        retained: set[str] = set()
        for row in connection.execute("SELECT revision_id, base_etag FROM revisions").fetchall():
            revision_id = _stored_revision_id(row["revision_id"])
            if revision_id == active_revision_id or str(row["base_etag"]) == current_etag:
                retained.add(revision_id)

        try:
            entries = sorted(self.revisions_dir.iterdir(), key=lambda path: path.name)
        except OSError:
            raise ControlPlaneStorageError("control_revision_material_retirement_failed") from None
        retirement_paths: list[Path] = []
        for path in entries:
            name = path.name
            if not name.endswith(".env") or not _REVISION_RE.fullmatch(name[:-4]):
                raise ControlPlaneStorageError("control_revision_directory_unexpected_entry")
            if path.is_symlink() or not path.is_file():
                raise ControlPlaneStorageError("control_revision_directory_unexpected_entry")
            if name[:-4] not in retained:
                retirement_paths.append(path)

        removed = False
        for path in retirement_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                raise ControlPlaneStorageError(
                    "control_revision_material_retirement_failed"
                ) from None
            removed = True
        if removed:
            try:
                _fsync_directory(self.revisions_dir)
            except OSError:
                raise ControlPlaneStorageError(
                    "control_revision_material_retirement_failed"
                ) from None

    def _verify_committed_managed_env_locked(
        self,
        connection: sqlite3.Connection,
        state: sqlite3.Row,
    ) -> None:
        observed = self._managed_env_state()
        revision_id = _optional_text(state["active_revision_id"])
        if revision_id is None:
            if observed is not None:
                raise ControlPlaneStorageError("control_managed_env_state_mismatch")
            return
        row = self._revision_row(connection, revision_id)
        self._verified_revision_content(row)
        if not self._managed_state_matches(observed, row):
            raise ControlPlaneStorageError("control_managed_env_state_mismatch")

    def _validate_intent_state(
        self,
        connection: sqlite3.Connection,
        intent: sqlite3.Row,
        state: sqlite3.Row,
    ) -> tuple[str, str | None]:
        revision_id = _stored_revision_id(intent["revision_id"])
        previous_revision_id = _stored_optional_revision_id(intent["previous_revision_id"])
        if state["active_revision_id"] != previous_revision_id:
            raise ControlPlaneStorageError("control_activation_intent_conflict")
        if str(state["etag"]) != str(intent["previous_etag"]):
            raise ControlPlaneStorageError("control_activation_intent_conflict")
        target_row = self._revision_row(connection, revision_id)
        if _row_contains_secret_changes(target_row) and str(intent["role"]) != "secret-admin":
            raise ControlPlaneStorageError("control_activation_intent_invalid")
        self._require_current_bootstrap_boundary(target_row, recovery=True)
        if str(target_row["base_etag"]) != str(intent["previous_etag"]) or str(
            target_row["target_etag"]
        ) != str(intent["target_etag"]):
            raise ControlPlaneStorageError("control_activation_intent_invalid")
        if str(target_row["env_sha256"]) != str(intent["target_env_sha256"]):
            raise ControlPlaneStorageError("control_activation_intent_invalid")
        for name in ("previous_etag", "target_etag", "target_env_sha256"):
            _required_digest(intent[name], "control_activation_intent_invalid")
        for name in ("key_hash", "request_hash"):
            _required_digest(intent[name], "control_activation_intent_invalid")
        return revision_id, previous_revision_id

    def _validate_intent_result(
        self,
        intent: sqlite3.Row,
        target_row: sqlite3.Row,
        result: ActivationResult,
    ) -> None:
        if (
            result.contract_version != CONFIG_CONTRACT
            or result.revision_id != str(intent["revision_id"])
            or result.previous_revision_id
            != _stored_optional_revision_id(intent["previous_revision_id"])
            or result.actor != str(intent["actor"])
            or result.activated_at != str(intent["prepared_at"])
            or result.etag != str(intent["target_etag"])
            or result.embedding_identity != str(target_row["embedding_identity"])
            or result.restart_required is not True
        ):
            raise ControlPlaneStorageError("control_activation_intent_invalid")
        if (result.desired_generation_id is None) != (
            result.desired_generation_manifest_sha256 is None
        ):
            raise ControlPlaneStorageError("control_activation_intent_invalid")
        if result.desired_generation_id is not None:
            if not _EVIDENCE_ID_RE.fullmatch(result.desired_generation_id):
                raise ControlPlaneStorageError("control_activation_intent_invalid")
            _required_sha256(
                result.desired_generation_manifest_sha256,
                "control_activation_intent_invalid",
            )

    def _activation_intent_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM activation_intent WHERE singleton = 1").fetchone()

    def _managed_env_state(self) -> tuple[str, str] | None:
        path = self.managed_env_path
        if path.is_symlink():
            raise ControlPlaneStorageError("control_managed_env_path_unsafe")
        if not path.exists():
            return None
        if not path.is_file():
            raise ControlPlaneStorageError("control_managed_env_path_unsafe")
        if path.stat().st_mode & 0o777 != 0o600:
            raise ControlPlaneStorageError("control_managed_env_permissions_invalid")
        content = path.read_bytes()
        return _managed_revision_marker(content), _bytes_sha256(content)

    @staticmethod
    def _managed_state_matches(
        observed: tuple[str, str] | None,
        revision_row: sqlite3.Row,
    ) -> bool:
        return observed == (
            str(revision_row["revision_id"]),
            str(revision_row["env_sha256"]),
        )

    def _activation_checkpoint(self, _phase: str) -> None:
        """Fault-injection seam for crash-consistency tests."""

    def _verify_generation_evidence(
        self,
        revision: ConfigRevision,
        evidence: Mapping[str, object] | None,
    ) -> dict[str, object]:
        summary = _validate_embedding_evidence(
            evidence,
            revision_id=revision.revision_id,
            embedding_identity=revision.embedding_identity,
        )
        raw_root = str(self._base_env.get("PLASTIC_LANCEDB_GENERATION_ROOT") or "").strip()
        if not raw_root or "\x00" in raw_root:
            raise ControlPlanePreconditionError("embedding_generation_required")

        try:
            from plastic_promise.core.lancedb_generation import GenerationManager

            with GenerationManager(Path(raw_root).expanduser(), create=False) as manager:
                manifest = manager.load_manifest(str(summary["shadow_generation_id"]))
                current_manifest = manager.current_manifest()
        except Exception:
            raise ControlPlanePreconditionError("embedding_generation_required") from None
        if (
            current_manifest is not None
            and current_manifest.generation_id == manifest.generation_id
        ):
            raise ControlPlaneConflictError("control_embedding_generation_not_inactive")

        embedding = revision.config.get("embedding")
        if not isinstance(embedding, Mapping):
            raise ControlPlaneStorageError("control_metadata_invalid")
        quality = manifest.quality_report
        outbox = manifest.index_outbox
        current_runtime_identity = runtime_embedding_index_identity(
            revision.config,
            self._base_env,
        )
        if current_runtime_identity != revision.runtime_embedding_index_identity:
            raise ControlPlaneConflictError("control_embedding_evidence_mismatch")
        expected_index_identity = revision.runtime_embedding_index_identity
        bound_index_identity = (
            outbox.get("embedding_index_identity") if isinstance(outbox, Mapping) else None
        )
        gate = quality.get("gate") if isinstance(quality, Mapping) else None
        backend = quality.get("backend") if isinstance(quality, Mapping) else None
        smoke = quality.get("smoke") if isinstance(quality, Mapping) else None
        quality_hash = manifest.quality_report_sha256
        identity_matches = (
            manifest.build_status == "complete"
            and manifest.verification_status == "verified"
            and manifest.embedding_model == embedding.get("model")
            and manifest.model_revision == embedding.get("model_revision")
            and manifest.embedding_dimension == embedding.get("dimension")
            and isinstance(outbox, Mapping)
            and outbox.get("reconciled") is True
            and bound_index_identity == expected_index_identity
            and isinstance(gate, Mapping)
            and gate.get("status") == "pass"
            and isinstance(backend, Mapping)
            and backend.get("fallback_used") is False
            and backend.get("degraded_used") is False
            and isinstance(smoke, Mapping)
            and smoke.get("passed") is True
            and isinstance(quality_hash, str)
            and summary["provider_smoke_evidence_id"] == quality_hash
            and summary["quality_gate_evidence_id"] == quality_hash
        )
        if not identity_matches:
            raise ControlPlaneConflictError("control_embedding_evidence_mismatch")
        return {
            **summary,
            "manifest_sha256": manifest.manifest_sha256,
            "verified_generation": True,
        }

    @contextmanager
    def _connect(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        if read_only:
            uri = f"file:{self.database_path}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        else:
            connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous = FULL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()
            if synchronous is None or int(synchronous[0]) != 2:
                raise ControlPlaneStorageError("control_sqlite_durability_invalid")
            connection.execute("PRAGMA foreign_keys = ON")
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()

    def _current_snapshot(self, connection: sqlite3.Connection) -> SafeConfigSnapshot:
        if self._activation_intent_row(connection) is not None:
            raise ControlPlaneStorageError("control_activation_recovery_required")
        state = self._state_row(connection)
        revision_id = state["active_revision_id"]
        if revision_id is None:
            config = normalize_safe_config(_json_object(json.loads(state["base_safe_json"])))
            secrets = _bool_object(json.loads(state["base_secret_state_json"]))
            identity = str(state["base_embedding_identity"])
            source = "base"
        else:
            row = self._revision_row(connection, str(revision_id))
            config = normalize_safe_config(_json_object(json.loads(row["safe_json"])))
            secrets = _bool_object(json.loads(row["secret_state_json"]))
            identity = str(row["embedding_identity"])
            source = "managed"
        self._overlay_current_bootstrap_boundary(config, secrets)
        return SafeConfigSnapshot(
            contract_version=CONFIG_CONTRACT,
            revision_id=revision_id,
            source=source,
            etag=str(state["etag"]),
            config=config,
            secrets=secrets,
            embedding_identity=identity,
            desired_generation_id=_optional_text(state["desired_generation_id"]),
            desired_generation_manifest_sha256=_optional_text(
                state["desired_generation_manifest_sha256"]
            ),
        )

    def _current_secret_values(
        self,
        connection: sqlite3.Connection,
        current: SafeConfigSnapshot,
    ) -> dict[str, str]:
        if current.revision_id is None:
            values = secret_values_from_environment(self._base_env)
        else:
            row = self._revision_row(connection, current.revision_id)
            environment = _parse_environment_file(self._verified_revision_content(row))
            values = secret_values_from_environment(environment)
            compute_path = self.compute_revisions_dir / f"{current.revision_id}.env"
            if compute_path.is_file() and not compute_path.is_symlink():
                compute_environment = _parse_environment_file(compute_path.read_bytes())
                values["compute_node_cloud_api_key"] = str(
                    compute_environment.get("PP_LOCAL_NODE_CLOUD_API_KEY") or ""
                )
            values["gateway_token"] = secret_values_from_environment(self._base_env)[
                "gateway_token"
            ]
        for name, configured in current.secrets.items():
            if configured and not values.get(name):
                raise ControlPlaneStorageError("control_secret_material_unavailable")
        return values

    def _state_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM control_state WHERE singleton = 1").fetchone()
        if row is None:
            raise ControlPlaneStorageError("control_state_missing")
        return row

    def _revision_row(self, connection: sqlite3.Connection, revision_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ControlPlaneNotFoundError()
        return row

    def _overlay_current_bootstrap_boundary(
        self,
        config: dict[str, object],
        secrets_state: dict[str, bool],
    ) -> None:
        current_base = safe_config_from_environment(self._base_env)
        base_gateway = current_base["gateway"]
        gateway = config["gateway"]
        if not isinstance(base_gateway, dict) or not isinstance(gateway, dict):
            raise ControlPlaneStorageError("control_metadata_invalid")
        gateway["project_id"] = base_gateway["project_id"]
        gateway["provider_host_allowlist"] = list(base_gateway["provider_host_allowlist"])
        secrets_state["gateway_token"] = bool(
            secret_values_from_environment(self._base_env)["gateway_token"]
        )

    def _current_runtime_embedding_index_identity(
        self,
        connection: sqlite3.Connection,
    ) -> str:
        state = self._state_row(connection)
        revision_id = _optional_text(state["active_revision_id"])
        if revision_id is not None:
            row = self._revision_row(connection, revision_id)
            return _required_text(
                row["runtime_embedding_index_identity"],
                "control_metadata_invalid",
            )
        config = _json_object(json.loads(state["base_safe_json"]))
        return runtime_embedding_index_identity(config, self._base_env)

    def _require_current_bootstrap_boundary(
        self,
        revision_row: sqlite3.Row,
        *,
        recovery: bool = False,
    ) -> None:
        stored = _required_digest(
            revision_row["bootstrap_boundary_sha256"],
            "control_metadata_invalid",
        )
        current = bootstrap_boundary_sha256(self._base_env)
        if stored != current:
            if recovery:
                raise ControlPlaneStorageError("control_bootstrap_boundary_changed")
            raise ControlPlaneConflictError("control_bootstrap_boundary_changed")

    def _idempotency_replay(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        key_hash: str,
        request_hash: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT request_hash, result_json FROM idempotency WHERE operation = ? AND key_hash = ?",
            (operation, key_hash),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ControlPlaneConflictError("control_idempotency_key_conflict")
        return _json_object(json.loads(row["result_json"]))

    def _store_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        key_hash: str,
        request_hash: str,
        result: Mapping[str, object],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotency(operation, key_hash, request_hash, result_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (operation, key_hash, request_hash, canonical_json(result), created_at),
        )

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        occurred_at: str,
        action: str,
        actor: str,
        role: str,
        revision_id: str | None,
        details: Mapping[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(
                occurred_at, action, actor, role, revision_id, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                occurred_at,
                action,
                actor,
                role,
                revision_id,
                canonical_json(details),
            ),
        )

    def _new_revision_id(self) -> str:
        stamp = self._clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"cfg-{stamp}-{uuid.uuid4().hex[:12]}"

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ControlPlaneStorageError("control_clock_invalid")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _write_new_revision(self, revision_id: str, content: bytes) -> Path:
        path = self.revisions_dir / f"{revision_id}.env"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        _fsync_directory(self.revisions_dir)
        return path

    def _write_compute_revision(self, revision_id: str, content: bytes) -> Path:
        path = self.compute_revisions_dir / f"{revision_id}.env"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        _fsync_directory(self.compute_revisions_dir)
        return path

    def _verified_revision_content(self, row: sqlite3.Row) -> bytes:
        revision_id = str(row["revision_id"])
        _validate_revision_id(revision_id)
        path = self.revisions_dir / f"{revision_id}.env"
        if path.is_symlink() or not path.is_file():
            raise ControlPlaneStorageError("control_revision_material_missing")
        if path.stat().st_mode & 0o777 != 0o600:
            raise ControlPlaneStorageError("control_revision_permissions_invalid")
        content = path.read_bytes()
        if _bytes_sha256(content) != row["env_sha256"]:
            raise ControlPlaneStorageError("control_revision_integrity_failed")
        environment = _parse_environment_file(content)
        if BOOTSTRAP_ONLY_ENV_NAMES.intersection(environment):
            raise ControlPlaneStorageError("control_revision_bootstrap_boundary_invalid")
        return content

    def _verified_compute_revision_content(self, row: sqlite3.Row) -> bytes:
        revision_id = _validate_revision_id(row["revision_id"])
        path = self.compute_revisions_dir / f"{revision_id}.env"
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
            raise ControlPlaneStorageError("control_compute_profile_material_missing")
        content = path.read_bytes()
        environment = _parse_environment_file(content)
        if environment.get("PP_ENDPOINT_ROLE") not in {None, "pp-compute-node"}:
            raise ControlPlaneStorageError("control_compute_profile_role_invalid")
        if (
            "PP_LOCAL_NODE_CLOUD_API_KEY" in environment
            and not environment["PP_LOCAL_NODE_CLOUD_API_KEY"]
        ):
            raise ControlPlaneStorageError("control_compute_profile_secret_invalid")
        return content

    def _atomic_write_managed_env(self, content: bytes) -> None:
        if self.managed_env_path.is_symlink() or (
            self.managed_env_path.exists() and not self.managed_env_path.is_file()
        ):
            raise ControlPlaneStorageError("control_managed_env_path_unsafe")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".managed.env.", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            self._activation_checkpoint("managed_temp_fsynced")
            os.replace(temporary, self.managed_env_path)
            self._activation_checkpoint("managed_replaced")
            os.chmod(self.managed_env_path, 0o600)
            _fsync_directory(self.root)
            self._activation_checkpoint("managed_fsynced")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _atomic_write_compute_env(self, content: bytes) -> None:
        """Atomically activate the private compute-only environment projection."""

        if self.compute_managed_env_path.is_symlink() or (
            self.compute_managed_env_path.exists() and not self.compute_managed_env_path.is_file()
        ):
            raise ControlPlaneStorageError("control_compute_profile_path_unsafe")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".compute.managed.env.", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.compute_managed_env_path)
            os.chmod(self.compute_managed_env_path, 0o600)
            _fsync_directory(self.root)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _activation_file_lock(self):
        return _FileLock(self._activation_lock_path)


class _FileLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> _FileLock:
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._descriptor = os.open(self._path, flags)
        fcntl.flock(self._descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args: object) -> None:
        assert self._descriptor is not None
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = None


def _revision_from_row(row: sqlite3.Row) -> ConfigRevision:
    config = normalize_safe_config(_json_object(json.loads(row["safe_json"])))
    return ConfigRevision(
        contract_version=CONFIG_CONTRACT,
        revision_id=str(row["revision_id"]),
        created_at=str(row["created_at"]),
        actor=str(row["actor"]),
        base_etag=str(row["base_etag"]),
        etag=str(row["target_etag"]),
        config=config,
        secrets=_bool_object(json.loads(row["secret_state_json"])),
        embedding_identity=str(row["embedding_identity"]),
        runtime_embedding_index_identity=_required_text(
            row["runtime_embedding_index_identity"],
            "control_metadata_invalid",
        ),
        requires_embedding_evidence=bool(row["requires_embedding_evidence"]),
        contains_secret_changes=_row_contains_secret_changes(row),
    )


def _row_contains_secret_changes(row: sqlite3.Row) -> bool:
    if _row_secret_metadata_unbound(row):
        return True
    return not (row["secret_change_metadata_bound"] == 1 and row["contains_secret_changes"] == 0)


def _row_secret_metadata_unbound(row: sqlite3.Row) -> bool:
    keys = set(row.keys())
    return "secret_change_metadata_bound" not in keys or row["secret_change_metadata_bound"] != 1


def _validated_evidence_summary(value: object) -> dict[str, object]:
    required = {
        "provider_smoke_evidence_id",
        "shadow_generation_id",
        "quality_gate_evidence_id",
        "manifest_sha256",
    }
    optional = {"verified_generation"}
    if not isinstance(value, Mapping) or not required.issubset(value):
        raise ControlPlaneStorageError("control_embedding_evidence_verifier_invalid")
    if set(value) - required - optional:
        raise ControlPlaneStorageError("control_embedding_evidence_verifier_invalid")

    summary: dict[str, object] = {}
    for name in sorted(required):
        evidence_id = value.get(name)
        if not isinstance(evidence_id, str) or not _EVIDENCE_ID_RE.fullmatch(evidence_id):
            raise ControlPlaneStorageError("control_embedding_evidence_verifier_invalid")
        summary[name] = evidence_id
    if not re.fullmatch(r"[0-9a-f]{64}", str(summary["manifest_sha256"])):
        raise ControlPlaneStorageError("control_embedding_evidence_verifier_invalid")
    if value.get("verified_generation") is not True:
        raise ControlPlaneStorageError("control_embedding_evidence_verifier_invalid")
    summary["verified_generation"] = True
    return summary


def _validate_embedding_evidence(
    evidence: Mapping[str, object] | None,
    *,
    revision_id: str,
    embedding_identity: str,
) -> dict[str, object]:
    if not isinstance(evidence, Mapping) or set(evidence) != {
        "revision_id",
        "embedding_identity",
        "provider_smoke",
        "shadow_generation",
        "quality_gate",
    }:
        raise ControlPlanePreconditionError("embedding_generation_required")
    if evidence.get("revision_id") != revision_id:
        raise ControlPlaneConflictError("control_embedding_evidence_mismatch")
    if evidence.get("embedding_identity") != embedding_identity:
        raise ControlPlaneConflictError("control_embedding_evidence_mismatch")
    provider = _passed_evidence(evidence["provider_smoke"], identifier="evidence_id")
    shadow = _passed_evidence(evidence["shadow_generation"], identifier="generation_id")
    quality = _passed_evidence(evidence["quality_gate"], identifier="evidence_id")
    return {
        "provider_smoke_evidence_id": provider,
        "shadow_generation_id": shadow,
        "quality_gate_evidence_id": quality,
    }


def _passed_evidence(value: object, *, identifier: str) -> str:
    if not isinstance(value, Mapping) or set(value) != {"passed", identifier}:
        raise ControlPlanePreconditionError("embedding_generation_required")
    evidence_id = value.get(identifier)
    if value.get("passed") is not True or not isinstance(evidence_id, str):
        raise ControlPlanePreconditionError("embedding_generation_required")
    if not _EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise ControlPlaneValidationError("control_embedding_evidence_invalid")
    return evidence_id


def _require_role(actor: object, role: object, required: str) -> None:
    if not isinstance(actor, str) or not _ACTOR_RE.fullmatch(actor):
        raise ControlPlaneValidationError("control_actor_invalid")
    if role not in _ROLE_RANK:
        raise ControlPlaneValidationError("control_role_invalid")
    if _ROLE_RANK[str(role)] < _ROLE_RANK[required]:
        raise ControlPlaneAuthorizationError()


def _required_etag(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'"sha256:[0-9a-f]{64}"', value):
        raise ControlPlanePreconditionError("control_if_match_required")
    return value


def _opaque_etag() -> str:
    """Create a secret-independent CAS token for one immutable state."""

    return f'"sha256:{secrets.token_hex(32)}"'


def _compare_etag(expected: str, current: str) -> None:
    if expected != current:
        raise ControlPlanePreconditionError("control_etag_mismatch", status_code=412)


def _idempotency_key_hash(value: object) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_RE.fullmatch(value):
        raise ControlPlanePreconditionError("control_idempotency_key_required")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validate_revision_id(value: object) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise ControlPlaneValidationError("control_revision_id_invalid")
    return value


def _bounded_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ControlPlaneValidationError("control_limit_invalid")
    return value


def _environment_file(revision_id: str, environment: Mapping[str, str]) -> bytes:
    lines = [
        "# Managed by Plastic Promise control plane. Do not edit.",
        f"# revision={revision_id}",
    ]
    for name, value in sorted(environment.items()):
        if not _ENV_NAME_RE.fullmatch(name):
            raise ControlPlaneStorageError("control_environment_name_invalid")
        if not isinstance(value, str) or any(character.isspace() for character in value):
            raise ControlPlaneStorageError("control_environment_value_invalid")
        if value and any(ord(character) < 0x21 or ord(character) == 0x7F for character in value):
            raise ControlPlaneStorageError("control_environment_value_invalid")
        lines.append(f"{name}={value}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _parse_environment_file(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ControlPlaneStorageError("control_revision_material_invalid") from None
    environment: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not _ENV_NAME_RE.fullmatch(name) or name in environment:
            raise ControlPlaneStorageError("control_revision_material_invalid")
        if any(character.isspace() for character in value):
            raise ControlPlaneStorageError("control_revision_material_invalid")
        environment[name] = value
    return environment


def _managed_revision_marker(content: bytes) -> str:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ControlPlaneStorageError("control_managed_env_marker_invalid") from None
    prefix = "# revision="
    if (
        len(lines) < 2
        or lines[0] != "# Managed by Plastic Promise control plane. Do not edit."
        or not lines[1].startswith(prefix)
        or sum(line.startswith(prefix) for line in lines) != 1
    ):
        raise ControlPlaneStorageError("control_managed_env_marker_invalid")
    return _stored_revision_id(lines[1][len(prefix) :])


def _stored_revision_id(value: object) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise ControlPlaneStorageError("control_activation_intent_invalid")
    return value


def _stored_optional_revision_id(value: object) -> str | None:
    if value is None:
        return None
    return _stored_revision_id(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ControlPlaneStorageError("control_metadata_invalid")
    return value


def _required_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControlPlaneStorageError(code)
    return value


def _required_sha256(value: object, code: str) -> str:
    text = _required_text(value, code)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ControlPlaneStorageError(code)
    return text


def _required_digest(value: object, code: str) -> str:
    text = _required_text(value, code)
    if not re.fullmatch(r'(?:sha256:[0-9a-f]{64}|"sha256:[0-9a-f]{64}")', text):
        raise ControlPlaneStorageError(code)
    return text


def _bytes_sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ControlPlaneStorageError("control_storage_path_unsafe")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ControlPlaneStorageError("control_storage_path_unsafe")
    os.chmod(path, 0o700)


def _private_file(path: Path) -> None:
    if path.is_symlink():
        raise ControlPlaneStorageError("control_storage_path_unsafe")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    return _json_object(json.loads(canonical_json(value)))


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ControlPlaneStorageError("control_metadata_invalid")
    return dict(value)


def _bool_object(value: object) -> dict[str, bool]:
    if (
        not isinstance(value, dict)
        or any(not isinstance(key, str) for key in value)
        or any(not isinstance(item, bool) for item in value.values())
    ):
        raise ControlPlaneStorageError("control_metadata_invalid")
    return dict(value)


__all__ = [
    "ActivationResult",
    "AuditEvent",
    "ConfigRevision",
    "ControlPlaneAuthorizationError",
    "ControlPlaneConfigStore",
    "ControlPlaneConflictError",
    "ControlPlaneNotFoundError",
    "ControlPlanePreconditionError",
    "ControlPlaneStorageError",
    "SafeConfigSnapshot",
    "ValidationResult",
]
