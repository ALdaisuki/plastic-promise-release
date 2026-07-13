"""Run the versioned bilingual recall-quality benchmark."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plastic_promise.core.fusion_policy import (  # noqa: E402
    FUSION_CHANNEL_ORDER,
    resolve_cli_fusion_policy,
)
from plastic_promise.core.recall_experiment import (  # noqa: E402
    RECALL_QUALITY_REPORT_SCHEMA,
    FrozenCandidateManifest,
    _config_payload,
    _grid_configs,
    calibrate_and_freeze,
    compare_fusion_reports,
    dataset_fingerprint,
    load_calibration_grid,
    load_frozen_manifest,
    opaque_file_fingerprint,
    select_calibration_candidate,
)
from plastic_promise.core.recall_quality import (  # noqa: E402
    REPORT_SCHEMA_VERSION,
    MetricSummary,
    RecallCase,
    RecallDataset,
    compare_summaries,
    evaluate_best_constituent_gate,
    evaluate_cases,
    load_dataset_bundle,
    metric_summary_from_dict,
    quality_payload,
)
from scripts.http_mcp_harness import (  # noqa: E402
    ManagedProcess,
    call_tool_json,
    call_tools_json,
    free_tcp_port,
    process_environment,
    require_owned_health,
    runtime_python,
    sanitized_log_tail,
    wait_for_health,
)

BENCHMARK_SOURCE_PATHS = (
    ROOT / "scripts" / "benchmark_recall_quality.py",
    ROOT / "scripts" / "http_mcp_harness.py",
    ROOT / "pyproject.toml",
)
RETRIEVAL_DEPENDENCY_MINIMUMS = {"lancedb": Version("0.34.0")}
LIVE_RETRIEVAL_CONFIGURATION = {
    "PP_VECTOR_WEIGHT": "0.50",
    "PP_QUERY_EXPANSION": "1",
    "PP_FTS_DISABLED": "0",
    "PP_FTS_FUSION": "1",
}
KNOWN_DATASET_CONTRACTS: dict[tuple[str, str], dict[str, Any]] = {
    ("recall-quality/v1", "2026-07-10.2"): {
        "corpus.revision": "2026-07-10.2-corpus.1",
        "corpus.provenance_revision": "2026-07-10.2-provenance.1",
        "corpus.sha256": "61c0c1002f23375404ab9aee768996dfc3f81eab2f9b8c0a8aa23e27744567ae",
        "corpus.count": 96,
        "cases.sha256": "fdd8c24359772a4b386b140d4514d26849f047d4d1955ef2e030260635035938",
        "cases.count": 32,
    }
}


@dataclass(frozen=True)
class LivePaths:
    root: Path
    sqlite: Path
    lancedb: Path


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure fixed bilingual retrieval quality without changing retrieval policy"
    )
    parser.add_argument("--dataset", type=Path, help="Versioned recall-quality JSON dataset")
    parser.add_argument(
        "--backend",
        choices=("deterministic", "live", "engine-diagnostic"),
        default="deterministic",
        help="Metric verifier, public HTTP MCP, or direct engine diagnostic",
    )
    parser.add_argument(
        "--candidate",
        "--index-text-policy",
        dest="candidate",
        choices=("legacy", "compact-v2"),
        default="legacy",
        help="Index-text policy under measurement",
    )
    parser.add_argument(
        "--fusion-policy",
        default="legacy-auto",
        help="Normalized fusion policy: legacy-auto, max-v1, or hash-qualified wrrf-v1",
    )
    parser.add_argument("--fusion-grid", type=Path)
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--heldout-dataset", type=Path)
    parser.add_argument("--freeze-manifest", type=Path)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--output", type=Path, help="Write the JSON report to this path")
    parser.add_argument("--warmup", type=int, default=0, help="Warmup calls per case")
    parser.add_argument("--repeat", type=int, default=1, help="Measured calls per case")

    absolute = parser.add_argument_group("absolute quality gates")
    absolute.add_argument("--min-hit-at-1", "--min-hit1", dest="min_hit_at_1", type=float)
    absolute.add_argument("--min-hit-at-5", "--min-hit5", dest="min_hit_at_5", type=float)
    absolute.add_argument("--min-mrr", type=float)
    absolute.add_argument("--max-forbidden-hit-rate", type=float)
    absolute.add_argument("--max-fallback-rate", type=float)
    absolute.add_argument("--max-degradation-rate", type=float)
    absolute.add_argument("--max-p95-ms", type=float)

    comparison = parser.add_argument_group("baseline comparison")
    comparison.add_argument("--gate", action="store_true", help="Compare two existing reports")
    comparison.add_argument("--baseline", type=Path)
    comparison.add_argument("--candidate-report", type=Path)
    comparison.add_argument("--tolerance", type=float, default=0.0)
    comparison.add_argument("--max-hit5-regression", type=float)
    comparison.add_argument("--max-mrr-regression", type=float)
    comparison.add_argument("--max-forbidden-hit-increase", type=float)
    comparison.add_argument("--max-p95-ratio", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)
    if args.gate:
        return _compare_reports(args, parser)
    if args.calibrate:
        return _run_calibration_cli(args, parser)
    if args.dataset is None:
        parser.error("--dataset is required unless --gate is used")
    if args.output is None:
        parser.error("--output is required unless --gate is used")
    if args.warmup < 0:
        parser.error("--warmup must be non-negative")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    try:
        manifest = (
            load_frozen_manifest(args.candidate_manifest)
            if args.candidate_manifest is not None
            else None
        )
        fusion_policy = _resolve_fusion_policy(args.fusion_policy, manifest)
        with _fusion_environment(manifest, fusion_policy):
            report = run_benchmark(
                dataset_path=args.dataset,
                backend=args.backend,
                candidate=args.candidate,
                fusion_policy=fusion_policy,
                warmup=args.warmup,
                repeat=args.repeat,
                thresholds=_absolute_thresholds(args),
            )
        if manifest is not None:
            dataset = load_dataset_bundle(args.dataset)
            report = _bind_experiment_report(
                report,
                dataset=dataset,
                dataset_path=args.dataset,
                fusion_policy=fusion_policy,
                manifest=manifest,
            )
            _validate_manifest_bound_report(report, manifest)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"recall-quality benchmark failed: {exc}", file=sys.stderr)
        return 2

    _write_json(args.output, report)
    print(json.dumps(_report_console_summary(report), ensure_ascii=False, sort_keys=True))
    return 1 if report["gate"]["status"] == "fail" else 0


def _run_calibration_cli(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    for name in ("dataset", "fusion_grid", "output"):
        if getattr(args, name) is None:
            parser.error(f"--{name.replace('_', '-')} is required with --calibrate")
    if args.backend not in {"deterministic", "live"}:
        parser.error("--calibrate requires --backend deterministic or live")
    if args.backend == "live":
        for name in ("heldout_dataset", "freeze_manifest"):
            if getattr(args, name) is None:
                parser.error(f"--{name.replace('_', '-')} is required for live calibration")
    if args.warmup < 0 or args.repeat < 1:
        parser.error("calibration requires non-negative warmup and positive repeat")
    try:
        if args.backend == "live":
            calibration, heldout_fingerprint = _calibration_inputs(
                args.dataset,
                args.heldout_dataset,
            )
        else:
            calibration = load_dataset_bundle(args.dataset)
            if calibration.evidence_role != "calibration":
                raise ValueError("calibration_evidence_role_required")
            heldout_fingerprint = ""
        grid = load_calibration_grid(args.fusion_grid)
        baseline_raw = run_benchmark(
            dataset_path=args.dataset,
            backend=args.backend,
            candidate=args.candidate,
            fusion_policy="max-v1",
            warmup=args.warmup,
            repeat=args.repeat,
            thresholds=_absolute_thresholds(args),
        )
        baseline = _bind_experiment_report(
            baseline_raw,
            dataset=calibration,
            dataset_path=args.dataset,
            fusion_policy="max-v1",
        )
        calibration_publishable = False
        if args.backend == "live":
            calibration_publishable = _validate_live_calibration_evidence(baseline)
        reports: list[dict[str, Any]] = []
        for config in _grid_configs(grid).values():
            candidate_id = f"wrrf-v1:{config.config_hash}"
            with _calibration_fusion_environment(config):
                raw = run_benchmark(
                    dataset_path=args.dataset,
                    backend=args.backend,
                    candidate=args.candidate,
                    fusion_policy=candidate_id,
                    warmup=args.warmup,
                    repeat=args.repeat,
                    thresholds=_absolute_thresholds(args),
                )
            report = _bind_experiment_report(
                raw,
                dataset=calibration,
                dataset_path=args.dataset,
                fusion_policy=candidate_id,
                fusion_config=_config_payload(config),
            )
            report.update(_calibration_selection_payload(baseline, report, grid))
            reports.append(report)
        if not reports:
            raise ValueError("calibration_grid_empty")
        try:
            selected = select_calibration_candidate(reports, grid)
        except ValueError as exc:
            _write_json(
                args.output,
                {
                    "schema": "recall-calibration-run/v1",
                    "publishable_claim": False,
                    "publishability_reason": str(exc),
                    "dataset_fingerprint": dataset_fingerprint(calibration),
                    "heldout_fingerprint": heldout_fingerprint or None,
                    "selected_candidate": None,
                    "candidate_manifest": None,
                    "baseline": baseline,
                    "candidates": reports,
                    "error": str(exc),
                    "heldout_queries_executed": 0,
                },
            )
            raise
        manifest = None
        if args.backend == "live":
            exemplar = reports[0]
            manifest = calibrate_and_freeze(
                reports=reports,
                grid=grid,
                calibration=calibration,
                heldout_fingerprint=heldout_fingerprint,
                manifest_path=args.freeze_manifest,
                source_commit=_nested_value(exemplar, "environment.source_commit"),
                dirty_fingerprint=_nested_value(exemplar, "environment.dirty_fingerprint"),
                retrieval_configuration=_nested_value(
                    exemplar, "environment.retrieval_configuration"
                ),
                embedding_configuration=_nested_value(
                    exemplar, "environment.embedding_configuration"
                ),
                dependency_versions=_nested_value(exemplar, "environment.dependencies"),
                runtime_route=_nested_value(exemplar, "backend.runtime_route"),
            )
        payload = {
            "schema": "recall-calibration-run/v1",
            "publishable_claim": bool(manifest is not None and calibration_publishable),
            "publishability_reason": (
                "public live calibration with frozen held-out binding"
                if args.backend == "live"
                else "deterministic calibration verifies selection math only"
            ),
            "dataset_fingerprint": dataset_fingerprint(calibration),
            "heldout_fingerprint": heldout_fingerprint or None,
            "selected_candidate": (
                manifest.candidate_id if manifest is not None else selected["candidate_id"]
            ),
            "candidate_manifest": manifest.to_dict() if manifest is not None else None,
            "baseline": baseline,
            "candidates": reports,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"recall-quality calibration failed: {exc}", file=sys.stderr)
        return 2
    _write_json(args.output, payload)
    print(
        json.dumps(
            {
                "selected_candidate": payload["selected_candidate"],
                "manifest_hash": manifest.manifest_hash if manifest is not None else None,
                "candidate_count": len(reports),
            },
            sort_keys=True,
        )
    )
    return 0


def _validate_live_calibration_evidence(report: Mapping[str, Any]) -> bool:
    """Reject an invalid max-v1 baseline before it can influence candidate selection."""

    attestation = report.get("fusion_attestation")
    transport_counts = report.get("public_transport_call_counts")
    requested_runtime = _nested_value(report, "backend.requested_runtime")
    effective_runtime = _nested_value(report, "backend.effective_runtime")
    fallback_rate = _nested_value(report, "metrics.fallback_rate")
    degradation_rate = _nested_value(report, "metrics.degradation_rate")

    def zero_rate(value: Any) -> bool:
        return type(value) in {int, float} and float(value) == 0.0

    failures = (
        report.get("publishable_claim") is not True,
        _nested_value(report, "backend.mode") != "live",
        _nested_value(report, "backend.transport") != "streamable-http",
        _nested_value(report, "backend.requested_policy") != "max-v1",
        _nested_value(report, "backend.effective_policy") != "max-v1",
        not requested_runtime or requested_runtime != effective_runtime,
        not zero_rate(fallback_rate),
        not zero_rate(degradation_rate),
        not isinstance(attestation, Mapping),
        not isinstance(transport_counts, Mapping),
    )
    if any(failures):
        raise ValueError("live_calibration_baseline_not_publishable")
    assert isinstance(attestation, Mapping)
    assert isinstance(transport_counts, Mapping)
    observed = ["max-v1", requested_runtime, effective_runtime]
    if set(transport_counts) != {"memory_recall", "context_supply"} or any(
        type(value) is not int or value <= 0 for value in transport_counts.values()
    ):
        raise ValueError("live_calibration_baseline_not_publishable")
    transport_total = sum(transport_counts.values())
    if (
        attestation.get("errors") != []
        or attestation.get("observed") != observed
        or not isinstance(attestation.get("attested_calls"), int)
        or isinstance(attestation.get("attested_calls"), bool)
        or attestation.get("attested_calls") != transport_total
        or transport_total <= 0
    ):
        raise ValueError("live_calibration_baseline_not_publishable")
    return True


def _calibration_selection_payload(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    grid: Mapping[str, Any],
) -> dict[str, Any]:
    baseline_slices = _experiment_fused_slices(baseline)
    candidate_slices = _experiment_fused_slices(candidate)
    required_names = {
        "overall",
        "language:en",
        "language:zh",
        "language:cross-lingual",
        "group:token-overlap",
        "group:partial-overlap",
        "group:zero-overlap",
    }
    tolerance = float(grid["required_split_tolerance"])

    def metric(report_slice: Mapping[str, Any], name: str) -> float:
        if name == "mrr":
            return float(report_slice.get("mrr", 0.0))
        hit_at = report_slice.get("hit_at") or {}
        return float(hit_at.get("5", hit_at.get(5, 0.0)))

    comparable = required_names <= set(baseline_slices) & set(candidate_slices)
    split_pass = comparable and all(
        metric(candidate_slices[name], metric_name) + tolerance
        >= metric(baseline_slices[name], metric_name)
        for name in required_names
        for metric_name in ("mrr", "hit5")
    )
    overall = candidate_slices.get("overall", {})
    minimum_mrr = min(
        (metric(candidate_slices[name], "mrr") for name in required_names),
        default=0.0,
    )
    base_p95 = float(_nested_value(baseline, "metrics.p95_ms") or 0.0)
    candidate_p95 = float(_nested_value(candidate, "metrics.p95_ms") or 0.0)
    deterministic_math = (
        _nested_value(baseline, "backend.mode") == "deterministic"
        and _nested_value(candidate, "backend.mode") == "deterministic"
    )
    latency_pass = (deterministic_math and base_p95 == candidate_p95 == 0.0) or (
        base_p95 > 0.0 and candidate_p95 <= base_p95 * float(grid["max_p95_ratio"])
    )
    return {
        "calibration_gate": {
            "overall_no_regression": split_pass,
            "required_splits_no_regression": split_pass,
            "best_constituent_no_regression": bool(
                (candidate.get("best_constituent_gate") or {}).get("passed")
            ),
            "forbidden_hits_not_increased": float(
                _nested_value(candidate, "metrics.forbidden_hit_rate") or 0.0
            )
            <= float(_nested_value(baseline, "metrics.forbidden_hit_rate") or 0.0),
            "no_degradation": float(_nested_value(candidate, "metrics.fallback_rate") or 0.0) == 0.0
            and float(_nested_value(candidate, "metrics.degradation_rate") or 0.0) == 0.0,
            "latency_within_budget": latency_pass,
        },
        "selection_metrics": {
            "minimum_required_split_fused_mrr": minimum_mrr,
            "overall_fused_hit_at_5": metric(overall, "hit5"),
            "overall_fused_mrr": metric(overall, "mrr"),
            "p95_ms": candidate_p95,
        },
    }


def _experiment_fused_slices(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    fused = _nested_value(report, "metrics.channels.fused")
    if not isinstance(fused, Mapping):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(fused.get("overall"), Mapping):
        result["overall"] = fused["overall"]
    for kind, field in (("language", "by_language"), ("group", "by_group")):
        values = fused.get(field)
        if isinstance(values, Mapping):
            result.update(
                {
                    f"{kind}:{name}": value
                    for name, value in values.items()
                    if isinstance(value, Mapping)
                }
            )
    return result


def run_benchmark(
    *,
    dataset_path: Path,
    backend: str,
    candidate: str,
    fusion_policy: str = "legacy-auto",
    warmup: int,
    repeat: int,
    thresholds: Mapping[str, float | None] | None = None,
) -> dict[str, Any]:
    """Run one candidate/backend pair and return a JSON-ready report."""

    if candidate not in {"legacy", "compact-v2"}:
        raise ValueError(f"unknown candidate: {candidate}")
    fusion_policy = _normalized_fusion_policy(fusion_policy)
    dataset = load_dataset_bundle(dataset_path)
    cases = list(dataset.cases)
    live_evidence: dict[str, Any] = {
        "isolated_corpus": {
            "seeded": False,
            "canonical_count": 0,
            "derived_count": 0,
        },
        "smoke": {"store": False, "recall": False, "supply": False, "passed": False},
    }
    if backend == "deterministic":
        base_retrieve = _deterministic_retrieve
        cold_latency_ms = 0.0
        cold_fallback = False
        cold_degraded = False
        backend_metadata = _deterministic_backend_metadata(candidate)
        retrieve = _repeat_retriever(base_retrieve, warmup=warmup, repeat=repeat)
        summary = evaluate_cases(cases, retrieve, ks=(1, 3, 5, 10))
        environment = _environment_metadata(dataset_path)
    elif backend in {"live", "engine-diagnostic"}:
        with _isolated_live_environment(candidate) as paths:
            if backend == "live":
                base_retrieve, backend_metadata, live_evidence = asyncio.run(
                    _http_live_backend(
                        dataset,
                        index_text_policy=candidate,
                        fusion_policy=fusion_policy,
                        paths=paths,
                    )
                )
            else:
                base_retrieve, backend_metadata, live_evidence = _engine_diagnostic_backend(
                    dataset, candidate, paths
                )
            cleanup = live_evidence.pop("_cleanup", None)
            try:
                if backend == "engine-diagnostic":
                    cold_started = time.perf_counter()
                    cold_result = base_retrieve(cases[0])
                    cold_latency_ms = (time.perf_counter() - cold_started) * 1000.0
                    if isinstance(cold_result, Mapping):
                        cold_latency_ms = float(cold_result.get("latency_ms", cold_latency_ms))
                        cold_fallback = _as_bool(cold_result.get("fallback_used", False))
                        cold_degraded = _as_bool(cold_result.get("degraded", False))
                    else:
                        cold_fallback = False
                        cold_degraded = False
                else:
                    cold_latency_ms = 0.0
                    cold_fallback = False
                    cold_degraded = False
                retrieve = _repeat_retriever(base_retrieve, warmup=warmup, repeat=repeat)
                summary = evaluate_cases(cases, retrieve, ks=(1, 3, 5, 10))
                environment = _environment_metadata(dataset_path)
            finally:
                if callable(cleanup):
                    cleanup()
                retrieve = None
                base_retrieve = None
                cleanup = None
                gc.collect()
    else:
        raise ValueError(f"unknown backend: {backend}")

    absolute_gate = _evaluate_absolute_gate(summary, thresholds or {})
    constituent_gate = evaluate_best_constituent_gate(summary)
    gate = _combine_quality_gates(absolute_gate, constituent_gate)
    fallback_used = bool(cold_fallback or summary.fallback_rate > 0.0)
    degraded_used = bool(cold_degraded or summary.degradation_rate > 0.0)
    backend_metadata.update(
        {
            "mode": backend,
            "deterministic": backend == "deterministic",
            "fallback_used": fallback_used,
            "degraded_used": degraded_used,
            "cold_latency_ms": cold_latency_ms,
            "warm_p50_ms": summary.p50_ms,
            "warm_p95_ms": summary.p95_ms,
            "channel_result_names": sorted(summary.channels),
        }
    )
    isolated_corpus = dict(live_evidence.get("isolated_corpus") or {})
    smoke = dict(live_evidence.get("smoke") or {})
    smoke_passed = bool(
        smoke.get("passed")
        and smoke.get("store")
        and smoke.get("recall")
        and smoke.get("supply")
        and smoke.get("verified_visible")
        and smoke.get("forbidden_hidden")
    )
    corpus_seeded = bool(
        isolated_corpus.get("seeded")
        and int(isolated_corpus.get("canonical_count", 0)) == dataset.corpus_count
        and int(isolated_corpus.get("eligible_count", 0)) > 0
        and int(isolated_corpus.get("derived_count", -1))
        == int(isolated_corpus.get("eligible_count", 0))
    )
    public_call_counts = dict(live_evidence.get("public_call_counts") or {})
    attestation_state = dict(live_evidence.get("fusion_attestation") or {})
    fusion_attested = bool(
        live_evidence.get("fusion_attested")
        or (
            not attestation_state.get("errors")
            and int(attestation_state.get("attested_calls", 0)) > 0
            and int(attestation_state.get("attested_calls", 0))
            == sum(
                int(value)
                for value in dict(live_evidence.get("public_transport_call_counts") or {}).values()
            )
        )
    )
    expected_public_queries = dataset.case_count
    public_surface_complete = bool(
        backend == "live"
        and backend_metadata.get("transport") == "streamable-http"
        and int(backend_metadata.get("server_pid", 0) or 0) > 0
        and int(public_call_counts.get("memory_recall", -1)) == expected_public_queries
        and int(public_call_counts.get("context_supply", -1)) == expected_public_queries
    )
    policy_matches = (
        backend_metadata.get("requested_policy") == fusion_policy
        and backend_metadata.get("effective_policy") == fusion_policy
    )
    runtime_matches = bool(
        backend_metadata.get("requested_runtime")
        and backend_metadata.get("requested_runtime") == backend_metadata.get("effective_runtime")
    )
    publishable_claim = bool(
        backend == "live"
        and public_surface_complete
        and fusion_attested
        and policy_matches
        and runtime_matches
        and not fallback_used
        and not degraded_used
        and corpus_seeded
        and smoke_passed
    )
    if backend == "engine-diagnostic":
        publishability_reason = "direct engine backends are diagnostic only"
    elif backend != "live":
        publishability_reason = "deterministic backends verify metric math only"
    elif not fusion_attested:
        publishability_reason = "public fusion attestation was incomplete"
    elif not policy_matches:
        publishability_reason = "requested and effective retrieval policy differ"
    elif not runtime_matches:
        publishability_reason = "requested and effective retrieval runtime differ"
    elif not public_surface_complete:
        publishability_reason = "public HTTP retrieval evidence is incomplete"
    elif fallback_used:
        publishability_reason = "a fallback backend was used"
    elif degraded_used:
        publishability_reason = "a degraded retrieval path was used"
    elif not corpus_seeded:
        publishability_reason = "the isolated corpus was not seeded completely"
    elif not smoke_passed:
        publishability_reason = "the store-recall-supply smoke did not pass"
    else:
        publishability_reason = "isolated live backend and store-recall-supply smoke passed"

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "dataset_schema_version": "recall-quality/v1",
        "dataset_revision": dataset.dataset_revision,
        "corpus": {
            "revision": dataset.corpus_revision,
            "provenance_revision": dataset.synthesis_provenance_revision,
            "sha256": dataset.corpus_hash,
            "count": dataset.corpus_count,
        },
        "cases": {
            "sha256": dataset.case_hash,
            "count": dataset.case_count,
        },
        "candidate": candidate,
        "backend": backend_metadata,
        "execution": {"warmup": warmup, "repeat": repeat},
        "environment": environment,
        "isolated_corpus": isolated_corpus,
        "smoke": smoke,
        "public_call_counts": public_call_counts,
        "public_transport_call_counts": dict(
            live_evidence.get("public_transport_call_counts") or {}
        ),
        "fusion_attestation": attestation_state,
        "server_logs": dict(live_evidence.get("server_logs") or {}),
        "publishable_claim": publishable_claim,
        "publishability_reason": publishability_reason,
        "metrics": summary.to_dict(include_cases=True),
        "quality": quality_payload(summary),
        "gate": gate,
        "best_constituent_gate": constituent_gate,
    }


def _deterministic_retrieve(case: RecallCase) -> dict[str, Any]:
    """Label-derived fixture backend used only to verify metric plumbing."""

    relevant = list(case.relevant_memory_ids)
    distractors = list(case.distractor_memory_ids)
    bm25 = [*relevant, *distractors] if case.group == "token-overlap" else [*distractors, *relevant]
    vector = [*relevant, *distractors]
    fused = [*relevant, *distractors]
    return {
        "ranked_ids": fused,
        "latency_ms": 0.0,
        "fallback_used": False,
        "degraded": False,
        "channel_rankings": {
            "bm25": _synthetic_channel_ranking(bm25),
            "vector": _synthetic_channel_ranking(vector),
        },
        "channel_states": {
            "bm25": _synthetic_channel_state(),
            "vector": _synthetic_channel_state(),
        },
        "metadata": {"fixture_policy": "label-derived-deterministic-v1"},
    }


def _synthetic_channel_ranking(memory_ids: list[str]) -> list[dict[str, Any]]:
    return [
        {"id": memory_id, "score": float(len(memory_ids) - rank + 1), "rank": rank}
        for rank, memory_id in enumerate(memory_ids, start=1)
    ]


def _synthetic_channel_state() -> dict[str, Any]:
    return {
        "planned": True,
        "enabled": True,
        "available": True,
        "executed": True,
        "participating": True,
        "evidence_only": False,
        "reason": "deterministic_fixture",
    }


def _deterministic_backend_metadata(candidate: str) -> dict[str, Any]:
    return {
        "model": "label-derived-deterministic-v1",
        "dimension": 0,
        "index_text_policy": candidate,
        "runtime": _runtime_metadata(),
    }


def _normalized_fusion_policy(value: str) -> str:
    policy = str(value or "").strip()
    if policy in {"legacy-auto", "max-v1"}:
        return policy
    if re.fullmatch(r"wrrf-v1:[0-9a-f]{64}", policy):
        return policy
    raise ValueError("fusion_policy must be normalized before benchmark launch")


def _resolve_fusion_policy(
    value: str,
    manifest: FrozenCandidateManifest | None,
) -> str:
    return resolve_cli_fusion_policy(value, manifest)


def _calibration_inputs(
    calibration_path: Path,
    heldout_path: Path,
) -> tuple[RecallDataset, str]:
    calibration = load_dataset_bundle(calibration_path)
    if calibration.evidence_role != "calibration":
        raise ValueError("calibration_evidence_role_required")
    return calibration, opaque_file_fingerprint(heldout_path)


@contextmanager
def _fusion_environment(
    manifest: FrozenCandidateManifest | None,
    fusion_policy: str,
) -> Iterator[None]:
    previous = {
        name: os.environ.get(name)
        for name in (
            "PP_RETRIEVAL_FUSION_POLICY",
            "PP_RETRIEVAL_RRF_K",
            "PP_RETRIEVAL_RRF_WEIGHTS_JSON",
            "PP_RETRIEVAL_RRF_WINDOWS_JSON",
        )
    }
    try:
        os.environ["PP_RETRIEVAL_FUSION_POLICY"] = fusion_policy
        if fusion_policy.startswith("wrrf-v1:"):
            if manifest is None or manifest.candidate_id != fusion_policy:
                raise ValueError("fusion_candidate_manifest_mismatch")
            config = manifest.fusion_config
            os.environ["PP_RETRIEVAL_RRF_K"] = str(config.k)
            os.environ["PP_RETRIEVAL_RRF_WEIGHTS_JSON"] = json.dumps(
                dict(config.weights), separators=(",", ":"), sort_keys=True
            )
            os.environ["PP_RETRIEVAL_RRF_WINDOWS_JSON"] = json.dumps(
                dict(config.windows), separators=(",", ":"), sort_keys=True
            )
        yield
    finally:
        for name, old_value in previous.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value


@contextmanager
def _calibration_fusion_environment(config: Any) -> Iterator[None]:
    candidate_id = f"wrrf-v1:{config.config_hash}"
    manifest = SimpleNamespace(candidate_id=candidate_id, fusion_config=config)
    with _fusion_environment(manifest, candidate_id):
        yield


def _source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip().casefold()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise ValueError("source_commit_unavailable")
    return commit


def _code_fingerprint() -> str:
    paths = sorted(
        {path.resolve() for path in (ROOT / "plastic_promise").rglob("*.py") if path.is_file()}
        | {path.resolve() for path in BENCHMARK_SOURCE_PATHS if path.is_file()},
        key=lambda path: path.as_posix(),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _experiment_retrieval_configuration(index_text_policy: str) -> dict[str, Any]:
    return {
        "index_text_policy": index_text_policy,
        **dict(LIVE_RETRIEVAL_CONFIGURATION),
    }


def _runtime_route(report: Mapping[str, Any]) -> str:
    runtime = str(_nested_value(report, "backend.effective_runtime") or "").strip()
    return f"{runtime}-http-mcp" if runtime else "unknown-http-mcp"


def _bind_experiment_report(
    report: Mapping[str, Any],
    *,
    dataset: RecallDataset,
    dataset_path: Path,
    fusion_policy: str,
    manifest: FrozenCandidateManifest | None = None,
    fusion_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    bound = json.loads(json.dumps(report))
    bound["schema_version"] = RECALL_QUALITY_REPORT_SCHEMA
    bound["dataset_role"] = dataset.evidence_role
    bound["dataset_fingerprint"] = (
        opaque_file_fingerprint(dataset_path)
        if manifest is not None
        else dataset_fingerprint(dataset)
    )
    bound["candidate_dimension"] = "fusion_policy"
    bound["candidate_id"] = fusion_policy
    bound["manifest_hash"] = (
        manifest.manifest_hash
        if manifest is not None and fusion_policy == manifest.candidate_id
        else ""
    )
    bound["fusion_config"] = (
        dict(fusion_config)
        if fusion_config is not None
        else (
            _config_payload(manifest.fusion_config)
            if manifest is not None and fusion_policy == manifest.candidate_id
            else None
        )
    )
    backend = bound.setdefault("backend", {})
    backend["public_call_counts"] = dict(bound.get("public_call_counts") or {})
    backend["runtime_route"] = _runtime_route(bound)
    environment = bound.setdefault("environment", {})
    environment.update(
        {
            "source_commit": _source_commit(),
            "dirty_fingerprint": _code_fingerprint(),
            "retrieval_configuration": _experiment_retrieval_configuration(
                str(backend.get("index_text_policy") or "legacy")
            ),
            "embedding_configuration": {
                "model": backend.get("model"),
                "dimension": backend.get("dimension"),
            },
            "dependencies": dict(environment.get("dependencies") or {}),
            "dataset_source": _source_label(dataset_path),
        }
    )
    channels = (bound.get("metrics") or {}).get("channels") or {}
    for summary in channels.values():
        if not isinstance(summary, dict):
            continue
        if "language" in summary:
            summary["by_language"] = summary.pop("language")
        if "group" in summary:
            summary["by_group"] = summary.pop("group")
    return bound


def _validate_manifest_bound_report(
    report: Mapping[str, Any],
    manifest: FrozenCandidateManifest,
) -> None:
    expected = {
        "dataset_fingerprint": manifest.heldout_fingerprint,
        "candidate_dimension": manifest.candidate_dimension,
        "environment.source_commit": manifest.source_commit,
        "environment.dirty_fingerprint": manifest.dirty_fingerprint,
        "environment.retrieval_configuration": dict(manifest.retrieval_configuration),
        "environment.embedding_configuration": dict(manifest.embedding_configuration),
        "environment.dependencies": dict(manifest.dependency_versions),
        "backend.runtime_route": manifest.runtime_route,
    }
    for path, value in expected.items():
        if _nested_value(report, path) != value:
            raise ValueError(f"candidate_manifest_runtime_mismatch:{path}")


def _compare_manifest_reports(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    manifest: FrozenCandidateManifest,
    tolerance: float,
    max_p95_ratio: float | None,
) -> dict[str, Any]:
    return compare_fusion_reports(
        baseline,
        candidate,
        manifest=manifest,
        tolerances={
            "required_split_tolerance": float(tolerance),
            "minimum_primary_delta": 0.01,
            "max_p95_ratio": 1.20 if max_p95_ratio is None else float(max_p95_ratio),
        },
    )


@contextmanager
def _isolated_live_environment(candidate: str) -> Iterator[LivePaths]:
    previous: dict[str, str | None] = {}
    root = Path(tempfile.mkdtemp(prefix="plastic-recall-quality-")).resolve()
    try:
        paths = LivePaths(
            root=root,
            sqlite=root / "canonical.db",
            lancedb=root / "lancedb",
        )
        overrides = {
            "PLASTIC_DB_PATH": str(paths.sqlite),
            "PLASTIC_LANCEDB_PATH": str(paths.lancedb),
            "PP_MEMORY_INDEX_TEXT_POLICY": candidate,
            "PP_MEMORY_SUMMARY_INDEX": "0",
            "PP_CODE_MEMORY_ENABLED": "0",
            "PP_FORCE_PYTHON_SUPPLY": "1",
            "PP_PREFER_RUST_SUPPLY": "0",
            "PP_SYNTHESIS_ARTIFACTS": "on",
            "PP_SYNTHESIS_RETRIEVAL": "1",
            "LDB_INIT_ON_HEAVY_INIT": "1",
            "LDB_BACKFILL_ON_INIT": "0",
            "LDB_REBUILD_ON_INIT": "0",
            "AGENT_OWNER": "recall-quality-benchmark",
            **LIVE_RETRIEVAL_CONFIGURATION,
        }
        for name, value in overrides.items():
            previous[name] = os.environ.get(name)
            os.environ[name] = value
        try:
            yield paths
        finally:
            for name, old_value in previous.items():
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value
    finally:
        cleanup_error: OSError | None = None
        for attempt in range(20):
            try:
                shutil.rmtree(root)
                cleanup_error = None
                break
            except FileNotFoundError:
                cleanup_error = None
                break
            except OSError as exc:
                cleanup_error = exc
                gc.collect()
                time.sleep(min(0.05 * (attempt + 1), 0.5))
        if root.exists():
            raise RuntimeError(f"isolated benchmark cleanup failed: {root}") from cleanup_error


def _public_fusion_attestation(
    payload: Mapping[str, Any],
) -> dict[str, str]:
    audit = payload.get("audit_metadata")
    if not isinstance(audit, Mapping):
        audit = payload.get("audit")
    if not isinstance(audit, Mapping):
        raise ValueError("fusion_attestation_missing")
    fusion = audit.get("retrieval_fusion")
    if not isinstance(fusion, Mapping):
        raise ValueError("fusion_attestation_missing")
    fields = (
        "requested_policy",
        "effective_policy",
        "requested_runtime",
        "effective_runtime",
    )
    result = {field: str(fusion.get(field) or "").strip() for field in fields}
    if any(not result[field] for field in fields):
        raise ValueError("fusion_attestation_incomplete")
    return result


async def _http_live_backend(
    dataset: RecallDataset,
    *,
    index_text_policy: str,
    fusion_policy: str,
    paths: LivePaths,
) -> tuple[
    Callable[[RecallCase], dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    port = free_tcp_port()
    url = f"http://127.0.0.1:{port}/mcp"
    health_url = f"http://127.0.0.1:{port}/health"
    stdout_path = paths.root / "http-mcp.stdout.log"
    stderr_path = paths.root / "http-mcp.stderr.log"
    env_keys = {
        "PLASTIC_DB_PATH",
        "PLASTIC_LANCEDB_PATH",
        "PP_MEMORY_INDEX_TEXT_POLICY",
        "PP_MEMORY_SUMMARY_INDEX",
        "PP_CODE_MEMORY_ENABLED",
        "PP_FORCE_PYTHON_SUPPLY",
        "PP_PREFER_RUST_SUPPLY",
        "PP_SYNTHESIS_ARTIFACTS",
        "PP_SYNTHESIS_RETRIEVAL",
        "PP_MEMORY_PROPOSALS",
        "LDB_INIT_ON_HEAVY_INIT",
        "LDB_BACKFILL_ON_INIT",
        "LDB_REBUILD_ON_INIT",
        "AGENT_OWNER",
        "PLASTIC_PROJECT_ID",
        "PLASTIC_RUNTIME_MODE",
        "PP_MCP_RUNTIME_ACTOR",
        "PP_RETRIEVAL_FUSION_POLICY",
        "PP_RETRIEVAL_RRF_K",
        "PP_RETRIEVAL_RRF_WEIGHTS_JSON",
        "PP_RETRIEVAL_RRF_WINDOWS_JSON",
        *LIVE_RETRIEVAL_CONFIGURATION,
    }
    overrides = {name: os.environ[name] for name in env_keys if name in os.environ}
    overrides.update(
        {
            "PLASTIC_DB_PATH": str(paths.sqlite),
            "PLASTIC_LANCEDB_PATH": str(paths.lancedb),
            "PP_MEMORY_INDEX_TEXT_POLICY": index_text_policy,
            "PP_RETRIEVAL_FUSION_POLICY": fusion_policy,
            "PP_MEMORY_PROPOSALS": "off",
            "PLASTIC_PROJECT_ID": "project:benchmark",
            "PLASTIC_RUNTIME_MODE": "full",
            "PP_MCP_RUNTIME_ACTOR": "codex",
        }
    )
    for optional in ("EMBEDDER_MODEL", "OLLAMA_HOST", "EMBEDDER_TIMEOUT"):
        if optional in os.environ:
            overrides[optional] = os.environ[optional]
    managed = ManagedProcess.start(
        (runtime_python(), "-m", "plastic_promise", "--streamable-http", str(port)),
        cwd=ROOT,
        env=process_environment(overrides, project_root=ROOT),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    try:
        health = await wait_for_health(
            health_url,
            managed,
            expected_source_root=ROOT,
            expected_source_revision=_source_commit(),
            expected_fusion_policy=fusion_policy,
        )
        require_owned_health(
            health,
            managed,
            expected_source_root=ROOT,
            expected_source_revision=_source_commit(),
            expected_fusion_policy=fusion_policy,
        )
        counts = {
            "memory_store": 0,
            "memory_update": 0,
            "feedback_apply": 0,
            "memory_recall": 0,
            "context_supply": 0,
        }
        transport_counts = {"memory_recall": 0, "context_supply": 0}
        fixture_to_actual, install_evidence = await _seed_public_corpus(
            url,
            dataset,
            counts,
        )
    except BaseException:
        managed.terminate()
        raise

    actual_to_fixture = {actual: fixture for fixture, actual in fixture_to_actual.items()}
    smoke = {
        "store": bool(install_evidence.get("seeded")),
        "recall": False,
        "supply": False,
        "verified_visible": False,
        "forbidden_hidden": True,
        "passed": False,
    }
    backend_metadata: dict[str, Any] = {
        "model": os.environ.get("EMBEDDER_MODEL", "mxbai-embed-large"),
        "dimension": 1024,
        "index_text_policy": index_text_policy,
        "runtime": _runtime_metadata(),
        "transport": "streamable-http",
        "server_pid": managed.pid,
        "requested_policy": fusion_policy,
        "effective_policy": None,
        "requested_runtime": None,
        "effective_runtime": None,
    }
    attestation_state: dict[str, Any] = {"attested_calls": 0, "errors": []}
    server_logs: dict[str, list[str]] = {"stdout": [], "stderr": []}
    observed_case_ids: set[str] = set()

    def retrieve(case: RecallCase) -> dict[str, Any]:
        started = time.perf_counter()
        common = {
            "task_type": case.task_type,
            "scope": "global",
            "debug": True,
            "project_id": case.project_id,
            "project_policy": "strict",
            "fusion_policy": fusion_policy,
            "stage_session_id": "recall-quality-http",
            "flow_line_id": "heldout",
        }
        recall, context = asyncio.run(
            call_tools_json(
                url,
                [
                    (
                        "memory_recall",
                        {
                            **common,
                            "query": case.query,
                            "max_results": 20,
                            "strict": False,
                            "request_id": f"recall:{case.case_id}",
                        },
                    ),
                    (
                        "context_supply",
                        {
                            **common,
                            "task_description": case.query,
                            "request_id": f"context:{case.case_id}",
                        },
                    ),
                ],
            )
        )
        transport_counts["memory_recall"] += 1
        transport_counts["context_supply"] += 1
        if case.case_id not in observed_case_ids:
            observed_case_ids.add(case.case_id)
            counts["memory_recall"] += 1
            counts["context_supply"] += 1
        latency_ms = (time.perf_counter() - started) * 1000.0
        audit = dict(context.get("audit_metadata") or {})
        fused_actual = [
            str(row.get("id") or "")
            for layer in ("core", "related", "divergent")
            for row in list(context.get(layer) or [])
            if isinstance(row, Mapping) and row.get("id")
        ]
        for tool_name, payload in (("memory_recall", recall), ("context_supply", context)):
            try:
                fusion = _public_fusion_attestation(payload)
                if fusion["requested_policy"] != fusion_policy:
                    raise ValueError("requested_policy_mismatch")
                if fusion["effective_policy"] != fusion_policy:
                    raise ValueError("effective_policy_mismatch")
                if fusion["requested_runtime"] != fusion["effective_runtime"]:
                    raise ValueError("runtime_mismatch")
                observed = (
                    fusion["effective_policy"],
                    fusion["requested_runtime"],
                    fusion["effective_runtime"],
                )
                previous = attestation_state.get("observed")
                if previous is not None and tuple(previous) != observed:
                    raise ValueError("cross_call_mismatch")
                attestation_state["observed"] = list(observed)
                attestation_state["attested_calls"] += 1
                backend_metadata.update(fusion)
            except (TypeError, ValueError) as exc:
                attestation_state["errors"].append(f"{tool_name}:{exc}")
        channel_rankings, channel_states = _remap_public_channel_evidence(
            context,
            actual_to_fixture,
        )
        recalled_ids = {
            str(row.get("id") or "")
            for layer in ("core", "related", "divergent")
            for row in list(recall.get(layer) or [])
            if isinstance(row, Mapping)
        }
        context_ids = set(fused_actual)
        forbidden_actual = {
            fixture_to_actual.get(memory_id, memory_id) for memory_id in case.forbidden_memory_ids
        }
        relevant_actual = {
            fixture_to_actual.get(memory_id, memory_id) for memory_id in case.relevant_memory_ids
        }
        smoke["recall"] = True
        smoke["supply"] = True
        smoke["forbidden_hidden"] = bool(
            smoke["forbidden_hidden"] and forbidden_actual.isdisjoint(recalled_ids | context_ids)
        )
        if relevant_actual & (recalled_ids | context_ids):
            smoke["verified_visible"] = True
        smoke["passed"] = bool(
            smoke["store"]
            and smoke["recall"]
            and smoke["supply"]
            and smoke["verified_visible"]
            and smoke["forbidden_hidden"]
        )
        fallback_reasons = _fallback_reasons(audit)
        return {
            "ranked_ids": [actual_to_fixture.get(item, item) for item in fused_actual],
            "latency_ms": latency_ms,
            "fallback_used": bool(fallback_reasons),
            "degraded": bool(
                context.get("degraded")
                or recall.get("degraded")
                or context.get("warnings")
                or recall.get("warnings")
            ),
            "channel_rankings": channel_rankings,
            "channel_states": channel_states,
            "metadata": {
                "engine_mode": audit.get("engine_mode", "unknown"),
                "fallback_reasons": fallback_reasons,
            },
        }

    def cleanup() -> None:
        managed.terminate()
        server_logs["stdout"][:] = sanitized_log_tail(stdout_path, private_roots=(paths.root,))
        server_logs["stderr"][:] = sanitized_log_tail(stderr_path, private_roots=(paths.root,))

    return (
        retrieve,
        backend_metadata,
        {
            "isolated_corpus": install_evidence,
            "smoke": smoke,
            "public_call_counts": counts,
            "public_transport_call_counts": transport_counts,
            "fusion_attestation": attestation_state,
            "server_logs": server_logs,
            "_cleanup": cleanup,
        },
    )


async def _seed_public_corpus(
    url: str,
    dataset: RecallDataset,
    counts: dict[str, int],
) -> tuple[dict[str, str], dict[str, Any]]:
    fixture_to_actual: dict[str, str] = {}
    ordinary = [record for record in dataset.corpus if record.memory_type != "synthesis"]
    synthesis = [record for record in dataset.corpus if record.memory_type == "synthesis"]
    for record in ordinary:
        payload = await call_tool_json(
            url,
            "memory_store",
            {
                "content": record.content,
                "memory_type": record.memory_type,
                "source": "recall-quality-http",
                "project_id": record.project_id,
                "project_policy": "strict",
                "visibility": "project",
                "source_class": "experience",
                "tags": [f"domain:{record.domain}", f"cat:{record.category}"],
                "metadata_json": {
                    "l0_abstract": record.l0_abstract,
                    "l1_summary": record.l1_summary,
                    "fixture_id": record.memory_id,
                },
                "max_llm_calls": 0,
            },
        )
        counts["memory_store"] += 1
        actual_id = str(payload.get("memory_id") or "")
        if not payload.get("stored") or not actual_id:
            raise RuntimeError(f"public_seed_failed:{record.memory_id}")
        fixture_to_actual[record.memory_id] = actual_id

    status_order = {"stale": 0, "contested": 1, "draft": 2, "verified": 3}
    for record in sorted(synthesis, key=lambda item: status_order[item.synthesis_status]):
        source_ids = [
            fixture_to_actual[source_id] for source_id in record.metadata.get("source_ids", ())
        ]
        payload = await call_tool_json(
            url,
            "memory_store",
            {
                "content": record.content,
                "memory_type": "synthesis",
                "source": "synthesis",
                "source_ids": source_ids,
                "synthesis_key": f"recall-quality:{record.memory_id}",
                "validity_scope": record.project_id,
                "project_id": record.project_id,
                "project_policy": "strict",
                "visibility": "project",
                "actor": "recall-quality-http",
                "automatic": False,
                "reuse_signal": False,
                "metadata_json": {"fixture_id": record.memory_id},
            },
        )
        counts["memory_store"] += 1
        actual_id = str(payload.get("memory_id") or "")
        if not payload.get("stored") or not actual_id:
            raise RuntimeError(f"public_synthesis_seed_failed:{record.memory_id}")
        fixture_to_actual[record.memory_id] = actual_id
        if record.synthesis_status in {"verified", "stale"}:
            feedback = "adopted"
        elif record.synthesis_status == "contested":
            feedback = "rejected"
        else:
            continue
        result = await call_tool_json(
            url,
            "feedback_apply",
            {
                "item_id": actual_id,
                "feedback_type": feedback,
                "expected_revision": 1,
                "rejection_reason": "fixture_contested",
                "project_id": record.project_id,
            },
        )
        counts["feedback_apply"] += 1
        if not result.get("updated"):
            raise RuntimeError(f"public_synthesis_feedback_failed:{record.memory_id}")
        if record.synthesis_status == "stale":
            source_fixture = str(record.metadata["source_ids"][0])
            source_record = next(item for item in ordinary if item.memory_id == source_fixture)
            for content in (
                source_record.content + " [stale-transition]",
                source_record.content,
            ):
                update = await call_tool_json(
                    url,
                    "memory_update",
                    {
                        "memory_id": fixture_to_actual[source_fixture],
                        "content": content,
                        "reason": "recall-quality stale lifecycle fixture",
                        "project_id": source_record.project_id,
                    },
                )
                counts["memory_update"] += 1
                if not update.get("updated"):
                    raise RuntimeError(f"public_stale_transition_failed:{record.memory_id}")

    eligible_count = len(ordinary) + sum(
        record.synthesis_status == "verified" for record in synthesis
    )
    return fixture_to_actual, {
        "seeded": len(fixture_to_actual) == len(dataset.corpus),
        "canonical_count": len(fixture_to_actual),
        "derived_count": eligible_count,
        "eligible_count": eligible_count,
        "seed_transport": "public-memory-tools",
    }


def _remap_public_channel_evidence(
    payload: Mapping[str, Any],
    actual_to_fixture: Mapping[str, str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    for raw_name, raw_rows in dict(payload.get("channel_rankings") or {}).items():
        channel = "bm25" if str(raw_name).casefold() == "lexical" else str(raw_name)
        rankings[channel] = [
            {
                "id": actual_to_fixture.get(str(row.get("id") or ""), str(row.get("id") or "")),
                "score": row.get("score"),
                "rank": row.get("rank"),
            }
            for row in list(raw_rows or [])
            if isinstance(row, Mapping)
        ]
    states = {
        ("bm25" if str(name).casefold() == "lexical" else str(name)): dict(state)
        for name, state in dict(payload.get("channel_states") or {}).items()
        if isinstance(state, Mapping)
    }
    missing_state = {
        "planned": True,
        "enabled": False,
        "available": False,
        "executed": False,
        "participating": False,
        "evidence_only": False,
        "reason": "missing_public_evidence",
    }
    return (
        {channel: rankings.get(channel, []) for channel in FUSION_CHANNEL_ORDER},
        {channel: states.get(channel, dict(missing_state)) for channel in FUSION_CHANNEL_ORDER},
    )


def _engine_diagnostic_backend(
    dataset: RecallDataset,
    candidate: str,
    paths: LivePaths,
) -> tuple[
    Callable[[RecallCase], dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    from plastic_promise.core.context_engine import ContextEngine
    from plastic_promise.core.embedder import get_embedder

    engine = ContextEngine(use_sqlite=True)
    try:
        engine.ensure_heavy_init()
        embedder = getattr(engine, "_embedder", None) or get_embedder(fallback_on_error=False)
        model = str(getattr(embedder, "model_name", type(embedder).__name__))
        dimension = int(getattr(embedder, "dim", 0) or 0)
        embedder_is_fallback = "fallback" in model.lower()
        if dimension <= 0:
            raise RuntimeError("live embedder did not report a positive dimension")

        fixture_to_actual, actual_to_fixture, install_evidence = _install_live_corpus(
            engine,
            embedder,
            dataset,
            candidate,
            model,
        )
        smoke = _run_store_recall_supply_smoke(
            engine,
            embedder,
            dataset,
            fixture_to_actual,
            actual_to_fixture,
        )
    except BaseException:
        _close_live_backend_resources(engine)
        raise

    def retrieve(case: RecallCase) -> dict[str, Any]:
        started = time.perf_counter()
        vector = embedder.embed(case.query)
        vector_fallback = not vector or not any(float(value) != 0.0 for value in vector)
        pack = engine.supply(
            case.query,
            task_vector=vector,
            task_type=case.task_type,
            scope="global",
            debug=True,
            project_id=case.project_id,
            project_policy="strict",
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        fused_actual = [item.id for item in (*pack.core, *pack.related, *pack.divergent)]
        fused = [actual_to_fixture.get(memory_id, memory_id) for memory_id in fused_actual]
        audit = dict(getattr(pack, "audit_metadata", {}) or {})
        channel_rankings, channel_states = _channel_evidence_from_pack(
            pack,
            actual_to_fixture,
        )
        fallback_reasons = _fallback_reasons(audit)
        return {
            "ranked_ids": fused,
            "latency_ms": latency_ms,
            "fallback_used": embedder_is_fallback or vector_fallback or bool(fallback_reasons),
            "degraded": _pack_is_degraded(pack, audit),
            "channel_rankings": channel_rankings,
            "channel_states": channel_states,
            "metadata": {
                "engine_mode": audit.get("engine_mode", "unknown"),
                "fallback_reasons": fallback_reasons,
            },
        }

    def cleanup() -> None:
        _close_live_backend_resources(engine)

    return (
        retrieve,
        {
            "model": model,
            "dimension": dimension,
            "index_text_policy": candidate,
            "runtime": _runtime_metadata(),
        },
        {
            "isolated_corpus": install_evidence,
            "smoke": smoke,
            "_cleanup": cleanup,
        },
    )


def _close_live_backend_resources(engine: Any) -> None:
    connections: list[Any] = []
    for component in vars(engine).values():
        connection = getattr(component, "_conn", None)
        if connection is not None and connection not in connections:
            connections.append(connection)
    for connection in connections:
        with suppress(Exception):
            connection.close()
    lancedb_store = getattr(engine, "lancedb_store", None)
    if lancedb_store is not None:
        database = getattr(lancedb_store, "_db", None)
        with suppress(Exception):
            lancedb_store._table = None
            lancedb_store._db = None
        close = getattr(database, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
    with suppress(Exception):
        engine._ldb = None
        engine._dm = None
        engine._sqlite = None
        engine._code_index = None
        engine._rust_engine_instance = None
        engine._memories.clear()


def _install_live_corpus(
    engine: Any,
    embedder: Any,
    dataset: RecallDataset,
    candidate: str,
    model: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    from plastic_promise.core.memory_index import (
        build_index_material,
        metadata_with_index_material,
    )
    from plastic_promise.core.synthesis import SynthesisStore

    fixture_to_actual: dict[str, str] = {}
    actual_to_fixture: dict[str, str] = {}
    ordinary = [record for record in dataset.corpus if record.memory_type != "synthesis"]
    synthesis = [record for record in dataset.corpus if record.memory_type == "synthesis"]
    for record in ordinary:
        material = build_index_material(
            {
                "content": record.content,
                "domain": record.domain,
                "category": record.category,
                "l0_abstract": record.l0_abstract,
                "l1_summary": record.l1_summary,
            },
            policy=candidate,
            model_name=model,
        )
        metadata = metadata_with_index_material(
            {
                "fixture_id": record.memory_id,
                "corpus_revision": dataset.corpus_revision,
            },
            material,
        )
        actual_id = engine.create_ordinary_if_absent(
            {
                "id": record.memory_id,
                "content": record.content,
                "memory_type": record.memory_type,
                "source": "recall-quality-fixture",
                "tier": "L1",
                "scope": "global",
                "category": record.category,
                "domain": record.domain,
                "project_id": record.project_id,
                "visibility": "project",
                "source_class": "experience",
                "raw_content": record.content,
                "l0_abstract": record.l0_abstract,
                "l1_summary": record.l1_summary,
                "l2_content": record.content,
                "embedding_text": material.vector_text,
                "embedding_hash": material.embedding_hash,
                "search_text": material.search_text,
                "metadata_json": metadata,
            }
        )
        fixture_to_actual[record.memory_id] = actual_id
        actual_to_fixture[actual_id] = record.memory_id

    synthesis_store = SynthesisStore(engine._sqlite._conn, engine=engine)
    for record in synthesis:
        source_ids = [
            fixture_to_actual[source_id] for source_id in record.metadata.get("source_ids", [])
        ]
        artifact = synthesis_store.create_draft(
            record.content,
            source_ids,
            synthesis_key=f"benchmark:{dataset.corpus_revision}:{record.memory_id}",
            validity_scope=record.project_id,
            project_id=record.project_id,
            visibility="project",
            actor="recall-quality-benchmark",
            call_id=f"seed:{record.memory_id}",
            automatic=False,
            metadata={
                "fixture_id": record.memory_id,
                "corpus_revision": dataset.corpus_revision,
                "l0_abstract": record.l0_abstract,
                "l1_summary": record.l1_summary,
                "domain": record.domain,
                "category": record.category,
                "fixture_provenance_revision": dataset.synthesis_provenance_revision,
                "fixture_source_ids": list(record.metadata.get("source_ids", [])),
                "fixture_verification_actor": record.metadata.get("verification_actor", ""),
                "fixture_verification_call_id": record.metadata.get("verification_call_id", ""),
                "fixture_verified_at": record.metadata.get("verified_at", ""),
            },
        )
        if artifact is None:
            raise RuntimeError(f"synthesis fixture was not created: {record.memory_id}")
        if record.synthesis_status == "verified":
            artifact = synthesis_store.verify(
                artifact.memory_id,
                str(record.metadata["verification_actor"]),
                str(record.metadata["verification_call_id"]),
                artifact.revision,
            )
        elif record.synthesis_status == "contested":
            artifact = synthesis_store.mark_contested(
                artifact.memory_id,
                "fixture contested control",
                artifact.revision,
                actor="recall-quality-reviewer",
                call_id=f"contest:{record.memory_id}",
            )
        elif record.synthesis_status == "stale":
            artifact = synthesis_store.verify(
                artifact.memory_id,
                str(record.metadata["verification_actor"]),
                str(record.metadata["verification_call_id"]),
                artifact.revision,
            )
            artifact = synthesis_store.mark_stale(
                artifact.memory_id,
                "fixture source drift",
                artifact.revision,
                actor="recall-quality-maintenance",
                call_id=f"stale:{record.memory_id}",
            )
        fixture_to_actual[record.memory_id] = artifact.memory_id
        actual_to_fixture[artifact.memory_id] = record.memory_id

    lancedb_store = engine.lancedb_store
    if lancedb_store is None:
        raise RuntimeError("isolated LanceDB store was not initialized")
    lancedb_store.clear_all()
    index_rows: list[tuple[str, Any, Any]] = []
    records_by_id = {record.memory_id: record for record in dataset.corpus}
    for fixture_id, actual_id in fixture_to_actual.items():
        record = records_by_id[fixture_id]
        if record.memory_type == "synthesis" and record.synthesis_status != "verified":
            continue
        material = _read_live_index_material(
            engine,
            actual_id,
            candidate=candidate,
            model=model,
            fixture_id=fixture_id,
        )
        index_rows.append((actual_id, record, material))

    vectors = embedder.embed_batch([material.vector_text for _, _, material in index_rows])
    if len(vectors) != len(index_rows):
        raise RuntimeError("live embedder returned an incomplete corpus batch")
    for (actual_id, record, material), vector in zip(index_rows, vectors, strict=True):
        if not vector or not any(float(value) != 0.0 for value in vector):
            raise RuntimeError(f"live embedder returned a zero vector for {record.memory_id}")
        lancedb_store.insert_checked(
            actual_id,
            vector,
            material.search_text,
            tier="L1",
            category=record.category,
            scope="global",
        )

    canonical_count = int(
        engine._sqlite._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    )
    derived_count = int(lancedb_store.count_rows())
    return (
        fixture_to_actual,
        actual_to_fixture,
        {
            "seeded": canonical_count == dataset.corpus_count and derived_count == len(index_rows),
            "canonical_count": canonical_count,
            "derived_count": derived_count,
            "eligible_count": len(index_rows),
            "temporary_sqlite": True,
            "temporary_lancedb": True,
        },
    )


def _read_live_index_material(
    engine: Any,
    actual_id: str,
    *,
    candidate: str,
    model: str,
    fixture_id: str,
) -> Any:
    from plastic_promise.core.memory_index import read_persisted_index_material

    memory = engine._sqlite.get(actual_id)
    if not isinstance(memory, Mapping):
        raise RuntimeError(f"canonical persisted memory is missing: {fixture_id}")
    material = read_persisted_index_material(memory, model_name=model)
    if material is None:
        raise RuntimeError(
            f"canonical persisted index material is missing or invalid: {fixture_id}"
        )
    if material.policy != candidate:
        raise RuntimeError(
            f"canonical persisted index material policy mismatch for {fixture_id}: "
            f"expected {candidate}, got {material.policy}"
        )
    return material


def _run_store_recall_supply_smoke(
    engine: Any,
    embedder: Any,
    dataset: RecallDataset,
    fixture_to_actual: Mapping[str, str],
    actual_to_fixture: Mapping[str, str],
) -> dict[str, Any]:
    smoke_case = next(
        (case for case in dataset.cases if case.case_id == "identifier-003"),
        None,
    ) or next(
        (
            case
            for case in dataset.cases
            if all(memory_id in fixture_to_actual for memory_id in case.relevant_memory_ids)
            and any(
                record.memory_id in case.relevant_memory_ids
                and (record.memory_type != "synthesis" or record.synthesis_status == "verified")
                for record in dataset.corpus
            )
        ),
        None,
    )
    if smoke_case is None:
        return {
            "store": False,
            "recall": False,
            "supply": False,
            "verified_visible": False,
            "forbidden_hidden": False,
            "passed": False,
        }
    expected_fixture = smoke_case.relevant_memory_ids[0]
    expected_actual = fixture_to_actual[expected_fixture]
    stored = engine.get_memory_dict(expected_actual) is not None
    query_vector = embedder.embed(smoke_case.query)
    vector_is_real = bool(query_vector and any(float(value) != 0.0 for value in query_vector))
    recalled_ids = []
    if vector_is_real and engine.lancedb_store is not None:
        recalled_ids = [
            actual_to_fixture.get(str(item[0]), str(item[0]))
            for item in engine.lancedb_store.search(query_vector, k=10)
        ]
    recalled = expected_fixture in recalled_ids
    pack = engine.supply(
        smoke_case.query,
        task_vector=query_vector,
        task_type=smoke_case.task_type,
        scope="global",
        debug=True,
        project_id=smoke_case.project_id,
        project_policy="strict",
        retrieval_mode="hybrid",
    )
    supplied_ids = [
        actual_to_fixture.get(item.id, item.id)
        for item in (*pack.core, *pack.related, *pack.divergent)
    ]
    supplied = expected_fixture in supplied_ids
    verified_fixture = next(
        (record.memory_id for record in dataset.corpus if record.synthesis_status == "verified"),
        "",
    )
    forbidden_fixtures = [
        record.memory_id
        for record in dataset.corpus
        if record.memory_type == "synthesis"
        and record.synthesis_status in {"draft", "contested", "stale"}
    ]
    indexed_ids = engine.lancedb_store.list_memory_ids()
    verified_actual = fixture_to_actual.get(verified_fixture, "")
    verified_gate = engine._gate_memory_ids([verified_actual]) if verified_actual else None
    verified_visible = bool(
        verified_actual
        and engine.get_memory_dict(verified_actual) is not None
        and verified_actual in indexed_ids
        and verified_actual in tuple(getattr(verified_gate, "items", ()))
    )
    forbidden_actual_ids = [fixture_to_actual[memory_id] for memory_id in forbidden_fixtures]
    forbidden_gate = engine._gate_memory_ids(forbidden_actual_ids)
    admitted_forbidden = set(getattr(forbidden_gate, "items", ()))
    forbidden_hidden = all(
        engine.get_memory_dict(actual_id) is None
        and actual_id not in indexed_ids
        and actual_id not in admitted_forbidden
        for actual_id in forbidden_actual_ids
    )
    return {
        "store": stored,
        "recall": recalled,
        "supply": supplied,
        "verified_visible": verified_visible,
        "forbidden_hidden": forbidden_hidden,
        "passed": bool(
            stored
            and recalled
            and supplied
            and vector_is_real
            and verified_visible
            and forbidden_hidden
        ),
        "fixture_id": expected_fixture,
        "verified_fixture_id": verified_fixture,
    }


def _repeat_retriever(
    retrieve: Callable[[RecallCase], Any], *, warmup: int, repeat: int
) -> Callable[[RecallCase], dict[str, Any]]:
    def repeated(case: RecallCase) -> dict[str, Any]:
        observed: list[Mapping[str, Any]] = []
        for _ in range(warmup):
            result = retrieve(case)
            observed.append(result if isinstance(result, Mapping) else {"ranked_ids": result})
        measured: list[Mapping[str, Any]] = []
        for _ in range(repeat):
            result = retrieve(case)
            if isinstance(result, Mapping):
                measured.append(result)
            else:
                measured.append({"ranked_ids": result})
        observed.extend(measured)
        last = dict(measured[-1])
        last["latencies_ms"] = [float(item.get("latency_ms", 0.0)) for item in measured]
        last["fallback_used"] = any(_as_bool(item.get("fallback_used", False)) for item in observed)
        last["degraded"] = any(_as_bool(item.get("degraded", False)) for item in observed)
        return last

    return repeated


def _channel_evidence_from_pack(
    pack: Any,
    actual_to_fixture: Mapping[str, str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Remap complete producer-owned channel evidence into fixture IDs."""

    raw_rankings = getattr(pack, "channel_rankings", None)
    raw_states = getattr(pack, "channel_states", None)
    if not isinstance(raw_rankings, Mapping):
        raise RuntimeError("live retrieval pack did not expose channel_rankings")
    if not isinstance(raw_states, Mapping):
        raise RuntimeError("live retrieval pack did not expose channel_states")

    rankings: dict[str, list[dict[str, Any]]] = {}
    for raw_name, raw_rows in raw_rankings.items():
        channel = "bm25" if str(raw_name).casefold() == "lexical" else str(raw_name)
        if channel not in {"vector", "bm25", "fts"}:
            continue
        if not isinstance(raw_rows, list):
            raise RuntimeError(f"live channel ranking {channel} is not a list")
        rows: list[dict[str, Any]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                raise RuntimeError(f"live channel ranking {channel} contains a non-object row")
            actual_id = str(raw_row.get("id", raw_row.get("memory_id", "")) or "").strip()
            if not actual_id:
                raise RuntimeError(f"live channel ranking {channel} contains an empty id")
            rows.append(
                {
                    "id": actual_to_fixture.get(actual_id, actual_id),
                    "score": raw_row.get("score"),
                    "rank": raw_row.get("rank"),
                }
            )
        rankings[channel] = rows

    states: dict[str, dict[str, Any]] = {}
    for raw_name, raw_state in raw_states.items():
        channel = "bm25" if str(raw_name).casefold() == "lexical" else str(raw_name)
        if not isinstance(raw_state, Mapping):
            raise RuntimeError(f"live channel state {channel} is not an object")
        states[channel] = dict(raw_state)
    return rankings, states


def _pack_is_degraded(pack: Any, audit: Mapping[str, Any]) -> bool:
    project_context = getattr(pack, "project_context", None)
    if not isinstance(project_context, Mapping):
        project_context = audit.get("project_context")
    project_context_degraded = (
        project_context.get("degraded", False)
        if isinstance(project_context, Mapping)
        else getattr(project_context, "degraded", False)
    )
    synthesis_retrieval = audit.get("synthesis_retrieval")
    synthesis_degradations = (
        synthesis_retrieval.get("degradations", ())
        if isinstance(synthesis_retrieval, Mapping)
        else ()
    )
    retrieval_degradations = audit.get("retrieval_degradations", ())
    return any(
        (
            _as_bool(getattr(pack, "degraded", False)),
            _as_bool(project_context_degraded),
            _as_bool(audit.get("project_degraded", False)),
            _as_bool(synthesis_degradations),
            _as_bool(retrieval_degradations),
        )
    )


def _fallback_reasons(audit: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in ("fallback_reason", "rust_fallback_reason", "vector_search"):
        value = audit.get(key)
        normalized = str(value or "").strip()
        if normalized.lower() not in {"", "none", "active", "native"}:
            reasons.append(f"{key}:{normalized}")
    fallback_used = audit.get("fallback_used")
    if isinstance(fallback_used, list):
        reasons.extend(str(value) for value in fallback_used if str(value).strip())
    elif fallback_used:
        reasons.append(f"fallback_used:{fallback_used}")
    return sorted(set(reasons))


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _runtime_metadata() -> dict[str, str]:
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
    }


def _environment_metadata(dataset_path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        code_revision = completed.stdout.strip() if completed.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        code_revision = "unknown"
    return {
        "provider": os.environ.get("EMBEDDER_PROVIDER", "ollama"),
        "configured_model": os.environ.get("EMBEDDER_MODEL", "mxbai-embed-large"),
        "supply_runtime": "python" if os.environ.get("PP_FORCE_PYTHON_SUPPLY") == "1" else "auto",
        "code_revision": code_revision,
        "dataset_source": _source_label(dataset_path),
        "source_fingerprint": _source_fingerprint(dataset_path),
        "source_files": [_source_label(path) for path in _source_paths(dataset_path)],
        "dependencies": _retrieval_dependency_versions(),
        "retrieval_configuration": {
            name: os.environ.get(name, default)
            for name, default in LIVE_RETRIEVAL_CONFIGURATION.items()
        },
    }


def _retrieval_dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in ("lancedb", "pyarrow"):
        try:
            versions[package] = package_version(package)
        except PackageNotFoundError:
            versions[package] = "unavailable"
    return versions


def _source_fingerprint(dataset_path: Path) -> str:
    digest = hashlib.sha256()
    for path in _source_paths(dataset_path):
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"benchmark source fingerprint input is unreadable: {path}") from exc
        label = _source_label(path).encode("utf-8")
        digest.update(len(label).to_bytes(8, "big"))
        digest.update(label)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _source_paths(dataset_path: Path) -> tuple[Path, ...]:
    package_sources = tuple(
        path for path in (ROOT / "plastic_promise").rglob("*.py") if path.is_file()
    )
    unique = {
        path.resolve() for path in (*BENCHMARK_SOURCE_PATHS, *package_sources, Path(dataset_path))
    }
    return tuple(sorted(unique, key=_source_label))


def _source_label(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _current_report_source_fingerprint(report: Mapping[str, Any]) -> str | None:
    dataset_source = _nested_value(report, "environment.dataset_source")
    if not isinstance(dataset_source, str) or not dataset_source.strip():
        return None
    candidate = (ROOT / dataset_source).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    try:
        return _source_fingerprint(candidate)
    except RuntimeError:
        return None


def _dependency_version_is_supported(name: str, value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or value == "unavailable":
        return False
    try:
        parsed = Version(value)
    except InvalidVersion:
        return False
    minimum = RETRIEVAL_DEPENDENCY_MINIMUMS.get(name)
    return minimum is None or parsed >= minimum


def _absolute_thresholds(args: argparse.Namespace) -> dict[str, float | None]:
    return {
        "min_hit_at_1": args.min_hit_at_1,
        "min_hit_at_5": args.min_hit_at_5,
        "min_mrr": args.min_mrr,
        "max_forbidden_hit_rate": args.max_forbidden_hit_rate,
        "max_fallback_rate": args.max_fallback_rate,
        "max_degradation_rate": args.max_degradation_rate,
        "max_p95_ms": args.max_p95_ms,
    }


def _evaluate_absolute_gate(
    summary: MetricSummary, thresholds: Mapping[str, float | None]
) -> dict[str, Any]:
    values = {
        "min_hit_at_1": summary.hit_at.get(1, 0.0),
        "min_hit_at_5": summary.hit_at.get(5, 0.0),
        "min_mrr": summary.mrr,
        "max_forbidden_hit_rate": summary.forbidden_hit_rate,
        "max_fallback_rate": summary.fallback_rate,
        "max_degradation_rate": summary.degradation_rate,
        "max_p95_ms": summary.p95_ms,
    }
    checks: list[dict[str, Any]] = []
    for name, current in values.items():
        limit = thresholds.get(name)
        if limit is None:
            continue
        direction = "minimum" if name.startswith("min_") else "maximum"
        passed = current >= float(limit) if direction == "minimum" else current <= float(limit)
        checks.append(
            {
                "metric": name.removeprefix("min_").removeprefix("max_"),
                "direction": direction,
                "current": current,
                "limit": float(limit),
                "passed": passed,
            }
        )
    failures = [check for check in checks if not check["passed"]]
    status = "fail" if failures else "pass" if checks else "not_configured"
    return {"status": status, "checks": checks, "failures": failures}


def _combine_quality_gates(
    absolute_gate: Mapping[str, Any],
    constituent_gate: Mapping[str, Any],
) -> dict[str, Any]:
    absolute_failures = list(absolute_gate.get("failures") or [])
    constituent_failures = list(constituent_gate.get("failures") or [])
    failures = [
        *({"gate": "absolute", **item} for item in absolute_failures),
        *({"gate": "best_constituent", **item} for item in constituent_failures),
    ]
    return {
        "status": "fail" if failures else "pass",
        "checks": [
            *({"gate": "absolute", **item} for item in absolute_gate.get("checks") or []),
            *(
                {"gate": "best_constituent", **item}
                for item in constituent_gate.get("checks") or []
            ),
        ],
        "failures": failures,
        "absolute_status": absolute_gate.get("status", "not_configured"),
        "best_constituent_status": constituent_gate.get("status", "fail"),
    }


def _compare_reports(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.baseline is None or args.candidate_report is None:
        parser.error("--gate requires --baseline and --candidate-report")
    try:
        baseline_report = _read_json(args.baseline)
        candidate_report = _read_json(args.candidate_report)
        if args.candidate_manifest is not None:
            manifest = load_frozen_manifest(args.candidate_manifest)
            payload = _compare_manifest_reports(
                baseline_report,
                candidate_report,
                manifest=manifest,
                tolerance=args.tolerance,
                max_p95_ratio=args.max_p95_ratio,
            )
        else:
            tolerances: dict[str, float] = {"default": float(args.tolerance)}
            if args.max_hit5_regression is not None:
                tolerances["hit_at_5"] = args.max_hit5_regression
            if args.max_mrr_regression is not None:
                tolerances["mrr"] = args.max_mrr_regression
            if args.max_forbidden_hit_increase is not None:
                tolerances["forbidden_hit_rate"] = args.max_forbidden_hit_increase
            payload = compare_recall_quality_reports(
                baseline_report,
                candidate_report,
                tolerances=tolerances,
                max_p95_ratio=args.max_p95_ratio,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot compare recall-quality reports: {exc}", file=sys.stderr)
        return 2

    if args.output is not None:
        _write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


def compare_recall_quality_reports(
    baseline_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    *,
    tolerances: float | Mapping[str, float] = 0.0,
    max_p95_ratio: float | None = None,
) -> dict[str, Any]:
    """Fail closed unless reports are complete, live, publishable, and comparable."""

    baseline = metric_summary_from_dict(_metrics_payload(baseline_report))
    candidate = metric_summary_from_dict(_metrics_payload(candidate_report))
    payload = compare_summaries(baseline, candidate, tolerances).to_dict()
    _append_p95_ratio_check(payload, baseline, candidate, max_p95_ratio)
    constituent_gate = evaluate_best_constituent_gate(candidate)
    payload["best_constituent_gate"] = constituent_gate
    if not constituent_gate["passed"]:
        payload["passed"] = False
        payload["status"] = "fail"
    comparability = _report_comparability(baseline_report, candidate_report)
    payload["comparability"] = comparability
    if not comparability["passed"]:
        payload["passed"] = False
        payload["status"] = "fail"
    return payload


def _report_comparability(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, baseline_value: Any, candidate_value: Any, passed: bool) -> None:
        checks.append(
            {
                "field": name,
                "baseline": baseline_value,
                "candidate": candidate_value,
                "passed": bool(passed),
            }
        )

    def equal(path: str) -> None:
        baseline_value = _nested_value(baseline, path)
        candidate_value = _nested_value(candidate, path)
        add(
            path,
            baseline_value,
            candidate_value,
            baseline_value is not None and baseline_value == candidate_value,
        )

    for path in (
        "schema_version",
        "dataset_schema_version",
        "dataset_revision",
        "corpus.revision",
        "corpus.provenance_revision",
        "corpus.sha256",
        "corpus.count",
        "cases.sha256",
        "cases.count",
        "backend.model",
        "backend.dimension",
        "backend.runtime",
        "execution.warmup",
        "execution.repeat",
        "environment",
        "isolated_corpus.eligible_count",
        "isolated_corpus.derived_count",
    ):
        equal(path)

    for label, report, expected_candidate in (
        ("baseline", baseline, "legacy"),
        ("candidate", candidate, "compact-v2"),
    ):
        schema_version = report.get("schema_version")
        dataset_schema_version = report.get("dataset_schema_version")
        add(
            f"{label}.schema_version_known",
            REPORT_SCHEMA_VERSION,
            schema_version,
            schema_version == REPORT_SCHEMA_VERSION,
        )
        add(
            f"{label}.dataset_schema_version_known",
            "recall-quality/v1",
            dataset_schema_version,
            dataset_schema_version == "recall-quality/v1",
        )
        for revision_path in (
            "dataset_revision",
            "corpus.revision",
            "corpus.provenance_revision",
        ):
            revision = _nested_value(report, revision_path)
            add(
                f"{label}.{revision_path}_present",
                True,
                revision,
                bool(str(revision or "").strip()),
            )
        corpus_hash = _nested_value(report, "corpus.sha256")
        add(
            f"{label}.corpus.sha256_valid",
            "64 lowercase hex characters",
            corpus_hash,
            isinstance(corpus_hash, str) and re.fullmatch(r"[0-9a-f]{64}", corpus_hash) is not None,
        )
        case_hash = _nested_value(report, "cases.sha256")
        add(
            f"{label}.cases.sha256_valid",
            "64 lowercase hex characters",
            case_hash,
            isinstance(case_hash, str) and re.fullmatch(r"[0-9a-f]{64}", case_hash) is not None,
        )
        contract = KNOWN_DATASET_CONTRACTS.get(
            (str(dataset_schema_version), str(report.get("dataset_revision")))
        )
        add(
            f"{label}.dataset_contract_known",
            True,
            bool(contract),
            contract is not None,
        )
        if contract is not None:
            for contract_path, expected_value in contract.items():
                actual_value = _nested_value(report, contract_path)
                add(
                    f"{label}.{contract_path}_known",
                    expected_value,
                    actual_value,
                    actual_value == expected_value,
                )
        runtime = _nested_value(report, "backend.runtime")
        environment = report.get("environment")
        add(
            f"{label}.backend.runtime_nonempty",
            True,
            runtime,
            isinstance(runtime, Mapping) and bool(runtime),
        )
        dependencies = environment.get("dependencies") if isinstance(environment, Mapping) else None
        for dependency in ("lancedb", "pyarrow"):
            version = dependencies.get(dependency) if isinstance(dependencies, Mapping) else None
            add(
                f"{label}.environment.dependencies.{dependency}_supported",
                str(RETRIEVAL_DEPENDENCY_MINIMUMS.get(dependency) or "valid version"),
                version,
                _dependency_version_is_supported(dependency, version),
            )
        add(
            f"{label}.environment_nonempty",
            True,
            environment,
            isinstance(environment, Mapping) and bool(environment),
        )
        source_fingerprint = _nested_value(report, "environment.source_fingerprint")
        add(
            f"{label}.environment.source_fingerprint_valid",
            "64 lowercase hex characters",
            source_fingerprint,
            isinstance(source_fingerprint, str)
            and re.fullmatch(r"[0-9a-f]{64}", source_fingerprint) is not None,
        )
        current_source_fingerprint = _current_report_source_fingerprint(report)
        add(
            f"{label}.environment.source_fingerprint_current",
            current_source_fingerprint,
            source_fingerprint,
            current_source_fingerprint is not None
            and source_fingerprint == current_source_fingerprint,
        )
        retrieval_configuration = _nested_value(report, "environment.retrieval_configuration")
        for name, expected_value in LIVE_RETRIEVAL_CONFIGURATION.items():
            actual_value = (
                retrieval_configuration.get(name)
                if isinstance(retrieval_configuration, Mapping)
                else None
            )
            add(
                f"{label}.environment.retrieval_configuration.{name}",
                expected_value,
                actual_value,
                actual_value == expected_value,
            )
        warmup = _nested_value(report, "execution.warmup")
        repeat = _nested_value(report, "execution.repeat")
        add(
            f"{label}.execution.warmup_valid",
            "integer >= 0",
            warmup,
            isinstance(warmup, int) and not isinstance(warmup, bool) and warmup >= 0,
        )
        add(
            f"{label}.execution.repeat_valid",
            "integer > 0",
            repeat,
            isinstance(repeat, int) and not isinstance(repeat, bool) and repeat > 0,
        )
        candidate_name = report.get("candidate")
        add(
            f"{label}.candidate_identity",
            expected_candidate,
            candidate_name,
            candidate_name == expected_candidate,
        )
        mode = _nested_value(report, "backend.mode")
        add(f"{label}.backend.mode", "live", mode, mode == "live")
        deterministic = _nested_value(report, "backend.deterministic")
        add(
            f"{label}.backend.deterministic",
            False,
            deterministic,
            deterministic is False,
        )
        fallback = _nested_value(report, "backend.fallback_used")
        add(
            f"{label}.backend.fallback_used",
            False,
            fallback,
            fallback is False,
        )
        degraded = _nested_value(report, "backend.degraded_used")
        add(
            f"{label}.backend.degraded_used",
            False,
            degraded,
            degraded is False,
        )
        publishable = report.get("publishable_claim")
        add(
            f"{label}.publishable_claim",
            True,
            publishable,
            publishable is True,
        )
        policy = _nested_value(report, "backend.index_text_policy")
        add(
            f"{label}.backend.index_text_policy",
            expected_candidate,
            policy,
            policy == expected_candidate,
        )
        model = _nested_value(report, "backend.model")
        dimension = _nested_value(report, "backend.dimension")
        add(f"{label}.backend.model_present", True, model, bool(str(model or "").strip()))
        add(
            f"{label}.backend.dimension_positive",
            True,
            dimension,
            isinstance(dimension, int) and not isinstance(dimension, bool) and dimension > 0,
        )

        corpus_count = _nested_value(report, "corpus.count")
        case_count = _nested_value(report, "cases.count")
        metric_case_count = _nested_value(report, "metrics.case_count")
        add(
            f"{label}.metrics.case_count",
            case_count,
            metric_case_count,
            type(case_count) is int and case_count > 0 and metric_case_count == case_count,
        )
        seeded = _nested_value(report, "isolated_corpus.seeded")
        canonical_count = _nested_value(report, "isolated_corpus.canonical_count")
        derived_count = _nested_value(report, "isolated_corpus.derived_count")
        eligible_count = _nested_value(report, "isolated_corpus.eligible_count")
        add(f"{label}.isolated_corpus.seeded", True, seeded, seeded is True)
        add(
            f"{label}.isolated_corpus.canonical_count",
            corpus_count,
            canonical_count,
            isinstance(corpus_count, int) and corpus_count > 0 and canonical_count == corpus_count,
        )
        add(
            f"{label}.isolated_corpus.derived_count",
            eligible_count,
            derived_count,
            isinstance(eligible_count, int)
            and not isinstance(eligible_count, bool)
            and eligible_count > 0
            and derived_count == eligible_count,
        )
        fallback_rate = _nested_value(report, "metrics.fallback_rate")
        degradation_rate = _nested_value(report, "metrics.degradation_rate")
        add(
            f"{label}.metrics.fallback_rate",
            0.0,
            fallback_rate,
            isinstance(fallback_rate, (int, float)) and fallback_rate == 0.0,
        )
        add(
            f"{label}.metrics.degradation_rate",
            0.0,
            degradation_rate,
            isinstance(degradation_rate, (int, float)) and degradation_rate == 0.0,
        )
        for smoke_field in (
            "store",
            "recall",
            "supply",
            "verified_visible",
            "forbidden_hidden",
            "passed",
        ):
            smoke_value = _nested_value(report, f"smoke.{smoke_field}")
            add(
                f"{label}.smoke.{smoke_field}",
                True,
                smoke_value,
                smoke_value is True,
            )

    failures = [check for check in checks if not check["passed"]]
    return {"passed": not failures, "checks": checks, "failures": failures}


def _nested_value(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _append_p95_ratio_check(
    payload: dict[str, Any],
    baseline: MetricSummary,
    candidate: MetricSummary,
    max_ratio: float | None,
) -> None:
    if max_ratio is None:
        return
    if max_ratio <= 0:
        raise ValueError("--max-p95-ratio must be positive")
    if baseline.p95_ms == 0.0:
        passed = candidate.p95_ms == 0.0
        ratio = None
    else:
        ratio = candidate.p95_ms / baseline.p95_ms
        passed = ratio <= max_ratio
    check = {
        "metric": "p95_ms_ratio",
        "split": "overall",
        "baseline": baseline.p95_ms,
        "candidate": candidate.p95_ms,
        "ratio": ratio,
        "limit": max_ratio,
        "direction": "lower-is-better",
        "passed": passed,
    }
    payload["checks"].append(check)
    if not passed:
        payload["regressions"].append(check)
        payload["passed"] = False
        payload["status"] = "fail"


def _metrics_payload(report: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("report is missing metrics")
    return metrics


def _read_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _report_console_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    metrics = report["metrics"]
    return {
        "output_schema": report["schema_version"],
        "dataset_revision": report["dataset_revision"],
        "backend": report["backend"]["mode"],
        "candidate": report["candidate"],
        "hit_at_5": metrics["hit_at"]["5"],
        "mrr": metrics["mrr"],
        "forbidden_hit_rate": metrics["forbidden_hit_rate"],
        "publishable_claim": report["publishable_claim"],
        "gate": report["gate"]["status"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
