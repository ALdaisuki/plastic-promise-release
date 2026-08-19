"""Tests for server-owned local-inference-node governance.

The fixture applies the additive schema explicitly before opening the runtime
registry.  Production startup never creates these canonical SQLite tables.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.core.derived_work import DerivedWorkStore
from plastic_promise.core.node_governance import (
    AcceleratorBudget,
    NodeExecutionFailure,
    NodeExecutionResult,
    NodeGovernanceError,
    NodeHealthEvidence,
    NodeIdentityEvidence,
    NodeInferenceWorkCoordinator,
    NodeRegistration,
    NodeRegistrationAuthority,
    NodeTaskRequest,
    ResolvedNodeTask,
    _open_node_governance_for_test,
    _open_node_registration_authority_for_test,
    accelerator_admission,
    fallback_chain_for,
    is_accelerator_task_kind,
    task_priority_for,
)
from plastic_promise.core.node_governance_schema import apply_node_governance_schema
from plastic_promise.core.node_private_transport import (
    PrivateNodeEndpoint,
    PrivateNodeTransportProbe,
)
from plastic_promise.core.node_task_authority import (
    _open_memory_index_node_task_authority_for_test,
)

_ACTIVE_REVISION = "cfg-20260806T000000Z-000000000000"


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 6, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _digest(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _material_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def test_accelerator_task_kind_policy_is_shared_by_scheduler_and_executor():
    assert is_accelerator_task_kind("semantic-dedupe") is True
    assert is_accelerator_task_kind("canonical-memory-write") is False


def _identity(
    *,
    embedding_revision: str = "a" * 40,
    structured_json: bool = False,
    provider_class: str | None = None,
) -> NodeIdentityEvidence:
    return NodeIdentityEvidence(
        protocol_version="local-inference-node/v1",
        embedding_model="BAAI/bge-m3",
        embedding_revision=embedding_revision,
        embedding_dimension=1024,
        embedding_normalization="l2",
        embedding_artifact_sha256=_digest("embedding-artifact"),
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_revision="b" * 40,
        rerank_artifact_sha256=_digest("rerank-artifact"),
        provider_class=provider_class or ("hybrid" if structured_json else "local"),
        structured_json_model="acme/structured-v1" if structured_json else None,
        structured_json_revision="c" * 40 if structured_json else None,
    )


def _registration(
    node_id: str,
    *,
    node_kind: str = "remote-node",
    identity: NodeIdentityEvidence | None = None,
    capabilities: tuple[str, ...] = ("embedding", "rerank"),
    max_concurrency: int = 4,
) -> NodeRegistration:
    return NodeRegistration(
        node_id=node_id,
        node_kind=node_kind,
        transport_id=f"transport:{node_id}",
        transport_evidence=_digest(f"transport:{node_id}"),
        expected_identity=identity or _identity(),
        capabilities=capabilities,
        max_concurrency=max_concurrency,
    )


def _health(
    node_id: str,
    *,
    identity: NodeIdentityEvidence | None = None,
    capabilities: tuple[str, ...] = ("embedding", "rerank"),
    queue_depth: int = 0,
    available_slots: int = 4,
) -> NodeHealthEvidence:
    return NodeHealthEvidence(
        node_id=node_id,
        observed_identity=identity or _identity(),
        capabilities=capabilities,
        queue_depth=queue_depth,
        available_slots=available_slots,
    )


@dataclass(frozen=True)
class _Revision:
    revision_id: str


@dataclass(frozen=True)
class _Snapshot:
    revision_id: str | None


class FakeControlStore:
    def __init__(self, revision_id: str = _ACTIVE_REVISION) -> None:
        self.revision_id = revision_id

    def get_revision(self, revision_id: str) -> _Revision:
        if revision_id != self.revision_id:
            raise LookupError("missing")
        return _Revision(revision_id)

    def safe_config(self) -> _Snapshot:
        return _Snapshot(self.revision_id)


@dataclass(frozen=True)
class _RoutingSnapshot:
    revision_id: str | None
    config: dict[str, object]


class ActiveRoutingConfig:
    def __init__(
        self,
        *,
        identity: str,
        revision_id: str | None = _ACTIVE_REVISION,
        enabled: bool = True,
        project_node_ids: list[str] | None = None,
    ) -> None:
        self.snapshot = _RoutingSnapshot(
            revision_id=revision_id,
            config={
                "node_routing": {
                    "enabled": enabled,
                    "embedding_policy": "pinned-node",
                    "embedding_required_identity": identity,
                    "embedding_pinned_node_id": "remote-a",
                    "allowed_node_ids": project_node_ids or ["remote-a"],
                }
            },
        )

    def safe_config(self) -> _RoutingSnapshot:
        return self.snapshot


class RevisionedRoutingConfig:
    def __init__(
        self,
        *,
        identity: str,
        original_revision: str,
        replacement_revision: str,
        original_profile_digest: str,
        replacement_profile_digest: str,
    ) -> None:
        self.active_revision = original_revision
        self.revisions = {
            original_revision: self._snapshot(
                revision_id=original_revision,
                identity=identity,
                inference_mode="local",
            ),
            replacement_revision: self._snapshot(
                revision_id=replacement_revision,
                identity=identity,
                inference_mode="cloud",
            ),
        }
        self.profile_digests = {
            original_revision: original_profile_digest,
            replacement_revision: replacement_profile_digest,
        }

    @staticmethod
    def _snapshot(
        *, revision_id: str, identity: str, inference_mode: str
    ) -> _RoutingSnapshot:
        return _RoutingSnapshot(
            revision_id,
            {
                "node_routing": {
                    "enabled": True,
                    "inference_mode": inference_mode,
                    "embedding_policy": "pinned-node",
                    "embedding_required_identity": identity,
                    "embedding_pinned_node_id": "remote-a",
                    "allowed_node_ids": ["remote-a"],
                }
            },
        )

    def safe_config(self) -> _RoutingSnapshot:
        return self.revisions[self.active_revision]

    def get_revision(self, revision_id: str) -> _RoutingSnapshot:
        return self.revisions[revision_id]

    def compute_profile_digest(self, revision_id: str) -> str:
        return self.profile_digests[revision_id]


def _store(tmp_path, *, clock: Clock | None = None):
    path = tmp_path / "canonical.db"
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        apply_node_governance_schema(connection)
        connection.commit()
    selected_clock = clock or Clock()
    return (
        _open_node_governance_for_test(path, clock=selected_clock),
        DerivedWorkStore(path, clock=selected_clock),
        _open_node_registration_authority_for_test(clock=selected_clock),
        path,
    )


def _activate(store, authority, registration: NodeRegistration):  # type: ignore[no-untyped-def]
    health = _health(
        registration.node_id,
        identity=registration.expected_identity,
        capabilities=registration.capabilities,
        available_slots=registration.max_concurrency,
    )
    verified = authority.verify_controlled_revision(
        FakeControlStore(),
        config_revision=_ACTIVE_REVISION,
        registration=registration,
        health=health,
    )
    store.register(verified)
    store.observe_health(health)
    return verified


def _insert_memory_index_outbox(
    path,
    *,
    project_id: str = "project:alpha",
    memory_id: str = "memory-authority",
    embedding_hash: str | None = None,
    outbox_id: str = "outbox_authority",
) -> str:
    material_hash = embedding_hash or _material_hash("authority-material")
    payload = {
        "action": "upsert",
        "expected_embedding_hash": material_hash,
        "material_revision": material_hash,
        "memory_id": memory_id,
        "memory_version": 1,
        "project_id": project_id,
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, project_id TEXT, embedding_hash TEXT)"
        )
        connection.execute(
            """
            CREATE TABLE store_outbox (
                outbox_id TEXT PRIMARY KEY, tool_name TEXT, project_id TEXT,
                status TEXT, payload_json TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO memories (id, project_id, embedding_hash) VALUES (?, ?, ?)",
            (memory_id, project_id, material_hash),
        )
        connection.execute(
            """
            INSERT INTO store_outbox (outbox_id, tool_name, project_id, status, payload_json)
            VALUES (?, 'memory_index', ?, 'pending', ?)
            """,
            (outbox_id, project_id, json.dumps(payload, sort_keys=True)),
        )
    return material_hash


class TaskAuthority:
    def __init__(
        self,
        *,
        identity: NodeIdentityEvidence | None = None,
        policy: str = "remote-node-first",
        pinned_node_id: str | None = None,
        allowed_node_ids: tuple[str, ...] = (),
        project_id: str = "project:alpha",
        inference_mode: str = "hybrid",
    ) -> None:
        self.identity = identity or _identity()
        self.policy = policy
        self.pinned_node_id = pinned_node_id
        self.allowed_node_ids = allowed_node_ids
        self.project_id = project_id
        self.inference_mode = inference_mode
        self.verified: list[ResolvedNodeTask] = []

    def resolve(self, request: NodeTaskRequest) -> ResolvedNodeTask:
        required_identity = (
            self.identity.embedding_key
            if request.operation == "embedding"
            else self.identity.rerank_key
            if request.operation == "rerank"
            else _digest("structure-v1-contract")
        )
        return ResolvedNodeTask(
            project_id=self.project_id,
            operation=request.operation,
            input_reference=request.input_reference,
            subject_hash=_digest(f"{self.project_id}:{request.input_reference}"),
            visibility="project",
            config_revision=_ACTIVE_REVISION,
            required_identity=required_identity,
            scheduling_policy=self.policy,
            inference_mode=self.inference_mode,
            pinned_node_id=self.pinned_node_id,
            allowed_node_ids=self.allowed_node_ids,
        )

    def verify(self, resolved: ResolvedNodeTask) -> None:
        if resolved.project_id != self.project_id:
            raise NodeGovernanceError("node_task_reference_ownership_invalid")
        self.verified.append(resolved)


class SuccessExecutor:
    def __init__(self) -> None:
        self.leases = []

    def execute(self, lease):
        self.leases.append(lease)
        return NodeExecutionResult(latency_ms=12, evidence={"transport": "verified"})


@dataclass(frozen=True)
class _HttpResponse:
    status_code: int
    payload: object

    @property
    def content(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def json(self) -> object:
        return self.payload


class _PrivateResolver:
    def resolve(self, node_id: str) -> PrivateNodeEndpoint:
        return PrivateNodeEndpoint(
            node_id=node_id,
            transport_id=f"transport:{node_id}",
            base_url="http://127.0.0.1:19130",
            authorization="Bearer private-only",
        )


class _PrivateHttp:
    def get(self, url: str, **kwargs: object) -> _HttpResponse:
        assert url.startswith("http://127.0.0.1:19130/")
        headers = kwargs.get("headers")
        assert headers == {"Authorization": "Bearer private-only"}
        if url.endswith("/health"):
            return _HttpResponse(
                200,
                {
                    "status": "ok",
                    "protocol_version": "local-inference-node/v1",
                    "queue_depth": 0,
                    "available_slots": 4,
                    "max_concurrency": 4,
                },
            )
        if url.endswith("/v1/identity"):
            return _HttpResponse(
                200,
                {
                    "protocol_version": "local-inference-node/v1",
                    "node_id": "remote-a",
                    "capabilities": ["embeddings", "rerank"],
                    "embedding": {
                        "model": "BAAI/bge-m3",
                        "revision": "a" * 40,
                        "dimension": 1024,
                        "normalization": "l2",
                        "artifact_sha256": _digest("embedding-artifact"),
                    },
                    "rerank": {
                        "model": "BAAI/bge-reranker-v2-m3",
                        "revision": "b" * 40,
                        "artifact_sha256": _digest("rerank-artifact"),
                    },
                },
            )
        raise AssertionError(url)

    def post(self, url: str, **kwargs: object) -> _HttpResponse:
        assert url == "http://127.0.0.1:19130/v1/embeddings"
        assert kwargs.get("headers") == {"Authorization": "Bearer private-only"}
        assert kwargs.get("json") == {"input": ["canonical vector input"]}
        return _HttpResponse(
            200,
            {
                "embedding_identity": "BAAI/bge-m3@" + "a" * 40,
                "dimension": 1024,
                "data": [{"index": 0, "embedding": [0.25] * 1024}],
            },
        )


class _ColdStartEmbeddingHttp(_PrivateHttp):
    """Record the operation timeout used by a cold-start embedding request."""

    def __init__(self) -> None:
        self.embedding_timeout: float | None = None

    def post(self, url: str, **kwargs: object) -> _HttpResponse:
        self.embedding_timeout = float(kwargs["timeout"])
        return super().post(url, **kwargs)


class _BusyEmbeddingHttp(_PrivateHttp):
    def post(self, url: str, **kwargs: object) -> _HttpResponse:
        assert url == "http://127.0.0.1:19130/v1/embeddings"
        return _HttpResponse(429, {"error": "node_overloaded"})


def test_runtime_requires_explicit_migration_and_does_not_create_a_database(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(NodeGovernanceError, match="node_governance_schema_missing"):
        _open_node_governance_for_test(missing)
    assert not missing.exists()


def test_explicit_schema_migration_remains_rollbackable(tmp_path):
    path = tmp_path / "canonical.db"
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        apply_node_governance_schema(connection)
        connection.rollback()
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inference_nodes'"
            ).fetchone()
            is None
        )


def test_registration_requires_server_issued_receipt_and_active_revision(tmp_path):
    store, _derived, authority, _path = _store(tmp_path)
    registration = _registration("remote-a")
    health = _health("remote-a")

    with pytest.raises(NodeGovernanceError, match="node_registration_authority_server_required"):
        NodeRegistrationAuthority(clock=Clock())
    with pytest.raises(NodeGovernanceError, match="node_controlled_revision_unavailable"):
        authority.verify_controlled_revision(
            FakeControlStore("cfg-20260806T000000Z-111111111111"),
            config_revision=_ACTIVE_REVISION,
            registration=registration,
            health=health,
        )

    verified = authority.verify_controlled_revision(
        FakeControlStore(),
        config_revision=_ACTIVE_REVISION,
        registration=registration,
        health=health,
    )
    registered = store.register(verified)
    assert registered.registration_reference == _ACTIVE_REVISION
    assert registered.state == "registered"


def test_private_transport_probe_binds_loopback_identity_without_exposing_endpoint(tmp_path):
    _store_value, _derived, authority, _path = _store(tmp_path)
    registration = _registration("remote-a")
    probe = PrivateNodeTransportProbe(_PrivateResolver(), http_client=_PrivateHttp())

    observation = probe.probe(registration)

    assert observation.registration.transport_evidence != registration.transport_evidence
    assert observation.health.observed_identity == registration.expected_identity
    assert observation.health.capabilities == ("embedding", "rerank")
    assert "19130" not in repr(observation)
    verified = authority.verify_private_transport(
        FakeControlStore(),
        config_revision=_ACTIVE_REVISION,
        registration=registration,
        transport=probe,
    )
    assert verified.receipt.source == "controlled-revision"
    with pytest.raises(NodeGovernanceError, match="node_private_endpoint_invalid"):
        PrivateNodeEndpoint(
            node_id="remote-a",
            transport_id="transport:remote-a",
            base_url="http://192.168.5.14:19130",
        )


def test_private_transport_discovery_creates_only_an_untrusted_declaration(tmp_path):
    _store_value, _derived, authority, _path = _store(tmp_path)
    probe = PrivateNodeTransportProbe(_PrivateResolver(), http_client=_PrivateHttp())

    declaration = probe.discover_registration(node_id="remote-a", max_concurrency=4)

    assert declaration.node_id == "remote-a"
    assert declaration.transport_id == "transport:remote-a"
    assert declaration.expected_identity == _identity()
    assert declaration.capabilities == ("embedding", "rerank")
    # Discovery alone is not a registry write or a registration receipt.
    verified = authority.verify_private_transport(
        FakeControlStore(),
        config_revision=_ACTIVE_REVISION,
        registration=declaration,
        transport=probe,
    )
    assert verified.receipt.source == "controlled-revision"


def test_private_transport_rechecks_identity_for_each_embedding_execution(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity),
    )
    job = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="private-embedding",
            operation="embedding",
            input_reference="memory:private-embedding",
        )
    )
    probe = PrivateNodeTransportProbe(_PrivateResolver(), http_client=_PrivateHttp())

    class PrivateEmbeddingExecutor:
        def execute(self, lease):
            return probe.execute_embedding(lease, input_text="canonical vector input")

    run = coordinator.run_job(
        job_id=job.job.job_id,
        project_id="project:alpha",
        executor=PrivateEmbeddingExecutor(),
    )
    result = derived.get(job_id=job.job.job_id, project_id="project:alpha").result

    assert (run.outcome, run.node_id) == ("completed", "remote-a")
    assert result["result"]["embedding_identity"] == identity.embedding_key
    assert result["result"]["embedding_dimension"] == 1024
    assert len(result["result"]["embedding"]) == 1024
    assert "19130" not in repr(result)
    assert "private-only" not in repr(result)


def test_private_transport_gives_embedding_a_cold_start_timeout_budget(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity),
    )
    job = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="private-embedding-cold-start",
            operation="embedding",
            input_reference="memory:private-embedding-cold-start",
        )
    )
    http = _ColdStartEmbeddingHttp()
    probe = PrivateNodeTransportProbe(_PrivateResolver(), http_client=http)

    class PrivateEmbeddingExecutor:
        def execute(self, lease):
            return probe.execute_embedding(lease, input_text="canonical vector input")

    run = coordinator.run_job(
        job_id=job.job.job_id,
        project_id="project:alpha",
        executor=PrivateEmbeddingExecutor(),
    )

    assert run.outcome == "completed"
    assert http.embedding_timeout is not None
    assert http.embedding_timeout >= 15.0


def test_private_transport_preserves_identity_drift_from_node_response(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity),
    )
    job = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="private-embedding-drift",
            operation="embedding",
            input_reference="memory:private-embedding-drift",
        )
    )

    class _IdentityDriftHttp(_PrivateHttp):
        def post(self, url: str, **kwargs: object) -> _HttpResponse:
            assert url == "http://127.0.0.1:19130/v1/embeddings"
            assert kwargs.get("headers") == {"Authorization": "Bearer private-only"}
            return _HttpResponse(409, {"error": "node_embedding_identity_drift"})

    probe = PrivateNodeTransportProbe(_PrivateResolver(), http_client=_IdentityDriftHttp())

    class PrivateEmbeddingExecutor:
        def execute(self, lease):
            return probe.execute_embedding(lease, input_text="canonical vector input")

    run = coordinator.run_job(
        job_id=job.job.job_id,
        project_id="project:alpha",
        executor=PrivateEmbeddingExecutor(),
    )

    assert run.failure_code == "node_private_embedding_identity_drift"
    assert store.require_node("remote-a").state == "quarantined"


def test_private_transport_preserves_resource_busy_as_retryable_without_quarantine(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, pinned_node_id="remote-a"),
        retry_delay_seconds=0,
    )
    job = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="private-embedding-resource-busy",
            operation="embedding",
            input_reference="memory:private-embedding-resource-busy",
        )
    )
    probe = PrivateNodeTransportProbe(_PrivateResolver(), http_client=_BusyEmbeddingHttp())

    class PrivateEmbeddingExecutor:
        def execute(self, lease):
            return probe.execute_embedding(lease, input_text="canonical vector input")

    run = coordinator.run_job(
        job_id=job.job.job_id,
        project_id="project:alpha",
        executor=PrivateEmbeddingExecutor(),
    )

    assert run.outcome == "retry-wait"
    assert run.failure_code == "node_overloaded"
    assert store.require_node("remote-a").state == "active"


def test_sqlite_memory_index_authority_rechecks_canonical_subject_and_active_route(tmp_path):
    _registry, _derived, _registration_authority, path = _store(tmp_path)
    identity = _identity().embedding_key
    material_hash = _insert_memory_index_outbox(path)
    config = ActiveRoutingConfig(identity=identity)
    authority = _open_memory_index_node_task_authority_for_test(path, config)
    request = NodeTaskRequest(
        project_id="project:alpha",
        idempotency_key="authority-test",
        operation="embedding",
        input_reference="outbox:outbox_authority",
    )

    resolved = authority.resolve(request)

    assert resolved.project_id == "project:alpha"
    assert resolved.subject_hash == f"sha256:{material_hash}"
    assert resolved.config_revision == _ACTIVE_REVISION
    assert resolved.required_identity == identity
    assert resolved.scheduling_policy == "pinned-node"
    assert resolved.pinned_node_id == "remote-a"
    assert resolved.allowed_node_ids == ("remote-a",)
    authority.verify(resolved)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memories SET embedding_hash = ? WHERE id = 'memory-authority'",
            (_material_hash("changed-material"),),
        )
    with pytest.raises(NodeGovernanceError, match="node_task_subject_stale"):
        authority.verify(resolved)


def test_leased_memory_index_work_keeps_original_route_after_active_mode_changes(tmp_path):
    store, derived, registration_authority, path = _store(tmp_path)
    identity = _identity()
    verified = _activate(
        store,
        registration_authority,
        _registration("remote-a", identity=identity),
    )
    _insert_memory_index_outbox(path)
    original_revision = _ACTIVE_REVISION
    active_revision = "cfg-20260806T000100Z-111111111111"
    original_profile_digest = _digest("original-compute-profile")
    active_profile_digest = _digest("active-compute-profile")
    config = RevisionedRoutingConfig(
        identity=identity.embedding_key,
        original_revision=original_revision,
        replacement_revision=active_revision,
        original_profile_digest=original_profile_digest,
        replacement_profile_digest=active_profile_digest,
    )
    authority = _open_memory_index_node_task_authority_for_test(path, config)
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=authority,
    )
    original = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="original-route-work",
            operation="embedding",
            input_reference="outbox:outbox_authority",
        )
    )
    assert original.job.config_revision == original_revision
    assert original.job.payload["profile_digest"] == original_profile_digest
    store.record_identity_revalidation(
        node_id="remote-a",
        config_revision=original_revision,
        required_identity=identity.embedding_key,
        profile_digest=original_profile_digest,
        verification_receipt=verified.receipt,
    )

    config.active_revision = active_revision
    executor = SuccessExecutor()
    run = coordinator.run_job(
        job_id=original.job.job_id,
        project_id="project:alpha",
        executor=executor,
    )

    assert run.outcome == "completed"
    assert executor.leases[0].resolved.config_revision == original_revision
    replacement = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="new-active-route-work",
            operation="embedding",
            input_reference="outbox:outbox_authority",
        )
    )
    assert replacement.job.config_revision == active_revision
    assert replacement.job.payload["profile_digest"] == active_profile_digest


def test_sqlite_memory_index_authority_rejects_caller_project_and_disabled_or_stale_route(tmp_path):
    _registry, _derived, _registration_authority, path = _store(tmp_path)
    identity = _identity().embedding_key
    _insert_memory_index_outbox(path)
    config = ActiveRoutingConfig(identity=identity)
    authority = _open_memory_index_node_task_authority_for_test(path, config)
    bad_project = NodeTaskRequest(
        project_id="project:other",
        idempotency_key="authority-project-test",
        operation="embedding",
        input_reference="outbox:outbox_authority",
    )
    with pytest.raises(NodeGovernanceError, match="node_task_reference_ownership_invalid"):
        authority.resolve(bad_project)

    disabled = ActiveRoutingConfig(identity=identity, enabled=False)
    disabled_authority = _open_memory_index_node_task_authority_for_test(path, disabled)
    request = NodeTaskRequest(
        project_id="project:alpha",
        idempotency_key="authority-disabled-test",
        operation="embedding",
        input_reference="outbox:outbox_authority",
    )
    with pytest.raises(NodeGovernanceError, match="node_task_routing_disabled"):
        disabled_authority.resolve(request)

    missing_revision = ActiveRoutingConfig(identity=identity, revision_id=None)
    missing_revision_authority = _open_memory_index_node_task_authority_for_test(
        path, missing_revision
    )
    with pytest.raises(NodeGovernanceError, match="node_task_control_revision_unavailable"):
        missing_revision_authority.resolve(request)


def test_identity_drift_quarantines_then_matching_health_recovers_with_audit(tmp_path):
    store, _derived, authority, path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))

    quarantined = store.observe_health(
        _health("remote-a", identity=_identity(embedding_revision="c" * 40))
    )
    assert (quarantined.state, quarantined.quarantine_reason) == (
        "quarantined",
        "node_identity_drift",
    )
    assert store.observe_health(_health("remote-a", identity=identity)).state == "active"
    with sqlite3.connect(path) as connection:
        events = [
            row[0]
            for row in connection.execute("SELECT event_name FROM inference_node_audit_events")
        ]
    assert events == ["node_identity_drift", "node_identity_recovered"]


def test_execution_identity_drift_quarantines_before_the_next_schedule(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, pinned_node_id="remote-a"),
    )
    job = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="execution-drift",
            operation="embedding",
            input_reference="memory:execution-drift",
        )
    )

    class DriftExecutor:
        def execute(self, _lease):
            raise NodeExecutionFailure("node_private_embedding_identity_drift")

    run = coordinator.run_job(
        job_id=job.job.job_id,
        project_id="project:alpha",
        executor=DriftExecutor(),
    )
    assert run.failure_code == "node_private_embedding_identity_drift"
    assert store.require_node("remote-a").state == "quarantined"
    with sqlite3.connect(_path) as connection:
        events = [
            row[0]
            for row in connection.execute(
                "SELECT event_name FROM inference_node_audit_events ORDER BY event_sequence"
            )
        ]
    assert events[-1] == "node_execution_identity_drift"


def test_foreground_rerank_uses_verified_selection_without_durable_prompt_payload(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, pinned_node_id="remote-a"),
    )
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="rerank",
        input_reference="rerank:foreground-a",
        subject_hash=_digest("foreground-rerank"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.rerank_key,
        scheduling_policy="pinned-node",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
    )

    class RerankExecutor:
        def execute(self, lease):
            assert lease.derived_lease is not None
            assert lease.node_id == "remote-a"
            return NodeExecutionResult(
                latency_ms=3.0,
                result={"rerank_scores": [{"raw-provider-result": "must-not-persist"}]},
            )

    result, node_id, reason, receipt_reference = coordinator.execute_foreground(
        resolved=resolved,
        request_fingerprint="never-persist-the-live-query",
        executor=RerankExecutor(),
    )
    assert result is not None
    assert (node_id, reason) == ("remote-a", "pinned-node")
    assert derived.stats(
        project_id="project:alpha", job_kind="node-inference-foreground"
    )["completed"] == 1
    with sqlite3.connect(_path) as connection:
        payload_json, result_json = connection.execute(
            "SELECT payload_json,result_json FROM derived_work_jobs "
            "WHERE project_id=? AND job_kind='node-inference-foreground'",
            ("project:alpha",),
        ).fetchone()
    connection.close()
    marker = json.loads(result_json)
    assert marker["outcome"] == "completed"
    assert marker["node_id"] == "remote-a"
    assert marker["receipt_reference"] == receipt_reference
    assert marker["result_digest"].startswith("sha256:")
    persisted = payload_json + result_json
    assert "never-persist-the-live-query" not in persisted
    assert "raw-provider-result" not in persisted
    assert "must-not-persist" not in persisted


def test_foreground_resource_busy_degrades_without_quarantining_node(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, pinned_node_id="remote-a"),
    )
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="embedding",
        input_reference="embedding:foreground-resource-busy",
        subject_hash=_digest("foreground-resource-busy"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.embedding_key,
        scheduling_policy="pinned-node",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
    )

    class BusyExecutor:
        def execute(self, _lease):
            raise NodeExecutionFailure("node_overloaded")

    result, node_id, reason, receipt_reference = coordinator.execute_foreground(
        resolved=resolved,
        request_fingerprint="foreground-resource-busy",
        executor=BusyExecutor(),
    )

    assert result is None
    assert node_id is None
    assert reason == "node_overloaded"
    assert store.require_node("remote-a").state == "active"
    assert derived.get(job_id=receipt_reference, project_id="project:alpha").status == "retry_wait"


def test_foreground_long_execution_renews_its_fence_bearing_lease(tmp_path):
    def realtime() -> datetime:
        return datetime.now(timezone.utc)

    store, derived, authority, _path = _store(tmp_path, clock=realtime)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, pinned_node_id="remote-a"),
    )
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="embedding",
        input_reference="embedding:foreground-long-running",
        subject_hash=_digest("foreground-long-running"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.embedding_key,
        scheduling_policy="pinned-node",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
    )

    class LongRunningExecutor:
        def execute(self, lease):
            assert lease.derived_lease is not None
            assert lease.derived_lease.job.fencing_generation == 1
            time.sleep(1.25)
            return NodeExecutionResult(
                latency_ms=1_250,
                result={"embedding": [0.25, 0.75]},
            )

    result, node_id, reason, receipt_reference = coordinator.execute_foreground(
        resolved=resolved,
        request_fingerprint="raw-query-must-remain-process-local",
        executor=LongRunningExecutor(),
        lease_seconds=1,
    )

    assert result is not None
    assert (node_id, reason) == ("remote-a", "pinned-node")
    marker = derived.get(job_id=receipt_reference, project_id="project:alpha")
    assert marker.status == "completed"
    assert marker.fencing_generation == 1


def test_reservation_renewal_cannot_shorten_a_live_fenced_reservation(tmp_path):
    clock = Clock()
    store, _derived, authority, _path = _store(tmp_path, clock=clock)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="embedding",
        input_reference="embedding:reservation-renewal",
        subject_hash=_digest("reservation-renewal"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.embedding_key,
        scheduling_policy="pinned-node",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
    )

    selection = store.reserve(
        job_id="foreground-marker",
        project_id="project:alpha",
        fencing_generation=1,
        resolved=resolved,
        lease_expires_at="2026-08-06T00:00:10Z",
    )
    assert selection is not None
    store.renew_reservation(
        job_id="foreground-marker",
        fencing_generation=1,
        lease_expires_at="2026-08-06T00:00:05Z",
    )

    with sqlite3.connect(_path) as connection:
        expires_at = connection.execute(
            "SELECT lease_expires_at FROM inference_node_reservations "
            "WHERE job_id=? AND fencing_generation=?",
            ("foreground-marker", 1),
        ).fetchone()[0]
    assert expires_at == "2026-08-06T00:00:10Z"


def test_reservation_renewal_rejects_a_stale_fencing_generation(tmp_path):
    clock = Clock()
    store, _derived, authority, _path = _store(tmp_path, clock=clock)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="embedding",
        input_reference="embedding:reservation-fence",
        subject_hash=_digest("reservation-fence"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.embedding_key,
        scheduling_policy="pinned-node",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
    )

    first = store.reserve(
        job_id="foreground-marker",
        project_id="project:alpha",
        fencing_generation=1,
        resolved=resolved,
        lease_expires_at="2026-08-06T00:00:10Z",
    )
    assert first is not None
    store.release_reservation(job_id="foreground-marker", fencing_generation=1)
    replacement = store.reserve(
        job_id="foreground-marker",
        project_id="project:alpha",
        fencing_generation=2,
        resolved=resolved,
        lease_expires_at="2026-08-06T00:00:20Z",
    )
    assert replacement is not None

    with pytest.raises(NodeGovernanceError) as error:
        store.renew_reservation(
            job_id="foreground-marker",
            fencing_generation=1,
            lease_expires_at="2026-08-06T00:00:30Z",
        )
    assert error.value.code == "node_reservation_renewal_conflict"


def test_foreground_rejects_result_after_lease_is_recovered_with_a_new_fence(tmp_path):
    clock = Clock()
    store, derived, authority, path = _store(tmp_path, clock=clock)
    identity = _identity(structured_json=True)
    _activate(
        store,
        authority,
        _registration(
            "remote-a",
            identity=identity,
            capabilities=("embedding", "rerank", "structured-json"),
        ),
    )
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, pinned_node_id="remote-a"),
    )
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="structured-json",
        input_reference="structured-json:foreground-fenced",
        subject_hash=_digest("foreground-fenced"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.structured_json_key or "",
        scheduling_policy="pinned-node",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
    )
    replacement_lease = None

    class FencedExecutor:
        def execute(self, lease):
            nonlocal replacement_lease
            assert lease.derived_lease is not None
            clock.advance(seconds=3)
            assert coordinator.reconcile(project_id="project:alpha") == {
                "derived_work_recovered": 1,
                "reservations_released": 1,
            }
            replacement_lease = derived.claim(
                job_id=lease.derived_lease.job.job_id,
                project_id="project:alpha",
                lease_seconds=30,
            )
            return NodeExecutionResult(
                latency_ms=3_000,
                result={"structured_json": {"secret-model-output": "reject-me"}},
            )

    with pytest.raises(NodeExecutionFailure) as error:
        coordinator.execute_foreground(
            resolved=resolved,
            request_fingerprint="raw-structured-prompt-must-not-persist",
            executor=FencedExecutor(),
            lease_seconds=2,
        )
    assert error.value.code == "node_foreground_lease_invalidated"
    assert replacement_lease is not None
    marker = derived.get(
        job_id=replacement_lease.job.job_id,
        project_id="project:alpha",
    )
    assert marker.status == "leased"
    assert marker.fencing_generation == 2
    assert marker.result is None
    with sqlite3.connect(path) as connection:
        payload_json, result_json = connection.execute(
            "SELECT payload_json,result_json FROM derived_work_jobs WHERE job_id=?",
            (marker.job_id,),
        ).fetchone()
    persisted = payload_json + (result_json or "")
    assert "raw-structured-prompt-must-not-persist" not in persisted
    assert "secret-model-output" not in persisted
    assert "reject-me" not in persisted


def test_foreground_defer_waits_for_caller_rehydration_without_persisting_content(tmp_path):
    store, derived, authority, path = _store(tmp_path)
    identity = _identity(structured_json=True)
    _activate(
        store,
        authority,
        _registration(
            "remote-a",
            identity=identity,
            capabilities=("embedding", "rerank", "structured-json"),
        ),
    )
    store.observe_health(
        _health(
            "remote-a",
            identity=identity,
            capabilities=("embedding", "rerank", "structured-json"),
            available_slots=0,
        )
    )
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, pinned_node_id="remote-a"),
    )
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="structured-json",
        input_reference="structured-json:opaque-subject",
        subject_hash=_digest("foreground-structured-json"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.structured_json_key or "",
        scheduling_policy="pinned-node",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
    )

    class MustNotExecute:
        def execute(self, _lease):
            raise AssertionError("deferred work must not execute")

    result, node_id, reason, receipt_reference = coordinator.execute_foreground(
        resolved=resolved,
        request_fingerprint="private-prompt-and-payload",
        executor=MustNotExecute(),
    )

    assert result is None
    assert node_id is None
    assert reason == "governed_node_deferred"
    assert derived.stats(
        project_id="project:alpha", job_kind="node-inference-foreground"
    )["retry_wait"] == 1
    with sqlite3.connect(path) as connection:
        payload_json, result_json, status, failure_code = connection.execute(
            "SELECT payload_json,result_json,status,failure_code FROM derived_work_jobs "
            "WHERE project_id=? AND job_kind='node-inference-foreground'",
            ("project:alpha",),
        ).fetchone()
    connection.close()
    assert (status, failure_code, result_json) == (
        "retry_wait",
        "governed_node_deferred",
        None,
    )
    assert "private-prompt-and-payload" not in payload_json

    replacement = replace(
        resolved,
        config_revision="cfg-20260806T000100Z-111111111111",
    )
    replacement_result, _, _, replacement_receipt = coordinator.execute_foreground(
        resolved=replacement,
        request_fingerprint="private-prompt-and-payload",
        executor=MustNotExecute(),
    )
    assert replacement_result is None
    assert replacement_receipt != receipt_reference
    assert derived.get(
        job_id=receipt_reference,
        project_id="project:alpha",
    ).config_revision == _ACTIVE_REVISION
    assert derived.get(
        job_id=replacement_receipt,
        project_id="project:alpha",
    ).config_revision == replacement.config_revision

    store.observe_health(
        _health(
            "remote-a",
            identity=identity,
            capabilities=("embedding", "rerank", "structured-json"),
            available_slots=1,
        )
    )

    class RehydratedExecutor:
        def execute(self, _lease):
            return NodeExecutionResult(
                latency_ms=1.0,
                result={"structured_json": {"classification": "safe"}},
            )

    replay, replay_node, _replay_reason, replay_receipt = coordinator.execute_foreground(
        resolved=resolved,
        request_fingerprint="private-prompt-and-payload",
        executor=RehydratedExecutor(),
    )
    assert replay is not None
    assert replay_node == "remote-a"
    assert replay_receipt == receipt_reference
    assert derived.get(job_id=receipt_reference, project_id="project:alpha").status == "completed"


@pytest.mark.parametrize(
    "failure_code",
    ("node_private_rerank_response_invalid", "node_execution_failed"),
)
def test_foreground_rerank_failure_quarantines_node_before_compatible_fallback(
    tmp_path, failure_code
):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity, max_concurrency=1))
    _activate(
        store,
        authority,
        _registration("cloud-a", node_kind="cloud", identity=identity, max_concurrency=1),
    )
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, allowed_node_ids=("remote-a", "cloud-a")),
    )
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="rerank",
        input_reference="rerank:foreground-fallback",
        subject_hash=_digest("foreground-rerank-fallback"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.rerank_key,
        scheduling_policy="remote-node-first",
        allowed_node_ids=("remote-a", "cloud-a"),
    )

    class RemoteFailsThenFallbackSucceeds:
        def __init__(self):
            self.node_ids: list[str] = []

        def execute(self, lease):
            self.node_ids.append(lease.node_id)
            if lease.node_id == "remote-a":
                if failure_code == "node_execution_failed":
                    raise RuntimeError("private rerank transport failed")
                raise NodeExecutionFailure(failure_code)
            return NodeExecutionResult(latency_ms=2.0, result={"rerank_scores": []})

    executor = RemoteFailsThenFallbackSucceeds()
    with pytest.raises(
        NodeExecutionFailure if failure_code != "node_execution_failed" else RuntimeError
    ):
        coordinator.execute_foreground(
            resolved=resolved,
            request_fingerprint="foreground-rerank-fallback-first",
            executor=executor,
        )
    assert store.require_node("remote-a").state == "quarantined"

    result, node_id, _reason, _receipt = coordinator.execute_foreground(
        resolved=resolved,
        request_fingerprint="foreground-rerank-fallback-second",
        executor=executor,
    )
    assert result is not None
    assert node_id == "cloud-a"
    assert executor.node_ids == ["remote-a", "cloud-a"]


def test_runtime_rerank_routes_active_revision_through_private_transport(tmp_path):
    from plastic_promise.core.memory_index_node_runtime import MemoryIndexNodeRuntime

    store, derived, authority, path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, pinned_node_id="remote-a"),
    )

    class Control:
        def safe_config(self):
            return _RoutingSnapshot(
                _ACTIVE_REVISION,
                {
                    "node_routing": {
                        "enabled": True,
                        "rerank_policy": "pinned-node",
                        "rerank_required_identity": identity.rerank_key,
                        "rerank_pinned_node_id": "remote-a",
                        "allowed_node_ids": ["remote-a"],
                    }
                },
            )

    class Transport:
        def execute_embedding(self, _lease, *, input_text):
            raise AssertionError(input_text)

        def execute_rerank(self, lease, *, query, documents):
            assert lease.node_id == "remote-a"
            assert query == "find second"
            assert documents == ["first", "second"]
            return NodeExecutionResult(
                latency_ms=4,
                result={
                    "rerank_identity": identity.rerank_key,
                    "rerank_scores": [{"index": 0, "score": 0.1}, {"index": 1, "score": 0.9}],
                },
            )

    runtime = MemoryIndexNodeRuntime(
        coordinator=coordinator,
        derived_work=derived,
        transport=Transport(),
        canonical_db_path=path,
        control_config=Control(),
    )
    outcome = runtime.rerank_for_context(
        project_id="project:alpha",
        query="find second",
        documents=["first", "second"],
    )
    assert outcome.scores == {0: 0.1, 1: 0.9}
    assert (outcome.node_id, outcome.selection_reason) == ("remote-a", "pinned-node")


def test_coordinator_reuses_derived_work_and_reserves_one_matching_node(tmp_path):
    store, derived, authority, path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity, max_concurrency=1))
    tasks = TaskAuthority(identity=identity)
    coordinator = NodeInferenceWorkCoordinator(
        registry=store, derived_work=derived, authority=tasks
    )

    created = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="embedding-a",
            operation="embedding",
            input_reference="memory:subject-a",
        )
    )
    reused = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="embedding-a",
            operation="embedding",
            input_reference="memory:subject-a",
        )
    )
    assert created.created is True
    assert reused.created is False
    assert coordinator.run_next("project:alpha", SuccessExecutor()).outcome == "completed"
    result = derived.get(job_id=created.job.job_id, project_id="project:alpha")
    assert result.status == "completed"
    assert result.result == {
        "outcome": "completed",
        "node_id": "remote-a",
        "selection_reason": "remote-node-first",
        "latency_ms": 12.0,
        "evidence": {"transport": "verified"},
    }
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inference_node_tasks'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM inference_node_reservations").fetchone()[0]
            == 0
        )


def test_run_job_claims_only_the_requested_job_and_persists_derived_result(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity),
    )
    selected = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="run-job-selected",
            operation="embedding",
            input_reference="memory:selected",
        )
    )
    other = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="run-job-other",
            operation="embedding",
            input_reference="memory:other",
        )
    )

    class VectorExecutor(SuccessExecutor):
        def execute(self, lease):
            self.leases.append(lease)
            return NodeExecutionResult(
                latency_ms=7,
                evidence={"transport": "verified"},
                result={"embedding": [0.125, 0.25], "identity": identity.embedding_key},
            )

    executor = VectorExecutor()
    run = coordinator.run_job(
        job_id=selected.job.job_id,
        project_id="project:alpha",
        executor=executor,
    )
    assert (run.outcome, run.node_id) == ("completed", "remote-a")
    assert [lease.derived_lease.job.job_id for lease in executor.leases] == [selected.job.job_id]
    assert derived.get(job_id=other.job.job_id, project_id="project:alpha").status == "pending"
    assert derived.get(job_id=selected.job.job_id, project_id="project:alpha").result == {
        "outcome": "completed",
        "node_id": "remote-a",
        "selection_reason": "remote-node-first",
        "latency_ms": 7.0,
        "evidence": {"transport": "verified"},
        "result": {"embedding": [0.125, 0.25], "identity": identity.embedding_key},
    }

    with pytest.raises(NodeGovernanceError, match="node_task_not_claimable"):
        coordinator.run_job(
            job_id=selected.job.job_id,
            project_id="project:alpha",
            executor=executor,
        )


def test_run_job_rejects_a_project_that_does_not_own_the_requested_job(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity),
    )
    job = coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="run-job-project",
            operation="embedding",
            input_reference="memory:project-scoped",
        )
    )

    with pytest.raises(NodeGovernanceError, match="node_task_not_claimable"):
        coordinator.run_job(
            job_id=job.job.job_id,
            project_id="project:other",
            executor=SuccessExecutor(),
        )
    assert derived.get(job_id=job.job.job_id, project_id="project:alpha").status == "pending"


def test_authority_controls_project_policy_and_input_ownership(tmp_path):
    store, derived, _authority, _path = _store(tmp_path)
    tasks = TaskAuthority(
        project_id="project:beta", policy="pinned-node", pinned_node_id="remote-a"
    )
    coordinator = NodeInferenceWorkCoordinator(
        registry=store, derived_work=derived, authority=tasks
    )
    request = NodeTaskRequest(
        project_id="project:alpha",
        idempotency_key="cross-project",
        operation="embedding",
        input_reference="memory:subject-a",
    )
    with pytest.raises(NodeGovernanceError, match="node_task_reference_ownership_invalid"):
        coordinator.enqueue(request)
    assert derived.stats(project_id="project:alpha") == {
        "pending": 0,
        "retry_wait": 0,
        "leased": 0,
        "completed": 0,
        "dead": 0,
        "cancelled": 0,
    }


@pytest.mark.parametrize(
    ("inference_mode", "expected_node"),
    (("local", "local-a"), ("cloud", "cloud-a")),
)
def test_active_inference_mode_filters_registered_compute_nodes(
    tmp_path,
    inference_mode,
    expected_node,
):
    store, derived, authority, _path = _store(tmp_path)
    local_identity = _identity(provider_class="local")
    cloud_identity = _identity(provider_class="cloud")
    _activate(store, authority, _registration("local-a", identity=local_identity))
    _activate(
        store,
        authority,
        _registration("cloud-a", node_kind="cloud", identity=cloud_identity),
    )
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(
            identity=local_identity,
            inference_mode=inference_mode,
            allowed_node_ids=("local-a", "cloud-a"),
        ),
    )
    coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key=f"mode-{inference_mode}",
            operation="embedding",
            input_reference=f"memory:mode-{inference_mode}",
        )
    )

    run = coordinator.run_next("project:alpha", SuccessExecutor())

    assert run is not None
    assert run.node_id == expected_node


def test_fastest_estimated_needs_twenty_samples_and_respects_allowed_node_set(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    _activate(store, authority, _registration("remote-b", identity=identity))
    for _ in range(20):
        store.record_success_latency("remote-b", "embedding", identity.embedding_key, latency_ms=2)
    tasks = TaskAuthority(
        identity=identity,
        policy="fastest-estimated",
        allowed_node_ids=("remote-a", "remote-b"),
    )
    coordinator = NodeInferenceWorkCoordinator(
        registry=store, derived_work=derived, authority=tasks
    )
    coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="fastest-a",
            operation="embedding",
            input_reference="memory:subject-a",
        )
    )
    executor = SuccessExecutor()
    run = coordinator.run_next("project:alpha", executor)
    assert run is not None and run.node_id == "remote-b"
    assert executor.leases[0].selection_reason == "fastest-estimated"


def test_fastest_estimated_uses_observed_queue_and_capacity(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    _activate(store, authority, _registration("remote-b", identity=identity))
    for _ in range(20):
        store.record_success_latency("remote-a", "embedding", identity.embedding_key, latency_ms=2)
        store.record_success_latency("remote-b", "embedding", identity.embedding_key, latency_ms=1)
    store.observe_health(_health("remote-b", identity=identity, queue_depth=10, available_slots=1))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(
            identity=identity,
            policy="fastest-estimated",
            allowed_node_ids=("remote-a", "remote-b"),
        ),
    )
    coordinator.enqueue(
        NodeTaskRequest("project:alpha", "observed-capacity", "embedding", "memory:subject-a")
    )

    run = coordinator.run_next("project:alpha", SuccessExecutor())

    assert run is not None and run.node_id == "remote-a"


@pytest.mark.parametrize(
    "failure_code",
    (
        "node_private_transport_unavailable",
        "node_private_embedding_response_invalid",
        "node_private_embedding_vector_invalid",
        "node_execution_failed",
    ),
)
def test_node_execution_failure_quarantines_and_retries_on_compatible_fallback(
    tmp_path, failure_code
):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity, max_concurrency=1))
    _activate(
        store,
        authority,
        _registration("cloud-a", node_kind="cloud", identity=identity, max_concurrency=1),
    )
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity, allowed_node_ids=("remote-a", "cloud-a")),
        retry_delay_seconds=0,
    )
    job = coordinator.enqueue(
        NodeTaskRequest("project:alpha", "fallback-after-failure", "embedding", "memory:subject-a")
    )

    class RemoteFailsThenFallbackSucceeds:
        def __init__(self):
            self.node_ids: list[str] = []

        def execute(self, lease):
            self.node_ids.append(lease.node_id)
            if lease.node_id == "remote-a":
                if failure_code == "node_execution_failed":
                    raise RuntimeError("unexpected private transport failure")
                raise NodeExecutionFailure(failure_code)
            return NodeExecutionResult(1.0, {"transport": "verified"})

    executor = RemoteFailsThenFallbackSucceeds()
    assert coordinator.run_next("project:alpha", executor).outcome == "retry-wait"
    assert store.require_node("remote-a").state == "quarantined"
    assert coordinator.run_next("project:alpha", executor).outcome == "completed"
    assert executor.node_ids == ["remote-a", "cloud-a"]
    assert derived.get(job_id=job.job.job_id, project_id="project:alpha").status == "completed"


def test_node_governed_fallbacks_are_distinct_and_embedding_stays_durable(tmp_path):
    store, derived, _authority, _path = _store(tmp_path)

    embedding = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(),
        retry_delay_seconds=1,
    )
    rerank = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(),
    )
    embedding_job = embedding.enqueue(
        NodeTaskRequest("project:alpha", "embedding-fallback", "embedding", "memory:embed")
    )
    rerank_job = rerank.enqueue(
        NodeTaskRequest("project:alpha", "rerank-fallback", "rerank", "memory:rerank")
    )
    executor = SuccessExecutor()
    assert embedding.run_next("project:alpha", executor).outcome == "retry-wait"
    assert rerank.run_next("project:alpha", executor).outcome == "original-order"
    assert (
        derived.get(job_id=embedding_job.job.job_id, project_id="project:alpha").status
        == "retry_wait"
    )
    assert derived.get(job_id=rerank_job.job.job_id, project_id="project:alpha").result == {
        "outcome": "original-order",
        "degradation_reason": "rerank_unavailable",
    }


def test_execution_failure_retries_and_reconcile_releases_expired_reservation(tmp_path):
    clock = Clock()
    store, derived, authority, _path = _store(tmp_path, clock=clock)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity, max_concurrency=1))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity),
        retry_delay_seconds=1,
    )
    job = coordinator.enqueue(
        NodeTaskRequest("project:alpha", "retry-a", "embedding", "memory:subject-a")
    )

    class Fails:
        def execute(self, lease):
            raise NodeExecutionFailure("private_transport_unavailable")

    run = coordinator.run_next("project:alpha", Fails())
    assert (run.outcome, run.failure_code) == ("retry-wait", "private_transport_unavailable")
    assert derived.get(job_id=job.job.job_id, project_id="project:alpha").status == "retry_wait"

    # The derived-store lease reconciler and reservation reconciler share the
    # same bounded, explicit worker call; neither touches canonical memory.
    clock.advance(seconds=400)
    assert coordinator.reconcile(project_id="project:alpha")["reservations_released"] == 0


def test_dashboard_projection_exposes_bounded_node_observability_without_transport_data(tmp_path):
    """The Dashboard seam receives useful node state but never private transport metadata."""

    clock = Clock()
    store, _derived, authority, _path = _store(tmp_path, clock=clock)
    identity = _identity()
    registration = _registration("remote-a", identity=identity, max_concurrency=6)
    _activate(store, authority, registration)
    store.observe_health(
        _health(
            "remote-a",
            identity=identity,
            queue_depth=2,
            available_slots=4,
        )
    )
    for _ in range(3):
        store.record_success_latency(
            "remote-a", "embedding", identity.embedding_key, latency_ms=12.5
        )

    projection = store.dashboard_projection()

    assert projection["schema"] == "plastic-promise/node-governance-dashboard/v1"
    assert projection["state"] == "ready"
    assert projection["nodes"] == [
            {
                "node_id": "remote-a",
                "node_kind": "remote-node",
                "provider_class": "local",
                "state": "active",
            "health": {"state": "fresh", "last_observed_at": "2026-08-06T00:00:00Z"},
            "capabilities": {
                "declared": ["embedding", "rerank"],
                "observed": ["embedding", "rerank"],
            },
            "embedding": {
                "model": "BAAI/bge-m3",
                "revision": "a" * 40,
                "dimension": 1024,
                "normalization": "l2",
            },
            "rerank": {"model": "BAAI/bge-reranker-v2-m3", "revision": "b" * 40},
            "capacity": {
                "queue_depth": 2,
                "available_slots": 4,
                "active_leases": 0,
                "max_concurrency": 6,
            },
            "latency": {
                "embedding": {"sample_count": 3, "median_ms": 12.5},
                "rerank": {"sample_count": 0, "median_ms": None},
            },
            "quarantine_reason": None,
        }
    ]
    serialized = json.dumps(projection, sort_keys=True)
    assert "transport:" not in serialized
    assert "transport_evidence" not in serialized
    assert "verification_receipt" not in serialized


def test_dashboard_projection_includes_only_safe_recent_route_and_degradation_codes(tmp_path):
    store, derived, authority, _path = _store(tmp_path)
    identity = _identity()
    _activate(store, authority, _registration("remote-a", identity=identity))
    coordinator = NodeInferenceWorkCoordinator(
        registry=store,
        derived_work=derived,
        authority=TaskAuthority(identity=identity),
    )
    coordinator.enqueue(
        NodeTaskRequest(
            project_id="project:alpha",
            idempotency_key="dashboard-route",
            operation="embedding",
            input_reference="memory:dashboard",
        )
    )
    assert coordinator.run_next("project:alpha", SuccessExecutor()).outcome == "completed"

    projection = store.dashboard_projection()

    assert projection["recent_routes"] == [
        {
            "node_id": "remote-a",
            "outcome": "completed",
            "selection_reason": "remote-node-first",
            "degradation_reason": None,
            "failure_code": None,
            "occurred_at": "2026-08-06T00:00:00Z",
        }
    ]
    assert projection["derived_work"] == {
        "node_inference": {
            "pending": 0,
            "retry_wait": 0,
            "leased": 0,
            "completed": 1,
            "dead": 0,
            "cancelled": 0,
        },
        "accelerator_max": {
            "pending": 0,
            "retry_wait": 0,
            "leased": 0,
            "completed": 0,
            "dead": 0,
            "cancelled": 0,
        },
    }
    assert "memory:dashboard" not in json.dumps(projection, sort_keys=True)
    assert '"transport"' not in json.dumps(projection, sort_keys=True)


def test_dashboard_projection_exposes_only_bounded_accelerator_audit_fields(tmp_path):
    store, derived, _authority, _path = _store(tmp_path)
    created = derived.enqueue_accelerator(
        project_id="project:alpha",
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        job_kind="accelerator-max",
        provider_identity="local-accelerator:v1",
        subject_id="memory:private-subject",
        subject_hash=_digest("private-subject"),
        dedupe_key="accelerator-audit",
        payload={
            "task_kind": "semantic-dedupe",
            "payload": {"private_input": "must-not-reach-dashboard"},
        },
        priority=0,
        max_attempts=4,
        max_queue_depth=4,
        max_daily_tasks=4,
    )
    claimed = derived.claim_next_accelerator(
        project_id="project:alpha",
        job_kind="accelerator-max",
        max_concurrency=1,
        foreground_priority_floor=100,
        lease_seconds=60,
    )
    assert claimed.lease is not None
    derived.complete(
        job_id=created.job.job_id,
        project_id="project:alpha",
        lease_token=claimed.lease.lease_token,
        fencing_generation=claimed.lease.job.fencing_generation,
        result={"outcome": "completed", "evidence": {"private_result": "hidden"}},
    )

    projection = store.dashboard_projection()

    assert projection["accelerator_audit"] == {
        "daily_admissions": 1,
        "recent_events": [
            {
                "event": "job_lifecycle",
                "task_kind": "semantic-dedupe",
                "decision": "completed",
                "reason": None,
                "occurred_at": "2026-08-06T00:00:00Z",
            },
            {
                "event": "attempt",
                "task_kind": "semantic-dedupe",
                "decision": "completed",
                "reason": None,
                "occurred_at": "2026-08-06T00:00:00Z",
            },
        ],
    }
    serialized = json.dumps(projection, sort_keys=True)
    for forbidden in (
        "project:alpha",
        "private-subject",
        "private_input",
        "must-not-reach-dashboard",
        "private_result",
        "local-accelerator",
    ):
        assert forbidden not in serialized


def test_dashboard_accelerator_audit_ignores_malformed_task_kind_without_failing(tmp_path):
    store, derived, _authority, _path = _store(tmp_path)
    created = derived.enqueue_accelerator(
        project_id="project:alpha",
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        job_kind="accelerator-max",
        provider_identity="local-accelerator:v1",
        subject_id="memory:malformed-task-kind",
        subject_hash=_digest("malformed-task-kind"),
        dedupe_key="accelerator-malformed-task-kind",
        payload={"task_kind": "semantic-dedupe", "payload": {}},
        priority=0,
        max_attempts=4,
        max_queue_depth=4,
        max_daily_tasks=4,
    )
    with sqlite3.connect(_path) as connection:
        connection.execute(
            "UPDATE derived_work_jobs SET payload_json = ? WHERE job_id = ?",
            (json.dumps({"task_kind": ["not", "a", "code"]}), created.job.job_id),
        )

    assert store.dashboard_projection()["accelerator_audit"] == {
        "daily_admissions": 1,
        "recent_events": [],
    }


def test_dashboard_projection_exposes_durable_accelerator_budget_denials_without_scope(tmp_path):
    store, derived, _authority, _path = _store(tmp_path)

    assert derived.record_accelerator_audit_event(
        event="admission",
        task_kind="semantic-dedupe",
        decision="denied",
        reason="accelerator_queue_budget_exhausted",
    )
    assert derived.record_accelerator_audit_event(
        event="scheduler",
        task_kind="scheduler",
        decision="deferred",
        reason="accelerator_memory_budget_exhausted",
    )
    assert not derived.record_accelerator_audit_event(
        event="admission",
        task_kind="semantic-dedupe",
        decision="denied",
        reason="accelerator_queue_budget_exhausted",
    )

    projection = store.dashboard_projection()["accelerator_audit"]

    assert projection == {
        "daily_admissions": 0,
        "recent_events": [
            {
                "event": "scheduler",
                "task_kind": "scheduler",
                "decision": "deferred",
                "reason": "accelerator_memory_budget_exhausted",
                "occurred_at": "2026-08-06T00:00:00Z",
            },
            {
                "event": "admission",
                "task_kind": "semantic-dedupe",
                "decision": "denied",
                "reason": "accelerator_queue_budget_exhausted",
                "occurred_at": "2026-08-06T00:00:00Z",
            },
        ],
    }
    serialized = json.dumps(projection, sort_keys=True)
    for forbidden in ("project:alpha", "subject", "payload", "provider", "result"):
        assert forbidden not in serialized


def test_policy_table_and_accelerator_admission_are_explicit():
    assert fallback_chain_for("embedding") == ("registered-compute-node", "defer")
    assert fallback_chain_for("rerank")[-1] == "original-order"
    assert fallback_chain_for("structured-json") == ("registered-compute-node", "defer")
    assert task_priority_for("embedding") > task_priority_for("rerank")
    budget = AcceleratorBudget(True, 1, 2, 3, 512)
    assert accelerator_admission(
        budget,
        task_kind="semantic-dedupe",
        active_tasks=0,
        queued_tasks=0,
        completed_today=0,
        free_memory_mib=1024,
    ).accepted
    assert not accelerator_admission(
        budget,
        task_kind="canonical-memory-write",
        active_tasks=0,
        queued_tasks=0,
        completed_today=0,
        free_memory_mib=1024,
    ).accepted


def test_profile_receipt_must_be_explicit_before_new_revision_selection(tmp_path):
    store, _derived, authority, _path = _store(tmp_path)
    identity = _identity()
    verified = _activate(store, authority, _registration("remote-a", identity=identity))
    required_identity = identity.embedding_key
    profile_digest = _digest("compute-profile")
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="embedding",
        input_reference="memory:profile-gated",
        subject_hash=_digest("subject:profile-gated"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=required_identity,
        scheduling_policy="pinned-node",
        inference_mode="hybrid",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
        profile_digest=profile_digest,
    )

    selection = store.reserve(
        job_id="profile-gated-online",
        project_id="project:alpha",
        fencing_generation=1,
        resolved=resolved,
        lease_expires_at="2026-08-06T00:01:00Z",
    )
    assert selection is None
    store.record_identity_revalidation(
        node_id="remote-a",
        config_revision=_ACTIVE_REVISION,
        required_identity=required_identity,
        profile_digest=profile_digest,
        verification_receipt=verified.receipt,
    )
    selection = store.reserve(
        job_id="profile-gated-after-revalidation",
        project_id="project:alpha",
        fencing_generation=1,
        resolved=resolved,
        lease_expires_at="2026-08-06T00:01:00Z",
    )
    assert selection is not None
    assert selection.node.node_id == "remote-a"
    with sqlite3.connect(_path) as connection:
        receipt = connection.execute(
            "SELECT config_revision,required_identity,profile_digest,observed_identity "
            "FROM inference_node_identity_receipts WHERE node_id='remote-a'"
        ).fetchone()
    assert receipt is not None
    assert receipt[:3] == (_ACTIVE_REVISION, required_identity, profile_digest)
    assert receipt[3] == _digest(
        json.dumps(identity.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def test_profile_receipt_requires_fresh_controlled_revision_evidence(tmp_path):
    clock = Clock()
    store, _derived, authority, _path = _store(tmp_path, clock=clock)
    identity = _identity()
    verified = _activate(store, authority, _registration("remote-a", identity=identity))
    clock.advance(seconds=301)

    with pytest.raises(NodeGovernanceError, match="node_identity_revalidation_required"):
        store.record_identity_revalidation(
            node_id="remote-a",
            config_revision=_ACTIVE_REVISION,
            required_identity=identity.embedding_key,
            profile_digest=_digest("compute-profile"),
            verification_receipt=verified.receipt,
        )

    other_revision = "cfg-20260806T000100Z-111111111111"
    store.observe_health(_health("remote-a", identity=identity))
    with pytest.raises(
        NodeGovernanceError,
        match="node_identity_receipt_activation_evidence_invalid",
    ):
        store.record_identity_revalidation(
            node_id="remote-a",
            config_revision=other_revision,
            required_identity=identity.embedding_key,
            profile_digest=_digest("compute-profile"),
            verification_receipt=verified.receipt,
        )


def test_structured_json_identity_and_operation_are_server_governed():
    identity = _identity(structured_json=True)
    restored = NodeIdentityEvidence.from_dict(identity.to_dict())
    assert restored == identity
    assert restored.provider_class == "hybrid"
    assert restored.structured_json_key is not None
    request = NodeTaskRequest(
        project_id="project:alpha",
        idempotency_key="structured-json-governed",
        operation="structured-json",
        input_reference="semantic:subject",
    )
    assert request.operation == "structured-json"
    resolved = ResolvedNodeTask(
        project_id="project:alpha",
        operation="structured-json",
        input_reference=request.input_reference,
        subject_hash=_digest("structured-json-subject"),
        visibility="project",
        config_revision=_ACTIVE_REVISION,
        required_identity=identity.structured_json_key or "",
        scheduling_policy="pinned-node",
        pinned_node_id="remote-a",
        allowed_node_ids=("remote-a",),
    )
    assert resolved.required_identity == identity.structured_json_key


def test_node_governance_rejects_structured_chunking_as_a_node_operation():
    with pytest.raises(NodeGovernanceError, match="node_task_operation_invalid"):
        NodeTaskRequest(
            "project:alpha",
            "structured-chunking-is-not-node-routed",
            "structured-chunking",
            "memory:structure",
        )
