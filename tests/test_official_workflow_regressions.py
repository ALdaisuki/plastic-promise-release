from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import MagicMock

import pytest
from mcp.types import TextContent

import plastic_promise.mcp.server as mcp_server
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.official_workflow import (
    COMPOSITE_SKILL_CALLS,
    OFFICIAL_SKILLS,
    UPSTREAM_SKILLS_REVISION,
    validate_execution_receipt,
)
from plastic_promise.core.workflow_state import (
    WorkflowState,
    compose_flow_scope,
    ensure_workflow_state_schema,
    load_workflow_state,
    save_workflow_state,
    split_flow_scope,
)
from plastic_promise.mcp.tools import skill_tracking
from plastic_promise.mcp.tools.skill_tracking import (
    get_current_entity_id,
    get_current_stage,
    get_stage_chain_state,
    handle_skill_auto_track,
    handle_skill_session_start,
    handle_skill_session_trace,
    set_current_stage,
)
from plastic_promise.passive_memory import coordinator as passive_coordinator
from plastic_promise.passive_memory.events import PassiveMemoryEvent
from plastic_promise.skills.engine import SkillDef, SkillEngine, SkillResult
from plastic_promise.skills.official_workflow_stages import SKILL_DEFS, STAGE_ATOMS
from plastic_promise.skills.session_lifecycle import skill_session_init
from plastic_promise.skills.tool_routing import ENGINEERING_SKILLS, OFFICIAL_WORKFLOW_ROUTES


def _text(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload))]


def _execution_receipt(stage: str, marker: str = "focused regression passed") -> dict:
    evidence = {"verification": marker}
    if stage in COMPOSITE_SKILL_CALLS:
        evidence["invoked_skills"] = [
            *COMPOSITE_SKILL_CALLS[stage]["optional"],
            *COMPOSITE_SKILL_CALLS[stage]["required"],
        ]
    return {
        "skill": stage,
        "upstream_revision": UPSTREAM_SKILLS_REVISION,
        "content_sha256": OFFICIAL_SKILLS[stage].content_sha256,
        "status": "completed",
        "evidence": evidence,
    }


def _real_official_skill_engine(engine: ContextEngine) -> SkillEngine:
    skill_engine = SkillEngine(engine)
    for definition in SKILL_DEFS.values():
        skill_engine.register(definition)
    return skill_engine


async def _call_sp_stage(
    stage: str,
    *,
    stage_session_id: str,
    flow_line_id: str,
    route: str,
    invocation_source: str,
    marker: str = "focused regression passed",
    project_id: str = "",
) -> dict:
    arguments = {
        "stage": stage,
        "task_description": f"exercise {stage}",
        "stage_session_id": stage_session_id,
        "flow_line_id": flow_line_id,
        "route": route,
        "invocation_source": invocation_source,
        "execution_receipt": _execution_receipt(stage, marker),
    }
    if project_id:
        arguments["project_id"] = project_id
    result = await mcp_server.call_tool(
        "sp-stage",
        arguments,
    )
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_real_official_skill_tracking_accepts_programmatic_stage_name():
    engine = MagicMock()
    nodes = {}

    def register_entity(**kwargs):
        node_id = f"{kwargs['entity_type']}:{kwargs['entity_id']}"
        nodes[node_id] = {
            "type": kwargs["entity_type"],
            "name": kwargs["entity_name"],
            "description": kwargs.get("entity_description", ""),
            "metadata": dict(kwargs.get("metadata") or {}),
        }
        return {"node_id": node_id, "type": kwargs["entity_type"]}

    engine.register_entity.side_effect = register_entity
    engine.get_graph_node.side_effect = lambda node_id: nodes.get(node_id)
    engine.iter_memories.return_value = iter(())
    skill_engine = SkillEngine(engine)
    skill_engine.register(SKILL_DEFS["diagnosing-bugs"])
    skill_engine._atoms["skill_session_start"] = handle_skill_session_start

    async def ok_atom(_engine, _args):
        return _text({"ok": True})

    for atom_name in STAGE_ATOMS["diagnosing-bugs"]:
        skill_engine._atoms[atom_name] = ok_atom

    result = await skill_engine.exec(
        "sp-diagnosing-bugs",
        {
            "task_description": "reproduce a workflow bug",
            "stage_session_id": "stage:tracking-regression",
            "route": "bug-onramp",
            "task_type": "debugging",
        },
        caller="claude",
    )

    assert result.success is True
    assert result.audit_trail["tracking_degraded"] is False
    assert result.audit_trail["entity_id"].startswith("skill:diagnosing-bugs:")


@pytest.mark.asyncio
async def test_skill_engine_treats_structured_atom_error_as_failure():
    engine = MagicMock()
    skill_engine = SkillEngine(engine)
    skill_engine._atoms.pop("skill_session_start", None)

    async def semantic_failure(_engine, _args):
        return _text({"error": "missing task_type", "tool": "principle_activate"})

    async def handler(_engine, _params, _atom_results):
        return SkillResult(
            skill_name="semantic-error-stage",
            success=True,
            data={},
            atom_results={},
            degrade_log=[],
            audit_trail={},
            errors=[],
        )

    skill_engine._atoms["principle_activate"] = semantic_failure
    skill_engine.register(
        SkillDef(
            name="semantic-error-stage",
            domain="fixing",
            description="semantic error regression",
            tier="P0",
            atoms=["principle_activate"],
            degrade_map={"principle_activate": "abort"},
            handler=handler,
            allowed_callers=["claude"],
            track_start_memory=False,
        )
    )

    result = await skill_engine.exec("semantic-error-stage", caller="claude")

    assert result.success is False
    assert result.errors == ["principle_activate: missing task_type"]


@pytest.mark.asyncio
async def test_skill_engine_rejects_structured_completion_error():
    engine = MagicMock()
    skill_engine = SkillEngine(engine)

    async def start(_engine, _args):
        return _text({"entity_id": "skill:test-completion:2026-01-01T00:00:00"})

    async def complete(_engine, _args):
        return _text({"error": "entity lifecycle update failed"})

    async def handler(_engine, _params, _atom_results):
        return SkillResult(
            skill_name="test-completion",
            success=True,
            data={"handled": True},
            atom_results={},
            degrade_log=[],
            audit_trail={},
            errors=[],
        )

    skill_engine._atoms["skill_session_start"] = start
    skill_engine._atoms["skill_session_complete"] = complete
    skill_engine.register(
        SkillDef(
            name="test-completion",
            domain="building",
            description="completion regression",
            tier="P0",
            handler=handler,
            allowed_callers=["claude"],
            track_start_memory=False,
        )
    )

    result = await skill_engine.exec(
        "test-completion",
        params={"stage_session_id": "stage:completion-regression"},
        caller="claude",
    )

    assert result.success is False
    assert result.errors == ["skill_session_complete: entity lifecycle update failed"]


def test_official_stage_entry_does_not_auto_close_unperformed_work():
    for stage_name, atoms in STAGE_ATOMS.items():
        assert "step_closure_light" not in atoms, stage_name
        assert "step_closure_full" not in atoms, stage_name


def test_every_declared_official_skill_and_branch_is_executable():
    registered = set(SKILL_DEFS)
    assert registered == set(ENGINEERING_SKILLS)
    for route in OFFICIAL_WORKFLOW_ROUTES.values():
        assert set(route["stages"]) <= registered
        for branch in (route.get("branches") or {}).values():
            assert set(branch) <= registered


def test_project_scoped_flow_identity_preserves_public_session_and_lane():
    first = compose_flow_scope("stage:shared", "codex", "project:first")
    second = compose_flow_scope("stage:shared", "codex", "project:second")

    assert first != second
    assert split_flow_scope(first) == ("stage:shared", "codex")
    assert split_flow_scope(second) == ("stage:shared", "codex")


def test_reserved_scope_delimiters_cannot_suppress_project_or_flow_isolation():
    first_project = compose_flow_scope(
        "stage:shared::project:caller-text",
        "lane::flow:caller-text",
        "project:first",
    )
    second_project = compose_flow_scope(
        "stage:shared::project:caller-text",
        "lane::flow:caller-text",
        "project:second",
    )
    second_lane = compose_flow_scope(
        "stage:shared::project:caller-text",
        "other::flow:caller-text",
        "project:first",
    )

    assert len({first_project, second_project, second_lane}) == 3
    assert split_flow_scope(first_project) == (
        "stage:shared::project:caller-text",
        "lane::flow:caller-text",
    )


def test_canonical_looking_session_component_is_always_encoded():
    forged = "stage:caller::flow:forged::project:forged"

    scope = compose_flow_scope(forged)

    assert scope != forged
    assert "%3A%3Aflow:forged%3A%3Aproject:forged" in scope
    assert split_flow_scope(scope) == (forged, "")


def test_save_workflow_state_participates_in_caller_transaction():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE caller_rows (value TEXT NOT NULL)")
    ensure_workflow_state_schema(connection)
    connection.commit()
    state = WorkflowState(
        scope_id="stage:transaction",
        stage_session_id="stage:transaction",
        flow_line_id="",
        route_id="review",
        current_stage="code-review",
        current_step_index=0,
        parent_entity_id=None,
        current_entity_id=None,
    )

    connection.execute("INSERT INTO caller_rows (value) VALUES ('pending')")
    save_workflow_state(connection, state)
    connection.rollback()

    assert connection.execute("SELECT COUNT(*) FROM caller_rows").fetchone()[0] == 0
    assert load_workflow_state(connection, state.scope_id) is None
    connection.close()


def test_execution_receipt_rejects_secret_values_under_neutral_keys():
    secret_values = (
        "sk-1234567890abcdefghijklmnop",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nredacted",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        "-".join(("xoxb", "1234567890", "abcdefghijklmnopqrstuvwxyz")),
        "postgresql://service:plain-password@db.internal/app",
    )
    for secret_value in secret_values:
        receipt = _execution_receipt("diagnosing-bugs")
        receipt["evidence"] = {"verification": secret_value}
        assert validate_execution_receipt("diagnosing-bugs", receipt)[1] == (
            "evidence_contains_secret"
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "aws_secret_access_key",
        "secret_key",
        "signing_key",
        "serviceSecretKey",
        "release-signing-key",
    ],
)
def test_execution_receipt_rejects_secret_key_field_names(field_name):
    receipt = _execution_receipt("diagnosing-bugs")
    receipt["evidence"] = {"verification": "passed", field_name: "redacted"}

    assert validate_execution_receipt("diagnosing-bugs", receipt)[1] == ("evidence_contains_secret")


def test_composite_execution_receipt_requires_attested_internal_skill_calls():
    receipt = _execution_receipt("implement")
    receipt["evidence"].pop("invoked_skills")
    assert validate_execution_receipt("implement", receipt)[1] == (
        "composite_invoked_skills_required"
    )

    receipt["evidence"]["invoked_skills"] = ["tdd"]
    assert validate_execution_receipt("implement", receipt)[1] == (
        "composite_required_skill_missing"
    )

    receipt["evidence"]["invoked_skills"] = ["code-review"]
    validated, error = validate_execution_receipt("implement", receipt)
    assert error is None
    assert validated["evidence"]["invoked_skills"] == ["code-review"]

    receipt["evidence"]["invoked_skills"] = ["code-review", "tdd"]
    assert validate_execution_receipt("implement", receipt)[1] == (
        "composite_invoked_skills_order_invalid"
    )


def test_execution_receipt_allows_benign_token_metrics_and_names():
    receipt = _execution_receipt("diagnosing-bugs")
    receipt["evidence"] = {
        "token_count": 42,
        "tokenizer": "cl100k_base",
        "verification": "focused regression passed",
    }

    validated, error = validate_execution_receipt("diagnosing-bugs", receipt)

    assert error is None
    assert validated is not None


def test_workflow_transition_rolls_back_receipt_when_cursor_write_fails(tmp_path, monkeypatch):
    from plastic_promise.core import workflow_state

    connection = __import__("sqlite3").connect(tmp_path / "workflow-atomic.db")
    receipt = _execution_receipt("diagnosing-bugs")

    def fail_cursor_write(_connection, _state):
        raise RuntimeError("injected cursor failure")

    monkeypatch.setattr(workflow_state, "_upsert_workflow_state", fail_cursor_write)
    try:
        with pytest.raises(RuntimeError, match="injected cursor failure"):
            workflow_state.commit_workflow_transition(
                connection,
                scope_id="stage:atomic::flow:bug-fix",
                route_id="bug-onramp",
                step_index=0,
                receipt=receipt,
                current_stage="diagnosing-bugs",
            )

        assert (
            connection.execute("SELECT COUNT(*) FROM official_workflow_receipts").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM official_workflow_state").fetchone()[0] == 0
    finally:
        connection.close()


def test_workflow_stage_state_survives_process_cache_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-state.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    scope = "stage:durable::flow:bug-onramp"
    try:
        set_current_stage(
            "diagnosing-bugs",
            stage_session_id=scope,
            parent_entity_id="skill:diagnosing-bugs:test",
            engine=engine,
            route_id="bug-onramp",
            current_step_index=0,
        )
        with skill_tracking._skill_state_lock:
            skill_tracking._stage_sessions.clear()

        assert get_current_stage(scope, engine=engine) == "diagnosing-bugs"
        state = skill_tracking.get_stage_chain_state(scope, engine=engine)
        assert state["route_id"] == "bug-onramp"
        assert state["current_step_index"] == 0
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_skill_auto_track_persists_scoped_entity_without_advancing_workflow(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-auto-track.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    stage_session_id = "stage:auto-track"
    flow_line_id = "bug-fix"
    scope = f"{stage_session_id}::flow:{flow_line_id}"
    try:
        set_current_stage(
            "diagnosing-bugs",
            stage_session_id=scope,
            engine=engine,
            route_id="bug-onramp",
            current_step_index=0,
        )

        started = await handle_skill_auto_track(
            engine,
            {
                "phase": "start",
                "skill_name": "tdd",
                "stage_session_id": stage_session_id,
                "flow_line_id": flow_line_id,
            },
        )
        started_data = json.loads(started[0].text)

        with skill_tracking._skill_state_lock:
            skill_tracking._stage_sessions.clear()
        restored = get_stage_chain_state(scope, engine=engine)
        assert restored["current_entity_id"] == started_data["entity_id"]
        assert restored["current_stage"] == "diagnosing-bugs"
        assert restored["current_step_index"] == 0

        completed = await handle_skill_auto_track(
            engine,
            {
                "phase": "complete",
                "skill_name": "tdd",
                "stage_session_id": stage_session_id,
                "flow_line_id": flow_line_id,
            },
        )
        completed_data = json.loads(completed[0].text)
        assert completed_data["current_stage"] == "diagnosing-bugs"

        with skill_tracking._skill_state_lock:
            skill_tracking._stage_sessions.clear()
        restored = get_stage_chain_state(scope, engine=engine)
        assert restored["current_entity_id"] is None
        assert restored["current_stage"] == "diagnosing-bugs"
        assert restored["current_step_index"] == 0

        trace = await handle_skill_session_trace(engine, {"session_scope": "all"})
        traced = json.loads(trace[0].text)
        session = next(
            item for item in traced["sessions"] if item["entity_id"] == started_data["entity_id"]
        )
        assert session["status"] == "done"
        assert session["tracking_persistence"] == "entity_only"
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_skill_auto_track_preserves_active_scope_when_completion_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-auto-track-failure.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    scope = "stage:auto-track-failure::flow:bug-fix"
    try:
        started = await handle_skill_auto_track(
            engine,
            {
                "phase": "start",
                "skill_name": "tdd",
                "stage_session_id": "stage:auto-track-failure",
                "flow_line_id": "bug-fix",
            },
        )
        entity_id = json.loads(started[0].text)["entity_id"]

        async def fail_completion(_engine, _args):
            return _text({"error": "injected completion failure"})

        monkeypatch.setattr(skill_tracking, "handle_skill_session_complete", fail_completion)
        completed = await handle_skill_auto_track(
            engine,
            {
                "phase": "complete",
                "skill_name": "tdd",
                "stage_session_id": "stage:auto-track-failure",
                "flow_line_id": "bug-fix",
            },
        )
        payload = json.loads(completed[0].text)

        assert payload["error"] == "skill_tracking_completion_failed"
        assert get_current_entity_id(scope, engine=engine) == entity_id
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_skill_engine_reuses_durable_hook_entity_after_cache_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-hook-reuse.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    scope = "stage:hook-reuse::flow:bug-fix"
    try:
        started = await handle_skill_auto_track(
            engine,
            {
                "phase": "start",
                "skill_name": "diagnosing-bugs",
                "stage_session_id": "stage:hook-reuse",
                "flow_line_id": "bug-fix",
            },
        )
        entity_id = json.loads(started[0].text)["entity_id"]
        with skill_tracking._skill_state_lock:
            skill_tracking._stage_sessions.clear()

        skill_engine = SkillEngine(engine)
        skill_engine.register(SKILL_DEFS["diagnosing-bugs"])

        async def ok_atom(_engine, _args):
            return _text({"ok": True})

        for atom_name in STAGE_ATOMS["diagnosing-bugs"]:
            skill_engine._atoms[atom_name] = ok_atom

        result = await skill_engine.exec(
            "sp-diagnosing-bugs",
            {
                "task_description": "reuse durable hook tracking",
                "stage_session_id": scope,
                "route": "bug-onramp",
                "task_type": "debugging",
            },
            caller="claude",
        )

        assert result.success is True
        assert result.audit_trail["entity_id"] == entity_id
        assert get_current_entity_id(scope, engine=engine) == entity_id
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_real_mcp_stage_pipeline_persists_receipts_and_resumes_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-real-mcp.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    scope = "stage:real-mcp::flow:bug-fix"
    try:
        diagnosed = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:real-mcp",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
        )
        assert diagnosed["execution_status"] == "completed"
        assert diagnosed["invocation_source_authenticated"] is False
        assert diagnosed["execution_receipt_id"].startswith("workflow-receipt:")

        engine._sqlite._conn.close()
        with skill_tracking._skill_state_lock:
            skill_tracking._stage_sessions.clear()
        engine = ContextEngine(use_sqlite=True)
        restarted_skill_engine = _real_official_skill_engine(engine)
        monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
        monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: restarted_skill_engine)

        tested = await _call_sp_stage(
            "tdd",
            stage_session_id="stage:real-mcp",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
        )
        assert tested["execution_status"] == "completed"
        state = get_stage_chain_state(scope, engine=engine)
        assert state["route_id"] == "bug-onramp"
        assert state["current_stage"] == "tdd"
        assert state["current_step_index"] == 1
        assert (
            engine._sqlite._conn.execute(
                "SELECT COUNT(*) FROM official_workflow_receipts WHERE scope_id = ?",
                (scope,),
            ).fetchone()[0]
            == 2
        )
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_real_mcp_parallel_flow_lines_keep_independent_cursors(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-parallel.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    try:
        first, second = await __import__("asyncio").gather(
            _call_sp_stage(
                "diagnosing-bugs",
                stage_session_id="stage:parallel",
                flow_line_id="lane-a",
                route="bug-onramp",
                invocation_source="model",
                marker="lane a verified",
            ),
            _call_sp_stage(
                "code-review",
                stage_session_id="stage:parallel",
                flow_line_id="lane-b",
                route="review",
                invocation_source="model",
                marker="lane b verified",
            ),
        )
        assert first["success"] is True
        assert second["success"] is True
        assert (
            get_stage_chain_state("stage:parallel::flow:lane-a", engine=engine)["current_stage"]
            == "diagnosing-bugs"
        )
        assert (
            get_stage_chain_state("stage:parallel::flow:lane-b", engine=engine)["current_stage"]
            == "code-review"
        )
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_same_session_and_lane_are_isolated_by_project(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-project-scope.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    try:
        first = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:shared-projects",
            flow_line_id="codex",
            route="bug-onramp",
            invocation_source="model",
            project_id="project:first",
        )
        second = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:shared-projects",
            flow_line_id="codex",
            route="bug-onramp",
            invocation_source="model",
            project_id="project:second",
        )

        assert first["execution_status"] == "completed"
        assert second["execution_status"] == "completed"
        scopes = {
            row[0]
            for row in engine._sqlite._conn.execute(
                "SELECT scope_id FROM official_workflow_state"
            ).fetchall()
        }
        assert scopes == {
            compose_flow_scope("stage:shared-projects", "codex", "project:first"),
            compose_flow_scope("stage:shared-projects", "codex", "project:second"),
        }
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_declared_small_build_branch_can_continue_parent_route(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-branch.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    scope = compose_flow_scope("stage:branch", "codex")
    try:
        discovered = await _call_sp_stage(
            "grill-with-docs",
            stage_session_id="stage:branch",
            flow_line_id="codex",
            route="idea-to-ship",
            invocation_source="user",
        )
        implemented = await _call_sp_stage(
            "implement",
            stage_session_id="stage:branch",
            flow_line_id="codex",
            route="small-build",
            invocation_source="user",
        )

        assert discovered["execution_status"] == "completed"
        assert implemented["execution_status"] == "completed"
        state = get_stage_chain_state(scope, engine=engine)
        assert state["route_id"] == "small-build"
        assert state["current_stage"] == "implement"
        assert state["current_step_index"] == 1
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_declared_prototype_detour_can_continue_parent_route(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-prototype-branch.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    scope = compose_flow_scope("stage:prototype-branch", "codex")
    try:
        discovered = await _call_sp_stage(
            "grill-with-docs",
            stage_session_id="stage:prototype-branch",
            flow_line_id="codex",
            route="idea-to-ship",
            invocation_source="user",
        )
        handed_off = await _call_sp_stage(
            "handoff",
            stage_session_id="stage:prototype-branch",
            flow_line_id="codex",
            route="prototype-detour",
            invocation_source="user",
        )

        assert discovered["execution_status"] == "completed"
        assert handed_off["execution_status"] == "completed"
        state = get_stage_chain_state(scope, engine=engine)
        assert state["route_id"] == "prototype-detour"
        assert state["current_stage"] == "handoff"
        assert state["current_step_index"] == 1
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_unrelated_route_switch_remains_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-route-switch.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    try:
        diagnosed = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:unrelated-switch",
            flow_line_id="codex",
            route="bug-onramp",
            invocation_source="model",
        )
        switched = await _call_sp_stage(
            "code-review",
            stage_session_id="stage:unrelated-switch",
            flow_line_id="codex",
            route="review",
            invocation_source="model",
        )

        assert diagnosed["execution_status"] == "completed"
        assert switched["error"] == "route_mismatch"
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_repeated_handoff_uses_authoritative_route_step_index(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-handoff.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    try:
        for stage, source in (
            ("grill-with-docs", "user"),
            ("handoff", "user"),
            ("prototype", "model"),
            ("handoff", "user"),
        ):
            data = await _call_sp_stage(
                stage,
                stage_session_id="stage:prototype",
                flow_line_id="detour",
                route="prototype-detour",
                invocation_source=source,
                marker=f"{stage} completed at the expected route position",
            )
            assert data.get("error") is None, data

        state = get_stage_chain_state("stage:prototype::flow:detour", engine=engine)
        assert state["current_stage"] == "handoff"
        assert state["current_step_index"] == 3
        rows = engine._sqlite._conn.execute(
            "SELECT step_index, stage FROM official_workflow_receipts "
            "WHERE scope_id = ? ORDER BY step_index",
            ("stage:prototype::flow:detour",),
        ).fetchall()
        assert rows == [
            (0, "grill-with-docs"),
            (1, "handoff"),
            (2, "prototype"),
            (3, "handoff"),
        ]
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_receipt_replay_is_idempotent_and_conflict_fails_before_execution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-replay.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    execution_count = 0
    original_exec = skill_engine.exec

    async def counted_exec(*args, **kwargs):
        nonlocal execution_count
        execution_count += 1
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(skill_engine, "exec", counted_exec)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    try:
        first = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:replay",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
            marker="stable evidence",
        )
        replay = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:replay",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
            marker="stable evidence",
        )
        conflict = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:replay",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
            marker="different evidence",
        )

        assert first["execution_status"] == "completed"
        assert replay["execution_status"] == "already_completed"
        assert replay["execution_receipt_id"] == first["execution_receipt_id"]
        assert conflict["error"] == "execution_receipt_conflict"
        assert execution_count == 1
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_composite_receipt_records_attested_internal_skill_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-composite.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    try:
        result = await _call_sp_stage(
            "implement",
            stage_session_id="stage:composite",
            flow_line_id="delivery",
            route="implement-to-review",
            invocation_source="user",
        )

        assert result["execution_status"] == "completed"
        assert result["data"]["attested_composite_skills"] == ["tdd", "code-review"]
        assert len(result["data"]["composite_child_entity_ids"]) == 2
        nodes = engine.list_graph_nodes("skill_session")
        by_name = {node["name"]: node for node in nodes}
        assert set(by_name) == {"implement", "tdd", "code-review"}
        assert by_name["implement"]["metadata"]["tracking_basis"] == "execution_receipt"
        assert by_name["tdd"]["metadata"]["tracking_basis"] == "composite_receipt"
        assert by_name["code-review"]["metadata"]["tracking_basis"] == ("composite_receipt")
        parent_edges = engine.list_graph_edges("parent_of")
        assert any(
            edge["from"].endswith(by_name["implement"]["id"].removeprefix("skill_session:"))
            and edge["to"].endswith(by_name["tdd"]["id"].removeprefix("skill_session:"))
            for edge in parent_edges
        )
        assert any(
            edge["from"].endswith(by_name["tdd"]["id"].removeprefix("skill_session:"))
            and edge["to"].endswith(by_name["code-review"]["id"].removeprefix("skill_session:"))
            for edge in parent_edges
        )
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_governance_adapter_retry_reuses_receipt_scoped_entities(tmp_path, monkeypatch):
    from plastic_promise.core import workflow_state

    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-crash-retry.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    original_commit = workflow_state.commit_workflow_transition
    commit_attempts = 0

    def fail_first_commit(*args, **kwargs):
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise RuntimeError("injected post-adapter crash")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(workflow_state, "commit_workflow_transition", fail_first_commit)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    try:
        failed = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:crash-retry",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
        )
        assert failed["error"] == "injected post-adapter crash"

        retried = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:crash-retry",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
        )

        assert retried["execution_status"] == "completed"
        matching = [
            node
            for node in engine.list_graph_nodes("skill_session")
            if node["name"] == "diagnosing-bugs"
        ]
        assert len(matching) == 1
        assert matching[0]["metadata"]["tracking_basis"] == "execution_receipt"
        assert (
            engine._sqlite._conn.execute(
                "SELECT COUNT(*) FROM official_workflow_receipts"
            ).fetchone()[0]
            == 1
        )
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_replaying_completed_root_cannot_rewind_an_advanced_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-no-rewind.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    scope = "stage:no-rewind::flow:bug-fix"
    try:
        diagnosed = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:no-rewind",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
        )
        tested = await _call_sp_stage(
            "tdd",
            stage_session_id="stage:no-rewind",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
        )

        replay = await _call_sp_stage(
            "diagnosing-bugs",
            stage_session_id="stage:no-rewind",
            flow_line_id="bug-fix",
            route="bug-onramp",
            invocation_source="model",
        )

        assert diagnosed["execution_status"] == "completed"
        assert tested["execution_status"] == "completed"
        assert replay["error"] == "chain_violation"
        state = get_stage_chain_state(scope, engine=engine)
        assert state["current_stage"] == "tdd"
        assert state["current_step_index"] == 1
    finally:
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_concurrent_same_flow_receipt_executes_governance_adapter_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-concurrent-replay.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    skill_engine = _real_official_skill_engine(engine)
    execution_count = 0
    first_execution_started = asyncio.Event()
    allow_first_execution_to_finish = asyncio.Event()
    original_exec = skill_engine.exec

    async def counted_exec(*args, **kwargs):
        nonlocal execution_count
        execution_count += 1
        if execution_count == 1:
            first_execution_started.set()
            await allow_first_execution_to_finish.wait()
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(skill_engine, "exec", counted_exec)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: skill_engine)
    try:
        first_task = asyncio.create_task(
            _call_sp_stage(
                "diagnosing-bugs",
                stage_session_id="stage:concurrent-replay",
                flow_line_id="bug-fix",
                route="bug-onramp",
                invocation_source="model",
                marker="same concurrent evidence",
            )
        )
        await asyncio.wait_for(first_execution_started.wait(), timeout=1)
        second_task = asyncio.create_task(
            _call_sp_stage(
                "diagnosing-bugs",
                stage_session_id="stage:concurrent-replay",
                flow_line_id="bug-fix",
                route="bug-onramp",
                invocation_source="model",
                marker="same concurrent evidence",
            )
        )
        await asyncio.sleep(0)
        allow_first_execution_to_finish.set()

        first, second = await asyncio.gather(first_task, second_task)

        assert {first["execution_status"], second["execution_status"]} == {
            "completed",
            "already_completed",
        }
        assert execution_count == 1
    finally:
        allow_first_execution_to_finish.set()
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_cancelling_while_waiting_for_flow_lock_runs_dispatch_cleanup(monkeypatch):
    engine = MagicMock()
    waiting_for_lock = asyncio.Event()
    never_acquire = asyncio.Event()
    runtime_statuses = []
    reset_tokens = []

    class BlockingLock:
        release_calls = 0

        async def acquire(self):
            waiting_for_lock.set()
            await never_acquire.wait()

        def release(self):
            self.release_calls += 1

    blocking_lock = BlockingLock()
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(
        mcp_server,
        "_workflow_flow_lock",
        lambda _engine, _arguments: blocking_lock,
    )
    monkeypatch.setattr(
        mcp_server,
        "_record_tool_runtime_event",
        lambda _engine, _context, status: runtime_statuses.append(status),
    )

    from plastic_promise.core import traceability

    original_reset = traceability.reset_call_span_start

    def capture_reset(token):
        reset_tokens.append(token)
        original_reset(token)

    monkeypatch.setattr(traceability, "reset_call_span_start", capture_reset)

    task = asyncio.create_task(
        mcp_server.call_tool(
            "sp-stage",
            {
                "stage": "diagnosing-bugs",
                "task_description": "cancel while waiting for the flow lock",
                "stage_session_id": "stage:cancelled-waiter",
                "flow_line_id": "bug-fix",
            },
        )
    )
    await asyncio.wait_for(waiting_for_lock.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime_statuses == ["pending", "running", "error"]
    assert len(reset_tokens) == 1
    assert blocking_lock.release_calls == 0


def test_hook_continues_persisted_route_instead_of_reclassifying_continue(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-hook.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    monkeypatch.setenv("PP_PASSIVE_CONTEXT", "on")
    monkeypatch.setenv("PP_PASSIVE_TOOL_ROUTING", "on")
    monkeypatch.setenv("PP_PASSIVE_CONTEXT_MAX_CHARS", "4000")
    engine = ContextEngine(use_sqlite=True)
    scope = compose_flow_scope("stage:hook-continuation", "codex", "project:test")
    try:
        set_current_stage(
            "diagnosing-bugs",
            stage_session_id=scope,
            engine=engine,
            route_id="bug-onramp",
            current_step_index=0,
        )

        async def fake_context_supply(_engine, _args):
            return _text({"core": [], "related": [], "divergent": []})

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.context.handle_context_supply",
            fake_context_supply,
        )
        event = PassiveMemoryEvent(
            event="before_invoke",
            task_description="continue",
            task_type="general",
            source="test",
            user_text="continue",
            assistant_text="",
            call_id="call:continue",
            parent_call_id="",
            request_id="turn:continue",
            stage_session_id="stage:hook-continuation",
            flow_line_id="codex",
            project_id="project:test",
            project_policy="balanced",
            visibility="project",
            metadata={},
        )

        result = __import__("asyncio").run(passive_coordinator.before_invoke(engine, event))

        assert result["tool_route"]["route"] == "bug-onramp"
        assert result["tool_route"]["current_stage"] == "tdd"
        assert result["tool_route"]["next_stage"] == "code-review"
    finally:
        passive_coordinator._COORDINATORS.pop(id(engine), None)
        engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_session_init_resumes_same_persisted_flow_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "workflow-session-init.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=True)
    scope = "stage:resume-session::flow:codex"
    try:
        set_current_stage(
            "diagnosing-bugs",
            stage_session_id=scope,
            engine=engine,
            route_id="bug-onramp",
            current_step_index=0,
        )
        skill_engine = SkillEngine(engine)

        async def ok_atom(_engine, _args):
            return _text({})

        for atom_name in (
            "principle_activate",
            "scarf_reflect",
            "domain",
            "system",
            "defense",
            "memory_gc",
            "skill_session_start",
            "skill_session_complete",
        ):
            skill_engine._atoms[atom_name] = ok_atom
        skill_engine.register(skill_session_init)

        result = await skill_engine.exec(
            "session-init",
            {
                "task_description": "continue",
                "stage_session_id": "stage:resume-session",
                "flow_line_id": "codex",
                "context_mode": "none",
            },
            caller="claude",
        )

        contract = result.data["workflow_contract"]
        assert contract["route"] == "bug-onramp"
        assert contract["flow_scope_id"] == scope
        assert contract["current_stage"] == "diagnosing-bugs"
        assert contract["next_call"]["stage"] == "tdd"
        assert result.data["chain_state"]["valid_next"] == ["tdd"]
    finally:
        engine._sqlite._conn.close()
