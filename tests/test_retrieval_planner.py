import pytest

from plastic_promise.core.retrieval_planner import (
    plan_retrieval,
    requires_synthesis_source_expansion,
)


def test_architecture_uses_mix_mode_with_broad_budget():
    plan = plan_retrieval(
        task_type="architecture",
        scope="global",
        project_policy="balanced",
        has_vector=True,
        has_graph=True,
        has_fts=True,
    )

    assert plan.mode == "mix"
    assert plan.budget["core"] >= 8
    assert {"graph", "vector", "bm25", "fts"}.issubset(set(plan.channels))


def test_code_review_uses_code_mode():
    plan = plan_retrieval(task_type="code_review", scope="global", project_policy="balanced")

    assert plan.mode == "code"
    assert plan.budget["raw_evidence"] >= 10


def test_strict_project_policy_prefers_project_mode():
    plan = plan_retrieval(task_type="general", scope="global", project_policy="strict")

    assert plan.mode == "project"
    assert plan.project_policy == "strict"


def test_explicit_mode_override_is_validated():
    plan = plan_retrieval(task_type="general", retrieval_mode="audit")

    assert plan.mode == "audit"
    assert plan.reason == "caller_override"


@pytest.mark.parametrize(
    "task_type",
    ["code", "audit", "principle", "governing", "correction", "code_review", "debugging"],
)
def test_high_impact_tasks_require_synthesis_source_expansion(task_type: str):
    plan = plan_retrieval(task_type=task_type)

    assert requires_synthesis_source_expansion(plan, task_type=task_type) is True


def test_learning_plan_does_not_force_synthesis_source_expansion():
    plan = plan_retrieval(task_type="learning")

    assert requires_synthesis_source_expansion(plan, task_type="learning") is False


def test_fusion_channels_exclude_graph_and_evidence_layers():
    plan = plan_retrieval(
        task_type="code_review",
        has_vector=True,
        has_graph=True,
        has_fts=True,
    )

    assert plan.fusion_channels == ("vector", "bm25", "fts")
    assert plan.channel_windows == {"vector": 32, "bm25": 32, "fts": 32}
    assert {"graph", "code"}.issubset(plan.channels)
    assert not ({"graph", "code", "audit", "principle"} & set(plan.fusion_channels))
