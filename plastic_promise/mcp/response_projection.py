"""Bounded MCP response projections for agent-facing tool calls."""

from __future__ import annotations

import json
from typing import Any

_RESPONSE_MODES = {"standard", "compact", "debug"}
_DIAGNOSTIC_LEVELS = {"summary", "full"}


def resolve_response_mode(args: dict[str, Any], *, default: str = "standard") -> tuple[str, str]:
    requested = str(args.get("response_mode") or "").strip().casefold()
    if not requested and bool(args.get("debug", False)):
        requested = "debug"
    mode = requested if requested in _RESPONSE_MODES else default
    requested_diagnostics = str(args.get("diagnostics_level") or "").strip().casefold()
    diagnostics_level = (
        requested_diagnostics if requested_diagnostics in _DIAGNOSTIC_LEVELS else "summary"
    )
    return mode, diagnostics_level


def compact_context_item(item: dict[str, Any], *, content_chars: int = 240) -> dict[str, Any]:
    compact = {
        "id": str(item.get("id") or ""),
        "content": str(item.get("content") or "")[:content_chars],
        "relevance": item.get("relevance", 0.0),
        "source": str(item.get("source") or ""),
    }
    for key in ("freshness", "worth_score", "project_id", "visibility", "origin_scope"):
        value = item.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    return compact


def json_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _count_rows(value: Any) -> int:
    if isinstance(value, dict):
        return sum(len(rows) for rows in value.values() if isinstance(rows, list))
    if isinstance(value, list):
        return len(value)
    return 0


def _top_channel_rows(
    channel_rankings: Any,
    *,
    per_channel: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(channel_rankings, dict):
        return {}
    projected: dict[str, list[dict[str, Any]]] = {}
    for channel, rows in list(channel_rankings.items())[:8]:
        if not isinstance(rows, list):
            continue
        projected[str(channel)] = [
            {
                "id": str(row.get("memory_id") or row.get("id") or ""),
                "rank": row.get("rank"),
                "score": row.get("score"),
            }
            for row in rows[:per_channel]
            if isinstance(row, dict)
        ]
    return projected


def _bounded_value(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 3,
    max_items: int = 10,
    max_string: int = 180,
) -> Any:
    if depth >= max_depth:
        if isinstance(value, (dict, list, tuple)):
            return {"truncated": True, "kind": type(value).__name__}
        return str(value)[:max_string]
    if isinstance(value, dict):
        projected = {
            str(key): _bounded_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
            for key, item in list(value.items())[:max_items]
        }
        if len(value) > max_items:
            projected["_truncated_items"] = len(value) - max_items
        return projected
    if isinstance(value, (list, tuple)):
        projected = [
            _bounded_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
            for item in list(value)[:max_items]
        ]
        if len(value) > max_items:
            projected.append({"_truncated_items": len(value) - max_items})
        return projected
    if isinstance(value, str):
        return value[:max_string]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:max_string]


def _fit_diagnostics_budget(payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    bounded_bytes = max(1024, min(int(max_bytes), 6144))
    if json_size(payload) <= bounded_bytes:
        return payload

    details = payload.get("details")
    if isinstance(details, dict):
        raw_rankings = details.get("channel_rankings")
        raw_rankings = raw_rankings if isinstance(raw_rankings, dict) else {}
        raw_states = details.get("channel_states")
        raw_states = raw_states if isinstance(raw_states, dict) else {}
        compact_states = {
            str(channel): {
                key: state[key]
                for key in (
                    "planned",
                    "enabled",
                    "available",
                    "executed",
                    "participating",
                    "evidence_only",
                    "reason",
                    "weight",
                    "result_count",
                )
                if key in state
            }
            for channel, state in list(raw_states.items())[:8]
            if isinstance(state, dict)
        }
        for per_channel in (5, 3, 1, 0):
            payload["details"] = {
                "channel_rankings": {
                    str(channel): list(rows or [])[:per_channel]
                    for channel, rows in list(raw_rankings.items())[:8]
                },
                "channel_states": compact_states,
                "truncated": True,
            }
            if json_size(payload) <= bounded_bytes:
                return payload

    payload.pop("details", None)
    payload["truncated"] = True
    payload["detail_ref"] = dict(payload.get("ref") or {})
    return payload


def build_diagnostics(
    *,
    call_id: str,
    audit: dict[str, Any] | None = None,
    pipeline_stats: dict[str, Any] | None = None,
    per_item_stats: list[Any] | None = None,
    channel_rankings: dict[str, Any] | None = None,
    channel_states: dict[str, Any] | None = None,
    level: str = "summary",
    max_bytes: int = 4096,
) -> dict[str, Any]:
    audit = audit if isinstance(audit, dict) else {}
    pipeline_stats = pipeline_stats if isinstance(pipeline_stats, dict) else {}
    per_item_stats = per_item_stats if isinstance(per_item_stats, list) else []
    channel_rankings = channel_rankings if isinstance(channel_rankings, dict) else {}
    channel_states = channel_states if isinstance(channel_states, dict) else {}
    retrieval_plan = audit.get("retrieval_plan")
    retrieval_plan = retrieval_plan if isinstance(retrieval_plan, dict) else {}
    trace = audit.get("trace")
    trace = trace if isinstance(trace, dict) else {}
    retrieval_fusion = audit.get("retrieval_fusion")
    retrieval_fusion = retrieval_fusion if isinstance(retrieval_fusion, dict) else {}
    diagnostics: dict[str, Any] = {
        "level": level,
        "ref": {"kind": "call_span", "call_id": call_id},
        "summary": {
            "engine_mode": audit.get("engine_mode") or audit.get("mode"),
            "retrieval_mode": retrieval_plan.get("mode") or audit.get("mode"),
            "candidate_count": _count_rows(channel_rankings),
            "per_item_count": len(per_item_stats),
            "pipeline_keys": sorted(str(key) for key in pipeline_stats)[:20],
            "warning_count": len(audit.get("warnings") or []),
            "fallback": audit.get("fallback") or audit.get("fallback_used"),
            "retrieval_fusion": _bounded_value(
                retrieval_fusion,
                max_depth=4,
                max_items=20,
                max_string=256,
            ),
            "trace": {
                key: trace.get(key)
                for key in ("call_id", "request_scope_id", "project_id")
                if trace.get(key) not in (None, "")
            },
        },
    }
    if level == "full":
        diagnostics["details"] = {
            "pipeline_stats": _bounded_value(pipeline_stats),
            "per_item_stats": _bounded_value(per_item_stats[:6]),
            "channel_rankings": _top_channel_rows(channel_rankings, per_channel=5),
            "channel_states": _bounded_value(channel_states),
            "budget": _bounded_value(
                audit.get("budget") or retrieval_plan.get("budget") or {},
                max_depth=2,
            ),
        }
    return _fit_diagnostics_budget(diagnostics, max_bytes)
