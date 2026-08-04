"""Inspect or explicitly requeue terminal derived-index outbox jobs."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plastic_promise.core.synthesis_maintenance import requeue_index_outbox_jobs  # noqa: E402
from plastic_promise.core.traceability import ensure_traceability_schema  # noqa: E402

_INDEX_TOOLS = frozenset({"memory_index", "synthesis_index"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing canonical SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect", help="Read safe job metadata only")
    inspect_parser.add_argument("outbox_ids", nargs="+", help="Explicit outbox IDs")

    requeue_parser = commands.add_parser(
        "requeue",
        help="Preview terminal jobs, or reset them to pending with --apply",
    )
    requeue_parser.add_argument("outbox_ids", nargs="+", help="Explicit outbox IDs")
    requeue_parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the requeue; without this flag the command is read-only",
    )
    return parser


def _database_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("database_must_be_existing_regular_file")
    return path.resolve(strict=True)


def _connect(path: Path, *, writable: bool) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=5)
    connection.execute("PRAGMA busy_timeout = 5000")
    if not writable:
        connection.execute("PRAGMA query_only = ON")
    return connection


def _safe_rows(connection: sqlite3.Connection, outbox_ids: list[str]) -> list[dict[str, object]]:
    normalized = list(dict.fromkeys(outbox_ids))
    if not normalized or len(normalized) > 1000:
        raise ValueError("invalid_outbox_id_count")
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
        for value in normalized
    ):
        raise ValueError("invalid_outbox_id")
    placeholders = ",".join("?" for _ in normalized)
    rows = connection.execute(
        "SELECT outbox_id, tool_name, status, attempt_count, updated_at, next_attempt_at "
        f"FROM store_outbox WHERE outbox_id IN ({placeholders})",
        tuple(normalized),
    ).fetchall()
    by_id = {
        str(row[0]): {
            "outbox_id": str(row[0]),
            "tool_name": str(row[1]),
            "status": str(row[2]),
            "attempt_count": int(row[3] or 0),
            "updated_at": str(row[4] or ""),
            "next_attempt_at": str(row[5] or ""),
            "requeue_eligible": str(row[1]) in _INDEX_TOOLS
            and str(row[2]) in {"blocked", "failed"},
        }
        for row in rows
    }
    return [
        by_id.get(
            outbox_id,
            {
                "outbox_id": outbox_id,
                "found": False,
                "requeue_eligible": False,
            },
        )
        for outbox_id in normalized
    ]


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    connection: sqlite3.Connection | None = None
    try:
        path = _database_path(arguments.db)
        writable = arguments.command == "requeue" and bool(arguments.apply)
        connection = _connect(path, writable=writable)
        if writable:
            ensure_traceability_schema(connection)
            report = requeue_index_outbox_jobs(connection, arguments.outbox_ids)
            payload: dict[str, object] = {
                "applied": True,
                "requested": report.requested,
                "requeued_ids": list(report.requeued_ids),
                "rejected_ids": list(report.rejected_ids),
            }
        else:
            payload = {
                "applied": False,
                "jobs": _safe_rows(connection, arguments.outbox_ids),
            }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"index outbox management failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
