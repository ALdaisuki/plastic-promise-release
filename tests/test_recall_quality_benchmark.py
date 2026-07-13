from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from plastic_promise.core import recall_quality
from plastic_promise.core.recall_quality import (
    ChannelMetricSummary,
    RecallCase,
    compare_summaries,
    evaluate_best_constituent_gate,
    evaluate_cases,
    load_dataset,
)
from scripts import benchmark_recall_quality
from scripts.http_mcp_harness import require_owned_health

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "fixtures" / "recall_quality" / "v1.json"
CURRENT_SOURCE_FINGERPRINT = benchmark_recall_quality._source_fingerprint(DATASET)


def _owned_health_payload(root: Path, *, pid: int = 7331, revision: str = "a" * 40):
    return {
        "status": "ok",
        "pid": pid,
        "source_root": str(root),
        "source_revision": revision,
        "fusion_policy": "max-v1",
        "fusion_attestation": {
            "schema": "retrieval-fusion-identity/v1",
            "requested_policy": "max-v1",
            "effective_policy": "max-v1",
            "requested_runtime": "python",
            "effective_runtime": "python",
            "capability_reason": "runtime_forced:python",
            "candidate_id": "",
            "config_hash": "",
            "config": None,
        },
    }


def test_owned_health_rejects_foreign_checkout_even_with_spawned_pid(tmp_path):
    managed = SimpleNamespace(pid=7331)
    payload = _owned_health_payload(tmp_path / "foreign")

    with pytest.raises(RuntimeError, match="health_source_root_mismatch"):
        require_owned_health(
            payload,
            managed,
            expected_source_root=tmp_path / "expected",
            expected_source_revision="a" * 40,
            expected_fusion_policy="max-v1",
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda payload: payload.update(source_revision="b" * 40),
            "health_source_revision_mismatch",
        ),
        (
            lambda payload: (
                payload.update(fusion_policy="legacy-auto"),
                payload["fusion_attestation"].update(
                    requested_policy="legacy-auto",
                    effective_policy="legacy-auto",
                ),
            ),
            "health_fusion_policy_mismatch",
        ),
    ],
)
def test_owned_health_rejects_revision_and_fusion_identity_drift(tmp_path, mutation, error):
    managed = SimpleNamespace(pid=7331)
    payload = _owned_health_payload(tmp_path)
    mutation(payload)

    with pytest.raises(RuntimeError, match=error):
        require_owned_health(
            payload,
            managed,
            expected_source_root=tmp_path,
            expected_source_revision="a" * 40,
            expected_fusion_policy="max-v1",
        )


def _case(case_id: str, *, language: str = "en", group: str = "token-overlap") -> RecallCase:
    return RecallCase(
        case_id=case_id,
        language=language,
        group=group,
        query=f"query {case_id}",
        relevant_memory_ids=(f"relevant-{case_id}",),
        forbidden_memory_ids=(f"forbidden-{case_id}",),
        task_type="architecture",
        project_id="project:benchmark",
    )


def _complete_summary():
    cases = load_dataset(DATASET)

    def retrieve(case):
        relevant = case.relevant_memory_ids[0]
        ranking = [{"id": relevant, "score": 1.0, "rank": 1}]
        return {
            "ranked_ids": [relevant],
            "latency_ms": 2.0,
            "channel_rankings": {"vector": ranking, "bm25": ranking},
            "channel_states": {
                "vector": _channel_state(),
                "bm25": _channel_state(),
            },
        }

    return evaluate_cases(
        cases,
        retrieve,
        ks=(1, 3, 5, 10),
    )


def _channel_state(**overrides):
    state = {
        "planned": True,
        "enabled": True,
        "available": True,
        "executed": True,
        "participating": True,
        "evidence_only": False,
        "reason": "",
    }
    state.update(overrides)
    return state


def _http_backend_from_sync(factory):
    async def backend(dataset, *, index_text_policy, fusion_policy, paths):
        retrieve, metadata, evidence = factory(dataset, index_text_policy, paths)
        counts = {
            "memory_store": len(dataset.corpus),
            "feedback_apply": 0,
            "memory_recall": 0,
            "context_supply": 0,
        }

        def public_retrieve(case):
            counts["memory_recall"] += 1
            counts["context_supply"] += 1
            return retrieve(case)

        metadata.update(
            {
                "transport": "streamable-http",
                "server_pid": 4101,
                "requested_policy": fusion_policy,
                "effective_policy": fusion_policy,
                "requested_runtime": "python",
                "effective_runtime": "python",
            }
        )
        evidence["public_call_counts"] = counts
        evidence["fusion_attested"] = True
        return public_retrieve, metadata, evidence

    return backend


def _comparable_live_report(candidate: str) -> dict:
    summary = _complete_summary()
    return {
        "schema_version": "recall-quality-report/v1",
        "dataset_schema_version": "recall-quality/v1",
        "dataset_revision": "2026-07-10.2",
        "candidate": candidate,
        "corpus": {
            "revision": "2026-07-10.2-corpus.1",
            "provenance_revision": "2026-07-10.2-provenance.1",
            "sha256": "61c0c1002f23375404ab9aee768996dfc3f81eab2f9b8c0a8aa23e27744567ae",
            "count": 96,
        },
        "cases": {
            "sha256": "fdd8c24359772a4b386b140d4514d26849f047d4d1955ef2e030260635035938",
            "count": 32,
        },
        "backend": {
            "mode": "live",
            "deterministic": False,
            "fallback_used": False,
            "degraded_used": False,
            "model": "mxbai-embed-large",
            "dimension": 1024,
            "index_text_policy": candidate,
            "runtime": {"os": "test", "python_version": "3.13"},
        },
        "execution": {"warmup": 1, "repeat": 3},
        "environment": {
            "provider": "ollama",
            "code_revision": "same-tree",
            "dataset_source": benchmark_recall_quality._source_label(DATASET),
            "source_fingerprint": CURRENT_SOURCE_FINGERPRINT,
            "dependencies": {"lancedb": "0.34.0", "pyarrow": "24.0.0"},
            "retrieval_configuration": dict(benchmark_recall_quality.LIVE_RETRIEVAL_CONFIGURATION),
        },
        "isolated_corpus": {
            "seeded": True,
            "canonical_count": 96,
            "derived_count": 64,
            "eligible_count": 64,
        },
        "smoke": {
            "store": True,
            "recall": True,
            "supply": True,
            "verified_visible": True,
            "forbidden_hidden": True,
            "passed": True,
        },
        "publishable_claim": True,
        "metrics": summary.to_dict(include_cases=False),
    }


def test_fixed_dataset_is_versioned_bilingual_and_covers_required_scenarios():
    dataset = recall_quality.load_dataset_bundle(DATASET)
    cases = list(dataset.cases)

    assert len(cases) >= 24
    assert {case.language for case in cases} == {"en", "zh", "cross-lingual"}
    assert {case.group for case in cases} == {
        "token-overlap",
        "partial-overlap",
        "zero-overlap",
    }
    assert all(case.dataset_revision == "2026-07-10.2" for case in cases)
    assert all(case.distractor_memory_ids for case in cases)
    assert all(case.forbidden_memory_ids for case in cases)

    ids = {case.case_id for case in cases}
    assert sum(case_id.startswith("cross-zh-en-") for case_id in ids) >= 4
    assert sum(case_id.startswith("cross-en-zh-") for case_id in ids) >= 4
    assert sum(case_id.startswith("identifier-") for case_id in ids) >= 4
    assert sum(case_id.startswith("synthesis-state-") for case_id in ids) >= 4

    referenced = {
        memory_id
        for case in cases
        for memory_id in (
            *case.relevant_memory_ids,
            *case.forbidden_memory_ids,
            *case.distractor_memory_ids,
        )
    }
    corpus_ids = {record.memory_id for record in dataset.corpus}
    assert corpus_ids == referenced
    assert dataset.corpus_revision == "2026-07-10.2-corpus.1"
    assert dataset.synthesis_provenance_revision == "2026-07-10.2-provenance.1"
    assert len(dataset.corpus_hash) == 64
    assert dataset.corpus_count == len(corpus_ids) == 96
    assert {record.synthesis_status for record in dataset.corpus} >= {
        "not-synthesis",
        "draft",
        "verified",
        "contested",
        "stale",
    }
    assert all(record.project_id == "project:benchmark" for record in dataset.corpus)
    assert all(record.domain and record.category for record in dataset.corpus)
    assert all(record.l0_abstract and record.l1_summary for record in dataset.corpus)
    synthesis_records = [record for record in dataset.corpus if record.memory_type == "synthesis"]
    assert all(len(record.metadata["source_ids"]) >= 2 for record in synthesis_records)
    assert all(
        record.metadata.get("verification_actor")
        and record.metadata.get("verification_call_id")
        and record.metadata.get("verified_at")
        for record in synthesis_records
        if record.synthesis_status in {"verified", "stale"}
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda root: root.pop("dataset_revision"), "dataset_revision"),
        (
            lambda root: root["cases"].append(dict(root["cases"][0])),
            "duplicate case_id",
        ),
        (
            lambda root: root["cases"][0].update(relevant_memory_ids=[]),
            "relevant_memory_ids",
        ),
        (
            lambda root: root["cases"][0].update(group="semantic-magic"),
            "unknown group",
        ),
        (
            lambda root: root["corpus"].append(dict(root["corpus"][0])),
            "duplicate corpus memory_id",
        ),
        (
            lambda root: root["corpus"].pop(0),
            "missing corpus record",
        ),
        (
            lambda root: root["corpus"][0].update(
                l0_abstract=f"leaked {root['corpus'][1]['memory_id']}"
            ),
            "fixture ID leaked",
        ),
        (
            lambda root: root["synthesis_provenance"].pop("s-draft-ineligible"),
            "missing synthesis provenance",
        ),
        (
            lambda root: root["synthesis_provenance"]["s-draft-ineligible"].update(
                source_ids=["m-sqlite-canonical", "m-does-not-exist"]
            ),
            "synthesis source missing",
        ),
        (
            lambda root: root["synthesis_provenance"]["s-verified-eligible"].pop(
                "verification_actor"
            ),
            "verification_actor",
        ),
    ],
)
def test_load_dataset_rejects_invalid_acceptance_evidence(tmp_path, mutation, message):
    root = json.loads(DATASET.read_text(encoding="utf-8"))
    mutation(root)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(root, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_dataset(path)


def test_metrics_compute_hit_mrr_forbidden_latency_fallback_and_splits():
    cases = [
        _case("one", language="en", group="token-overlap"),
        _case("two", language="en", group="zero-overlap"),
        _case("three", language="zh", group="token-overlap"),
        _case("four", language="zh", group="zero-overlap"),
    ]
    rankings = {
        "one": ["relevant-one", "d1"],
        "two": ["relevant-two", "d2"],
        "three": ["d3", "relevant-three", "forbidden-three"],
        "four": ["d4", "relevant-four"],
    }
    latencies = {"one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0}

    def fake_retrieve(case: RecallCase):
        return {
            "ranked_ids": rankings[case.case_id],
            "latency_ms": latencies[case.case_id],
            "fallback_used": case.case_id == "four",
            "channels": {
                "lexical": rankings[case.case_id],
                "fused": rankings[case.case_id],
            },
        }

    summary = evaluate_cases(cases, fake_retrieve, ks=(1, 3, 5, 10))

    assert summary.hit_at[1] == pytest.approx(0.5)
    assert summary.hit_at[5] == pytest.approx(1.0)
    assert summary.mrr == pytest.approx(0.75)
    assert summary.forbidden_hit_rate == pytest.approx(0.25)
    assert summary.fallback_rate == pytest.approx(0.25)
    assert summary.p50_ms == pytest.approx(2.0)
    assert summary.p95_ms == pytest.approx(4.0)
    assert summary.p50_ms <= summary.p95_ms
    assert summary.by_language["en"].hit_at[1] == pytest.approx(1.0)
    assert summary.by_language["zh"].hit_at[1] == pytest.approx(0.0)
    assert summary.by_group["token-overlap"].forbidden_hit_rate == pytest.approx(0.5)
    assert summary.channels["fused"].overall.mrr == pytest.approx(summary.mrr)
    assert "bm25" in summary.channels
    assert "lexical" not in summary.channels
    assert len(summary.case_results) == 4


def test_channel_metrics_include_overall_language_and_group_slices():
    cases = [
        _case("en", language="en", group="token-overlap"),
        _case("zh", language="zh", group="partial-overlap"),
        _case("cross", language="cross-lingual", group="zero-overlap"),
    ]

    def retrieve(case):
        relevant = case.relevant_memory_ids[0]
        return {
            "ranked_ids": [relevant],
            "channel_rankings": {
                "vector": [{"id": relevant, "score": 0.9, "rank": 1}],
                "bm25": [{"id": relevant, "score": 7.0, "rank": 1}],
            },
            "channel_states": {
                "vector": _channel_state(),
                "bm25": _channel_state(),
                "graph": _channel_state(
                    participating=False,
                    evidence_only=True,
                    reason="evidence_only",
                ),
            },
        }

    summary = evaluate_cases(cases, retrieve, ks=(1, 5))

    assert isinstance(summary.channels["vector"], ChannelMetricSummary)
    assert summary.channels["vector"].overall.mrr == 1.0
    assert set(summary.channels["vector"].by_language) == {"en", "zh", "cross-lingual"}
    assert set(summary.channels["vector"].by_group) == {
        "token-overlap",
        "partial-overlap",
        "zero-overlap",
    }
    payload = summary.to_dict()
    assert set(payload["channels"]["vector"]) == {"overall", "language", "group"}
    assert payload["cases"][0]["channel_rankings"]["vector"][0] == {
        "id": f"relevant-{cases[0].case_id}",
        "score": 0.9,
        "rank": 1,
    }
    assert payload["channel_states"]["graph"]["evidence_only"] is True


def test_zero_weight_constituent_still_participates_in_quality_gate():
    cases = [
        _case("en", language="en", group="token-overlap"),
        _case("zh", language="zh", group="partial-overlap"),
        _case("cross", language="cross-lingual", group="zero-overlap"),
    ]

    def retrieve(case):
        relevant = case.relevant_memory_ids[0]
        return {
            "ranked_ids": [f"fused-miss-{case.case_id}"],
            "channel_rankings": {
                "vector": [{"id": f"vector-miss-{case.case_id}", "score": 0.9, "rank": 1}],
                "bm25": [{"id": relevant, "score": 0.0, "rank": 1}],
            },
            "channel_states": {
                "vector": _channel_state(weight=1.0),
                "bm25": _channel_state(
                    participating=False,
                    reason="zero_weight",
                    weight=0.0,
                ),
            },
        }

    summary = evaluate_cases(cases, retrieve, ks=(1, 5))
    gate = evaluate_best_constituent_gate(summary)

    assert gate["passed"] is False
    assert any(
        check["metric"] == "mrr"
        and check["split"] == "overall"
        and check["best_channel"] == "bm25"
        and not check["passed"]
        for check in gate["checks"]
    )
    assert any(check["metric"] == "hit_at_5" for check in gate["failures"])


def test_planned_enabled_unavailable_channel_fails_before_metric_comparison():
    cases = [
        _case("en", language="en", group="token-overlap"),
        _case("zh", language="zh", group="zero-overlap"),
    ]

    def retrieve(case):
        return {
            "ranked_ids": [case.relevant_memory_ids[0]],
            "channel_rankings": {"fts": []},
            "channel_states": {
                "fts": _channel_state(
                    available=False,
                    executed=False,
                    participating=False,
                    reason="backend_unavailable",
                )
            },
        }

    summary = evaluate_cases(cases, retrieve, ks=(1, 5))
    comparison = compare_summaries(summary, summary)

    assert comparison.passed is False
    assert {check.metric for check in comparison.regressions} >= {
        "channel_available",
        "channel_executed",
    }
    assert not any(check.metric == "mrr" for check in comparison.checks)


@pytest.mark.parametrize(
    "state",
    [
        {key: value for key, value in _channel_state().items() if key != "executed"},
        _channel_state(available=False, reason=""),
    ],
)
def test_channel_states_require_complete_booleans_and_stable_reason(state):
    case = _case("state")

    with pytest.raises(ValueError, match="channel_states"):
        evaluate_cases(
            [case],
            lambda _case: {
                "ranked_ids": [],
                "channel_rankings": {"vector": []},
                "channel_states": {"vector": state},
            },
            ks=(1, 5),
        )


def test_compare_summaries_reports_quality_regression_without_mutating_inputs():
    cases = [_case("one"), _case("two")]

    baseline = evaluate_cases(
        cases,
        lambda case: {"ranked_ids": [case.relevant_memory_ids[0]], "latency_ms": 2.0},
        ks=(1, 5),
    )
    candidate = replace(
        baseline,
        mrr=0.80,
        forbidden_hit_rate=0.20,
        p95_ms=3.0,
    )

    comparison = compare_summaries(baseline, candidate, tolerance=0.01)

    assert not comparison.passed
    assert {item.metric for item in comparison.regressions} >= {"mrr", "forbidden_hit_rate"}
    assert baseline.mrr == 1.0


@pytest.mark.parametrize(
    "candidate_mutation",
    [
        lambda baseline: replace(baseline, case_count=baseline.case_count - 1),
        lambda baseline: replace(baseline, by_language={}),
        lambda baseline: replace(baseline, by_group={}),
        lambda baseline: replace(
            baseline,
            by_language={
                **baseline.by_language,
                "zh": replace(
                    baseline.by_language["zh"],
                    case_count=baseline.by_language["zh"].case_count - 1,
                ),
            },
        ),
        lambda baseline: replace(
            baseline,
            by_group={
                **baseline.by_group,
                "zero-overlap": replace(
                    baseline.by_group["zero-overlap"],
                    case_count=baseline.by_group["zero-overlap"].case_count - 1,
                ),
            },
        ),
    ],
)
def test_compare_summaries_fails_closed_on_incomplete_cases_or_splits(candidate_mutation):
    baseline = _complete_summary()

    comparison = compare_summaries(baseline, candidate_mutation(baseline))

    assert not comparison.passed
    assert any(
        check.metric in {"case_count", "split_key_set", "required_split"}
        for check in comparison.regressions
    )


def test_compare_summaries_requires_en_zh_and_zero_overlap_even_when_both_omit_them():
    incomplete = evaluate_cases(
        [_case("only-en")],
        lambda case: [case.relevant_memory_ids[0]],
        ks=(1, 5),
    )

    comparison = compare_summaries(incomplete, incomplete)

    assert not comparison.passed
    assert {check.split for check in comparison.regressions} >= {
        "language:zh",
        "group:zero-overlap",
    }


def test_compare_summaries_accepts_complete_identical_splits():
    summary = _complete_summary()

    assert compare_summaries(summary, summary).passed


def test_deterministic_cli_report_is_reproducible_and_not_publishable(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "benchmark_recall_quality.py"),
        "--dataset",
        str(DATASET),
        "--backend",
        "deterministic",
        "--candidate",
        "legacy",
        "--warmup",
        "1",
        "--repeat",
        "2",
    ]

    first_run = subprocess.run(
        [*command, "--output", str(first)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    second_run = subprocess.run(
        [*command, "--output", str(second)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert first_run.returncode == 0, first_run.stderr
    assert second_run.returncode == 0, second_run.stderr
    report = json.loads(first.read_text(encoding="utf-8"))
    repeated = json.loads(second.read_text(encoding="utf-8"))

    assert report["schema_version"] == "recall-quality-report/v1"
    assert report["dataset_revision"] == "2026-07-10.2"
    assert report["corpus"]["revision"] == "2026-07-10.2-corpus.1"
    assert len(report["corpus"]["sha256"]) == 64
    assert report["corpus"]["count"] == 96
    assert report["cases"]["count"] == 32
    assert len(report["cases"]["sha256"]) == 64
    assert len(report["environment"]["source_fingerprint"]) == 64
    assert report["backend"]["mode"] == "deterministic"
    assert report["backend"]["deterministic"] is True
    assert report["backend"]["fallback_used"] is False
    assert report["candidate"] == "legacy"
    assert report["publishable_claim"] is False
    assert report["metrics"]["case_count"] >= 24
    assert set(report["metrics"]["language"]) == {"en", "zh", "cross-lingual"}
    assert set(report["metrics"]["group"]) == {
        "token-overlap",
        "partial-overlap",
        "zero-overlap",
    }
    assert {"bm25", "vector", "fused"} <= set(report["metrics"]["channels"])
    assert report["quality"] == repeated["quality"]
    assert report["gate"] == repeated["gate"]


def test_live_report_uses_isolated_seeded_corpus_restores_environment_and_requires_smoke(
    monkeypatch,
):
    original_db = "C:/ambient/current-memory.db"
    original_lancedb = "C:/ambient/current-vectors"
    monkeypatch.setenv("PLASTIC_DB_PATH", original_db)
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", original_lancedb)
    ambient_retrieval = {
        "PP_VECTOR_WEIGHT": "0.91",
        "PP_QUERY_EXPANSION": "0",
        "PP_FTS_DISABLED": "1",
        "PP_FTS_FUSION": "0",
    }
    for name, value in ambient_retrieval.items():
        monkeypatch.setenv(name, value)
    observed: dict[str, object] = {}

    class FakeNonzeroEmbedder:
        model_name = "real-multilingual-model"
        dim = 3

        def embed(self, text: str):
            observed["embed_calls"] = int(observed.get("embed_calls", 0)) + 1
            return [1.0, 0.5, 0.25]

    def fake_live_backend(dataset, candidate, paths):
        embedder = FakeNonzeroEmbedder()
        observed["db"] = os.environ["PLASTIC_DB_PATH"]
        observed["lancedb"] = os.environ["PLASTIC_LANCEDB_PATH"]
        observed["candidate"] = os.environ["PP_MEMORY_INDEX_TEXT_POLICY"]
        observed["retrieval_configuration"] = {
            name: os.environ[name] for name in benchmark_recall_quality.LIVE_RETRIEVAL_CONFIGURATION
        }
        observed["paths"] = paths
        assert observed["db"] != original_db
        assert observed["lancedb"] != original_lancedb
        assert Path(str(observed["db"])).parent == paths.root
        assert Path(str(observed["lancedb"])).parent == paths.root
        assert len(dataset.corpus) == 96
        for record in dataset.corpus:
            embedder.embed(record.l0_abstract)

        def retrieve(case: RecallCase):
            embedder.embed(case.query)
            ranking = [
                {"id": memory_id, "score": 1.0 / rank, "rank": rank}
                for rank, memory_id in enumerate(
                    [*case.relevant_memory_ids, *case.distractor_memory_ids],
                    start=1,
                )
            ]
            return {
                "ranked_ids": [*case.relevant_memory_ids, *case.distractor_memory_ids],
                "latency_ms": 1.0,
                "fallback_used": False,
                "channel_rankings": {"vector": ranking, "bm25": ranking},
                "channel_states": {
                    "vector": _channel_state(),
                    "bm25": _channel_state(),
                },
            }

        return (
            retrieve,
            {
                "model": embedder.model_name,
                "dimension": embedder.dim,
                "index_text_policy": candidate,
                "runtime": {"os": "test"},
            },
            {
                "isolated_corpus": {
                    "seeded": True,
                    "canonical_count": len(dataset.corpus),
                    "derived_count": len(dataset.corpus),
                    "eligible_count": len(dataset.corpus),
                },
                "smoke": {
                    "store": True,
                    "recall": True,
                    "supply": True,
                    "verified_visible": True,
                    "forbidden_hidden": True,
                    "passed": True,
                },
            },
        )

    monkeypatch.setattr(
        benchmark_recall_quality,
        "_http_live_backend",
        _http_backend_from_sync(fake_live_backend),
    )
    live = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="live",
        candidate="compact-v2",
        warmup=0,
        repeat=1,
    )
    assert live["publishable_claim"] is True
    assert live["backend"]["fallback_used"] is False
    assert live["backend"]["model"] == "real-multilingual-model"
    assert live["backend"]["dimension"] == 3
    assert live["isolated_corpus"]["seeded"] is True
    assert live["smoke"]["passed"] is True
    assert "cold_latency_ms" in live["backend"]
    assert "warm_p95_ms" in live["backend"]
    assert observed["candidate"] == "compact-v2"
    assert (
        observed["retrieval_configuration"] == benchmark_recall_quality.LIVE_RETRIEVAL_CONFIGURATION
    )
    assert int(observed["embed_calls"]) > 96
    assert os.environ["PLASTIC_DB_PATH"] == original_db
    assert os.environ["PLASTIC_LANCEDB_PATH"] == original_lancedb
    assert {name: os.environ[name] for name in ambient_retrieval} == ambient_retrieval
    paths = observed["paths"]
    assert not paths.root.exists()


def test_live_backend_closes_owned_resources_when_corpus_install_fails(tmp_path, monkeypatch):
    import sqlite3

    class FakeDatabase:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeLanceDBStore:
        def __init__(self):
            self._table = object()
            self._db = FakeDatabase()

    class FakeEmbedder:
        model_name = "real-multilingual-model"
        dim = 3

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.connection = sqlite3.connect(tmp_path / "owned.db")
            self._sqlite = SimpleNamespace(_conn=self.connection)
            self._ldb = FakeLanceDBStore()
            self._embedder = FakeEmbedder()
            self._dm = object()
            self._code_index = object()
            self._rust_engine_instance = object()
            self._memories = {"owned": {}}

        def ensure_heavy_init(self):
            return None

        @property
        def lancedb_store(self):
            return self._ldb

    observed = {}

    def fake_engine_factory(**kwargs):
        engine = FakeEngine(**kwargs)
        observed["engine"] = engine
        observed["database"] = engine._ldb._db
        return engine

    monkeypatch.setattr(
        "plastic_promise.core.context_engine.ContextEngine",
        fake_engine_factory,
    )
    monkeypatch.setattr(
        benchmark_recall_quality,
        "_install_live_corpus",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("seed failed")),
    )
    dataset = recall_quality.load_dataset_bundle(DATASET)
    paths = benchmark_recall_quality.LivePaths(
        root=tmp_path,
        sqlite=tmp_path / "owned.db",
        lancedb=tmp_path / "lancedb",
    )

    with pytest.raises(RuntimeError, match="seed failed"):
        benchmark_recall_quality._engine_diagnostic_backend(dataset, "compact-v2", paths)

    engine = observed["engine"]
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        engine.connection.execute("SELECT 1")
    assert observed["database"].closed is True
    assert engine._ldb is None
    assert engine._sqlite is None


def test_live_report_is_not_publishable_when_seed_or_smoke_evidence_fails(monkeypatch):
    def fake_live_backend(dataset, candidate, paths):
        def retrieve(case: RecallCase):
            return {"ranked_ids": case.relevant_memory_ids, "latency_ms": 1.0}

        return (
            retrieve,
            {
                "model": "real-multilingual-model",
                "dimension": 1024,
                "index_text_policy": candidate,
                "runtime": {"os": "test"},
            },
            {
                "isolated_corpus": {
                    "seeded": False,
                    "canonical_count": 0,
                    "derived_count": 0,
                    "eligible_count": 0,
                },
                "smoke": {
                    "store": False,
                    "recall": False,
                    "supply": False,
                    "verified_visible": False,
                    "forbidden_hidden": False,
                    "passed": False,
                },
            },
        )

    monkeypatch.setattr(
        benchmark_recall_quality,
        "_http_live_backend",
        _http_backend_from_sync(fake_live_backend),
    )
    failed = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="live",
        candidate="legacy",
        warmup=0,
        repeat=1,
    )
    assert failed["publishable_claim"] is False
    assert "isolated corpus" in failed["publishability_reason"]


def test_live_report_is_not_publishable_when_any_case_is_degraded(monkeypatch):
    def fake_live_backend(dataset, candidate, paths):
        def retrieve(case: RecallCase):
            return {
                "ranked_ids": case.relevant_memory_ids,
                "latency_ms": 1.0,
                "degraded": True,
            }

        return (
            retrieve,
            {
                "model": "real-multilingual-model",
                "dimension": 1024,
                "index_text_policy": candidate,
                "runtime": {"os": "test"},
            },
            {
                "isolated_corpus": {
                    "seeded": True,
                    "canonical_count": len(dataset.corpus),
                    "derived_count": len(dataset.corpus),
                    "eligible_count": len(dataset.corpus),
                },
                "smoke": {
                    "store": True,
                    "recall": True,
                    "supply": True,
                    "verified_visible": True,
                    "forbidden_hidden": True,
                    "passed": True,
                },
            },
        )

    monkeypatch.setattr(
        benchmark_recall_quality,
        "_http_live_backend",
        _http_backend_from_sync(fake_live_backend),
    )
    report = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="live",
        candidate="legacy",
        warmup=0,
        repeat=1,
    )

    assert report["metrics"]["degradation_rate"] == 1.0
    assert report["publishable_claim"] is False


def test_repeat_retriever_aggregates_warmup_fallback_and_degradation():
    outcomes = iter(
        [
            {"ranked_ids": ["warm-fallback"], "fallback_used": True},
            {"ranked_ids": ["warm-degraded"], "degraded": True},
            {"ranked_ids": ["measured-clean"], "latency_ms": 1.0},
        ]
    )
    repeated = benchmark_recall_quality._repeat_retriever(
        lambda case: next(outcomes),
        warmup=2,
        repeat=1,
    )

    result = repeated(_case("aggregate-signals"))

    assert result["ranked_ids"] == ["measured-clean"]
    assert result["fallback_used"] is True
    assert result["degraded"] is True


def test_live_report_is_not_publishable_when_cold_probe_is_degraded(monkeypatch):
    call_count = 0

    def fake_live_backend(dataset, candidate, paths):
        def retrieve(case: RecallCase):
            nonlocal call_count
            call_count += 1
            return {
                "ranked_ids": case.relevant_memory_ids,
                "latency_ms": 1.0,
                "degraded": call_count == 1,
            }

        return (
            retrieve,
            {
                "model": "real-multilingual-model",
                "dimension": 1024,
                "index_text_policy": candidate,
                "runtime": {"os": "test"},
            },
            {
                "isolated_corpus": {
                    "seeded": True,
                    "canonical_count": len(dataset.corpus),
                    "derived_count": len(dataset.corpus),
                    "eligible_count": len(dataset.corpus),
                },
                "smoke": {
                    "store": True,
                    "recall": True,
                    "supply": True,
                    "verified_visible": True,
                    "forbidden_hidden": True,
                    "passed": True,
                },
            },
        )

    monkeypatch.setattr(
        benchmark_recall_quality,
        "_engine_diagnostic_backend",
        fake_live_backend,
    )
    report = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="engine-diagnostic",
        candidate="legacy",
        warmup=0,
        repeat=1,
    )

    assert report["backend"]["degraded_used"] is True
    assert report["publishable_claim"] is False


def test_source_fingerprint_changes_with_benchmark_source_content(tmp_path, monkeypatch):
    source = tmp_path / "runner.py"
    dataset = tmp_path / "dataset.json"
    source.write_text("version = 1\n", encoding="utf-8")
    dataset.write_text('{"revision": 1}\n', encoding="utf-8")
    monkeypatch.setattr(benchmark_recall_quality, "BENCHMARK_SOURCE_PATHS", (source,))

    before = benchmark_recall_quality._source_fingerprint(dataset)
    source.write_text("version = 2\n", encoding="utf-8")
    after = benchmark_recall_quality._source_fingerprint(dataset)

    assert len(before) == 64
    assert before != after


def test_source_fingerprint_covers_entire_python_package():
    fingerprinted = set(benchmark_recall_quality._source_paths(DATASET))
    package_sources = {
        path.resolve() for path in (ROOT / "plastic_promise").rglob("*.py") if path.is_file()
    }

    assert package_sources
    assert package_sources <= fingerprinted


def test_experiment_fingerprint_includes_http_process_harness():
    assert ROOT / "scripts/http_mcp_harness.py" in benchmark_recall_quality.BENCHMARK_SOURCE_PATHS


def test_environment_metadata_records_native_retrieval_dependencies():
    environment = benchmark_recall_quality._environment_metadata(DATASET)

    assert set(environment["dependencies"]) == {"lancedb", "pyarrow"}
    assert all(environment["dependencies"].values())
    assert environment["dataset_source"] == "tests/fixtures/recall_quality/v1.json"
    assert environment["retrieval_configuration"] == {
        name: os.environ.get(name, default)
        for name, default in benchmark_recall_quality.LIVE_RETRIEVAL_CONFIGURATION.items()
    }


def test_live_index_material_requires_canonical_persisted_candidate():
    from plastic_promise.core.memory_index import (
        build_index_material,
        index_metadata,
    )

    material = build_index_material(
        {
            "domain": "building",
            "category": "decision",
            "l0_abstract": "SQLite truth",
            "l1_summary": "Persist exact compact material",
        },
        policy="compact-v2",
        model_name="Model-A",
    )
    row = {
        "embedding_text": material.vector_text,
        "search_text": material.search_text,
        "embedding_hash": material.embedding_hash,
        "metadata_json": {"memory_index": index_metadata(material)},
    }
    engine = SimpleNamespace(
        _sqlite=SimpleNamespace(get=lambda memory_id: row),
        _memories={"ordinary-1": row},
    )

    assert (
        benchmark_recall_quality._read_live_index_material(
            engine,
            "ordinary-1",
            candidate="compact-v2",
            model="Model-A",
            fixture_id="ordinary-1",
        )
        == material
    )
    tampered = {**row, "embedding_text": material.vector_text + " tampered"}
    engine._sqlite = SimpleNamespace(get=lambda memory_id: tampered)
    with pytest.raises(RuntimeError, match="ordinary-1"):
        benchmark_recall_quality._read_live_index_material(
            engine,
            "ordinary-1",
            candidate="compact-v2",
            model="Model-A",
            fixture_id="ordinary-1",
        )
    engine._sqlite = SimpleNamespace(get=lambda memory_id: row)
    with pytest.raises(RuntimeError, match="policy"):
        benchmark_recall_quality._read_live_index_material(
            engine,
            "ordinary-1",
            candidate="legacy",
            model="Model-A",
            fixture_id="ordinary-1",
        )


def test_live_adapter_consumes_complete_rankings_without_survivor_reconstruction():
    pack = SimpleNamespace(
        channel_rankings={
            "vector": [
                {"id": "actual-b", "score": 0.9, "rank": 1},
                {"id": "actual-tail", "score": 0.1, "rank": 2},
            ],
            "bm25": [
                {"id": "actual-a", "score": 0.8, "rank": 1},
                {"id": "actual-b", "score": 0.3, "rank": 2},
            ],
            "fts": [{"id": "actual-b", "score": 0.7, "rank": 1}],
        },
        channel_states={
            "vector": _channel_state(),
            "bm25": _channel_state(),
            "fts": _channel_state(),
            "graph": _channel_state(
                participating=False,
                evidence_only=True,
                reason="evidence_only",
            ),
        },
        per_item_stats=[
            {
                "id": "actual-a",
                "vector_score": 999.0,
            },
        ],
    )

    rankings, states = benchmark_recall_quality._channel_evidence_from_pack(
        pack,
        {
            "actual-a": "fixture-a",
            "actual-b": "fixture-b",
            "actual-tail": "fixture-tail",
        },
    )

    assert rankings["vector"] == [
        {"id": "fixture-b", "score": 0.9, "rank": 1},
        {"id": "fixture-tail", "score": 0.1, "rank": 2},
    ]
    assert "fixture-tail" not in {"fixture-a", "fixture-b"}
    assert rankings["bm25"][0]["id"] == "fixture-a"
    assert "graph" not in rankings
    assert states["graph"]["evidence_only"] is True


@pytest.mark.parametrize(
    ("pack", "audit", "expected"),
    [
        (SimpleNamespace(degraded=True), {}, True),
        (SimpleNamespace(project_context={"degraded": True}), {}, True),
        (SimpleNamespace(), {"project_degraded": "true"}, True),
        (
            SimpleNamespace(),
            {
                "synthesis_retrieval": {
                    "degradations": [{"id": "synthesis-1", "reason": "candidate_missing"}]
                }
            },
            True,
        ),
        (SimpleNamespace(), {"synthesis_retrieval": {"degradations": []}}, False),
        (
            SimpleNamespace(),
            {"retrieval_degradations": [{"channel": "fts", "reason": "query_failed"}]},
            True,
        ),
        (SimpleNamespace(degraded=False, project_context={"degraded": False}), {}, False),
    ],
)
def test_live_adapter_combines_all_degradation_signals(pack, audit, expected):
    assert benchmark_recall_quality._pack_is_degraded(pack, audit) is expected


def test_configured_absolute_gate_fails_closed():
    report = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="deterministic",
        candidate="legacy",
        warmup=0,
        repeat=1,
        thresholds={"min_mrr": 1.01},
    )

    assert report["gate"]["status"] == "fail"
    assert report["gate"]["failures"] == [
        {
            "gate": "absolute",
            "metric": "mrr",
            "direction": "minimum",
            "current": 1.0,
            "limit": 1.01,
            "passed": False,
        }
    ]


def test_compact_v2_deterministic_report_runs_but_remains_nonpublishable():
    report = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="deterministic",
        candidate="compact-v2",
        warmup=0,
        repeat=1,
    )

    assert report["candidate"] == "compact-v2"
    assert report["backend"]["index_text_policy"] == "compact-v2"
    assert report["publishable_claim"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(publishable_claim=False),
        lambda report: report["backend"].update(mode="deterministic", deterministic=True),
        lambda report: report["backend"].update(fallback_used=True),
        lambda report: report["backend"].update(model="different-model"),
        lambda report: report["backend"].update(dimension=768),
        lambda report: report.update(dataset_revision="different-revision"),
        lambda report: report["corpus"].update(revision="different-corpus"),
        lambda report: report["corpus"].update(provenance_revision="different-provenance"),
        lambda report: report["corpus"].update(sha256="b" * 64),
        lambda report: report["corpus"].update(count=95),
        lambda report: report["backend"].update(runtime={"os": "different"}),
        lambda report: report["execution"].update(warmup=0),
        lambda report: report["execution"].update(repeat=2),
        lambda report: report.update(environment={"provider": "different"}),
        lambda report: report["isolated_corpus"].update(seeded=False),
        lambda report: report["isolated_corpus"].update(derived_count=63),
        lambda report: report["metrics"].update(
            fallback_rate=0.1, fallback_or_degradation_rate=0.1
        ),
        lambda report: report["metrics"].update(
            degradation_rate=0.1, fallback_or_degradation_rate=0.1
        ),
        lambda report: report["smoke"].update(passed=False),
        lambda report: report["smoke"].update(store=False),
        lambda report: report["smoke"].update(recall=False),
        lambda report: report["smoke"].update(supply=False),
        lambda report: report["smoke"].update(verified_visible=False),
        lambda report: report["smoke"].update(forbidden_hidden=False),
        lambda report: report.update(candidate="legacy"),
    ],
)
def test_report_gate_rejects_nonpublishable_or_incomparable_live_reports(mutation):
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    mutation(candidate)

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert not result["passed"]
    assert result["comparability"]["passed"] is False


def test_report_gate_rejects_nonlegacy_baseline_identity():
    baseline = _comparable_live_report("compact-v2")
    candidate = _comparable_live_report("compact-v2")

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert not result["passed"]
    assert result["comparability"]["passed"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(schema_version="unknown-report-schema"),
        lambda report: report.update(dataset_schema_version="unknown-dataset-schema"),
        lambda report: report.update(dataset_revision=""),
        lambda report: report["corpus"].update(revision=""),
        lambda report: report["corpus"].update(sha256="not-a-sha256"),
        lambda report: report["execution"].update(warmup=-1),
        lambda report: report["execution"].update(repeat=0),
        lambda report: report["backend"].update(runtime={}),
        lambda report: report.update(environment={}),
        lambda report: report["environment"].update(dependencies={}),
        lambda report: report["isolated_corpus"].update(derived_count=63),
    ],
)
def test_report_gate_rejects_equally_invalid_metadata(mutation):
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    mutation(baseline)
    mutation(candidate)

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert not result["passed"]
    assert result["comparability"]["passed"] is False


def test_report_gate_rejects_reports_without_retrieval_dependency_versions():
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    baseline["environment"].pop("dependencies")
    candidate["environment"].pop("dependencies")

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False


@pytest.mark.parametrize(
    ("dependency", "version"),
    [
        ("lancedb", "0.30.0"),
        ("lancedb", "garbage"),
        ("pyarrow", "garbage"),
    ],
)
def test_report_gate_rejects_unsupported_or_invalid_dependency_versions(dependency, version):
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    for report in (baseline, candidate):
        report["environment"]["dependencies"][dependency] = version

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False


def test_report_gate_rejects_matching_but_stale_source_fingerprints():
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    for report in (baseline, candidate):
        report["environment"]["source_fingerprint"] = "f" * 64

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PP_VECTOR_WEIGHT", "0.91"),
        ("PP_QUERY_EXPANSION", "0"),
        ("PP_FTS_DISABLED", "1"),
        ("PP_FTS_FUSION", "0"),
    ],
)
def test_report_gate_rejects_matching_noncanonical_retrieval_configuration(name, value):
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    for report in (baseline, candidate):
        report["environment"]["retrieval_configuration"][name] = value

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False


@pytest.mark.parametrize(
    "missing_metric",
    ["mrr", "hit_at", "fallback_rate", "degradation_rate", "fallback_or_degradation_rate"],
)
def test_report_gate_rejects_equally_missing_required_quality_metric(missing_metric):
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    del baseline["metrics"][missing_metric]
    del candidate["metrics"][missing_metric]

    with pytest.raises(ValueError, match=missing_metric):
        benchmark_recall_quality.compare_recall_quality_reports(
            baseline,
            candidate,
            tolerances={"default": 0.01},
            max_p95_ratio=1.2,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda metrics: metrics.update(hit_at={"5": 1.0}), "hit_at"),
        (lambda metrics: metrics.update(case_count=0), "case_count"),
        (lambda metrics: metrics.update(mrr=-0.01), "mrr"),
        (lambda metrics: metrics.update(mrr=True), "mrr"),
        (lambda metrics: metrics.update(mrr="1.0"), "mrr"),
        (lambda metrics: metrics.update(forbidden_hit_rate=-0.01), "forbidden_hit_rate"),
        (lambda metrics: metrics.update(fallback_rate=1.01), "fallback_rate"),
        (lambda metrics: metrics.update(degradation_rate=-0.01), "degradation_rate"),
        (
            lambda metrics: metrics.update(
                fallback_rate=0.0,
                degradation_rate=0.0,
                fallback_or_degradation_rate=1.0,
            ),
            "fallback_or_degradation_rate",
        ),
        (lambda metrics: metrics.update(p50_ms=-1.0), "p50_ms"),
        (lambda metrics: metrics.update(p50_ms=3.0, p95_ms=2.0), "p50_ms"),
    ],
)
def test_report_gate_rejects_equally_invalid_metric_values(mutation, expected_error):
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    mutation(baseline["metrics"])
    mutation(candidate["metrics"])

    with pytest.raises(ValueError, match=expected_error):
        benchmark_recall_quality.compare_recall_quality_reports(
            baseline,
            candidate,
            tolerances={"default": 0.01},
            max_p95_ratio=1.2,
        )


def test_report_gate_rejects_incomplete_fixed_case_splits():
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    for report in (baseline, candidate):
        del report["metrics"]["language"]["cross-lingual"]
        del report["metrics"]["group"]["partial-overlap"]

    with pytest.raises(ValueError, match="split"):
        benchmark_recall_quality.compare_recall_quality_reports(
            baseline,
            candidate,
            tolerances={"default": 0.01},
            max_p95_ratio=1.2,
        )


def test_report_gate_rejects_case_contract_mismatch_even_when_reports_match():
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    for report in (baseline, candidate):
        report["cases"]["count"] = 31

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert result["passed"] is False
    assert result["comparability"]["passed"] is False


def test_report_gate_accepts_only_matching_live_legacy_and_compact_reports():
    result = benchmark_recall_quality.compare_recall_quality_reports(
        _comparable_live_report("legacy"),
        _comparable_live_report("compact-v2"),
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert result["passed"] is True
    assert result["comparability"]["passed"] is True
    assert result["best_constituent_gate"]["passed"] is True


def test_report_gate_rejects_candidate_below_best_constituent():
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    candidate["metrics"]["channels"]["fused"]["overall"]["mrr"] = 0.0

    result = benchmark_recall_quality.compare_recall_quality_reports(
        baseline,
        candidate,
        tolerances={"default": 0.01},
        max_p95_ratio=1.2,
    )

    assert result["passed"] is False
    assert result["best_constituent_gate"]["passed"] is False


def test_cli_gate_rejects_deterministic_reports(tmp_path):
    baseline = _comparable_live_report("legacy")
    candidate = _comparable_live_report("compact-v2")
    for report in (baseline, candidate):
        report["backend"].update(mode="deterministic", deterministic=True)
        report["publishable_claim"] = False
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    output_path = tmp_path / "gate.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    returncode = benchmark_recall_quality.main(
        [
            "--gate",
            "--baseline",
            str(baseline_path),
            "--candidate-report",
            str(candidate_path),
            "--output",
            str(output_path),
        ]
    )

    assert returncode == 1
    assert json.loads(output_path.read_text(encoding="utf-8"))["passed"] is False


def _public_backend_evidence(dataset, *, index_text_policy, fusion_policy, paths=None):
    counts = {
        "memory_store": len(dataset.corpus),
        "feedback_apply": 2,
        "memory_recall": 0,
        "context_supply": 0,
    }

    def retrieve(case):
        counts["memory_recall"] += 1
        counts["context_supply"] += 1
        relevant = case.relevant_memory_ids[0]
        ranking = [{"id": relevant, "score": 1.0, "rank": 1}]
        return {
            "ranked_ids": [relevant],
            "channel_rankings": {"vector": ranking, "bm25": ranking},
            "channel_states": {
                "vector": _channel_state(),
                "bm25": _channel_state(),
            },
        }

    return (
        retrieve,
        {
            "model": "real-model",
            "dimension": 1024,
            "index_text_policy": index_text_policy,
            "runtime": {"os": "test"},
            "transport": "streamable-http",
            "server_pid": 4101,
            "requested_policy": fusion_policy,
            "effective_policy": fusion_policy,
            "requested_runtime": "python",
            "effective_runtime": "python",
        },
        {
            "public_call_counts": counts,
            "fusion_attested": True,
            "isolated_corpus": {
                "seeded": True,
                "canonical_count": len(dataset.corpus),
                "derived_count": len(dataset.corpus),
                "eligible_count": len(dataset.corpus),
            },
            "smoke": {
                "store": True,
                "recall": True,
                "supply": True,
                "verified_visible": True,
                "forbidden_hidden": True,
                "passed": True,
            },
        },
    )


def test_live_backend_requires_health_pid_equal_spawned_process(tmp_path, monkeypatch):
    dataset = recall_quality.load_dataset_bundle(DATASET)
    paths = benchmark_recall_quality.LivePaths(
        root=tmp_path,
        sqlite=tmp_path / "canonical.db",
        lancedb=tmp_path / "lancedb",
    )
    seeded = False

    class FakeManaged:
        pid = 4101
        stdout_path = tmp_path / "stdout.log"
        stderr_path = tmp_path / "stderr.log"

        def terminate(self, timeout=10.0):
            return None

    monkeypatch.setattr(
        benchmark_recall_quality.ManagedProcess,
        "start",
        lambda *args, **kwargs: FakeManaged(),
    )

    async def mismatched_health(*args, **kwargs):
        return {"status": "ok", "pid": 4102}

    async def forbidden_seed(*args, **kwargs):
        nonlocal seeded
        seeded = True

    monkeypatch.setattr(benchmark_recall_quality, "wait_for_health", mismatched_health)
    monkeypatch.setattr(benchmark_recall_quality, "_seed_public_corpus", forbidden_seed)

    with pytest.raises(RuntimeError, match="health_pid_mismatch"):
        asyncio.run(
            benchmark_recall_quality._http_live_backend(
                dataset,
                index_text_policy="legacy",
                fusion_policy="legacy-auto",
                paths=paths,
            )
        )
    assert seeded is False


def test_engine_diagnostic_report_can_never_be_publishable(monkeypatch):
    monkeypatch.setattr(
        benchmark_recall_quality,
        "_engine_diagnostic_backend",
        lambda dataset, candidate, paths: _public_backend_evidence(
            dataset,
            index_text_policy=candidate,
            fusion_policy="legacy-auto",
        ),
    )

    report = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="engine-diagnostic",
        candidate="legacy",
        fusion_policy="legacy-auto",
        warmup=0,
        repeat=1,
    )

    assert report["publishable_claim"] is False
    assert report["backend"]["mode"] == "engine-diagnostic"


def test_live_report_requires_public_recall_and_context_calls_for_every_case(monkeypatch):
    async def public_backend(dataset, **kwargs):
        return _public_backend_evidence(dataset, **kwargs)

    monkeypatch.setattr(benchmark_recall_quality, "_http_live_backend", public_backend)
    monkeypatch.setattr(
        benchmark_recall_quality,
        "_engine_diagnostic_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("live backend used direct ContextEngine")
        ),
    )

    report = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="live",
        candidate="legacy",
        fusion_policy="max-v1",
        warmup=0,
        repeat=1,
    )

    assert report["public_call_counts"]["memory_recall"] == report["cases"]["count"]
    assert report["public_call_counts"]["context_supply"] == report["cases"]["count"]
    assert report["backend"]["transport"] == "streamable-http"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("effective_policy", "legacy-auto", "requested and effective retrieval policy differ"),
        ("effective_runtime", "rust", "requested and effective retrieval runtime differ"),
    ],
)
def test_requested_and_effective_policy_runtime_must_match(field, value, reason, monkeypatch):
    async def mismatched_backend(dataset, **kwargs):
        retrieve, metadata, evidence = _public_backend_evidence(dataset, **kwargs)
        metadata[field] = value
        return retrieve, metadata, evidence

    monkeypatch.setattr(benchmark_recall_quality, "_http_live_backend", mismatched_backend)
    report = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="live",
        candidate="legacy",
        fusion_policy="max-v1",
        warmup=0,
        repeat=1,
    )

    assert report["publishable_claim"] is False
    assert report["publishability_reason"] == reason


def test_phase_b_cli_exposes_exact_experiment_surface(tmp_path):
    args = benchmark_recall_quality.build_argparser().parse_args(
        [
            "--dataset",
            str(DATASET),
            "--backend",
            "live",
            "--index-text-policy",
            "legacy",
            "--fusion-grid",
            str(tmp_path / "grid.json"),
            "--calibrate",
            "--heldout-dataset",
            str(tmp_path / "heldout.json"),
            "--freeze-manifest",
            str(tmp_path / "manifest.json"),
            "--fusion-policy",
            "wrrf-v1",
            "--candidate-manifest",
            str(tmp_path / "candidate.json"),
        ]
    )

    assert args.calibrate is True
    assert args.candidate == "legacy"
    assert args.fusion_grid.name == "grid.json"
    assert args.heldout_dataset.name == "heldout.json"
    assert args.freeze_manifest.name == "manifest.json"
    assert args.candidate_manifest.name == "candidate.json"


def test_calibration_inputs_hash_heldout_without_loading_cases(tmp_path, monkeypatch):
    heldout = tmp_path / "heldout.json"
    heldout.write_text("opaque heldout bytes", encoding="utf-8")
    loaded = []

    def load(path):
        loaded.append(Path(path))
        if Path(path) == heldout:
            raise AssertionError("heldout cases were opened during calibration")
        return recall_quality.load_dataset_bundle(Path(path))

    monkeypatch.setattr(benchmark_recall_quality, "load_dataset_bundle", load)
    calibration, fingerprint = benchmark_recall_quality._calibration_inputs(
        DATASET,
        heldout,
    )

    assert calibration.evidence_role == "calibration"
    assert loaded == [DATASET]
    assert len(fingerprint) == 64


def test_bare_wrrf_requires_manifest_and_normalizes_to_hash_candidate():
    candidate_id = f"wrrf-v1:{'a' * 64}"
    manifest = SimpleNamespace(candidate_id=candidate_id)

    with pytest.raises(ValueError, match="manifest_required"):
        benchmark_recall_quality._resolve_fusion_policy("wrrf-v1", None)
    assert benchmark_recall_quality._resolve_fusion_policy("wrrf-v1", manifest) == candidate_id


def test_manifest_gate_delegates_to_strict_experiment_comparator(monkeypatch):
    observed = {}

    def compare(baseline, candidate, *, manifest, tolerances):
        observed.update(
            baseline=baseline,
            candidate=candidate,
            manifest=manifest,
            tolerances=tolerances,
        )
        return {"passed": True, "comparability": {"passed": True}}

    monkeypatch.setattr(benchmark_recall_quality, "compare_fusion_reports", compare)
    manifest = SimpleNamespace(candidate_id=f"wrrf-v1:{'b' * 64}")
    result = benchmark_recall_quality._compare_manifest_reports(
        {"candidate_id": "max-v1"},
        {"candidate_id": manifest.candidate_id},
        manifest=manifest,
        tolerance=0.0,
        max_p95_ratio=1.2,
    )

    assert result["passed"] is True
    assert observed["manifest"] is manifest
    assert observed["tolerances"]["required_split_tolerance"] == 0.0
    assert observed["tolerances"]["max_p95_ratio"] == 1.2


def test_deterministic_calibration_math_never_requires_or_writes_manifest(tmp_path):
    output = tmp_path / "math.json"
    result = benchmark_recall_quality.main(
        [
            "--dataset",
            str(DATASET),
            "--backend",
            "deterministic",
            "--index-text-policy",
            "legacy",
            "--fusion-grid",
            str(ROOT / "tests/fixtures/recall_quality/wrrf-v1-grid.json"),
            "--calibrate",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert payload["publishable_claim"] is False
    assert payload["candidate_manifest"] is None
    assert payload["selected_candidate"].startswith("wrrf-v1:")


def test_calibration_failure_preserves_reports_without_opening_heldout(tmp_path, monkeypatch):
    output = tmp_path / "failed-calibration.json"

    def reject_candidates(_reports, _grid):
        raise ValueError("no_calibration_candidate")

    monkeypatch.setattr(
        benchmark_recall_quality,
        "select_calibration_candidate",
        reject_candidates,
    )
    result = benchmark_recall_quality.main(
        [
            "--dataset",
            str(DATASET),
            "--backend",
            "deterministic",
            "--index-text-policy",
            "legacy",
            "--fusion-grid",
            str(ROOT / "tests/fixtures/recall_quality/wrrf-v1-grid.json"),
            "--calibrate",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result == 2
    assert payload["error"] == "no_calibration_candidate"
    assert payload["publishable_claim"] is False
    assert payload["selected_candidate"] is None
    assert payload["candidate_manifest"] is None
    assert payload["baseline"]["candidate_id"] == "max-v1"
    assert payload["candidates"]
    assert payload["heldout_queries_executed"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(publishable_claim=False),
        lambda report: report["metrics"].update(fallback_rate=0.1),
        lambda report: report["metrics"].update(degradation_rate=0.1),
        lambda report: report["fusion_attestation"].update(errors=["memory_recall:missing"]),
    ],
)
def test_live_calibration_rejects_unpublishable_baseline(mutation):
    baseline = _comparable_live_report("legacy")
    baseline["backend"].update(
        transport="streamable-http",
        requested_policy="max-v1",
        effective_policy="max-v1",
        requested_runtime="python",
        effective_runtime="python",
    )
    baseline["fusion_attestation"] = {
        "attested_calls": 64,
        "errors": [],
        "observed": ["max-v1", "python", "python"],
    }
    baseline["public_transport_call_counts"] = {
        "memory_recall": 32,
        "context_supply": 32,
    }
    assert benchmark_recall_quality._validate_live_calibration_evidence(baseline) is True
    mutation(baseline)

    with pytest.raises(ValueError, match="live_calibration_baseline_not_publishable"):
        benchmark_recall_quality._validate_live_calibration_evidence(baseline)


def test_live_report_fails_closed_without_complete_public_fusion_attestation(monkeypatch):
    async def unattested_backend(dataset, **kwargs):
        retrieve, metadata, evidence = _public_backend_evidence(dataset, **kwargs)
        metadata["effective_policy"] = None
        metadata["effective_runtime"] = None
        evidence["fusion_attested"] = False
        evidence["fusion_attestation_error"] = "memory_recall:missing"
        return retrieve, metadata, evidence

    monkeypatch.setattr(benchmark_recall_quality, "_http_live_backend", unattested_backend)
    report = benchmark_recall_quality.run_benchmark(
        dataset_path=DATASET,
        backend="live",
        candidate="legacy",
        fusion_policy="max-v1",
        warmup=0,
        repeat=1,
    )

    assert report["publishable_claim"] is False
    assert report["publishability_reason"] == "public fusion attestation was incomplete"
