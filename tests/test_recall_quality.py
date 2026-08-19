"""Offline recall quality regression plus an explicit live diagnostic.

The live diagnostic intentionally talks to the configured embedding provider and
the current database.  It is kept behind the module entry point so ordinary
pytest runs remain deterministic and do not silently depend on Ollama or a
developer's local LanceDB contents.
"""

import os
import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PP_RUN_RECALL_QUALITY_TEST") != "1",
        reason="set PP_RUN_RECALL_QUALITY_TEST=1 with a populated live index",
    ),
]


class _OfflineLanceDB:
    def __init__(self, memory_ids):
        self._memory_ids = tuple(memory_ids)

    def search(self, vector, k=20, scope=None):
        del vector, scope
        return [
            (memory_id, 0.95 - index * 0.03) for index, memory_id in enumerate(self._memory_ids[:k])
        ]

    def fts_search(self, query, k=20, scope=None):
        del query, scope
        return [
            (memory_id, 0.92 - index * 0.03) for index, memory_id in enumerate(self._memory_ids[:k])
        ]

    def count_rows(self):
        return len(self._memory_ids)

    def list_memory_ids(self):
        return list(self._memory_ids)

    def consume_search_diagnostics(self):
        return []


class _OfflineEmbedder:
    def embed(self, text):
        del text
        return [0.1] * 1024


def _offline_engine():
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)
    engine._code_index = None
    engine._embedder = _OfflineEmbedder()
    engine._memories = {}
    for index in range(8):
        memory_id = f"recall-quality-{index}"
        engine._memories[memory_id] = {
            "id": memory_id,
            "content": (
                f"Code review scanner data quality fix pattern {index} with durable "
                "engineering evidence"
            ),
            "memory_type": "experience",
            "source": "offline-recall-quality-fixture",
            "tier": "L1" if index < 3 else "L2",
            "category": "fact",
            "scope": "global",
            "visibility": "global",
            "project_id": "project:recall-quality",
            "tags": ["domain:building"],
            "domain": "building",
            "access_count": 0,
            "worth_success": 1,
            "worth_failure": 0,
        }
    engine._ldb = _OfflineLanceDB(engine._memories)
    return engine


def test_recall_quality_offline(monkeypatch):
    """Exercise the full recall projection without external providers or data."""

    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
    monkeypatch.setenv("PP_RERANK_DISABLED", "1")
    monkeypatch.setenv("PP_CANONICAL_HOT_LOOKUP", "0")
    monkeypatch.setenv("PP_CODE_MEMORY_ENABLED", "0")
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "off")
    engine = _offline_engine()
    query = "code review scanner data quality fix"
    pack = engine.supply(query, [0.1] * 1024, "code_generation", "global")

    audit = pack.audit_metadata
    assert int(audit["ldb_rows"]) <= int(audit["memory_pool_size"])
    assert audit["vector_search"] == "active"
    assert len(pack.core) >= 1
    assert len(pack.core) + len(pack.related) >= 3
    assert "Performance test memory" not in " ".join(
        item.content for item in pack.core + pack.related
    )
    assert len(pack.activated_principles) >= 2
    assert len(engine._text_retrieval(query)) >= 3
    assert audit.get("rerank_status")


def run_live_recall_quality():
    """Run the real provider/database diagnostic when explicitly requested."""

    from plastic_promise.core.context_engine import ContextEngine
    from plastic_promise.core.embedder import get_embedder

    engine = ContextEngine()
    engine._ensure_heavy_init()
    embedder = get_embedder(fallback_on_error=True)

    query = "code review scanner data quality fix"
    vec = embedder.embed(query)
    pack = engine.supply(query, vec, "code_generation", "global")

    audit = pack.audit_metadata
    failures = []

    # Check 1: No ghost vectors
    ldb_count = int(audit.get("ldb_rows", "0"))
    mem_count = int(audit.get("memory_pool_size", "0"))
    if ldb_count > mem_count:
        failures.append(f"Ghost vectors: LDB {ldb_count} > SQLite {mem_count}")
    print(
        f"  [{'PASS' if ldb_count <= mem_count else 'FAIL'}] LDB rows: {ldb_count} <= SQLite: {mem_count}"
    )

    # Check 2: Vector search active
    vec_status = audit.get("vector_search", "fallback")
    if vec_status != "active":
        failures.append(f"Vector search not active: {vec_status}")
    print(f"  [{'PASS' if vec_status == 'active' else 'FAIL'}] Vector search: {vec_status}")

    # Check 3: Core has >= 1 item (lowered threshold 0.70)
    core_count = len(pack.core)
    if core_count < 1:
        failures.append(f"Core count {core_count} < 1")
    print(f"  [{'PASS' if core_count >= 1 else 'FAIL'}] Core items: {core_count}")

    # Check 4: Related has >= 5 items
    related_count = len(pack.related)
    if related_count < 5:
        failures.append(f"Related count {related_count} < 5")
    print(f"  [{'PASS' if related_count >= 5 else 'FAIL'}] Related items: {related_count}")

    # Check 5: No test pollution in top results
    all_content = " ".join(item.content for item in pack.core + pack.related)
    if "Performance test memory" in all_content:
        failures.append("Test pollution detected in results")
    print(
        f"  [{'PASS' if 'Performance test memory' not in all_content else 'FAIL'}] No test pollution"
    )

    # Check 6: Principles activated (dict format)
    principles = pack.activated_principles
    if len(principles) < 2:
        failures.append(f"Only {len(principles)} principles activated")
    has_content = all("content" in (p if isinstance(p, dict) else {}) for p in principles)
    print(
        f"  [{'PASS' if len(principles) >= 2 and has_content else 'FAIL'}] Principles: {len(principles)} (dict: {has_content})"
    )

    # Check 7: BM25 hit rate
    text_results = engine._text_retrieval(query)
    if len(text_results) < 3:
        failures.append(f"BM25 hits {len(text_results)} < 3")
    print(f"  [{'PASS' if len(text_results) >= 3 else 'FAIL'}] BM25 hits: {len(text_results)}")

    # Check 8: Rerank status present in audit
    rerank = audit.get("rerank_status", "")
    if not rerank:
        failures.append("Missing rerank_status in audit")
    print(f"  [{'PASS' if rerank else 'FAIL'}] Rerank status: {rerank}")

    # Show results
    print("\n--- Top Core ---")
    for item in pack.core[:3]:
        print(f"  [{item.relevance:.3f}] {item.content[:120]}")
    print("--- Top Related ---")
    for item in pack.related[:5]:
        print(f"  [{item.relevance:.3f}] {item.content[:120]}")

    if failures:
        print(f"\n{failures.__len__()} FAILURES:")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("\nAll 8 checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_live_recall_quality())
