"""MGP-compatible shadow bridge.

P1 keeps Plastic Promise as the memory truth source. This bridge validates and
audits MGP-like memory governance envelopes, but it does not mutate memory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from plastic_promise.core.event_protocol import record_runtime_event

VALID_MGP_MODES = {"off", "shadow", "inject"}

MGP_OPERATION_MAP = {
    "write": "memory_store",
    "search": "memory_recall/context_supply",
    "get": "memory_list",
    "update": "memory_update",
    "expire": "memory_forget",
    "delete": "memory_forget",
    "revoke": "memory_correct",
    "purge": "memory_gc",
    "list": "memory_list",
}


def _env_mode() -> str:
    mode = os.environ.get("PP_MGP_BRIDGE_MODE", "off").strip().lower()
    return mode if mode in VALID_MGP_MODES else "off"


def _engine_conn(engine: Any):
    sqlite = getattr(engine, "_sqlite", None)
    return getattr(sqlite, "_conn", None)


def _scope(policy_context: dict[str, Any]) -> dict[str, str]:
    stage_session_id = str(
        policy_context.get("stage_session_id") or "session:mgp_shadow_bridge:default"
    )
    flow_line_id = str(policy_context.get("flow_line_id") or "default")
    request_id = str(policy_context.get("request_id") or "shadow")
    return {
        "stage_session_id": stage_session_id,
        "flow_line_id": flow_line_id,
        "request_id": request_id,
        "request_scope_id": str(
            policy_context.get("request_scope_id")
            or f"{stage_session_id}::flow:{flow_line_id}::req:{request_id}"
        ),
    }


@dataclass
class MgpShadowBridge:
    mode: str = ""

    def __post_init__(self) -> None:
        if not self.mode:
            self.mode = _env_mode()
        self.mode = self._validate_mode(self.mode)

    @staticmethod
    def _validate_mode(mode: str) -> str:
        mode = str(mode or "off").strip().lower()
        if mode not in VALID_MGP_MODES:
            raise ValueError(f"Unknown MGP bridge mode: {mode}")
        return mode

    def set_mode(self, mode: str) -> dict[str, Any]:
        self.mode = self._validate_mode(mode)
        os.environ["PP_MGP_BRIDGE_MODE"] = self.mode
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "valid_modes": sorted(VALID_MGP_MODES),
            "operation_map": dict(MGP_OPERATION_MAP),
            "truth_source": "plastic_promise_sqlite",
            "audit_only": self.mode in {"shadow", "inject"},
        }

    def map_operation(self, operation: str) -> dict[str, str]:
        operation = str(operation or "").strip().lower()
        return {
            "operation": operation,
            "plastic_operation": MGP_OPERATION_MAP.get(operation, "unsupported"),
        }

    def evaluate(self, envelope: dict[str, Any], *, engine: Any = None) -> dict[str, Any]:
        operation = str(envelope.get("operation") or "").strip().lower()
        policy_context = dict(envelope.get("policy_context") or {})
        mapping = self.map_operation(operation)
        scope = _scope(policy_context)
        result = {
            "mode": self.mode,
            "operation": operation,
            "plastic_operation": mapping["plastic_operation"],
            "subject": str(envelope.get("subject") or policy_context.get("subject") or ""),
            "policy_context": policy_context,
            "audit_only": self.mode in {"shadow", "inject"},
            "inject_reserved": self.mode == "inject",
            "would_mutate_memory": False,
            "event_id": "",
            **scope,
        }

        if self.mode == "off":
            result["audit_only"] = False
            return result

        conn = _engine_conn(engine)
        if conn is not None:
            event_id = record_runtime_event(
                conn,
                event_kind="agent",
                event_name="mgp_shadow_bridge",
                status="completed",
                request_scope_id=scope["request_scope_id"],
                stage_session_id=scope["stage_session_id"],
                flow_line_id=scope["flow_line_id"],
                project_id=str(policy_context.get("project_id") or ""),
                actor=str(policy_context.get("actor") or "mgp_shadow_bridge"),
                trust_tier=str(policy_context.get("trust_tier") or ""),
                defense_decision=str(policy_context.get("defense_decision") or "allow"),
                audit_trace={
                    "operation": operation,
                    "plastic_operation": mapping["plastic_operation"],
                    "mode": self.mode,
                },
                metadata={
                    "operation": operation,
                    "subject": result["subject"],
                    "audit_only": True,
                    "inject_reserved": result["inject_reserved"],
                },
            )
            result["event_id"] = event_id
        return result
