from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

import plastic_promise.core.lancedb_generation as generation_module
import scripts.manage_lancedb_generations as generation_cli_module
from plastic_promise.core.lancedb_generation import (
    QUALITY_GATE_POLICY,
    QUALITY_REPORT_SCHEMA,
    RECALL_QUALITY_DATASET_SCHEMA,
    RECALL_QUALITY_REPORT_SCHEMA,
    ArtifactVerification,
    ArtifactVerificationRequest,
    BuildResult,
    GenerationError,
    GenerationManager,
    GenerationSpec,
    ManifestError,
    PromotionError,
    index_material_sha256,
)
from scripts.manage_lancedb_generations import main as generation_cli

if TYPE_CHECKING:
    from pathlib import Path

SOURCE_SHA = "a" * 64
BENCHMARK_CORPUS_SHA = "1" * 64
BENCHMARK_CORPUS_COUNT = 27
BENCHMARK_CASES_SHA = "b" * 64
BENCHMARK_CASE_COUNT = 9
_DELETE = object()
_EMPTY_OUTBOX_DIGEST = hashlib.sha256(b"").hexdigest()
INDEX_TEXT_POLICY = "compact-v2"


def _artifact_rows(row_count: int) -> dict[str, dict[str, str]]:
    return {
        f"row-{index}": {
            "text": f"text:{index}",
            "tier": "L1",
            "category": "other",
            "scope": "global",
        }
        for index in range(row_count)
    }


def _metric_slice(case_count: int) -> dict[str, object]:
    return {
        "case_count": case_count,
        "hit_at": {"1": 0.75, "5": 1.0},
        "mrr": 0.875,
        "forbidden_hit_rate": 0.0,
        "p95_ms": 25.0,
        "fallback_rate": 0.0,
        "degradation_rate": 0.0,
        "fallback_or_degradation_rate": 0.0,
    }


def test_v2_switch_requires_and_invokes_current_runtime_validator(tmp_path):
    report = {"benchmark": {"candidate_dimension": "fusion_policy"}}
    spec = _spec("candidate")
    with (
        GenerationManager(tmp_path / "missing", artifact_verifier=_artifact_verifier) as manager,
        pytest.raises(
            PromotionError,
            match="promotion_runtime_environment_validator_required",
        ),
    ):
        manager._validate_current_runtime_environment(
            spec,
            report,
            operation="promotion",
        )

    observed: list[tuple[GenerationSpec, object]] = []

    def validate(current_spec, current_report):
        observed.append((current_spec, current_report))

    with GenerationManager(
        tmp_path / "validated",
        artifact_verifier=_artifact_verifier,
        runtime_environment_validator=validate,
    ) as manager:
        manager._validate_current_runtime_environment(
            spec,
            report,
            operation="promotion",
        )

    assert observed == [(spec, report)]


def _quality_report(
    *,
    model: str = "text-embedding-v4",
    revision: str = "2026-07-23",
    dimension: int = 1024,
    **overrides,
) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": QUALITY_REPORT_SCHEMA,
        "benchmark": {
            "report_schema": RECALL_QUALITY_REPORT_SCHEMA,
            "dataset_schema": RECALL_QUALITY_DATASET_SCHEMA,
            "dataset_revision": "2026-07-10.2",
            "candidate": "compact-v2",
        },
        "gate": {
            "status": "pass",
            "policy": QUALITY_GATE_POLICY,
            "thresholds": {
                "min_hit_at_1": 0.01,
                "min_hit_at_5": 0.05,
                "min_mrr": 0.01,
                "max_p95_ms": 5000.0,
                "max_forbidden_hit_rate": 0.0,
                "max_fallback_or_degradation_rate": 0.0,
            },
        },
        "degraded": False,
        "publishable_claim": True,
        "backend": {
            "mode": "live",
            "fallback_used": False,
            "degraded_used": False,
            "model": model,
            "revision": revision,
            "dimension": dimension,
        },
        "cases": {"sha256": BENCHMARK_CASES_SHA, "count": BENCHMARK_CASE_COUNT},
        "corpus": {
            "sha256": BENCHMARK_CORPUS_SHA,
            "count": BENCHMARK_CORPUS_COUNT,
            "revision": "fixed-corpus-v1",
            "provenance_revision": "fixed-provenance-v1",
        },
        "environment": {
            "source_commit": "c" * 40,
            "source_fingerprint": "d" * 64,
            "configuration_sha256": "e" * 64,
            "dependencies_sha256": "f" * 64,
        },
        "smoke": {
            "store": True,
            "recall": True,
            "context": True,
            "verified_visible": True,
            "forbidden_hidden": True,
            "passed": True,
        },
        "usage": {
            "embedding_requests": 42,
            "embedding_input_tokens": 4096,
            "cost_usd": 0.25,
            "pricing_revision": "cloud-embedding-pricing/v1",
        },
        "metrics": {
            **_metric_slice(BENCHMARK_CASE_COUNT),
            "language": {
                "en": _metric_slice(3),
                "zh": _metric_slice(3),
                "cross-lingual": _metric_slice(3),
            },
        },
    }
    report.update(overrides)
    return report


def _mutated_quality_report(path: str, value: object, **report_options) -> dict[str, object]:
    report = _quality_report(**report_options)
    target: dict[str, object] = report
    parts = path.split(".")
    for part in parts[:-1]:
        child = target[part]
        assert isinstance(child, dict)
        target = child
    if value is _DELETE:
        target.pop(parts[-1])
    else:
        target[parts[-1]] = value
    return report


def _spec(
    generation_id: str,
    *,
    model: str = "text-embedding-v4",
    revision: str = "2026-07-23",
    dimension: int = 1024,
    source_count: int = 3,
    benchmark_corpus_sha256: str = BENCHMARK_CORPUS_SHA,
    benchmark_corpus_count: int = BENCHMARK_CORPUS_COUNT,
    benchmark_cases_sha256: str = BENCHMARK_CASES_SHA,
    benchmark_case_count: int = BENCHMARK_CASE_COUNT,
    index_outbox_watermark: int | None = None,
    index_outbox_digest: str | None = None,
    index_outbox_job_count: int | None = None,
    index_outbox_source_fingerprint: str | None = None,
    embedding_index_identity: str | None = None,
    index_text_policy: str | None = INDEX_TEXT_POLICY,
    index_material_digest: str | None = None,
) -> GenerationSpec:
    if index_text_policy is not None and index_material_digest is None:
        index_material_digest = index_material_sha256(
            index_text_policy,
            _artifact_rows(source_count),
        )
    return GenerationSpec(
        generation_id=generation_id,
        index_schema="memory-vectors/v1",
        embedding_model=model,
        model_revision=revision,
        embedding_dimension=dimension,
        source_db_sha256=SOURCE_SHA,
        source_row_count=source_count,
        benchmark_corpus_sha256=benchmark_corpus_sha256,
        benchmark_corpus_count=benchmark_corpus_count,
        benchmark_cases_sha256=benchmark_cases_sha256,
        benchmark_case_count=benchmark_case_count,
        index_outbox_watermark=index_outbox_watermark,
        index_outbox_digest=index_outbox_digest,
        index_outbox_job_count=index_outbox_job_count,
        index_outbox_source_fingerprint=index_outbox_source_fingerprint,
        embedding_index_identity=embedding_index_identity,
        index_text_policy=index_text_policy,
        index_material_sha256=index_material_digest,
    )


def _write_artifact(index_path: Path, spec: GenerationSpec, row_count: int) -> None:
    artifact = {
        "row_count": row_count,
        "index_schema": spec.index_schema,
        "embedding_model": spec.embedding_model,
        "model_revision": spec.model_revision,
        "embedding_dimension": spec.embedding_dimension,
        "rows": [
            {"memory_id": memory_id, **row} for memory_id, row in _artifact_rows(row_count).items()
        ],
    }
    (index_path / "artifact.json").write_text(
        json.dumps(artifact, sort_keys=True),
        encoding="utf-8",
    )


def _read_artifact(request: ArtifactVerificationRequest) -> dict[str, object]:
    descriptor = os.open(
        "artifact.json",
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=request.index_fd,
    )
    try:
        chunks = []
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _artifact_verifier(request: ArtifactVerificationRequest) -> ArtifactVerification:
    payload = _read_artifact(request)
    rows = payload["rows"]
    assert isinstance(rows, list)
    assert payload["row_count"] == len(rows)
    material_rows = {
        row["memory_id"]: {
            "text": row["text"],
            "tier": row["tier"],
            "category": row["category"],
            "scope": row["scope"],
        }
        for row in rows
    }
    assert request.index_text_policy is not None
    return ArtifactVerification(
        row_count=len(rows),
        index_schema=payload["index_schema"],
        embedding_model=payload["embedding_model"],
        model_revision=payload["model_revision"],
        embedding_dimension=payload["embedding_dimension"],
        index_material_sha256=index_material_sha256(
            request.index_text_policy,
            material_rows,
        ),
    )


def _manager(root: Path) -> GenerationManager:
    return GenerationManager(root, artifact_verifier=_artifact_verifier)


def _build_complete(
    manager: GenerationManager,
    generation_id: str,
    *,
    model: str = "text-embedding-v4",
    revision: str = "2026-07-23",
    dimension: int = 1024,
    source_count: int = 3,
    built_count: int | None = None,
    quality_report: dict[str, object] | None = None,
    index_outbox_watermark: int | None = None,
    index_outbox_digest: str | None = None,
    index_outbox_job_count: int | None = None,
    embedding_index_identity: str | None = None,
    reconcile: bool = False,
):
    if reconcile:
        if any(
            value is not None
            for value in (index_outbox_watermark, index_outbox_digest, index_outbox_job_count)
        ):
            raise ValueError("reconcile_helper_only_supports_zero_job_evidence")
        index_outbox_watermark = 0
        index_outbox_digest = _EMPTY_OUTBOX_DIGEST
        index_outbox_job_count = 0
    observed = []
    spec = _spec(
        generation_id,
        model=model,
        revision=revision,
        dimension=dimension,
        source_count=source_count,
        index_outbox_watermark=index_outbox_watermark,
        index_outbox_digest=index_outbox_digest,
        index_outbox_job_count=index_outbox_job_count,
        embedding_index_identity=embedding_index_identity,
    )
    actual_count = source_count if built_count is None else built_count

    def build(index_path: Path) -> BuildResult:
        observed.append(index_path)
        _write_artifact(index_path, spec, actual_count)
        return BuildResult(
            row_count=actual_count,
            quality_report=(
                _quality_report(
                    model=model,
                    revision=revision,
                    dimension=dimension,
                )
                if quality_report is None
                else quality_report
            ),
        )

    manifest = manager.build_generation(spec, build)
    if reconcile:
        assert manifest.index_outbox is not None
        receipt = {
            "generation_id": manifest.generation_id,
            "manifest_hash": manifest.manifest_sha256,
            "watermark": 0,
            "immutable_digest": _EMPTY_OUTBOX_DIGEST,
            "job_count": 0,
            "marked_done_count": 0,
            "reconciled_at": "2026-07-23T00:00:00Z",
        }
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE index_generation_reconciliation ("
                "generation_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, "
                "watermark INTEGER NOT NULL, immutable_digest TEXT NOT NULL, "
                "job_count INTEGER NOT NULL, marked_done_count INTEGER NOT NULL, "
                "reconciled_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO index_generation_reconciliation VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(receipt.values()),
            )
            connection.commit()
            manifest = manager.mark_reconciled(
                manifest.generation_id,
                receipt,
                connection=connection,
            )
        finally:
            connection.close()
    return manifest, observed


def test_build_binds_manifest_index_quality_and_actual_rows_without_touching_current(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    manifest, observed = _build_complete(manager, "gen-v4-r1")
    generation_path = manager.generations_path / "gen-v4-r1"

    assert observed == [generation_path / "index"]
    assert manifest.build_status == "complete"
    assert manifest.source_row_count == manifest.built_row_count == 3
    assert manifest.benchmark_corpus_sha256 == BENCHMARK_CORPUS_SHA
    assert manifest.benchmark_corpus_count == BENCHMARK_CORPUS_COUNT
    assert manifest.benchmark_cases_sha256 == BENCHMARK_CASES_SHA
    assert manifest.benchmark_case_count == BENCHMARK_CASE_COUNT
    assert manifest.index_text_policy == INDEX_TEXT_POLICY
    assert manifest.index_material_sha256 == _spec("gen-v4-r1").index_material_sha256
    assert manifest.verification_status == "unverified"
    assert len(manifest.identity_sha256) == 64
    assert len(manifest.index_tree_sha256 or "") == 64
    assert len(manifest.quality_report_sha256 or "") == 64
    assert len(manifest.manifest_sha256) == 64
    assert not manager.current_path.exists()
    persisted = json.loads((generation_path / "manifest.json").read_text(encoding="utf-8"))
    assert persisted == manifest.to_dict()


def test_missing_artifact_verifier_fails_build_closed_and_removes_staging(tmp_path):
    manager = GenerationManager(tmp_path / "lancedb-root")
    spec = _spec("candidate")

    def build(index_path: Path) -> BuildResult:
        _write_artifact(index_path, spec, 3)
        return BuildResult(3, _quality_report())

    with pytest.raises(GenerationError, match="artifact_verifier_required"):
        manager.build_generation(spec, build)
    assert not (manager.generations_path / "candidate").exists()


def test_artifact_verifier_observes_actual_row_count_instead_of_callback_claim(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    spec = _spec("candidate")

    def build(index_path: Path) -> BuildResult:
        _write_artifact(index_path, spec, 0)
        return BuildResult(3, _quality_report())

    with pytest.raises(GenerationError, match="artifact_row_count_mismatch"):
        manager.build_generation(spec, build)
    assert not (manager.generations_path / "candidate").exists()


def test_artifact_verifier_rejects_same_count_with_different_index_material(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    spec = _spec("candidate")

    def build(index_path: Path) -> BuildResult:
        _write_artifact(index_path, spec, 3)
        artifact_path = index_path / "artifact.json"
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        payload["rows"][0]["text"] = "different indexed text"
        artifact_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return BuildResult(3, _quality_report())

    with pytest.raises(GenerationError, match="artifact_index_material_mismatch"):
        manager.build_generation(spec, build)
    assert not (manager.generations_path / "candidate").exists()


def test_model_or_revision_change_runs_distinct_full_builds(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    first, _ = _build_complete(manager, "model-a-r1", model="model-a", revision="r1")
    second, _ = _build_complete(manager, "model-b-r1", model="model-b", revision="r1")
    third, _ = _build_complete(manager, "model-b-r2", model="model-b", revision="r2")
    assert len({first.identity_sha256, second.identity_sha256, third.identity_sha256}) == 3
    assert len(manager.list_manifests()) == 3


def test_current_manifest_metadata_skips_expensive_index_tree_digest(tmp_path, monkeypatch):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "active", reconcile=True)
    manager.promote("active")

    def reject_index_walk(_index_fd):
        raise AssertionError("status metadata must not hash the index tree")

    monkeypatch.setattr(generation_module, "_index_tree_sha256", reject_index_walk)

    manifest = manager.current_manifest_metadata()

    assert manifest is not None
    assert manifest.generation_id == "active"


def test_benchmark_binding_changes_identity_without_changing_production_source():
    original = _spec("candidate")
    changed = _spec("candidate", benchmark_cases_sha256="8" * 64)

    assert original.source_db_sha256 == changed.source_db_sha256 == SOURCE_SHA
    assert original.source_row_count == changed.source_row_count == 3
    assert original.identity_sha256 != changed.identity_sha256


def test_index_policy_and_material_each_change_generation_identity():
    original = _spec("candidate")
    legacy = _spec("candidate", index_text_policy="legacy")
    changed_material = _spec("candidate", index_material_digest="8" * 64)

    assert (
        len({original.identity_sha256, legacy.identity_sha256, changed_material.identity_sha256})
        == 3
    )


def test_failed_build_removes_staging_and_preserves_active_generation(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "active-a", reconcile=True)
    manager.promote("active-a")
    current_before = os.readlink(manager.current_path)

    def fail(index_path: Path) -> BuildResult:
        (index_path / "partial").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected build failure")

    with pytest.raises(RuntimeError, match="injected build failure"):
        manager.build_generation(_spec("failed-b"), fail)
    assert not (manager.generations_path / "failed-b").exists()
    assert os.readlink(manager.current_path) == current_before
    assert manager.resolve_current_manifest().generation_id == "active-a"


def test_source_and_actual_row_count_mismatch_is_not_promotable(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "baseline", source_count=1, reconcile=True)
    manager.promote("baseline")

    with pytest.raises(GenerationError, match="artifact_index_material_mismatch"):
        _build_complete(manager, "candidate", source_count=3, built_count=2)

    assert manager.resolve_current_manifest().generation_id == "baseline"
    assert not (manager.generations_path / "candidate").exists()


def test_outbox_evidence_requires_explicit_reconciliation_before_runtime_selection(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    manifest, _ = _build_complete(
        manager,
        "candidate",
        index_outbox_watermark=7,
        index_outbox_digest="a" * 64,
        index_outbox_job_count=2,
    )
    assert manifest.index_outbox is not None
    with pytest.raises(PromotionError, match="outbox_reconciliation_required"):
        manager.promote("candidate")

    receipt = {
        "generation_id": "candidate",
        "manifest_hash": manifest.manifest_sha256,
        "watermark": 7,
        "immutable_digest": "a" * 64,
        "job_count": 2,
        "marked_done_count": 2,
        "reconciled_at": "2026-07-23T00:00:00Z",
    }
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE index_generation_reconciliation ("
        "generation_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, "
        "watermark INTEGER NOT NULL, immutable_digest TEXT NOT NULL, "
        "job_count INTEGER NOT NULL, marked_done_count INTEGER NOT NULL, "
        "reconciled_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO index_generation_reconciliation VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            receipt["generation_id"],
            receipt["manifest_hash"],
            receipt["watermark"],
            receipt["immutable_digest"],
            receipt["job_count"],
            receipt["marked_done_count"],
            receipt["reconciled_at"],
        ),
    )
    connection.commit()
    reconciled = manager.mark_reconciled("candidate", receipt, connection=connection)
    assert reconciled.index_outbox["reconciled"] is True
    promoted = manager.promote("candidate")
    assert promoted.verification_status == "verified"
    assert manager.resolve_current_manifest().generation_id == "candidate"


def test_verify_candidate_marks_inactive_manifest_verified_then_can_promote(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "active", reconcile=True)
    manager.promote("active")
    expected_identity = "cloud|endpoint=candidate"
    _build_complete(
        manager,
        "candidate",
        reconcile=True,
        embedding_index_identity=expected_identity,
    )
    current_before = os.readlink(manager.current_path)

    verified = manager.verify_candidate(
        "candidate",
        expected_embedding_index_identity=expected_identity,
    )

    assert verified.verification_status == "verified"
    assert verified.verified_at is not None
    assert manager.load_manifest("candidate") == verified
    assert os.readlink(manager.current_path) == current_before
    assert manager.resolve_current_manifest().generation_id == "active"

    promoted = manager.promote("candidate")
    assert promoted == verified
    assert manager.resolve_current_manifest().generation_id == "candidate"


def test_verify_candidate_runs_runtime_evidence_gate_before_and_after_write(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path / "lancedb-root")
    manifest, _ = _build_complete(manager, "candidate", reconcile=True)
    observed = []

    def validate_runtime(spec, report, *, operation):
        observed.append((spec, report, operation))

    monkeypatch.setattr(
        manager,
        "_validate_current_runtime_environment",
        validate_runtime,
    )

    verified = manager.verify_candidate("candidate")

    assert verified.verification_status == "verified"
    assert observed == [
        (manifest.spec, manifest.quality_report, "verification"),
        (manifest.spec, manifest.quality_report, "verification"),
    ]
    assert not manager.current_path.exists()


def test_verify_candidate_runtime_gate_failure_restores_manifest_and_current(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "active", reconcile=True)
    manager.promote("active")
    _build_complete(manager, "candidate", reconcile=True)
    current_before = os.readlink(manager.current_path)
    validation_calls = 0

    def fail_after_manifest_write(_spec, _report, *, operation):
        nonlocal validation_calls
        assert operation == "verification"
        validation_calls += 1
        if validation_calls == 2:
            raise PromotionError("injected_runtime_environment_mismatch")

    monkeypatch.setattr(
        manager,
        "_validate_current_runtime_environment",
        fail_after_manifest_write,
    )

    with pytest.raises(PromotionError, match="injected_runtime_environment_mismatch"):
        manager.verify_candidate("candidate")

    assert validation_calls == 2
    restored = manager.load_manifest("candidate")
    assert restored.verification_status == "unverified"
    assert restored.verified_at is None
    assert os.readlink(manager.current_path) == current_before
    assert manager.resolve_current_manifest().generation_id == "active"


def test_verify_candidate_rejects_embedding_index_identity_mismatch(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(
        manager,
        "candidate",
        reconcile=True,
        embedding_index_identity="cloud|endpoint=staged",
    )

    with pytest.raises(
        PromotionError,
        match="generation_embedding_index_identity_mismatch",
    ):
        manager.verify_candidate(
            "candidate",
            expected_embedding_index_identity="cloud|endpoint=different",
        )

    assert manager.load_manifest("candidate").verification_status == "unverified"
    assert not manager.current_path.exists()


def test_verify_candidate_rejects_current_generation(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "active", reconcile=True)
    manager.promote("active")
    current_before = os.readlink(manager.current_path)

    with pytest.raises(PromotionError, match="candidate_generation_must_be_inactive"):
        manager.verify_candidate("active")

    assert os.readlink(manager.current_path) == current_before
    assert manager.resolve_current_manifest().generation_id == "active"


def test_verify_candidate_restores_manifest_after_post_write_interruption(tmp_path):
    verification_enabled = False
    verification_calls = 0

    def interrupt_second_verification(request):
        nonlocal verification_calls
        if verification_enabled:
            verification_calls += 1
            if verification_calls == 2:
                raise KeyboardInterrupt
        return _artifact_verifier(request)

    manager = GenerationManager(
        tmp_path / "lancedb-root",
        artifact_verifier=interrupt_second_verification,
    )
    _build_complete(manager, "candidate", reconcile=True)
    verification_enabled = True

    with pytest.raises(KeyboardInterrupt):
        manager.verify_candidate("candidate")

    restored = manager.load_manifest("candidate")
    assert restored.verification_status == "unverified"
    assert restored.verified_at is None
    assert not manager.current_path.exists()


def test_generation_manifest_round_trips_logical_source_fingerprint(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    source_fingerprint = "c" * 64
    spec = _spec(
        "candidate",
        index_outbox_watermark=0,
        index_outbox_digest=_EMPTY_OUTBOX_DIGEST,
        index_outbox_job_count=0,
        index_outbox_source_fingerprint=source_fingerprint,
    )

    manifest = manager.build_generation(
        spec,
        lambda index_path: (
            _write_artifact(index_path, spec, 3),
            BuildResult(3, _quality_report()),
        )[1],
    )

    assert manifest.index_outbox["source_fingerprint"] == source_fingerprint
    loaded = manager.load_manifest("candidate")
    assert loaded.spec.index_outbox_source_fingerprint == source_fingerprint


def test_generation_manifest_round_trips_full_embedding_index_identity(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    index_identity = (
        "text-embedding-v4|provider=openai-compatible|revision=2026-07-23|"
        "dim=1024|endpoint_sha256=" + "e" * 64
    )
    spec = _spec(
        "candidate",
        index_outbox_watermark=0,
        index_outbox_digest=_EMPTY_OUTBOX_DIGEST,
        index_outbox_job_count=0,
        embedding_index_identity=index_identity,
    )

    manifest = manager.build_generation(
        spec,
        lambda index_path: (
            _write_artifact(index_path, spec, 3),
            BuildResult(3, _quality_report()),
        )[1],
    )

    assert manifest.index_outbox["embedding_index_identity"] == index_identity
    assert manager.load_manifest("candidate").spec.embedding_index_identity == index_identity


def test_legacy_manifest_without_outbox_field_preserves_original_hash(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    manifest, _ = _build_complete(manager, "legacy-candidate")
    payload = manifest.to_dict()
    payload.pop("index_outbox")
    payload.pop("index_text_policy")
    payload.pop("index_material_sha256")
    unbound_spec = replace(
        manifest.spec,
        index_text_policy=None,
        index_material_sha256=None,
    )
    payload["identity_sha256"] = generation_module._legacy_generation_identity_sha256(unbound_spec)
    binding = dict(payload)
    binding.pop("manifest_sha256")
    payload["manifest_sha256"] = generation_module._json_sha256(binding)
    original_hash = payload["manifest_sha256"]
    manifest_path = manager.generations_path / "legacy-candidate" / "manifest.json"
    manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    loaded = manager.load_manifest("legacy-candidate")

    assert loaded.manifest_sha256 == original_hash
    assert loaded.index_outbox is None
    assert "index_outbox" not in loaded.to_dict()
    assert "index_text_policy" not in loaded.to_dict()
    assert "index_material_sha256" not in loaded.to_dict()
    assert loaded.to_dict() == payload


def test_outbox_era_manifest_without_material_binding_loads_but_cannot_promote(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    manifest, _ = _build_complete(manager, "old-outbox", reconcile=True)
    payload = manifest.to_dict()
    payload.pop("index_text_policy")
    payload.pop("index_material_sha256")
    unbound_spec = replace(
        manifest.spec,
        index_text_policy=None,
        index_material_sha256=None,
    )
    payload["identity_sha256"] = unbound_spec.identity_sha256
    binding = dict(payload)
    binding.pop("manifest_sha256")
    payload["manifest_sha256"] = generation_module._json_sha256(binding)
    manifest_path = manager.generations_path / "old-outbox" / "manifest.json"
    manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    loaded = manager.load_manifest("old-outbox")

    assert loaded.to_dict() == payload
    with pytest.raises(PromotionError, match="generation_index_material_binding_required"):
        manager.promote("old-outbox")


def test_manifest_with_non_mapping_outbox_evidence_fails_as_manifest_error(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "candidate")
    manifest_path = manager.generations_path / "candidate" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["index_outbox"] = []
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestError, match="invalid_index_outbox_evidence"):
        manager.load_manifest("candidate")


def test_promote_rejects_manifest_without_outbox_reconciliation_evidence(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "legacy-candidate")

    with pytest.raises(PromotionError, match="generation_outbox_reconciliation_required"):
        manager.promote("legacy-candidate")
    assert not manager.current_path.exists()


def test_mark_reconciled_rejects_receipt_missing_from_sqlite(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    manifest, _ = _build_complete(
        manager,
        "candidate",
        index_outbox_watermark=7,
        index_outbox_digest="a" * 64,
        index_outbox_job_count=2,
    )
    receipt = {
        "generation_id": "candidate",
        "manifest_hash": manifest.manifest_sha256,
        "watermark": 7,
        "immutable_digest": "a" * 64,
        "job_count": 2,
        "marked_done_count": 2,
        "reconciled_at": "2026-07-23T00:00:00Z",
    }
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE index_generation_reconciliation ("
        "generation_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, "
        "watermark INTEGER NOT NULL, immutable_digest TEXT NOT NULL, "
        "job_count INTEGER NOT NULL, marked_done_count INTEGER NOT NULL, "
        "reconciled_at TEXT NOT NULL)"
    )

    with pytest.raises(PromotionError, match="reconciliation_database_receipt_mismatch"):
        manager.mark_reconciled("candidate", receipt, connection=connection)
    assert manager.load_manifest("candidate").index_outbox["reconciled"] is False


def test_mark_reconciled_rejects_persisted_receipt_with_forged_evidence(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    manifest, _ = _build_complete(
        manager,
        "candidate",
        index_outbox_watermark=7,
        index_outbox_digest="a" * 64,
        index_outbox_job_count=2,
    )
    receipt = {
        "generation_id": "candidate",
        "manifest_hash": manifest.manifest_sha256,
        "watermark": 8,
        "immutable_digest": "a" * 64,
        "job_count": 2,
        "marked_done_count": 2,
        "reconciled_at": "2026-07-23T00:00:00Z",
    }
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE index_generation_reconciliation ("
        "generation_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, "
        "watermark INTEGER NOT NULL, immutable_digest TEXT NOT NULL, "
        "job_count INTEGER NOT NULL, marked_done_count INTEGER NOT NULL, "
        "reconciled_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO index_generation_reconciliation VALUES (?, ?, ?, ?, ?, ?, ?)",
        tuple(receipt.values()),
    )
    connection.commit()

    with pytest.raises(PromotionError, match="reconciliation_receipt_evidence_mismatch"):
        manager.mark_reconciled("candidate", receipt, connection=connection)
    assert manager.load_manifest("candidate").index_outbox["reconciled"] is False


def test_mark_reconciled_rechecks_outbox_window_before_manifest_update(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    db_path = tmp_path / "source.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)")
        connection.execute("INSERT INTO memories VALUES ('memory-1', 'source')")
        connection.execute(
            "CREATE TABLE store_outbox ("
            "outbox_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, project_id TEXT NOT NULL, "
            "call_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO store_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-1", "memory_index", "project", "call-1", "done", "{}", "{}", "now", "now"),
        )
        connection.commit()

        from plastic_promise.core.index_outbox_reconciliation import snapshot_index_outbox

        evidence = snapshot_index_outbox(connection)
        spec = _spec(
            "candidate",
            source_count=1,
            index_outbox_watermark=evidence["watermark"],
            index_outbox_digest=evidence["immutable_digest"],
            index_outbox_job_count=evidence["job_count"],
            index_outbox_source_fingerprint=evidence["source_fingerprint"],
        )
        manifest = manager.build_generation(
            spec,
            lambda index_path: (
                _write_artifact(index_path, spec, 1),
                BuildResult(1, _quality_report()),
            )[1],
        )
        receipt = {
            "generation_id": manifest.generation_id,
            "manifest_hash": manifest.manifest_sha256,
            "watermark": evidence["watermark"],
            "immutable_digest": evidence["immutable_digest"],
            "job_count": evidence["job_count"],
            "marked_done_count": 0,
            "reconciled_at": "2026-07-23T00:00:00Z",
        }
        connection.execute(
            "CREATE TABLE index_generation_reconciliation ("
            "generation_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, "
            "watermark INTEGER NOT NULL, immutable_digest TEXT NOT NULL, "
            "job_count INTEGER NOT NULL, marked_done_count INTEGER NOT NULL, "
            "reconciled_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO index_generation_reconciliation VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(receipt.values()),
        )
        # Simulate a writer committing a newer job after reconciliation but
        # before the generation manifest could be marked.
        connection.execute(
            "INSERT INTO store_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-2", "memory_index", "project", "call-2", "pending", "{}", "{}", "now", "now"),
        )
        connection.commit()

        with pytest.raises(PromotionError, match="generation_outbox_newer_jobs_make_stale"):
            manager.mark_reconciled(manifest.generation_id, receipt, connection=connection)
        assert manager.load_manifest(manifest.generation_id).index_outbox["reconciled"] is False
    finally:
        connection.close()


def test_source_bound_promotion_requires_database_and_rechecks_freshness(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    connection = sqlite3.connect(tmp_path / "source.db")
    try:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)")
        connection.execute("INSERT INTO memories VALUES ('memory-1', 'source')")
        connection.execute(
            "CREATE TABLE store_outbox ("
            "outbox_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, project_id TEXT NOT NULL, "
            "call_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO store_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-1", "memory_index", "project", "call-1", "done", "{}", "{}", "now", "now"),
        )
        connection.commit()

        from plastic_promise.core.index_outbox_reconciliation import (
            reconcile_index_outbox,
            snapshot_index_outbox,
        )

        evidence = snapshot_index_outbox(connection)
        spec = _spec(
            "candidate",
            source_count=1,
            index_outbox_watermark=evidence["watermark"],
            index_outbox_digest=evidence["immutable_digest"],
            index_outbox_job_count=evidence["job_count"],
            index_outbox_source_fingerprint=evidence["source_fingerprint"],
            embedding_index_identity="cloud|endpoint=a",
        )
        manifest = manager.build_generation(
            spec,
            lambda index_path: (
                _write_artifact(index_path, spec, 1),
                BuildResult(1, _quality_report()),
            )[1],
        )
        receipt = reconcile_index_outbox(
            connection,
            generation_id=manifest.generation_id,
            manifest_hash=manifest.manifest_sha256,
            evidence=manifest.index_outbox,
        )
        manager.mark_reconciled(manifest.generation_id, receipt, connection=connection)

        with pytest.raises(PromotionError, match="promotion_database_required"):
            manager.promote(manifest.generation_id)

        # A newer durable job after reconciliation makes the candidate stale;
        # the pointer must not move even though the manifest is otherwise valid.
        connection.execute(
            "INSERT INTO store_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-2", "memory_index", "project", "call-2", "pending", "{}", "{}", "now", "now"),
        )
        connection.commit()
        with pytest.raises(PromotionError, match="generation_outbox_newer_jobs_make_stale"):
            manager.promote(manifest.generation_id, connection=connection)
        assert not manager.current_path.exists()
    finally:
        connection.close()


def test_verify_candidate_rejects_newer_outbox_and_preserves_current(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "active", reconcile=True)
    manager.promote("active")
    current_before = os.readlink(manager.current_path)
    expected_identity = "cloud|endpoint=staged"
    connection = sqlite3.connect(tmp_path / "source.db")
    try:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)")
        connection.execute("INSERT INTO memories VALUES ('memory-1', 'source')")
        connection.execute(
            "CREATE TABLE store_outbox ("
            "outbox_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, project_id TEXT NOT NULL, "
            "call_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO store_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-1", "memory_index", "project", "call-1", "done", "{}", "{}", "now", "now"),
        )
        connection.commit()

        from plastic_promise.core.index_outbox_reconciliation import (
            reconcile_index_outbox,
            snapshot_index_outbox,
        )

        evidence = snapshot_index_outbox(connection)
        spec = _spec(
            "candidate",
            source_count=1,
            index_outbox_watermark=evidence["watermark"],
            index_outbox_digest=evidence["immutable_digest"],
            index_outbox_job_count=evidence["job_count"],
            index_outbox_source_fingerprint=evidence["source_fingerprint"],
            embedding_index_identity=expected_identity,
        )
        manifest = manager.build_generation(
            spec,
            lambda index_path: (
                _write_artifact(index_path, spec, 1),
                BuildResult(1, _quality_report()),
            )[1],
        )
        receipt = reconcile_index_outbox(
            connection,
            generation_id=manifest.generation_id,
            manifest_hash=manifest.manifest_sha256,
            evidence=manifest.index_outbox,
        )
        manager.mark_reconciled(manifest.generation_id, receipt, connection=connection)
        connection.execute(
            "INSERT INTO store_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-2",
                "memory_index",
                "project",
                "call-2",
                "pending",
                "{}",
                "{}",
                "now",
                "now",
            ),
        )
        connection.commit()

        with pytest.raises(
            PromotionError,
            match="generation_outbox_newer_jobs_make_stale",
        ):
            manager.verify_candidate(
                manifest.generation_id,
                connection=connection,
                expected_embedding_index_identity=expected_identity,
            )

        assert manager.load_manifest(manifest.generation_id).verification_status == "unverified"
        assert os.readlink(manager.current_path) == current_before
        assert manager.resolve_current_manifest().generation_id == "active"
    finally:
        connection.close()


def test_rollback_rejects_verified_but_unreconciled_generation(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    manifest, _ = _build_complete(
        manager,
        "candidate",
        index_outbox_watermark=7,
        index_outbox_digest="a" * 64,
        index_outbox_job_count=2,
    )
    forced_verified = replace(
        manifest,
        verification_status="verified",
        verified_at="2026-07-23T00:00:00Z",
    ).reseal()
    forced_verified.validate()
    manifest_path = manager.generations_path / "candidate" / "manifest.json"
    manifest_path.write_text(
        json.dumps(forced_verified.to_dict(), sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(PromotionError, match="generation_outbox_reconciliation_required"):
        manager.rollback("candidate")
    assert not manager.current_path.exists()


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        ("metrics.hit_at.1", 0.0, "hit_at_metric_invalid"),
        ("metrics.hit_at.5", 0.01, "hit_at_metric_invalid"),
        ("metrics.mrr", 0.0, "mrr_metric_invalid"),
        ("metrics.p95_ms", 5000.01, "p95_metric_invalid"),
        ("metrics.forbidden_hit_rate", 0.01, "forbidden_metric_invalid"),
        ("metrics.fallback_rate", 0.01, "fallback_or_degradation_detected"),
        ("metrics.language.zh.degradation_rate", 0.01, "fallback_or_degradation_detected"),
        ("metrics.language.en", _DELETE, "language_evidence_invalid"),
        ("metrics.language.cross-lingual", _DELETE, "language_evidence_invalid"),
        ("metrics.language.zh.case_count", 2, "language_evidence_invalid"),
        ("publishable_claim", False, "publishable_claim_required"),
        ("backend.mode", "synthetic", "live_backend_evidence_invalid"),
        ("backend.fallback_used", True, "live_backend_evidence_invalid"),
        ("backend.model", "different-model", "backend_identity_mismatch"),
        ("cases.sha256", "not-a-digest", "fixed_case_evidence_invalid"),
        ("cases.count", 7, "fixed_case_binding_mismatch"),
        ("corpus.sha256", "9" * 64, "fixed_corpus_binding_mismatch"),
        ("environment.source_commit", "unknown", "environment_evidence_invalid"),
        ("smoke.context", False, "store_recall_context_evidence_invalid"),
    ],
)
def test_invalid_quality_evidence_fails_build_before_persistence(
    tmp_path,
    path,
    value,
    reason,
):
    manager = _manager(tmp_path / "lancedb-root")
    with pytest.raises(GenerationError, match=reason):
        _build_complete(
            manager,
            "candidate",
            quality_report=_mutated_quality_report(path, value),
        )
    assert not (manager.generations_path / "candidate").exists()


def test_quality_report_rejects_unknown_secret_fields_without_persisting_them(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    secret = "sk-sensitive-marker"
    report = _quality_report()
    report["api_key"] = secret

    with pytest.raises(GenerationError, match="quality_report_fields_invalid") as raised:
        _build_complete(manager, "candidate", quality_report=report)
    assert secret not in str(raised.value)
    assert not (manager.generations_path / "candidate").exists()


def test_generation_quality_report_binds_cny_cost_currency(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    report = _quality_report()
    report["usage"] = {
        "embedding_requests": 42,
        "embedding_input_tokens": 4096,
        "cost": 0.00012288,
        "cost_currency": "CNY",
        "cost_usd": None,
        "pricing_revision": "syuan-pricing-2026-07-24",
    }

    manifest, _ = _build_complete(manager, "candidate", quality_report=report)

    assert manifest.quality_report["usage"] == report["usage"]


def test_generation_quality_report_rejects_currency_mismatch(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    report = _quality_report()
    report["usage"].update(
        {
            "cost": 0.25,
            "cost_currency": "CNY",
        }
    )

    with pytest.raises(GenerationError, match="embedding_cost_currency_mismatch"):
        _build_complete(manager, "candidate", quality_report=report)


def test_nested_index_symlink_is_rejected_and_cleanup_does_not_follow_it(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    spec = _spec("candidate")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("outside", encoding="utf-8")

    def build(index_path: Path) -> BuildResult:
        _write_artifact(index_path, spec, 3)
        (index_path / "escape").symlink_to(outside, target_is_directory=True)
        return BuildResult(3, _quality_report())

    with pytest.raises(GenerationError, match="index_contains_symlink"):
        manager.build_generation(spec, build)
    assert sentinel.read_text(encoding="utf-8") == "outside"
    assert not (manager.generations_path / "candidate").exists()


def test_special_index_file_is_rejected(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    spec = _spec("candidate")

    def build(index_path: Path) -> BuildResult:
        _write_artifact(index_path, spec, 3)
        os.mkfifo(index_path / "unsafe-pipe")
        return BuildResult(3, _quality_report())

    with pytest.raises(GenerationError, match="index_contains_unsafe_entry"):
        manager.build_generation(spec, build)
    assert not (manager.generations_path / "candidate").exists()


def test_parent_path_swap_is_detected_and_new_path_is_not_cleaned(tmp_path):
    root = tmp_path / "lancedb-root"
    displaced = tmp_path / "displaced-root"
    manager = _manager(root)
    spec = _spec("candidate")

    def build(index_path: Path) -> BuildResult:
        _write_artifact(index_path, spec, 3)
        root.rename(displaced)
        root.mkdir(mode=0o700)
        (root / "generations").mkdir(mode=0o700)
        marker = root / "generations" / "must-survive"
        marker.write_text("new-root", encoding="utf-8")
        return BuildResult(3, _quality_report())

    with pytest.raises(GenerationError, match="generation_root_replaced"):
        manager.build_generation(spec, build)
    assert (root / "generations" / "must-survive").read_text(encoding="utf-8") == "new-root"
    assert not (displaced / "generations" / "candidate").exists()


def test_index_tamper_after_build_is_detected_on_load_and_current_resolution(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "candidate", reconcile=True)
    manager.promote("candidate")
    artifact_path = manager.generations_path / "candidate" / "index" / "artifact.json"
    artifact_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ManifestError, match="index_tree_digest_mismatch"):
        manager.load_manifest("candidate")
    with pytest.raises(ManifestError, match="index_tree_digest_mismatch"):
        manager.resolve_current_manifest()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("built_row_count", 2, "manifest_digest_mismatch"),
        ("embedding_model", "different-model", "manifest_identity_mismatch"),
    ],
)
def test_manifest_identity_and_built_count_tamper_is_detected(tmp_path, field, value, reason):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "candidate")
    manifest_path = manager.generations_path / "candidate" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestError, match=reason):
        manager.load_manifest("candidate")


def test_quality_report_tamper_is_detected_by_digest(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "candidate")
    manifest_path = manager.generations_path / "candidate" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["quality_report"]["usage"]["cost_usd"] = 0.50
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestError, match="quality_report_digest_mismatch"):
        manager.load_manifest("candidate")


def test_cli_rejects_tampered_secret_without_printing_it(tmp_path, capsys):
    root = tmp_path / "lancedb-root"
    manager = _manager(root)
    _build_complete(manager, "candidate")
    manifest_path = manager.generations_path / "candidate" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    secret = "sk-sensitive-marker"
    payload["quality_report"]["api_key"] = secret
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert generation_cli(["--root", str(root), "inspect", "candidate"]) == 2
    output = capsys.readouterr()
    assert secret not in output.out
    assert secret not in output.err
    assert "quality_report_fields_invalid" in output.err


def test_oversized_manifest_is_rejected_by_bounded_descriptor_read(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "candidate")
    manifest_path = manager.generations_path / "candidate" / "manifest.json"
    manifest_path.write_text("{" + (" " * (1024 * 1024)) + "}", encoding="utf-8")

    with pytest.raises(ManifestError, match="manifest_too_large"):
        manager.load_manifest("candidate")


def test_atomic_switch_uses_root_descriptor_and_relative_temporary_link(tmp_path, monkeypatch):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")
    current_before = os.readlink(manager.current_path)
    _build_complete(manager, "generation-b", reconcile=True)
    calls = []
    real_replace = os.replace

    def recording_replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        if destination == "current":
            calls.append(
                {
                    "source": source,
                    "target": os.readlink(source, dir_fd=src_dir_fd),
                    "old_current": os.readlink("current", dir_fd=dst_dir_fd),
                    "same_root_fd": src_dir_fd == dst_dir_fd,
                }
            )
        return real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(generation_module.os, "replace", recording_replace)
    promoted = manager.promote("generation-b")

    assert promoted.verification_status == "verified"
    assert len(calls) == 1
    assert calls[0]["source"].startswith(".current.")
    assert calls[0]["target"].startswith("selections/")
    assert calls[0]["old_current"] == current_before
    assert calls[0]["same_root_fd"] is True
    assert os.readlink(manager.current_path) == calls[0]["target"]
    assert os.readlink(manager.root / calls[0]["target"]) == "../generations/generation-b"


def test_atomic_switch_failure_preserves_current_and_reverts_verification(tmp_path, monkeypatch):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")
    current_before = os.readlink(manager.current_path)
    _build_complete(manager, "generation-b", reconcile=True)
    real_replace = os.replace

    def fail_current_replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        if destination == "current":
            raise OSError("injected current replace failure")
        return real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(generation_module.os, "replace", fail_current_replace)
    with pytest.raises(PromotionError, match="current_pointer_switch_failed"):
        manager.promote("generation-b")

    assert os.readlink(manager.current_path) == current_before
    assert manager.resolve_current_manifest().generation_id == "generation-a"
    assert manager.load_manifest("generation-b").verification_status == "unverified"
    assert list(manager.root.glob(".current.*.tmp")) == []


def test_atomic_switch_detects_replaced_temporary_link_and_restores_current(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")
    current_before = os.readlink(manager.current_path)
    _build_complete(manager, "generation-b", reconcile=True)
    real_replace = os.replace
    attacked = False

    def replace_temporary_link(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal attacked
        if destination == "current" and source.startswith(".current.") and not attacked:
            attacked = True
            os.unlink(source, dir_fd=src_dir_fd)
            os.symlink("/outside-generation-root", source, dir_fd=src_dir_fd)
        return real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(generation_module.os, "replace", replace_temporary_link)
    with pytest.raises(PromotionError, match="current_pointer_switch_corrupted"):
        manager.promote("generation-b")

    assert attacked is True
    assert os.readlink(manager.current_path) == current_before
    assert manager.resolve_current_manifest().generation_id == "generation-a"
    assert manager.load_manifest("generation-b").verification_status == "unverified"


def test_restart_resolves_verified_current_and_rollback_revalidates_artifact(tmp_path):
    root = tmp_path / "lancedb-root"
    manager = _manager(root)
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")
    _build_complete(manager, "generation-b", reconcile=True)
    manager.promote("generation-b")
    _build_complete(manager, "never-verified")
    manager.close()

    restarted = _manager(root)
    assert restarted.resolve_current_manifest().generation_id == "generation-b"
    with pytest.raises(PromotionError, match="generation_not_verified"):
        restarted.rollback("never-verified")
    assert restarted.rollback("generation-a").generation_id == "generation-a"
    assert restarted.resolve_current_manifest().generation_id == "generation-a"


def test_current_selection_identity_changes_across_a_to_b_to_a(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")
    pointer_a = os.readlink(manager.current_path)
    manifest_a, index_a, selection_a = manager.resolve_verified_current_selection()

    _build_complete(manager, "generation-b", reconcile=True)
    manager.promote("generation-b")
    pointer_b = os.readlink(manager.current_path)
    manifest_b, _index_b, selection_b = manager.resolve_verified_current_selection()
    manager.rollback("generation-a")
    pointer_a_again = os.readlink(manager.current_path)
    manifest_a_again, index_a_again, selection_a_again = (
        manager.resolve_verified_current_selection()
    )

    assert manifest_a.generation_id == manifest_a_again.generation_id == "generation-a"
    assert manifest_b.generation_id == "generation-b"
    assert index_a == index_a_again
    assert len(selection_a) == len(selection_b) == len(selection_a_again) == 64
    assert len({selection_a, selection_b, selection_a_again}) == 3
    assert pointer_a == f"selections/{selection_a}"
    assert pointer_b == f"selections/{selection_b}"
    assert pointer_a_again == f"selections/{selection_a_again}"
    assert len({pointer_a, pointer_b, pointer_a_again}) == 3
    assert os.readlink(manager.root / pointer_a) == "../generations/generation-a"
    assert os.readlink(manager.root / pointer_b) == "../generations/generation-b"
    assert os.readlink(manager.root / pointer_a_again) == "../generations/generation-a"


def test_activation_identity_collision_never_reuses_retained_selection(tmp_path, monkeypatch):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "generation-a", reconcile=True)
    _build_complete(manager, "generation-b", reconcile=True)
    activation_ids = iter(["a" * 64, "a" * 64, "b" * 64])
    monkeypatch.setattr(generation_module.secrets, "token_hex", lambda _size: next(activation_ids))

    manager.promote("generation-a")
    assert manager.current_selection_identity() == "a" * 64
    manager.promote("generation-b")

    assert manager.current_selection_identity() == "b" * 64
    assert os.readlink(manager.root / "selections" / ("a" * 64)) == ("../generations/generation-a")
    assert os.readlink(manager.root / "selections" / ("b" * 64)) == ("../generations/generation-b")


def test_current_selection_rejects_replaced_selection_root(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")
    selections = manager.root / "selections"
    selections.rename(manager.root / "displaced-selections")
    selections.mkdir(mode=0o700)

    with pytest.raises(GenerationError, match="current_selection_root_replaced"):
        manager.current_selection_identity()


def test_verified_current_generation_returns_one_manifest_path_snapshot(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")

    manifest, index_path = manager.resolve_verified_current_generation()

    assert manifest.generation_id == "generation-a"
    assert index_path == manager.generations_path / manifest.generation_id / "index"


def test_resolve_verified_current_index_returns_only_revalidated_concrete_path(tmp_path):
    root = tmp_path / "lancedb-root"
    manager = _manager(root)
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")

    assert manager.resolve_verified_current_index() == (
        root / "generations" / "generation-a" / "index"
    )

    artifact_path = root / "generations" / "generation-a" / "index" / "artifact.json"
    artifact_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ManifestError, match="index_tree_digest_mismatch"):
        manager.resolve_verified_current_index()


def test_resolve_current_rejects_unverified_generation_even_with_valid_artifact(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "candidate")
    manager.current_path.symlink_to("generations/candidate", target_is_directory=True)

    with pytest.raises(PromotionError, match="current_generation_not_verified"):
        manager.resolve_current_manifest()


def test_legacy_direct_current_pointer_has_no_live_selection_identity(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    _build_complete(manager, "generation-a", reconcile=True)
    manager.promote("generation-a")
    manager.current_path.unlink()
    manager.current_path.symlink_to("generations/generation-a", target_is_directory=True)

    assert manager.resolve_current_manifest().generation_id == "generation-a"
    with pytest.raises(GenerationError, match="current_selection_identity_unavailable"):
        manager.resolve_verified_current_selection()


@pytest.mark.parametrize(
    "generation_id",
    ["", ".", "..", "../escape", "a/../b", "a\\..\\b", ".hidden", "current"],
)
def test_generation_id_rejects_path_traversal_and_reserved_names(tmp_path, generation_id):
    manager = _manager(tmp_path / "lancedb-root")
    with pytest.raises(ValueError, match="invalid_generation_id"):
        manager.build_generation(
            _spec(generation_id),
            lambda _path: BuildResult(3, _quality_report()),
        )


def test_current_symlink_cannot_escape_generation_root(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")
    outside = tmp_path / "outside"
    outside.mkdir()
    manager.current_path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(GenerationError, match="invalid_current_target"):
        manager.resolve_current_manifest()


def test_root_or_ancestor_symlink_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(GenerationError, match="generation_root_must_not_contain_symlink"):
        GenerationManager(alias / "root")


def test_interrupted_build_removes_staging(tmp_path):
    manager = _manager(tmp_path / "lancedb-root")

    def interrupt(index_path: Path) -> BuildResult:
        (index_path / "partial").write_text("partial", encoding="utf-8")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        manager.build_generation(_spec("interrupted"), interrupt)
    assert not (manager.generations_path / "interrupted").exists()


def test_cli_inspects_and_lazily_loads_default_verifier_for_switches(
    tmp_path,
    capsys,
    monkeypatch,
):
    root = tmp_path / "lancedb-root"
    manager = _manager(root)
    _build_complete(manager, "generation-a", reconcile=True)
    expected_identity = "cloud|endpoint=staged"
    _build_complete(
        manager,
        "generation-b",
        reconcile=True,
        embedding_index_identity=expected_identity,
    )
    source_db = tmp_path / "source.db"
    sqlite3.connect(source_db).close()
    monkeypatch.setenv("PLASTIC_DB_PATH", str(source_db))

    def unexpected_verifier_load():
        raise AssertionError("inspect must not import the LanceDB verifier")

    monkeypatch.setattr(
        generation_cli_module,
        "_load_default_artifact_verifier",
        unexpected_verifier_load,
    )
    assert generation_cli(["--root", str(root), "inspect"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["current"] is None
    assert [item["generation_id"] for item in inspected["generations"]] == [
        "generation-a",
        "generation-b",
    ]

    monkeypatch.setattr(
        generation_cli_module,
        "_load_default_artifact_verifier",
        lambda: _artifact_verifier,
    )
    assert generation_cli(["--root", str(root), "promote", "generation-a"]) == 0
    assert json.loads(capsys.readouterr().out)["generation_id"] == "generation-a"
    assert (
        generation_cli(
            [
                "--root",
                str(root),
                "verify-candidate",
                "generation-b",
                "--db",
                str(source_db),
                "--embedding-index-identity",
                expected_identity,
            ],
            artifact_verifier=_artifact_verifier,
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["verification_status"] == "verified"
    assert manager.resolve_current_manifest().generation_id == "generation-a"
    assert (
        generation_cli(
            ["--root", str(root), "promote", "generation-b"],
            artifact_verifier=_artifact_verifier,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        generation_cli(
            ["--root", str(root), "rollback", "generation-a"],
            artifact_verifier=_artifact_verifier,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["generation_id"] == "generation-a"


def test_cli_inspect_missing_root_is_read_only(tmp_path, capsys):
    root = tmp_path / "missing-root"
    assert generation_cli(["--root", str(root), "inspect"]) == 2
    assert not root.exists()
    assert "generation_root_not_found" in capsys.readouterr().err
