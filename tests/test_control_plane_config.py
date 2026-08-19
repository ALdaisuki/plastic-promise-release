from __future__ import annotations

import json
import re
import sqlite3
import stat
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from plastic_promise.control_plane import (
    ControlPlaneAuthorizationError,
    ControlPlaneConfigStore,
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlanePreconditionError,
    ControlPlaneStorageError,
    ControlPlaneValidationError,
)
from plastic_promise.control_plane.config_schema import (
    default_safe_config,
    embedding_identity,
    prepare_configuration,
    runtime_embedding_index_identity,
    safe_config_from_environment,
)


class _InjectedCrash(BaseException):
    pass


def _base_env() -> dict[str, str]:
    return {
        "PP_INFERENCE_GATEWAY_PROJECT_ID": "project:test",
        "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST": "api.example.test",
        "PP_INFERENCE_GATEWAY_TOKEN": "g" * 48,
    }


_BOOTSTRAP_DRIFT_CASES = (
    ("EMBEDDER_CHUNK_CHARS", "321"),
    ("EMBEDDER_STRUCTURE_HARD_CHARS", "654"),
    ("EMBEDDER_STRUCTURE_MAX_CHUNKS", "17"),
    ("EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", "123456"),
    ("PLASTIC_DB_PATH", "/srv/other/plastic_memory.db"),
    ("PLASTIC_LANCEDB_GENERATION_ROOT", "/srv/other/generations"),
    ("PLASTIC_LANCEDB_PATH", "/srv/other/lancedb"),
    ("PLASTIC_PROJECT_ID", "project:fallback-one"),
    ("PP_CONTROL_OPERATOR_TOKEN_SHA256", "1" * 64),
    ("PP_CONTROL_ALLOWED_ORIGINS", "http://127.0.0.1:29020"),
    ("PP_CONTROL_PLANE", "1"),
    ("PP_CONTROL_ROOT", "/srv/other/control"),
    ("PP_CONTROL_SECRET_ADMIN_TOKEN_SHA256", "2" * 64),
    ("PP_CONTROL_VIEWER_TOKEN_SHA256", "3" * 64),
    ("PP_INFERENCE_CLIENT_VECTOR_DIMENSION", "2048"),
    ("PP_INFERENCE_CLIENT_VECTOR_IDENTITY", "synthetic-client-vector-v2"),
    ("PP_INFERENCE_GATEWAY_BIND", "127.0.0.2"),
    ("PP_INFERENCE_GATEWAY_DB_PATH", "/srv/other/inference_jobs.db"),
    ("PP_INFERENCE_GATEWAY_PROJECT_ID", "project:other"),
    ("PP_INFERENCE_GATEWAY_TOKEN", "h" * 48),
    ("PP_INFERENCE_PROVIDER_HOST_ALLOWLIST", "other.example.test"),
    ("PP_MAINTENANCE_ENABLED", "1"),
    ("PP_MAINTENANCE_RUN_DIR", "/srv/other/run"),
    ("PP_PROJECT_ID", "project:fallback-two"),
)


def _embedding_patch() -> dict[str, object]:
    return {
        "embedding": {
            "enabled": True,
            "base_url": "https://api.example.test/v1",
            "model": "text-embedding-v4",
            "model_revision": "text-embedding-v4-r1",
            "dimension": 1024,
        }
    }


def _embedding_secret(value: str = "synthetic-embedding-secret") -> dict[str, object]:
    return {"embedding_api_key": {"op": "set", "value": value}}


def _evidence(revision) -> dict[str, object]:
    return {
        "revision_id": revision.revision_id,
        "embedding_identity": revision.embedding_identity,
        "provider_smoke": {"passed": True, "evidence_id": "smoke-001"},
        "shadow_generation": {"passed": True, "generation_id": "generation-001"},
        "quality_gate": {"passed": True, "evidence_id": "quality-001"},
    }


def _verify_generation(revision, evidence):
    if not isinstance(evidence, dict):
        raise ControlPlanePreconditionError("embedding_generation_required")
    if evidence.get("revision_id") != revision.revision_id:
        raise ControlPlaneConflictError("control_embedding_evidence_mismatch")
    if evidence.get("embedding_identity") != revision.embedding_identity:
        raise ControlPlaneConflictError("control_embedding_evidence_mismatch")
    try:
        provider = evidence["provider_smoke"]
        generation = evidence["shadow_generation"]
        quality = evidence["quality_gate"]
        assert provider == {"passed": True, "evidence_id": "smoke-001"}
        assert generation == {"passed": True, "generation_id": "generation-001"}
        assert quality == {"passed": True, "evidence_id": "quality-001"}
    except (AssertionError, KeyError):
        raise ControlPlanePreconditionError("embedding_generation_required") from None
    return {
        "provider_smoke_evidence_id": "smoke-001",
        "shadow_generation_id": "generation-001",
        "quality_gate_evidence_id": "quality-001",
        "manifest_sha256": "a" * 64,
        "verified_generation": True,
    }


def _install_current_generation(
    monkeypatch,
    *,
    generation_id: str,
    manifest_sha256: str,
    embedding_index_identity: str,
    quality_report: dict[str, object] | None = None,
) -> None:
    selected = SimpleNamespace(
        generation_id=generation_id,
        manifest_sha256=manifest_sha256,
        spec=SimpleNamespace(embedding_index_identity=embedding_index_identity),
        quality_report=quality_report
        or {
            "gate": {"status": "pass"},
            "smoke": {"passed": True},
            "backend": {"fallback_used": False, "degraded_used": False},
        },
    )

    class FakeGenerationManager:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def resolve_verified_current_selection(self):
            return selected, None, {}

    from plastic_promise.core import lancedb_generation

    monkeypatch.setattr(lancedb_generation, "GenerationManager", FakeGenerationManager)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _scalar(database_path, statement: str, parameters=()):
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(statement, parameters).fetchone()
    assert row is not None
    return row[0]


def _assert_bytes_absent(root, value: str) -> None:
    encoded = value.encode("utf-8")
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert encoded not in path.read_bytes(), path


def _install_crash(store, phase: str) -> None:
    def checkpoint(current: str) -> None:
        if current == phase:
            raise _InjectedCrash(phase)

    store._activation_checkpoint = checkpoint


def test_safe_config_is_fail_closed_and_reads_are_zero_write(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial_mtime = store.database_path.stat().st_mtime_ns

    snapshot = store.safe_config()
    revisions = store.list_revisions()
    events = store.audit()

    assert snapshot.source == "base"
    assert snapshot.revision_id is None
    assert snapshot.config["embedding"]["enabled"] is False
    assert snapshot.config["rerank"]["enabled"] is False
    assert snapshot.config["chunk_inference"]["enrichment_mode"] == "off"
    assert snapshot.config["chunk_inference"]["temperature"] == 0.0
    assert snapshot.config["chunk_inference"]["top_p"] == 1.0
    assert snapshot.config["chunk_inference"]["json_mode"] is True
    assert snapshot.config["chunk_inference"]["fusion_mode"] == "off"
    assert snapshot.config["chunk_inference"]["fusion_batch_size"] == 20
    assert snapshot.config["chunk_inference"]["fusion_lease_seconds"] == 120
    assert snapshot.config["chunk_inference"]["fusion_retry_delay_seconds"] == 5
    assert snapshot.config["chunk_inference"]["fusion_poll_seconds"] == 0.25
    assert snapshot.config["gateway"]["project_id"] == "project:test"
    assert snapshot.secrets["gateway_token"] is True
    assert "g" * 48 not in json.dumps(snapshot.to_dict())
    assert revisions == ()
    assert events == ()
    assert store.database_path.stat().st_mtime_ns == initial_mtime
    assert _mode(root) == 0o700
    assert _mode(store.revisions_dir) == 0o700
    assert _mode(store.database_path) == 0o600


def test_legacy_config_without_sampling_controls_upgrades_as_one_known_shape():
    legacy = default_safe_config()
    chunk = legacy["chunk_inference"]
    assert isinstance(chunk, dict)
    for name in ("temperature", "top_p", "json_mode"):
        chunk.pop(name)

    prepared = prepare_configuration(legacy, {}, {}, {})

    assert prepared.safe_config["chunk_inference"]["temperature"] == 0.0
    assert prepared.safe_config["chunk_inference"]["top_p"] == 1.0
    assert prepared.safe_config["chunk_inference"]["json_mode"] is True


def test_node_routing_is_controlled_non_secret_and_legacy_configs_remain_fail_closed():
    legacy = default_safe_config()
    legacy.pop("node_routing")
    prepared_legacy = prepare_configuration(legacy, {}, {}, {})
    assert prepared_legacy.safe_config["node_routing"]["enabled"] is False
    assert prepared_legacy.safe_config["node_routing"]["allowed_node_ids"] == []

    candidate = default_safe_config()
    routing = candidate["node_routing"]
    assert isinstance(routing, dict)
    routing.update(
        {
            "enabled": True,
            "embedding_policy": "pinned-node",
            "rerank_policy": "fastest-estimated",
            "embedding_required_identity": "sha256:" + "a" * 64,
            "rerank_required_identity": "sha256:" + "b" * 64,
            "embedding_pinned_node_id": "remote-a",
            "allowed_node_ids": ["remote-a", "ollama-local"],
            "accelerator_max_enabled": True,
        }
    )
    prepared = prepare_configuration(candidate, {}, {}, {})
    normalized = prepared.safe_config["node_routing"]
    assert normalized == {
        **routing,
        "allowed_node_ids": ["remote-a", "ollama-local"],
    }
    assert all("endpoint" not in name for name in prepared.environment)

    unsafe = default_safe_config()
    unsafe_routing = unsafe["node_routing"]
    assert isinstance(unsafe_routing, dict)
    unsafe_routing["endpoint"] = "http://192.168.5.14:19130"
    with pytest.raises(ControlPlaneValidationError, match="control_config_field_not_allowed"):
        prepare_configuration(unsafe, {}, {}, {})

    obsolete = default_safe_config()
    obsolete_routing = obsolete["node_routing"]
    assert isinstance(obsolete_routing, dict)
    obsolete_routing["structured_chunking_policy"] = "pinned-node"
    with pytest.raises(ControlPlaneValidationError, match="control_config_field_not_allowed"):
        prepare_configuration(obsolete, {}, {}, {})


def test_compute_projection_materializes_provider_contract_without_server_secret_projection():
    candidate = default_safe_config()
    candidate["embedding"].update(
        {
            "enabled": True,
            "base_url": "https://api.example.test/v1",
            "model": "embed-v1",
            "model_revision": "embed-r1",
            "dimension": 1024,
        }
    )
    candidate["rerank"].update(
        {
            "enabled": True,
            "base_url": "https://api.example.test/v1",
            "model": "rerank-v1",
            "model_revision": "rerank-r1",
        }
    )
    candidate["node_routing"].update(
        {
            "enabled": True,
            "inference_mode": "cloud",
            "embedding_required_identity": "sha256:" + "a" * 64,
            "rerank_required_identity": "sha256:" + "b" * 64,
            "allowed_node_ids": ["compute-a"],
        }
    )
    candidate["gateway"]["provider_host_allowlist"] = ["api.example.test"]
    prepared = prepare_configuration(
        candidate,
        {},
        {},
        {
            "embedding_api_key": {"op": "set", "value": "synthetic-embedding-key"},
            "rerank_api_key": {"op": "set", "value": "synthetic-rerank-key"},
            "compute_node_cloud_api_key": {
                "op": "set",
                "value": "synthetic-compute-key",
            },
        },
    )

    assert prepared.compute_environment == {
        "PP_ENDPOINT_ROLE": "pp-compute-node",
        "PP_LOCAL_NODE_CLOUD_API_KEY": "synthetic-compute-key",
        "PP_LOCAL_NODE_EMBEDDING_BACKEND": "openai-compatible",
        "PP_LOCAL_NODE_EMBEDDING_CLOUD_BASE_URL": "https://api.example.test/v1",
        "PP_LOCAL_NODE_EMBEDDING_CLOUD_PATH": "/embeddings",
        "PP_LOCAL_NODE_EMBEDDING_DIMENSION": "1024",
        "PP_LOCAL_NODE_EMBEDDING_MODEL": "embed-v1",
        "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION": "l2",
        "PP_LOCAL_NODE_EMBEDDING_REVISION": "embed-r1",
        "PP_LOCAL_NODE_PROVIDER_MODE": "cloud",
        "PP_LOCAL_NODE_RERANK_BACKEND": "openai-compatible",
        "PP_LOCAL_NODE_RERANK_CLOUD_BASE_URL": "https://api.example.test/v1",
        "PP_LOCAL_NODE_RERANK_CLOUD_PATH": "/rerank",
        "PP_LOCAL_NODE_RERANK_MODEL": "rerank-v1",
        "PP_LOCAL_NODE_RERANK_REVISION": "rerank-r1",
    }
    assert "EMBEDDER_API_KEY" not in prepared.environment
    assert "PP_RERANK_API_KEY" not in prepared.environment
    assert "PP_MEMORY_CHUNK_ENRICHMENT_API_KEY" not in prepared.environment
    assert "PP_LOCAL_NODE_PROVIDER_MODE" not in prepared.environment
    assert prepared.environment["EMBEDDER_PROVIDER"] == "fallback"
    assert prepared.environment["EMBEDDER_BASE_URL"] == ""
    assert prepared.environment["PP_RERANK_DISABLED"] == "1"
    assert prepared.environment["PP_RERANK_PROVIDERS"] == "original"
    assert prepared.environment["PP_RERANK_BASE_URL"] == ""
    assert prepared.environment["PP_MEMORY_CHUNK_ENRICHMENT"] == "off"
    assert prepared.environment["PP_MEMORY_CHUNK_ENRICHMENT_BASE_URL"] == ""
    assert prepared.environment["PP_INFERENCE_GATEWAY"] == "0"


def test_local_compute_projection_preserves_active_identity_without_cloud_fields():
    candidate = default_safe_config()
    candidate["embedding"].update(
        {
            "enabled": True,
            "base_url": "https://compute.local.test/v1",
            "model": "qwen3-embedding:4b",
            "model_revision": "sha256:" + "a" * 64,
            "dimension": 2560,
        }
    )
    candidate["rerank"].update(
        {
            "enabled": True,
            "base_url": "https://compute.local.test/v1",
            "model": "Qwen/Qwen3-Reranker-4B",
            "model_revision": "sha256:" + "b" * 64,
        }
    )
    candidate["chunk_inference"].update(
        {
            "chunking_mode": "structure-v1",
            "enrichment_mode": "on",
            "base_url": "https://compute.local.test/v1",
            "model": "structured-local",
            "model_revision": "sha256:" + "c" * 64,
        }
    )
    candidate["node_routing"].update(
        {
            "enabled": True,
            "inference_mode": "local",
            "embedding_required_identity": "sha256:" + "d" * 64,
            "rerank_required_identity": "sha256:" + "e" * 64,
            "allowed_node_ids": ["compute-local"],
        }
    )
    candidate["gateway"]["provider_host_allowlist"] = ["compute.local.test"]
    prepared = prepare_configuration(candidate, {}, {}, {})
    assert prepared.compute_environment == {
        "PP_ENDPOINT_ROLE": "pp-compute-node",
        "PP_LOCAL_NODE_EMBEDDING_DIMENSION": "2560",
        "PP_LOCAL_NODE_EMBEDDING_MODEL": "qwen3-embedding:4b",
        "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION": "l2",
        "PP_LOCAL_NODE_EMBEDDING_REVISION": "sha256:" + "a" * 64,
        "PP_LOCAL_NODE_PROVIDER_MODE": "local",
        "PP_LOCAL_NODE_RERANK_MODEL": "Qwen/Qwen3-Reranker-4B",
        "PP_LOCAL_NODE_RERANK_REVISION": "sha256:" + "b" * 64,
        "PP_LOCAL_NODE_STRUCTURED_JSON_MODEL": "structured-local",
        "PP_LOCAL_NODE_STRUCTURED_JSON_REVISION": "sha256:" + "c" * 64,
    }


def test_existing_control_store_restarts_from_governed_server_projection(tmp_path):
    root = tmp_path / "control"
    original = ControlPlaneConfigStore(root, base_env=_base_env()).safe_config()
    candidate = default_safe_config()
    candidate["embedding"].update(
        {
            "enabled": True,
            "base_url": "https://compute.local.test/v1",
            "model": "qwen3-embedding:4b",
            "model_revision": "qwen3-embedding-r1",
            "dimension": 2560,
        }
    )
    candidate["rerank"].update(
        {
            "enabled": True,
            "base_url": "https://compute.local.test/v1",
            "model": "Qwen/Qwen3-Reranker-4B",
            "model_revision": "qwen3-reranker-r1",
        }
    )
    candidate["node_routing"].update(
        {
            "enabled": True,
            "inference_mode": "local",
            "embedding_required_identity": "sha256:" + "d" * 64,
            "rerank_required_identity": "sha256:" + "e" * 64,
            "allowed_node_ids": ["compute-local"],
        }
    )
    candidate["gateway"]["provider_host_allowlist"] = ["compute.local.test"]
    server_projection = prepare_configuration(candidate, {}, {}, {}).environment

    restarted = ControlPlaneConfigStore(
        root,
        base_env={**_base_env(), **server_projection},
    )

    assert restarted.safe_config() == original


def test_governed_node_identity_owns_runtime_embedding_index_identity():
    candidate = default_safe_config()
    routing = candidate["node_routing"]
    assert isinstance(routing, dict)
    required_identity = "sha256:" + "f" * 64
    routing.update(
        {
            "enabled": True,
            "embedding_required_identity": required_identity,
            "rerank_required_identity": "sha256:" + "e" * 64,
            "allowed_node_ids": ["compute-local"],
        }
    )

    assert runtime_embedding_index_identity(candidate) == required_identity


def test_legacy_config_without_fusion_controls_upgrades_fail_closed():
    legacy = default_safe_config()
    chunk = legacy["chunk_inference"]
    assert isinstance(chunk, dict)
    for name in (
        "fusion_mode",
        "fusion_batch_size",
        "fusion_max_wait_seconds",
        "fusion_max_queue_size",
        "fusion_workers",
        "fusion_lease_seconds",
        "fusion_retry_delay_seconds",
        "fusion_poll_seconds",
    ):
        chunk.pop(name)

    prepared = prepare_configuration(legacy, {}, {}, {})

    assert prepared.safe_config["chunk_inference"]["fusion_mode"] == "off"
    assert prepared.safe_config["chunk_inference"]["fusion_batch_size"] == 20
    assert prepared.safe_config["chunk_inference"]["fusion_max_wait_seconds"] == 2.0
    assert prepared.safe_config["chunk_inference"]["fusion_max_queue_size"] == 1_000
    assert prepared.safe_config["chunk_inference"]["fusion_workers"] == 2
    assert prepared.safe_config["chunk_inference"]["fusion_lease_seconds"] == 120
    assert prepared.safe_config["chunk_inference"]["fusion_retry_delay_seconds"] == 5
    assert prepared.safe_config["chunk_inference"]["fusion_poll_seconds"] == 0.25


def test_legacy_config_without_embedding_cost_policy_upgrades_as_one_known_shape():
    legacy = default_safe_config()
    embedding = legacy["embedding"]
    assert isinstance(embedding, dict)
    for name in ("cost_per_million_tokens", "cost_currency", "pricing_revision"):
        embedding.pop(name)

    prepared = prepare_configuration(legacy, {}, {}, {})

    assert prepared.safe_config["embedding"]["cost_per_million_tokens"] is None
    assert prepared.safe_config["embedding"]["cost_currency"] == ""
    assert prepared.safe_config["embedding"]["pricing_revision"] == ""


def test_persisted_legacy_cost_shape_upgrades_on_snapshot_and_revision_reads(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 41}},
        expected_etag=initial.etag,
        idempotency_key="stage-before-cost-schema-001",
        actor="operator",
        role="operator",
    )

    def legacy_json(raw: str) -> str:
        payload = json.loads(raw)
        embedding = payload["embedding"]
        for name in ("cost_per_million_tokens", "cost_currency", "pricing_revision"):
            embedding.pop(name)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    with sqlite3.connect(store.database_path) as connection:
        state = connection.execute(
            "SELECT base_safe_json FROM control_state WHERE singleton = 1"
        ).fetchone()
        row = connection.execute(
            "SELECT safe_json FROM revisions WHERE revision_id = ?",
            (revision.revision_id,),
        ).fetchone()
        assert state is not None
        assert row is not None
        connection.execute(
            "UPDATE control_state SET base_safe_json = ? WHERE singleton = 1",
            (legacy_json(state[0]),),
        )
        connection.execute(
            "UPDATE revisions SET safe_json = ? WHERE revision_id = ?",
            (legacy_json(row[0]), revision.revision_id),
        )
        connection.commit()

    reopened = ControlPlaneConfigStore(root, base_env=_base_env())
    snapshot_embedding = reopened.safe_config().config["embedding"]
    fetched_embedding = reopened.get_revision(revision.revision_id).config["embedding"]
    listed_embedding = reopened.list_revisions()[0].config["embedding"]
    for embedding in (snapshot_embedding, fetched_embedding, listed_embedding):
        assert embedding["cost_per_million_tokens"] is None
        assert embedding["cost_currency"] == ""
        assert embedding["pricing_revision"] == ""


def test_embedding_cost_policy_imports_renders_and_does_not_change_vector_identity():
    base = default_safe_config()
    embedding = base["embedding"]
    gateway = base["gateway"]
    assert isinstance(embedding, dict)
    assert isinstance(gateway, dict)
    embedding.update(
        {
            "enabled": True,
            "base_url": "https://api.example.test/v1",
            "model": "embedding-model",
            "model_revision": "embedding-model-r1",
        }
    )
    gateway["provider_host_allowlist"] = ["api.example.test"]
    without_cost = prepare_configuration(
        base,
        {"embedding_api_key": "synthetic-embedding-secret"},
        {},
        {},
    )
    with_cost = prepare_configuration(
        base,
        {"embedding_api_key": "synthetic-embedding-secret"},
        {
            "embedding": {
                "cost_per_million_tokens": 0.03,
                "cost_currency": "CNY",
                "pricing_revision": "provider-pricing-2026-07-24",
            }
        },
        {},
    )

    assert with_cost.embedding_identity == without_cost.embedding_identity
    assert runtime_embedding_index_identity(
        with_cost.safe_config
    ) == runtime_embedding_index_identity(without_cost.safe_config)
    assert with_cost.environment["EMBEDDER_COST_PER_MILLION_TOKENS"] == "0.03"
    assert with_cost.environment["EMBEDDER_COST_CURRENCY"] == "CNY"
    assert with_cost.environment["EMBEDDER_PRICING_REVISION"] == "provider-pricing-2026-07-24"
    imported = safe_config_from_environment(
        {
            "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST": "api.example.test",
            **with_cost.environment,
        }
    )
    assert imported["embedding"]["cost_per_million_tokens"] == 0.03
    assert imported["embedding"]["cost_currency"] == "CNY"
    assert imported["embedding"]["pricing_revision"] == "provider-pricing-2026-07-24"
    assert embedding_identity(imported) == embedding_identity(with_cost.safe_config)


@pytest.mark.parametrize(
    "patch",
    [
        {"cost_per_million_tokens": 0.03},
        {"cost_currency": "CNY"},
        {"pricing_revision": "provider-pricing-2026-07-24"},
        {
            "cost_per_million_tokens": 0.03,
            "cost_currency": "EUR",
            "pricing_revision": "provider-pricing-2026-07-24",
        },
    ],
)
def test_embedding_cost_policy_rejects_partial_or_unsupported_values(patch):
    with pytest.raises(
        ControlPlaneValidationError,
        match="control_embedding_cost_(?:policy_incomplete|currency_invalid)",
    ):
        prepare_configuration(default_safe_config(), {}, {"embedding": patch}, {})


def test_partial_legacy_sampling_shape_remains_fail_closed():
    malformed = default_safe_config()
    chunk = malformed["chunk_inference"]
    assert isinstance(chunk, dict)
    chunk.pop("temperature")

    with pytest.raises(ControlPlaneValidationError, match="control_config_incomplete"):
        prepare_configuration(malformed, {}, {}, {})


def test_etag_is_opaque_and_secret_independent(tmp_path):
    first_env = _base_env()
    first_env["EMBEDDER_API_KEY"] = "candidate-secret-one"
    second_env = dict(first_env)
    second_env["EMBEDDER_API_KEY"] = "candidate-secret-two"

    first = ControlPlaneConfigStore(tmp_path / "first", base_env=first_env).safe_config()
    same_material = ControlPlaneConfigStore(
        tmp_path / "same-material",
        base_env=first_env,
    ).safe_config()
    different_secret = ControlPlaneConfigStore(
        tmp_path / "different-secret",
        base_env=second_env,
    ).safe_config()

    assert first.etag != same_material.etag
    assert first.etag != different_secret.etag
    assert re.fullmatch(r'"sha256:[0-9a-f]{64}"', first.etag)


def test_validation_compares_expected_etag_in_validated_snapshot(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())

    with pytest.raises(ControlPlanePreconditionError, match="control_etag_mismatch") as error:
        store.validate({}, {}, expected_etag='"sha256:' + "0" * 64 + '"')

    assert error.value.status_code == 412


@pytest.mark.parametrize(
    ("candidate", "secret_ops", "reason"),
    [
        ({"environment": {"UNSAFE": "1"}}, {}, "control_config_field_not_allowed"),
        (
            {"gateway": {"project_id": "project:other"}},
            {},
            "control_config_field_not_allowed",
        ),
        (
            {"gateway": {"provider_host_allowlist": ["other.example.test"]}},
            {},
            "control_config_field_not_allowed",
        ),
        (
            {},
            {"gateway_token": {"op": "set", "value": "x" * 48}},
            "control_secret_field_not_allowed",
        ),
        (
            {"embedding": {"base_url": "https://wiki.example.test/v1"}},
            {},
            "control_provider_documentation_url",
        ),
        (
            {"embedding": {"base_url": "http://127.0.0.1:8080/v1"}},
            {},
            "control_provider_base_url_invalid",
        ),
    ],
)
def test_validation_rejects_arbitrary_or_bootstrap_authority_fields(
    tmp_path,
    candidate,
    secret_ops,
    reason,
):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())

    with pytest.raises(ControlPlaneValidationError, match=reason):
        store.validate(candidate, secret_ops)


def test_stage_requires_secret_admin_and_keeps_secret_write_only(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    etag = store.safe_config().etag
    secret = "synthetic-embedding-secret"

    with pytest.raises(ControlPlaneAuthorizationError, match="control_role_insufficient"):
        store.stage(
            _embedding_patch(),
            _embedding_secret(secret),
            expected_etag=etag,
            idempotency_key="stage-auth-001",
            actor="operator",
            role="operator",
        )

    revision = store.stage(
        _embedding_patch(),
        _embedding_secret(secret),
        expected_etag=etag,
        idempotency_key="stage-secret-001",
        actor="secret-admin",
        role="secret-admin",
    )

    public_material = json.dumps(
        {"dto": revision.to_dict(), "dataclass": asdict(revision)},
        sort_keys=True,
    ) + repr(revision)
    from plastic_promise.control_plane.config_schema import bootstrap_boundary_sha256

    assert secret not in public_material
    assert "bootstrap_boundary" not in public_material
    assert bootstrap_boundary_sha256(_base_env()) not in public_material
    assert not hasattr(revision, "bootstrap_boundary_sha256")
    assert revision.secrets["embedding_api_key"] is True
    assert revision.requires_embedding_evidence is True
    assert store.safe_config().revision_id is None
    assert store.get_revision(revision.revision_id) == revision
    assert store.list_revisions() == (revision,)
    revision_path = root / "revisions" / f"{revision.revision_id}.env"
    assert secret in revision_path.read_text(encoding="utf-8")
    assert secret.encode() not in store.database_path.read_bytes()
    assert _mode(revision_path) == 0o600


@pytest.mark.parametrize(
    ("name", "changed_value"),
    _BOOTSTRAP_DRIFT_CASES,
)
def test_activation_rejects_process_visible_bootstrap_drift(tmp_path, name, changed_value):
    root = tmp_path / name.casefold()
    base_env = _base_env()
    store = ControlPlaneConfigStore(root, base_env=base_env)
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 41}},
        expected_etag=initial.etag,
        idempotency_key=f"stage-bootstrap-{name.casefold()}",
        actor="operator",
        role="operator",
    )

    reopened = ControlPlaneConfigStore(
        root,
        base_env={**base_env, name: changed_value},
    )
    with pytest.raises(ControlPlaneConflictError, match="control_bootstrap_boundary_changed"):
        reopened.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key=f"activate-bootstrap-{name.casefold()}",
            actor="operator",
            role="operator",
        )

    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 0
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activations") == 0
    assert not store.managed_env_path.exists()


def test_staged_revision_exposes_exact_runtime_embedding_index_identity(
    tmp_path,
    monkeypatch,
):
    base_env = {
        **_base_env(),
        "EMBEDDER_CHUNK_CHARS": "321",
        "EMBEDDER_STRUCTURE_HARD_CHARS": "654",
        "EMBEDDER_STRUCTURE_MAX_CHUNKS": "17",
        "EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS": "123456",
    }
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=base_env)
    initial = store.safe_config()
    candidate = {
        **_embedding_patch(),
        "chunk_inference": {"chunking_mode": "structure-v1"},
    }
    validation = store.validate(candidate, _embedding_secret())
    revision = store.stage(
        candidate,
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-runtime-identity-001",
        actor="secret-admin",
        role="secret-admin",
    )
    revision_path = store.revisions_dir / f"{revision.revision_id}.env"
    managed_env = dict(
        line.split("=", 1)
        for line in revision_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    for name, value in {**base_env, **managed_env}.items():
        monkeypatch.setenv(name, value)

    from plastic_promise.core.memory_index import effective_embedding_model_name

    expected = effective_embedding_model_name()
    assert revision.embedding_identity.startswith("sha256:")
    assert validation.runtime_embedding_index_identity == expected
    assert validation.to_dict()["runtime_embedding_index_identity"] == expected
    assert revision.runtime_embedding_index_identity == expected
    assert revision.runtime_embedding_index_identity != revision.embedding_identity
    assert revision.to_dict()["runtime_embedding_index_identity"] == expected
    assert store.get_revision(revision.revision_id).runtime_embedding_index_identity == expected
    assert store.list_revisions()[0].runtime_embedding_index_identity == expected

    replay = store.stage(
        candidate,
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-runtime-identity-001",
        actor="secret-admin",
        role="secret-admin",
    )
    assert replay.runtime_embedding_index_identity == expected
    assert (
        _scalar(
            store.database_path,
            "SELECT runtime_embedding_index_identity FROM revisions WHERE revision_id = ?",
            (revision.revision_id,),
        )
        == expected
    )

    changed_base_env = {
        **base_env,
        "EMBEDDER_CHUNK_CHARS": "999",
        "EMBEDDER_STRUCTURE_HARD_CHARS": "1000",
    }
    reopened = ControlPlaneConfigStore(store.root, base_env=changed_base_env)
    assert reopened.get_revision(revision.revision_id).runtime_embedding_index_identity == expected
    reopened_replay = reopened.stage(
        candidate,
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-runtime-identity-001",
        actor="secret-admin",
        role="secret-admin",
    )
    assert reopened_replay.runtime_embedding_index_identity == expected


def test_embedding_activation_rejects_bootstrap_identity_drift_before_evidence(tmp_path):
    root = tmp_path / "control"
    original_env = {
        **_base_env(),
        "EMBEDDER_CHUNK_CHARS": "321",
        "EMBEDDER_STRUCTURE_HARD_CHARS": "654",
        "PLASTIC_LANCEDB_GENERATION_ROOT": str(tmp_path / "index"),
    }
    store = ControlPlaneConfigStore(root, base_env=original_env)
    initial = store.safe_config()
    revision = store.stage(
        {
            **_embedding_patch(),
            "chunk_inference": {"chunking_mode": "structure-v1"},
        },
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-runtime-drift-001",
        actor="secret-admin",
        role="secret-admin",
    )
    changed_env = {
        **original_env,
        "EMBEDDER_CHUNK_CHARS": "999",
        "EMBEDDER_STRUCTURE_HARD_CHARS": "1000",
    }

    def must_not_verify(_revision, _evidence):
        raise AssertionError("bootstrap drift must fail before evidence verification")

    reopened = ControlPlaneConfigStore(
        root,
        base_env=changed_env,
        generation_evidence_verifier=must_not_verify,
    )

    with pytest.raises(ControlPlaneConflictError, match="control_bootstrap_boundary_changed"):
        reopened.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-runtime-drift-001",
            actor="secret-admin",
            role="secret-admin",
            evidence=_evidence(revision),
        )
    assert _scalar(reopened.database_path, "SELECT COUNT(*) FROM activation_intent") == 0


def test_chunk_budget_change_requires_fresh_generation_for_unrelated_activation(tmp_path):
    root = tmp_path / "control"
    original_env = {
        **_base_env(),
        "EMBEDDER_CHUNK_CHARS": "321",
        "EMBEDDER_STRUCTURE_HARD_CHARS": "654",
    }
    store = ControlPlaneConfigStore(
        root,
        base_env=original_env,
        generation_evidence_verifier=_verify_generation,
    )
    initial = store.safe_config()
    first = store.stage(
        {
            **_embedding_patch(),
            "chunk_inference": {"chunking_mode": "structure-v1"},
        },
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-original-chunk-budget",
        actor="secret-admin",
        role="secret-admin",
    )
    first_result = store.activate(
        first.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-original-chunk-budget",
        actor="secret-admin",
        role="secret-admin",
        evidence=_evidence(first),
    )
    assert first_result.desired_generation_id == "generation-001"

    changed_env = {
        **original_env,
        "EMBEDDER_CHUNK_CHARS": "777",
        "EMBEDDER_STRUCTURE_HARD_CHARS": "888",
    }

    def verify_changed_generation(revision, evidence):
        if not isinstance(evidence, dict):
            raise ControlPlanePreconditionError("embedding_generation_required")
        if evidence.get("revision_id") != revision.revision_id:
            raise ControlPlaneConflictError("control_embedding_evidence_mismatch")
        shadow = evidence.get("shadow_generation")
        if shadow != {"passed": True, "generation_id": "generation-002"}:
            raise ControlPlanePreconditionError("embedding_generation_required")
        return {
            "provider_smoke_evidence_id": "smoke-002",
            "shadow_generation_id": "generation-002",
            "quality_gate_evidence_id": "quality-002",
            "manifest_sha256": "b" * 64,
            "verified_generation": True,
        }

    reopened = ControlPlaneConfigStore(
        root,
        base_env=changed_env,
        generation_evidence_verifier=verify_changed_generation,
    )
    active = reopened.safe_config()
    unrelated = reopened.stage(
        {"rerank": {"max_candidates": 47}},
        expected_etag=active.etag,
        idempotency_key="stage-after-chunk-budget-change",
        actor="operator",
        role="operator",
    )
    assert unrelated.embedding_identity == first.embedding_identity
    assert unrelated.runtime_embedding_index_identity != first.runtime_embedding_index_identity
    assert unrelated.requires_embedding_evidence is True

    with pytest.raises(ControlPlanePreconditionError, match="embedding_generation_required"):
        reopened.activate(
            unrelated.revision_id,
            expected_etag=active.etag,
            idempotency_key="activate-without-fresh-generation",
            actor="operator",
            role="operator",
        )

    evidence = _evidence(unrelated)
    evidence["shadow_generation"] = {"passed": True, "generation_id": "generation-002"}
    result = reopened.activate(
        unrelated.revision_id,
        expected_etag=active.etag,
        idempotency_key="activate-with-fresh-generation",
        actor="operator",
        role="operator",
        evidence=evidence,
    )
    assert result.desired_generation_id == "generation-002"
    assert result.desired_generation_manifest_sha256 == "b" * 64


def test_stage_idempotency_replays_and_conflicts_on_different_material(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())
    arguments = {
        "expected_etag": store.safe_config().etag,
        "idempotency_key": "stage-replay-001",
        "actor": "secret-admin",
        "role": "secret-admin",
    }

    first = store.stage(_embedding_patch(), _embedding_secret(), **arguments)
    replay = store.stage(_embedding_patch(), _embedding_secret(), **arguments)

    assert replay == first
    assert len(store.list_revisions()) == 1
    with pytest.raises(ControlPlaneConflictError, match="control_idempotency_key_conflict"):
        store.stage(
            {"embedding": {**_embedding_patch()["embedding"], "dimension": 1536}},
            _embedding_secret(),
            **arguments,
        )


def test_stage_requires_current_etag_and_secret_for_enabled_provider(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())

    with pytest.raises(ControlPlanePreconditionError, match="control_etag_mismatch"):
        store.stage(
            {},
            expected_etag='"sha256:' + "0" * 64 + '"',
            idempotency_key="stage-stale-001",
            actor="operator",
            role="operator",
        )
    with pytest.raises(ControlPlaneValidationError, match="control_required_secret_missing"):
        store.validate(_embedding_patch())


@pytest.mark.parametrize(
    ("candidate", "secret_ops"),
    [
        (
            {
                "embedding": {
                    "enabled": True,
                    "base_url": "https://other.example.test/v1",
                    "model": "text-embedding-v4",
                    "model_revision": "r1",
                }
            },
            {"embedding_api_key": {"op": "set", "value": "synthetic-embedding-key"}},
        ),
        (
            {
                "rerank": {
                    "enabled": True,
                    "base_url": "https://other.example.test/v1",
                    "model": "rerank-v1",
                    "model_revision": "r1",
                }
            },
            {"rerank_api_key": {"op": "set", "value": "synthetic-rerank-key"}},
        ),
        (
            {
                "chunk_inference": {
                    "chunking_mode": "structure-v1",
                    "enrichment_mode": "shadow",
                    "base_url": "https://other.example.test/v1",
                    "model": "chunk-v1",
                    "model_revision": "r1",
                }
            },
            {
                "chunk_inference_api_key": {
                    "op": "set",
                    "value": "synthetic-chunk-key",
                }
            },
        ),
    ],
)
def test_hosted_provider_cannot_escape_bootstrap_allowlist_when_gateway_is_disabled(
    tmp_path,
    candidate,
    secret_ops,
):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())

    with pytest.raises(
        ControlPlaneValidationError,
        match="control_gateway_provider_host_missing",
    ):
        store.validate(candidate, secret_ops)


def test_chunk_inference_sampling_and_json_mode_render_to_managed_environment(tmp_path):
    base_env = {
        **_base_env(),
        "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST": "api.deepseek.com",
    }
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=base_env)
    initial = store.safe_config()
    candidate = {
        "chunk_inference": {
            "chunking_mode": "structure-v1",
            "enrichment_mode": "shadow",
            "base_url": "https://api.deepseek.com",
            "path": "/chat/completions",
            "model": "deepseek-v4-flash",
            "model_revision": "deepseek-v4-flash",
            "temperature": 0.0,
            "top_p": 1.0,
            "json_mode": True,
            "num_predict": 16_384,
            "fusion_mode": "shadow",
            "fusion_batch_size": 20,
            "fusion_max_wait_seconds": 2.0,
            "fusion_max_queue_size": 1_000,
            "fusion_workers": 2,
        }
    }
    secret = {
        "chunk_inference_api_key": {
            "op": "set",
            "value": "synthetic-chunk-key",
        }
    }

    revision = store.stage(
        candidate,
        secret,
        expected_etag=initial.etag,
        idempotency_key="stage-deepseek-json-mode-001",
        actor="secret-admin",
        role="secret-admin",
    )
    private_environment = (store.revisions_dir / f"{revision.revision_id}.env").read_text(
        encoding="utf-8"
    )

    assert "PP_MEMORY_CHUNK_ENRICHMENT_TEMPERATURE=0" in private_environment
    assert "PP_MEMORY_CHUNK_ENRICHMENT_TOP_P=1" in private_environment
    assert "PP_MEMORY_CHUNK_ENRICHMENT_JSON_MODE=1" in private_environment
    assert "PP_MEMORY_CHUNK_ENRICHMENT_NUM_PREDICT=16384" in private_environment
    assert "PP_STRUCTURED_MEMORY_FUSION=shadow" in private_environment
    assert "PP_STRUCTURED_MEMORY_FUSION_BATCH_SIZE=20" in private_environment
    assert "PP_STRUCTURED_MEMORY_FUSION_MAX_WAIT_SECONDS=2" in private_environment
    assert "PP_STRUCTURED_MEMORY_FUSION_MAX_QUEUE=1000" in private_environment
    assert "PP_STRUCTURED_MEMORY_FUSION_WORKERS=2" in private_environment
    assert "PP_STRUCTURED_MEMORY_FUSION_LEASE_SECONDS=120" in private_environment
    assert "PP_STRUCTURED_MEMORY_FUSION_RETRY_DELAY_SECONDS=5" in private_environment
    assert "PP_STRUCTURED_MEMORY_FUSION_POLL_SECONDS=0.25" in private_environment


@pytest.mark.parametrize(
    ("chunk_patch", "reason"),
    [
        (
            {"fusion_mode": "shadow"},
            "control_fusion_requires_chunk_inference",
        ),
        (
            {
                "fusion_mode": "shadow",
                "enrichment_mode": "shadow",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
                "model_revision": "deepseek-v4-flash",
            },
            "control_chunk_inference_requires_structure_v1",
        ),
    ],
)
def test_fusion_configuration_requires_structured_chunk_inference(
    tmp_path,
    chunk_patch,
    reason,
):
    store = ControlPlaneConfigStore(
        tmp_path / "control",
        base_env={
            **_base_env(),
            "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST": "api.deepseek.com",
        },
    )

    with pytest.raises(ControlPlaneValidationError, match=reason):
        store.validate(
            {"chunk_inference": chunk_patch},
            {
                "chunk_inference_api_key": {
                    "op": "set",
                    "value": "synthetic-chunk-key",
                }
            },
        )


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        (
            {"temperature": 0.2, "top_p": 0.8},
            "control_chunk_inference_sampling_invalid",
        ),
        (
            {"json_mode": False},
            "control_chunk_inference_requires_json_mode",
        ),
    ],
)
def test_enabled_chunk_inference_rejects_unsafe_json_sampling(tmp_path, patch, reason):
    base_env = {
        **_base_env(),
        "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST": "api.deepseek.com",
    }
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=base_env)
    candidate = {
        "chunk_inference": {
            "chunking_mode": "structure-v1",
            "enrichment_mode": "shadow",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "model_revision": "deepseek-v4-flash",
            **patch,
        }
    }

    with pytest.raises(ControlPlaneValidationError, match=reason):
        store.validate(
            candidate,
            {
                "chunk_inference_api_key": {
                    "op": "set",
                    "value": "synthetic-chunk-key",
                }
            },
        )


def test_embedding_activation_requires_matching_evidence_and_is_atomic(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(
        root,
        base_env=_base_env(),
        generation_evidence_verifier=_verify_generation,
    )
    initial = store.safe_config()
    revision = store.stage(
        _embedding_patch(),
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-activate-001",
        actor="secret-admin",
        role="secret-admin",
    )

    with pytest.raises(
        ControlPlanePreconditionError,
        match="embedding_generation_required",
    ):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-missing-001",
            actor="secret-admin",
            role="secret-admin",
        )
    mismatched = _evidence(revision)
    mismatched["embedding_identity"] = "sha256:" + "0" * 64
    with pytest.raises(ControlPlaneConflictError, match="control_embedding_evidence_mismatch"):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-mismatch-001",
            actor="secret-admin",
            role="secret-admin",
            evidence=mismatched,
        )

    immutable_before = (root / "revisions" / f"{revision.revision_id}.env").read_bytes()
    result = store.activate(
        revision.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-valid-001",
        actor="secret-admin",
        role="secret-admin",
        evidence=_evidence(revision),
    )

    assert result.restart_required is True
    assert result.revision_id == revision.revision_id
    assert store.safe_config().revision_id == revision.revision_id
    assert store.safe_config().etag == revision.etag
    assert store.managed_env_path.read_bytes() == immutable_before
    assert _mode(store.managed_env_path) == 0o600
    assert (root / "revisions" / f"{revision.revision_id}.env").read_bytes() == immutable_before
    managed_environment = store.managed_env_path.read_text()
    assert "EMBEDDER_PROVIDER=openai-compatible" in managed_environment
    for bootstrap_name in (
        "PP_INFERENCE_GATEWAY_BIND",
        "PP_INFERENCE_GATEWAY_PROJECT_ID",
        "PP_INFERENCE_GATEWAY_TOKEN",
        "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST",
    ):
        assert f"{bootstrap_name}=" not in managed_environment


def test_operator_cannot_activate_revision_containing_secret_changes(tmp_path):
    store = ControlPlaneConfigStore(
        tmp_path / "control",
        base_env=_base_env(),
        generation_evidence_verifier=_verify_generation,
    )
    initial = store.safe_config()
    revision = store.stage(
        _embedding_patch(),
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-secret-activation-001",
        actor="secret-admin",
        role="secret-admin",
    )

    with pytest.raises(ControlPlaneAuthorizationError, match="control_role_insufficient"):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-secret-operator-001",
            actor="operator",
            role="operator",
            evidence=_evidence(revision),
        )

    assert store.safe_config().revision_id is None
    assert not store.managed_env_path.exists()


def test_activation_replay_precedes_stale_etag_check(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 40}},
        expected_etag=initial.etag,
        idempotency_key="stage-rerank-001",
        actor="operator",
        role="operator",
    )
    arguments = {
        "expected_etag": initial.etag,
        "idempotency_key": "activate-replay-001",
        "actor": "operator",
        "role": "operator",
    }

    first = store.activate(revision.revision_id, **arguments)
    replay = store.activate(revision.revision_id, **arguments)

    assert replay == first
    with pytest.raises(ControlPlaneConflictError, match="control_idempotency_key_conflict"):
        store.activate(
            revision.revision_id,
            **{**arguments, "actor": "another-operator"},
        )


def test_non_embedding_activation_rejects_unneeded_evidence(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 40}},
        expected_etag=initial.etag,
        idempotency_key="stage-no-evidence-001",
        actor="operator",
        role="operator",
    )
    accidental_secret = "sk-synthetic-must-not-enter-audit"

    with pytest.raises(
        ControlPlaneValidationError,
        match="control_embedding_evidence_not_applicable",
    ):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-no-evidence-001",
            actor="operator",
            role="operator",
            evidence={
                "revision_id": revision.revision_id,
                "embedding_identity": revision.embedding_identity,
                "provider_smoke": {"passed": True, "evidence_id": accidental_secret},
                "shadow_generation": {"passed": True, "generation_id": "unused"},
                "quality_gate": {"passed": True, "evidence_id": "unused"},
            },
        )

    assert accidental_secret not in store.database_path.read_bytes().decode(
        "utf-8",
        errors="ignore",
    )
    assert store.audit()[0].action == "config.stage"
    assert not store.managed_env_path.exists()


def test_revision_staged_from_old_base_cannot_be_activated_later(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())
    initial = store.safe_config()
    first = store.stage(
        {"rerank": {"max_candidates": 40}},
        expected_etag=initial.etag,
        idempotency_key="stage-first-001",
        actor="operator",
        role="operator",
    )
    stale = store.stage(
        {"rerank": {"max_candidates": 41}},
        expected_etag=initial.etag,
        idempotency_key="stage-second-001",
        actor="operator",
        role="operator",
    )
    store.activate(
        first.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-first-001",
        actor="operator",
        role="operator",
    )

    with pytest.raises(ControlPlaneConflictError, match="control_revision_stale"):
        store.activate(
            stale.revision_id,
            expected_etag=first.etag,
            idempotency_key="activate-stale-001",
            actor="operator",
            role="operator",
        )


def test_secret_rotation_retires_old_and_competing_material_without_breaking_replay(
    tmp_path,
):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(
        root,
        base_env=_base_env(),
        generation_evidence_verifier=_verify_generation,
    )
    initial = store.safe_config()
    old_secret = "synthetic-retired-embedding-secret"
    active_revision = store.stage(
        _embedding_patch(),
        _embedding_secret(old_secret),
        expected_etag=initial.etag,
        idempotency_key="stage-retired-embedding-secret",
        actor="secret-admin",
        role="secret-admin",
    )
    store.activate(
        active_revision.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-retired-embedding-secret",
        actor="secret-admin",
        role="secret-admin",
        evidence=_evidence(active_revision),
    )

    current = store.safe_config()
    competing_secret = "synthetic-losing-embedding-secret"
    competing_arguments = {
        "expected_etag": current.etag,
        "idempotency_key": "stage-losing-embedding-secret",
        "actor": "secret-admin",
        "role": "secret-admin",
    }
    competing = store.stage(
        {},
        _embedding_secret(competing_secret),
        **competing_arguments,
    )
    new_secret = "synthetic-current-embedding-secret"
    rotation_arguments = {
        "expected_etag": current.etag,
        "idempotency_key": "stage-current-embedding-secret",
        "actor": "secret-admin",
        "role": "secret-admin",
    }
    rotated = store.stage({}, _embedding_secret(new_secret), **rotation_arguments)
    active_path = store.revisions_dir / f"{active_revision.revision_id}.env"
    competing_path = store.revisions_dir / f"{competing.revision_id}.env"
    rotated_path = store.revisions_dir / f"{rotated.revision_id}.env"
    assert active_path.exists()
    assert competing_path.exists()
    assert rotated_path.exists()

    activation_arguments = {
        "expected_etag": current.etag,
        "idempotency_key": "activate-current-embedding-secret",
        "actor": "secret-admin",
        "role": "secret-admin",
    }
    activated = store.activate(rotated.revision_id, **activation_arguments)

    assert not active_path.exists()
    assert not competing_path.exists()
    assert rotated_path.exists()
    assert store.managed_env_path.read_bytes() == rotated_path.read_bytes()
    assert new_secret in rotated_path.read_text(encoding="utf-8")
    _assert_bytes_absent(root, old_secret)
    _assert_bytes_absent(root, competing_secret)
    assert {revision.revision_id for revision in store.list_revisions()} == {
        active_revision.revision_id,
        competing.revision_id,
        rotated.revision_id,
    }
    assert store.get_revision(active_revision.revision_id) == active_revision
    assert {event.revision_id for event in store.audit()} >= {
        active_revision.revision_id,
        competing.revision_id,
        rotated.revision_id,
    }

    stage_replay = store.stage({}, _embedding_secret(competing_secret), **competing_arguments)
    activation_replay = store.activate(rotated.revision_id, **activation_arguments)
    assert stage_replay == competing
    assert activation_replay == activated
    assert not competing_path.exists()
    with pytest.raises(ControlPlaneConflictError, match="control_revision_stale"):
        store.activate(
            competing.revision_id,
            expected_etag=rotated.etag,
            idempotency_key="activate-retired-losing-revision",
            actor="secret-admin",
            role="secret-admin",
        )


def test_secret_clear_retires_previous_plaintext_but_preserves_metadata(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    old_secret = "synthetic-rerank-secret-to-clear"
    configured = store.stage(
        {},
        {"rerank_api_key": {"op": "set", "value": old_secret}},
        expected_etag=initial.etag,
        idempotency_key="stage-rerank-secret-to-clear",
        actor="secret-admin",
        role="secret-admin",
    )
    store.activate(
        configured.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-rerank-secret-to-clear",
        actor="secret-admin",
        role="secret-admin",
    )
    current = store.safe_config()
    cleared = store.stage(
        {},
        {"rerank_api_key": {"op": "clear"}},
        expected_etag=current.etag,
        idempotency_key="stage-cleared-rerank-secret",
        actor="secret-admin",
        role="secret-admin",
    )
    store.activate(
        cleared.revision_id,
        expected_etag=current.etag,
        idempotency_key="activate-cleared-rerank-secret",
        actor="secret-admin",
        role="secret-admin",
    )

    configured_path = store.revisions_dir / f"{configured.revision_id}.env"
    cleared_path = store.revisions_dir / f"{cleared.revision_id}.env"
    assert not configured_path.exists()
    assert cleared_path.exists()
    assert "PP_RERANK_API_KEY=\n" in cleared_path.read_text(encoding="utf-8")
    assert store.safe_config().secrets["rerank_api_key"] is False
    assert store.get_revision(configured.revision_id).secrets["rerank_api_key"] is True
    assert store.get_revision(cleared.revision_id).secrets["rerank_api_key"] is False
    _assert_bytes_absent(root, old_secret)


def test_startup_retires_valid_orphan_but_fails_closed_on_unknown_entry(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    staged = store.stage(
        {"rerank": {"max_candidates": 39}},
        expected_etag=initial.etag,
        idempotency_key="stage-retained-current-baseline",
        actor="operator",
        role="operator",
    )
    staged_path = store.revisions_dir / f"{staged.revision_id}.env"
    orphan_path = store.revisions_dir / "cfg-20260724T000000Z-deadbeefcafe.env"
    orphan_path.write_text("synthetic-orphan-private-material\n", encoding="utf-8")
    orphan_path.chmod(0o600)

    reopened = ControlPlaneConfigStore(root, base_env=_base_env())
    assert staged_path.exists()
    assert reopened.get_revision(staged.revision_id) == staged
    assert not orphan_path.exists()

    blocked_orphan = reopened.revisions_dir / "cfg-20260724T000000Z-feedfacecafe.env"
    blocked_orphan.write_text("must-wait-for-clean-directory\n", encoding="utf-8")
    blocked_orphan.chmod(0o600)
    unexpected = reopened.revisions_dir / "unexpected-entry"
    unexpected.write_text("must-not-be-deleted\n", encoding="utf-8")
    with pytest.raises(
        ControlPlaneStorageError,
        match="control_revision_directory_unexpected_entry",
    ):
        ControlPlaneConfigStore(root, base_env=_base_env())
    assert unexpected.exists()
    assert blocked_orphan.exists()
    assert staged_path.exists()


def test_startup_retires_managed_env_temp_left_by_hard_crash(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    stale_secret = "synthetic-hard-crash-managed-env-secret"
    stale_temp = root / ".managed.env.dead-process"
    stale_temp.write_text(
        f"PP_EMBEDDING_API_KEY={stale_secret}\n",
        encoding="utf-8",
    )
    stale_temp.chmod(0o600)

    reopened = ControlPlaneConfigStore(root, base_env=_base_env())

    assert reopened.safe_config() == store.safe_config()
    assert not stale_temp.exists()
    _assert_bytes_absent(root, stale_secret)


def test_audit_contains_only_bounded_secret_metadata(tmp_path):
    secret = "audit-must-not-contain-this-secret"
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())
    revision = store.stage(
        _embedding_patch(),
        _embedding_secret(secret),
        expected_etag=store.safe_config().etag,
        idempotency_key="stage-audit-001",
        actor="secret-admin",
        role="secret-admin",
    )

    events = store.audit()
    serialized = json.dumps([event.to_dict() for event in events])
    assert secret not in serialized
    assert events[0].revision_id == revision.revision_id
    assert events[0].details["secret_fields_changed"] == ["embedding_api_key"]


def test_get_revision_rejects_unknown_revision(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())
    missing = "cfg-20260724T000000Z-000000000000"

    with pytest.raises(ControlPlaneNotFoundError, match="control_revision_not_found"):
        store.get_revision(missing)


@pytest.mark.parametrize(
    "phase",
    [
        "intent_committed",
        "managed_temp_fsynced",
        "managed_replaced",
        "managed_fsynced",
        "finalize_precommit",
    ],
)
def test_activation_recovers_each_precommit_crash_boundary(tmp_path, phase):
    root = tmp_path / phase
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 44}},
        expected_etag=initial.etag,
        idempotency_key=f"stage-{phase}",
        actor="operator",
        role="operator",
    )
    arguments = {
        "expected_etag": initial.etag,
        "idempotency_key": f"activate-{phase}",
        "actor": "operator",
        "role": "operator",
    }
    _install_crash(store, phase)

    with pytest.raises(_InjectedCrash, match=phase):
        store.activate(revision.revision_id, **arguments)

    assert (
        _scalar(
            store.database_path,
            "SELECT active_revision_id FROM control_state WHERE singleton = 1",
        )
        is None
    )
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 1
    assert (
        _scalar(
            store.database_path,
            "SELECT COUNT(*) FROM idempotency WHERE operation = 'activate'",
        )
        == 0
    )
    assert (
        _scalar(
            store.database_path,
            "SELECT COUNT(*) FROM audit_events WHERE action = 'config.activate'",
        )
        == 0
    )
    with pytest.raises(
        ControlPlaneStorageError,
        match="control_activation_recovery_required",
    ):
        store.safe_config()

    recovered = ControlPlaneConfigStore(root, base_env=_base_env())
    snapshot = recovered.safe_config()
    replay = recovered.activate(revision.revision_id, **arguments)

    assert snapshot.revision_id == revision.revision_id
    assert snapshot.etag == revision.etag
    assert replay.revision_id == revision.revision_id
    assert f"# revision={revision.revision_id}" in recovered.managed_env_path.read_text()
    assert _mode(recovered.managed_env_path) == 0o600
    assert _scalar(recovered.database_path, "SELECT COUNT(*) FROM activation_intent") == 0
    assert _scalar(recovered.database_path, "SELECT COUNT(*) FROM activations") == 1
    assert (
        _scalar(
            recovered.database_path,
            "SELECT COUNT(*) FROM idempotency WHERE operation = 'activate'",
        )
        == 1
    )
    assert (
        _scalar(
            recovered.database_path,
            "SELECT COUNT(*) FROM audit_events WHERE action = 'config.activate'",
        )
        == 1
    )
    assert recovered.audit()[0].details["recovered"] is True


def test_activation_commit_is_replayed_after_response_crash(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 45}},
        expected_etag=initial.etag,
        idempotency_key="stage-after-commit",
        actor="operator",
        role="operator",
    )
    arguments = {
        "expected_etag": initial.etag,
        "idempotency_key": "activate-after-commit",
        "actor": "operator",
        "role": "operator",
    }
    _install_crash(store, "finalize_committed")

    with pytest.raises(_InjectedCrash, match="finalize_committed"):
        store.activate(revision.revision_id, **arguments)

    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 0
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activations") == 1
    recovered = ControlPlaneConfigStore(root, base_env=_base_env())
    replay = recovered.activate(revision.revision_id, **arguments)
    stored = json.loads(
        _scalar(
            recovered.database_path,
            "SELECT result_json FROM idempotency WHERE operation = 'activate'",
        )
    )
    assert replay.to_dict() == stored
    assert recovered.audit()[0].details["recovered"] is False


def test_finalize_committed_recovery_retires_previous_secret_material(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(
        root,
        base_env=_base_env(),
        generation_evidence_verifier=_verify_generation,
    )
    initial = store.safe_config()
    old_secret = "synthetic-before-finalize-committed"
    previous = store.stage(
        _embedding_patch(),
        _embedding_secret(old_secret),
        expected_etag=initial.etag,
        idempotency_key="stage-before-finalize-committed",
        actor="secret-admin",
        role="secret-admin",
    )
    store.activate(
        previous.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-before-finalize-committed",
        actor="secret-admin",
        role="secret-admin",
        evidence=_evidence(previous),
    )
    current = store.safe_config()
    current_secret = "synthetic-after-finalize-committed"
    rotated = store.stage(
        {},
        _embedding_secret(current_secret),
        expected_etag=current.etag,
        idempotency_key="stage-after-finalize-committed",
        actor="secret-admin",
        role="secret-admin",
    )
    arguments = {
        "expected_etag": current.etag,
        "idempotency_key": "activate-after-finalize-committed",
        "actor": "secret-admin",
        "role": "secret-admin",
    }
    previous_path = store.revisions_dir / f"{previous.revision_id}.env"
    rotated_path = store.revisions_dir / f"{rotated.revision_id}.env"
    _install_crash(store, "finalize_committed")

    with pytest.raises(_InjectedCrash, match="finalize_committed"):
        store.activate(rotated.revision_id, **arguments)

    assert previous_path.exists()
    assert rotated_path.exists()
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 0
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activations") == 2

    recovered = ControlPlaneConfigStore(
        root,
        base_env=_base_env(),
        generation_evidence_verifier=_verify_generation,
    )
    replay = recovered.activate(rotated.revision_id, **arguments)
    assert replay.revision_id == rotated.revision_id
    assert recovered.safe_config().revision_id == rotated.revision_id
    assert not previous_path.exists()
    assert rotated_path.exists()
    assert recovered.managed_env_path.read_bytes() == rotated_path.read_bytes()
    assert current_secret in rotated_path.read_text(encoding="utf-8")
    _assert_bytes_absent(root, old_secret)
    assert (
        _scalar(
            store.database_path,
            "SELECT COUNT(*) FROM audit_events WHERE action = 'config.activate'",
        )
        == 2
    )


def test_stage_recovers_pending_activation_before_its_cas(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 46}},
        expected_etag=initial.etag,
        idempotency_key="stage-before-recovery",
        actor="operator",
        role="operator",
    )
    _install_crash(store, "intent_committed")
    with pytest.raises(_InjectedCrash):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-before-stage",
            actor="operator",
            role="operator",
        )
    store._activation_checkpoint = lambda _phase: None

    with pytest.raises(ControlPlanePreconditionError, match="control_etag_mismatch"):
        store.stage(
            {"rerank": {"max_candidates": 47}},
            expected_etag=initial.etag,
            idempotency_key="stage-after-recovery",
            actor="operator",
            role="operator",
        )

    assert store.safe_config().revision_id == revision.revision_id
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activations") == 1


@pytest.mark.parametrize(("name", "changed_value"), _BOOTSTRAP_DRIFT_CASES)
def test_startup_recovery_rejects_bootstrap_drift(tmp_path, name, changed_value):
    root = tmp_path / f"bootstrap-drift-{name.casefold()}"
    base_env = _base_env()
    store = ControlPlaneConfigStore(root, base_env=base_env)
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 47}},
        expected_etag=initial.etag,
        idempotency_key="stage-bootstrap-recovery",
        actor="operator",
        role="operator",
    )
    _install_crash(store, "intent_committed")
    with pytest.raises(_InjectedCrash, match="intent_committed"):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-bootstrap-recovery",
            actor="operator",
            role="operator",
        )

    changed_env = {**base_env, name: changed_value}
    with pytest.raises(ControlPlaneStorageError, match="control_bootstrap_boundary_changed"):
        ControlPlaneConfigStore(root, base_env=changed_env)

    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 1
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activations") == 0
    assert not store.managed_env_path.exists()


@pytest.mark.parametrize("tamper", ["wrong_hash", "wrong_mode", "wrong_marker"])
def test_startup_recovery_fails_closed_on_managed_env_conflict(tmp_path, tamper):
    root = tmp_path / tamper
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 48}},
        expected_etag=initial.etag,
        idempotency_key=f"stage-tamper-{tamper}",
        actor="operator",
        role="operator",
    )
    _install_crash(store, "managed_fsynced")
    with pytest.raises(_InjectedCrash):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key=f"activate-tamper-{tamper}",
            actor="operator",
            role="operator",
        )

    if tamper == "wrong_hash":
        store.managed_env_path.write_bytes(store.managed_env_path.read_bytes() + b"# changed\n")
    elif tamper == "wrong_mode":
        store.managed_env_path.chmod(0o644)
    else:
        content = store.managed_env_path.read_text()
        store.managed_env_path.write_text(
            content.replace(revision.revision_id, "cfg-20260724T000000Z-000000000000")
        )
        store.managed_env_path.chmod(0o600)

    with pytest.raises(ControlPlaneStorageError):
        ControlPlaneConfigStore(root, base_env=_base_env())
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 1
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activations") == 0


def test_embedding_recovery_persists_desired_generation_without_reverification(tmp_path):
    root = tmp_path / "control"
    calls = 0

    def verifier(revision, evidence):
        nonlocal calls
        calls += 1
        return _verify_generation(revision, evidence)

    store = ControlPlaneConfigStore(
        root,
        base_env=_base_env(),
        generation_evidence_verifier=verifier,
    )
    initial = store.safe_config()
    revision = store.stage(
        _embedding_patch(),
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-generation-recovery",
        actor="secret-admin",
        role="secret-admin",
    )
    arguments = {
        "expected_etag": initial.etag,
        "idempotency_key": "activate-generation-recovery",
        "actor": "secret-admin",
        "role": "secret-admin",
        "evidence": _evidence(revision),
    }
    _install_crash(store, "managed_fsynced")
    with pytest.raises(_InjectedCrash):
        store.activate(revision.revision_id, **arguments)
    assert calls == 1

    def must_not_verify(_revision, _evidence):
        raise AssertionError("recovery reran the generation verifier")

    recovered = ControlPlaneConfigStore(
        root,
        base_env=_base_env(),
        generation_evidence_verifier=must_not_verify,
    )
    snapshot = recovered.safe_config()
    replay = recovered.activate(revision.revision_id, **arguments)
    assert snapshot.desired_generation_id == "generation-001"
    assert snapshot.desired_generation_manifest_sha256 == "a" * 64
    assert replay.desired_generation_id == "generation-001"
    assert replay.desired_generation_manifest_sha256 == "a" * 64
    with sqlite3.connect(recovered.database_path) as connection:
        desired = connection.execute(
            """
            SELECT desired_generation_id, desired_generation_manifest_sha256
            FROM activations
            """
        ).fetchone()
    assert desired == ("generation-001", "a" * 64)

    ordinary = recovered.stage(
        {"rerank": {"max_candidates": 49}},
        expected_etag=snapshot.etag,
        idempotency_key="stage-preserve-generation",
        actor="operator",
        role="operator",
    )
    ordinary_result = recovered.activate(
        ordinary.revision_id,
        expected_etag=snapshot.etag,
        idempotency_key="activate-preserve-generation",
        actor="operator",
        role="operator",
    )
    assert ordinary_result.desired_generation_id == "generation-001"
    assert ordinary_result.desired_generation_manifest_sha256 == "a" * 64
    assert recovered.safe_config().desired_generation_id == "generation-001"


@pytest.mark.parametrize(
    "summary",
    [
        {
            "provider_smoke_evidence_id": "smoke-001",
            "shadow_generation_id": "generation-001",
            "quality_gate_evidence_id": "quality-001",
            "manifest_sha256": "A" * 64,
            "verified_generation": True,
        },
        {
            "provider_smoke_evidence_id": "smoke-001",
            "shadow_generation_id": "generation-001",
            "quality_gate_evidence_id": "quality-001",
            "manifest_sha256": "a" * 64,
        },
    ],
)
def test_embedding_verifier_summary_requires_verified_lowercase_manifest(tmp_path, summary):
    store = ControlPlaneConfigStore(
        tmp_path / "control",
        base_env=_base_env(),
        generation_evidence_verifier=lambda _revision, _evidence: summary,
    )
    initial = store.safe_config()
    revision = store.stage(
        _embedding_patch(),
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-invalid-generation-summary",
        actor="secret-admin",
        role="secret-admin",
    )
    with pytest.raises(
        ControlPlaneStorageError,
        match="control_embedding_evidence_verifier_invalid",
    ):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-invalid-generation-summary",
            actor="secret-admin",
            role="secret-admin",
            evidence=_evidence(revision),
        )
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 0
    assert not store.managed_env_path.exists()


def test_default_verifier_rejects_current_generation(tmp_path, monkeypatch):
    generation = SimpleNamespace(generation_id="generation-001")

    class FakeGenerationManager:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def load_manifest(self, _generation_id):
            return generation

        def current_manifest(self):
            return generation

    from plastic_promise.core import lancedb_generation

    monkeypatch.setattr(lancedb_generation, "GenerationManager", FakeGenerationManager)
    store = ControlPlaneConfigStore(
        tmp_path / "control",
        base_env={**_base_env(), "PLASTIC_LANCEDB_GENERATION_ROOT": str(tmp_path / "index")},
    )
    initial = store.safe_config()
    revision = store.stage(
        _embedding_patch(),
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-current-generation",
        actor="secret-admin",
        role="secret-admin",
    )

    with pytest.raises(
        ControlPlaneConflictError,
        match="control_embedding_generation_not_inactive",
    ):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-current-generation",
            actor="secret-admin",
            role="secret-admin",
            evidence=_evidence(revision),
        )


def test_schema_migrates_desired_generation_columns(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "ALTER TABLE control_state DROP COLUMN desired_generation_manifest_sha256"
        )
        connection.execute("ALTER TABLE control_state DROP COLUMN desired_generation_id")
        connection.execute("ALTER TABLE activations DROP COLUMN desired_generation_manifest_sha256")
        connection.execute("ALTER TABLE activations DROP COLUMN desired_generation_id")
        connection.commit()

    migrated = ControlPlaneConfigStore(root, base_env=_base_env())
    with sqlite3.connect(migrated.database_path) as connection:
        state_columns = {row[1] for row in connection.execute("PRAGMA table_info(control_state)")}
        activation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(activations)")
        }
        synchronous = connection.execute("PRAGMA synchronous").fetchone()
    assert {
        "desired_generation_id",
        "desired_generation_manifest_sha256",
    } <= state_columns
    assert {
        "desired_generation_id",
        "desired_generation_manifest_sha256",
    } <= activation_columns
    assert synchronous == (2,)


def test_schema_migrates_runtime_identity_and_leaves_legacy_bootstrap_unbound(tmp_path):
    root = tmp_path / "control"
    original_env = {
        **_base_env(),
        "EMBEDDER_CHUNK_CHARS": "321",
        "EMBEDDER_STRUCTURE_HARD_CHARS": "654",
    }
    store = ControlPlaneConfigStore(root, base_env=original_env)
    initial = store.safe_config()
    candidate = {
        **_embedding_patch(),
        "chunk_inference": {"chunking_mode": "structure-v1"},
    }
    revision = store.stage(
        candidate,
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-runtime-migration-001",
        actor="secret-admin",
        role="secret-admin",
    )
    with sqlite3.connect(store.database_path) as connection:
        replay = json.loads(
            connection.execute(
                "SELECT result_json FROM idempotency WHERE operation = 'stage'"
            ).fetchone()[0]
        )
        replay.pop("runtime_embedding_index_identity")
        connection.execute(
            "UPDATE idempotency SET result_json = ? WHERE operation = 'stage'",
            (json.dumps(replay),),
        )
        connection.execute("ALTER TABLE revisions DROP COLUMN runtime_embedding_index_identity")
        connection.execute("ALTER TABLE revisions DROP COLUMN bootstrap_boundary_sha256")
        connection.commit()

    migration_env = {
        **original_env,
        "EMBEDDER_CHUNK_CHARS": "777",
        "EMBEDDER_STRUCTURE_HARD_CHARS": "888",
    }
    from plastic_promise.control_plane.config_schema import bootstrap_boundary_sha256

    expected = "unbound:legacy-runtime-index-identity"
    expected_bootstrap = bootstrap_boundary_sha256(migration_env)
    migrated = ControlPlaneConfigStore(
        root,
        base_env=migration_env,
        generation_evidence_verifier=_verify_generation,
    )
    restored = migrated.get_revision(revision.revision_id)
    assert restored.runtime_embedding_index_identity == expected
    assert "bootstrap_boundary" not in json.dumps(restored.to_dict()) + repr(restored)
    assert not hasattr(restored, "bootstrap_boundary_sha256")
    assert (
        _scalar(
            migrated.database_path,
            "SELECT runtime_embedding_index_identity FROM revisions WHERE revision_id = ?",
            (revision.revision_id,),
        )
        == expected
    )
    legacy_boundary = _scalar(
        migrated.database_path,
        "SELECT bootstrap_boundary_sha256 FROM revisions WHERE revision_id = ?",
        (revision.revision_id,),
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", legacy_boundary)
    assert legacy_boundary != expected_bootstrap
    replayed = migrated.stage(
        candidate,
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-runtime-migration-001",
        actor="secret-admin",
        role="secret-admin",
    )
    assert replayed.revision_id == revision.revision_id
    assert replayed.runtime_embedding_index_identity == expected
    assert "bootstrap_boundary" not in json.dumps(replayed.to_dict()) + repr(replayed)

    with pytest.raises(ControlPlaneConflictError, match="control_bootstrap_boundary_changed"):
        migrated.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-runtime-migration-unbound",
            actor="secret-admin",
            role="secret-admin",
            evidence=_evidence(replayed),
        )

    rebound = migrated.stage(
        candidate,
        _embedding_secret(),
        expected_etag=initial.etag,
        idempotency_key="stage-runtime-migration-rebound",
        actor="secret-admin",
        role="secret-admin",
    )
    assert rebound.revision_id != revision.revision_id
    assert (
        _scalar(
            migrated.database_path,
            "SELECT bootstrap_boundary_sha256 FROM revisions WHERE revision_id = ?",
            (rebound.revision_id,),
        )
        == expected_bootstrap
    )
    result = migrated.activate(
        rebound.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-runtime-migration-rebound",
        actor="secret-admin",
        role="secret-admin",
        evidence=_evidence(rebound),
    )
    assert result.revision_id == rebound.revision_id


def test_schema_migration_treats_unbound_secret_metadata_as_secret_change(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 51}},
        expected_etag=initial.etag,
        idempotency_key="stage-before-secret-metadata-migration",
        actor="operator",
        role="operator",
    )
    assert revision.contains_secret_changes is False

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("ALTER TABLE revisions DROP COLUMN secret_change_metadata_bound")
        connection.commit()

    migrated = ControlPlaneConfigStore(root, base_env=_base_env())
    restored = migrated.get_revision(revision.revision_id)
    assert restored.contains_secret_changes is True
    with pytest.raises(ControlPlaneAuthorizationError, match="control_role_insufficient"):
        migrated.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-unbound-secret-metadata-operator",
            actor="operator",
            role="operator",
        )

    result = migrated.activate(
        revision.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-unbound-secret-metadata-admin",
        actor="secret-admin",
        role="secret-admin",
    )
    assert result.revision_id == revision.revision_id


def test_schema_migration_requires_secret_admin_to_rebind_inherited_active_secret(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(
        root,
        base_env=_base_env(),
        generation_evidence_verifier=_verify_generation,
    )
    initial = store.safe_config()
    secret = "synthetic-inherited-embedding-secret"
    revision = store.stage(
        _embedding_patch(),
        _embedding_secret(secret),
        expected_etag=initial.etag,
        idempotency_key="stage-active-secret-before-migration",
        actor="secret-admin",
        role="secret-admin",
    )
    store.activate(
        revision.revision_id,
        expected_etag=initial.etag,
        idempotency_key="activate-active-secret-before-migration",
        actor="secret-admin",
        role="secret-admin",
        evidence=_evidence(revision),
    )

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("ALTER TABLE revisions DROP COLUMN secret_change_metadata_bound")
        connection.commit()

    migrated = ControlPlaneConfigStore(
        root,
        base_env=_base_env(),
        generation_evidence_verifier=_verify_generation,
    )
    active = migrated.safe_config()
    safe_patch = {"rerank": {"max_candidates": 51}}
    with pytest.raises(ControlPlaneAuthorizationError, match="control_role_insufficient"):
        migrated.stage(
            safe_patch,
            expected_etag=active.etag,
            idempotency_key="stage-inherited-secret-operator",
            actor="operator",
            role="operator",
        )

    rebound = migrated.stage(
        safe_patch,
        expected_etag=active.etag,
        idempotency_key="stage-inherited-secret-admin",
        actor="secret-admin",
        role="secret-admin",
    )
    assert rebound.contains_secret_changes is False
    assert secret not in json.dumps(rebound.to_dict()) + repr(rebound)
    migrated.activate(
        rebound.revision_id,
        expected_etag=active.etag,
        idempotency_key="activate-inherited-secret-admin",
        actor="secret-admin",
        role="secret-admin",
    )

    rebound_active = migrated.safe_config()
    operator_revision = migrated.stage(
        {"rerank": {"max_candidates": 52}},
        expected_etag=rebound_active.etag,
        idempotency_key="stage-after-inherited-secret-rebind",
        actor="operator",
        role="operator",
    )
    assert operator_revision.contains_secret_changes is False


def test_schema_migration_rejects_operator_pending_intent_with_unbound_secrets(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 52}},
        expected_etag=initial.etag,
        idempotency_key="stage-before-pending-secret-migration",
        actor="operator",
        role="operator",
    )
    _install_crash(store, "intent_committed")
    with pytest.raises(_InjectedCrash, match="intent_committed"):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-before-pending-secret-migration",
            actor="operator",
            role="operator",
        )

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("ALTER TABLE revisions DROP COLUMN secret_change_metadata_bound")
        connection.commit()

    with pytest.raises(ControlPlaneStorageError, match="control_activation_intent_invalid"):
        ControlPlaneConfigStore(root, base_env=_base_env())
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 1
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activations") == 0
    assert not store.managed_env_path.exists()


def test_schema_migration_fails_closed_for_unbound_pending_activation(tmp_path):
    root = tmp_path / "control"
    store = ControlPlaneConfigStore(root, base_env=_base_env())
    initial = store.safe_config()
    revision = store.stage(
        {"rerank": {"max_candidates": 51}},
        expected_etag=initial.etag,
        idempotency_key="stage-legacy-pending-activation",
        actor="operator",
        role="operator",
    )
    _install_crash(store, "intent_committed")
    with pytest.raises(_InjectedCrash, match="intent_committed"):
        store.activate(
            revision.revision_id,
            expected_etag=initial.etag,
            idempotency_key="activate-legacy-pending-activation",
            actor="operator",
            role="operator",
        )

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("ALTER TABLE revisions DROP COLUMN bootstrap_boundary_sha256")
        connection.commit()

    with pytest.raises(ControlPlaneStorageError, match="control_bootstrap_boundary_changed"):
        ControlPlaneConfigStore(root, base_env=_base_env())

    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activation_intent") == 1
    assert _scalar(store.database_path, "SELECT COUNT(*) FROM activations") == 0
    assert not store.managed_env_path.exists()


def test_generation_retarget_rejects_invalid_id_without_state_change(tmp_path):
    store = ControlPlaneConfigStore(tmp_path / "control", base_env=_base_env())
    initial = store.safe_config()

    with pytest.raises(ControlPlaneValidationError, match="control_generation_id_invalid"):
        store.retarget_current_generation(
            "../outside-generation",
            manifest_sha256="a" * 64,
            expected_etag=initial.etag,
            idempotency_key="retarget-invalid-id-001",
            actor="operator",
            role="operator",
        )

    current = store.safe_config()
    assert current.etag == initial.etag
    assert current.desired_generation_id is None
    assert current.desired_generation_manifest_sha256 is None
    assert store.audit() == ()


def test_generation_retarget_is_audited_cas_and_idempotent(tmp_path, monkeypatch):
    manifest = "b" * 64
    store = ControlPlaneConfigStore(
        tmp_path / "control",
        base_env={
            **_base_env(),
            "PLASTIC_LANCEDB_GENERATION_ROOT": str(tmp_path / "generations"),
        },
    )
    initial = store.safe_config()
    _install_current_generation(
        monkeypatch,
        generation_id="generation-current",
        manifest_sha256=manifest,
        embedding_index_identity=runtime_embedding_index_identity(initial.config),
    )

    result = store.retarget_current_generation(
        "generation-current",
        manifest_sha256=manifest,
        expected_etag=initial.etag,
        idempotency_key="retarget-current-001",
        actor="operator",
        role="operator",
    )
    replay = store.retarget_current_generation(
        "generation-current",
        manifest_sha256=manifest,
        expected_etag=initial.etag,
        idempotency_key="retarget-current-001",
        actor="operator",
        role="operator",
    )

    current = store.safe_config()
    assert replay == result
    assert current.etag == result["etag"]
    assert current.desired_generation_id == "generation-current"
    assert current.desired_generation_manifest_sha256 == manifest
    assert store.list_revisions() == ()
    events = store.audit()
    assert len(events) == 1
    assert events[0].action == "config.generation_retarget"
    assert events[0].details["verified_current"] is True

    with pytest.raises(ControlPlanePreconditionError, match="control_etag_mismatch"):
        store.retarget_current_generation(
            "generation-current",
            manifest_sha256=manifest,
            expected_etag=initial.etag,
            idempotency_key="retarget-current-stale",
            actor="operator",
            role="operator",
        )
    with pytest.raises(ControlPlaneConflictError, match="control_idempotency_key_conflict"):
        store.retarget_current_generation(
            "generation-current",
            manifest_sha256="c" * 64,
            expected_etag=initial.etag,
            idempotency_key="retarget-current-001",
            actor="operator",
            role="operator",
        )
    with pytest.raises(ControlPlaneAuthorizationError, match="control_role_insufficient"):
        store.retarget_current_generation(
            "generation-current",
            manifest_sha256=manifest,
            expected_etag=current.etag,
            idempotency_key="retarget-current-viewer",
            actor="viewer",
            role="viewer",
        )


def test_generation_retarget_requires_publishable_current_quality(tmp_path, monkeypatch):
    manifest = "d" * 64
    store = ControlPlaneConfigStore(
        tmp_path / "control",
        base_env={
            **_base_env(),
            "PLASTIC_LANCEDB_GENERATION_ROOT": str(tmp_path / "generations"),
        },
    )
    initial = store.safe_config()
    _install_current_generation(
        monkeypatch,
        generation_id="generation-unverified",
        manifest_sha256=manifest,
        embedding_index_identity=runtime_embedding_index_identity(initial.config),
        quality_report={
            "gate": {"status": "fail"},
            "smoke": {"passed": True},
            "backend": {"fallback_used": False, "degraded_used": False},
        },
    )

    with pytest.raises(ControlPlaneConflictError, match="control_generation_quality_mismatch"):
        store.retarget_current_generation(
            "generation-unverified",
            manifest_sha256=manifest,
            expected_etag=initial.etag,
            idempotency_key="retarget-unverified-001",
            actor="operator",
            role="operator",
        )

    assert store.safe_config().etag == initial.etag
    assert store.audit() == ()
