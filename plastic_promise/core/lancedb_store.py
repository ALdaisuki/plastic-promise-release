"""LanceDBStore — persistent vector storage with ANN + FTS search.

Table: memory_vectors (memory_id, vector, text, tier, category, scope)
Vector dim: 1024 (mxbai-embed-large), configurable via PP_EMBEDDING_DIM.
"""

import logging
import os

import lancedb
import pyarrow as pa

from plastic_promise.core.embedder import Embedder

logger = logging.getLogger("plastic-promise.lancedb")

EMB_DIM = int(os.environ.get("PP_EMBEDDING_DIM", "1024"))
TABLE_NAME = "memory_vectors"

_MEMORY_VECTORS_SCHEMA = pa.schema(
    [
        pa.field("memory_id", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), EMB_DIM)),
        pa.field("text", pa.string()),
        pa.field("tier", pa.string()),
        pa.field("category", pa.string()),
        pa.field("scope", pa.string()),
    ]
)


class LanceDBStore:
    """Persistent vector store backed by LanceDB.

    Provides ANN vector search, FTS text search, and CRUD operations.
    Table is created on first access if it doesn't exist.
    """

    def __init__(self, db_path: str, embedder: Embedder) -> None:
        self._path = db_path
        self._embedder = embedder
        self._vectors_disabled = getattr(embedder, "model_name", "") == "fallback-zero"
        if self._vectors_disabled:
            logger.warning("LanceDBStore: FallbackEmbedder detected — vector operations disabled")
        self._db: lancedb.DBConnection | None = None
        self._table: lancedb.table.Table | None = None
        self._fts_ready = False
        self._init_db()

    def _init_db(self) -> None:
        """Open or create LanceDB database and table."""
        os.makedirs(self._path, exist_ok=True)
        self._db = lancedb.connect(self._path)
        try:
            self._table = self._db.open_table(TABLE_NAME)
            logger.info(
                "LanceDB: opened existing table '%s' (%d rows)",
                TABLE_NAME,
                self._table.count_rows(),
            )
        except Exception:
            self._table = self._db.create_table(TABLE_NAME, schema=_MEMORY_VECTORS_SCHEMA, data=[])
            logger.info("LanceDB: created table '%s'", TABLE_NAME)
        self._ensure_fts()

    def _ensure_fts(self) -> None:
        """Create FTS index on 'text' column if not already present.

        Uses a fast check via table schema introspection to avoid
        attempting to recreate an existing FTS index on every init.
        """
        try:
            # Fast check: try to list indices; if FTS exists, skip creation
            try:
                indices = self._table.list_indices()
                for idx in indices:
                    if getattr(idx, "name", "") == "text_idx" or (
                        hasattr(idx, "column") and "text" in str(getattr(idx, "column", ""))
                    ):
                        self._fts_ready = True
                        return
            except Exception:
                pass  # list_indices may not be available — fall through to creation

            # LanceDB 0.30+ expects a single column name string, not a list
            self._table.create_fts_index("text", replace=True)
            self._fts_ready = True
            logger.info("LanceDB: FTS index ready on 'text'")
        except Exception as e:
            # If creation fails but index already exists, mark as ready
            err_msg = str(e).lower()
            if "already exists" in err_msg or "duplicate" in err_msg:
                self._fts_ready = True
                logger.info("LanceDB: FTS index already exists on 'text'")
            else:
                logger.warning("LanceDB: FTS index not available (%s), using fallback", e)
                self._fts_ready = False

    def search(
        self,
        vector: list[float],
        k: int = 20,
        scope: str | None = None,
        tier: str | None = None,
    ) -> list[tuple[str, float, str, str, str]]:
        """ANN vector search by cosine similarity.

        Args:
            vector: Query embedding (len == EMB_DIM).
            k: Max results to return.
            scope: Optional scope filter.
            tier: Optional tier filter.

        Returns:
            List of (memory_id, score, text, tier, scope) sorted by similarity descending.
        """
        if self._vectors_disabled:
            return []
        if self._table is None:
            return []
        try:
            q = self._table.search(vector).metric("cosine").limit(k)
            # LanceDB returns distance for cosine metric; lower is better.
            # Convert to similarity: 1.0 - distance (cosine dist in [0, 2])
            raw = q.to_list()
            results = []
            for row in raw:
                dist = row.get("_distance", 0.0)
                sim = 1.0 - (dist / 2.0)  # normalize to [0, 1]
                mid = row["memory_id"]
                if scope and row.get("scope") != scope:
                    continue
                if tier and row.get("tier") != tier:
                    continue
                results.append(
                    (
                        mid,
                        max(0.0, min(1.0, sim)),
                        row.get("text", ""),
                        row.get("tier", "L1"),
                        row.get("scope", "global"),
                    )
                )
            return results
        except Exception as e:
            logger.error("LanceDB vector search failed: %s", e)
            return []

    def search_fts(
        self,
        query: str,
        k: int = 20,
        scope: str | None = None,
    ) -> list[tuple[str, float, str, str, str]]:
        """Full-text search with fallback to LIKE-based filtering.

        Args:
            query: Text query string.
            k: Max results to return.
            scope: Optional scope filter.

        Returns:
            List of (memory_id, score, text, tier, scope).
        """
        if self._vectors_disabled:
            return []
        if self._table is None:
            return []
        try:
            if self._fts_ready:
                raw = self._table.search(query, query_type="fts").limit(k).to_list()
            else:
                # Fallback: substring match via pyarrow compute
                safe_query = query.replace("'", "''")  # escape single quotes
                pattern = f"%{safe_query}%"
                raw = (
                    self._table.search()
                    .where(f"text LIKE '{pattern}'", prefilter=True)
                    .limit(k)
                    .to_list()
                )
            results = []
            for row in raw:
                mid = row["memory_id"]
                score = row.get("_distance", row.get("_score", 0.5))
                if isinstance(score, (int, float)):
                    score = 1.0 - min(float(score), 1.0)
                else:
                    score = 0.5
                if scope and row.get("scope") != scope:
                    continue
                results.append(
                    (
                        mid,
                        max(0.0, min(1.0, score)),
                        row.get("text", ""),
                        row.get("tier", "L1"),
                        row.get("scope", "global"),
                    )
                )
            return results
        except Exception as e:
            logger.warning("LanceDB FTS search failed: %s", e)
            return []

    def search_similar(
        self,
        vector: list[float],
        k: int = 5,
    ) -> list[tuple[str, float]]:
        """Return top-k (memory_id, similarity) sorted by similarity descending.

        No scope/tier filtering — raw vector similarity for dedup and merge.
        Uses existing ANN search with cosine metric.
        Returns empty list when table is None or search fails.
        """
        if self._table is None:
            return []
        try:
            raw = self._table.search(vector).metric("cosine").limit(k).to_list()
            results = []
            for row in raw:
                dist = row.get("_distance", 0.0)
                sim = 1.0 - (dist / 2.0)  # cosine distance -> similarity [0, 1]
                results.append((row["memory_id"], max(0.0, min(1.0, sim))))
            return results
        except Exception as e:
            logger.warning("LanceDB search_similar failed: %s", e)
            return []

    def get_vector(
        self,
        memory_id: str,
    ) -> list[float] | None:
        """Return the stored vector for a single memory, or None if not found.

        Used by MMR diversity checking — compare candidate vectors against
        already-selected items without a full ANN search.

        Args:
            memory_id: The memory ID to look up.

        Returns:
            List of floats (embedding vector), or None if not found or on error.
        """
        if self._table is None:
            return None
        try:
            rows = (
                self._table.search()
                .where(f"memory_id = '{memory_id}'", prefilter=True)
                .limit(1)
                .to_list()
            )
            if rows and rows[0].get("vector"):
                return list(rows[0]["vector"])
            return None
        except Exception:
            return None

    def check_duplicate(
        self,
        vector: list[float],
        threshold: float = 0.85,
    ) -> str | None:
        """Return memory_id of the nearest match if similarity >= threshold, else None.

        Thin wrapper over search_similar(k=1). Used by pipeline dedup (Task 3).
        """
        if self._vectors_disabled:
            return None
        results = self.search_similar(vector, k=1)
        if results and results[0][1] >= threshold:
            return results[0][0]
        return None

    def insert(
        self,
        memory_id: str,
        vector: list[float],
        text: str,
        tier: str = "L1",
        category: str = "other",
        scope: str = "global",
    ) -> None:
        """Insert a vector row. No-op if memory_id already exists."""
        if self._vectors_disabled:
            logger.debug("LanceDBStore.insert(%s): vectors disabled, skipping write", memory_id)
            return
        if self._table is None:
            return
        try:
            existing = (
                self._table.search()
                .where(f"memory_id = '{memory_id}'", prefilter=True)
                .limit(1)
                .to_list()
            )
            if existing:
                return  # already exists, skip
            self._table.add(
                [
                    {
                        "memory_id": memory_id,
                        "vector": vector,
                        "text": text,
                        "tier": tier,
                        "category": category,
                        "scope": scope,
                    }
                ]
            )
        except Exception as e:
            logger.error("LanceDB insert failed for %s: %s", memory_id, e)

    def update(
        self,
        memory_id: str,
        vector: list[float],
        text: str,
        tier: str = "L1",
        category: str = "other",
        scope: str = "global",
    ) -> None:
        """Update or insert a vector row (upsert)."""
        self.delete(memory_id)
        self.insert(memory_id, vector, text, tier, category, scope)

    def delete(self, memory_id: str) -> None:
        """Delete a vector row by memory_id."""
        if self._table is None:
            return
        try:
            self._table.delete(f"memory_id = '{memory_id}'")
        except Exception as e:
            logger.error("LanceDB delete failed for %s: %s", memory_id, e)

    def count_rows(self) -> int:
        """Return total rows in the table."""
        if self._table is None:
            return 0
        try:
            return self._table.count_rows()
        except Exception:
            return 0

    def clear_all(self) -> int:
        """Delete all rows from the table and return the count that was removed.

        After clearing, the table is empty but still exists with its schema
        and FTS index intact.
        """
        if self._table is None:
            return 0
        try:
            count = self._table.count_rows()
            if count > 0:
                self._table.delete("memory_id IS NOT NULL")
            logger.info("LanceDB: cleared %d rows", count)
            return count
        except Exception as e:
            logger.error("LanceDB clear_all failed: %s", e)
            return 0

    def rebuild_all(self, engine: object) -> int:
        """Clear LanceDB table and rebuild all vectors from SQLite memories.

        Used when LanceDB vectors are out of sync with SQLite (ghost vectors,
        stale entries, test pollution). Regenerates every vector via embedder.

        Safety: calls clear_all() first, then embeds and inserts each memory.
        Circuit-breaker: stops on first embedding failure (Ollama down).
        Respects LDB_REBUILD_MAX_PER_CALL env var for batching.

        Args:
            engine: ContextEngine instance with _memories dict.

        Returns:
            Number of memories re-indexed.
        """
        import os as _os

        _max_batch = int(_os.environ.get("LDB_REBUILD_MAX_PER_CALL", "200"))

        removed = self.clear_all()
        logger.info("LanceDB rebuild: removed %d rows, starting re-index", removed)

        memories = getattr(engine, "_memories", {})
        if not memories:
            logger.warning("LanceDB rebuild: engine._memories is empty — nothing to rebuild")
            return 0

        rebuilt = 0

        for mid, mem_data in memories.items():
            if rebuilt >= _max_batch:
                logger.info(
                    "LanceDB rebuild: hit batch limit (%d), %d remaining — deferring",
                    _max_batch,
                    len(memories) - rebuilt,
                )
                break

            content = mem_data.get("content", "")
            if not content or not content.strip():
                continue

            try:
                vector = self._embedder.embed(content)
            except Exception as e:
                logger.warning("LanceDB rebuild: embed failed for %s — %s (skipping)", mid, e)
                continue

            tier = mem_data.get("tier", "L1")
            category = mem_data.get("category", "other")
            scope = mem_data.get("scope", "global")

            try:
                self.insert(mid, vector, content, tier=tier, category=category, scope=scope)
                rebuilt += 1
            except Exception as e:
                logger.error("LanceDB rebuild: insert failed for %s: %s", mid, e)

        logger.info("LanceDB rebuild: complete — %d memories re-indexed", rebuilt)
        return rebuilt

    def backfill(self, engine: object) -> int:
        """Backfill LanceDB from SQLite for memories missing vectors.

        Called once during ContextEngine initialization. Only runs if
        the LanceDB table has fewer entries than SQLite.

        Safety: limits per-call backfill to MAX_BACKFILL_PER_CALL (50) to
        avoid blocking auto_context_inject / session-init on cold start.
        Remaining items are left for incremental backfill by later calls.

        Safety: per-item error handling — skips individual failures, rate-limited by caller.

        Args:
            engine: ContextEngine instance with list_memories().

        Returns:
            Number of memories backfilled.
        """
        import os as _os

        _max_backfill = int(_os.environ.get("LDB_BACKFILL_MAX_PER_CALL", "50"))

        ldb_count = self.count_rows()
        sqlite_count = getattr(engine, "memory_count", 0)
        if ldb_count >= sqlite_count:
            logger.info(
                "LanceDB backfill: table has %d rows, SQLite has %d — skip", ldb_count, sqlite_count
            )
            return 0

        logger.info(
            "LanceDB backfill: %d in LDB < %d in SQLite — starting (max %d per call)",
            ldb_count,
            sqlite_count,
            _max_backfill,
        )
        # Use engine._memories dict directly (already loaded from SQLite) — avoids redundant re-query
        memories = getattr(engine, "_memories", {})
        backfilled = 0
        for mid, mem_data in memories.items():
            if backfilled >= _max_backfill:
                logger.info(
                    "LanceDB backfill: hit per-call limit (%d), deferring remaining", _max_backfill
                )
                break
            content = mem_data.get("content", "")
            if not content or not content.strip():
                continue
            # Check if already in LanceDB
            try:
                existing = (
                    self._table.search()
                    .where(f"memory_id = '{mid}'", prefilter=True)
                    .limit(1)
                    .to_list()
                )
                if existing:
                    continue
            except Exception:
                pass
            # Per-item embed — skip failures, continue with next
            try:
                vec = self._embedder.embed(content)
                self.insert(
                    memory_id=mid,
                    vector=vec,
                    text=content,
                    tier=mem_data.get("tier", "L1"),
                    category=mem_data.get("category", "other"),
                    scope=mem_data.get("scope", "global"),
                )
                backfilled += 1
                if backfilled % 10 == 0:
                    logger.info("LanceDB backfill: %d/%d done", backfilled, len(memories))
            except Exception as e:
                logger.warning("LanceDB backfill: embed failed for %s — %s (skipping)", mid, e)
        logger.info("LanceDB backfill: %d memories indexed (remaining deferred)", backfilled)
        return backfilled
