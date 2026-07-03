"""MCP Audit & Defense tool handlers — 3 tools for audit and defense layers.

公开工具:
- audit_run       : 七维审计 + 报告查询 (action=full|report)
- audit_pre_check : 实时合规检查 — L0 硬边界 + L1 约束衰减
- defense         : 防线管理统一入口 (action=get|history|adjust|status)

内部处理器:
- handle_audit_report   : 获取最近审计报告 (由 audit_run action=report 调用)
- handle_defense_status : 获取三层防线状态 (由 defense action=status 调用)
- handle_defense_trust  : 信任分管理 (由 defense action=get|history|adjust 调用)
"""

import json
from typing import Any

from mcp.types import TextContent

# ---------------------------------------------------------------------------
# audit_run (stub)
# ---------------------------------------------------------------------------


async def handle_audit_run(engine: Any, args: dict) -> list[TextContent]:
    """Execute seven-dimension audit or retrieve report.

    Args:
        engine: ContextEngine instance.
        args: {"action"?: "full"|"report", "scope"?: str, "time_range_hours"?: int,
               "dimension"?: str, "format"?: str}.

    Returns:
        list[TextContent]: MCP response.
    """
    action = args.get("action", "full")
    if action == "report":
        return await handle_audit_report(engine, args)

    # full audit
    try:
        from plastic_promise.defense.soul_audit import SoulAuditor

        scope = args.get("scope", "global")
        time_range_hours = args.get("time_range_hours", 24)
        auditor = SoulAuditor()
        report = await auditor.run_audit()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "tool": "audit_run",
                        "scope": scope,
                        "time_range_hours": time_range_hours,
                        "report": report.to_dict() if hasattr(report, "to_dict") else str(report),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "audit_run"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# audit_pre_check
# ---------------------------------------------------------------------------


async def handle_audit_pre_check(engine: Any, args: dict) -> list[TextContent]:
    """Pre-execution compliance check against L0/L1/L2 defense layers.

    Delegates to SoulAuditor.pre_check() for real-time compliance evaluation.
    Returns layer-by-layer check results with risk assessment.

    Args:
        engine: ContextEngine instance.
        args: {"action_description": str, "action_type"?: str}.

    Returns:
        list[TextContent]: MCP response with layer checks and risk score.
    """
    try:
        action: str = args.get("action_description", "")
        action_type: str = args.get("action_type", "exec")

        from plastic_promise.defense.soul_audit import SoulAuditor

        auditor = SoulAuditor()
        check_result = auditor.pre_check(action_description=action, action_type=action_type)

        return [
            TextContent(type="text", text=json.dumps(check_result, ensure_ascii=False, indent=2))
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "audit_pre_check"}, ensure_ascii=False),
            )
        ]


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
        from plastic_promise.defense.soul_audit import SoulAuditor

        dimension = args.get("dimension")
        auditor = SoulAuditor()
        report = auditor.get_report()
        fmt = args.get("format", "json")
        if fmt == "markdown" and hasattr(report, "to_markdown"):
            return [TextContent(type="text", text=report.to_markdown())]
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "tool": "audit_report",
                        "dimension": dimension,
                        "report": report.to_dict() if hasattr(report, "to_dict") else str(report),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "audit_report"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# defense_trust (stub)
# ---------------------------------------------------------------------------

_trust_manager = None


def _get_trust_manager() -> "TrustManager":
    """Return a singleton TrustManager backed by TrustStore for persistence."""
    global _trust_manager
    if _trust_manager is None:
        from plastic_promise.defense.soul_enforcer import TrustManager
        from plastic_promise.defense.trust_store import TrustStore

        _trust_manager = TrustManager(trust_store=TrustStore())
    return _trust_manager


async def handle_defense_trust(engine: Any, args: dict) -> list[TextContent]:
    """View or adjust the current trust score and its change history.

    Args:
        engine: ContextEngine instance.
        args: {"action": str, "delta"?: float, "reason"?: str}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        action = args.get("action", "get")
        target = args.get("target", "")  # 空串=当前Agent，多Agent时传角色名
        tm = _get_trust_manager()
        if action == "get":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "trust": tm.get(target),
                            "target": target or "default",
                            "tier": tm.tier(target),
                            "autonomy_level": tm.autonomy_level(target),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
        elif action == "history":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "trust": tm.get(target),
                            "target": target or "default",
                            "tier": tm.tier(target),
                            "history": tm.history(target, 20),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
        elif action == "adjust":
            delta = args.get("delta", 0.0)
            reason = args.get("reason", "manual adjustment")
            if delta >= 0:
                new_trust = tm.boost(abs(delta) if delta == 0 else delta, reason, target=target)
            else:
                new_trust = tm.decay(abs(delta), reason, target=target)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "action": "adjust",
                            "delta": delta,
                            "target": target or "default",
                            "new_trust": new_trust,
                            "tier": tm.tier(target),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
        else:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"Unknown action '{action}'. Valid: get, history, adjust"},
                        ensure_ascii=False,
                    ),
                )
            ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "defense_trust"}, ensure_ascii=False),
            )
        ]


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

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "L0": {
                            "status": "active",
                            "name": DEFENSE_LAYERS["L0"]["name"],
                            "enforcement": DEFENSE_LAYERS["L0"]["enforcement"],
                        },
                        "L1": {
                            "status": "active",
                            "name": DEFENSE_LAYERS["L1"]["name"],
                            "enforcement": DEFENSE_LAYERS["L1"]["enforcement"],
                            "trust_threshold_loosen": DEFENSE_LAYERS["L1"][
                                "trust_threshold_loosen"
                            ],
                            "trust_threshold_tighten": DEFENSE_LAYERS["L1"][
                                "trust_threshold_tighten"
                            ],
                        },
                        "L2": {
                            "status": "active",
                            "name": DEFENSE_LAYERS["L2"]["name"],
                            "enforcement": DEFENSE_LAYERS["L2"]["enforcement"],
                            "scan_interval_hours": DEFENSE_LAYERS["L2"]["scan_interval_hours"],
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "defense_status"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# defense — 统一入口 (replaces defense_trust + defense_status as MCP tools)
# ---------------------------------------------------------------------------


async def handle_defense(engine: Any, args: dict) -> list[TextContent]:
    """防线统一入口。action: get|history|adjust|status"""
    action = args.get("action", "get")
    if action == "status":
        return await handle_defense_status(engine, args)
    else:
        return await handle_defense_trust(engine, args)
