from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from plastic_promise.core.synthesis import (
    canonical_memory_hash,
    canonical_synthesis_binding,
    source_fingerprint,
    synthesis_binding_hash,
    visibility_allows,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping

_MEMORY_COLUMNS = (
    "id",
    "content",
    "memory_type",
    "project_id",
    "visibility",
    "source_class",
    "origin_kind",
    "origin_uri",
    "origin_ref",
    "origin_hash",
    "embedding_hash",
    "embedding_text",
    "search_text",
    "tags",
    "metadata_json",
)
_BLOCKED_SOURCE_STATES = frozenset(
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
_HEALTHY_SOURCE_STATES = frozenset(
    {"active", "current", "healthy", "pass", "reviewed", "valid", "verified"}
)
_HEALTHY_QUALITY_STATES = _HEALTHY_SOURCE_STATES | frozenset({"low_quality", "store"})


@dataclass(frozen=True)
class SynthesisGateResult:
    items: tuple[str, ...]
    dropped_ids: tuple[str, ...]
    degradations: tuple[dict[str, str], ...]
    admitted_synthesis_ids: tuple[str, ...] = ()


class _GateFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _has_complete_verification_evidence(*values: object) -> bool:
    return all(isinstance(value, str) and bool(value.strip()) for value in values)


def read_memory_version(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT version FROM memory_version").fetchall()
    if len(rows) != 1:
        raise ValueError("memory_version_unavailable")
    version = rows[0][0]
    if type(version) is not int or version < 0:
        raise ValueError("memory_version_invalid")
    return version


def is_governed_synthesis_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    memory_type: object = None,
) -> bool:
    """Return whether an id is reserved for the governed synthesis lifecycle.

    The control row remains authoritative even if the canonical memory type
    drifts. Lookup failures fail closed so ordinary CRUD cannot take ownership
    while governance state is unavailable.
    """
    if str(memory_type or "").strip().casefold() == "synthesis":
        return True
    try:
        row = conn.execute(
            "SELECT (SELECT memory_type FROM memories WHERE id = ?), "
            "EXISTS(SELECT 1 FROM synthesis_artifacts WHERE memory_id = ?)",
            (memory_id, memory_id),
        ).fetchone()
    except Exception:
        return True
    if row is None or len(row) != 2:
        return True
    canonical_type = row[0]
    if canonical_type is not None and not isinstance(canonical_type, str):
        return True
    return str(canonical_type or "").strip().casefold() == "synthesis" or bool(row[1])


def engine_memory_is_governed_synthesis(
    engine: object,
    memory_id: str,
    *,
    memory_type: object = None,
) -> bool:
    """Apply the canonical reservation check without trusting dynamic mocks."""
    if str(memory_type or "").strip().casefold() == "synthesis":
        return True
    class_guard = getattr(type(engine), "_synthesis_memory_reserved", None)
    if callable(class_guard):
        try:
            return bool(class_guard(engine, memory_id))
        except Exception:
            return True
    state = getattr(engine, "__dict__", {})
    sqlite = state.get("_sqlite") if isinstance(state, dict) else None
    conn = getattr(sqlite, "_conn", None)
    if conn is not None:
        return is_governed_synthesis_memory(
            conn,
            memory_id,
            memory_type=memory_type,
        )
    memories = state.get("_memories") if isinstance(state, dict) else None
    if isinstance(memories, dict):
        candidate = memories.get(memory_id)
        if isinstance(candidate, dict):
            return str(candidate.get("memory_type") or "").strip().casefold() == "synthesis"
    return False


def ordinary_memory_sql_predicate(table_alias: str = "memories") -> str:
    """Build the atomic SQL guard used by independent ordinary mutators."""
    if not table_alias or not table_alias.replace("_", "").isalnum():
        raise ValueError("invalid_memory_table_alias")
    return (
        f"LOWER(TRIM(COALESCE({table_alias}.memory_type, ''))) <> 'synthesis' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM synthesis_artifacts "
        f"WHERE synthesis_artifacts.memory_id = {table_alias}.id"
        ")"
    )


def available_ordinary_memory_sql_predicate(
    table_alias: str = "memories",
) -> str:
    """Build the canonical discovery guard for an available ordinary source."""
    ordinary_guard = ordinary_memory_sql_predicate(table_alias)
    blocked = ", ".join(f"'{state}'" for state in sorted(_BLOCKED_SOURCE_STATES))
    healthy = ", ".join(f"'{state}'" for state in sorted(_HEALTHY_SOURCE_STATES))
    healthy_quality = ", ".join(f"'{state}'" for state in sorted(_HEALTHY_QUALITY_STATES))
    raw_tags = f"COALESCE({table_alias}.tags, '[]')"
    raw_metadata = f"COALESCE({table_alias}.metadata_json, '{{}}')"
    tags = f"CASE WHEN json_valid({raw_tags}) THEN {raw_tags} ELSE 'null' END"
    metadata = f"CASE WHEN json_valid({raw_metadata}) THEN {raw_metadata} ELSE 'null' END"
    tag_value = "LOWER(TRIM(CAST(source_tag.value AS TEXT)))"
    tag_prefix = f"SUBSTR({tag_value}, 1, INSTR({tag_value}, ':') - 1)"
    tag_state = f"SUBSTR({tag_value}, INSTR({tag_value}, ':') + 1)"

    lifecycle_paths = (
        "lifecycle_status",
        "mark_as",
        "state",
        "status",
    )
    quality_paths = ("quality_flag", "quality_status")
    metadata_guards: list[str] = []
    for path in lifecycle_paths:
        metadata_guards.append(
            "("
            f"json_type({metadata}, '$.{path}') IS NULL OR ("
            f"json_type({metadata}, '$.{path}') = 'text' AND "
            f"LOWER(TRIM(json_extract({metadata}, '$.{path}'))) IN ({healthy})"
            ")"
            ")"
        )
    for path in quality_paths:
        metadata_guards.append(
            "("
            f"json_type({metadata}, '$.{path}') IS NULL OR ("
            f"json_type({metadata}, '$.{path}') = 'text' AND "
            f"LOWER(TRIM(json_extract({metadata}, '$.{path}'))) IN ({healthy_quality})"
            ")"
            ")"
        )
    metadata_state_guard = " AND ".join(metadata_guards)
    quality_guard = (
        "("
        f"json_type({metadata}, '$.quality') IS NULL OR ("
        f"json_type({metadata}, '$.quality') = 'text' AND "
        f"LOWER(TRIM(json_extract({metadata}, '$.quality'))) IN ({healthy_quality})"
        ") OR ("
        f"json_type({metadata}, '$.quality') = 'object' AND "
        f"json_type({metadata}, '$.quality.status') = 'text' AND "
        f"LOWER(TRIM(json_extract({metadata}, '$.quality.status'))) "
        f"IN ({healthy_quality}) AND ("
        f"json_type({metadata}, '$.quality.decision') IS NULL OR ("
        f"json_type({metadata}, '$.quality.decision') = 'text' AND "
        f"LOWER(TRIM(json_extract({metadata}, '$.quality.decision'))) "
        "IN ('low_quality', 'store')"
        ")"
        ")"
        ")"
        ")"
    )
    blocked_metadata_guard = (
        "NOT EXISTS ("
        f"SELECT 1 FROM json_each({metadata}) AS source_state "
        f"WHERE LOWER(source_state.key) IN ({blocked}) "
        "AND source_state.type <> 'false'"
        ")"
    )
    tag_guard = (
        "NOT EXISTS ("
        f"SELECT 1 FROM json_each({tags}) AS source_tag WHERE "
        "source_tag.type <> 'text' OR "
        f"{tag_value} = 'decay:pending' OR ("
        f"INSTR({tag_value}, ':') > 0 AND "
        f"{tag_prefix} IN ('lifecycle', 'quality', 'status') AND ("
        f"{tag_state} IN ({blocked}) OR "
        f"({tag_prefix} IN ('lifecycle', 'status') AND "
        f"{tag_state} NOT IN ({healthy})) OR "
        f"({tag_prefix} = 'quality' AND "
        f"{tag_state} NOT IN ({healthy_quality}))"
        ")"
        ")"
        ")"
    )
    return (
        f"({ordinary_guard}) "
        f"AND json_valid({raw_tags}) AND json_type({tags}) = 'array' "
        f"AND {tag_guard} "
        f"AND json_valid({raw_metadata}) AND json_type({metadata}) = 'object' "
        f"AND {metadata_state_guard} "
        f"AND {quality_guard} "
        f"AND {blocked_metadata_guard}"
    )


def increment_memory_version_if_present(conn: sqlite3.Connection) -> bool:
    """Increment a valid canonical version row when the schema provides one."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_version'"
    ).fetchone()
    if table is None:
        return False
    read_memory_version(conn)
    cursor = conn.execute("UPDATE memory_version SET version = version + 1 WHERE singleton = 1")
    if cursor.rowcount != 1:
        raise ValueError("memory_version_unavailable")
    return True


def _transaction_open(conn: sqlite3.Connection) -> bool:
    try:
        return bool(conn.in_transaction)
    except Exception:
        return True


def evaluate_synthesis_ids(
    conn: sqlite3.Connection,
    ids: Iterable[str],
    *,
    allow_review: bool = False,
    memory_version: int,
) -> SynthesisGateResult:
    items: list[str] = []
    dropped_ids: list[str] = []
    degradations: list[dict[str, str]] = []
    seen: set[str] = set()
    admitted_synthesis_ids: list[str] = []

    for memory_id in ids:
        if memory_id in seen:
            continue
        seen.add(memory_id)

        try:
            candidate = conn.execute(
                """
                SELECT memories.memory_type, synthesis_artifacts.memory_id
                FROM memories
                LEFT JOIN synthesis_artifacts
                    ON synthesis_artifacts.memory_id = memories.id
                WHERE memories.id = ?
                """,
                (memory_id,),
            ).fetchone()
        except Exception:
            _record_drop(memory_id, "candidate_lookup_error", dropped_ids, degradations)
            continue

        if candidate is None:
            _record_drop(memory_id, "candidate_missing", dropped_ids, degradations)
            continue
        candidate_type = candidate[0]
        if not isinstance(candidate_type, str) or not candidate_type.strip():
            _record_drop(memory_id, "candidate_type_invalid", dropped_ids, degradations)
            continue
        is_synthesis = candidate_type.strip().casefold() == "synthesis"
        has_control = candidate[1] is not None
        if not is_synthesis and not has_control:
            items.append(memory_id)
            continue
        if _transaction_open(conn):
            _record_drop(memory_id, "transaction_open", dropped_ids, degradations)
            continue
        if os.environ.get("PP_SYNTHESIS_RETRIEVAL") != "1":
            _record_drop(memory_id, "retrieval_disabled", dropped_ids, degradations)
            continue
        if type(memory_version) is not int or memory_version < 0:
            _record_drop(memory_id, "memory_version_invalid", dropped_ids, degradations)
            continue
        try:
            current_memory_version = read_memory_version(conn)
        except Exception:
            _record_drop(memory_id, "memory_version_invalid", dropped_ids, degradations)
            continue
        if current_memory_version != memory_version:
            _record_drop(memory_id, "memory_version_mismatch", dropped_ids, degradations)
            continue
        if _transaction_open(conn):
            _record_drop(memory_id, "transaction_open", dropped_ids, degradations)
            continue

        try:
            _validate_synthesis(conn, memory_id, allow_review=allow_review)
        except _GateFailure as exc:
            _record_drop(memory_id, exc.reason, dropped_ids, degradations)
        except Exception:
            _record_drop(memory_id, "synthesis_validation_error", dropped_ids, degradations)
        else:
            if _transaction_open(conn):
                _record_drop(memory_id, "transaction_open", dropped_ids, degradations)
                continue
            try:
                final_memory_version = read_memory_version(conn)
            except Exception:
                _record_drop(memory_id, "memory_version_invalid", dropped_ids, degradations)
                continue
            if final_memory_version != memory_version:
                _record_drop(memory_id, "memory_version_mismatch", dropped_ids, degradations)
                continue
            if _transaction_open(conn):
                _record_drop(memory_id, "transaction_open", dropped_ids, degradations)
                continue
            items.append(memory_id)
            admitted_synthesis_ids.append(memory_id)

    if admitted_synthesis_ids:
        if _transaction_open(conn):
            batch_reason = "transaction_open"
        else:
            try:
                batch_memory_version = read_memory_version(conn)
            except Exception:
                batch_reason = "memory_version_invalid"
            else:
                if _transaction_open(conn):
                    batch_reason = "transaction_open"
                else:
                    batch_reason = (
                        "memory_version_mismatch" if batch_memory_version != memory_version else ""
                    )
        if batch_reason:
            admitted_set = set(admitted_synthesis_ids)
            items = [memory_id for memory_id in items if memory_id not in admitted_set]
            for memory_id in admitted_synthesis_ids:
                _record_drop(memory_id, batch_reason, dropped_ids, degradations)

    return SynthesisGateResult(
        items=tuple(items),
        dropped_ids=tuple(dropped_ids),
        degradations=tuple(degradations),
        admitted_synthesis_ids=tuple(
            memory_id for memory_id in admitted_synthesis_ids if memory_id in items
        ),
    )


def evaluate_public_memory_ids(
    conn: sqlite3.Connection,
    ids: Iterable[str],
    *,
    allow_review: bool = False,
    memory_version: int | None = None,
    memory_types: Mapping[str, object] | None = None,
) -> SynthesisGateResult:
    """Admit ordinary ids and canonically valid synthesis ids for public reads.

    Candidate discovery is one canonical query. If discovery fails, the full
    requested set is dropped because the caller cannot prove which ids are
    governed. Ordinary rows also pass their persisted availability state before
    public admission, while non-memory graph identifiers remain untouched.
    """
    ordered_ids = tuple(dict.fromkeys(str(memory_id) for memory_id in ids if memory_id))
    if not ordered_ids:
        return SynthesisGateResult((), (), ())
    try:
        governed_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM memories "
                "WHERE LOWER(TRIM(COALESCE(memory_type, ''))) = 'synthesis' "
                "UNION SELECT memory_id FROM synthesis_artifacts"
            ).fetchall()
        }
    except Exception:
        return SynthesisGateResult(
            (),
            ordered_ids,
            tuple(
                {"id": memory_id, "reason": "candidate_lookup_error"} for memory_id in ordered_ids
            ),
        )

    for memory_id, candidate_type in (memory_types or {}).items():
        if str(candidate_type or "").strip().casefold() == "synthesis":
            governed_ids.add(str(memory_id))
    candidates = tuple(memory_id for memory_id in ordered_ids if memory_id in governed_ids)
    ordinary_candidates = tuple(
        memory_id for memory_id in ordered_ids if memory_id not in governed_ids
    )
    ordinary_admitted: set[str] = set(ordinary_candidates)
    ordinary_degradations: dict[str, str] = {}
    if ordinary_candidates:
        placeholders = ",".join("?" for _ in ordinary_candidates)
        try:
            rows = conn.execute(
                f"SELECT id, tags, metadata_json FROM memories WHERE id IN ({placeholders})",  # noqa: S608
                ordinary_candidates,
            ).fetchall()
        except Exception:
            return SynthesisGateResult(
                (),
                ordered_ids,
                tuple(
                    {"id": memory_id, "reason": "candidate_lookup_error"}
                    for memory_id in ordered_ids
                ),
            )
        for memory_id, tags, metadata_json in rows:
            candidate_id = str(memory_id)
            try:
                available = _source_is_available({"tags": tags, "metadata_json": metadata_json})
            except _GateFailure as exc:
                ordinary_admitted.discard(candidate_id)
                ordinary_degradations[candidate_id] = exc.reason
            else:
                if not available:
                    ordinary_admitted.discard(candidate_id)
                    ordinary_degradations[candidate_id] = "source_unavailable"

    if not candidates:
        dropped = tuple(
            memory_id for memory_id in ordered_ids if memory_id in ordinary_degradations
        )
        return SynthesisGateResult(
            tuple(memory_id for memory_id in ordered_ids if memory_id in ordinary_admitted),
            dropped,
            tuple(
                {"id": memory_id, "reason": ordinary_degradations[memory_id]}
                for memory_id in dropped
            ),
        )
    if memory_version is None:
        try:
            memory_version = read_memory_version(conn)
        except Exception:
            memory_version = -1
    gated = evaluate_synthesis_ids(
        conn,
        candidates,
        allow_review=allow_review,
        memory_version=memory_version,
    )
    admitted_synthesis = set(gated.items)
    admitted_set = ordinary_admitted | admitted_synthesis
    gated_degradations = {row["id"]: row["reason"] for row in gated.degradations}
    gated_dropped = set(gated.dropped_ids)
    dropped = tuple(
        memory_id
        for memory_id in ordered_ids
        if memory_id in ordinary_degradations
        or memory_id in gated_degradations
        or memory_id in gated_dropped
    )
    return SynthesisGateResult(
        tuple(memory_id for memory_id in ordered_ids if memory_id in admitted_set),
        dropped,
        tuple(
            {
                "id": memory_id,
                "reason": (
                    ordinary_degradations[memory_id]
                    if memory_id in ordinary_degradations
                    else gated_degradations.get(memory_id, "candidate_state_unavailable")
                ),
            }
            for memory_id in dropped
        ),
        gated.admitted_synthesis_ids,
    )


def synthesis_provenance(
    conn: sqlite3.Connection,
    memory_id: str,
) -> dict[str, Any]:
    """Return compact provenance only for a currently valid verified synthesis.

    SQLite is the sole authority. The version checks make this a read-only
    committed snapshot operation; any uncertainty returns no provenance.
    """
    memory_id = str(memory_id or "")
    if not memory_id:
        return {}
    try:
        if _transaction_open(conn):
            return {}
        memory_version = read_memory_version(conn)
        decision = evaluate_synthesis_ids(
            conn,
            [memory_id],
            allow_review=False,
            memory_version=memory_version,
        )
        if decision.admitted_synthesis_ids != (memory_id,):
            return {}
        row = conn.execute(
            """
            SELECT status, revision, source_fingerprint, last_verified_at,
                   verified_by_actor, verified_by_call_id
            FROM synthesis_artifacts
            WHERE memory_id = ?
            """,
            (memory_id,),
        ).fetchone()
        source_rows = conn.execute(
            """
            SELECT DISTINCT target
            FROM behavior_graph_edges
            WHERE source = ? AND relation = 'derived_from'
            ORDER BY target
            """,
            (memory_id,),
        ).fetchall()
        if row is None or len(row) != 6:
            return {}
        (
            status,
            revision,
            fingerprint,
            last_verified_at,
            verification_actor,
            verification_call_id,
        ) = row
        source_ids = [str(source_row[0]) for source_row in source_rows]
        if (
            status != "verified"
            or type(revision) is not int
            or revision < 1
            or not isinstance(fingerprint, str)
            or len(source_ids) < 2
            or not all(source_ids)
            or not _has_complete_verification_evidence(
                last_verified_at,
                verification_actor,
                verification_call_id,
            )
        ):
            return {}
        if _transaction_open(conn) or read_memory_version(conn) != memory_version:
            return {}
        return {
            "status": status,
            "revision": revision,
            "source_ids": source_ids,
            "source_fingerprint": fingerprint,
            "last_verified_at": last_verified_at,
            "verified_by_actor": verification_actor,
            "verified_by_call_id": verification_call_id,
        }
    except Exception:
        return {}


def expand_synthesis_sources(
    conn: sqlite3.Connection,
    selected_ids: Iterable[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Hydrate current ordinary source leaves for selected synthesis IDs.

    The traversal follows only canonical ``derived_from`` edges. Governed
    synthesis sources are traversed recursively and never exposed as raw
    evidence themselves. Results are deterministic, deduplicated, bounded, and
    read-only; an unstable canonical snapshot fails closed.
    """
    if type(limit) is not int or limit <= 0:
        return []
    queue = list(dict.fromkeys(str(memory_id) for memory_id in selected_ids if memory_id))
    if not queue:
        return []
    try:
        if _transaction_open(conn):
            return []
        memory_version = read_memory_version(conn)
        evidence: list[dict[str, Any]] = []
        visited_synthesis: set[str] = set()
        seen_sources: set[str] = set()
        cursor = 0
        while cursor < len(queue) and len(evidence) < limit:
            synthesis_id = queue[cursor]
            cursor += 1
            if synthesis_id in visited_synthesis:
                continue
            visited_synthesis.add(synthesis_id)
            governed = conn.execute(
                """
                SELECT memories.memory_type, synthesis_artifacts.memory_id
                FROM memories
                LEFT JOIN synthesis_artifacts
                    ON synthesis_artifacts.memory_id = memories.id
                WHERE memories.id = ?
                """,
                (synthesis_id,),
            ).fetchone()
            if governed is None or (
                str(governed[0] or "").strip().casefold() != "synthesis" and governed[1] is None
            ):
                continue
            source_rows = conn.execute(
                """
                SELECT DISTINCT target
                FROM behavior_graph_edges
                WHERE source = ? AND relation = 'derived_from'
                ORDER BY target
                """,
                (synthesis_id,),
            ).fetchall()
            for source_row in source_rows:
                source_id = str(source_row[0] or "")
                if not source_id or source_id in seen_sources:
                    continue
                source = _load_memory(conn, source_id)
                if source is None or not _source_is_available(source):
                    continue
                source_control = conn.execute(
                    "SELECT 1 FROM synthesis_artifacts WHERE memory_id = ?",
                    (source_id,),
                ).fetchone()
                is_synthesis = (
                    str(source.get("memory_type") or "").strip().casefold() == "synthesis"
                    or source_control is not None
                )
                if is_synthesis:
                    queue.append(source_id)
                    continue
                seen_sources.add(source_id)
                evidence.append(
                    {
                        "id": source_id,
                        "source": "synthesis_source",
                        "score": 0.0,
                        "content": str(source.get("content") or "")[:300],
                    }
                )
                if len(evidence) >= limit:
                    break
        if _transaction_open(conn) or read_memory_version(conn) != memory_version:
            return []
        return evidence
    except Exception:
        return []


def synthesis_index_eligible(conn: sqlite3.Connection, memory_id: str) -> bool:
    if os.environ.get("PP_SYNTHESIS_RETRIEVAL") != "1":
        return False
    try:
        if _transaction_open(conn):
            return False
        candidate = conn.execute(
            """
            SELECT memories.memory_type, synthesis_artifacts.memory_id
            FROM memories
            LEFT JOIN synthesis_artifacts
                ON synthesis_artifacts.memory_id = memories.id
            WHERE memories.id = ?
            """,
            (memory_id,),
        ).fetchone()
        if candidate is None or candidate[0] != "synthesis" or candidate[1] is None:
            return False
        result = evaluate_synthesis_ids(
            conn,
            [memory_id],
            allow_review=False,
            memory_version=read_memory_version(conn),
        )
        return result.items == (memory_id,)
    except Exception:
        return False


def _record_drop(
    memory_id: str,
    reason: str,
    dropped_ids: list[str],
    degradations: list[dict[str, str]],
) -> None:
    dropped_ids.append(memory_id)
    degradations.append({"id": memory_id, "reason": reason})


def _validate_synthesis(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    allow_review: bool,
    _visiting: set[str] | None = None,
    _validated: set[str] | None = None,
) -> None:
    visiting = set() if _visiting is None else _visiting
    validated = set() if _validated is None else _validated
    if memory_id in validated:
        return
    if memory_id in visiting:
        raise _GateFailure("synthesis_cycle")
    visiting.add(memory_id)
    try:
        _validate_synthesis_state(
            conn,
            memory_id,
            allow_review=allow_review,
            visiting=visiting,
            validated=validated,
        )
        validated.add(memory_id)
    finally:
        visiting.remove(memory_id)


def _validate_synthesis_state(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    allow_review: bool,
    visiting: set[str],
    validated: set[str],
) -> None:
    control = conn.execute(
        """
        SELECT status, synthesis_key, revision, support_count, validity_scope,
               source_fingerprint, last_verified_at, verified_by_actor,
               verified_by_call_id, metadata_json
        FROM synthesis_artifacts
        WHERE memory_id = ?
        """,
        (memory_id,),
    ).fetchone()
    if control is None:
        raise _GateFailure("control_missing")

    candidate = _load_memory(conn, memory_id)
    if candidate is None:
        raise _GateFailure("candidate_missing")
    candidate_type = candidate.get("memory_type")
    if not isinstance(candidate_type, str) or candidate_type.strip().casefold() != "synthesis":
        raise _GateFailure("candidate_type_mismatch")

    (
        status,
        synthesis_key,
        revision,
        support_count,
        validity_scope,
        expected_fingerprint,
        last_verified_at,
        verification_actor,
        verification_call_id,
        control_metadata_json,
    ) = control
    control_metadata = _json_object(control_metadata_json, "control_metadata_invalid")
    if type(revision) is not int or revision < 1:
        raise _GateFailure("control_binding_invalid")
    try:
        candidate_available = _source_is_available(candidate)
    except _GateFailure as exc:
        raise _GateFailure("candidate_unavailable") from exc
    if not candidate_available:
        raise _GateFailure("candidate_unavailable")
    _validate_candidate_binding(
        candidate,
        control_metadata,
        synthesis_key=synthesis_key,
        revision=revision,
    )
    allowed_statuses = {"verified"}
    if allow_review:
        allowed_statuses.update({"draft", "contested"})
    if status not in allowed_statuses:
        raise _GateFailure("status_not_allowed")
    if status == "verified" and not _has_complete_verification_evidence(
        last_verified_at,
        verification_actor,
        verification_call_id,
    ):
        raise _GateFailure("verification_evidence_missing")
    if type(support_count) is not int or support_count < 2:
        raise _GateFailure("support_count_mismatch")
    if not isinstance(expected_fingerprint, str):
        raise _GateFailure("source_fingerprint_mismatch")

    edge_rows = conn.execute(
        """
        SELECT DISTINCT target, metadata_json
        FROM behavior_graph_edges
        WHERE source = ? AND relation = 'derived_from'
        ORDER BY target, metadata_json
        """,
        (memory_id,),
    ).fetchall()

    snapshots: dict[str, str] = {}
    source_visibilities: list[str] = []
    for source_id, metadata_json in edge_rows:
        if source_id in snapshots:
            raise _GateFailure("edge_metadata_invalid")
        edge_metadata = _json_object(metadata_json, "edge_metadata_invalid")
        observed_hash = edge_metadata.get("observed_content_hash")
        if not isinstance(observed_hash, str) or not observed_hash:
            raise _GateFailure("edge_metadata_invalid")
        edge_revision = edge_metadata.get("synthesis_revision")
        if type(edge_revision) is not int or edge_revision != revision:
            raise _GateFailure("edge_metadata_invalid")
        if edge_metadata.get("support_scope") != validity_scope:
            raise _GateFailure("edge_metadata_invalid")

        source = _load_memory(conn, source_id)
        if source is None:
            raise _GateFailure("source_missing")
        if not _source_is_available(source):
            raise _GateFailure("source_unavailable")
        source_type = source.get("memory_type")
        source_control = conn.execute(
            "SELECT revision FROM synthesis_artifacts WHERE memory_id = ?",
            (source_id,),
        ).fetchone()
        source_has_control = source_control is not None
        if (
            isinstance(source_type, str) and source_type.strip().casefold() == "synthesis"
        ) or source_has_control:
            try:
                _validate_synthesis(
                    conn,
                    source_id,
                    allow_review=False,
                    _visiting=visiting,
                    _validated=validated,
                )
            except _GateFailure as exc:
                if exc.reason == "synthesis_cycle":
                    raise
                raise _GateFailure("source_synthesis_invalid") from exc
            source_revision = source_control[0] if source_control is not None else None
            observed_revision = edge_metadata.get("source_revision")
            if (
                type(source_revision) is not int
                or source_revision < 1
                or type(observed_revision) is not int
                or observed_revision != source_revision
            ):
                raise _GateFailure("source_revision_mismatch")

        current_hash = canonical_memory_hash(source)
        if observed_hash != current_hash:
            raise _GateFailure("source_hash_mismatch")
        snapshots[source_id] = current_hash
        source_visibilities.append(str(source["visibility"]))

    source_ids = set(snapshots)
    _reject_open_supersession(conn, source_ids)
    _reject_open_contradiction(conn, {memory_id, *source_ids})

    if support_count != len(snapshots):
        raise _GateFailure("support_count_mismatch")
    if expected_fingerprint != source_fingerprint(snapshots):
        raise _GateFailure("source_fingerprint_mismatch")
    if not visibility_allows(str(candidate["visibility"]), source_visibilities):
        raise _GateFailure("visibility_widening")


def _validate_candidate_binding(
    candidate: Mapping[str, Any],
    control_metadata: Mapping[str, Any],
    *,
    synthesis_key: object,
    revision: object,
) -> None:
    from plastic_promise.core.memory_index import read_persisted_index_material

    expected = control_metadata.get("synthesis_binding")
    expected_hash = control_metadata.get("synthesis_binding_hash")
    if not isinstance(expected, dict) or not isinstance(expected_hash, str):
        raise _GateFailure("control_binding_invalid")
    if synthesis_binding_hash(expected) != expected_hash:
        raise _GateFailure("control_binding_invalid")
    if (
        expected.get("schema") != "synthesis-binding/v1"
        or expected.get("memory_type") != "synthesis"
        or expected.get("source_class") != "synthesis"
        or expected.get("origin_kind") != "synthesis"
        or expected.get("synthesis_key") != synthesis_key
        or type(expected.get("synthesis_revision")) is not int
        or expected.get("synthesis_revision") != revision
        or expected.get("project_id") != control_metadata.get("project_id")
        or expected.get("visibility") != control_metadata.get("visibility")
    ):
        raise _GateFailure("control_binding_invalid")

    material = read_persisted_index_material(candidate)
    if material is None:
        raise _GateFailure("candidate_index_material_mismatch")
    current = canonical_synthesis_binding(candidate, material)
    memory_metadata = _json_object(
        candidate.get("metadata_json"),
        "candidate_binding_invalid",
    )
    if (
        memory_metadata.get("synthesis_binding") != expected
        or memory_metadata.get("synthesis_binding_hash") != expected_hash
    ):
        raise _GateFailure("candidate_binding_mismatch")
    if current.get("project_id") != expected.get("project_id"):
        raise _GateFailure("candidate_project_mismatch")
    if current.get("visibility") != expected.get("visibility"):
        raise _GateFailure("candidate_visibility_mismatch")
    if current.get("content_hash") != expected.get("content_hash"):
        raise _GateFailure("candidate_content_mismatch")
    if current.get("index_material_hash") != expected.get("index_material_hash"):
        raise _GateFailure("candidate_index_material_mismatch")
    if current != expected or synthesis_binding_hash(current) != expected_hash:
        raise _GateFailure("candidate_binding_mismatch")


def _load_memory(conn: sqlite3.Connection, memory_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(_MEMORY_COLUMNS)} FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_MEMORY_COLUMNS, row, strict=True))


def _source_is_available(source: Mapping[str, Any]) -> bool:
    tags = _json_value(source.get("tags"), "source_state_invalid")
    metadata = _json_value(source.get("metadata_json"), "source_state_invalid")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise _GateFailure("source_state_invalid")
    if not isinstance(metadata, dict):
        raise _GateFailure("source_state_invalid")

    normalized_tags = {tag.strip().lower() for tag in tags}
    if "decay:pending" in normalized_tags:
        return False
    for tag in normalized_tags:
        prefix, separator, state = tag.partition(":")
        if not separator or prefix not in {"lifecycle", "quality", "status"}:
            continue
        if state in _BLOCKED_SOURCE_STATES:
            return False
        allowed_states = _HEALTHY_QUALITY_STATES if prefix == "quality" else _HEALTHY_SOURCE_STATES
        if state not in allowed_states:
            raise _GateFailure("source_state_invalid")

    lifecycle_states: set[str] = set()
    quality_states: set[str] = set()
    state_keys = {
        "lifecycle_status": lifecycle_states,
        "mark_as": lifecycle_states,
        "quality_flag": quality_states,
        "quality_status": quality_states,
        "state": lifecycle_states,
        "status": lifecycle_states,
    }
    for key, destination in state_keys.items():
        if key not in metadata:
            continue
        value = metadata[key]
        if not isinstance(value, str) or not value.strip():
            raise _GateFailure("source_state_invalid")
        destination.add(value.strip().lower())
    if "quality" in metadata:
        quality = metadata["quality"]
        if isinstance(quality, str):
            if not quality.strip():
                raise _GateFailure("source_state_invalid")
            quality_states.add(quality.strip().lower())
        elif isinstance(quality, dict):
            if "status" not in quality:
                raise _GateFailure("source_state_invalid")
            quality_status = quality["status"]
            if not isinstance(quality_status, str) or not quality_status.strip():
                raise _GateFailure("source_state_invalid")
            quality_states.add(quality_status.strip().lower())
            if "decision" in quality:
                quality_decision = quality["decision"]
                if not isinstance(quality_decision, str) or not quality_decision.strip():
                    raise _GateFailure("source_state_invalid")
                quality_decision = quality_decision.strip().lower()
                if quality_decision == "discard":
                    return False
                if quality_decision not in {"low_quality", "store"}:
                    raise _GateFailure("source_state_invalid")
        else:
            raise _GateFailure("source_state_invalid")
    for state in _BLOCKED_SOURCE_STATES:
        if state not in metadata:
            continue
        blocked_flag = metadata[state]
        if blocked_flag is True:
            lifecycle_states.add(state)
        elif blocked_flag is not False:
            raise _GateFailure("source_state_invalid")
    states = lifecycle_states | quality_states
    if not states.isdisjoint(_BLOCKED_SOURCE_STATES):
        return False
    if not lifecycle_states.issubset(_HEALTHY_SOURCE_STATES):
        raise _GateFailure("source_state_invalid")
    if not quality_states.issubset(_HEALTHY_QUALITY_STATES):
        raise _GateFailure("source_state_invalid")
    return True


def _reject_open_supersession(conn: sqlite3.Connection, source_ids: set[str]) -> None:
    rows = conn.execute(
        """
        SELECT target, metadata_json
        FROM behavior_graph_edges
        WHERE relation = 'supersedes'
        """
    ).fetchall()
    for target, metadata_json in rows:
        if target in source_ids:
            _json_object(metadata_json, "edge_metadata_invalid")
            raise _GateFailure("source_superseded")


def _reject_open_contradiction(conn: sqlite3.Connection, relevant_ids: set[str]) -> None:
    rows = conn.execute(
        """
        SELECT source, target, metadata_json
        FROM behavior_graph_edges
        WHERE relation = 'contradicts'
        """
    ).fetchall()
    for source, target, metadata_json in rows:
        if source in relevant_ids or target in relevant_ids:
            _json_object(metadata_json, "edge_metadata_invalid")
            raise _GateFailure("contradiction_open")


def _json_object(raw_value: object, reason: str) -> dict[str, Any]:
    value = _json_value(raw_value, reason)
    if not isinstance(value, dict):
        raise _GateFailure(reason)
    return value


def _json_value(raw_value: object, reason: str) -> object:
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        raise _GateFailure(reason)
    if not isinstance(raw_value, str):
        return raw_value
    try:
        return json.loads(raw_value)
    except (TypeError, ValueError) as exc:
        raise _GateFailure(reason) from exc
