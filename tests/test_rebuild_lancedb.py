from __future__ import annotations

import json
import os
import platform
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from plastic_promise.core.lancedb_generation import (
    QUALITY_GATE_POLICY,
    QUALITY_REPORT_SCHEMA,
    RECALL_QUALITY_DATASET_SCHEMA,
    RECALL_QUALITY_REPORT_SCHEMA,
    ArtifactVerification,
    ArtifactVerificationRequest,
)
from plastic_promise.core.memory_index import (
    build_index_material,
    index_metadata,
    read_persisted_index_material,
)
from scripts import rebuild_lancedb

MODEL = "text-embedding-v4"
REVISION = "2026-07-23"
DIMENSION = 1024


def _persisted_memory_row(
    memory_id: str,
    content: str,
    *,
    policy: str = "compact-v2",
) -> dict[str, str]:
    material = build_index_material(
        {"content": content, "l0_abstract": content},
        policy=policy,
        model_name="unknown",
    )
    return {
        "id": memory_id,
        "content": content,
        "embedding_text": material.vector_text,
        "embedding_hash": material.embedding_hash,
        "search_text": material.search_text,
        "metadata_json": json.dumps(
            {"memory_index": index_metadata(material)},
            sort_keys=True,
        ),
    }


def _metric_slice(case_count: int) -> dict[str, Any]:
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


def _normalized_quality_report() -> dict[str, Any]:
    return {
        "schema": QUALITY_REPORT_SCHEMA,
        "benchmark": {
            "report_schema": RECALL_QUALITY_REPORT_SCHEMA,
            "dataset_schema": RECALL_QUALITY_DATASET_SCHEMA,
            "dataset_revision": "2026-07-23",
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
            "model": MODEL,
            "revision": REVISION,
            "dimension": DIMENSION,
        },
        "cases": {"sha256": "b" * 64, "count": 6},
        "corpus": {
            "sha256": "c" * 64,
            "count": 12,
            "revision": "fixed-corpus-v1",
            "provenance_revision": "fixed-provenance-v1",
        },
        "environment": {
            "source_commit": "d" * 40,
            "source_fingerprint": "e" * 64,
            "configuration_sha256": "f" * 64,
            "dependencies_sha256": "1" * 64,
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
            "embedding_requests": 4,
            "embedding_input_tokens": 120,
            "cost_usd": 0.25,
            "pricing_revision": "pricing-v1",
        },
        "metrics": {
            **_metric_slice(6),
            "language": {
                "en": _metric_slice(2),
                "zh": _metric_slice(2),
                "cross-lingual": _metric_slice(2),
            },
        },
    }


def test_runtime_embedder_identity_rejects_stale_cloud_endpoint(monkeypatch):
    """A reused singleton must not write vectors after endpoint reconfiguration."""

    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_MODEL", MODEL)
    monkeypatch.setenv("EMBEDDER_MODEL_REVISION", REVISION)
    monkeypatch.setenv("PP_EMBEDDING_DIM", str(DIMENSION))
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://endpoint-a.example/v1")
    from plastic_promise.core.memory_index import effective_embedding_model_name

    stale_index_identity = effective_embedding_model_name()

    class _StaleEmbedder:
        model_name = MODEL
        index_model_name = stale_index_identity
        _model_revision = REVISION
        dim = DIMENSION

    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://endpoint-b.example/v1")

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="^runtime_embedding_identity_environment_mismatch$",
    ):
        rebuild_lancedb._runtime_embedder_identity(_StaleEmbedder())


def _create_source_database(
    path: Path,
    *,
    memory_count: int,
    include_outbox_job: bool = True,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE memories ("
            "id TEXT PRIMARY KEY, content TEXT NOT NULL, embedding_text TEXT NOT NULL, "
            "embedding_hash TEXT NOT NULL, search_text TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO memories ("
            "id, content, embedding_text, embedding_hash, search_text, metadata_json"
            ") VALUES ("
            ":id, :content, :embedding_text, :embedding_hash, :search_text, :metadata_json"
            ")",
            [
                _persisted_memory_row(f"memory-{index:04d}", f"content {index}")
                for index in range(memory_count)
            ],
        )
        connection.execute(
            "CREATE TABLE store_outbox ("
            "outbox_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, status TEXT NOT NULL)"
        )
        if include_outbox_job:
            connection.execute(
                "INSERT INTO store_outbox (outbox_id, tool_name, status) VALUES (?, ?, ?)",
                ("pending-index-job", "memory_index", "pending"),
            )
        connection.commit()
    finally:
        connection.close()


def _source_state(path: Path) -> tuple[bytes, int, int, int]:
    entry = path.stat()
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row_count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        connection.close()
    return path.read_bytes(), entry.st_mtime_ns, entry.st_size, row_count


def _fake_artifact_verifier(
    request: ArtifactVerificationRequest,
) -> ArtifactVerification:
    descriptor = os.open(
        "artifact.json",
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=request.index_fd,
    )
    try:
        payload = json.loads(os.read(descriptor, 64 * 1024).decode("utf-8"))
    finally:
        os.close(descriptor)
    return ArtifactVerification(
        row_count=payload["row_count"],
        index_schema=request.index_schema,
        embedding_model=request.embedding_model,
        model_revision=request.model_revision,
        embedding_dimension=request.embedding_dimension,
        index_material_sha256=payload["index_material_sha256"],
    )


@dataclass
class FakeState:
    behavior: str = "success"
    engine_paths: list[Path] = field(default_factory=list)
    index_paths: list[Path] = field(default_factory=list)
    rebuild_limits: list[int] = field(default_factory=list)
    backfill_calls: int = 0


class _FakeEmbedder:
    model_name = MODEL
    dim = DIMENSION
    _model_revision = REVISION

    def close(self) -> None:
        return None


def _install_shadow_fakes(monkeypatch: pytest.MonkeyPatch, state: FakeState) -> None:
    class FakeSQLite:
        def __init__(self, path: Path) -> None:
            self._conn = sqlite3.connect(path)

    class FakeEngine:
        def __init__(self) -> None:
            path = Path(os.environ["PLASTIC_DB_PATH"])
            state.engine_paths.append(path)
            self._sqlite = FakeSQLite(path)
            self._sqlite._conn.execute(
                "CREATE TABLE IF NOT EXISTS clone_only_schema (value TEXT NOT NULL)"
            )
            self._sqlite._conn.commit()

    class FakeStore:
        def __init__(self, index_path: Path, _embedder: object) -> None:
            self.path = index_path
            self.ids: set[str] = set()
            self._index_failures: list[dict[str, str]] = []
            state.index_paths.append(index_path)

        def rebuild_all(self, engine: object) -> int:
            source_memories = eligible(engine)
            source_ids = sorted(source_memories)
            limit = int(os.environ.get("LDB_REBUILD_MAX_PER_CALL", "200"))
            state.rebuild_limits.append(limit)
            selected = source_ids[:limit]
            reported = len(selected)
            if state.behavior in {"partial", "embed-failure", "transient-partial"}:
                selected = selected[:-1]
                reported = len(selected)
            elif state.behavior == "backend-mismatch":
                selected = selected[:-1]
                reported = len(source_ids)
            elif state.behavior == "index-failure":
                self._index_failures.append(
                    {"memory_id": source_ids[-1], "reason": "embedding_failed"}
                )
            self.ids = set(selected)
            self._write_artifact(source_memories)
            return reported

        def backfill(self, engine: object) -> int:
            state.backfill_calls += 1
            source_memories = eligible(engine)
            if state.behavior != "transient-partial":
                return 0
            before = len(self.ids)
            self.ids.update(source_memories)
            self._index_failures = []
            self._write_artifact(source_memories)
            return len(self.ids) - before

        def _write_artifact(self, source_memories: dict[str, dict[str, str]]) -> None:
            source_ids = sorted(source_memories)
            first_material = read_persisted_index_material(source_memories[source_ids[0]])
            assert first_material is not None
            material_sha256 = rebuild_lancedb._source_index_material_sha256(
                {memory_id: source_memories[memory_id] for memory_id in self.ids},
                expected_policy=first_material.policy,
            )
            (self.path / "artifact.json").write_text(
                json.dumps(
                    {
                        "row_count": len(self.ids),
                        "ids": sorted(self.ids),
                        "index_material_sha256": material_sha256,
                    }
                ),
                encoding="utf-8",
            )

        def count_rows(self) -> int:
            return len(self.ids)

        def list_memory_ids(self) -> set[str]:
            return set(self.ids)

    def eligible(engine: object) -> dict[str, dict[str, str]]:
        rows = engine._sqlite._conn.execute(
            "SELECT id, content, embedding_text, embedding_hash, search_text, metadata_json "
            "FROM memories ORDER BY id"
        ).fetchall()
        return {
            str(row[0]): {
                "id": str(row[0]),
                "content": str(row[1]),
                "embedding_text": str(row[2]),
                "embedding_hash": str(row[3]),
                "search_text": str(row[4]),
                "metadata_json": str(row[5]),
            }
            for row in rows
        }

    monkeypatch.setattr(rebuild_lancedb, "_create_context_engine", FakeEngine)
    monkeypatch.setattr(rebuild_lancedb, "_get_embedder", _FakeEmbedder)
    monkeypatch.setattr(
        rebuild_lancedb,
        "_create_lancedb_store",
        lambda path, embedder: FakeStore(path, embedder),
    )
    monkeypatch.setattr(rebuild_lancedb, "_eligible_memories", eligible)
    monkeypatch.setattr(
        rebuild_lancedb,
        "_normalize_quality_report",
        lambda _raw, **_kwargs: _normalized_quality_report(),
    )


def _generation_root(path: Path) -> tuple[Path, Path]:
    root = path / "generation-root"
    root.mkdir(mode=0o700)
    generations = root / "generations"
    generations.mkdir(mode=0o700)
    active = generations / "active"
    active.mkdir(mode=0o700)
    active_index = active / "index"
    active_index.mkdir(mode=0o700)
    sentinel = active_index / "sentinel.txt"
    sentinel.write_text("active remains immutable", encoding="utf-8")
    (root / "current").symlink_to("generations/active")
    return root, sentinel


def _quality_report_file(path: Path) -> Path:
    report = path / "quality-report.json"
    report.write_text("{}\n", encoding="utf-8")
    return report


def _runtime_bound_v2_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Path]:
    root = tmp_path / "runtime-checkout"
    dataset = root / "tests" / "fixtures" / "heldout.json"
    source = root / "plastic_promise" / "feature.py"
    for path, content in (
        (root / "scripts" / "benchmark_recall_quality.py", "benchmark = 1\n"),
        (root / "scripts" / "http_mcp_harness.py", "harness = 1\n"),
        (root / "scripts" / "manage_lancedb_generations.py", "manage = 1\n"),
        (root / "scripts" / "rebuild_lancedb.py", "rebuild = 1\n"),
        (root / "pyproject.toml", "[project]\nname = 'fixture'\n"),
        (root / "plastic_promise" / "__init__.py", ""),
        (source, "feature = 1\n"),
        (root / "rust" / "context-engine-core" / "Cargo.toml", "[package]\nname = 'fixture'\n"),
        (root / "rust" / "context-engine-core" / "Cargo.lock", "# fixture lock\n"),
        (root / "rust" / "context-engine-core" / "build.rs", "fn main() {}\n"),
        (root / "rust" / "context-engine-core" / "src" / "lib.rs", "pub fn fixture() {}\n"),
        (dataset, "{}\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    commit = "a" * 40
    dependencies = {"lancedb": "0.34.0", "pyarrow": "20.0.0"}
    monkeypatch.setattr(rebuild_lancedb, "ROOT", root)
    monkeypatch.setattr(rebuild_lancedb, "recall_quality_source_commit", lambda _root: commit)
    monkeypatch.setattr(
        rebuild_lancedb,
        "retrieval_dependency_versions",
        lambda: dict(dependencies),
    )
    for name, value in {
        "EMBEDDER_PROVIDER": "cloud",
        "EMBEDDER_MODEL": "text-embedding-v4",
        "EMBEDDER_MODEL_REVISION": "revision-a",
        "EMBEDDER_DIMENSION": "1024",
        "PP_MEMORY_INDEX_TEXT_POLICY": "legacy",
        "PP_RETRIEVAL_FUSION_POLICY": "max-v1",
        "PP_FORCE_PYTHON_SUPPLY": "1",
        "PP_PREFER_RUST_SUPPLY": "0",
        "PP_VECTOR_WEIGHT": "0.50",
        "PP_QUERY_EXPANSION": "1",
        "PP_FTS_DISABLED": "0",
        "PP_FTS_FUSION": "1",
    }.items():
        monkeypatch.setenv(name, value)
    source_paths = rebuild_lancedb.recall_quality_source_paths(root, dataset)
    report = {
        "benchmark": {
            "report_schema": RECALL_QUALITY_REPORT_SCHEMA,
            "candidate_dimension": "fusion_policy",
        },
        "backend": {
            "provider": "openai-compatible",
            "requested_policy": "max-v1",
            "requested_runtime": "python",
            "effective_runtime": "python",
            "rust_runtime": None,
        },
        "environment": {
            "source_commit": commit,
            "code_revision": commit,
            "dirty_fingerprint": rebuild_lancedb.recall_quality_code_fingerprint(root),
            "environment_fingerprint": (rebuild_lancedb.recall_quality_environment_fingerprint()),
            "comparison_environment_fingerprint": (
                rebuild_lancedb.recall_quality_comparison_environment_fingerprint()
            ),
            "source_fingerprint": rebuild_lancedb.recall_quality_source_fingerprint(root, dataset),
            "source_files": [
                rebuild_lancedb.recall_quality_source_label(root, path) for path in source_paths
            ],
            "dataset_source": "tests/fixtures/heldout.json",
            "dependencies": dict(dependencies),
            "embedding_configuration": {
                "provider": "openai-compatible",
                "model": "text-embedding-v4",
                "model_revision": "revision-a",
                "dimension": 1024,
            },
            "retrieval_configuration": {
                "index_text_policy": "legacy",
                "PP_VECTOR_WEIGHT": "0.50",
                "PP_QUERY_EXPANSION": "1",
                "PP_FTS_DISABLED": "0",
                "PP_FTS_FUSION": "1",
            },
            "supply_runtime": "python",
            "runtime": {
                "os": platform.system(),
                "os_release": platform.release(),
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
                "machine": platform.machine(),
            },
        },
    }
    return report, source


def test_v2_runtime_environment_accepts_exact_checkout_and_dependencies(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)

    rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_live_runtime_environment_normalizes_isolated_benchmark_overrides(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    report["backend"].update(
        mode="live",
        index_text_policy="legacy",
        effective_policy="max-v1",
    )
    evidence_environment = rebuild_lancedb._quality_report_fingerprint_environment(report)
    assert evidence_environment is not None
    report["environment"]["environment_fingerprint"] = (
        rebuild_lancedb.recall_quality_environment_fingerprint(evidence_environment)
    )
    report["environment"]["comparison_environment_fingerprint"] = (
        rebuild_lancedb.recall_quality_comparison_environment_fingerprint(evidence_environment)
    )

    rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_live_configuration_normalizes_benchmark_overrides_but_binds_embedding(
    tmp_path,
    monkeypatch,
):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    report["backend"].update(
        mode="live",
        index_text_policy="legacy",
        effective_policy="max-v1",
    )
    evidence_environment = rebuild_lancedb._quality_report_fingerprint_environment(report)
    assert evidence_environment is not None
    report["environment"]["environment_fingerprint"] = (
        rebuild_lancedb.recall_quality_environment_fingerprint(evidence_environment)
    )
    report["environment"]["comparison_environment_fingerprint"] = (
        rebuild_lancedb.recall_quality_comparison_environment_fingerprint(evidence_environment)
    )

    # This is the staged production profile.  The live benchmark normalizes
    # these controls back to legacy/max-v1/python without changing embedding.
    monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "structure-v1")
    monkeypatch.setenv("PP_RETRIEVAL_FUSION_POLICY", "rust-auto")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "0")
    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")

    rebuild_lancedb._assert_quality_report_runtime_environment(report)

    monkeypatch.setenv("EMBEDDER_MODEL_REVISION", "revision-b")
    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_embedding_configuration_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_live_runtime_environment_still_rejects_provider_drift(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    report["backend"].update(
        mode="live",
        index_text_policy="legacy",
        effective_policy="max-v1",
    )
    evidence_environment = rebuild_lancedb._quality_report_fingerprint_environment(report)
    assert evidence_environment is not None
    report["environment"]["environment_fingerprint"] = (
        rebuild_lancedb.recall_quality_environment_fingerprint(evidence_environment)
    )
    report["environment"]["comparison_environment_fingerprint"] = (
        rebuild_lancedb.recall_quality_comparison_environment_fingerprint(evidence_environment)
    )
    monkeypatch.setenv("PP_RERANK_CLOUD_MODEL_REVISION", "revision-b")

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_environment_fingerprint_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def _use_rust_runtime(report: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    identity = {
        "module": "context_engine_core",
        "version": "0.1.0",
        "binary_sha256": "1" * 64,
        "source_sha256": "2" * 64,
    }
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "0")
    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")
    monkeypatch.setenv("PP_RETRIEVAL_FUSION_POLICY", "legacy-auto")
    report["backend"].update(
        requested_policy="legacy-auto",
        requested_runtime="rust",
        effective_runtime="rust",
        rust_runtime=dict(identity),
    )
    report["environment"]["supply_runtime"] = "auto"
    report["environment"]["environment_fingerprint"] = (
        rebuild_lancedb.recall_quality_environment_fingerprint()
    )
    report["environment"]["comparison_environment_fingerprint"] = (
        rebuild_lancedb.recall_quality_comparison_environment_fingerprint()
    )
    return identity


def test_v2_runtime_environment_accepts_exact_rust_binary_and_source(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    identity = _use_rust_runtime(report, monkeypatch)
    monkeypatch.setattr(
        rebuild_lancedb,
        "loaded_rust_extension_identity",
        lambda _root: dict(identity),
    )

    rebuild_lancedb._assert_quality_report_runtime_environment(report)


@pytest.mark.parametrize("field", ["binary_sha256", "source_sha256", "version"])
def test_v2_runtime_environment_rejects_changed_rust_build_identity(tmp_path, monkeypatch, field):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    identity = _use_rust_runtime(report, monkeypatch)
    current = dict(identity)
    current[field] = "9" * 64 if field.endswith("sha256") else "0.2.0"
    monkeypatch.setattr(
        rebuild_lancedb,
        "loaded_rust_extension_identity",
        lambda _root: current,
    )

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_rust_runtime_identity_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_runtime_environment_rejects_unavailable_rust_build_identity(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    _use_rust_runtime(report, monkeypatch)

    def unavailable(_root):
        raise ValueError("extension has no embedded source identity")

    monkeypatch.setattr(rebuild_lancedb, "loaded_rust_extension_identity", unavailable)
    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_rust_runtime_identity_unavailable",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_runtime_environment_rejects_rust_identity_for_python_runtime(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    report["backend"]["rust_runtime"] = {
        "module": "context_engine_core",
        "version": "0.1.0",
        "binary_sha256": "1" * 64,
        "source_sha256": "2" * 64,
    }

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_rust_runtime_unexpected",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_runtime_environment_rejects_nonsecret_environment_drift(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    monkeypatch.setenv("EMBEDDER_TIMEOUT", "37")

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_environment_fingerprint_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_runtime_environment_rejects_reranker_identity_drift(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    monkeypatch.setenv("PP_RERANK_CLOUD_MODEL_REVISION", "revision-b")

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_environment_fingerprint_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_runtime_environment_rejects_source_change(tmp_path, monkeypatch):
    report, source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    source.write_text("feature = 2\n", encoding="utf-8")

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_source_fingerprint_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


@pytest.mark.parametrize(
    "relative_path",
    ["scripts/manage_lancedb_generations.py", "scripts/rebuild_lancedb.py"],
)
def test_v2_runtime_environment_binds_generation_lifecycle_scripts(
    tmp_path,
    monkeypatch,
    relative_path,
):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    (rebuild_lancedb.ROOT / relative_path).write_text("changed = 1\n", encoding="utf-8")

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_source_fingerprint_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def test_v2_runtime_environment_uses_canonical_source_set(tmp_path, monkeypatch):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    report["environment"]["source_files"].remove("plastic_promise/feature.py")

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_source_files_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


@pytest.mark.parametrize("dependency", ["lancedb", "pyarrow"])
def test_v2_runtime_environment_rejects_dependency_change(tmp_path, monkeypatch, dependency):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    report["environment"]["dependencies"][dependency] = "999.0.0"

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="quality_report_dependencies_not_current",
    ):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


@pytest.mark.parametrize(
    ("environment_name", "value", "reason"),
    [
        ("EMBEDDER_MODEL_REVISION", "revision-b", "embedding_configuration_not_current"),
        ("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2", "retrieval_configuration_not_current"),
        ("PP_RETRIEVAL_FUSION_POLICY", "legacy-auto", "runtime_configuration_not_current"),
        ("PP_FORCE_PYTHON_SUPPLY", "0", "runtime_configuration_not_current"),
    ],
)
def test_v2_runtime_environment_rejects_target_configuration_drift(
    tmp_path,
    monkeypatch,
    environment_name,
    value,
    reason,
):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    monkeypatch.setenv(environment_name, value)

    with pytest.raises(rebuild_lancedb.ShadowBuildError, match=reason):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("source_commit", "quality_report_source_revision_not_current"),
        ("code_revision", "quality_report_source_revision_not_current"),
        ("dirty_fingerprint", "quality_report_dirty_fingerprint_not_current"),
    ],
)
def test_v2_runtime_environment_rejects_revision_or_code_change(
    tmp_path,
    monkeypatch,
    field,
    reason,
):
    report, _source = _runtime_bound_v2_report(tmp_path, monkeypatch)
    report["environment"][field] = "f" * len(report["environment"][field])

    with pytest.raises(rebuild_lancedb.ShadowBuildError, match=reason):
        rebuild_lancedb._assert_quality_report_runtime_environment(report)


def _arguments(root: Path, source: Path, report: Path, generation_id: str) -> list[str]:
    return [
        "--generation-root",
        str(root),
        "--generation-id",
        generation_id,
        "--source-db",
        str(source),
        "--quality-report",
        str(report),
    ]


def test_v2_max_v1_control_does_not_require_candidate_manifest(tmp_path, monkeypatch):
    report = tmp_path / "max-v1-heldout.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": RECALL_QUALITY_REPORT_SCHEMA,
                "candidate_id": "max-v1",
            }
        ),
        encoding="utf-8",
    )

    def reached_quality_validation(raw_report, *, candidate_manifest=None):
        assert raw_report["candidate_id"] == "max-v1"
        assert candidate_manifest is None
        raise rebuild_lancedb.ShadowBuildError("quality_validation_reached")

    monkeypatch.setattr(rebuild_lancedb, "_normalize_quality_report", reached_quality_validation)

    with pytest.raises(rebuild_lancedb.ShadowBuildError, match="quality_validation_reached"):
        rebuild_lancedb._shadow_generation_build(
            tmp_path / "generation-root",
            "max-v1-control",
            tmp_path / "source.db",
            report,
            None,
            artifact_verifier=_fake_artifact_verifier,
        )


def test_v2_wrrf_generation_still_requires_candidate_manifest(tmp_path):
    report = tmp_path / "wrrf-heldout.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": RECALL_QUALITY_REPORT_SCHEMA,
                "candidate_id": f"wrrf-v1:{'a' * 64}",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        rebuild_lancedb.ShadowBuildError,
        match="generation_mode_requires_candidate_manifest_for_v2",
    ):
        rebuild_lancedb._shadow_generation_build(
            tmp_path / "generation-root",
            "wrrf-candidate",
            tmp_path / "source.db",
            report,
            None,
            artifact_verifier=_fake_artifact_verifier,
        )


def test_generation_builds_all_rows_over_legacy_200_limit_without_promoting_or_mutating_source(
    tmp_path,
    monkeypatch,
    capsys,
):
    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=201, include_outbox_job=False)
    source_before = _source_state(source)
    root, active_sentinel = _generation_root(tmp_path)
    report = _quality_report_file(tmp_path)
    state = FakeState()
    _install_shadow_fakes(monkeypatch, state)
    monkeypatch.setenv("LDB_REBUILD_MAX_PER_CALL", "17")

    exit_code = rebuild_lancedb.main(
        _arguments(root, source, report, "candidate-201"),
        artifact_verifier=_fake_artifact_verifier,
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["generation"]["built_row_count"] == 201
    assert output["generation"]["source_row_count"] == 201
    assert output["promoted"] is False
    assert output["source_snapshot"]["sqlite_memory_rows"] == 201
    assert output["source_snapshot"]["eligible_index_rows"] == 201
    assert output["benchmark"]["corpus_sha256"] == "c" * 64
    assert output["outbox_reconciliation"] == {
        "active_snapshot_jobs": 0,
        "reason": "generation_manifest_has_no_outbox_snapshot_watermark",
        "reconciled": False,
        "required_action": (
            "after promotion, inspect and explicitly requeue only durable index jobs "
            "newer than a separately recorded cutover watermark"
        ),
        "snapshot_jobs": 0,
        "status": "unresolved",
        "status_counts": {},
    }
    assert state.rebuild_limits == [201]
    assert state.index_paths == [root / "generations" / "candidate-201" / "index"]
    assert os.environ["LDB_REBUILD_MAX_PER_CALL"] == "17"
    assert os.readlink(root / "current") == "generations/active"
    assert active_sentinel.read_text(encoding="utf-8") == "active remains immutable"
    assert _source_state(source) == source_before
    with sqlite3.connect(source) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'clone_only_schema'"
            ).fetchone()
            is None
        )
    assert state.engine_paths
    assert all(path != source and not path.exists() for path in state.engine_paths)


def test_generation_rejects_decorated_identity_without_outbox_watermark(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Cloud-bound index identity cannot be persisted without source evidence."""

    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=1, include_outbox_job=False)
    root, active_sentinel = _generation_root(tmp_path)
    report = _quality_report_file(tmp_path)
    state = FakeState()
    _install_shadow_fakes(monkeypatch, state)
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_MODEL", MODEL)
    monkeypatch.setenv("EMBEDDER_MODEL_REVISION", REVISION)
    monkeypatch.setenv("PP_EMBEDDING_DIM", str(DIMENSION))
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://endpoint.example/v1")

    exit_code = rebuild_lancedb.main(
        _arguments(root, source, report, "candidate-cloud-without-outbox"),
        artifact_verifier=_fake_artifact_verifier,
    )

    assert exit_code == 2
    assert "generation_mode_requires_embedding_index_identity_binding" in capsys.readouterr().err
    assert not (root / "generations" / "candidate-cloud-without-outbox").exists()
    assert os.readlink(root / "current") == "generations/active"
    assert active_sentinel.read_text(encoding="utf-8") == "active remains immutable"
    assert state.index_paths == []


def test_generation_rejects_minimal_outbox_schema_when_index_job_exists(
    tmp_path,
    monkeypatch,
    capsys,
):
    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=3, include_outbox_job=True)
    source_before = _source_state(source)
    root, active_sentinel = _generation_root(tmp_path)
    report = _quality_report_file(tmp_path)
    state = FakeState()
    _install_shadow_fakes(monkeypatch, state)

    exit_code = rebuild_lancedb.main(
        _arguments(root, source, report, "candidate-without-watermark"),
        artifact_verifier=_fake_artifact_verifier,
    )

    assert exit_code == 2
    assert "generation_mode_requires_outbox_watermark" in capsys.readouterr().err
    assert not (root / "generations" / "candidate-without-watermark").exists()
    assert os.readlink(root / "current") == "generations/active"
    assert active_sentinel.read_text(encoding="utf-8") == "active remains immutable"
    assert state.index_paths == []
    assert _source_state(source) == source_before


def test_generation_rejects_outbox_schema_without_job_identity_columns(
    tmp_path,
    monkeypatch,
    capsys,
):
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL)")
        connection.execute("INSERT INTO memories VALUES ('memory-0000', 'content')")
        connection.execute("CREATE TABLE store_outbox (outbox_id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    source_before = _source_state(source)
    root, active_sentinel = _generation_root(tmp_path)
    report = _quality_report_file(tmp_path)
    state = FakeState()
    _install_shadow_fakes(monkeypatch, state)

    exit_code = rebuild_lancedb.main(
        _arguments(root, source, report, "candidate-schema-without-identity"),
        artifact_verifier=_fake_artifact_verifier,
    )

    assert exit_code == 2
    assert "generation_mode_requires_outbox_watermark" in capsys.readouterr().err
    assert not (root / "generations" / "candidate-schema-without-identity").exists()
    assert os.readlink(root / "current") == "generations/active"
    assert active_sentinel.read_text(encoding="utf-8") == "active remains immutable"
    assert state.index_paths == []
    assert _source_state(source) == source_before


@pytest.mark.parametrize(
    "behavior",
    ["partial", "embed-failure", "backend-mismatch", "index-failure"],
)
def test_incomplete_or_failed_shadow_build_removes_staging_and_preserves_current(
    tmp_path,
    monkeypatch,
    capsys,
    behavior,
):
    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=3, include_outbox_job=False)
    source_before = _source_state(source)
    root, active_sentinel = _generation_root(tmp_path)
    report = _quality_report_file(tmp_path)
    state = FakeState(behavior=behavior)
    _install_shadow_fakes(monkeypatch, state)

    exit_code = rebuild_lancedb.main(
        _arguments(root, source, report, f"candidate-{behavior}"),
        artifact_verifier=_fake_artifact_verifier,
    )

    assert exit_code == 2
    assert "LanceDB rebuild failed:" in capsys.readouterr().err
    assert not (root / "generations" / f"candidate-{behavior}").exists()
    assert os.readlink(root / "current") == "generations/active"
    assert active_sentinel.read_text(encoding="utf-8") == "active remains immutable"
    assert _source_state(source) == source_before


def test_shadow_build_repairs_transiently_missing_rows_before_verification(
    tmp_path,
    monkeypatch,
    capsys,
):
    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=3, include_outbox_job=False)
    source_before = _source_state(source)
    root, active_sentinel = _generation_root(tmp_path)
    report = _quality_report_file(tmp_path)
    state = FakeState(behavior="transient-partial")
    _install_shadow_fakes(monkeypatch, state)

    exit_code = rebuild_lancedb.main(
        _arguments(root, source, report, "candidate-transient-partial"),
        artifact_verifier=_fake_artifact_verifier,
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["generation"]["built_row_count"] == 3
    assert output["generation"]["verification_status"] == "unverified"
    assert state.backfill_calls == 1
    assert os.readlink(root / "current") == "generations/active"
    assert active_sentinel.read_text(encoding="utf-8") == "active remains immutable"
    assert _source_state(source) == source_before


def test_v2_environment_is_rechecked_after_rebuild_before_manifest(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=3, include_outbox_job=False)
    root, active_sentinel = _generation_root(tmp_path)
    report = tmp_path / "quality-report-v2.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": RECALL_QUALITY_REPORT_SCHEMA,
                "candidate_id": "max-v1",
            }
        ),
        encoding="utf-8",
    )
    state = FakeState()
    _install_shadow_fakes(monkeypatch, state)
    checks = 0

    def validate_runtime_environment(_report):
        nonlocal checks
        checks += 1
        if checks == 3:
            raise rebuild_lancedb.ShadowBuildError("quality_report_source_fingerprint_not_current")

    monkeypatch.setattr(
        rebuild_lancedb,
        "_assert_quality_report_runtime_environment",
        validate_runtime_environment,
    )

    exit_code = rebuild_lancedb.main(
        _arguments(root, source, report, "candidate-source-changed"),
        artifact_verifier=_fake_artifact_verifier,
    )

    assert exit_code == 2
    assert checks == 3
    assert "quality_report_source_fingerprint_not_current" in capsys.readouterr().err
    assert not (root / "generations" / "candidate-source-changed").exists()
    assert os.readlink(root / "current") == "generations/active"
    assert active_sentinel.read_text(encoding="utf-8") == "active remains immutable"


def test_private_backup_is_mode_600_and_allows_clone_only_mutation(tmp_path):
    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=2)
    source_before = _source_state(source)

    with rebuild_lancedb._private_sqlite_backup(source) as clone:
        assert stat.S_IMODE(clone.stat().st_mode) == 0o600
        connection = sqlite3.connect(clone)
        try:
            connection.execute("UPDATE memories SET content = 'clone-only'")
            connection.commit()
        finally:
            connection.close()
        assert clone.read_bytes() != source.read_bytes()

    assert _source_state(source) == source_before


def test_source_fingerprint_detects_uncheckpointed_wal_update(tmp_path):
    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=1, include_outbox_job=False)
    connection = sqlite3.connect(source)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute(
            "UPDATE memories SET content = ? WHERE id = ?",
            ("first WAL value", "memory-0000"),
        )
        connection.commit()
        before = rebuild_lancedb._source_fingerprint(source)
        connection.execute(
            "UPDATE memories SET content = ? WHERE id = ?",
            ("second WAL value", "memory-0000"),
        )
        connection.commit()
        after = rebuild_lancedb._source_fingerprint(source)
    finally:
        connection.close()

    assert before.sha256 == after.sha256
    assert before.memory_row_count == after.memory_row_count == 1
    assert before.wal is not None
    assert after.wal is not None
    assert before.wal != after.wal
    assert before != after


def test_source_fingerprint_ignores_transient_empty_wal(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    _create_source_database(source, memory_count=1, include_outbox_job=False)
    wal = Path(f"{source}-wal")
    original_row_count = rebuild_lancedb._memory_row_count

    def row_count_with_empty_wal(connection):
        count = original_row_count(connection)
        wal.touch()
        return count

    monkeypatch.setattr(rebuild_lancedb, "_memory_row_count", row_count_with_empty_wal)

    fingerprint = rebuild_lancedb._source_fingerprint(source)

    assert fingerprint.memory_row_count == 1
    assert fingerprint.wal is None


def test_generation_outbox_evidence_contains_logical_source_fingerprint(tmp_path):
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL)")
        connection.execute("INSERT INTO memories VALUES ('memory-0000', 'content')")
        connection.execute(
            "CREATE TABLE store_outbox ("
            "outbox_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, project_id TEXT NOT NULL, "
            "call_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL, created_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO store_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("job", "memory_index", "project", "call", "pending", "{}", "{}", "now"),
        )
        connection.commit()
        evidence = rebuild_lancedb._index_outbox_evidence(connection)
    finally:
        connection.close()

    assert evidence["status"] == "snapshot"
    assert len(evidence["source_fingerprint"]) == 64


def test_logical_source_fingerprint_tolerates_clone_only_empty_schema_tables(tmp_path):
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL)")
        connection.execute("INSERT INTO memories VALUES ('memory-0000', 'content')")
        connection.commit()
        from plastic_promise.core.index_outbox_reconciliation import canonical_source_fingerprint

        expected = canonical_source_fingerprint(connection)
    finally:
        connection.close()

    with rebuild_lancedb._private_sqlite_backup(source) as clone:
        clone_connection = sqlite3.connect(clone)
        try:
            clone_connection.execute(
                "CREATE TABLE synthesis_artifacts (memory_id TEXT PRIMARY KEY)"
            )
            clone_connection.execute(
                "CREATE TABLE behavior_graph_edges ("
                "id TEXT PRIMARY KEY, source TEXT, target TEXT, relation TEXT"
                ")"
            )
            clone_connection.commit()
            assert canonical_source_fingerprint(clone_connection) == expected
        finally:
            clone_connection.close()


def test_shadow_build_rejects_clone_only_index_material_mutation(tmp_path):
    source = tmp_path / "source.db"
    persisted_row = _persisted_memory_row("memory-0000", "content", policy="legacy")
    connection = sqlite3.connect(source)
    try:
        connection.execute(
            "CREATE TABLE memories ("
            "id TEXT PRIMARY KEY, content TEXT NOT NULL, embedding_text TEXT NOT NULL, "
            "embedding_hash TEXT NOT NULL, search_text TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO memories ("
            "id, content, embedding_text, embedding_hash, search_text, metadata_json"
            ") VALUES ("
            ":id, :content, :embedding_text, :embedding_hash, :search_text, :metadata_json"
            ")",
            persisted_row,
        )
        connection.execute(
            "CREATE TABLE store_outbox ("
            "outbox_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, project_id TEXT NOT NULL, "
            "call_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL, created_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO store_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("job", "memory_index", "project", "call", "pending", "{}", "{}", "now"),
        )
        connection.commit()
        evidence = rebuild_lancedb._index_outbox_evidence(connection)
    finally:
        connection.close()

    snapshot = rebuild_lancedb.SourceSnapshot(
        path=source,
        sha256="a" * 64,
        memory_row_count=1,
        eligible_memory_ids=frozenset({"memory-0000"}),
        index_text_policy="legacy",
        index_material_sha256=rebuild_lancedb._source_index_material_sha256(
            {"memory-0000": persisted_row},
            expected_policy="legacy",
        ),
        index_outbox=evidence,
    )

    with rebuild_lancedb._private_sqlite_backup(source) as clone:
        clone_connection = sqlite3.connect(clone)
        try:
            clone_connection.execute(
                "UPDATE memories SET embedding_text = 'clone-only' WHERE id = 'memory-0000'"
            )
            clone_connection.commit()

            class FakeSQLite:
                _conn = clone_connection

            class FakeEngine:
                _sqlite = FakeSQLite()

            with pytest.raises(rebuild_lancedb.ShadowBuildError, match="fingerprint_mismatch"):
                rebuild_lancedb._assert_shadow_source_fingerprint(snapshot, FakeEngine())
        finally:
            clone_connection.close()


def test_no_generation_arguments_preserves_legacy_entrypoint(monkeypatch):
    called = []
    monkeypatch.setattr(rebuild_lancedb, "_legacy_rebuild", lambda: called.append(True) or 7)

    assert rebuild_lancedb.main([]) == 7
    assert called == [True]


def test_partial_generation_arguments_fail_without_running_legacy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        rebuild_lancedb,
        "_legacy_rebuild",
        lambda: pytest.fail("legacy mode must not run for partial generation arguments"),
    )

    assert rebuild_lancedb.main(["--generation-root", str(tmp_path)]) == 2
    assert (
        "generation_mode_requires_root_id_source_db_and_quality_report" in capsys.readouterr().err
    )
