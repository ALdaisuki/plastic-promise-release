# tests/test_memory_sync.py
import asyncio
import builtins
import json
import os
import tempfile
from pathlib import Path

from plastic_promise.core.context_engine import ContextEngine


def _runtime(source_dir, **overrides):
    runtime = {
        "actor": "codex",
        "call_id": "call:memory-sync-test",
        "project_id": "project:memory-sync",
        "trust_score": 0.95,
        "trust_tier": "high",
        "defense_decision": "allow",
        "allowed_source_roots": [str(Path(source_dir).resolve())],
    }
    runtime.update(overrides)
    return runtime


async def _call(engine, args, runtime_context=None):
    from plastic_promise.mcp.tools.sync import handle_memory_sync_files

    runtime_context = runtime_context or _runtime(args["source_dir"])
    r = await handle_memory_sync_files(engine, args, _runtime_context=runtime_context)
    return json.loads(r[0].text)


class TestMemorySyncFiles:
    def test_sync_marks_nested_memory_store_as_trusted(self, monkeypatch):
        from mcp.types import TextContent

        from plastic_promise.core.memory_proposals import has_trusted_internal_origin
        from plastic_promise.mcp.tools import memory as memory_tools

        monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
        observed = []

        async def capture_store(_engine, args):
            trusted = has_trusted_internal_origin(args)
            observed.append(
                {
                    "trusted": trusted,
                    "project_id": args.get("project_id"),
                    "project_policy": args.get("project_policy"),
                }
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "stored": trusted,
                            "status": "canonical" if trusted else "pending",
                            "memory_id": "mem_file_sync" if trusted else None,
                        }
                    ),
                )
            ]

        monkeypatch.setattr(memory_tools, "handle_memory_store", capture_store)

        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                md_path = os.path.join(td, "trusted-sync.md")
                with open(md_path, "w", encoding="utf-8") as handle:
                    handle.write(
                        "---\n"
                        "name: trusted-sync\n"
                        "description: Trusted file sync route\n"
                        "metadata:\n"
                        "  type: reference\n"
                        "---\n\n"
                        "Canonical file-backed memory.\n"
                    )
                result = await memory_tools.handle_memory_sync_files(
                    engine,
                    {"source_dir": td},
                    _runtime_context=_runtime(td),
                )
                return json.loads(result[0].text)

        payload = asyncio.run(run())

        assert observed == [
            {
                "trusted": True,
                "project_id": "project:memory-sync",
                "project_policy": "balanced",
            }
        ]
        assert payload["synced"] == 1
        assert payload["stored"] == 1
        assert payload["committed"] is True
        assert payload["errors"] == 0
        assert has_trusted_internal_origin({}) is False

    def test_sync_reports_partial_commit_when_marker_write_fails(self, monkeypatch, tmp_path):
        from mcp.types import TextContent

        from plastic_promise.mcp.tools import memory as memory_tools

        memory_file = tmp_path / "partial.md"
        original = "partial sync body\n"
        memory_file.write_text(original, encoding="utf-8")

        async def stored(_engine, _args):
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"stored": True, "memory_id": "memory:partial"}),
                )
            ]

        def fail_marker_write(path, mode="r", *args, **kwargs):
            if "w" in mode:
                raise OSError("marker write failed")
            return builtins.open(path, mode, *args, **kwargs)

        monkeypatch.setattr(memory_tools, "handle_memory_store", stored)
        monkeypatch.setattr(memory_tools, "open", fail_marker_write, raising=False)

        payload = asyncio.run(
            memory_tools.handle_memory_sync_files(
                ContextEngine(use_sqlite=False),
                {"source_dir": str(tmp_path)},
                _runtime_context=_runtime(tmp_path),
            )
        )
        result = json.loads(payload[0].text)

        assert result["stored"] == 1
        assert result["synced"] == 0
        assert result["errors"] == 1
        assert result["committed"] is True
        assert result["partial"] is True
        assert result["reason"] == "memory_sync_files_partial"
        assert memory_file.read_text(encoding="utf-8") == original

    def test_sync_denied_runtime_does_not_read_or_mark_files(self, tmp_path, monkeypatch):
        from plastic_promise.mcp.tools import memory as memory_tools

        source = tmp_path / "denied"
        source.mkdir()
        memory_file = source / "private.md"
        original = "private memory\n"
        memory_file.write_text(original, encoding="utf-8")
        store_calls = []

        async def capture_store(*_args, **_kwargs):
            store_calls.append(True)

        monkeypatch.setattr(memory_tools, "handle_memory_store", capture_store)

        payload = asyncio.run(
            _call(
                ContextEngine(use_sqlite=False),
                {"source_dir": str(source)},
                _runtime(
                    source,
                    trust_score=0.1,
                    trust_tier="low",
                    defense_decision="deny",
                ),
            )
        )

        assert payload["reason"] == "memory_sync_files_runtime_authorization_denied"
        assert payload["synced"] == 0
        assert store_calls == []
        assert memory_file.read_text(encoding="utf-8") == original

    def test_sync_rejects_source_outside_server_allowed_roots(self, tmp_path):
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        memory_file = outside / "outside.md"
        original = "outside memory\n"
        memory_file.write_text(original, encoding="utf-8")

        payload = asyncio.run(
            _call(
                ContextEngine(use_sqlite=False),
                {"source_dir": str(outside)},
                _runtime(outside, allowed_source_roots=[str(allowed)]),
            )
        )

        assert payload["reason"] == "memory_sync_source_not_allowed"
        assert payload["synced"] == 0
        assert memory_file.read_text(encoding="utf-8") == original

    def test_sync_rejects_missing_server_allowed_roots(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        memory_file = source / "memory.md"
        original = "must not be read\n"
        memory_file.write_text(original, encoding="utf-8")

        payload = asyncio.run(
            _call(
                ContextEngine(use_sqlite=False),
                {"source_dir": str(source)},
                _runtime(source, allowed_source_roots=None),
            )
        )

        assert payload["reason"] == "memory_sync_source_not_allowed"
        assert payload["synced"] == 0
        assert memory_file.read_text(encoding="utf-8") == original

    def test_sync_empty_dir(self):
        """空目录返回 0 条同步。"""

        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None  # disable LanceDB dedup in tests
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer

            _get_fuzzy_buffer(engine)
            with tempfile.TemporaryDirectory() as td:
                result = await _call(engine, {"source_dir": td})
                assert result["synced"] == 0
                assert result["stored"] == 0
                assert result["skipped"] == 0
                assert result["committed"] is False

        asyncio.run(run())

    def test_sync_single_md(self):
        """单条 .md 正确解析 frontmatter 并存储。"""

        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None  # disable LanceDB dedup in tests
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer

            _get_fuzzy_buffer(engine)
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
                for _mid, mem in engine._memories.items():
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
