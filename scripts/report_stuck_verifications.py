"""Read-only inventory of pending verify_task rows stuck in the task queue.
Read-only inventory only: this script never writes to SQLite. Re-verification
(accept/reject/reassign) must go through an elder session's "task_verify"
one item at a time; direct SQLite writes are forbidden in production under
the single-writer convention -- this tool is the dry-run surface for a
deliberate spec relaxation, nothing more.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plastic_promise.core.paths import get_db_path  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True, help="Canonical project id")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite db path (default: plastic_promise.core.paths.get_db_path())",
    )
    parser.add_argument("--limit", type=int, default=50, help="Max rows to list")
    return parser


def _connect_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parent_status(conn: sqlite3.Connection, parent_task_id: object) -> str:
    if parent_task_id is None:
        return "(no-parent)"
    row = conn.execute("SELECT status FROM task_queue WHERE id=?", (parent_task_id,)).fetchone()
    return row["status"] if row else "(missing)"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = str(Path(args.db).expanduser()) if args.db else str(get_db_path())
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error as exc:
        print("cannot-open-db:", exc, file=sys.stderr)
        return 2

    rows = conn.execute(
        "SELECT id, created_at, parent_task_id, payload FROM task_queue"
        " WHERE project_id=? AND status='pending' AND task_type='verify_task'"
        " ORDER BY created_at LIMIT ?",
        (args.project_id, args.limit),
    ).fetchall()

    now = time.time()
    summary: Counter[str] = Counter()
    oldest_age_days: float | None = None
    entries: list[dict] = []
    for row in rows:
        payload: dict = {}
        try:
            payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        parent_status = _parent_status(conn, row["parent_task_id"])
        summary[parent_status] += 1
        created = row["created_at"]
        age_days = 0.0
        if isinstance(created, (int, float)):
            age_days = max(0.0, (now - float(created)) / 86400.0)
            oldest_age_days = (
                age_days if oldest_age_days is None else max(oldest_age_days, age_days)
            )
        result_text = str(payload.get("original_result") or "")
        entries.append(
            {
                "id": row["id"],
                "created_at": created,
                "age_days": age_days,
                "parent": parent_status,
                "agent": str(payload.get("original_agent") or "(unknown)"),
                "result": result_text[:80],
            }
        )

    print(f"project={args.project_id} db={db_path}")
    if not entries:
        print("none")
        return 0

    buckets = ", ".join(f"{s}={c}" for s, c in sorted(summary.items()))
    assert oldest_age_days is not None
    print(f"total_pending_verify_tasks={len(entries)} oldest_age_days={oldest_age_days:.2f}")
    print(f"by_parent_status: {buckets}")
    header = f"{'id':<24} {'created_at':<20} {'age_d':>7}  {'parent':<14} {'agent':<18} result"
    print(header)
    print("-" * len(header))
    for e in entries:
        print(
            f"{str(e['id'])[:24]:<24} {str(e['created_at'])[:20]:<20} {e['age_days']:>7.2f}  "
            f"{str(e['parent'])[:14]:<14} {str(e['agent'])[:18]:<18} {e['result']}"
        )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
