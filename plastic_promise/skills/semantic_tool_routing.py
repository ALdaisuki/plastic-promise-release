"""Bounded cloud JSON classification for ambiguous official workflow routing."""

from __future__ import annotations

import math
import os
import re
import threading
from typing import Any

from plastic_promise.core.memory_proposals import contains_secret
from plastic_promise.core.server_structured_json import (
    OpenAICompatibleJSONProvider,
    StructuredJSONProvider,
)
from plastic_promise.skills.tool_routing import (
    OFFICIAL_WORKFLOW_ROUTES,
    invocation_policy,
)

_SCHEMA_VERSION = "workflow-route-classification-v1"
_EXPECTED_KEYS = frozenset({"schema_version", "decision", "route", "confidence", "evidence"})
_CONTINUATION_RE = re.compile(r"^(?:继续|接着|继续吧|continue|go\s+on|proceed)[.!。！]?$", re.I)
_CANCELLATION_RE = re.compile(r"^(?:取消|停止|停下|算了|先不做|cancel|stop|never\s+mind)\b", re.I)
_QUESTION_RE = re.compile(
    r"^(?:为什么|为何|怎么|如何|是否|能否|可否|要不要|有没有|是不是|"
    r"why|how|what|when|where|who|which|whether|can|could|would|should|do|does|did)\b",
    re.I,
)
_NEGATION_RE = re.compile(
    r"^(?:不要|无需|不需要|不能|不应|不该|禁止|别|do\s+not|don't|never|no\s+need)\b",
    re.I,
)
_CLASSIFIER: SemanticWorkflowRouteClassifier | None = None
_CLASSIFIER_LOCK = threading.Lock()


def semantic_routing_mode() -> str:
    mode = os.getenv("PP_PASSIVE_SEMANTIC_ROUTING", "off").strip().casefold()
    return mode if mode in {"off", "shadow", "on"} else "off"


def semantic_routing_timeout_seconds() -> float:
    try:
        value = float(os.getenv("PP_PASSIVE_SEMANTIC_ROUTING_TIMEOUT_SECONDS", "2.0"))
    except (TypeError, ValueError):
        value = 2.0
    return min(5.0, max(0.1, value))


def semantic_routing_eligible(text: object) -> bool:
    value = " ".join(str(text or "").split())
    if not value or len(value) > 2_000 or contains_secret(value):
        return False
    if value.endswith(("?", "？")):
        return False
    return not any(
        pattern.search(value)
        for pattern in (_CONTINUATION_RE, _CANCELLATION_RE, _QUESTION_RE, _NEGATION_RE)
    )


def _model_entry_routes() -> tuple[str, ...]:
    return tuple(
        sorted(
            route_id
            for route_id, route in OFFICIAL_WORKFLOW_ROUTES.items()
            if route.get("stages") and invocation_policy(str(route["stages"][0])) == "model"
        )
    )


class SemanticWorkflowRouteClassifier:
    """Validate advisory route candidates returned by a structured JSON model."""

    def __init__(self, provider: StructuredJSONProvider) -> None:
        self._provider = provider

    def classify(self, *, task_description: str, task_type: str) -> dict[str, Any]:
        if not semantic_routing_eligible(task_description):
            return {"accepted": False, "reason": "semantic_route_ineligible"}
        allowed_routes = _model_entry_routes()
        payload = self._provider.complete_json(
            system_prompt=(
                "Return one strict JSON object for advisory workflow routing. "
                "The schema is {schema_version,decision,route,confidence,evidence}. "
                f"schema_version must be {_SCHEMA_VERSION}. decision is "
                "start_model_route or none. route must be empty for none, otherwise one of: "
                f"{', '.join(allowed_routes)}. evidence must be an exact substring of the "
                "user text. Never authorize a user-invoked Skill. Do not include extra JSON keys."
            ),
            user_payload={
                "schema_version": _SCHEMA_VERSION,
                "task_description": task_description,
                "task_type": str(task_type or "general"),
            },
            max_tokens=256,
        )
        if set(payload) != _EXPECTED_KEYS:
            return {"accepted": False, "reason": "semantic_route_schema_invalid"}
        if payload.get("schema_version") != _SCHEMA_VERSION:
            return {"accepted": False, "reason": "semantic_route_schema_version_invalid"}
        if payload.get("decision") != "start_model_route":
            return {"accepted": False, "reason": "semantic_route_none"}
        route_id = str(payload.get("route") or "").strip()
        if route_id not in allowed_routes:
            return {"accepted": False, "reason": "semantic_route_authority_rejected"}
        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return {"accepted": False, "reason": "semantic_route_confidence_invalid"}
        confidence_value = float(confidence)
        if not math.isfinite(confidence_value) or confidence_value < _confidence_threshold():
            return {"accepted": False, "reason": "semantic_route_confidence_low"}
        evidence = " ".join(str(payload.get("evidence") or "").split())
        if not evidence or len(evidence) > 240 or evidence not in task_description:
            return {"accepted": False, "reason": "semantic_route_evidence_invalid"}
        return {
            "accepted": True,
            "reason": "semantic_route_accepted",
            "route": route_id,
            "confidence": confidence_value,
            "evidence": evidence,
            "provider_identity": self._provider.identity,
        }

    def close(self) -> None:
        self._provider.close()


def get_semantic_workflow_route_classifier() -> SemanticWorkflowRouteClassifier:
    global _CLASSIFIER
    with _CLASSIFIER_LOCK:
        if _CLASSIFIER is None:
            _CLASSIFIER = SemanticWorkflowRouteClassifier(
                create_chunk_json_provider(deterministic=True)
            )
        return _CLASSIFIER


def create_chunk_json_provider(*, deterministic: bool = False) -> OpenAICompatibleJSONProvider:
    """Create the shared structured-chunk JSON provider configuration."""

    return OpenAICompatibleJSONProvider(
        api_key=_first_env(
            "PP_MEMORY_CHUNK_ENRICHMENT_API_KEY",
            "PP_INFERENCE_API_KEY",
            "DEEPSEEK_API_KEY",
        ),
        base_url=_first_env(
            "PP_MEMORY_CHUNK_ENRICHMENT_BASE_URL",
            "PP_INFERENCE_BASE_URL",
        ),
        model=_first_env("PP_MEMORY_CHUNK_ENRICHMENT_MODEL", "PP_INFERENCE_MODEL"),
        model_revision=_first_env(
            "PP_MEMORY_CHUNK_ENRICHMENT_MODEL_REVISION",
            "PP_INFERENCE_MODEL_REVISION",
        ),
        path=_first_env("PP_MEMORY_CHUNK_ENRICHMENT_PATH", "PP_INFERENCE_PATH"),
        temperature=(
            0.0 if deterministic else _optional_float("PP_MEMORY_CHUNK_ENRICHMENT_TEMPERATURE")
        ),
        top_p=(None if deterministic else _optional_float("PP_MEMORY_CHUNK_ENRICHMENT_TOP_P")),
        json_mode=True,
        max_output_chars=_optional_int("PP_MEMORY_CHUNK_ENRICHMENT_MAX_OUTPUT_CHARS"),
    )


def _confidence_threshold() -> float:
    try:
        value = float(os.getenv("PP_PASSIVE_SEMANTIC_ROUTING_MIN_CONFIDENCE", "0.75"))
    except (TypeError, ValueError):
        value = 0.75
    return min(1.0, max(0.0, value))


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _optional_float(name: str) -> float | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    return float(value)


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    return int(value)
