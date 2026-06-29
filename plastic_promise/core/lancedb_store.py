"""LanceDBStore — persistent vector storage with ANN + FTS search.

Table: memory_vectors (memory_id, vector, text, tier, category, scope)
Vector dim: 1024 (mxbai-embed-large), configurable via PP_EMBEDDING_DIM.
"""

import os
import logging
from typing import Optional

import pyarrow as pa
import lancedb

from plastic_promise.core.embedder import Embedder, get_embedder

logger = logging.getLogger("plastic-promise.lancedb")

EMB_DIM = int(os.environ.get("PP_EMBEDDING_DIM", "1024"))
TABLE_NAME = "memory_vectors"

_MEMORY_VECTORS_SCHEMA = pa.schema([
    pa.field("memory_id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), EMB_DIM)),
    pa.field("text", pa.string()),
    pa.field("tier", pa.string()),
    pa.field("category", pa.string()),
    pa.field("scope", pa.string()),
])


class LanceDBStore:
    """Persistent vector store backed by LanceDB.

    Provides ANN vector search, FTS text search, and CRUD operations.
    Table is created on first access if it doesn't exist.
    """

    def __init__(self, db_path: str, embedder: Embedder) -> None:
        self._path = db_path
        self._embedder = embedder
        self._db: Optional[lancedb.DBConnection] = None
        self._table: Optional[lancedb.table.Table] = None
        self._fts_ready = False
        self._init_db()

    def _init_db(self) -> None:
        """Open or create LanceDB database and table."""
        os.makedirs(self._path, exist_ok=True)
        self._db = lancedb.connect(self._path)
        try:
            self._table = self._db.open_table(TABLE_NAME)
            logger.info("LanceDB: opened existing table '%s' (%d rows)",
                        TABLE_NAME, self._table.count_rows())
        except Exception:
            self._table = self._db.create_table(
                TABLE_NAME, schema=_MEMORY_VECTORS_SCHEMA, data=[]
            )
            logger.info("LanceDB: created table '%s'", TABLE_NAME)
        self._ensure_fts()

    def _ensure_fts(self) -> None:
        """Create FTS index on 'text' column if not already present."""
        try:
            self._table.create_fts_index("text", replace=False)
            self._fts_ready = True
            logger.info("LanceDB: FTS index ready on 'text'")
        except Exception as e:
            logger.warning("LanceDB: FTS index not available (%s), using fallback", e)
            self._fts_ready = False

    def search(
        self,
        vector: list[float],
        k: int = 20,
        scope: Optional[str] = None,
        tier: Optional[str] = None,
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
                results.append((mid, max(0.0, min(1.0, sim)),
                                row.get("text", ""),
                                row.get("tier", "L1"),
                                row.get("scope", "global")))
            return results
        except Exception as e:
            logger.error("LanceDB vector search failed: %s", e)
            return []

    def search_fts(
        self,
        query: str,
        k: int = 20,
        scope: Optional[str] = None,
    ) -> list[tuple[str, float, str, str, str]]:
        """Full-text search with fallback to LIKE-based filtering.

        Args:
            query: Text query string.
            k: Max results to return.
            scope: Optional scope filter.

        Returns:
            List of (memory_id, score, text, tier, scope).
        """
        if self._table is None:
            return []
        try:
            if self._fts_ready:
                raw = self._table.search(query, query_type="fts").limit(k).to_list()
            else:
                # Fallback: substring match via pyarrow compute
                pattern = f"%{query}%"
                raw = self._table.search().where(
                    f"text LIKE '{pattern}'", prefilter=True
                ).limit(k).to_list()
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
                results.append((mid, max(0.0, min(1.0, score)),
                                row.get("text", ""),
                                row.get("tier", "L1"),
                                row.get("scope", "global")))
            return results
        except Exception as e:
            logger.warning("LanceDB FTS search failed: %s", e)
            return []

    def search_similar(
        self, vector: list[float], k: int = 5,
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

    def check_duplicate(
        self, vector: list[float], threshold: float = 0.85,
    ) -> Optional[str]:
        """Return memory_id of the nearest match if similarity >= threshold, else None.

        Thin wrapper over search_similar(k=1). Used by pipeline dedup (Task 3).
        """
        results = self.search_similar(vector, k=1)
        if results and results[0][1] >= threshold:
            return results[0][0]
        return None

    def insert(
        self, memory_id: str, vector: list[float], text: str,
        tier: str = "L1", category: str = "other", scope: str = "global",
    ) -> None:
        """Insert a vector row. No-op if memory_id already exists."""
        if self._table is None:
            return
        try:
            existing = self._table.search().where(
                f"memory_id = '{memory_id}'", prefilter=True
            ).limit(1).to_list()
            if existing:
                return  # already exists, skip
            self._table.add([{
                "memory_id": memory_id,
                "vector": vector,
                "text": text,
                "tier": tier,
                "category": category,
                "scope": scope,
            }])
        except Exception as e:
            logger.error("LanceDB insert failed for %s: %s", memory_id, e)

    def update(
        self, memory_id: str, vector: list[float], text: str,
        tier: str = "L1", category: str = "other", scope: str = "global",
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

    def backfill(self, engine: object) -> int:
        """Backfill LanceDB from SQLite for memories missing vectors.

        Called once during ContextEngine initialization. Only runs if
        the LanceDB table has fewer entries than SQLite.

        Args:
            engine: ContextEngine instance with list_memories().

        Returns:
            Number of memories backfilled.
        """
        ldb_count = self.count_rows()
        sqlite_count = getattr(engine, 'memory_count', 0)
        if ldb_count >= sqlite_count:
            logger.info("LanceDB backfill: table has %d rows, SQLite has %d — skip",
                        ldb_count, sqlite_count)
            return 0

        logger.info("LanceDB backfill: %d in LDB < %d in SQLite — starting",
                    ldb_count, sqlite_count)
        records = engine.list_memories(limit=10000)
        backfilled = 0
        for r in records:
            mid = r.id
            # Check if already in LanceDB
            try:
                existing = self._table.search().where(
                    f"memory_id = '{mid}'", prefilter=True
                ).limit(1).to_list()
                if existing:
                    continue
            except Exception:
                pass
            # Generate embedding and insert
            try:
                vec = self._embedder.embed(r.content)
                self.insert(
                    memory_id=mid,
                    vector=vec,
                    text=r.content,
                    tier=getattr(r, 'tier', 'L1'),
                    category=getattr(r, 'category', 'other'),
                    scope=getattr(r, 'scope', 'global'),
                )
                backfilled += 1
                if backfilled % 10 == 0:
                    logger.info("LanceDB backfill: %d/%d done", backfilled, len(records))
            except Exception as e:
                logger.warning("LanceDB backfill: skip %s — %s", mid, e)
        logger.info("LanceDB backfill complete: %d memories indexed", backfilled)
        return backfilled
