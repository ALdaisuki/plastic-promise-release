"""Atomic lifecycle changes for existing ordinary source memories.

SQLite is the authoritative state boundary.  Derived LanceDB work is recorded
in the durable outbox during that boundary and is only replayed after commit.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from plastic_promise.core.synthesis import SynthesisStore, synthesis_content_hash
from plastic_promise.core.synthesis_retrieval import (
    increment_memory_version_if_present,
    is_governed_synthesis_memory,
    read_memory_version,
)
from plastic_promise.core.traceability import (
    enqueue_memory_index_delete,
    enqueue_memory_index_upsert,
    record_memory_lineage,
)
from plastic_promise.memory.pipeline import MemoryPipeline, PreparedMemory


class OrdinaryMemoryMutationError(RuntimeError):
    """Reject an ordinary source mutation before a partial state can commit."""


@dataclass(frozen=True)
class OrdinaryMutationResult:
    memory_id: str
    operation: str
    previous_content_hash: str
    current_content_hash: str
    committed_memory_version: int
    ordinary_index_job_id: str
    peer_index_job_ids: tuple[str, ...]
    stale_synthesis_ids: tuple[str, ...]
    synthesis_index_job_ids: tuple[str, ...]


_UNAVAILABLE_STATES = frozenset({"wrong", "deprecated", "forgotten"})
_STALE_REASONS = {
    "corrected": "source_changed",
    "wrong": "source_wrong",
    "deprecated": "source_deprecated",
    "forgotten": "source_forgotten",
}
_BLOCKED_TAG_STATES = frozenset(
    {
        "conflict",
        "corrected",
        "deleted",
        "deprecated",
        "expired",
        "forgotten",
        "obsolete",
        "replaced",
        "rejected",
        "stale",
        "wrong",
    }
)
_PRECONDITION_SNAPSHOT_FIELDS = frozenset(
    {
        "access_count",
        "category",
        "created_at",
        "decay_multiplier",
        "effective_half_life",
        "embedding_hash",
        "last_accessed",
        "metadata_json",
        "tags",
        "tier",
        "worth_failure",
        "worth_success",
    }
)
_CORRECTION_METADATA_REPLACEMENT_FIELDS = frozenset(
    {"importance", "worth_failure", "worth_success"}
)
_CORRECTION_POLICY_REPLACEMENT_FIELDS = frozenset({"category", "domain", "tags", "tier"})


class OrdinaryMemoryMutationCoordinator:
    """Own one source mutation transaction and its dependent invalidation."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def replace_content(
        self,
        memory_id: str,
        *,
        content: str,
        reason: str,
        actor: str,
        call_id: str,
        expected_project_id: str | None = None,
        expected_content_hash: str | None = None,
        expected_source_snapshot: Mapping[str, Any] | None = None,
        expected_peer_snapshots: Mapping[str, Mapping[str, Any]] | None = None,
        peer_metadata_replacements: Mapping[str, Mapping[str, Any]] | None = None,
        require_source_available: bool = False,
        metadata_replacements: Mapping[str, int | float] | None = None,
        policy_replacements: Mapping[str, Any] | None = None,
    ) -> OrdinaryMutationResult:
        return self._mutate(
            memory_id,
            operation="corrected",
            content=content,
            reason=reason,
            actor=actor,
            call_id=call_id,
            expected_project_id=expected_project_id,
            expected_content_hash=expected_content_hash,
            expected_source_snapshot=expected_source_snapshot,
            expected_peer_snapshots=expected_peer_snapshots,
            peer_metadata_replacements=peer_metadata_replacements,
            require_source_available=require_source_available,
            metadata_replacements=metadata_replacements,
            policy_replacements=policy_replacements,
        )

    def mark_unavailable(
        self,
        memory_id: str,
        *,
        state: str,
        reason: str,
        actor: str,
        call_id: str,
        expected_project_id: str | None = None,
        expected_content_hash: str | None = None,
        expected_source_snapshot: Mapping[str, Any] | None = None,
        expected_peer_snapshots: Mapping[str, Mapping[str, Any]] | None = None,
        peer_metadata_replacements: Mapping[str, Mapping[str, Any]] | None = None,
        require_source_available: bool = False,
    ) -> OrdinaryMutationResult:
        state = str(state or "").strip().casefold()
        if state not in _UNAVAILABLE_STATES:
            raise OrdinaryMemoryMutationError("ordinary_source_state_invalid")
        return self._mutate(
            memory_id,
            operation=state,
            content=None,
            reason=reason,
            actor=actor,
            call_id=call_id,
            expected_project_id=expected_project_id,
            expected_content_hash=expected_content_hash,
            expected_source_snapshot=expected_source_snapshot,
            expected_peer_snapshots=expected_peer_snapshots,
            peer_metadata_replacements=peer_metadata_replacements,
            require_source_available=require_source_available,
            metadata_replacements=None,
            policy_replacements=None,
        )

    def _mutate(
        self,
        memory_id: str,
        *,
        operation: str,
        content: str | None,
        reason: str,
        actor: str,
        call_id: str,
        expected_project_id: str | None,
        expected_content_hash: str | None,
        expected_source_snapshot: Mapping[str, Any] | None,
        expected_peer_snapshots: Mapping[str, Mapping[str, Any]] | None,
        peer_metadata_replacements: Mapping[str, Mapping[str, Any]] | None,
        require_source_available: bool,
        metadata_replacements: Mapping[str, int | float] | None,
        policy_replacements: Mapping[str, Any] | None,
    ) -> OrdinaryMutationResult:
        memory_id, reason, actor, call_id = self._normalize_inputs(
            memory_id,
            reason=reason,
            actor=actor,
            call_id=call_id,
        )
        if not isinstance(require_source_available, bool):
            raise OrdinaryMemoryMutationError("ordinary_source_precondition_availability_invalid")
        (
            expected_project_id,
            expected_content_hash,
            expected_source_snapshot,
        ) = self._normalize_preconditions(
            expected_project_id=expected_project_id,
            expected_content_hash=expected_content_hash,
            expected_source_snapshot=expected_source_snapshot,
        )
        expected_peer_snapshots = self._normalize_peer_snapshots(
            memory_id,
            expected_peer_snapshots,
        )
        self._require_expected_peer_projects(
            expected_project_id,
            expected_peer_snapshots,
        )
        peer_metadata_replacements = self._normalize_peer_metadata_replacements(
            memory_id,
            expected_peer_snapshots,
            peer_metadata_replacements,
        )
        metadata_replacements = self._normalize_metadata_replacements(
            operation,
            metadata_replacements,
        )
        policy_replacements = self._normalize_policy_replacements(
            operation,
            policy_replacements,
        )
        storage = getattr(self.engine, "_sqlite", None)
        conn = getattr(storage, "_conn", None)
        batch = getattr(storage, "batch", None)
        patch = getattr(storage, "patch_ordinary", None)
        get = getattr(storage, "get", None)
        if conn is None or not callable(batch) or not callable(patch) or not callable(get):
            raise OrdinaryMemoryMutationError("ordinary_source_sqlite_required")

        prepared: PreparedMemory | None = None
        legacy_index_replacements: dict[str, Any] | None = None
        canonical: dict[str, Any] | None = None
        peer_canonicals: dict[str, dict[str, Any]] = {}
        result: OrdinaryMutationResult | None = None
        write_lock = getattr(self.engine, "_write_lock", None)
        if write_lock is None:
            raise OrdinaryMemoryMutationError("ordinary_source_lock_required")

        with write_lock:
            if conn.in_transaction:
                raise OrdinaryMemoryMutationError("ordinary_source_requires_clean_transaction")
            initial = self._require_ordinary_source(storage, memory_id)
            self._require_preconditions(
                initial,
                expected_project_id=expected_project_id,
                expected_content_hash=expected_content_hash,
                expected_source_snapshot=expected_source_snapshot,
                require_source_available=require_source_available,
            )
            initial_content_hash = synthesis_content_hash(initial.get("content"))
            initial_embedding_hash = str(initial.get("embedding_hash") or "")
            initial_correction_snapshot = None
            if operation == "corrected":
                initial_correction_snapshot = self._correction_snapshot(initial)
                try:
                    prepared = self._prepare_correction(initial, str(content or ""))
                except OrdinaryMemoryMutationError:
                    raise
                except (RuntimeError, ValueError) as exc:
                    raise OrdinaryMemoryMutationError("ordinary_source_preparation_failed") from exc
            elif not initial_embedding_hash.strip():
                initial_correction_snapshot = self._correction_snapshot(initial)
                legacy_index_replacements = self._legacy_index_replacements(initial)

            store = SynthesisStore(conn)
            with batch():
                # The result contract promises a committed version and
                # post-commit publication. Re-check the batch ownership to
                # close the gap between the clean-connection check and BEGIN.
                if not bool(getattr(storage, "_batch_owns_transaction", False)):
                    raise OrdinaryMemoryMutationError("ordinary_source_requires_clean_transaction")
                current = self._require_ordinary_source(storage, memory_id)
                self._require_preconditions(
                    current,
                    expected_project_id=expected_project_id,
                    expected_content_hash=expected_content_hash,
                    expected_source_snapshot=expected_source_snapshot,
                    require_source_available=require_source_available,
                )
                self._require_peer_preconditions(
                    storage,
                    expected_peer_snapshots,
                    source_project_id=str(current.get("project_id") or "").strip(),
                )
                previous_content_hash = synthesis_content_hash(current.get("content"))
                if (
                    previous_content_hash != initial_content_hash
                    or str(current.get("embedding_hash") or "") != initial_embedding_hash
                ):
                    raise OrdinaryMemoryMutationError("ordinary_source_cas_mismatch")
                if (
                    initial_correction_snapshot is not None
                    and self._correction_snapshot(current) != initial_correction_snapshot
                ):
                    raise OrdinaryMemoryMutationError("ordinary_source_cas_mismatch")
                changed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                if operation == "corrected":
                    assert prepared is not None
                    replacements = self._correction_replacements(
                        prepared,
                        previous_content_hash=previous_content_hash,
                        actor=actor,
                        call_id=call_id,
                        reason=reason,
                        changed_at=changed_at,
                    )
                    replacements.update(metadata_replacements)
                    replacements.update(policy_replacements)
                    canonical = patch(
                        memory_id,
                        replacements=replacements,
                        expected_content_hash=initial_content_hash,
                        expected_embedding_hash=initial_embedding_hash,
                        bump_memory_version=False,
                    )
                    ordinary_action = "upsert"
                else:
                    unavailable_source = current
                    if legacy_index_replacements is not None:
                        unavailable_source = dict(current)
                        unavailable_source["metadata_json"] = legacy_index_replacements[
                            "metadata_json"
                        ]
                    replacements = self._unavailable_replacements(
                        unavailable_source,
                        state=operation,
                        reason=reason,
                        actor=actor,
                        call_id=call_id,
                        changed_at=changed_at,
                    )
                    if legacy_index_replacements is not None:
                        replacements.update(
                            {
                                field: value
                                for field, value in legacy_index_replacements.items()
                                if field != "metadata_json"
                            }
                        )
                    canonical = patch(
                        memory_id,
                        replacements=replacements,
                        expected_content_hash=initial_content_hash,
                        expected_embedding_hash=initial_embedding_hash,
                        bump_memory_version=False,
                    )
                    ordinary_action = "delete"

                for peer_id, peer_metadata in peer_metadata_replacements.items():
                    expected_peer = expected_peer_snapshots[peer_id]
                    peer_snapshot = expected_peer["source_snapshot"]
                    peer_canonicals[peer_id] = patch(
                        peer_id,
                        replacements={"metadata_json": peer_metadata},
                        expected_project_id=expected_peer["project_id"],
                        expected_content_hash=expected_peer["content_hash"],
                        expected_embedding_hash=peer_snapshot.get("embedding_hash"),
                        expected_tags=peer_snapshot.get("tags"),
                        expected_category=peer_snapshot.get("category"),
                        require_source_available=True,
                        bump_memory_version=False,
                    )

                current_content_hash = synthesis_content_hash(canonical.get("content"))
                current_embedding_hash = str(canonical.get("embedding_hash") or "")
                record_memory_lineage(
                    conn,
                    memory_id=memory_id,
                    parent_memory_id=memory_id,
                    relation=f"ordinary_source_{operation}",
                    call_id=call_id,
                    metadata={
                        "actor": actor,
                        "reason": reason,
                        "previous_content_hash": previous_content_hash,
                        "current_content_hash": current_content_hash,
                        "previous_embedding_hash": initial_embedding_hash,
                        "current_embedding_hash": current_embedding_hash,
                        "changed_at": changed_at,
                    },
                )
                affected = store.stale_verified_dependents(
                    memory_id,
                    reason=_STALE_REASONS[operation],
                    actor=actor,
                    call_id=call_id,
                )
                if not increment_memory_version_if_present(conn):
                    raise OrdinaryMemoryMutationError("memory_version_unavailable")
                committed_memory_version = read_memory_version(conn)
                expected_embedding_hash = str(canonical.get("embedding_hash") or "").strip()
                if not expected_embedding_hash:
                    raise OrdinaryMemoryMutationError("ordinary_source_index_material_unavailable")
                project_id = str(canonical.get("project_id") or "").strip()
                if not project_id:
                    raise OrdinaryMemoryMutationError("ordinary_source_project_required")
                enqueue = (
                    enqueue_memory_index_upsert
                    if ordinary_action == "upsert"
                    else enqueue_memory_index_delete
                )
                ordinary_index_job_id = enqueue(
                    conn,
                    memory_id=memory_id,
                    project_id=project_id,
                    expected_embedding_hash=expected_embedding_hash,
                    call_id=call_id,
                )
                peer_index_job_ids: list[str] = []
                for peer_id, peer_canonical in peer_canonicals.items():
                    peer_project_id = str(peer_canonical.get("project_id") or "").strip()
                    peer_embedding_hash = str(peer_canonical.get("embedding_hash") or "").strip()
                    if not peer_project_id or not peer_embedding_hash:
                        raise OrdinaryMemoryMutationError(
                            "ordinary_source_index_material_unavailable"
                        )
                    peer_index_job_ids.append(
                        enqueue_memory_index_upsert(
                            conn,
                            memory_id=peer_id,
                            project_id=peer_project_id,
                            expected_embedding_hash=peer_embedding_hash,
                            call_id=call_id,
                        )
                    )
                from plastic_promise.core.synthesis_maintenance import (
                    enqueue_synthesis_index_job,
                )

                stale_synthesis_ids: list[str] = []
                synthesis_index_job_ids: list[str] = []
                for synthesis_id, revision, _project_id in affected:
                    stale_synthesis_ids.append(synthesis_id)
                    synthesis_index_job_ids.append(
                        enqueue_synthesis_index_job(
                            conn,
                            memory_id=synthesis_id,
                            revision=revision,
                            action="delete",
                            call_id=call_id,
                        )
                    )
                result = OrdinaryMutationResult(
                    memory_id=memory_id,
                    operation=operation,
                    previous_content_hash=previous_content_hash,
                    current_content_hash=current_content_hash,
                    committed_memory_version=committed_memory_version,
                    ordinary_index_job_id=ordinary_index_job_id,
                    peer_index_job_ids=tuple(peer_index_job_ids),
                    stale_synthesis_ids=tuple(stale_synthesis_ids),
                    synthesis_index_job_ids=tuple(synthesis_index_job_ids),
                )

        assert canonical is not None
        assert result is not None
        self._publish_after_commit(memory_id, canonical)
        for peer_id, peer_canonical in peer_canonicals.items():
            self._publish_after_commit(peer_id, peer_canonical)
        self._replay_after_commit(
            ordinary_job_count=1 + len(result.peer_index_job_ids),
            synthesis_job_count=len(result.synthesis_index_job_ids),
        )
        return result

    @staticmethod
    def _normalize_inputs(
        memory_id: str,
        *,
        reason: str,
        actor: str,
        call_id: str,
    ) -> tuple[str, str, str, str]:
        memory_id = str(memory_id or "").strip()
        reason = str(reason or "").strip()
        actor = str(actor or "").strip()
        call_id = str(call_id or "").strip()
        if not memory_id:
            raise OrdinaryMemoryMutationError("ordinary_source_id_required")
        if not reason:
            raise OrdinaryMemoryMutationError("ordinary_source_reason_required")
        if not actor or not call_id:
            raise OrdinaryMemoryMutationError("ordinary_source_evidence_required")
        return memory_id, reason, actor, call_id

    @staticmethod
    def _normalize_preconditions(
        *,
        expected_project_id: str | None,
        expected_content_hash: str | None,
        expected_source_snapshot: Mapping[str, Any] | None,
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        if expected_project_id is not None:
            expected_project_id = str(expected_project_id).strip()
            if not expected_project_id:
                raise OrdinaryMemoryMutationError("ordinary_source_expected_project_required")
        if expected_content_hash is not None:
            expected_content_hash = str(expected_content_hash).strip()
            if not expected_content_hash:
                raise OrdinaryMemoryMutationError("ordinary_source_expected_content_hash_required")
        if expected_source_snapshot is None:
            return expected_project_id, expected_content_hash, {}
        if not isinstance(expected_source_snapshot, Mapping):
            raise OrdinaryMemoryMutationError("ordinary_source_precondition_snapshot_invalid")
        unexpected = set(expected_source_snapshot) - _PRECONDITION_SNAPSHOT_FIELDS
        if unexpected:
            raise OrdinaryMemoryMutationError("ordinary_source_precondition_field_invalid")
        try:
            snapshot = copy.deepcopy(dict(expected_source_snapshot))
            json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise OrdinaryMemoryMutationError(
                "ordinary_source_precondition_snapshot_invalid"
            ) from exc
        return expected_project_id, expected_content_hash, snapshot

    @staticmethod
    def _require_preconditions(
        current: Mapping[str, Any],
        *,
        expected_project_id: str | None,
        expected_content_hash: str | None,
        expected_source_snapshot: Mapping[str, Any],
        require_source_available: bool,
    ) -> None:
        if expected_project_id is not None and (
            str(current.get("project_id") or "").strip() != expected_project_id
        ):
            raise OrdinaryMemoryMutationError("ordinary_source_precondition_mismatch")
        if expected_content_hash is not None and (
            synthesis_content_hash(current.get("content")) != expected_content_hash
        ):
            raise OrdinaryMemoryMutationError("ordinary_source_precondition_mismatch")
        if any(
            current.get(field) != expected for field, expected in expected_source_snapshot.items()
        ):
            raise OrdinaryMemoryMutationError("ordinary_source_precondition_mismatch")
        if require_source_available:
            try:
                from plastic_promise.core.synthesis_retrieval import (
                    _source_is_available,
                )

                available = _source_is_available(current)
            except Exception as exc:
                raise OrdinaryMemoryMutationError("ordinary_source_precondition_mismatch") from exc
            if not available:
                raise OrdinaryMemoryMutationError("ordinary_source_precondition_mismatch")

    @classmethod
    def _normalize_peer_snapshots(
        cls,
        memory_id: str,
        expected_peer_snapshots: Mapping[str, Mapping[str, Any]] | None,
    ) -> dict[str, dict[str, Any]]:
        if expected_peer_snapshots is None:
            return {}
        if not isinstance(expected_peer_snapshots, Mapping):
            raise OrdinaryMemoryMutationError("ordinary_source_peer_precondition_invalid")
        normalized: dict[str, dict[str, Any]] = {}
        required = {
            "content_hash",
            "metadata_json",
            "project_id",
            "tags",
            "worth_failure",
            "worth_success",
        }
        allowed = _PRECONDITION_SNAPSHOT_FIELDS | {"content_hash", "project_id"}
        for raw_peer_id, raw_snapshot in expected_peer_snapshots.items():
            peer_id = str(raw_peer_id or "").strip()
            if not peer_id or peer_id == memory_id or not isinstance(raw_snapshot, Mapping):
                raise OrdinaryMemoryMutationError("ordinary_source_peer_precondition_invalid")
            snapshot = dict(raw_snapshot)
            if (
                peer_id in normalized
                or not required <= set(snapshot)
                or not set(snapshot) <= allowed
                or not isinstance(snapshot.get("tags"), (list, tuple))
                or not all(isinstance(tag, str) for tag in snapshot["tags"])
                or not isinstance(snapshot.get("metadata_json"), Mapping)
            ):
                raise OrdinaryMemoryMutationError("ordinary_source_peer_precondition_invalid")
            project_id, content_hash, source_snapshot = cls._normalize_preconditions(
                expected_project_id=snapshot.pop("project_id"),
                expected_content_hash=snapshot.pop("content_hash"),
                expected_source_snapshot=snapshot,
            )
            if project_id is None or content_hash is None:
                raise OrdinaryMemoryMutationError("ordinary_source_peer_precondition_invalid")
            normalized[peer_id] = {
                "project_id": project_id,
                "content_hash": content_hash,
                "source_snapshot": source_snapshot,
            }
        return normalized

    @staticmethod
    def _require_expected_peer_projects(
        expected_project_id: str | None,
        expected_peer_snapshots: Mapping[str, Mapping[str, Any]],
    ) -> None:
        if expected_peer_snapshots and (
            expected_project_id is None
            or any(
                expected["project_id"] != expected_project_id
                for expected in expected_peer_snapshots.values()
            )
        ):
            raise OrdinaryMemoryMutationError("ordinary_source_peer_project_mismatch")

    @staticmethod
    def _normalize_metadata_replacements(
        operation: str,
        metadata_replacements: Mapping[str, int | float] | None,
    ) -> dict[str, int | float]:
        if metadata_replacements is None:
            return {}
        if operation != "corrected" or not isinstance(metadata_replacements, Mapping):
            raise OrdinaryMemoryMutationError("ordinary_source_metadata_replacements_invalid")
        replacements = dict(metadata_replacements)
        if not replacements or not set(replacements) <= (_CORRECTION_METADATA_REPLACEMENT_FIELDS):
            raise OrdinaryMemoryMutationError("ordinary_source_metadata_replacements_invalid")
        for value in replacements.values():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise OrdinaryMemoryMutationError("ordinary_source_metadata_replacements_invalid")
        return replacements

    @staticmethod
    def _normalize_policy_replacements(
        operation: str,
        policy_replacements: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if policy_replacements is None:
            return {}
        if operation != "corrected" or not isinstance(policy_replacements, Mapping):
            raise OrdinaryMemoryMutationError("ordinary_source_policy_replacements_invalid")
        replacements = copy.deepcopy(dict(policy_replacements))
        if not replacements or not set(replacements) <= _CORRECTION_POLICY_REPLACEMENT_FIELDS:
            raise OrdinaryMemoryMutationError("ordinary_source_policy_replacements_invalid")
        for field in ("category", "domain", "tier"):
            if field in replacements and not isinstance(replacements[field], str):
                raise OrdinaryMemoryMutationError("ordinary_source_policy_replacements_invalid")
        if "tags" in replacements and (
            not isinstance(replacements["tags"], (list, tuple))
            or not all(isinstance(tag, str) for tag in replacements["tags"])
        ):
            raise OrdinaryMemoryMutationError("ordinary_source_policy_replacements_invalid")
        if "tags" in replacements:
            replacements["tags"] = list(replacements["tags"])
        return replacements

    @staticmethod
    def _normalize_peer_metadata_replacements(
        memory_id: str,
        expected_peer_snapshots: Mapping[str, Mapping[str, Any]],
        peer_metadata_replacements: Mapping[str, Mapping[str, Any]] | None,
    ) -> dict[str, dict[str, Any]]:
        if peer_metadata_replacements is None:
            return {}
        if not isinstance(peer_metadata_replacements, Mapping):
            raise OrdinaryMemoryMutationError("ordinary_source_peer_metadata_replacements_invalid")
        replacements: dict[str, dict[str, Any]] = {}
        for raw_peer_id, raw_metadata in peer_metadata_replacements.items():
            peer_id = str(raw_peer_id or "").strip()
            if (
                not peer_id
                or peer_id == memory_id
                or peer_id not in expected_peer_snapshots
                or peer_id in replacements
                or not isinstance(raw_metadata, Mapping)
            ):
                raise OrdinaryMemoryMutationError(
                    "ordinary_source_peer_metadata_replacements_invalid"
                )
            try:
                metadata = copy.deepcopy(dict(raw_metadata))
                json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise OrdinaryMemoryMutationError(
                    "ordinary_source_peer_metadata_replacements_invalid"
                ) from exc
            replacements[peer_id] = metadata
        if not replacements:
            raise OrdinaryMemoryMutationError("ordinary_source_peer_metadata_replacements_invalid")
        return replacements

    def _require_peer_preconditions(
        self,
        storage: Any,
        expected_peer_snapshots: Mapping[str, Mapping[str, Any]],
        *,
        source_project_id: str,
    ) -> None:
        for peer_id, expected in expected_peer_snapshots.items():
            try:
                peer = self._require_ordinary_source(storage, peer_id)
            except OrdinaryMemoryMutationError as exc:
                raise OrdinaryMemoryMutationError("ordinary_source_precondition_mismatch") from exc
            peer_project_id = str(peer.get("project_id") or "").strip()
            if not source_project_id or not peer_project_id or peer_project_id != source_project_id:
                raise OrdinaryMemoryMutationError("ordinary_source_peer_project_mismatch")
            try:
                self._require_preconditions(
                    peer,
                    expected_project_id=str(expected["project_id"]),
                    expected_content_hash=str(expected["content_hash"]),
                    expected_source_snapshot=expected["source_snapshot"],
                    require_source_available=True,
                )
            except OrdinaryMemoryMutationError as exc:
                raise OrdinaryMemoryMutationError("ordinary_source_precondition_mismatch") from exc

    def _require_ordinary_source(self, storage: Any, memory_id: str) -> dict[str, Any]:
        current = storage.get(memory_id)
        if not isinstance(current, dict):
            raise OrdinaryMemoryMutationError("ordinary_source_not_found")
        conn = storage._conn
        if is_governed_synthesis_memory(
            conn,
            memory_id,
            memory_type=current.get("memory_type"),
        ):
            raise OrdinaryMemoryMutationError("ordinary_source_reserved")
        return current

    @staticmethod
    def _correction_snapshot(current: dict[str, Any]) -> str:
        tags = current.get("tags")
        metadata = current.get("metadata_json")
        if not isinstance(tags, (list, tuple)) or not isinstance(metadata, dict):
            raise OrdinaryMemoryMutationError("ordinary_source_snapshot_invalid")
        values = {
            field: current.get(field)
            for field in (
                "content",
                "category",
                "tier",
                "domain",
                "importance",
                "raw_content",
                "l0_abstract",
                "l1_summary",
                "l2_content",
                "embedding_text",
                "embedding_hash",
                "search_text",
            )
        }
        values["tags"] = list(tags)
        values["metadata_json"] = metadata
        try:
            return json.dumps(
                values,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise OrdinaryMemoryMutationError("ordinary_source_snapshot_invalid") from exc

    def _prepare_correction(
        self,
        current: dict[str, Any],
        content: str,
    ) -> PreparedMemory:
        ensure_heavy_init = getattr(self.engine, "ensure_heavy_init", None)
        if callable(ensure_heavy_init):
            ensure_heavy_init()
        pipeline = None
        get_pipeline = getattr(self.engine, "get_fuzzy_buffer", None)
        if callable(get_pipeline):
            pipeline = get_pipeline()
        if pipeline is None:
            pipeline = MemoryPipeline(
                embedder=getattr(self.engine, "_embedder", None),
                tier_manager=getattr(self.engine, "_tier_manager", None),
                domain_manager=getattr(self.engine, "_dm", None),
                lancedb=getattr(self.engine, "_ldb", None),
            )
        return pipeline.prepare_correction(current, content)

    @staticmethod
    def _legacy_index_replacements(current: Mapping[str, Any]) -> dict[str, Any]:
        """Materialize a deterministic pre-v2 contract for a checked delete."""
        from plastic_promise.core.memory_index import (
            LEGACY_FALLBACK_POLICY,
            build_index_material,
            metadata_with_index_material,
        )

        content = str(current.get("content") or "")
        if not content.strip():
            raise OrdinaryMemoryMutationError("ordinary_source_index_material_unavailable")
        try:
            material = build_index_material(
                {"content": content},
                policy=LEGACY_FALLBACK_POLICY,
                model_name="unknown",
            )
            metadata = metadata_with_index_material(
                current.get("metadata_json"),
                material,
            )
        except (TypeError, ValueError) as exc:
            raise OrdinaryMemoryMutationError("ordinary_source_index_material_unavailable") from exc
        return {
            "embedding_text": material.vector_text,
            "embedding_hash": material.embedding_hash,
            "search_text": material.search_text,
            "metadata_json": metadata,
        }

    @staticmethod
    def _correction_replacements(
        prepared: PreparedMemory,
        *,
        previous_content_hash: str,
        actor: str,
        call_id: str,
        reason: str,
        changed_at: str,
    ) -> dict[str, Any]:
        metadata = dict(prepared.metadata)
        current_content_hash = synthesis_content_hash(prepared.content)
        quality = metadata.get("quality")
        quality = dict(quality) if isinstance(quality, dict) else {}
        quality.update(
            {
                "status": "current",
                "reason": reason,
                "actor": actor,
                "call_id": call_id,
                "changed_at": changed_at,
            }
        )
        metadata["quality"] = quality
        metadata["last_correction"] = {
            "previous_content_hash": previous_content_hash,
            "current_content_hash": current_content_hash,
            "call_id": call_id,
        }
        return {
            "content": prepared.content,
            "category": prepared.category,
            "tier": prepared.tier,
            "tags": list(prepared.tags),
            "metadata_json": metadata,
            "raw_content": str(metadata.get("raw_content") or prepared.content),
            "l0_abstract": str(metadata.get("l0_abstract") or ""),
            "l1_summary": str(metadata.get("l1_summary") or ""),
            "l2_content": str(metadata.get("l2_content") or prepared.content),
            "embedding_text": prepared.index_material.vector_text,
            "embedding_hash": prepared.index_material.embedding_hash,
            "search_text": prepared.index_material.search_text,
        }

    @staticmethod
    def _unavailable_replacements(
        current: dict[str, Any],
        *,
        state: str,
        reason: str,
        actor: str,
        call_id: str,
        changed_at: str,
    ) -> dict[str, Any]:
        source_tags = current.get("tags")
        if not isinstance(source_tags, (list, tuple)):
            raise OrdinaryMemoryMutationError("ordinary_source_tags_invalid")
        tags: list[str] = []
        for value in source_tags:
            tag = str(value).strip()
            prefix, separator, tag_state = tag.partition(":")
            if (
                separator
                and prefix.casefold() in {"lifecycle", "quality", "status"}
                and tag_state.strip().casefold() in _BLOCKED_TAG_STATES
            ):
                continue
            if tag.casefold() == "decay:pending":
                continue
            if tag:
                tags.append(tag)
        tags.append(f"status:{state}")
        if state == "forgotten":
            tags.append("decay:pending")

        raw_metadata = current.get("metadata_json")
        if not isinstance(raw_metadata, dict):
            raise OrdinaryMemoryMutationError("ordinary_source_metadata_invalid")
        metadata = dict(raw_metadata)
        metadata.update(
            {
                "lifecycle_status": state,
                "quality": {
                    "status": state,
                    "reason": reason,
                    "actor": actor,
                    "call_id": call_id,
                    "changed_at": changed_at,
                },
                "source_unavailable_reason": reason,
            }
        )
        current_failures = current.get("worth_failure", 0)
        try:
            failures = max(float(current_failures or 0), 10.0)
        except (TypeError, ValueError) as exc:
            raise OrdinaryMemoryMutationError("ordinary_source_worth_invalid") from exc
        return {
            "tags": list(dict.fromkeys(tags)),
            "metadata_json": metadata,
            "importance": 0.0,
            "activation_weight": 0.0,
            "worth_success": 0,
            "worth_failure": failures,
            "decay_multiplier": 0.0,
            "last_accessed": changed_at,
        }

    def _publish_after_commit(self, memory_id: str, canonical: dict[str, Any]) -> None:
        refresh = getattr(self.engine, "_refresh_canonical_cache_if_changed", None)
        if callable(refresh):
            try:
                if refresh(force=True):
                    return
            except Exception:
                pass
        memories = getattr(self.engine, "_memories", None)
        if isinstance(memories, dict):
            memories[memory_id] = copy.deepcopy(canonical)

    def _replay_after_commit(
        self,
        *,
        ordinary_job_count: int,
        synthesis_job_count: int,
    ) -> None:
        from plastic_promise.core.synthesis_maintenance import (
            replay_memory_index_jobs,
            replay_synthesis_index_jobs,
        )

        with suppress(Exception):
            replay_memory_index_jobs(self.engine, limit=ordinary_job_count)
        if synthesis_job_count:
            with suppress(Exception):
                replay_synthesis_index_jobs(
                    self.engine,
                    limit=synthesis_job_count,
                )
