"""Focused safety tests for immutable LanceDB runtime generations."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import lancedb
import pyarrow as pa
import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.generation_live_index import bootstrap_generation_live_index
from plastic_promise.core.index_outbox_reconciliation import (
    reconcile_index_outbox,
    snapshot_index_outbox,
)
from plastic_promise.core.lancedb_artifact import (
    LanceDBArtifactError,
    _validate_vector,
    verify_lancedb_artifact,
)
from plastic_promise.core.lancedb_generation import (
    ArtifactVerificationRequest,
    _index_tree_sha256,
)
from plastic_promise.core.lancedb_store import LanceDBStore

_SELECTION_A = "d" * 64

if TYPE_CHECKING:
    from collections.abc import Callable


class _Embedder:
    model_name = "test-embedding"
    index_model_name = "test-embedding"
    dim = 3

    def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _schema(dimension: int = 3) -> pa.Schema:
    return pa.schema(
        [
            pa.field("memory_id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dimension)),
            pa.field("text", pa.string()),
            pa.field("tier", pa.string()),
            pa.field("category", pa.string()),
            pa.field("scope", pa.string()),
        ]
    )


def _row(memory_id: str, vector: list[float]) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "vector": vector,
        "text": f"text:{memory_id}",
        "tier": "L1",
        "category": "fact",
        "scope": "global",
    }


def _create_index(
    path: Path,
    rows: list[dict[str, object]],
    *,
    schema: pa.Schema | None = None,
) -> None:
    path.mkdir()
    database = lancedb.connect(path)
    table = database.create_table("memory_vectors", schema=schema or _schema(), data=[])
    if rows:
        table.add(rows)


def _request(path: Path, *, dimension: int = 3) -> tuple[int, ArtifactVerificationRequest]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    return descriptor, ArtifactVerificationRequest(
        index_fd=descriptor,
        generation_id="generation-a",
        index_schema="memory-vectors/v1",
        embedding_model="test-embedding",
        model_revision="revision-a",
        embedding_dimension=dimension,
        expected_tree_sha256=_index_tree_sha256(descriptor),
    )


def test_artifact_verifier_observes_rows_and_preserves_tree(tmp_path):
    index = tmp_path / "index"
    _create_index(
        index,
        [_row("memory-a", [0.1, 0.2, 0.3]), _row("memory-b", [0.4, 0.5, 0.6])],
    )
    descriptor, request = _request(index)
    before = _index_tree_sha256(descriptor)
    try:
        result = verify_lancedb_artifact(request)
        after = _index_tree_sha256(descriptor)
    finally:
        os.close(descriptor)

    assert result.row_count == 2
    assert result.embedding_dimension == 3
    assert result.embedding_model == request.embedding_model
    assert result.model_revision == request.model_revision
    assert after == before


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([_row("", [0.1, 0.2, 0.3])], "artifact_memory_id_empty"),
        (
            [_row("duplicate", [0.1, 0.2, 0.3]), _row("duplicate", [0.4, 0.5, 0.6])],
            "artifact_memory_id_duplicate",
        ),
        ([_row("zero", [0.0, -0.0, 0.0])], "artifact_vector_zero"),
    ],
)
def test_artifact_verifier_rejects_invalid_row_material(tmp_path, rows, reason):
    index = tmp_path / "index"
    _create_index(index, rows)
    descriptor, request = _request(index)
    try:
        with pytest.raises(LanceDBArtifactError, match=reason):
            verify_lancedb_artifact(request)
    finally:
        os.close(descriptor)


def test_artifact_verifier_rejects_non_fixed_requested_dimension(tmp_path):
    index = tmp_path / "index"
    _create_index(index, [_row("memory-a", [0.1, 0.2])], schema=_schema(2))
    descriptor, request = _request(index, dimension=3)
    try:
        with pytest.raises(LanceDBArtifactError, match="artifact_vector_schema_mismatch"):
            verify_lancedb_artifact(request)
    finally:
        os.close(descriptor)


def test_artifact_vector_validation_rejects_nonfinite_values():
    with pytest.raises(LanceDBArtifactError, match="artifact_vector_nonfinite"):
        _validate_vector([0.1, float("nan"), 0.3], 3)


def test_read_only_store_opens_without_fts_and_rejects_every_mutation(tmp_path):
    index = tmp_path / "index"
    _create_index(index, [_row("memory-a", [0.1, 0.2, 0.3])])
    descriptor = os.open(index, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    before = _index_tree_sha256(descriptor)
    store = LanceDBStore(str(index), _Embedder(), read_only=True)

    assert store.read_only is True
    assert store.count_rows() == 1
    assert store._fts_ready is False

    mutations: tuple[Callable[[], object], ...] = (
        lambda: store._ensure_fts(),
        lambda: store.optimize_if_fragmented(),
        lambda: store.insert("new", [0.1, 0.2, 0.3], "new"),
        lambda: store.insert_checked("new", [0.1, 0.2, 0.3], "new"),
        lambda: store.replace_checked("memory-a", [0.3, 0.2, 0.1], "replacement"),
        lambda: store.update("memory-a", [0.3, 0.2, 0.1], "replacement"),
        lambda: store.delete("memory-a"),
        lambda: store.delete_checked("memory-a"),
        lambda: store.clear_all(),
        lambda: store.clear_all_checked(),
        lambda: store.sync_with_engine(object()),
        lambda: store.backfill(object()),
        lambda: store.rebuild_all(object()),
    )
    for mutation in mutations:
        with pytest.raises(RuntimeError, match="lancedb_read_only"):
            mutation()

    after = _index_tree_sha256(descriptor)
    os.close(descriptor)
    assert after == before


def test_read_only_store_does_not_create_missing_path(tmp_path):
    missing = tmp_path / "missing-index"
    with pytest.raises(RuntimeError, match="lancedb_read_only_path_unavailable"):
        LanceDBStore(str(missing), _Embedder(), read_only=True)
    assert not missing.exists()


def test_read_only_store_rejects_symlinked_index_path(tmp_path):
    index = tmp_path / "index"
    _create_index(index, [_row("memory-a", [0.1, 0.2, 0.3])])
    alias = tmp_path / "index-alias"
    alias.symlink_to(index, target_is_directory=True)

    with pytest.raises(RuntimeError, match="lancedb_read_only_path_unavailable"):
        LanceDBStore(str(alias), _Embedder(), read_only=True)


class _DomainManager:
    def __init__(self, db_path: str):
        self.db_path = db_path


def _install_generation_manager(monkeypatch, selected: dict[str, object]):
    class _GenerationManager:
        calls: list[dict[str, object]] = []

        def __init__(self, root, *, create, artifact_verifier):
            self.root = Path(root).absolute()
            self.calls.append(
                {
                    "root": self.root,
                    "create": create,
                    "artifact_verifier": artifact_verifier,
                }
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def resolve_verified_current_generation(self):
            self.calls[-1]["generation_resolution_calls"] = (
                self.calls[-1].get("generation_resolution_calls", 0) + 1
            )
            error = selected.get("error")
            if isinstance(error, BaseException):
                raise error
            return selected.get("manifest"), Path(selected["path"])

        def resolve_verified_current_selection(self):
            manifest, path = self.resolve_verified_current_generation()
            return manifest, path, str(selected.get("selection_identity") or _SELECTION_A)

    monkeypatch.setattr(
        "plastic_promise.core.lancedb_generation.GenerationManager",
        _GenerationManager,
    )
    return _GenerationManager


def _generation_engine(monkeypatch, tmp_path: Path) -> ContextEngine:
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "canonical.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_GENERATION_ROOT", str(tmp_path / "generations"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "legacy-decoy"))
    monkeypatch.delenv("EMBEDDER_MODEL_REVISION", raising=False)
    monkeypatch.setenv("LDB_INIT_ON_HEAVY_INIT", "1")
    monkeypatch.setenv("LDB_BACKFILL_ON_INIT", "1")
    monkeypatch.setenv("LDB_REBUILD_ON_INIT", "1")
    monkeypatch.setattr(
        "plastic_promise.core.domain_manager.DomainManager",
        _DomainManager,
    )
    engine = ContextEngine(use_sqlite=False)
    engine._embedder = _Embedder()
    engine._build_principle_anchors = lambda: None
    return engine


def _reconciled_outbox_evidence(
    database_path: Path,
    *,
    generation_id: str,
    embedding_index_identity: str,
) -> dict[str, object]:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE store_outbox (
                outbox_id TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT '',
                call_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        snapshot = snapshot_index_outbox(connection, project_id="project:test")
        receipt = reconcile_index_outbox(
            connection,
            generation_id=generation_id,
            manifest_hash="c" * 64,
            evidence=snapshot,
        )
    finally:
        connection.close()
    return {
        **snapshot,
        "reconciled": True,
        "embedding_index_identity": embedding_index_identity,
        "receipt": receipt,
    }


def _manifest(
    *,
    embedding_model: str = "test-embedding",
    model_revision: str = "test-embedding",
    embedding_dimension: int = 3,
) -> dict[str, object]:
    return {
        "embedding_model": embedding_model,
        "model_revision": model_revision,
        "embedding_dimension": embedding_dimension,
        "index_outbox": None,
    }


def test_context_engine_binds_generation_manifest_embedding_identity(tmp_path, monkeypatch):
    index = tmp_path / "generation-a" / "index"
    index.parent.mkdir()
    _create_index(index, [_row("memory-a", [0.1, 0.2, 0.3])])
    selected: dict[str, object] = {
        "path": index,
        "manifest": _manifest(),
    }
    _install_generation_manager(monkeypatch, selected)
    engine = _generation_engine(monkeypatch, tmp_path)

    engine._ensure_heavy_init()

    assert engine._ldb is not None
    assert Path(engine._ldb._path) == index


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("embedding_model", "different-embedding"),
        ("model_revision", "revision-a"),
        ("embedding_dimension", 4),
    ],
)
def test_context_engine_rejects_generation_embedding_identity_mismatch(
    tmp_path,
    monkeypatch,
    field,
    value,
):
    index = tmp_path / "generation-a" / "index"
    index.parent.mkdir()
    _create_index(index, [_row("memory-a", [0.1, 0.2, 0.3])])
    manifest = _manifest()
    manifest[field] = value
    selected: dict[str, object] = {
        "path": index,
        "manifest": manifest,
    }
    manager_type = _install_generation_manager(monkeypatch, selected)
    engine = _generation_engine(monkeypatch, tmp_path)

    engine._ensure_heavy_init()

    assert engine._ldb is None
    assert engine._lancedb_generation_path == ""
    assert engine._lancedb_generation_error == "RuntimeError"
    assert not (tmp_path / "legacy-decoy").exists()
    # The manifest and concrete path are selected together, but the mismatched
    # path is never opened as LanceDB.
    assert manager_type.calls[0].get("generation_resolution_calls", 0) == 1


def test_context_engine_resolves_revision_through_embedder_delegate(
    tmp_path,
    monkeypatch,
):
    class _RevisionDelegate(_Embedder):
        _model_revision = "revision-a"

    class _Wrapper:
        def __init__(self):
            self._delegate = _RevisionDelegate()

        @property
        def model_name(self):
            return self._delegate.model_name

        @property
        def index_model_name(self):
            return self._delegate.index_model_name

        @property
        def dim(self):
            return self._delegate.dim

        def embed(self, text):
            return self._delegate.embed(text)

    index = tmp_path / "generation-a" / "index"
    index.parent.mkdir()
    _create_index(index, [_row("memory-a", [0.1, 0.2, 0.3])])
    selected: dict[str, object] = {
        "path": index,
        "manifest": _manifest(model_revision="revision-a"),
    }
    _install_generation_manager(monkeypatch, selected)
    engine = _generation_engine(monkeypatch, tmp_path)
    engine._embedder = _Wrapper()

    engine._ensure_heavy_init()

    assert engine._ldb is not None
    assert Path(engine._ldb._path) == index


def test_context_engine_requires_bound_decorated_embedding_identity(tmp_path, monkeypatch):
    class _EndpointEmbedder(_Embedder):
        index_model_name = "test-embedding|endpoint_sha256=" + "a" * 64

    from plastic_promise.core.context_engine import _assert_generation_embedding_identity

    with pytest.raises(RuntimeError, match="generation_embedding_index_identity_missing"):
        _assert_generation_embedding_identity(_EndpointEmbedder(), _manifest())


def test_context_engine_rejects_top_level_decorated_identity_without_outbox_binding(
    tmp_path,
    monkeypatch,
):
    index_identity = "test-embedding|endpoint_sha256=" + "a" * 64

    class _EndpointEmbedder(_Embedder):
        index_model_name = index_identity

    from plastic_promise.core.context_engine import _assert_generation_embedding_identity

    with pytest.raises(RuntimeError, match="generation_embedding_index_identity_missing"):
        _assert_generation_embedding_identity(
            _EndpointEmbedder(),
            {
                **_manifest(embedding_model=index_identity),
                "index_outbox": None,
            },
        )


def test_context_engine_checks_bound_decorated_embedding_identity(tmp_path, monkeypatch):
    index_identity = "test-embedding|endpoint_sha256=" + "a" * 64

    class _EndpointEmbedder(_Embedder):
        index_model_name = index_identity

    from plastic_promise.core.context_engine import _assert_generation_embedding_identity

    _assert_generation_embedding_identity(
        _EndpointEmbedder(),
        {
            **_manifest(),
            "index_outbox": {"embedding_index_identity": index_identity},
        },
    )


def test_context_engine_uses_verified_generation_for_python_and_rust(tmp_path, monkeypatch):
    index = tmp_path / "generation-a" / "index"
    index.parent.mkdir()
    _create_index(index, [_row("memory-a", [0.1, 0.2, 0.3])])
    selected: dict[str, object] = {"path": index}
    manager_type = _install_generation_manager(monkeypatch, selected)
    engine = _generation_engine(monkeypatch, tmp_path)

    engine._ensure_heavy_init()

    assert engine._ldb is not None
    assert engine._ldb.read_only is True
    assert Path(engine._ldb._path) == index
    assert Path(engine._lancedb_generation_path) == index
    assert engine._lancedb_sync_status == {
        "success": True,
        "status": "generation_verified_read_only",
    }
    assert engine._ldb._table.list_indices() == []
    assert not (tmp_path / "legacy-decoy").exists()
    assert manager_type.calls[0]["create"] is False
    assert callable(manager_type.calls[0]["artifact_verifier"])

    _db_path, rust_lancedb_path = engine._rust_backend_paths()
    assert Path(rust_lancedb_path) == index


def test_context_engine_uses_writable_generation_live_view_for_python_and_rust(
    tmp_path,
    monkeypatch,
):
    index = tmp_path / "generation-a" / "index"
    index.parent.mkdir()
    _create_index(index, [_row("memory-a", [0.1, 0.2, 0.3])])
    generation_id = "generation-a"
    manifest = {
        **_manifest(),
        "generation_id": generation_id,
        "manifest_sha256": "a" * 64,
        "index_outbox": _reconciled_outbox_evidence(
            tmp_path / "canonical.db",
            generation_id=generation_id,
            embedding_index_identity=_Embedder.index_model_name,
        ),
    }
    selected: dict[str, object] = {"path": index, "manifest": manifest}
    _install_generation_manager(monkeypatch, selected)
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=index,
        base_manifest=manifest,
        base_selection_identity=_SELECTION_A,
        live_root=live_root,
    )
    monkeypatch.setenv("PLASTIC_LANCEDB_LIVE_ROOT", str(live_root))
    engine = _generation_engine(monkeypatch, tmp_path)

    engine._ensure_heavy_init()

    assert engine._ldb is not None
    assert engine._ldb.read_only is False
    assert Path(engine._ldb._path) == live_root / "index"
    assert Path(engine._lancedb_generation_path) == live_root / "index"
    assert engine._lancedb_sync_status == {
        "success": True,
        "status": "generation_live_index",
        "base_generation_id": "generation-a",
        "lag": {
            "schema": "plastic-promise/generation-live-index-lag/v1",
            "state": "ready",
            "base_generation_id": "generation-a",
            "base_outbox_watermark": 0,
            "newer_job_count": 0,
            "active_job_count": 0,
            "completed_job_count": 0,
            "blocked_job_count": 0,
            "status_counts": {},
            "newest_rowid": 0,
        },
    }
    engine._ldb.replace_checked(
        "memory-a",
        [0.3, 0.2, 0.1],
        "replacement",
        compact=False,
    )
    assert LanceDBStore(str(index), _Embedder(), read_only=True).get_vector(
        "memory-a"
    ) == pytest.approx([0.1, 0.2, 0.3])
    assert engine._ldb.get_vector("memory-a") == pytest.approx([0.3, 0.2, 0.1])

    _db_path, rust_lancedb_path = engine._rust_backend_paths()
    assert Path(rust_lancedb_path) == live_root / "index"


def test_context_engine_rejects_live_view_without_reconciled_outbox(tmp_path, monkeypatch):
    index = tmp_path / "generation-a" / "index"
    index.parent.mkdir()
    _create_index(index, [_row("memory-a", [0.1, 0.2, 0.3])])
    generation_id = "generation-a"
    replayable_manifest = {
        **_manifest(),
        "generation_id": generation_id,
        "manifest_sha256": "a" * 64,
        "index_outbox": _reconciled_outbox_evidence(
            tmp_path / "canonical.db",
            generation_id=generation_id,
            embedding_index_identity=_Embedder.index_model_name,
        ),
    }
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=index,
        base_manifest=replayable_manifest,
        base_selection_identity=_SELECTION_A,
        live_root=live_root,
    )
    selected: dict[str, object] = {
        "path": index,
        "manifest": {**replayable_manifest, "index_outbox": None},
    }
    _install_generation_manager(monkeypatch, selected)
    monkeypatch.setenv("PLASTIC_LANCEDB_LIVE_ROOT", str(live_root))
    engine = _generation_engine(monkeypatch, tmp_path)

    engine._ensure_heavy_init()

    assert engine._ldb is None
    assert engine._lancedb_generation_path == ""
    assert engine._lancedb_generation_error == "GenerationLiveIndexError"


def test_context_engine_rejects_live_view_without_generation_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "canonical.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_LIVE_ROOT", str(tmp_path / "live"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "legacy-decoy"))
    monkeypatch.setenv("LDB_INIT_ON_HEAVY_INIT", "1")
    monkeypatch.setattr(
        "plastic_promise.core.domain_manager.DomainManager",
        _DomainManager,
    )
    engine = ContextEngine(use_sqlite=False)
    engine._embedder = _Embedder()
    engine._build_principle_anchors = lambda: None

    engine._ensure_heavy_init()

    assert engine._ldb is None
    assert engine._lancedb_generation_path == ""
    assert engine._lancedb_generation_error == "RuntimeError"
    assert not (tmp_path / "legacy-decoy").exists()


@pytest.mark.parametrize(
    "state",
    ["missing", "unverified", "tampered", "escape"],
)
def test_context_generation_resolution_failure_never_uses_legacy_decoy(
    tmp_path,
    monkeypatch,
    state,
):
    selected: dict[str, object] = {
        "path": tmp_path / "unused",
        "error": RuntimeError(f"current_generation_{state}"),
    }
    _install_generation_manager(monkeypatch, selected)
    engine = _generation_engine(monkeypatch, tmp_path)

    engine._ensure_heavy_init()

    assert engine._ldb is None
    assert engine._lancedb_generation_path == ""
    assert engine._lancedb_generation_error == "RuntimeError"
    assert not (tmp_path / "legacy-decoy").exists()
    with pytest.raises(RuntimeError, match="verified LanceDB generation unavailable"):
        engine._rust_backend_paths()
    assert not (tmp_path / "legacy-decoy").exists()


def test_new_context_engine_resolves_promoted_and_rolled_back_generation(
    tmp_path,
    monkeypatch,
):
    generation_a = tmp_path / "generation-a" / "index"
    generation_b = tmp_path / "generation-b" / "index"
    generation_a.parent.mkdir()
    generation_b.parent.mkdir()
    _create_index(generation_a, [_row("memory-a", [0.1, 0.2, 0.3])])
    _create_index(generation_b, [_row("memory-b", [0.3, 0.2, 0.1])])
    selected: dict[str, object] = {"path": generation_a}
    _install_generation_manager(monkeypatch, selected)

    first = _generation_engine(monkeypatch, tmp_path)
    first._ensure_heavy_init()
    selected["path"] = generation_b
    promoted = _generation_engine(monkeypatch, tmp_path)
    promoted._ensure_heavy_init()
    selected["path"] = generation_a
    rolled_back = _generation_engine(monkeypatch, tmp_path)
    rolled_back._ensure_heavy_init()

    assert Path(first._ldb._path) == generation_a
    assert Path(promoted._ldb._path) == generation_b
    assert Path(rolled_back._ldb._path) == generation_a
    assert first._ldb.read_only is promoted._ldb.read_only is rolled_back._ldb.read_only is True
