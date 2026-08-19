from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from plastic_promise.collaboration.awareness import ProjectWorkingSet
from plastic_promise.collaboration.context_supply_runtime import (
    CollaborationContextReadRequest,
    CollaborationContextReadResult,
    open_server_collaboration_context_runtime,
    render_collaboration_prompt,
)
from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationEvent,
    CoordinationSession,
    ProjectScope,
)
from plastic_promise.collaboration.event_log import CollaborationEventLog
from plastic_promise.core.context_engine import ContextItem, ContextPack
from plastic_promise.mcp.tools.context import handle_context_supply

SERVER_NOW = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
SERVER_NOW_TEXT = "2026-08-11T01:00:00.000000Z"
EARLIER = "2026-08-11T00:00:00.000000Z"
HEARTBEAT = "2026-08-11T00:59:00.000000Z"
LATER = "2026-08-11T03:00:00.000000Z"


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass
class RuntimeHarness:
    connection: sqlite3.Connection
    runtime: object
    project: ProjectScope
    session: AgentSession
    sources: dict[str, ProjectWorkingSet]

    def close(self) -> None:
        self.connection.close()


def _runtime_harness() -> RuntimeHarness:
    project = ProjectScope("project:plastic-promise")
    actor = AgentIdentity("agent:builder", "implementer")
    session = AgentSession(
        session_id="agent-session:builder",
        identity=actor,
        project=project,
        coordination_session_id="coord:pr4",
        state="active",
        started_at=EARLIER,
        last_heartbeat_at=HEARTBEAT,
        expires_at=LATER,
    )
    coordination = CoordinationSession(
        session_id="coord:pr4",
        project=project,
        objective="Compose authenticated project collaboration context",
        created_at=EARLIER,
        expires_at=LATER,
    )
    progress = CollaborationEvent(
        event_id="event:progress",
        project=project,
        coordination_session_id="coord:pr4",
        actor=actor,
        event_type="work.progressed",
        summary="PR4 runtime is being verified",
        created_at=HEARTBEAT,
        subject_refs=("module:collaboration",),
        payload={"adapter_state": "must never be rendered"},
    )
    source = ProjectWorkingSet(
        coordination=coordination,
        goal_summary="Compose authenticated project collaboration context",
        plan_revision="plan:pr4",
        observed_at=HEARTBEAT,
        agent_sessions=(session,),
    )
    sources = {"working_set": source}
    clock = MutableClock(SERVER_NOW)
    connection = sqlite3.connect(":memory:")
    event_log = CollaborationEventLog(connection=connection, clock=clock)
    event_log.append(progress)
    runtime = open_server_collaboration_context_runtime(
        bound_session=session,
        event_log=event_log,
        working_set_provider=lambda: sources["working_set"],
        clock=clock,
    )
    return RuntimeHarness(connection, runtime, project, session, sources)


def _memory(project_id: str = "project:plastic-promise") -> dict[str, object]:
    return {
        "project_id": project_id,
        "core": [{"id": "memory:core", "content": "canonical decision"}],
        "related": [],
        "divergent": [],
    }


def _request(
    project: ProjectScope,
    *,
    after_sequence: int | None = None,
    request_scope_id: str = "request:pr4",
) -> CollaborationContextReadRequest:
    return CollaborationContextReadRequest(
        project=project,
        request_scope_id=request_scope_id,
        response_mode="compact",
        after_sequence=after_sequence,
        limit=20,
    )


def test_runtime_restamps_working_set_with_server_utc() -> None:
    harness = _runtime_harness()
    try:
        result = asyncio.run(
            harness.runtime.compose(
                memory_context=_memory(),
                request=_request(harness.project),
            )
        )
    finally:
        harness.close()

    assert result.state == "available"
    assert result.projection is not None
    assert result.projection["observed_at"] == SERVER_NOW_TEXT
    assert result.projection["source_tuple"]["generated_at_utc"] == SERVER_NOW_TEXT
    assert result.projection["canonical_memory_effect"] == "none"
    assert result.projection["items"][0]["kind"] == "progress"
    assert "adapter_state" not in result.prompt_section
    assert "must never be rendered" not in result.prompt_section


def test_runtime_rejects_source_time_ahead_of_the_server_without_advancing_cursor() -> None:
    harness = _runtime_harness()
    try:
        future_session = AgentSession(
            session_id="agent-session:peer",
            identity=AgentIdentity("agent:peer", "reviewer"),
            project=harness.project,
            coordination_session_id="coord:pr4",
            state="active",
            started_at=EARLIER,
            last_heartbeat_at="2026-08-11T01:30:00.000000Z",
            expires_at=LATER,
        )
        harness.sources["working_set"] = replace(
            harness.sources["working_set"],
            observed_at="2026-08-11T02:00:00.000000Z",
            agent_sessions=(harness.session, future_session),
        )
        rejected = asyncio.run(
            harness.runtime.compose(
                memory_context=_memory(),
                request=_request(harness.project),
            )
        )
        harness.sources["working_set"] = replace(
            harness.sources["working_set"],
            observed_at=HEARTBEAT,
            agent_sessions=(harness.session,),
        )
        retried = asyncio.run(
            harness.runtime.compose(
                memory_context=_memory(),
                request=_request(harness.project, after_sequence=0),
            )
        )
    finally:
        harness.close()

    assert rejected.state == "rejected"
    assert rejected.reason == "collaboration_context_source_time_invalid"
    assert retried.state == "available"
    assert retried.projection["cursor"]["after"]["sequence"] == 0


def test_runtime_cursor_is_server_owned_and_replay_is_exact() -> None:
    harness = _runtime_harness()
    try:
        first = asyncio.run(
            harness.runtime.compose(
                memory_context=_memory(),
                request=_request(harness.project),
            )
        )
        replay = asyncio.run(
            harness.runtime.compose(
                memory_context=_memory(),
                request=_request(harness.project, after_sequence=0),
            )
        )
        gap = asyncio.run(
            harness.runtime.compose(
                memory_context=_memory(),
                request=_request(harness.project, after_sequence=3),
            )
        )
        ambiguous = asyncio.run(
            harness.runtime.compose(
                memory_context=_memory(),
                request=_request(
                    harness.project,
                    after_sequence=0,
                    request_scope_id="request:other",
                ),
            )
        )
    finally:
        harness.close()

    assert first.state == "available"
    assert replay.replayed is True
    assert replay.projection == first.projection
    assert gap.reason == "collaboration_cursor_gap"
    assert ambiguous.reason == "collaboration_cursor_replay_ambiguous"


def test_runtime_rejects_naive_server_clock() -> None:
    harness = _runtime_harness()
    try:
        harness.runtime._clock = lambda: datetime(2026, 8, 11, 1, 0)  # noqa: SLF001
        result = asyncio.run(
            harness.runtime.compose(
                memory_context=_memory(),
                request=_request(harness.project),
            )
        )
    finally:
        harness.close()

    assert result.state == "rejected"
    assert result.reason == "collaboration_context_clock_invalid"


def test_prompt_renderer_treats_corrupt_cursor_values_as_zero() -> None:
    rendered = render_collaboration_prompt(
        {
            "cursor": {
                "after": {"sequence": "not-an-integer"},
                "next": {"sequence": -4},
                "has_more": False,
            },
            "items": [],
        }
    )

    assert "- cursor: 0 -> 0" in rendered


class _HandlerEngine:
    def __init__(self) -> None:
        self._memories = {
            "memory:core": {
                "project_id": "project:plastic-promise",
                "visibility": "project",
                "source_class": "experience",
            }
        }

    def retrieval_embedding_probe(self, _text: str, *, project_id: str) -> list[float]:
        assert project_id == "project:plastic-promise"
        return [0.0]

    def supply(self, *_args: object, **_kwargs: object) -> ContextPack:
        return ContextPack(
            core=[
                ContextItem(
                    id="memory:core",
                    content="canonical decision",
                    relevance=0.9,
                    source="test",
                    layer="core",
                )
            ]
        )


class _HandlerRuntime:
    async def compose(self, **_kwargs: object) -> CollaborationContextReadResult:
        return CollaborationContextReadResult(
            state="available",
            reason="collaboration_context_available",
            projection={
                "authority": "non-authoritative-context-projection",
                "canonical_memory_effect": "none",
                "items": [],
            },
            prompt_section="## [COLLABORATION]\n- canonical_memory_effect: none",
        )


class _SlowHandlerRuntime:
    async def compose(self, **_kwargs: object) -> CollaborationContextReadResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_context_supply_keeps_memory_plane_unchanged_when_collaboration_is_added(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PP_COLLABORATION_AWARENESS", "inject")
    args = {
        "task_description": "Review collaboration time authority",
        "project_id": "project:plastic-promise",
        "request_id": "request:handler",
    }
    baseline = asyncio.run(handle_context_supply(_HandlerEngine(), args))[0].text
    injected = asyncio.run(
        handle_context_supply(
            _HandlerEngine(),
            args,
            _collaboration_runtime=_HandlerRuntime(),
        )
    )[0].text
    compact = json.loads(
        asyncio.run(
            handle_context_supply(
                _HandlerEngine(),
                {**args, "response_mode": "compact"},
                _collaboration_runtime=_HandlerRuntime(),
            )
        )[0].text
    )

    assert injected.startswith(baseline)
    assert injected[len(baseline) :] == (
        "\n\n## [COLLABORATION]\n- canonical_memory_effect: none"
    )
    assert compact["core"][0]["content"] == "canonical decision"
    assert compact["collaboration"]["canonical_memory_effect"] == "none"
    assert compact["degraded"] is False


def test_context_supply_collaboration_timeout_fails_open_without_degrading_memory(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PP_COLLABORATION_CONTEXT_TIMEOUT_SEC", "0.01")
    payload = json.loads(
        asyncio.run(
            handle_context_supply(
                _HandlerEngine(),
                {
                    "task_description": "Keep canonical memory available",
                    "project_id": "project:plastic-promise",
                    "request_id": "request:timeout",
                    "response_mode": "compact",
                },
                _collaboration_runtime=_SlowHandlerRuntime(),
            )
        )[0].text
    )

    assert payload["core"][0]["content"] == "canonical decision"
    assert payload["degraded"] is False
    assert payload["collaboration_status"] == {
        "mode": "shadow",
        "state": "degraded",
        "reason": "collaboration_context_timeout",
        "retryable": True,
        "replayed": False,
        "canonical_memory_effect": "none",
    }
    assert "collaboration" not in payload


def test_context_supply_shadow_reads_without_injecting_and_inject_requires_gate(
    monkeypatch,
) -> None:
    args = {
        "task_description": "Observe authenticated collaboration",
        "project_id": "project:plastic-promise",
        "request_id": "request:shadow-gate",
    }
    baseline = asyncio.run(handle_context_supply(_HandlerEngine(), args))[0].text

    monkeypatch.setenv("PP_COLLABORATION_AWARENESS", "shadow")
    shadow = asyncio.run(
        handle_context_supply(
            _HandlerEngine(),
            args,
            _collaboration_runtime=_HandlerRuntime(),
        )
    )[0].text
    shadow_compact = json.loads(
        asyncio.run(
            handle_context_supply(
                _HandlerEngine(),
                {**args, "response_mode": "compact"},
                _collaboration_runtime=_HandlerRuntime(),
            )
        )[0].text
    )

    assert shadow == baseline
    assert shadow_compact["collaboration_status"]["mode"] == "shadow"
    assert "collaboration" not in shadow_compact

    monkeypatch.setenv("PP_COLLABORATION_AWARENESS", "inject")
    injected = asyncio.run(
        handle_context_supply(
            _HandlerEngine(),
            args,
            _collaboration_runtime=_HandlerRuntime(),
        )
    )[0].text
    assert injected.endswith("## [COLLABORATION]\n- canonical_memory_effect: none")
