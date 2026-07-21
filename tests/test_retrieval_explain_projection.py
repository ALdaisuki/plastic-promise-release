import asyncio
import json
from types import SimpleNamespace

import pytest

from plastic_promise.core.context_engine import ContextItem, ContextPack
from plastic_promise.core.project_context import infer_project_context
from plastic_promise.core.retrieval_explain import (
    build_retrieval_explain_snapshot,
    sanitize_retrieval_explain_snapshot,
)
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools.memory import _sanitize_pack_for_project


@pytest.fixture(autouse=True)
def _clear_recall_cache():
    with memory_tools._query_cache_lock:
        memory_tools._query_cache.clear()
    yield
    with memory_tools._query_cache_lock:
        memory_tools._query_cache.clear()


def _debug_pack() -> ContextPack:
    return ContextPack(
        core=[
            ContextItem(
                id="m1",
                content="private memory content",
                relevance=0.91,
                source="test",
                layer="core",
            )
        ],
        audit_metadata={
            "engine_mode": "snapshot",
            "mode": "hybrid",
            "budget": {"core": 20, "related": 20, "divergent": 20, "raw_evidence": 10},
            "retrieval_fusion": {
                "effective_policy": "wrrf-v1",
                "effective_runtime": "python",
                "algorithm": "weighted-rrf-v1",
                "query": "must not leak",
            },
            "degraded": False,
            "prompt": "must not leak",
            "api_key": "sk-live-must-not-leak",
        },
        pipeline_stats={
            "vector_count": 4,
            "bm25_count": "3",
            "after_hard_score_filter": 2,
            "after_mmr": 1,
            "fallback_reason": "none",
            "query": "must not leak",
            "content": "must not leak",
            "embedding": [0.1, 0.2],
            "token": "must-not-leak",
            "nan": float("nan"),
        },
        per_item_stats=[
            {
                "id": "m1",
                "initial_score": 0.91,
                "final_score": 0.82,
                "source_penalty": 1.0,
                "filter_decision": "keep",
                "filter_reason": "passed",
                "gate_decision": "core",
                "retrieval_source": "vector",
                "content": "must not leak",
                "prompt": "must not leak",
                "vector": [0.1, 0.2],
                "password": "must-not-leak",
            }
        ],
        channel_rankings={
            "vector": [
                {
                    "memory_id": "m1",
                    "score": 0.91,
                    "rank": 1,
                    "content": "must not leak",
                    "secret": "must-not-leak",
                }
            ]
        },
        channel_states={
            "vector": {
                "planned": True,
                "enabled": True,
                "available": True,
                "executed": True,
                "participating": True,
                "evidence_only": False,
                "reason": "participating",
                "result_count": 1,
                "api_key": "sk-live-must-not-leak",
            }
        },
    )


def test_stored_explain_snapshot_is_reprojected_through_the_fixed_allowlist():
    snapshot = {
        "schema": "retrieval_explain_v1",
        "content": "TOP_LEVEL_MEMORY_SECRET",
        "query": "TOP_LEVEL_QUERY_SECRET",
        "channels": [
            {
                "name": "vector",
                "content": "CHANNEL_CONTENT_SECRET",
                "state": {
                    "planned": True,
                    "reason": "available",
                    "prompt": "CHANNEL_PROMPT_SECRET",
                },
                "items": [
                    {
                        "id": "mem-safe",
                        "rank": 1,
                        "score": 0.91,
                        "content": "CHANNEL_ITEM_SECRET",
                        "embedding": [0.1, 0.2],
                    }
                ],
            }
        ],
        "items": [
            {
                "id": "mem-safe",
                "rank": 1,
                "final_score": 0.91,
                "layer": "core",
                "content": "ITEM_CONTENT_SECRET",
                "query": "ITEM_QUERY_SECRET",
            }
        ],
        "pipeline": {
            "candidate_count": 1,
            "fusion_policy": "wrrf-v1",
            "prompt": "PIPELINE_PROMPT_SECRET",
        },
        "truncated": {"items": True},
    }

    sanitized = sanitize_retrieval_explain_snapshot(snapshot)

    assert sanitized == {
        "schema": "retrieval_explain_v1",
        "channels": [
            {
                "name": "vector",
                "state": {"planned": True, "reason": "available"},
                "items": [{"id": "mem-safe", "rank": 1, "score": 0.91}],
            }
        ],
        "items": [
            {
                "id": "mem-safe",
                "rank": 1,
                "final_score": 0.91,
                "layer": "core",
            }
        ],
        "pipeline": {"candidate_count": 1, "fusion_policy": "wrrf-v1"},
        "truncated": {"channels": False, "channel_items": False, "items": True},
    }
    rendered = json.dumps(sanitized)
    for secret in (
        "TOP_LEVEL_MEMORY_SECRET",
        "TOP_LEVEL_QUERY_SECRET",
        "CHANNEL_CONTENT_SECRET",
        "CHANNEL_PROMPT_SECRET",
        "CHANNEL_ITEM_SECRET",
        "ITEM_CONTENT_SECRET",
        "ITEM_QUERY_SECRET",
        "PIPELINE_PROMPT_SECRET",
    ):
        assert secret not in rendered


def test_rust_stage_timing_json_is_projected_to_canonical_timings(monkeypatch):
    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")
    pack = ContextPack(
        pipeline_stats={
            "stage_timing_ms": json.dumps(
                {
                    "principle_injection": "0.125",
                    "snapshot_parse": 1.25,
                    "candidate_retrieval": 4.567,
                    "filter_and_layer": 1.234,
                    "total": "7.176",
                }
            )
        }
    )

    snapshot = build_retrieval_explain_snapshot(pack)

    assert snapshot is not None
    assert snapshot["pipeline"]["stage_timings"] == {
        "principle_injection": 0.125,
        "snapshot_parse": 1.25,
        "candidate_retrieval": 4.567,
        "filter_and_layer": 1.234,
        "total": 7.176,
    }
    assert "stage_timing_ms" not in snapshot["pipeline"]


@pytest.mark.parametrize("source_field", ["stage_timing_ms", "stage_timings"])
def test_stage_timing_mapping_is_strictly_sanitized(monkeypatch, source_field):
    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")
    pack = ContextPack(
        pipeline_stats={
            source_field: {
                "principle_injection": 0,
                "snapshot_parse": -1,
                "candidate_retrieval": float("nan"),
                "filter_and_layer": float("inf"),
                "fallback_filter_and_layer": False,
                "total": 86_400_000.001,
                "untrusted_stage": 12.5,
            }
        }
    )

    snapshot = build_retrieval_explain_snapshot(pack)

    assert snapshot is not None
    assert snapshot["pipeline"]["stage_timings"] == {"principle_injection": 0.0}


def test_stored_stage_timings_are_idempotently_sanitized():
    snapshot = {
        "schema": "retrieval_explain_v1",
        "channels": [],
        "items": [],
        "pipeline": {
            "stage_timing_ms": '{"candidate_retrieval":"2.5","total":"3.75"}',
        },
        "truncated": {},
    }

    sanitized = sanitize_retrieval_explain_snapshot(snapshot)

    assert sanitized is not None
    assert sanitized["pipeline"] == {
        "stage_timings": {"candidate_retrieval": 2.5, "total": 3.75}
    }
    assert sanitize_retrieval_explain_snapshot(sanitized) == sanitized


def test_explain_snapshot_preserves_structural_chunk_anchor_without_body_text():
    snapshot = {
        "schema": "retrieval_explain_v1",
        "channels": [
            {
                "name": "vector",
                "state": {"participating": True},
                "items": [
                    {
                        "id": "mem-1",
                        "rank": 1,
                        "score": 0.92,
                        "chunk_id": "chunk_abc",
                        "parent_memory_id": "mem-1",
                        "ordinal": 0,
                        "kind": "paragraph",
                        "header_path": ["检索", "证据"],
                        "source_start": 0,
                        "source_end": 18,
                        "source_hash": "a" * 64,
                        "text_hash": "b" * 64,
                        "text": "CHUNK_BODY_MUST_NOT_LEAK",
                    }
                ],
            }
        ],
        "items": [
            {
                "id": "mem-1",
                "rank": 1,
                "final_score": 0.88,
                "chunk_id": "chunk_abc",
                "parent_memory_id": "mem-1",
                "ordinal": 0,
                "kind": "paragraph",
                "header_path": ["检索", "证据"],
                "source_start": 0,
                "source_end": 18,
                "source_hash": "a" * 64,
                "text_hash": "b" * 64,
                "content": "MEMORY_BODY_MUST_NOT_LEAK",
            }
        ],
        "pipeline": {},
        "truncated": {},
    }

    sanitized = sanitize_retrieval_explain_snapshot(snapshot)

    expected_anchor = {
        "chunk_id": "chunk_abc",
        "parent_memory_id": "mem-1",
        "kind": "paragraph",
        "source_hash": "a" * 64,
        "text_hash": "b" * 64,
        "ordinal": 0,
        "source_start": 0,
        "source_end": 18,
        "header_path": ["检索", "证据"],
    }
    assert sanitized["items"][0] == {
        "id": "mem-1",
        "rank": 1,
        "final_score": 0.88,
        **expected_anchor,
    }
    assert sanitized["channels"][0]["items"][0] == {
        "id": "mem-1",
        "rank": 1,
        "score": 0.92,
        **expected_anchor,
    }
    rendered = json.dumps(sanitized)
    assert "CHUNK_BODY_MUST_NOT_LEAK" not in rendered
    assert "MEMORY_BODY_MUST_NOT_LEAK" not in rendered


def test_stored_explain_snapshot_rejects_unknown_schema():
    assert sanitize_retrieval_explain_snapshot({"schema": "legacy", "items": []}) is None


def test_schema_only_snapshot_is_not_reported_as_captured(monkeypatch):
    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")

    assert sanitize_retrieval_explain_snapshot(
        {"schema": "retrieval_explain_v1", "channels": [], "items": [], "pipeline": {}}
    ) is None
    assert build_retrieval_explain_snapshot(ContextPack()) is None


@pytest.mark.parametrize("value", [None, "", "0", "true", "01", " 1"])
def test_explain_gate_only_accepts_exact_one(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PP_RETRIEVAL_EXPLAIN", raising=False)
    else:
        monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", value)

    assert build_retrieval_explain_snapshot(_debug_pack()) is None


def test_projection_is_bounded_stable_and_drops_unsafe_data(monkeypatch):
    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")
    pack = _debug_pack()
    pack.channel_rankings = {
        f"channel-{index:02d}": [
            {"memory_id": f"m-{index:02d}-{rank:02d}", "score": rank / 10, "rank": rank}
            for rank in range(1, 13)
        ]
        for index in range(10)
    }
    pack.channel_states = {
        name: {
            "planned": True,
            "reason": "participating",
            "authorization": "Bearer must-not-leak",
        }
        for name in reversed(pack.channel_rankings)
    }
    pack.per_item_stats = [
        {
            "id": f"item-{index:02d}",
            "rank": index + 1,
            "final_score": index / 100,
            "layer": "core",
            "gate_reason": "passed",
            "query": "must not leak",
            "raw_embedding": [index],
            "secret": "must-not-leak",
        }
        for index in range(25)
    ]
    pack.per_item_stats.extend(
        [
            {"id": "bad-nan", "final_score": float("nan")},
            {"id": "bad-inf", "final_score": float("inf")},
            {"id": object(), "final_score": object()},
        ]
    )

    snapshot = build_retrieval_explain_snapshot(pack)

    assert snapshot is not None
    assert snapshot["schema"] == "retrieval_explain_v1"
    assert [row["name"] for row in snapshot["channels"]] == [
        f"channel-{index:02d}" for index in range(8)
    ]
    assert len(snapshot["channels"]) == 8
    assert all(len(row["items"]) == 10 for row in snapshot["channels"])
    assert [row["id"] for row in snapshot["channels"][0]["items"]] == [
        f"m-00-{rank:02d}" for rank in range(1, 11)
    ]
    assert len(snapshot["items"]) == 20
    assert [row["id"] for row in snapshot["items"]] == [
        f"item-{index:02d}" for index in range(20)
    ]
    assert snapshot["pipeline"] == {
        "vector_count": 4,
        "bm25_count": 3,
        "after_hard_score_filter": 2,
        "after_mmr": 1,
        "fallback_reason": "none",
        "engine_mode": "snapshot",
        "retrieval_mode": "hybrid",
        "fusion_policy": "wrrf-v1",
        "fusion_runtime": "python",
        "fusion_algorithm": "weighted-rrf-v1",
        "degraded": False,
    }
    assert snapshot["truncated"] == {
        "channels": True,
        "channel_items": True,
        "items": True,
    }

    serialized = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).casefold()
    for forbidden in (
        "must not leak",
        "must-not-leak",
        "sk-live",
        "query",
        "content",
        "prompt",
        "embedding",
        "vector\"",
        "api_key",
        "token",
        "password",
        "secret",
        "authorization",
        "nan",
        "infinity",
    ):
        assert forbidden not in serialized


def test_projection_runs_after_project_sanitization(monkeypatch):
    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")
    project_ctx = infer_project_context(
        {"project_id": "project:alpha", "project_policy": "strict"}
    )
    pack = ContextPack(
        core=[
            ContextItem(id="allowed", content="allowed", relevance=0.9, layer="core"),
            ContextItem(id="blocked", content="blocked", relevance=0.8, layer="core"),
        ],
        per_item_stats=[
            {"id": "allowed", "final_score": 0.9},
            {"id": "blocked", "final_score": 0.8},
        ],
        channel_rankings={
            "vector": [
                {"memory_id": "allowed", "score": 0.9, "rank": 1},
                {"memory_id": "blocked", "score": 0.8, "rank": 2},
            ]
        },
    )
    engine = SimpleNamespace(
        _memories={
            "allowed": {
                "id": "allowed",
                "project_id": "project:alpha",
                "visibility": "project",
                "source_class": "experience",
            },
            "blocked": {
                "id": "blocked",
                "project_id": "project:beta",
                "visibility": "project",
                "source_class": "experience",
            },
        }
    )

    sanitized = _sanitize_pack_for_project(pack, project_ctx, engine, task_type="general")
    snapshot = build_retrieval_explain_snapshot(sanitized)

    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "allowed" in serialized
    assert "blocked" not in serialized


class _FakeEmbedder:
    async def aembed(self, _text):
        return [0.0] * 1024


class _RecallEngine:
    def __init__(self):
        self.supply_calls = 0
        self._memories = {
            "m1": {
                "id": "m1",
                "content": "private memory content",
                "memory_type": "experience",
                "project_id": "project:alpha",
                "visibility": "project",
                "source_class": "experience",
            }
        }

    def supply(self, *_args, **_kwargs):
        self.supply_calls += 1
        return _debug_pack()

    def _finalize_supply_pack(self, pack, *_args, **_kwargs):
        return pack


def _recall_args(call_id: str) -> dict:
    return {
        "query": "retrieval explain integration",
        "task_type": "general",
        "scope": "global",
        "project_id": "project:alpha",
        "project_policy": "strict",
        "debug": True,
        "call_id": call_id,
        "request_id": "request-retrieval-explain",
    }


def test_memory_recall_normal_and_cache_hit_spans_capture_explain_without_rerieval(
    monkeypatch,
):
    import plastic_promise.core.embedder as embedder_mod

    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")
    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: _FakeEmbedder()
    )
    spans = []
    monkeypatch.setattr(memory_tools, "safe_record_call_span", lambda _engine, **kw: spans.append(kw))
    engine = _RecallEngine()

    first = asyncio.run(memory_tools.handle_memory_recall(engine, _recall_args("call-first")))
    engine._memories["blocked"] = {
        "id": "blocked",
        "content": "cross-project private memory",
        "memory_type": "experience",
        "project_id": "project:beta",
        "visibility": "project",
        "source_class": "experience",
    }
    with memory_tools._query_cache_lock:
        cache_key, (cached_json, cached_at) = next(iter(memory_tools._query_cache.items()))
        poisoned = json.loads(cached_json)
        poisoned["core"].append(
            {
                "id": "blocked",
                "content": "cross-project private memory",
                "relevance": 1.0,
                "source": "test",
            }
        )
        poisoned["per_item_stats"].append({"id": "blocked", "final_score": 1.0})
        poisoned["channel_rankings"]["vector"].append(
            {"id": "blocked", "rank": 2, "score": 1.0}
        )
        memory_tools._query_cache[cache_key] = (
            json.dumps(poisoned, ensure_ascii=False),
            cached_at,
        )
    second = asyncio.run(memory_tools.handle_memory_recall(engine, _recall_args("call-second")))

    assert engine.supply_calls == 1
    assert len(spans) == 2
    assert spans[0]["metadata"]["cache_hit"] is False
    assert spans[1]["metadata"]["cache_hit"] is True
    for span in spans:
        snapshot = span["metadata"]["retrieval_explain_v1"]
        assert snapshot["schema"] == "retrieval_explain_v1"
        assert snapshot["channels"][0]["items"] == [{"id": "m1", "rank": 1, "score": 0.91}]
        assert "private memory content" not in json.dumps(snapshot)
        assert "blocked" not in json.dumps(snapshot)

    assert "retrieval_explain_v1" not in json.loads(first[0].text)
    second_payload = json.loads(second[0].text)
    assert "retrieval_explain_v1" not in second_payload
    assert "blocked" not in json.dumps(second_payload)


def test_context_supply_span_captures_explain_but_public_response_does_not(
    monkeypatch,
):
    import plastic_promise.core.embedder as embedder_mod
    import plastic_promise.core.traceability as traceability_mod
    from plastic_promise.mcp.tools.context import handle_context_supply

    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")
    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: _FakeEmbedder()
    )
    spans = []
    monkeypatch.setattr(
        traceability_mod, "safe_record_call_span", lambda _engine, **kw: spans.append(kw)
    )
    engine = _RecallEngine()

    result = asyncio.run(
        handle_context_supply(
            engine,
            {
                "task_description": "retrieval explain context integration",
                "task_type": "general",
                "project_id": "project:alpha",
                "project_policy": "strict",
                "debug": True,
                "call_id": "call-context",
            },
        )
    )

    assert len(spans) == 1
    assert spans[0]["metadata"]["retrieval_explain_v1"]["schema"] == "retrieval_explain_v1"
    payload = json.loads(result[0].text)
    assert "retrieval_explain_v1" not in payload
    assert "retrieval_explain_v1" not in payload["audit_metadata"]


@pytest.mark.parametrize("handler", ["memory", "context"])
def test_disabled_gate_does_not_write_explain_to_span(monkeypatch, handler):
    import plastic_promise.core.embedder as embedder_mod
    import plastic_promise.core.traceability as traceability_mod
    from plastic_promise.mcp.tools.context import handle_context_supply

    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "0")
    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: _FakeEmbedder()
    )
    spans = []
    engine = _RecallEngine()
    if handler == "memory":
        monkeypatch.setattr(
            memory_tools, "safe_record_call_span", lambda _engine, **kw: spans.append(kw)
        )
        asyncio.run(memory_tools.handle_memory_recall(engine, _recall_args("call-memory-off")))
    else:
        monkeypatch.setattr(
            traceability_mod, "safe_record_call_span", lambda _engine, **kw: spans.append(kw)
        )
        asyncio.run(
            handle_context_supply(
                engine,
                {
                    "task_description": "retrieval explain disabled",
                    "project_id": "project:alpha",
                    "debug": True,
                    "call_id": "call-context-off",
                },
            )
        )

    assert len(spans) == 1
    assert "retrieval_explain_v1" not in spans[0]["metadata"]


def test_non_debug_pack_does_not_persist_empty_explain_snapshot(monkeypatch):
    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")
    pack = ContextPack(
        audit_metadata={
            "mode": "hybrid",
            "retrieval_fusion": {"effective_policy": "legacy-auto"},
        }
    )

    assert build_retrieval_explain_snapshot(pack) is None
