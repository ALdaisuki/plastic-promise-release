from __future__ import annotations

import json
import sqlite3

import pytest

from plastic_promise.core.chunking import build_chunk_manifest, chunk_manifest_hash
from plastic_promise.mcp.dashboard_v2.config import (
    DashboardAccessError,
    DashboardConfigurationError,
    DashboardSettings,
    resolve_local_scope,
)
from plastic_promise.mcp.dashboard_v2.repository import (
    DashboardCursorError,
    DashboardRepository,
    redact_value,
)


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT,
            memory_type TEXT,
            source TEXT,
            owner TEXT,
            tier TEXT,
            scope TEXT,
            category TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            domain TEXT,
            importance REAL,
            created_at TEXT,
            access_count INTEGER,
            worth_success INTEGER,
            worth_failure INTEGER,
            activation_weight REAL,
            last_accessed TEXT,
            project_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            source_class TEXT,
            created_by_call_id TEXT,
            origin_kind TEXT,
            origin_uri TEXT,
            origin_ref TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            embedding_text TEXT NOT NULL DEFAULT '',
            l0_abstract TEXT,
            l1_summary TEXT
        );
        CREATE TABLE call_spans (
            call_id TEXT PRIMARY KEY,
            parent_call_id TEXT NOT NULL DEFAULT '',
            request_scope_id TEXT NOT NULL DEFAULT '',
            stage_session_id TEXT NOT NULL DEFAULT '',
            flow_line_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            tool_name TEXT NOT NULL,
            stage_name TEXT NOT NULL DEFAULT '',
            caller TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'success',
            degraded INTEGER NOT NULL DEFAULT 0,
            input_hash TEXT NOT NULL DEFAULT '',
            output_hash TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL
        );
        CREATE TABLE memory_lineage (
            lineage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            parent_memory_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            request_scope_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE synthesis_artifacts (
            memory_id TEXT PRIMARY KEY,
            synthesis_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            support_count INTEGER NOT NULL DEFAULT 0,
            validity_scope TEXT NOT NULL DEFAULT '',
            source_fingerprint TEXT NOT NULL DEFAULT '',
            last_verified_at TEXT NOT NULL DEFAULT '',
            last_linted_at TEXT NOT NULL DEFAULT '',
            stale_reason TEXT NOT NULL DEFAULT '',
            created_by_call_id TEXT NOT NULL DEFAULT '',
            verified_by_actor TEXT NOT NULL DEFAULT '',
            verified_by_call_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE runtime_events (
            event_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_kind TEXT NOT NULL,
            event_name TEXT NOT NULL,
            status TEXT NOT NULL,
            request_scope_id TEXT NOT NULL DEFAULT '',
            stage_session_id TEXT NOT NULL DEFAULT '',
            flow_line_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            trust_tier TEXT NOT NULL DEFAULT '',
            defense_decision TEXT NOT NULL DEFAULT '',
            audit_trace_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE degradation_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL,
            request_scope_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            link_name TEXT NOT NULL,
            policy TEXT NOT NULL,
            level TEXT NOT NULL,
            error_class TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            fallback_used TEXT NOT NULL DEFAULT '',
            minimum_result TEXT NOT NULL DEFAULT '',
            user_visible INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE store_outbox (
            outbox_id TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            error_class TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            dedupe_key TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT '',
            next_attempt_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE trust_scores (
            target TEXT PRIMARY KEY,
            trust REAL NOT NULL,
            tier TEXT NOT NULL,
            autonomy_level TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    memories = [
        ("mem-a-1", "A first", "experience", "project:a", "project", "2026-07-19T01:00:00Z"),
        ("mem-a-2", "A second", "experience", "project:a", "private", "2026-07-19T02:00:00Z"),
        (
            "mem-b-secret",
            "B_MEMORY_SECRET",
            "experience",
            "project:b",
            "project",
            "2026-07-19T09:00:00Z",
        ),
        (
            "mem-global",
            "globally visible",
            "experience",
            "project:b",
            "global",
            "2026-07-19T03:00:00Z",
        ),
        ("syn-a", "A synthesis", "synthesis", "project:a", "project", "2026-07-19T04:00:00Z"),
        (
            "syn-b",
            "B_SYNTHESIS_SECRET",
            "synthesis",
            "project:b",
            "project",
            "2026-07-19T10:00:00Z",
        ),
    ]
    for memory_id, content, memory_type, project_id, visibility, created_at in memories:
        conn.execute(
            """
            INSERT INTO memories (
                id, content, memory_type, source, owner, tier, scope, category,
                tags, domain, importance, created_at, access_count, worth_success,
                worth_failure, activation_weight, last_accessed, project_id,
                visibility, source_class, created_by_call_id, origin_kind,
                origin_uri, origin_ref, metadata_json, l0_abstract, l1_summary
            ) VALUES (?, ?, ?, 'fixture', '', 'L1', 'global', 'fact', '[]',
                      'building', 0.5, ?, 0, 1, 0, 0.5, ?, ?, ?, 'experience',
                      'call-seed', 'test', '', '', '{"safe": true}', '', '')
            """,
            (memory_id, content, memory_type, created_at, created_at, project_id, visibility),
        )

    for call_id, project_id, started_at, ended_at in (
        (
            "call-a-1",
            "project:a",
            "2026-07-19T01:00:00Z",
            "2026-07-19T01:00:01.234Z",
        ),
        ("call-a-2", "project:a", "2026-07-19T02:00:00Z", "2026-07-19T02:00:00Z"),
        ("call-b", "project:b", "2026-07-19T09:00:00Z", "2026-07-19T09:00:02Z"),
        ("call-legacy", "", "2026-07-19T10:00:00Z", "2026-07-19T10:00:03Z"),
    ):
        conn.execute(
            """
            INSERT INTO call_spans (
                call_id, request_scope_id, project_id, tool_name, caller, status,
                metadata_json, started_at, ended_at
            ) VALUES (?, 'scope-collision', ?, 'memory_recall', 'fixture',
                      'success', '{"api_key":"SPAN_SECRET","safe":1}', ?, ?)
            """,
            (call_id, project_id, started_at, ended_at),
        )
    conn.execute("UPDATE call_spans SET degraded = 1 WHERE call_id = 'call-a-2'")

    conn.execute(
        "INSERT INTO memory_lineage (memory_id,parent_memory_id,call_id,project_id,relation,metadata_json,created_at) "
        "VALUES ('mem-a-2','mem-a-1','call-a-2','project:a','derived_from','{}','2026-07-19T02:00:00Z')"
    )
    conn.execute(
        "INSERT INTO memory_lineage (memory_id,parent_memory_id,call_id,project_id,relation,metadata_json,created_at) "
        "VALUES ('mem-b-secret','mem-a-1','call-b','project:b','derived_from','{\"secret\":\"B_LINEAGE_SECRET\"}','2026-07-19T09:00:00Z')"
    )
    conn.execute(
        "INSERT INTO memory_lineage (memory_id,parent_memory_id,call_id,project_id,relation,metadata_json,created_at) "
        "VALUES ('mem-global','mem-global','call-legacy','project:legacy-global',"
        "'ordinary_source_forgotten','{\"actor\":\"maintenance\"}','2026-07-19T03:30:00Z')"
    )
    conn.execute(
        "INSERT INTO memory_lineage (memory_id,parent_memory_id,call_id,project_id,relation,metadata_json,created_at) "
        "VALUES ('mem-global','mem-global','call-b','project:b','supports',"
        "'{\"secret\":\"B_GLOBAL_LINEAGE_SECRET\"}','2026-07-19T03:31:00Z')"
    )

    for memory_id, key, status, updated_at in (
        ("syn-a", "key-a", "verified", "2026-07-19T04:00:00Z"),
        ("syn-b", "key-b", "draft", "2026-07-19T10:00:00Z"),
    ):
        conn.execute(
            """
            INSERT INTO synthesis_artifacts (
                memory_id,synthesis_key,status,revision,support_count,validity_scope,
                source_fingerprint,metadata_json,created_at,updated_at
            ) VALUES (?, ?, ?, 1, 2, 'project', 'sha256:test', '{}', ?, ?)
            """,
            (memory_id, key, status, updated_at, updated_at),
        )

    for event_id, project_id, created_at in (
        ("evt-a", "project:a", "2026-07-19T01:30:00Z"),
        ("evt-b", "project:b", "2026-07-19T09:30:00Z"),
        ("evt-legacy", "", "2026-07-19T10:30:00Z"),
    ):
        conn.execute(
            """
            INSERT INTO runtime_events (
                event_id,event_kind,event_name,status,request_scope_id,project_id,
                actor,metadata_json,created_at
            ) VALUES (?, 'tool', 'memory_recall', 'completed', 'scope-collision', ?,
                      'fixture', '{"token":"EVENT_SECRET","safe":1}', ?)
            """,
            (event_id, project_id, created_at),
        )

    for project_id, call_id, created_at in (
        ("project:a", "call-a-1", "2026-07-19T01:45:00Z"),
        ("project:b", "call-b", "2026-07-19T09:45:00Z"),
        ("", "call-legacy", "2026-07-19T10:45:00Z"),
    ):
        conn.execute(
            """
            INSERT INTO degradation_events (
                call_id,request_scope_id,project_id,tool_name,link_name,policy,
                level,error_message,metadata_json,created_at
            ) VALUES (?, 'scope-collision', ?, 'memory_recall', 'vector',
                      'best_effort', 'warning', 'Bearer DEGRADATION_SECRET', '{}', ?)
            """,
            (call_id, project_id, created_at),
        )

    for outbox_id, project_id, call_id, created_at in (
        ("outbox-a", "project:a", "call-a-1", "2026-07-19T01:50:00Z"),
        ("outbox-b", "project:b", "call-b", "2026-07-19T09:50:00Z"),
        ("outbox-legacy", "", "call-legacy", "2026-07-19T10:50:00Z"),
    ):
        conn.execute(
            """
            INSERT INTO store_outbox (
                outbox_id,tool_name,project_id,call_id,status,payload_json,
                error_message,metadata_json,created_at,attempt_count,updated_at,
                next_attempt_at
            ) VALUES (?, 'memory_index', ?, ?, 'pending',
                      '{"content":"B_OUTBOX_SECRET"}', 'password=OUTBOX_SECRET',
                      '{"authorization":"OUTBOX_AUTH_SECRET","safe":1}', ?, 2, ?, ?)
            """,
            (outbox_id, project_id, call_id, created_at, created_at, created_at),
        )

    conn.execute(
        "INSERT INTO trust_scores VALUES "
        "('codex',0.72,'medium','standard','2026-07-19T00:00:00Z','2026-07-01T00:00:00Z')"
    )
    conn.commit()
    return conn


@pytest.fixture
def repository():
    conn = _database()
    settings = DashboardSettings.from_env(
        {"PP_DASHBOARD_V2": "1", "PP_DASHBOARD_PROJECT_ID": "project:a"}
    )
    scope = resolve_local_scope(settings, client_host="127.0.0.1")
    yield DashboardRepository(conn, scope)
    conn.close()


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", "2", 1])
def test_dashboard_feature_gates_require_exact_string_one(value):
    env = {"PP_DASHBOARD_PROJECT_ID": "project:a"}
    if value is not None:
        env["PP_DASHBOARD_V2"] = value

    settings = DashboardSettings.from_env(env)

    assert settings.enabled is False
    assert settings.explain_enabled is False


def test_retrieval_explain_gate_requires_dashboard_and_exact_string_one():
    base = {"PP_DASHBOARD_PROJECT_ID": "project:a"}

    disabled = DashboardSettings.from_env({**base, "PP_RETRIEVAL_EXPLAIN": "1"})
    enabled = DashboardSettings.from_env(
        {**base, "PP_DASHBOARD_V2": "1", "PP_RETRIEVAL_EXPLAIN": "1"}
    )
    inexact = DashboardSettings.from_env(
        {**base, "PP_DASHBOARD_V2": "1", "PP_RETRIEVAL_EXPLAIN": "true"}
    )

    assert disabled.explain_enabled is False
    assert enabled.explain_enabled is True
    assert inexact.explain_enabled is False


def test_dashboard_settings_reject_invalid_or_unimplemented_auth_when_enabled():
    base = {"PP_DASHBOARD_V2": "1", "PP_DASHBOARD_PROJECT_ID": "project:a"}
    with pytest.raises(DashboardConfigurationError, match="dashboard_auth_mode_invalid"):
        DashboardSettings.from_env({**base, "PP_DASHBOARD_AUTH": "open"})
    for mode in ("token", "required"):
        with pytest.raises(DashboardConfigurationError, match="dashboard_auth_backend_unavailable"):
            DashboardSettings.from_env({**base, "PP_DASHBOARD_AUTH": mode})


def test_dashboard_settings_resolve_server_owned_project_by_priority():
    settings = DashboardSettings.from_env(
        {
            "PP_DASHBOARD_V2": "1",
            "PP_DASHBOARD_PROJECT_ID": "dashboard",
            "PLASTIC_PROJECT_ID": "plastic",
            "PP_PROJECT_ID": "fallback",
        }
    )

    assert settings.project_id == "project:dashboard"


def test_local_scope_fails_closed_for_bind_client_project_and_missing_authority():
    with pytest.raises(DashboardConfigurationError, match="dashboard_local_bind_not_loopback"):
        DashboardSettings.from_env(
            {"PP_DASHBOARD_V2": "1", "PP_DASHBOARD_PROJECT_ID": "project:a"},
            bind_host="0.0.0.0",
        )

    settings = DashboardSettings.from_env(
        {"PP_DASHBOARD_V2": "1", "PP_DASHBOARD_PROJECT_ID": "project:a"}
    )
    with pytest.raises(DashboardAccessError) as remote:
        resolve_local_scope(settings, client_host="192.0.2.10")
    assert remote.value.status_code == 403
    assert remote.value.code == "dashboard_loopback_required"
    with pytest.raises(DashboardAccessError) as foreign:
        resolve_local_scope(
            settings,
            client_host="127.0.0.1",
            requested_project_id="project:b",
        )
    assert foreign.value.status_code == 403
    assert foreign.value.code == "dashboard_scope_denied"

    missing = DashboardSettings.from_env({"PP_DASHBOARD_V2": "1"})
    with pytest.raises(DashboardAccessError) as unavailable:
        resolve_local_scope(missing, client_host="127.0.0.1")
    assert unavailable.value.status_code == 503


def test_request_pagination_applies_project_scope_before_limit_and_count(repository):
    first = repository.list_requests(limit=1)

    assert first["page"]["total"] == 2
    assert [row["call_id"] for row in first["data"]] == ["call-a-2"]
    assert first["page"]["has_more"] is True
    assert first["page"]["next_cursor"]
    assert first["data"][0]["duration_ms"] is None
    assert first["data"][0]["duration_status"] == "not_captured"
    assert "SPAN_SECRET" not in json.dumps(first)

    second = repository.list_requests(limit=1, cursor=first["page"]["next_cursor"])
    assert [row["call_id"] for row in second["data"]] == ["call-a-1"]
    assert second["data"][0]["duration_ms"] == 1234.0
    assert second["data"][0]["duration_status"] == "measured"
    assert second["page"]["has_more"] is False


def test_request_filters_are_scoped_counted_and_bound_to_cursor(repository):
    degraded = repository.list_requests(
        limit=1,
        status="success",
        tool_name="memory_recall",
        degraded=True,
    )

    assert degraded["page"]["total"] == 1
    assert [row["call_id"] for row in degraded["data"]] == ["call-a-2"]
    assert degraded["data"][0]["degraded"] is True

    cursor = repository.list_requests(limit=1)["page"]["next_cursor"]
    with pytest.raises(DashboardCursorError, match="cursor_filter_mismatch"):
        repository.list_requests(limit=1, cursor=cursor, degraded=False)


def test_cursor_is_bound_to_scope_collection_and_filters(repository):
    cursor = repository.list_requests(limit=1)["page"]["next_cursor"]
    assert cursor

    with pytest.raises(DashboardCursorError, match="cursor_filter_mismatch"):
        repository.list_requests(limit=1, cursor=cursor, status="success")
    with pytest.raises(DashboardCursorError, match="cursor_invalid"):
        repository.list_requests(limit=1, cursor=cursor[:-2] + "xx")

    other_settings = DashboardSettings.from_env(
        {"PP_DASHBOARD_V2": "1", "PP_DASHBOARD_PROJECT_ID": "project:b"}
    )
    other_scope = resolve_local_scope(other_settings, client_host="::1")
    other = DashboardRepository(repository.connection, other_scope)
    with pytest.raises(DashboardCursorError, match="cursor_scope_mismatch"):
        other.list_requests(limit=1, cursor=cursor)


def test_memory_scope_includes_global_but_hides_foreign_project_rows(repository):
    page = repository.list_memories(limit=20)
    ids = [row["id"] for row in page["data"]]

    assert page["page"]["total"] == 4
    assert "mem-a-1" in ids
    assert "mem-a-2" in ids
    assert "mem-global" in ids
    assert "syn-a" in ids
    assert "mem-b-secret" not in ids
    assert "syn-b" not in ids
    assert repository.get_memory("mem-b-secret") is None
    assert repository.get_memory("does-not-exist") is None
    assert repository.get_memory("mem-global")["content"] == "globally visible"


def test_direct_request_id_uses_exact_project_scope(repository):
    request = repository.get_request("call-a-1")
    assert request["project_id"] == "project:a"
    assert request["duration_ms"] == 1234.0
    assert request["duration_status"] == "measured"
    assert repository.get_request("call-b") is None
    assert repository.get_request("call-legacy") is None


def test_request_metadata_reprojects_stored_explain_snapshots(repository):
    repository.connection.execute(
        "UPDATE call_spans SET metadata_json = ? WHERE call_id = ?",
        (
            json.dumps(
                {
                    "safe": "visible",
                    "retrieval_explain_v1": {
                        "schema": "retrieval_explain_v1",
                        "content": "STORED_EXPLAIN_CONTENT_SECRET",
                        "query": "STORED_EXPLAIN_QUERY_SECRET",
                        "items": [
                            {
                                "id": "mem-a-1",
                                "rank": 1,
                                "final_score": 0.91,
                                "prompt": "STORED_EXPLAIN_PROMPT_SECRET",
                            }
                        ],
                    },
                }
            ),
            "call-a-1",
        ),
    )

    detail = repository.get_request("call-a-1")
    listed = next(
        row
        for row in repository.list_requests(limit=10)["data"]
        if row["call_id"] == "call-a-1"
    )

    for result in (detail, listed):
        assert result["metadata"]["safe"] == "visible"
        assert result["metadata"]["retrieval_explain_v1"]["items"] == [
            {"id": "mem-a-1", "rank": 1, "final_score": 0.91}
        ]
        rendered = json.dumps(result)
        assert "STORED_EXPLAIN_CONTENT_SECRET" not in rendered
        assert "STORED_EXPLAIN_QUERY_SECRET" not in rendered
        assert "STORED_EXPLAIN_PROMPT_SECRET" not in rendered


def test_lineage_requires_anchor_and_both_ends_to_be_visible(repository):
    visible = repository.get_lineage("mem-a-1")

    assert visible is not None
    assert [(row["memory_id"], row["parent_memory_id"]) for row in visible["data"]] == [
        ("mem-a-2", "mem-a-1")
    ]
    assert visible["memory"]["id"] == "mem-a-1"
    assert visible["summary"]["relations"] == {"derived_from": 1}

    global_visible = repository.get_lineage("mem-global")
    assert [row["relation"] for row in global_visible["data"]] == [
        "ordinary_source_forgotten"
    ]
    assert global_visible["data"][0]["evidence_scope"] == "legacy_global"
    assert global_visible["summary"]["legacy_global_edges"] == 1
    assert repository.get_lineage("mem-b-secret") is None
    assert "B_LINEAGE_SECRET" not in json.dumps(visible)
    assert "B_GLOBAL_LINEAGE_SECRET" not in json.dumps(global_visible)


def _install_manifest(
    repository,
    manifest,
    memory_id="mem-a-1",
    *,
    source_text=None,
    **metadata_fields,
):
    metadata = {
        "safe": True,
        **metadata_fields,
        "memory_index": {
            "chunk_manifest": manifest,
            "chunk_manifest_hash": chunk_manifest_hash(manifest),
        },
    }
    if source_text is None:
        repository.connection.execute(
            "UPDATE memories SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), memory_id),
        )
    else:
        repository.connection.execute(
            "UPDATE memories SET metadata_json = ?, embedding_text = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), source_text, memory_id),
        )
    repository.connection.commit()
    return manifest


def _install_chunk_manifest(repository, memory_id="mem-a-1"):
    manifest = {
        "schema_version": "structure-v1",
        "source_hash": "a" * 64,
        "source_chars": 24,
        "chunk_count": 2,
        "covered_source_chars": 24,
        "last_source_end": 24,
        "truncated": False,
        "resource_limited": False,
        "chunks": [
            {
                "chunk_id": "chunk_intro",
                "ordinal": 0,
                "kind": "paragraph",
                "header_path": ["检索", "说明"],
                "source_start": 0,
                "source_end": 12,
                "source_hash": "a" * 64,
                "text_hash": "b" * 64,
                "text": "检索\n第一段",
                "context_truncated": False,
            },
            {
                "chunk_id": "chunk_tail",
                "ordinal": 1,
                "kind": "list",
                "header_path": ["检索"],
                "source_start": 12,
                "source_end": 24,
                "source_hash": "a" * 64,
                "text_hash": "c" * 64,
                "text": "- 保留尾部",
                "context_truncated": False,
            },
        ],
    }
    return _install_manifest(repository, manifest, memory_id)


def _install_many_chunk_manifest(repository, memory_id="mem-a-1"):
    source = "\n\n".join(f"# Section {index}\nparagraph {index}" for index in range(24))
    manifest = build_chunk_manifest(
        source,
        target_chars=20,
        hard_chars=40,
        max_chunks=64,
    )
    assert manifest["chunk_count"] == 24
    return _install_manifest(repository, manifest, memory_id)


def test_memory_detail_projects_structured_chunks_as_parent_evidence(repository):
    manifest = _install_chunk_manifest(repository)

    detail = repository.get_memory("mem-a-1")
    listed = next(
        row for row in repository.list_memories(limit=20)["data"] if row["id"] == "mem-a-1"
    )

    assert detail["chunking"] == {
        "status": "available",
        "enabled": True,
        "schema_version": "structure-v1",
        "chunk_count": 2,
        "source_hash": "a" * 64,
        "source_chars": 24,
        "covered_source_chars": 24,
        "last_source_end": 24,
        "truncated": False,
        "resource_limited": False,
        "returned_count": 2,
        "projection_limit": 256,
        "projection_truncated": False,
        "manifest_hash": chunk_manifest_hash(manifest),
    }
    assert [chunk["chunk_id"] for chunk in detail["chunks"]] == [
        "chunk_intro",
        "chunk_tail",
    ]
    assert all(chunk["parent_memory_id"] == "mem-a-1" for chunk in detail["chunks"])
    assert detail["chunks"][0]["header_path"] == ["检索", "说明"]
    assert detail["chunks"][1]["source_start"] == 12
    assert listed["chunk_count"] == 2
    assert "chunks" not in listed
    assert "chunk_manifest" not in listed["metadata"]["memory_index"]
    assert "chunk_manifest" not in detail["metadata"]["memory_index"]


def test_memory_manifest_is_validated_before_public_redaction(repository):
    source = "# Security\n\nDocumentation may say bearer token without a credential."
    manifest = build_chunk_manifest(
        source,
        target_chars=128,
        hard_chars=256,
        max_chunks=64,
    )
    _install_manifest(
        repository,
        manifest,
        source_text=source,
        raw_content="RAW_CONTENT_MUST_NOT_BYPASS_THE_PUBLIC_PROJECTION",
    )

    detail = repository.get_memory("mem-a-1")
    listed = next(
        row for row in repository.list_memories(limit=20)["data"] if row["id"] == "mem-a-1"
    )

    assert detail["chunking"]["status"] == "available"
    assert "Bearer [REDACTED]" in detail["chunks"][0]["text"]
    for public_row in (detail, listed):
        rendered = json.dumps(public_row, ensure_ascii=False)
        assert "RAW_CONTENT_MUST_NOT_BYPASS_THE_PUBLIC_PROJECTION" not in rendered
        assert "chunk_manifest" not in public_row["metadata"].get("memory_index", {})


def test_memory_manifest_must_match_the_persisted_embedding_text(repository):
    source = "# 原始证据\n\n这是当前记忆的向量文本。"
    unrelated_source = "# 无关证据\n\n这是另一条记忆的切片。"
    unrelated = build_chunk_manifest(
        unrelated_source,
        target_chars=64,
        hard_chars=128,
        max_chunks=64,
    )
    _install_manifest(repository, unrelated, source_text=source)

    detail = repository.get_memory("mem-a-1")

    assert detail["chunking"]["status"] == "invalid"
    assert detail["chunking"]["reason"] == "manifest_source_mismatch"
    assert detail["chunks"] == []


def test_memory_detail_rejects_chunk_manifest_with_mismatched_hash(repository):
    _install_chunk_manifest(repository)
    row = repository.connection.execute(
        "SELECT metadata_json FROM memories WHERE id = ?",
        ("mem-a-1",),
    ).fetchone()
    metadata = json.loads(row[0])
    metadata["memory_index"]["chunk_manifest_hash"] = "0" * 64
    repository.connection.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False), "mem-a-1"),
    )
    repository.connection.commit()

    detail = repository.get_memory("mem-a-1")

    assert detail["chunking"] == {
        "status": "invalid",
        "enabled": False,
        "chunk_count": 0,
        "returned_count": 0,
        "projection_limit": 256,
        "projection_truncated": False,
        "reason": "manifest_hash_mismatch",
    }
    assert detail["chunks"] == []


def test_lineage_returns_typed_nodes_directed_edges_and_chunk_anchors(repository):
    _install_chunk_manifest(repository)

    lineage = repository.get_lineage("mem-a-1")

    assert [node["id"] for node in lineage["nodes"]] == ["mem-a-1", "mem-a-2"]
    assert lineage["nodes"][0]["type"] == "memory"
    assert lineage["nodes"][0]["roles"] == ["anchor", "parent"]
    assert [anchor["chunk_id"] for anchor in lineage["nodes"][0]["chunk_anchors"]] == [
        "chunk_intro",
        "chunk_tail",
    ]
    edge = lineage["edges"][0]
    assert edge["source"] == "mem-a-1"
    assert edge["target"] == "mem-a-2"
    assert edge["directed"] is True
    assert edge["direction"] == "parent_to_child"
    assert edge["timestamp"] == "2026-07-19T02:00:00Z"
    assert edge["evidence"]["call_id"] == "call-a-2"
    assert edge["call"]["duration_status"] == "not_captured"
    assert edge["chunk_anchors"]["status"] == "manifest_available_not_lineage_specific"
    assert edge["chunk_anchors"]["source"][0]["chunk_id"] == "chunk_intro"
    assert edge["chunk_anchors"]["source_summary"] == {
        "total": 2,
        "returned": 2,
        "truncated": False,
        "limit": 8,
    }
    assert lineage["nodes"][0]["chunk_anchor_summary"] == {
        "total": 2,
        "returned": 2,
        "truncated": False,
        "limit": 16,
    }
    assert lineage["data"] == lineage["edges"]
    assert lineage["summary"]["node_count"] == 2
    assert lineage["summary"]["edge_count"] == 1
    assert lineage["summary"]["chunk_anchor_count"] == 2


def test_lineage_chunk_anchor_limits_report_total_and_truncation(repository):
    _install_many_chunk_manifest(repository)

    lineage = repository.get_lineage("mem-a-1")

    anchor = lineage["nodes"][0]
    edge = lineage["edges"][0]
    assert len(anchor["chunk_anchors"]) == 16
    assert anchor["chunk_anchor_summary"] == {
        "total": 24,
        "returned": 16,
        "truncated": True,
        "limit": 16,
    }
    assert len(edge["chunk_anchors"]["source"]) == 8
    assert edge["chunk_anchors"]["source_summary"] == {
        "total": 24,
        "returned": 8,
        "truncated": True,
        "limit": 8,
    }
    assert lineage["summary"]["chunk_anchor_count"] == 24
    assert lineage["summary"]["chunk_anchor_returned"] == 16
    assert lineage["summary"]["chunk_anchors_truncated"] is True


def test_explain_enrichment_keeps_channel_scores_and_does_not_invent_chunk_hit(repository):
    _install_chunk_manifest(repository)
    snapshot = {
        "schema": "retrieval_explain_v1",
        "channels": [
            {
                "name": "vector",
                "state": {"participating": True},
                "items": [{"id": "mem-a-1", "rank": 1, "score": 0.91}],
            }
        ],
        "items": [{"id": "mem-a-1", "rank": 1, "final_score": 0.88}],
        "pipeline": {},
        "truncated": {},
    }

    enriched = repository.enrich_retrieval_explain(snapshot)

    candidate = enriched["items"][0]
    assert candidate["channel_scores"] == {"vector": 0.91}
    assert candidate["chunk_evidence"]["status"] == "available_not_recorded"
    assert [row["chunk_id"] for row in candidate["chunk_evidence"]["anchors"]] == [
        "chunk_intro",
        "chunk_tail",
    ]
    assert "chunk_id" not in candidate
    assert enriched["channels"][0]["items"][0]["channel"] == "vector"


def test_explain_enrichment_matches_explicit_chunk_after_anchor_preview_limit(repository):
    manifest = _install_many_chunk_manifest(repository)
    explicit = manifest["chunks"][20]
    snapshot = {
        "schema": "retrieval_explain_v1",
        "channels": [],
        "items": [
            {
                "id": "mem-a-1",
                "rank": 1,
                "final_score": 0.88,
                "chunk_id": explicit["chunk_id"],
            }
        ],
        "pipeline": {},
        "truncated": {},
    }

    enriched = repository.enrich_retrieval_explain(snapshot)

    evidence = enriched["items"][0]["chunk_evidence"]
    assert evidence["status"] == "matched"
    assert evidence["chunk_id"] == explicit["chunk_id"]
    assert evidence["ordinal"] == 20


def test_synthesis_collection_joins_memory_scope_before_limit(repository):
    page = repository.list_synthesis(limit=1)

    assert page["page"]["total"] == 1
    assert [row["memory_id"] for row in page["data"]] == ["syn-a"]
    assert page["summary"] == {
        "artifact_count": 1,
        "status_counts": {"verified": 1},
    }


def test_operations_union_scopes_every_source_and_never_projects_outbox_payload(repository):
    page = repository.list_operations(limit=20)
    rendered = json.dumps(page)

    assert page["page"]["total"] == 3
    assert {row["kind"] for row in page["data"]} == {
        "runtime_event",
        "degradation",
        "outbox",
    }
    assert all(row["project_id"] == "project:a" for row in page["data"])
    assert "scope-collision" in rendered
    assert "payload" not in next(row for row in page["data"] if row["kind"] == "outbox")
    for secret in ("EVENT_SECRET", "DEGRADATION_SECRET", "B_OUTBOX_SECRET", "OUTBOX_SECRET"):
        assert secret not in rendered


def test_trust_read_is_pure_select_and_does_not_bootstrap_unknown_target(repository):
    before = repository.connection.total_changes

    assert repository.get_trust("codex") == {
        "target": "codex",
        "trust": 0.72,
        "tier": "medium",
        "autonomy_level": "standard",
        "last_updated": "2026-07-19T00:00:00Z",
    }
    assert repository.get_trust("missing") is None
    assert repository.connection.total_changes == before


def test_recursive_redaction_covers_nested_mappings_sequences_and_bearer_values():
    value = {
        "api_key": "alpha",
        "nested": {
            "password": "beta",
            "safe": ["ok", {"authorization": "Bearer gamma"}],
        },
        "message": "request failed with Bearer delta",
    }

    redacted = redact_value(value)
    rendered = json.dumps(redacted)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["safe"][0] == "ok"
    for secret in ("alpha", "beta", "gamma", "delta"):
        assert secret not in rendered


def test_overview_counts_only_effective_scope(repository):
    overview = repository.overview()

    assert overview["memory_count"] == 4
    assert overview["request_count"] == 2
    assert overview["synthesis_count"] == 1
    assert overview["operation_count"] == 3
    assert overview["runtime_event_count"] == 1
    assert overview["degradation_count"] == 1
    assert overview["outbox_count"] == 1
    assert overview["pending_outbox_count"] == 1
