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
    weighted_max_v1,
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


def test_max_v1_routes_to_rust_when_extension_is_healthy(monkeypatch):
    from plastic_promise.core.context_engine import ContextPack

    engine, _seen = _routing_engine(monkeypatch)
    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "0")
    monkeypatch.setattr(engine, "_check_rust_health", lambda: True)
    captured = {}

    def rust_supply(*_args, **kwargs):
        captured.update(kwargs)
        return ContextPack()

    monkeypatch.setattr(engine, "_supply_rust", rust_supply)

    pack = engine.supply("query", [1.0] + [0.0] * 1023, fusion_policy="max-v1")

    assert captured["fusion_policy"] == "max-v1"
    assert pack.audit_metadata["retrieval_fusion"]["effective_runtime"] == "rust"
    assert pack.audit_metadata["retrieval_fusion"]["algorithm"] == "weighted-max-v1"


def test_weighted_max_v1_preserves_production_formula_and_tie_order():
    result = weighted_max_v1(
        {
            "vector": [("vector", 1.0), ("shared", 0.8), ("tie-a", 0.4)],
            "bm25": [("exact", 0.95), ("strong", 0.8), ("shared", 0.9)],
            "fts": [("fts", 0.86), ("tie-b", 0.4)],
        },
        vector_weight=0.5,
    )

    assert result == [
        ("exact", 0.95),
        ("shared", 0.9),
        ("fts", 0.86),
        ("strong", 0.7200000000000001),
        ("vector", 0.5),
        ("tie-a", 0.2),
        ("tie-b", 0.2),
    ]


def test_weighted_max_v1_uses_text_only_fallback_without_vector_hits():
    result = weighted_max_v1(
        {
            "vector": [("ignored", 1.0)],
            "bm25": [("exact", 0.95), ("shared", 0.9)],
            "fts": [("fts", 0.86), ("tail", 0.4)],
        },
        vector_weight=0.5,
        has_vector=False,
    )

    assert result == [
        ("fts", 0.86),
        ("exact", 0.76),
        ("shared", 0.7200000000000001),
        ("tail", 0.32000000000000006),
    ]


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
    engine._activate_principles = lambda *_args, **_kwargs: []
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


# ---------------------------------------------------------------------------
# Three-Librarians alignment: default equal-vote RRF (PP_FUSION_DEFAULT=rrf)
# ---------------------------------------------------------------------------


def _default_plan(channels=("vector", "bm25", "fts")):
    return plan_retrieval(
        has_vector="vector" in channels,
        has_graph=False,
        has_fts="fts" in channels,
    )


def test_default_fusion_config_is_equal_vote_with_bounded_windows():
    from plastic_promise.core.fusion_policy import default_fusion_config

    config = default_fusion_config(_default_plan(), env={})
    assert config is not None
    assert set(config.channels) == {"vector", "bm25", "fts"}
    assert all(weight == 1.0 for weight in config.weights.values())
    assert all(0 < window <= 80 for window in config.windows.values())


def test_default_fusion_config_respects_env_overrides():
    from plastic_promise.core.fusion_policy import default_fusion_config

    config = default_fusion_config(
        _default_plan(),
        env={"PP_RETRIEVAL_RRF_K": "60", "PP_FUSION_DEFAULT_WINDOW": "16"},
    )
    assert config is not None
    assert config.k == 60
    assert all(window <= 16 for window in config.windows.values())


def test_default_fusion_config_returns_none_without_channels():
    from plastic_promise.core.fusion_policy import default_fusion_config

    # the planner always keeps at least bm25; simulate a truly channel-less plan
    plan = SimpleNamespace(fusion_channels=(), channel_windows={})
    assert default_fusion_config(plan, env={}) is None


def test_rrf_consensus_beats_single_arm_enthusiasm():
    """Two arms agreeing at rank 3 outrank one arm's rank 1 (RuleSage law)."""
    from plastic_promise.core.fusion_policy import weighted_rrf

    k = 20
    weights = {"vector": 1.0, "bm25": 1.0}
    windows = {"vector": 80, "bm25": 80}
    channels = ("vector", "bm25")
    config = FusionConfig(k=k, weights=weights, windows=windows, channels=channels, config_hash="")

    rankings = {
        "vector": [("consensus_a", 9.1), ("noise_v1", 8.0), ("both_third", 7.0)],
        "bm25": [("exact_bm25", 30.0), ("noise_b1", 20.0), ("both_third", 10.0)],
    }
    fused = dict(weighted_rrf(rankings, config))
    consensus = fused["both_third"]
    single = max(fused["consensus_a"], fused["exact_bm25"])
    # both_third sits at rank 3 on each arm: 2 * 1/(k+3)
    assert abs(consensus - 2.0 / (k + 3)) < 1e-12
    # any lone top hit caps at 1/(k+1); the pair beats it comfortably
    assert consensus > single


def test_rrf_fused_ceiling_is_derived_not_hardcoded():
    """Per-rulebook ceiling law: arms/(k+1), computed from live parameters."""
    from plastic_promise.core.fusion_policy import default_fusion_config

    env = {"PP_RETRIEVAL_RRF_K": "60"}
    plan = _default_plan()
    config = default_fusion_config(plan, env=env)
    assert config is not None
    arms = len(config.channels)
    ceiling = sum(config.weights.values()) / (config.k + 1)
    assert ceiling == pytest.approx(arms / (config.k + 1))


# ---------------------------------------------------------------------------
# Phase A wrap-up: shadow comparison metrics + explain field allowlist
# ---------------------------------------------------------------------------


def _shadow_config():
    return FusionConfig(
        k=20,
        weights={"vector": 1.0, "bm25": 1.0},
        windows={"vector": 80, "bm25": 80},
        channels=("vector", "bm25"),
        config_hash="",
    )


def test_shadow_comparison_reports_bounded_metrics():
    from plastic_promise.core.fusion_shadow import compare_fusion_strategies

    rankings = {
        "vector": [("consensus", 9.0), ("vec_only", 8.0), ("weak_v", 1.0)],
        "bm25": [("exact_hit", 30.0), ("consensus", 10.0), ("weak_b", 2.0)],
    }
    report = compare_fusion_strategies(rankings, _shadow_config())
    assert set(report) == {
        "rrf_order",
        "legacy_order",
        "top_k_overlap",
        "kendall_tau",
        "fused_ceiling",
        "top_k",
    }
    assert -1.0 <= report["kendall_tau"] <= 1.0
    assert 0.0 <= report["top_k_overlap"] <= 1.0
    assert abs(report["fused_ceiling"] - 2.0 / 21) < 1e-12


def test_shadow_comparison_is_deterministic():
    from plastic_promise.core.fusion_shadow import compare_fusion_strategies

    rankings = {
        "vector": [("a", 9.0), ("b", 5.0)],
        "bm25": [("c", 7.0), ("a", 3.0)],
    }
    first = compare_fusion_strategies(rankings, _shadow_config())
    second = compare_fusion_strategies(rankings, _shadow_config())
    assert first == second


def test_shadow_consensus_case_ranks_shared_item_first_under_rrf():
    from plastic_promise.core.fusion_shadow import compare_fusion_strategies

    rankings = {
        "vector": [("noise1", 9.5), ("noise2", 9.0), ("shared_star", 8.5)],
        "bm25": [("exact", 40.0), ("noise3", 20.0), ("shared_star", 15.0)],
    }
    report = compare_fusion_strategies(rankings, _shadow_config())
    # RRF: two rank-3 ballots beat every single-arm hit.
    assert report["rrf_order"][0] == "shared_star"
    # Legacy blend crowns the exact BM25 bypass instead.
    assert report["legacy_order"][0] == "exact"


def test_explain_allowlist_includes_fusion_fields():
    import plastic_promise.core.retrieval_explain as rx

    for field in ("fusion_rrf_k", "fusion_ceiling"):
        assert field in rx._PIPELINE_NUMBER_FIELDS
    for field in ("fusion_algorithm", "fusion_channels", "fusion_policy"):
        assert field in rx._PIPELINE_TEXT_FIELDS


# ---------------------------------------------------------------------------
# Phase B: aisle arm (third channel) + fusion guarantees
# ---------------------------------------------------------------------------


def test_validator_accepts_aisle_and_rejects_unknown():
    from plastic_promise.core.fusion_policy import (
        FusionConfigurationError,
        _validated_config,
    )

    ok = _validated_config(
        FusionConfig(
            k=20,
            weights={"vector": 1.0, "bm25": 1.0, "fts": 1.0, "aisle": 1.0},
            windows={"vector": 32, "bm25": 32, "fts": 32, "aisle": 16},
            channels=("vector", "bm25", "fts", "aisle"),
            config_hash="",
        )
    )
    assert "aisle" in ok.channels
    with pytest.raises(FusionConfigurationError):
        _validated_config(
            FusionConfig(
                k=20,
                weights={"vector": 1.0, "ghost": 1.0},
                windows={"vector": 8, "ghost": 8},
                channels=("vector", "ghost"),
                config_hash="",
            )
        )


def test_default_fusion_config_covers_aisle_when_plan_carries_it():
    from plastic_promise.core.constants import RRF_K
    from plastic_promise.core.fusion_policy import default_fusion_config

    plan = plan_retrieval(has_vector=True, has_graph=False, has_fts=True, has_aisle=True)
    config = default_fusion_config(plan, env={})
    assert config is not None
    assert config.channels == ("vector", "bm25", "fts", "aisle")
    assert config.k == RRF_K
    assert all(weight == 1.0 for weight in config.weights.values())


def test_four_arm_consensus_beats_single_arm_top():
    from plastic_promise.core.fusion_policy import weighted_rrf

    config = FusionConfig(
        k=20,
        weights={c: 1.0 for c in ("vector", "bm25", "fts", "aisle")},
        windows={c: 32 for c in ("vector", "bm25", "fts", "aisle")},
        channels=("vector", "bm25", "fts", "aisle"),
        config_hash="",
    )
    rankings = {
        "vector": [("n1", 9.0), ("shared4", 3.0)],
        "bm25": [("n2", 30.0), ("shared4", 10.0)],
        "fts": [("n3", 5.0), ("shared4", 2.0)],
        "aisle": [("n4", 1.0), ("shared4", 0.9)],
    }
    fused = dict(weighted_rrf(rankings, config))
    # four arms agreeing at rank 2 beats any single arm's rank 1
    assert fused["shared4"] > max(fused["n1"], fused["n2"], fused["n3"], fused["n4"])


def _engine_with_memories(memories):
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)
    engine._memories = memories
    return engine


def test_aisle_retrieval_nominates_domain_and_principles_with_caps():
    engine = _engine_with_memories(
        {
            "m_building_1": {"domain": "building", "memory_type": "experience", "worth_success": 5, "content": "building memory one"},
            "m_building_2": {"domain": "building", "memory_type": "experience", "worth_success": 3, "content": "building memory two"},
            "m_design_1": {"domain": "designing", "memory_type": "experience", "worth_success": 9, "content": "designing memory one"},
            "principle:7": {"domain": "governing", "memory_type": "principle", "content": "a principle"},
        }
    )
    results = engine._aisle_retrieval(task_type="general", domain_hint="building", limit=32, per_domain_cap=1)
    ids = [row[0] for row in results]
    # per-domain cap of 1 keeps only the top building memory; designing is a different aisle
    assert "m_building_1" in ids and "m_building_2" not in ids
    assert "m_design_1" not in ids
    # principles ride every aisle regardless of cap
    assert "principle:7" in ids


def test_guarantee_lifts_principle_into_window_and_reports_fire():
    rows = [(f"m{i}", float(10 - i), f"content {i}", "bm25") for i in range(12)]
    rows.append(("principle:2", 0.01, "the principle", "aisle"))
    engine = _engine_with_memories({"principle:2": {"memory_type": "principle"}})
    results, fired = engine._apply_fusion_guarantees(rows, retention_window=8)
    ids_in_window = [str(row[0]) for row in results[:8]]
    assert "principle:2" in ids_in_window
    assert len(fired) == 1
    assert fired[0]["id"] == "principle:2"
    assert fired[0]["from_rank"] == 13
    assert fired[0]["to_rank"] == 8


def test_guarantee_noop_when_pool_within_window_or_unqualified():
    engine = _engine_with_memories({})
    small = [(f"m{i}", float(i), "c", "bm25") for i in range(5)]
    results, fired = engine._apply_fusion_guarantees(list(small), retention_window=8)
    assert [r[0] for r in results] == [r[0] for r in small]
    assert fired == []

def test_explain_allowlist_includes_ceiling_formula():
    import plastic_promise.core.retrieval_explain as rx

    assert "fusion_ceiling_formula" in rx._PIPELINE_TEXT_FIELDS


def test_both_ends_window_shared_with_engine():
    from plastic_promise.core.reranker import _both_ends_window, both_ends_window

    assert both_ends_window is _both_ends_window
    long = "a" * 3000 + "TAIL-SENTENCE"
    windowed = both_ends_window(long, 1200)
    assert "TAIL-SENTENCE" in windowed and len(windowed) <= 1200
