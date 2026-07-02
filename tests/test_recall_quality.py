"""End-to-end recall quality verification for Phase 1."""

import sys


def test_recall_quality():
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
    sys.exit(test_recall_quality())
