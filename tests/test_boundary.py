"""CI guard: ensure no external code accesses engine._* private fields.

All access to ContextEngine internals (_memories, _graph_nodes,
_graph_edges, _sqlite, _ldb, _dm, _embedder) MUST go through
public methods defined in plastic_promise/core/context_engine.py.

This test uses AST-based scanning to catch violations at the syntax
level — it catches violations even before runtime, and does not
depend on the MCP server or any external service.
"""

import ast
import glob
import os


def test_no_underscore_access():
    """Verify no external code accesses engine._* fields.

    Scans production Python files under plastic_promise/ and daemons/ for
    Attribute nodes where the value is `engine` and the attribute starts with
    `_`, plus dynamic access to the governed node-runtime slots. The context
    engine itself is exempt because it owns its private state.

    This is an AST-level check — it only matches actual code,
    not comments or docstrings.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source_dirs = (
        os.path.join(project_root, "plastic_promise"),
        os.path.join(project_root, "daemons"),
    )

    if not all(os.path.isdir(path) for path in source_dirs):
        # Not running from the right directory; skip gracefully
        return

    violations = []

    for source_dir in source_dirs:
        for py_file in glob.glob(os.path.join(source_dir, "**", "*.py"), recursive=True):
            rel_path = os.path.relpath(py_file, project_root)

            # The engine itself is allowed to access its own internals.
            if rel_path == os.path.join("plastic_promise", "core", "context_engine.py"):
                continue

            try:
                with open(py_file, encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            try:
                tree = ast.parse(source, filename=py_file)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.attr, str)
                    and node.attr.startswith("_")
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "engine"
                ):
                    violations.append(f"{rel_path}:{node.lineno}: engine.{node.attr}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "engine"
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                    and node.args[1].value
                    in {"_memory_index_node_runtime", "_memory_index_node_runtime_status"}
                ):
                    violations.append(
                        f"{rel_path}:{node.lineno}: getattr(engine, {node.args[1].value!r})"
                    )

    if violations:
        msg = (
            f"Boundary violations found ({len(violations)}):\n"
            + "\n".join(violations[:20])
            + ("\n... (truncated)" if len(violations) > 20 else "")
            + "\n\nUse public methods on ContextEngine instead of "
            + "accessing engine._* fields directly."
        )
        raise AssertionError(msg)
