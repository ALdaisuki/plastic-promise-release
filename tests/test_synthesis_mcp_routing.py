"""MCP routing tests for governed synthesis lifecycle operations."""

from __future__ import annotations

import asyncio
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
from plastic_promise.core.memory_index import build_index_material, metadata_with_index_material
from plastic_promise.core.synthesis import SynthesisStore
from plastic_promise.mcp import server as mcp_server
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools import reflection as reflection_tools
from plastic_promise.mcp.tools.memory import (
    handle_memory_correct,
    handle_memory_forget,
    handle_memory_store,
    handle_memory_update,
)
from plastic_promise.mcp.tools.reflection import handle_feedback_apply
from plastic_promise.memory.pipeline import MemoryPipeline, PreparedMemory


def _version(conn) -> int:
    return int(conn.execute("SELECT version FROM memory_version").fetchone()[0])


def _payload(result) -> dict:
    return json.loads(result[0].text)


def _review_runtime(
    *,
    actor: str = "runtime-reviewer",
    call_id: str = "call:runtime-review",
    project_id: str = "project:test",
    trust_score: float = 0.95,
    defense_decision: str = "allow",
) -> dict:
    return {
        "actor": actor,
        "call_id": call_id,
        "project_id": project_id,
        "trust_score": trust_score,
        "trust_tier": "high",
        "defense_decision": defense_decision,
    }


class _RoutingEmbedder:
    model_name = "routing-test"

    def embed(self, text):
        assert text
        return [0.25] * 1024


def _prepare_routing_correction(_pipeline, current, new_content):
    normalized = " ".join(str(new_content or "").split())
    material = build_index_material(
        {"content": normalized},
        policy="legacy",
        model_name=_RoutingEmbedder.model_name,
    )
    metadata = metadata_with_index_material(current["metadata_json"], material)
    metadata.update(
        {
            "quality": {"status": "current"},
            "raw_content": normalized,
            "l0_abstract": material.vector_text,
            "l1_summary": material.search_text,
            "l2_content": normalized,
        }
    )
    return PreparedMemory(
        content=normalized,
        category=str(current["category"]),
        tier=str(current["tier"]),
        tags=tuple(current["tags"]),
        vector=tuple([0.25] * 1024),
        index_material=material,
        metadata=MappingProxyType(metadata),
    )


@pytest.fixture
def governed_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    storage = _SQLiteStorage(str(tmp_path / "synthesis-mcp.db"))
    sources = {
        "source-a": {
            "id": "source-a",
            "content": "Alpha evidence independently supports the durable conclusion. " * 4,
            "memory_type": "experience",
            "source": "user",
            "source_class": "user_fact",
            "project_id": "project:test",
            "visibility": "project",
            "origin_kind": "document",
            "origin_uri": "file:///alpha.md",
            "origin_ref": "alpha",
            "origin_hash": "origin-alpha",
            "metadata_json": {"status": "current"},
            "raw_content": "Alpha evidence independently supports the durable conclusion.",
            "l0_abstract": "Alpha durable evidence.",
            "l1_summary": "- alpha durable evidence",
            "l2_content": "Alpha evidence independently supports the durable conclusion.",
            "embedding_text": "Alpha durable evidence.",
            "embedding_hash": "sha256:source-a-before",
            "search_text": "alpha durable evidence",
        },
        "source-b": {
            "id": "source-b",
            "content": "Beta evidence confirms the same conclusion from another source. " * 4,
            "memory_type": "experience",
            "source": "agent",
            "source_class": "experience",
            "project_id": "project:test",
            "visibility": "project",
            "origin_kind": "document",
            "origin_uri": "file:///beta.md",
            "origin_ref": "beta",
            "origin_hash": "origin-beta",
            "metadata_json": {"quality_status": "verified"},
            "raw_content": "Beta evidence confirms the same conclusion from another source.",
            "l0_abstract": "Beta durable evidence.",
            "l1_summary": "- beta durable evidence",
            "l2_content": "Beta evidence confirms the same conclusion from another source.",
            "embedding_text": "Beta durable evidence.",
            "embedding_hash": "sha256:source-b-before",
            "search_text": "beta durable evidence",
        },
    }
    for source in sources.values():
        storage.upsert(source["id"], source)

    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    engine._loaded_memory_version = _version(storage._conn)
    engine.canonical_sync_ok = True
    engine._embedder = _RoutingEmbedder()
    engine.ensure_heavy_init = lambda: None
    yield engine
    storage._conn.close()


def _synthesis_args(**overrides) -> dict:
    args = {
        "content": "The independent evidence supports one stable reusable conclusion.",
        "memory_type": "synthesis",
        "source": "synthesis",
        "source_ids": ["source-a", "source-b"],
        "synthesis_key": "topic:mcp-routing",
        "validity_scope": "project:test",
        "project_id": "project:test",
        "visibility": "project",
        "actor": "codex",
        "call_id": "call-create",
        "automatic": False,
        "reuse_signal": False,
    }
    args.update(overrides)
    return args


def _forbid_generic_pipeline(monkeypatch) -> None:
    def fail(_engine):
        raise AssertionError("governed synthesis reached the generic memory pipeline")

    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", fail)


def test_memory_store_routes_synthesis_before_generic_pipeline(
    governed_engine,
    monkeypatch,
) -> None:
    _forbid_generic_pipeline(monkeypatch)

    payload = _payload(asyncio.run(handle_memory_store(governed_engine, _synthesis_args())))

    assert payload["success"] is True
    assert payload["stored"] is True
    assert payload["status"] == "draft"
    assert payload["revision"] == 1
    assert payload["support_count"] == 2
    assert payload["source_fingerprint"].startswith("sha256:")
    assert payload["trace"]["call_id"] == "call-create"
    assert (
        governed_engine._sqlite._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]
        == 0
    )


def test_governance_rejection_never_persists_synthesis_in_generic_outbox(
    governed_engine,
    monkeypatch,
) -> None:
    _forbid_generic_pipeline(monkeypatch)
    secret = "SYNTHESIS-GOVERNANCE-REJECTION-MUST-NOT-BE-QUEUED"

    payload = _payload(
        asyncio.run(
            handle_memory_store(
                governed_engine,
                _synthesis_args(
                    content=secret,
                    source_ids=["source-a"],
                    synthesis_key="topic:rejected",
                ),
            )
        )
    )

    assert payload["success"] is False
    assert payload["stored"] is False
    assert payload["degraded"] is False
    assert payload["fallback_used"] == []
    assert payload["reason"] == "insufficient_distinct_sources"
    assert secret not in json.dumps(payload)
    assert (
        governed_engine._sqlite._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]
        == 0
    )


def test_memory_store_refreshes_contested_synthesis_by_key(
    governed_engine,
    monkeypatch,
) -> None:
    _forbid_generic_pipeline(monkeypatch)
    created = _payload(asyncio.run(handle_memory_store(governed_engine, _synthesis_args())))
    synthesis_id = created["memory_id"]
    store = SynthesisStore(governed_engine._sqlite._conn, engine=governed_engine)
    store.mark_contested(synthesis_id, "source review", 1)

    refreshed = _payload(
        asyncio.run(
            handle_memory_store(
                governed_engine,
                _synthesis_args(
                    content="Refreshed conclusion after resolving the evidence review.",
                    expected_revision=1,
                    actor="reviewer",
                    call_id="call-refresh",
                ),
            )
        )
    )

    assert refreshed["success"] is True
    assert refreshed["memory_id"] == synthesis_id
    assert refreshed["status"] == "draft"
    assert refreshed["revision"] == 2


def test_feedback_adopted_verifies_synthesis_with_audit_identity(
    governed_engine,
    monkeypatch,
) -> None:
    _forbid_generic_pipeline(monkeypatch)
    created = _payload(asyncio.run(handle_memory_store(governed_engine, _synthesis_args())))

    payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                governed_engine,
                {
                    "item_id": created["memory_id"],
                    "feedback_type": "adopted",
                    "actor": "declared-reviewer",
                    "call_id": "call:declared-verify",
                    "expected_revision": 1,
                },
                _runtime_context=_review_runtime(call_id="call:runtime-verify"),
            )
        )
    )

    assert payload["updated"] is True
    assert payload["status"] == "verified"
    assert payload["revision"] == 1
    assert payload["verified_by_actor"] == "runtime-reviewer"
    assert payload["verified_by_call_id"] == "call:runtime-verify"


def test_feedback_rejected_contests_synthesis_and_ignored_is_rejected(
    governed_engine,
    monkeypatch,
) -> None:
    _forbid_generic_pipeline(monkeypatch)
    created = _payload(asyncio.run(handle_memory_store(governed_engine, _synthesis_args())))
    synthesis_id = created["memory_id"]

    ignored = _payload(
        asyncio.run(
            handle_feedback_apply(
                governed_engine,
                {
                    "item_id": synthesis_id,
                    "feedback_type": "ignored",
                    "actor": "reviewer",
                    "call_id": "call-ignore",
                    "expected_revision": 1,
                },
            )
        )
    )
    assert ignored["updated"] is False
    assert ignored["reason"] == "synthesis_feedback_ignored_not_allowed"
    assert SynthesisStore(governed_engine._sqlite._conn).get(synthesis_id).status == "draft"

    rejected = _payload(
        asyncio.run(
            handle_feedback_apply(
                governed_engine,
                {
                    "item_id": synthesis_id,
                    "feedback_type": "rejected",
                    "actor": "reviewer",
                    "call_id": "call-reject",
                    "expected_revision": 1,
                    "rejection_reason": "conflicting evidence",
                },
                _runtime_context=_review_runtime(call_id="call:runtime-reject"),
            )
        )
    )
    assert rejected["updated"] is True
    assert rejected["status"] == "contested"
    assert rejected["stale_reason"] == "conflicting evidence"


def test_feedback_synthesis_public_reviewer_fields_cannot_authorize(
    governed_engine,
    monkeypatch,
) -> None:
    _forbid_generic_pipeline(monkeypatch)
    created = _payload(asyncio.run(handle_memory_store(governed_engine, _synthesis_args())))

    payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                governed_engine,
                {
                    "item_id": created["memory_id"],
                    "feedback_type": "adopted",
                    "actor": "forged-reviewer",
                    "call_id": "call:forged",
                    "project_id": "project:test",
                    "trust_score": 1.0,
                    "trust_tier": "high",
                    "defense_decision": "allow",
                    "_runtime_context": _review_runtime(),
                    "expected_revision": 1,
                },
            )
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == "feedback_runtime_authorization_required"
    assert SynthesisStore(governed_engine._sqlite._conn).get(created["memory_id"]).status == "draft"


@pytest.mark.parametrize(
    ("runtime", "reason"),
    [
        (_review_runtime(trust_score=0.20), "feedback_runtime_authorization_denied"),
        (
            _review_runtime(defense_decision="deny"),
            "feedback_runtime_authorization_denied",
        ),
        (
            _review_runtime(project_id="project:other"),
            "feedback_project_mismatch",
        ),
    ],
)
def test_feedback_synthesis_requires_runtime_trust_and_project(
    governed_engine,
    monkeypatch,
    runtime,
    reason,
) -> None:
    _forbid_generic_pipeline(monkeypatch)
    created = _payload(
        asyncio.run(
            handle_memory_store(
                governed_engine,
                _synthesis_args(synthesis_key=f"topic:auth-{reason}-{runtime['project_id']}"),
            )
        )
    )

    payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                governed_engine,
                {
                    "item_id": created["memory_id"],
                    "feedback_type": "adopted",
                    "expected_revision": 1,
                },
                _runtime_context=runtime,
            )
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == reason
    assert SynthesisStore(governed_engine._sqlite._conn).get(created["memory_id"]).status == "draft"


def _verified_public_dependent(engine, *, key: str):
    store = SynthesisStore(engine._sqlite._conn, engine=engine)
    draft = store.create_draft(
        "A verified public dependent combines both ordinary source records.",
        ["source-a", "source-b"],
        synthesis_key=key,
        validity_scope="project:test",
        project_id="project:test",
        visibility="project",
        actor="codex",
        call_id=f"call:draft:{key}",
    )
    assert draft is not None
    return store, store.verify(
        draft.memory_id,
        "runtime-reviewer",
        f"call:verify:{key}",
        draft.revision,
    )


def _mutation_snapshot(engine):
    conn = engine._sqlite._conn
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in (
            "memories",
            "synthesis_artifacts",
            "memory_lineage",
            "store_outbox",
            "memory_version",
        )
    }


@pytest.mark.asyncio
async def test_public_memory_correct_stales_dependent_before_response(
    governed_engine,
    monkeypatch,
):
    from plastic_promise.core import synthesis_maintenance

    store, dependent = _verified_public_dependent(
        governed_engine,
        key="topic:public-correction",
    )
    monkeypatch.setattr(MemoryPipeline, "prepare_correction", _prepare_routing_correction)
    monkeypatch.setattr(synthesis_maintenance, "replay_memory_index_jobs", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        synthesis_maintenance,
        "replay_synthesis_index_jobs",
        lambda *_a, **_k: 0,
    )

    payload = _payload(
        await handle_memory_correct(
            governed_engine,
            {
                "memory_id": "source-a",
                "content": "Corrected alpha evidence changes the supported conclusion.",
                "mark_as": "corrected",
                "reason": "reviewed source correction",
            },
            _runtime_context=_review_runtime(call_id="call:public-correct"),
        )
    )

    assert payload["corrected"] is True
    assert payload["committed"] is True
    assert payload["operation"] == "corrected"
    assert payload["reason"] == ""
    assert payload["stale_dependents"] == [dependent.memory_id]
    assert payload["ordinary_index_job_id"]
    assert len(payload["synthesis_index_job_ids"]) == 1
    assert set(payload["pending_job_ids"]) == {
        payload["ordinary_index_job_id"],
        *payload["synthesis_index_job_ids"],
    }
    assert payload["completed_job_ids"] == []
    assert store.get(dependent.memory_id).status == "stale"
    assert governed_engine._sqlite.get("source-a")["content"].startswith("Corrected alpha")


@pytest.mark.asyncio
async def test_memory_update_content_invalidates_but_importance_only_does_not(
    governed_engine,
    monkeypatch,
):
    from plastic_promise.core import synthesis_maintenance

    store, dependent = _verified_public_dependent(
        governed_engine,
        key="topic:public-update",
    )
    monkeypatch.setattr(MemoryPipeline, "prepare_correction", _prepare_routing_correction)
    monkeypatch.setattr(synthesis_maintenance, "replay_memory_index_jobs", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        synthesis_maintenance,
        "replay_synthesis_index_jobs",
        lambda *_a, **_k: 0,
    )

    content_payload = _payload(
        await handle_memory_update(
            governed_engine,
            {
                "memory_id": "source-a",
                "content": "Updated alpha evidence invalidates dependent conclusions.",
            },
            _runtime_context=_review_runtime(call_id="call:public-update"),
        )
    )
    assert content_payload["updated"] is True
    assert content_payload["committed"] is True
    assert content_payload["stale_dependents"] == [dependent.memory_id]
    assert store.get(dependent.memory_id).status == "stale"

    conn = governed_engine._sqlite._conn
    before_lineage = conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
    before_jobs = conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]
    metadata_payload = _payload(
        await handle_memory_update(
            governed_engine,
            {"memory_id": "source-b", "importance": 0.33},
            _runtime_context=_review_runtime(call_id="call:metadata-update"),
        )
    )
    assert metadata_payload == {
        "updated": True,
        "committed": True,
        "memory_id": "source-b",
        "operation": "metadata_patch",
        "reason": "",
        "stale_dependents": [],
        "ordinary_index_job_id": "",
        "synthesis_index_job_ids": [],
        "pending_job_ids": [],
        "completed_job_ids": [],
    }
    assert governed_engine._sqlite.get("source-b")["importance"] == 0.33
    assert conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0] == before_lineage
    assert conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0] == before_jobs


@pytest.mark.asyncio
async def test_memory_correct_rejects_ambiguous_operations_without_mutation(
    governed_engine,
):
    original_content = governed_engine._sqlite.get("source-a")["content"]
    trusted = _review_runtime(call_id="call:ambiguous-correct")
    for args, reason in (
        ({"memory_id": "source-a", "mark_as": "corrected"}, "correction_content_required"),
        (
            {
                "memory_id": "source-a",
                "mark_as": "wrong",
                "content": "replacement",
            },
            "wrong_content_not_allowed",
        ),
        (
            {
                "memory_id": "source-a",
                "mark_as": "corrected",
                "content": original_content,
            },
            "correction_content_unchanged",
        ),
    ):
        before = _mutation_snapshot(governed_engine)
        payload = _payload(
            await handle_memory_correct(
                governed_engine,
                args,
                _runtime_context=trusted,
            )
        )
        assert payload["corrected"] is False
        assert payload["committed"] is False
        assert payload["reason"] == reason
        assert _mutation_snapshot(governed_engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "args", "outcome_key"),
    [
        (
            handle_memory_update,
            {"memory_id": "source-a", "content": "Unauthorized replacement."},
            "updated",
        ),
        (
            handle_memory_update,
            {"memory_id": "source-a", "importance": 0.15},
            "updated",
        ),
        (handle_memory_forget, {"memory_id": "source-a"}, "forgotten"),
        (
            handle_memory_correct,
            {"memory_id": "source-a", "mark_as": "wrong"},
            "corrected",
        ),
    ],
)
@pytest.mark.parametrize(
    ("runtime", "reason"),
    [
        (
            _review_runtime(trust_score=0.20, defense_decision="deny"),
            "ordinary_mutation_runtime_authorization_denied",
        ),
        (
            _review_runtime(project_id="project:other"),
            "ordinary_mutation_project_mismatch",
        ),
    ],
)
async def test_public_source_mutations_reject_untrusted_runtime_without_writes(
    governed_engine,
    handler,
    args,
    outcome_key,
    runtime,
    reason,
):
    before = _mutation_snapshot(governed_engine)

    payload = _payload(
        await handler(
            governed_engine,
            args,
            _runtime_context=runtime,
        )
    )

    assert payload[outcome_key] is False
    assert payload["committed"] is False
    assert payload["reason"] == reason
    assert _mutation_snapshot(governed_engine) == before


@pytest.mark.asyncio
async def test_public_source_mutation_fails_closed_without_canonical_project(
    governed_engine,
    monkeypatch,
):
    before = _mutation_snapshot(governed_engine)
    monkeypatch.setattr(
        governed_engine,
        "get_memory_dict_for_review",
        lambda _memory_id: {"id": "source-a"},
    )

    payload = _payload(
        await handle_memory_update(
            governed_engine,
            {"memory_id": "source-a", "content": "Must not commit."},
            _runtime_context=_review_runtime(),
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == "ordinary_mutation_source_project_required"
    assert _mutation_snapshot(governed_engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_result", [None, object()])
async def test_public_source_mutation_ignores_project_bearing_fallback_when_canonical_missing(
    governed_engine,
    monkeypatch,
    canonical_result,
):
    before = _mutation_snapshot(governed_engine)
    canonical = governed_engine._sqlite.get("source-a")
    monkeypatch.setattr(governed_engine, "get_memory", lambda _memory_id: canonical)
    monkeypatch.setattr(
        governed_engine,
        "get_memory_dict_for_review",
        lambda _memory_id: canonical_result,
    )

    payload = _payload(
        await handle_memory_update(
            governed_engine,
            {"memory_id": "source-a", "content": "Must not commit."},
            _runtime_context=_review_runtime(),
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == "ordinary_mutation_source_project_required"
    assert _mutation_snapshot(governed_engine) == before


@pytest.mark.asyncio
async def test_public_source_mutation_ignores_project_bearing_fallback_when_canonical_read_fails(
    governed_engine,
    monkeypatch,
):
    before = _mutation_snapshot(governed_engine)
    canonical = governed_engine._sqlite.get("source-a")
    monkeypatch.setattr(governed_engine, "get_memory", lambda _memory_id: canonical)

    def fail_review_read(_memory_id):
        raise RuntimeError("canonical read failed")

    monkeypatch.setattr(
        governed_engine,
        "get_memory_dict_for_review",
        fail_review_read,
    )

    payload = _payload(
        await handle_memory_update(
            governed_engine,
            {"memory_id": "source-a", "content": "Must not commit."},
            _runtime_context=_review_runtime(),
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == "ordinary_mutation_source_project_required"
    assert _mutation_snapshot(governed_engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "args", "outcome_key"),
    [
        (
            handle_memory_update,
            {"memory_id": "source-a", "content": "Concurrent replacement."},
            "updated",
        ),
        (handle_memory_forget, {"memory_id": "source-a"}, "forgotten"),
        (
            handle_memory_correct,
            {"memory_id": "source-a", "mark_as": "wrong"},
            "corrected",
        ),
    ],
)
async def test_public_source_mutation_rechecks_project_inside_coordinator_transaction(
    governed_engine,
    monkeypatch,
    handler,
    args,
    outcome_key,
):
    conn = governed_engine._sqlite._conn
    original_mutate = governed_engine.mutate_ordinary_source
    before = governed_engine._sqlite.get("source-a")
    before_version = _version(conn)
    before_lineage = conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
    before_jobs = conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]

    def move_project_then_mutate(memory_id, **kwargs):
        conn.execute(
            "UPDATE memories SET project_id = ? WHERE id = ?",
            ("project:other", memory_id),
        )
        conn.commit()
        return original_mutate(memory_id, **kwargs)

    monkeypatch.setattr(
        governed_engine,
        "mutate_ordinary_source",
        move_project_then_mutate,
    )

    payload = _payload(
        await handler(
            governed_engine,
            args,
            _runtime_context=_review_runtime(),
        )
    )

    after = governed_engine._sqlite.get("source-a")
    assert payload[outcome_key] is False
    assert payload["reason"] == "ordinary_source_precondition_mismatch"
    assert after["project_id"] == "project:other"
    assert after["content"] == before["content"]
    assert after["tags"] == before["tags"]
    assert after["metadata_json"] == before["metadata_json"]
    assert _version(conn) == before_version
    assert conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0] == before_lineage
    assert conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0] == before_jobs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"memory_id": "source-a", "importance": 0.17},
        {"memory_id": "source-a", "category": "decision"},
        {"memory_id": "source-a", "reset_worth": True},
    ],
)
async def test_public_metadata_mutation_rechecks_project_inside_patch_transaction(
    governed_engine,
    monkeypatch,
    args,
):
    conn = governed_engine._sqlite._conn
    conn.execute(
        "UPDATE memories SET worth_success = 3, worth_failure = 2 WHERE id = ?",
        ("source-a",),
    )
    conn.commit()
    original_patch = governed_engine.patch_ordinary_memory
    before = governed_engine._sqlite.get("source-a")
    before_version = _version(conn)

    def move_project_then_patch(memory_id, **kwargs):
        conn.execute(
            "UPDATE memories SET project_id = ? WHERE id = ?",
            ("project:other", memory_id),
        )
        conn.commit()
        return original_patch(memory_id, **kwargs)

    monkeypatch.setattr(
        governed_engine,
        "patch_ordinary_memory",
        move_project_then_patch,
    )

    payload = _payload(
        await handle_memory_update(
            governed_engine,
            args,
            _runtime_context=_review_runtime(),
        )
    )

    after = governed_engine._sqlite.get("source-a")
    assert payload["updated"] is False
    assert payload["reason"] == "ordinary_metadata_update_failed"
    assert after["project_id"] == "project:other"
    assert after["importance"] == before["importance"]
    assert after["category"] == before["category"]
    assert after["worth_success"] == before["worth_success"]
    assert after["worth_failure"] == before["worth_failure"]
    assert _version(conn) == before_version


@pytest.mark.asyncio
@pytest.mark.parametrize("feedback_type", ["adopted", "rejected", "ignored"])
@pytest.mark.parametrize(
    ("runtime", "reason"),
    [
        (
            _review_runtime(trust_score=0.20, defense_decision="deny"),
            "feedback_runtime_authorization_denied",
        ),
        (_review_runtime(project_id="project:other"), "feedback_project_mismatch"),
    ],
)
async def test_public_ordinary_feedback_requires_runtime_trust_and_project(
    governed_engine,
    feedback_type,
    runtime,
    reason,
):
    conn = governed_engine._sqlite._conn
    before = governed_engine._sqlite.get("source-a")
    before_version = _version(conn)

    payload = _payload(
        await handle_feedback_apply(
            governed_engine,
            {"item_id": "source-a", "feedback_type": feedback_type},
            _runtime_context=runtime,
        )
    )

    after = governed_engine._sqlite.get("source-a")
    assert payload["updated"] is False
    assert payload["reason"] == reason
    assert after["worth_success"] == before["worth_success"]
    assert after["worth_failure"] == before["worth_failure"]
    assert _version(conn) == before_version


@pytest.mark.asyncio
async def test_public_ordinary_feedback_rechecks_project_inside_patch_transaction(
    governed_engine,
    monkeypatch,
):
    conn = governed_engine._sqlite._conn
    original_feedback = governed_engine.apply_ordinary_feedback
    before = governed_engine._sqlite.get("source-a")
    before_version = _version(conn)

    def move_project_then_feedback(memory_id, feedback_type, **kwargs):
        conn.execute(
            "UPDATE memories SET project_id = ? WHERE id = ?",
            ("project:other", memory_id),
        )
        conn.commit()
        return original_feedback(memory_id, feedback_type, **kwargs)

    monkeypatch.setattr(
        governed_engine,
        "apply_ordinary_feedback",
        move_project_then_feedback,
    )

    payload = _payload(
        await handle_feedback_apply(
            governed_engine,
            {"item_id": "source-a", "feedback_type": "adopted"},
            _runtime_context=_review_runtime(),
        )
    )

    after = governed_engine._sqlite.get("source-a")
    assert payload["updated"] is False
    assert payload["reason"] == "ordinary_patch_cas_mismatch"
    assert after["project_id"] == "project:other"
    assert after["worth_success"] == before["worth_success"]
    assert after["worth_failure"] == before["worth_failure"]
    assert _version(conn) == before_version


@pytest.mark.asyncio
async def test_public_ordinary_feedback_rejects_unavailable_tombstone(
    governed_engine,
):
    forgotten = _payload(
        await handle_memory_forget(
            governed_engine,
            {"memory_id": "source-a", "reason": "remove invalid source"},
            _runtime_context=_review_runtime(),
        )
    )
    assert forgotten["forgotten"] is True
    conn = governed_engine._sqlite._conn
    before = governed_engine._sqlite.get("source-a")
    before_version = _version(conn)

    payload = _payload(
        await handle_feedback_apply(
            governed_engine,
            {"item_id": "source-a", "feedback_type": "ignored"},
            _runtime_context=_review_runtime(),
        )
    )

    after = governed_engine._sqlite.get("source-a")
    assert payload["updated"] is False
    assert payload["reason"] == "ordinary_patch_source_unavailable"
    assert after["worth_success"] == before["worth_success"]
    assert after["worth_failure"] == before["worth_failure"]
    assert _version(conn) == before_version


@pytest.mark.asyncio
async def test_public_metadata_update_rejects_unavailable_tombstone(
    governed_engine,
):
    forgotten = _payload(
        await handle_memory_forget(
            governed_engine,
            {"memory_id": "source-a", "reason": "remove invalid source"},
            _runtime_context=_review_runtime(),
        )
    )
    assert forgotten["forgotten"] is True
    conn = governed_engine._sqlite._conn
    before = governed_engine._sqlite.get("source-a")
    before_version = _version(conn)

    payload = _payload(
        await handle_memory_update(
            governed_engine,
            {"memory_id": "source-a", "importance": 0.91},
            _runtime_context=_review_runtime(),
        )
    )

    after = governed_engine._sqlite.get("source-a")
    assert payload["updated"] is False
    assert payload["reason"] == "not_found"
    assert after["importance"] == before["importance"]
    assert _version(conn) == before_version


@pytest.mark.asyncio
async def test_public_ordinary_feedback_reports_committed_graph_sync_degradation(
    governed_engine,
    monkeypatch,
):
    monkeypatch.setattr(
        governed_engine,
        "apply_edge_feedback_for_memory",
        lambda _memory_id: (_ for _ in ()).throw(RuntimeError("graph unavailable")),
    )

    payload = _payload(
        await handle_feedback_apply(
            governed_engine,
            {"item_id": "source-a", "feedback_type": "adopted"},
            _runtime_context=_review_runtime(),
        )
    )

    assert payload["updated"] is True
    assert payload["committed"] is True
    assert payload["partial"] is True
    assert payload["graph_sync_pending"] is True
    assert payload["degraded"] == ["graph_feedback_sync"]
    assert payload["error_class"] == "RuntimeError"
    assert governed_engine._sqlite.get("source-a")["worth_success"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "module", "handler_name"),
    [
        ("memory_update", memory_tools, "handle_memory_update"),
        ("memory_forget", memory_tools, "handle_memory_forget"),
        ("memory_correct", memory_tools, "handle_memory_correct"),
        ("feedback_apply", reflection_tools, "handle_feedback_apply"),
    ],
)
async def test_server_dispatch_injects_private_mutation_runtime_context(
    monkeypatch,
    tool_name,
    module,
    handler_name,
):
    runtime = _review_runtime(call_id=f"call:runtime:{tool_name}")
    captured = []

    async def capture(_engine, _args, *, _runtime_context=None):
        captured.append(_runtime_context)
        return []

    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mcp_server,
        "_mutation_runtime_context",
        lambda requested_tool, _arguments=None: {**runtime, "tool_name": requested_tool},
        raising=False,
    )
    monkeypatch.setattr(module, handler_name, capture)

    await mcp_server.call_tool(
        tool_name,
        {
            "memory_id": "source-a",
            "item_id": "source-a",
            "actor": "forged-client",
            "call_id": "call:forged",
            "project_id": "project:forged",
            "trust_score": 1.0,
        },
    )

    assert captured == [{**runtime, "tool_name": tool_name}]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["smart-remember", "smart_remember"])
async def test_smart_remember_aliases_use_server_owned_memory_update_authority(
    monkeypatch,
    tool_name,
):
    runtime = _review_runtime(
        actor="pi_builder",
        call_id=f"call:runtime:{tool_name}",
    )
    runtime_requests = []
    skill_calls = []

    class CapturingSkillEngine:
        async def exec(self, skill_name, params, caller):
            skill_calls.append((skill_name, params, caller))
            return SimpleNamespace(
                skill_name=skill_name,
                success=True,
                data={"action": "unchanged"},
                degrade_log=[],
                errors=[],
                audit_trail={},
            )

    def runtime_context(requested_tool, _arguments=None):
        runtime_requests.append(requested_tool)
        return dict(runtime)

    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", CapturingSkillEngine)
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *_a, **_k: None)
    monkeypatch.setattr(mcp_server, "_mutation_runtime_context", runtime_context)

    await mcp_server.call_tool(
        tool_name,
        {
            "content": "Existing evidence",
            "memory_type": "experience",
            "project_id": "project:test",
            "actor": "forged-client",
            "call_id": "call:forged",
            "trust_score": 1.0,
            "defense_decision": "allow",
            "_runtime_context": {
                **_review_runtime(actor="forged-client"),
                "call_id": "call:forged-private",
            },
        },
    )

    assert runtime_requests == ["memory_update"]
    assert len(skill_calls) == 1
    skill_name, params, caller = skill_calls[0]
    assert skill_name == "smart-remember"
    assert params["_runtime_context"] == runtime
    assert caller == "pi"


@pytest.mark.parametrize(
    "actor,expected_caller",
    [
        ("claude", "claude"),
        ("codex", "claude"),
        ("mcp", "claude"),
        ("pi_builder", "pi"),
        ("pi", "pi"),
        ("external_agent", "external_agent"),
        ("", ""),
    ],
)
def test_smart_remember_runtime_caller_preserves_skill_role_boundary(
    actor,
    expected_caller,
):
    assert mcp_server._smart_remember_runtime_caller({"actor": actor}) == expected_caller


@pytest.mark.asyncio
async def test_step_closure_internal_smart_remember_uses_server_authority(
    monkeypatch,
):
    from plastic_promise.loop import soul_loop
    from plastic_promise.skills import engine as skill_engine_module

    runtime = _review_runtime(
        actor="pi_builder",
        call_id="call:runtime:step-closure-smart",
    )
    runtime_requests = []
    skill_calls = []

    class CapturingSkillEngine:
        def __init__(self, _engine):
            pass

        def register(self, _skill):
            pass

        async def exec(self, skill_name, params, caller):
            skill_calls.append((skill_name, params, caller))
            return SimpleNamespace(
                success=True,
                data={"memory_id": "memory:closure"},
            )

    def runtime_context(requested_tool, _arguments=None):
        runtime_requests.append(requested_tool)
        return dict(runtime)

    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *_a, **_k: None)
    monkeypatch.setattr(mcp_server, "_mutation_runtime_context", runtime_context)
    monkeypatch.setattr(mcp_server, "_closure_history", [])
    monkeypatch.setattr(skill_engine_module, "SkillEngine", CapturingSkillEngine)
    monkeypatch.setattr(
        soul_loop,
        "post_task",
        lambda *_a, **_k: {
            "scarf": {"summary": {"overall_score": 0.8}},
            "trust": {"score": 0.8},
            "cei": {"score": 0.8, "tier": "stable"},
            "hormone": {"trust_delta": 0.0},
            "reflection": {"step_id": "step:test"},
        },
    )

    await mcp_server.call_tool(
        "step-closure",
        {
            "task_description": "Verify internal smart authority",
            "mode": "full",
            "lesson": "Server authority must cross the internal skill boundary.",
        },
    )

    assert runtime_requests == ["memory_update"]
    assert len(skill_calls) == 1
    skill_name, params, caller = skill_calls[0]
    assert skill_name == "smart-remember"
    assert params["_runtime_context"] == runtime
    assert caller == "pi"


def test_existing_tools_expose_governed_synthesis_fields_without_new_tool() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.list_tools())}

    memory_store_fields = set(tools["memory_store"].inputSchema["properties"])
    assert {
        "source_ids",
        "synthesis_key",
        "validity_scope",
        "automatic",
        "reuse_signal",
        "expected_revision",
        "actor",
        "call_id",
    } <= memory_store_fields
    feedback_fields = set(tools["feedback_apply"].inputSchema["properties"])
    assert {
        "actor",
        "call_id",
        "expected_revision",
        "rejection_reason",
        "stage_session_id",
        "flow_line_id",
        "request_id",
    } <= feedback_fields
    assert "synthesis_store" not in tools
