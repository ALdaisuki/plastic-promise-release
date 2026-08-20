from __future__ import annotations

import hashlib

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.derived_work import DerivedWorkStore
from plastic_promise.core.memory_proposals import MemoryProposalStore
from plastic_promise.passive_memory.events import PassiveMemoryEvent
from plastic_promise.passive_memory.semantic_pipeline import (
    PASSIVE_SEMANTIC_INTENT_ID,
    PASSIVE_SEMANTIC_SCHEMA_ID,
    SEMANTIC_CONFIG_REVISION,
    SEMANTIC_JOB_KIND,
    SEMANTIC_SCHEMA_VERSION,
    DurableSemanticMemoryWorker,
    _content_is_grounded,
    close_semantic_memory_runtime,
    enqueue_semantic_capture,
    get_semantic_memory_provider,
    process_semantic_memory_jobs,
)


class FakeSemanticProvider:
    identity = "openai-compatible:test-semantic@v1"

    def __init__(self) -> None:
        self.calls = []

    def complete_json(self, *, system_prompt, user_payload, max_tokens=768):
        self.calls.append((system_prompt, user_payload, max_tokens))
        texts = [item["user_text"] for item in user_payload["inputs"]]
        return {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "items": [
                {
                    "content": "Project boundaries remain the semantic batching boundary.",
                    "category": "decision",
                    "confidence": 0.93,
                    "source_indices": [0, 1],
                    "evidence": texts,
                }
            ],
        }

    @property
    def stats(self):
        return {}

    def close(self):
        return None


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "semantic-memory.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    instance = ContextEngine(use_sqlite=True)
    try:
        yield instance
    finally:
        close_semantic_memory_runtime(instance, timeout=0)
        instance._sqlite._conn.close()


def _enqueue(
    store,
    provider,
    *,
    subject_id,
    text,
    project_id="project:alpha",
    visibility="project",
    config_revision=SEMANTIC_CONFIG_REVISION,
):
    content_hash = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    return store.enqueue(
        project_id=project_id,
        visibility=visibility,
        config_revision=config_revision,
        job_kind=SEMANTIC_JOB_KIND,
        provider_identity=provider.identity,
        subject_id=subject_id,
        subject_hash=content_hash,
        dedupe_key=f"semantic:{project_id}:{content_hash}",
        payload={
            "schema": SEMANTIC_SCHEMA_VERSION,
            "user_text": text,
            "origin_role": "user",
            "origin_turn_hash": content_hash,
            "origin_call_id": f"call:{subject_id}",
        },
    ).job


class EchoSemanticProvider(FakeSemanticProvider):
    def complete_json(self, *, system_prompt, user_payload, max_tokens=768):
        self.calls.append((system_prompt, user_payload, max_tokens))
        text = user_payload["inputs"][0]["user_text"]
        return {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "items": [
                {
                    "content": text,
                    "category": "fact",
                    "confidence": 0.9,
                    "source_indices": [0],
                    "evidence": [text],
                }
            ],
        }


def test_server_semantic_provider_routes_only_through_governed_compute_runtime(engine, monkeypatch):
    """The server must never rediscover a cloud JSON provider for passive work."""

    from plastic_promise.passive_memory import semantic_pipeline

    class Outcome:
        output = {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "items": [],
        }

    class GovernedRuntime:
        def __init__(self) -> None:
            self.calls = []

        def structured_json_for_context(self, **kwargs):
            self.calls.append(kwargs)
            return Outcome()

    runtime = GovernedRuntime()
    engine.install_memory_index_node_runtime(runtime)
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-server-backend")
    monkeypatch.setattr(semantic_pipeline, "_PROVIDER", None)

    def direct_provider_forbidden(*_args, **_kwargs):
        raise AssertionError("server must not construct a direct JSON provider")

    monkeypatch.setattr(
        semantic_pipeline,
        "create_chunk_json_provider",
        direct_provider_forbidden,
        raising=False,
    )

    provider = get_semantic_memory_provider(engine)
    output = provider.complete_json(
        system_prompt="untrusted caller prompt",
        user_payload={
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "scope": {"project_id": "project:alpha"},
            "inputs": [{"index": 0, "user_text": "Keep this project isolated."}],
        },
        max_tokens=777,
    )

    assert output == Outcome.output
    assert provider.identity == "governed-node:structured-json/v1"
    assert runtime.calls == [
        {
            "project_id": "project:alpha",
            "intent_id": PASSIVE_SEMANTIC_INTENT_ID,
            "schema_id": PASSIVE_SEMANTIC_SCHEMA_ID,
            "user_payload": {
                "schema_version": SEMANTIC_SCHEMA_VERSION,
                "scope": {"project_id": "project:alpha"},
                "inputs": [{"index": 0, "user_text": "Keep this project isolated."}],
            },
            "max_tokens": 777,
        }
    ]


def test_server_semantic_provider_does_not_replace_or_close_direct_provider(engine, monkeypatch):
    """Engine-bound server adapters must not share the direct-provider cache."""

    from plastic_promise.passive_memory import semantic_pipeline

    class DirectProvider(FakeSemanticProvider):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def close(self):
            self.closed = True

    class GovernedRuntime:
        def structured_json_for_context(self, **_kwargs):
            raise AssertionError("provider construction must not invoke the runtime")

    direct_provider = DirectProvider()
    engine.install_memory_index_node_runtime(GovernedRuntime())
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-server-backend")
    monkeypatch.setattr(semantic_pipeline, "_PROVIDER", direct_provider)

    governed_provider = get_semantic_memory_provider(engine)

    assert governed_provider.identity == "governed-node:structured-json/v1"
    assert governed_provider is not direct_provider
    assert semantic_pipeline._PROVIDER is direct_provider

    close_semantic_memory_runtime(engine, timeout=0)

    assert direct_provider.closed is False
    assert semantic_pipeline._PROVIDER is direct_provider


def test_semantic_worker_can_fuse_multiple_user_turns_into_one_grounded_proposal(engine):
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    provider = FakeSemanticProvider()
    first = _enqueue(
        store,
        provider,
        subject_id="passive-turn:first",
        text="Keep semantic work inside each project boundary.",
    )
    second = _enqueue(
        store,
        provider,
        subject_id="passive-turn:second",
        text="Visibility must remain part of the semantic batch key.",
    )
    worker = DurableSemanticMemoryWorker(
        engine,
        store,
        provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )

    assert worker.run_once(raise_errors=True) is True

    first_done = store.get(job_id=first.job_id, project_id=first.project_id)
    second_done = store.get(job_id=second.job_id, project_id=second.project_id)
    assert first_done.status == "completed"
    assert second_done.status == "completed"
    assert len(provider.calls) == 1
    assert provider.calls[0][1]["scope"] == {
        "project_id": "project:alpha",
        "visibility": "project",
        "config_revision": SEMANTIC_CONFIG_REVISION,
        "provider_identity": provider.identity,
    }
    proposal_ids = first_done.result["proposal_ids"]
    assert proposal_ids == second_done.result["proposal_ids"]
    assert len(proposal_ids) == 1
    proposal = MemoryProposalStore(engine._sqlite._conn).get(proposal_ids[0])
    assert proposal["content"] == "Project boundaries remain the semantic batching boundary."
    assert proposal["status"] == "pending"
    assert proposal["project_id"] == "project:alpha"


def test_semantic_shadow_persists_project_scoped_classification_results(engine):
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    provider = EchoSemanticProvider()
    job = _enqueue(
        store,
        provider,
        subject_id="passive-turn:shadow",
        text="Shadow classification remains durable.",
    )
    worker = DurableSemanticMemoryWorker(
        engine,
        store,
        provider,
        mode="shadow",
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )

    assert worker.run_once(raise_errors=True) is True

    completed = store.get(job_id=job.job_id, project_id=job.project_id)
    assert completed.status == "completed"
    assert completed.result == {
        "schema": SEMANTIC_SCHEMA_VERSION,
        "mode": "shadow",
        "proposal_ids": [],
        "classified_item_count": 1,
        "classifications": [
            {
                "content": "Shadow classification remains durable.",
                "category": "fact",
                "confidence": 0.9,
                "evidence": ["Shadow classification remains durable."],
                "source_job_ids": [job.job_id],
            }
        ],
    }
    assert engine._sqlite._conn.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0


def test_semantic_enqueue_preserves_original_user_text(engine, monkeypatch):
    original = "  Preserve  user-authored\nspacing exactly.  "
    monkeypatch.setenv("PP_PASSIVE_SEMANTIC_CAPTURE", "shadow")
    monkeypatch.setenv("PP_PASSIVE_SEMANTIC_WORKER_AUTOSTART", "0")
    monkeypatch.setattr(
        "plastic_promise.passive_memory.semantic_pipeline.get_semantic_memory_provider",
        lambda _engine: EchoSemanticProvider(),
    )
    event = PassiveMemoryEvent(
        event="after_invoke",
        request_id="turn:original-text",
        call_id="call:original-text",
        stage_session_id="stage:original-text",
        flow_line_id="codex",
        project_id="project:alpha",
        visibility="project",
        user_text=original,
        assistant_text="",
    )

    queued = enqueue_semantic_capture(engine, event, user_text=original)

    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    job = DerivedWorkStore(db_path).get(
        job_id=queued["job_id"],
        project_id="project:alpha",
    )
    assert job.payload["user_text"] == original
    assert job.subject_hash == "sha256:" + hashlib.sha256(original.encode()).hexdigest()


def test_process_semantic_jobs_reports_runtime_initialization_failure(engine, monkeypatch):
    from plastic_promise.passive_memory import semantic_pipeline

    monkeypatch.setenv("PP_PASSIVE_SEMANTIC_CAPTURE", "shadow")
    close_semantic_memory_runtime(engine, timeout=0)

    class BrokenWorker:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("provider configuration unavailable")

    monkeypatch.setattr(semantic_pipeline, "DurableSemanticMemoryWorker", BrokenWorker)

    assert process_semantic_memory_jobs(engine) == {
        "skipped": "semantic_memory_runtime_unavailable",
        "failure_code": "semantic_memory_runtime_init_failed",
        "processed_batches": 0,
    }


def test_semantic_worker_never_mixes_project_partitions(engine, monkeypatch):
    monkeypatch.delenv("PP_PASSIVE_SEMANTIC_MAX_TOKENS", raising=False)
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    provider = EchoSemanticProvider()
    alpha = _enqueue(
        store,
        provider,
        subject_id="passive-turn:alpha",
        text="Alpha keeps its own semantic context.",
        project_id="project:alpha",
    )
    beta = _enqueue(
        store,
        provider,
        subject_id="passive-turn:beta",
        text="Beta keeps its own semantic context.",
        project_id="project:beta",
    )
    worker = DurableSemanticMemoryWorker(
        engine,
        store,
        provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )

    assert worker.run_once(raise_errors=True) is True
    assert worker.run_once(raise_errors=True) is True

    observed_projects = [call[1]["scope"]["project_id"] for call in provider.calls]
    assert observed_projects == ["project:alpha", "project:beta"]
    assert all(len(call[1]["inputs"]) == 1 for call in provider.calls)
    assert all(call[2] == 32 * 1024 for call in provider.calls)
    assert store.get(job_id=alpha.job_id, project_id="project:alpha").status == "completed"
    assert store.get(job_id=beta.job_id, project_id="project:beta").status == "completed"


def test_semantic_worker_isolates_visibility_config_and_provider_partitions(engine):
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    first_provider = EchoSemanticProvider()
    second_provider = EchoSemanticProvider()
    second_provider.identity = "openai-compatible:test-semantic@v2"
    private = _enqueue(
        store,
        first_provider,
        subject_id="passive-turn:private",
        text="Private visibility stays isolated.",
        visibility="private",
    )
    shared = _enqueue(
        store,
        first_provider,
        subject_id="passive-turn:shared",
        text="Shared visibility stays isolated.",
        visibility="shared",
    )
    foreign_provider = _enqueue(
        store,
        second_provider,
        subject_id="passive-turn:provider-v2",
        text="Provider identities stay isolated.",
    )
    foreign_config = _enqueue(
        store,
        first_provider,
        subject_id="passive-turn:config-v2",
        text="Configuration revisions stay isolated.",
        config_revision="passive-semantic-memory-v2",
    )
    first_worker = DurableSemanticMemoryWorker(
        engine,
        store,
        first_provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )

    assert first_worker.run_once() is True
    assert first_worker.run_once() is True
    assert first_worker.run_once() is True
    assert (
        store.get(job_id=foreign_provider.job_id, project_id=foreign_provider.project_id).status
        == "pending"
    )
    assert store.get(job_id=foreign_config.job_id, project_id=foreign_config.project_id).status == (
        "dead"
    )
    observed_scopes = [call[1]["scope"] for call in first_provider.calls]
    assert {scope["visibility"] for scope in observed_scopes} == {"private", "shared"}
    assert all(scope["config_revision"] == SEMANTIC_CONFIG_REVISION for scope in observed_scopes)
    assert all(scope["provider_identity"] == first_provider.identity for scope in observed_scopes)
    assert all(len(call[1]["inputs"]) == 1 for call in first_provider.calls)

    second_worker = DurableSemanticMemoryWorker(
        engine,
        store,
        second_provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )
    assert second_worker.run_once(raise_errors=True) is True
    assert (
        store.get(job_id=foreign_provider.job_id, project_id=foreign_provider.project_id).status
        == "completed"
    )
    assert len(second_provider.calls) == 1
    assert store.get(job_id=private.job_id, project_id=private.project_id).status == "completed"
    assert store.get(job_id=shared.job_id, project_id=shared.project_id).status == "completed"


def test_invalid_semantic_json_retries_with_reason_then_becomes_dead(engine):
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)

    class InvalidEvidenceProvider(EchoSemanticProvider):
        def complete_json(self, *, system_prompt, user_payload, max_tokens=768):
            self.calls.append((system_prompt, user_payload, max_tokens))
            return {
                "schema_version": SEMANTIC_SCHEMA_VERSION,
                "items": [
                    {
                        "content": "Ungrounded output",
                        "category": "fact",
                        "confidence": 0.99,
                        "source_indices": [0],
                        "evidence": ["not present in the user text"],
                    }
                ],
            }

    provider = InvalidEvidenceProvider()
    text = "Only grounded user facts may become proposals."
    content_hash = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    job = store.enqueue(
        project_id="project:alpha",
        visibility="project",
        config_revision=SEMANTIC_CONFIG_REVISION,
        job_kind=SEMANTIC_JOB_KIND,
        provider_identity=provider.identity,
        subject_id="passive-turn:invalid",
        subject_hash=content_hash,
        dedupe_key=f"semantic:invalid:{content_hash}",
        payload={
            "schema": SEMANTIC_SCHEMA_VERSION,
            "user_text": text,
            "origin_role": "user",
            "origin_turn_hash": content_hash,
            "origin_call_id": "call:invalid",
        },
        max_attempts=2,
    ).job
    worker = DurableSemanticMemoryWorker(
        engine,
        store,
        provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        retry_delay_seconds=0,
        autostart=False,
    )

    assert worker.run_once() is True
    retry = store.get(job_id=job.job_id, project_id=job.project_id)
    assert retry.status == "retry_wait"
    assert retry.failure_code == "passive_semantic_output_invalid"
    assert retry.attempt_count == 1

    assert worker.run_once() is True
    dead = store.get(job_id=job.job_id, project_id=job.project_id)
    assert dead.status == "dead"
    assert dead.failure_code == "passive_semantic_output_invalid"
    assert dead.attempt_count == 2


def test_semantic_worker_rejects_fabricated_content_with_valid_source_evidence(engine):
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)

    class FabricatedContentProvider(EchoSemanticProvider):
        def complete_json(self, *, system_prompt, user_payload, max_tokens=768):
            self.calls.append((system_prompt, user_payload, max_tokens))
            return {
                "schema_version": SEMANTIC_SCHEMA_VERSION,
                "items": [
                    {
                        "content": "I use MySQL for production.",
                        "category": "fact",
                        "confidence": 0.99,
                        "source_indices": [0],
                        "evidence": ["I use"],
                    }
                ],
            }

    provider = FabricatedContentProvider()
    job = _enqueue(
        store,
        provider,
        subject_id="passive-turn:fabricated",
        text="I use Postgres for production.",
    )
    worker = DurableSemanticMemoryWorker(
        engine,
        store,
        provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        retry_delay_seconds=0,
        autostart=False,
    )

    assert worker.run_once() is True

    failed = store.get(job_id=job.job_id, project_id=job.project_id)
    assert failed.status == "retry_wait"
    assert failed.failure_code == "passive_semantic_output_invalid"
    assert (
        engine._sqlite._conn.execute(
            "SELECT COUNT(*) FROM memory_proposals WHERE project_id = ?",
            ("project:alpha",),
        ).fetchone()[0]
        == 0
    )


def test_semantic_worker_rejects_inverted_negation_with_subset_tokens(engine):
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)

    class InvertedNegationProvider(EchoSemanticProvider):
        def complete_json(self, *, system_prompt, user_payload, max_tokens=768):
            self.calls.append((system_prompt, user_payload, max_tokens))
            return {
                "schema_version": SEMANTIC_SCHEMA_VERSION,
                "items": [
                    {
                        "content": "I prefer dark mode, and I do not prefer light mode.",
                        "category": "preference",
                        "confidence": 0.99,
                        "source_indices": [0],
                        "evidence": ["I do not prefer dark mode, and I prefer light mode."],
                    }
                ],
            }

    provider = InvertedNegationProvider()
    job = _enqueue(
        store,
        provider,
        subject_id="passive-turn:negation",
        text="I do not prefer dark mode, and I prefer light mode.",
    )
    worker = DurableSemanticMemoryWorker(
        engine,
        store,
        provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )

    assert worker.run_once() is True

    failed = store.get(job_id=job.job_id, project_id=job.project_id)
    assert failed.status == "retry_wait"
    assert failed.failure_code == "passive_semantic_output_invalid"
    assert engine._sqlite._conn.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0


def test_semantic_grounding_binds_chinese_negation_to_its_clause():
    assert (
        _content_is_grounded(
            "我喜欢深色模式，但我不喜欢浅色模式。",
            ["我不喜欢深色模式，但我喜欢浅色模式。"],
        )
        is False
    )


def test_semantic_worker_requires_evidence_for_every_selected_source(engine):
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)

    class MissingSourceEvidenceProvider(EchoSemanticProvider):
        def complete_json(self, *, system_prompt, user_payload, max_tokens=768):
            self.calls.append((system_prompt, user_payload, max_tokens))
            return {
                "schema_version": SEMANTIC_SCHEMA_VERSION,
                "items": [
                    {
                        "content": user_payload["inputs"][0]["user_text"],
                        "category": "fact",
                        "confidence": 0.99,
                        "source_indices": [0, 1],
                        "evidence": [user_payload["inputs"][0]["user_text"]],
                    }
                ],
            }

    provider = MissingSourceEvidenceProvider()
    first = _enqueue(
        store,
        provider,
        subject_id="passive-turn:covered",
        text="Use Postgres for production.",
    )
    second = _enqueue(
        store,
        provider,
        subject_id="passive-turn:uncovered",
        text="Keep Redis scoped to caching.",
    )
    worker = DurableSemanticMemoryWorker(
        engine,
        store,
        provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        retry_delay_seconds=0,
        autostart=False,
    )

    assert worker.run_once() is True

    assert store.get(job_id=first.job_id, project_id=first.project_id).failure_code == (
        "passive_semantic_output_invalid"
    )
    assert store.get(job_id=second.job_id, project_id=second.project_id).failure_code == (
        "passive_semantic_output_invalid"
    )
    assert (
        engine._sqlite._conn.execute(
            "SELECT COUNT(*) FROM memory_proposals WHERE project_id = ?",
            ("project:alpha",),
        ).fetchone()[0]
        == 0
    )


def test_non_user_semantic_payload_dies_before_provider_inference(engine):
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    provider = EchoSemanticProvider()
    text = "Assistant text must not become passive memory."
    content_hash = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    job = store.enqueue(
        project_id="project:alpha",
        visibility="project",
        config_revision=SEMANTIC_CONFIG_REVISION,
        job_kind=SEMANTIC_JOB_KIND,
        provider_identity=provider.identity,
        subject_id="passive-turn:assistant",
        subject_hash=content_hash,
        dedupe_key=f"semantic:assistant:{content_hash}",
        payload={
            "schema": SEMANTIC_SCHEMA_VERSION,
            "user_text": text,
            "origin_role": "assistant",
            "origin_turn_hash": content_hash,
        },
    ).job
    worker = DurableSemanticMemoryWorker(
        engine,
        store,
        provider,
        mode="on",
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )

    assert worker.run_once() is True

    failed = store.get(job_id=job.job_id, project_id=job.project_id)
    assert failed.status == "dead"
    assert failed.failure_code == "passive_semantic_source_invalid"
    assert provider.calls == []
