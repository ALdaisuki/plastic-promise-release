from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.derived_work import DerivedWorkStore
from plastic_promise.core.structured_memory_fusion import (
    DurableFusionWorker,
    FusionSource,
    FusionValidationError,
    GovernedSynthesisDraftSink,
    ProjectScopedFusionBatcher,
    StructuredMemoryFusion,
    close_structured_fusion_batcher,
    enqueue_canonical_memory_for_fusion,
    get_durable_fusion_runtime,
    initialize_durable_fusion_runtime,
)


class _FakeProvider:
    identity = "cloud:chunk-fusion@revision-a"

    def __init__(self, responder=None):
        self.calls: list[dict] = []
        self._responder = responder or self._default_response

    def complete_json(self, *, system_prompt, user_payload, max_tokens=768):
        assert "number of returned chunks is intentionally unconstrained" in system_prompt
        assert max_tokens > 0
        self.calls.append(dict(user_payload))
        return self._responder(user_payload)

    @staticmethod
    def _default_response(payload):
        sources = payload["sources"]
        first = sources[0]
        return {
            "chunks": [
                {
                    "text": f"Fused: {first['content'][:20]}",
                    "source_ids": [first["source_id"]],
                    "evidence": [
                        {
                            "source_id": first["source_id"],
                            "start": 0,
                            "end": min(8, len(first["content"])),
                        }
                    ],
                }
            ]
        }


def _source(
    source_id: str,
    content: str,
    *,
    project_id: str = "project:alpha",
    visibility: str = "project",
    revision: str = "revision:a",
) -> FusionSource:
    return FusionSource.create(
        source_id=source_id,
        content=content,
        project_id=project_id,
        visibility=visibility,
        config_revision=revision,
    )


def test_fusion_accepts_variable_output_count_with_exact_provenance():
    def variable_response(payload):
        first, second = payload["sources"]
        return {
            "chunks": [
                {
                    "text": "Shared deployment constraint",
                    "source_ids": [first["source_id"], second["source_id"]],
                    "evidence": [
                        {"source_id": first["source_id"], "start": 0, "end": 10},
                        {"source_id": second["source_id"], "start": 0, "end": 9},
                    ],
                },
                {
                    "text": "A second independently useful retrieval chunk",
                    "source_ids": [second["source_id"]],
                    "evidence": [
                        {"source_id": second["source_id"], "start": 2, "end": 12},
                    ],
                },
                {
                    "text": "A third result is valid even for two inputs",
                    "source_ids": [first["source_id"]],
                    "evidence": [
                        {"source_id": first["source_id"], "start": 3, "end": 13},
                    ],
                },
            ]
        }

    processor = StructuredMemoryFusion(_FakeProvider(variable_response))
    result = processor.fuse(
        [_source("memory:a", "alpha deployment rules"), _source("memory:b", "beta retry strategy")],
        dispatched_reason="batch_size",
    )

    assert len(result.sources) == 2
    assert len(result.chunks) == 3
    assert result.chunks[0].source_ids == ("memory:a", "memory:b")
    assert result.chunks[0].evidence[0].text == "alpha depl"


def test_fusion_rejects_cross_project_or_uncited_source_output():
    processor = StructuredMemoryFusion(
        _FakeProvider(
            lambda _payload: {
                "chunks": [
                    {
                        "text": "bad",
                        "source_ids": ["memory:a"],
                        "evidence": [{"source_id": "memory:foreign", "start": 0, "end": 1}],
                    }
                ]
            }
        )
    )

    with pytest.raises(FusionValidationError, match="evidence_span_invalid"):
        processor.fuse([_source("memory:a", "alpha")], dispatched_reason="max_wait")

    with pytest.raises(ValueError, match="scope_mismatch"):
        processor.fuse(
            [
                _source("memory:a", "alpha", project_id="project:alpha"),
                _source("memory:b", "beta", project_id="project:beta"),
            ],
            dispatched_reason="batch_size",
        )


def test_batcher_never_combines_projects_or_configuration_revisions():
    provider = _FakeProvider()
    batcher = ProjectScopedFusionBatcher(
        StructuredMemoryFusion(provider), batch_size=2, max_wait_seconds=1.0
    )
    try:
        alpha_one = batcher.submit(_source("memory:a1", "alpha one"))
        beta_one = batcher.submit(_source("memory:b1", "beta one", project_id="project:beta"))
        alpha_two = batcher.submit(_source("memory:a2", "alpha two"))
        beta_two = batcher.submit(_source("memory:b2", "beta two", project_id="project:beta"))
        revision_two = batcher.submit(_source("memory:a3", "alpha revision", revision="revision:b"))

        assert alpha_one.result(timeout=1).scope.project_id == "project:alpha"
        assert alpha_two.result(timeout=1).scope.project_id == "project:alpha"
        assert beta_one.result(timeout=1).scope.project_id == "project:beta"
        assert beta_two.result(timeout=1).scope.project_id == "project:beta"
        batcher.flush()
        assert revision_two.result(timeout=1).scope.config_revision == "revision:b"

        submitted = [
            (call["project_id"], {row["source_id"] for row in call["sources"]})
            for call in provider.calls
        ]
        assert ("project:alpha", {"memory:a1", "memory:a2"}) in submitted
        assert ("project:beta", {"memory:b1", "memory:b2"}) in submitted
        assert ("project:alpha", {"memory:a3"}) in submitted
    finally:
        batcher.close()


def test_batcher_flushes_small_project_after_timeout_without_borrowing_other_project_sources():
    provider = _FakeProvider()
    batcher = ProjectScopedFusionBatcher(
        StructuredMemoryFusion(provider), batch_size=20, max_wait_seconds=0.03
    )
    try:
        alpha = batcher.submit(_source("memory:alpha", "alpha timeout source"))
        beta = batcher.submit(
            _source("memory:beta", "beta timeout source", project_id="project:beta")
        )

        assert alpha.result(timeout=1).dispatched_reason == "max_wait"
        assert beta.result(timeout=1).dispatched_reason == "max_wait"
        assert [{row["source_id"] for row in call["sources"]} for call in provider.calls] == [
            {"memory:alpha"},
            {"memory:beta"},
        ]
    finally:
        batcher.close()


def test_batcher_runs_full_batches_concurrently():
    entered = threading.Event()
    release = threading.Event()

    def slow_response(payload):
        if len(provider.calls) >= 2:
            entered.set()
        release.wait(timeout=1)
        return _FakeProvider._default_response(payload)

    provider = _FakeProvider(slow_response)
    batcher = ProjectScopedFusionBatcher(
        StructuredMemoryFusion(provider), batch_size=2, max_workers=2, max_wait_seconds=1.0
    )
    try:
        futures = [
            batcher.submit(_source(f"memory:{index}", f"content {index}")) for index in range(4)
        ]
        assert entered.wait(timeout=1), "two full batches should enter provider concurrently"
        release.set()
        assert all(future.result(timeout=1).chunks for future in futures)
    finally:
        release.set()
        batcher.close()


def test_governed_sink_creates_only_multisource_drafts_with_provenance():
    processor = StructuredMemoryFusion(
        _FakeProvider(
            lambda payload: {
                "chunks": [
                    {
                        "text": "joined",
                        "source_ids": ["memory:a", "memory:b"],
                        "evidence": [
                            {"source_id": "memory:a", "start": 0, "end": 3},
                            {"source_id": "memory:b", "start": 0, "end": 3},
                        ],
                    },
                    {
                        "text": "single",
                        "source_ids": ["memory:a"],
                        "evidence": [{"source_id": "memory:a", "start": 0, "end": 3}],
                    },
                ]
            }
        )
    )
    result = processor.fuse(
        [_source("memory:a", "alpha"), _source("memory:b", "beta")],
        dispatched_reason="batch_size",
    )
    calls = []

    class _Store:
        def create_draft(self, *args, **kwargs):
            calls.append((args, kwargs))
            return object()

    sink = GovernedSynthesisDraftSink(
        SimpleNamespace(_sqlite=SimpleNamespace(_conn=object()), _write_lock=threading.RLock()),
        store_factory=lambda _conn, _engine: _Store(),
    )

    report = sink(result)

    assert report == {"created": 1, "shadowed": 0, "skipped": 1, "failed": 0}
    assert calls[0][0] == ("joined", ("memory:a", "memory:b"))
    assert calls[0][1]["automatic"] is True
    assert calls[0][1]["reuse_signal"] is True
    assert calls[0][1]["metadata"]["source_evidence"] == [
        {
            "source_id": "memory:a",
            "content_hash": result.chunks[0].evidence[0].content_hash,
            "start": 0,
            "end": 3,
        },
        {
            "source_id": "memory:b",
            "content_hash": result.chunks[0].evidence[1].content_hash,
            "start": 0,
            "end": 3,
        },
    ]


def test_governed_sink_does_not_swallow_sqlite_failures():
    processor = StructuredMemoryFusion(
        _FakeProvider(
            lambda _payload: {
                "chunks": [
                    {
                        "text": "joined",
                        "source_ids": ["memory:a", "memory:b"],
                        "evidence": [
                            {"source_id": "memory:a", "start": 0, "end": 3},
                            {"source_id": "memory:b", "start": 0, "end": 3},
                        ],
                    }
                ]
            }
        )
    )
    result = processor.fuse(
        [_source("memory:a", "alpha"), _source("memory:b", "beta")],
        dispatched_reason="batch_size",
    )

    class _BrokenStore:
        def create_draft(self, *args, **kwargs):
            raise sqlite3.OperationalError("injected_write_failure")

    sink = GovernedSynthesisDraftSink(
        SimpleNamespace(_sqlite=SimpleNamespace(_conn=object()), _write_lock=threading.RLock()),
        store_factory=lambda _conn, _engine: _BrokenStore(),
    )

    with pytest.raises(sqlite3.OperationalError, match="injected_write_failure"):
        sink(result)


def test_canonical_enqueue_is_feature_gated_and_skips_synthesis(monkeypatch):
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE memories (id TEXT, content TEXT, project_id TEXT, visibility TEXT, memory_type TEXT)"
    )
    connection.executemany(
        "INSERT INTO memories VALUES (?, ?, ?, ?, ?)",
        [
            ("ordinary", "ordinary source", "project:alpha", "project", "fact"),
            ("derived", "derived source", "project:alpha", "project", "synthesis"),
        ],
    )
    engine = SimpleNamespace(
        _sqlite=SimpleNamespace(_conn=connection), _write_lock=threading.RLock()
    )
    try:
        assert enqueue_canonical_memory_for_fusion(engine, "ordinary") is None

        submitted = []

        class _Batcher:
            fusion_identity = "provider:identity"

            def submit(self, source):
                submitted.append(source)
                return "queued"

        monkeypatch.setattr(
            "plastic_promise.core.structured_memory_fusion.get_structured_fusion_batcher",
            lambda _engine: _Batcher(),
        )
        assert enqueue_canonical_memory_for_fusion(engine, "ordinary") == "queued"
        assert submitted[0].scope.project_id == "project:alpha"
        assert enqueue_canonical_memory_for_fusion(engine, "derived") is None
    finally:
        connection.close()


def test_canonical_sources_flow_through_queue_into_draft_only(tmp_path, monkeypatch):
    project_id = "project:fusion-e2e"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "fusion-e2e.db"))
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    engine = ContextEngine(use_sqlite=True)
    for source_id, label in (("memory:a", "Alpha"), ("memory:b", "Beta")):
        engine.register_memory(
            {
                "id": source_id,
                "content": (
                    f"{label} records a durable project-scoped fusion constraint with "
                    "independent operational evidence. "
                )
                * 3,
                "memory_type": "experience",
                "source": "user",
                "source_class": "experience",
                "project_id": project_id,
                "visibility": "project",
                "origin_kind": "document",
                "origin_uri": f"file:///{label.casefold()}.md",
                "origin_ref": label.casefold(),
                "origin_hash": f"origin:{label.casefold()}:v1",
            }
        )

    provider = _FakeProvider(
        lambda payload: {
            "chunks": [
                {
                    "text": "The project requires isolated, evidence-backed fusion batches.",
                    "source_ids": [row["source_id"] for row in payload["sources"]],
                    "evidence": [
                        {"source_id": row["source_id"], "start": 0, "end": 12}
                        for row in payload["sources"]
                    ],
                }
            ]
        }
    )
    batcher = ProjectScopedFusionBatcher(
        StructuredMemoryFusion(provider),
        batch_size=2,
        max_wait_seconds=1.0,
        on_result=GovernedSynthesisDraftSink(engine),
    )
    monkeypatch.setattr(
        "plastic_promise.core.structured_memory_fusion.get_structured_fusion_batcher",
        lambda _engine: batcher,
    )

    try:
        first = enqueue_canonical_memory_for_fusion(engine, "memory:a")
        second = enqueue_canonical_memory_for_fusion(engine, "memory:b")
        assert first is not None and second is not None
        assert first.result(timeout=2).scope.project_id == project_id
        assert second.result(timeout=2).scope.project_id == project_id
        assert batcher.wait_for_idle(timeout=2)

        row = engine._sqlite._conn.execute(
            "SELECT sa.memory_id, sa.status, m.project_id, m.visibility "
            "FROM synthesis_artifacts AS sa "
            "JOIN memories AS m ON m.id = sa.memory_id"
        ).fetchone()
        assert row is not None
        synthesis_id, status, stored_project, visibility = row
        assert status == "draft"
        assert stored_project == project_id
        assert visibility == "project"
        assert synthesis_id not in engine.memory_ids()
        assert engine._sqlite._conn.execute(
            "SELECT COUNT(*) FROM synthesis_artifacts WHERE status = 'verified'"
        ).fetchone() == (0,)
    finally:
        batcher.close()
        engine._sqlite._conn.close()


def test_canonical_create_and_durable_fusion_receipt_commit_together(tmp_path, monkeypatch):
    db_path = tmp_path / "durable-canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.delenv("PP_STRUCTURED_MEMORY_FUSION", raising=False)
    engine = ContextEngine(use_sqlite=True)
    monkeypatch.setenv("PP_STRUCTURED_MEMORY_FUSION", "shadow")
    worker = initialize_durable_fusion_runtime(
        engine,
        fusion=StructuredMemoryFusion(_FakeProvider()),
        autostart=False,
    )
    assert worker is not None
    engine._structured_fusion_runtime = worker
    try:
        engine.register_memory(
            {
                "id": "memory:durable",
                "content": "Durable canonical source",
                "memory_type": "experience",
                "source": "user",
                "project_id": "project:durable",
                "visibility": "project",
            }
        )

        row = engine._sqlite._conn.execute(
            "SELECT project_id, subject_id, status FROM derived_work_jobs"
        ).fetchone()
        assert row == ("project:durable", "memory:durable", "pending")
    finally:
        close_structured_fusion_batcher(engine)
        engine._sqlite._conn.close()


def test_durable_worker_recovers_expired_batch_after_restart(tmp_path, monkeypatch):
    class _Clock:
        def __init__(self):
            self.value = datetime(2026, 7, 26, tzinfo=timezone.utc)

        def __call__(self):
            return self.value

    db_path = tmp_path / "durable-restart.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "shadow")
    monkeypatch.delenv("PP_STRUCTURED_MEMORY_FUSION", raising=False)
    engine = ContextEngine(use_sqlite=True)
    engine.register_memory(
        {
            "id": "memory:restart",
            "content": "Restart-safe fusion source",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:restart",
            "visibility": "project",
        }
    )
    clock = _Clock()
    store = DerivedWorkStore(db_path, clock=clock)
    provider = _FakeProvider()
    first_worker = DurableFusionWorker(
        engine,
        store,
        StructuredMemoryFusion(provider),
        mode="shadow",
        batch_size=1,
        max_wait_seconds=0.001,
        lease_seconds=5,
        autostart=False,
    )
    source = _source(
        "memory:restart",
        "Restart-safe fusion source",
        project_id="project:restart",
        revision=first_worker.fusion_identity,
    )
    job = first_worker.enqueue_source(source).job
    stale = store.claim(job_id=job.job_id, project_id=job.project_id, lease_seconds=5)
    clock.value += timedelta(seconds=6)
    replacement = DurableFusionWorker(
        engine,
        DerivedWorkStore(db_path, clock=clock),
        StructuredMemoryFusion(provider),
        mode="shadow",
        batch_size=1,
        max_wait_seconds=0.001,
        lease_seconds=5,
        autostart=False,
    )
    try:
        assert replacement.run_once(raise_errors=True) is True
        completed = replacement.store.get(job_id=job.job_id, project_id=job.project_id)
        assert completed.status == "completed"
        assert completed.fencing_generation == stale.job.fencing_generation + 1
        assert completed.attempt_count == 1
    finally:
        first_worker.close()
        replacement.close()
        engine._sqlite._conn.close()


def test_durable_worker_rolls_back_draft_when_batch_receipt_commit_fails(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "durable-atomic-result.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.delenv("PP_STRUCTURED_MEMORY_FUSION", raising=False)
    engine = ContextEngine(use_sqlite=True)
    contents = {"memory:a": "alpha evidence", "memory:b": "beta evidence"}
    for memory_id, content in contents.items():
        engine.register_memory(
            {
                "id": memory_id,
                "content": content,
                "memory_type": "experience",
                "source": "user",
                "project_id": "project:atomic",
                "visibility": "project",
            }
        )
    provider = _FakeProvider(
        lambda payload: {
            "chunks": [
                {
                    "text": "joined result",
                    "source_ids": [row["source_id"] for row in payload["sources"]],
                    "evidence": [
                        {"source_id": row["source_id"], "start": 0, "end": 5}
                        for row in payload["sources"]
                    ],
                }
            ]
        }
    )
    store = DerivedWorkStore(db_path)
    worker = DurableFusionWorker(
        engine,
        store,
        StructuredMemoryFusion(provider),
        mode="on",
        batch_size=2,
        max_wait_seconds=60,
        sink=GovernedSynthesisDraftSink(engine),
        autostart=False,
    )
    for memory_id, content in contents.items():
        worker.enqueue_source(
            _source(
                memory_id,
                content,
                project_id="project:atomic",
                revision=worker.fusion_identity,
            )
        )
    original_complete = store.complete_in_transaction
    completion_count = 0

    def fail_second_completion(*args, **kwargs):
        nonlocal completion_count
        completion_count += 1
        if completion_count == 2:
            raise RuntimeError("injected_receipt_failure")
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(store, "complete_in_transaction", fail_second_completion)
    try:
        assert worker.run_once() is True
        assert engine._sqlite._conn.execute(
            "SELECT COUNT(*) FROM synthesis_artifacts"
        ).fetchone() == (0,)
        assert store.stats(project_id="project:atomic")["retry_wait"] == 2
    finally:
        worker.close()
        engine._sqlite._conn.close()


def test_durable_runtime_rejects_mock_sqlite_backend_without_creating_files(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PP_STRUCTURED_MEMORY_FUSION", "shadow")
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "shadow")
    engine = SimpleNamespace(
        _sqlite=SimpleNamespace(
            _db_path=MagicMock(),
            _conn=SimpleNamespace(in_transaction=False),
        )
    )
    monkeypatch.setattr(
        "plastic_promise.core.structured_memory_fusion._create_structured_fusion",
        lambda: StructuredMemoryFusion(_FakeProvider()),
    )

    assert initialize_durable_fusion_runtime(engine) is None
    assert list(tmp_path.iterdir()) == []


def test_durable_runtime_caches_provider_configuration_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "provider-failure.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.setenv("PP_STRUCTURED_MEMORY_FUSION", "shadow")
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "shadow")
    calls = 0

    def fail_provider_configuration():
        nonlocal calls
        calls += 1
        raise RuntimeError("provider_not_configured")

    monkeypatch.setattr(
        "plastic_promise.core.structured_memory_fusion._create_structured_fusion",
        fail_provider_configuration,
    )
    engine = ContextEngine(use_sqlite=True)
    try:
        assert get_durable_fusion_runtime(engine) is None
        assert get_durable_fusion_runtime(engine) is None
        assert calls == 1
    finally:
        close_structured_fusion_batcher(engine)
        engine._sqlite._conn.close()


def test_durable_worker_does_not_close_provider_while_batch_is_running(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "close-drain.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.delenv("PP_STRUCTURED_MEMORY_FUSION", raising=False)
    engine = ContextEngine(use_sqlite=True)
    entered = threading.Event()
    release = threading.Event()
    provider_closed = threading.Event()

    class _BlockingProvider(_FakeProvider):
        def complete_json(self, *, system_prompt, user_payload, max_tokens=768):
            entered.set()
            assert not provider_closed.is_set()
            assert release.wait(timeout=2)
            assert not provider_closed.is_set()
            return super().complete_json(
                system_prompt=system_prompt,
                user_payload=user_payload,
                max_tokens=max_tokens,
            )

        def close(self):
            provider_closed.set()

    worker = DurableFusionWorker(
        engine,
        DerivedWorkStore(db_path),
        StructuredMemoryFusion(_BlockingProvider()),
        mode="shadow",
        batch_size=1,
        max_wait_seconds=0.001,
        poll_seconds=0.001,
    )
    engine.register_memory(
        {
            "id": "memory:close",
            "content": "Provider close must wait for the running batch",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:close",
            "visibility": "project",
        }
    )
    worker.enqueue_source(
        _source(
            "memory:close",
            "Provider close must wait for the running batch",
            project_id="project:close",
            revision=worker.fusion_identity,
        )
    )
    try:
        assert entered.wait(timeout=1)
        assert worker.close(timeout=0.01) is False
        assert not provider_closed.is_set()
        release.set()
        deadline = time.monotonic() + 2
        while not provider_closed.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert provider_closed.is_set()
    finally:
        release.set()
        worker.close(timeout=1)
        engine._sqlite._conn.close()
