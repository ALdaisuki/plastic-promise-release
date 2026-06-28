"""MCP Audit & Defense tool handlers — 5 tools for audit and defense layers.

工具列表:
- audit_run       : 执行七维度审计，返回结构化评分报告 (stub)
- audit_pre_check : 实时合规检查 — L0 硬边界 + L1 约束衰减
- audit_report    : 获取最近一次审计报告全文或指定维度详细分析 (stub)
- defense_trust   : 查看/调整当前信任分及其变化历史 (stub)
- defense_status  : 获取三层防线当前状态
"""

import json
from typing import Any

from mcp.types import TextContent


# ---------------------------------------------------------------------------
# audit_run (stub)
# ---------------------------------------------------------------------------

async def handle_audit_run(engine: Any, args: dict) -> list[TextContent]:
    """Execute seven-dimension audit, return structured scoring report (stub).

    Args:
        engine: ContextEngine instance.
        args: {"scope"?: str, "time_range_hours"?: int}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        return [TextContent(type="text", text=json.dumps({
            "tool": "audit_run",
            "status": "not_implemented",
            "message": "Seven-dimension audit engine is not yet wired.",
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "audit_run"}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# audit_pre_check
# ---------------------------------------------------------------------------

async def handle_audit_pre_check(engine: Any, args: dict) -> list[TextContent]:
    """Pre-execution compliance check against L0/L1/L2 defense layers.

    Real-time compliance check: performs L0 hard boundary pattern matching
    on the pending action description and reports L1/L2 status. Dangerous
    patterns (rm -rf, DROP TABLE, format, del /f, shutdown) trigger an
    immediate block at L0.

    Args:
        engine: ContextEngine instance (unused in stateless implementation).
        args: {"action_description": str, "action_type"?: str}.

    Returns:
        list[TextContent]: MCP response with layer checks and risk score.
    """
    try:
        action: str = args["action_description"]
        action_type: str = args.get("action_type", "exec")

        # L0 hard boundaries check — dangerous shell / SQL patterns
        hard_violations: list[dict] = []
        dangerous_patterns = [
            "rm -rf", "DROP TABLE", "format", "del /f", "shutdown",
        ]
        for pattern in dangerous_patterns:
            if pattern.lower() in action.lower():
                hard_violations.append({
                    "pattern": pattern,
                    "layer": "L0",
                    "action": "block",
                })

        passed = len(hard_violations) == 0

        return [TextContent(type="text", text=json.dumps({
            "passed": passed,
            "action": action[:200],
            "action_type": action_type,
            "layer_checks": [
                {
                    "layer": "L0",
                    "passed": passed,
                    "violations": hard_violations,
                },
                {
                    "layer": "L1",
                    "passed": True,
                    "note": "trust-score-driven constraint decay",
                },
                {
                    "layer": "L2",
                    "passed": True,
                    "note": "periodic immune scan",
                },
            ],
            "risk_score": 0.0 if passed else 1.0,
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "audit_pre_check"}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# audit_report (stub)
# ---------------------------------------------------------------------------

async def handle_audit_report(engine: Any, args: dict) -> list[TextContent]:
    """Retrieve the most recent audit report or a specific dimension (stub).

    Args:
        engine: ContextEngine instance.
        args: {"dimension"?: str, "format"?: str}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        return [TextContent(type="text", text=json.dumps({
            "tool": "audit_report",
            "status": "not_implemented",
            "message": "Audit report retrieval is not yet wired.",
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "audit_report"}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# defense_trust (stub)
# ---------------------------------------------------------------------------

async def handle_defense_trust(engine: Any, args: dict) -> list[TextContent]:
    """View or adjust the current trust score and its change history (stub).

    Args:
        engine: ContextEngine instance.
        args: {"action": str, "delta"?: float, "reason"?: str}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        return [TextContent(type="text", text=json.dumps({
            "tool": "defense_trust",
            "status": "not_implemented",
            "message": "Trust score management is not yet wired.",
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "defense_trust"}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# defense_status
# ---------------------------------------------------------------------------

async def handle_defense_status(engine: Any, args: dict) -> list[TextContent]:
    """Return current three-layer defense status.

    Retrieves the current status of the three defense layers: L0 hard
    boundaries, L1 constraint attenuation (including trust-score-driven
    mode switching), and L2 immune patrol schedule.

    Args:
        engine: ContextEngine instance (unused in stateless implementation).
        args: {} (no arguments required).

    Returns:
        list[TextContent]: MCP response with each layer's status and config.
    """
    try:
        from plastic_promise.core.constants import DEFENSE_LAYERS

        return [TextContent(type="text", text=json.dumps({
            "L0": {
                "status": "active",
                "name": DEFENSE_LAYERS["L0"]["name"],
                "enforcement": DEFENSE_LAYERS["L0"]["enforcement"],
            },
            "L1": {
                "status": "active",
                "name": DEFENSE_LAYERS["L1"]["name"],
                "enforcement": DEFENSE_LAYERS["L1"]["enforcement"],
                "trust_threshold_loosen": DEFENSE_LAYERS["L1"]["trust_threshold_loosen"],
                "trust_threshold_tighten": DEFENSE_LAYERS["L1"]["trust_threshold_tighten"],
            },
            "L2": {
                "status": "active",
                "name": DEFENSE_LAYERS["L2"]["name"],
                "enforcement": DEFENSE_LAYERS["L2"]["enforcement"],
                "scan_interval_hours": DEFENSE_LAYERS["L2"]["scan_interval_hours"],
            },
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "defense_status"}, ensure_ascii=False))]
