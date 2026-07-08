import asyncio
import json
import sqlite3

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.mcp import server as mcp_server
from plastic_promise.mcp.tools.mgp_shadow import handle_mgp_shadow_bridge


def test_mgp_bridge_mode_lifecycle_and_operation_mapping():
    from plastic_promise.core.mgp_shadow import MgpShadowBridge

    bridge = MgpShadowBridge(mode="off")

    assert bridge.status()["mode"] == "off"
    assert bridge.set_mode("shadow")["mode"] == "shadow"
    assert bridge.set_mode("inject")["mode"] == "inject"
    assert bridge.map_operation("write")["plastic_operation"] == "memory_store"
    assert bridge.map_operation("search")["plastic_operation"] == "memory_recall/context_supply"
    assert bridge.map_operation("revoke")["plastic_operation"] == "memory_correct"


def test_mgp_shadow_evaluate_records_event_without_memory_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "mgp.db"))
    engine = ContextEngine(use_sqlite=True)
    before_count = engine.memory_count

    result = asyncio.run(
        handle_mgp_shadow_bridge(
            engine,
            {
                "action": "evaluate",
                "mode": "shadow",
                "operation": "write",
                "subject": "project:plastic-promise",
                "content": "candidate memory that must not be stored",
                "policy_context": {
                    "project_id": "project:plastic-promise",
                    "stage_session_id": "stage_mgp",
                    "flow_line_id": "flow_mgp",
                    "request_id": "shadow-write",
                    "trust_tier": "medium",
                },
            },
        )
    )
    payload = json.loads(result[0].text)

    assert engine.memory_count == before_count
    assert payload["mode"] == "shadow"
    assert payload["audit_only"] is True
    assert payload["plastic_operation"] == "memory_store"
    assert payload["event_id"].startswith("evt_")

    conn = sqlite3.connect(tmp_path / "mgp.db")
    row = conn.execute(
        """
        SELECT event_kind, event_name, status, project_id, metadata_json
        FROM runtime_events
        WHERE event_id = ?
        """,
        (payload["event_id"],),
    ).fetchone()
    conn.close()

    assert row[0:4] == (
        "agent",
        "mgp_shadow_bridge",
        "completed",
        "project:plastic-promise",
    )
    assert json.loads(row[4])["operation"] == "write"


def test_mgp_inject_mode_is_reserved_and_audit_only():
    from plastic_promise.core.mgp_shadow import MgpShadowBridge

    bridge = MgpShadowBridge(mode="inject")
    result = bridge.evaluate(
        {
            "operation": "search",
            "subject": "project:plastic-promise",
            "policy_context": {"project_id": "project:plastic-promise"},
        }
    )

    assert result["mode"] == "inject"
    assert result["audit_only"] is True
    assert result["inject_reserved"] is True
    assert result["plastic_operation"] == "memory_recall/context_supply"


def test_mgp_shadow_bridge_mcp_schema_exposed():
    tool = next(
        tool for tool in asyncio.run(mcp_server.list_tools()) if tool.name == "mgp_shadow_bridge"
    )
    props = tool.inputSchema["properties"]

    assert set(props["action"]["enum"]) == {"status", "set_mode", "evaluate"}
    assert set(props["mode"]["enum"]) == {"off", "shadow", "inject"}
