"""Knowledge schema migrations and backup evidence.

Migrations are idempotent and transactional.  Operator commands support
--check and --dry-run; real production migration requires explicit
authorization and backup evidence, mirroring the memory-store convention.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from plastic_promise.knowledge.repository import (
    KnowledgeRepository,
    schema_ddl_statements,
)


def schema_check(db_path: str | Path) -> dict[str, Any]:
    """Return a bounded schema health snapshot without mutating anything."""
    repository = KnowledgeRepository(db_path)
    repository.init_schema()
    tables = repository.present_tables()
    return {
        "database": str(repository.db_path),
        "tables": tables,
        "missing": [name for name, present in tables.items() if not present],
        "quick_check": repository.quick_check(),
        "integrity_check": repository.integrity_check(),
        "ok": all(tables.values())
        and repository.quick_check() == "ok"
        and repository.integrity_check() == "ok",
    }


def migrate_dry_run(db_path: str | Path) -> dict[str, Any]:
    """Return the DDL statements that a real migration would apply."""
    repository = KnowledgeRepository(db_path)
    repository.init_schema()
    statements = list(schema_ddl_statements())
    return {
        "database": str(repository.db_path),
        "statement_count": len(statements),
        "statements": statements,
        "would_execute": True,
        "note": "dry-run: no database was modified",
    }


def backup_evidence(
    db_path: str | Path,
    target_path: str | Path,
) -> dict[str, Any]:
    """Create a SQLite Online Backup API snapshot with digest evidence.

    The source database is never mutated; the backup is validated by a
    quick_check and a SHA-256 digest before returning.
    """
    source = Path(db_path).expanduser().resolve()
    target = Path(target_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise FileNotFoundError(f"knowledge database not found: {source}")

    source_connection = sqlite3.connect(str(source))
    target_connection = sqlite3.connect(str(target))
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    with sqlite3.connect(str(target)) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
        quick_check = str(row[0])
    return {
        "source": str(source),
        "target": str(target),
        "byte_size": target.stat().st_size,
        "sha256": digest,
        "quick_check": quick_check,
        "ok": quick_check == "ok",
    }
