from __future__ import annotations

import copy
import inspect
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from plastic_promise.core import recall_experiment as recall_experiment_module
from plastic_promise.core.fusion_policy import canonical_fusion_config_hash
from plastic_promise.core.recall_experiment import (
    CALIBRATION_GRID_SCHEMA,
    FrozenCandidateManifest,
    calibrate_and_freeze,
    canonical_json,
    compare_fusion_reports,
    dataset_fingerprint,
    freeze_candidate_manifest,
    load_calibration_grid,
    load_frozen_manifest,
    load_heldout_result,
    opaque_file_fingerprint,
    select_calibration_candidate,
    validate_heldout_separation,
    validate_manifest_runtime,
)
from plastic_promise.core.recall_quality import load_dataset_bundle

FIXTURES = Path(__file__).parent / "fixtures" / "recall_quality"
GRID_PATH = FIXTURES / "wrrf-v1-grid.json"
V1_PATH = FIXTURES / "v1.json"
V2_PATH = FIXTURES / "v2-heldout.json"

PREREGISTERED_GRID = {
    "schema": "wrrf-calibration-grid/v1",
    "k_values": [2],
    "weight_sets": [
        {"vector": 0.55, "bm25": 0.30, "fts": 0.15},
        {"vector": 0.60, "bm25": 0.25, "fts": 0.15},
        {"vector": 0.65, "bm25": 0.20, "fts": 0.15},
    ],
    "channel_windows": [
        {"vector": 20, "bm25": 20, "fts": 20},
        {"vector": 32, "bm25": 24, "fts": 16},
    ],
    "primary_quality_metric": "overall.fused.mrr",
    "minimum_primary_delta": 0.01,
    "required_split_tolerance": 0.0,
    "max_p95_ratio": 1.20,
    "selection_order": [
        "maximize minimum required-split fused MRR",
        "maximize overall fused hit@5",
        "maximize overall fused MRR",
        "minimize p95 latency",
        "lexicographically smallest canonical config JSON",
    ],
}


def _config(weight_index=0, window_index=0):
    return {
        "k": 2,
        "channels": ["vector", "bm25", "fts"],
        "weights": PREREGISTERED_GRID["weight_sets"][weight_index],
        "windows": PREREGISTERED_GRID["channel_windows"][window_index],
    }


def _report(weight_index=0, window_index=0, *, mrr=0.8, hit5=0.9, minimum=0.7, p95=10.0):
    return {
        "schema_version": "recall-quality-report/v2",
        "dataset_role": "calibration",
        "dataset_fingerprint": dataset_fingerprint(load_dataset_bundle(V1_PATH)),
        "candidate_dimension": "fusion_policy",
        "publishable_claim": True,
        "backend": {
            "mode": "live",
            "transport": "streamable-http",
            "runtime_route": "python-http-mcp",
            "requested_runtime": "rust-full",
            "effective_runtime": "rust-full",
        },
        "environment": {
            "source_commit": "a" * 40,
            "dirty_fingerprint": "b" * 64,
            "retrieval_configuration": {
                "query_expansion": True,
                "index_text_policy": "legacy",
                "channels": {"vector": {"window": 32}},
            },
            "embedding_configuration": {"model": "fixture", "dimension": 8},
            "dependencies": {"lancedb": "0.34.0", "pyarrow": "20.0.0"},
        },
        "fusion_config": _config(weight_index, window_index),
        "calibration_gate": {
            "overall_no_regression": True,
            "required_splits_no_regression": True,
            "best_constituent_no_regression": True,
            "forbidden_hits_not_increased": True,
            "no_degradation": True,
            "latency_within_budget": True,
        },
        "selection_metrics": {
            "minimum_required_split_fused_mrr": minimum,
            "overall_fused_hit_at_5": hit5,
            "overall_fused_mrr": mrr,
            "p95_ms": p95,
        },
    }


def _manifest_fields():
    return {
        "source_commit": "a" * 40,
        "dirty_fingerprint": "b" * 64,
        "retrieval_configuration": {
            "query_expansion": True,
            "index_text_policy": "legacy",
            "channels": {"vector": {"window": 32}},
        },
        "embedding_configuration": {"model": "fixture", "dimension": 8},
        "dependency_versions": {"lancedb": "0.34.0", "pyarrow": "20.0.0"},
        "runtime_route": "python-http-mcp",
    }


def _frozen_manifest():
    return freeze_candidate_manifest(
        selected_report=_report(),
        grid=PREREGISTERED_GRID,
        calibration=load_dataset_bundle(V1_PATH),
        heldout_fingerprint=opaque_file_fingerprint(V2_PATH),
        **_manifest_fields(),
    )


def test_preregistered_grid_has_exact_canonical_content():
    grid = load_calibration_grid(GRID_PATH)
    assert grid["schema"] == CALIBRATION_GRID_SCHEMA
    assert grid == PREREGISTERED_GRID
    assert canonical_json(grid) == canonical_json(PREREGISTERED_GRID)


def test_preregistered_grid_cannot_drift_from_authoritative_plan():
    plan_path = (
        Path(__file__).parents[1]
        / "docs"
        / "superpowers"
        / "plans"
        / "2026-07-12-corrective-governed-retrieval-plan.md"
    )
    plan = plan_path.read_text(encoding="utf-8")
    match = re.search(
        r"## Preregistered Fusion Search.*?```json\s*(\{.*?\})\s*```",
        plan,
        flags=re.DOTALL,
    )
    assert match is not None
    assert canonical_json(json.loads(match.group(1))) == canonical_json(
        load_calibration_grid(GRID_PATH)
    )


def test_selector_is_order_independent_and_uses_preregistered_objective():
    reports = [
        _report(0, 0, minimum=0.70, hit5=0.99, mrr=0.99, p95=1.0),
        _report(1, 0, minimum=0.72, hit5=0.91, mrr=0.82, p95=12.0),
        _report(2, 0, minimum=0.72, hit5=0.92, mrr=0.81, p95=15.0),
        _report(2, 1, minimum=0.72, hit5=0.92, mrr=0.83, p95=20.0),
        _report(1, 1, minimum=0.72, hit5=0.92, mrr=0.83, p95=9.0),
    ]
    expected = select_calibration_candidate(reports, PREREGISTERED_GRID)
    for seed in range(8):
        shuffled = list(reports)
        random.Random(seed).shuffle(shuffled)
        assert (
            select_calibration_candidate(shuffled, PREREGISTERED_GRID)["candidate_id"]
            == expected["candidate_id"]
        )
    assert expected["fusion_config"] == _config(1, 1)


def test_candidate_id_is_hash_of_canonical_config():
    selected = select_calibration_candidate([_report()], PREREGISTERED_GRID)
    assert selected["candidate_id"] == "wrrf-v1:" + canonical_fusion_config_hash(_config())


def test_no_survivor_fails_without_manifest_write(tmp_path):
    report = _report()
    report["calibration_gate"]["no_degradation"] = False
    manifest_path = tmp_path / "manifest.json"
    with pytest.raises(ValueError, match="no_calibration_candidate"):
        calibrate_and_freeze(
            reports=[report],
            grid=PREREGISTERED_GRID,
            calibration=load_dataset_bundle(V1_PATH),
            heldout_fingerprint=opaque_file_fingerprint(V2_PATH),
            manifest_path=manifest_path,
            **_manifest_fields(),
        )
    assert not manifest_path.exists()


def test_v2_heldout_is_bilingual_separate_and_covers_required_scenarios():
    calibration = load_dataset_bundle(V1_PATH)
    first = load_dataset_bundle(V2_PATH)
    second = load_dataset_bundle(V2_PATH)
    validate_heldout_separation(calibration, first)
    assert first.evidence_role == "held-out"
    assert {case.language for case in first.cases} == {"en", "zh", "cross-lingual"}
    assert {case.group for case in first.cases} == {
        "token-overlap",
        "partial-overlap",
        "zero-overlap",
    }
    scenarios = {item for case in first.cases for item in case.metadata.get("scenarios", [])}
    assert {
        "identifier",
        "same-domain-distractor",
        "draft-synthesis",
        "contested-synthesis",
        "stale-synthesis",
        "source-change-invalidation",
    } <= scenarios
    assert first.corpus_hash == second.corpus_hash
    assert first.case_hash == second.case_hash
    assert dataset_fingerprint(first) == dataset_fingerprint(second)


def test_known_heldout_report_contract_matches_frozen_fixture():
    heldout = load_dataset_bundle(V2_PATH)
    contract = recall_experiment_module._KNOWN_HELDOUT_REPORT_CONTRACTS[
        opaque_file_fingerprint(V2_PATH)
    ]

    assert dict(contract["fields"]) == {
        "dataset_schema_version": heldout.schema_version,
        "dataset_revision": heldout.dataset_revision,
        "corpus.revision": heldout.corpus_revision,
        "corpus.provenance_revision": heldout.synthesis_provenance_revision,
        "corpus.sha256": heldout.corpus_hash,
        "corpus.count": heldout.corpus_count,
        "cases.sha256": heldout.case_hash,
        "cases.count": heldout.case_count,
    }
    assert tuple(contract["case_identities"]) == tuple(
        (case.case_id, case.language, case.group) for case in heldout.cases
    )


def test_opaque_fingerprint_is_line_ending_invariant_but_content_sensitive(tmp_path):
    lf = tmp_path / "heldout-lf.json"
    crlf = tmp_path / "heldout-crlf.json"
    changed = tmp_path / "heldout-changed.json"
    lf.write_bytes(b'{"heldout":true}\n{"case":1}\n')
    crlf.write_bytes(b'{"heldout":true}\r\n{"case":1}\r\n')
    changed.write_bytes(b'{"heldout":false}\n{"case":1}\n')

    assert opaque_file_fingerprint(lf) == opaque_file_fingerprint(crlf)
    assert opaque_file_fingerprint(lf) != opaque_file_fingerprint(changed)


def test_v1_is_rejected_as_heldout_evidence():
    calibration = load_dataset_bundle(V1_PATH)
    assert calibration.evidence_role == "calibration"
    with pytest.raises(ValueError, match="heldout_evidence_role_required"):
        validate_heldout_separation(calibration, calibration)


def test_manifest_must_exist_before_heldout_result_can_be_loaded(tmp_path):
    result = tmp_path / "heldout.json"
    result.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="candidate_manifest_required_before_heldout"):
        load_heldout_result(result, manifest_path=None, expected_manifest_hash="a" * 64)
    with pytest.raises(ValueError, match="candidate_manifest_required_before_heldout"):
        load_heldout_result(
            result,
            manifest_path=tmp_path / "missing.json",
            expected_manifest_hash="a" * 64,
        )


def test_calibration_freeze_hashes_but_never_retrieves_heldout(tmp_path):
    calibration = load_dataset_bundle(V1_PATH)
    calibration_calls = []

    def calibration_retrieve(case):
        calibration_calls.append(case.case_id)

    manifest_path = tmp_path / "manifest.json"
    manifest = calibrate_and_freeze(
        reports=[_report()],
        grid=PREREGISTERED_GRID,
        calibration=calibration,
        heldout_fingerprint=opaque_file_fingerprint(V2_PATH),
        manifest_path=manifest_path,
        calibration_retrieve=calibration_retrieve,
        **_manifest_fields(),
    )
    assert calibration_calls == [case.case_id for case in calibration.cases]
    assert manifest.heldout_fingerprint == opaque_file_fingerprint(V2_PATH)
    assert load_frozen_manifest(manifest_path) == manifest
    assert "heldout" not in inspect.signature(calibrate_and_freeze).parameters
    assert "heldout" not in inspect.signature(freeze_candidate_manifest).parameters


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_commit", "c" * 40),
        ("dirty_fingerprint", "d" * 64),
        ("runtime_route", "rust-http-mcp"),
        ("retrieval_configuration", {"query_expansion": False}),
        ("embedding_configuration", {"model": "other", "dimension": 8}),
        ("dependency_versions", {"lancedb": "0.35.0", "pyarrow": "20.0.0"}),
    ],
)
def test_manifest_runtime_drift_fails_closed(field, replacement):
    manifest = _frozen_manifest()
    values = _manifest_fields()
    values[field] = replacement
    with pytest.raises(ValueError, match=f"candidate_manifest_runtime_mismatch:{field}"):
        validate_manifest_runtime(
            manifest,
            calibration=load_dataset_bundle(V1_PATH),
            heldout_fingerprint=opaque_file_fingerprint(V2_PATH),
            **values,
        )


@pytest.mark.parametrize(
    "versions",
    [
        {"lancedb": "0.34.0"},
        {"lancedb": "0.34.0", "pyarrow": "20.0.0", "unknown": "1"},
        {"lancedb": "unknown", "pyarrow": "20.0.0"},
    ],
)
def test_unknown_or_missing_dependency_versions_are_rejected(versions):
    fields = _manifest_fields()
    fields["dependency_versions"] = versions
    with pytest.raises(ValueError, match="dependency"):
        freeze_candidate_manifest(
            selected_report=_report(),
            grid=PREREGISTERED_GRID,
            calibration=load_dataset_bundle(V1_PATH),
            heldout_fingerprint=opaque_file_fingerprint(V2_PATH),
            **fields,
        )


def test_unsupported_candidate_dimension_is_rejected():
    with pytest.raises(ValueError, match="dimension_unsupported"):
        freeze_candidate_manifest(
            selected_report=_report(),
            grid=PREREGISTERED_GRID,
            calibration=load_dataset_bundle(V1_PATH),
            heldout_fingerprint=opaque_file_fingerprint(V2_PATH),
            candidate_dimension="embedding_model",
            **_manifest_fields(),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(source_commit="f" * 40),
        lambda value: value["fusion_config"]["weights"].update(vector=0.7),
        lambda value: value.update(runtime_route="other"),
        lambda value: value.update(manifest_hash="0" * 64),
    ],
)
def test_manifest_source_config_and_hash_drift_are_rejected(mutation):
    value = copy.deepcopy(_frozen_manifest().to_dict())
    mutation(value)
    with pytest.raises(ValueError):
        FrozenCandidateManifest.from_dict(value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(dataset_role="held-out"),
        lambda report: report.update(dataset_fingerprint="0" * 64),
        lambda report: report.update(publishable_claim=False),
        lambda report: report["backend"].update(mode="engine-diagnostic"),
        lambda report: report["backend"].update(transport="direct-engine"),
        lambda report: report["backend"].update(runtime_route="other"),
        lambda report: report["backend"].update(effective_runtime="python-full"),
        lambda report: report["environment"].update(source_commit="f" * 40),
        lambda report: report["environment"].update(dirty_fingerprint="0" * 64),
        lambda report: report["environment"].update(
            dependencies={"lancedb": "0.35.0", "pyarrow": "20.0.0"}
        ),
        lambda report: report["environment"].update(
            retrieval_configuration={"query_expansion": False}
        ),
        lambda report: report["environment"].update(
            embedding_configuration={"model": "other", "dimension": 8}
        ),
    ],
)
def test_freeze_rejects_calibration_report_binding_drift(mutation):
    report = _report()
    mutation(report)
    with pytest.raises(ValueError, match="calibration_report_binding_mismatch"):
        freeze_candidate_manifest(
            selected_report=report,
            grid=PREREGISTERED_GRID,
            calibration=load_dataset_bundle(V1_PATH),
            heldout_fingerprint=opaque_file_fingerprint(V2_PATH),
            **_manifest_fields(),
        )


def test_frozen_manifest_is_deeply_immutable():
    manifest = _frozen_manifest()
    with pytest.raises(TypeError):
        manifest.retrieval_configuration["query_expansion"] = False
    with pytest.raises(TypeError):
        manifest.retrieval_configuration["channels"]["vector"]["window"] = 64
    with pytest.raises(TypeError):
        manifest.dependency_versions["lancedb"] = "0.35.0"
    with pytest.raises(TypeError):
        manifest.fusion_config.weights["vector"] = 1.0


def test_manifest_write_uses_exclusive_create(tmp_path):
    from plastic_promise.core.recall_experiment import write_frozen_manifest

    path = tmp_path / "manifest.json"
    manifest = _frozen_manifest()

    def write_once():
        try:
            write_frozen_manifest(path, manifest)
            return "created"
        except ValueError as exc:
            assert str(exc) == "candidate_manifest_already_exists"
            return "exists"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: write_once(), range(2)))
    assert sorted(outcomes) == ["created", "exists"]
    assert load_frozen_manifest(path) == manifest


def _metric(mrr, hit5):
    return {"mrr": mrr, "hit_at": {"1": mrr, "3": hit5, "5": hit5, "10": hit5}}


def _channel(mrr, hit5):
    return {
        "overall": _metric(mrr, hit5),
        "by_language": {
            "en": _metric(mrr, hit5),
            "zh": _metric(mrr, hit5),
            "cross-lingual": _metric(mrr, hit5),
        },
        "by_group": {
            "token-overlap": _metric(mrr, hit5),
            "partial-overlap": _metric(mrr, hit5),
            "zero-overlap": _metric(mrr, hit5),
        },
    }


def _fusion_report(manifest, *, candidate):
    policy = manifest.candidate_id if candidate else "max-v1"
    fused_mrr = 0.82 if candidate else 0.80
    channel_states = {
        name: {
            "planned": True,
            "enabled": True,
            "available": True,
            "executed": True,
            "participating": True,
            "evidence_only": False,
            "reason": "participating",
        }
        for name in ("vector", "bm25", "fts")
    }
    channel_rankings = {
        name: [{"id": "memory-1", "score": 1.0, "rank": 1}] for name in ("vector", "bm25", "fts")
    }
    case_identities = [
        ("heldout-en-token-identifier", "en", "token-overlap"),
        ("heldout-zh-partial-source-change", "zh", "partial-overlap"),
        ("heldout-cross-zero-contested", "cross-lingual", "zero-overlap"),
        ("heldout-en-partial-deadline", "en", "partial-overlap"),
        ("heldout-zh-zero-current-revision", "zh", "zero-overlap"),
        ("heldout-cross-token-manifest", "cross-lingual", "token-overlap"),
    ]
    return {
        "schema_version": "recall-quality-report/v2",
        "dataset_schema_version": "recall-quality/v1",
        "dataset_revision": "2026-07-12-heldout.1",
        "dataset_role": "held-out",
        "dataset_fingerprint": manifest.heldout_fingerprint,
        "candidate_dimension": "fusion_policy",
        "candidate_id": policy,
        "manifest_hash": manifest.manifest_hash if candidate else "",
        "fusion_config": (
            {
                "k": manifest.fusion_config.k,
                "channels": list(manifest.fusion_config.channels),
                "weights": dict(manifest.fusion_config.weights),
                "windows": dict(manifest.fusion_config.windows),
            }
            if candidate
            else None
        ),
        "publishable_claim": True,
        "corpus": {
            "revision": "2026-07-12-heldout-corpus.1",
            "provenance_revision": "2026-07-12-heldout-provenance.1",
            "sha256": "0e84b5a48e974326694448b0ec60b905c56c930a29398398cf2d70a52dc2425c",
            "count": 15,
        },
        "cases": {
            "sha256": "a81a2509583b41f06565107ef2d5aee4c4d51c2b14b834967f195f491f53a92b",
            "count": 6,
        },
        "execution": {"warmup": 1, "repeat": 3},
        "backend": {
            "mode": "live",
            "transport": "streamable-http",
            "requested_policy": policy,
            "effective_policy": policy,
            "requested_runtime": "rust-full",
            "effective_runtime": "rust-full",
            "runtime_route": manifest.runtime_route,
            "public_call_counts": {"memory_recall": 6, "context_supply": 6},
            "index_text_policy": "legacy",
            "server_pid": 1234,
        },
        "environment": {
            "source_commit": manifest.source_commit,
            "dirty_fingerprint": manifest.dirty_fingerprint,
            "retrieval_configuration": dict(manifest.retrieval_configuration),
            "embedding_configuration": dict(manifest.embedding_configuration),
            "dependencies": dict(manifest.dependency_versions),
        },
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
        "public_transport_call_counts": {"memory_recall": 24, "context_supply": 24},
        "fusion_attestation": {
            "attested_calls": 48,
            "errors": [],
            "observed": [policy, "rust-full", "rust-full"],
        },
        "metrics": {
            "channels": {
                "fused": _channel(fused_mrr, 0.92),
                "vector": _channel(0.76, 0.89),
                "bm25": _channel(0.74, 0.88),
                "fts": _channel(0.72, 0.87),
            },
            "channel_states": channel_states,
            "cases": [
                {
                    "case_id": case_id,
                    "language": language,
                    "group": group,
                    "channel_rankings": channel_rankings,
                    "channel_states": channel_states,
                }
                for case_id, language, group in case_identities
            ],
            "forbidden_hit_rate": 0.0,
            "fallback_rate": 0.0,
            "degradation_rate": 0.0,
            "p95_ms": 11.0 if candidate else 10.0,
        },
    }


def test_comparator_accepts_only_manifest_bound_fusion_difference():
    manifest = _frozen_manifest()
    result = compare_fusion_reports(
        _fusion_report(manifest, candidate=False),
        _fusion_report(manifest, candidate=True),
        manifest=manifest,
        tolerances={"required_split_tolerance": 0.0, "minimum_primary_delta": 0.01},
    )
    assert result["comparability"]["passed"] is True
    assert result["passed"] is True


def test_comparator_rejects_synchronized_forged_heldout_contract():
    manifest = _frozen_manifest()
    baseline = _fusion_report(manifest, candidate=False)
    candidate = _fusion_report(manifest, candidate=True)
    for report in (baseline, candidate):
        report["corpus"].update(
            revision="forged-heldout-corpus",
            provenance_revision="forged-heldout-provenance",
            sha256="e" * 64,
        )
        report["cases"]["sha256"] = "f" * 64

    result = compare_fusion_reports(
        baseline,
        candidate,
        manifest=manifest,
        tolerances={"required_split_tolerance": 0.0, "minimum_primary_delta": 0.01},
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False
    assert {
        check["name"]
        for check in result["failed_checks"]
        if check["name"].startswith("baseline.evidence.heldout_contract")
    } == {
        "baseline.evidence.heldout_contract.corpus.provenance_revision",
        "baseline.evidence.heldout_contract.corpus.revision",
        "baseline.evidence.heldout_contract.corpus.sha256",
        "baseline.evidence.heldout_contract.cases.sha256",
    }


def test_comparator_fails_closed_for_unregistered_heldout_fingerprint():
    manifest = replace(_frozen_manifest(), heldout_fingerprint="0" * 64)
    result = compare_fusion_reports(
        _fusion_report(manifest, candidate=False),
        _fusion_report(manifest, candidate=True),
        manifest=manifest,
        tolerances={"required_split_tolerance": 0.0, "minimum_primary_delta": 0.01},
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False
    assert any(
        check["name"] == "manifest.heldout_report_contract" and not check["passed"]
        for check in result["failed_checks"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report["corpus"].update(count=1),
        lambda report: report["cases"].update(count=1),
        lambda report: report["backend"].update(index_text_policy="compact-v2"),
        lambda report: report["isolated_corpus"].update(seeded=False),
        lambda report: report["smoke"].update(passed=False),
        lambda report: report["fusion_attestation"].update(attested_calls=47),
        lambda report: report["fusion_attestation"].update(errors=["context_supply:missing"]),
        lambda report: report["metrics"]["cases"][0]["channel_rankings"].pop("fts"),
        lambda report: report["metrics"]["cases"][0]["channel_states"].pop("fts"),
    ],
)
def test_comparator_rejects_nonfusion_evidence_drift(mutation):
    manifest = _frozen_manifest()
    candidate = _fusion_report(manifest, candidate=True)
    mutation(candidate)

    result = compare_fusion_reports(
        _fusion_report(manifest, candidate=False),
        candidate,
        manifest=manifest,
        tolerances={"required_split_tolerance": 0.0, "minimum_primary_delta": 0.01},
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("environment", "source_commit"), "f" * 40),
        (("environment", "dirty_fingerprint"), "0" * 64),
        (("environment", "dependencies"), {"lancedb": "0.35.0", "pyarrow": "20.0.0"}),
        (("backend", "runtime_route"), "other"),
        (("candidate_dimension",), "embedding_model"),
        (("manifest_hash",), "0" * 64),
    ],
)
def test_comparator_allows_only_manifest_candidate_dimension(path, value):
    manifest = _frozen_manifest()
    candidate = _fusion_report(manifest, candidate=True)
    target = candidate
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = compare_fusion_reports(
        _fusion_report(manifest, candidate=False),
        candidate,
        manifest=manifest,
        tolerances={"minimum_primary_delta": 0.01},
    )
    assert result["comparability"]["passed"] is False


def test_requested_and_effective_policy_runtime_must_match():
    manifest = _frozen_manifest()
    candidate = _fusion_report(manifest, candidate=True)
    candidate["backend"]["effective_runtime"] = "python-full"
    candidate["backend"]["effective_policy"] = "legacy-auto"
    result = compare_fusion_reports(
        _fusion_report(manifest, candidate=False),
        candidate,
        manifest=manifest,
        tolerances={"minimum_primary_delta": 0.01},
    )
    assert result["passed"] is False
    assert result["comparability"]["passed"] is False


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("candidate_id",), "legacy-auto"),
        (("manifest_hash",), "unexpected"),
        (("fusion_config",), {"k": 1}),
        (("execution", "repeat"), 99),
    ],
)
def test_comparator_rejects_baseline_config_or_execution_drift(path, value):
    manifest = _frozen_manifest()
    baseline = _fusion_report(manifest, candidate=False)
    target = baseline
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    result = compare_fusion_reports(
        baseline,
        _fusion_report(manifest, candidate=True),
        manifest=manifest,
        tolerances={"minimum_primary_delta": 0.01},
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False


@pytest.mark.parametrize(
    ("kind", "split", "metric"),
    [
        ("overall", "overall", "mrr"),
        ("language", "en", "mrr"),
        ("language", "zh", "hit_at"),
        ("language", "cross-lingual", "mrr"),
        ("group", "token-overlap", "hit_at"),
        ("group", "partial-overlap", "mrr"),
        ("group", "zero-overlap", "mrr"),
    ],
)
def test_comparator_gates_fused_against_baseline_and_best_constituent_per_split(
    kind, split, metric
):
    manifest = _frozen_manifest()
    candidate = _fusion_report(manifest, candidate=True)
    if kind == "overall":
        target = candidate["metrics"]["channels"]["fused"]["overall"]
    else:
        field = "by_language" if kind == "language" else "by_group"
        target = candidate["metrics"]["channels"]["fused"][field][split]
    if metric == "mrr":
        target["mrr"] = 0.0
    else:
        target["hit_at"]["5"] = 0.0
    result = compare_fusion_reports(
        _fusion_report(manifest, candidate=False),
        candidate,
        manifest=manifest,
        tolerances={"minimum_primary_delta": 0.01},
    )
    assert result["passed"] is False
    assert any(
        not check["passed"] and f"{kind}:{split}" in check["name"]
        for check in result["failed_checks"]
    )
