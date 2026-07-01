"""TrustStore — SQLite 持久化层 for TrustManager

Persists trust scores and change history to the existing plastic_memory.db,
enabling trust scores to survive MCP server restarts and applying lazy time decay.

Schema:
    trust_scores: (target, trust, tier, autonomy_level, last_updated, created_at)
    trust_history: (id, target, delta, reason, old_value, new_value, direction, timestamp)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

from plastic_promise.core.constants import (
    TRUST_INITIAL,
    TRUST_MIN,
    TRUST_MAX,
)


class TrustStore:
    """SQLite-backed trust score persistence with lazy time decay.

    Uses the same SQLite database as _SQLiteStorage (PLASTIC_DB_PATH env var
    or default ``plastic_memory.db``).  All reads automatically apply a
    time-decay penalty when the score has not been updated for > 24 hours.

    Time-decay formula::

        days_since = (now - last_updated).days
        if days_since >= 1:
            decay = min(days_since * 0.005, 0.30)
            new_trust = max(0.10, current_trust - decay)
    """

    TIME_DECAY_RATE = 0.005       # per day
    TIME_DECAY_CAP = 0.30         # max total decay from time alone
    TIME_DECAY_THRESHOLD_HOURS = 24

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
        # Resolve to absolute path to prevent split-brain when CWD differs
        db_path = os.path.abspath(db_path)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._db_path = db_path
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS trust_scores ("
            "  target TEXT PRIMARY KEY,"
            "  trust REAL NOT NULL DEFAULT 0.6,"
            "  tier TEXT NOT NULL DEFAULT 'medium',"
            "  autonomy_level TEXT NOT NULL DEFAULT 'standard',"
            "  last_updated TEXT NOT NULL,"
            "  created_at TEXT NOT NULL"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS trust_history ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  target TEXT NOT NULL,"
            "  delta REAL NOT NULL,"
            "  reason TEXT NOT NULL DEFAULT '',"
            "  old_value REAL NOT NULL,"
            "  new_value REAL NOT NULL,"
            "  direction TEXT NOT NULL,"
            "  timestamp TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

        # Hunter Guild task queue tables
        from plastic_promise.core.task_queue_schema import ensure_task_tables
        ensure_task_tables(self._conn)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, target: str = "") -> dict:
        """Return {trust, tier, autonomy_level, last_updated} for *target*.

        Automatically applies time decay before returning.  If *target* has
        no row yet, an initial record is created and returned.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        row = self._conn.execute(
            "SELECT trust, tier, autonomy_level, last_updated FROM trust_scores WHERE target = ?",
            (target,),
        ).fetchone()

        if row is None:
            # Bootstrap: create initial record
            self._conn.execute(
                "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (target, TRUST_INITIAL, "medium", "standard", now_iso, now_iso),
            )
            self._conn.commit()
            return {
                "trust": TRUST_INITIAL,
                "tier": "medium",
                "autonomy_level": "standard",
                "last_updated": now_iso,
            }

        current = {
            "trust": row[0],
            "tier": row[1],
            "autonomy_level": row[2],
            "last_updated": row[3],
        }
        return self._apply_time_decay(target, current, now)

    def _apply_time_decay(self, target: str, current: dict, now: datetime) -> dict:
        """Apply lazy time decay if the score is stale.

        Only mutates the DB when decay actually occurs (>= 24 h since
        last_updated).  Returns the (possibly decayed) dict.
        """
        last_updated_str = current.get("last_updated", "")
        if not last_updated_str:
            return current

        try:
            last_updated = datetime.fromisoformat(last_updated_str)
            # SQLite 的 datetime('now') 是 naive；对齐为 naive 比较
            if last_updated.tzinfo is not None:
                last_updated = last_updated.replace(tzinfo=None)
        except (ValueError, TypeError):
            return current

        now_naive = now.replace(tzinfo=None)
        delta = now_naive - last_updated
        hours = delta.total_seconds() / 3600.0
        if hours < self.TIME_DECAY_THRESHOLD_HOURS:
            return current

        days = max(1, int(hours / 24))
        decay = min(days * self.TIME_DECAY_RATE, self.TIME_DECAY_CAP)
        old_trust = current["trust"]
        new_trust = round(max(TRUST_MIN, old_trust - decay), 4)

        if new_trust >= old_trust:
            return current  # already at floor, no change

        # Persist the decayed value
        now_iso = now.isoformat()
        new_tier = self._compute_tier(new_trust)
        new_autonomy = self._compute_autonomy(new_tier)

        self._conn.execute(
            "UPDATE trust_scores SET trust = ?, tier = ?, autonomy_level = ?, last_updated = ? "
            "WHERE target = ?",
            (new_trust, new_tier, new_autonomy, now_iso, target),
        )
        self._conn.execute(
            "INSERT INTO trust_history (target, delta, reason, old_value, new_value, direction, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target, -decay, f"time decay: {days}d idle", old_trust, new_trust, "decay", now_iso),
        )
        self._conn.commit()

        return {
            "trust": new_trust,
            "tier": new_tier,
            "autonomy_level": new_autonomy,
            "last_updated": now_iso,
        }

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, target: str, trust: float, tier: str, autonomy_level: str) -> None:
        """Upsert the current trust score for *target*."""
        now_iso = datetime.now(timezone.utc).isoformat()
        clamped = round(max(TRUST_MIN, min(TRUST_MAX, trust)), 4)
        self._conn.execute(
            "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target) DO UPDATE SET "
            "  trust = excluded.trust,"
            "  tier = excluded.tier,"
            "  autonomy_level = excluded.autonomy_level,"
            "  last_updated = excluded.last_updated",
            (target, clamped, tier, autonomy_level, now_iso, now_iso),
        )
        self._conn.commit()

    def log_history(
        self,
        target: str,
        delta: float,
        reason: str,
        old_value: float,
        new_value: float,
        direction: str,
    ) -> None:
        """Append a row to the trust_history table."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO trust_history (target, delta, reason, old_value, new_value, direction, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target, delta, reason, old_value, new_value, direction, now_iso),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def history(self, target: str = "", limit: int = 50) -> list:
        """Return recent trust-history entries for *target*."""
        rows = self._conn.execute(
            "SELECT target, delta, reason, old_value, new_value, direction, timestamp "
            "FROM trust_history WHERE target = ? ORDER BY id DESC LIMIT ?",
            (target, limit),
        ).fetchall()
        return [
            {
                "target": r[0],
                "delta": r[1],
                "reason": r[2],
                "old_value": r[3],
                "new_value": r[4],
                "direction": r[5],
                "timestamp": r[6],
            }
            for r in reversed(rows)
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_tier(trust: float) -> str:
        if trust >= 0.80:
            return "high"
        elif trust >= 0.50:
            return "medium"
        elif trust >= 0.30:
            return "low"
        return "critical"

    @staticmethod
    def _compute_autonomy(tier: str) -> str:
        if tier == "high":
            return "full"
        elif tier == "medium":
            return "standard"
        elif tier == "low":
            return "restricted"
        return "minimal"