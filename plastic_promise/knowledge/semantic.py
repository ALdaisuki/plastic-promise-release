"""Durable semantic compilation for knowledge evidence chunks (Slice 2a).

The cloud semantic provider compiles batches of structure-v1 chunks into:
semantic units, candidate knowledge domains, claims, and Wiki artifact
recommendations.  Everything downstream of the provider response is
deterministic and auditable: strict JSON validation, grounding checks,
project-scoped persistence, and durable retry via knowledge_semantic_jobs.

The provider seam is credential-boundary: ``create_knowledge_semantic_provider``
returns ``None`` (graceful degradation) until the operator configures the
shared chunk-inference env.  No secret is ever logged or persisted here.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from plastic_promise.core.memory_proposals import contains_secret
from plastic_promise.knowledge.contracts import SemanticChunkCursor, knowledge_feature_gate
from plastic_promise.knowledge.query import tokenize

if TYPE_CHECKING:
    from plastic_promise.knowledge.repository import KnowledgeRepository

SEMANTIC_SCHEMA_VERSION = "knowledge-semantic-v1"
SEMANTIC_GATE = "PP_KNOWLEDGE_SEMANTIC"
DEFAULT_BATCH_SIZE = 20
DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_CONCURRENCY = 4

UNIT_KINDS = frozenset(
    {
        "summary",
        "concept",
        "insight",
        "decision",
        "preference",
        "fact",
        "statement",
        "quote",
        "procedure",
    }
)
_VERBATIM_UNIT_KINDS = frozenset({"fact", "statement", "quote"})
CLAIM_STANCES = frozenset({"supports", "refutes", "qualifies", "neutral"})
RISK_TIERS = frozenset({"low", "medium", "high"})
ARTIFACT_KINDS = frozenset(
    {"source_summary", "entity", "concept", "topic", "overview", "saved_query"}
)
HIGH_RISK_ARTIFACT_KINDS = frozenset(
    {"finance", "medical", "legal", "security", "production_operations"}
)

_CAPS = {
    "unit_text": 2000,
    "claim_text": 2000,
    "domain_name": 128,
    "domain_description": 2000,
    "artifact_title": 300,
    "artifact_content": 8000,
    "kind": 64,
    "evidence_ids_per_unit": 64,
    "units_per_response": 100,
    "domains_per_response": 20,
    "claims_per_response": 100,
    "artifacts_per_response": 20,
    "metadata_bytes": 4096,
}

_SYSTEM_PROMPT = (
    "You compile grounded semantic projections for a knowledge system. "
    "Return one strict JSON object with schema_version exactly "
    f"{SEMANTIC_SCHEMA_VERSION!r} and only these keys: units, domains, claims, artifacts. "
    "units: array of {kind, text, evidence_chunk_ids, metadata} where kind is one of "
    f"{sorted(UNIT_KINDS)} and every evidence_chunk_ids entry exists in the supplied batch. "
    "For fact/statement/quote units the text must appear verbatim in the cited chunk text; "
    "other kinds must be grounded in the cited chunks. "
    f"domains: array of {{name, description}} naming candidate knowledge domains only. "
    f"claims: array of {{text, stance, evidence_chunk_ids, temporal_start, temporal_end}} "
    f"with stance in {sorted(CLAIM_STANCES)}. "
    f"artifacts: array of {{kind, title, content, evidence_chunk_ids}} with kind in "
    f"{sorted(ARTIFACT_KINDS)}. "
    "Never include secrets, credentials, or API keys in any field. "
    "Do not invent chunk ids, projects, or source text absent from the batch."
)
SEMANTIC_PROMPT_SHA256 = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SemanticBatch:
    """One project-scoped, version-scoped batch of evidence chunks."""

    project_id: str
    space_id: str
    source_id: str
    version_id: str
    batch_sha256: str
    chunks: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def chunk_ids(self) -> set[str]:
        return {str(chunk["chunk_id"]) for chunk in self.chunks}

    def cited_text(self, chunk_ids: set[str]) -> str:
        return "\n".join(
            str(chunk["text"]) for chunk in self.chunks if str(chunk["chunk_id"]) in chunk_ids
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "space_id": self.space_id,
            "source_id": self.source_id,
            "version_id": self.version_id,
            "batch_sha256": self.batch_sha256,
            "chunks": [
                {
                    "chunk_id": chunk["chunk_id"],
                    "ordinal": chunk["ordinal"],
                    "kind": chunk["kind"],
                    "header_path": json.loads(str(chunk.get("header_path_json") or "[]")),
                    "text": chunk["text"],
                }
                for chunk in self.chunks
            ],
        }


def _batch_sha256(chunk_ids: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for chunk_id in sorted(chunk_ids):
        digest.update(chunk_id.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_semantic_batches(
    chunk_rows: list[dict[str, Any]], *, batch_size: int = DEFAULT_BATCH_SIZE
) -> list[SemanticBatch]:
    """Group active chunks by project/space/version and split into batches.

    Only chunks sharing a project, knowledge space, source version, and
    adjacent ordinal order are batched together.  Partial trailing batches
    are preserved (the worker flushes them on the next cycle).
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    ordered = list(chunk_rows)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in ordered:
        key = (str(row["project_id"]), str(row["space_id"]), str(row["version_id"]))
        grouped.setdefault(key, []).append(row)
    batches: list[SemanticBatch] = []
    for (project_id, space_id, version_id), rows in grouped.items():
        source_ids = {str(row["source_id"]) for row in rows}
        source_id = next(iter(source_ids), "")
        for start in range(0, len(rows), batch_size):
            part = rows[start : start + batch_size]
            chunk_ids = tuple(str(row["chunk_id"]) for row in part)
            batches.append(
                SemanticBatch(
                    project_id=project_id,
                    space_id=space_id,
                    source_id=source_id,
                    version_id=version_id,
                    batch_sha256=_batch_sha256(chunk_ids),
                    chunks=tuple(part),
                )
            )
    return batches


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def _grounding_ratio(unit_text: str, cited_text: str) -> float:
    tokens = set(tokenize(unit_text))
    if not tokens:
        return 0.0
    cited = " ".join(str(cited_text or "").split()).lower()
    matched = sum(1 for token in tokens if token in cited)
    return matched / len(tokens)


class SemanticValidationError(ValueError):
    """Rejected semantic response with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SemanticResponseValidator:
    """Validate an untrusted cloud response against its source batch."""

    def validate(self, payload: Any, batch: SemanticBatch) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SemanticValidationError("semantic_payload_type", "response must be a JSON object")
        if str(payload.get("schema_version", "")) != SEMANTIC_SCHEMA_VERSION:
            raise SemanticValidationError(
                "semantic_schema_version",
                f"expected {SEMANTIC_SCHEMA_VERSION}",
            )
        allowed = set(payload)
        if allowed - {"schema_version", "units", "domains", "claims", "artifacts"}:
            raise SemanticValidationError(
                "semantic_unknown_keys",
                f"unexpected keys: {sorted(allowed - {'schema_version', 'units', 'domains', 'claims', 'artifacts'})[:5]}",
            )
        batch_ids = batch.chunk_ids()
        units = self._validate_units(payload.get("units") or [], batch, batch_ids)
        domains = self._validate_domains(payload.get("domains") or [])
        claims = self._validate_claims(payload.get("claims") or [], batch_ids)
        artifacts = self._validate_artifacts(payload.get("artifacts") or [])
        return {"units": units, "domains": domains, "claims": claims, "artifacts": artifacts}

    def _check_secret(self, *values: str) -> None:
        if any(contains_secret(value) for value in values if value):
            raise SemanticValidationError("semantic_secret_shape", "secret-shaped content rejected")

    def _validate_units(
        self, units: Any, batch: SemanticBatch, batch_ids: set[str]
    ) -> list[dict[str, Any]]:
        if not isinstance(units, list):
            raise SemanticValidationError("semantic_units_type", "units must be a list")
        if len(units) > _CAPS["units_per_response"]:
            raise SemanticValidationError("semantic_units_overflow", "too many units")
        validated: list[dict[str, Any]] = []
        for index, unit in enumerate(units):
            if not isinstance(unit, dict):
                raise SemanticValidationError(
                    "semantic_unit_type", f"units[{index}] must be an object"
                )
            kind = str(unit.get("kind") or "").strip()
            text = str(unit.get("text") or "").strip()
            evidence = unit.get("evidence_chunk_ids") or []
            if kind not in UNIT_KINDS:
                raise SemanticValidationError(
                    "semantic_unit_kind", f"unsupported unit kind: {kind[:64]}"
                )
            if not text or len(text) > _CAPS["unit_text"]:
                raise SemanticValidationError(
                    "semantic_unit_text", "unit text missing or oversized"
                )
            if not isinstance(evidence, list) or not evidence:
                raise SemanticValidationError(
                    "semantic_unit_evidence", "unit evidence_chunk_ids required"
                )
            evidence_ids = [str(item) for item in evidence]
            if len(evidence_ids) > _CAPS["evidence_ids_per_unit"]:
                raise SemanticValidationError(
                    "semantic_unit_evidence_overflow", "too many evidence ids"
                )
            unknown = [cid for cid in evidence_ids if cid not in batch_ids]
            if unknown:
                raise SemanticValidationError(
                    "semantic_unknown_chunk_id",
                    f"chunk ids not in batch: {unknown[:3]}",
                )
            self._check_secret(text)
            cited = batch.cited_text(set(evidence_ids))
            if kind in _VERBATIM_UNIT_KINDS:
                if _normalize(text) not in _normalize(cited):
                    raise SemanticValidationError(
                        "semantic_ungrounded_text",
                        f"verbatim unit {index} not present in cited chunks",
                    )
            elif _grounding_ratio(text, cited) < 0.3:
                raise SemanticValidationError(
                    "semantic_ungrounded_text",
                    f"unit {index} has insufficient grounding overlap",
                )
            metadata = unit.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise SemanticValidationError(
                    "semantic_unit_metadata", "unit metadata must be an object"
                )
            serialized = json.dumps(metadata, ensure_ascii=False)
            if len(serialized.encode("utf-8")) > _CAPS["metadata_bytes"]:
                raise SemanticValidationError(
                    "semantic_unit_metadata_overflow", "unit metadata oversized"
                )
            validated.append(
                {
                    "kind": kind,
                    "text": text,
                    "evidence_chunk_ids": evidence_ids,
                    "metadata": metadata,
                }
            )
        return validated

    def _validate_domains(self, domains: Any) -> list[dict[str, Any]]:
        if not isinstance(domains, list):
            raise SemanticValidationError("semantic_domains_type", "domains must be a list")
        if len(domains) > _CAPS["domains_per_response"]:
            raise SemanticValidationError("semantic_domains_overflow", "too many domains")
        validated: list[dict[str, Any]] = []
        for index, domain in enumerate(domains):
            if not isinstance(domain, dict):
                raise SemanticValidationError(
                    "semantic_domain_type", f"domains[{index}] must be an object"
                )
            name = str(domain.get("name") or "").strip()
            description = str(domain.get("description") or "").strip()
            if not name or len(name) > _CAPS["domain_name"]:
                raise SemanticValidationError(
                    "semantic_domain_name", "domain name missing or oversized"
                )
            if len(description) > _CAPS["domain_description"]:
                raise SemanticValidationError(
                    "semantic_domain_description", "domain description oversized"
                )
            self._check_secret(name, description)
            validated.append({"name": name, "description": description})
        return validated

    def _validate_claims(self, claims: Any, batch_ids: set[str]) -> list[dict[str, Any]]:
        if not isinstance(claims, list):
            raise SemanticValidationError("semantic_claims_type", "claims must be a list")
        if len(claims) > _CAPS["claims_per_response"]:
            raise SemanticValidationError("semantic_claims_overflow", "too many claims")
        validated: list[dict[str, Any]] = []
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                raise SemanticValidationError(
                    "semantic_claim_type", f"claims[{index}] must be an object"
                )
            text = str(claim.get("text") or "").strip()
            stance = str(claim.get("stance") or "neutral").strip()
            if not text or len(text) > _CAPS["claim_text"]:
                raise SemanticValidationError(
                    "semantic_claim_text", "claim text missing or oversized"
                )
            if stance not in CLAIM_STANCES:
                raise SemanticValidationError(
                    "semantic_claim_stance", f"unsupported stance: {stance[:64]}"
                )
            evidence = [str(item) for item in (claim.get("evidence_chunk_ids") or [])]
            unknown = [cid for cid in evidence if cid not in batch_ids]
            if unknown:
                raise SemanticValidationError(
                    "semantic_unknown_chunk_id",
                    f"claim evidence not in batch: {unknown[:3]}",
                )
            self._check_secret(text)
            validated.append(
                {
                    "claim_text": text,
                    "stance": stance,
                    "evidence_chunk_ids": evidence,
                    "temporal_start": claim.get("temporal_start"),
                    "temporal_end": claim.get("temporal_end"),
                    "risk_tier": "low",
                }
            )
        return validated

    def _validate_artifacts(self, artifacts: Any) -> list[dict[str, Any]]:
        if not isinstance(artifacts, list):
            raise SemanticValidationError("semantic_artifacts_type", "artifacts must be a list")
        if len(artifacts) > _CAPS["artifacts_per_response"]:
            raise SemanticValidationError("semantic_artifacts_overflow", "too many artifacts")
        validated: list[dict[str, Any]] = []
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                raise SemanticValidationError(
                    "semantic_artifact_type", f"artifacts[{index}] must be an object"
                )
            kind = str(artifact.get("kind") or "").strip()
            title = str(artifact.get("title") or "").strip()
            content = str(artifact.get("content") or "").strip()
            evidence = [str(item) for item in (artifact.get("evidence_chunk_ids") or [])]
            if kind not in ARTIFACT_KINDS:
                raise SemanticValidationError(
                    "semantic_artifact_kind", f"unsupported artifact kind: {kind[:64]}"
                )
            if not title or len(title) > _CAPS["artifact_title"]:
                raise SemanticValidationError(
                    "semantic_artifact_title", "artifact title missing or oversized"
                )
            if not content or len(content) > _CAPS["artifact_content"]:
                raise SemanticValidationError(
                    "semantic_artifact_content", "artifact content missing or oversized"
                )
            self._check_secret(title, content)
            validated.append(
                {
                    "kind": kind,
                    "title": title,
                    "content": content,
                    "evidence_chunk_ids": evidence,
                    "risk_tier": "high" if kind in HIGH_RISK_ARTIFACT_KINDS else "low",
                }
            )
        return validated


class KnowledgeSemanticProvider(Protocol):
    """Provider seam.  Implementations must never raise with secrets in text."""

    def complete_batch(self, batch: SemanticBatch) -> dict[str, Any]: ...


class _OpenAICompatibleBatchAdapter:
    """Adapt the shared structured-JSON provider to the batch contract."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def complete_batch(self, batch: SemanticBatch) -> dict[str, Any]:
        raw = self._provider.complete_json(
            system_prompt=_SYSTEM_PROMPT,
            user_text=json.dumps(batch.as_payload(), ensure_ascii=False),
        )
        if isinstance(raw, dict) and "content" in raw:
            raw = raw["content"]
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SemanticValidationError(
                    "semantic_invalid_json", "provider returned malformed JSON"
                ) from exc
        return raw if isinstance(raw, dict) else {}


def create_knowledge_semantic_provider() -> KnowledgeSemanticProvider | None:
    """Return the shared chunk-inference provider, or None when unconfigured.

    This is the credential boundary: the operator must set
    PP_MEMORY_CHUNK_ENRICHMENT_* (or PP_INFERENCE_*) before cloud semantic
    compilation activates.  A missing key degrades gracefully to None.
    """
    if knowledge_feature_gate(SEMANTIC_GATE) not in {"shadow", "on"}:
        return None
    try:
        from plastic_promise.skills.semantic_tool_routing import create_chunk_json_provider

        return _OpenAICompatibleBatchAdapter(create_chunk_json_provider(deterministic=True))
    except ValueError:
        return None


def _payload_hash(unit: dict[str, Any]) -> str:
    canonical = json.dumps(unit, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_error_detail(exc: Exception) -> str:
    """Return bounded diagnostic detail without persisting secret-shaped text."""
    detail = str(exc)[:2000]
    if not detail or contains_secret(detail):
        return type(exc).__name__
    return detail


class KnowledgeSemanticCoordinator:
    """Durable, project-scoped semantic compilation worker."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        provider: KnowledgeSemanticProvider | None = None,
        owner: str = "knowledge-semantic",
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size
        self._max_concurrency = min(DEFAULT_MAX_CONCURRENCY, max(1, int(max_concurrency)))
        self._heartbeat_interval_seconds = (
            max(0.05, float(heartbeat_interval_seconds))
            if heartbeat_interval_seconds is not None
            else min(30.0, max(0.1, float(lease_seconds) / 3.0))
        )
        self._lock = threading.Lock()

    def plan(
        self,
        project_id: str,
        *,
        limit_chunks: int = 500,
        partial_flush_seconds: float | None = None,
        after_cursor: SemanticChunkCursor | None = None,
    ) -> dict[str, Any]:
        """Create durable jobs for batches not yet compiled (idempotent)."""
        rows = self._repository.list_active_chunks_for_semantic(
            project_id,
            limit=limit_chunks,
            after_cursor=after_cursor,
        )
        batches = build_semantic_batches(rows, batch_size=self._batch_size)
        created = 0
        with self._lock:
            for batch in batches:
                if not self._batch_ready(batch, partial_flush_seconds):
                    continue
                if self._repository.semantic_job_by_batch(project_id, batch.batch_sha256) is None:
                    payload = batch.as_payload()
                    payload["project_id"] = project_id
                    self._repository.create_semantic_job(payload)
                    created += 1
        next_cursor = None
        if rows:
            last = rows[-1]
            next_cursor = SemanticChunkCursor(
                created_at=str(last["created_at"]),
                ordinal=int(last["ordinal"]),
                row_id=str(last["row_id"]),
            )
        return {
            "batches": len(batches),
            "created": created,
            "scanned": len(rows),
            "has_more": len(rows) == limit_chunks,
            "next_cursor": next_cursor,
        }

    def _batch_ready(
        self,
        batch: SemanticBatch,
        partial_flush_seconds: float | None,
    ) -> bool:
        if len(batch.chunks) >= self._batch_size or partial_flush_seconds is None:
            return True
        if partial_flush_seconds <= 0:
            return True
        timestamps: list[datetime] = []
        for chunk in batch.chunks:
            raw = str(chunk.get("created_at") or "")
            if not raw:
                return True
            try:
                timestamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                return True
        if not timestamps:
            return True
        age = datetime.now(timezone.utc) - max(timestamps)
        return age.total_seconds() >= partial_flush_seconds

    def process_next(
        self,
        *,
        provider: KnowledgeSemanticProvider | None = None,
        limit: int = 5,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim and compile ready batches.  Returns a bounded result summary."""
        provider = provider or self._provider or create_knowledge_semantic_provider()
        self._repository.reconcile_semantic_jobs()
        if provider is None:
            return {"processed": 0, "failed": 0, "reason": "provider_unconfigured"}
        summary: dict[str, Any] = {
            "processed": 0,
            "failed": 0,
            "units": 0,
            "domains": 0,
            "claims": 0,
            "artifacts": 0,
            "projects": [],
            "stale": 0,
        }
        jobs = self._repository.claim_ready_semantic_jobs(
            self._owner,
            limit=limit,
            lease_seconds=self._lease_seconds,
            project_id=project_id,
        )
        processed_projects: set[str] = set()
        pending: list[tuple[dict[str, Any], SemanticBatch, Future[dict[str, Any]]]] = []
        executor = ThreadPoolExecutor(
            max_workers=min(self._max_concurrency, max(1, len(jobs))),
            thread_name_prefix="knowledge-semantic-provider",
        )
        for job in jobs:
            try:
                batch_payload = json.loads(str(job["batch_json"]))
                batch = self._batch_from_payload(batch_payload)
                pending.append((job, batch, executor.submit(provider.complete_batch, batch)))
            except Exception as exc:
                self._repository.fail_semantic_job(
                    str(job["id"]),
                    "semantic_job_payload_error",
                    _safe_error_detail(exc),
                    retryable=False,
                    owner=self._owner,
                )
                summary["failed"] += 1
        future_jobs = {future: job for job, _batch, future in pending}
        unfinished = set(future_jobs)
        stale_futures: set[Future[dict[str, Any]]] = set()
        while unfinished:
            _done, unfinished = wait(
                unfinished,
                timeout=self._heartbeat_interval_seconds,
                return_when=FIRST_COMPLETED,
            )
            for future in unfinished - stale_futures:
                job = future_jobs[future]
                if not self._repository.renew_semantic_job_lease(
                    str(job["id"]),
                    owner=self._owner,
                    lease_seconds=self._lease_seconds,
                ):
                    stale_futures.add(future)
        executor.shutdown(wait=True)
        ready_by_project: dict[str, list[tuple[dict[str, Any], SemanticBatch, dict[str, Any]]]] = {}
        for job, batch, future in pending:
            if future in stale_futures:
                summary["stale"] += 1
                continue
            try:
                response = future.result()
                validated = SemanticResponseValidator().validate(response, batch)
                if not self._repository.renew_semantic_job_lease(
                    str(job["id"]),
                    owner=self._owner,
                    lease_seconds=self._lease_seconds,
                ):
                    summary["stale"] += 1
                    continue
                self._persist(job, batch, validated)
                ready_by_project.setdefault(str(job["project_id"]), []).append(
                    (job, batch, validated)
                )
            except SemanticValidationError as exc:
                self._repository.fail_semantic_job(
                    str(job["id"]),
                    exc.code,
                    _safe_error_detail(exc),
                    retryable=False,
                    owner=self._owner,
                )
                summary["failed"] += 1
            except Exception as exc:
                self._repository.fail_semantic_job(
                    str(job["id"]),
                    "semantic_provider_error",
                    _safe_error_detail(exc),
                    retryable=True,
                    owner=self._owner,
                )
                summary["failed"] += 1
        for project, ready in sorted(ready_by_project.items()):
            fenced = [
                item
                for item in ready
                if self._repository.renew_semantic_job_lease(
                    str(item[0]["id"]),
                    owner=self._owner,
                    lease_seconds=self._lease_seconds,
                )
            ]
            summary["stale"] += len(ready) - len(fenced)
            if not fenced:
                continue
            try:
                self._promote_derived(project)
            except Exception as exc:
                for job, _batch, _validated in fenced:
                    if self._repository.fail_semantic_job(
                        str(job["id"]),
                        "semantic_promotion_error",
                        _safe_error_detail(exc),
                        retryable=True,
                        owner=self._owner,
                    ):
                        summary["failed"] += 1
                continue
            for job, batch, validated in fenced:
                if not self._repository.complete_semantic_job(str(job["id"]), owner=self._owner):
                    summary["stale"] += 1
                    continue
                self._repository.audit(
                    project_id=project,
                    actor=self._owner,
                    action="semantic_compiled",
                    object_type="knowledge_semantic_job",
                    object_id=str(job["id"]),
                    detail={"batch_sha256": batch.batch_sha256},
                )
                summary["processed"] += 1
                summary["units"] += len(validated["units"])
                summary["domains"] += len(validated["domains"])
                summary["claims"] += len(validated["claims"])
                summary["artifacts"] += len(validated["artifacts"])
                processed_projects.add(project)
        summary["projects"] = sorted(processed_projects)
        return summary

    def _batch_from_payload(self, payload: dict[str, Any]) -> SemanticBatch:
        chunks = tuple(payload.get("chunks") or [])
        return SemanticBatch(
            project_id=str(payload.get("project_id") or ""),
            space_id=payload.get("space_id"),
            source_id=str(payload.get("source_id") or ""),
            version_id=payload.get("version_id"),
            batch_sha256=str(payload.get("batch_sha256") or ""),
            chunks=chunks,
        )

    def _persist(
        self,
        job: dict[str, Any],
        batch: SemanticBatch,
        validated: dict[str, Any],
    ) -> None:
        project_id = str(job["project_id"])
        source_id = batch.source_id or (str(batch.chunks[0]["source_id"]) if batch.chunks else None)
        units = []
        for unit in validated["units"]:
            units.append(
                {
                    "job_id": str(job["id"]),
                    "project_id": project_id,
                    "space_id": batch.space_id,
                    "source_id": source_id,
                    "version_id": batch.version_id,
                    "kind": unit["kind"],
                    "text": unit["text"],
                    "text_hash": hashlib.sha256(unit["text"].encode("utf-8")).hexdigest(),
                    "evidence_chunk_ids": unit["evidence_chunk_ids"],
                    "metadata": unit["metadata"],
                    "payload_hash": _payload_hash(unit),
                }
            )
        self._repository.insert_semantic_units(units)
        for domain in validated["domains"]:
            self._repository.upsert_domain_candidate(
                project_id=project_id,
                name=domain["name"],
                description=domain["description"],
                source_id=source_id or "",
                space_id=batch.space_id,
                evidence={"source_ids": source_id or "", "space_ids": batch.space_id or ""},
            )
        for claim in validated["claims"]:
            self._repository.insert_claim(
                {
                    "project_id": project_id,
                    "claim_text": claim["claim_text"],
                    "claim_hash": hashlib.sha256(claim["claim_text"].encode("utf-8")).hexdigest(),
                    "stance": claim["stance"],
                    "temporal_start": claim.get("temporal_start"),
                    "temporal_end": claim.get("temporal_end"),
                    "risk_tier": claim.get("risk_tier", "low"),
                    "evidence_chunk_ids": claim["evidence_chunk_ids"],
                    "source_id": source_id,
                }
            )
        for artifact in validated["artifacts"]:
            artifact_id = self._repository.upsert_artifact(
                {
                    "project_id": project_id,
                    "kind": artifact["kind"],
                    "title": artifact["title"],
                    "content": artifact["content"],
                    "content_hash": hashlib.sha256(
                        f"{artifact['kind']}\0{artifact['title']}\0{artifact['content']}".encode()
                    ).hexdigest(),
                    "risk_tier": artifact.get("risk_tier", "low"),
                    "source_ids": [source_id] if source_id else [],
                }
            )
            for chunk_id in artifact["evidence_chunk_ids"]:
                self._repository.insert_citation(artifact_id, chunk_id, project_id)

    def _promote_derived(self, project_id: str) -> None:
        from plastic_promise.knowledge.artifacts import promote_eligible_artifacts
        from plastic_promise.knowledge.domains import evaluate_domain_activations

        evaluate_domain_activations(self._repository, project_id)
        promote_eligible_artifacts(self._repository, project_id)

    def status(self, project_id: str) -> dict[str, Any]:
        return self._repository.semantic_status(project_id)
