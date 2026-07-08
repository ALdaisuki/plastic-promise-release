from pathlib import Path

from plastic_promise.core.code_memory import build_code_index
from plastic_promise.core.context_engine import ContextEngine


def _write_sample_repo(root: Path) -> None:
    pkg = root / "sample_pkg"
    tests = root / "tests"
    docs = root / "docs"
    pkg.mkdir()
    tests.mkdir()
    docs.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "service.py").write_text(
        """
import json


class Service:
    def run(self, payload):
        return helper(payload)


def helper(payload):
    return json.dumps(payload)
""".lstrip(),
        encoding="utf-8",
    )
    (tests / "test_service.py").write_text(
        """
from sample_pkg.service import helper


def test_helper():
    assert helper({"ok": True})
""".lstrip(),
        encoding="utf-8",
    )
    (docs / "service.md").write_text("# Service\n\nDocuments Service.run.\n", encoding="utf-8")


def test_code_memory_indexes_files_symbols_tests_docs_and_edges(tmp_path):
    _write_sample_repo(tmp_path)

    index = build_code_index(tmp_path, max_files=20)
    node_ids = {node["id"] for node in index.nodes}
    edge_relations = {edge["relation"] for edge in index.edges}

    assert "code:file:sample_pkg/service.py" in node_ids
    assert any(node["type"] == "class" and node["name"] == "Service" for node in index.nodes)
    assert any(node["type"] == "method" and node["name"] == "Service.run" for node in index.nodes)
    assert any(node["type"] == "function" and node["name"] == "helper" for node in index.nodes)
    assert any(node["type"] == "test" and node["name"] == "test_helper" for node in index.nodes)
    assert any(node["type"] == "doc" and node["name"] == "service.md" for node in index.nodes)
    assert {"contains", "imports", "calls", "documents"}.issubset(edge_relations)


def test_code_memory_excludes_local_worktrees_by_default(tmp_path):
    _write_sample_repo(tmp_path)
    shadow = tmp_path / ".worktrees" / "stale" / "shadow_pkg"
    shadow.mkdir(parents=True)
    (shadow / "shadow.py").write_text(
        """
class ShadowService:
    def run(self):
        return "stale"
""".lstrip(),
        encoding="utf-8",
    )

    index = build_code_index(tmp_path, max_files=50)

    assert not any(".worktrees" in node["id"] for node in index.nodes)
    assert not any(node["name"] == "ShadowService" for node in index.nodes)


def test_context_supply_includes_read_only_code_memory_evidence(tmp_path, monkeypatch):
    _write_sample_repo(tmp_path)
    monkeypatch.setenv("PP_CODE_MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("PP_CODE_MEMORY_MAX_FILES", "20")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
    monkeypatch.setenv("PP_RERANK_DISABLED", "1")
    monkeypatch.setenv("PP_QUERY_EXPANSION", "0")

    engine = ContextEngine(use_sqlite=False)
    pack = engine.supply(
        "debug Service.run helper behavior",
        [0.0] * 1024,
        task_type="debugging",
        scope="global",
    )
    all_items = pack.core + pack.related + pack.divergent
    graph = engine.query_graph("full_graph")

    assert any(item.source == "code_memory" and "Service.run" in item.content for item in all_items)
    assert any(node.get("source_kind") == "code_memory" for node in graph["nodes"].values())
    assert any(edge.get("source_kind") == "code_memory" for edge in graph["edges"])
    assert pack.audit_metadata["code_memory"]["enabled"] is True
    assert pack.audit_metadata["code_memory"]["node_count"] >= 5


def test_context_supply_reports_canonical_hot_and_gate_debug_without_enforcement(
    tmp_path, monkeypatch
):
    tool_dir = tmp_path / "plastic_promise" / "mcp" / "tools"
    tool_dir.mkdir(parents=True)
    (tool_dir / "context.py").write_text(
        """
async def handle_context_supply(engine, args):
    '''Return task context.'''
    return []
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PP_CODE_MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("PP_CODE_MEMORY_MAX_FILES", "20")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
    monkeypatch.setenv("PP_RERANK_DISABLED", "1")
    monkeypatch.setenv("PP_QUERY_EXPANSION", "0")
    monkeypatch.setenv("PP_CANONICAL_HOT_LOOKUP", "1")
    monkeypatch.setenv("PP_CANONICAL_HOT_ENFORCE", "0")
    monkeypatch.setenv("PP_CONTEXT_GATE", "1")
    monkeypatch.setenv("PP_CONTEXT_GATE_ENFORCE", "0")

    engine = ContextEngine(use_sqlite=False)
    pack = engine.supply(
        "call context_supply before acting",
        [0.0] * 1024,
        task_type="general",
        scope="global",
        debug=True,
    )
    all_items = pack.core + pack.related + pack.divergent

    assert pack.audit_metadata["canonical_hot"]["enabled"] is True
    assert "mcp_tool:context_supply" in pack.audit_metadata["canonical_hot"]["keys"]
    assert pack.audit_metadata["context_gate"]["enabled"] is True
    assert pack.pipeline_stats["canonical_hot_count"] >= 1
    assert not any(item.id == "mcp_tool:context_supply" for item in all_items)


def test_context_supply_canonical_hot_respects_code_memory_disabled(
    tmp_path, monkeypatch
):
    tool_dir = tmp_path / "plastic_promise" / "mcp" / "tools"
    tool_dir.mkdir(parents=True)
    (tool_dir / "context.py").write_text(
        """
async def handle_context_supply(engine, args):
    '''Return task context.'''
    return []
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PP_CODE_MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("PP_CODE_MEMORY_MAX_FILES", "20")
    monkeypatch.setenv("PP_CODE_MEMORY_ENABLED", "0")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
    monkeypatch.setenv("PP_RERANK_DISABLED", "1")
    monkeypatch.setenv("PP_QUERY_EXPANSION", "0")
    monkeypatch.setenv("PP_CANONICAL_HOT_LOOKUP", "1")
    monkeypatch.setenv("PP_CANONICAL_HOT_ENFORCE", "0")
    monkeypatch.setenv("PP_CANONICAL_HOT_LIMIT", "not-an-int")

    engine = ContextEngine(use_sqlite=False)
    pack = engine.supply(
        "please call context_supply before acting",
        [0.0] * 1024,
        task_type="general",
        scope="global",
        debug=True,
    )

    assert pack.audit_metadata["code_memory"]["enabled"] is False
    assert pack.audit_metadata["canonical_hot"]["limit"] == 12
    assert "mcp_tool:context_supply" not in pack.audit_metadata["canonical_hot"]["keys"]


def test_context_supply_reports_gate_per_item_stats_for_layered_code_items(
    tmp_path, monkeypatch
):
    _write_sample_repo(tmp_path)
    monkeypatch.setenv("PP_CODE_MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("PP_CODE_MEMORY_MAX_FILES", "20")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
    monkeypatch.setenv("PP_RERANK_DISABLED", "1")
    monkeypatch.setenv("PP_QUERY_EXPANSION", "0")
    monkeypatch.setenv("PP_CONTEXT_GATE", "1")
    monkeypatch.setenv("PP_CONTEXT_GATE_ENFORCE", "0")

    engine = ContextEngine(use_sqlite=False)
    pack = engine.supply(
        "debug Service.run helper behavior",
        [0.0] * 1024,
        task_type="debugging",
        scope="global",
        debug=True,
    )

    gate_stats = [
        item for item in pack.per_item_stats if item.get("retrieval_source") == "code_memory"
    ]
    assert gate_stats
    assert all("gate_score" in item for item in gate_stats)
    assert all("gate_decision" in item for item in gate_stats)
    assert all("gate_reasons" in item for item in gate_stats)
