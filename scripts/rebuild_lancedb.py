"""Rebuild the LanceDB index from the canonical SQLite memory set.

This maintenance entry point deliberately delegates eligibility and index
materialization to ``LanceDBStore.rebuild_all``.  LanceDB is derived state, so
the script must not reinsert raw runtime memories or bypass synthesis review
status.
"""

from __future__ import annotations

from plastic_promise.core.context_engine import ContextEngine


def main() -> int:
    engine = ContextEngine(use_sqlite=True)
    engine._ensure_heavy_init()
    ldb = engine._ldb
    if ldb is None:
        print("LanceDB is not available; nothing rebuilt.")
        return 1

    rebuilt = ldb.rebuild_all(engine)
    print(f"Re-indexed canonical eligible memories: {rebuilt}")
    print(f"LanceDB rows: {ldb.count_rows()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
