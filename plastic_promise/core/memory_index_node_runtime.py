"""Server-owned remote embedding execution for ordinary index outbox jobs.

``store_outbox`` remains the canonical invalidation event.  This module only
adds a durable, fenced node execution beneath that event: a completed node
vector is retained in ``derived_work_jobs`` so an outbox replay that crashes
after inference can safely reproject it into LanceDB without asking a node to
repeat work.  Neither this module nor its node adapter writes canonical memory.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from plastic_promise.control_plane.config_schema import routing_for_project
from plastic_promise.core.derived_work import DerivedWorkStore
from plastic_promise.core.memory_index import read_persisted_index_material
from plastic_promise.core.node_governance import (
    NodeExecutionFailure,
    NodeExecutionResult,
    NodeGovernanceError,
    NodeInferenceWorkCoordinator,
    NodeTaskRequest,
    NodeWorkLease,
    ResolvedNodeTask,
    open_server_node_governance,
)
from plastic_promise.core.node_task_authority import (
    ActiveControlConfig,
    open_server_memory_index_node_task_authority,
)
from plastic_promise.core.paths import get_db_path
from plastic_promise.core.server_embedder import Embedder
from plastic_promise.core.structured_intent import structured_intent_digest

if TYPE_CHECKING:
    from plastic_promise.core.node_private_transport import PrivateNodeTransportProbe

_OUTBOX_ID_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,127}\Z")
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_MATERIAL_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class MemoryIndexNodeRuntimeError(RuntimeError):
    """A stable, non-sensitive failure from the derived node path."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_.:-]{1,127}", code) is None:
            code = "node_index_runtime_failed"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NodeIndexEmbedding:
    """A checked vector plus the complete identity used to produce it."""

    vector: tuple[float, ...]
    identity: str
    model: str
    revision: str
    dimension: int
    normalization: str
    receipt_reference: str = ""


@dataclass(frozen=True)
class NodeRerankOutcome:
    """Foreground rerank scores plus the safe routing explanation."""

    scores: dict[int, float]
    node_id: str | None
    selection_reason: str
    degradation_reason: str = ""
    receipt_reference: str = ""


@dataclass(frozen=True)
class NodeStructuredJSONOutcome:
    """One bounded structured object plus its governed route evidence."""

    output: dict[str, object]
    node_id: str | None
    selection_reason: str
    degradation_reason: str = ""
    receipt_reference: str = ""


class GovernedRetrievalEmbedder(Embedder):
    """Embedder adapter backed only by the active governed node route.

    LanceDB still expects an ``Embedder`` for generation identity and optional
    repair work.  This adapter keeps that interface small while ensuring those
    calls cannot rediscover an unregistered local or cloud provider.  The
    first fixed probe captures the complete node identity; every later result
    must match it.
    """

    _IDENTITY_PROBE = "plastic promise governed retrieval identity probe"

    def __init__(
        self,
        runtime: MemoryIndexNodeRuntime,
        *,
        default_project_id: str = "project:legacy-global",
    ) -> None:
        if not callable(getattr(runtime, "embedding_for_retrieval", None)):
            raise MemoryIndexNodeRuntimeError("node_retrieval_runtime_invalid")
        self._runtime = runtime
        self._default_project_id = _project_id(default_project_id)
        self._identity_cache: NodeIndexEmbedding | None = None
        self._stats_lock = threading.Lock()
        self._embedding_requests = 0
        self._embedding_input_tokens = 0

    def embed(self, text: str) -> list[float]:
        return self.embed_for_project(text, project_id=self._default_project_id)

    def embed_for_project(self, text: str, *, project_id: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise MemoryIndexNodeRuntimeError("node_retrieval_embedding_input_invalid")
        embedding = self._runtime.embedding_for_retrieval(
            text=text,
            project_id=project_id,
        )
        cached = self._identity_cache
        if cached is not None and _embedding_identity_tuple(cached) != _embedding_identity_tuple(
            embedding
        ):
            raise MemoryIndexNodeRuntimeError("node_retrieval_embedding_identity_drift")
        self._identity_cache = embedding
        with self._stats_lock:
            self._embedding_requests += 1
            self._embedding_input_tokens += max(1, (len(text.encode("utf-8")) + 3) // 4)
        return list(embedding.vector)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list):
            raise MemoryIndexNodeRuntimeError("node_retrieval_embedding_input_invalid")
        return [self.embed(text) for text in texts]

    @property
    def dim(self) -> int:
        return self._identity().dimension

    @property
    def model_name(self) -> str:
        return self._identity().model

    @property
    def index_model_name(self) -> str:
        return self._identity().identity

    @property
    def normalization(self) -> str:
        return self._identity().normalization

    @property
    def _model_revision(self) -> str:
        return self._identity().revision

    @property
    def stats(self) -> dict[str, object]:
        """Expose bounded local accounting for governed embedding evidence."""

        identity = self._identity()
        with self._stats_lock:
            return {
                "provider": "governed-node",
                "revision": identity.revision,
                "embedding_requests": self._embedding_requests,
                "embedding_input_tokens": self._embedding_input_tokens,
                "cost": 0.0,
                "cost_currency": "USD",
                "pricing_revision": "governed-node-local-v1",
            }

    def _identity(self) -> NodeIndexEmbedding:
        if self._identity_cache is None:
            self.embed(self._IDENTITY_PROBE)
        assert self._identity_cache is not None
        return self._identity_cache


class MemoryIndexNodeRuntime:
    """Deep module that replays one canonical index outbox through a node.

    Its one public operation accepts an opaque canonical outbox ID.  Project
    ownership, index material, active controlled revision, route policy and
    node selection are all independently revalidated by its coordinator and
    authority.  The caller receives a checked vector only after the current
    LanceDB generation has proven it belongs to the same full identity.
    """

    def __init__(
        self,
        *,
        coordinator: NodeInferenceWorkCoordinator,
        derived_work: DerivedWorkStore,
        transport: PrivateNodeTransportProbe,
        canonical_db_path: str | Path,
        control_config: ActiveControlConfig | None = None,
    ) -> None:
        if not isinstance(coordinator, NodeInferenceWorkCoordinator):
            raise MemoryIndexNodeRuntimeError("node_index_coordinator_invalid")
        if not isinstance(derived_work, DerivedWorkStore):
            raise MemoryIndexNodeRuntimeError("node_index_derived_work_invalid")
        if not callable(getattr(transport, "execute_embedding", None)):
            raise MemoryIndexNodeRuntimeError("node_index_transport_invalid")
        path = Path(canonical_db_path).expanduser()
        if not path.is_file():
            raise MemoryIndexNodeRuntimeError("node_index_canonical_unavailable")
        self._coordinator = coordinator
        self._derived_work = derived_work
        self._transport = transport
        self._db_path = path.resolve()
        self._control_config = control_config
        self._usage_lock = threading.Lock()
        self._embedding_requests = 0
        self._embedding_input_tokens = 0

    @property
    def embedding_usage(self) -> dict[str, object]:
        """Return bounded accounting for the same governed retrieval route."""

        with self._usage_lock:
            return {
                "embedding_requests": self._embedding_requests,
                "embedding_input_tokens": self._embedding_input_tokens,
                "cost": 0.0,
                "cost_currency": "USD",
                "cost_usd": 0.0,
                "pricing_revision": "governed-node-local-v1",
            }

    def embedding_for_outbox(
        self,
        *,
        engine: Any,
        outbox_id: str,
        project_id: str,
    ) -> NodeIndexEmbedding:
        """Return a durable remote vector for exactly one canonical outbox job."""

        normalized_outbox = _outbox_id(outbox_id)
        normalized_project = _project_id(project_id)
        request = NodeTaskRequest(
            project_id=normalized_project,
            idempotency_key=f"memory-index:{normalized_outbox}",
            operation="embedding",
            input_reference=f"outbox:{normalized_outbox}",
        )
        try:
            created = self._coordinator.enqueue(request)
            job = created.job
            if job.status != "completed":
                if job.status == "retry_wait":
                    raise MemoryIndexNodeRuntimeError("node_index_embedding_retry_wait")
                if job.status != "pending":
                    raise MemoryIndexNodeRuntimeError("node_index_embedding_not_runnable")
                outcome = self._coordinator.run_job(
                    job_id=job.job_id,
                    project_id=normalized_project,
                    executor=self,
                )
                if outcome.outcome != "completed":
                    raise MemoryIndexNodeRuntimeError(
                        outcome.failure_code or "node_index_embedding_deferred"
                    )
            completed = self._derived_work.get(
                job_id=job.job_id,
                project_id=normalized_project,
            )
            embedding = _completed_embedding(completed.result)
            _assert_generation_identity(engine, embedding)
            return embedding
        except MemoryIndexNodeRuntimeError:
            raise
        except NodeGovernanceError as exc:
            raise MemoryIndexNodeRuntimeError(exc.code) from exc
        except Exception as exc:
            raise MemoryIndexNodeRuntimeError("node_index_embedding_failed") from exc

    def execute(self, lease: NodeWorkLease) -> NodeExecutionResult:
        """Load only current canonical material, then delegate private inference."""

        try:
            material = self._load_current_material(lease)
            return self._transport.execute_embedding(lease, input_text=material)
        except NodeExecutionFailure:
            raise
        except MemoryIndexNodeRuntimeError as exc:
            raise NodeExecutionFailure(exc.code) from exc
        except Exception as exc:
            raise NodeExecutionFailure("node_index_embedding_failed") from exc

    def reconcile(self, *, project_id: str | None = None) -> dict[str, int]:
        """Release only expired derived leases/reservations; canonical rows stay intact."""

        result = self._coordinator.reconcile(project_id=project_id)
        return {str(name): int(count) for name, count in result.items()}

    def embedding_for_retrieval(
        self,
        *,
        text: str,
        project_id: str,
    ) -> NodeIndexEmbedding:
        """Route one non-persistent retrieval embedding through governed nodes.

        Query text is held only for the duration of the private request.  The
        active control revision supplies identity, node allowlist, scheduling
        policy and optional pinning; callers cannot override those decisions.
        """

        if self._control_config is None:
            raise MemoryIndexNodeRuntimeError("node_retrieval_control_unavailable")
        if not isinstance(text, str) or not text.strip():
            raise MemoryIndexNodeRuntimeError("node_retrieval_embedding_input_invalid")
        normalized_project = _project_id(project_id)
        try:
            snapshot = self._control_config.safe_config()
            config = getattr(snapshot, "config", None)
            revision = getattr(snapshot, "revision_id", None)
            routing = (
                routing_for_project(config, normalized_project)
                if isinstance(config, Mapping)
                else None
            )
            if (
                not isinstance(revision, str)
                or not isinstance(routing, Mapping)
                or routing.get("enabled") is not True
            ):
                raise MemoryIndexNodeRuntimeError("node_retrieval_routing_unavailable")
            required_identity = routing.get("embedding_required_identity")
            policy = routing.get("embedding_policy")
            inference_mode = routing.get("inference_mode", "hybrid")
            allowed = routing.get("allowed_node_ids")
            pinned = routing.get("embedding_pinned_node_id") or None
            if (
                not isinstance(required_identity, str)
                or not isinstance(policy, str)
                or inference_mode not in {"local", "cloud", "hybrid"}
                or not isinstance(allowed, list)
                or not all(isinstance(value, str) for value in allowed)
            ):
                raise MemoryIndexNodeRuntimeError("node_retrieval_routing_invalid")
            fingerprint = hashlib.sha256(
                (normalized_project + "\x1f" + text).encode("utf-8")
            ).hexdigest()
            from plastic_promise.core.node_governance import ResolvedNodeTask

            route = ResolvedNodeTask(
                project_id=normalized_project,
                operation="embedding",
                input_reference=f"retrieval:{fingerprint[:48]}",
                subject_hash="sha256:" + fingerprint,
                visibility="project",
                config_revision=revision,
                required_identity=required_identity,
                scheduling_policy=policy,
                inference_mode=inference_mode,
                pinned_node_id=pinned if isinstance(pinned, str) else None,
                allowed_node_ids=tuple(allowed),
                profile_digest=self._profile_digest(revision),
            )
            result, _node_id, selection, receipt_reference = self._coordinator.execute_foreground(
                resolved=route,
                request_fingerprint=fingerprint,
                executor=_ForegroundEmbeddingExecutor(self._transport, text),
            )
            if result is None:
                raise MemoryIndexNodeRuntimeError(
                    selection or "node_retrieval_embedding_unavailable"
                )
            embedding = _embedding_result(
                result.result,
                expected_identity=required_identity,
                receipt_reference=receipt_reference,
            )
            with self._usage_lock:
                self._embedding_requests += 1
                self._embedding_input_tokens += max(1, (len(text.encode("utf-8")) + 3) // 4)
            return embedding
        except MemoryIndexNodeRuntimeError:
            raise
        except NodeExecutionFailure as exc:
            raise MemoryIndexNodeRuntimeError(exc.code) from exc
        except NodeGovernanceError as exc:
            raise MemoryIndexNodeRuntimeError(exc.code) from exc
        except Exception as exc:
            raise MemoryIndexNodeRuntimeError("node_retrieval_embedding_failed") from exc

    def rerank_for_context(
        self,
        *,
        project_id: str,
        query: str,
        documents: list[str],
    ) -> NodeRerankOutcome:
        """Route a live retrieval rerank through the verified node registry.

        The foreground input is not copied into derived-work or canonical
        SQLite.  The route itself is rebuilt from the active controlled
        revision for every call, then a short-lived reservation protects node
        capacity while the private transport rechecks the full rerank identity.
        """

        if self._control_config is None:
            raise MemoryIndexNodeRuntimeError("node_rerank_control_unavailable")
        if not isinstance(query, str) or not query.strip() or not isinstance(documents, list):
            raise MemoryIndexNodeRuntimeError("node_rerank_input_invalid")
        if len(documents) < 2:
            raise MemoryIndexNodeRuntimeError("node_rerank_input_invalid")
        try:
            snapshot = self._control_config.safe_config()
            config = getattr(snapshot, "config", None)
            revision = getattr(snapshot, "revision_id", None)
            routing = (
                routing_for_project(config, project_id) if isinstance(config, Mapping) else None
            )
            if (
                not isinstance(revision, str)
                or not isinstance(routing, Mapping)
                or routing.get("enabled") is not True
            ):
                raise MemoryIndexNodeRuntimeError("node_rerank_routing_unavailable")
            required_identity = routing.get("rerank_required_identity")
            policy = routing.get("rerank_policy")
            inference_mode = routing.get("inference_mode", "hybrid")
            allowed = routing.get("allowed_node_ids")
            pinned = routing.get("rerank_pinned_node_id") or None
            if (
                not isinstance(required_identity, str)
                or not isinstance(policy, str)
                or inference_mode not in {"local", "cloud", "hybrid"}
                or not isinstance(allowed, list)
                or not all(isinstance(value, str) for value in allowed)
            ):
                raise MemoryIndexNodeRuntimeError("node_rerank_routing_invalid")
            fingerprint = hashlib.sha256(
                (project_id + "\x1f" + query + "\x1f" + "\x1e".join(documents)).encode("utf-8")
            ).hexdigest()
            resolved = NodeTaskRequest(
                project_id=project_id,
                idempotency_key=f"rerank:{fingerprint[:48]}",
                operation="rerank",
                input_reference=f"rerank:{fingerprint[:48]}",
            )
            from plastic_promise.core.node_governance import ResolvedNodeTask

            route = ResolvedNodeTask(
                project_id=resolved.project_id,
                operation="rerank",
                input_reference=resolved.input_reference,
                subject_hash="sha256:" + fingerprint,
                visibility="project",
                config_revision=revision,
                required_identity=required_identity,
                scheduling_policy=policy,
                inference_mode=inference_mode,
                pinned_node_id=pinned if isinstance(pinned, str) else None,
                allowed_node_ids=tuple(allowed),
                profile_digest=self._profile_digest(revision),
            )
            result, node_id, selection, receipt_reference = self._coordinator.execute_foreground(
                resolved=route,
                request_fingerprint=fingerprint,
                executor=_ForegroundRerankExecutor(
                    self._transport,
                    query,
                    documents,
                ),
            )
            if result is None:
                return NodeRerankOutcome({}, None, selection, selection, receipt_reference)
            scores = _completed_rerank_scores(result.result, expected_identity=required_identity)
            return NodeRerankOutcome(
                scores,
                node_id,
                selection,
                receipt_reference=receipt_reference,
            )
        except MemoryIndexNodeRuntimeError:
            raise
        except NodeExecutionFailure as exc:
            raise MemoryIndexNodeRuntimeError(exc.code) from exc
        except NodeGovernanceError as exc:
            raise MemoryIndexNodeRuntimeError(exc.code) from exc
        except Exception as exc:
            raise MemoryIndexNodeRuntimeError("node_rerank_failed") from exc

    def structured_json_for_context(
        self,
        *,
        project_id: str,
        intent_id: str,
        schema_id: str,
        user_payload: Mapping[str, object],
        max_tokens: int = 512,
    ) -> NodeStructuredJSONOutcome:
        """Route semantic JSON classification through the registered node path.

        Cloud/local selection is deliberately absent here: the compute node
        owns that provider choice.  The server only resolves the active
        identity, node allowlist and bounded request, then receives a checked
        object or an explicit ``defer`` outcome.
        """

        if self._control_config is None:
            raise MemoryIndexNodeRuntimeError("node_structured_json_control_unavailable")
        if (
            not isinstance(intent_id, str)
            or not intent_id.strip()
            or not isinstance(schema_id, str)
            or not schema_id.strip()
            or not isinstance(user_payload, Mapping)
            or not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or max_tokens < 1
        ):
            raise MemoryIndexNodeRuntimeError("node_structured_json_input_invalid")
        normalized_project = _project_id(project_id)
        try:
            snapshot = self._control_config.safe_config()
            config = getattr(snapshot, "config", None)
            revision = getattr(snapshot, "revision_id", None)
            routing = (
                routing_for_project(config, normalized_project)
                if isinstance(config, Mapping)
                else None
            )
            if (
                not isinstance(revision, str)
                or not isinstance(routing, Mapping)
                or routing.get("enabled") is not True
            ):
                raise MemoryIndexNodeRuntimeError("node_structured_json_routing_unavailable")
            required_identity = routing.get("structured_json_required_identity")
            policy = routing.get("structured_json_policy")
            inference_mode = routing.get("inference_mode", "hybrid")
            allowed = routing.get("allowed_node_ids")
            pinned = routing.get("structured_json_pinned_node_id") or None
            if (
                not isinstance(required_identity, str)
                or not isinstance(policy, str)
                or inference_mode not in {"local", "cloud", "hybrid"}
                or not isinstance(allowed, list)
                or not all(isinstance(value, str) for value in allowed)
            ):
                raise MemoryIndexNodeRuntimeError("node_structured_json_routing_invalid")
            input_digest = structured_intent_digest(
                project_id=normalized_project,
                intent_id=intent_id,
                schema_id=schema_id,
                user_payload=user_payload,
            )
            fingerprint = input_digest.removeprefix("sha256:")
            route = ResolvedNodeTask(
                project_id=normalized_project,
                operation="structured-json",
                input_reference=f"structured-json:{fingerprint[:48]}",
                subject_hash="sha256:" + fingerprint,
                visibility="project",
                config_revision=revision,
                required_identity=required_identity,
                scheduling_policy=policy,
                inference_mode=inference_mode,
                pinned_node_id=pinned if isinstance(pinned, str) else None,
                allowed_node_ids=tuple(allowed),
                profile_digest=self._profile_digest(revision),
            )
            result, node_id, selection, receipt_reference = self._coordinator.execute_foreground(
                resolved=route,
                request_fingerprint=fingerprint,
                executor=_ForegroundStructuredJSONExecutor(
                    self._transport,
                    user_payload,
                    max_tokens,
                    intent_id=intent_id,
                    schema_id=schema_id,
                    input_digest=input_digest,
                ),
            )
            if result is None:
                return NodeStructuredJSONOutcome({}, None, selection, selection, receipt_reference)
            output = _completed_structured_json(
                result.result,
                expected_identity=required_identity,
            )
            return NodeStructuredJSONOutcome(
                output,
                node_id,
                selection,
                receipt_reference=receipt_reference,
            )
        except MemoryIndexNodeRuntimeError:
            raise
        except NodeExecutionFailure as exc:
            raise MemoryIndexNodeRuntimeError(exc.code) from exc
        except NodeGovernanceError as exc:
            raise MemoryIndexNodeRuntimeError(exc.code) from exc
        except (TypeError, ValueError, RecursionError) as exc:
            raise MemoryIndexNodeRuntimeError("node_structured_json_failed") from exc

    def _load_current_material(self, lease: NodeWorkLease) -> str:
        if not isinstance(lease, NodeWorkLease) or lease.resolved.operation != "embedding":
            raise MemoryIndexNodeRuntimeError("node_index_lease_invalid")
        outbox_id = _outbox_reference(lease.resolved.input_reference)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{self._db_path.as_uri()}?mode=ro",
                uri=True,
                timeout=10.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            outbox = connection.execute(
                """
                SELECT project_id, tool_name, status, payload_json
                FROM store_outbox WHERE outbox_id = ?
                """,
                (outbox_id,),
            ).fetchone()
            if outbox is None or outbox["tool_name"] != "memory_index":
                raise MemoryIndexNodeRuntimeError("node_index_outbox_missing")
            if outbox["status"] not in {"pending", "processing"}:
                raise MemoryIndexNodeRuntimeError("node_index_outbox_not_pending")
            payload = _outbox_payload(outbox["payload_json"])
            if (
                payload["project_id"] != lease.resolved.project_id
                or outbox["project_id"] != payload["project_id"]
            ):
                raise MemoryIndexNodeRuntimeError("node_index_project_mismatch")
            if payload["expected_embedding_hash"] != _material_from_subject_hash(
                lease.resolved.subject_hash
            ):
                raise MemoryIndexNodeRuntimeError("node_index_subject_stale")
            memory = connection.execute(
                """
                SELECT project_id, embedding_hash, embedding_text, search_text, metadata_json
                FROM memories WHERE id = ?
                """,
                (payload["memory_id"],),
            ).fetchone()
        except MemoryIndexNodeRuntimeError:
            raise
        except sqlite3.Error as exc:
            raise MemoryIndexNodeRuntimeError("node_index_canonical_schema_missing") from exc
        finally:
            if connection is not None:
                connection.close()

        if memory is None or memory["project_id"] != lease.resolved.project_id:
            raise MemoryIndexNodeRuntimeError("node_index_subject_missing")
        record = dict(memory)
        material = read_persisted_index_material(record)
        if material is None:
            raise MemoryIndexNodeRuntimeError("node_index_subject_stale")
        if material.embedding_hash != _material_from_subject_hash(lease.resolved.subject_hash):
            raise MemoryIndexNodeRuntimeError("node_index_subject_stale")
        vector_text = material.vector_text
        if not isinstance(vector_text, str) or not vector_text.strip():
            raise MemoryIndexNodeRuntimeError("node_index_subject_stale")
        return vector_text

    def _profile_digest(self, revision_id: object) -> str | None:
        """Bind foreground work to the active compute profile when available.

        Older fixture control objects intentionally do not expose a profile
        reader; those remain compatible.  A real control-plane reader is a
        production authority, so failures or malformed digests fail closed.
        """

        reader = getattr(self._control_config, "compute_profile_digest", None)
        if reader is None:
            return None
        if not isinstance(revision_id, str) or not revision_id:
            raise MemoryIndexNodeRuntimeError("node_compute_profile_revision_invalid")
        try:
            digest = reader(revision_id)
        except Exception as exc:
            raise MemoryIndexNodeRuntimeError("node_compute_profile_unavailable") from exc
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise MemoryIndexNodeRuntimeError("node_compute_profile_invalid")
        return digest


class _ForegroundRerankExecutor:
    """Per-request adapter that never persists retrieval source text."""

    def __init__(
        self, transport: PrivateNodeTransportProbe, query: str, documents: list[str]
    ) -> None:
        self._transport = transport
        self._query = query
        self._documents = documents

    def execute(self, lease: NodeWorkLease) -> NodeExecutionResult:
        return self._transport.execute_rerank(
            lease,
            query=self._query,
            documents=self._documents,
        )


class _ForegroundEmbeddingExecutor:
    """Per-request adapter that never persists retrieval query text."""

    def __init__(self, transport: PrivateNodeTransportProbe, text: str) -> None:
        self._transport = transport
        self._text = text

    def execute(self, lease: NodeWorkLease) -> NodeExecutionResult:
        return self._transport.execute_embedding(lease, input_text=self._text)


class _ForegroundStructuredJSONExecutor:
    """Per-request adapter that never persists semantic source payloads."""

    def __init__(
        self,
        transport: PrivateNodeTransportProbe,
        user_payload: Mapping[str, object],
        max_tokens: int,
        *,
        intent_id: str,
        schema_id: str,
        input_digest: str,
    ) -> None:
        self._transport = transport
        self._user_payload = dict(user_payload)
        self._max_tokens = max_tokens
        self._intent_id = intent_id
        self._schema_id = schema_id
        self._input_digest = input_digest

    def execute(self, lease: NodeWorkLease) -> NodeExecutionResult:
        return self._transport.execute_structured_json(
            lease,
            user_payload=self._user_payload,
            max_tokens=self._max_tokens,
            intent_id=self._intent_id,
            schema_id=self._schema_id,
            input_digest=self._input_digest,
        )


def open_server_memory_index_node_runtime(
    control_config: ActiveControlConfig,
    transport: PrivateNodeTransportProbe,
) -> MemoryIndexNodeRuntime:
    """Build the production runtime from server-owned dependencies only.

    The caller supplies the private resolver-backed transport and the active
    control-plane projection; endpoint data never enters a revision or public
    runtime status.  Opening this factory validates only the already-migrated
    canonical SQLite schema and never creates or migrates state.
    """

    path = Path(get_db_path()).expanduser()
    derived_work = DerivedWorkStore(path)
    return MemoryIndexNodeRuntime(
        coordinator=NodeInferenceWorkCoordinator(
            registry=open_server_node_governance(),
            derived_work=derived_work,
            authority=open_server_memory_index_node_task_authority(control_config),
        ),
        derived_work=derived_work,
        transport=transport,
        canonical_db_path=path,
        control_config=control_config,
    )


def install_memory_index_node_runtime(engine: Any, runtime: MemoryIndexNodeRuntime) -> None:
    """Attach a server-created runtime to one engine without exposing a route API."""

    if not isinstance(runtime, MemoryIndexNodeRuntime):
        raise MemoryIndexNodeRuntimeError("node_index_runtime_invalid")
    if engine is None:
        raise MemoryIndexNodeRuntimeError("node_index_engine_invalid")
    installer = getattr(engine, "install_memory_index_node_runtime", None)
    if not callable(installer):
        raise MemoryIndexNodeRuntimeError("node_index_engine_invalid")
    installer(runtime)


def _completed_embedding(value: object) -> NodeIndexEmbedding:
    if not isinstance(value, Mapping) or value.get("outcome") != "completed":
        raise MemoryIndexNodeRuntimeError("node_index_embedding_result_missing")
    result = value.get("result")
    if not isinstance(result, Mapping):
        raise MemoryIndexNodeRuntimeError("node_index_embedding_result_missing")
    return _embedding_result(result)


def _embedding_result(
    result: object,
    *,
    expected_identity: str | None = None,
    receipt_reference: str = "",
) -> NodeIndexEmbedding:
    if not isinstance(result, Mapping):
        raise MemoryIndexNodeRuntimeError("node_index_embedding_result_missing")
    identity = result.get("embedding_identity")
    model = result.get("embedding_model")
    revision = result.get("embedding_revision")
    dimension = result.get("embedding_dimension")
    normalization = result.get("embedding_normalization")
    vector = result.get("embedding")
    if (
        not isinstance(identity, str)
        or _SHA256_RE.fullmatch(identity) is None
        or not isinstance(model, str)
        or not model
        or not isinstance(revision, str)
        or not revision
        or isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or dimension <= 0
        or not isinstance(normalization, str)
        or not normalization
        or not isinstance(vector, list)
        or len(vector) != dimension
    ):
        raise MemoryIndexNodeRuntimeError("node_index_embedding_result_invalid")
    normalized: list[float] = []
    for component in vector:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise MemoryIndexNodeRuntimeError("node_index_embedding_result_invalid")
        numeric = float(component)
        if not math.isfinite(numeric):
            raise MemoryIndexNodeRuntimeError("node_index_embedding_result_invalid")
        normalized.append(numeric)
    if not any(normalized):
        raise MemoryIndexNodeRuntimeError("node_index_embedding_result_invalid")
    embedding = NodeIndexEmbedding(
        vector=tuple(normalized),
        identity=identity,
        model=model,
        revision=revision,
        dimension=dimension,
        normalization=normalization,
        receipt_reference=receipt_reference,
    )
    if expected_identity is not None and embedding.identity != expected_identity:
        raise MemoryIndexNodeRuntimeError("node_retrieval_embedding_identity_mismatch")
    return embedding


def _embedding_identity_tuple(embedding: NodeIndexEmbedding) -> tuple[object, ...]:
    return (
        embedding.identity,
        embedding.model,
        embedding.revision,
        embedding.dimension,
        embedding.normalization,
    )


def _completed_rerank_scores(
    value: object,
    *,
    expected_identity: str,
) -> dict[int, float]:
    if not isinstance(value, Mapping) or value.get("rerank_identity") != expected_identity:
        raise MemoryIndexNodeRuntimeError("node_rerank_result_identity_mismatch")
    raw = value.get("rerank_scores")
    if not isinstance(raw, list) or not raw:
        raise MemoryIndexNodeRuntimeError("node_rerank_result_invalid")
    scores: dict[int, float] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise MemoryIndexNodeRuntimeError("node_rerank_result_invalid")
        index = item.get("index")
        score = item.get("score")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index in scores
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            raise MemoryIndexNodeRuntimeError("node_rerank_result_invalid")
        scores[index] = float(score)
    return scores


def _completed_structured_json(
    value: object,
    *,
    expected_identity: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise MemoryIndexNodeRuntimeError("node_structured_json_result_invalid")
    if value.get("structured_json_identity") != expected_identity:
        raise MemoryIndexNodeRuntimeError("node_structured_json_result_identity_mismatch")
    output = value.get("structured_json")
    if not isinstance(output, Mapping):
        raise MemoryIndexNodeRuntimeError("node_structured_json_result_invalid")
    try:
        json.dumps(output, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise MemoryIndexNodeRuntimeError("node_structured_json_result_invalid") from exc
    return dict(output)


def _assert_generation_identity(engine: Any, embedding: NodeIndexEmbedding) -> None:
    manifest = getattr(engine, "_lancedb_generation_manifest", None)
    if manifest is None:
        raise MemoryIndexNodeRuntimeError("node_index_generation_identity_unavailable")
    try:
        spec = manifest.spec
        index_identity = spec.embedding_index_identity
        model = spec.embedding_model
        revision = spec.model_revision
        dimension = spec.embedding_dimension
    except Exception as exc:
        raise MemoryIndexNodeRuntimeError("node_index_generation_identity_unavailable") from exc
    if (
        not isinstance(index_identity, str)
        or _SHA256_RE.fullmatch(index_identity) is None
        or index_identity != embedding.identity
        or model != embedding.model
        or revision != embedding.revision
        or dimension != embedding.dimension
    ):
        raise MemoryIndexNodeRuntimeError("node_index_generation_identity_mismatch")


def _outbox_payload(value: object) -> dict[str, str]:
    if not isinstance(value, (str, bytes, bytearray)):
        raise MemoryIndexNodeRuntimeError("node_index_outbox_payload_invalid")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise MemoryIndexNodeRuntimeError("node_index_outbox_payload_invalid") from exc
    required = {
        "action",
        "expected_embedding_hash",
        "material_revision",
        "memory_id",
        "memory_version",
        "project_id",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("action") != "upsert"
    ):
        raise MemoryIndexNodeRuntimeError("node_index_outbox_payload_invalid")
    project_id = _project_id(payload.get("project_id"))
    expected = payload.get("expected_embedding_hash")
    if (
        not isinstance(expected, str)
        or _MATERIAL_SHA256_RE.fullmatch(expected) is None
        or payload.get("material_revision") != expected
        or not isinstance(payload.get("memory_version"), int)
        or payload["memory_version"] < 0
        or not isinstance(payload.get("memory_id"), str)
        or not payload["memory_id"].strip()
    ):
        raise MemoryIndexNodeRuntimeError("node_index_outbox_payload_invalid")
    return {
        "project_id": project_id,
        "memory_id": str(payload["memory_id"]),
        "expected_embedding_hash": expected,
    }


def _outbox_id(value: object) -> str:
    if not isinstance(value, str) or _OUTBOX_ID_RE.fullmatch(value) is None:
        raise MemoryIndexNodeRuntimeError("node_index_outbox_id_invalid")
    return value


def _outbox_reference(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("outbox:"):
        raise MemoryIndexNodeRuntimeError("node_index_outbox_reference_invalid")
    return _outbox_id(value.removeprefix("outbox:"))


def _project_id(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("project:"):
        raise MemoryIndexNodeRuntimeError("node_index_project_invalid")
    suffix = value.removeprefix("project:")
    if _OUTBOX_ID_RE.fullmatch(suffix) is None:
        raise MemoryIndexNodeRuntimeError("node_index_project_invalid")
    return value


def _material_from_subject_hash(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise MemoryIndexNodeRuntimeError("node_index_subject_hash_invalid")
    material_hash = value.removeprefix("sha256:")
    if _MATERIAL_SHA256_RE.fullmatch(material_hash) is None:
        raise MemoryIndexNodeRuntimeError("node_index_subject_hash_invalid")
    return material_hash


__all__ = [
    "GovernedRetrievalEmbedder",
    "MemoryIndexNodeRuntime",
    "MemoryIndexNodeRuntimeError",
    "NodeIndexEmbedding",
    "install_memory_index_node_runtime",
    "open_server_memory_index_node_runtime",
]
