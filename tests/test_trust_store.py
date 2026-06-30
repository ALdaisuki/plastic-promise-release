"""Unit tests for TrustStore — SQLite-backed trust score persistence."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.defense.trust_store import TrustStore


@pytest.fixture
def store():
    """Create a TrustStore with a temporary DB."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TrustStore(db_path=path)
    yield s
    s._conn.close()
    try:
        os.unlink(path)
    except PermissionError:
        pass  # Windows may hold the file briefly


class TestTrustStoreBasic:
    """Basic CRUD operations."""

    def test_get_default_returns_initial_trust(self, store):
        """Fresh store returns trust=0.6 for unknown target."""
        data = store.get("")
        assert data["trust"] == 0.6
        assert data["tier"] == "medium"
        assert data["autonomy_level"] == "standard"

    def test_save_and_get(self, store):
        """After save, get returns the updated value."""
        store.save("", 0.75, "medium", "standard")
        data = store.get("")
        assert data["trust"] == 0.75

    def test_save_clamps_to_range(self, store):
        """Save clamps values to [TRUST_MIN, TRUST_MAX]."""
        store.save("", 1.5, "high", "full")
        data = store.get("")
        assert data["trust"] == 1.0

        store.save("", -0.5, "critical", "minimal")
        data = store.get("")
        assert data["trust"] == 0.1


class TestTrustHistory:
    """History logging and querying."""

    def test_log_and_query_history(self, store):
        """After boost/decay, history is queryable."""
        store.save("", 0.6, "medium", "standard")
        store.log_history("", 0.05, "test boost", 0.55, 0.6, "boost")
        store.log_history("", -0.03, "test decay", 0.6, 0.57, "decay")

        hist = store.history("")
        assert len(hist) == 2
        assert hist[0]["direction"] == "boost"
        assert hist[0]["delta"] == 0.05
        assert hist[1]["direction"] == "decay"
        assert hist[1]["delta"] == -0.03

    def test_history_empty_for_new_target(self, store):
        """Unknown target returns empty history."""
        hist = store.history("unknown")
        assert hist == []

    def test_history_limit(self, store):
        """History respects the limit parameter."""
        for i in range(10):
            store.log_history("", 0.01, f"step {i}", 0.6, 0.61, "boost")

        hist = store.history("", limit=5)
        assert len(hist) == 5


class TestTimeDecay:
    """Lazy time decay on get()."""

    def test_no_decay_within_24h(self, store):
        """No decay when last_updated < 24h ago."""
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=12)).isoformat()
        store._conn.execute(
            "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("recent", 0.8, "high", "full", recent, recent),
        )
        store._conn.commit()

        data = store.get("recent")
        assert data["trust"] == 0.8  # No decay

    def test_time_decay_after_48h(self, store):
        """After 48h idle, trust decreases by 0.01 (2 * 0.005)."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=48)).isoformat()
        store._conn.execute(
            "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("stale", 0.6, "medium", "standard", old, old),
        )
        store._conn.commit()

        data = store.get("stale")
        # 48h = 2 days, decay = 2 * 0.005 = 0.01
        assert data["trust"] == 0.59  # 0.6 - 0.01

    def test_time_decay_capped(self, store):
        """Time decay never drops trust below TRUST_MIN (0.10)."""
        now = datetime.now(timezone.utc)
        very_old = (now - timedelta(days=365)).isoformat()
        store._conn.execute(
            "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ancient", 0.15, "critical", "minimal", very_old, very_old),
        )
        store._conn.commit()

        data = store.get("ancient")
        assert data["trust"] >= 0.10  # Never below TRUST_MIN

    def test_time_decay_cap_30_total(self, store):
        """Max time decay is 0.30 from time alone."""
        now = datetime.now(timezone.utc)
        very_old = (now - timedelta(days=200)).isoformat()
        store._conn.execute(
            "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("old", 0.9, "high", "full", very_old, very_old),
        )
        store._conn.commit()

        data = store.get("old")
        # 200 days * 0.005 = 1.0, but capped at 0.30
        # 0.9 - 0.30 = 0.60
        assert data["trust"] == 0.60


class TestMultiTarget:
    """Multi-target isolation."""

    def test_independent_targets(self, store):
        """Different targets have independent trust scores."""
        store.save("agent_a", 0.8, "high", "full")
        store.save("agent_b", 0.3, "low", "restricted")

        assert store.get("agent_a")["trust"] == 0.8
        assert store.get("agent_b")["trust"] == 0.3

    def test_default_target_isolation(self, store):
        """Default target is independent from named targets."""
        store.save("", 0.7, "medium", "standard")
        store.save("pi_builder", 0.5, "medium", "standard")

        assert store.get("")["trust"] == 0.7
        assert store.get("pi_builder")["trust"] == 0.5


class TestPersistence:
    """Cross-instance persistence."""

    def test_second_instance_reads_same_data(self, store):
        """A second TrustStore instance on the same DB reads the same data."""
        store.save("", 0.72, "medium", "standard")
        store.log_history("", 0.12, "test", 0.6, 0.72, "boost")

        # Second instance on same DB
        db_path = store._conn.execute("PRAGMA database_list").fetchone()[2]
        store2 = TrustStore(db_path=db_path)

        data = store2.get("")
        assert data["trust"] == 0.72

        hist = store2.history("")
        assert len(hist) == 1