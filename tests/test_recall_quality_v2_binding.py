from __future__ import annotations

import copy
from pathlib import Path

import pytest

from plastic_promise.core.fusion_policy import canonical_fusion_config_hash
from plastic_promise.core.lancedb_generation import (
    PromotionError,
    adapt_recall_quality_report,
    quality_report_generation_identity,
)
from plastic_promise.core.recall_experiment import heldout_report_contract
from scripts import benchmark_recall_quality

ROOT = Path(__file__).resolve().parents[1]
HELDOUT = ROOT / "tests/fixtures/recall_quality/v2-heldout.json"
HELDOUT_FINGERPRINT = "cbf31d0be739d7f4cbb5313be86a8b267e12addc729804414d9a968717a71036"


def _bound_report() -> tuple[dict[str, object], dict[str, object]]:
    config = {
        "k": 2,
        "channels": ["vector", "bm25", "fts"],
        "weights": {"vector": 0.55, "bm25": 0.30, "fts": 0.15},
        "windows": {"vector": 20, "bm25": 20, "fts": 20},
    }
    candidate_id = f"wrrf-v1:{canonical_fusion_config_hash(config)}"
    manifest: dict[str, object] = {
        "candidate_id": candidate_id,
        "candidate_dimension": "fusion_policy",
        "heldout_fingerprint": HELDOUT_FINGERPRINT,
        "manifest_hash": "a" * 64,
        "source_commit": "b" * 40,
        "dirty_fingerprint": "c" * 64,
        "comparison_environment_fingerprint": "e" * 64,
        "retrieval_configuration": {"index_text_policy": "legacy", "query_expansion": True},
        "embedding_configuration": {
            "provider": "cloud",
            "model": "text-embedding-v4",
            "model_revision": "revision-a",
            "dimension": 1024,
        },
        "dependency_versions": {"lancedb": "0.34.0", "pyarrow": "20.0.0"},
        "runtime_route": "python-http-mcp",
        "fusion_config": config,
    }
    report = benchmark_recall_quality.run_benchmark(
        dataset_path=HELDOUT,
        backend="deterministic",
        candidate="legacy",
        fusion_policy=candidate_id,
        warmup=1,
        repeat=3,
    )
    report.update(
        {
            "schema_version": "recall-quality-report/v2",
            "dataset_role": "held-out",
            "dataset_fingerprint": HELDOUT_FINGERPRINT,
            "candidate_dimension": "fusion_policy",
            "candidate_id": candidate_id,
            "manifest_hash": manifest["manifest_hash"],
            "fusion_config": config,
            "publishable_claim": True,
            "publishability_reason": "isolated live backend and store-recall-supply smoke passed",
            "isolated_corpus": {
                "seeded": True,
                "canonical_count": 15,
                "derived_count": 6,
                "eligible_count": 6,
            },
            "smoke": {
                "store": True,
                "recall": True,
                "supply": True,
                "verified_visible": True,
                "forbidden_hidden": True,
                "passed": True,
            },
            "public_call_counts": {"memory_recall": 6, "context_supply": 6},
            "public_transport_call_counts": {"memory_recall": 24, "context_supply": 24},
            "fusion_attestation": {
                "attested_calls": 48,
                "errors": [],
                "observed": [candidate_id, "python", "python"],
                "algorithm": "weighted-rrf-v1",
                "config": {
                    **config,
                    "config_hash": candidate_id.partition(":")[2],
                },
            },
        }
    )
    backend = report["backend"]
    assert isinstance(backend, dict)
    backend.update(
        {
            "mode": "live",
            "deterministic": False,
            "fallback_used": False,
            "degraded_used": False,
            "transport": "streamable-http",
            "server_pid": 1234,
            "requested_policy": candidate_id,
            "effective_policy": candidate_id,
            "requested_runtime": "python",
            "effective_runtime": "python",
            "runtime_route": "python-http-mcp",
            "provider": "cloud",
            "model": "text-embedding-v4",
            "model_revision": "revision-a",
            "dimension": 1024,
            "usage": {
                "embedding_requests": 3,
                "embedding_input_tokens": 100,
                "cost_usd": 0.1,
                "pricing_revision": "pricing-v1",
            },
            "index_text_policy": "legacy",
            "channel_result_names": ["bm25", "vector", "fused"],
        }
    )
    report["usage"] = copy.deepcopy(backend["usage"])
    environment = report["environment"]
    assert isinstance(environment, dict)
    environment.update(
        {
            "provider": "cloud",
            "configured_model": "text-embedding-v4",
            "configured_model_revision": "revision-a",
            "supply_runtime": "python",
            "code_revision": "b" * 40,
            "source_commit": "b" * 40,
            "dirty_fingerprint": "c" * 64,
            "comparison_environment_fingerprint": manifest["comparison_environment_fingerprint"],
            "source_fingerprint": "d" * 64,
            "source_files": ["tests/fixtures/recall_quality/v2-heldout.json"],
            "dataset_source": "tests/fixtures/recall_quality/v2-heldout.json",
            "dependencies": manifest["dependency_versions"],
            "retrieval_configuration": manifest["retrieval_configuration"],
            "embedding_configuration": manifest["embedding_configuration"],
        }
    )
    report["server_logs"] = {}
    return report, manifest


def test_bound_v2_report_is_normalized_with_live_evidence():
    report, manifest = _bound_report()
    normalized = adapt_recall_quality_report(report, candidate_manifest=manifest)

    assert normalized["benchmark"]["candidate_id"] == manifest["candidate_id"]
    assert normalized["backend"]["provider"] == "openai-compatible"
    assert normalized["backend"]["rust_runtime"] is None
    assert len(normalized["environment"]["environment_fingerprint"]) == 64
    assert len(normalized["environment"]["comparison_environment_fingerprint"]) == 64
    assert normalized["environment"]["embedding_configuration"]["provider"] == ("openai-compatible")
    assert normalized["environment"]["embedding_configuration"]["dimension"] == 1024
    assert normalized["usage"]["cost_currency"] == "USD"
    assert normalized["usage"]["cost"] == normalized["usage"]["cost_usd"] == 0.1


def test_bound_v2_report_preserves_explicit_cny_cost():
    report, manifest = _bound_report()
    cny_usage = {
        "embedding_requests": 3,
        "embedding_input_tokens": 100,
        "cost": 0.000003,
        "cost_currency": "CNY",
        "cost_usd": None,
        "pricing_revision": "syuan-pricing-2026-07-24",
    }
    report["backend"]["usage"] = copy.deepcopy(cny_usage)
    report["usage"] = copy.deepcopy(cny_usage)

    normalized = adapt_recall_quality_report(report, candidate_manifest=manifest)

    assert normalized["usage"] == cny_usage


def _rust_bound_report() -> dict[str, object]:
    report, _manifest = _bound_report()
    candidate_id = report["candidate_id"]
    backend = report["backend"]
    assert isinstance(backend, dict)
    backend.update(
        requested_runtime="rust",
        effective_runtime="rust",
        runtime_route="rust-http-mcp",
        rust_runtime={
            "module": "context_engine_core",
            "version": "0.1.0",
            "binary_sha256": "1" * 64,
            "source_sha256": "2" * 64,
        },
    )
    fusion_attestation = report["fusion_attestation"]
    assert isinstance(fusion_attestation, dict)
    fusion_attestation["observed"] = [candidate_id, "rust", "rust"]
    environment = report["environment"]
    assert isinstance(environment, dict)
    environment["supply_runtime"] = "auto"
    return report


def test_bound_v2_report_preserves_strict_rust_build_identity():
    normalized = adapt_recall_quality_report(_rust_bound_report())

    assert normalized["backend"]["rust_runtime"] == {
        "module": "context_engine_core",
        "version": "0.1.0",
        "binary_sha256": "1" * 64,
        "source_sha256": "2" * 64,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report["backend"].update(rust_runtime=None),
        lambda report: report["backend"]["rust_runtime"].update(binary_sha256="1" * 63),
        lambda report: report["backend"]["rust_runtime"].update(source_sha256="A" * 64),
        lambda report: report["backend"]["rust_runtime"].update(module="foreign_extension"),
        lambda report: report["backend"]["rust_runtime"].update(extra="unbound"),
    ],
)
def test_bound_v2_report_rejects_missing_or_malformed_rust_build_identity(mutation):
    report = _rust_bound_report()
    mutation(report)

    with pytest.raises(PromotionError, match="recall_quality_backend_runtime_invalid"):
        adapt_recall_quality_report(report)


def test_bound_v2_python_report_rejects_rust_build_identity():
    report, manifest = _bound_report()
    report["backend"]["rust_runtime"] = {
        "module": "context_engine_core",
        "version": "0.1.0",
        "binary_sha256": "1" * 64,
        "source_sha256": "2" * 64,
    }

    with pytest.raises(PromotionError, match="recall_quality_backend_runtime_invalid"):
        adapt_recall_quality_report(report, candidate_manifest=manifest)


def _manifestless_max_v1_report() -> dict[str, object]:
    report, _manifest = _bound_report()
    report.update(
        candidate_id="max-v1",
        manifest_hash="",
        fusion_config=None,
    )
    backend = report["backend"]
    assert isinstance(backend, dict)
    backend.update(
        requested_policy="max-v1",
        effective_policy="max-v1",
        requested_runtime="python",
        effective_runtime="python",
        runtime_route="python-http-mcp",
    )
    report["fusion_attestation"] = {
        "attested_calls": 48,
        "errors": [],
        "observed": ["max-v1", "python", "python"],
        "algorithm": "weighted-max-v1",
        "config": None,
    }
    environment = report["environment"]
    assert isinstance(environment, dict)
    environment.update(
        {
            "supply_runtime": "python",
            "code_revision": environment["source_commit"],
            "dataset_source": "tests/fixtures/recall_quality/v2-heldout.json",
            "retrieval_configuration": {
                "index_text_policy": "legacy",
                "PP_VECTOR_WEIGHT": "0.50",
                "PP_QUERY_EXPANSION": "1",
                "PP_FTS_DISABLED": "0",
                "PP_FTS_FUSION": "1",
            },
        }
    )
    return report


def test_manifestless_max_v1_heldout_report_is_valid_generation_evidence():
    normalized = adapt_recall_quality_report(_manifestless_max_v1_report())

    assert normalized["benchmark"]["candidate_id"] == "max-v1"
    assert normalized["benchmark"]["manifest_hash"] == ""
    assert normalized["benchmark"]["fusion_config"] is None
    assert normalized["backend"]["requested_policy"] == "max-v1"
    assert normalized["backend"]["effective_policy"] == "max-v1"
    assert normalized["backend"]["requested_runtime"] == "python"
    assert normalized["backend"]["effective_runtime"] == "python"
    assert normalized["backend"]["runtime_route"] == "python-http-mcp"
    assert normalized["fusion_attestation"] == {
        "attested_calls": 48,
        "errors": [],
        "observed": ["max-v1", "python", "python"],
        "algorithm": "weighted-max-v1",
        "config": None,
    }
    assert normalized["environment"]["dataset_source"] == (
        "tests/fixtures/recall_quality/v2-heldout.json"
    )
    assert (
        normalized["environment"]["code_revision"] == (normalized["environment"]["source_commit"])
    )


def test_generation_adapter_accepts_public_seed_transport_attestation():
    report = _manifestless_max_v1_report()
    report["isolated_corpus"]["seed_transport"] = "public-memory-tools"

    normalized = adapt_recall_quality_report(report)

    assert normalized["publishable_claim"] is True


def test_generation_adapter_rejects_nonpublic_seed_transport():
    report = _manifestless_max_v1_report()
    report["isolated_corpus"]["seed_transport"] = "direct-sqlite"

    with pytest.raises(PromotionError, match="recall_quality_isolated_corpus_invalid"):
        adapt_recall_quality_report(report)


def test_generation_adapter_accepts_public_setup_call_counts():
    report = _manifestless_max_v1_report()
    report["public_call_counts"].update(
        memory_store=15,
        memory_update=2,
        feedback_apply=2,
    )

    normalized = adapt_recall_quality_report(report)

    assert normalized["publishable_claim"] is True


def test_generation_adapter_rejects_unknown_public_setup_call():
    report = _manifestless_max_v1_report()
    report["public_call_counts"]["direct_sqlite_write"] = 1

    with pytest.raises(PromotionError, match="recall_quality_public_call_evidence_invalid"):
        adapt_recall_quality_report(report)


def test_generation_adapter_accepts_v2_channel_split_aliases():
    report = _manifestless_max_v1_report()
    for channel in report["metrics"]["channels"].values():
        channel["by_language"] = channel.pop("language")
        channel["by_group"] = channel.pop("group")
    original = copy.deepcopy(report)

    normalized = adapt_recall_quality_report(report)

    assert normalized["publishable_claim"] is True
    assert report == original


def test_generation_adapter_rejects_ambiguous_channel_split_aliases():
    report = _manifestless_max_v1_report()
    report["metrics"]["channels"]["fused"]["by_language"] = copy.deepcopy(
        report["metrics"]["channels"]["fused"]["language"]
    )

    with pytest.raises(PromotionError, match="recall_quality_metrics_invalid"):
        adapt_recall_quality_report(report)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report["fusion_attestation"].update(
            observed=["legacy-auto", "rust", "rust"],
            algorithm="legacy-route-dependent",
        ),
        lambda report: report["fusion_attestation"].update(algorithm="legacy-route-dependent"),
        lambda report: (
            report["backend"].update(
                requested_runtime="rust",
                effective_runtime="rust",
                runtime_route="rust-http-mcp",
            ),
            report["fusion_attestation"].update(observed=["max-v1", "rust", "rust"]),
            report["environment"].update(supply_runtime="auto"),
        ),
        lambda report: report["backend"].update(runtime_route="rust-http-mcp"),
        lambda report: report["environment"]["retrieval_configuration"].update(
            PP_VECTOR_WEIGHT="0.99"
        ),
        lambda report: report["environment"]["retrieval_configuration"].update(
            arbitrary_switch="1"
        ),
        lambda report: report["environment"].update(code_revision="f" * 40),
        lambda report: report["environment"]["dependencies"].update(lancedb="unavailable"),
        lambda report: report["environment"]["dependencies"].update(extra="1.0"),
        lambda report: (
            report["backend"].update(dimension=8),
            report["environment"]["embedding_configuration"].update(dimension=8),
        ),
    ],
)
def test_manifestless_max_v1_control_rejects_relabelled_or_unbound_evidence(mutation):
    report = _manifestless_max_v1_report()
    mutation(report)

    with pytest.raises(PromotionError):
        adapt_recall_quality_report(report)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report["fusion_attestation"].update(
            observed=["legacy-auto", "rust", "rust"],
            algorithm="legacy-route-dependent",
        ),
        lambda report: report["backend"].update(effective_runtime="rust"),
        lambda report: report["backend"].update(runtime_route="rust-http-mcp"),
        lambda report: report["environment"]["retrieval_configuration"].update(
            PP_QUERY_EXPANSION="0"
        ),
        lambda report: report["environment"]["dependencies"].update(lancedb="unavailable"),
        lambda report: report["environment"]["embedding_configuration"].update(dimension=8),
    ],
)
def test_normalized_max_v1_control_is_revalidated_fail_closed(mutation):
    normalized = adapt_recall_quality_report(_manifestless_max_v1_report())
    mutation(normalized)

    with pytest.raises(PromotionError):
        quality_report_generation_identity(normalized)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report["fusion_attestation"].update(attested_calls=1),
        lambda report: report["corpus"].update(sha256="0" * 64),
        lambda report: report["corpus"].update(count=16),
        lambda report: report["corpus"].update(revision="forged-revision"),
        lambda report: report["corpus"].update(provenance_revision="forged-provenance"),
        lambda report: report["cases"].update(sha256="0" * 64),
        lambda report: report["cases"].update(count=7),
    ],
)
def test_normalized_v2_rebinds_complete_heldout_contract_and_call_count(mutation):
    normalized = adapt_recall_quality_report(_manifestless_max_v1_report())
    mutation(normalized)

    with pytest.raises(PromotionError):
        quality_report_generation_identity(normalized)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(candidate_id="legacy-auto"),
        lambda report: report.update(manifest_hash="a" * 64),
        lambda report: report.update(fusion_config={"k": 2}),
    ],
)
def test_manifestless_control_report_rejects_noncanonical_binding(mutation):
    report = _manifestless_max_v1_report()
    mutation(report)

    with pytest.raises(PromotionError):
        adapt_recall_quality_report(report)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(dataset_fingerprint="0" * 64),
        lambda report: report["fusion_config"].update(k=3),
        lambda report: report["metrics"]["cases"].reverse(),
        lambda report: report.update(manifest_hash="e" * 64),
        lambda report: report["environment"].update(comparison_environment_fingerprint="f" * 64),
        lambda report: report["environment"]["embedding_configuration"].update(dimension=8),
    ],
)
def test_bound_v2_report_rejects_tampering(mutation):
    report, manifest = _bound_report()
    mutation(report)
    with pytest.raises(PromotionError):
        adapt_recall_quality_report(report, candidate_manifest=manifest)


def test_unknown_heldout_contract_is_not_accepted():
    assert heldout_report_contract("0" * 64) is None
