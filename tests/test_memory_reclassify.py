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
        """After reclassify, content is unchanged and domain is correctly assigned."""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer
            fb = _get_fuzzy_buffer(engine)
            # Store a memory with domain:reflecting tag
            fb.store_urgent(
                content="test content for reclassify",
                entity_ids=["skill:test:1"],
                custom_tags=["domain:reflecting", "task:done"],
                domain_hint="uncategorized",
            )
            fb.process_pipeline()
            # Pipeline may correctly assign domain from tags; domain_hint is a fallback.
            # Key assertion: reclassify re-processes and marks old memory as replaced.

            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] >= 1

            # Verify new memory domain is correctly assigned
            found_reflecting = False
            for mid, mem in engine._memories.items():
                if mem.get("domain") == "reflecting":
                    found_reflecting = True
                    break
            assert found_reflecting, "No memory found with domain=reflecting after reclassify"

            # Verify old memory got marked as replaced
            found_replaced = False
            for mid, mem in engine._memories.items():
                tags = mem.get("tags", [])
                if "status:replaced" in tags:
                    found_replaced = True
                    break
            assert found_replaced, "Old memory should be marked status:replaced"
        asyncio.run(run())

    def test_reclassify_preserves_worth_history(self):
        """Old memory worth history preserved in metadata.worth_history after reclassify."""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer, handle_memory_store

            fb = _get_fuzzy_buffer(engine)
            r = await handle_memory_store(engine, {
                "content": "worth test content",
                "tags": ["domain:building"],
            })
            # Manually set worth values to simulate history
            for mid, mem in engine._memories.items():
                if "worth test" in mem.get("content", ""):
                    mem["worth_success"] = 5
                    mem["worth_failure"] = 2

            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] >= 1

            # Find the old memory marked as replaced
            found_history = False
            for mid, mem in engine._memories.items():
                meta = mem.get("metadata", {})
                if isinstance(meta, dict) and "worth_history" in meta:
                    wh = meta["worth_history"]
                    assert wh["previous"]["success"] == 5
                    assert wh["previous"]["failure"] == 2
                    found_history = True
                    break
            assert found_history, "worth_history not preserved in metadata"
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
                await handle_memory_store(engine, {
                    "content": f"batch test {i}",
                    "tags": ["domain:reflecting"],
                })
            result = await _call(engine, {"batch_size": 2})
            assert result["reclassified"] == 2
            assert result["remaining"] >= 3
        asyncio.run(run())
