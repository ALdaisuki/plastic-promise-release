from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SYNTHESIS_STATUSES = frozenset({"draft", "verified", "contested", "stale"})
VISIBILITY_RANK = {"private": 0, "project": 1, "shared": 2, "global": 3}
SYNTHESIS_BINDING_SCHEMA = "synthesis-binding/v1"


class SynthesisConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class SynthesisArtifact:
    memory_id: str
    synthesis_key: str
    status: str
    revision: int
    support_count: int
    validity_scope: str
    source_fingerprint: str
    last_verified_at: str
    last_linted_at: str
    stale_reason: str
    created_by_call_id: str
    verified_by_actor: str
    verified_by_call_id: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    content: str
    project_id: str
    visibility: str
    source_ids: tuple[str, ...]


def ensure_synthesis_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS synthesis_artifacts (
            memory_id TEXT PRIMARY KEY,
            synthesis_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            support_count INTEGER NOT NULL DEFAULT 0,
            validity_scope TEXT NOT NULL DEFAULT '',
            source_fingerprint TEXT NOT NULL DEFAULT '',
            last_verified_at TEXT NOT NULL DEFAULT '',
            last_linted_at TEXT NOT NULL DEFAULT '',
            stale_reason TEXT NOT NULL DEFAULT '',
            created_by_call_id TEXT NOT NULL DEFAULT '',
            verified_by_actor TEXT NOT NULL DEFAULT '',
            verified_by_call_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_synthesis_artifacts_status_updated_at
        ON synthesis_artifacts(status, updated_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_synthesis_artifacts_synthesis_key
        ON synthesis_artifacts(synthesis_key)
        """
    )


def canonical_memory_hash(memory: Mapping[str, Any]) -> str:
    stable = {
        "id": str(memory.get("id", "")),
        "content": str(memory.get("content", "")),
        "project_id": str(memory.get("project_id", "")),
        "visibility": str(memory.get("visibility", "project")),
        "origin_kind": str(memory.get("origin_kind", "")),
        "origin_uri": str(memory.get("origin_uri", "")),
        "origin_ref": str(memory.get("origin_ref", "")),
        "origin_hash": str(memory.get("origin_hash", "")),
        "embedding_hash": str(memory.get("embedding_hash", "")),
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def synthesis_content_hash(content: object) -> str:
    return "sha256:" + hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def synthesis_index_material_hash(material: Any) -> str:
    payload = {
        "policy": str(material.policy),
        "vector_text": str(material.vector_text),
        "search_text": str(material.search_text),
        "embedding_hash": str(material.embedding_hash),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_synthesis_binding(memory: Mapping[str, Any], material: Any) -> dict[str, Any]:
    metadata = _mapping_from_json(memory.get("metadata_json"))
    return {
        "schema": SYNTHESIS_BINDING_SCHEMA,
        "memory_type": str(memory.get("memory_type") or "").strip().casefold(),
        "source_class": str(memory.get("source_class") or "").strip().casefold(),
        "origin_kind": str(memory.get("origin_kind") or "").strip().casefold(),
        "synthesis_key": str(metadata.get("synthesis_key") or ""),
        "synthesis_revision": metadata.get("synthesis_revision"),
        "project_id": str(memory.get("project_id") or ""),
        "visibility": str(memory.get("visibility") or ""),
        "content_hash": synthesis_content_hash(memory.get("content")),
        "origin_hash": str(memory.get("origin_hash") or ""),
        "index_material_hash": synthesis_index_material_hash(material),
    }


def synthesis_binding_hash(binding: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(binding),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping_from_json(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def source_fingerprint(snapshots: Mapping[str, str]) -> str:
    pairs = [f"{source_id}:{content_hash}" for source_id, content_hash in snapshots.items()]
    payload = "\n".join(sorted(pairs))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def visibility_allows(derived: str, sources: Iterable[str]) -> bool:
    try:
        return VISIBILITY_RANK[derived] <= min(VISIBILITY_RANK[value] for value in sources)
    except (KeyError, ValueError):
        return False


class SynthesisStore:
    def __init__(self, conn: Any, engine: Any = None) -> None:
        self.conn = conn
        self.engine = engine
        ensure_synthesis_schema(conn)
        from plastic_promise.core.traceability import ensure_traceability_schema

        ensure_traceability_schema(conn)

    def create_draft(
        self,
        content: str,
        source_ids: Iterable[str],
        *,
        synthesis_key: str,
        validity_scope: str,
        project_id: str = "project:legacy-global",
        visibility: str = "project",
        actor: str = "",
        call_id: str = "",
        automatic: bool = False,
        reuse_signal: bool = False,
        audit_synthesis: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> SynthesisArtifact | None:
        mode = os.environ.get("PP_SYNTHESIS_ARTIFACTS", "off")
        if mode not in {"off", "shadow", "on"}:
            raise SynthesisConflict("unknown_synthesis_mode")
        if mode == "off":
            raise SynthesisConflict("synthesis_artifacts_disabled")

        normalized_ids = self._normalize_source_ids(source_ids)
        project_id = str(project_id or "").strip()
        self._validate_basic_inputs(
            content,
            synthesis_key,
            validity_scope,
            normalized_ids,
            project_id,
        )
        if mode == "shadow":
            try:
                sources, snapshots = self._validate_sources(
                    normalized_ids,
                    project_id=project_id,
                    visibility=visibility,
                )
                self._validate_automatic(
                    content,
                    sources,
                    automatic=automatic,
                    reuse_signal=reuse_signal,
                    audit_synthesis=audit_synthesis,
                )
                self._reject_existing_key(synthesis_key)
            except SynthesisConflict as exc:
                self._record_shadow_diagnostic(
                    call_id=call_id,
                    content=content,
                    synthesis_key=synthesis_key,
                    source_count=len(normalized_ids),
                    fingerprint="",
                    reason=str(exc),
                )
                raise
            self._record_shadow_diagnostic(
                call_id=call_id,
                content=content,
                synthesis_key=synthesis_key,
                source_count=len(normalized_ids),
                fingerprint=source_fingerprint(snapshots),
                reason="eligible",
            )
            return None

        owns_transaction = not self.conn.in_transaction
        memory_id = f"synthesis:{secrets.token_hex(12)}"
        try:
            with self._transaction():
                sources, snapshots = self._validate_sources(
                    normalized_ids,
                    project_id=project_id,
                    visibility=visibility,
                )
                self._validate_automatic(
                    content,
                    sources,
                    automatic=automatic,
                    reuse_signal=reuse_signal,
                    audit_synthesis=audit_synthesis,
                )
                self._reject_existing_key(synthesis_key)
                now = self._utc_now()
                binding_state = self._insert_memory(
                    memory_id=memory_id,
                    content=content.strip(),
                    source_ids=normalized_ids,
                    project_id=project_id,
                    visibility=visibility,
                    actor=actor,
                    call_id=call_id,
                    synthesis_key=synthesis_key.strip(),
                    revision=1,
                    now=now,
                    metadata=metadata,
                )
                control_metadata = {
                    "automatic": bool(automatic),
                    "audit_synthesis": bool(audit_synthesis),
                    "project_id": project_id,
                    "reuse_signal": bool(reuse_signal),
                    "visibility": visibility,
                    **binding_state,
                }
                if actor.strip():
                    control_metadata["created_by_actor"] = actor.strip()
                self.conn.execute(
                    """
                    INSERT INTO synthesis_artifacts (
                        memory_id, synthesis_key, status, revision, support_count,
                        validity_scope, source_fingerprint, created_by_call_id,
                        metadata_json, created_at, updated_at
                    )
                    VALUES (?, ?, 'draft', 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        synthesis_key.strip(),
                        len(normalized_ids),
                        validity_scope.strip(),
                        source_fingerprint(snapshots),
                        call_id.strip(),
                        self._json(control_metadata),
                        now,
                        now,
                    ),
                )
                self._insert_graph_node(memory_id, content.strip(), now)
                for source in sources:
                    self._insert_derived_edge(
                        memory_id,
                        source,
                        snapshots[str(source["id"])],
                        call_id=call_id,
                        validity_scope=validity_scope,
                        now=now,
                        revision=1,
                    )
                self._increment_memory_version()
        except sqlite3.IntegrityError as exc:
            raise SynthesisConflict("synthesis_key_conflict") from exc

        self._refresh_engine_after_commit(owns_transaction)
        artifact = self.get(memory_id)
        if artifact is None:
            raise SynthesisConflict("synthesis_create_unavailable")
        return artifact

    def verify(
        self,
        memory_id: str,
        actor: str,
        call_id: str,
        expected_revision: int,
    ) -> SynthesisArtifact:
        if not actor.strip() or not call_id.strip():
            raise SynthesisConflict("missing_verification_evidence")
        owns_transaction = not self.conn.in_transaction
        with self._transaction():
            artifact = self._require_artifact(memory_id)
            self._require_revision(artifact, expected_revision)
            if artifact.status != "draft":
                raise SynthesisConflict("transition_not_allowed")
            findings = self.lint(memory_id=memory_id)
            if findings:
                raise SynthesisConflict(findings[0]["code"])
            from plastic_promise.core.memory_index import read_persisted_index_material
            from plastic_promise.core.noise_filter import is_noise
            from plastic_promise.core.synthesis_retrieval import (
                _GateFailure,
                _validate_synthesis,
            )

            try:
                _validate_synthesis(self.conn, memory_id, allow_review=True)
            except _GateFailure as exc:
                raise SynthesisConflict(exc.reason) from exc

            memory = self._load_memory(memory_id)
            if memory is None or is_noise(str(memory.get("content") or "")):
                raise SynthesisConflict("synthesis_content_quality_failed")
            if read_persisted_index_material(memory) is None:
                raise SynthesisConflict("synthesis_index_material_invalid")
            now = self._utc_now()
            metadata = dict(artifact.metadata)
            metadata["last_transition"] = "verified"
            self._cas_transition(
                artifact,
                new_status="verified",
                new_revision=artifact.revision,
                now=now,
                metadata=metadata,
                extra={
                    "last_verified_at": now,
                    "stale_reason": "",
                    "verified_by_actor": actor.strip(),
                    "verified_by_call_id": call_id.strip(),
                },
            )
            self._increment_memory_version()
            from plastic_promise.core.synthesis_maintenance import (
                enqueue_synthesis_index_job,
            )

            enqueue_synthesis_index_job(
                self.conn,
                memory_id=artifact.memory_id,
                revision=artifact.revision,
                action="upsert",
                call_id=call_id,
            )
        self._refresh_engine_after_commit(owns_transaction)
        if owns_transaction and self.engine is not None:
            from plastic_promise.core.synthesis_maintenance import (
                replay_synthesis_index_jobs,
            )

            replay_synthesis_index_jobs(self.engine, limit=1)
        return self._require_artifact(memory_id)

    def mark_contested(
        self,
        memory_id: str,
        reason: str,
        expected_revision: int,
        *,
        actor: str = "",
        call_id: str = "",
    ) -> SynthesisArtifact:
        return self._mark_blocked(
            memory_id,
            reason,
            expected_revision,
            new_status="contested",
            allowed_statuses={"draft", "verified"},
            actor=actor,
            call_id=call_id,
        )

    def mark_stale(
        self,
        memory_id: str,
        reason: str,
        expected_revision: int,
        *,
        actor: str = "",
        call_id: str = "",
    ) -> SynthesisArtifact:
        return self._mark_blocked(
            memory_id,
            reason,
            expected_revision,
            new_status="stale",
            allowed_statuses={"verified"},
            actor=actor,
            call_id=call_id,
        )

    def stale_verified_dependents(
        self,
        source_id: str,
        *,
        reason: str,
        actor: str,
        call_id: str,
    ) -> tuple[tuple[str, int, str], ...]:
        """Stale every verified synthesis that depends on one ordinary source.

        The caller owns transaction lifecycle, global-version advancement, and
        derived-index publication.  Keeping those effects outside this helper
        lets one source mutation commit as a single versioned unit.
        """
        source_id = str(source_id or "").strip()
        reason = str(reason or "").strip()
        if not source_id:
            raise SynthesisConflict("missing_source_id")
        if not reason:
            raise SynthesisConflict("missing_transition_reason")

        rows = self.conn.execute(
            """
            WITH RECURSIVE dependent_ids(memory_id) AS (
                SELECT behavior_graph_edges.source
                FROM behavior_graph_edges
                WHERE behavior_graph_edges.target = ?
                  AND behavior_graph_edges.relation = 'derived_from'
                UNION
                SELECT behavior_graph_edges.source
                FROM behavior_graph_edges
                JOIN dependent_ids
                  ON behavior_graph_edges.target = dependent_ids.memory_id
                WHERE behavior_graph_edges.relation = 'derived_from'
            )
            SELECT synthesis_artifacts.memory_id
            FROM synthesis_artifacts
            JOIN dependent_ids
              ON dependent_ids.memory_id = synthesis_artifacts.memory_id
            WHERE synthesis_artifacts.status = 'verified'
            ORDER BY synthesis_artifacts.memory_id
            """,
            (source_id,),
        ).fetchall()
        if not rows:
            return ()

        from plastic_promise.core.traceability import record_memory_lineage

        affected: list[tuple[str, int, str]] = []
        for row in rows:
            artifact = self._require_artifact(str(row[0]))
            if artifact.status != "verified":
                continue
            now = self._utc_now()
            metadata = dict(artifact.metadata)
            metadata["last_transition"] = "stale"
            metadata["transition_reason"] = reason
            if actor.strip():
                metadata["transition_actor"] = actor.strip()
            if call_id.strip():
                metadata["transition_call_id"] = call_id.strip()
            self._cas_transition(
                artifact,
                new_status="stale",
                new_revision=artifact.revision,
                now=now,
                metadata=metadata,
                extra={
                    "stale_reason": reason,
                    "last_verified_at": "",
                    "verified_by_actor": "",
                    "verified_by_call_id": "",
                },
            )
            record_memory_lineage(
                self.conn,
                memory_id=artifact.memory_id,
                parent_memory_id=source_id,
                relation="synthesis_invalidated",
                call_id=call_id.strip(),
                metadata={
                    "reason": reason,
                    "source_id": source_id,
                    "synthesis_revision": artifact.revision,
                },
            )
            affected.append((artifact.memory_id, artifact.revision, artifact.project_id))
        return tuple(affected)

    def refresh(
        self,
        memory_id: str,
        content: str,
        source_ids: Iterable[str],
        expected_revision: int,
        *,
        validity_scope: str | None = None,
        project_id: str | None = None,
        visibility: str | None = None,
        actor: str = "",
        call_id: str = "",
        automatic: bool = False,
        reuse_signal: bool = False,
        audit_synthesis: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> SynthesisArtifact:
        normalized_ids = self._normalize_source_ids(source_ids)
        if not content.strip():
            raise SynthesisConflict("missing_synthesis_content")
        if len(normalized_ids) < 2:
            raise SynthesisConflict("insufficient_distinct_sources")
        owns_transaction = not self.conn.in_transaction
        with self._transaction():
            artifact = self._require_artifact(memory_id)
            self._require_revision(artifact, expected_revision)
            if artifact.status not in {"stale", "contested"}:
                raise SynthesisConflict("transition_not_allowed")
            effective_scope = (
                validity_scope.strip() if validity_scope is not None else artifact.validity_scope
            )
            if not effective_scope:
                raise SynthesisConflict("missing_validity_scope")
            effective_project = (
                artifact.project_id if project_id is None else str(project_id).strip()
            )
            if not effective_project:
                raise SynthesisConflict("missing_project_id")
            effective_visibility = visibility or artifact.visibility
            sources, snapshots = self._validate_sources(
                normalized_ids,
                project_id=effective_project,
                visibility=effective_visibility,
            )
            self._validate_automatic(
                content,
                sources,
                automatic=automatic,
                reuse_signal=reuse_signal,
                audit_synthesis=audit_synthesis,
            )
            new_revision = artifact.revision + 1
            now = self._utc_now()
            binding_state = self._update_memory_for_refresh(
                artifact,
                content=content.strip(),
                source_ids=normalized_ids,
                project_id=effective_project,
                visibility=effective_visibility,
                actor=actor,
                call_id=call_id,
                revision=new_revision,
                now=now,
                metadata=metadata,
            )
            self._insert_graph_node(memory_id, content.strip(), now)
            self.conn.execute(
                "DELETE FROM behavior_graph_edges WHERE source = ? AND relation = 'derived_from'",
                (memory_id,),
            )
            for source in sources:
                self._insert_derived_edge(
                    memory_id,
                    source,
                    snapshots[str(source["id"])],
                    call_id=call_id,
                    validity_scope=effective_scope,
                    now=now,
                    revision=new_revision,
                )
            control_metadata = dict(artifact.metadata)
            control_metadata.update(
                {
                    "automatic": bool(automatic),
                    "audit_synthesis": bool(audit_synthesis),
                    "reuse_signal": bool(reuse_signal),
                    "last_transition": "refreshed",
                    "project_id": effective_project,
                    "visibility": effective_visibility,
                    **binding_state,
                }
            )
            self._cas_transition(
                artifact,
                new_status="draft",
                new_revision=new_revision,
                now=now,
                metadata=control_metadata,
                extra={
                    "support_count": len(normalized_ids),
                    "validity_scope": effective_scope,
                    "source_fingerprint": source_fingerprint(snapshots),
                    "last_verified_at": "",
                    "stale_reason": "",
                    "verified_by_actor": "",
                    "verified_by_call_id": "",
                },
            )
            self._increment_memory_version()
            from plastic_promise.core.synthesis_maintenance import (
                enqueue_synthesis_index_job,
            )

            enqueue_synthesis_index_job(
                self.conn,
                memory_id=artifact.memory_id,
                revision=artifact.revision,
                action="delete",
                call_id=call_id,
            )
        self._refresh_engine_after_commit(owns_transaction)
        return self._require_artifact(memory_id)

    def get(self, memory_id: str) -> SynthesisArtifact | None:
        cursor = self.conn.execute(
            """
            SELECT sa.memory_id, sa.synthesis_key, sa.status, sa.revision,
                   sa.support_count, sa.validity_scope, sa.source_fingerprint,
                   sa.last_verified_at, sa.last_linted_at, sa.stale_reason,
                   sa.created_by_call_id, sa.verified_by_actor,
                   sa.verified_by_call_id, sa.metadata_json, sa.created_at,
                   sa.updated_at, m.content, m.project_id, m.visibility
            FROM synthesis_artifacts AS sa
            LEFT JOIN memories AS m ON m.id = sa.memory_id
            WHERE sa.memory_id = ?
            """,
            (memory_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        source_ids = tuple(
            str(edge[0])
            for edge in self.conn.execute(
                "SELECT DISTINCT target FROM behavior_graph_edges "
                "WHERE source = ? AND relation = 'derived_from' ORDER BY target",
                (memory_id,),
            ).fetchall()
        )
        return SynthesisArtifact(
            memory_id=str(row[0]),
            synthesis_key=str(row[1]),
            status=str(row[2]),
            revision=int(row[3]),
            support_count=int(row[4]),
            validity_scope=str(row[5]),
            source_fingerprint=str(row[6]),
            last_verified_at=str(row[7]),
            last_linted_at=str(row[8]),
            stale_reason=str(row[9]),
            created_by_call_id=str(row[10]),
            verified_by_actor=str(row[11]),
            verified_by_call_id=str(row[12]),
            metadata=self._json_object(row[13]),
            created_at=str(row[14]),
            updated_at=str(row[15]),
            content=str(row[16] or ""),
            project_id=str(row[17] or ""),
            visibility=str(row[18] or ""),
            source_ids=source_ids,
        )

    def list(
        self,
        status: str | None = None,
        project_id: str | None = None,
    ) -> list[SynthesisArtifact]:
        if status is not None and status not in SYNTHESIS_STATUSES:
            raise SynthesisConflict("unknown_synthesis_status")
        clauses: list[str] = []
        values: list[str] = []
        if status is not None:
            clauses.append("sa.status = ?")
            values.append(status)
        if project_id is not None:
            clauses.append("m.project_id = ?")
            values.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT sa.memory_id
            FROM synthesis_artifacts AS sa
            JOIN memories AS m ON m.id = sa.memory_id
            {where}
            ORDER BY sa.updated_at, sa.memory_id
            """,
            values,
        ).fetchall()
        return [artifact for row in rows if (artifact := self.get(str(row[0]))) is not None]

    def lint(
        self,
        memory_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from plastic_promise.core.synthesis_retrieval import (
            _GateFailure,
            _source_is_available,
            _validate_synthesis,
        )

        findings: list[dict[str, Any]] = []
        control_rows = self.conn.execute(
            "SELECT memory_id, metadata_json FROM synthesis_artifacts"
        ).fetchall()
        control_ids = {str(row[0]) for row in control_rows}
        control_projects = {
            str(row[0]): str(self._json_object(row[1]).get("project_id") or "")
            for row in control_rows
        }
        memory_rows = self.conn.execute(
            "SELECT id, project_id FROM memories WHERE lower(trim(memory_type)) = 'synthesis'"
        ).fetchall()
        synthesis_memory_ids = {str(row[0]) for row in memory_rows}
        memory_projects = {str(row[0]): str(row[1] or "") for row in memory_rows}
        candidate_ids = sorted(control_ids | synthesis_memory_ids)
        if memory_id is not None:
            candidate_ids = [item_id for item_id in candidate_ids if item_id == memory_id]

        for item_id in candidate_ids:
            has_control = item_id in control_ids
            has_memory = item_id in synthesis_memory_ids
            if project_id is not None and has_memory and memory_projects[item_id] != project_id:
                continue
            if (
                project_id is not None
                and has_control
                and not has_memory
                and control_projects.get(item_id) != project_id
            ):
                continue
            if has_control and not has_memory:
                self._add_finding(findings, "SYNTHESIS_ORPHAN_CONTROL", item_id)
                continue
            if has_memory and not has_control:
                self._add_finding(findings, "SYNTHESIS_ORPHAN_MEMORY", item_id)
                continue

            artifact = self.get(item_id)
            memory = self._load_memory(item_id)
            if artifact is None or memory is None:
                continue
            edge_rows = self.conn.execute(
                """
                SELECT target, metadata_json
                FROM behavior_graph_edges
                WHERE source = ? AND relation = 'derived_from'
                ORDER BY target, metadata_json
                """,
                (item_id,),
            ).fetchall()
            observed: dict[str, str] = {}
            source_visibilities: list[str] = []
            source_ids: set[str] = set()
            for source_id_raw, metadata_json in edge_rows:
                source_id = str(source_id_raw)
                source_ids.add(source_id)
                edge_metadata = self._json_object(metadata_json)
                observed_hash = str(edge_metadata.get("observed_content_hash") or "")
                if observed_hash:
                    observed[source_id] = observed_hash
                source = self._load_memory(source_id)
                if source is None:
                    self._add_finding(
                        findings,
                        "SYNTHESIS_SOURCE_MISSING",
                        item_id,
                        source_id=source_id,
                    )
                    continue
                source_visibilities.append(str(source.get("visibility") or ""))
                if not observed_hash or canonical_memory_hash(source) != observed_hash:
                    self._add_finding(
                        findings,
                        "SYNTHESIS_SOURCE_CHANGED",
                        item_id,
                        source_id=source_id,
                    )
                try:
                    source_available = _source_is_available(source)
                    source_control = self.conn.execute(
                        "SELECT revision FROM synthesis_artifacts WHERE memory_id = ?",
                        (source_id,),
                    ).fetchone()
                    source_is_synthesis = (
                        str(source.get("memory_type") or "").strip().casefold() == "synthesis"
                    )
                    if source_available and (source_is_synthesis or source_control is not None):
                        _validate_synthesis(self.conn, source_id, allow_review=False)
                        source_revision = source_control[0] if source_control is not None else None
                        observed_revision = edge_metadata.get("source_revision")
                        if (
                            type(source_revision) is not int
                            or source_revision < 1
                            or type(observed_revision) is not int
                            or observed_revision != source_revision
                        ):
                            source_available = False
                except _GateFailure:
                    source_available = False
                if not source_available:
                    self._add_finding(
                        findings,
                        "SYNTHESIS_SOURCE_CHANGED",
                        item_id,
                        source_id=source_id,
                    )
                superseded = self.conn.execute(
                    "SELECT 1 FROM behavior_graph_edges "
                    "WHERE relation = 'supersedes' AND target = ? LIMIT 1",
                    (source_id,),
                ).fetchone()
                if superseded:
                    self._add_finding(
                        findings,
                        "SYNTHESIS_SOURCE_SUPERSEDED",
                        item_id,
                        source_id=source_id,
                    )

            if (
                artifact.support_count < 2
                or len(source_ids) < 2
                or artifact.support_count != len(source_ids)
            ):
                self._add_finding(findings, "SYNTHESIS_SUPPORT_MISMATCH", item_id)
            if artifact.source_fingerprint != source_fingerprint(observed):
                self._add_finding(findings, "SYNTHESIS_FINGERPRINT_MISMATCH", item_id)
            placeholders = ",".join("?" for _ in range(len(source_ids) + 1))
            relevant_ids = [item_id, *sorted(source_ids)]
            contradiction = self.conn.execute(
                f"SELECT 1 FROM behavior_graph_edges WHERE relation = 'contradicts' "
                f"AND (source IN ({placeholders}) OR target IN ({placeholders})) LIMIT 1",
                [*relevant_ids, *relevant_ids],
            ).fetchone()
            if contradiction:
                self._add_finding(findings, "SYNTHESIS_CONTRADICTION_OPEN", item_id)
            if source_visibilities and not visibility_allows(
                str(memory.get("visibility") or ""), source_visibilities
            ):
                self._add_finding(findings, "SYNTHESIS_VISIBILITY_WIDENED", item_id)

        return sorted(
            findings,
            key=lambda finding: (
                str(finding["memory_id"]),
                str(finding["code"]),
                str(finding.get("source_id", "")),
            ),
        )

    def _mark_blocked(
        self,
        memory_id: str,
        reason: str,
        expected_revision: int,
        *,
        new_status: str,
        allowed_statuses: set[str],
        actor: str,
        call_id: str,
    ) -> SynthesisArtifact:
        if not reason.strip():
            raise SynthesisConflict("missing_transition_reason")
        owns_transaction = not self.conn.in_transaction
        with self._transaction():
            artifact = self._require_artifact(memory_id)
            self._require_revision(artifact, expected_revision)
            if artifact.status not in allowed_statuses:
                raise SynthesisConflict("transition_not_allowed")
            now = self._utc_now()
            metadata = dict(artifact.metadata)
            metadata["last_transition"] = new_status
            metadata["transition_reason"] = reason.strip()
            if actor.strip():
                metadata["transition_actor"] = actor.strip()
            if call_id.strip():
                metadata["transition_call_id"] = call_id.strip()
            self._cas_transition(
                artifact,
                new_status=new_status,
                new_revision=artifact.revision,
                now=now,
                metadata=metadata,
                extra={"stale_reason": reason.strip()},
            )
            self._increment_memory_version()
            from plastic_promise.core.synthesis_maintenance import (
                enqueue_synthesis_index_job,
            )

            enqueue_synthesis_index_job(
                self.conn,
                memory_id=artifact.memory_id,
                revision=artifact.revision,
                action="delete",
                call_id=call_id,
            )
        self._refresh_engine_after_commit(owns_transaction)
        return self._require_artifact(memory_id)

    def _normalize_source_ids(self, source_ids: Iterable[str]) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(
                str(source_id).strip() for source_id in source_ids if str(source_id).strip()
            )
        )
        return normalized

    @staticmethod
    def _validate_basic_inputs(
        content: str,
        synthesis_key: str,
        validity_scope: str,
        source_ids: tuple[str, ...],
        project_id: str,
    ) -> None:
        if not content.strip():
            raise SynthesisConflict("missing_synthesis_content")
        if not synthesis_key.strip():
            raise SynthesisConflict("missing_synthesis_key")
        if not validity_scope.strip():
            raise SynthesisConflict("missing_validity_scope")
        if not project_id:
            raise SynthesisConflict("missing_project_id")
        if len(source_ids) < 2:
            raise SynthesisConflict("insufficient_distinct_sources")

    def _validate_sources(
        self,
        source_ids: tuple[str, ...],
        *,
        project_id: str,
        visibility: str,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        from plastic_promise.core.synthesis_retrieval import (
            _GateFailure,
            _reject_open_contradiction,
            _reject_open_supersession,
            _source_is_available,
            _validate_synthesis,
        )

        sources: list[dict[str, Any]] = []
        snapshots: dict[str, str] = {}
        source_visibilities: list[str] = []
        for source_id in source_ids:
            source = self._load_memory(source_id)
            if source is None:
                raise SynthesisConflict(f"source_missing:{source_id}")
            try:
                available = _source_is_available(source)
            except _GateFailure as exc:
                raise SynthesisConflict(exc.reason) from exc
            if not available:
                raise SynthesisConflict(f"source_unavailable:{source_id}")
            source_control = self.conn.execute(
                "SELECT revision FROM synthesis_artifacts WHERE memory_id = ?",
                (source_id,),
            ).fetchone()
            source_is_synthesis = (
                str(source.get("memory_type") or "").strip().casefold() == "synthesis"
            )
            if source_is_synthesis or source_control is not None:
                try:
                    _validate_synthesis(self.conn, source_id, allow_review=False)
                except _GateFailure as exc:
                    raise SynthesisConflict(f"source_unavailable:{source_id}") from exc
            source_project = str(source.get("project_id") or "")
            source_visibility = str(source.get("visibility") or "")
            if source_project != project_id and source_visibility in {"private", "project"}:
                raise SynthesisConflict(f"source_project_mismatch:{source_id}")
            sources.append(source)
            snapshots[source_id] = canonical_memory_hash(source)
            source_visibilities.append(source_visibility)
        if not visibility_allows(visibility, source_visibilities):
            raise SynthesisConflict("synthesis_visibility_widened")
        try:
            _reject_open_supersession(self.conn, set(source_ids))
            _reject_open_contradiction(self.conn, set(source_ids))
        except _GateFailure as exc:
            raise SynthesisConflict(exc.reason) from exc
        return sources, snapshots

    @staticmethod
    def _validate_automatic(
        content: str,
        sources: list[dict[str, Any]],
        *,
        automatic: bool,
        reuse_signal: bool,
        audit_synthesis: bool,
    ) -> None:
        if not automatic:
            return
        if not reuse_signal:
            raise SynthesisConflict("missing_reuse_signal")
        try:
            minimum = float(os.environ.get("PP_SYNTHESIS_MIN_COMPRESSION", "1.5"))
        except (TypeError, ValueError) as exc:
            raise SynthesisConflict("invalid_synthesis_min_compression") from exc
        if not math.isfinite(minimum) or minimum <= 0:
            raise SynthesisConflict("invalid_synthesis_min_compression")
        ratio = sum(len(str(source.get("content") or "")) for source in sources) / max(
            len(content.strip()), 1
        )
        if ratio < minimum:
            raise SynthesisConflict("insufficient_compression")
        if not audit_synthesis and all(
            str(source.get("memory_type") or "").strip().casefold() == "reflection"
            for source in sources
        ):
            raise SynthesisConflict("reflection_only_sources")

    def _reject_existing_key(self, synthesis_key: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM synthesis_artifacts WHERE synthesis_key = ?",
            (synthesis_key.strip(),),
        ).fetchone():
            raise SynthesisConflict("synthesis_key_conflict")

    def _load_memory(self, memory_id: str) -> dict[str, Any] | None:
        cursor = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(zip((column[0] for column in cursor.description), row, strict=True))

    def _insert_memory(
        self,
        *,
        memory_id: str,
        content: str,
        source_ids: tuple[str, ...],
        project_id: str,
        visibility: str,
        actor: str,
        call_id: str,
        synthesis_key: str,
        revision: int,
        now: str,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        from plastic_promise.core.memory_index import (
            COMPACT_V2_POLICY,
            effective_embedding_model_name,
            initial_index_policy,
            metadata_with_index_material,
        )

        memory_metadata = dict(metadata or {})
        policy = initial_index_policy(summary_index_enabled=False)
        self._require_current_compact_summary(metadata, policy=policy)
        embedder = getattr(self.engine, "_embedder", None) if self.engine is not None else None
        material = self._build_synthesis_index_material(
            content=content,
            metadata=memory_metadata,
            policy=policy,
            model_name=effective_embedding_model_name(embedder),
            embedder=embedder,
            compact_policy=COMPACT_V2_POLICY,
        )
        memory_metadata.update(
            {
                "synthesis_key": synthesis_key,
                "synthesis_revision": revision,
            }
        )
        memory_metadata = metadata_with_index_material(memory_metadata, material)
        origin_hash = synthesis_content_hash(content)
        binding = canonical_synthesis_binding(
            {
                "content": content,
                "memory_type": "synthesis",
                "source_class": "synthesis",
                "origin_kind": "synthesis",
                "origin_hash": origin_hash,
                "project_id": project_id,
                "visibility": visibility,
                "metadata_json": memory_metadata,
            },
            material,
        )
        binding_hash = synthesis_binding_hash(binding)
        memory_metadata["synthesis_binding"] = binding
        memory_metadata["synthesis_binding_hash"] = binding_hash
        self.conn.execute(
            """
            INSERT INTO memories (
                id, content, memory_type, source, owner, tier, scope, category,
                tags, domain, importance, entity_ids, created_at, access_count,
                worth_success, worth_failure, activation_weight, decay_multiplier,
                effective_half_life, last_accessed, project_id, visibility,
                source_class, created_by_call_id, origin_kind, origin_uri,
                origin_ref, origin_hash, parent_memory_ids, metadata_json,
                raw_content, l0_abstract, l1_summary, l2_content,
                embedding_text, embedding_hash, search_text
            )
            VALUES (?, ?, 'synthesis', 'synthesis', ?, 'L1', 'global', 'knowledge',
                    '[]', 'synthesis', 0.7, '[]', ?, 0, 0, 0, 0.5, 1.0, 3.0,
                    ?, ?, ?, 'synthesis', ?, 'synthesis', ?, ?, ?, ?, ?, '', '',
                    '', '', ?, ?, ?)
            """,
            (
                memory_id,
                content,
                actor.strip(),
                now,
                now,
                project_id,
                visibility,
                call_id.strip(),
                f"synthesis://{synthesis_key}",
                synthesis_key,
                origin_hash,
                self._json(list(source_ids)),
                self._json(memory_metadata),
                material.vector_text,
                material.embedding_hash,
                material.search_text,
            ),
        )
        return {
            "synthesis_binding": binding,
            "synthesis_binding_hash": binding_hash,
        }

    def _update_memory_for_refresh(
        self,
        artifact: SynthesisArtifact,
        *,
        content: str,
        source_ids: tuple[str, ...],
        project_id: str,
        visibility: str,
        actor: str,
        call_id: str,
        revision: int,
        now: str,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        from plastic_promise.core.memory_index import (
            COMPACT_V2_POLICY,
            effective_embedding_model_name,
            metadata_with_index_material,
            read_persisted_index_material,
        )

        memory = self._load_memory(artifact.memory_id)
        if memory is None:
            raise SynthesisConflict("synthesis_memory_missing")
        memory_metadata = self._json_object(memory.get("metadata_json"))
        memory_metadata.update(dict(metadata or {}))
        memory_metadata.update(
            {"synthesis_key": artifact.synthesis_key, "synthesis_revision": revision}
        )
        embedder = getattr(self.engine, "_embedder", None) if self.engine is not None else None
        persisted_material = read_persisted_index_material(memory)
        if persisted_material is None:
            raise SynthesisConflict("synthesis_index_material_invalid")
        if embedder is None:
            model_name = persisted_material.model_name
        else:
            model_name = effective_embedding_model_name(embedder)
        self._require_current_compact_summary(metadata, policy=persisted_material.policy)
        material = self._build_synthesis_index_material(
            content=content,
            metadata=memory_metadata,
            policy=persisted_material.policy,
            model_name=model_name,
            embedder=embedder,
            compact_policy=COMPACT_V2_POLICY,
        )
        memory_metadata = metadata_with_index_material(memory_metadata, material)
        origin_hash = synthesis_content_hash(content)
        binding = canonical_synthesis_binding(
            {
                **memory,
                "content": content,
                "memory_type": "synthesis",
                "source_class": "synthesis",
                "origin_kind": "synthesis",
                "project_id": project_id,
                "visibility": visibility,
                "origin_hash": origin_hash,
                "metadata_json": memory_metadata,
            },
            material,
        )
        binding_hash = synthesis_binding_hash(binding)
        memory_metadata["synthesis_binding"] = binding
        memory_metadata["synthesis_binding_hash"] = binding_hash
        cursor = self.conn.execute(
            """
            UPDATE memories
            SET content = ?, memory_type = 'synthesis', source = 'synthesis',
                source_class = 'synthesis', origin_kind = 'synthesis',
                origin_uri = ?, origin_ref = ?, project_id = ?, visibility = ?, owner = ?,
                created_by_call_id = ?, origin_hash = ?, parent_memory_ids = ?,
                metadata_json = ?, embedding_text = ?, embedding_hash = ?,
                search_text = ?, last_accessed = ?
            WHERE id = ? AND lower(trim(memory_type)) = 'synthesis'
            """,
            (
                content,
                f"synthesis://{artifact.synthesis_key}",
                artifact.synthesis_key,
                project_id,
                visibility,
                actor.strip() or str(memory.get("owner") or ""),
                call_id.strip(),
                origin_hash,
                self._json(list(source_ids)),
                self._json(memory_metadata),
                material.vector_text,
                material.embedding_hash,
                material.search_text,
                now,
                artifact.memory_id,
            ),
        )
        if cursor.rowcount != 1:
            raise SynthesisConflict("synthesis_memory_type_mismatch")
        return {
            "synthesis_binding": binding,
            "synthesis_binding_hash": binding_hash,
        }

    @staticmethod
    def _require_current_compact_summary(
        metadata: Mapping[str, Any] | None,
        *,
        policy: str,
    ) -> None:
        from plastic_promise.core.memory_index import COMPACT_V2_POLICY

        if policy != COMPACT_V2_POLICY:
            return
        current = dict(metadata or {})
        if not all(str(current.get(key) or "").strip() for key in ("l0_abstract", "l1_summary")):
            raise SynthesisConflict("compact_index_material_requires_current_summary")

    @staticmethod
    def _build_synthesis_index_material(
        *,
        content: str,
        metadata: Mapping[str, Any],
        policy: str,
        model_name: str,
        embedder: object | None,
        compact_policy: str,
    ) -> Any:
        from plastic_promise.core.memory_index import (
            build_index_material,
            prepare_index_material,
        )

        try:
            record = {
                "content": content,
                "domain": metadata.get("domain"),
                "category": metadata.get("category"),
                "l0_abstract": metadata.get("l0_abstract"),
                "l1_summary": metadata.get("l1_summary"),
            }
            if embedder is None:
                return build_index_material(record, policy=policy, model_name=model_name)
            return prepare_index_material(
                record,
                embedder=embedder,
                policy=policy,
                model_name=model_name,
            )
        except ValueError as exc:
            reason = (
                "compact_index_material_requires_current_summary"
                if policy == compact_policy
                else "synthesis_index_material_invalid"
            )
            raise SynthesisConflict(reason) from exc

    def _insert_graph_node(self, memory_id: str, content: str, now: str) -> None:
        from plastic_promise.core.behavior_graph import graph_node

        node = graph_node(
            memory_id,
            "memory",
            "governed synthesis",
            "",
            source_kind="synthesis",
            metadata={"governed": True},
        )
        self.conn.execute(
            """
            INSERT INTO behavior_graph_nodes (
                id, node_type, name, description, source_kind,
                metadata_json, schema_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                node_type = excluded.node_type,
                name = excluded.name,
                description = excluded.description,
                source_kind = excluded.source_kind,
                metadata_json = excluded.metadata_json,
                schema_version = excluded.schema_version,
                updated_at = excluded.updated_at
            """,
            (
                memory_id,
                node["type"],
                node["name"],
                node["description"],
                node["source_kind"],
                self._json(node["metadata"]),
                node["schema_version"],
                now,
            ),
        )

    def _insert_derived_edge(
        self,
        memory_id: str,
        source: Mapping[str, Any],
        observed_hash: str,
        *,
        call_id: str,
        validity_scope: str,
        now: str,
        revision: int,
    ) -> None:
        from plastic_promise.core.behavior_graph import graph_edge
        from plastic_promise.core.traceability import record_memory_lineage

        source_id = str(source["id"])
        source_metadata = self._json_object(source.get("metadata_json"))
        source_control = self.conn.execute(
            "SELECT revision FROM synthesis_artifacts WHERE memory_id = ?",
            (source_id,),
        ).fetchone()
        if source_control is not None:
            source_revision: object = source_metadata.get("synthesis_revision")
            if type(source_revision) is not int:
                source_revision = source_control[0]
        else:
            source_revision = str(source_metadata.get("revision") or "")
        edge_metadata = {
            "observed_content_hash": observed_hash,
            "observed_origin_hash": str(source.get("origin_hash") or ""),
            "observed_embedding_hash": str(source.get("embedding_hash") or ""),
            "observed_at": now,
            "source_revision": source_revision,
            "support_scope": validity_scope.strip(),
            "synthesis_revision": revision,
        }
        edge = graph_edge(
            memory_id,
            source_id,
            "derived_from",
            1.0,
            source_kind="synthesis",
            evidence_id=call_id.strip(),
            metadata=edge_metadata,
        )
        self.conn.execute(
            """
            INSERT INTO behavior_graph_edges (
                id, source, target, relation, weight, source_kind,
                evidence_id, metadata_json, schema_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge["id"],
                edge["from"],
                edge["to"],
                edge["relation"],
                edge["weight"],
                edge["source_kind"],
                edge["evidence_id"],
                self._json(edge["metadata"]),
                edge["schema_version"],
                now,
            ),
        )
        record_memory_lineage(
            self.conn,
            memory_id=memory_id,
            parent_memory_id=source_id,
            relation="derived_from",
            call_id=call_id.strip(),
            metadata=edge_metadata,
        )

    def _cas_transition(
        self,
        artifact: SynthesisArtifact,
        *,
        new_status: str,
        new_revision: int,
        now: str,
        metadata: Mapping[str, Any],
        extra: Mapping[str, Any],
    ) -> None:
        assignments = ["status = ?", "revision = ?", "updated_at = ?", "metadata_json = ?"]
        values: list[Any] = [new_status, new_revision, now, self._json(dict(metadata))]
        for column, value in extra.items():
            assignments.append(f"{column} = ?")
            values.append(value)
        values.extend([artifact.memory_id, artifact.status, artifact.revision])
        cursor = self.conn.execute(
            f"UPDATE synthesis_artifacts SET {', '.join(assignments)} "
            "WHERE memory_id = ? AND status = ? AND revision = ?",
            values,
        )
        if cursor.rowcount != 1:
            raise SynthesisConflict("revision_conflict")

    def _increment_memory_version(self) -> None:
        cursor = self.conn.execute(
            "UPDATE memory_version SET version = version + 1 WHERE singleton = 1"
        )
        if cursor.rowcount != 1:
            raise SynthesisConflict("memory_version_unavailable")

    def _record_shadow_diagnostic(
        self,
        *,
        call_id: str,
        content: str,
        synthesis_key: str,
        source_count: int,
        fingerprint: str,
        reason: str,
    ) -> None:
        if not call_id.strip():
            return
        row = self.conn.execute(
            "SELECT metadata_json FROM call_spans WHERE call_id = ?", (call_id.strip(),)
        ).fetchone()
        if row is None:
            return
        metadata = self._json_object(row[0])
        metadata["synthesis_shadow"] = {
            "content_hash": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "synthesis_key_hash": "sha256:"
            + hashlib.sha256(synthesis_key.encode("utf-8")).hexdigest(),
            "source_fingerprint": fingerprint[:71],
            "source_count": min(max(int(source_count), 0), 1000),
            "reason": reason.split(":", 1)[0][:64],
        }
        with self._transaction():
            self.conn.execute(
                "UPDATE call_spans SET metadata_json = ?, ended_at = ? WHERE call_id = ?",
                (self._json(metadata), self._utc_now(), call_id.strip()),
            )

    def _require_artifact(self, memory_id: str) -> SynthesisArtifact:
        artifact = self.get(memory_id)
        if artifact is None:
            raise SynthesisConflict("synthesis_not_found")
        return artifact

    @staticmethod
    def _require_revision(artifact: SynthesisArtifact, expected_revision: int) -> None:
        if type(expected_revision) is not int or artifact.revision != expected_revision:
            raise SynthesisConflict("revision_conflict")

    def _refresh_engine_after_commit(self, committed: bool) -> None:
        if not committed or self.engine is None:
            return
        refresh = getattr(self.engine, "_refresh_canonical_cache_if_changed", None)
        if callable(refresh):
            refresh(force=True)

    @contextmanager
    def _transaction(self):
        outer = bool(self.conn.in_transaction)
        savepoint = f"synthesis_{secrets.token_hex(6)}"
        if outer:
            self.conn.execute(f"SAVEPOINT {savepoint}")
        else:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            if outer:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.conn.commit()
        except BaseException:
            if outer:
                with suppress(Exception):
                    self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                with suppress(Exception):
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                with suppress(Exception):
                    self.conn.rollback()
            raise

    @staticmethod
    def _add_finding(
        findings: list[dict[str, Any]],
        code: str,
        memory_id: str,
        *,
        source_id: str = "",
    ) -> None:
        finding: dict[str, Any] = {"code": code, "memory_id": memory_id}
        if source_id:
            finding["source_id"] = source_id
        if finding not in findings:
            findings.append(finding)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if not isinstance(value, str) or not value.strip():
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    @staticmethod
    def _utc_now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
