"""Bounded, redacted projections of retrieval debug metadata."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

SCHEMA = "retrieval_explain_v1"
METADATA_KEY = "retrieval_explain_v1"
MAX_CHANNELS = 8
MAX_CHANNEL_ITEMS = 10
MAX_ITEMS = 20

_MAX_ID_LENGTH = 160
_MAX_CHANNEL_LENGTH = 64
_MAX_TEXT_LENGTH = 120
_MAX_NUMBER = 1_000_000_000_000
_MAX_STAGE_TIMING_MS = 86_400_000.0

_SENSITIVE_MARKERS = (
    "api_key",
    "api-key",
    "apikey",
    "authorization",
    "bearer ",
    "password",
    "passwd",
    "secret",
    "token",
)

_STATE_BOOL_FIELDS = (
    "planned",
    "enabled",
    "available",
    "executed",
    "participating",
    "evidence_only",
)
_STATE_TEXT_FIELDS = ("reason",)
_STATE_INT_FIELDS = ("result_count",)

_ITEM_NUMBER_FIELDS = (
    "initial_score",
    "final_score",
    "source_penalty",
    "relevance",
    "score",
    "mmr_score",
    "fused_score",
)
_ITEM_INT_FIELDS = ("rank",)
_ITEM_NONNEGATIVE_INT_FIELDS = ("ordinal", "source_start", "source_end")
_ITEM_TEXT_FIELDS = (
    "layer",
    "filter_decision",
    "filter_reason",
    "gate_decision",
    "gate_reason",
    "retrieval_source",
)

_PIPELINE_INT_FIELDS = (
    "vector_count",
    "bm25_count",
    "fts_count",
    "graph_count",
    "fused_count",
    "candidate_count",
    "result_count",
    "reranked_count",
    "after_noise_filter",
    "after_source_filter",
    "after_hard_score_filter",
    "after_mmr",
    "core_count",
    "related_count",
    "divergent_count",
    "canonical_hot_count",
    "context_gate_evaluated",
)
_PIPELINE_NUMBER_FIELDS = (
    "minimum_score",
    "maximum_score",
    "mean_score",
)
_PIPELINE_TEXT_FIELDS = (
    "engine_mode",
    "retrieval_mode",
    "fusion_mode",
    "fusion_policy",
    "fusion_runtime",
    "fusion_algorithm",
    "fallback_reason",
    "degradation_state",
)
_PIPELINE_BOOL_FIELDS = ("degraded",)
_PIPELINE_STAGE_TIMING_FIELDS = (
    "principle_injection",
    "snapshot_parse",
    "candidate_retrieval",
    "filter_and_layer",
    "fallback_filter_and_layer",
    "total",
)


def retrieval_explain_enabled() -> bool:
    """Return whether bounded explain capture is explicitly enabled."""
    return os.environ.get("PP_RETRIEVAL_EXPLAIN") == "1"


def _source_value(source: Any, field: str, default: Any) -> Any:
    try:
        if isinstance(source, Mapping):
            return source.get(field, default)
        return getattr(source, field, default)
    except Exception:
        return default


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        try:
            converted = asdict(value)
        except Exception:
            return None
        return converted if isinstance(converted, Mapping) else None
    return None


def _safe_text(value: Any, *, limit: int = _MAX_TEXT_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    folded = cleaned.casefold()
    if any(marker in folded for marker in _SENSITIVE_MARKERS):
        return None
    return cleaned[:limit]


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 32:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > _MAX_NUMBER:
        return None
    if number.is_integer():
        return int(number)
    return round(number, 6)


def _safe_int(value: Any, *, minimum: int = 0) -> int | None:
    number = _safe_number(value)
    if number is None or isinstance(number, float):
        return None
    if number < minimum:
        return None
    return number


def _project_channel_item(value: Any) -> dict[str, Any] | None:
    row = _as_mapping(value)
    if row is None:
        return None
    item_id = _safe_text(row.get("id", row.get("memory_id")), limit=_MAX_ID_LENGTH)
    if item_id is None:
        return None
    projected: dict[str, Any] = {"id": item_id}
    rank = _safe_int(row.get("rank"), minimum=1)
    if rank is not None:
        projected["rank"] = rank
    score = _safe_number(row.get("score"))
    if score is not None:
        projected["score"] = score
    _project_chunk_fields(projected, row)
    return projected


def _project_chunk_fields(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Project structural anchors without exposing chunk or memory body text."""
    for field in (
        "chunk_id",
        "parent_memory_id",
        "kind",
        "source_hash",
        "text_hash",
        "match_status",
    ):
        text = _safe_text(source.get(field), limit=_MAX_ID_LENGTH)
        if text is not None:
            target[field] = text
    for field in _ITEM_NONNEGATIVE_INT_FIELDS:
        number = _safe_int(source.get(field), minimum=0)
        if number is not None:
            target[field] = number
    raw_header = source.get("header_path", source.get("heading_path"))
    if isinstance(raw_header, str):
        header_path = [_safe_text(raw_header)]
    elif isinstance(raw_header, (list, tuple)):
        header_path = [_safe_text(value) for value in raw_header[:16]]
    else:
        header_path = []
    cleaned = [value for value in header_path if value is not None]
    if cleaned:
        target["header_path"] = cleaned


def _project_channel_state(value: Any) -> dict[str, Any]:
    state = _as_mapping(value)
    if state is None:
        return {}
    projected: dict[str, Any] = {}
    for field in _STATE_BOOL_FIELDS:
        if isinstance(state.get(field), bool):
            projected[field] = state[field]
    for field in _STATE_TEXT_FIELDS:
        text = _safe_text(state.get(field))
        if text is not None:
            projected[field] = text
    for field in _STATE_INT_FIELDS:
        number = _safe_int(state.get(field))
        if number is not None:
            projected[field] = number
    return projected


def _project_channels(source: Any) -> tuple[list[dict[str, Any]], bool, bool]:
    rankings = _source_value(source, "channel_rankings", {})
    states = _source_value(source, "channel_states", {})
    rankings = rankings if isinstance(rankings, Mapping) else {}
    states = states if isinstance(states, Mapping) else {}

    channel_names = {
        name
        for raw_name in (*rankings.keys(), *states.keys())
        if (name := _safe_text(raw_name, limit=_MAX_CHANNEL_LENGTH)) is not None
    }
    ordered_names = sorted(channel_names)
    channels_truncated = len(ordered_names) > MAX_CHANNELS
    channel_items_truncated = False
    channels: list[dict[str, Any]] = []

    for name in ordered_names[:MAX_CHANNELS]:
        raw_rows = rankings.get(name, [])
        raw_rows = raw_rows if isinstance(raw_rows, (list, tuple)) else []
        projected_rows = [
            projected
            for row in raw_rows
            if (projected := _project_channel_item(row)) is not None
        ]
        if len(projected_rows) > MAX_CHANNEL_ITEMS:
            channel_items_truncated = True
        channels.append(
            {
                "name": name,
                "state": _project_channel_state(states.get(name)),
                "items": projected_rows[:MAX_CHANNEL_ITEMS],
            }
        )

    for name in ordered_names[MAX_CHANNELS:]:
        raw_rows = rankings.get(name, [])
        if isinstance(raw_rows, (list, tuple)) and len(raw_rows) > MAX_CHANNEL_ITEMS:
            channel_items_truncated = True

    return channels, channels_truncated, channel_items_truncated


def _sanitize_snapshot_channels(value: Any) -> tuple[list[dict[str, Any]], bool, bool]:
    raw_channels = value if isinstance(value, (list, tuple)) else []
    channels_truncated = len(raw_channels) > MAX_CHANNELS
    channel_items_truncated = False
    channels: list[dict[str, Any]] = []

    for raw_channel in raw_channels[: MAX_CHANNELS + 1]:
        row = _as_mapping(raw_channel)
        if row is None:
            continue
        name = _safe_text(row.get("name"), limit=_MAX_CHANNEL_LENGTH)
        if name is None:
            continue
        raw_items = row.get("items")
        raw_items = raw_items if isinstance(raw_items, (list, tuple)) else []
        projected_items = [
            projected
            for item in raw_items[: MAX_CHANNEL_ITEMS + 1]
            if (projected := _project_channel_item(item)) is not None
        ]
        if len(raw_items) > MAX_CHANNEL_ITEMS or len(projected_items) > MAX_CHANNEL_ITEMS:
            channel_items_truncated = True
        channels.append(
            {
                "name": name,
                "state": _project_channel_state(row.get("state")),
                "items": projected_items[:MAX_CHANNEL_ITEMS],
            }
        )

    if len(channels) > MAX_CHANNELS:
        channels_truncated = True
    return channels[:MAX_CHANNELS], channels_truncated, channel_items_truncated


def _project_item(value: Any) -> dict[str, Any] | None:
    row = _as_mapping(value)
    if row is None:
        return None
    item_id = _safe_text(row.get("id", row.get("memory_id")), limit=_MAX_ID_LENGTH)
    if item_id is None:
        return None
    projected: dict[str, Any] = {"id": item_id}
    for field in _ITEM_INT_FIELDS:
        number = _safe_int(row.get(field), minimum=1)
        if number is not None:
            projected[field] = number
    for field in _ITEM_NUMBER_FIELDS:
        number = _safe_number(row.get(field))
        if number is not None:
            projected[field] = number
    for field in _ITEM_TEXT_FIELDS:
        text = _safe_text(row.get(field))
        if text is not None:
            projected[field] = text
    _project_chunk_fields(projected, row)
    return projected


def _project_items(source: Any) -> tuple[list[dict[str, Any]], bool]:
    raw_items = _source_value(source, "per_item_stats", [])
    raw_items = raw_items if isinstance(raw_items, (list, tuple)) else []
    items = [
        projected
        for row in raw_items
        if (projected := _project_item(row)) is not None
    ]
    return items[:MAX_ITEMS], len(items) > MAX_ITEMS


def _copy_pipeline_fields(
    target: dict[str, Any],
    source: Mapping[str, Any],
    fields: tuple[str, ...],
    converter: Any,
) -> None:
    for field in fields:
        value = converter(source.get(field))
        if value is not None:
            target[field] = value


def _project_stage_timings(value: Any) -> dict[str, float]:
    raw_timings = value
    if isinstance(raw_timings, str):
        if len(raw_timings) > 4096:
            return {}
        try:
            raw_timings = json.loads(raw_timings)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw_timings, Mapping):
        return {}

    timings: dict[str, float] = {}
    for field in _PIPELINE_STAGE_TIMING_FIELDS:
        number = _safe_number(raw_timings.get(field))
        if number is None:
            continue
        duration_ms = float(number)
        if duration_ms < 0 or duration_ms > _MAX_STAGE_TIMING_MS:
            continue
        timings[field] = round(duration_ms, 6)
    return timings


def _project_pipeline(source: Any) -> dict[str, Any]:
    raw_pipeline = _source_value(source, "pipeline_stats", {})
    raw_pipeline = raw_pipeline if isinstance(raw_pipeline, Mapping) else {}
    pipeline: dict[str, Any] = {}
    _copy_pipeline_fields(pipeline, raw_pipeline, _PIPELINE_INT_FIELDS, _safe_int)
    _copy_pipeline_fields(pipeline, raw_pipeline, _PIPELINE_NUMBER_FIELDS, _safe_number)
    _copy_pipeline_fields(pipeline, raw_pipeline, _PIPELINE_TEXT_FIELDS, _safe_text)
    for field in _PIPELINE_BOOL_FIELDS:
        if isinstance(raw_pipeline.get(field), bool):
            pipeline[field] = raw_pipeline[field]
    raw_stage_timings = raw_pipeline.get("stage_timings")
    if raw_stage_timings is None:
        raw_stage_timings = raw_pipeline.get("stage_timing_ms")
    stage_timings = _project_stage_timings(raw_stage_timings)
    if stage_timings:
        pipeline["stage_timings"] = stage_timings

    raw_audit = _source_value(source, "audit_metadata", None)
    if raw_audit is None:
        raw_audit = _source_value(source, "audit", {})
    audit = raw_audit if isinstance(raw_audit, Mapping) else {}

    audit_fields = {
        "engine_mode": audit.get("engine_mode"),
        "retrieval_mode": audit.get("mode"),
    }
    retrieval_plan = audit.get("retrieval_plan")
    if isinstance(retrieval_plan, Mapping) and audit_fields["retrieval_mode"] is None:
        audit_fields["retrieval_mode"] = retrieval_plan.get("mode")
    for field, value in audit_fields.items():
        text = _safe_text(value)
        if field not in pipeline and text is not None:
            pipeline[field] = text

    fusion = audit.get("retrieval_fusion")
    if isinstance(fusion, Mapping):
        fusion_fields = {
            "fusion_policy": fusion.get("effective_policy"),
            "fusion_runtime": fusion.get("effective_runtime"),
            "fusion_algorithm": fusion.get("algorithm"),
        }
        for field, value in fusion_fields.items():
            text = _safe_text(value)
            if field not in pipeline and text is not None:
                pipeline[field] = text

    if "degraded" not in pipeline and isinstance(audit.get("degraded"), bool):
        pipeline["degraded"] = audit["degraded"]
    return pipeline


def _has_explain_evidence(
    channels: list[dict[str, Any]],
    items: list[dict[str, Any]],
    pipeline: Mapping[str, Any],
) -> bool:
    """Return whether a snapshot contains captured retrieval evidence."""
    if channels or items:
        return True
    return any(
        key in pipeline
        for key in (
            *_PIPELINE_INT_FIELDS,
            *_PIPELINE_NUMBER_FIELDS,
            "stage_timings",
            "fallback_reason",
            "degradation_state",
            "degraded",
        )
    )


def sanitize_retrieval_explain_snapshot(value: Any) -> dict[str, Any] | None:
    """Re-project an untrusted stored snapshot through the public allowlist."""
    snapshot = _as_mapping(value)
    if snapshot is None or snapshot.get("schema") != SCHEMA:
        return None

    channels, channels_truncated, channel_items_truncated = _sanitize_snapshot_channels(
        snapshot.get("channels")
    )
    raw_items = snapshot.get("items")
    raw_items = raw_items if isinstance(raw_items, (list, tuple)) else []
    projected_items = [
        projected
        for item in raw_items[: MAX_ITEMS + 1]
        if (projected := _project_item(item)) is not None
    ]
    items_truncated = len(raw_items) > MAX_ITEMS or len(projected_items) > MAX_ITEMS

    raw_pipeline = snapshot.get("pipeline")
    if not isinstance(raw_pipeline, Mapping):
        raw_pipeline = snapshot.get("pipeline_stats")
    pipeline = _project_pipeline(
        {"pipeline_stats": raw_pipeline if isinstance(raw_pipeline, Mapping) else {}}
    )
    # Legacy callers could persist a schema-only object when debug capture was
    # disabled. Treat that as not captured instead of presenting an empty
    # explanation as available in the Dashboard.
    if not _has_explain_evidence(channels, projected_items, pipeline):
        return None

    stored_truncated = _as_mapping(snapshot.get("truncated")) or {}
    return {
        "schema": SCHEMA,
        "channels": channels,
        "items": projected_items[:MAX_ITEMS],
        "pipeline": pipeline,
        "truncated": {
            "channels": channels_truncated or stored_truncated.get("channels") is True,
            "channel_items": channel_items_truncated
            or stored_truncated.get("channel_items") is True,
            "items": items_truncated or stored_truncated.get("items") is True,
        },
    }


def build_retrieval_explain_snapshot(source: Any) -> dict[str, Any] | None:
    """Project already-produced debug data into a bounded trace snapshot."""
    if not retrieval_explain_enabled():
        return None

    channels, channels_truncated, channel_items_truncated = _project_channels(source)
    items, items_truncated = _project_items(source)
    pipeline = _project_pipeline(source)
    # The feature gate is deliberately independent from retrieval debug mode.
    # A normal Python call has only the public pack/audit fields; persisting an
    # empty schema here would make the Dashboard claim that an explanation was
    # captured when there is no channel, candidate, counter, or timing evidence.
    if not _has_explain_evidence(channels, items, pipeline):
        return None
    return {
        "schema": SCHEMA,
        "channels": channels,
        "items": items,
        "pipeline": pipeline,
        "truncated": {
            "channels": channels_truncated,
            "channel_items": channel_items_truncated,
            "items": items_truncated,
        },
    }
