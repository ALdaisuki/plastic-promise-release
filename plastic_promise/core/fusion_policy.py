from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from plastic_promise.core.retrieval_planner import RetrievalPlan


FUSION_CHANNEL_ORDER = ("vector", "bm25", "fts")
_CANDIDATE_RE = re.compile(r"^wrrf-v1:([0-9a-f]{64})$")


class FusionConfigurationError(ValueError):
    """Reject invalid or unbound retrieval fusion configuration."""


@dataclass(frozen=True)
class FusionConfig:
    k: int
    weights: Mapping[str, float]
    windows: Mapping[str, int]
    channels: tuple[str, ...]
    config_hash: str


@dataclass(frozen=True)
class FusionDecision:
    requested_policy: str
    effective_policy: str
    requested_runtime: str
    effective_runtime: str
    candidate_id: str
    capability_reason: str


def canonical_fusion_config_hash(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FusionConfigurationError("fusion_config_not_canonicalizable") from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_k(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 0xFFFFFFFF:
        raise FusionConfigurationError("invalid_k:must_be_positive_integer")
    return value


def _validate_channels(channels: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(str(channel) for channel in channels)
    if not normalized or len(set(normalized)) != len(normalized):
        raise FusionConfigurationError("invalid_channels:duplicate_or_empty")
    if any(channel not in FUSION_CHANNEL_ORDER for channel in normalized):
        raise FusionConfigurationError("invalid_channels:unknown_channel")
    expected_order = tuple(channel for channel in FUSION_CHANNEL_ORDER if channel in normalized)
    if normalized != expected_order:
        raise FusionConfigurationError("invalid_channels:noncanonical_order")
    return normalized


def _validate_weights(weights: Mapping[str, Any], channels: tuple[str, ...]) -> dict[str, float]:
    if not isinstance(weights, Mapping) or set(weights) != set(channels):
        raise FusionConfigurationError("invalid_weights:channel_mismatch")
    normalized: dict[str, float] = {}
    for channel in channels:
        value = weights[channel]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FusionConfigurationError("invalid_weights:must_be_finite_non_negative")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            raise FusionConfigurationError("invalid_weights:must_be_finite_non_negative")
        normalized[channel] = numeric
    if not any(value > 0.0 for value in normalized.values()):
        raise FusionConfigurationError("invalid_weights:all_zero")
    return normalized


def _validate_windows(
    windows: Mapping[str, Any],
    channels: tuple[str, ...],
    *,
    planner_limits: Mapping[str, int] | None = None,
) -> dict[str, int]:
    if not isinstance(windows, Mapping) or set(windows) != set(channels):
        raise FusionConfigurationError("invalid_windows:channel_mismatch")
    normalized: dict[str, int] = {}
    for channel in channels:
        value = windows[channel]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise FusionConfigurationError("invalid_windows:must_be_positive_integer")
        if planner_limits is not None and value > int(planner_limits.get(channel, 0)):
            raise FusionConfigurationError(f"invalid_windows:planner_budget_exceeded:{channel}")
        normalized[channel] = value
    return normalized


def _canonical_payload(
    *,
    k: int,
    channels: tuple[str, ...],
    weights: Mapping[str, float],
    windows: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "k": k,
        "channels": list(channels),
        "weights": {channel: weights[channel] for channel in channels},
        "windows": {channel: windows[channel] for channel in channels},
    }


def _validated_config(
    config: FusionConfig,
    *,
    planner_limits: Mapping[str, int] | None = None,
) -> FusionConfig:
    k = _validate_k(config.k)
    channels = _validate_channels(config.channels)
    weights = _validate_weights(config.weights, channels)
    windows = _validate_windows(
        config.windows,
        channels,
        planner_limits=planner_limits,
    )
    config_hash = canonical_fusion_config_hash(
        _canonical_payload(
            k=k,
            channels=channels,
            weights=weights,
            windows=windows,
        )
    )
    if config.config_hash and config.config_hash != config_hash:
        raise FusionConfigurationError("invalid_config_hash:mismatch")
    return FusionConfig(
        k=k,
        weights=weights,
        windows=windows,
        channels=channels,
        config_hash=config_hash,
    )


def _json_env(env: Mapping[str, str], name: str, reason: str) -> Any:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        raise FusionConfigurationError(reason)
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise FusionConfigurationError(reason) from exc


def load_fusion_config(
    candidate_id: str,
    plan: RetrievalPlan,
    env: Mapping[str, str] = os.environ,
) -> FusionConfig | None:
    policy = str(candidate_id or "").strip()
    if policy in {"legacy-auto", "max-v1"}:
        return None
    match = _CANDIDATE_RE.fullmatch(policy)
    if match is None:
        if policy == "wrrf-v1":
            raise FusionConfigurationError("fusion_candidate_hash_required")
        raise FusionConfigurationError("fusion_policy_invalid")

    channels = tuple(plan.fusion_channels)
    config = FusionConfig(
        k=_json_env(
            env,
            "PP_RETRIEVAL_RRF_K",
            "invalid_k:must_be_positive_integer",
        ),
        weights=_json_env(
            env,
            "PP_RETRIEVAL_RRF_WEIGHTS_JSON",
            "invalid_weights:json_required",
        ),
        windows=_json_env(
            env,
            "PP_RETRIEVAL_RRF_WINDOWS_JSON",
            "invalid_windows:json_required",
        ),
        channels=channels,
        config_hash=match.group(1),
    )
    return _validated_config(config, planner_limits=plan.channel_windows)


def _manifest_candidate_id(candidate_manifest: Any) -> str:
    if isinstance(candidate_manifest, Mapping):
        value = candidate_manifest.get("candidate_id", "")
    else:
        value = getattr(candidate_manifest, "candidate_id", "")
    return str(value or "")


def resolve_cli_fusion_policy(policy: str, candidate_manifest: Any | None) -> str:
    requested = str(policy or "").strip()
    if requested in {"legacy-auto", "max-v1"}:
        return requested

    manifest_id = _manifest_candidate_id(candidate_manifest)
    if requested == "wrrf-v1":
        if not manifest_id:
            raise FusionConfigurationError("fusion_candidate_manifest_required")
        if _CANDIDATE_RE.fullmatch(manifest_id) is None:
            raise FusionConfigurationError("fusion_candidate_manifest_invalid")
        return manifest_id

    if _CANDIDATE_RE.fullmatch(requested) is not None:
        if not manifest_id:
            raise FusionConfigurationError("fusion_candidate_manifest_required")
        if requested != manifest_id:
            raise FusionConfigurationError("fusion_candidate_manifest_mismatch")
        return requested

    raise FusionConfigurationError("fusion_policy_invalid")


def weighted_rrf(
    rankings: Mapping[str, Sequence[tuple[str, float]]],
    config: FusionConfig,
) -> list[tuple[str, float]]:
    validated = _validated_config(config)
    if not isinstance(rankings, Mapping) or set(rankings) != set(validated.channels):
        raise FusionConfigurationError("invalid_rankings:channel_mismatch")

    fused: dict[str, float] = {}
    for channel in validated.channels:
        seen: set[str] = set()
        canonical: list[tuple[str, float]] = []
        for row in rankings[channel]:
            if not isinstance(row, (tuple, list)) or len(row) < 2:
                raise FusionConfigurationError(f"invalid_rankings:row:{channel}")
            memory_id = str(row[0])
            score = row[1]
            if not memory_id:
                raise FusionConfigurationError(f"invalid_rankings:empty_id:{channel}")
            if memory_id in seen:
                raise FusionConfigurationError(f"invalid_rankings:duplicate_id:{channel}")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise FusionConfigurationError(f"invalid_rankings:score:{channel}")
            numeric_score = float(score)
            if not math.isfinite(numeric_score):
                raise FusionConfigurationError(f"invalid_rankings:score:{channel}")
            seen.add(memory_id)
            canonical.append((memory_id, numeric_score))

        canonical.sort(key=lambda row: (-row[1], row[0]))
        for rank, (memory_id, _score) in enumerate(
            canonical[: validated.windows[channel]],
            start=1,
        ):
            fused[memory_id] = fused.get(memory_id, 0.0) + (
                validated.weights[channel] / (validated.k + rank)
            )

    return sorted(fused.items(), key=lambda row: (-row[1], row[0]))
