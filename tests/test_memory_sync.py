# tests/test_memory_sync.py
import json, os, tempfile
import asyncio
from plastic_promise.core.context_engine import ContextEngine

async def _call(engine, args):
    from plastic_promise.mcp.tools.sync import handle_memory_sync_files
    r = await handle_memory_sync_files(engine, args)
    return json.loads(r[0].text)

class TestMemorySyncFiles:
    def test_sync_empty_dir(self):
        """空目录返回 0 条同步。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                result = await _call(engine, {"source_dir": td})
                assert result["synced"] == 0
                assert result["skipped"] == 0
        asyncio.run(run())

    def test_sync_single_md(self):
        """单条 .md 正确解析 frontmatter 并存储。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                md_path = os.path.join(td, "test-memory.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write("""---
name: test-memory
description: "A test memory for sync"
metadata:
  type: reference
---

This is the memory body content.
""")
                result = await _call(engine, {"source_dir": td})
                assert result["synced"] == 1
                assert result["skipped"] == 0
                # Verify stored in engine
                found = False
                for mid, mem in engine._memories.items():
                    if "test-memory" in mem.get("content", ""):
                        found = True
                        assert "reference" in str(mem.get("tags", []))
                        break
                assert found, "Memory not stored in engine"
        asyncio.run(run())

    def test_skip_synced(self):
        """已标记 [[synced-to-mcp]] 的文件跳过。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                md_path = os.path.join(td, "already-synced.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write("""---
name: already-synced
description: "Already synced"
metadata:
  type: reference
---

Already synced. [[synced-to-mcp]]
""")
                result = await _call(engine, {"source_dir": td})
                assert result["skipped"] == 1
        asyncio.run(run())

    def test_skip_memory_system_primary_channel(self):
        """已标记 [[memory-system-primary-channel]] 的文件跳过（代表此记忆来自 MCP 系统）。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                md_path = os.path.join(td, "from-mcp.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write("""---
name: from-mcp
description: "From MCP system"
metadata:
  type: feedback
---

Already tracked. [[memory-system-primary-channel]]
""")
                result = await _call(engine, {"source_dir": td})
                assert result["skipped"] == 1
        asyncio.run(run())
