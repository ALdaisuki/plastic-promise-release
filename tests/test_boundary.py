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

    Scans all Python files under plastic_promise/ for Attribute
    nodes where the value is `engine` and the attribute starts
    with `_`. The context_engine.py file itself is exempt.

    This is an AST-level check — it only matches actual code,
    not comments or docstrings.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plastic_promise_dir = os.path.join(project_root, "plastic_promise")

    if not os.path.isdir(plastic_promise_dir):
        # Not running from the right directory; skip gracefully
        return

    violations = []

    for py_file in glob.glob(os.path.join(plastic_promise_dir, "**", "*.py"), recursive=True):
        rel_path = os.path.relpath(py_file, project_root)

        # The engine itself is allowed to access its own internals
        if rel_path.endswith("context_engine.py"):
            continue

        try:
            with open(py_file, "r", encoding="utf-8") as f:
                source = f.read()
        except (IOError, UnicodeDecodeError):
            continue

        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                # Check if this is engine._something
                if (
                    isinstance(node.attr, str)
                    and node.attr.startswith("_")
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "engine"
                ):
                    violations.append(f"{rel_path}:{node.lineno}: engine.{node.attr}")

    if violations:
        msg = (
            f"Boundary violations found ({len(violations)}):\n"
            + "\n".join(violations[:20])
            + ("\n... (truncated)" if len(violations) > 20 else "")
            + "\n\nUse public methods on ContextEngine instead of "
            + "accessing engine._* fields directly."
        )
        raise AssertionError(msg)
