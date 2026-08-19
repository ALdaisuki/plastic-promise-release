"""Fail-closed server bootstrap tests for private inference-node routing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest

import plastic_promise.core.node_runtime_bootstrap as bootstrap
from plastic_promise.core.memory_index_node_runtime import MemoryIndexNodeRuntimeError
from plastic_promise.core.node_governance import (
    NodeHealthEvidence,
    NodeIdentityEvidence,
    NodeRegistration,
)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _identity() -> NodeIdentityEvidence:
    return NodeIdentityEvidence(
        protocol_version="local-inference-node/v1",
        embedding_model="BAAI/bge-m3",
        embedding_revision="a" * 40,
        embedding_dimension=1024,
        embedding_normalization="l2",
        embedding_artifact_sha256=_digest("embedding-artifact"),
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_revision="b" * 40,
        rerank_artifact_sha256=_digest("rerank-artifact"),
    )


@dataclass(frozen=True)
class Snapshot:
    config: dict[str, object]
    revision_id: str


class Control:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    def safe_config(self) -> Snapshot:
        return self.snapshot

    def get_revision(self, revision_id: str) -> Snapshot:
        if revision_id != self.snapshot.revision_id:
            raise LookupError("missing")
        return self.snapshot


class Engine:
    def __init__(self) -> None:
        self.runtime: object | None = None
        self.runtime_status: dict[str, object] = {}

    def install_memory_index_node_runtime(self, runtime: object) -> None:
        self.runtime = runtime

    def memory_index_node_runtime(self) -> object | None:
        return self.runtime

    def set_memory_index_node_runtime_status(self, status: dict[str, object]) -> None:
        self.runtime_status = dict(status)

    def memory_index_node_runtime_status(self) -> dict[str, object]:
        return dict(self.runtime_status)


def _manifest(tmp_path, *, max_concurrency: int = 1):  # type: ignore[no-untyped-def]
    path = tmp_path / "deployment.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "plastic-promise-deployment/v1",
                "deployment_id": "server-primary",
                "profile": "split-accelerated",
                "modules": {},
                "nodes": [
                    {
                        "id": "remote-a",
                        "role": "local-heterogeneous-inference-node",
                        "ssh_host": "remote-a",
                        "capabilities": {"embedding": True, "rerank": True},
                        "max_concurrency": max_concurrency,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _routing(identity: NodeIdentityEvidence) -> dict[str, object]:
    return {
        "enabled": True,
        "allowed_node_ids": ["remote-a"],
        "embedding_required_identity": identity.embedding_key,
        "rerank_required_identity": identity.rerank_key,
    }


def test_disabled_control_plane_never_attempts_node_bootstrap(tmp_path):
    engine = Engine()
    report = bootstrap.bootstrap_memory_index_node_runtime(
        engine,
        environ={"PP_CONTROL_PLANE": "0"},
        control_config_factory=lambda *_args: pytest.fail("unexpected control read"),
    )

    assert report.state == "disabled"
    assert report.reason == "node_routing_control_plane_disabled"
    assert engine.memory_index_node_runtime() is None


def test_enabled_route_blocks_derived_indexing_when_private_runtime_is_missing(tmp_path):
    identity = _identity()
    engine = Engine()
    manifest = _manifest(tmp_path)
    report = bootstrap.bootstrap_memory_index_node_runtime(
        engine,
        environ={
            "PP_CONTROL_PLANE": "1",
            "PP_DEPLOYMENT_MANIFEST_PATH": str(manifest),
        },
        control_config_factory=lambda *_args: Control(
            Snapshot({"node_routing": _routing(identity)}, "cfg-20260806T000000Z-000000000000")
        ),
    )

    assert report.state == "blocked"
    assert report.reason == "node_private_runtime_config_missing"
    assert engine.memory_index_node_runtime_status()["reason"] == report.reason
    with pytest.raises(MemoryIndexNodeRuntimeError, match=report.reason):
        engine.memory_index_node_runtime().embedding_for_outbox()


def test_ready_bootstrap_registers_private_probed_node_and_installs_runtime(tmp_path, monkeypatch):
    identity = _identity()
    control = Control(
        Snapshot({"node_routing": _routing(identity)}, "cfg-20260806T000000Z-000000000000")
    )
    registration = NodeRegistration(
        node_id="remote-a",
        node_kind="remote-node",
        transport_id="transport:remote-a",
        transport_evidence=_digest("declared"),
        expected_identity=identity,
        capabilities=("embedding", "rerank"),
        max_concurrency=3,
    )
    observation = type(
        "Observation",
        (),
        {
            "registration": registration,
            "health": NodeHealthEvidence(
                node_id="remote-a",
                observed_identity=identity,
                capabilities=("embedding", "rerank"),
                queue_depth=0,
                available_slots=3,
            ),
        },
    )()

    class Transport:
        def discover_registration(self, *, node_id: str, max_concurrency: int):
            assert node_id == "remote-a"
            assert max_concurrency == 3
            return registration

        def probe(self, supplied):
            assert supplied.node_id == "remote-a"
            return observation

    class Authority:
        def verify_deployment(self, deployment, supplied, health):
            assert deployment.deployment_id == "server-primary"
            assert supplied == registration
            assert health == observation.health

        def verify_private_transport(self, control_store, **kwargs):
            assert control_store is control
            assert kwargs["config_revision"] == control.snapshot.revision_id
            return type("Verified", (), {"registration": registration})()

    class Registry:
        def __init__(self) -> None:
            self.registered = []
            self.health = []

        def register(self, value):
            self.registered.append(value)

        def observe_health(self, value):
            self.health.append(value)

    registry = Registry()
    monkeypatch.setattr(bootstrap, "open_server_node_governance", lambda: registry)
    monkeypatch.setattr(bootstrap, "open_server_node_registration_authority", lambda: Authority())
    engine = Engine()
    installed = object()
    report = bootstrap.bootstrap_memory_index_node_runtime(
        engine,
        environ={
            "PP_CONTROL_PLANE": "1",
            "PP_DEPLOYMENT_MANIFEST_PATH": str(_manifest(tmp_path, max_concurrency=3)),
        },
        control_config_factory=lambda *_args: control,
        resolver_factory=lambda _env: object(),
        transport_factory=lambda _resolver: Transport(),
        runtime_factory=lambda supplied_control, supplied_transport: (
            installed if supplied_control is control else pytest.fail("wrong control")
        ),
        installer=lambda target, runtime: target.install_memory_index_node_runtime(runtime),
    )

    assert report.state == "ready"
    assert report.registered_nodes == 1
    assert engine.memory_index_node_runtime() is installed
    assert len(registry.registered) == len(registry.health) == 1


def test_route_identity_drift_blocks_before_registering(tmp_path, monkeypatch):
    identity = _identity()
    wrong = NodeRegistration(
        node_id="remote-a",
        node_kind="remote-node",
        transport_id="transport:remote-a",
        transport_evidence=_digest("wrong"),
        expected_identity=NodeIdentityEvidence(
            **{**identity.__dict__, "embedding_revision": "c" * 40}
        ),
        capabilities=("embedding", "rerank"),
        max_concurrency=1,
    )

    class Transport:
        def discover_registration(self, **_kwargs):
            return wrong

        def probe(self, _registration):
            pytest.fail("identity mismatch must stop before probe")

    engine = Engine()
    report = bootstrap.bootstrap_memory_index_node_runtime(
        engine,
        environ={
            "PP_CONTROL_PLANE": "1",
            "PP_DEPLOYMENT_MANIFEST_PATH": str(_manifest(tmp_path)),
        },
        control_config_factory=lambda *_args: Control(
            Snapshot({"node_routing": _routing(identity)}, "cfg-20260806T000000Z-000000000000")
        ),
        resolver_factory=lambda _env: object(),
        transport_factory=lambda _resolver: Transport(),
    )

    assert report.state == "blocked"
    assert report.reason == "node_private_embedding_identity_mismatch"
