import asyncio
import json
import sqlite3
from types import SimpleNamespace


def _engine_with_conn(conn):
    return SimpleNamespace(_sqlite=SimpleNamespace(_conn=conn))


def test_project_principle_overlay_boost_can_enter_activation_window(tmp_path):
    from plastic_promise.core.principle_overlays import (
        ensure_project_principle_overlay_schema,
        upsert_project_principle_overlay,
    )
    from plastic_promise.mcp.tools.principles import handle_principle_activate

    conn = sqlite3.connect(tmp_path / "principles.db")
    ensure_project_principle_overlay_schema(conn)
    upsert_project_principle_overlay(
        conn,
        project_id="project:test-app",
        principle_id=4,
        action="boost",
        weight_delta=100.0,
        reason="project requires explicit context provenance",
    )

    result = asyncio.run(
        handle_principle_activate(
            _engine_with_conn(conn),
            {
                "task_type": "architecture",
                "task_description": "release architecture",
                "project_id": "project:test-app",
                "max_principles": 2,
            },
        )
    )
    payload = json.loads(result[0].text)

    assert payload["project_id"] == "project:test-app"
    assert payload["overlay_applied"] is True
    assert [item["id"] for item in payload["activated"]][0] == 4
    conn.close()


def test_project_principle_overlay_suppress_removes_principle_for_project(tmp_path):
    from plastic_promise.core.principle_overlays import (
        ensure_project_principle_overlay_schema,
        upsert_project_principle_overlay,
    )
    from plastic_promise.mcp.tools.principles import handle_principle_activate

    conn = sqlite3.connect(tmp_path / "principles.db")
    ensure_project_principle_overlay_schema(conn)
    upsert_project_principle_overlay(
        conn,
        project_id="project:test-app",
        principle_id=2,
        action="suppress",
        reason="local project tracks audit in another release gate",
    )

    result = asyncio.run(
        handle_principle_activate(
            _engine_with_conn(conn),
            {
                "task_type": "architecture",
                "project_id": "project:test-app",
                "max_principles": 5,
            },
        )
    )
    payload = json.loads(result[0].text)

    assert 2 not in [item["id"] for item in payload["activated"]]
    assert "suppress" in payload["overlay_summary"]["2"]["actions"]
    conn.close()


def test_project_principle_overlay_tags_activated_principle(tmp_path):
    from plastic_promise.core.principle_overlays import (
        ensure_project_principle_overlay_schema,
        upsert_project_principle_overlay,
    )
    from plastic_promise.mcp.tools.principles import handle_principle_activate

    conn = sqlite3.connect(tmp_path / "principles.db")
    ensure_project_principle_overlay_schema(conn)
    upsert_project_principle_overlay(
        conn,
        project_id="project:test-app",
        principle_id=7,
        action="tag",
        tag="project-critical",
        reason="release architecture needs subsystem protection emphasis",
    )

    result = asyncio.run(
        handle_principle_activate(
            _engine_with_conn(conn),
            {
                "task_type": "architecture",
                "project_id": "project:test-app",
                "max_principles": 5,
            },
        )
    )
    payload = json.loads(result[0].text)
    by_id = {item["id"]: item for item in payload["activated"]}

    assert by_id[7]["project_tags"] == ["project-critical"]
    assert by_id[7]["overlay_actions"] == ["tag"]
    conn.close()
