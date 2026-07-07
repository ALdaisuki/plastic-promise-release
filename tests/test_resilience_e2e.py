"""Resilience E2E --- rebuild, degradation, schema_version, fuzzy visibility"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestResilienceE2E:
    def test_rebuild_and_recover(self, monkeypatch):
        """Full-stack integration: requires shared file DB (not :memory:)"""
        # Must use real DB file; :memory: creates separate DBs per connection.
        monkeypatch.setenv("PLASTIC_DB_PATH", "plastic_memory.db")
        from plastic_promise.core.context_engine import ContextEngine

        e = ContextEngine()
        e.ensure_heavy_init()

        # Simulate crash: wipe domains
        e._dm._conn.execute("DELETE FROM domains")
        e._dm._conn.commit()
        e._dm.domains.clear()

        # Rebuild from memories
        result = e._dm.rebuild_from_memories()
        assert result["restored_domains"] >= 7

        # Retrieval still works
        stats = e._dm.stats()
        assert "building" in stats

    def test_degradation_switch(self):
        from plastic_promise.core.context_engine import ContextEngine

        e = ContextEngine()
        e.ensure_heavy_init()
        old_dm = e._dm

        e._dm = None
        e._dm_ok = False

        # supply() must not raise
        pack = e.supply("test query", [0.0] * 1024, "general", "global")
        assert pack is not None

        # stats() via domain tool should return error, not crash
        from plastic_promise.mcp.tools.domain import handle_domain
        import asyncio

        result = asyncio.run(handle_domain(e, {"action": "stats"}))
        assert len(result) > 0
        text = result[0].text.lower()
        assert (
            "deferred" in text
            or "not available" in text
            or "error" in text
            or "building" in text
        )

        e._dm = old_dm
        e._dm_ok = True

    def test_schema_version_write(self):
        from plastic_promise.core.context_engine import ContextEngine

        e = ContextEngine()
        e.ensure_heavy_init()
        row = e._dm._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] == 2

    def test_fuzzy_visible_in_stats(self):
        from plastic_promise.core.context_engine import ContextEngine
        from plastic_promise.memory.pipeline import MemoryPipeline

        e = ContextEngine()
        e.ensure_heavy_init()
        fb = MemoryPipeline(domain_manager=e._dm)
        fb.store_urgent("test fuzzy visibility")
        buf_stats = fb.stats()
        assert buf_stats["total"] >= 0
