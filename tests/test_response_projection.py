"""Regression tests for bounded MCP response projections."""

import json

import pytest

from plastic_promise.core.recall_quality import evaluate_live_retrieval_quality
from plastic_promise.mcp.response_projection import build_diagnostics, json_size
from plastic_promise.mcp.server import _project_session_init_result, _project_sp_stage_data
from plastic_promise.mcp.tools.memory import _project_memory_recall_payload
from plastic_promise.skills.engine import SkillResult


def _large_recall_payload() -> dict:
    items = [
        {
            "id": f"memory-{index}",
            "content": "large diagnostic memory " + ("x" * 5000),
            "relevance": 0.9 - index * 0.01,
            "source": "test",
        }
        for index in range(18)
    ]
    return {
        "core": items[:8],
        "related": items[8:15],
        "divergent": items[15:],
        "activated_principles": [
            {"id": index, "name": f"principle-{index}"} for index in range(12)
        ],
        "trace": {"call_id": "call-old", "request_scope_id": "scope-old"},
        "audit": {
            "trace": {"call_id": "call-old", "request_scope_id": "scope-old"},
            "raw_evidence": items,
            "retrieval_plan": {"mode": "mix", "budget": {"core": 8}},
        },
        "pipeline_stats": {f"stage_{index}": index for index in range(100)},
        "per_item_stats": [{"id": item["id"], "blob": "y" * 4000} for item in items],
        "channel_rankings": {
            "bm25": [
                {"memory_id": item["id"], "rank": index + 1, "score": 1.0}
                for index, item in enumerate(items)
            ],
            "vector": [
                {"memory_id": item["id"], "rank": index + 1, "score": 0.9}
                for index, item in enumerate(items)
            ],
        },
        "channel_states": {"bm25": {"executed": True}, "vector": {"executed": True}},
        "project_id": "project:test",
        "project_policy": "balanced",
        "request_scope_id": "scope-current",
        "total_items": len(items),
        "context_recommendations": [f"recommendation-{index}" for index in range(20)],
        "federation_signals": [f"signal-{index}" for index in range(20)],
    }


def test_memory_recall_projection_is_bounded_and_has_current_trace():
    projected = _project_memory_recall_payload(
        _large_recall_payload(),
        response_mode="compact",
        diagnostics_level="summary",
        call_id="call-current",
        project_warnings=[],
        project_degraded=False,
    )

    assert json_size(projected) < 60_000
    assert projected["trace"]["call_id"] == "call-current"
    assert projected["diagnostics"]["summary"]["trace"]["call_id"] == "call-current"
    assert "data" not in projected
    assert "audit" not in projected
    assert "raw_evidence" not in json.dumps(projected)


def test_debug_diagnostics_respect_byte_budget():
    payload = build_diagnostics(
        call_id="call-debug",
        audit=_large_recall_payload()["audit"],
        pipeline_stats=_large_recall_payload()["pipeline_stats"],
        per_item_stats=_large_recall_payload()["per_item_stats"],
        channel_rankings=_large_recall_payload()["channel_rankings"],
        channel_states=_large_recall_payload()["channel_states"],
        level="full",
        max_bytes=3072,
    )

    assert json_size(payload) <= 3072
    assert payload["ref"]["call_id"] == "call-debug"
    assert payload["details"]["channel_states"] == _large_recall_payload()["channel_states"]
    assert len(payload["details"]["channel_rankings"]["bm25"]) == 5
    assert len(payload["details"]["channel_rankings"]["vector"]) == 5


def test_debug_diagnostics_preserve_public_fusion_attestation():
    fusion = {
        "requested_policy": "max-v1",
        "effective_policy": "max-v1",
        "requested_runtime": "python",
        "effective_runtime": "python",
        "algorithm": "weighted-max-v1",
    }

    payload = build_diagnostics(
        call_id="call-fusion",
        audit={"retrieval_fusion": fusion},
        level="full",
        max_bytes=1024,
    )

    assert payload["summary"]["retrieval_fusion"] == fusion


def test_session_init_compact_omits_context_mirror_and_large_health_catalogs():
    context_status = {
        "status": "ready",
        "items": [
            {"id": f"m-{index}", "content": "z" * 2000, "relevance": 0.9, "source": "test"}
            for index in range(10)
        ],
    }
    result = SkillResult(
        skill_name="session-init",
        success=True,
        data={
            "context": dict(context_status),
            "context_status": context_status,
            "principles": [{"id": 1, "name": "Occam", "content": "x" * 3000}],
            "trust": {"score": 0.8},
            "component_health": {
                f"component-{index}": {"details": "x" * 2000} for index in range(50)
            },
            "domain_health": {f"domain-{index}": {"details": "x" * 2000} for index in range(50)},
            "system_stats": {"details": "x" * 20_000},
            "gc_preview": {"details": "x" * 20_000},
            "stage_session_id": "stage:test",
            "workflow_contract": {
                "entry_stage": "grill-with-docs",
                "entry_authority": "user",
                "flow_line_id": "idea-to-ship",
            },
            "chain_state": {
                "current_stage": "grill-with-docs",
                "valid_next": ["to-spec"],
            },
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )

    projected = _project_session_init_result(result, "compact")

    assert json_size(projected) < 6_000
    assert "context" not in projected
    assert "component_health" not in projected
    assert len(projected["context_status"]["items"]) == 2


def test_sp_stage_summary_hides_full_guidance_but_full_preserves_it():
    data = {
        "stage": "exemplar-research",
        "exemplar": {
            "problem": "study mature implementations",
            "search_query": "memory systems",
            "instructions": "full template " + ("x" * 20_000),
            "dispatch_template": "dispatch " + ("y" * 20_000),
        },
    }

    summary = _project_sp_stage_data(data, "summary")
    full = _project_sp_stage_data(data, "full")

    assert "instructions" not in summary["exemplar"]
    assert "dispatch_template" not in summary["exemplar"]
    assert summary["exemplar"]["instructions_available"] is True
    assert full["exemplar"]["instructions"].startswith("full template")


def test_live_ground_truth_records_hit_mrr_and_forbidden_only_in_trace_payload():
    metrics = evaluate_live_retrieval_quality(
        [{"id": "m-other"}, {"id": "m-relevant"}, {"id": "m-forbidden"}],
        {
            "case_id": "case-1",
            "dataset_revision": "dataset-v1",
            "corpus_hash": "sha256:corpus",
            "relevant_memory_ids": ["m-relevant"],
            "forbidden_memory_ids": ["m-forbidden"],
            "ks": [1, 3],
        },
        request_scope_id="scope:1",
        runtime_mode="rust-full",
        tool_name="memory_recall",
        task_type="debugging",
        scope="global",
        project_id="project:test",
        project_policy="balanced",
    )

    assert metrics["hit_at"] == {"1": False, "3": True}
    assert metrics["mrr"] == 0.5
    assert metrics["forbidden_hit"] is True
    assert metrics["request_scope_id"] == "scope:1"


def test_live_ground_truth_rejects_boolean_ks():
    with pytest.raises(ValueError, match="must contain integers"):
        evaluate_live_retrieval_quality(
            [{"id": "m-relevant"}],
            {
                "dataset_revision": "dataset-v1",
                "corpus_hash": "sha256:corpus",
                "relevant_memory_ids": ["m-relevant"],
                "ks": [True],
            },
            request_scope_id="scope:1",
            runtime_mode="normal",
            tool_name="context_supply",
            task_type="general",
            scope="global",
            project_id="project:test",
            project_policy="balanced",
        )
