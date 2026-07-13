"""Deterministic recall-quality metrics for fixed acceptance datasets.

This module deliberately does not own retrieval.  Callers provide a callback so
the same metric code can evaluate a deterministic test double or the live
ContextEngine without changing either retrieval implementation.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATASET_SCHEMA_VERSION = "recall-quality/v1"
REPORT_SCHEMA_VERSION = "recall-quality-report/v1"
ALLOWED_LANGUAGES = frozenset({"en", "zh", "cross-lingual"})
ALLOWED_GROUPS = frozenset({"token-overlap", "partial-overlap", "zero-overlap"})
DEFAULT_KS = (1, 3, 5, 10)
FORBIDDEN_CUTOFF = 10
ALLOWED_SYNTHESIS_STATUSES = frozenset({"not-synthesis", "draft", "verified", "contested", "stale"})
REQUIRED_LANGUAGE_SPLITS = frozenset({"en", "zh"})
REQUIRED_GROUP_SPLITS = frozenset({"zero-overlap"})
CHANNEL_STATE_FLAGS = (
    "planned",
    "enabled",
    "available",
    "executed",
    "participating",
    "evidence_only",
)
RANK_CHANNELS = frozenset({"vector", "bm25", "fts"})


@dataclass(frozen=True)
class RecallCorpusRecord:
    """One explicit canonical record installed for a benchmark run."""

    memory_id: str
    content: str
    domain: str
    category: str
    l0_abstract: str
    l1_summary: str
    project_id: str
    memory_type: str
    synthesis_status: str
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "domain": self.domain,
            "category": self.category,
            "l0_abstract": self.l0_abstract,
            "l1_summary": self.l1_summary,
            "project_id": self.project_id,
            "memory_type": self.memory_type,
            "synthesis_status": self.synthesis_status,
            **dict(self.metadata),
        }


@dataclass(frozen=True)
class RecallDataset:
    """Validated cases plus the exact canonical corpus they reference."""

    schema_version: str
    evidence_role: str
    dataset_revision: str
    corpus_revision: str
    synthesis_provenance_revision: str
    corpus_hash: str
    case_hash: str
    corpus: tuple[RecallCorpusRecord, ...]
    cases: tuple[RecallCase, ...]

    @property
    def corpus_count(self) -> int:
        return len(self.corpus)

    @property
    def case_count(self) -> int:
        return len(self.cases)


@dataclass(frozen=True)
class RecallCase:
    """One versioned retrieval-quality case."""

    case_id: str
    language: str
    group: str
    query: str
    relevant_memory_ids: tuple[str, ...]
    forbidden_memory_ids: tuple[str, ...]
    task_type: str
    project_id: str
    distractor_memory_ids: tuple[str, ...] = ()
    dataset_revision: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "relevant_memory_ids", _id_tuple(self.relevant_memory_ids))
        object.__setattr__(self, "forbidden_memory_ids", _id_tuple(self.forbidden_memory_ids))
        object.__setattr__(self, "distractor_memory_ids", _id_tuple(self.distractor_memory_ids))


@dataclass(frozen=True)
class ChannelRankingItem:
    """One admitted pre-fusion item with its channel-local score and rank."""

    memory_id: str
    score: float
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.memory_id, "score": self.score, "rank": self.rank}


@dataclass(frozen=True)
class CaseResult:
    """Measured outcome for one recall case."""

    case_id: str
    language: str
    group: str
    ranked_ids: tuple[str, ...]
    relevant_rank: int | None
    hit_at: Mapping[int, bool]
    reciprocal_rank: float
    forbidden_hit: bool
    latency_ms: float
    latency_samples_ms: tuple[float, ...]
    fallback_used: bool
    degraded: bool
    channel_rankings: Mapping[str, tuple[ChannelRankingItem, ...]] = field(default_factory=dict)
    channel_states: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    retrieval_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def channel_ranked_ids(self) -> dict[str, tuple[str, ...]]:
        """Compatibility view for older callers that consumed ID-only channels."""

        channels = {
            name: tuple(item.memory_id for item in ranking)
            for name, ranking in self.channel_rankings.items()
        }
        channels["fused"] = self.ranked_ids
        return channels

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "language": self.language,
            "group": self.group,
            "ranked_ids": list(self.ranked_ids),
            "relevant_rank": self.relevant_rank,
            "hit_at": {str(k): bool(v) for k, v in sorted(self.hit_at.items())},
            "reciprocal_rank": self.reciprocal_rank,
            "forbidden_hit": self.forbidden_hit,
            "latency_ms": self.latency_ms,
            "latency_samples_ms": list(self.latency_samples_ms),
            "fallback_used": self.fallback_used,
            "degraded": self.degraded,
            "channels": {name: list(ids) for name, ids in sorted(self.channel_ranked_ids.items())},
            "channel_rankings": {
                name: [item.to_dict() for item in ranking]
                for name, ranking in sorted(self.channel_rankings.items())
            },
            "channel_states": {
                name: dict(state) for name, state in sorted(self.channel_states.items())
            },
            "metadata": dict(self.retrieval_metadata),
        }


@dataclass(frozen=True)
class MetricSlice:
    """Aggregate metrics for all cases or a named split."""

    case_count: int
    hit_at: Mapping[int, float]
    mrr: float
    forbidden_hit_rate: float
    p50_ms: float
    p95_ms: float
    fallback_rate: float
    degradation_rate: float
    fallback_or_degradation_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_count": self.case_count,
            "hit_at": {str(k): value for k, value in sorted(self.hit_at.items())},
            "mrr": self.mrr,
            "forbidden_hit_rate": self.forbidden_hit_rate,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "fallback_rate": self.fallback_rate,
            "degradation_rate": self.degradation_rate,
            "fallback_or_degradation_rate": self.fallback_or_degradation_rate,
        }


@dataclass(frozen=True)
class ChannelMetricSummary:
    """Overall and split metrics for one complete pre-fusion channel."""

    overall: MetricSlice
    by_language: Mapping[str, MetricSlice] = field(default_factory=dict)
    by_group: Mapping[str, MetricSlice] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.to_dict(),
            "language": {
                name: metrics.to_dict() for name, metrics in sorted(self.by_language.items())
            },
            "group": {name: metrics.to_dict() for name, metrics in sorted(self.by_group.items())},
        }


@dataclass(frozen=True)
class MetricSummary(MetricSlice):
    """Overall recall metrics with language, group, and channel splits."""

    by_language: Mapping[str, MetricSlice] = field(default_factory=dict)
    by_group: Mapping[str, MetricSlice] = field(default_factory=dict)
    channels: Mapping[str, ChannelMetricSummary] = field(default_factory=dict)
    channel_states: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    case_results: tuple[CaseResult, ...] = ()

    @property
    def language(self) -> Mapping[str, MetricSlice]:
        """Compatibility alias for report terminology."""

        return self.by_language

    @property
    def group(self) -> Mapping[str, MetricSlice]:
        """Compatibility alias for report terminology."""

        return self.by_group

    @property
    def splits(self) -> dict[str, Mapping[str, MetricSlice]]:
        return {"language": self.by_language, "group": self.by_group}

    def to_dict(self, *, include_cases: bool = True) -> dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "language": {
                    name: metrics.to_dict() for name, metrics in sorted(self.by_language.items())
                },
                "group": {
                    name: metrics.to_dict() for name, metrics in sorted(self.by_group.items())
                },
                "channels": {
                    name: metrics.to_dict() for name, metrics in sorted(self.channels.items())
                },
                "channel_states": {
                    name: dict(state) for name, state in sorted(self.channel_states.items())
                },
            }
        )
        if include_cases:
            payload["cases"] = [result.to_dict() for result in self.case_results]
        return payload


@dataclass(frozen=True)
class ComparisonCheck:
    metric: str
    baseline: float
    candidate: float
    tolerance: float
    direction: str
    passed: bool
    split: str = "overall"

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "split": self.split,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "tolerance": self.tolerance,
            "direction": self.direction,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class ComparisonResult:
    passed: bool
    checks: tuple[ComparisonCheck, ...]
    regressions: tuple[ComparisonCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "pass" if self.passed else "fail",
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "regressions": [check.to_dict() for check in self.regressions],
        }


@dataclass(frozen=True)
class _RetrievalOutcome:
    ranked_ids: tuple[str, ...]
    latency_samples_ms: tuple[float, ...]
    fallback_used: bool
    degraded: bool
    channel_rankings: Mapping[str, tuple[ChannelRankingItem, ...]]
    channel_states: Mapping[str, Mapping[str, Any]]
    metadata: Mapping[str, Any]


def load_dataset(path: str | Path) -> list[RecallCase]:
    """Load and validate a versioned recall-quality JSON dataset."""

    return list(load_dataset_bundle(path).cases)


def load_dataset_bundle(path: str | Path) -> RecallDataset:
    """Load cases and their explicit, versioned canonical fixture corpus."""

    dataset_path = Path(path)
    try:
        root = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid recall-quality dataset {dataset_path}: {exc}") from exc

    if not isinstance(root, dict):
        raise ValueError("dataset root must be a JSON object")
    schema_version = str(root.get("schema_version") or "").strip()
    if schema_version != DATASET_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {DATASET_SCHEMA_VERSION!r}, got {schema_version!r}"
        )
    evidence_role = str(root.get("evidence_role") or "").strip()
    if evidence_role not in {"calibration", "held-out"}:
        raise ValueError("evidence_role must be 'calibration' or 'held-out'")
    revision = str(root.get("dataset_revision") or "").strip()
    if not revision:
        raise ValueError("dataset_revision is required")
    corpus_revision = str(root.get("corpus_revision") or "").strip()
    if not corpus_revision:
        raise ValueError("corpus_revision is required")
    synthesis_provenance_revision = str(root.get("synthesis_provenance_revision") or "").strip()
    if not synthesis_provenance_revision:
        raise ValueError("synthesis_provenance_revision is required")
    corpus = _load_corpus(root.get("corpus"))
    raw_cases = root.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty list")

    cases: list[RecallCase] = []
    seen_ids: set[str] = set()
    known_fields = {
        "case_id",
        "language",
        "group",
        "query",
        "relevant_memory_ids",
        "forbidden_memory_ids",
        "distractor_memory_ids",
        "task_type",
        "project_id",
    }
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise ValueError(f"cases[{index}] must be a JSON object")
        try:
            case = RecallCase(
                case_id=str(raw["case_id"]).strip(),
                language=str(raw["language"]).strip(),
                group=str(raw["group"]).strip(),
                query=str(raw["query"]).strip(),
                relevant_memory_ids=_required_id_list(
                    raw.get("relevant_memory_ids"), "relevant_memory_ids", index
                ),
                forbidden_memory_ids=_optional_id_list(
                    raw.get("forbidden_memory_ids", []), "forbidden_memory_ids", index
                ),
                distractor_memory_ids=_optional_id_list(
                    raw.get("distractor_memory_ids", []), "distractor_memory_ids", index
                ),
                task_type=str(raw["task_type"]).strip(),
                project_id=str(raw["project_id"]).strip(),
                dataset_revision=revision,
                metadata={key: value for key, value in raw.items() if key not in known_fields},
            )
        except KeyError as exc:
            raise ValueError(f"cases[{index}] is missing required field {exc.args[0]!r}") from exc
        _validate_case(case, index=index)
        if case.case_id in seen_ids:
            raise ValueError(f"duplicate case_id: {case.case_id}")
        seen_ids.add(case.case_id)
        cases.append(case)

    referenced_ids = {
        memory_id
        for case in cases
        for memory_id in (
            *case.relevant_memory_ids,
            *case.forbidden_memory_ids,
            *case.distractor_memory_ids,
        )
    }
    corpus_ids = {record.memory_id for record in corpus}
    missing_ids = sorted(referenced_ids - corpus_ids)
    if missing_ids:
        raise ValueError(f"missing corpus record for referenced IDs: {missing_ids!r}")
    extra_ids = sorted(corpus_ids - referenced_ids)
    if extra_ids:
        raise ValueError(f"unreferenced corpus record IDs: {extra_ids!r}")
    corpus = _attach_synthesis_provenance(
        corpus,
        root.get("synthesis_provenance"),
        synthesis_provenance_revision,
    )

    return RecallDataset(
        schema_version=schema_version,
        evidence_role=evidence_role,
        dataset_revision=revision,
        corpus_revision=corpus_revision,
        synthesis_provenance_revision=synthesis_provenance_revision,
        corpus_hash=_corpus_hash(corpus),
        case_hash=_case_hash(cases),
        corpus=tuple(corpus),
        cases=tuple(cases),
    )


def evaluate_cases(
    cases: Iterable[RecallCase],
    retrieve: Callable[..., Any],
    *,
    ks: Sequence[int] = DEFAULT_KS,
) -> MetricSummary:
    """Evaluate fixed cases through a caller-owned retrieval function.

    The preferred callback signature is ``retrieve(case)``.  For small external
    adapters, ``retrieve(query, task_type, project_id)`` is also accepted.
    Callback results may be a ranked ID sequence or a mapping containing
    ``ranked_ids``, optional ``channels``, latency samples, and fallback flags.
    """

    case_list = list(cases)
    if not case_list:
        raise ValueError("evaluate_cases requires at least one case")
    normalized_ks = tuple(sorted({int(k) for k in ks if int(k) > 0}))
    if not normalized_ks:
        raise ValueError("ks must contain at least one positive cutoff")
    seen: set[str] = set()
    for index, case in enumerate(case_list):
        _validate_case(case, index=index)
        if case.case_id in seen:
            raise ValueError(f"duplicate case_id: {case.case_id}")
        seen.add(case.case_id)

    results: list[CaseResult] = []
    for case in case_list:
        started = time.perf_counter()
        raw_result = _invoke_retrieve(retrieve, case)
        measured_latency_ms = (time.perf_counter() - started) * 1000.0
        outcome = _normalize_retrieval(raw_result, measured_latency_ms)
        results.append(_case_result(case, outcome, normalized_ks))

    overall = _metric_slice(results, normalized_ks)
    by_language = _split_metrics(results, normalized_ks, "language")
    by_group = _split_metrics(results, normalized_ks, "group")
    channel_names = sorted({name for result in results for name in result.channel_rankings})
    channels: dict[str, ChannelMetricSummary] = {}
    channel_names.append("fused")
    for channel_name in channel_names:
        channel_results = [
            _with_channel_ranking(result, channel_name, case, normalized_ks)
            for result, case in zip(results, case_list, strict=True)
        ]
        channels[channel_name] = ChannelMetricSummary(
            overall=_metric_slice(channel_results, normalized_ks),
            by_language=_split_metrics(channel_results, normalized_ks, "language"),
            by_group=_split_metrics(channel_results, normalized_ks, "group"),
        )

    return MetricSummary(
        **overall.__dict__,
        by_language=by_language,
        by_group=by_group,
        channels=channels,
        channel_states=_aggregate_channel_states(results),
        case_results=tuple(results),
    )


def compare_summaries(
    baseline: MetricSummary,
    candidate: MetricSummary,
    tolerance: float | Mapping[str, float] = 0.0,
) -> ComparisonResult:
    """Compare two summaries using transparent higher/lower-is-better checks.

    This is the policy-neutral comparison foundation.  Callers can supply a
    single absolute tolerance or per-metric tolerances; candidate-specific
    policy acceptance remains outside the metric runner.
    """

    checks: list[ComparisonCheck] = []

    def allowed(metric: str) -> float:
        if isinstance(tolerance, Mapping):
            value = tolerance.get(metric, tolerance.get("default", 0.0))
        else:
            value = tolerance
        numeric = float(value)
        if numeric < 0:
            raise ValueError("tolerance values must be non-negative")
        return numeric

    def higher(metric: str, base: float, current: float, *, split: str = "overall") -> None:
        margin = allowed(metric)
        checks.append(
            ComparisonCheck(
                metric=metric,
                baseline=float(base),
                candidate=float(current),
                tolerance=margin,
                direction="higher-is-better",
                passed=float(current) + margin >= float(base),
                split=split,
            )
        )

    def lower(metric: str, base: float, current: float, *, split: str = "overall") -> None:
        margin = allowed(metric)
        checks.append(
            ComparisonCheck(
                metric=metric,
                baseline=float(base),
                candidate=float(current),
                tolerance=margin,
                direction="lower-is-better",
                passed=float(current) <= float(base) + margin,
                split=split,
            )
        )

    def exact(
        metric: str,
        base: float,
        current: float,
        *,
        passed: bool | None = None,
        split: str = "overall",
    ) -> None:
        checks.append(
            ComparisonCheck(
                metric=metric,
                baseline=float(base),
                candidate=float(current),
                tolerance=0.0,
                direction="equal",
                passed=(float(base) == float(current)) if passed is None else bool(passed),
                split=split,
            )
        )

    _append_channel_comparability_checks(checks, baseline, candidate)
    channel_regressions = tuple(check for check in checks if not check.passed)
    if channel_regressions:
        return ComparisonResult(
            passed=False,
            checks=tuple(checks),
            regressions=channel_regressions,
        )

    exact("case_count", baseline.case_count, candidate.case_count)
    exact(
        "hit_at_key_set",
        len(baseline.hit_at),
        len(candidate.hit_at),
        passed=set(baseline.hit_at) == set(candidate.hit_at),
    )

    for split_name, baseline_splits, candidate_splits, required_names in (
        (
            "language",
            baseline.by_language,
            candidate.by_language,
            REQUIRED_LANGUAGE_SPLITS,
        ),
        ("group", baseline.by_group, candidate.by_group, REQUIRED_GROUP_SPLITS),
    ):
        baseline_names = set(baseline_splits)
        candidate_names = set(candidate_splits)
        exact(
            "split_key_set",
            len(baseline_names),
            len(candidate_names),
            passed=baseline_names == candidate_names,
            split=split_name,
        )
        for name in sorted(required_names):
            baseline_present = name in baseline_names
            candidate_present = name in candidate_names
            exact(
                "required_split",
                float(baseline_present),
                float(candidate_present),
                passed=baseline_present and candidate_present,
                split=f"{split_name}:{name}",
            )
        exact(
            "split_case_total",
            baseline.case_count,
            sum(item.case_count for item in baseline_splits.values()),
            split=f"{split_name}:baseline",
        )
        exact(
            "split_case_total",
            candidate.case_count,
            sum(item.case_count for item in candidate_splits.values()),
            split=f"{split_name}:candidate",
        )
        for name in sorted(baseline_names & candidate_names):
            exact(
                "case_count",
                baseline_splits[name].case_count,
                candidate_splits[name].case_count,
                split=f"{split_name}:{name}",
            )

    higher("mrr", baseline.mrr, candidate.mrr)
    for k in sorted(set(baseline.hit_at) & set(candidate.hit_at)):
        higher(f"hit_at_{k}", baseline.hit_at[k], candidate.hit_at[k])
    lower("forbidden_hit_rate", baseline.forbidden_hit_rate, candidate.forbidden_hit_rate)
    lower("fallback_rate", baseline.fallback_rate, candidate.fallback_rate)
    lower("degradation_rate", baseline.degradation_rate, candidate.degradation_rate)

    for split_name, baseline_splits, candidate_splits in (
        ("language", baseline.by_language, candidate.by_language),
        ("group", baseline.by_group, candidate.by_group),
    ):
        for name in sorted(set(baseline_splits) & set(candidate_splits)):
            baseline_slice = baseline_splits[name]
            candidate_slice = candidate_splits[name]
            if 5 in baseline_slice.hit_at and 5 in candidate_slice.hit_at:
                higher(
                    "hit_at_5",
                    baseline_slice.hit_at[5],
                    candidate_slice.hit_at[5],
                    split=f"{split_name}:{name}",
                )

    regressions = tuple(check for check in checks if not check.passed)
    return ComparisonResult(
        passed=not regressions,
        checks=tuple(checks),
        regressions=regressions,
    )


def _append_channel_comparability_checks(
    checks: list[ComparisonCheck],
    baseline: MetricSummary,
    candidate: MetricSummary,
) -> None:
    baseline_planned = {
        name for name, state in baseline.channel_states.items() if state.get("planned") is True
    }
    candidate_planned = {
        name for name, state in candidate.channel_states.items() if state.get("planned") is True
    }
    if baseline.channel_states or candidate.channel_states:
        checks.append(
            ComparisonCheck(
                metric="planned_channel_set",
                baseline=float(len(baseline_planned)),
                candidate=float(len(candidate_planned)),
                tolerance=0.0,
                direction="equal",
                passed=baseline_planned == candidate_planned,
                split="channels",
            )
        )

    for label, summary in (("baseline", baseline), ("candidate", candidate)):
        for channel in sorted(
            name
            for name, state in summary.channel_states.items()
            if state.get("planned") is True
            and state.get("enabled") is True
            and state.get("evidence_only") is False
        ):
            state = summary.channel_states[channel]
            for metric, field_name in (
                ("channel_available", "available"),
                ("channel_executed", "executed"),
            ):
                actual = state.get(field_name) is True
                checks.append(
                    ComparisonCheck(
                        metric=metric,
                        baseline=1.0,
                        candidate=float(actual),
                        tolerance=0.0,
                        direction="equal",
                        passed=actual,
                        split=f"{label}:channel:{channel}",
                    )
                )
            present = channel in summary.channels
            checks.append(
                ComparisonCheck(
                    metric="channel_metric_present",
                    baseline=1.0,
                    candidate=float(present),
                    tolerance=0.0,
                    direction="equal",
                    passed=present,
                    split=f"{label}:channel:{channel}",
                )
            )

    for channel in sorted(baseline_planned & candidate_planned):
        for field_name in ("enabled", "evidence_only"):
            baseline_value = baseline.channel_states[channel].get(field_name) is True
            candidate_value = candidate.channel_states[channel].get(field_name) is True
            checks.append(
                ComparisonCheck(
                    metric=f"channel_{field_name}",
                    baseline=float(baseline_value),
                    candidate=float(candidate_value),
                    tolerance=0.0,
                    direction="equal",
                    passed=baseline_value == candidate_value,
                    split=f"channel:{channel}",
                )
            )


def evaluate_best_constituent_gate(
    summary: MetricSummary,
    *,
    required_languages: Sequence[str] = ("en", "zh", "cross-lingual"),
    required_groups: Sequence[str] = (
        "token-overlap",
        "partial-overlap",
        "zero-overlap",
    ),
    tolerance: float = 0.0,
) -> dict[str, Any]:
    """Require fused MRR/hit@5 to match the best executable rank channel."""

    margin = float(tolerance)
    if not math.isfinite(margin) or margin < 0.0:
        raise ValueError("best constituent tolerance must be finite and non-negative")

    checks: list[dict[str, Any]] = []

    def add(
        metric: str,
        split: str,
        fused: float | None,
        best: float | None,
        *,
        best_channel: str = "",
        passed: bool,
        reason: str = "",
    ) -> None:
        checks.append(
            {
                "metric": metric,
                "split": split,
                "fused": fused,
                "best_constituent": best,
                "best_channel": best_channel,
                "tolerance": margin,
                "passed": bool(passed),
                "reason": reason,
            }
        )

    eligible: list[str] = []
    for channel, state in sorted(summary.channel_states.items()):
        if (
            state.get("planned") is not True
            or state.get("enabled") is not True
            or state.get("evidence_only") is True
        ):
            continue
        ready = state.get("available") is True and state.get("executed") is True
        metric_present = channel in summary.channels
        add(
            "channel_ready",
            f"channel:{channel}",
            None,
            None,
            best_channel=channel,
            passed=ready and metric_present,
            reason=("" if ready and metric_present else str(state.get("reason") or "missing")),
        )
        if ready and metric_present:
            # A zero fusion weight or participating=false does not hide an executed channel.
            eligible.append(channel)

    if "fused" not in summary.channels:
        add("fused_metric_present", "overall", None, None, passed=False, reason="missing")
    if not eligible:
        add(
            "constituent_metric_present",
            "overall",
            None,
            None,
            passed=False,
            reason="no_ready_rank_channel",
        )

    readiness_failures = [check for check in checks if not check["passed"]]
    if readiness_failures:
        return {
            "status": "fail",
            "passed": False,
            "checks": checks,
            "failures": readiness_failures,
        }

    fused_summary = summary.channels["fused"]

    def check_slice(
        split: str,
        fused_slice: MetricSlice | None,
        constituent_slices: Mapping[str, MetricSlice | None],
    ) -> None:
        if fused_slice is None or any(value is None for value in constituent_slices.values()):
            add(
                "required_split",
                split,
                None,
                None,
                passed=False,
                reason="missing_channel_split",
            )
            return
        typed_slices = {
            channel: value for channel, value in constituent_slices.items() if value is not None
        }
        for metric, getter in (
            ("mrr", lambda metrics: metrics.mrr),
            ("hit_at_5", lambda metrics: metrics.hit_at.get(5, 0.0)),
        ):
            best_channel, best_value = max(
                ((channel, getter(metrics)) for channel, metrics in typed_slices.items()),
                key=lambda item: (item[1], item[0]),
            )
            fused_value = getter(fused_slice)
            add(
                metric,
                split,
                fused_value,
                best_value,
                best_channel=best_channel,
                passed=fused_value + margin >= best_value,
            )

    check_slice(
        "overall",
        fused_summary.overall,
        {channel: summary.channels[channel].overall for channel in eligible},
    )
    for language in required_languages:
        check_slice(
            f"language:{language}",
            fused_summary.by_language.get(language),
            {channel: summary.channels[channel].by_language.get(language) for channel in eligible},
        )
    for group in required_groups:
        check_slice(
            f"group:{group}",
            fused_summary.by_group.get(group),
            {channel: summary.channels[channel].by_group.get(group) for channel in eligible},
        )

    failures = [check for check in checks if not check["passed"]]
    return {
        "status": "fail" if failures else "pass",
        "passed": not failures,
        "checks": checks,
        "failures": failures,
    }


def metric_summary_from_dict(payload: Mapping[str, Any]) -> MetricSummary:
    """Deserialize report metrics for an explicit baseline comparison."""

    overall = _metric_slice_from_dict(payload, label="metrics")
    language_payload = _required_mapping(payload.get("language"), "metrics.language")
    group_payload = _required_mapping(payload.get("group"), "metrics.group")
    channel_payload = _required_mapping(payload.get("channels"), "metrics.channels")
    if set(language_payload) != set(ALLOWED_LANGUAGES):
        raise ValueError("metrics.language split keys do not match the fixed dataset")
    if set(group_payload) != set(ALLOWED_GROUPS):
        raise ValueError("metrics.group split keys do not match the fixed dataset")
    if "fused" not in channel_payload:
        raise ValueError("metrics.channels missing required fused channel")
    by_language = {
        str(name): _metric_slice_from_dict(value, label=f"metrics.language.{name}")
        for name, value in language_payload.items()
    }
    by_group = {
        str(name): _metric_slice_from_dict(value, label=f"metrics.group.{name}")
        for name, value in group_payload.items()
    }
    channels: dict[str, ChannelMetricSummary] = {}
    for raw_name, value in channel_payload.items():
        name = _canonical_channel_name(raw_name)
        if name in channels:
            raise ValueError(f"metrics.channels duplicate canonical channel: {name}")
        channels[name] = _channel_metric_summary_from_dict(
            value,
            label=f"metrics.channels.{raw_name}",
        )
    state_payload = payload.get("channel_states", {})
    channel_states = (
        _normalize_channel_states(state_payload)
        if isinstance(state_payload, Mapping) and state_payload
        else {}
    )
    return MetricSummary(
        **overall.__dict__,
        by_language=by_language,
        by_group=by_group,
        channels=channels,
        channel_states=channel_states,
        case_results=(),
    )


def quality_payload(summary: MetricSummary) -> dict[str, Any]:
    """Return stable quality-only fields, excluding wall-clock measurements."""

    def quality(metrics: MetricSlice) -> dict[str, Any]:
        return {
            "case_count": metrics.case_count,
            "hit_at": {str(k): value for k, value in sorted(metrics.hit_at.items())},
            "mrr": metrics.mrr,
            "forbidden_hit_rate": metrics.forbidden_hit_rate,
            "fallback_rate": metrics.fallback_rate,
            "degradation_rate": metrics.degradation_rate,
            "fallback_or_degradation_rate": metrics.fallback_or_degradation_rate,
        }

    return {
        "overall": quality(summary),
        "language": {name: quality(value) for name, value in sorted(summary.by_language.items())},
        "group": {name: quality(value) for name, value in sorted(summary.by_group.items())},
        "channels": {
            name: {
                "overall": quality(value.overall),
                "language": {
                    split: quality(metrics) for split, metrics in sorted(value.by_language.items())
                },
                "group": {
                    split: quality(metrics) for split, metrics in sorted(value.by_group.items())
                },
            }
            for name, value in sorted(summary.channels.items())
        },
    }


def _load_corpus(value: Any) -> list[RecallCorpusRecord]:
    if not isinstance(value, list) or not value:
        raise ValueError("corpus must be a non-empty list")

    known_fields = {
        "memory_id",
        "content",
        "domain",
        "category",
        "l0_abstract",
        "l1_summary",
        "project_id",
        "memory_type",
        "synthesis_status",
    }
    records: list[RecallCorpusRecord] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"corpus[{index}] must be a JSON object")
        memory_id = _required_text(raw, "memory_id", f"corpus[{index}]")
        if memory_id in seen_ids:
            raise ValueError(f"duplicate corpus memory_id: {memory_id}")
        seen_ids.add(memory_id)
        record = RecallCorpusRecord(
            memory_id=memory_id,
            content=_required_text(raw, "content", memory_id),
            domain=_required_text(raw, "domain", memory_id),
            category=_required_text(raw, "category", memory_id),
            l0_abstract=_required_text(raw, "l0_abstract", memory_id),
            l1_summary=_required_text(raw, "l1_summary", memory_id),
            project_id=_required_text(raw, "project_id", memory_id),
            memory_type=_required_text(raw, "memory_type", memory_id).casefold(),
            synthesis_status=_required_text(raw, "synthesis_status", memory_id).casefold(),
            metadata={key: item for key, item in raw.items() if key not in known_fields},
        )
        if record.synthesis_status not in ALLOWED_SYNTHESIS_STATUSES:
            raise ValueError(f"{memory_id}: unknown synthesis_status {record.synthesis_status!r}")
        if record.memory_type == "synthesis":
            if record.synthesis_status == "not-synthesis":
                raise ValueError(f"{memory_id}: synthesis record requires a lifecycle status")
        elif record.synthesis_status != "not-synthesis":
            raise ValueError(
                f"{memory_id}: non-synthesis record must use synthesis_status 'not-synthesis'"
            )
        records.append(record)

    fixture_ids = tuple(record.memory_id.casefold() for record in records)
    for record in records:
        indexable_text = "\n".join(
            (record.content, record.l0_abstract, record.l1_summary)
        ).casefold()
        leaked_id = next(
            (fixture_id for fixture_id in fixture_ids if fixture_id in indexable_text),
            "",
        )
        if leaked_id:
            raise ValueError(
                f"{record.memory_id}: fixture ID leaked into indexable text: {leaked_id}"
            )
    return records


def _attach_synthesis_provenance(
    records: Sequence[RecallCorpusRecord],
    value: Any,
    revision: str,
) -> list[RecallCorpusRecord]:
    if not isinstance(value, Mapping):
        raise ValueError("synthesis_provenance must be a JSON object")
    by_id = {record.memory_id: record for record in records}
    synthesis_ids = {record.memory_id for record in records if record.memory_type == "synthesis"}
    provenance_ids = {str(memory_id) for memory_id in value}
    missing = sorted(synthesis_ids - provenance_ids)
    if missing:
        raise ValueError(f"missing synthesis provenance: {missing!r}")
    extra = sorted(provenance_ids - synthesis_ids)
    if extra:
        raise ValueError(f"unknown synthesis provenance IDs: {extra!r}")

    enriched: list[RecallCorpusRecord] = []
    for record in records:
        if record.memory_type != "synthesis":
            enriched.append(record)
            continue
        raw = value.get(record.memory_id)
        if not isinstance(raw, Mapping):
            raise ValueError(f"{record.memory_id}: synthesis provenance must be an object")
        source_ids = _provenance_source_ids(raw.get("source_ids"), record.memory_id)
        for source_id in source_ids:
            source = by_id.get(source_id)
            if source is None:
                raise ValueError(
                    f"{record.memory_id}: synthesis source missing from corpus: {source_id}"
                )
            if source.memory_type == "synthesis":
                raise ValueError(
                    f"{record.memory_id}: synthesis source must be ordinary: {source_id}"
                )
            if source.project_id != record.project_id:
                raise ValueError(
                    f"{record.memory_id}: synthesis source project mismatch: {source_id}"
                )

        metadata = dict(record.metadata)
        metadata.update(dict(raw))
        metadata["source_ids"] = list(source_ids)
        metadata["provenance_revision"] = revision
        if record.synthesis_status in {"verified", "stale"}:
            for field_name in (
                "verification_actor",
                "verification_call_id",
                "verified_at",
            ):
                _required_text(metadata, field_name, record.memory_id)
            verified_at = str(metadata["verified_at"])
            if "T" not in verified_at or not verified_at.endswith("Z"):
                raise ValueError(f"{record.memory_id}: verified_at must be UTC ISO-8601")
        enriched.append(
            RecallCorpusRecord(
                memory_id=record.memory_id,
                content=record.content,
                domain=record.domain,
                category=record.category,
                l0_abstract=record.l0_abstract,
                l1_summary=record.l1_summary,
                project_id=record.project_id,
                memory_type=record.memory_type,
                synthesis_status=record.synthesis_status,
                metadata=metadata,
            )
        )
    return enriched


def _provenance_source_ids(value: Any, memory_id: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise ValueError(f"{memory_id}: source_ids must be a list")
    source_ids = tuple(str(item or "").strip() for item in value)
    if len(source_ids) < 2 or any(not item for item in source_ids):
        raise ValueError(f"{memory_id}: source_ids require two explicit sources")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError(f"{memory_id}: source_ids must be distinct")
    return source_ids


def _required_text(raw: Mapping[str, Any], field_name: str, label: str) -> str:
    value = str(raw.get(field_name) or "").strip()
    if not value:
        raise ValueError(f"{label}: {field_name} is required")
    return value


def _corpus_hash(records: Sequence[RecallCorpusRecord]) -> str:
    canonical = [record.to_dict() for record in sorted(records, key=lambda item: item.memory_id)]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _case_hash(cases: Sequence[RecallCase]) -> str:
    canonical = [
        {
            "case_id": case.case_id,
            "language": case.language,
            "group": case.group,
            "query": case.query,
            "relevant_memory_ids": list(case.relevant_memory_ids),
            "forbidden_memory_ids": list(case.forbidden_memory_ids),
            "distractor_memory_ids": list(case.distractor_memory_ids),
            "task_type": case.task_type,
            "project_id": case.project_id,
            **dict(case.metadata),
        }
        for case in sorted(cases, key=lambda item: item.case_id)
    ]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_case(case: RecallCase, *, index: int) -> None:
    label = case.case_id or f"cases[{index}]"
    if not case.case_id:
        raise ValueError(f"cases[{index}].case_id is required")
    if case.language not in ALLOWED_LANGUAGES:
        raise ValueError(f"{label}: unknown language {case.language!r}")
    if case.group not in ALLOWED_GROUPS:
        raise ValueError(f"{label}: unknown group {case.group!r}")
    if not case.query:
        raise ValueError(f"{label}: query is required")
    if not case.relevant_memory_ids:
        raise ValueError(f"{label}: relevant_memory_ids must not be empty")
    if not case.task_type:
        raise ValueError(f"{label}: task_type is required")
    if not case.project_id:
        raise ValueError(f"{label}: project_id is required")
    overlap = set(case.relevant_memory_ids) & set(case.forbidden_memory_ids)
    if overlap:
        raise ValueError(f"{label}: relevant and forbidden IDs overlap: {sorted(overlap)!r}")


def _required_id_list(value: Any, field_name: str, index: int) -> tuple[str, ...]:
    ids = _optional_id_list(value, field_name, index)
    if not ids:
        raise ValueError(f"cases[{index}].{field_name} must not be empty")
    return ids


def _optional_id_list(value: Any, field_name: str, index: int) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise ValueError(f"cases[{index}].{field_name} must be a list")
    ids = _id_tuple(value)
    if len(ids) != len(value):
        raise ValueError(f"cases[{index}].{field_name} contains an empty ID")
    if len(set(ids)) != len(ids):
        raise ValueError(f"cases[{index}].{field_name} contains duplicate IDs")
    return ids


def _id_tuple(values: Iterable[Any]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        return (str(values).strip(),) if str(values).strip() else ()
    return tuple(str(value).strip() for value in values if str(value).strip())


def _invoke_retrieve(retrieve: Callable[..., Any], case: RecallCase) -> Any:
    try:
        signature = inspect.signature(retrieve)
    except (TypeError, ValueError):
        return retrieve(case)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if has_varargs or len(positional) <= 1:
        return retrieve(case)
    if len(positional) == 2:
        return retrieve(case.query, case)
    return retrieve(case.query, case.task_type, case.project_id)


def _normalize_retrieval(raw: Any, measured_latency_ms: float) -> _RetrievalOutcome:
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], Mapping):
        combined = dict(raw[1])
        combined.setdefault("ranked_ids", raw[0])
        raw = combined

    if isinstance(raw, Mapping):
        ranked_source = raw.get("ranked_ids", raw.get("ids", raw.get("results")))
        explicit_rankings = "channel_rankings" in raw
        if explicit_rankings:
            channel_rankings = _normalize_channel_rankings(raw.get("channel_rankings"))
            channel_states = _normalize_channel_states(raw.get("channel_states"))
            _validate_channel_evidence(channel_rankings, channel_states)
        else:
            channel_rankings = _normalize_legacy_channels(raw.get("channels"))
            channel_states = _legacy_channel_states(channel_rankings)
        if ranked_source is None:
            legacy_channels = (
                raw.get("channels") if isinstance(raw.get("channels"), Mapping) else {}
            )
            ranked_source = raw.get("fused", legacy_channels.get("fused", ()))
        ranked_ids = _ranked_ids(ranked_source)
        latency_samples = _latency_samples(raw, measured_latency_ms)
        fallback_used = _signal(raw.get("fallback_used", raw.get("fallback")))
        degraded = _signal(raw.get("degraded", raw.get("degradation")))
        metadata = raw.get("metadata")
        return _RetrievalOutcome(
            ranked_ids=ranked_ids,
            latency_samples_ms=latency_samples,
            fallback_used=fallback_used,
            degraded=degraded,
            channel_rankings=channel_rankings,
            channel_states=channel_states,
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )

    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        ranked_ids = _ranked_ids(raw)
        return _RetrievalOutcome(
            ranked_ids=ranked_ids,
            latency_samples_ms=(max(0.0, measured_latency_ms),),
            fallback_used=False,
            degraded=False,
            channel_rankings={},
            channel_states={},
            metadata={},
        )

    ranked_ids = _ranked_ids(getattr(raw, "ranked_ids", getattr(raw, "ids", ())))
    explicit_rankings = hasattr(raw, "channel_rankings")
    if explicit_rankings:
        channel_rankings = _normalize_channel_rankings(raw.channel_rankings)
        channel_states = _normalize_channel_states(getattr(raw, "channel_states", None))
        _validate_channel_evidence(channel_rankings, channel_states)
    else:
        channel_rankings = _normalize_legacy_channels(getattr(raw, "channels", None))
        channel_states = _legacy_channel_states(channel_rankings)
    latency = getattr(raw, "latency_ms", measured_latency_ms)
    return _RetrievalOutcome(
        ranked_ids=ranked_ids,
        latency_samples_ms=(max(0.0, float(latency)),),
        fallback_used=_signal(getattr(raw, "fallback_used", False)),
        degraded=_signal(getattr(raw, "degraded", False)),
        channel_rankings=channel_rankings,
        channel_states=channel_states,
        metadata={},
    )


def _canonical_channel_name(value: Any) -> str:
    name = str(value or "").strip().casefold()
    return "bm25" if name == "lexical" else name


def _normalize_legacy_channels(
    value: Any,
) -> dict[str, tuple[ChannelRankingItem, ...]]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, tuple[ChannelRankingItem, ...]] = {}
    for raw_name, results in value.items():
        name = _canonical_channel_name(raw_name)
        if name == "fused":
            continue
        if name in normalized:
            raise ValueError(f"channels duplicate canonical channel: {name}")
        ids = _ranked_ids(results)
        normalized[name] = tuple(
            ChannelRankingItem(memory_id=memory_id, score=0.0, rank=rank)
            for rank, memory_id in enumerate(ids, start=1)
        )
    return normalized


def _normalize_channel_rankings(
    value: Any,
) -> dict[str, tuple[ChannelRankingItem, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError("channel_rankings must be a mapping")
    normalized: dict[str, tuple[ChannelRankingItem, ...]] = {}
    for raw_name, raw_rows in value.items():
        name = _canonical_channel_name(raw_name)
        if name not in RANK_CHANNELS:
            raise ValueError(f"channel_rankings contains non-rank channel: {name}")
        if name in normalized:
            raise ValueError(f"channel_rankings duplicate canonical channel: {name}")
        if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
            raise ValueError(f"channel_rankings.{name} must be a sequence")
        items: list[ChannelRankingItem] = []
        seen_ids: set[str] = set()
        seen_ranks: set[int] = set()
        for row in raw_rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"channel_rankings.{name} rows must be mappings")
            memory_id = str(row.get("id", row.get("memory_id", "")) or "").strip()
            score = row.get("score")
            rank = row.get("rank")
            if not memory_id or memory_id in seen_ids:
                raise ValueError(f"channel_rankings.{name} has empty or duplicate id")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise ValueError(f"channel_rankings.{name} score must be finite")
            if (
                isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank < 1
                or rank in seen_ranks
            ):
                raise ValueError(f"channel_rankings.{name} rank must be unique positive integer")
            seen_ids.add(memory_id)
            seen_ranks.add(rank)
            items.append(ChannelRankingItem(memory_id, float(score), rank))
        items.sort(key=lambda item: item.rank)
        if [item.rank for item in items] != list(range(1, len(items) + 1)):
            raise ValueError(f"channel_rankings.{name} ranks must be contiguous")
        normalized[name] = tuple(items)
    return normalized


def _normalize_channel_states(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValueError("channel_states must be a mapping")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_name, raw_state in value.items():
        name = _canonical_channel_name(raw_name)
        if name in normalized:
            raise ValueError(f"channel_states duplicate canonical channel: {name}")
        if not isinstance(raw_state, Mapping):
            raise ValueError(f"channel_states.{name} must be a mapping")
        missing = [field_name for field_name in CHANNEL_STATE_FLAGS if field_name not in raw_state]
        if missing:
            raise ValueError(f"channel_states.{name} missing fields: {missing}")
        if any(type(raw_state[field_name]) is not bool for field_name in CHANNEL_STATE_FLAGS):
            raise ValueError(f"channel_states.{name} flags must be booleans")
        state = dict(raw_state)
        reason = str(state.get("reason") or "").strip()
        if (
            any(state[field_name] is False for field_name in CHANNEL_STATE_FLAGS[:-1])
            and not reason
        ):
            raise ValueError(f"channel_states.{name} requires stable reason")
        state["reason"] = reason
        normalized[name] = state
    return normalized


def _validate_channel_evidence(
    rankings: Mapping[str, tuple[ChannelRankingItem, ...]],
    states: Mapping[str, Mapping[str, Any]],
) -> None:
    missing_states = sorted(set(rankings) - set(states))
    if missing_states:
        raise ValueError(f"channel_states missing ranking channels: {missing_states}")
    for channel, state in states.items():
        if state["evidence_only"] is True and channel in rankings:
            raise ValueError(f"channel_rankings contains evidence-only channel: {channel}")
        if (
            channel in RANK_CHANNELS
            and state["planned"] is True
            and state["enabled"] is True
            and state["available"] is True
            and state["executed"] is True
            and channel not in rankings
        ):
            raise ValueError(f"channel_rankings missing executed channel: {channel}")


def _legacy_channel_states(
    rankings: Mapping[str, tuple[ChannelRankingItem, ...]],
) -> dict[str, dict[str, Any]]:
    return {
        channel: {
            "planned": True,
            "enabled": True,
            "available": True,
            "executed": True,
            "participating": True,
            "evidence_only": False,
            "reason": "legacy_channel_input",
        }
        for channel in rankings
    }


def _ranked_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, Iterable):
        return ()
    ranked: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, Mapping):
            item_id = item.get("id", item.get("memory_id", ""))
        else:
            item_id = getattr(item, "id", item)
        normalized = str(item_id or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            ranked.append(normalized)
    return tuple(ranked)


def _latency_samples(raw: Mapping[str, Any], measured_latency_ms: float) -> tuple[float, ...]:
    values = raw.get("latencies_ms", raw.get("latency_samples_ms"))
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        samples = tuple(max(0.0, float(value)) for value in values)
        if samples:
            return samples
    value = raw.get("latency_ms", measured_latency_ms)
    return (max(0.0, float(value)),)


def _signal(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "none", "no", "native", "active"}
    return bool(value)


def _case_result(case: RecallCase, outcome: _RetrievalOutcome, ks: tuple[int, ...]) -> CaseResult:
    relevant_rank = _first_relevant_rank(outcome.ranked_ids, case.relevant_memory_ids)
    hit_at = {k: relevant_rank is not None and relevant_rank <= k for k in ks}
    forbidden = set(case.forbidden_memory_ids)
    return CaseResult(
        case_id=case.case_id,
        language=case.language,
        group=case.group,
        ranked_ids=outcome.ranked_ids,
        relevant_rank=relevant_rank,
        hit_at=hit_at,
        reciprocal_rank=1.0 / relevant_rank if relevant_rank else 0.0,
        forbidden_hit=any(
            item_id in forbidden for item_id in outcome.ranked_ids[:FORBIDDEN_CUTOFF]
        ),
        latency_ms=_nearest_rank(outcome.latency_samples_ms, 50),
        latency_samples_ms=outcome.latency_samples_ms,
        fallback_used=outcome.fallback_used,
        degraded=outcome.degraded,
        channel_rankings=outcome.channel_rankings,
        channel_states=outcome.channel_states,
        retrieval_metadata=outcome.metadata,
    )


def _with_channel_ranking(
    result: CaseResult,
    channel_name: str,
    case: RecallCase,
    ks: tuple[int, ...],
) -> CaseResult:
    ranked_ids = (
        result.ranked_ids
        if channel_name == "fused"
        else tuple(item.memory_id for item in result.channel_rankings.get(channel_name, ()))
    )
    relevant_rank = _first_relevant_rank(ranked_ids, case.relevant_memory_ids)
    forbidden = set(case.forbidden_memory_ids)
    return CaseResult(
        case_id=result.case_id,
        language=result.language,
        group=result.group,
        ranked_ids=ranked_ids,
        relevant_rank=relevant_rank,
        hit_at={k: relevant_rank is not None and relevant_rank <= k for k in ks},
        reciprocal_rank=1.0 / relevant_rank if relevant_rank else 0.0,
        forbidden_hit=any(item_id in forbidden for item_id in ranked_ids[:FORBIDDEN_CUTOFF]),
        latency_ms=result.latency_ms,
        latency_samples_ms=result.latency_samples_ms,
        fallback_used=result.fallback_used,
        degraded=result.degraded,
        channel_rankings=result.channel_rankings,
        channel_states=result.channel_states,
        retrieval_metadata=result.retrieval_metadata,
    )


def _aggregate_channel_states(
    results: Sequence[CaseResult],
) -> dict[str, dict[str, Any]]:
    if not results:
        return {}
    expected_names = set(results[0].channel_states)
    for result in results[1:]:
        if set(result.channel_states) != expected_names:
            raise ValueError("channel_states key set changed across benchmark cases")
    aggregated: dict[str, dict[str, Any]] = {}
    for name in sorted(expected_names):
        reference = dict(results[0].channel_states[name])
        for result in results[1:]:
            current = dict(result.channel_states[name])
            for field_name in CHANNEL_STATE_FLAGS:
                if current[field_name] != reference[field_name]:
                    raise ValueError(
                        f"channel_states.{name}.{field_name} changed across benchmark cases"
                    )
            if current.get("weight") != reference.get("weight"):
                raise ValueError(f"channel_states.{name}.weight changed across benchmark cases")
        aggregated[name] = reference
    return aggregated


def _first_relevant_rank(ranked_ids: Sequence[str], relevant_ids: Sequence[str]) -> int | None:
    relevant = set(relevant_ids)
    return next(
        (index for index, item_id in enumerate(ranked_ids, start=1) if item_id in relevant), None
    )


def _metric_slice(results: Sequence[CaseResult], ks: tuple[int, ...]) -> MetricSlice:
    count = len(results)
    if count == 0:
        return MetricSlice(
            case_count=0,
            hit_at=dict.fromkeys(ks, 0.0),
            mrr=0.0,
            forbidden_hit_rate=0.0,
            p50_ms=0.0,
            p95_ms=0.0,
            fallback_rate=0.0,
            degradation_rate=0.0,
            fallback_or_degradation_rate=0.0,
        )
    latencies = tuple(latency for result in results for latency in result.latency_samples_ms)
    return MetricSlice(
        case_count=count,
        hit_at={k: sum(bool(result.hit_at[k]) for result in results) / count for k in ks},
        mrr=sum(result.reciprocal_rank for result in results) / count,
        forbidden_hit_rate=sum(result.forbidden_hit for result in results) / count,
        p50_ms=_nearest_rank(latencies, 50),
        p95_ms=_nearest_rank(latencies, 95),
        fallback_rate=sum(result.fallback_used for result in results) / count,
        degradation_rate=sum(result.degraded for result in results) / count,
        fallback_or_degradation_rate=sum(
            result.fallback_used or result.degraded for result in results
        )
        / count,
    )


def _split_metrics(
    results: Sequence[CaseResult], ks: tuple[int, ...], attribute: str
) -> dict[str, MetricSlice]:
    names = sorted({str(getattr(result, attribute)) for result in results})
    return {
        name: _metric_slice(
            [result for result in results if str(getattr(result, attribute)) == name], ks
        )
        for name in names
    }


def _nearest_rank(values: Sequence[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile / 100.0 * len(ordered)) - 1))
    return ordered[index]


def _metric_slice_from_dict(payload: Any, *, label: str) -> MetricSlice:
    data = _required_mapping(payload, label)
    required_fields = (
        "case_count",
        "hit_at",
        "mrr",
        "forbidden_hit_rate",
        "p50_ms",
        "p95_ms",
        "fallback_rate",
        "degradation_rate",
        "fallback_or_degradation_rate",
    )
    missing = [field_name for field_name in required_fields if field_name not in data]
    if missing:
        raise ValueError(f"{label} missing required metric fields: {', '.join(missing)}")

    case_count = data["case_count"]
    if type(case_count) is not int or case_count <= 0:
        raise ValueError(f"{label}.case_count must be a positive integer")
    raw_hit_at = _required_mapping(data["hit_at"], f"{label}.hit_at")
    expected_cutoffs = {str(cutoff) for cutoff in DEFAULT_KS}
    if set(raw_hit_at) != expected_cutoffs:
        raise ValueError(f"{label}.hit_at must contain exactly cutoffs {sorted(DEFAULT_KS)}")
    hit_at = {
        int(cutoff): _metric_number(value, f"{label}.hit_at.{cutoff}", minimum=0.0, maximum=1.0)
        for cutoff, value in raw_hit_at.items()
    }
    if any(
        hit_at[left] > hit_at[right]
        for left, right in zip(DEFAULT_KS, DEFAULT_KS[1:], strict=False)
    ):
        raise ValueError(f"{label}.hit_at must be monotonic across cutoffs")
    metrics = MetricSlice(
        case_count=case_count,
        hit_at=hit_at,
        mrr=_metric_number(data["mrr"], f"{label}.mrr", minimum=0.0, maximum=1.0),
        forbidden_hit_rate=_metric_number(
            data["forbidden_hit_rate"],
            f"{label}.forbidden_hit_rate",
            minimum=0.0,
            maximum=1.0,
        ),
        p50_ms=_metric_number(data["p50_ms"], f"{label}.p50_ms", minimum=0.0),
        p95_ms=_metric_number(data["p95_ms"], f"{label}.p95_ms", minimum=0.0),
        fallback_rate=_metric_number(
            data["fallback_rate"], f"{label}.fallback_rate", minimum=0.0, maximum=1.0
        ),
        degradation_rate=_metric_number(
            data["degradation_rate"],
            f"{label}.degradation_rate",
            minimum=0.0,
            maximum=1.0,
        ),
        fallback_or_degradation_rate=_metric_number(
            data["fallback_or_degradation_rate"],
            f"{label}.fallback_or_degradation_rate",
            minimum=0.0,
            maximum=1.0,
        ),
    )
    if metrics.p50_ms > metrics.p95_ms:
        raise ValueError(f"{label}.p50_ms must not exceed p95_ms")
    if metrics.fallback_or_degradation_rate < max(metrics.fallback_rate, metrics.degradation_rate):
        raise ValueError(
            f"{label}.fallback_or_degradation_rate must cover fallback and degradation"
        )
    union_upper_bound = min(1.0, metrics.fallback_rate + metrics.degradation_rate)
    if metrics.fallback_or_degradation_rate > union_upper_bound + 1e-12:
        raise ValueError(
            f"{label}.fallback_or_degradation_rate exceeds fallback and degradation union"
        )
    return metrics


def _channel_metric_summary_from_dict(
    payload: Any,
    *,
    label: str,
) -> ChannelMetricSummary:
    data = _required_mapping(payload, label)
    if "overall" not in data:
        # Historical v1 reports exposed only an overall channel slice.
        return ChannelMetricSummary(overall=_metric_slice_from_dict(data, label=label))
    language = _required_mapping(data.get("language"), f"{label}.language")
    group = _required_mapping(data.get("group"), f"{label}.group")
    return ChannelMetricSummary(
        overall=_metric_slice_from_dict(data["overall"], label=f"{label}.overall"),
        by_language={
            str(name): _metric_slice_from_dict(value, label=f"{label}.language.{name}")
            for name, value in language.items()
        },
        by_group={
            str(name): _metric_slice_from_dict(value, label=f"{label}.group.{name}")
            for name, value in group.items()
        },
    )


def _metric_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{label} must be a JSON number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and numeric < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    if maximum is not None and numeric > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return numeric


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value
