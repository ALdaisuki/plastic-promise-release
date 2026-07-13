"""Rebuild canonical LanceDB vectors after an embedder recovery.

The legacy implementation read derived LanceDB text and wrote it back directly.
That could resurrect draft, stale, or control-only synthesis.  A repair is now
a canonical full rebuild, which also removes ineligible derived rows.
"""

from __future__ import annotations

import argparse
import logging

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.embedder import FallbackEmbedder, get_embedder

logger = logging.getLogger("repair_zero_vectors")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild canonical LanceDB vectors")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Deprecated: canonical repair always rebuilds the complete eligible set",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.limit:
        logger.error("--limit is incompatible with a complete canonical index rebuild")
        return 2

    embedder = get_embedder(fallback_on_error=False)
    if isinstance(embedder, FallbackEmbedder):
        logger.error("Embedder is FallbackEmbedder; cannot rebuild vectors")
        return 1
    probe = embedder.embed("canonical index rebuild probe")
    if not probe or not any(value != 0.0 for value in probe):
        logger.error("Embedder returns zero vectors; cannot rebuild")
        return 1

    engine = ContextEngine(use_sqlite=True)
    engine._ensure_heavy_init()
    ldb = engine._ldb
    if ldb is None:
        logger.error("LanceDB is not available")
        return 1

    if args.dry_run:
        canonical = ldb._canonical_engine_memories(engine)
        if canonical is None:
            logger.error("Canonical SQLite memories are unavailable")
            return 1
        eligible = ldb._eligible_engine_memories(engine, canonical)
        logger.info("Would rebuild %d canonical eligible memories", len(eligible))
        return 0

    rebuilt = ldb.rebuild_all(engine)
    logger.info("Canonical vector rebuild complete: %d memories indexed", rebuilt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
