from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = (
    ROOT / "docs" / "engineering-patterns" / "2026-07-12-ordinary-memory-caller-inventory.md"
)
PRODUCTION_ROOTS = ("plastic_promise", "daemons", "scripts")
WRITER_METHODS = frozenset(
    {
        "store_memory",
        "register_memory",
        "_persist_ordinary_memory",
        "upsert_ordinary",
        "mutate_ordinary_source",
        "patch_ordinary_memory",
        "update_memory",
        "update_memory_fields",
        "increment_field",
        "batch_update",
        "delete_memory",
        "_patch_ordinary_fields",
        "_patch_ordinary_tags",
    }
)
RAW_MEMORY_SQL = re.compile(
    r"\b(INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)"
    r"\s+memories\b",
    re.IGNORECASE,
)
INVENTORY_START = "<!-- writer-inventory:start -->"
INVENTORY_END = "<!-- writer-inventory:end -->"
INVENTORY_COLUMNS = (
    "path",
    "symbol",
    "writer",
    "occurrences",
    "existing_id",
    "current_semantics",
    "target_owner",
    "focused_test",
    "migration_task",
    "status",
)
ORDINARY_TARGET_OWNERS = frozenset(
    {
        "create_ordinary_if_absent",
        "patch_ordinary_memory",
        "ContextEngine.mutate_ordinary_source",
    }
)
NONORDINARY_SYNTHESIS_WRITERS = frozenset(
    {
        (
            "plastic_promise/core/synthesis.py",
            "SynthesisStore._insert_memory",
            "sql:INSERT INTO",
        ),
        (
            "plastic_promise/core/synthesis.py",
            "SynthesisStore._update_memory_for_refresh",
            "sql:UPDATE",
        ),
    }
)


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


class _WriterVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.writer_aliases: list[dict[str, str | None]] = [{}]
        self.writers: Counter[tuple[str, str, str]] = Counter()

    @property
    def symbol(self) -> str:
        return ".".join(self.scope) if self.scope else "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.writer_aliases.append({})
        self.generic_visit(node)
        self.writer_aliases.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.writer_aliases.append({})
        self.generic_visit(node)
        self.writer_aliases.pop()
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def _writer_from_alias_value(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute) and node.attr in WRITER_METHODS:
            return node.attr
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in WRITER_METHODS
        ):
            return node.args[1].value
        if isinstance(node, ast.Name):
            return self.writer_aliases[-1].get(node.id)
        return None

    def _bind_writer_alias(self, target: ast.AST, writer: str | None) -> None:
        if isinstance(target, ast.Name):
            self.writer_aliases[-1][target.id] = writer
        elif isinstance(target, (ast.List, ast.Tuple)):
            for element in target.elts:
                self._bind_writer_alias(element, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        writer = self._writer_from_alias_value(node.value)
        for target in node.targets:
            self._bind_writer_alias(target, writer)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        writer = self._writer_from_alias_value(node.value) if node.value is not None else None
        self._bind_writer_alias(node.target, writer)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind_writer_alias(node.target, None)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        called_method: str | None = None
        if isinstance(node.func, ast.Attribute):
            called_method = node.func.attr
        elif isinstance(node.func, ast.Name):
            called_method = self.writer_aliases[-1].get(node.func.id, node.func.id)
        if called_method in WRITER_METHODS:
            self.writers[(self.path, self.symbol, called_method)] += 1
        self.generic_visit(node)

    def _record_raw_sql(self, node: ast.AST) -> None:
        text = _static_string(node)
        if text is None:
            return
        for match in RAW_MEMORY_SQL.finditer(text):
            operation = " ".join(match.group(1).upper().split())
            self.writers[(self.path, self.symbol, f"sql:{operation}")] += 1

    def visit_Constant(self, node: ast.Constant) -> None:
        self._record_raw_sql(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._record_raw_sql(node)
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                self.visit(part)


def _production_writers() -> Counter[tuple[str, str, str]]:
    writers: Counter[tuple[str, str, str]] = Counter()
    for production_root in PRODUCTION_ROOTS:
        for path in sorted((ROOT / production_root).rglob("*.py")):
            relative_path = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative_path)
            visitor = _WriterVisitor(relative_path)
            visitor.visit(tree)
            writers.update(visitor.writers)
    return writers


def _table_cells(line: str) -> list[str]:
    return [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]


def _inventory_rows() -> dict[tuple[str, str, str], dict[str, str]]:
    assert INVENTORY_PATH.is_file(), f"inventory document missing: {INVENTORY_PATH}"
    document = INVENTORY_PATH.read_text(encoding="utf-8")
    assert INVENTORY_START in document and INVENTORY_END in document
    table = document.split(INVENTORY_START, 1)[1].split(INVENTORY_END, 1)[0]
    lines = [line for line in table.splitlines() if line.strip().startswith("|")]
    assert len(lines) >= 3, "writer inventory table is empty"
    assert tuple(_table_cells(lines[0])) == INVENTORY_COLUMNS

    rows: dict[tuple[str, str, str], dict[str, str]] = {}
    for line in lines[2:]:
        values = _table_cells(line)
        assert len(values) == len(INVENTORY_COLUMNS), f"malformed inventory row: {line}"
        row = dict(zip(INVENTORY_COLUMNS, values, strict=True))
        key = (row["path"], row["symbol"], row["writer"])
        assert key not in rows, f"duplicate inventory tuple: {key}"
        rows[key] = row
    return rows


def _inventory_writer_counts(
    inventory: dict[tuple[str, str, str], dict[str, str]],
) -> Counter[tuple[str, str, str]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for key, row in inventory.items():
        raw_count = row["occurrences"]
        assert raw_count.isdecimal() and int(raw_count) > 0, (
            f"positive occurrence count required: {key}"
        )
        counts[key] = int(raw_count)
    return counts


def test_writer_visitor_preserves_same_symbol_multiplicity() -> None:
    tree = ast.parse(
        """
def persist(engine, record):
    engine.store_memory(record)
    engine.store_memory(record)
"""
    )
    visitor = _WriterVisitor("fixture.py")
    visitor.visit(tree)

    assert visitor.writers == Counter({("fixture.py", "persist", "store_memory"): 2})


def test_writer_visitor_resolves_proven_getattr_aliases_with_multiplicity() -> None:
    tree = ast.parse(
        """
def persist(engine, memory_id):
    update_fields = getattr(engine, "update_memory_fields", None)
    update_fields(memory_id, tags=[])
    update_fields(memory_id, domain="building")
    upsert = getattr(engine._sqlite, "upsert_ordinary", None)
    upsert(memory_id, {})
"""
    )
    visitor = _WriterVisitor("fixture.py")
    visitor.visit(tree)

    assert visitor.writers == Counter(
        {
            ("fixture.py", "persist", "update_memory_fields"): 2,
            ("fixture.py", "persist", "upsert_ordinary"): 1,
        }
    )


def test_writer_visitor_does_not_guess_unproved_or_reassigned_aliases() -> None:
    tree = ast.parse(
        """
def persist(engine, memory_id, method_name, callback):
    dynamic_writer = getattr(engine, method_name, None)
    dynamic_writer(memory_id)
    update_fields = getattr(engine, "update_memory_fields", None)
    update_fields = callback
    update_fields(memory_id)
"""
    )
    visitor = _WriterVisitor("fixture.py")
    visitor.visit(tree)

    assert visitor.writers == Counter()


def test_writer_visitor_counts_static_f_string_sql_once() -> None:
    tree = ast.parse(
        """
def persist(conn, guard):
    conn.execute(
        "UPDATE memories SET tags = ? "
        f"WHERE id = ? AND {guard}"
    )
"""
    )
    visitor = _WriterVisitor("fixture.py")
    visitor.visit(tree)

    assert visitor.writers == Counter({("fixture.py", "persist", "sql:UPDATE"): 1})


def test_inventory_matches_production_writers_and_has_migration_evidence() -> None:
    inventory = _inventory_rows()
    expected = _inventory_writer_counts(inventory)
    actual = _production_writers()

    assert actual == expected, (
        f"unclassified or added writer occurrences: {sorted((actual - expected).items())}; "
        f"stale or removed writer occurrences: {sorted((expected - actual).items())}"
    )

    required_evidence = (
        "existing_id",
        "current_semantics",
        "target_owner",
        "focused_test",
        "migration_task",
        "status",
    )
    for key, row in inventory.items():
        assert all(row[field] for field in required_evidence), (
            f"missing classification evidence: {key}"
        )
        assert row["focused_test"].startswith("tests/") and "::test_" in row["focused_test"], (
            f"focused regression test required: {key}"
        )
        if key in NONORDINARY_SYNTHESIS_WRITERS:
            assert row["target_owner"] == "SynthesisStore (non-ordinary)"
            assert row["status"] == "reviewed-nonordinary"
        else:
            assert row["target_owner"] in ORDINARY_TARGET_OWNERS, (
                f"unknown ordinary-memory target owner: {key}"
            )
        assert row["status"] != "pending", f"unresolved writer classification: {key}"
