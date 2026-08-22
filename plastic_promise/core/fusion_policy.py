from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from plastic_promise.core.constants import RRF_K

if TYPE_CHECKING:
    from plastic_promise.core.retrieval_planner import RetrievalPlan


FUSION_CHANNEL_ORDER = ("vector", "bm25", "fts")
# Extended validation universe. The three-channel tuple above is the frozen
# golden/experiment contract (recall_experiment locks it); the aisle arm is a
# metadata-prior channel that only plans which opt in will carry.
FUSION_CHANNELS_ALL = ("vector", "bm25", "fts", "aisle")
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


def validate_fusion_candidate_binding(
    candidate_id: object,
    payload: object,
) -> dict[str, Any]:
    """Validate a serialized WRRF candidate and return its canonical payload.

    Benchmark evidence is consumed outside the retrieval planner, so it must
    not trust a candidate id or a JSON object merely because both are present.
    Reusing the planner's validators keeps the evidence binding identical to
    the runtime binding.
    """

    if not isinstance(candidate_id, str) or not _CANDIDATE_RE.fullmatch(candidate_id):
        raise FusionConfigurationError("fusion_candidate_binding_invalid")
    if not isinstance(payload, Mapping):
        raise FusionConfigurationError("fusion_config_invalid")
    expected_fields = {"k", "channels", "weights", "windows"}
    if set(payload) != expected_fields:
        raise FusionConfigurationError("fusion_config_fields_invalid")
    channels = payload.get("channels")
    if not isinstance(channels, (list, tuple)):
        raise FusionConfigurationError("fusion_config_channels_invalid")
    config = _validated_config(
        FusionConfig(
            k=payload.get("k"),
            channels=tuple(channels),
            weights=payload.get("weights"),
            windows=payload.get("windows"),
            config_hash=candidate_id.split(":", 1)[1],
        )
    )
    canonical = _canonical_payload(
        k=config.k,
        channels=config.channels,
        weights=config.weights,
        windows=config.windows,
    )
    if f"wrrf-v1:{config.config_hash}" != candidate_id:
        raise FusionConfigurationError("fusion_candidate_binding_mismatch")
    return canonical


def _validate_k(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 0xFFFFFFFF:
        raise FusionConfigurationError("invalid_k:must_be_positive_integer")
    return value


def _validate_channels(channels: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(str(channel) for channel in channels)
    if not normalized or len(set(normalized)) != len(normalized):
        raise FusionConfigurationError("invalid_channels:duplicate_or_empty")
    if any(channel not in FUSION_CHANNELS_ALL for channel in normalized):
        raise FusionConfigurationError("invalid_channels:unknown_channel")
    expected_order = tuple(channel for channel in FUSION_CHANNELS_ALL if channel in normalized)
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


DEFAULT_FUSION_WINDOW = 80


def default_fusion_config(
	plan: "RetrievalPlan",
	env: Mapping[str, str] = os.environ,
) -> "FusionConfig | None":
	"""Zero-config equal-vote RRF for the legacy-auto retrieval path.

	Three-Librarians alignment: every channel casts one equal ballot of
	``weight / (k + rank)``; raw channel scores only order candidates within
	their own channel and are never averaged across currencies. Returns None
	when the plan carries no fusion channels so callers keep legacy behavior.
	"""
	channels = tuple(plan.fusion_channels)
	if not channels:
		return None
	raw_k = str(env.get("PP_RETRIEVAL_RRF_K", "")).strip() or str(RRF_K)
	try:
		k = int(raw_k)
	except ValueError as exc:
		raise FusionConfigurationError("invalid_k:must_be_positive_integer") from exc
	try:
		window = int(str(env.get("PP_FUSION_DEFAULT_WINDOW", str(DEFAULT_FUSION_WINDOW))).strip())
	except ValueError as exc:
		raise FusionConfigurationError("invalid_windows:must_be_positive_integer") from exc
	windows = {
		channel: min(int(plan.channel_windows.get(channel, window)), window)
		for channel in channels
	}
	weights = {channel: 1.0 for channel in channels}
	return _validated_config(
		FusionConfig(
			k=k,
			weights=weights,
			windows=windows,
			channels=channels,
			config_hash="",
		),
		planner_limits=plan.channel_windows,
	)

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


def weighted_max_v1(
    rankings: Mapping[str, Sequence[tuple[str, float]]],
    *,
    vector_weight: float = 0.5,
    has_vector: bool = True,
) -> list[tuple[str, float]]:
    """Fuse vector, BM25, and FTS scores with the production max-v1 policy."""
    expected_channels = {"vector", "bm25", "fts"}
    if not isinstance(rankings, Mapping) or set(rankings) != expected_channels:
        raise FusionConfigurationError("invalid_rankings:channel_mismatch")
    if (
        isinstance(vector_weight, bool)
        or not isinstance(vector_weight, (int, float))
        or not math.isfinite(float(vector_weight))
        or not 0.0 <= float(vector_weight) <= 1.0
    ):
        raise FusionConfigurationError("invalid_vector_weight")

    normalized: dict[str, list[tuple[str, float]]] = {}
    for channel in FUSION_CHANNEL_ORDER:
        rows: list[tuple[str, float]] = []
        for row in rankings[channel]:
            if not isinstance(row, (tuple, list)) or len(row) < 2:
                raise FusionConfigurationError(f"invalid_rankings:row:{channel}")
            memory_id = str(row[0])
            score = row[1]
            if not memory_id:
                raise FusionConfigurationError(f"invalid_rankings:empty_id:{channel}")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise FusionConfigurationError(f"invalid_rankings:score:{channel}")
            numeric_score = float(score)
            if not math.isfinite(numeric_score):
                raise FusionConfigurationError(f"invalid_rankings:score:{channel}")
            rows.append((memory_id, numeric_score))
        normalized[channel] = rows

    fused: dict[str, float] = {}
    encounter_order: dict[str, int] = {}

    def merge(memory_id: str, score: float) -> None:
        encounter_order.setdefault(memory_id, len(encounter_order))
        fused[memory_id] = max(fused.get(memory_id, -math.inf), score)

    if has_vector and normalized["vector"]:
        weight = float(vector_weight)
        text_weight = 1.0 - weight
        for memory_id, score in normalized["vector"]:
            merge(memory_id, score * weight)
        for memory_id, score in normalized["bm25"]:
            weighted = score * text_weight
            if score >= 0.90:
                weighted = score
            elif score >= 0.75:
                weighted = max(weighted, score * 0.9)
            merge(memory_id, weighted)
        for memory_id, score in normalized["fts"]:
            weighted = score if score >= 0.85 else score * text_weight
            merge(memory_id, weighted)
    else:
        for memory_id, score in normalized["bm25"]:
            merge(memory_id, score * 0.8)
        for memory_id, score in normalized["fts"]:
            weighted = score if score >= 0.85 else score * 0.8
            merge(memory_id, weighted)

    return sorted(fused.items(), key=lambda row: (-row[1], encounter_order[row[0]]))
