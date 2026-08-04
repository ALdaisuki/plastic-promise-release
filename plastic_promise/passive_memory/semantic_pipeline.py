"""Durable asynchronous semantic classification for passive user memory."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from plastic_promise.core.derived_work import DerivedWorkLease, DerivedWorkStore
from plastic_promise.core.memory_proposals import (
    MemoryProposalStore,
    ProposalCandidate,
    contains_secret,
)
from plastic_promise.core.proposal_promotion import ProposalAutomation, auto_promotion_mode
from plastic_promise.skills.semantic_tool_routing import create_chunk_json_provider

if TYPE_CHECKING:
    from plastic_promise.passive_memory.events import PassiveMemoryEvent

SEMANTIC_JOB_KIND = "passive_semantic"
SEMANTIC_SCHEMA_VERSION = "passive-semantic-memory-v1"
SEMANTIC_CONFIG_REVISION = "passive-semantic-memory-v1"

_ASCII_GROUNDING_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_GROUNDING_TEXT = re.compile(r"[\u3400-\u9fff]+")
_NEGATION_TOKEN = re.compile(
    r"\b(?:cannot|can't|couldn't|didn't|doesn't|don't|isn't|never|no|not|shouldn't|"
    r"wasn't|weren't|without|won't|wouldn't)\b|[不没无非未勿否别莫]"
)
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?;,:，。！？；：]+|\b(?:and|but|whereas|while)\b|(?:但是|不过|以及|而且|但|而|却)"
)

_PROVIDER = None
_PROVIDER_LOCK = threading.Lock()
_RUNTIMES: dict[int, DurableSemanticMemoryWorker] = {}
_RUNTIMES_LOCK = threading.Lock()
_RUNTIME_FAILURES: dict[int, str] = {}
_LOGGER = logging.getLogger(__name__)


class SemanticSourceError(RuntimeError):
    pass


class SemanticOutputError(RuntimeError):
    pass


class DurableSemanticMemoryWorker:
    """Batch and classify passive user turns without crossing durable partitions."""

    def __init__(
        self,
        engine: Any,
        store: DerivedWorkStore,
        provider: Any,
        *,
        mode: str,
        batch_size: int = 20,
        max_wait_seconds: float = 30.0,
        lease_seconds: int = 180,
        retry_delay_seconds: int = 10,
        poll_seconds: float = 1.0,
        max_workers: int = 2,
        autostart: bool = True,
    ) -> None:
        normalized_mode = str(mode or "").strip().casefold()
        if normalized_mode not in {"shadow", "on"}:
            raise ValueError("passive_semantic_mode_invalid")
        self._engine = engine
        self._store = store
        self._provider = provider
        self._mode = normalized_mode
        self._batch_size = min(100, max(1, int(batch_size)))
        self._max_wait_seconds = min(3600.0, max(0.0, float(max_wait_seconds)))
        self._lease_seconds = min(15 * 60, max(1, int(lease_seconds)))
        self._retry_delay_seconds = min(3600, max(0, int(retry_delay_seconds)))
        self._poll_seconds = min(60.0, max(0.05, float(poll_seconds)))
        self._max_workers = min(8, max(1, int(max_workers)))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        if autostart:
            self.start()

    @property
    def provider_identity(self) -> str:
        return str(self._provider.identity)

    @property
    def store(self) -> DerivedWorkStore:
        return self._store

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=self._worker_loop,
                name=f"passive-semantic-{index + 1}",
                daemon=True,
            )
            for index in range(self._max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def wake(self) -> None:
        self._wake.set()

    def close(self, *, timeout: float = 5.0) -> bool:
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        drained = all(not thread.is_alive() for thread in self._threads)
        if drained:
            self._threads.clear()
        return drained

    def run_once(self, *, raise_errors: bool = False) -> bool:
        leases = self._store.claim_batch(
            limit=self._batch_size,
            job_kind=SEMANTIC_JOB_KIND,
            provider_identity=self.provider_identity,
            min_batch_size=self._batch_size,
            max_wait_seconds=self._max_wait_seconds,
            lease_seconds=self._lease_seconds,
        )
        if not leases:
            return False
        try:
            inputs = self._validated_inputs(leases)
            payload = self._provider.complete_json(
                system_prompt=(
                    "Return strict JSON with schema_version and items. Each item must contain "
                    "only content, category, confidence, source_indices, and evidence. "
                    "Categories are fact, preference, or decision. You may merge or split "
                    "facts, so item count need not match input count. Every evidence string "
                    "must be copied exactly from a selected user input, every selected input "
                    "must contribute evidence, and content must not introduce claim words absent "
                    "from that evidence. Never include secrets."
                ),
                user_payload={
                    "schema_version": SEMANTIC_SCHEMA_VERSION,
                    "scope": {
                        "project_id": leases[0].job.project_id,
                        "visibility": leases[0].job.visibility,
                        "config_revision": leases[0].job.config_revision,
                        "provider_identity": leases[0].job.provider_identity,
                    },
                    "inputs": inputs,
                },
                max_tokens=_bounded_int("PP_PASSIVE_SEMANTIC_MAX_TOKENS", 2048, 128, 8192),
            )
            candidates = self._validated_candidates(payload, leases, inputs)
            self._commit(leases, candidates)
        except BaseException as exc:
            self._fail(leases, exc)
            if raise_errors:
                raise
        return True

    def _validated_inputs(self, leases: tuple[DerivedWorkLease, ...]) -> list[dict[str, Any]]:
        inputs = []
        for index, lease in enumerate(leases):
            job = lease.job
            text = str(job.payload.get("user_text") or "")
            expected_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            if (
                job.job_kind != SEMANTIC_JOB_KIND
                or job.config_revision != SEMANTIC_CONFIG_REVISION
                or job.payload.get("schema") != SEMANTIC_SCHEMA_VERSION
                or job.payload.get("origin_role") != "user"
                or not text
                or len(text) > 8_000
                or contains_secret(text)
                or expected_hash != job.subject_hash
            ):
                raise SemanticSourceError("passive_semantic_source_invalid")
            inputs.append({"index": index, "user_text": text})
        return inputs

    def _validated_candidates(
        self,
        payload: Any,
        leases: tuple[DerivedWorkLease, ...],
        inputs: list[dict[str, Any]],
    ) -> list[tuple[ProposalCandidate, tuple[int, ...], dict[str, Any]]]:
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "items"}:
            raise SemanticOutputError("passive_semantic_output_invalid")
        if payload.get("schema_version") != SEMANTIC_SCHEMA_VERSION:
            raise SemanticOutputError("passive_semantic_output_invalid")
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > 100:
            raise SemanticOutputError("passive_semantic_output_invalid")
        candidates: list[tuple[ProposalCandidate, tuple[int, ...], dict[str, Any]]] = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "content",
                "category",
                "confidence",
                "source_indices",
                "evidence",
            }:
                raise SemanticOutputError("passive_semantic_output_invalid")
            content = " ".join(str(item.get("content") or "").split())
            category = str(item.get("category") or "").strip().casefold()
            confidence = item.get("confidence")
            indices = item.get("source_indices")
            evidence = item.get("evidence")
            if (
                not content
                or len(content) > 500
                or contains_secret(content)
                or category not in {"fact", "preference", "decision"}
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or float(confidence) < 0.5
                or not isinstance(indices, list)
                or not indices
                or any(isinstance(value, bool) or not isinstance(value, int) for value in indices)
            ):
                raise SemanticOutputError("passive_semantic_output_invalid")
            source_indices = tuple(dict.fromkeys(int(value) for value in indices))
            if any(index < 0 or index >= len(inputs) for index in source_indices):
                raise SemanticOutputError("passive_semantic_output_invalid")
            if not isinstance(evidence, list) or not evidence:
                raise SemanticOutputError("passive_semantic_output_invalid")
            source_texts = [inputs[index]["user_text"] for index in source_indices]
            if any(
                not isinstance(fragment, str)
                or not fragment
                or len(fragment) > 8_000
                or not any(fragment in source_text for source_text in source_texts)
                for fragment in evidence
            ):
                raise SemanticOutputError("passive_semantic_output_invalid")
            if any(
                not any(fragment in inputs[index]["user_text"] for fragment in evidence)
                for index in source_indices
            ) or not _content_is_grounded(content, evidence):
                raise SemanticOutputError("passive_semantic_output_invalid")
            origin_hash = (
                "sha256:"
                + hashlib.sha256(
                    "\x1f".join(leases[index].job.subject_hash for index in source_indices).encode(
                        "utf-8"
                    )
                ).hexdigest()
            )
            candidates.append(
                (
                    ProposalCandidate(
                        content=content,
                        category=category,
                        project_id=leases[0].job.project_id,
                        visibility=leases[0].job.visibility,
                        origin_role="user",
                        origin_turn_hash=origin_hash,
                        origin_call_id=str(
                            leases[source_indices[0]].job.payload.get("origin_call_id") or ""
                        ),
                        origin_visibility=leases[0].job.visibility,
                        metadata={
                            "capture_source": "passive_semantic_batch",
                            "semantic_schema": SEMANTIC_SCHEMA_VERSION,
                            "semantic_confidence": round(float(confidence), 6),
                            "classification_confidence": round(float(confidence), 6),
                            "semantic_source_count": len(source_indices),
                            "source_job_ids": [
                                leases[index].job.job_id for index in source_indices
                            ],
                        },
                    ),
                    source_indices,
                    {
                        "content": content,
                        "category": category,
                        "confidence": round(float(confidence), 6),
                        "evidence": list(evidence),
                        "source_job_ids": [
                            leases[source_index].job.job_id for source_index in source_indices
                        ],
                    },
                )
            )
        return candidates

    def _commit(
        self,
        leases: tuple[DerivedWorkLease, ...],
        candidates: list[tuple[ProposalCandidate, tuple[int, ...], dict[str, Any]]],
    ) -> None:
        connection = getattr(getattr(self._engine, "_sqlite", None), "_conn", None)
        if not isinstance(connection, sqlite3.Connection):
            raise SemanticSourceError("passive_semantic_canonical_store_unavailable")
        lock = getattr(self._engine, "_write_lock", threading.RLock())
        promotion_ids: list[str] = []
        with lock:
            if connection.in_transaction:
                raise RuntimeError("passive_semantic_canonical_transaction_open")
            connection.execute("BEGIN IMMEDIATE")
            try:
                persisted: list[tuple[ProposalCandidate, tuple[int, ...], dict[str, Any]]] = []
                if self._mode == "on":
                    if auto_promotion_mode() == "off":
                        proposal_store = MemoryProposalStore(connection)
                        for offset in range(0, len(candidates), 5):
                            batch = candidates[offset : offset + 5]
                            rows = proposal_store.create_many(
                                [candidate for candidate, _indices, _classification in batch]
                            )
                            persisted.extend(
                                (candidate, indices, row)
                                for (candidate, indices, _classification), row in zip(
                                    batch, rows, strict=True
                                )
                            )
                    else:
                        automation = ProposalAutomation(connection)
                        for candidate, source_indices, _classification in candidates:
                            proposal = None
                            for source_index in source_indices:
                                lease = leases[source_index]
                                payload = lease.job.payload
                                observation = automation.observe_candidate(
                                    replace(
                                        candidate,
                                        origin_turn_hash=str(
                                            payload.get("origin_turn_hash")
                                            or lease.job.subject_hash
                                        ),
                                        origin_call_id=str(payload.get("origin_call_id") or ""),
                                        metadata={
                                            **dict(candidate.metadata),
                                            "stage_session_id": str(
                                                payload.get("stage_session_id") or ""
                                            ),
                                            "request_id": str(payload.get("request_id") or ""),
                                            "flow_line_id": str(payload.get("flow_line_id") or ""),
                                        },
                                    )
                                )
                                proposal = observation.proposal
                            if proposal is not None:
                                persisted.append((candidate, source_indices, proposal))
                                promotion_ids.append(str(proposal["proposal_id"]))
                per_job: dict[int, list[str]] = {index: [] for index in range(len(leases))}
                per_job_classifications: dict[int, list[dict[str, Any]]] = {
                    index: [] for index in range(len(leases))
                }
                for _candidate, source_indices, classification in candidates:
                    for source_index in source_indices:
                        per_job_classifications[source_index].append(classification)
                for _candidate, source_indices, row in persisted:
                    for source_index in source_indices:
                        per_job[source_index].append(str(row["proposal_id"]))
                for index, lease in enumerate(leases):
                    self._store.complete_in_transaction(
                        connection,
                        job_id=lease.job.job_id,
                        project_id=lease.job.project_id,
                        lease_token=lease.lease_token,
                        fencing_generation=lease.job.fencing_generation,
                        result={
                            "schema": SEMANTIC_SCHEMA_VERSION,
                            "mode": self._mode,
                            "proposal_ids": list(dict.fromkeys(per_job[index])),
                            "classified_item_count": len(per_job_classifications[index]),
                            "classifications": per_job_classifications[index],
                        },
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        for proposal_id in dict.fromkeys(promotion_ids):
            try:
                from plastic_promise.core.proposal_promotion_jobs import (
                    enqueue_proposal_promotion_job,
                )

                enqueue_proposal_promotion_job(self._engine, proposal_id)
            except Exception:
                # Reconciliation owns crash recovery for post-commit enqueue gaps.
                continue

    def _fail(self, leases: tuple[DerivedWorkLease, ...], error: BaseException) -> None:
        source_failure = isinstance(error, SemanticSourceError)
        output_failure = isinstance(error, SemanticOutputError)
        failure_code = (
            "passive_semantic_source_invalid"
            if source_failure
            else "passive_semantic_output_invalid"
            if output_failure
            else "passive_semantic_provider_failed"
        )
        for lease in leases:
            try:
                self._store.fail(
                    job_id=lease.job.job_id,
                    project_id=lease.job.project_id,
                    lease_token=lease.lease_token,
                    fencing_generation=lease.job.fencing_generation,
                    failure_code=failure_code,
                    retryable=not source_failure,
                    retry_delay_seconds=self._retry_delay_seconds,
                )
            except Exception:
                continue

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            if self.run_once():
                continue
            self._wake.wait(self._poll_seconds)
            self._wake.clear()


def semantic_capture_mode() -> str:
    mode = os.getenv("PP_PASSIVE_SEMANTIC_CAPTURE", "off").strip().casefold()
    return mode if mode in {"off", "shadow", "on"} else "off"


def get_semantic_memory_provider():
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = create_chunk_json_provider()
        return _PROVIDER


def enqueue_semantic_capture(
    engine: Any,
    event: PassiveMemoryEvent,
    *,
    user_text: str,
) -> dict[str, Any]:
    """Persist one idempotent semantic job without invoking the provider."""

    mode = semantic_capture_mode()
    text = str(user_text or "")
    eligibility_text = text.strip()
    if mode == "off":
        return {"status": "skipped", "reason": "semantic_capture_disabled"}
    if (
        not eligibility_text
        or len(text) > 8_000
        or eligibility_text.endswith(("?", "？"))
        or contains_secret(text)
    ):
        return {"status": "skipped", "reason": "semantic_capture_ineligible"}
    db_path = _canonical_db_path(engine)
    if not db_path:
        return {"status": "degraded", "reason": "canonical_store_unavailable"}
    provider = get_semantic_memory_provider()
    content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    material = {
        "schema": SEMANTIC_SCHEMA_VERSION,
        "project_id": event.project_id,
        "visibility": event.visibility,
        "config_revision": SEMANTIC_CONFIG_REVISION,
        "provider_identity": provider.identity,
        "content_hash": content_hash,
        "origin_turn_hash": event.origin_turn_hash(text),
    }
    dedupe_key = (
        "passive-semantic:"
        + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    result = DerivedWorkStore(db_path).enqueue(
        project_id=event.project_id or "project:unknown",
        visibility=event.visibility,
        config_revision=SEMANTIC_CONFIG_REVISION,
        job_kind=SEMANTIC_JOB_KIND,
        provider_identity=provider.identity,
        subject_id="passive-turn:" + content_hash.removeprefix("sha256:")[:24],
        subject_hash=content_hash,
        dedupe_key=dedupe_key,
        payload={
            "schema": SEMANTIC_SCHEMA_VERSION,
            "user_text": text,
            "origin_role": "user",
            "origin_turn_hash": event.origin_turn_hash(text),
            "origin_call_id": event.call_id,
            "request_id": event.request_id,
            "stage_session_id": event.stage_session_id,
            "flow_line_id": event.flow_line_id,
            "source": event.source,
        },
        max_attempts=_bounded_int("PP_PASSIVE_SEMANTIC_MAX_ATTEMPTS", 4, 1, 20),
        max_active_jobs=_bounded_int("PP_PASSIVE_SEMANTIC_MAX_QUEUE", 4096, 1, 100_000),
    )
    runtime = initialize_semantic_memory_runtime(engine)
    if runtime is not None:
        runtime.wake()
    return {
        "status": "queued" if result.created else "duplicate",
        "reason": "",
        "job_id": result.job.job_id,
        "created": result.created,
        "provider_identity": result.job.provider_identity,
        "config_revision": result.job.config_revision,
        "mode": mode,
        "worker_available": runtime is not None,
    }


def initialize_semantic_memory_runtime(
    engine: Any,
    *,
    autostart: bool | None = None,
) -> DurableSemanticMemoryWorker | None:
    mode = semantic_capture_mode()
    if mode == "off":
        return None
    engine_id = id(engine)
    with _RUNTIMES_LOCK:
        if engine_id in _RUNTIMES:
            return _RUNTIMES[engine_id]
        if engine_id in _RUNTIME_FAILURES:
            return None
        db_path = _canonical_db_path(engine)
        if not db_path:
            _RUNTIME_FAILURES[engine_id] = "semantic_memory_runtime_db_unavailable"
            return None
        try:
            should_start = (
                os.getenv("PP_PASSIVE_SEMANTIC_WORKER_AUTOSTART", "1").strip() == "1"
                if autostart is None
                else bool(autostart)
            )
            runtime = DurableSemanticMemoryWorker(
                engine,
                DerivedWorkStore(
                    db_path,
                    default_lease_seconds=_bounded_int(
                        "PP_PASSIVE_SEMANTIC_LEASE_SECONDS", 180, 1, 15 * 60
                    ),
                ),
                get_semantic_memory_provider(),
                mode=mode,
                batch_size=_bounded_int("PP_PASSIVE_SEMANTIC_BATCH_SIZE", 20, 1, 100),
                max_wait_seconds=_bounded_float(
                    "PP_PASSIVE_SEMANTIC_MAX_WAIT_SECONDS", 30.0, 0.0, 3600.0
                ),
                lease_seconds=_bounded_int("PP_PASSIVE_SEMANTIC_LEASE_SECONDS", 180, 1, 15 * 60),
                retry_delay_seconds=_bounded_int("PP_PASSIVE_SEMANTIC_RETRY_SECONDS", 10, 0, 3600),
                poll_seconds=_bounded_float("PP_PASSIVE_SEMANTIC_POLL_SECONDS", 1.0, 0.05, 60.0),
                max_workers=_bounded_int("PP_PASSIVE_SEMANTIC_MAX_WORKERS", 2, 1, 8),
                autostart=should_start,
            )
        except Exception as exc:
            _RUNTIME_FAILURES[engine_id] = "semantic_memory_runtime_init_failed"
            _LOGGER.error(
                "semantic_memory_runtime_init_failed exception_type=%s",
                exc.__class__.__name__,
            )
            return None
        _RUNTIMES[engine_id] = runtime
        return runtime


def process_semantic_memory_jobs(engine: Any, *, max_batches: int = 1) -> dict[str, Any]:
    if semantic_capture_mode() == "off":
        return {"skipped": "semantic_capture_disabled", "processed_batches": 0}
    runtime = initialize_semantic_memory_runtime(engine, autostart=False)
    if runtime is None:
        return {
            "skipped": "semantic_memory_runtime_unavailable",
            "failure_code": _RUNTIME_FAILURES.get(
                id(engine), "semantic_memory_runtime_unavailable"
            ),
            "processed_batches": 0,
        }
    processed = 0
    for _index in range(min(100, max(0, int(max_batches)))):
        if not runtime.run_once():
            break
        processed += 1
    return {"processed_batches": processed}


def close_semantic_memory_runtime(engine: Any, *, timeout: float = 5.0) -> bool:
    """Stop and forget one process-local worker during controlled shutdown."""

    with _RUNTIMES_LOCK:
        runtime = _RUNTIMES.pop(id(engine), None)
        _RUNTIME_FAILURES.pop(id(engine), None)
    return runtime.close(timeout=timeout) if runtime is not None else False


def _canonical_db_path(engine: Any) -> str:
    connection = getattr(getattr(engine, "_sqlite", None), "_conn", None)
    if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
        return ""
    try:
        return next(
            (
                str(row[2] or "").strip()
                for row in connection.execute("PRAGMA database_list").fetchall()
                if len(row) >= 3 and str(row[1]) == "main"
            ),
            "",
        )
    except sqlite3.Error:
        return ""


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _content_is_grounded(content: str, evidence: list[str]) -> bool:
    normalized_content = unicodedata.normalize("NFKC", content).casefold()
    normalized_evidence = " ".join(
        unicodedata.normalize("NFKC", fragment).casefold() for fragment in evidence
    )
    if not _polarity_units(normalized_content).issubset(_polarity_units(normalized_evidence)):
        return False
    content_ascii = {
        _grounding_stem(token) for token in _ASCII_GROUNDING_TOKEN.findall(normalized_content)
    }
    evidence_ascii = {
        _grounding_stem(token) for token in _ASCII_GROUNDING_TOKEN.findall(normalized_evidence)
    }
    if not content_ascii.issubset(evidence_ascii):
        return False
    content_cjk = _cjk_grounding_units(normalized_content)
    evidence_cjk = _cjk_grounding_units(normalized_evidence)
    if not content_cjk.issubset(evidence_cjk):
        return False
    return bool(content_ascii or content_cjk)


def _grounding_stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) <= len(suffix) + 2 or not token.endswith(suffix):
            continue
        stem = token[: -len(suffix)]
        if suffix in {"ing", "ed"} and len(stem) > 2 and stem[-1] == stem[-2]:
            stem = stem[:-1]
        return stem
    return token


def _cjk_grounding_units(text: str) -> set[str]:
    units: set[str] = set()
    for segment in _CJK_GROUNDING_TEXT.findall(text):
        if len(segment) == 1:
            units.add(segment)
        else:
            units.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return units


def _polarity_units(text: str) -> set[tuple[str, bool]]:
    units: set[tuple[str, bool]] = set()
    for clause in _CLAUSE_BOUNDARY.split(text):
        if not clause.strip():
            continue
        negated = bool(_NEGATION_TOKEN.search(clause))
        ascii_tokens = [_grounding_stem(token) for token in _ASCII_GROUNDING_TOKEN.findall(clause)]
        units.update((f"ascii:{token}", negated) for token in ascii_tokens)
        units.update((f"cjk:{unit}", negated) for unit in _cjk_grounding_units(clause))
    return units
