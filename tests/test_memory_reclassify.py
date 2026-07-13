# tests/test_memory_reclassify.py
"""Tests for memory_reclassify — bulk re-run classification pipeline on existing memories."""

import asyncio
import json

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.memory_index import build_index_material, metadata_with_index_material
from plastic_promise.core.synthesis_retrieval import read_memory_version


def _runtime(project_id, **overrides):
    runtime = {
        "actor": "codex",
        "call_id": "call:memory-reclassify-test",
        "project_id": project_id,
        "trust_score": 0.95,
        "trust_tier": "high",
        "defense_decision": "allow",
    }
    runtime.update(overrides)
    return runtime


async def _call(engine, args, runtime_context=None):
    from plastic_promise.mcp.tools.reclassify import handle_memory_reclassify

    if runtime_context is None:
        project_id = str(args.get("project_id") or "")
        if not project_id:
            project_id = next(
                (
                    str(memory.get("project_id") or "")
                    for memory in engine.iter_memories()
                    if memory.get("project_id")
                ),
                "project:reclassify",
            )
        runtime_context = _runtime(project_id)
    r = await handle_memory_reclassify(engine, args, _runtime_context=runtime_context)
    return json.loads(r[0].text)


def _seed_indexed_ordinary(engine, memory_id="reclassify-ordinary"):
    content = "The team decided to reclassify this durable project decision."
    material = build_index_material(
        {"content": content},
        policy="legacy",
        model_name="reclassify-test",
    )
    engine.create_ordinary_if_absent(
        {
            "id": memory_id,
            "content": content,
            "memory_type": "experience",
            "source": "test",
            "tier": "L1",
            "category": "other",
            "tags": ["domain:building"],
            "domain": "building",
            "project_id": "project:reclassify",
            "visibility": "project",
            "source_class": "experience",
            "metadata_json": metadata_with_index_material({}, material),
            "embedding_text": material.vector_text,
            "embedding_hash": material.embedding_hash,
            "search_text": material.search_text,
        }
    )
    return memory_id


class TestMemoryReclassify:
    @pytest.mark.parametrize("batch_size", [0, -1, True, 1001, "5"])
    def test_reclassify_rejects_invalid_batch_size(self, batch_size):
        async def run():
            engine = ContextEngine(use_sqlite=False)
            _seed_indexed_ordinary(engine)

            result = await _call(
                engine,
                {"batch_size": batch_size},
                _runtime("project:reclassify"),
            )

            assert result["committed"] is False
            assert result["reason"] == "memory_reclassify_batch_size_invalid"

        asyncio.run(run())

    @pytest.mark.parametrize("resume_from", [-1, True, "memory-id"])
    def test_reclassify_rejects_invalid_resume_offset(self, resume_from):
        async def run():
            engine = ContextEngine(use_sqlite=False)
            _seed_indexed_ordinary(engine)

            result = await _call(
                engine,
                {"resume_from": resume_from},
                _runtime("project:reclassify"),
            )

            assert result["committed"] is False
            assert result["reason"] == "memory_reclassify_resume_from_invalid"

        asyncio.run(run())

    def test_reclassify_resume_cursor_advances_past_completed_batch(self):
        async def run():
            engine = ContextEngine(use_sqlite=False)
            for suffix in ("a", "b", "c"):
                _seed_indexed_ordinary(engine, f"reclassify-cursor-{suffix}")

            first = await _call(
                engine,
                {"batch_size": 1},
                _runtime("project:reclassify"),
            )
            second = await _call(
                engine,
                {"batch_size": 1, "resume_from": first["next_resume_from"]},
                _runtime("project:reclassify"),
            )
            third = await _call(
                engine,
                {"batch_size": 1, "resume_from": second["next_resume_from"]},
                _runtime("project:reclassify"),
            )
            exhausted = await _call(
                engine,
                {"batch_size": 1, "resume_from": third["next_resume_from"]},
                _runtime("project:reclassify"),
            )

            assert first["last_id"] == "reclassify-cursor-a"
            assert first["next_resume_from"] == 1
            assert first["remaining"] == 2
            assert second["last_id"] == "reclassify-cursor-b"
            assert second["remaining"] == 1
            assert third["last_id"] == "reclassify-cursor-c"
            assert third["next_resume_from"] == 3
            assert third["remaining"] == 0
            assert exhausted["last_id"] is None
            assert exhausted["next_resume_from"] == 3
            assert exhausted["remaining"] == 0
            assert exhausted["committed"] is False

        asyncio.run(run())

    def test_reclassify_denied_runtime_is_zero_write(self):
        async def run():
            engine = ContextEngine(use_sqlite=False)
            memory_id = _seed_indexed_ordinary(engine, "reclassify-denied")
            before = dict(engine._memories[memory_id])

            result = await _call(
                engine,
                {"memory_id": memory_id},
                _runtime(
                    "project:reclassify",
                    trust_score=0.1,
                    trust_tier="low",
                    defense_decision="deny",
                ),
            )

            assert result["reason"] == "memory_reclassify_runtime_authorization_denied"
            assert result["reclassified"] == 0
            assert engine._memories[memory_id] == before

        asyncio.run(run())

    def test_reclassify_default_scope_skips_foreign_project(self):
        async def run():
            engine = ContextEngine(use_sqlite=False)
            local_id = _seed_indexed_ordinary(engine, "reclassify-local")
            foreign_id = _seed_indexed_ordinary(engine, "reclassify-foreign")
            engine._memories[foreign_id]["project_id"] = "project:foreign"
            foreign_before = dict(engine._memories[foreign_id])

            result = await _call(
                engine,
                {"batch_size": 10},
                _runtime("project:reclassify"),
            )

            assert result["reclassified"] == 1
            assert engine._memories[local_id]["category"] == "decision"
            assert engine._memories[foreign_id] == foreign_before

        asyncio.run(run())

    def test_reclassify_empty_pool(self):
        """Empty memory pool returns 0 reclassified."""

        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None  # disable LanceDB to avoid dedup interference
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer

            _get_fuzzy_buffer(engine)
            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] == 0
            assert result["remaining"] == 0

        asyncio.run(run())

    def test_reclassify_single_memory_preserves_content(self):
        """After reclassify, content and domain tag provenance are unchanged."""

        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer

            fb = _get_fuzzy_buffer(engine)
            content = (
                "test content for reclassify with enough reflecting audit detail "
                "to pass the memory quality gate deterministically"
            )
            # Store a memory with domain:reflecting tag
            fb.store_urgent(
                content=content,
                entity_ids=["skill:test:1"],
                custom_tags=["domain:reflecting", "task:done"],
                domain_hint="reflecting",
                max_llm_calls=0,
            )
            fb.process_pipeline()
            # In lightweight ContextEngine, DomainManager is not initialized.
            # Key assertion: reclassify re-processes in place and preserves traceable tags.

            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] + result["skipped"] >= 1

            # Verify domain provenance tag is still present after in-place reclassify.
            found_reflecting_tag = False
            for _mid, mem in engine._memories.items():
                if "domain:reflecting" in mem.get("tags", []):
                    found_reflecting_tag = True
                    break
            assert found_reflecting_tag, (
                "domain:reflecting tag should be preserved after reclassify"
            )

            # Verify content is still present after in-place reclassify.
            found_content = False
            for _mid, mem in engine._memories.items():
                if mem.get("content") == content:
                    found_content = True
                    break
            assert found_content, "Memory content should be preserved during in-place reclassify"

        asyncio.run(run())

    def test_reclassify_preserves_worth_history(self):
        """Old memory worth history preserved in metadata.worth_history after reclassify."""

        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer, handle_memory_store

            _get_fuzzy_buffer(engine)
            await handle_memory_store(
                engine,
                {
                    "content": "worth test content",
                    "tags": ["domain:building"],
                    "max_llm_calls": 0,
                    "project_id": "project:reclassify",
                },
            )
            # Manually set worth values to simulate history
            for _mid, mem in engine._memories.items():
                if "worth test" in mem.get("content", ""):
                    mem["worth_success"] = 5
                    mem["worth_failure"] = 2

            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] + result["skipped"] >= 1

            # Worth counters stay on the same memory during in-place reclassify.
            found_history = False
            for _mid, mem in engine._memories.items():
                if "worth test" in mem.get("content", ""):
                    assert mem["worth_success"] == 5
                    assert mem["worth_failure"] == 2
                    found_history = True
                    break
            assert found_history, "worth history not preserved on memory"

        asyncio.run(run())

    def test_reclassify_batch_respects_limit(self):
        """batch_size limits single-run count; remaining reflects leftovers."""

        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer, handle_memory_store

            _get_fuzzy_buffer(engine)
            # Store 5 memories
            for i in range(5):
                await handle_memory_store(
                    engine,
                    {
                        "content": f"batch test {i}",
                        "tags": ["domain:reflecting"],
                        "max_llm_calls": 0,
                        "project_id": "project:reclassify",
                    },
                )
            result = await _call(engine, {"batch_size": 2})
            assert result["reclassified"] + result["skipped"] == 2
            assert result["remaining"] >= 3

        asyncio.run(run())

    def test_reclassify_sqlite_patch_and_v3_index_upsert_commit_together(
        self, tmp_path, monkeypatch
    ):
        async def run():
            monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "reclassify-index.db"))
            engine = ContextEngine(use_sqlite=True)
            try:
                memory_id = _seed_indexed_ordinary(engine)
                conn = engine._sqlite._conn
                conn.execute("DELETE FROM store_outbox")
                conn.commit()
                before_version = read_memory_version(conn)

                result = await _call(engine, {"memory_id": memory_id})

                assert result["reclassified"] == 1
                canonical = engine._sqlite.get(memory_id)
                assert canonical["category"] == "decision"
                assert "cat:decision" in canonical["tags"]
                assert read_memory_version(conn) == before_version + 1
                rows = conn.execute(
                    "SELECT status, payload_json, metadata_json FROM store_outbox "
                    "WHERE tool_name = 'memory_index'"
                ).fetchall()
                assert len(rows) == 1
                status, raw_payload, raw_metadata = rows[0]
                payload = json.loads(raw_payload)
                assert status == "pending"
                assert payload["action"] == "upsert"
                assert payload["memory_id"] == memory_id
                assert payload["project_id"] == canonical["project_id"]
                assert payload["expected_embedding_hash"] == canonical["embedding_hash"]
                assert json.loads(raw_metadata)["job_schema"] == "memory-index/v3"
            finally:
                engine._sqlite._conn.close()

        asyncio.run(run())

    def test_reclassify_sqlite_rolls_back_patch_version_and_outbox_on_enqueue_failure(
        self, tmp_path, monkeypatch
    ):
        async def run():
            from plastic_promise.core import traceability

            monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "reclassify-rollback.db"))
            engine = ContextEngine(use_sqlite=True)
            try:
                memory_id = _seed_indexed_ordinary(engine)
                conn = engine._sqlite._conn
                conn.execute("DELETE FROM store_outbox")
                conn.commit()
                before = engine._sqlite.get(memory_id)
                before_version = read_memory_version(conn)

                def fail_enqueue(*_args, **_kwargs):
                    raise RuntimeError("injected reclassify enqueue failure")

                monkeypatch.setattr(traceability, "enqueue_memory_index_upsert", fail_enqueue)
                result = await _call(engine, {"memory_id": memory_id})

                assert result["errors"] == 1
                assert result["reclassified"] == 0
                assert engine._sqlite.get(memory_id) == before
                assert read_memory_version(conn) == before_version
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM store_outbox WHERE tool_name = 'memory_index'"
                    ).fetchone()[0]
                    == 0
                )
            finally:
                engine._sqlite._conn.close()

        asyncio.run(run())

    def test_reclassify_in_memory_uses_public_narrow_patch_api(self, monkeypatch):
        async def run():
            engine = ContextEngine(use_sqlite=False)
            memory_id = _seed_indexed_ordinary(engine, "reclassify-in-memory")
            calls = []
            public_patch = engine.patch_ordinary_memory

            def observe_public_patch(*args, **kwargs):
                calls.append((args, dict(kwargs)))
                return public_patch(*args, **kwargs)

            monkeypatch.setattr(engine, "patch_ordinary_memory", observe_public_patch)
            result = await _call(engine, {"memory_id": memory_id})

            assert result["reclassified"] == 1
            assert len(calls) == 1
            assert calls[0][0] == (memory_id,)
            assert calls[0][1]["index_upsert_call_id"].startswith("memory-reclassify:")
            assert engine._memories[memory_id]["category"] == "decision"

        asyncio.run(run())

    @pytest.mark.parametrize("reservation_kind", ["type", "control"])
    def test_reclassify_skips_governed_synthesis_without_sql_fallback(
        self,
        tmp_path,
        reservation_kind,
    ):
        async def run():
            from plastic_promise.core.context_engine import _SQLiteStorage

            storage = _SQLiteStorage(str(tmp_path / f"reclassify-{reservation_kind}.db"))
            memory_id = "governed-reclassify"
            storage.upsert(
                memory_id,
                {
                    "id": memory_id,
                    "content": "governed synthesis must not be reclassified",
                    "memory_type": "synthesis" if reservation_kind == "type" else "experience",
                    "source": "synthesis",
                    "source_class": "synthesis",
                    "tags": ["domain:building"],
                },
            )
            if reservation_kind == "control":
                storage._conn.execute(
                    "INSERT INTO synthesis_artifacts "
                    "(memory_id, synthesis_key, status, metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, 'draft', '{}', ?, ?)",
                    (memory_id, "reclassify:control", "2026-07-10", "2026-07-10"),
                )
                storage._conn.commit()
            engine = ContextEngine(use_sqlite=False)
            engine._sqlite = storage
            engine._memories = dict(storage.iter_all())
            engine._loaded_memory_version = storage._conn.execute(
                "SELECT version FROM memory_version"
            ).fetchone()[0]
            engine.canonical_sync_ok = True
            before = storage.get(memory_id)
            statements = []
            storage._conn.set_trace_callback(statements.append)

            result = await _call(engine, {"batch_size": 10})

            assert result["reclassified"] == 0
            assert storage.get(memory_id) == before
            assert not any("UPDATE memories" in statement.upper() for statement in statements)
            storage._conn.close()

        asyncio.run(run())
