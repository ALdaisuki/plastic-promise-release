from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plastic_promise.core.behavior_graph import graph_edge, graph_node

SOURCE_KIND = "code_memory"
DEFAULT_EXCLUDES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".worktrees",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "tmp",
    "var",
    "worktrees",
}


@dataclass
class CodeIndex:
    root: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    files_scanned: int = 0

    def to_audit(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "root": self.root,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "evidence_count": len(self.evidence),
            "files_scanned": self.files_scanned,
        }


def build_code_index(root: str | Path, max_files: int = 400) -> CodeIndex:
    root_path = Path(root).resolve()
    index = CodeIndex(root=str(root_path))
    if not root_path.exists():
        return index

    known_symbols: dict[str, str] = {}
    python_files = _iter_files(root_path, {".py"}, max_files)
    markdown_files = _iter_files(root_path, {".md"}, max(0, max_files - len(python_files)))

    for path in python_files:
        _index_python_file(index, root_path, path, known_symbols)
    for path in markdown_files:
        _index_markdown_file(index, root_path, path, known_symbols)

    index.files_scanned = len(python_files) + len(markdown_files)
    _add_call_edges(index, known_symbols)
    return index


def search_code_index(index: CodeIndex, query: str, limit: int = 12) -> list[tuple[str, float, str, str]]:
    terms = _terms(query)
    results: list[tuple[str, float, str, str]] = []
    for item in index.evidence:
        haystack = " ".join(
            str(item.get(key, "")) for key in ("name", "path", "kind", "content")
        ).lower()
        if not haystack:
            continue
        matches = sum(1 for term in terms if term in haystack)
        if terms and matches == 0:
            continue
        base = {
            "mcp_tool": 0.82,
            "test": 0.78,
            "function": 0.76,
            "method": 0.76,
            "class": 0.74,
            "file": 0.62,
            "doc": 0.58,
        }.get(str(item.get("kind")), 0.55)
        score = min(0.95, base + matches * 0.04)
        content = (
            f"{item.get('kind')} {item.get('name')} in {item.get('path')}: "
            f"{item.get('content', '')}"
        )
        results.append((str(item["id"]), score, content, SOURCE_KIND))
    results.sort(key=lambda row: row[1], reverse=True)
    return results[:limit]


def _iter_files(root: Path, suffixes: set[str], max_files: int) -> list[Path]:
    if max_files <= 0:
        return []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= max_files:
            break
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if any(part in DEFAULT_EXCLUDES for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return files


def _index_python_file(
    index: CodeIndex, root: Path, path: Path, known_symbols: dict[str, str]
) -> None:
    rel = path.relative_to(root).as_posix()
    file_id = f"code:file:{rel}"
    text = path.read_text(encoding="utf-8", errors="ignore")
    index.nodes.append(
        graph_node(
            file_id,
            "file",
            rel,
            f"Python source file {rel}",
            source_kind=SOURCE_KIND,
            metadata={"path": rel, "language": "python", "symbol_kind": "file"},
        )
    )
    index.evidence.append(_evidence(file_id, "file", rel, rel, text.splitlines()[0:5]))

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return

    imports = _imports(tree)
    for module in imports:
        module_id = f"code:module:{module}"
        index.nodes.append(
            graph_node(
                module_id,
                "code_symbol",
                module,
                f"Imported module {module}",
                source_kind=SOURCE_KIND,
                metadata={"symbol_kind": "module"},
            )
        )
        index.edges.append(graph_edge(file_id, module_id, "imports", 0.55, source_kind=SOURCE_KIND))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            class_id = f"code:class:{rel}:{node.name}"
            known_symbols[node.name] = class_id
            index.nodes.append(
                graph_node(
                    class_id,
                    "class",
                    node.name,
                    _doc(node),
                    source_kind=SOURCE_KIND,
                    metadata={"path": rel, "lineno": node.lineno, "symbol_kind": "class"},
                )
            )
            index.edges.append(graph_edge(file_id, class_id, "contains", 0.8, source_kind=SOURCE_KIND))
            index.evidence.append(_evidence(class_id, "class", node.name, rel, [_doc(node)]))
            for base in node.bases:
                base_name = _call_name(base)
                if base_name:
                    base_id = f"code:class:{base_name}"
                    index.edges.append(
                        graph_edge(class_id, base_id, "inherits", 0.6, source_kind=SOURCE_KIND)
                    )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_name = f"{node.name}.{child.name}"
                    method_id = f"code:method:{rel}:{method_name}"
                    known_symbols[child.name] = method_id
                    known_symbols[method_name] = method_id
                    index.nodes.append(
                        graph_node(
                            method_id,
                            "method",
                            method_name,
                            _doc(child),
                            source_kind=SOURCE_KIND,
                            metadata={
                                "path": rel,
                                "lineno": child.lineno,
                                "symbol_kind": "method",
                                "class": node.name,
                                "calls": _calls(child),
                            },
                        )
                    )
                    index.edges.append(
                        graph_edge(class_id, method_id, "contains", 0.82, source_kind=SOURCE_KIND)
                    )
                    index.evidence.append(
                        _evidence(method_id, "method", method_name, rel, [_doc(child)])
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "test" if node.name.startswith("test_") or rel.startswith("tests/") else "function"
            node_id = f"code:{kind}:{rel}:{node.name}"
            known_symbols[node.name] = node_id
            index.nodes.append(
                graph_node(
                    node_id,
                    kind,
                    node.name,
                    _doc(node),
                    source_kind=SOURCE_KIND,
                    metadata={
                        "path": rel,
                        "lineno": node.lineno,
                        "symbol_kind": kind,
                        "calls": _calls(node),
                    },
                )
            )
            index.edges.append(graph_edge(file_id, node_id, "contains", 0.78, source_kind=SOURCE_KIND))
            if kind == "test":
                index.edges.append(graph_edge(node_id, file_id, "tests", 0.5, source_kind=SOURCE_KIND))
            index.evidence.append(_evidence(node_id, kind, node.name, rel, [_doc(node)]))
            if rel.startswith("plastic_promise/mcp/tools/") and node.name.startswith("handle_"):
                tool_name = node.name.removeprefix("handle_")
                tool_id = f"mcp_tool:{tool_name}"
                index.nodes.append(
                    graph_node(
                        tool_id,
                        "mcp_tool",
                        tool_name,
                        f"MCP tool handler {node.name}",
                        source_kind=SOURCE_KIND,
                        metadata={"handler": node.name, "path": rel},
                    )
                )
                index.edges.append(
                    graph_edge(node_id, tool_id, "exposes_tool", 0.9, source_kind=SOURCE_KIND)
                )
                index.evidence.append(_evidence(tool_id, "mcp_tool", tool_name, rel, [_doc(node)]))


def _index_markdown_file(
    index: CodeIndex, root: Path, path: Path, known_symbols: dict[str, str]
) -> None:
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="ignore")
    title = _markdown_title(text) or path.name
    doc_id = f"code:doc:{rel}"
    index.nodes.append(
        graph_node(
            doc_id,
            "doc",
            path.name,
            title,
            source_kind=SOURCE_KIND,
            metadata={"path": rel, "symbol_kind": "doc"},
        )
    )
    index.evidence.append(_evidence(doc_id, "doc", path.name, rel, text.splitlines()[:8]))
    lower_text = text.lower()
    for symbol_name, symbol_id in known_symbols.items():
        if len(symbol_name) >= 3 and symbol_name.lower() in lower_text:
            index.edges.append(graph_edge(doc_id, symbol_id, "documents", 0.62, source_kind=SOURCE_KIND))


def _add_call_edges(index: CodeIndex, known_symbols: dict[str, str]) -> None:
    for node in index.nodes:
        metadata = node.get("metadata", {})
        calls = metadata.get("calls", [])
        if not isinstance(calls, list):
            continue
        for call in calls:
            target = known_symbols.get(call)
            if target and target != node["id"]:
                index.edges.append(graph_edge(node["id"], target, "calls", 0.58, source_kind=SOURCE_KIND))


def _imports(tree: ast.AST) -> list[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return sorted(modules)


def _calls(node: ast.AST) -> list[str]:
    calls = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_name(child.func)
            if name:
                calls.append(name.split(".")[-1])
    return sorted(set(calls))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _doc(node: ast.AST) -> str:
    try:
        return ast.get_docstring(node) or ""
    except Exception:
        return ""


def _markdown_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _terms(query: str) -> list[str]:
    return [term.lower() for term in query.replace("_", " ").split() if len(term) >= 2]


def _evidence(
    item_id: str, kind: str, name: str, path: str, lines: list[str]
) -> dict[str, Any]:
    return {
        "id": item_id,
        "kind": kind,
        "name": name,
        "path": path,
        "content": " ".join(line.strip() for line in lines if line.strip())[:240],
    }
