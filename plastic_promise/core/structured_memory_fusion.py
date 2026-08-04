"""Project-isolated asynchronous structured-memory fusion.

The module intentionally separates three concerns:

* callers submit canonical source material;
* a bounded scheduler batches only sources with an identical project, visibility,
  and fusion identity; and
* a processor validates variable-cardinality model output before a caller decides
  whether and how to persist it.

The processor never promotes a proposal, writes a canonical memory, or widens a
visibility scope.  This lets Hook work and the formal memory pipeline reuse the
same structured-fusion implementation without bypassing their different
governance rules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from plastic_promise.core.derived_work import (
    DerivedWorkConflictError,
    DerivedWorkCreateResult,
    DerivedWorkLease,
    DerivedWorkStore,
)

FUSION_SCHEMA_VERSION = "structured-memory-fusion/v1"
DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_WAIT_SECONDS = 2.0
DEFAULT_MAX_QUEUE_SIZE = 1_000
DEFAULT_MAX_WORKERS = 2
DEFAULT_MAX_OUTPUT_CHUNKS = 80
DEFAULT_MAX_CHUNK_CHARS = 4_000
DEFAULT_MAX_EVIDENCE_PER_CHUNK = 16
FUSION_MODE_ENV = "PP_STRUCTURED_MEMORY_FUSION"
FUSION_CONFIG_REVISION_ENV = "PP_STRUCTURED_MEMORY_FUSION_CONFIG_REVISION"
FUSION_JOB_KIND = "structured_fusion"
DEFAULT_LEASE_SECONDS = 120
DEFAULT_RETRY_DELAY_SECONDS = 5
DEFAULT_WORKER_POLL_SECONDS = 0.25

SYSTEM_PROMPT = """You consolidate related memory sources into retrieval chunks.
Return only a JSON object with this exact top-level shape:
{"chunks":[{"text":"...","source_ids":["..."],"evidence":[{"source_id":"...","start":0,"end":1}]}]}.
The number of returned chunks is intentionally unconstrained: return zero or more
useful chunks, never padding to match the input count. Each chunk must cite one or
more source_ids from the request. Evidence spans must point to exact source text;
do not cite a source that does not support the chunk. Do not include instructions,
secrets, provider configuration, or content outside the provided sources."""


class StructuredJSONProvider(Protocol):
    """Minimal cloud/local JSON interface used by the fusion processor."""

    @property
    def identity(self) -> str: ...

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        max_tokens: int = 768,
    ) -> dict[str, object]: ...


class FusionValidationError(ValueError):
    """The untrusted processor output does not satisfy the fusion contract."""


class FusionQueueFull(RuntimeError):
    """The bounded scheduler rejected a new source before any remote call."""


class FusionSourceStaleError(RuntimeError):
    """A durable receipt no longer matches its canonical source row."""


@dataclass(frozen=True)
class FusionScope:
    """The isolation key of a batchable fusion source."""

    project_id: str
    visibility: str
    config_revision: str

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ValueError("fusion_project_id_required")
        if self.visibility not in {"private", "project", "shared", "global"}:
            raise ValueError("fusion_visibility_invalid")
        if not self.config_revision.strip():
            raise ValueError("fusion_config_revision_required")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.project_id, self.visibility, self.config_revision)


@dataclass(frozen=True)
class FusionSource:
    """One immutable source submitted to a project-scoped fusion batch."""

    source_id: str
    content: str
    content_hash: str
    scope: FusionScope
    memory_type: str = ""

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("fusion_source_id_required")
        if not self.content.strip():
            raise ValueError("fusion_source_content_required")
        expected = sha256_text(self.content)
        if self.content_hash != expected:
            raise ValueError("fusion_source_hash_mismatch")

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        content: str,
        project_id: str,
        visibility: str = "project",
        config_revision: str,
        memory_type: str = "",
    ) -> FusionSource:
        return cls(
            source_id=source_id,
            content=content,
            content_hash=sha256_text(content),
            scope=FusionScope(project_id, visibility, config_revision),
            memory_type=memory_type,
        )


@dataclass(frozen=True)
class FusionEvidence:
    """An exact source span supporting an untrusted fused chunk."""

    source_id: str
    content_hash: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class FusedChunk:
    """Validated variable-cardinality derived material with source provenance."""

    chunk_id: str
    batch_id: str
    project_id: str
    visibility: str
    text: str
    source_ids: tuple[str, ...]
    evidence: tuple[FusionEvidence, ...]


@dataclass(frozen=True)
class FusionBatchResult:
    """One processor result shared by every source submitted in the batch."""

    batch_id: str
    scope: FusionScope
    sources: tuple[FusionSource, ...]
    chunks: tuple[FusedChunk, ...]
    fusion_identity: str
    dispatched_reason: str


@dataclass
class _PendingSource:
    source: FusionSource
    submitted_at: float
    futures: list[Future[FusionBatchResult]] = field(default_factory=list)


class StructuredMemoryFusion:
    """Validate variable-cardinality chunk fusion from a JSON-capable model.

    A result is intentionally not required to have the same cardinality as its
    input.  The invariant is provenance: every returned chunk must name and cite
    only sources supplied in its own project-scoped batch.
    """

    def __init__(
        self,
        provider: StructuredJSONProvider,
        *,
        max_output_chunks: int = DEFAULT_MAX_OUTPUT_CHUNKS,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
        max_evidence_per_chunk: int = DEFAULT_MAX_EVIDENCE_PER_CHUNK,
        max_tokens: int = 2_048,
    ) -> None:
        identity = str(getattr(provider, "identity", "") or "").strip()
        if not identity:
            raise ValueError("fusion_provider_identity_required")
        self._provider = provider
        self._identity = identity
        self._max_output_chunks = _bounded_positive_int(
            max_output_chunks, DEFAULT_MAX_OUTPUT_CHUNKS
        )
        self._max_chunk_chars = _bounded_positive_int(max_chunk_chars, DEFAULT_MAX_CHUNK_CHARS)
        self._max_evidence_per_chunk = _bounded_positive_int(
            max_evidence_per_chunk, DEFAULT_MAX_EVIDENCE_PER_CHUNK
        )
        self._max_tokens = _bounded_positive_int(max_tokens, 2_048)

    @property
    def identity(self) -> str:
        return self._identity

    def close(self) -> None:
        close = getattr(self._provider, "close", None)
        if callable(close):
            close()

    def fuse(
        self,
        sources: list[FusionSource] | tuple[FusionSource, ...],
        *,
        dispatched_reason: str,
    ) -> FusionBatchResult:
        ordered = tuple(sources)
        scope = _validate_sources(ordered)
        batch_id = _batch_id(scope, self._identity, ordered)
        payload = {
            "schema": FUSION_SCHEMA_VERSION,
            "batch_id": batch_id,
            "project_id": scope.project_id,
            "visibility": scope.visibility,
            "sources": [
                {
                    "source_id": source.source_id,
                    "content": source.content,
                    "content_hash": source.content_hash,
                    "memory_type": source.memory_type,
                }
                for source in ordered
            ],
        }
        raw = self._provider.complete_json(
            system_prompt=SYSTEM_PROMPT,
            user_payload=payload,
            max_tokens=self._max_tokens,
        )
        chunks = _validate_output(
            raw,
            batch_id=batch_id,
            scope=scope,
            sources=ordered,
            max_output_chunks=self._max_output_chunks,
            max_chunk_chars=self._max_chunk_chars,
            max_evidence_per_chunk=self._max_evidence_per_chunk,
        )
        return FusionBatchResult(
            batch_id=batch_id,
            scope=scope,
            sources=ordered,
            chunks=chunks,
            fusion_identity=self._identity,
            dispatched_reason=dispatched_reason,
        )


class ProjectScopedFusionBatcher:
    """Bounded asynchronous micro-batcher with strict project isolation.

    Every submitted source receives a future for the batch result.  All futures
    in a batch resolve to the same result because a fused chunk may describe any
    subset of the sources and output cardinality is deliberately variable.
    """

    def __init__(
        self,
        fusion: StructuredMemoryFusion,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        max_workers: int = DEFAULT_MAX_WORKERS,
        on_result: Callable[[FusionBatchResult], object] | None = None,
    ) -> None:
        self._fusion = fusion
        self._batch_size = _bounded_positive_int(batch_size, DEFAULT_BATCH_SIZE)
        self._max_wait_seconds = _bounded_positive_float(max_wait_seconds, DEFAULT_MAX_WAIT_SECONDS)
        self._max_queue_size = _bounded_positive_int(max_queue_size, DEFAULT_MAX_QUEUE_SIZE)
        self._on_result = on_result
        self._condition = threading.Condition(threading.RLock())
        self._buckets: dict[tuple[str, str, str], deque[_PendingSource]] = {}
        self._active_batches = 0
        self._closed = False
        self._dispatcher_stop = False
        self._executor = ThreadPoolExecutor(
            max_workers=_bounded_positive_int(max_workers, DEFAULT_MAX_WORKERS),
            thread_name_prefix="structured-memory-fusion",
        )
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="structured-memory-fusion-dispatcher",
            daemon=True,
        )
        self._dispatcher.start()

    @property
    def fusion_identity(self) -> str:
        return self._fusion.identity

    def submit(self, source: FusionSource) -> Future[FusionBatchResult]:
        """Queue a source without waiting for cloud inference or persistence."""

        future: Future[FusionBatchResult] = Future()
        with self._condition:
            if self._closed:
                future.set_exception(RuntimeError("fusion_batcher_closed"))
                return future
            if self._pending_count_locked() >= self._max_queue_size:
                future.set_exception(FusionQueueFull("fusion_queue_full"))
                return future
            bucket = self._buckets.setdefault(source.scope.key, deque())
            bucket.append(
                _PendingSource(source=source, submitted_at=time.monotonic(), futures=[future])
            )
            self._dispatch_full_buckets_locked()
            self._condition.notify_all()
        return future

    def flush(self) -> int:
        """Dispatch all pending buckets now; useful for controlled shutdown/tests."""

        with self._condition:
            dispatched = 0
            for key in list(self._buckets):
                while self._buckets.get(key):
                    self._submit_batch_locked(key, reason="flush")
                    dispatched += 1
            self._condition.notify_all()
            return dispatched

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._pending_count_locked() or self._active_batches:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def close(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        self.flush()
        with self._condition:
            self._closed = True
            self._dispatcher_stop = True
            self._condition.notify_all()
        if wait:
            self.wait_for_idle(timeout=timeout)
        self._executor.shutdown(wait=wait, cancel_futures=False)
        if wait and self._dispatcher.is_alive():
            self._dispatcher.join(timeout=max(0.0, float(timeout)))

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                if self._dispatcher_stop:
                    return
                self._dispatch_full_buckets_locked()
                timeout = self._seconds_until_oldest_deadline_locked()
                if timeout is None:
                    self._condition.wait()
                    continue
                if timeout > 0:
                    self._condition.wait(timeout=timeout)
                    continue
                for key in list(self._buckets):
                    bucket = self._buckets.get(key)
                    if (
                        bucket
                        and time.monotonic() - bucket[0].submitted_at >= self._max_wait_seconds
                    ):
                        self._submit_batch_locked(key, reason="max_wait")

    def _dispatch_full_buckets_locked(self) -> None:
        for key in list(self._buckets):
            while len(self._buckets.get(key, ())) >= self._batch_size:
                self._submit_batch_locked(key, reason="batch_size")

    def _submit_batch_locked(self, key: tuple[str, str, str], *, reason: str) -> None:
        bucket = self._buckets.get(key)
        if not bucket:
            return
        pending = tuple(bucket.popleft() for _ in range(min(self._batch_size, len(bucket))))
        if not bucket:
            self._buckets.pop(key, None)
        self._active_batches += 1
        self._executor.submit(self._run_batch, pending, reason)

    def _run_batch(self, pending: tuple[_PendingSource, ...], reason: str) -> None:
        try:
            result = self._fusion.fuse([item.source for item in pending], dispatched_reason=reason)
            if self._on_result is not None:
                self._on_result(result)
        except BaseException as exc:
            for item in pending:
                for future in item.futures:
                    if not future.done():
                        future.set_exception(exc)
        else:
            for item in pending:
                for future in item.futures:
                    if not future.done():
                        future.set_result(result)
        finally:
            with self._condition:
                self._active_batches -= 1
                self._condition.notify_all()

    def _seconds_until_oldest_deadline_locked(self) -> float | None:
        oldest = [bucket[0].submitted_at for bucket in self._buckets.values() if bucket]
        if not oldest:
            return None
        return max(0.0, min(oldest) + self._max_wait_seconds - time.monotonic())

    def _pending_count_locked(self) -> int:
        return sum(len(bucket) for bucket in self._buckets.values())


class GovernedSynthesisDraftSink:
    """Persist only validated multi-source results as unverified synthesis drafts.

    The sink is deliberately downstream of the batch processor.  It cannot make
    Hook proposals canonical, cannot verify a draft, and cannot widen a source's
    visibility.  The existing synthesis state machine remains the sole route to
    retrieval-visible derived memory.
    """

    def __init__(
        self,
        engine: object,
        *,
        store_factory: Callable[[object, object], object] | None = None,
    ) -> None:
        self._engine = engine
        self._store_factory = store_factory

    def __call__(self, result: FusionBatchResult) -> dict[str, int]:
        return self.persist(result)

    def persist(
        self,
        result: FusionBatchResult,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, int]:
        """Persist drafts on the engine connection or a caller-owned transaction."""

        conn = connection or getattr(getattr(self._engine, "_sqlite", None), "_conn", None)
        if conn is None:
            return {"created": 0, "shadowed": 0, "skipped": len(result.chunks), "failed": 0}
        lock = getattr(self._engine, "_write_lock", threading.RLock())
        created = 0
        shadowed = 0
        skipped = 0
        failed = 0
        from plastic_promise.core.synthesis import SynthesisConflict

        with lock:
            store = self._store(conn)
            for chunk in result.chunks:
                # A one-source output may be useful derived retrieval material in
                # the future, but it is not a governed synthesis artifact.
                if len(chunk.source_ids) < 2:
                    skipped += 1
                    continue
                try:
                    artifact = store.create_draft(
                        chunk.text,
                        chunk.source_ids,
                        synthesis_key=f"{result.batch_id}:{chunk.chunk_id}",
                        validity_scope=FUSION_SCHEMA_VERSION,
                        project_id=result.scope.project_id,
                        visibility=result.scope.visibility,
                        actor="system:structured-memory-fusion",
                        call_id=f"fusion:{result.batch_id}",
                        automatic=True,
                        reuse_signal=True,
                        metadata={
                            "fusion_schema": FUSION_SCHEMA_VERSION,
                            "fusion_identity": result.fusion_identity,
                            "fusion_batch_id": result.batch_id,
                            "fusion_chunk_id": chunk.chunk_id,
                            "config_revision": result.scope.config_revision,
                            "dispatched_reason": result.dispatched_reason,
                            "source_evidence": [
                                {
                                    "source_id": evidence.source_id,
                                    "content_hash": evidence.content_hash,
                                    "start": evidence.start,
                                    "end": evidence.end,
                                }
                                for evidence in chunk.evidence
                            ],
                        },
                    )
                except SynthesisConflict as exc:
                    # A duplicate key, a source mutation, or a synthesis gate is
                    # a per-chunk outcome.  Do not fail unrelated valid chunks or
                    # expose source content in logs.
                    logging.debug("Structured fusion draft skipped: %s", exc.__class__.__name__)
                    failed += 1
                    continue
                if artifact is None:
                    shadowed += 1
                else:
                    created += 1
        return {"created": created, "shadowed": shadowed, "skipped": skipped, "failed": failed}

    def _store(self, conn: object) -> object:
        if self._store_factory is not None:
            return self._store_factory(conn, self._engine)
        from plastic_promise.core.synthesis import SynthesisStore

        return SynthesisStore(conn, engine=self._engine)


class _BatchLeaseHeartbeat:
    """Renew all capabilities in one provider batch until local application starts."""

    def __init__(
        self,
        store: DerivedWorkStore,
        leases: tuple[DerivedWorkLease, ...],
        *,
        lease_seconds: int,
    ) -> None:
        self._store = store
        self._leases = leases
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="structured-fusion-lease-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._lease_seconds / 2))

    def require_healthy(self) -> None:
        if self._lost.is_set():
            raise DerivedWorkConflictError("derived_work_batch_lease_lost")

    def _run(self) -> None:
        interval = max(0.25, self._lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                for lease in self._leases:
                    self._store.renew_lease(
                        job_id=lease.job.job_id,
                        project_id=lease.job.project_id,
                        lease_token=lease.lease_token,
                        fencing_generation=lease.job.fencing_generation,
                        lease_seconds=self._lease_seconds,
                    )
            except Exception:
                self._lost.set()
                return


class DurableFusionWorker:
    """Restart-safe adapter from durable derived work to structured fusion.

    The queue stores only source identities and hashes.  Canonical text is read
    from SQLite after a lease is obtained, provider work runs outside a write
    transaction, and draft persistence plus completion receipts commit together.
    """

    def __init__(
        self,
        engine: object,
        store: DerivedWorkStore,
        fusion: StructuredMemoryFusion,
        *,
        mode: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        max_workers: int = DEFAULT_MAX_WORKERS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS,
        sink: GovernedSynthesisDraftSink | None = None,
        autostart: bool = True,
    ) -> None:
        normalized_mode = str(mode or "").strip().casefold()
        if normalized_mode not in {"shadow", "on"}:
            raise ValueError("fusion_durable_mode_invalid")
        self._engine = engine
        self._store = store
        self._fusion = fusion
        self._mode = normalized_mode
        self._batch_size = _bounded_positive_int(batch_size, DEFAULT_BATCH_SIZE)
        self._max_wait_seconds = _bounded_positive_float(
            max_wait_seconds,
            DEFAULT_MAX_WAIT_SECONDS,
        )
        self._max_queue_size = _bounded_positive_int(max_queue_size, DEFAULT_MAX_QUEUE_SIZE)
        self._max_workers = _bounded_positive_int(max_workers, DEFAULT_MAX_WORKERS)
        self._lease_seconds = _bounded_positive_int(lease_seconds, DEFAULT_LEASE_SECONDS)
        self._retry_delay_seconds = max(0, int(retry_delay_seconds))
        self._poll_seconds = _bounded_positive_float(poll_seconds, DEFAULT_WORKER_POLL_SECONDS)
        self._sink = sink if normalized_mode == "on" else None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lifecycle_lock = threading.Lock()
        self._active_workers = 0
        self._close_requested = False
        self._fusion_closed = False
        if autostart:
            self.start()

    @property
    def fusion_identity(self) -> str:
        return self._fusion.identity

    @property
    def store(self) -> DerivedWorkStore:
        return self._store

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._close_requested:
                raise RuntimeError("fusion_worker_closed")
            if any(thread.is_alive() for thread in self._threads):
                return
            self._threads.clear()
            for index in range(self._max_workers):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"structured-fusion-durable-{index + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                self._active_workers += 1
                try:
                    thread.start()
                except BaseException:
                    self._active_workers -= 1
                    self._threads.pop()
                    self._stop.set()
                    raise

    def enqueue_source(self, source: FusionSource) -> DerivedWorkCreateResult:
        result = self._store.enqueue(**self._enqueue_values(source))
        self._wake.set()
        return result

    def enqueue_source_in_transaction(
        self,
        connection: sqlite3.Connection,
        source: FusionSource,
    ) -> DerivedWorkCreateResult:
        result = self._store.enqueue_in_transaction(connection, **self._enqueue_values(source))
        self._wake.set()
        return result

    def run_once(self, *, raise_errors: bool = False) -> bool:
        """Claim and process one durable batch; useful for workers and tests."""

        leases = self._store.claim_batch(
            limit=self._batch_size,
            job_kind=FUSION_JOB_KIND,
            provider_identity=self.fusion_identity,
            min_batch_size=self._batch_size,
            max_wait_seconds=self._max_wait_seconds,
            lease_seconds=self._lease_seconds,
        )
        if not leases:
            return False
        heartbeat = _BatchLeaseHeartbeat(
            self._store,
            leases,
            lease_seconds=self._lease_seconds,
        )
        heartbeat.start()
        try:
            sources = self._load_sources(leases)
            result = self._fusion.fuse(sources, dispatched_reason="durable_batch")
            heartbeat.stop()
            heartbeat.require_healthy()
            self._commit_result(leases, result)
        except BaseException as exc:
            heartbeat.stop()
            self._fail_leases(leases, exc)
            if raise_errors:
                raise
        return True

    def close(self, *, timeout: float = 5.0) -> bool:
        """Request shutdown and close the provider only after every worker exits."""

        with self._lifecycle_lock:
            self._close_requested = True
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in tuple(self._threads):
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        close_fusion = False
        with self._lifecycle_lock:
            drained = self._active_workers == 0
            if drained:
                self._threads.clear()
                if not self._fusion_closed:
                    self._fusion_closed = True
                    close_fusion = True
        if close_fusion:
            self._fusion.close()
        return drained

    def _enqueue_values(self, source: FusionSource) -> dict[str, object]:
        material = {
            "schema": FUSION_SCHEMA_VERSION,
            "project_id": source.scope.project_id,
            "visibility": source.scope.visibility,
            "config_revision": source.scope.config_revision,
            "provider_identity": self.fusion_identity,
            "source_id": source.source_id,
            "content_hash": source.content_hash,
        }
        dedupe_key = (
            "structured-fusion:"
            + hashlib.sha256(
                json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        return {
            "project_id": source.scope.project_id,
            "visibility": source.scope.visibility,
            "config_revision": source.scope.config_revision,
            "job_kind": FUSION_JOB_KIND,
            "provider_identity": self.fusion_identity,
            "subject_id": source.source_id,
            "subject_hash": source.content_hash,
            "dedupe_key": dedupe_key,
            "payload": {
                "schema": FUSION_SCHEMA_VERSION,
                "source_id": source.source_id,
                "memory_type": source.memory_type,
            },
            "max_active_jobs": self._max_queue_size,
        }

    def _load_sources(self, leases: tuple[DerivedWorkLease, ...]) -> tuple[FusionSource, ...]:
        conn = getattr(getattr(self._engine, "_sqlite", None), "_conn", None)
        if conn is None:
            raise FusionSourceStaleError("fusion_canonical_database_unavailable")
        ids = [lease.job.subject_id for lease in leases]
        placeholders = ",".join("?" for _ in ids)
        lock = getattr(self._engine, "_write_lock", threading.RLock())
        with lock:
            if conn.in_transaction:
                raise FusionSourceStaleError("fusion_canonical_transaction_open")
            rows = conn.execute(
                "SELECT id, content, project_id, visibility, memory_type "
                f"FROM memories WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        by_id = {str(row[0]): row for row in rows}
        sources: list[FusionSource] = []
        for lease in leases:
            job = lease.job
            row = by_id.get(job.subject_id)
            if row is None:
                raise FusionSourceStaleError("fusion_source_missing")
            source_id, content, project_id, visibility, memory_type = (
                str(value or "") for value in row
            )
            if (
                project_id != job.project_id
                or (visibility or "project") != job.visibility
                or str(job.payload.get("schema") or "") != FUSION_SCHEMA_VERSION
                or str(job.payload.get("source_id") or "") != source_id
                or memory_type.strip().casefold() == "synthesis"
                or sha256_text(content) != job.subject_hash
            ):
                raise FusionSourceStaleError("fusion_source_binding_changed")
            sources.append(
                FusionSource.create(
                    source_id=source_id,
                    content=content,
                    project_id=project_id,
                    visibility=visibility or "project",
                    config_revision=job.config_revision,
                    memory_type=memory_type,
                )
            )
        return tuple(sources)

    def _commit_result(
        self,
        leases: tuple[DerivedWorkLease, ...],
        result: FusionBatchResult,
    ) -> None:
        conn = getattr(getattr(self._engine, "_sqlite", None), "_conn", None)
        if conn is None:
            raise RuntimeError("fusion_canonical_database_unavailable")
        lock = getattr(self._engine, "_write_lock", threading.RLock())
        with lock:
            if conn.in_transaction:
                raise RuntimeError("fusion_canonical_transaction_open")
            conn.execute("BEGIN IMMEDIATE")
            try:
                sink_report = (
                    self._sink.persist(result, connection=conn)
                    if self._sink is not None
                    else {
                        "created": 0,
                        "shadowed": len(result.chunks),
                        "skipped": 0,
                        "failed": 0,
                    }
                )
                chunk_ids = [chunk.chunk_id for chunk in result.chunks]
                for lease in leases:
                    self._store.complete_in_transaction(
                        conn,
                        job_id=lease.job.job_id,
                        project_id=lease.job.project_id,
                        lease_token=lease.lease_token,
                        fencing_generation=lease.job.fencing_generation,
                        result={
                            "schema": FUSION_SCHEMA_VERSION,
                            "batch_id": result.batch_id,
                            "chunk_ids": chunk_ids,
                            "sink": sink_report,
                            "source_id": lease.job.subject_id,
                        },
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            refresh = getattr(self._engine, "_refresh_canonical_cache_if_changed", None)
            if callable(refresh):
                refresh(force=True)

    def _fail_leases(
        self,
        leases: tuple[DerivedWorkLease, ...],
        error: BaseException,
    ) -> None:
        retryable = not isinstance(error, FusionSourceStaleError)
        failure_code = (
            "source_binding_changed"
            if isinstance(error, FusionSourceStaleError)
            else "fusion_processing_failed"
        )
        for lease in leases:
            try:
                self._store.fail(
                    job_id=lease.job.job_id,
                    project_id=lease.job.project_id,
                    lease_token=lease.lease_token,
                    fencing_generation=lease.job.fencing_generation,
                    failure_code=failure_code,
                    retryable=retryable,
                    retry_delay_seconds=self._retry_delay_seconds,
                )
            except Exception:
                logging.debug(
                    "Structured fusion lease failure could not be recorded",
                    exc_info=True,
                )

    def _worker_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    if self.run_once():
                        continue
                except Exception:
                    logging.exception("Durable structured fusion worker failed")
                self._wake.wait(self._poll_seconds)
                self._wake.clear()
        finally:
            close_fusion = False
            with self._lifecycle_lock:
                self._active_workers -= 1
                if self._close_requested and self._active_workers == 0 and not self._fusion_closed:
                    self._fusion_closed = True
                    close_fusion = True
            if close_fusion:
                try:
                    self._fusion.close()
                except Exception:
                    logging.exception("Durable structured fusion provider close failed")


_RUNTIME_BATCHERS: dict[int, ProjectScopedFusionBatcher] = {}
_RUNTIME_BATCHERS_LOCK = threading.RLock()
_DURABLE_RUNTIMES: dict[int, DurableFusionWorker] = {}
_DURABLE_RUNTIME_FAILURES: set[int] = set()


def structured_fusion_mode() -> str:
    mode = os.environ.get(FUSION_MODE_ENV, "off").strip().casefold()
    return mode if mode in {"off", "shadow", "on"} else "off"


def _create_structured_fusion() -> StructuredMemoryFusion:
    from plastic_promise.core.inference_provider import OpenAICompatibleJSONProvider

    provider = OpenAICompatibleJSONProvider(
        api_key=_first_env(
            "PP_STRUCTURED_MEMORY_FUSION_API_KEY", "PP_MEMORY_CHUNK_ENRICHMENT_API_KEY"
        ),
        base_url=_first_env(
            "PP_STRUCTURED_MEMORY_FUSION_BASE_URL",
            "PP_MEMORY_CHUNK_ENRICHMENT_BASE_URL",
            "PP_INFERENCE_BASE_URL",
        ),
        model=_first_env(
            "PP_STRUCTURED_MEMORY_FUSION_MODEL",
            "PP_MEMORY_CHUNK_ENRICHMENT_MODEL",
            "PP_INFERENCE_MODEL",
        ),
        model_revision=_first_env(
            "PP_STRUCTURED_MEMORY_FUSION_MODEL_REVISION",
            "PP_MEMORY_CHUNK_ENRICHMENT_MODEL_REVISION",
            "PP_INFERENCE_MODEL_REVISION",
        ),
        path=_first_env(
            "PP_STRUCTURED_MEMORY_FUSION_PATH",
            "PP_MEMORY_CHUNK_ENRICHMENT_PATH",
            "PP_INFERENCE_PATH",
        ),
        temperature=_optional_float_env(
            "PP_STRUCTURED_MEMORY_FUSION_TEMPERATURE",
            "PP_MEMORY_CHUNK_ENRICHMENT_TEMPERATURE",
        ),
        top_p=_optional_float_env(
            "PP_STRUCTURED_MEMORY_FUSION_TOP_P",
            "PP_MEMORY_CHUNK_ENRICHMENT_TOP_P",
        ),
        json_mode=True,
        max_output_chars=_optional_int_env(
            "PP_STRUCTURED_MEMORY_FUSION_MAX_OUTPUT_CHARS",
            "PP_MEMORY_CHUNK_ENRICHMENT_MAX_OUTPUT_CHARS",
        ),
    )
    return StructuredMemoryFusion(
        provider,
        max_tokens=_env_positive_int(
            "PP_STRUCTURED_MEMORY_FUSION_MAX_TOKENS",
            _env_positive_int("PP_MEMORY_CHUNK_ENRICHMENT_NUM_PREDICT", 2_048),
        ),
    )


def initialize_durable_fusion_runtime(
    engine: object,
    *,
    fusion: StructuredMemoryFusion | None = None,
    autostart: bool = True,
) -> DurableFusionWorker | None:
    """Create the durable worker only for an explicitly enabled, governed mode."""

    mode = structured_fusion_mode()
    synthesis_mode = os.environ.get("PP_SYNTHESIS_ARTIFACTS", "off").strip().casefold()
    if mode == "off" or synthesis_mode not in {"shadow", "on"}:
        return None
    engine_id = id(engine)
    with _RUNTIME_BATCHERS_LOCK:
        existing = _DURABLE_RUNTIMES.get(engine_id)
        if existing is not None:
            return existing
        if engine_id in _DURABLE_RUNTIME_FAILURES:
            return None
        storage = getattr(engine, "_sqlite", None)
        conn = getattr(storage, "_conn", None)
        if not isinstance(conn, sqlite3.Connection) or conn.in_transaction:
            return None
        try:
            db_path = next(
                (
                    str(row[2] or "").strip()
                    for row in conn.execute("PRAGMA database_list").fetchall()
                    if len(row) >= 3 and str(row[1]) == "main"
                ),
                "",
            )
        except sqlite3.Error:
            return None
        if not db_path or db_path == ":memory:":
            return None
        selected_fusion = fusion
        if selected_fusion is None:
            try:
                selected_fusion = _create_structured_fusion()
            except Exception as exc:
                _DURABLE_RUNTIME_FAILURES.add(engine_id)
                logging.warning(
                    "Durable structured fusion provider unavailable: %s",
                    exc.__class__.__name__,
                )
                return None
        try:
            lease_seconds = min(
                15 * 60,
                _env_positive_int(
                    "PP_STRUCTURED_MEMORY_FUSION_LEASE_SECONDS", DEFAULT_LEASE_SECONDS
                ),
            )
            runtime = DurableFusionWorker(
                engine,
                DerivedWorkStore(db_path, default_lease_seconds=lease_seconds),
                selected_fusion,
                mode=mode,
                batch_size=_env_positive_int(
                    "PP_STRUCTURED_MEMORY_FUSION_BATCH_SIZE", DEFAULT_BATCH_SIZE
                ),
                max_wait_seconds=_env_positive_float(
                    "PP_STRUCTURED_MEMORY_FUSION_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS
                ),
                max_queue_size=_env_positive_int(
                    "PP_STRUCTURED_MEMORY_FUSION_MAX_QUEUE", DEFAULT_MAX_QUEUE_SIZE
                ),
                max_workers=_env_positive_int(
                    "PP_STRUCTURED_MEMORY_FUSION_WORKERS", DEFAULT_MAX_WORKERS
                ),
                lease_seconds=lease_seconds,
                retry_delay_seconds=_env_positive_int(
                    "PP_STRUCTURED_MEMORY_FUSION_RETRY_DELAY_SECONDS",
                    DEFAULT_RETRY_DELAY_SECONDS,
                ),
                poll_seconds=_env_positive_float(
                    "PP_STRUCTURED_MEMORY_FUSION_POLL_SECONDS",
                    DEFAULT_WORKER_POLL_SECONDS,
                ),
                sink=GovernedSynthesisDraftSink(engine),
                autostart=autostart,
            )
        except Exception as exc:
            try:
                selected_fusion.close()
            except Exception:
                logging.debug("Structured fusion cleanup failed", exc_info=True)
            logging.warning(
                "Durable structured fusion store unavailable: %s",
                exc.__class__.__name__,
            )
            return None
        _DURABLE_RUNTIMES[engine_id] = runtime
        return runtime


def get_durable_fusion_runtime(engine: object) -> DurableFusionWorker | None:
    with _RUNTIME_BATCHERS_LOCK:
        cached = _DURABLE_RUNTIMES.get(id(engine))
    if cached is not None:
        return cached
    return initialize_durable_fusion_runtime(engine)


def get_structured_fusion_batcher(engine: object) -> ProjectScopedFusionBatcher | None:
    """Return the process-local canonical fusion batcher when explicitly enabled.

    Enabling fusion without the governed-synthesis gate is rejected by omission:
    no provider request is made and no derived material is created.  This keeps
    feature rollout fail-closed.
    """

    mode = structured_fusion_mode()
    synthesis_mode = os.environ.get("PP_SYNTHESIS_ARTIFACTS", "off").strip().casefold()
    if mode == "off" or synthesis_mode not in {"shadow", "on"}:
        return None
    engine_id = id(engine)
    with _RUNTIME_BATCHERS_LOCK:
        if engine_id in _DURABLE_RUNTIMES:
            return None
        existing = _RUNTIME_BATCHERS.get(engine_id)
        if existing is not None:
            return existing
        try:
            fusion = _create_structured_fusion()
        except Exception as exc:
            logging.debug("Structured fusion batcher unavailable: %s", exc.__class__.__name__)
            return None
        batcher = ProjectScopedFusionBatcher(
            fusion,
            batch_size=_env_positive_int(
                "PP_STRUCTURED_MEMORY_FUSION_BATCH_SIZE", DEFAULT_BATCH_SIZE
            ),
            max_wait_seconds=_env_positive_float(
                "PP_STRUCTURED_MEMORY_FUSION_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS
            ),
            max_queue_size=_env_positive_int(
                "PP_STRUCTURED_MEMORY_FUSION_MAX_QUEUE", DEFAULT_MAX_QUEUE_SIZE
            ),
            max_workers=_env_positive_int(
                "PP_STRUCTURED_MEMORY_FUSION_WORKERS", DEFAULT_MAX_WORKERS
            ),
            on_result=GovernedSynthesisDraftSink(engine),
        )
        _RUNTIME_BATCHERS[engine_id] = batcher
        return batcher


def enqueue_canonical_memory_for_fusion(
    engine: object, memory_id: str
) -> Future[FusionBatchResult] | DerivedWorkCreateResult | None:
    """Queue one already-canonical ordinary memory for governed fusion.

    This function is safe to call from both the formal memory pipeline and the
    auto-promotion path.  It performs no work unless both fusion and governed
    synthesis are explicitly enabled.
    """

    runtime = get_durable_fusion_runtime(engine)
    batcher = None if runtime is not None else get_structured_fusion_batcher(engine)
    if runtime is None and batcher is None:
        return None
    conn = getattr(getattr(engine, "_sqlite", None), "_conn", None)
    if conn is None:
        return None
    lock = getattr(engine, "_write_lock", threading.RLock())
    with lock:
        row = conn.execute(
            "SELECT id, content, project_id, visibility, memory_type FROM memories WHERE id = ?",
            (str(memory_id or ""),),
        ).fetchone()
    if row is None:
        return None
    source_id, content, project_id, visibility, memory_type = (str(value or "") for value in row)
    if not content.strip() or memory_type.casefold() == "synthesis":
        return None
    fusion_identity = runtime.fusion_identity if runtime is not None else batcher.fusion_identity
    config_revision = os.environ.get(FUSION_CONFIG_REVISION_ENV, "").strip()
    source = FusionSource.create(
        source_id=source_id,
        content=content,
        project_id=project_id,
        visibility=visibility or "project",
        config_revision=config_revision or fusion_identity,
        memory_type=memory_type,
    )
    if runtime is not None:
        return runtime.enqueue_source(source)
    return batcher.submit(source)


def enqueue_canonical_memory_for_fusion_in_transaction(
    engine: object,
    connection: sqlite3.Connection,
    canonical: Mapping[str, object],
) -> DerivedWorkCreateResult | None:
    """Write the fusion receipt beside a newly created canonical memory row."""

    with _RUNTIME_BATCHERS_LOCK:
        runtime = _DURABLE_RUNTIMES.get(id(engine))
    if not isinstance(runtime, DurableFusionWorker):
        return None
    memory_type = str(canonical.get("memory_type") or "")
    content = str(canonical.get("content") or "")
    if not content.strip() or memory_type.strip().casefold() == "synthesis":
        return None
    config_revision = os.environ.get(FUSION_CONFIG_REVISION_ENV, "").strip()
    source = FusionSource.create(
        source_id=str(canonical.get("id") or ""),
        content=content,
        project_id=str(canonical.get("project_id") or ""),
        visibility=str(canonical.get("visibility") or "project"),
        config_revision=config_revision or runtime.fusion_identity,
        memory_type=memory_type,
    )
    return runtime.enqueue_source_in_transaction(connection, source)


def close_structured_fusion_batcher(engine: object, *, timeout: float = 5.0) -> bool:
    """Close durable and compatibility batchers during controlled shutdown."""

    with _RUNTIME_BATCHERS_LOCK:
        batcher = _RUNTIME_BATCHERS.pop(id(engine), None)
        runtime = _DURABLE_RUNTIMES.pop(id(engine), None)
        _DURABLE_RUNTIME_FAILURES.discard(id(engine))
    closed = False
    if runtime is not None:
        runtime.close(timeout=timeout)
        closed = True
    if batcher is not None:
        batcher.close(wait=True, timeout=timeout)
        closed = True
    return closed


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_sources(sources: tuple[FusionSource, ...]) -> FusionScope:
    if not sources:
        raise ValueError("fusion_sources_required")
    scope = sources[0].scope
    seen_ids: set[str] = set()
    for source in sources:
        if source.scope != scope:
            raise ValueError("fusion_scope_mismatch")
        if source.source_id in seen_ids:
            raise ValueError("fusion_source_duplicate")
        seen_ids.add(source.source_id)
    return scope


def _batch_id(scope: FusionScope, identity: str, sources: tuple[FusionSource, ...]) -> str:
    material = {
        "schema": FUSION_SCHEMA_VERSION,
        "project_id": scope.project_id,
        "visibility": scope.visibility,
        "config_revision": scope.config_revision,
        "fusion_identity": identity,
        "sources": [(source.source_id, source.content_hash) for source in sources],
    }
    return (
        "fusion:"
        + hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:32]
    )


def _validate_output(
    raw: object,
    *,
    batch_id: str,
    scope: FusionScope,
    sources: tuple[FusionSource, ...],
    max_output_chunks: int,
    max_chunk_chars: int,
    max_evidence_per_chunk: int,
) -> tuple[FusedChunk, ...]:
    if not isinstance(raw, Mapping) or set(raw) != {"chunks"}:
        raise FusionValidationError("fusion_output_schema_invalid")
    raw_chunks = raw.get("chunks")
    if not isinstance(raw_chunks, list) or len(raw_chunks) > max_output_chunks:
        raise FusionValidationError("fusion_output_chunks_invalid")
    source_by_id = {source.source_id: source for source in sources}
    chunks: list[FusedChunk] = []
    seen_chunks: set[str] = set()
    for position, raw_chunk in enumerate(raw_chunks):
        if not isinstance(raw_chunk, Mapping) or set(raw_chunk) != {
            "text",
            "source_ids",
            "evidence",
        }:
            raise FusionValidationError("fusion_output_chunk_schema_invalid")
        text = raw_chunk.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > max_chunk_chars:
            raise FusionValidationError("fusion_output_chunk_text_invalid")
        source_ids = raw_chunk.get("source_ids")
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or len(source_ids) != len(set(source_ids))
            or not all(
                isinstance(source_id, str) and source_id in source_by_id for source_id in source_ids
            )
        ):
            raise FusionValidationError("fusion_output_source_ids_invalid")
        evidence_rows = raw_chunk.get("evidence")
        if (
            not isinstance(evidence_rows, list)
            or not evidence_rows
            or len(evidence_rows) > max_evidence_per_chunk
        ):
            raise FusionValidationError("fusion_output_evidence_invalid")
        evidence: list[FusionEvidence] = []
        evidence_source_ids: set[str] = set()
        for raw_evidence in evidence_rows:
            if not isinstance(raw_evidence, Mapping) or set(raw_evidence) != {
                "source_id",
                "start",
                "end",
            }:
                raise FusionValidationError("fusion_output_evidence_schema_invalid")
            source_id = raw_evidence.get("source_id")
            start = raw_evidence.get("start")
            end = raw_evidence.get("end")
            source = source_by_id.get(source_id) if isinstance(source_id, str) else None
            if (
                source is None
                or not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or end > len(source.content)
            ):
                raise FusionValidationError("fusion_output_evidence_span_invalid")
            evidence.append(
                FusionEvidence(
                    source_id=source.source_id,
                    content_hash=source.content_hash,
                    start=start,
                    end=end,
                    text=source.content[start:end],
                )
            )
            evidence_source_ids.add(source.source_id)
        if evidence_source_ids != set(source_ids):
            raise FusionValidationError("fusion_output_evidence_source_mismatch")
        chunk_material = {
            "batch_id": batch_id,
            "position": position,
            "text": text.strip(),
            "source_ids": source_ids,
            "evidence": [
                (item.source_id, item.content_hash, item.start, item.end) for item in evidence
            ],
        }
        chunk_id = (
            "fused:"
            + hashlib.sha256(
                json.dumps(
                    chunk_material,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:32]
        )
        if chunk_id in seen_chunks:
            raise FusionValidationError("fusion_output_chunk_duplicate")
        seen_chunks.add(chunk_id)
        chunks.append(
            FusedChunk(
                chunk_id=chunk_id,
                batch_id=batch_id,
                project_id=scope.project_id,
                visibility=scope.visibility,
                text=text.strip(),
                source_ids=tuple(source_ids),
                evidence=tuple(evidence),
            )
        )
    return tuple(chunks)


def _bounded_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def _bounded_positive_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.001, parsed)


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _optional_float_env(*names: str) -> float | None:
    value = _first_env(*names)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _optional_int_env(*names: str) -> int | None:
    value = _first_env(*names)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _env_positive_int(name: str, default: int) -> int:
    return _bounded_positive_int(os.environ.get(name, default), default)


def _env_positive_float(name: str, default: float) -> float:
    return _bounded_positive_float(os.environ.get(name, default), default)
