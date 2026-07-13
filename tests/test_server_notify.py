from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
from plastic_promise.core.synthesis import synthesis_content_hash
from plastic_promise.core.synthesis_retrieval import _source_is_available
from plastic_promise.mcp.server import (
    _NOTIFICATION_RUNTIME_TOOL_BY_EVENT,
    _handle_notification_event,
    _mutation_runtime_context,
    _persist_audit_report_notification,
    _persist_llm_classification_notification,
    _persist_then_publish_notification,
)


class _RecordingQueue:
    def __init__(self, order=None):
        self.items = []
        self.order = order

    async def put(self, item):
        if self.order is not None:
            self.order.append("queue")
        self.items.append(item)


def _runtime(**overrides):
    runtime = {
        "actor": "maintenance_daemon",
        "call_id": "call:audit-notify",
        "project_id": "project:test",
        "trust_score": 0.95,
        "defense_decision": "allow",
    }
    runtime.update(overrides)
    return runtime


def test_notify_issue_change_uses_active_http_queue(monkeypatch):
    from plastic_promise.mcp import server as mcp_server

    queue = MagicMock()
    event = {"type": "memory_stored", "memory_id": "memory-1"}
    monkeypatch.setattr(mcp_server, "_notify_queue", queue)

    mcp_server.notify_issue_change(event)

    queue.put_nowait.assert_called_once_with(event)


def test_mutation_runtime_context_keeps_process_owned_project_scope(
    monkeypatch,
):
    monkeypatch.setenv("PLASTIC_PROJECT_ID", "project:default")

    runtime = _mutation_runtime_context(
        "memory_update",
        {"project_id": "project:tenant", "project_policy": "strict"},
    )

    assert runtime["project_id"] == "project:default"
    assert runtime["project_policy"] == "balanced"


def test_audit_report_notification_denial_is_zero_write():
    engine = MagicMock()

    result = _persist_audit_report_notification(
        engine,
        {"type": "audit_report", "content": "denied"},
        _runtime(trust_score=0.20, defense_decision="deny"),
    )

    assert result == {
        "committed": False,
        "partial": False,
        "reason": "audit_notification_runtime_authorization_denied",
        "tombstoned_ids": [],
        "memory_id": "",
    }
    engine.iter_memories.assert_not_called()
    engine.mutate_ordinary_source.assert_not_called()
    engine.create_ordinary_if_absent.assert_not_called()


def test_audit_report_notification_tombstones_only_runtime_project():
    engine = MagicMock()
    engine.iter_memories.return_value = [
        {"id": "audit-current"},
        {"id": "audit-other"},
        {"id": "ordinary"},
    ]
    canonical = {
        "audit-current": {
            "id": "audit-current",
            "content": "current audit",
            "tags": ["audit"],
            "metadata_json": {},
            "project_id": "project:test",
        },
        "audit-other": {
            "id": "audit-other",
            "content": "other audit",
            "tags": ["audit"],
            "metadata_json": {},
            "project_id": "project:other",
        },
        "ordinary": {
            "id": "ordinary",
            "tags": [],
            "project_id": "project:test",
        },
    }
    engine.get_memory_dict_for_review.side_effect = canonical.get
    engine.mutate_ordinary_source.return_value = SimpleNamespace(
        stale_synthesis_ids=("synthesis-stale",)
    )
    engine.create_ordinary_if_absent.return_value = "audit-next"

    result = _persist_audit_report_notification(
        engine,
        {"type": "audit_report", "content": "AUDIT score=0.91", "overall": 0.91},
        _runtime(),
    )

    assert result == {
        "committed": True,
        "partial": False,
        "reason": "",
        "tombstoned_ids": ["audit-current"],
        "stale_dependents": ["synthesis-stale"],
        "memory_id": "audit-next",
    }
    engine.mutate_ordinary_source.assert_called_once_with(
        "audit-current",
        operation="forgotten",
        reason="http_notify:audit_replaced",
        actor="maintenance_daemon",
        call_id="call:audit-notify:audit-replaced:0",
        expected_project_id="project:test",
        expected_content_hash=synthesis_content_hash("current audit"),
        expected_source_snapshot={"tags": ["audit"]},
        require_source_available=True,
    )
    stored = engine.create_ordinary_if_absent.call_args.args[0]
    assert stored["project_id"] == "project:test"
    assert stored["created_by_call_id"] == "call:audit-notify"
    assert stored["tags"] == ["audit", "domain:governing", "score:0.91"]


def test_audit_report_notification_reports_partial_tombstones():
    engine = MagicMock()
    engine.iter_memories.return_value = [
        {"id": "audit-first", "tags": ["audit"], "project_id": "project:test"},
        {"id": "audit-second", "tags": ["audit"], "project_id": "project:test"},
    ]
    canonical = {
        memory_id: {
            "id": memory_id,
            "content": f"content for {memory_id}",
            "tags": ["audit"],
            "metadata_json": {},
            "project_id": "project:test",
        }
        for memory_id in ("audit-first", "audit-second")
    }
    engine.get_memory_dict_for_review.side_effect = canonical.get
    engine.mutate_ordinary_source.side_effect = [
        SimpleNamespace(stale_synthesis_ids=("synthesis-first",)),
        RuntimeError("injected second mutation failure"),
    ]

    result = _persist_audit_report_notification(
        engine,
        {"type": "audit_report", "content": "partial"},
        _runtime(),
    )

    assert result == {
        "committed": False,
        "partial": True,
        "reason": "audit_replacement_failed",
        "tombstoned_ids": ["audit-first"],
        "stale_dependents": ["synthesis-first"],
        "memory_id": "",
    }
    engine.create_ordinary_if_absent.assert_not_called()


def test_consecutive_real_audit_notifications_replace_indexable_source(tmp_path):
    storage = _SQLiteStorage(str(tmp_path / "audit-notify.db"))
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = {}
    try:
        first = _persist_audit_report_notification(
            engine,
            {"type": "audit_report", "content": "first audit", "overall": 0.81},
            _runtime(call_id="call:first-audit"),
        )
        assert first["committed"] is True
        first_id = first["memory_id"]
        first_row = storage.get(first_id)
        assert first_row["embedding_hash"]
        assert _source_is_available(first_row) is True

        second = _persist_audit_report_notification(
            engine,
            {"type": "audit_report", "content": "second audit", "overall": 0.92},
            _runtime(call_id="call:second-audit"),
        )

        assert second["committed"] is True
        assert second["tombstoned_ids"] == [first_id]
        assert _source_is_available(storage.get(first_id)) is False
        second_row = storage.get(second["memory_id"])
        assert second_row["embedding_hash"]
        assert _source_is_available(second_row) is True
    finally:
        storage._conn.close()


def test_new_audit_notification_migrates_legacy_unindexed_audit(tmp_path):
    storage = _SQLiteStorage(str(tmp_path / "legacy-audit-notify.db"))
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = {}
    try:
        legacy_id = engine.register_memory(
            {
                "id": "legacy-audit",
                "content": "legacy audit without index material",
                "memory_type": "reflection",
                "tags": ["audit"],
                "metadata_json": {},
                "source": "maintenance_daemon",
                "project_id": "project:test",
                "visibility": "project",
                "source_class": "reflection",
            }
        )
        assert storage.get(legacy_id)["embedding_hash"] == ""

        result = _persist_audit_report_notification(
            engine,
            {"type": "audit_report", "content": "post-upgrade audit", "overall": 0.9},
            _runtime(call_id="call:post-upgrade-audit"),
        )

        assert result["committed"] is True
        assert result["tombstoned_ids"] == [legacy_id]
        legacy = storage.get(legacy_id)
        assert legacy["embedding_hash"]
        assert _source_is_available(legacy) is False
        assert _source_is_available(storage.get(result["memory_id"])) is True
    finally:
        storage._conn.close()


def test_audit_replacement_identity_race_fails_before_followup_write():
    engine = MagicMock()
    engine.iter_memories.return_value = [{"id": "audit-current"}]
    engine.get_memory_dict_for_review.return_value = {
        "id": "audit-current",
        "content": "observed audit",
        "tags": ["audit"],
        "metadata_json": {},
        "project_id": "project:test",
    }
    engine.mutate_ordinary_source.side_effect = RuntimeError(
        "ordinary_source_precondition_mismatch"
    )

    result = _persist_audit_report_notification(
        engine,
        {"type": "audit_report", "content": "raced"},
        _runtime(),
    )

    assert result == {
        "committed": False,
        "partial": False,
        "reason": "audit_replacement_failed",
        "tombstoned_ids": [],
        "stale_dependents": [],
        "memory_id": "",
    }
    engine.mutate_ordinary_source.assert_called_once_with(
        "audit-current",
        operation="forgotten",
        reason="http_notify:audit_replaced",
        actor="maintenance_daemon",
        call_id="call:audit-notify:audit-replaced:0",
        expected_project_id="project:test",
        expected_content_hash=synthesis_content_hash("observed audit"),
        expected_source_snapshot={"tags": ["audit"]},
        require_source_available=True,
    )
    engine.create_ordinary_if_absent.assert_not_called()


async def test_denied_audit_notification_is_not_published():
    engine = MagicMock()
    queue = _RecordingQueue()
    event = {"type": "audit_report", "content": "denied"}

    result = await _persist_then_publish_notification(
        queue,
        event,
        engine=engine,
        runtime_authority=_runtime(trust_score=0.20, defense_decision="deny"),
    )

    assert result["reason"] == "audit_notification_runtime_authorization_denied"
    assert queue.items == []
    engine.create_ordinary_if_absent.assert_not_called()


async def test_fresh_install_default_runtime_can_commit_audit_rollover(tmp_path, monkeypatch):
    from plastic_promise.defense.soul_enforcer import TrustManager
    from plastic_promise.defense.trust_store import TrustStore
    from plastic_promise.mcp.tools import audit_defense

    monkeypatch.delenv("PP_MCP_RUNTIME_ACTOR", raising=False)
    monkeypatch.setenv("PLASTIC_PROJECT_ID", "project:test")
    trust_store = TrustStore(str(tmp_path / "fresh-trust.db"))
    try:
        monkeypatch.setattr(
            audit_defense,
            "_trust_manager",
            TrustManager(trust_store=trust_store),
        )
        runtime = _mutation_runtime_context("audit_rollover")
        engine = MagicMock()
        engine.iter_memories.return_value = []
        engine.create_ordinary_if_absent.return_value = "audit-fresh"
        queue = _RecordingQueue()
        event = {"type": "audit_report", "content": "fresh audit", "overall": 0.8}

        response = await _handle_notification_event(
            queue,
            event,
            engine=engine,
            runtime_authority=runtime,
        )
    finally:
        trust_store._conn.close()

    assert runtime["actor"] == "mcp"
    assert runtime["trust_score"] == 0.60
    assert runtime["defense_decision"] == "allow"
    assert _NOTIFICATION_RUNTIME_TOOL_BY_EVENT == {
        "audit_report": "audit_rollover",
        "llm_classified": "memory_update",
    }
    assert response["ok"] is True
    assert response["audit_persistence"]["memory_id"] == "audit-fresh"
    assert queue.items == [event]


async def test_uncommitted_audit_persistence_failure_is_not_published():
    engine = MagicMock()
    engine.iter_memories.return_value = []
    engine.create_ordinary_if_absent.return_value = ""
    queue = _RecordingQueue()
    event = {"type": "audit_report", "content": "store failed"}

    result = await _persist_then_publish_notification(
        queue,
        event,
        engine=engine,
        runtime_authority=_runtime(),
    )

    assert result["reason"] == "audit_report_store_failed"
    assert result["partial"] is False
    assert queue.items == []


async def test_committed_audit_notification_is_published_after_persistence():
    order = []
    engine = MagicMock()
    engine.iter_memories.return_value = []
    engine.create_ordinary_if_absent.side_effect = lambda _memory: (
        order.append("persist") or "audit-next"
    )
    queue = _RecordingQueue(order)
    event = {"type": "audit_report", "content": "committed", "overall": 0.9}

    result = await _persist_then_publish_notification(
        queue,
        event,
        engine=engine,
        runtime_authority=_runtime(),
    )

    assert result["committed"] is True
    assert order == ["persist", "queue"]
    assert queue.items == [event]


async def test_partially_committed_audit_notification_uses_explicit_envelope():
    engine = MagicMock()
    engine.iter_memories.return_value = [
        {"id": "audit-current", "tags": ["audit"], "project_id": "project:test"}
    ]
    engine.get_memory_dict_for_review.return_value = {
        "id": "audit-current",
        "content": "partial audit",
        "tags": ["audit"],
        "metadata_json": {},
        "project_id": "project:test",
    }
    engine.mutate_ordinary_source.return_value = SimpleNamespace(stale_synthesis_ids=())
    engine.create_ordinary_if_absent.return_value = ""
    queue = _RecordingQueue()
    event = {"type": "audit_report", "content": "partial"}

    result = await _persist_then_publish_notification(
        queue,
        event,
        engine=engine,
        runtime_authority=_runtime(),
    )

    assert result["committed"] is False
    assert result["partial"] is True
    assert queue.items == [
        {
            "type": "audit_report_persistence",
            "status": "partial",
            "event": event,
            "audit_persistence": result,
        }
    ]
    assert queue.items[0] is not event


def _classification_record(*, project_id="project:test"):
    return {
        "id": "memory-1",
        "content": "classified content",
        "project_id": project_id,
        "tags": ["keep", "llm_pending:true", "cat:other"],
        "category": "other",
    }


def _classification_event(**overrides):
    event = {
        "type": "llm_classified",
        "memory_id": "memory-1",
        "new_category": "decision",
        "expected_category": "other",
        "expected_project_id": "project:test",
        "expected_content_hash": synthesis_content_hash("classified content"),
        "expected_tags": ["keep", "llm_pending:true", "cat:other"],
    }
    event.update(overrides)
    return event


def test_llm_classification_denial_is_zero_write():
    engine = MagicMock()

    result = _persist_llm_classification_notification(
        engine,
        _classification_event(),
        _runtime(trust_score=0.20, defense_decision="deny"),
    )

    assert result["reason"] == "llm_classification_runtime_authorization_denied"
    assert result["committed"] is False
    engine.get_memory_dict_for_review.assert_not_called()
    engine.patch_ordinary_memory.assert_not_called()


def test_llm_classification_nonfinite_trust_is_zero_write():
    engine = MagicMock()

    result = _persist_llm_classification_notification(
        engine,
        _classification_event(),
        _runtime(trust_score=float("nan"), defense_decision="allow"),
    )

    assert result["reason"] == "llm_classification_runtime_authorization_denied"
    assert result["committed"] is False
    engine.get_memory_dict_for_review.assert_not_called()
    engine.patch_ordinary_memory.assert_not_called()


def test_llm_classification_canonical_missing_is_zero_write():
    engine = MagicMock()
    engine.get_memory_dict_for_review.return_value = None

    result = _persist_llm_classification_notification(
        engine,
        _classification_event(memory_id="missing"),
        _runtime(),
    )

    assert result["reason"] == "llm_classification_canonical_source_missing"
    assert result["committed"] is False
    engine.patch_ordinary_memory.assert_not_called()


def test_llm_classification_cross_project_is_zero_write():
    engine = MagicMock()
    engine.get_memory_dict_for_review.return_value = _classification_record(
        project_id="project:other"
    )

    result = _persist_llm_classification_notification(
        engine,
        _classification_event(),
        _runtime(),
    )

    assert result["reason"] == "llm_classification_project_mismatch"
    assert result["committed"] is False
    engine.patch_ordinary_memory.assert_not_called()


@pytest.mark.parametrize("new_category", [None, "", "unknown"])
def test_llm_classification_invalid_category_is_zero_write(new_category):
    engine = MagicMock()

    result = _persist_llm_classification_notification(
        engine,
        _classification_event(new_category=new_category),
        _runtime(),
    )

    assert result["reason"] == "llm_classification_category_invalid"
    assert result["committed"] is False
    engine.get_memory_dict_for_review.assert_not_called()
    engine.patch_ordinary_memory.assert_not_called()


async def test_llm_classification_endpoint_patches_once_before_publication():
    order = []
    engine = MagicMock()
    engine.get_memory_dict_for_review.return_value = _classification_record()
    engine.patch_ordinary_memory.side_effect = lambda *_args, **_kwargs: (
        order.append("patch") or _classification_record()
    )
    queue = _RecordingQueue(order)
    event = _classification_event()

    response = await _handle_notification_event(
        queue,
        event,
        engine=engine,
        runtime_authority=_runtime(),
    )

    assert response["ok"] is True
    assert response["classification_persistence"]["committed"] is True
    assert order == ["patch", "queue"]
    assert queue.items == [event]
    engine.patch_ordinary_memory.assert_called_once_with(
        "memory-1",
        replacements={
            "tags": ["keep", "llm_classified:true", "cat:decision"],
            "category": "decision",
        },
        expected_project_id="project:test",
        expected_content_hash=synthesis_content_hash("classified content"),
        expected_tags=["keep", "llm_pending:true", "cat:other"],
        expected_category="other",
        require_source_available=True,
    )
    engine.update_memory_fields.assert_not_called()


async def test_llm_classification_same_category_still_clears_pending_once():
    order = []
    engine = MagicMock()
    canonical = _classification_record()
    canonical["category"] = "fact"
    canonical["tags"] = ["keep", "llm_pending:true", "cat:fact"]
    engine.get_memory_dict_for_review.return_value = canonical
    engine.patch_ordinary_memory.side_effect = lambda *_args, **_kwargs: (
        order.append("patch") or canonical
    )
    queue = _RecordingQueue(order)
    event = _classification_event(
        new_category="fact",
        expected_category="fact",
        expected_tags=["keep", "llm_pending:true", "cat:fact"],
    )

    response = await _handle_notification_event(
        queue,
        event,
        engine=engine,
        runtime_authority=_runtime(),
    )

    assert response["ok"] is True
    assert order == ["patch", "queue"]
    engine.patch_ordinary_memory.assert_called_once_with(
        "memory-1",
        replacements={
            "tags": ["keep", "llm_classified:true", "cat:fact"],
            "category": "fact",
        },
        expected_project_id="project:test",
        expected_content_hash=synthesis_content_hash("classified content"),
        expected_tags=["keep", "llm_pending:true", "cat:fact"],
        expected_category="fact",
        require_source_available=True,
    )


async def test_llm_classification_removes_all_case_insensitive_category_tags():
    engine = MagicMock()
    canonical = _classification_record()
    canonical["tags"] = [
        "keep",
        "CAT:LEGACY",
        "llm_pending:true",
        "cat:other",
    ]
    engine.get_memory_dict_for_review.return_value = canonical
    engine.patch_ordinary_memory.return_value = canonical
    queue = _RecordingQueue()
    event = _classification_event(
        new_category="fact",
        expected_tags=list(canonical["tags"]),
    )

    response = await _handle_notification_event(
        queue,
        event,
        engine=engine,
        runtime_authority=_runtime(),
    )

    assert response["ok"] is True
    engine.patch_ordinary_memory.assert_called_once_with(
        "memory-1",
        replacements={
            "tags": ["keep", "llm_classified:true", "cat:fact"],
            "category": "fact",
        },
        expected_project_id="project:test",
        expected_content_hash=synthesis_content_hash("classified content"),
        expected_tags=[
            "keep",
            "CAT:LEGACY",
            "llm_pending:true",
            "cat:other",
        ],
        expected_category="other",
        require_source_available=True,
    )


async def test_llm_classification_patch_failure_endpoint_is_not_success():
    engine = MagicMock()
    engine.get_memory_dict_for_review.return_value = _classification_record()
    engine.patch_ordinary_memory.side_effect = RuntimeError("ordinary_patch_cas_mismatch")
    queue = _RecordingQueue()
    event = _classification_event()

    response = await _handle_notification_event(
        queue,
        event,
        engine=engine,
        runtime_authority=_runtime(),
    )

    assert response == {
        "ok": False,
        "classification_persistence": {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_patch_failed",
            "memory_id": "memory-1",
        },
    }
    assert queue.items == []


def test_llm_classification_stale_content_is_zero_write():
    engine = MagicMock()
    engine.get_memory_dict_for_review.return_value = _classification_record()

    result = _persist_llm_classification_notification(
        engine,
        _classification_event(expected_content_hash=synthesis_content_hash("superseded content")),
        _runtime(),
    )

    assert result == {
        "committed": False,
        "partial": False,
        "reason": "llm_classification_source_changed",
        "memory_id": "memory-1",
    }
    engine.patch_ordinary_memory.assert_not_called()


def test_llm_classification_stale_tags_are_zero_write():
    engine = MagicMock()
    engine.get_memory_dict_for_review.return_value = _classification_record()

    result = _persist_llm_classification_notification(
        engine,
        _classification_event(expected_tags=["llm_pending:true"]),
        _runtime(),
    )

    assert result == {
        "committed": False,
        "partial": False,
        "reason": "llm_classification_source_changed",
        "memory_id": "memory-1",
    }
    engine.patch_ordinary_memory.assert_not_called()


def test_llm_classification_stale_category_is_zero_write():
    engine = MagicMock()
    engine.get_memory_dict_for_review.return_value = _classification_record()

    result = _persist_llm_classification_notification(
        engine,
        _classification_event(expected_category="fact"),
        _runtime(),
    )

    assert result == {
        "committed": False,
        "partial": False,
        "reason": "llm_classification_source_changed",
        "memory_id": "memory-1",
    }
    engine.patch_ordinary_memory.assert_not_called()
