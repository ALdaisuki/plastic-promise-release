"""Repair zero-vector entries in LanceDB by re-embedding with current embedder.

Usage:
    python scripts/repair_zero_vectors.py [--dry-run] [--limit N]

One-shot script. Run after embedder recovery to fix existing corrupted vectors.
"""

import argparse
import logging
import os
import sys

# Ensure project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("repair_zero_vectors")


def main():
    parser = argparse.ArgumentParser(description="Repair LanceDB zero-vector entries")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--limit", type=int, default=0, help="Max entries to repair (0 = unlimited)")
    args = parser.parse_args()

    # Import after arg parsing to keep --help fast
    from plastic_promise.core.context_engine import ContextEngine
    from plastic_promise.core.embedder import get_embedder, FallbackEmbedder

    embedder = get_embedder(fallback_on_error=False)
    if isinstance(embedder, FallbackEmbedder):
        logger.error("Embedder is FallbackEmbedder -- cannot repair. Fix embedder first.")
        sys.exit(1)

    # Verify embedder produces real vectors
    test_vec = embedder.embed("test")
    if not test_vec or not any(v != 0.0 for v in test_vec):
        logger.error("Embedder returns zero vectors -- cannot repair. Fix embedder first.")
        sys.exit(1)

    logger.info("Embedder OK (%s, dim=%d)", embedder.model_name, len(test_vec))

    engine = ContextEngine(use_sqlite=False)
    # Trigger heavy init to load LanceDB
    engine.register_memory({
        "id": "__repair_probe__",
        "content": "repair probe",
        "memory_type": "task",
        "source": "repair",
    })

    ldb = getattr(engine, '_ldb', None)
    if ldb is None or getattr(ldb, '_table', None) is None:
        logger.error("LanceDB not available")
        sys.exit(1)

    table = ldb._table
    total = table.count_rows()
    logger.info("LanceDB has %d total rows", total)

    # Scan for zero vectors
    repaired = 0
    skipped = 0
    to_fix = []

    rows = table.search().limit(total).to_list()
    for row in rows:
        mid = row["memory_id"]
        vec = row.get("vector", [])
        if mid == "__repair_probe__":
            continue
        if vec and not any(v != 0.0 for v in vec):
            to_fix.append((mid, row.get("text", ""), row))

    logger.info("Found %d zero-vector entries out of %d rows", len(to_fix), total)

    if args.dry_run:
        for mid, text, _row in to_fix[:10]:
            logger.info("  [DRY RUN] would repair: %s (%s...)", mid, text[:60])
        if len(to_fix) > 10:
            logger.info("  ... and %d more", len(to_fix) - 10)
        logger.info("Dry run complete. %d entries would be repaired.", len(to_fix))
        return

    for mid, text, row in to_fix:
        if args.limit > 0 and repaired >= args.limit:
            logger.info("Limit reached (%d), stopping", args.limit)
            break
        try:
            new_vec = embedder.embed(text)
            if not new_vec or not any(v != 0.0 for v in new_vec):
                logger.warning("  SKIP %s: embedder returned zero vector", mid)
                skipped += 1
                continue
            # Update LanceDB
            ldb.update(
                memory_id=mid,
                vector=new_vec,
                text=text,
                tier=row.get("tier", "L1"),
                category=row.get("category", "other"),
                scope=row.get("scope", "global"),
            )
            repaired += 1
            if repaired % 10 == 0:
                logger.info("  %d/%d repaired...", repaired, len(to_fix))
        except Exception as e:
            logger.warning("  FAIL %s: %s", mid, e)
            skipped += 1

    # Clean up probe
    try:
        ldb.delete("__repair_probe__")
    except Exception:
        pass

    logger.info("Repair complete: %d repaired, %d skipped, %d remaining",
                repaired, skipped, len(to_fix) - repaired - skipped)


if __name__ == "__main__":
    main()
