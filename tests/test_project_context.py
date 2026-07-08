from plastic_promise.core.project_context import (
    ProjectContext,
    infer_project_context,
    source_class_from_inputs,
)


def test_explicit_project_context_wins():
    ctx = infer_project_context(
        {
            "project_id": "project:billing-api",
            "project_policy": "strict",
            "visibility": "shared",
            "source_class": "code_fact",
        }
    )

    assert ctx == ProjectContext(
        project_id="project:billing-api",
        project_policy="strict",
        visibility="shared",
        source_class="code_fact",
        degraded=False,
        warnings=[],
    )


def test_project_tag_infers_project_id():
    ctx = infer_project_context({"tags": ["domain:building", "project:mobile-app"]})

    assert ctx.project_id == "project:mobile-app"
    assert ctx.visibility == "project"
    assert ctx.degraded is False


def test_agent_scope_maps_to_project():
    ctx = infer_project_context({"scope": "agent:claude"})

    assert ctx.project_id == "project:agent:claude"
    assert ctx.visibility == "project"


def test_environment_project_id_infers_project_context(monkeypatch):
    monkeypatch.setenv("PLASTIC_PROJECT_ID", "plastic-promise")

    ctx = infer_project_context({})

    assert ctx.project_id == "project:plastic-promise"
    assert ctx.visibility == "project"
    assert ctx.degraded is False
    assert ctx.warnings == []


def test_pp_project_id_fallback_infers_project_context(monkeypatch):
    monkeypatch.delenv("PLASTIC_PROJECT_ID", raising=False)
    monkeypatch.setenv("PP_PROJECT_ID", "project:plastic-promise")

    ctx = infer_project_context({"scope": "building"})

    assert ctx.project_id == "project:plastic-promise"
    assert ctx.degraded is False


def test_unknown_project_degrades_and_restricts(monkeypatch):
    monkeypatch.delenv("PLASTIC_PROJECT_ID", raising=False)
    monkeypatch.delenv("PP_PROJECT_ID", raising=False)

    ctx = infer_project_context({"scope": "building"})

    assert ctx.project_id == "project:unknown"
    assert ctx.degraded is True
    assert "project_id unresolved" in ctx.warnings[0]


def test_source_class_from_inputs_filters_prompts_and_telemetry():
    assert source_class_from_inputs("claude_code", "task", ["review:prompt"]) == "prompt"
    assert source_class_from_inputs("maintenance_daemon", "task", []) == "telemetry"
    assert source_class_from_inputs("codex", "experience", []) == "experience"
    assert source_class_from_inputs("codex", "reflection", []) == "reflection"
    assert source_class_from_inputs("codex", "improvement", []) == "reflection"
    assert source_class_from_inputs("user", "code", []) == "code_fact"
