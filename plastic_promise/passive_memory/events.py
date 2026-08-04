"""Provider-neutral passive memory event schema."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

VALID_PASSIVE_EVENTS = frozenset({"before_invoke", "after_invoke"})


def _text(value: object) -> str:
    return str(value or "").strip()


def _raw_field(values: dict[str, Any], *names: str) -> str:
    for name in names:
        if name in values and values[name] is not None:
            return str(values[name])
    return ""


@dataclass(frozen=True)
class PassiveMemoryEvent:
    event: str
    task_description: str = ""
    task_type: str = "general"
    source: str = "mcp"
    user_text: str = ""
    assistant_text: str = ""
    call_id: str = ""
    parent_call_id: str = ""
    request_id: str = ""
    stage_session_id: str = ""
    flow_line_id: str = ""
    project_id: str = ""
    project_policy: str = "balanced"
    visibility: str = "project"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_event = _text(self.event).casefold()
        if normalized_event not in VALID_PASSIVE_EVENTS:
            raise ValueError(f"unknown passive memory event: {self.event}")
        object.__setattr__(self, "event", normalized_event)
        object.__setattr__(self, "task_type", _text(self.task_type) or "general")
        object.__setattr__(self, "source", _text(self.source) or "mcp")
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> PassiveMemoryEvent:
        values = dict(args or {})
        return cls(
            event=values.get("event") or "before_invoke",
            task_description=_text(values.get("task_description")),
            task_type=_text(values.get("task_type")) or "general",
            source=_text(values.get("source")) or "mcp",
            user_text=_raw_field(values, "user_text", "user_message", "prompt"),
            assistant_text=_text(
                values.get("assistant_text")
                or values.get("assistant_message")
                or values.get("response")
                or values.get("result_text")
            ),
            call_id=_text(values.get("call_id")),
            parent_call_id=_text(values.get("parent_call_id") or values.get("parent_call")),
            request_id=_text(values.get("request_id")),
            stage_session_id=_text(values.get("stage_session_id") or values.get("stage_id")),
            flow_line_id=_text(values.get("flow_line_id") or values.get("flow_id")),
            project_id=_text(values.get("project_id")),
            project_policy=_text(values.get("project_policy")) or "balanced",
            visibility=_text(values.get("visibility")) or "project",
            metadata=dict(values.get("metadata") or {}),
        )

    def to_args(self) -> dict[str, Any]:
        values = {
            "task_description": self.task_description,
            "task_type": self.task_type,
            "source": self.source,
            "call_id": self.call_id,
            "parent_call_id": self.parent_call_id,
            "request_id": self.request_id,
            "stage_session_id": self.stage_session_id,
            "flow_line_id": self.flow_line_id,
            "project_id": self.project_id,
            "project_policy": self.project_policy,
            "visibility": self.visibility,
        }
        return {key: value for key, value in values.items() if value not in (None, "")}

    def origin_turn_hash(self, content: str) -> str:
        stable = "\x1f".join(
            (
                self.project_id,
                self.stage_session_id,
                self.flow_line_id,
                self.request_id or self.call_id,
                content,
            )
        )
        return "sha256:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def capture_dedupe_key(self, content_hash: str = "") -> str:
        """Return a stable capture key without generated request-scope identifiers."""
        explicit_identity = self.request_id or self.call_id
        turn_identity = explicit_identity or self.origin_turn_hash(
            self.user_text or self.task_description
        )
        stable = "\x1f".join(
            (
                self.event,
                self.project_id,
                self.stage_session_id,
                self.flow_line_id,
                turn_identity,
                content_hash,
            )
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def idempotency_key(self, content_hash: str = "") -> str:
        stable = "\x1f".join(
            (
                self.event,
                self.project_id,
                self.stage_session_id,
                self.flow_line_id,
                self.request_id,
                self.call_id,
                content_hash,
            )
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()
