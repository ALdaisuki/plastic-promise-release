"""Fail-closed calibration and held-out experiment contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from packaging.version import InvalidVersion, Version

from plastic_promise.core.fusion_policy import (
    FUSION_CHANNEL_ORDER,
    FusionConfig,
    _validated_config,
    canonical_fusion_config_hash,
)

if TYPE_CHECKING:
    from plastic_promise.core.recall_quality import RecallDataset

EXPERIMENT_MANIFEST_SCHEMA = "recall-experiment/v1"
CALIBRATION_GRID_SCHEMA = "wrrf-calibration-grid/v1"
RECALL_QUALITY_REPORT_SCHEMA = "recall-quality-report/v2"
SUPPORTED_CANDIDATE_DIMENSIONS = frozenset({"fusion_policy"})
REQUIRED_DEPENDENCIES = frozenset({"lancedb", "pyarrow"})
MINIMUM_DEPENDENCY_VERSIONS = {"lancedb": Version("0.34.0")}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")

# The held-out file stays opaque during calibration. Its normalized text
# fingerprint is the lookup key for the independently frozen report contract
# used later by the comparator, so line-ending changes cannot invalidate a
# legitimate report and two reports cannot validate each other's forged metadata.
_KNOWN_HELDOUT_REPORT_CONTRACTS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "cbf31d0be739d7f4cbb5313be86a8b267e12addc729804414d9a968717a71036": MappingProxyType(
            {
                "fields": MappingProxyType(
                    {
                        "dataset_schema_version": "recall-quality/v1",
                        "dataset_revision": "2026-07-12-heldout.1",
                        "corpus.revision": "2026-07-12-heldout-corpus.1",
                        "corpus.provenance_revision": "2026-07-12-heldout-provenance.1",
                        "corpus.sha256": (
                            "0e84b5a48e974326694448b0ec60b905c56c930a29398398cf2d70a52dc2425c"
                        ),
                        "corpus.count": 15,
                        "cases.sha256": (
                            "a81a2509583b41f06565107ef2d5aee4c4d51c2b14b834967f195f491f53a92b"
                        ),
                        "cases.count": 6,
                    }
                ),
                "case_identities": (
                    ("heldout-en-token-identifier", "en", "token-overlap"),
                    ("heldout-zh-partial-source-change", "zh", "partial-overlap"),
                    ("heldout-cross-zero-contested", "cross-lingual", "zero-overlap"),
                    ("heldout-en-partial-deadline", "en", "partial-overlap"),
                    ("heldout-zh-zero-current-revision", "zh", "zero-overlap"),
                    ("heldout-cross-token-manifest", "cross-lingual", "token-overlap"),
                ),
            }
        )
    }
)


def canonical_json(value: Any) -> str:
    """Return the canonical JSON representation used by every experiment hash."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def opaque_file_fingerprint(path: str | Path) -> str:
    """Hash opaque held-out text with platform-neutral line endings."""

    digest = hashlib.sha256()
    pending_cr = False
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if pending_cr:
                    chunk = b"\r" + chunk
                    pending_cr = False
                if chunk.endswith(b"\r"):
                    chunk = chunk[:-1]
                    pending_cr = True
                digest.update(chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
            if pending_cr:
                digest.update(b"\n")
    except OSError as exc:
        raise ValueError("opaque_fingerprint_unavailable") from exc
    return digest.hexdigest()


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _immutable_fusion_config(value: Mapping[str, Any] | FusionConfig) -> FusionConfig:
    validated = _fusion_config(value)
    return FusionConfig(
        k=validated.k,
        weights=_deep_freeze(validated.weights),
        windows=_deep_freeze(validated.windows),
        channels=validated.channels,
        config_hash=validated.config_hash,
    )


def _config_payload(config: FusionConfig) -> dict[str, Any]:
    return {
        "k": config.k,
        "channels": list(config.channels),
        "weights": {name: config.weights[name] for name in config.channels},
        "windows": {name: config.windows[name] for name in config.channels},
    }


def _fusion_config(value: Mapping[str, Any] | FusionConfig) -> FusionConfig:
    if isinstance(value, FusionConfig):
        return _validated_config(value)
    if not isinstance(value, Mapping):
        raise ValueError("fusion_config_invalid")
    payload = {
        "k": value.get("k"),
        "channels": value.get("channels", list(FUSION_CHANNEL_ORDER)),
        "weights": value.get("weights"),
        "windows": value.get("windows"),
    }
    config_hash = canonical_fusion_config_hash(payload)
    return _validated_config(
        FusionConfig(
            k=payload["k"],
            channels=tuple(payload["channels"]),
            weights=payload["weights"],
            windows=payload["windows"],
            config_hash=str(value.get("config_hash") or config_hash),
        )
    )


@dataclass(frozen=True)
class FrozenCandidateManifest:
    candidate_id: str
    candidate_dimension: str
    calibration_fingerprint: str
    heldout_fingerprint: str
    source_commit: str
    dirty_fingerprint: str
    fusion_config: FusionConfig
    retrieval_configuration: Mapping[str, Any]
    embedding_configuration: Mapping[str, Any]
    dependency_versions: Mapping[str, str]
    runtime_route: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fusion_config", _immutable_fusion_config(self.fusion_config))
        object.__setattr__(
            self,
            "retrieval_configuration",
            _deep_freeze(self.retrieval_configuration),
        )
        object.__setattr__(
            self,
            "embedding_configuration",
            _deep_freeze(self.embedding_configuration),
        )
        object.__setattr__(
            self,
            "dependency_versions",
            _deep_freeze({str(key): str(value) for key, value in self.dependency_versions.items()}),
        )

    def to_dict(self, *, include_manifest_hash: bool = True) -> dict[str, Any]:
        payload = {
            "schema": EXPERIMENT_MANIFEST_SCHEMA,
            "candidate_id": self.candidate_id,
            "candidate_dimension": self.candidate_dimension,
            "calibration_fingerprint": self.calibration_fingerprint,
            "heldout_fingerprint": self.heldout_fingerprint,
            "source_commit": self.source_commit,
            "dirty_fingerprint": self.dirty_fingerprint,
            "fusion_config": _config_payload(self.fusion_config),
            "retrieval_configuration": _deep_thaw(self.retrieval_configuration),
            "embedding_configuration": _deep_thaw(self.embedding_configuration),
            "dependency_versions": _deep_thaw(self.dependency_versions),
            "runtime_route": self.runtime_route,
        }
        if include_manifest_hash:
            payload["manifest_hash"] = _sha256(payload)
        return payload

    @property
    def manifest_hash(self) -> str:
        return str(self.to_dict()["manifest_hash"])

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FrozenCandidateManifest:
        if not isinstance(value, Mapping) or value.get("schema") != EXPERIMENT_MANIFEST_SCHEMA:
            raise ValueError("candidate_manifest_schema_invalid")
        config = _fusion_config(value.get("fusion_config", {}))
        manifest = cls(
            candidate_id=str(value.get("candidate_id") or ""),
            candidate_dimension=str(value.get("candidate_dimension") or ""),
            calibration_fingerprint=str(value.get("calibration_fingerprint") or ""),
            heldout_fingerprint=str(value.get("heldout_fingerprint") or ""),
            source_commit=str(value.get("source_commit") or ""),
            dirty_fingerprint=str(value.get("dirty_fingerprint") or ""),
            fusion_config=config,
            retrieval_configuration=_required_mapping(value, "retrieval_configuration"),
            embedding_configuration=_required_mapping(value, "embedding_configuration"),
            dependency_versions={
                str(key): str(item)
                for key, item in _required_mapping(value, "dependency_versions").items()
            },
            runtime_route=str(value.get("runtime_route") or ""),
        )
        _validate_manifest_shape(manifest)
        supplied_hash = str(value.get("manifest_hash") or "")
        if not _SHA256_RE.fullmatch(supplied_hash) or supplied_hash != manifest.manifest_hash:
            raise ValueError("candidate_manifest_hash_mismatch")
        return manifest


def _required_mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    item = value.get(name)
    if not isinstance(item, Mapping) or not item:
        raise ValueError(f"candidate_manifest_{name}_invalid")
    return dict(item)


def dataset_fingerprint(dataset: RecallDataset) -> str:
    role = str(getattr(dataset, "evidence_role", "") or "")
    return _sha256(
        {
            "schema_version": dataset.schema_version,
            "dataset_revision": dataset.dataset_revision,
            "evidence_role": role,
            "corpus_hash": dataset.corpus_hash,
            "case_hash": dataset.case_hash,
        }
    )


def load_calibration_grid(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("calibration_grid_invalid") from exc
    validate_calibration_grid(value)
    return dict(value)


def validate_calibration_grid(grid: Mapping[str, Any]) -> None:
    required = {
        "schema",
        "k_values",
        "weight_sets",
        "channel_windows",
        "primary_quality_metric",
        "minimum_primary_delta",
        "required_split_tolerance",
        "max_p95_ratio",
        "selection_order",
    }
    if not isinstance(grid, Mapping) or set(grid) != required:
        raise ValueError("calibration_grid_fields_invalid")
    if grid.get("schema") != CALIBRATION_GRID_SCHEMA:
        raise ValueError("calibration_grid_schema_invalid")
    candidates = _grid_configs(grid)
    if not candidates:
        raise ValueError("calibration_grid_empty")


def _grid_configs(grid: Mapping[str, Any]) -> dict[str, FusionConfig]:
    configs: dict[str, FusionConfig] = {}
    for k in grid.get("k_values", ()):
        for weights in grid.get("weight_sets", ()):
            for windows in grid.get("channel_windows", ()):
                config = _fusion_config(
                    {
                        "k": k,
                        "channels": list(FUSION_CHANNEL_ORDER),
                        "weights": weights,
                        "windows": windows,
                    }
                )
                configs[config.config_hash] = config
    return configs


def select_calibration_candidate(
    reports: Sequence[Mapping[str, Any]], grid: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Select one preregistered candidate using the immutable five-key objective."""

    validate_calibration_grid(grid)
    allowed = _grid_configs(grid)
    survivors: list[tuple[tuple[Any, ...], Mapping[str, Any]]] = []
    gate_names = (
        "overall_no_regression",
        "required_splits_no_regression",
        "best_constituent_no_regression",
        "forbidden_hits_not_increased",
        "no_degradation",
        "latency_within_budget",
    )
    metric_names = (
        "minimum_required_split_fused_mrr",
        "overall_fused_hit_at_5",
        "overall_fused_mrr",
        "p95_ms",
    )
    for report in reports:
        try:
            config = _fusion_config(report["fusion_config"])
            gate = report["calibration_gate"]
            metrics = report["selection_metrics"]
        except (KeyError, TypeError, ValueError):
            continue
        if config.config_hash not in allowed or _config_payload(config) != _config_payload(
            allowed[config.config_hash]
        ):
            continue
        if not isinstance(gate, Mapping) or any(gate.get(name) is not True for name in gate_names):
            continue
        if not isinstance(metrics, Mapping) or any(name not in metrics for name in metric_names):
            continue
        try:
            values = tuple(float(metrics[name]) for name in metric_names)
        except (TypeError, ValueError):
            continue
        if any(value != value or value in {float("inf"), float("-inf")} for value in values):
            continue
        key = (
            -values[0],
            -values[1],
            -values[2],
            values[3],
            canonical_json(_config_payload(config)),
        )
        normalized = dict(report)
        normalized["fusion_config"] = _config_payload(config)
        normalized["candidate_id"] = f"wrrf-v1:{config.config_hash}"
        survivors.append((key, normalized))
    if not survivors:
        raise ValueError("no_calibration_candidate")
    survivors.sort(key=lambda item: item[0])
    return survivors[0][1]


def validate_heldout_separation(calibration: RecallDataset, heldout: RecallDataset) -> None:
    if getattr(calibration, "evidence_role", "") != "calibration":
        raise ValueError("calibration_evidence_role_required")
    if getattr(heldout, "evidence_role", "") != "held-out":
        raise ValueError("heldout_evidence_role_required")
    calibration_pairs = {
        (case.query.casefold().strip(), tuple(sorted(case.relevant_memory_ids)))
        for case in calibration.cases
    }
    heldout_pairs = {
        (case.query.casefold().strip(), tuple(sorted(case.relevant_memory_ids)))
        for case in heldout.cases
    }
    if calibration_pairs & heldout_pairs:
        raise ValueError("heldout_case_overlap")
    if calibration.corpus_hash == heldout.corpus_hash or calibration.case_hash == heldout.case_hash:
        raise ValueError("heldout_fingerprint_not_separate")


def _validate_dependencies(versions: Mapping[str, str]) -> None:
    if set(versions) != REQUIRED_DEPENDENCIES:
        raise ValueError("candidate_manifest_dependency_versions_unknown_or_missing")
    for name, version in versions.items():
        if not str(version).strip() or str(version).casefold() in {"unknown", "none", "n/a"}:
            raise ValueError(f"candidate_manifest_dependency_version_invalid:{name}")
        try:
            parsed = Version(str(version))
        except InvalidVersion as exc:
            raise ValueError(f"candidate_manifest_dependency_version_invalid:{name}") from exc
        minimum = MINIMUM_DEPENDENCY_VERSIONS.get(name)
        if minimum is not None and parsed < minimum:
            raise ValueError(f"candidate_manifest_dependency_version_unsupported:{name}")


def _validate_manifest_shape(manifest: FrozenCandidateManifest) -> None:
    if manifest.candidate_dimension not in SUPPORTED_CANDIDATE_DIMENSIONS:
        raise ValueError("candidate_manifest_dimension_unsupported")
    if not _SHA256_RE.fullmatch(manifest.calibration_fingerprint):
        raise ValueError("candidate_manifest_calibration_fingerprint_invalid")
    if not _SHA256_RE.fullmatch(manifest.heldout_fingerprint):
        raise ValueError("candidate_manifest_heldout_fingerprint_invalid")
    if not _COMMIT_RE.fullmatch(manifest.source_commit):
        raise ValueError("candidate_manifest_source_commit_invalid")
    if not _SHA256_RE.fullmatch(manifest.dirty_fingerprint):
        raise ValueError("candidate_manifest_dirty_fingerprint_invalid")
    if not manifest.runtime_route:
        raise ValueError("candidate_manifest_runtime_route_invalid")
    _validate_dependencies(manifest.dependency_versions)
    expected_id = f"wrrf-v1:{manifest.fusion_config.config_hash}"
    if manifest.candidate_id != expected_id:
        raise ValueError("candidate_manifest_candidate_id_mismatch")


def _nested_value(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _validate_calibration_report_binding(
    report: Mapping[str, Any],
    *,
    calibration_fingerprint: str,
    source_commit: str,
    dirty_fingerprint: str,
    retrieval_configuration: Mapping[str, Any],
    embedding_configuration: Mapping[str, Any],
    dependency_versions: Mapping[str, str],
    runtime_route: str,
    candidate_dimension: str,
) -> None:
    expected = {
        "schema_version": RECALL_QUALITY_REPORT_SCHEMA,
        "dataset_role": "calibration",
        "dataset_fingerprint": calibration_fingerprint,
        "candidate_dimension": candidate_dimension,
        "publishable_claim": True,
        "backend.mode": "live",
        "backend.transport": "streamable-http",
        "backend.runtime_route": runtime_route,
        "environment.source_commit": source_commit,
        "environment.dirty_fingerprint": dirty_fingerprint,
        "environment.retrieval_configuration": dict(retrieval_configuration),
        "environment.embedding_configuration": dict(embedding_configuration),
        "environment.dependencies": dict(dependency_versions),
    }
    for path, expected_value in expected.items():
        if _nested_value(report, path) != expected_value:
            raise ValueError(f"calibration_report_binding_mismatch:{path}")
    requested_runtime = _nested_value(report, "backend.requested_runtime")
    effective_runtime = _nested_value(report, "backend.effective_runtime")
    if not requested_runtime or requested_runtime != effective_runtime:
        raise ValueError("calibration_report_binding_mismatch:backend.effective_runtime")


def freeze_candidate_manifest(
    *,
    selected_report: Mapping[str, Any],
    grid: Mapping[str, Any],
    calibration: RecallDataset,
    heldout_fingerprint: str,
    source_commit: str,
    dirty_fingerprint: str,
    retrieval_configuration: Mapping[str, Any],
    embedding_configuration: Mapping[str, Any],
    dependency_versions: Mapping[str, str],
    runtime_route: str,
    candidate_dimension: str = "fusion_policy",
) -> FrozenCandidateManifest:
    if candidate_dimension not in SUPPORTED_CANDIDATE_DIMENSIONS:
        raise ValueError("candidate_manifest_dimension_unsupported")
    if getattr(calibration, "evidence_role", "") != "calibration":
        raise ValueError("calibration_evidence_role_required")
    heldout_fingerprint = str(heldout_fingerprint or "")
    if not _SHA256_RE.fullmatch(heldout_fingerprint):
        raise ValueError("candidate_manifest_heldout_fingerprint_invalid")
    calibration_fingerprint = dataset_fingerprint(calibration)
    _validate_dependencies(dependency_versions)
    _validate_calibration_report_binding(
        selected_report,
        calibration_fingerprint=calibration_fingerprint,
        source_commit=str(source_commit),
        dirty_fingerprint=str(dirty_fingerprint),
        retrieval_configuration=retrieval_configuration,
        embedding_configuration=embedding_configuration,
        dependency_versions=dependency_versions,
        runtime_route=str(runtime_route),
        candidate_dimension=candidate_dimension,
    )
    selected = select_calibration_candidate([selected_report], grid)
    manifest = FrozenCandidateManifest(
        candidate_id=str(selected["candidate_id"]),
        candidate_dimension=candidate_dimension,
        calibration_fingerprint=calibration_fingerprint,
        heldout_fingerprint=heldout_fingerprint,
        source_commit=str(source_commit),
        dirty_fingerprint=str(dirty_fingerprint),
        fusion_config=_immutable_fusion_config(selected["fusion_config"]),
        retrieval_configuration=dict(retrieval_configuration),
        embedding_configuration=dict(embedding_configuration),
        dependency_versions={str(key): str(value) for key, value in dependency_versions.items()},
        runtime_route=str(runtime_route),
    )
    _validate_manifest_shape(manifest)
    return manifest


def write_frozen_manifest(path: str | Path, manifest: FrozenCandidateManifest) -> None:
    target = Path(path)
    _validate_manifest_shape(manifest)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = target.open("x", encoding="utf-8", newline="\n")
    except FileExistsError as exc:
        raise ValueError("candidate_manifest_already_exists") from exc
    try:
        with handle:
            handle.write(canonical_json(manifest.to_dict()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def load_frozen_manifest(path: str | Path) -> FrozenCandidateManifest:
    target = Path(path)
    if not target.is_file():
        raise ValueError("candidate_manifest_required_before_heldout")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("candidate_manifest_invalid") from exc
    return FrozenCandidateManifest.from_dict(value)


def validate_manifest_runtime(
    manifest: FrozenCandidateManifest,
    *,
    calibration: RecallDataset,
    heldout_fingerprint: str,
    source_commit: str,
    dirty_fingerprint: str,
    retrieval_configuration: Mapping[str, Any],
    embedding_configuration: Mapping[str, Any],
    dependency_versions: Mapping[str, str],
    runtime_route: str,
) -> None:
    checks = {
        "calibration_fingerprint": dataset_fingerprint(calibration),
        "heldout_fingerprint": str(heldout_fingerprint),
        "source_commit": source_commit,
        "dirty_fingerprint": dirty_fingerprint,
        "retrieval_configuration": dict(retrieval_configuration),
        "embedding_configuration": dict(embedding_configuration),
        "dependency_versions": dict(dependency_versions),
        "runtime_route": runtime_route,
    }
    _validate_dependencies(checks["dependency_versions"])
    for name, current in checks.items():
        if getattr(manifest, name) != current:
            raise ValueError(f"candidate_manifest_runtime_mismatch:{name}")


def load_heldout_result(
    path: str | Path,
    *,
    manifest_path: str | Path | None,
    expected_manifest_hash: str,
) -> Mapping[str, Any]:
    if manifest_path is None:
        raise ValueError("candidate_manifest_required_before_heldout")
    manifest = load_frozen_manifest(manifest_path)
    if manifest.manifest_hash != expected_manifest_hash:
        raise ValueError("candidate_manifest_hash_mismatch")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("heldout_result_invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError("heldout_result_invalid")
    return value


def calibrate_and_freeze(
    *,
    reports: Sequence[Mapping[str, Any]],
    grid: Mapping[str, Any],
    calibration: RecallDataset,
    heldout_fingerprint: str,
    manifest_path: str | Path,
    calibration_retrieve: Callable[[Any], Any] | None = None,
    **manifest_fields: Any,
) -> FrozenCandidateManifest:
    """Freeze a calibration winner without ever invoking held-out retrieval."""

    if calibration_retrieve is not None:
        for case in calibration.cases:
            calibration_retrieve(case)
    selected = select_calibration_candidate(reports, grid)
    manifest = freeze_candidate_manifest(
        selected_report=selected,
        grid=grid,
        calibration=calibration,
        heldout_fingerprint=heldout_fingerprint,
        **manifest_fields,
    )
    write_frozen_manifest(manifest_path, manifest)
    return manifest


def _path(value: Mapping[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _metric_slice(
    report: Mapping[str, Any], channel: str, kind: str, name: str
) -> Mapping[str, Any] | None:
    base = _path(report, f"metrics.channels.{channel}")
    if not isinstance(base, Mapping):
        return None
    if kind == "overall":
        value = base.get("overall")
    else:
        collection = base.get("by_language" if kind == "language" else "by_group")
        value = collection.get(name) if isinstance(collection, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _hit5(metric_slice: Mapping[str, Any] | None) -> float | None:
    if not isinstance(metric_slice, Mapping):
        return None
    hit_at = metric_slice.get("hit_at")
    if not isinstance(hit_at, Mapping):
        return None
    return _finite_number(hit_at.get("5", hit_at.get(5)))


def compare_fusion_reports(
    baseline_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    *,
    manifest: FrozenCandidateManifest,
    tolerances: Mapping[str, float],
) -> dict[str, Any]:
    """Compare one manifest-bound WRRF report against max-v1, failing closed."""

    _validate_manifest_shape(manifest)
    checks: list[dict[str, Any]] = []

    def add(name: str, expected: Any, actual: Any, passed: bool) -> None:
        checks.append(
            {
                "name": name,
                "expected": expected,
                "actual": actual,
                "passed": bool(passed),
            }
        )

    heldout_contract = _KNOWN_HELDOUT_REPORT_CONTRACTS.get(manifest.heldout_fingerprint)
    add(
        "manifest.heldout_report_contract",
        "registered opaque held-out fingerprint",
        manifest.heldout_fingerprint,
        heldout_contract is not None,
    )

    for label, report in (("baseline", baseline_report), ("candidate", candidate_report)):
        add(
            f"{label}.schema_version",
            RECALL_QUALITY_REPORT_SCHEMA,
            report.get("schema_version"),
            report.get("schema_version") == RECALL_QUALITY_REPORT_SCHEMA,
        )
        add(
            f"{label}.publishable_claim",
            True,
            report.get("publishable_claim"),
            report.get("publishable_claim") is True,
        )
        add(
            f"{label}.dataset_role",
            "held-out",
            report.get("dataset_role"),
            report.get("dataset_role") == "held-out",
        )
        add(
            f"{label}.dataset_fingerprint",
            manifest.heldout_fingerprint,
            report.get("dataset_fingerprint"),
            report.get("dataset_fingerprint") == manifest.heldout_fingerprint,
        )
        if heldout_contract is not None:
            contract_fields = heldout_contract["fields"]
            for path, expected in contract_fields.items():
                actual = _path(report, path)
                add(
                    f"{label}.evidence.heldout_contract.{path}",
                    expected,
                    actual,
                    actual == expected,
                )
            report_cases = _path(report, "metrics.cases")
            actual_case_identities = (
                [
                    (
                        case.get("case_id"),
                        case.get("language"),
                        case.get("group"),
                    )
                    for case in report_cases
                ]
                if isinstance(report_cases, list)
                and all(isinstance(case, Mapping) for case in report_cases)
                else None
            )
            expected_case_identities = list(heldout_contract["case_identities"])
            add(
                f"{label}.evidence.heldout_contract.metrics.case_identities",
                expected_case_identities,
                actual_case_identities,
                actual_case_identities == expected_case_identities,
            )
        add(
            f"{label}.candidate_dimension",
            manifest.candidate_dimension,
            report.get("candidate_dimension"),
            report.get("candidate_dimension") == manifest.candidate_dimension,
        )
        expected_bindings = {
            "environment.source_commit": manifest.source_commit,
            "environment.dirty_fingerprint": manifest.dirty_fingerprint,
            "environment.retrieval_configuration": dict(manifest.retrieval_configuration),
            "environment.embedding_configuration": dict(manifest.embedding_configuration),
            "environment.dependencies": dict(manifest.dependency_versions),
            "backend.runtime_route": manifest.runtime_route,
        }
        for path, expected in expected_bindings.items():
            actual = _path(report, path)
            add(f"{label}.{path}", expected, actual, actual == expected)
        requested_runtime = _path(report, "backend.requested_runtime")
        effective_runtime = _path(report, "backend.effective_runtime")
        add(
            f"{label}.requested_effective_runtime",
            requested_runtime,
            effective_runtime,
            bool(requested_runtime) and requested_runtime == effective_runtime,
        )
        public_counts = _path(report, "backend.public_call_counts")
        case_count = _path(report, "cases.count")
        recall_count = (
            public_counts.get("memory_recall") if isinstance(public_counts, Mapping) else None
        )
        supply_count = (
            public_counts.get("context_supply") if isinstance(public_counts, Mapping) else None
        )
        add(
            f"{label}.public_calls_per_case",
            case_count,
            {"memory_recall": recall_count, "context_supply": supply_count},
            isinstance(case_count, int)
            and case_count > 0
            and recall_count == case_count
            and supply_count == case_count,
        )
        add(
            f"{label}.server_pid",
            "positive integer",
            _path(report, "backend.server_pid"),
            isinstance(_path(report, "backend.server_pid"), int)
            and not isinstance(_path(report, "backend.server_pid"), bool)
            and _path(report, "backend.server_pid") > 0,
        )
        expected_policy = "max-v1" if label == "baseline" else manifest.candidate_id
        index_policy = manifest.retrieval_configuration.get("index_text_policy")
        add(
            f"{label}.evidence.backend_mode",
            "live",
            _path(report, "backend.mode"),
            _path(report, "backend.mode") == "live",
        )
        add(
            f"{label}.evidence.transport",
            "streamable-http",
            _path(report, "backend.transport"),
            _path(report, "backend.transport") == "streamable-http",
        )
        add(
            f"{label}.evidence.index_text_policy",
            index_policy,
            _path(report, "backend.index_text_policy"),
            bool(index_policy) and _path(report, "backend.index_text_policy") == index_policy,
        )
        corpus = report.get("corpus")
        cases = report.get("cases")
        corpus_count = corpus.get("count") if isinstance(corpus, Mapping) else None
        add(
            f"{label}.evidence.corpus",
            "versioned non-empty corpus",
            corpus,
            isinstance(corpus, Mapping)
            and isinstance(corpus_count, int)
            and not isinstance(corpus_count, bool)
            and corpus_count > 0
            and bool(corpus.get("sha256")),
        )
        add(
            f"{label}.evidence.cases",
            "versioned non-empty cases",
            cases,
            isinstance(cases, Mapping)
            and isinstance(case_count, int)
            and not isinstance(case_count, bool)
            and case_count > 0
            and bool(cases.get("sha256")),
        )
        isolated = report.get("isolated_corpus")
        isolated_valid = bool(
            isinstance(isolated, Mapping)
            and isolated.get("seeded") is True
            and isolated.get("canonical_count") == corpus_count
            and isinstance(isolated.get("eligible_count"), int)
            and isolated.get("eligible_count", 0) > 0
            and isolated.get("derived_count") == isolated.get("eligible_count")
        )
        add(
            f"{label}.evidence.isolated_corpus",
            "complete isolated corpus",
            isolated,
            isolated_valid,
        )
        smoke = report.get("smoke")
        smoke_fields = (
            "store",
            "recall",
            "supply",
            "verified_visible",
            "forbidden_hidden",
            "passed",
        )
        add(
            f"{label}.evidence.smoke",
            dict.fromkeys(smoke_fields, True),
            smoke,
            isinstance(smoke, Mapping) and all(smoke.get(name) is True for name in smoke_fields),
        )
        transport_counts = report.get("public_transport_call_counts")
        execution = report.get("execution")
        expected_transport_count = (
            case_count * (execution.get("warmup") + execution.get("repeat"))
            if isinstance(case_count, int)
            and not isinstance(case_count, bool)
            and isinstance(execution, Mapping)
            and type(execution.get("warmup")) is int
            and type(execution.get("repeat")) is int
            else None
        )
        transport_valid = bool(
            expected_transport_count is not None
            and isinstance(transport_counts, Mapping)
            and set(transport_counts) == {"memory_recall", "context_supply"}
            and transport_counts.get("memory_recall") == expected_transport_count
            and transport_counts.get("context_supply") == expected_transport_count
        )
        add(
            f"{label}.evidence.transport_counts",
            expected_transport_count,
            transport_counts,
            transport_valid,
        )
        attestation = report.get("fusion_attestation")
        expected_observed = [expected_policy, requested_runtime, effective_runtime]
        attestation_valid = bool(
            isinstance(attestation, Mapping)
            and attestation.get("errors") == []
            and attestation.get("observed") == expected_observed
            and isinstance(attestation.get("attested_calls"), int)
            and not isinstance(attestation.get("attested_calls"), bool)
            and isinstance(transport_counts, Mapping)
            and attestation.get("attested_calls")
            == sum(
                value
                for value in transport_counts.values()
                if isinstance(value, int) and not isinstance(value, bool)
            )
        )
        add(
            f"{label}.evidence.fusion_attestation",
            {"errors": [], "observed": expected_observed},
            attestation,
            attestation_valid,
        )
        metric_cases = _path(report, "metrics.cases")
        aggregate_states = _path(report, "metrics.channel_states")

        def complete_case_channels(case: Any) -> bool:
            if not isinstance(case, Mapping):
                return False
            rankings = case.get("channel_rankings")
            states = case.get("channel_states")
            if (
                not isinstance(rankings, Mapping)
                or not isinstance(states, Mapping)
                or set(rankings) != set(FUSION_CHANNEL_ORDER)
                or set(states) != set(FUSION_CHANNEL_ORDER)
            ):
                return False
            for channel in FUSION_CHANNEL_ORDER:
                state = states[channel]
                rows = rankings[channel]
                if not isinstance(state, Mapping) or not isinstance(rows, list):
                    return False
                if any(
                    type(state.get(field)) is not bool
                    for field in ("planned", "enabled", "available", "executed")
                ):
                    return False
                if state.get("planned") is not True or not (
                    state.get("enabled") and state.get("available") and state.get("executed")
                ):
                    return False
                ids: set[str] = set()
                for rank, row in enumerate(rows, start=1):
                    if not isinstance(row, Mapping):
                        return False
                    memory_id = str(row.get("id", row.get("memory_id", "")) or "")
                    if (
                        not memory_id
                        or memory_id in ids
                        or row.get("rank") != rank
                        or _finite_number(row.get("score")) is None
                    ):
                        return False
                    ids.add(memory_id)
            return True

        case_evidence_valid = bool(
            isinstance(metric_cases, list)
            and len(metric_cases) == case_count
            and isinstance(aggregate_states, Mapping)
            and set(aggregate_states) == set(FUSION_CHANNEL_ORDER)
            and all(complete_case_channels(case) for case in metric_cases)
        )
        add(
            f"{label}.evidence.case_channels",
            f"{case_count} cases with complete channel evidence",
            metric_cases,
            case_evidence_valid,
        )

    shared_bindings = (
        "dataset_schema_version",
        "dataset_revision",
        "corpus",
        "cases",
        "backend.index_text_policy",
        "isolated_corpus",
        "smoke",
        "public_transport_call_counts",
    )
    for path in shared_bindings:
        baseline_value = _path(baseline_report, path)
        candidate_value = _path(candidate_report, path)
        add(
            f"shared.{path}",
            baseline_value,
            candidate_value,
            baseline_value == candidate_value,
        )
    baseline_cases = _path(baseline_report, "metrics.cases")
    candidate_cases = _path(candidate_report, "metrics.cases")
    baseline_case_ids = (
        [case.get("case_id") for case in baseline_cases if isinstance(case, Mapping)]
        if isinstance(baseline_cases, list)
        else None
    )
    candidate_case_ids = (
        [case.get("case_id") for case in candidate_cases if isinstance(case, Mapping)]
        if isinstance(candidate_cases, list)
        else None
    )
    add(
        "shared.case_ids",
        baseline_case_ids,
        candidate_case_ids,
        baseline_case_ids is not None and baseline_case_ids == candidate_case_ids,
    )

    baseline_policy = _path(baseline_report, "backend.requested_policy")
    add("baseline.requested_policy", "max-v1", baseline_policy, baseline_policy == "max-v1")
    add(
        "baseline.effective_policy",
        "max-v1",
        _path(baseline_report, "backend.effective_policy"),
        _path(baseline_report, "backend.effective_policy") == "max-v1",
    )
    add(
        "baseline.candidate_id",
        "max-v1",
        baseline_report.get("candidate_id"),
        baseline_report.get("candidate_id") == "max-v1",
    )
    add(
        "baseline.manifest_hash",
        "",
        baseline_report.get("manifest_hash"),
        baseline_report.get("manifest_hash") == "",
    )
    add(
        "baseline.fusion_config",
        None,
        baseline_report.get("fusion_config"),
        baseline_report.get("fusion_config") is None,
    )
    candidate_policy = _path(candidate_report, "backend.requested_policy")
    add(
        "candidate.requested_policy",
        manifest.candidate_id,
        candidate_policy,
        candidate_policy == manifest.candidate_id,
    )
    add(
        "candidate.effective_policy",
        manifest.candidate_id,
        _path(candidate_report, "backend.effective_policy"),
        _path(candidate_report, "backend.effective_policy") == manifest.candidate_id,
    )
    add(
        "candidate.candidate_id",
        manifest.candidate_id,
        candidate_report.get("candidate_id"),
        candidate_report.get("candidate_id") == manifest.candidate_id,
    )
    add(
        "candidate.manifest_hash",
        manifest.manifest_hash,
        candidate_report.get("manifest_hash"),
        candidate_report.get("manifest_hash") == manifest.manifest_hash,
    )
    add(
        "candidate.fusion_config",
        _config_payload(manifest.fusion_config),
        candidate_report.get("fusion_config"),
        candidate_report.get("fusion_config") == _config_payload(manifest.fusion_config),
    )
    baseline_execution = baseline_report.get("execution")
    candidate_execution = candidate_report.get("execution")
    execution_valid = bool(
        isinstance(baseline_execution, Mapping)
        and type(baseline_execution.get("warmup")) is int
        and baseline_execution["warmup"] >= 0
        and type(baseline_execution.get("repeat")) is int
        and baseline_execution["repeat"] >= 1
    )
    add(
        "baseline.execution_valid",
        "non-negative warmup and positive repeat",
        baseline_execution,
        execution_valid,
    )
    add(
        "candidate.execution_matches_baseline",
        baseline_execution,
        candidate_execution,
        execution_valid and candidate_execution == baseline_execution,
    )

    quality_tolerance = float(
        tolerances.get("required_split_tolerance", tolerances.get("default", 0.0))
    )
    if quality_tolerance < 0:
        raise ValueError("comparison_tolerance_must_be_non_negative")
    minimum_delta = float(tolerances.get("minimum_primary_delta", 0.01))
    max_p95_ratio = float(tolerances.get("max_p95_ratio", 1.20))
    split_names: list[tuple[str, str]] = [("overall", "overall")]
    for kind, field in (("language", "by_language"), ("group", "by_group")):
        candidate_fused = _path(candidate_report, f"metrics.channels.fused.{field}")
        baseline_fused = _path(baseline_report, f"metrics.channels.fused.{field}")
        names = set(candidate_fused) if isinstance(candidate_fused, Mapping) else set()
        names &= set(baseline_fused) if isinstance(baseline_fused, Mapping) else set()
        split_names.extend((kind, str(name)) for name in sorted(names))
    required = {
        ("language", "en"),
        ("language", "zh"),
        ("language", "cross-lingual"),
        ("group", "token-overlap"),
        ("group", "partial-overlap"),
        ("group", "zero-overlap"),
    }
    for item in required:
        add(
            f"required_split_present:{item[0]}:{item[1]}",
            True,
            item in split_names,
            item in split_names,
        )

    for kind, name in split_names:
        baseline_slice = _metric_slice(baseline_report, "fused", kind, name)
        candidate_slice = _metric_slice(candidate_report, "fused", kind, name)
        baseline_mrr = _finite_number(baseline_slice.get("mrr")) if baseline_slice else None
        candidate_mrr = _finite_number(candidate_slice.get("mrr")) if candidate_slice else None
        baseline_hit5 = _hit5(baseline_slice)
        candidate_hit5 = _hit5(candidate_slice)
        prefix = f"{kind}:{name}"
        add(
            f"{prefix}.fused_mrr_vs_baseline",
            baseline_mrr,
            candidate_mrr,
            baseline_mrr is not None
            and candidate_mrr is not None
            and candidate_mrr + quality_tolerance >= baseline_mrr,
        )
        add(
            f"{prefix}.fused_hit5_vs_baseline",
            baseline_hit5,
            candidate_hit5,
            baseline_hit5 is not None
            and candidate_hit5 is not None
            and candidate_hit5 + quality_tolerance >= baseline_hit5,
        )
        constituent_mrr: list[float] = []
        constituent_hit5: list[float] = []
        for channel in FUSION_CHANNEL_ORDER:
            state = _path(candidate_report, f"metrics.channel_states.{channel}")
            if (
                isinstance(state, Mapping)
                and state.get("planned")
                and not (state.get("enabled") and state.get("available") and state.get("executed"))
            ):
                add(f"{prefix}.{channel}.planned_channel_ready", True, state, False)
                continue
            channel_slice = _metric_slice(candidate_report, channel, kind, name)
            mrr = _finite_number(channel_slice.get("mrr")) if channel_slice else None
            hit5 = _hit5(channel_slice)
            if mrr is not None:
                constituent_mrr.append(mrr)
            if hit5 is not None:
                constituent_hit5.append(hit5)
        best_mrr = max(constituent_mrr) if constituent_mrr else None
        best_hit5 = max(constituent_hit5) if constituent_hit5 else None
        add(
            f"{prefix}.fused_mrr_vs_best_constituent",
            best_mrr,
            candidate_mrr,
            best_mrr is not None
            and candidate_mrr is not None
            and candidate_mrr + quality_tolerance >= best_mrr,
        )
        add(
            f"{prefix}.fused_hit5_vs_best_constituent",
            best_hit5,
            candidate_hit5,
            best_hit5 is not None
            and candidate_hit5 is not None
            and candidate_hit5 + quality_tolerance >= best_hit5,
        )

    base_overall = _metric_slice(baseline_report, "fused", "overall", "overall")
    candidate_overall = _metric_slice(candidate_report, "fused", "overall", "overall")
    base_mrr = _finite_number(base_overall.get("mrr")) if base_overall else None
    candidate_mrr = _finite_number(candidate_overall.get("mrr")) if candidate_overall else None
    add(
        "primary_metric_improvement",
        minimum_delta,
        None if base_mrr is None or candidate_mrr is None else candidate_mrr - base_mrr,
        base_mrr is not None
        and candidate_mrr is not None
        and candidate_mrr - base_mrr >= minimum_delta,
    )
    base_forbidden = _finite_number(_path(baseline_report, "metrics.forbidden_hit_rate"))
    candidate_forbidden = _finite_number(_path(candidate_report, "metrics.forbidden_hit_rate"))
    add(
        "forbidden_hit_nonincrease",
        base_forbidden,
        candidate_forbidden,
        base_forbidden is not None
        and candidate_forbidden is not None
        and candidate_forbidden <= base_forbidden,
    )
    for label, report in (("baseline", baseline_report), ("candidate", candidate_report)):
        fallback = _finite_number(_path(report, "metrics.fallback_rate"))
        degradation = _finite_number(_path(report, "metrics.degradation_rate"))
        add(f"{label}.no_fallback", 0.0, fallback, fallback == 0.0)
        add(f"{label}.no_degradation", 0.0, degradation, degradation == 0.0)
    base_p95 = _finite_number(_path(baseline_report, "metrics.p95_ms"))
    candidate_p95 = _finite_number(_path(candidate_report, "metrics.p95_ms"))
    ratio = None if base_p95 in {None, 0.0} or candidate_p95 is None else candidate_p95 / base_p95
    add(
        "p95_latency_ratio",
        max_p95_ratio,
        ratio,
        ratio is not None and ratio <= max_p95_ratio,
    )

    comparability_prefixes = (
        "manifest.heldout_report_contract",
        "baseline.schema_version",
        "candidate.schema_version",
        "baseline.publishable_claim",
        "candidate.publishable_claim",
        "baseline.dataset_",
        "candidate.dataset_",
        "baseline.candidate_dimension",
        "candidate.candidate_dimension",
        "baseline.environment.",
        "candidate.environment.",
        "baseline.backend.runtime_route",
        "candidate.backend.runtime_route",
        "baseline.requested_effective_runtime",
        "candidate.requested_effective_runtime",
        "baseline.public_calls_per_case",
        "candidate.public_calls_per_case",
        "baseline.server_pid",
        "candidate.server_pid",
        "baseline.requested_policy",
        "baseline.effective_policy",
        "baseline.candidate_id",
        "baseline.manifest_hash",
        "baseline.fusion_config",
        "candidate.requested_policy",
        "candidate.effective_policy",
        "candidate.candidate_id",
        "candidate.manifest_hash",
        "candidate.fusion_config",
        "baseline.execution_valid",
        "candidate.execution_matches_baseline",
        "required_split_present:",
        "baseline.evidence.",
        "candidate.evidence.",
        "shared.",
    )
    comparability_checks = [
        check for check in checks if check["name"].startswith(comparability_prefixes)
    ]
    return {
        "passed": all(check["passed"] for check in checks),
        "comparability": {
            "passed": all(check["passed"] for check in comparability_checks),
            "checks": comparability_checks,
        },
        "checks": checks,
        "failed_checks": [check for check in checks if not check["passed"]],
    }
