import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plastic_promise.core.fusion_policy import (
    FusionConfig,
    FusionConfigurationError,
    canonical_fusion_config_hash,
    load_fusion_config,
    resolve_cli_fusion_policy,
    weighted_rrf,
)
from plastic_promise.core.retrieval_planner import plan_retrieval

_WRRF_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "recall_quality" / "wrrf-v1-golden.json"


def _decode_special_numbers(value):
    if isinstance(value, dict):
        return {key: _decode_special_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_special_numbers(item) for item in value]
    if value == "NaN":
        return float("nan")
    if value == "Infinity":
        return float("inf")
    if value == "-Infinity":
        return float("-inf")
    return value


def _load_wrrf_golden():
    payload = json.loads(_WRRF_GOLDEN_PATH.read_text(encoding="utf-8"))
    return _decode_special_numbers(payload)


def _golden_config(payload):
    return FusionConfig(
        k=payload["k"],
        channels=tuple(payload["channels"]),
        weights=payload["weights"],
        windows=payload["windows"],
        config_hash="",
    )


_WRRF_GOLDEN = _load_wrrf_golden()


def _config(
    *,
    k=2,
    weights=None,
    windows=None,
    channels=("vector", "bm25"),
):
    weights = weights or {"vector": 0.6, "bm25": 0.4}
    windows = windows or {"vector": 3, "bm25": 3}
    payload = {
        "k": k,
        "channels": list(channels),
        "weights": weights,
        "windows": windows,
    }
    return FusionConfig(
        k=k,
        weights=weights,
        windows=windows,
        channels=tuple(channels),
        config_hash=canonical_fusion_config_hash(payload),
    )


def test_weighted_rrf_uses_one_based_rank_and_id_tie_break():
    config = _config()

    result = weighted_rrf(
        {
            "vector": [("b", 99.0), ("a", 0.1)],
            "bm25": [("a", 500.0), ("b", 1.0)],
        },
        config,
    )

    assert result == sorted(result, key=lambda row: (-row[1], row[0]))
    assert dict(result)["a"] == pytest.approx(0.6 / 4 + 0.4 / 3)
    assert dict(result)["b"] == pytest.approx(0.6 / 3 + 0.4 / 4)


def test_wrrf_golden_fixture_covers_required_contracts():
    assert _WRRF_GOLDEN["schema_version"] == "wrrf-golden/v1"
    valid_names = {case["name"] for case in _WRRF_GOLDEN["valid_cases"]}
    invalid_names = {case["name"] for case in _WRRF_GOLDEN["invalid_cases"]}

    assert valid_names == {
        "one_based_rank",
        "zero_weight_channel",
        "missing_item_in_one_channel",
        "input_scores_only_define_order",
        "deterministic_id_tie",
        "window_truncation",
    }
    assert {
        "duplicate_id",
        "missing_weight",
        "extra_weight",
        "all_zero_weights",
        "negative_weight",
        "nan_weight",
        "infinite_weight",
        "nan_ranking_score",
        "infinite_ranking_score",
        "fractional_k",
        "boolean_k",
        "zero_k",
        "negative_k",
        "u32_overflow_k",
    } == invalid_names


@pytest.mark.parametrize(
    "case",
    _WRRF_GOLDEN["valid_cases"],
    ids=lambda case: case["name"],
)
def test_python_wrrf_matches_shared_golden(case):
    actual = weighted_rrf(case["rankings"], _golden_config(case["config"]))
    expected = case["expected"]

    assert [row[0] for row in actual] == [row[0] for row in expected]
    assert [row[1] for row in actual] == pytest.approx(
        [row[1] for row in expected],
        abs=_WRRF_GOLDEN["score_tolerance"],
        rel=0.0,
    )


@pytest.mark.parametrize(
    "case",
    _WRRF_GOLDEN["invalid_cases"],
    ids=lambda case: case["name"],
)
def test_python_wrrf_rejects_shared_invalid_golden(case):
    with pytest.raises(FusionConfigurationError) as exc_info:
        weighted_rrf(case["rankings"], _golden_config(case["config"]))

    assert str(exc_info.value) == case["expected_error"]


@pytest.mark.parametrize(
    ("config", "rankings", "reason"),
    [
        (_config(k=0), {"vector": [], "bm25": []}, "invalid_k:must_be_positive_integer"),
        (
            _config(weights={"vector": -0.1, "bm25": 1.1}),
            {"vector": [], "bm25": []},
            "invalid_weights:must_be_finite_non_negative",
        ),
        (
            _config(weights={"vector": 1.0}),
            {"vector": [], "bm25": []},
            "invalid_weights:channel_mismatch",
        ),
        (
            _config(),
            {"vector": [("dup", 1.0), ("dup", 0.5)], "bm25": []},
            "invalid_rankings:duplicate_id:vector",
        ),
        (
            _config(),
            {"vector": [], "bm25": [], "graph": []},
            "invalid_rankings:channel_mismatch",
        ),
    ],
)
def test_wrrf_invalid_configuration_or_rankings_fail_closed(config, rankings, reason):
    with pytest.raises(FusionConfigurationError, match=f"^{reason}$"):
        weighted_rrf(rankings, config)


def test_load_fusion_config_validates_hash_and_planner_windows():
    plan = plan_retrieval(has_vector=True, has_graph=True, has_fts=True)
    payload = {
        "k": 2,
        "channels": ["vector", "bm25", "fts"],
        "weights": {"vector": 0.6, "bm25": 0.25, "fts": 0.15},
        "windows": {"vector": 32, "bm25": 24, "fts": 16},
    }
    candidate_id = f"wrrf-v1:{canonical_fusion_config_hash(payload)}"
    env = {
        "PP_RETRIEVAL_RRF_K": "2",
        "PP_RETRIEVAL_RRF_WEIGHTS_JSON": json.dumps(payload["weights"]),
        "PP_RETRIEVAL_RRF_WINDOWS_JSON": json.dumps(payload["windows"]),
    }

    config = load_fusion_config(candidate_id, plan, env)

    assert config is not None
    assert config.config_hash == candidate_id.split(":", 1)[1]
    assert config.channels == ("vector", "bm25", "fts")

    env["PP_RETRIEVAL_RRF_WINDOWS_JSON"] = json.dumps({"vector": 33, "bm25": 24, "fts": 16})
    with pytest.raises(
        FusionConfigurationError,
        match="^invalid_windows:planner_budget_exceeded:vector$",
    ):
        load_fusion_config(candidate_id, plan, env)


def test_bare_wrrf_cli_policy_requires_manifest_and_normalizes_before_mcp():
    candidate_id = f"wrrf-v1:{'a' * 64}"
    manifest = SimpleNamespace(candidate_id=candidate_id)

    with pytest.raises(
        FusionConfigurationError,
        match="^fusion_candidate_manifest_required$",
    ):
        resolve_cli_fusion_policy("wrrf-v1", None)

    assert resolve_cli_fusion_policy("wrrf-v1", manifest) == candidate_id
    assert resolve_cli_fusion_policy(candidate_id, manifest) == candidate_id

    with pytest.raises(
        FusionConfigurationError,
        match="^fusion_candidate_manifest_mismatch$",
    ):
        resolve_cli_fusion_policy(f"wrrf-v1:{'b' * 64}", manifest)


def test_legacy_and_max_policies_do_not_load_candidate_configuration():
    plan = plan_retrieval()

    assert load_fusion_config("legacy-auto", plan, {}) is None
    assert load_fusion_config("max-v1", plan, {}) is None
    assert resolve_cli_fusion_policy("legacy-auto", None) == "legacy-auto"
    assert resolve_cli_fusion_policy("max-v1", None) == "max-v1"


def _routing_engine(monkeypatch, *, has_fts=False):
    from plastic_promise.core.context_engine import ContextEngine, ContextPack

    engine = ContextEngine(use_sqlite=False)
    engine._refresh_canonical_cache_if_changed = lambda: None
    engine._ensure_heavy_init = lambda: None
    engine._graph_edges = [{"from": "a", "to": "b"}]
    engine._ldb = object() if has_fts else None
    engine._finalize_supply_pack = lambda pack, *_args, **_kwargs: pack
    seen = {}

    def python_supply(*_args, **kwargs):
        seen.update(kwargs)
        return ContextPack()

    monkeypatch.setattr(engine, "_supply_python", python_supply)
    return engine, seen


def test_max_v1_routes_to_python_before_rust_health_check(monkeypatch):
    engine, seen = _routing_engine(monkeypatch)
    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "0")
    monkeypatch.setattr(
        engine,
        "_check_rust_health",
        lambda: pytest.fail("max-v1 must route before Rust health"),
    )

    engine.supply("query", [1.0] + [0.0] * 1023, fusion_policy="max-v1")

    decision = seen["fusion_decision"]
    assert decision.requested_policy == "max-v1"
    assert decision.effective_policy == "max-v1"
    assert decision.effective_runtime == "python"
    assert decision.capability_reason == "policy_requires_python:max-v1"


def test_wrrf_plan_with_fts_routes_entire_request_to_python(monkeypatch):
    engine, seen = _routing_engine(monkeypatch, has_fts=True)
    payload = {
        "k": 2,
        "channels": ["vector", "bm25", "fts"],
        "weights": {"vector": 0.6, "bm25": 0.25, "fts": 0.15},
        "windows": {"vector": 20, "bm25": 20, "fts": 20},
    }
    candidate_id = f"wrrf-v1:{canonical_fusion_config_hash(payload)}"
    monkeypatch.setenv("PP_RETRIEVAL_RRF_K", "2")
    monkeypatch.setenv("PP_RETRIEVAL_RRF_WEIGHTS_JSON", json.dumps(payload["weights"]))
    monkeypatch.setenv("PP_RETRIEVAL_RRF_WINDOWS_JSON", json.dumps(payload["windows"]))
    monkeypatch.setattr(
        engine,
        "_check_rust_health",
        lambda: pytest.fail("FTS WRRF must route before Rust health"),
    )

    engine.supply("query", [1.0] + [0.0] * 1023, fusion_policy=candidate_id)

    decision = seen["fusion_decision"]
    assert decision.effective_runtime == "python"
    assert decision.effective_policy == candidate_id
    assert decision.capability_reason == "rust_capability_missing:fts"


def test_two_channel_wrrf_falls_back_until_rust_supply_accepts_config(monkeypatch):
    from plastic_promise.core.context_engine import _RustFusionFallback

    engine, seen = _routing_engine(monkeypatch)
    payload = {
        "k": 2,
        "channels": ["vector", "bm25"],
        "weights": {"vector": 0.6, "bm25": 0.4},
        "windows": {"vector": 20, "bm25": 20},
    }
    candidate_id = f"wrrf-v1:{canonical_fusion_config_hash(payload)}"
    monkeypatch.setenv("PP_RETRIEVAL_RRF_K", "2")
    monkeypatch.setenv("PP_RETRIEVAL_RRF_WEIGHTS_JSON", json.dumps(payload["weights"]))
    monkeypatch.setenv("PP_RETRIEVAL_RRF_WINDOWS_JSON", json.dumps(payload["windows"]))
    monkeypatch.setattr(engine, "_check_rust_health", lambda: True)

    def unsupported(*_args, **_kwargs):
        raise _RustFusionFallback("rust_capability_missing:fusion_config_boundary")

    monkeypatch.setattr(engine, "_supply_rust", unsupported)

    pack = engine.supply("query", [1.0] + [0.0] * 1023, fusion_policy=candidate_id)

    decision = seen["fusion_decision"]
    assert decision.effective_runtime == "python"
    assert decision.capability_reason == "rust_capability_missing:fusion_config_boundary"
    assert pack.audit_metadata["rust_fallback_reason"] == (
        "rust_capability_missing:fusion_config_boundary"
    )


def test_legacy_rust_k60_is_never_labeled_max_or_wrrf(monkeypatch):
    from plastic_promise.core.context_engine import ContextPack

    engine, _seen = _routing_engine(monkeypatch)
    monkeypatch.setattr(engine, "_check_rust_health", lambda: True)
    monkeypatch.setattr(engine, "_supply_rust", lambda *_args, **_kwargs: ContextPack())

    pack = engine.supply("query", [1.0] + [0.0] * 1023)

    audit = pack.audit_metadata["retrieval_fusion"]
    assert audit["effective_policy"] == "legacy-auto"
    assert audit["effective_runtime"] == "rust"
    assert audit["compatibility"] == "unweighted-rrf-k60"
    assert "max" not in audit["algorithm"]
    assert "wrrf" not in audit["algorithm"]


def test_python_wrrf_populates_policy_and_complete_channel_debug(monkeypatch):
    from plastic_promise.core.context_engine import ContextEngine
    from plastic_promise.core.fusion_policy import FusionDecision
    from plastic_promise.core.reranker import MultiProviderReranker

    monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
    monkeypatch.setenv("PP_QUERY_EXPANSION", "0")
    monkeypatch.setenv("PP_CONTEXT_GATE", "0")
    monkeypatch.setenv("PP_DECAY_IN_RANKING", "0")
    monkeypatch.setattr(MultiProviderReranker, "rerank", lambda _self, _query, items: items)
    engine = ContextEngine(use_sqlite=False)
    engine._ensure_heavy_init = lambda: None
    engine._activate_principles = lambda *_args: []
    engine._inject_activated_to_graph = lambda *_args: 0
    engine._graph_traversal = lambda *_args: [("g", 0.9, "graph", "graph")]
    engine._text_retrieval = lambda *_args: [
        ("a", 500.0, "text a", "bm25"),
        ("b", 1.0, "text b", "bm25"),
    ]
    engine._vector_retrieval = lambda *_args, **_kwargs: [
        ("b", 99.0, "vector b", "vector"),
        ("a", 0.1, "vector a", "vector"),
        ("tail", 0.01, "vector tail", "vector"),
    ]
    engine._fts_retrieval = lambda *_args, **_kwargs: []
    engine._code_memory_retrieval = lambda *_args, **_kwargs: []
    engine._layered_fuse = lambda graph, fused, _unused: [*graph, *fused]
    engine._apply_edge_feedback = lambda: None
    engine._apply_mmr = lambda items, **_kwargs: items
    engine._compute_divergent_quality = lambda items, _all: items
    engine._calc_freshness = lambda _item_id: "valid"
    engine._calc_decay_status = lambda _item_id, _memory: "healthy"
    engine._finalize_supply_pack = lambda pack, *_args, **_kwargs: pack
    engine._memories = {
        item_id: {
            "id": item_id,
            "content": content,
            "source": "test",
            "memory_type": "experience",
            "worth_success": 0,
            "worth_failure": 0,
        }
        for item_id, content in {
            "a": "text a",
            "b": "text b",
            "tail": "vector tail",
            "g": "graph",
        }.items()
    }
    plan = plan_retrieval(has_vector=True, has_graph=True, has_fts=False)
    config = _config(windows={"vector": 3, "bm25": 3})
    candidate_id = f"wrrf-v1:{config.config_hash}"
    decision = FusionDecision(
        requested_policy=candidate_id,
        effective_policy=candidate_id,
        requested_runtime="rust",
        effective_runtime="python",
        candidate_id=candidate_id,
        capability_reason="rust_capability_missing:fusion_config_boundary",
    )

    pack = engine._supply_python(
        "query",
        [1.0] + [0.0] * 1023,
        debug=True,
        retrieval_plan=plan,
        fusion_config=config,
        fusion_decision=decision,
    )

    assert [row["memory_id"] for row in pack.channel_rankings["vector"]] == [
        "b",
        "a",
        "tail",
    ]
    assert pack.channel_states["graph"]["evidence_only"] is True
    assert pack.channel_states["vector"]["participating"] is True
    audit = pack.audit_metadata["retrieval_fusion"]
    assert audit["effective_policy"] == candidate_id
    assert audit["effective_runtime"] == "python"
    assert audit["algorithm"] == "weighted-rrf-v1"


def test_final_gate_keeps_admitted_channel_tail_and_drops_cross_project_id():
    from plastic_promise.core.context_engine import ContextEngine, ContextPack

    engine = ContextEngine(use_sqlite=False)
    engine._memories = {
        "vector-only-tail": {
            "id": "vector-only-tail",
            "content": "admitted tail",
            "memory_type": "experience",
            "source": "test",
            "project_id": "project:alpha",
            "visibility": "project",
            "source_class": "experience",
        },
        "private-cross-project": {
            "id": "private-cross-project",
            "content": "must not leak",
            "memory_type": "experience",
            "source": "test",
            "project_id": "project:beta",
            "visibility": "project",
            "source_class": "experience",
        },
    }
    pack = ContextPack(
        channel_rankings={
            "vector": [
                {"memory_id": "private-cross-project", "score": 1.0, "rank": 1},
                {"memory_id": "vector-only-tail", "score": 0.5, "rank": 2},
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
            }
        },
    )
    plan = plan_retrieval(
        scope="project:alpha",
        project_policy="strict",
        has_vector=True,
        has_graph=False,
        has_fts=False,
    )

    result = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:alpha",
        project_policy="strict",
    )

    assert result.core == result.related == result.divergent == []
    assert result.channel_rankings["vector"] == [
        {"memory_id": "vector-only-tail", "score": 0.5, "rank": 1}
    ]
