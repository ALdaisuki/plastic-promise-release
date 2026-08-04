"""Tests for provider-neutral passive context injection."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import TextContent

_START_PATH = "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
_COMPLETE_PATH = "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
_STORE_PATH = "plastic_promise.mcp.tools.memory.handle_memory_store"
_BEFORE_PATH = "plastic_promise.passive_memory.before_invoke"
_AFTER_PATH = "plastic_promise.passive_memory.after_invoke"
_PRINCIPLE_PATH = "plastic_promise.mcp.tools.principles.handle_principle_activate"


def _text(payload):
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _start_payload(*, principles=None):
    return _text(
        {
            "entity_id": "skill:auto_inject:test:runtime",
            "activated_principles": list(principles or []),
        }
    )


def _before_payload(
    *, principles=None, injection="<untrusted-memory-context>fact</untrusted-memory-context>"
):
    return {
        "event": "before_invoke",
        "status": "injected" if injection else "empty",
        "mode": "on",
        "ephemeral": True,
        "injection": injection,
        "context_pack": {
            "core": [{"id": "mem_core", "content": "relevant fact"}],
            "related": [],
            "divergent": [],
        },
        "principles": list(principles or []),
        "request_scope_id": "scope:test",
        "memory_ids": ["mem_core"],
        "inject_memory_id": None,
        "diagnostics": {"level": "summary"},
        "errors": None,
        "partial": False,
    }


class TestAutoContextInject:
    @pytest.fixture(autouse=True)
    def _enable_passive_routes(self, monkeypatch):
        monkeypatch.setenv("PP_PASSIVE_CONTEXT", "on")
        monkeypatch.setenv("PP_PASSIVE_MEMORY", "on")
        monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")

    @pytest.mark.parametrize("context_mode", ["off", "invalid"])
    def test_before_invoke_disabled_short_circuits_before_tracking(self, monkeypatch, context_mode):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        monkeypatch.setenv("PP_PASSIVE_CONTEXT", context_mode)
        start = AsyncMock()
        before = AsyncMock()
        principle_activate = AsyncMock()
        with (
            patch(_START_PATH, new=start),
            patch(_BEFORE_PATH, new=before),
            patch(_PRINCIPLE_PATH, new=principle_activate),
        ):
            result = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {"event": "before_invoke", "task_description": "disabled"},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "skipped"
        assert payload["reason"] == "passive_context_disabled"
        assert payload["injection"] == ""
        assert payload["request_scope_id"]
        start.assert_not_awaited()
        before.assert_not_awaited()
        principle_activate.assert_not_awaited()

    @pytest.mark.parametrize(
        ("memory_mode", "proposal_setting", "reason"),
        [
            ("off", "on", "passive_memory_disabled"),
            ("invalid", "on", "passive_memory_disabled"),
            ("on", "off", "proposal_gate_closed"),
        ],
    )
    def test_after_invoke_disabled_short_circuits_before_tracking(
        self,
        monkeypatch,
        memory_mode,
        proposal_setting,
        reason,
    ):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        monkeypatch.setenv("PP_PASSIVE_MEMORY", memory_mode)
        monkeypatch.setenv("PP_MEMORY_PROPOSALS", proposal_setting)
        start = AsyncMock()
        after = AsyncMock()
        with (
            patch(_START_PATH, new=start),
            patch(_AFTER_PATH, new=after),
        ):
            result = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {"event": "after_invoke", "task_description": "disabled"},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "skipped"
        assert payload["reason"] == reason
        assert payload["queued"] is False
        assert payload["request_scope_id"]
        assert payload["candidate_count"] is None
        start.assert_not_awaited()
        after.assert_not_awaited()

    def test_invalid_proposal_mode_is_reported_as_configuration_error(self, monkeypatch):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        monkeypatch.setenv("PP_MEMORY_PROPOSALS", "invalid")
        start = AsyncMock()
        after = AsyncMock()
        with (
            patch(_START_PATH, new=start),
            patch(_AFTER_PATH, new=after),
        ):
            result = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {"event": "after_invoke", "task_description": "invalid config"},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "degraded"
        assert payload["reason"] == "invalid_proposal_mode"
        assert payload["proposal_mode"] == "invalid"
        assert payload["partial"] is True
        assert payload["errors"] == ["proposal_mode: unknown_proposal_mode"]
        assert payload["request_scope_id"]
        start.assert_not_awaited()
        after.assert_not_awaited()

    def test_before_invoke_is_read_only_and_ephemeral(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        before = AsyncMock(return_value=_before_payload())
        store = AsyncMock()
        with (
            patch(_START_PATH, new=AsyncMock(return_value=_start_payload())),
            patch(_COMPLETE_PATH, new=AsyncMock(return_value=_text({"status": "done"}))),
            patch(_BEFORE_PATH, new=before),
            patch(_STORE_PATH, new=store),
        ):
            result = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {
                        "event": "before_invoke",
                        "task_description": "Inject governed context",
                        "source": "manual",
                    },
                )
            )

        payload = json.loads(result[0].text)
        assert payload["ephemeral"] is True
        assert payload["inject_memory_id"] is None
        assert payload["memory_ids"] == ["mem_core"]
        assert "untrusted-memory-context" in payload["injection"]
        before.assert_awaited_once()
        store.assert_not_awaited()

    def test_before_invoke_tracks_session_and_uses_tracked_principles(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        principles = [{"id": 2, "name": "全过程可查可透明"}]
        start = AsyncMock(return_value=_start_payload(principles=principles))
        complete = AsyncMock(return_value=_text({"status": "done"}))
        with (
            patch(_START_PATH, new=start),
            patch(_COMPLETE_PATH, new=complete),
            patch(_BEFORE_PATH, new=AsyncMock(return_value=_before_payload(principles=[]))),
        ):
            result = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {
                        "task_description": "修复 JWT 认证 bug",
                        "task_type": "code_generation",
                        "source": "claude_code",
                    },
                )
            )

        payload = json.loads(result[0].text)
        assert payload["skill_name"] == "auto_inject:claude_code"
        assert payload["entity_id"] == "skill:auto_inject:test:runtime"
        assert payload["principles"] == principles
        assert payload["context_pack"]["core"][0]["id"] == "mem_core"
        assert payload["inject_memory_id"] is None
        start.assert_awaited_once()
        assert start.await_args.args[1]["record_memory"] is False
        complete.assert_awaited_once()

    def test_tracking_failure_does_not_block_context_preload(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        with (
            patch(_START_PATH, new=AsyncMock(side_effect=RuntimeError("tracking offline"))),
            patch(_BEFORE_PATH, new=AsyncMock(return_value=_before_payload())),
            patch(
                _PRINCIPLE_PATH,
                new=AsyncMock(return_value=_text({"activated": []})),
            ),
        ):
            result = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {"task_description": "修复 bug", "source": "manual"},
                )
            )

        payload = json.loads(result[0].text)
        assert payload["context_pack"]["core"]
        assert payload["partial"] is True
        assert any("skill_session_start" in error for error in payload["errors"])

    def test_full_task_description_reaches_passive_preload_without_storage(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        task_description = "修复 JWT 认证 bug — token 过期后 refresh 流程异常"
        observed = []

        async def capture_before(_engine, values):
            observed.append(dict(values))
            return _before_payload(injection="")

        store = AsyncMock()
        with (
            patch(_START_PATH, new=AsyncMock(return_value=_start_payload())),
            patch(_COMPLETE_PATH, new=AsyncMock(return_value=_text({"status": "done"}))),
            patch(_BEFORE_PATH, new=capture_before),
            patch(_STORE_PATH, new=store),
        ):
            asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {"task_description": task_description, "source": "manual"},
                )
            )

        assert observed[0]["task_description"] == task_description
        store.assert_not_awaited()

    def test_principle_fallback_when_passive_preload_has_none(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        fallback_principles = [
            {"id": 1, "name": "奥卡姆剃刀"},
            {"id": 2, "name": "全过程可查可透明"},
        ]
        principle_activate = AsyncMock(return_value=_text({"activated": fallback_principles}))
        with (
            patch(_START_PATH, new=AsyncMock(return_value=_start_payload())),
            patch(_COMPLETE_PATH, new=AsyncMock(return_value=_text({"status": "done"}))),
            patch(_BEFORE_PATH, new=AsyncMock(return_value=_before_payload(principles=[]))),
            patch(_PRINCIPLE_PATH, new=principle_activate),
        ):
            result = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {
                        "task_description": "修复 bug",
                        "task_type": "debugging",
                        "source": "manual",
                    },
                )
            )

        payload = json.loads(result[0].text)
        assert payload["principles"] == fallback_principles
        principle_activate.assert_awaited_once()

    def test_after_invoke_routes_to_passive_proposal_audit(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        after_payload = {
            "event": "after_invoke",
            "status": "queued",
            "mode": "on",
            "proposal_mode": "on",
            "queued": True,
            "worker_scheduled": True,
            "candidate_count": 1,
            "candidate_hashes": ["sha256:abc"],
            "outbox_id": "outbox_1",
            "reason": None,
            "request_scope_id": "scope:after",
            "inject_memory_id": None,
        }
        after = AsyncMock(return_value=after_payload)
        start = AsyncMock(return_value=_start_payload())
        store = AsyncMock()
        with (
            patch(_START_PATH, new=start),
            patch(_COMPLETE_PATH, new=AsyncMock(return_value=_text({"status": "done"}))),
            patch(_AFTER_PATH, new=after),
            patch(_STORE_PATH, new=store),
        ):
            result = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {
                        "event": "after_invoke",
                        "task_description": "记住我喜欢 TypeScript",
                        "user_text": "记住我喜欢 TypeScript",
                        "assistant_text": "好的",
                        "source": "manual",
                    },
                )
            )

        payload = json.loads(result[0].text)
        assert payload["queued"] is True
        assert payload["outbox_id"] == "outbox_1"
        assert payload["inject_memory_id"] is None
        after.assert_awaited_once()
        start.assert_awaited_once()
        assert start.await_args.args[1]["record_memory"] is False
        store.assert_not_awaited()

    def test_repeated_preload_does_not_create_self_feedback_memory(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        before = AsyncMock(side_effect=[_before_payload(), _before_payload()])
        store = AsyncMock()
        with (
            patch(_START_PATH, new=AsyncMock(return_value=_start_payload())),
            patch(_COMPLETE_PATH, new=AsyncMock(return_value=_text({"status": "done"}))),
            patch(_BEFORE_PATH, new=before),
            patch(_STORE_PATH, new=store),
        ):
            first = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {"task_description": "修复 JWT 认证 bug", "source": "manual"},
                )
            )
            second = asyncio.run(
                handle_auto_context_inject(
                    object(),
                    {"task_description": "修复 OAuth 认证 bug", "source": "manual"},
                )
            )

        assert json.loads(first[0].text)["inject_memory_id"] is None
        assert json.loads(second[0].text)["inject_memory_id"] is None
        assert before.await_count == 2
        store.assert_not_awaited()
