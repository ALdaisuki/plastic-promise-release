"""Tests for Safety-Net Daemon scanner functions (Tag Dispatch + Innovation Phase).

Requires MCP server running on localhost:9020 (integration tests).
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)


class TestSafetyNetImports:
    """Verify all scan + dispatch functions are importable without side effects."""

    def test_import_dispatch_fix_task(self):
        from daemons.maintenance_daemon import dispatch_fix_task

        assert callable(dispatch_fix_task)

    def test_import_tag_for_redo(self):
        from daemons.maintenance_daemon import tag_for_redo

        assert callable(tag_for_redo)

    def test_import_tag_audit_finding(self):
        from daemons.maintenance_daemon import tag_audit_finding

        assert callable(tag_audit_finding)

    def test_import_store_tagged_memory(self):
        from daemons.maintenance_daemon import _store_tagged_memory

        assert callable(_store_tagged_memory)

    def test_import_scan_innovation_opportunities(self):
        from daemons.maintenance_daemon import scan_innovation_opportunities

        assert callable(scan_innovation_opportunities)

    def test_import_scan_redo_queue(self):
        from daemons.maintenance_daemon import scan_redo_queue

        assert callable(scan_redo_queue)

    def test_import_scan_duplicate_clusters(self):
        from daemons.maintenance_daemon import scan_duplicate_clusters

        assert callable(scan_duplicate_clusters)

    def test_import_scan_stale_worth(self):
        from daemons.maintenance_daemon import scan_stale_worth

        assert callable(scan_stale_worth)

    def test_import_scan_tier_migration(self):
        from daemons.maintenance_daemon import scan_tier_migration

        assert callable(scan_tier_migration)

    def test_import_scan_category_stuck(self):
        from daemons.maintenance_daemon import scan_category_stuck

        assert callable(scan_category_stuck)

    def test_import_scan_orphan_steps(self):
        from daemons.maintenance_daemon import scan_orphan_steps

        assert callable(scan_orphan_steps)

    def test_import_scan_unclosed_issues(self):
        from daemons.maintenance_daemon import scan_unclosed_issues

        assert callable(scan_unclosed_issues)

    def test_import_recover_stuck_tasks(self):
        from daemons.maintenance_daemon import recover_stuck_tasks

        assert callable(recover_stuck_tasks)

    def test_import_cleanup_old_tags(self):
        from daemons.maintenance_daemon import cleanup_old_tags

        assert callable(cleanup_old_tags)

    def test_import_scan_llm_classify(self):
        from daemons.maintenance_daemon import scan_llm_classify

        assert callable(scan_llm_classify)

    @pytest.mark.asyncio
    async def test_governed_maintenance_cycle_has_stable_failure_isolated_order(self, monkeypatch):
        from daemons import maintenance_daemon

        calls = []

        async def lifecycle(_engine):
            calls.append("memory_lifecycle")
            raise RuntimeError("lifecycle failed")

        def expiry(_engine):
            calls.append("proposal_expiry")
            return {"expired": 0}

        def synthesis_scan(_engine):
            calls.append("synthesis_integrity")
            return {"stale": 0}

        def replay(_engine):
            calls.append("synthesis_index_replay")
            return {"done": 0}

        def replay_memory(_engine):
            calls.append("memory_index_replay")
            return {"done": 0}

        async def audit():
            calls.append("audit")
            return {"score": 1.0}

        monkeypatch.setattr(maintenance_daemon, "scan_memory_decay", lifecycle)
        monkeypatch.setattr(maintenance_daemon, "expire_pending_memory_proposals", expiry)
        monkeypatch.setattr(maintenance_daemon, "scan_synthesis_integrity", synthesis_scan)
        monkeypatch.setattr(maintenance_daemon, "replay_memory_index_jobs", replay_memory)
        monkeypatch.setattr(maintenance_daemon, "replay_synthesis_index_jobs", replay)
        monkeypatch.setattr(maintenance_daemon, "run_audit", audit)

        report = await maintenance_daemon.run_governed_maintenance_cycle(object())

        assert calls == [
            "memory_lifecycle",
            "proposal_expiry",
            "synthesis_integrity",
            "memory_index_replay",
            "synthesis_index_replay",
            "audit",
        ]
        assert report["order"] == calls
        assert report["errors"] == {"memory_lifecycle": "RuntimeError"}

    def test_proposal_expiry_bridge_redacts_pending_plaintext(self):
        from daemons.maintenance_daemon import expire_pending_memory_proposals
        from plastic_promise.core.memory_proposals import (
            MemoryProposalStore,
            ProposalCandidate,
        )

        conn = sqlite3.connect(":memory:")
        store = MemoryProposalStore(conn)
        created_at = datetime.now(timezone.utc) - timedelta(days=8)
        proposal = store.create_many(
            [
                ProposalCandidate(
                    content="The user prefers concise technical explanations.",
                    category="preference",
                    project_id="project:test",
                    visibility="project",
                    origin_role="user",
                    origin_turn_hash="sha256:expired-daemon-turn",
                    origin_visibility="project",
                )
            ],
            now=created_at,
        )[0]
        engine = type("Engine", (), {"_sqlite": type("Storage", (), {"_conn": conn})()})()

        assert expire_pending_memory_proposals(engine) == {"expired": 1, "limit": 100}
        row = store.get(proposal["proposal_id"])
        assert row["status"] == "expired"
        assert row["content"] == ""
        conn.close()

    def test_dispatch_map_coverage(self):
        """_DISPATCH_MAP should cover all 4 agent types."""
        from daemons.maintenance_daemon import _DISPATCH_MAP

        assert set(_DISPATCH_MAP.keys()) == {"fixer", "reviewer", "builder", "claude"}
        for agent in ("fixer", "reviewer", "builder", "claude"):
            assert "assignee" in _DISPATCH_MAP[agent]
            assert "domain" in _DISPATCH_MAP[agent]


@pytest.mark.asyncio
async def test_category_stuck_uses_runtime_authority_and_counts_committed_results(
    monkeypatch, capsys
):
    from daemons import maintenance_daemon

    queries = []

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

        def fetchall(self):
            return self.value

    class Connection:
        def execute(self, statement, parameters=()):
            queries.append((statement, parameters))
            if "COUNT(1)" in statement and "llm_pending:true" in statement:
                assert parameters == ("project:daemon-reclassify",)
                return Result(6)
            if "COUNT(1)" in statement:
                assert parameters == ("project:daemon-reclassify",)
                return Result(21)
            if "SELECT id" in statement and "id > ?" in statement:
                assert parameters == ("project:daemon-reclassify", "memory-5")
                return Result([("memory-6",), ("memory-7",)])
            if "SELECT id" in statement:
                assert parameters == ("project:daemon-reclassify",)
                return Result([(f"memory-{index}",) for index in range(1, 6)])
            raise AssertionError(statement)

        def close(self):
            return None

    contexts = []
    async def reclassify(_engine, arguments, *, _runtime_context=None):
        contexts.append((arguments, _runtime_context))
        if arguments["memory_id"] == "memory-3":
            raise RuntimeError("isolated reclassification failure")
        return [SimpleNamespace(text=json.dumps({"reclassified": 1}))]

    monkeypatch.setenv("PLASTIC_PROJECT_ID", "project:daemon-reclassify")
    monkeypatch.setattr(maintenance_daemon, "_connect_memory_db", Connection)
    monkeypatch.setattr("plastic_promise.core.context_engine.ContextEngine", object)
    monkeypatch.setattr(
        "plastic_promise.mcp.server._mutation_runtime_context",
        lambda tool_name, arguments: {
            "tool_name": tool_name,
            "project_id": arguments["project_id"],
        },
    )
    monkeypatch.setattr(
        "plastic_promise.mcp.tools.memory.handle_memory_reclassify",
        reclassify,
    )
    maintenance_daemon._category_stuck_cursors.clear()

    await maintenance_daemon.scan_category_stuck()
    await maintenance_daemon.scan_category_stuck()

    assert contexts[0][0] == {
        "memory_id": "memory-1",
        "project_id": "project:daemon-reclassify",
    }
    assert contexts[5][0] == {
        "memory_id": "memory-6",
        "project_id": "project:daemon-reclassify",
    }
    assert maintenance_daemon._category_stuck_cursors == {}
    assert len(contexts) == 7
    assert len(queries) == 6
    assert all("project_id = ?" in statement for statement, _parameters in queries)
    assert "ORDER BY id ASC" in queries[2][0]
    assert "id > ?" in queries[5][0]
    assert all(
        entry[0]["project_id"] == "project:daemon-reclassify"
        and entry[1]["project_id"] == "project:daemon-reclassify"
        for entry in contexts
    )
    output = capsys.readouterr().out
    assert "reclassified 4 stale 'other' memories" in output
    assert "reclassified 2 stale 'other' memories" in output


@pytest.mark.asyncio
async def test_duplicate_cluster_cleanup_tombstones_through_coordinator_without_raw_delete(
    tmp_path,
    monkeypatch,
):
    from daemons import maintenance_daemon
    from plastic_promise.core import synthesis_maintenance
    from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
    from plastic_promise.core.synthesis import SynthesisStore, ensure_synthesis_schema
    from plastic_promise.core.synthesis_retrieval import read_memory_version

    db_path = tmp_path / "duplicate-cleanup.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    monkeypatch.setattr(
        synthesis_maintenance,
        "replay_memory_index_jobs",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        synthesis_maintenance,
        "replay_synthesis_index_jobs",
        lambda *_args, **_kwargs: 0,
    )

    storage = _SQLiteStorage(str(db_path))
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    duplicate_content = (
        "The same durable observation was imported twice from independent records. "
        "Only the higher-worth source should remain publicly available."
    )

    def source(
        memory_id,
        *,
        worth_success,
        worth_failure,
        origin_ref,
        content=duplicate_content,
    ):
        return {
            "id": memory_id,
            "content": content,
            "memory_type": "experience",
            "source": "test",
            "source_class": "experience",
            "project_id": "project:duplicate-cleanup",
            "visibility": "project",
            "tags": ["status:current"],
            "metadata_json": {"quality": {"status": "current"}},
            "created_at": "2026-07-12T00:00:00Z",
            "worth_success": worth_success,
            "worth_failure": worth_failure,
            "origin_kind": "document",
            "origin_uri": f"file:///{origin_ref}.md",
            "origin_ref": origin_ref,
            "origin_hash": f"sha256:{origin_ref}",
            "raw_content": content,
            "l0_abstract": "A duplicate durable observation.",
            "l1_summary": "- duplicate durable observation",
            "l2_content": content,
            "embedding_text": content,
            "embedding_hash": f"sha256:embedding-{origin_ref}",
            "search_text": content,
        }

    winner_id = "duplicate-winner"
    loser_id = "duplicate-loser"
    extra_loser_id = "duplicate-extra-loser"
    next_winner_id = "next-duplicate-winner"
    next_loser_id = "next-duplicate-loser"
    next_content = "A second live duplicate cluster must not be starved by tombstones."
    try:
        assert storage.upsert_ordinary(
            winner_id,
            source(winner_id, worth_success=9, worth_failure=1, origin_ref="winner"),
        )
        assert storage.upsert_ordinary(
            loser_id,
            source(loser_id, worth_success=1, worth_failure=9, origin_ref="loser"),
        )
        assert storage.upsert_ordinary(
            extra_loser_id,
            source(
                extra_loser_id,
                worth_success=2,
                worth_failure=8,
                origin_ref="extra-loser",
            ),
        )
        assert storage.upsert_ordinary(
            next_winner_id,
            source(
                next_winner_id,
                worth_success=8,
                worth_failure=1,
                origin_ref="next-winner",
                content=next_content,
            ),
        )
        assert storage.upsert_ordinary(
            next_loser_id,
            source(
                next_loser_id,
                worth_success=0,
                worth_failure=8,
                origin_ref="next-loser",
                content=next_content,
            ),
        )
        engine._memories = dict(storage.iter_all())
        engine._loaded_memory_version = read_memory_version(storage._conn)
        engine.canonical_sync_ok = True

        store = SynthesisStore(storage._conn, engine=engine)
        draft = store.create_draft(
            "The duplicate imports support one stable, independently reviewable conclusion.",
            [loser_id, winner_id],
            synthesis_key="duplicate-cleanup:dependent",
            validity_scope="project:duplicate-cleanup",
            project_id="project:duplicate-cleanup",
            visibility="project",
            actor="test",
            call_id="call:duplicate-cleanup-draft",
        )
        assert draft is not None
        verified = store.verify(
            draft.memory_id,
            "reviewer",
            "call:duplicate-cleanup-verify",
            draft.revision,
        )
        assert verified.status == "verified"

        daemon_sql = []

        def traced_daemon_connection():
            conn = sqlite3.connect(db_path)
            conn.set_trace_callback(daemon_sql.append)
            ensure_synthesis_schema(conn)
            conn.commit()
            return conn

        monkeypatch.setattr(
            maintenance_daemon,
            "_connect_memory_db",
            traced_daemon_connection,
        )

        await maintenance_daemon.scan_duplicate_clusters()

        normalized_sql = [" ".join(statement.split()) for statement in daemon_sql]
        direct_mutations = [
            statement
            for statement in normalized_sql
            if statement.casefold().startswith(("delete from memories", "update memories set"))
        ]
        assert not direct_mutations, f"daemon bypassed coordinator: {direct_mutations}"

        loser = storage.get(loser_id)
        assert loser is not None
        assert "status:forgotten" in loser["tags"]
        assert loser["metadata_json"]["quality"]["status"] == "forgotten"
        engine._refresh_canonical_cache_if_changed(force=True)
        assert engine.get_memory_dict(loser_id) is None
        assert engine.memory_exists(loser_id) is False

        dependent = store.get(verified.memory_id)
        assert dependent is not None
        assert dependent.status == "stale"
        assert dependent.stale_reason == "source_forgotten"

        first_loser_row = storage._conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (loser_id,),
        ).fetchone()
        first_loser_lineage = storage._conn.execute(
            "SELECT COUNT(*) FROM memory_lineage WHERE memory_id = ?",
            (loser_id,),
        ).fetchone()[0]

        def loser_job_count():
            return sum(
                json.loads(row[0]).get("memory_id") == loser_id
                for row in storage._conn.execute("SELECT payload_json FROM store_outbox").fetchall()
            )

        first_loser_jobs = loser_job_count()

        await maintenance_daemon.scan_duplicate_clusters()

        assert (
            storage._conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (loser_id,),
            ).fetchone()
            == first_loser_row
        )
        assert (
            storage._conn.execute(
                "SELECT COUNT(*) FROM memory_lineage WHERE memory_id = ?",
                (loser_id,),
            ).fetchone()[0]
            == first_loser_lineage
        )
        assert loser_job_count() == first_loser_jobs
        assert "status:forgotten" in storage.get(next_loser_id)["tags"]

        delete_jobs = []
        for tool_name, status, payload_json, metadata_json in storage._conn.execute(
            "SELECT tool_name, status, payload_json, metadata_json "
            "FROM store_outbox ORDER BY created_at, outbox_id"
        ).fetchall():
            payload = json.loads(payload_json)
            if payload.get("action") != "delete":
                continue
            delete_jobs.append((tool_name, status, payload, json.loads(metadata_json)))

        ordinary_jobs = [
            job
            for job in delete_jobs
            if job[0] == "memory_index" and job[2].get("memory_id") == loser_id
        ]
        synthesis_jobs = [
            job
            for job in delete_jobs
            if job[0] == "synthesis_index" and job[2].get("memory_id") == verified.memory_id
        ]
        assert [(job[1], job[3].get("job_schema")) for job in ordinary_jobs] == [
            ("pending", "memory-index/v3")
        ]
        assert [(job[1], job[3].get("job_schema")) for job in synthesis_jobs] == [
            ("pending", "synthesis-index/v1")
        ]
    finally:
        storage._conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "survivor_change",
    [
        "tombstone",
        "worth",
        "tier",
        "effective_half_life",
        "embedding_hash",
    ],
)
async def test_duplicate_cleanup_rejects_changed_survivor(
    tmp_path,
    monkeypatch,
    survivor_change,
):
    from daemons import maintenance_daemon
    from plastic_promise.core import synthesis_maintenance
    from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
    from plastic_promise.core.synthesis import ensure_synthesis_schema
    from plastic_promise.core.synthesis_retrieval import read_memory_version

    db_path = tmp_path / f"daemon-survivor-{survivor_change}.sqlite"
    monkeypatch.setattr(
        synthesis_maintenance,
        "replay_memory_index_jobs",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        synthesis_maintenance,
        "replay_synthesis_index_jobs",
        lambda *_args, **_kwargs: 0,
    )
    storage = _SQLiteStorage(str(db_path))
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    body = "Daemon duplicate selection must retain its observed live survivor."

    def source(memory_id, success, failure):
        return {
            "id": memory_id,
            "content": body,
            "memory_type": "experience",
            "project_id": "project:daemon-survivor",
            "visibility": "project",
            "tags": ["status:current"],
            "metadata_json": {"quality": {"status": "current"}},
            "created_at": "2026-07-12T00:00:00Z",
            "worth_success": success,
            "worth_failure": failure,
            "embedding_text": body,
            "embedding_hash": f"sha256:{memory_id}",
            "search_text": body,
        }

    survivor_id = "daemon-survivor"
    loser_id = "daemon-loser"
    try:
        assert storage.upsert_ordinary(survivor_id, source(survivor_id, 9, 0))
        assert storage.upsert_ordinary(loser_id, source(loser_id, 0, 9))

        def discovery_connection():
            conn = sqlite3.connect(db_path)
            ensure_synthesis_schema(conn)
            conn.commit()
            return conn

        monkeypatch.setattr(
            maintenance_daemon,
            "_connect_memory_db",
            discovery_connection,
        )
        before_loser = storage._conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (loser_id,),
        ).fetchone()
        before_version = read_memory_version(storage._conn)
        before_lineage = storage._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
        before_jobs = storage._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]

        class RacingEngine:
            def mutate_ordinary_source(self, memory_id, **mutation):
                if survivor_change == "tombstone":
                    storage._conn.execute(
                        "UPDATE memories SET tags = ?, metadata_json = ? WHERE id = ?",
                        (
                            json.dumps(["status:wrong"]),
                            json.dumps({"quality": {"status": "wrong"}}),
                            survivor_id,
                        ),
                    )
                elif survivor_change == "worth":
                    storage._conn.execute(
                        "UPDATE memories SET worth_success = 0, worth_failure = 100 WHERE id = ?",
                        (survivor_id,),
                    )
                elif survivor_change == "tier":
                    storage._conn.execute(
                        "UPDATE memories SET tier = 'L3' WHERE id = ?",
                        (survivor_id,),
                    )
                elif survivor_change == "effective_half_life":
                    storage._conn.execute(
                        "UPDATE memories SET effective_half_life = 365 WHERE id = ?",
                        (survivor_id,),
                    )
                else:
                    storage._conn.execute(
                        "UPDATE memories SET embedding_hash = ? WHERE id = ?",
                        ("sha256:changed-survivor", survivor_id),
                    )
                storage._conn.commit()
                return engine.mutate_ordinary_source(memory_id, **mutation)

        await maintenance_daemon.scan_duplicate_clusters(RacingEngine())

        assert (
            storage._conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (loser_id,),
            ).fetchone()
            == before_loser
        )
        assert read_memory_version(storage._conn) == before_version
        assert (
            storage._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
            == before_lineage
        )
        assert (
            storage._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0] == before_jobs
        )
    finally:
        storage._conn.close()


@pytest.mark.asyncio
async def test_duplicate_cleanup_preserves_fractional_worth_ranking(
    tmp_path,
    monkeypatch,
):
    from daemons import maintenance_daemon
    from plastic_promise.core import synthesis_maintenance
    from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
    from plastic_promise.core.synthesis import ensure_synthesis_schema
    from plastic_promise.core.synthesis_retrieval import _source_is_available

    db_path = tmp_path / "daemon-fractional-worth.sqlite"
    monkeypatch.setattr(
        synthesis_maintenance,
        "replay_memory_index_jobs",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        synthesis_maintenance,
        "replay_synthesis_index_jobs",
        lambda *_args, **_kwargs: 0,
    )
    storage = _SQLiteStorage(str(db_path))
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    body = "Fractional feedback must participate in duplicate survivor ranking."

    def source(memory_id, success, failure):
        return {
            "id": memory_id,
            "content": body,
            "memory_type": "experience",
            "project_id": "project:fractional-worth",
            "visibility": "project",
            "tags": ["status:current"],
            "metadata_json": {"quality": {"status": "current"}},
            "created_at": "2026-07-12T00:00:00Z",
            "worth_success": success,
            "worth_failure": failure,
            "embedding_text": body,
            "embedding_hash": f"sha256:{memory_id}",
            "search_text": body,
        }

    zero_score_id = "fractional-zero-score"
    survivor_id = "fractional-positive-score"
    try:
        assert storage.upsert_ordinary(zero_score_id, source(zero_score_id, 0, 0.5))
        assert storage.upsert_ordinary(survivor_id, source(survivor_id, 1, 2))
        engine._memories = dict(storage.iter_all())

        def discovery_connection():
            conn = sqlite3.connect(db_path)
            ensure_synthesis_schema(conn)
            conn.commit()
            return conn

        monkeypatch.setattr(
            maintenance_daemon,
            "_connect_memory_db",
            discovery_connection,
        )

        await maintenance_daemon.scan_duplicate_clusters(engine)

        assert _source_is_available(storage.get(zero_score_id)) is False
        assert _source_is_available(storage.get(survivor_id)) is True
    finally:
        storage._conn.close()


@pytest.mark.asyncio
async def test_duplicate_cleanup_weak_types_do_not_occupy_fixed_limit(
    tmp_path,
    monkeypatch,
):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage
    from plastic_promise.core.synthesis import ensure_synthesis_schema

    db_path = tmp_path / "daemon-weak-type-duplicates.sqlite"
    storage = _SQLiteStorage(str(db_path))
    columns = (
        "id",
        "content",
        "memory_type",
        "tier",
        "created_at",
        "last_accessed",
        "access_count",
        "worth_success",
        "worth_failure",
        "tags",
        "project_id",
        "metadata_json",
        "embedding_hash",
        "decay_multiplier",
        "effective_half_life",
    )
    now = "2026-07-12T00:00:00Z"
    rows = []
    for cluster_index in range(21):
        cluster = f"bad-ranking-{cluster_index:02d}"
        for member in range(3):
            access_count = 0 if cluster_index % 2 == 0 else "not-numeric"
            worth_success = "not-numeric" if cluster_index % 2 == 0 else member + 1
            rows.append(
                (
                    f"{cluster}-{member}",
                    f"{cluster}-content",
                    "experience",
                    "L1",
                    now,
                    now,
                    access_count,
                    worth_success,
                    1,
                    '["status:current"]',
                    f"project:{cluster}",
                    '{"quality":{"status":"current"}}',
                    f"sha256:{cluster}-{member}",
                    1.0,
                    3.0,
                )
            )
    for memory_id, success, failure in (
        ("valid-ranking-winner", 9, 1),
        ("valid-ranking-loser", 1, 9),
    ):
        rows.append(
            (
                memory_id,
                "valid-ranking-content",
                "experience",
                "L1",
                now,
                now,
                0,
                success,
                failure,
                '["status:current"]',
                "project:valid-ranking",
                '{"quality":{"status":"current"}}',
                f"sha256:{memory_id}",
                1.0,
                3.0,
            )
        )
    storage._conn.executemany(
        f"INSERT INTO memories ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _column in columns)})",
        rows,
    )
    storage._conn.commit()

    class MutationSpy:
        def __init__(self):
            self.mutations = []

        def mutate_ordinary_source(self, memory_id, **mutation):
            self.mutations.append({"memory_id": memory_id, **mutation})
            return {"memory_id": memory_id}

    engine = MutationSpy()

    def discovery_connection():
        conn = sqlite3.connect(db_path)
        ensure_synthesis_schema(conn)
        conn.commit()
        return conn

    monkeypatch.setattr(
        maintenance_daemon,
        "_connect_memory_db",
        discovery_connection,
    )
    try:
        await maintenance_daemon.scan_duplicate_clusters(engine)

        assert [mutation["memory_id"] for mutation in engine.mutations] == ["valid-ranking-loser"]
    finally:
        storage._conn.close()


def _llm_candidate(
    memory_id,
    *,
    content,
    project_id,
    created_at,
    tags=None,
    category="other",
):
    return {
        "id": memory_id,
        "content": content,
        "memory_type": "experience",
        "project_id": project_id,
        "visibility": "project",
        "category": category,
        "tags": tags if tags is not None else ["llm_pending:true", "cat:other"],
        "metadata_json": {"quality": {"status": "current"}},
        "embedding_text": content,
        "embedding_hash": f"sha256:{memory_id}",
        "search_text": content,
        "created_at": created_at,
    }


@pytest.mark.asyncio
async def test_llm_classification_filters_ineligible_and_other_project_rows(
    tmp_path,
    monkeypatch,
):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage
    from plastic_promise.core.synthesis import ensure_synthesis_schema

    db_path = tmp_path / "llm-eligibility.sqlite"
    cursor_path = tmp_path / "llm-eligibility-cursor.json"
    storage = _SQLiteStorage(str(db_path))
    candidates = [
        _llm_candidate(
            "a-other-project",
            content="other project",
            project_id="project:other",
            created_at="2026-01-01T00:00:00",
        ),
        _llm_candidate(
            "b-substring-tag",
            content="substring tag",
            project_id="project:classification",
            created_at="2026-01-01T00:00:01",
            tags=["prefix-llm_pending:true-suffix"],
        ),
        _llm_candidate(
            "c-malformed-tags",
            content="malformed tags",
            project_id="project:classification",
            created_at="2026-01-01T00:00:02",
        ),
        _llm_candidate(
            "d-empty-category",
            content="empty category",
            project_id="project:classification",
            created_at="2026-01-01T00:00:03",
            category="",
        ),
        _llm_candidate(
            "e-valid",
            content="valid candidate",
            project_id="project:classification",
            created_at="2026-01-01T00:00:04",
        ),
    ]
    events = []
    llm_calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, json, timeout):
            events.append(json)
            assert timeout == 3
            return Response()

    try:
        for candidate in candidates:
            assert storage.upsert_ordinary(candidate["id"], candidate)
        storage._conn.execute("UPDATE memories SET tags = 'not-json' WHERE id = 'c-malformed-tags'")
        storage._conn.commit()
        before = storage._conn.execute("SELECT * FROM memories ORDER BY id").fetchall()

        def discovery_connection():
            conn = sqlite3.connect(db_path)
            ensure_synthesis_schema(conn)
            conn.commit()
            return conn

        monkeypatch.setenv("PLASTIC_PROJECT_ID", "project:classification")
        monkeypatch.setenv("LLM_CLASSIFY_BATCH", "1")
        monkeypatch.setenv("PP_LLM_CLASSIFY_CURSOR_PATH", str(cursor_path))
        monkeypatch.setattr(maintenance_daemon, "DB_PATH", db_path)
        monkeypatch.setattr(maintenance_daemon, "_connect_memory_db", discovery_connection)
        monkeypatch.setattr(maintenance_daemon.httpx, "AsyncClient", Client)

        def classify(content, *_args, **_kwargs):
            llm_calls.append(content)
            return "decision"

        monkeypatch.setattr("plastic_promise.smart_extractor._llm_classify", classify)

        await maintenance_daemon.scan_llm_classify()

        assert llm_calls == ["valid candidate"]
        assert [event["memory_id"] for event in events] == ["e-valid"]
        assert storage._conn.execute("SELECT * FROM memories ORDER BY id").fetchall() == before
    finally:
        storage._conn.close()


@pytest.mark.asyncio
async def test_llm_classification_durable_cursor_advances_before_failures_and_wraps(
    tmp_path,
    monkeypatch,
):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage
    from plastic_promise.core.synthesis import ensure_synthesis_schema
    from plastic_promise.core.synthesis_retrieval import read_memory_version

    db_path = tmp_path / "llm-cursor.sqlite"
    cursor_path = tmp_path / "llm-cursor.json"
    storage = _SQLiteStorage(str(db_path))
    created_at = "2026-01-01T00:00:00"
    llm_calls = []
    post_calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, json, timeout):
            post_calls.append(json["memory_id"])
            if json["memory_id"] == "c-http-failure":
                raise RuntimeError("notify unavailable")
            return Response()

    try:
        for memory_id in ("a-none", "b-success", "c-http-failure"):
            assert storage.upsert_ordinary(
                memory_id,
                _llm_candidate(
                    memory_id,
                    content=memory_id,
                    project_id="project:classification",
                    created_at=created_at,
                ),
            )
        before = storage._conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
        before_version = read_memory_version(storage._conn)
        before_lineage = storage._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
        before_jobs = storage._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]

        def discovery_connection():
            conn = sqlite3.connect(db_path)
            ensure_synthesis_schema(conn)
            conn.commit()
            return conn

        monkeypatch.setenv("PLASTIC_PROJECT_ID", "project:classification")
        monkeypatch.setenv("LLM_CLASSIFY_BATCH", "1")
        monkeypatch.setenv("PP_LLM_CLASSIFY_CURSOR_PATH", str(cursor_path))
        monkeypatch.setattr(maintenance_daemon, "DB_PATH", db_path)
        monkeypatch.setattr(maintenance_daemon, "_connect_memory_db", discovery_connection)
        monkeypatch.setattr(maintenance_daemon.httpx, "AsyncClient", Client)

        def classify(content, *_args, **_kwargs):
            llm_calls.append(content)
            return None if content == "a-none" else "decision"

        monkeypatch.setattr("plastic_promise.smart_extractor._llm_classify", classify)

        for _ in range(4):
            await maintenance_daemon.scan_llm_classify()

        assert llm_calls == ["a-none", "b-success", "c-http-failure", "a-none"]
        assert post_calls == ["b-success", "c-http-failure"]
        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        assert cursor["version"] == "llm-classify-cursor/v1"
        assert cursor["project_id"] == "project:classification"
        assert cursor["cursor"] == {"created_at": created_at, "id": "a-none"}
        assert storage._conn.execute("SELECT * FROM memories ORDER BY id").fetchall() == before
        assert read_memory_version(storage._conn) == before_version
        assert (
            storage._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
            == before_lineage
        )
        assert (
            storage._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0] == before_jobs
        )
    finally:
        storage._conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_state", ["corrupt", "mismatched"])
async def test_llm_classification_invalid_cursor_resets_before_attempt(
    tmp_path,
    monkeypatch,
    cursor_state,
):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage
    from plastic_promise.core.synthesis import ensure_synthesis_schema

    db_path = tmp_path / f"llm-{cursor_state}.sqlite"
    cursor_path = tmp_path / f"llm-{cursor_state}-cursor.json"
    storage = _SQLiteStorage(str(db_path))
    memory_id = "candidate"
    try:
        assert storage.upsert_ordinary(
            memory_id,
            _llm_candidate(
                memory_id,
                content="candidate",
                project_id="project:classification",
                created_at="2026-01-01T00:00:00",
            ),
        )
        if cursor_state == "corrupt":
            cursor_path.write_text("{", encoding="utf-8")
        else:
            cursor_path.write_text(
                json.dumps(
                    {
                        "version": "llm-classify-cursor/v1",
                        "db_path": "different.sqlite",
                        "project_id": "project:other",
                        "cursor": {"created_at": "2099", "id": "later"},
                    }
                ),
                encoding="utf-8",
            )

        def discovery_connection():
            conn = sqlite3.connect(db_path)
            ensure_synthesis_schema(conn)
            conn.commit()
            return conn

        class NoPostClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                raise AssertionError("None classification must not notify")

        monkeypatch.setenv("PLASTIC_PROJECT_ID", "project:classification")
        monkeypatch.setenv("LLM_CLASSIFY_BATCH", "1")
        monkeypatch.setenv("PP_LLM_CLASSIFY_CURSOR_PATH", str(cursor_path))
        monkeypatch.setattr(maintenance_daemon, "DB_PATH", db_path)
        monkeypatch.setattr(maintenance_daemon, "_connect_memory_db", discovery_connection)
        monkeypatch.setattr(maintenance_daemon.httpx, "AsyncClient", NoPostClient)
        monkeypatch.setattr(
            "plastic_promise.smart_extractor._llm_classify",
            lambda *_args, **_kwargs: None,
        )

        await maintenance_daemon.scan_llm_classify()

        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        assert cursor["db_path"] == os.path.realpath(os.path.abspath(db_path))
        assert cursor["project_id"] == "project:classification"
        assert cursor["cursor"]["id"] == memory_id
    finally:
        storage._conn.close()


@pytest.mark.asyncio
async def test_llm_classification_unknown_runtime_project_is_zero_io(monkeypatch):
    from daemons import maintenance_daemon

    monkeypatch.delenv("PLASTIC_PROJECT_ID", raising=False)
    monkeypatch.delenv("PP_PROJECT_ID", raising=False)
    monkeypatch.setattr(
        maintenance_daemon,
        "_connect_memory_db",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be queried")),
    )
    monkeypatch.setattr(
        "plastic_promise.smart_extractor._llm_classify",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run")),
    )

    await maintenance_daemon.scan_llm_classify()


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_mode", ["deny", "http_failure"])
async def test_llm_classification_producer_is_zero_write_until_notify_commit(
    tmp_path,
    monkeypatch,
    notify_mode,
):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage
    from plastic_promise.core.synthesis import ensure_synthesis_schema, synthesis_content_hash
    from plastic_promise.core.synthesis_retrieval import read_memory_version

    db_path = tmp_path / f"llm-producer-{notify_mode}.sqlite"
    storage = _SQLiteStorage(str(db_path))
    memory_id = "llm-pending-source"
    content = "A classification candidate must remain unchanged until server commit."
    source = {
        "id": memory_id,
        "content": content,
        "memory_type": "experience",
        "project_id": "project:classification",
        "visibility": "project",
        "category": "other",
        "tags": ["keep", "llm_pending:true", "status:current"],
        "metadata_json": {"quality": {"status": "current"}},
        "embedding_text": content,
        "embedding_hash": "sha256:llm-pending",
        "search_text": content,
        "created_at": "2026-01-01T00:00:00",
    }
    events = []
    connect_calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": False, "reason": "runtime denied"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, json, timeout):
            events.append(json)
            assert timeout == 3
            if notify_mode == "http_failure":
                raise RuntimeError("notify unavailable")
            return Response()

    try:
        assert storage.upsert_ordinary(memory_id, source)
        monkeypatch.setenv("PLASTIC_PROJECT_ID", "project:classification")
        monkeypatch.setenv(
            "PP_LLM_CLASSIFY_CURSOR_PATH",
            str(tmp_path / f"llm-producer-{notify_mode}-cursor.json"),
        )

        def discovery_connection():
            connect_calls.append(True)
            conn = sqlite3.connect(db_path)
            ensure_synthesis_schema(conn)
            conn.commit()
            return conn

        monkeypatch.setattr(
            maintenance_daemon,
            "_connect_memory_db",
            discovery_connection,
        )
        monkeypatch.setattr(maintenance_daemon.httpx, "AsyncClient", Client)
        monkeypatch.setattr(
            "plastic_promise.smart_extractor._llm_classify",
            lambda *_args, **_kwargs: "decision",
        )
        before_row = storage._conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        before_version = read_memory_version(storage._conn)
        before_lineage = storage._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
        before_jobs = storage._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]

        await maintenance_daemon.scan_llm_classify()

        assert connect_calls == [True]
        assert len(events) == 1
        event = events[0]
        assert event["type"] == "llm_classified"
        assert event["memory_id"] == memory_id
        assert event["new_category"] == "decision"
        assert event["expected_project_id"] == "project:classification"
        assert event["expected_content_hash"] == synthesis_content_hash(content)
        assert event["expected_tags"] == source["tags"]
        assert event["expected_category"] == "other"
        assert "replacement_tags" not in event
        assert (
            storage._conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            == before_row
        )
        assert read_memory_version(storage._conn) == before_version
        assert (
            storage._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
            == before_lineage
        )
        assert (
            storage._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0] == before_jobs
        )
    finally:
        storage._conn.close()


@pytest.mark.integration
class TestSafetyNetIntegration:
    """Smoke tests that require MCP server running."""

    @pytest.mark.asyncio
    async def test_scan_duplicate_clusters_no_crash(self):
        """scan_duplicate_clusters should not raise."""
        from daemons.maintenance_daemon import scan_duplicate_clusters

        try:
            await scan_duplicate_clusters()
        except Exception as e:
            pytest.fail(f"scan_duplicate_clusters raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_stale_worth_no_crash(self):
        """scan_stale_worth should not raise."""
        from daemons.maintenance_daemon import scan_stale_worth

        try:
            await scan_stale_worth()
        except Exception as e:
            pytest.fail(f"scan_stale_worth raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_tier_migration_no_crash(self):
        """scan_tier_migration should not raise."""
        from daemons.maintenance_daemon import scan_tier_migration

        try:
            await scan_tier_migration()
        except Exception as e:
            pytest.fail(f"scan_tier_migration raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_orphan_steps_no_crash(self):
        """scan_orphan_steps should not raise when MCP is available."""
        from daemons.maintenance_daemon import scan_orphan_steps

        try:
            await scan_orphan_steps()
        except Exception as e:
            pytest.fail(f"scan_orphan_steps raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_unclosed_issues_no_crash(self):
        """scan_unclosed_issues should not raise when MCP is available."""
        from daemons.maintenance_daemon import scan_unclosed_issues

        try:
            await scan_unclosed_issues()
        except Exception as e:
            pytest.fail(f"scan_unclosed_issues raised: {e}")

    @pytest.mark.asyncio
    async def test_dispatch_multi_agent(self):
        """Verify dispatch works for all 4 agent types."""
        from daemons.maintenance_daemon import dispatch_fix_task

        for agent in ("fixer", "reviewer", "builder", "claude"):
            try:
                await dispatch_fix_task(
                    task_type="test_multi_agent",
                    detail=f"Smoke test dispatch → {agent}",
                    target_id=f"test-{agent}",
                    assignee=agent,
                    severity="info",
                )
            except Exception as e:
                pytest.fail(f"dispatch → {agent} raised: {e}")

    @pytest.mark.asyncio
    async def test_tag_for_redo_no_crash(self):
        """tag_for_redo should not raise."""
        from daemons.maintenance_daemon import tag_for_redo

        try:
            await tag_for_redo(
                memory_id="test-dummy-id",
                reason="测试打回区标记",
                assignee="reviewer",
                severity="info",
            )
        except Exception as e:
            pytest.fail(f"tag_for_redo raised: {e}")

    @pytest.mark.asyncio
    async def test_tag_audit_finding_no_crash(self):
        """tag_audit_finding should not raise."""
        from daemons.maintenance_daemon import tag_audit_finding

        try:
            await tag_audit_finding(
                dimension="memory_quality",
                detail="测试审计发现",
                severity="info",
            )
        except Exception as e:
            pytest.fail(f"tag_audit_finding raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_innovation_opportunities_no_crash(self):
        """scan_innovation_opportunities should not raise."""
        from daemons.maintenance_daemon import scan_innovation_opportunities

        try:
            await scan_innovation_opportunities()
        except Exception as e:
            pytest.fail(f"scan_innovation_opportunities raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_redo_queue_no_crash(self):
        """scan_redo_queue should not raise."""
        from daemons.maintenance_daemon import scan_redo_queue

        try:
            await scan_redo_queue()
        except Exception as e:
            pytest.fail(f"scan_redo_queue raised: {e}")
