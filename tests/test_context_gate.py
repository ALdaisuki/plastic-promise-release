from plastic_promise.core.context_gate import CandidateEvidence, evaluate_context_gate


def test_context_gate_allows_high_worth_same_project_core(monkeypatch):
    monkeypatch.setenv("PP_CONTEXT_GATE_CORE_THRESHOLD", "0.72")
    candidate = CandidateEvidence(
        id="m1",
        content="stable same-project memory",
        source="manual",
        retrieval_source="bm25",
        base_score=0.91,
        project_id="project:alpha",
        visibility="project",
        source_class="manual",
        worth_score=0.95,
        freshness_score=1.0,
    )

    result = evaluate_context_gate(
        candidate,
        task_type="architecture",
        retrieval_mode="mix",
        project_id="project:alpha",
    )

    assert result.decision == "core"
    assert result.gate_score >= 0.72


def test_context_gate_blocks_strict_cross_project():
    candidate = CandidateEvidence(
        id="m2",
        content="other project memory",
        source="agent",
        retrieval_source="vector",
        base_score=0.95,
        project_id="project:beta",
        visibility="project",
        source_class="experience",
        worth_score=1.0,
        freshness_score=1.0,
    )

    result = evaluate_context_gate(
        candidate,
        task_type="architecture",
        retrieval_mode="mix",
        project_id="project:alpha",
        project_policy="strict",
    )

    assert result.decision == "block"
    assert "hard_block:strict_cross_project" in result.reasons


def test_context_gate_demotes_prompt_source_to_raw_only():
    candidate = CandidateEvidence(
        id="m3",
        content="prompt telemetry should not enter prompt layers",
        source="prompt",
        retrieval_source="bm25",
        base_score=0.95,
        source_class="prompt",
        worth_score=1.0,
        freshness_score=1.0,
    )

    result = evaluate_context_gate(candidate)

    assert result.decision == "raw_only"
    assert "hard_demote:source_class:prompt" in result.reasons
