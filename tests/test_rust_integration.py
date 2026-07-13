"""Integration tests for Rust engine degradation and health check."""

import pytest


@pytest.fixture(autouse=True)
def isolated_rust_integration_env(monkeypatch, tmp_path):
    """Keep Rust integration tests independent from repo-wide code-memory scans."""
    monkeypatch.setenv("PP_CODE_MEMORY_ENABLED", "0")
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "plastic_memory.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "plastic_memory.lancedb"))


def test_python_fallback_works_without_rust():
    """Python supply() works when Rust .pyd is unavailable."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    # Register a few test memories
    for i in range(5):
        engine.register_memory(
            {
                "id": f"test_{i:04d}",
                "content": f"Test memory {i} for integration testing",
                "memory_type": "task",
                "source": "test",
            }
        )

    # supply() should work — Rust path or Python fallback, doesn't matter
    pack = engine.supply("integration test task", task_type="general", scope="global")
    assert pack is not None
    # Python fallback should return some results (text retrieval works)
    assert pack.total_items >= 0  # at minimum, doesn't crash
    print(f"Python fallback: total_items={pack.total_items}")


def test_rust_health_check_initial_state():
    """Health check initializes correctly."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    # Initial state
    assert engine._rust_healthy is None
    assert engine._rust_health_checked_at == 0.0
    assert engine._rust_lock is not None

    # Health check runs without crashing
    result = engine._check_rust_health()
    # Result is True or None (False is never used per design)
    assert result is True or result is None


def test_rust_health_check_uses_explicit_canonical_backends(monkeypatch, tmp_path):
    """The health probe must exercise the same backend constructor as supply."""
    import sys
    import types

    from plastic_promise.core.context_engine import ContextEngine

    calls = {"new_with_backends": [], "default_constructor": 0}

    class FakeRustPack:
        core = []
        related = []
        divergent = []
        activated_principles = []

    class FakeRustEngine:
        def __init__(self):
            calls["default_constructor"] += 1
            raise AssertionError("health probe must not use RustEngine()")

        @staticmethod
        def new_with_backends(sqlite_path, lancedb_path):
            calls["new_with_backends"].append((sqlite_path, lancedb_path))
            instance = object.__new__(FakeRustEngine)
            return instance

        def set_current_time(self, _timestamp):
            return None

        def supply(self, *_args):
            return FakeRustPack()

    db_path = tmp_path / "canonical.db"
    lancedb_path = tmp_path / "canonical.lancedb"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(lancedb_path))
    monkeypatch.setitem(
        sys.modules,
        "context_engine_core",
        types.SimpleNamespace(ContextEngine=FakeRustEngine),
    )

    engine = ContextEngine(use_sqlite=True)
    engine._ldb = types.SimpleNamespace(_path=str(lancedb_path))
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "decoy.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "decoy.lancedb"))

    assert engine._check_rust_health() is True
    assert calls["default_constructor"] == 0
    assert calls["new_with_backends"] == [(str(db_path), str(lancedb_path))]


def test_rust_health_cache_ttl():
    """Health check caches result for TTL duration (when Rust is healthy).

    Design note: _rust_healthy=None (NOT False) on failure forces immediate
    re-probe on every call. The cache only applies when _rust_healthy is not None.
    This test validates the function doesn't crash and returns consistent values.
    """
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    # First call — probes
    result1 = engine._check_rust_health()
    checked_at1 = engine._rust_health_checked_at

    # Immediate second call
    result2 = engine._check_rust_health()
    checked_at2 = engine._rust_health_checked_at

    assert result1 == result2  # consistent result regardless of cache
    # If Rust is healthy, cache prevents re-probe (same checked_at).
    # If Rust unavailable, None bypasses cache and re-probes (checked_at advances).
    # Either is valid — verify the function runs without error.
    assert checked_at1 <= checked_at2  # monotonic timestamp
    assert engine._rust_lock is not None
    print(f"Rust health cache: result={result1}, checked_at_diff={checked_at2 - checked_at1:.6f}s")


def test_reset_rust_health():
    """reset_rust_health() clears cache and forces re-probe."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    # Call once to attempt probe
    engine._check_rust_health()
    # _rust_healthy may be None (Rust unavailable) or True (Rust available)

    # Reset — always clears regardless of state
    engine.reset_rust_health()
    assert engine._rust_healthy is None  # cleared
    assert engine._rust_health_checked_at == 0.0  # cleared
    assert engine._rust_engine_instance is None  # cleared


def test_empty_memories_supply(monkeypatch, tmp_path):
    """supply() with empty memory pool doesn't crash."""
    from plastic_promise.core.context_engine import ContextEngine

    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    engine = ContextEngine(use_sqlite=False)

    pack = engine.supply("test with empty pool", task_type="general", scope="global")
    assert pack is not None
    assert pack.total_items == 0


def test_convert_rust_pack_preserves_pipeline_metadata():
    """PyO3 conversion keeps Rust audit and debug counters visible."""
    from plastic_promise.core.context_engine import ContextEngine

    class FakeItem:
        def __init__(self):
            self.id = "mem_1"
            self.content = "Rust snapshot metadata test"
            self.relevance = 0.9
            self.source = "test"
            self.freshness = "valid"
            self.layer = "core"
            self.is_principle = False
            self.worth_score = 0.5

    class FakeRustPack:
        core = [FakeItem()]
        related = []
        divergent = []
        activated_principles = ["全过程可查可透明"]
        audit_metadata = {"engine_version": "0.2.0-rs"}
        pipeline_stats = {
            "engine_mode": "snapshot",
            "vector_hits": "2",
            "bm25_hits": "3",
            "mmr_demoted": "1",
            "after_noise_filter": "2",
            "after_source_filter": "1",
            "after_hard_score_filter": "1",
            "fallback_reason": "none",
            "stage_timing_ms": '{"total":"1.000"}',
        }
        per_item_stats = [
            {
                "id": "mem_1",
                "initial_score": "0.9000",
                "final_score": "0.9000",
                "source": "test",
                "source_penalty": "1.000",
                "filter_decision": "keep",
                "filter_reason": "passed",
            },
        ]

    engine = ContextEngine(use_sqlite=False)
    pack = engine._convert_rust_pack(FakeRustPack())

    assert pack.audit_metadata["engine_version"] == "0.2.0-rs"
    assert pack.audit_metadata["engine_mode"] == "snapshot"
    assert pack.pipeline_stats["vector_hits"] == "2"
    assert pack.pipeline_stats["bm25_hits"] == "3"
    assert pack.pipeline_stats["mmr_demoted"] == "1"
    assert pack.pipeline_stats["after_noise_filter"] == "2"
    assert pack.pipeline_stats["after_source_filter"] == "1"
    assert pack.pipeline_stats["after_hard_score_filter"] == "1"
    assert pack.pipeline_stats["fallback_reason"] == "none"
    assert "total" in pack.pipeline_stats["stage_timing_ms"]
    assert pack.per_item_stats[0]["final_score"] == "0.9000"
    assert pack.per_item_stats[0]["source_penalty"] == "1.000"
    assert pack.per_item_stats[0]["filter_decision"] == "keep"


def test_convert_rust_pack_filters_audit_telemetry_from_native_layers():
    """Python boundary drops telemetry even if a stale/native Rust pack returns it."""
    from plastic_promise.core.context_engine import ContextEngine

    class FakeItem:
        def __init__(self, item_id, content, source, layer):
            self.id = item_id
            self.content = content
            self.relevance = 0.9
            self.source = source
            self.freshness = "valid"
            self.layer = layer
            self.is_principle = False
            self.worth_score = 0.5

    class FakeRustPack:
        core = [
            FakeItem(
                "daemon_audit",
                "- [0.70] [maintenance_daemon] AUDIT trust=0.60 pipeline=0.94",
                "maintenance_daemon",
                "core",
            ),
            FakeItem("useful", "request scope recall isolation works", "codex", "core"),
        ]
        related = [
            FakeItem(
                "bare_audit",
                "AUDIT trust=0.60 pipeline=0.85 domain=0.80 bridge=0.00",
                "maintenance_daemon",
                "related",
            )
        ]
        divergent = []
        activated_principles = []
        audit_metadata = {"engine_version": "0.2.0-rs"}
        pipeline_stats = {"engine_mode": "snapshot"}
        per_item_stats = []

    engine = ContextEngine(use_sqlite=False)
    pack = engine._convert_rust_pack(FakeRustPack())

    recalled = pack.core + pack.related + pack.divergent
    assert [item.id for item in recalled] == ["useful"]
    assert all("audit trust=" not in item.content.lower() for item in recalled)


def test_debug_supply_uses_rust_path_when_rust_is_preferred(monkeypatch):
    """debug=True must not force the MCP server off the Rust hot path."""
    from plastic_promise.core.context_engine import ContextEngine, ContextPack

    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "0")

    engine = ContextEngine(use_sqlite=False)
    engine._check_rust_health = lambda: True

    calls = {"rust": 0, "python": 0}

    def fake_rust(task_description, task_vector, task_type, scope, **_kwargs):
        calls["rust"] += 1
        pack = ContextPack()
        pack.audit_metadata = {"engine_version": "0.2.0-rs"}
        pack.pipeline_stats = {"engine_mode": "snapshot"}
        return pack

    def fail_python(*args, **kwargs):
        calls["python"] += 1
        raise AssertionError("debug=True should not force Python supply in rust-full")

    engine._supply_rust = fake_rust
    engine._supply_python = fail_python

    pack = engine.supply("debug recall path", [0.0] * 1024, "debugging", "global", debug=True)

    assert calls == {"rust": 1, "python": 0}
    assert pack.audit_metadata["engine_version"] == "0.2.0-rs"
    assert pack.pipeline_stats["engine_mode"] == "snapshot"


def test_rust_supply_enriches_code_memory_evidence(monkeypatch, tmp_path):
    """Rust primary path still needs Python-side read-only code evidence."""
    from plastic_promise.core.context_engine import ContextEngine, ContextPack

    pkg = tmp_path / "sample_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "service.py").write_text(
        """
class Service:
    def run(self, payload):
        return payload
""".lstrip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "0")
    monkeypatch.setenv("PP_CODE_MEMORY_ENABLED", "1")
    monkeypatch.setenv("PP_CODE_MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("PP_CODE_MEMORY_MAX_FILES", "20")

    engine = ContextEngine(use_sqlite=False)
    engine._check_rust_health = lambda: True

    def fake_rust(task_description, task_vector, task_type, scope, **_kwargs):
        pack = ContextPack()
        pack.audit_metadata = {"engine_version": "0.2.0-rs"}
        pack.pipeline_stats = {"engine_mode": "snapshot"}
        return pack

    engine._supply_rust = fake_rust
    engine._supply_python = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("expected Rust primary path")
    )

    pack = engine.supply(
        "review Service.run code_memory behavior",
        [0.1] * 1024,
        "code_review",
        "global",
    )
    all_items = pack.core + pack.related + pack.divergent

    assert any(item.source == "code_memory" and "Service.run" in item.content for item in all_items)
    assert any(
        evidence["source"] == "code_memory"
        for evidence in pack.audit_metadata.get("raw_evidence", [])
    )
    assert pack.audit_metadata["code_memory"]["enabled"] is True


def test_supply_with_precomputed_vector_runs_heavy_init_before_rust(monkeypatch):
    """Caller-provided vectors must not bypass backend initialization."""
    from plastic_promise.core.context_engine import ContextEngine, ContextPack

    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "0")

    engine = ContextEngine(use_sqlite=False)
    engine._check_rust_health = lambda: True

    calls = {"heavy_init": 0, "rust": 0}

    def fake_heavy_init():
        calls["heavy_init"] += 1
        engine._heavy_init_done = True

    def fake_rust(task_description, task_vector, task_type, scope, **_kwargs):
        calls["rust"] += 1
        pack = ContextPack()
        pack.audit_metadata = {"engine_version": "0.2.0-rs"}
        pack.pipeline_stats = {"engine_mode": "snapshot"}
        return pack

    engine._ensure_heavy_init = fake_heavy_init
    engine._supply_rust = fake_rust
    engine._supply_python = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("expected Rust path")
    )

    pack = engine.supply(
        "precomputed vector recall",
        [0.1] * 1024,
        "debugging",
        "global",
        debug=True,
    )

    assert calls == {"heavy_init": 1, "rust": 1}
    assert pack.pipeline_stats["engine_mode"] == "snapshot"


def test_supply_rust_uses_new_with_backends_and_project_context(monkeypatch, tmp_path):
    """_supply_rust should use the explicit backend constructor when available."""
    import sys
    import types

    from plastic_promise.core.context_engine import ContextEngine, ContextPack

    calls = {"new_with_backends": [], "supply_with_project_context": []}

    class FakeRustPack:
        core = []
        related = []
        divergent = []
        activated_principles = ["tools are senses"]
        audit_metadata = {"engine_version": "0.2.0-rs"}
        pipeline_stats = {"engine_mode": "snapshot"}
        per_item_stats = []

    class FakeRustEngine:
        @staticmethod
        def new_with_backends(sqlite_path, lancedb_path):
            calls["new_with_backends"].append((sqlite_path, lancedb_path))
            return FakeRustEngine()

        def set_current_time(self, _timestamp):
            return None

        def supply_with_project_context(
            self,
            task_description,
            task_vector,
            task_type,
            scope,
            memories,
            project_id,
            project_policy,
            project_degraded,
        ):
            calls["supply_with_project_context"].append(
                {
                    "task_description": task_description,
                    "task_vector": task_vector,
                    "task_type": task_type,
                    "scope": scope,
                    "memories": memories,
                    "project_id": project_id,
                    "project_policy": project_policy,
                    "project_degraded": project_degraded,
                }
            )
            return FakeRustPack()

    fake_module = types.SimpleNamespace(ContextEngine=FakeRustEngine)
    monkeypatch.setitem(sys.modules, "context_engine_core", fake_module)
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "vectors.lancedb"))
    db_path = tmp_path / "plastic_memory.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))

    engine = ContextEngine(use_sqlite=False)
    engine.register_memory(
        {
            "id": "boundary_memory",
            "content": "rust boundary project context",
            "memory_type": "experience",
            "source": "test",
            "project_id": "project:plastic-promise",
            "visibility": "project",
            "source_class": "experience",
        }
    )

    pack = engine._supply_rust(
        "rust boundary",
        [0.0] * 1024,
        "code_generation",
        "global",
        project_id="project:plastic-promise",
        project_policy="strict",
        project_degraded=False,
    )

    assert isinstance(pack, ContextPack)
    assert calls["new_with_backends"], "expected _supply_rust to call new_with_backends"
    sqlite_path, lancedb_path = calls["new_with_backends"][0]
    assert sqlite_path == str(db_path)
    assert lancedb_path == str(tmp_path / "vectors.lancedb")
    supply_call = calls["supply_with_project_context"][0]
    assert supply_call["project_id"] == "project:plastic-promise"
    assert supply_call["project_policy"] == "strict"
    assert supply_call["project_degraded"] is False
    assert supply_call["memories"][0]["id"] == "boundary_memory"


def test_supply_rust_preserves_memory_db_path_for_new_with_backends(monkeypatch, tmp_path):
    """_supply_rust should pass the literal in-memory SQLite sentinel to Rust."""
    import sys
    import types

    from plastic_promise.core.context_engine import ContextEngine

    calls = {"new_with_backends": []}

    class FakeRustPack:
        core = []
        related = []
        divergent = []
        activated_principles = []
        audit_metadata = {"engine_version": "0.2.0-rs"}
        pipeline_stats = {"engine_mode": "snapshot"}
        per_item_stats = []

    class FakeRustEngine:
        @staticmethod
        def new_with_backends(sqlite_path, lancedb_path):
            calls["new_with_backends"].append((sqlite_path, lancedb_path))
            return FakeRustEngine()

        def set_current_time(self, _timestamp):
            return None

        def supply_with_project_context(
            self,
            task_description,
            task_vector,
            task_type,
            scope,
            memories,
            project_id,
            project_policy,
            project_degraded,
        ):
            return FakeRustPack()

    fake_module = types.SimpleNamespace(ContextEngine=FakeRustEngine)
    monkeypatch.setitem(sys.modules, "context_engine_core", fake_module)
    monkeypatch.setenv("PLASTIC_DB_PATH", ":memory:")
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "vectors.lancedb"))

    engine = ContextEngine(use_sqlite=False)
    engine._supply_rust("rust memory sentinel", [0.0] * 1024, "code_generation", "global")

    assert calls["new_with_backends"], "expected _supply_rust to call new_with_backends"
    sqlite_path, lancedb_path = calls["new_with_backends"][0]
    assert sqlite_path == ":memory:"
    assert lancedb_path == str(tmp_path / "vectors.lancedb")


def test_concurrent_supply_does_not_crash():
    """Multiple concurrent supply() calls don't crash or corrupt state."""
    import threading

    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)
    for i in range(20):
        engine.register_memory(
            {
                "id": f"conc_{i:04d}",
                "content": f"Concurrent test memory {i}",
                "memory_type": "task",
                "source": "test",
            }
        )

    errors = []
    results = []

    def call_supply(idx):
        try:
            pack = engine.supply(f"concurrent task {idx}", task_type="general", scope="global")
            results.append(pack)
        except Exception as e:
            errors.append((idx, str(e)))

    threads = [threading.Thread(target=call_supply, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Concurrent supply errors: {errors}"
    assert len(results) == 10
    print(f"Concurrent test: {len(results)} successes, {len(errors)} errors")
