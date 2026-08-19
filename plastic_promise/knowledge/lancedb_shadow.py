"""Rebuildable, knowledge-only LanceDB shadow generations.

SQLite owns every lifecycle and content decision.  This module reads active
Semantic Units and active Wiki Artifacts, writes one identity-isolated LanceDB
generation, and records bounded build evidence back in ``knowledge_generations``.
It deliberately has no activation, promotion, query-routing, or Maintenance seam.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import lancedb
import pyarrow as pa

from plastic_promise.core.chunking import CHUNK_SCHEMA_VERSION
from plastic_promise.core.memory_proposals import contains_secret
from plastic_promise.knowledge.contracts import (
    knowledge_feature_gate,
    knowledge_state_root,
    utc_now_iso,
)
from plastic_promise.knowledge.semantic import (
    SEMANTIC_PROMPT_SHA256,
    SEMANTIC_SCHEMA_VERSION,
)

if TYPE_CHECKING:
    from plastic_promise.knowledge.repository import KnowledgeRepository

LANCE_SHADOW_GATE = "PP_KNOWLEDGE_LANCE_SHADOW"
SHADOW_SCHEMA_VERSION = "knowledge-lance-shadow-v2"
SHADOW_PROJECTION_VERSION = "semantic-unit-artifact-domain-v2"
SHADOW_FUSION_POLICY_IDENTITY = "knowledge-shadow-unrouted-v1"
PROJECT_WIDE_DOMAIN_ID = "all"
PROJECT_WIDE_DOMAIN_ALIASES = frozenset({PROJECT_WIDE_DOMAIN_ID, "knowledge"})
TABLE_NAME = "knowledge_vectors"
DEFAULT_BATCH_SIZE = 64
DEFAULT_MAX_RECORDS = 10_000
_MAX_IDENTITY_CHARS = 2048
_MAX_PROJECTION_CHARS = 12_000


class KnowledgeEmbeddingProvider(Protocol):
    """Minimal provider contract shared with the existing embedding stack."""

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def dim(self) -> int: ...

    @property
    def index_model_name(self) -> str: ...


class ShadowBuildError(RuntimeError):
    """Stable shadow-build failure without source or provider response text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ShadowRecord:
    record_id: str
    record_kind: str
    text: str
    canonical_hash: str
    source_ids: tuple[str, ...]
    version_ids: tuple[str, ...]
    evidence_chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalSnapshot:
    """One transactionally consistent project-wide SQLite projection."""

    records: tuple[ShadowRecord, ...]
    source_revision_set_sha256: str
    corpus_sha256: str
    chunking_identities: tuple[str, ...]


@dataclass(frozen=True)
class ShadowGenerationIdentity:
    project_id: str
    domain_id: str
    config_revision: str
    provider_identity: str
    embedding_dimension: int
    chunking_schema: str
    chunking_identities: tuple[str, ...]
    fusion_policy_identity: str
    source_revision_set_sha256: str
    corpus_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SHADOW_SCHEMA_VERSION,
            "project_id": self.project_id,
            "domain_id": self.domain_id,
            "config_revision": self.config_revision,
            "provider_identity": self.provider_identity,
            "embedding_dimension": self.embedding_dimension,
            "chunking_schema": self.chunking_schema,
            "chunking_identities": list(self.chunking_identities),
            "semantic_schema_version": SEMANTIC_SCHEMA_VERSION,
            "semantic_prompt_sha256": SEMANTIC_PROMPT_SHA256,
            "projection_version": SHADOW_PROJECTION_VERSION,
            "fusion_policy_identity": self.fusion_policy_identity,
            "source_revision_set_sha256": self.source_revision_set_sha256,
            "corpus_sha256": self.corpus_sha256,
        }

    @property
    def generation_id(self) -> str:
        return "klsh_" + _sha256_json(self.as_dict())[:24]


def create_knowledge_lance_embedder() -> KnowledgeEmbeddingProvider | None:
    """Resolve the shared embedding provider only while the shadow gate is enabled."""
    if knowledge_feature_gate(LANCE_SHADOW_GATE) not in {"shadow", "on"}:
        return None
    try:
        from plastic_promise.core.server_embedder import get_embedder

        embedder = get_embedder(fallback_on_error=False)
    except (RuntimeError, ValueError):
        return None
    if str(getattr(embedder, "model_name", "")) == "fallback-zero":
        close = getattr(embedder, "close", None)
        if callable(close):
            close()
        return None
    return embedder


class KnowledgeLanceShadowBuilder:
    """Build and reconcile one deterministic shadow generation at a time."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        root: str | Path | None = None,
        provider: KnowledgeEmbeddingProvider | None = None,
        config_revision: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        self._repository = repository
        self._root = Path(root or (knowledge_state_root() / "generations" / "lance-shadow"))
        self._provider = provider
        self._config_revision = _bounded_identity(
            config_revision
            or os.environ.get("PP_KNOWLEDGE_LANCE_CONFIG_REVISION")
            or SHADOW_PROJECTION_VERSION,
            "knowledge_lance_config_revision_invalid",
        )
        self._batch_size = _bounded_int(batch_size, 1, 512, "knowledge_lance_batch_size_invalid")
        self._max_records = _bounded_int(
            max_records, 1, 100_000, "knowledge_lance_max_records_invalid"
        )
        self._actor = "knowledge-lance-shadow"

    def build(self, project_id: str, *, domain_id: str = "all") -> dict[str, Any]:
        """Build a project-wide shadow; ``knowledge`` is an alias for ``all``."""
        gate = knowledge_feature_gate(LANCE_SHADOW_GATE)
        if gate not in {"shadow", "on"}:
            return {
                "status": "disabled",
                "reason": "knowledge_lance_shadow_disabled",
                "gate": gate,
                "promotion_eligible": False,
            }

        project_id = _bounded_identity(project_id, "knowledge_lance_project_id_invalid")
        try:
            domain_id = _normalize_domain_scope(domain_id)
        except ShadowBuildError as exc:
            return {
                "status": "failed",
                "generation_id": None,
                "failure_code": exc.code,
                "promotion_eligible": False,
            }
        provider = self._provider
        owns_provider = False
        if provider is None:
            provider = create_knowledge_lance_embedder()
            owns_provider = provider is not None
        if provider is None:
            return {
                "status": "degraded",
                "reason": "knowledge_lance_provider_unconfigured",
                "gate": gate,
                "promotion_eligible": False,
            }

        started = time.monotonic()
        generation_id = ""
        manifest: dict[str, Any] = {}
        before_stats = _provider_stats(provider)
        try:
            provider_identity = _provider_identity(provider)
            dimension = _bounded_int(
                getattr(provider, "dim", 0),
                1,
                16_384,
                "knowledge_lance_dimension_invalid",
            )
            snapshot = self._load_snapshot(project_id)
            records = list(snapshot.records)
            identity = ShadowGenerationIdentity(
                project_id=project_id,
                domain_id=domain_id,
                config_revision=self._config_revision,
                provider_identity=provider_identity,
                embedding_dimension=dimension,
                chunking_schema=CHUNK_SCHEMA_VERSION,
                chunking_identities=snapshot.chunking_identities,
                fusion_policy_identity=SHADOW_FUSION_POLICY_IDENTITY,
                source_revision_set_sha256=snapshot.source_revision_set_sha256,
                corpus_sha256=snapshot.corpus_sha256,
            )
            generation_id = identity.generation_id
            generation_path = self._generation_path(generation_id)
            existing_generation = self._repository.shadow_generation(generation_id)
            manifest = self._base_manifest(identity, len(records), status="building")
            manifest["resumed"] = existing_generation is not None
            self._repository.record_shadow_generation(
                generation_id, "building", manifest, actor=self._actor
            )
            self._write_manifest(generation_path, manifest)

            table = _open_table(generation_path / "index", dimension)
            existing = _existing_rows(table, self._max_records)
            expected = {record.record_id: record for record in records}
            invalid_existing = {
                record_id
                for record_id, row in existing.items()
                if record_id not in expected
                or not _row_matches(
                    row,
                    expected[record_id],
                    project_id=project_id,
                    domain_id=domain_id,
                    config_revision=self._config_revision,
                    provider_identity=provider_identity,
                )
            }
            if invalid_existing:
                _delete_records(table, invalid_existing)
                for record_id in invalid_existing:
                    existing.pop(record_id, None)

            missing = [record for record in records if record.record_id not in existing]
            embedded = 0
            batches = 0
            for start in range(0, len(missing), self._batch_size):
                batch = missing[start : start + self._batch_size]
                texts = [_prepare_index_text(provider, record.text) for record in batch]
                try:
                    vectors = provider.embed_batch(texts)
                except Exception:
                    raise ShadowBuildError("knowledge_lance_provider_error") from None
                _validate_vectors(vectors, expected_count=len(batch), dimension=dimension)
                table.add(
                    [
                        _lance_row(
                            record,
                            vector,
                            text,
                            project_id=project_id,
                            domain_id=domain_id,
                            config_revision=self._config_revision,
                            provider_identity=provider_identity,
                        )
                        for record, vector, text in zip(batch, vectors, texts, strict=True)
                    ]
                )
                embedded += len(batch)
                batches += 1
                manifest["checkpoint"] = {
                    "embedded": embedded,
                    "remaining": len(missing) - embedded,
                }
                self._repository.record_shadow_generation(
                    generation_id, "building", manifest, actor=self._actor
                )
                self._write_manifest(generation_path, manifest)

            try:
                current_snapshot = self._load_snapshot(project_id)
            except Exception:
                raise ShadowBuildError("knowledge_lance_canonical_changed") from None
            if _snapshot_identity(current_snapshot) != _snapshot_identity(snapshot):
                raise ShadowBuildError("knowledge_lance_canonical_changed")

            duration_ms = max(int((time.monotonic() - started) * 1000), 0)
            metrics = {
                "canonical_records": len(records),
                "existing_records": len(existing),
                "embedded_records": embedded,
                "reconciled_deleted": len(invalid_existing),
                "provider_batches": batches,
                "final_rows": int(table.count_rows()),
                "duration_ms": duration_ms,
            }
            manifest.update(
                {
                    "status": "shadow",
                    "completed_at": utc_now_iso(),
                    "promotion_eligible": False,
                    "metrics": metrics,
                    "provider_usage": _provider_usage_delta(
                        before_stats, _provider_stats(provider)
                    ),
                }
            )
            manifest.pop("checkpoint", None)
            self._repository.record_shadow_generation(
                generation_id, "shadow", manifest, actor=self._actor
            )
            manifest_sha256 = self._write_manifest(generation_path, manifest)
            return {
                "status": "shadow",
                "generation_id": generation_id,
                "manifest_sha256": manifest_sha256,
                "identity_sha256": _sha256_json(identity.as_dict()),
                "metrics": metrics,
                "resumed": bool(manifest["resumed"]),
                "promotion_eligible": False,
            }
        except Exception as exc:
            failure_code = (
                exc.code if isinstance(exc, ShadowBuildError) else "knowledge_lance_build_error"
            )
            if generation_id and manifest:
                manifest.update(
                    {
                        "status": "failed",
                        "failed_at": utc_now_iso(),
                        "failure_code": failure_code,
                        "promotion_eligible": False,
                    }
                )
                self._repository.record_shadow_generation(
                    generation_id, "failed", manifest, actor=self._actor
                )
                self._write_manifest(self._generation_path(generation_id), manifest)
            return {
                "status": "failed",
                "generation_id": generation_id or None,
                "failure_code": failure_code,
                "promotion_eligible": False,
            }
        finally:
            if owns_provider:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()

    def _load_snapshot(self, project_id: str) -> CanonicalSnapshot:
        with self._repository.connect() as connection:
            connection.execute("BEGIN")
            source_rows = connection.execute(
                "SELECT s.id, s.space_id, s.active_version_id,"
                " v.id AS version_id, v.status AS version_status, v.content_hash,"
                " v.parser_id, v.parse_schema, v.structure_manifest_json"
                " FROM knowledge_sources s"
                " LEFT JOIN knowledge_source_versions v ON v.id=s.active_version_id"
                " WHERE s.project_id=? AND s.status='active' ORDER BY s.id",
                (project_id,),
            ).fetchall()
            units = connection.execute(
                "SELECT u.id, u.kind, u.text, u.text_hash, u.payload_hash, u.source_id,"
                " u.version_id, u.space_id, u.evidence_chunk_ids_json, u.metadata_json"
                " FROM knowledge_semantic_units u"
                " JOIN knowledge_source_versions v ON v.id=u.version_id"
                " JOIN knowledge_sources s ON s.id=u.source_id"
                " WHERE u.project_id=? AND u.status='active' AND v.status='active'"
                " AND s.status='active' AND s.active_version_id=u.version_id"
                " ORDER BY u.id LIMIT ?",
                (project_id, self._max_records + 1),
            ).fetchall()
            artifacts = connection.execute(
                "SELECT id, kind, title, content, content_hash, source_ids_json"
                " FROM knowledge_artifacts"
                " WHERE project_id=? AND status='active' ORDER BY id LIMIT ?",
                (project_id, self._max_records + 1),
            ).fetchall()
            domains = connection.execute(
                "SELECT id, name FROM knowledge_domains"
                " WHERE project_id=? AND retired_at IS NULL"
                " ORDER BY id",
                (project_id,),
            ).fetchall()
            citations = connection.execute(
                "SELECT artifact_id, chunk_id FROM knowledge_citations"
                " WHERE project_id=? ORDER BY artifact_id, chunk_id",
                (project_id,),
            ).fetchall()

        if not source_rows:
            raise ShadowBuildError("knowledge_lance_active_source_snapshot_empty")
        if len(units) > self._max_records or len(artifacts) > self._max_records:
            raise ShadowBuildError("knowledge_lance_canonical_row_limit_exceeded")
        sources: dict[str, str] = {}
        source_revisions: list[dict[str, str]] = []
        chunking_identities: set[str] = set()
        for row in source_rows:
            source_id = str(row["id"])
            version_id = str(row["version_id"] or "")
            if (
                not version_id
                or version_id != str(row["active_version_id"] or "")
                or str(row["version_status"] or "") != "active"
            ):
                raise ShadowBuildError("knowledge_lance_active_source_revision_invalid")
            chunking_identity = _validated_chunking_identity(row["structure_manifest_json"])
            content_hash = str(row["content_hash"] or "")
            parser_id = str(row["parser_id"] or "")
            parse_schema = str(row["parse_schema"] or "")
            if len(content_hash) != 64 or not parser_id or parse_schema != CHUNK_SCHEMA_VERSION:
                raise ShadowBuildError("knowledge_lance_active_source_revision_invalid")
            chunking_identities.add(chunking_identity)
            sources[source_id] = version_id
            source_revisions.append(
                {
                    "source_id": source_id,
                    "space_id": str(row["space_id"]),
                    "version_id": version_id,
                    "content_hash": content_hash,
                    "parser_id": parser_id,
                    "parse_schema": parse_schema,
                    "chunking_identity": chunking_identity,
                }
            )
        artifact_citations: dict[str, list[str]] = {}
        for row in citations:
            artifact_citations.setdefault(str(row["artifact_id"]), []).append(str(row["chunk_id"]))
        domain_names = {
            str(row["id"]): _bounded_domain_name(row["name"])
            for row in domains
            if str(row["id"] or "") and str(row["name"] or "").strip()
        }
        records: list[ShadowRecord] = []
        for row in units:
            metadata = _json_object(row["metadata_json"])
            labels = sorted(
                {
                    domain_names[domain_id]
                    for domain_id in _json_strings(metadata.get("domain_ids"))
                    if domain_id in domain_names
                }
            )
            domain_prefix = " ".join(f"[domain:{name}]" for name in labels)
            text = _bounded_projection(f"{domain_prefix} [{row['kind']}] {row['text']}".strip())
            records.append(
                ShadowRecord(
                    record_id=f"unit:{row['id']}",
                    record_kind="semantic_unit",
                    text=text,
                    canonical_hash=_projection_hash(
                        "semantic_unit",
                        str(row["payload_hash"] or row["text_hash"]),
                        text,
                        source_ids=_nonempty_tuple(row["source_id"]),
                        version_ids=_nonempty_tuple(row["version_id"]),
                        evidence_chunk_ids=tuple(_json_strings(row["evidence_chunk_ids_json"])),
                    ),
                    source_ids=_nonempty_tuple(row["source_id"]),
                    version_ids=_nonempty_tuple(row["version_id"]),
                    evidence_chunk_ids=tuple(_json_strings(row["evidence_chunk_ids_json"])),
                )
            )
        for row in artifacts:
            source_ids = tuple(_json_strings(row["source_ids_json"]))
            if not source_ids or any(source_id not in sources for source_id in source_ids):
                raise ShadowBuildError("knowledge_lance_artifact_source_invalid")
            text = _bounded_projection(f"{row['title']}\n\n{row['content']}")
            records.append(
                ShadowRecord(
                    record_id=f"artifact:{row['id']}",
                    record_kind="artifact",
                    text=text,
                    canonical_hash=_projection_hash(
                        "artifact",
                        str(row["content_hash"]),
                        text,
                        source_ids=source_ids,
                        version_ids=tuple(sorted(sources[source_id] for source_id in source_ids)),
                        evidence_chunk_ids=tuple(artifact_citations.get(str(row["id"]), [])),
                    ),
                    source_ids=source_ids,
                    version_ids=tuple(sorted(sources[source_id] for source_id in source_ids)),
                    evidence_chunk_ids=tuple(artifact_citations.get(str(row["id"]), [])),
                )
            )
        if len(records) > self._max_records:
            raise ShadowBuildError("knowledge_lance_canonical_row_limit_exceeded")
        ordered_records = tuple(sorted(records, key=lambda record: record.record_id))
        return CanonicalSnapshot(
            records=ordered_records,
            source_revision_set_sha256=_sha256_json({"sources": source_revisions}),
            corpus_sha256=_corpus_sha256(list(ordered_records)),
            chunking_identities=tuple(sorted(chunking_identities)),
        )

    def _generation_path(self, generation_id: str) -> Path:
        root = self._root.expanduser().resolve()
        path = root / generation_id
        if path.is_symlink():
            raise ShadowBuildError("knowledge_lance_generation_symlink_rejected")
        return path

    @staticmethod
    def _base_manifest(
        identity: ShadowGenerationIdentity, record_count: int, *, status: str
    ) -> dict[str, Any]:
        return {
            "schema_version": SHADOW_SCHEMA_VERSION,
            "generation_id": identity.generation_id,
            "status": status,
            "created_at": utc_now_iso(),
            "identity": identity.as_dict(),
            "canonical_record_count": record_count,
            "truth_store": "sqlite",
            "index_role": "rebuildable-shadow",
            "promotion_eligible": False,
        }

    @staticmethod
    def _write_manifest(generation_path: Path, manifest: Mapping[str, Any]) -> str:
        generation_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if len(payload) > 64 * 1024:
            raise ShadowBuildError("knowledge_lance_manifest_oversized")
        temporary = generation_path / ".manifest.json.tmp"
        target = generation_path / "manifest.json"
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return hashlib.sha256(payload).hexdigest()


def _open_table(path: Path, dimension: int):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = lancedb.connect(path)
    schema = pa.schema(
        [
            pa.field("record_id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dimension)),
            pa.field("text", pa.string()),
            pa.field("record_kind", pa.string()),
            pa.field("project_id", pa.string()),
            pa.field("domain_id", pa.string()),
            pa.field("config_revision", pa.string()),
            pa.field("provider_identity", pa.string()),
            pa.field("canonical_hash", pa.string()),
            pa.field("source_ids_json", pa.string()),
            pa.field("version_ids_json", pa.string()),
            pa.field("evidence_chunk_ids_json", pa.string()),
        ]
    )
    try:
        table = database.open_table(TABLE_NAME)
    except Exception:
        table = database.create_table(TABLE_NAME, schema=schema, data=[])
    vector_type = table.schema.field("vector").type
    if not pa.types.is_fixed_size_list(vector_type) or vector_type.list_size != dimension:
        raise ShadowBuildError("knowledge_lance_schema_mismatch")
    return table


def _existing_rows(table: Any, max_records: int) -> dict[str, dict[str, Any]]:
    if int(table.count_rows()) > max_records + 1:
        raise ShadowBuildError("knowledge_lance_existing_row_limit_exceeded")
    rows = table.to_arrow().to_pylist()
    if len(rows) > max_records + 1:
        raise ShadowBuildError("knowledge_lance_existing_row_limit_exceeded")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        record_id = str(row.get("record_id") or "")
        if not record_id or record_id in result:
            raise ShadowBuildError("knowledge_lance_existing_row_identity_invalid")
        result[record_id] = row
    return result


def _row_matches(
    row: Mapping[str, Any],
    record: ShadowRecord,
    *,
    project_id: str,
    domain_id: str,
    config_revision: str,
    provider_identity: str,
) -> bool:
    return all(
        (
            str(row.get("canonical_hash") or "") == record.canonical_hash,
            str(row.get("project_id") or "") == project_id,
            str(row.get("domain_id") or "") == domain_id,
            str(row.get("config_revision") or "") == config_revision,
            str(row.get("provider_identity") or "") == provider_identity,
            str(row.get("source_ids_json") or "")
            == json.dumps(record.source_ids, ensure_ascii=False),
            str(row.get("version_ids_json") or "")
            == json.dumps(record.version_ids, ensure_ascii=False),
            str(row.get("evidence_chunk_ids_json") or "")
            == json.dumps(record.evidence_chunk_ids, ensure_ascii=False),
        )
    )


def _delete_records(table: Any, record_ids: set[str]) -> None:
    for record_id in sorted(record_ids):
        escaped = record_id.replace("'", "''")
        table.delete(f"record_id = '{escaped}'")


def _lance_row(
    record: ShadowRecord,
    vector: list[float],
    text: str,
    *,
    project_id: str,
    domain_id: str,
    config_revision: str,
    provider_identity: str,
) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "vector": vector,
        "text": text,
        "record_kind": record.record_kind,
        "project_id": project_id,
        "domain_id": domain_id,
        "config_revision": config_revision,
        "provider_identity": provider_identity,
        "canonical_hash": record.canonical_hash,
        "source_ids_json": json.dumps(record.source_ids, ensure_ascii=False),
        "version_ids_json": json.dumps(record.version_ids, ensure_ascii=False),
        "evidence_chunk_ids_json": json.dumps(record.evidence_chunk_ids, ensure_ascii=False),
    }


def _prepare_index_text(provider: KnowledgeEmbeddingProvider, text: str) -> str:
    prepare = getattr(provider, "prepare_index_text", None)
    prepared = prepare(text) if callable(prepare) else text
    if not isinstance(prepared, str) or not prepared.strip():
        raise ShadowBuildError("knowledge_lance_projection_invalid")
    return _bounded_projection(prepared)


def _validate_vectors(vectors: Any, *, expected_count: int, dimension: int) -> None:
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise ShadowBuildError("knowledge_lance_vector_count_mismatch")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ShadowBuildError("knowledge_lance_vector_dimension_mismatch")
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in vector
        ):
            raise ShadowBuildError("knowledge_lance_vector_value_invalid")
        if not any(float(value) != 0.0 for value in vector):
            raise ShadowBuildError("knowledge_lance_vector_zero")


def _normalize_domain_scope(value: Any) -> str:
    requested = _bounded_identity(value, "knowledge_lance_domain_id_invalid")
    if requested not in PROJECT_WIDE_DOMAIN_ALIASES:
        raise ShadowBuildError("knowledge_lance_domain_bindings_unavailable")
    return PROJECT_WIDE_DOMAIN_ID


def _validated_chunking_identity(value: Any) -> str:
    manifest = _json_object(value)
    if (
        str(manifest.get("schema_version") or "") != CHUNK_SCHEMA_VERSION
        or str(manifest.get("algorithm") or "") != CHUNK_SCHEMA_VERSION
    ):
        raise ShadowBuildError("knowledge_lance_chunking_identity_invalid")
    target = _bounded_int(
        manifest.get("target_chars"), 1, 10_000_000, "knowledge_lance_chunking_identity_invalid"
    )
    hard = _bounded_int(
        manifest.get("hard_chars"), target, 10_000_000, "knowledge_lance_chunking_identity_invalid"
    )
    max_chunks_value = manifest.get("max_chunks")
    max_chunks = (
        None
        if max_chunks_value is None
        else _bounded_int(
            max_chunks_value,
            1,
            1_000_000,
            "knowledge_lance_chunking_identity_invalid",
        )
    )
    expected = (
        f"{CHUNK_SCHEMA_VERSION}|target_chars={target}|hard_chars={hard}"
        f"|max_chunks={max_chunks if max_chunks is not None else 'unbounded'}"
        "|offsets=unicode-codepoints"
    )
    if str(manifest.get("chunking_identity") or "") != expected:
        raise ShadowBuildError("knowledge_lance_chunking_identity_invalid")
    return expected


def _snapshot_identity(snapshot: CanonicalSnapshot) -> tuple[str, str, tuple[str, ...]]:
    return (
        snapshot.source_revision_set_sha256,
        snapshot.corpus_sha256,
        snapshot.chunking_identities,
    )


def _corpus_sha256(records: list[ShadowRecord]) -> str:
    return hashlib.sha256(
        "\n".join(
            json.dumps(
                {
                    "record_id": record.record_id,
                    "canonical_hash": record.canonical_hash,
                    "source_ids": record.source_ids,
                    "version_ids": record.version_ids,
                    "evidence_chunk_ids": record.evidence_chunk_ids,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for record in records
        ).encode("utf-8")
    ).hexdigest()


def _projection_hash(
    kind: str,
    canonical_hash: str,
    text: str,
    *,
    source_ids: tuple[str, ...] = (),
    version_ids: tuple[str, ...] = (),
    evidence_chunk_ids: tuple[str, ...] = (),
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "projection_version": SHADOW_PROJECTION_VERSION,
                "kind": kind,
                "canonical_hash": canonical_hash,
                "text": text,
                "source_ids": source_ids,
                "version_ids": version_ids,
                "evidence_chunk_ids": evidence_chunk_ids,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _provider_identity(provider: KnowledgeEmbeddingProvider) -> str:
    """Return a secret-free model + endpoint identity for derived indexes."""
    model_identity = _bounded_identity(
        getattr(provider, "index_model_name", ""),
        "knowledge_lance_provider_identity_invalid",
        maximum=_MAX_IDENTITY_CHARS,
    )
    endpoint = None
    for attribute in ("base_url", "endpoint", "host", "url", "_base_url", "_endpoint", "_host"):
        value = getattr(provider, attribute, None)
        if value:
            endpoint = str(value).strip()
            break
    path = getattr(provider, "path", None) or getattr(provider, "_path", None)
    if endpoint and path and str(path) not in endpoint:
        endpoint = f"{endpoint.rstrip('/')}/{str(path).lstrip('/')}"
    if endpoint and "endpoint_sha256=" not in model_identity:
        endpoint = endpoint.split("?", 1)[0].split("#", 1)[0]
        endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        model_identity = f"{model_identity}|endpoint_sha256={endpoint_hash}"
    if contains_secret(model_identity):
        raise ShadowBuildError("knowledge_lance_provider_identity_invalid")
    return _bounded_identity(
        model_identity,
        "knowledge_lance_provider_identity_invalid",
        maximum=_MAX_IDENTITY_CHARS,
    )


def _provider_stats(provider: KnowledgeEmbeddingProvider) -> dict[str, Any]:
    stats = getattr(provider, "stats", {})
    if not isinstance(stats, Mapping):
        return {}
    allowed = {
        "requests",
        "input_count",
        "input_tokens",
        "total_tokens",
        "latency_ms",
        "cost_usd",
        "currency",
        "pricing_revision",
    }
    return {key: stats[key] for key in allowed if key in stats}


def _provider_usage_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in after.items():
        old = before.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = round(float(value) - float(old or 0), 6)
        elif isinstance(value, str):
            result[key] = value[:128]
    return result


def _bounded_identity(value: Any, code: str, *, maximum: int = 256) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(character) < 32 for character in text):
        raise ShadowBuildError(code)
    return text


def _bounded_projection(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > _MAX_PROJECTION_CHARS:
        raise ShadowBuildError("knowledge_lance_projection_invalid")
    return text


def _bounded_domain_name(value: Any) -> str:
    """Normalize a domain label before it becomes embedding input."""
    text = " ".join(str(value or "").split())
    if not text or len(text) > 256 or any(ord(character) < 32 for character in text):
        raise ShadowBuildError("knowledge_lance_domain_name_invalid")
    return text


def _bounded_int(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool):
        raise ShadowBuildError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ShadowBuildError(code) from None
    if not minimum <= parsed <= maximum:
        raise ShadowBuildError(code)
    return parsed


def _json_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nonempty_tuple(value: Any) -> tuple[str, ...]:
    text = str(value or "")
    return (text,) if text else ()


def _sha256_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
