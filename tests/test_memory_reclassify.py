# tests/test_memory_reclassify.py
"""Tests for memory_reclassify — bulk re-run classification pipeline on existing memories."""

import json
import asyncio
from plastic_promise.core.context_engine import ContextEngine


async def _call(engine, args):
    from plastic_promise.mcp.tools.reclassify import handle_memory_reclassify

    r = await handle_memory_reclassify(engine, args)
    return json.loads(r[0].text)


class TestMemoryReclassify:
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
            for mid, mem in engine._memories.items():
                if "domain:reflecting" in mem.get("tags", []):
                    found_reflecting_tag = True
                    break
            assert found_reflecting_tag, "domain:reflecting tag should be preserved after reclassify"

            # Verify content is still present after in-place reclassify.
            found_content = False
            for mid, mem in engine._memories.items():
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

            fb = _get_fuzzy_buffer(engine)
            r = await handle_memory_store(
                engine,
                {
                    "content": "worth test content",
                    "tags": ["domain:building"],
                    "max_llm_calls": 0,
                },
            )
            # Manually set worth values to simulate history
            for mid, mem in engine._memories.items():
                if "worth test" in mem.get("content", ""):
                    mem["worth_success"] = 5
                    mem["worth_failure"] = 2

            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] + result["skipped"] >= 1

            # Worth counters stay on the same memory during in-place reclassify.
            found_history = False
            for mid, mem in engine._memories.items():
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

            fb = _get_fuzzy_buffer(engine)
            # Store 5 memories
            for i in range(5):
                await handle_memory_store(
                    engine,
                    {
                        "content": f"batch test {i}",
                        "tags": ["domain:reflecting"],
                        "max_llm_calls": 0,
                    },
                )
            result = await _call(engine, {"batch_size": 2})
            assert result["reclassified"] + result["skipped"] == 2
            assert result["remaining"] >= 3

        asyncio.run(run())
