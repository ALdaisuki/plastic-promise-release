"""Static import-boundary checks for the deployment contract layer."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_FORBIDDEN_PREFIXES = (
    "plastic_promise.control_plane",
    "plastic_promise.core",
    "plastic_promise.knowledge",
    "plastic_promise.launcher",
    "plastic_promise.mcp",
    "plastic_promise.memory",
    "plastic_promise.passive_memory",
    "plastic_promise.skills",
)
_DEPLOYMENT_PACKAGE = "plastic_promise.deployment"


@dataclass(frozen=True)
class ModuleLayerViolation:
    """One source-level dependency that breaches the deployment boundary."""

    path: Path
    line: int
    kind: str
    target: str


def _is_forbidden(target: str) -> bool:
    return any(
        target == prefix or target.startswith(f"{prefix}.") for prefix in _FORBIDDEN_PREFIXES
    )


class _DeploymentImportVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, *, package_parts: tuple[str, ...]) -> None:
        self.path = path
        self.package_parts = ("plastic_promise", "deployment", *package_parts)
        self.violations: list[ModuleLayerViolation] = []
        self.importlib_aliases = {"importlib"}
        self.import_module_aliases = {"import_module"}
        self.local_imports: set[str] = set()

    def _record(self, node: ast.AST, kind: str, target: str) -> None:
        if _is_forbidden(target):
            self.violations.append(
                ModuleLayerViolation(
                    path=self.path,
                    line=getattr(node, "lineno", 0),
                    kind=kind,
                    target=target,
                )
            )

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name == "importlib":
                self.importlib_aliases.add(alias.asname or alias.name)
            self._record(node, "import", alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.level == 0 and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    self.import_module_aliases.add(alias.asname or alias.name)
        if node.level == 0 and node.module:
            self._record(node, "import", node.module)
            if node.module == "plastic_promise.deployment":
                self.local_imports.update(alias.name for alias in node.names)
            elif node.module.startswith("plastic_promise.deployment."):
                self.local_imports.add(node.module.removeprefix("plastic_promise.deployment."))
        elif node.level > 0:
            for target in self._relative_import_targets(node):
                self._record(node, "import", target)
                if target.startswith(f"{_DEPLOYMENT_PACKAGE}."):
                    self.local_imports.add(
                        target.removeprefix(f"{_DEPLOYMENT_PACKAGE}.").replace(".", "/")
                    )
        self.generic_visit(node)

    def _relative_import_targets(self, node: ast.ImportFrom) -> set[str]:
        """Resolve local ``from .`` targets to graph module identifiers."""

        parent_levels = node.level - 1
        if parent_levels >= len(self.package_parts):
            return set()
        base_parts = self.package_parts[: len(self.package_parts) - parent_levels]
        if node.module:
            return {".".join((*base_parts, *node.module.split(".")))}
        return {".".join((*base_parts, alias.name)) for alias in node.names if alias.name != "*"}

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        function_name = _call_name(node.func, self.importlib_aliases)
        if function_name == "__import__" or function_name in self.import_module_aliases:
            target = _literal_string(node.args[0]) if node.args else None
            if target is None:
                self.violations.append(
                    ModuleLayerViolation(
                        path=self.path,
                        line=node.lineno,
                        kind="deferred-import-nonliteral",
                        target="<nonliteral>",
                    )
                )
            else:
                self._record(node, "deferred-import", target)
        self.generic_visit(node)


def _call_name(node: ast.AST, importlib_aliases: set[str]) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in importlib_aliases
        and node.attr == "import_module"
    ):
        return "import_module"
    return None


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def check_deployment_layering(repo_root: Path) -> tuple[ModuleLayerViolation, ...]:
    """Return deployment-layer violations, including literal delayed imports.

    The deployment package must stay safe to load for planning and validation;
    it cannot initialise service, database, MCP, memory, or control-plane code.
    """

    deployment_dir = repo_root / "plastic_promise" / "deployment"
    violations: list[ModuleLayerViolation] = []
    dependency_graph: dict[str, set[str]] = {}
    for source_file in sorted(deployment_dir.rglob("*.py")):
        try:
            tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        except (OSError, SyntaxError) as exc:
            raise ValueError(f"module_layer_source_unreadable:{source_file}") from exc
        relative_source = source_file.relative_to(deployment_dir)
        visitor = _DeploymentImportVisitor(
            source_file.relative_to(repo_root),
            package_parts=relative_source.parent.parts,
        )
        visitor.visit(tree)
        violations.extend(visitor.violations)
        module_id = source_file.relative_to(deployment_dir).with_suffix("").as_posix()
        if module_id.endswith("/__init__"):
            module_id = module_id.removesuffix("/__init__")
        elif module_id == "__init__":
            module_id = ""
        dependency_graph[module_id] = visitor.local_imports
    violations.extend(_cycle_violations(dependency_graph, deployment_dir, repo_root))
    return tuple(violations)


def _cycle_violations(
    graph: dict[str, set[str]], deployment_dir: Path, repo_root: Path
) -> list[ModuleLayerViolation]:
    violations: list[ModuleLayerViolation] = []
    visited: set[str] = set()
    active: list[str] = []

    def visit(module_id: str) -> None:
        if module_id in active:
            start = active.index(module_id)
            cycle = active[start:] + [module_id]
            violations.append(
                ModuleLayerViolation(
                    path=(deployment_dir / f"{module_id}.py").relative_to(repo_root),
                    line=0,
                    kind="cycle",
                    target=" -> ".join(cycle),
                )
            )
            return
        if module_id in visited:
            return
        visited.add(module_id)
        active.append(module_id)
        for dependency in sorted(graph.get(module_id, ())):
            if dependency in graph:
                visit(dependency)
        active.pop()

    for module_id in sorted(graph):
        visit(module_id)
    return violations
