"""Server-owned canonical authorization for node-routed index work.

The scheduler accepts only an opaque ``outbox:<id>`` reference.  This module
resolves that reference against the canonical SQLite outbox and memory row on
every enqueue and immediately before execution.  It therefore keeps project
ownership, index-material identity, and active controlled routing policy on
the server rather than trusting a worker or a caller-supplied envelope.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from plastic_promise.control_plane.config_schema import routing_for_project
from plastic_promise.core.node_governance import (
    NodeGovernanceError,
    NodeTaskRequest,
    ResolvedNodeTask,
)
from plastic_promise.core.paths import get_db_path

_OUTBOX_REFERENCE_RE = re.compile(r"\Aoutbox:([a-z][a-z0-9_.:-]{1,127})\Z")
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_MATERIAL_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CONTROL_REVISION_RE = re.compile(r"\Acfg-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\Z")
_NODE_ID_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,127}\Z")
_POLICIES = frozenset({"remote-node-first", "ollama-first", "fastest-estimated", "pinned-node"})
_SERVER_AUTHORITY_TOKEN = object()


class ActiveControlConfig(Protocol):
    """Read-only projection of the active server-controlled revision."""

    def safe_config(self) -> object: ...

    def get_revision(self, revision_id: str) -> object: ...

    def compute_profile_digest(self, revision_id: str) -> str: ...


def open_server_memory_index_node_task_authority(
    control_config: ActiveControlConfig,
) -> SQLiteMemoryIndexNodeTaskAuthority:
    """Open the production resolver against only the canonical SQLite path."""

    return SQLiteMemoryIndexNodeTaskAuthority(
        Path(get_db_path()).expanduser(),
        control_config,
        _server_token=_SERVER_AUTHORITY_TOKEN,
    )


def _open_memory_index_node_task_authority_for_test(
    canonical_db_path: str | Path,
    control_config: ActiveControlConfig,
) -> SQLiteMemoryIndexNodeTaskAuthority:
    """Test-only opener for an explicitly created canonical SQLite fixture."""

    return SQLiteMemoryIndexNodeTaskAuthority(
        canonical_db_path,
        control_config,
        _server_token=_SERVER_AUTHORITY_TOKEN,
    )


class SQLiteMemoryIndexNodeTaskAuthority:
    """Resolve one canonical memory-index outbox job into a governed route.

    Its external interface is intentionally the three methods required by
    :class:`NodeTaskAuthority`: ``resolve``, ``verify`` and ``verify_lease``.
    The implementation owns SQLite query-only access, exact V3 job validation,
    immutable-revision lookup and schedule policy extraction.
    """

    def __init__(
        self,
        canonical_db_path: str | Path,
        control_config: ActiveControlConfig,
        *,
        _server_token: object | None = None,
    ) -> None:
        if _server_token is not _SERVER_AUTHORITY_TOKEN:
            raise NodeGovernanceError("node_task_authority_server_required")
        if not callable(getattr(control_config, "safe_config", None)):
            raise NodeGovernanceError("node_task_control_config_invalid")
        path = Path(canonical_db_path).expanduser()
        if not path.is_file():
            raise NodeGovernanceError("node_task_canonical_unavailable")
        self._db_path = path.resolve()
        self._control_config = control_config

    def resolve(self, request: NodeTaskRequest) -> ResolvedNodeTask:
        """Resolve a caller envelope only from current canonical state."""

        if not isinstance(request, NodeTaskRequest):
            raise NodeGovernanceError("node_task_request_invalid")
        if request.operation != "embedding":
            raise NodeGovernanceError("node_task_reference_operation_unsupported")
        outbox_id = self._outbox_id(request.input_reference)
        canonical = self._load_canonical_outbox(outbox_id)
        if request.project_id != canonical.project_id:
            raise NodeGovernanceError("node_task_reference_ownership_invalid")
        revision_id, routing, profile_digest = self._active_routing(canonical.project_id)
        return ResolvedNodeTask(
            project_id=canonical.project_id,
            operation="embedding",
            input_reference=request.input_reference,
            subject_hash="sha256:" + canonical.embedding_hash,
            visibility="project",
            config_revision=revision_id,
            required_identity=routing.embedding_required_identity,
            scheduling_policy=routing.embedding_policy,
            inference_mode=routing.inference_mode,
            pinned_node_id=routing.embedding_pinned_node_id or None,
            allowed_node_ids=routing.allowed_node_ids,
            profile_digest=profile_digest,
        )

    def verify(self, resolved: ResolvedNodeTask) -> None:
        """Fail closed if project, material, revision, or routing has drifted."""

        if not isinstance(resolved, ResolvedNodeTask):
            raise NodeGovernanceError("node_task_resolved_invalid")
        current = self.resolve(
            NodeTaskRequest(
                project_id=resolved.project_id,
                idempotency_key="node-authority-verify",
                operation=resolved.operation,
                input_reference=resolved.input_reference,
            )
        )
        if current != resolved:
            raise NodeGovernanceError("node_task_reference_stale")

    def verify_lease(self, resolved: ResolvedNodeTask) -> None:
        """Verify a claimed lease against its original immutable revision."""

        if not isinstance(resolved, ResolvedNodeTask):
            raise NodeGovernanceError("node_task_resolved_invalid")
        if resolved.operation != "embedding":
            raise NodeGovernanceError("node_task_reference_operation_unsupported")
        outbox_id = self._outbox_id(resolved.input_reference)
        canonical = self._load_canonical_outbox(outbox_id)
        if (
            resolved.project_id != canonical.project_id
            or resolved.subject_hash != "sha256:" + canonical.embedding_hash
        ):
            raise NodeGovernanceError("node_task_reference_stale")
        routing, profile_digest = self._routing_at_revision(
            canonical.project_id,
            resolved.config_revision,
            require_profile_digest=resolved.profile_digest is not None,
        )
        expected = ResolvedNodeTask(
            project_id=canonical.project_id,
            operation="embedding",
            input_reference=resolved.input_reference,
            subject_hash="sha256:" + canonical.embedding_hash,
            visibility="project",
            config_revision=resolved.config_revision,
            required_identity=routing.embedding_required_identity,
            scheduling_policy=routing.embedding_policy,
            inference_mode=routing.inference_mode,
            pinned_node_id=routing.embedding_pinned_node_id or None,
            allowed_node_ids=routing.allowed_node_ids,
            profile_digest=profile_digest,
        )
        if expected != resolved:
            raise NodeGovernanceError("node_task_reference_stale")

    def _load_canonical_outbox(self, outbox_id: str) -> _CanonicalMemoryIndexJob:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect_read_only()
            row = connection.execute(
                """
                SELECT outbox_id, tool_name, project_id, status, payload_json
                FROM store_outbox
                WHERE outbox_id = ?
                """,
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise NodeGovernanceError("node_task_reference_missing")
            if str(row["tool_name"] or "") != "memory_index":
                raise NodeGovernanceError("node_task_reference_kind_invalid")
            if str(row["status"] or "") not in {"pending", "processing"}:
                raise NodeGovernanceError("node_task_reference_not_pending")
            project_id = _required_project(row["project_id"])
            payload = _memory_index_payload(row["payload_json"])
            if payload.project_id != project_id:
                raise NodeGovernanceError("node_task_reference_ownership_invalid")
            memory = connection.execute(
                """
                SELECT project_id, embedding_hash
                FROM memories
                WHERE id = ?
                """,
                (payload.memory_id,),
            ).fetchone()
        except NodeGovernanceError:
            raise
        except sqlite3.Error as exc:
            raise NodeGovernanceError("node_task_canonical_schema_missing") from exc
        finally:
            if connection is not None:
                connection.close()

        if memory is None:
            raise NodeGovernanceError("node_task_canonical_subject_missing")
        if _required_project(memory["project_id"]) != project_id:
            raise NodeGovernanceError("node_task_reference_ownership_invalid")
        if _required_material_hash(memory["embedding_hash"], "node_task_subject_hash_invalid") != (
            payload.embedding_hash
        ):
            raise NodeGovernanceError("node_task_subject_stale")
        return _CanonicalMemoryIndexJob(
            project_id=project_id,
            memory_id=payload.memory_id,
            embedding_hash=payload.embedding_hash,
        )

    def _active_routing(self, project_id: str) -> tuple[str, _RoutingPolicy, str | None]:
        try:
            snapshot = self._control_config.safe_config()
            revision_id = str(getattr(snapshot, "revision_id", "") or "")
            config = getattr(snapshot, "config", None)
        except Exception as exc:
            raise NodeGovernanceError("node_task_control_config_unavailable") from exc
        if not _CONTROL_REVISION_RE.fullmatch(revision_id):
            raise NodeGovernanceError("node_task_control_revision_unavailable")
        if not isinstance(config, Mapping):
            raise NodeGovernanceError("node_task_control_config_invalid")
        try:
            routing = routing_for_project(config, project_id)
        except Exception:
            routing = None
        if not isinstance(routing, Mapping):
            raise NodeGovernanceError("node_task_routing_unavailable")
        policy = _RoutingPolicy.from_mapping(routing)
        if not policy.enabled:
            raise NodeGovernanceError("node_task_routing_disabled")
        profile_digest: str | None = None
        digest_reader = getattr(self._control_config, "compute_profile_digest", None)
        if callable(digest_reader):
            try:
                profile_digest = digest_reader(revision_id)
            except Exception as exc:
                raise NodeGovernanceError("node_task_compute_profile_unavailable") from exc
            if not isinstance(profile_digest, str) or not _SHA256_RE.fullmatch(profile_digest):
                raise NodeGovernanceError("node_task_compute_profile_invalid")
        return revision_id, policy, profile_digest

    def _routing_at_revision(
        self,
        project_id: str,
        revision_id: str,
        *,
        require_profile_digest: bool,
    ) -> tuple[_RoutingPolicy, str | None]:
        revision_reader = getattr(self._control_config, "get_revision", None)
        if not callable(revision_reader):
            raise NodeGovernanceError("node_task_control_revision_unavailable")
        try:
            snapshot = revision_reader(revision_id)
            stored_revision = str(getattr(snapshot, "revision_id", "") or "")
            config = getattr(snapshot, "config", None)
        except Exception as exc:
            raise NodeGovernanceError("node_task_control_revision_unavailable") from exc
        if stored_revision != revision_id or not _CONTROL_REVISION_RE.fullmatch(stored_revision):
            raise NodeGovernanceError("node_task_control_revision_unavailable")
        if not isinstance(config, Mapping):
            raise NodeGovernanceError("node_task_control_config_invalid")
        try:
            raw_routing = routing_for_project(config, project_id)
        except Exception:
            raw_routing = None
        if not isinstance(raw_routing, Mapping):
            raise NodeGovernanceError("node_task_routing_unavailable")
        routing = _RoutingPolicy.from_mapping(raw_routing)
        if not routing.enabled:
            raise NodeGovernanceError("node_task_routing_disabled")
        if not require_profile_digest:
            return routing, None
        digest_reader = getattr(self._control_config, "compute_profile_digest", None)
        if not callable(digest_reader):
            raise NodeGovernanceError("node_task_compute_profile_unavailable")
        try:
            profile_digest = digest_reader(revision_id)
        except Exception as exc:
            raise NodeGovernanceError("node_task_compute_profile_unavailable") from exc
        if not isinstance(profile_digest, str) or not _SHA256_RE.fullmatch(profile_digest):
            raise NodeGovernanceError("node_task_compute_profile_invalid")
        return routing, profile_digest

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self._db_path.as_uri()}?mode=ro",
            uri=True,
            timeout=10.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    @staticmethod
    def _outbox_id(reference: str) -> str:
        match = _OUTBOX_REFERENCE_RE.fullmatch(reference)
        if match is None:
            raise NodeGovernanceError("node_task_input_reference_invalid")
        return match.group(1)


class _RoutingPolicy:
    """Defensive decoder for the active, already schema-validated policy."""

    def __init__(
        self,
        *,
        enabled: bool,
        inference_mode: str,
        embedding_policy: str,
        embedding_required_identity: str,
        embedding_pinned_node_id: str,
        allowed_node_ids: tuple[str, ...],
    ) -> None:
        self.enabled = enabled
        self.inference_mode = inference_mode
        self.embedding_policy = embedding_policy
        self.embedding_required_identity = embedding_required_identity
        self.embedding_pinned_node_id = embedding_pinned_node_id
        self.allowed_node_ids = allowed_node_ids

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> _RoutingPolicy:
        enabled = value.get("enabled")
        if type(enabled) is not bool:
            raise NodeGovernanceError("node_task_routing_invalid")
        inference_mode = value.get("inference_mode", "hybrid")
        policy = value.get("embedding_policy")
        identity = value.get("embedding_required_identity")
        pin = value.get("embedding_pinned_node_id")
        raw_allowed = value.get("allowed_node_ids")
        if (
            inference_mode not in {"local", "cloud", "hybrid"}
            or not isinstance(policy, str)
            or policy not in _POLICIES
            or not isinstance(identity, str)
            or not isinstance(pin, str)
            or not isinstance(raw_allowed, list)
        ):
            raise NodeGovernanceError("node_task_routing_invalid")
        if not enabled:
            return cls(
                enabled=False,
                inference_mode=inference_mode,
                embedding_policy=policy,
                embedding_required_identity="",
                embedding_pinned_node_id="",
                allowed_node_ids=(),
            )
        if not _SHA256_RE.fullmatch(identity) or not raw_allowed:
            raise NodeGovernanceError("node_task_routing_invalid")
        allowed: list[str] = []
        for node_id in raw_allowed:
            if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id):
                raise NodeGovernanceError("node_task_routing_invalid")
            allowed.append(node_id)
        if len(set(allowed)) != len(allowed):
            raise NodeGovernanceError("node_task_routing_invalid")
        if pin and (not _NODE_ID_RE.fullmatch(pin) or pin not in allowed):
            raise NodeGovernanceError("node_task_routing_invalid")
        if policy == "pinned-node" and not pin:
            raise NodeGovernanceError("node_task_routing_invalid")
        return cls(
            enabled=True,
            inference_mode=inference_mode,
            embedding_policy=policy,
            embedding_required_identity=identity,
            embedding_pinned_node_id=pin,
            allowed_node_ids=tuple(allowed),
        )


class _MemoryIndexPayload:
    def __init__(self, *, project_id: str, memory_id: str, embedding_hash: str) -> None:
        self.project_id = project_id
        self.memory_id = memory_id
        self.embedding_hash = embedding_hash


class _CanonicalMemoryIndexJob(_MemoryIndexPayload):
    pass


def _memory_index_payload(value: object) -> _MemoryIndexPayload:
    if not isinstance(value, str):
        raise NodeGovernanceError("node_task_reference_payload_invalid")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise NodeGovernanceError("node_task_reference_payload_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "action",
        "expected_embedding_hash",
        "material_revision",
        "memory_id",
        "memory_version",
        "project_id",
    }:
        raise NodeGovernanceError("node_task_reference_payload_invalid")
    action = payload.get("action")
    material_revision = payload.get("material_revision")
    memory_version = payload.get("memory_version")
    if (
        action != "upsert"
        or type(memory_version) is not int
        or memory_version < 0
        or material_revision != payload.get("expected_embedding_hash")
    ):
        raise NodeGovernanceError("node_task_reference_payload_invalid")
    return _MemoryIndexPayload(
        project_id=_required_project(payload.get("project_id")),
        memory_id=_required_memory_id(payload.get("memory_id")),
        embedding_hash=_required_material_hash(
            payload.get("expected_embedding_hash"),
            "node_task_reference_payload_invalid",
        ),
    )


def _required_project(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("project:"):
        raise NodeGovernanceError("node_task_reference_ownership_invalid")
    if not _NODE_ID_RE.fullmatch(value.removeprefix("project:")):
        raise NodeGovernanceError("node_task_reference_ownership_invalid")
    return value


def _required_memory_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 256:
        raise NodeGovernanceError("node_task_reference_payload_invalid")
    if value != value.strip() or "\x00" in value:
        raise NodeGovernanceError("node_task_reference_payload_invalid")
    return value


def _required_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise NodeGovernanceError(code)
    return value


def _required_material_hash(value: object, code: str) -> str:
    if not isinstance(value, str) or not _MATERIAL_SHA256_RE.fullmatch(value):
        raise NodeGovernanceError(code)
    return value


__all__ = [
    "SQLiteMemoryIndexNodeTaskAuthority",
    "open_server_memory_index_node_task_authority",
]
