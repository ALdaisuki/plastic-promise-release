from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plastic_promise.core.memory_proposals import (
    MemoryProposalStore,
    ProposalPolicyError,
    ensure_memory_proposal_schema,
)
from plastic_promise.core.proposal_promotion import ensure_proposal_automation_schema
from plastic_promise.core.security_findings import SecurityFinding
from plastic_promise.core.shield_candidate_bridge import ShieldCandidateProposalBridge
from plastic_promise.core.shield_scan_store import ShieldScanStore


def _resolved(store: ShieldScanStore, project_id: str, commit_sha: str):
    opened = SecurityFinding(
        finding_id="finding:bridge",
        project_id=project_id,
        commit_sha=commit_sha,
        scan_revision="deepsec:v1",
        rule_id="deepsec.safe.pattern",
        request_scope_id=f"scope:{project_id}:open",
        remediation_pattern="Use a bounded, parameterized operation.",
    )
    opened_version = store.append_version(opened)
    remediation = opened.transition(
        "remediation_required",
        evidence={"decision": "patch_required"},
    )
    remediation_version = store.append_version(
        remediation,
        parent_version_id=opened_version.version_id,
    )
    fixed = remediation.transition(
        "fixed",
        evidence={"tests_passed": True},
    )
    fixed_version = store.append_version(fixed, parent_version_id=remediation_version.version_id)
    return store.record_rescan(
        project_id=project_id,
        parent_version_id=fixed_version.version_id,
        commit_sha=commit_sha,
        scan_revision="deepsec:v2",
        request_scope_id=f"scope:{project_id}:rescan",
        finding_present=False,
    )


def _candidate(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    source = _resolved(store, "project:alpha", "alpha-commit")
    validation = _resolved(store, "project:beta", "beta-commit")
    candidate = store.create_remediation_candidate(
        project_id="project:alpha",
        source_version_id=source.version_id,
    )
    store.record_remediation_validation(
        candidate_id=candidate.candidate_id,
        validation_project_id="project:beta",
        validation_version_id=validation.version_id,
    )
    store.register_shadow_generation(
        project_id="project:alpha",
        generation_id="shadow:security:v1",
        manifest_hash="a" * 64,
    )
    candidate = store.shadow_promote_remediation_candidate(
        candidate_id=candidate.candidate_id,
        shadow_generation="shadow:security:v1",
    )
    return store, store.record_shadow_canary(
        candidate_id=candidate.candidate_id,
        passed=True,
        evidence={"hit_at": 1.0, "sample_count": 12},
    )


def _shadowed_candidate(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    source = _resolved(store, "project:alpha", "alpha-commit")
    validation = _resolved(store, "project:beta", "beta-commit")
    candidate = store.create_remediation_candidate(
        project_id="project:alpha",
        source_version_id=source.version_id,
    )
    store.record_remediation_validation(
        candidate_id=candidate.candidate_id,
        validation_project_id="project:beta",
        validation_version_id=validation.version_id,
    )
    store.register_shadow_generation(
        project_id="project:alpha",
        generation_id="shadow:security:v1",
        manifest_hash="a" * 64,
    )
    return store, store.shadow_promote_remediation_candidate(
        candidate_id=candidate.candidate_id,
        shadow_generation="shadow:security:v1",
    )


def _quality_manifest(**overrides):
    report = {
        "degraded": False,
        "publishable_claim": True,
        "gate": {"status": "pass"},
        "corpus": {"sha256": "b" * 64, "count": 27},
        "cases": {"sha256": "c" * 64, "count": 20},
        "backend": {
            "model": "text-embedding-v4",
            "revision": "revision-a",
            "dimension": 1024,
        },
        "metrics": {
            "case_count": 20,
            "hit_at": {"1": 0.8, "5": 1.0},
            "mrr": 0.9,
            "p95_ms": 120.0,
            "forbidden_hit_rate": 0.0,
            "language": {"en": {"case_count": 10}, "zh": {"case_count": 10}},
        },
        "usage": {"cost_usd": 0.25, "pricing_revision": "pricing-v1"},
        "environment": {"comparison_environment_fingerprint": "d" * 64},
    }
    report.update(overrides.pop("quality_report", {}))
    manifest = {
        "generation_id": "shadow:security:v1",
        "manifest_sha256": "a" * 64,
        "build_status": "complete",
        "embedding_model": "text-embedding-v4",
        "model_revision": "revision-a",
        "embedding_dimension": 1024,
        "benchmark_corpus_sha256": "b" * 64,
        "benchmark_cases_sha256": "c" * 64,
        "created_at": "2026-08-05T00:00:00Z",
        "completed_at": "2026-08-05T01:00:00Z",
        "quality_report": report,
    }
    manifest.update(overrides)
    return manifest


def test_canary_failure_rolls_back_without_deleting_candidate(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    source = _resolved(store, "project:alpha", "alpha-commit")
    validation = _resolved(store, "project:beta", "beta-commit")
    candidate = store.create_remediation_candidate(
        project_id="project:alpha",
        source_version_id=source.version_id,
    )
    store.record_remediation_validation(
        candidate_id=candidate.candidate_id,
        validation_project_id="project:beta",
        validation_version_id=validation.version_id,
    )
    store.register_shadow_generation(
        project_id="project:alpha",
        generation_id="shadow:security:v1",
        manifest_hash="a" * 64,
    )
    shadowed = store.shadow_promote_remediation_candidate(
        candidate_id=candidate.candidate_id,
        shadow_generation="shadow:security:v1",
    )
    rolled_back = store.record_shadow_canary(
        candidate_id=shadowed.candidate_id,
        passed=False,
        reason="forbidden_hit_rate_exceeded",
        evidence={"forbidden_hit_rate": 0.25},
    )

    assert rolled_back.status == "rolled_back"
    assert rolled_back.canary_result == "failed"
    assert rolled_back.canary_reason == "forbidden_hit_rate_exceeded"
    assert rolled_back.canary_evidence == {"forbidden_hit_rate": 0.25}
    generation = (
        sqlite3.connect(tmp_path / "memory.db")
        .execute(
            "SELECT project_id, status FROM security_shadow_generations WHERE generation_id = ?",
            ("shadow:security:v1",),
        )
        .fetchone()
    )
    assert generation == ("project:alpha", "rolled_back")
    with pytest.raises(ProposalPolicyError, match="security_candidate_canary_required"):
        ShieldCandidateProposalBridge(sqlite3.connect(tmp_path / "memory.db")).link(rolled_back)


def test_bridge_rejects_forged_candidate_project_binding(tmp_path):
    store, candidate = _candidate(tmp_path)
    conn = sqlite3.connect(tmp_path / "memory.db")

    with pytest.raises(ProposalPolicyError, match="security_candidate_ownership_invalid"):
        ShieldCandidateProposalBridge(conn).link(replace(candidate, project_id="project:beta"))
    conn.close()


def test_bridge_rejects_forged_candidate_lineage_binding(tmp_path):
    store, candidate = _candidate(tmp_path)
    conn = sqlite3.connect(tmp_path / "memory.db")

    with pytest.raises(ProposalPolicyError, match="security_candidate_ownership_invalid"):
        ShieldCandidateProposalBridge(conn).link(replace(candidate, source_version_id="sfv_forged"))
    conn.close()


@pytest.mark.parametrize(
    ("reason", "evidence", "error_code"),
    [
        ("api_key=sk-test-secret-material-123456", {}, "remediation_canary_secret_detected"),
        ("/srv/plastic-promise/source.py", {}, "remediation_canary_unredacted_material"),
        (
            "",
            {"prompt": "include the original conversation"},
            "remediation_canary_unredacted_material",
        ),
    ],
)
def test_canary_evidence_rejects_secrets_and_raw_material(
    tmp_path,
    reason,
    evidence,
    error_code,
):
    store, shadowed = _shadowed_candidate(tmp_path)

    with pytest.raises(ValueError, match=error_code):
        store.record_shadow_canary(
            candidate_id=shadowed.candidate_id,
            passed=False,
            reason=reason or "failed",
            evidence=evidence,
        )


def test_shadow_canary_receipt_is_project_and_generation_bound(tmp_path):
    store, shadowed = _shadowed_candidate(tmp_path)
    receipt = {
        "generation_id": "shadow:security:v1",
        "manifest_hash": "a" * 64,
        "benchmark_corpus_sha256": "b" * 64,
        "benchmark_cases_sha256": "c" * 64,
        "language_split": ["en", "zh"],
        "embedding_identity": "cloud:test-embedding:v1:1024",
        "metrics": {
            "hit_at_1": 0.8,
            "hit_at_5": 1.0,
            "mrr": 0.9,
            "p95_ms": 120.0,
            "forbidden_hit_rate": 0.0,
            "conflict_rate": 0.05,
            "cost_usd": 0.01,
            "sample_count": 20,
        },
        "observed_from": "2026-08-05T00:00:00Z",
        "observed_until": "2026-08-05T01:00:00Z",
    }

    passed = store.record_shadow_canary(
        candidate_id=shadowed.candidate_id,
        passed=True,
        receipt=receipt,
    )
    assert passed.status == "canary_passed"
    conn = sqlite3.connect(tmp_path / "memory.db")
    stored = conn.execute(
        "SELECT project_id, generation_id, manifest_hash, language_split_json, metrics_json "
        "FROM security_shadow_canary_receipts WHERE candidate_id = ?",
        (shadowed.candidate_id,),
    ).fetchone()
    assert stored[0:3] == ("project:alpha", "shadow:security:v1", "a" * 64)
    assert json.loads(stored[3]) == ["en", "zh"]
    assert json.loads(stored[4])["sample_count"] == 20
    conn.close()


def test_generation_quality_report_adapts_to_canary_receipt(tmp_path):
    store, shadowed = _shadowed_candidate(tmp_path)
    passed = store.record_generation_quality_canary(
        candidate_id=shadowed.candidate_id,
        manifest=_quality_manifest(),
        conflict_rate=0.05,
        passed=True,
    )

    assert passed.status == "canary_passed"
    conn = sqlite3.connect(tmp_path / "memory.db")
    metrics = conn.execute(
        "SELECT metrics_json FROM security_shadow_canary_receipts WHERE candidate_id = ?",
        (shadowed.candidate_id,),
    ).fetchone()[0]
    assert json.loads(metrics)["cost_usd"] == 0.25
    assert json.loads(metrics)["conflict_rate"] == 0.05
    conn.close()


def test_generation_quality_receipt_binds_source_dependencies_and_cost_currency(tmp_path):
    store, shadowed = _shadowed_candidate(tmp_path)
    manifest = _quality_manifest()
    report = manifest["quality_report"]
    report["usage"].update(cost=0.25, cost_currency="USD")
    report["environment"].update(
        dependencies_sha256="e" * 64,
        source_fingerprint="f" * 64,
    )

    passed = store.record_generation_quality_canary(
        candidate_id=shadowed.candidate_id,
        manifest=manifest,
        conflict_rate=0.05,
        passed=True,
    )

    assert passed.status == "canary_passed"
    conn = sqlite3.connect(tmp_path / "memory.db")
    stored = conn.execute(
        "SELECT dependency_digest, source_fingerprint, cost_currency "
        "FROM security_shadow_canary_receipts WHERE candidate_id = ?",
        (shadowed.candidate_id,),
    ).fetchone()
    assert stored == ("e" * 64, "f" * 64, "USD")
    conn.close()


def test_production_gate_requires_complete_receipt_and_survives_restart(tmp_path):
    store, shadowed = _shadowed_candidate(tmp_path)
    legacy_store, legacy_shadowed = _shadowed_candidate(tmp_path / "legacy")
    legacy_store.record_shadow_canary(
        candidate_id=legacy_shadowed.candidate_id,
        passed=True,
    )
    with pytest.raises(ValueError, match="production_canary_receipt_required"):
        legacy_store.require_production_canary_receipt(
            candidate_id=legacy_shadowed.candidate_id,
            project_id="project:alpha",
            generation_id="shadow:security:v1",
            manifest_hash="a" * 64,
        )

    manifest = _quality_manifest()
    report = manifest["quality_report"]
    report["usage"].update(cost=0.25, cost_currency="USD")
    report["environment"].update(
        dependencies_sha256="e" * 64,
        source_fingerprint="f" * 64,
    )
    store.record_generation_quality_canary(
        candidate_id=shadowed.candidate_id,
        manifest=manifest,
        conflict_rate=0.05,
        passed=True,
    )

    restarted = ShieldScanStore(tmp_path / "memory.db")
    receipt = restarted.require_production_canary_receipt(
        candidate_id=shadowed.candidate_id,
        project_id="project:alpha",
        generation_id="shadow:security:v1",
        manifest_hash="a" * 64,
    )
    assert receipt.project_id == "project:alpha"
    assert receipt.dependency_digest == "e" * 64
    assert receipt.source_fingerprint == "f" * 64
    assert receipt.cost_currency == "USD"


def test_production_gate_rejects_cross_project_receipt_reuse(tmp_path):
    store, shadowed = _shadowed_candidate(tmp_path)
    manifest = _quality_manifest()
    report = manifest["quality_report"]
    report["usage"].update(cost=0.25, cost_currency="USD")
    report["environment"].update(
        dependencies_sha256="e" * 64,
        source_fingerprint="f" * 64,
    )
    store.record_generation_quality_canary(
        candidate_id=shadowed.candidate_id,
        manifest=manifest,
        conflict_rate=0.05,
        passed=True,
    )

    with pytest.raises(ValueError, match="production_canary_project_mismatch"):
        store.require_production_canary_receipt(
            candidate_id=shadowed.candidate_id,
            project_id="project:other",
            generation_id="shadow:security:v1",
            manifest_hash="a" * 64,
        )


def test_canary_receipt_replay_is_idempotent_and_conflicts_fail_closed(tmp_path):
    store, shadowed = _shadowed_candidate(tmp_path)
    manifest = _quality_manifest()
    report = manifest["quality_report"]
    report["usage"].update(cost=0.25, cost_currency="USD")
    report["environment"].update(
        dependencies_sha256="e" * 64,
        source_fingerprint="f" * 64,
    )
    first = store.record_generation_quality_canary(
        candidate_id=shadowed.candidate_id,
        manifest=manifest,
        conflict_rate=0.05,
        passed=True,
    )
    replay = store.record_generation_quality_canary(
        candidate_id=shadowed.candidate_id,
        manifest=manifest,
        conflict_rate=0.05,
        passed=True,
    )
    assert replay == first

    conflicting = _quality_manifest()
    conflicting["quality_report"]["environment"].update(
        dependencies_sha256="1" * 64,
        source_fingerprint="f" * 64,
    )
    with pytest.raises(ValueError, match="remediation_canary_replay_conflict"):
        store.record_generation_quality_canary(
            candidate_id=shadowed.candidate_id,
            manifest=conflicting,
            conflict_rate=0.05,
            passed=True,
        )


def test_invalid_receipt_rolls_back_candidate_and_generation_state(tmp_path):
    store, shadowed = _shadowed_candidate(tmp_path)
    receipt = {
        "generation_id": "shadow:security:v1",
        "manifest_hash": "a" * 64,
        "benchmark_corpus_sha256": "b" * 64,
        "benchmark_cases_sha256": "c" * 64,
        "language_split": ["en", "zh"],
        "embedding_identity": "cloud:test-embedding:v1:1024",
        "metrics": {
            "hit_at_1": 0.8,
            "hit_at_5": 1.0,
            "mrr": 0.9,
            "p95_ms": 120.0,
            "forbidden_hit_rate": 0.0,
            "conflict_rate": 0.05,
            "cost_usd": 0.01,
            "sample_count": 20,
        },
        "observed_from": "2026-08-05T00:00:00Z",
        "observed_until": "2026-08-05T01:00:00Z",
        "dependency_digest": "not-a-digest",
    }
    with pytest.raises(ValueError, match="remediation_canary_receipt_dependency_digest_invalid"):
        store.record_shadow_canary(
            candidate_id=shadowed.candidate_id,
            passed=True,
            receipt=receipt,
        )

    conn = sqlite3.connect(tmp_path / "memory.db")
    assert (
        conn.execute(
            "SELECT status FROM security_remediation_candidates WHERE candidate_id = ?",
            (shadowed.candidate_id,),
        ).fetchone()[0]
        == "shadowed"
    )
    assert (
        conn.execute(
            "SELECT status FROM security_shadow_generations WHERE generation_id = ?",
            ("shadow:security:v1",),
        ).fetchone()[0]
        == "shadow"
    )
    assert conn.execute("SELECT COUNT(*) FROM security_shadow_canary_receipts").fetchone()[0] == 0
    conn.close()


def test_receipt_schema_migrates_legacy_columns_without_data_loss(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE security_shadow_canary_receipts (
            receipt_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            benchmark_corpus_sha256 TEXT NOT NULL,
            benchmark_cases_sha256 TEXT NOT NULL,
            language_split_json TEXT NOT NULL,
            embedding_identity TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            observed_from TEXT NOT NULL,
            observed_until TEXT NOT NULL,
            environment_fingerprint TEXT NOT NULL DEFAULT '',
            pricing_revision TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(candidate_id, generation_id, manifest_hash, observed_from, observed_until)
        );
        INSERT INTO security_shadow_canary_receipts VALUES (
            'receipt-legacy', 'candidate-legacy', 'project:legacy', 'generation-legacy',
            'a', 'b', 'c', '["en","zh"]', 'embedder',
            '{"sample_count":1}', '2026-08-05T00:00:00Z', '2026-08-05T01:00:00Z',
            '', 'pricing-v1', '2026-08-05T01:00:00Z'
        );
        """
    )
    conn.commit()
    conn.close()

    ShieldScanStore(db_path)
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(security_shadow_canary_receipts)")}
    legacy = conn.execute(
        "SELECT receipt_id, project_id, pricing_revision "
        "FROM security_shadow_canary_receipts WHERE receipt_id = 'receipt-legacy'"
    ).fetchone()
    assert {"dependency_digest", "source_fingerprint", "cost_currency"}.issubset(columns)
    assert legacy == ("receipt-legacy", "project:legacy", "pricing-v1")
    conn.close()


@pytest.mark.parametrize(
    ("manifest_patch", "error_code"),
    [
        (
            {"benchmark_corpus_sha256": "d" * 64},
            "remediation_generation_quality_benchmark_mismatch",
        ),
        (
            {"embedding_model": "other-model"},
            "remediation_generation_quality_embedding_mismatch",
        ),
        (
            {
                "quality_report": {
                    "metrics": {
                        "case_count": 20,
                        "hit_at": {"1": 0.8, "5": 1.0},
                        "mrr": 0.9,
                        "p95_ms": 120.0,
                        "forbidden_hit_rate": 0.1,
                        "language": {"en": {}, "zh": {}},
                    }
                }
            },
            "remediation_generation_quality_forbidden_hit",
        ),
        (
            {"quality_report": {"environment": {}}},
            "remediation_generation_quality_environment_invalid",
        ),
        (
            {"quality_report": {"usage": {"cost_usd": 0.25}}},
            "remediation_generation_quality_pricing_invalid",
        ),
    ],
)
def test_generation_quality_report_rejects_unbound_or_unsafe_evidence(
    tmp_path,
    manifest_patch,
    error_code,
):
    store, shadowed = _shadowed_candidate(tmp_path)
    manifest = _quality_manifest(**manifest_patch)

    with pytest.raises(ValueError, match=error_code):
        store.record_generation_quality_canary(
            candidate_id=shadowed.candidate_id,
            manifest=manifest,
            conflict_rate=0.05,
            passed=True,
        )


@pytest.mark.parametrize(
    ("patch", "error_code"),
    [
        ({"manifest_hash": "d" * 64}, "remediation_canary_receipt_manifest_mismatch"),
        ({"language_split": ["en"]}, "remediation_canary_receipt_language_split_invalid"),
        (
            {"metrics": {"hit_at_1": 2.0}},
            "remediation_canary_receipt_metrics_incomplete",
        ),
    ],
)
def test_shadow_canary_receipt_rejects_unbound_or_incomplete_evidence(
    tmp_path,
    patch,
    error_code,
):
    store, shadowed = _shadowed_candidate(tmp_path)
    receipt = {
        "generation_id": "shadow:security:v1",
        "manifest_hash": "a" * 64,
        "benchmark_corpus_sha256": "b" * 64,
        "benchmark_cases_sha256": "c" * 64,
        "language_split": ["en", "zh"],
        "embedding_identity": "cloud:test-embedding:v1:1024",
        "metrics": {
            "hit_at_1": 0.8,
            "hit_at_5": 1.0,
            "mrr": 0.9,
            "p95_ms": 120.0,
            "forbidden_hit_rate": 0.0,
            "conflict_rate": 0.05,
            "cost_usd": 0.01,
            "sample_count": 20,
        },
        "observed_from": "2026-08-05T00:00:00Z",
        "observed_until": "2026-08-05T01:00:00Z",
    }
    receipt.update(patch)

    with pytest.raises(ValueError, match=error_code):
        store.record_shadow_canary(
            candidate_id=shadowed.candidate_id,
            passed=True,
            receipt=receipt,
        )


def test_passed_candidate_enters_existing_scoring_projection_only_as_pending(tmp_path):
    store, candidate = _candidate(tmp_path)
    conn = sqlite3.connect(tmp_path / "memory.db")
    ensure_memory_proposal_schema(conn)
    ensure_proposal_automation_schema(conn)
    link = ShieldCandidateProposalBridge(conn).link(candidate)
    conn.commit()

    assert link.created is True
    row = MemoryProposalStore(conn).get(link.proposal_id)
    assert row is not None
    assert row["status"] == "pending"
    assert row["origin_role"] == "system"
    assert row["metadata"]["security_candidate_id"] == candidate.candidate_id
    assert link.score.score.observation_count == 0
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
        ).fetchone()
        is None
    )

    attached = store.attach_shadow_proposal(
        candidate_id=candidate.candidate_id,
        proposal_id=link.proposal_id,
    )
    assert attached.shadow_proposal_id == link.proposal_id
    conn.close()


@pytest.mark.parametrize(
    ("metadata_patch", "error_code"),
    [
        ({"security_candidate_id": "rpc:other"}, "remediation_shadow_proposal_provenance_mismatch"),
        (
            {"shadow_generation": "shadow:security:other"},
            "remediation_shadow_proposal_generation_mismatch",
        ),
    ],
)
def test_shadow_proposal_binding_rejects_forged_provenance(
    tmp_path,
    metadata_patch,
    error_code,
):
    store, candidate = _candidate(tmp_path)
    conn = sqlite3.connect(tmp_path / "memory.db")
    ensure_memory_proposal_schema(conn)
    ensure_proposal_automation_schema(conn)
    link = ShieldCandidateProposalBridge(conn).link(candidate)
    conn.commit()
    current = conn.execute(
        "SELECT metadata_json FROM memory_proposals WHERE proposal_id = ?",
        (link.proposal_id,),
    ).fetchone()
    metadata = json.loads(current[0])
    metadata.update(metadata_patch)
    conn.execute(
        "UPDATE memory_proposals SET metadata_json = ? WHERE proposal_id = ?",
        (json.dumps(metadata, sort_keys=True), link.proposal_id),
    )
    conn.commit()

    with pytest.raises(ValueError, match=error_code):
        store.attach_shadow_proposal(
            candidate_id=candidate.candidate_id,
            proposal_id=link.proposal_id,
        )
    conn.close()


def test_shadow_proposal_binding_rejects_foreign_project(tmp_path):
    store, candidate = _candidate(tmp_path)
    conn = sqlite3.connect(tmp_path / "memory.db")
    ensure_memory_proposal_schema(conn)
    ensure_proposal_automation_schema(conn)
    link = ShieldCandidateProposalBridge(conn).link(candidate)
    conn.execute(
        "UPDATE memory_proposals SET project_id = ? WHERE proposal_id = ?",
        ("project:beta", link.proposal_id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="remediation_shadow_proposal_scope_mismatch"):
        store.attach_shadow_proposal(
            candidate_id=candidate.candidate_id,
            proposal_id=link.proposal_id,
        )
    conn.close()


def test_vector_evidence_reuses_existing_proposal_pipeline(tmp_path):
    store, candidate = _candidate(tmp_path)
    conn = sqlite3.connect(tmp_path / "memory.db")
    ensure_memory_proposal_schema(conn)
    ensure_proposal_automation_schema(conn)
    link = ShieldCandidateProposalBridge(conn).link(candidate)
    conn.commit()
    attached = store.attach_shadow_proposal(
        candidate_id=candidate.candidate_id,
        proposal_id=link.proposal_id,
    )

    class Embedder:
        model_name = "test-embedding"
        index_model_name = "test-embedding"

        def embed(self, _text):
            return [1.0, 0.0]

    class LanceDB:
        def search_similar(self, _vector, k=12):
            assert k == 12
            return []

    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
    engine = SimpleNamespace(
        _sqlite=SimpleNamespace(_conn=conn),
        _embedder=Embedder(),
        _ldb=LanceDB(),
        _principle_anchors={},
    )

    result = ShieldCandidateProposalBridge.collect_vector_evidence(
        engine,
        attached,
        query="bounded operation",
    )

    assert result["status"] == "recorded"
    assert result["candidate_id"] == candidate.candidate_id
    assert result["signals"]["query_similarity"] == 1.0
    conn.close()


def test_vector_evidence_batch_rejects_cross_project_candidates(tmp_path):
    store, candidate = _candidate(tmp_path)
    foreign = replace(candidate, candidate_id="rpc_foreign", project_id="project:beta")

    with pytest.raises(ProposalPolicyError, match="security_candidate_batch_scope_mismatch"):
        ShieldCandidateProposalBridge.collect_vector_evidence_batch(
            object(),
            [candidate, foreign],
        )


def test_vector_evidence_batch_reuses_pipeline_and_keeps_candidate_provenance(tmp_path):
    store, candidate = _candidate(tmp_path)
    conn = sqlite3.connect(tmp_path / "memory.db")
    ensure_memory_proposal_schema(conn)
    ensure_proposal_automation_schema(conn)
    link = ShieldCandidateProposalBridge(conn).link(candidate)
    conn.commit()
    attached = store.attach_shadow_proposal(
        candidate_id=candidate.candidate_id,
        proposal_id=link.proposal_id,
    )

    class Embedder:
        model_name = "test-embedding"
        index_model_name = "test-embedding"

        def embed_batch(self, texts):
            return [[1.0, 0.0] for _text in texts]

    class LanceDB:
        def search_similar(self, _vector, k=12):
            assert k == 12
            return []

    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
    engine = SimpleNamespace(
        _sqlite=SimpleNamespace(_conn=conn),
        _embedder=Embedder(),
        _ldb=LanceDB(),
        _principle_anchors={},
    )

    results = ShieldCandidateProposalBridge.collect_vector_evidence_batch(
        engine,
        [attached],
        query_by_candidate_id={attached.candidate_id: "bounded operation"},
    )

    assert len(results) == 1
    assert results[0]["status"] == "recorded"
    assert results[0]["candidate_id"] == attached.candidate_id
    assert results[0]["project_id"] == "project:alpha"
    assert results[0]["signals"]["query_similarity"] == 1.0
    conn.close()
