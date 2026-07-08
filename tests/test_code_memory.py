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
