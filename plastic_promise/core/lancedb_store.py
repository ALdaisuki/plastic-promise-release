"""LanceDBStore — persistent vector storage with ANN + FTS search.

Table: memory_vectors (memory_id, vector, text, tier, category, scope)
Vector dim: 1024 (mxbai-embed-large), configurable via PP_EMBEDDING_DIM.
"""

import logging
import math
import os
from collections.abc import Iterable

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from plastic_promise.core.embedder import Embedder
from plastic_promise.core.memory_index import (
    IndexMaterial,
    IndexMaterialError,
    effective_embedding_model_name,
    embedding_model_family,
    metadata_with_index_material,
    prepare_index_material,
    read_persisted_index_material,
    resolve_index_material,
)
from plastic_promise.core.synthesis_retrieval import synthesis_index_eligible

logger = logging.getLogger("plastic-promise.lancedb")

EMB_DIM = int(os.environ.get("PP_EMBEDDING_DIM", "1024"))
TABLE_NAME = "memory_vectors"
_BULK_VECTOR_CHUNK_SIZE = 256

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
        self._last_search_diagnostics: list[dict[str, str]] = []
        self._index_diagnostics: list[dict[str, str]] = []
        self._index_failures: list[dict[str, str]] = []
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

            self._table.create_index("text", config=FTS(), replace=True)
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
        self._last_search_diagnostics = []
        try:
            if self._fts_ready:
                raw = (
                    self._table.search(query, query_type="fts")
                    .select(["memory_id", "text", "tier", "category", "scope", "_score"])
                    .limit(k)
                    .to_list()
                )
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
                if "_distance" in row:
                    raw_score = row.get("_distance", 0.5)
                    score = (
                        1.0 - min(float(raw_score), 1.0)
                        if isinstance(raw_score, (int, float))
                        else 0.5
                    )
                else:
                    raw_score = row.get("_score", 0.5)
                    score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.5
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
            self._last_search_diagnostics = [
                {
                    "channel": "fts",
                    "reason": "lancedb_fts_query_failed",
                    "error_class": e.__class__.__name__,
                }
            ]
            return []

    def consume_search_diagnostics(self) -> list[dict[str, str]]:
        """Return and clear structured diagnostics from the latest search call."""
        diagnostics = list(self._last_search_diagnostics)
        self._last_search_diagnostics = []
        return diagnostics

    def fts_search(
        self, query: str, k: int = 20, scope: str | None = None
    ) -> list[tuple[str, float, str, str, str]]:
        """Convenience wrapper around search_fts returning top-k results.

        Returns list of (memory_id, score, text, tier, scope).
        """
        return self.search_fts(query, k, scope)

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
            escaped_memory_id = str(memory_id).replace("'", "''")
            rows = (
                self._table.search()
                .where(f"memory_id = '{escaped_memory_id}'", prefilter=True)
                .limit(1)
                .to_list()
            )
            if rows and rows[0].get("vector"):
                return list(rows[0]["vector"])
            return None
        except Exception:
            return None

    def get_vectors(self, memory_ids: Iterable[str]) -> dict[str, list[float]]:
        """Return stored vectors for the requested memory IDs in bounded batches."""
        if self._table is None:
            return {}

        requested_ids = list(dict.fromkeys(str(memory_id) for memory_id in memory_ids))
        vectors: dict[str, list[float]] = {}
        for offset in range(0, len(requested_ids), _BULK_VECTOR_CHUNK_SIZE):
            batch = requested_ids[offset : offset + _BULK_VECTOR_CHUNK_SIZE]
            if not batch:
                continue
            quoted_ids = ", ".join(
                f"'{memory_id.replace(chr(39), chr(39) * 2)}'" for memory_id in batch
            )
            try:
                rows = (
                    self._table.search()
                    .where(f"memory_id IN ({quoted_ids})", prefilter=True)
                    .select(["memory_id", "vector"])
                    .limit(len(batch))
                    .to_list()
                )
            except Exception as exc:
                logger.warning("LanceDB bulk vector lookup failed: %s", exc)
                continue
            for row in rows:
                memory_id = str(row.get("memory_id", ""))
                vector = row.get("vector")
                if memory_id in batch and vector:
                    vectors[memory_id] = list(vector)
        return vectors

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
        try:
            self.insert_checked(memory_id, vector, text, tier, category, scope)
        except Exception as e:
            logger.error("LanceDB insert failed for %s: %s", memory_id, e)

    def insert_checked(
        self,
        memory_id: str,
        vector: list[float],
        text: str,
        tier: str = "L1",
        category: str = "other",
        scope: str = "global",
    ) -> None:
        """Insert a vector row and propagate backend failures to repair workers."""
        if self._vectors_disabled:
            raise RuntimeError("lancedb_vectors_disabled")
        if self._table is None:
            raise RuntimeError("lancedb_table_unavailable")
        escaped_memory_id = str(memory_id).replace("'", "''")
        existing = (
            self._table.search()
            .where(f"memory_id = '{escaped_memory_id}'", prefilter=True)
            .limit(1)
            .to_list()
        )
        if existing:
            return
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

    def replace_checked(
        self,
        memory_id: str,
        vector: list[float],
        text: str,
        tier: str = "L1",
        category: str = "other",
        scope: str = "global",
    ) -> None:
        """Replace one vector row and propagate failures to repair workers."""
        if self._vectors_disabled:
            raise RuntimeError("lancedb_vectors_disabled")
        if self._table is None:
            raise RuntimeError("lancedb_table_unavailable")
        escaped_memory_id = str(memory_id).replace("'", "''")
        existing = (
            self._table.search()
            .where(f"memory_id = '{escaped_memory_id}'", prefilter=True)
            .limit(2)
            .to_list()
        )
        desired = {
            "memory_id": memory_id,
            "vector": vector,
            "text": text,
            "tier": tier,
            "category": category,
            "scope": scope,
        }
        if len(existing) == 1 and self._checked_row_matches(existing[0], desired):
            return
        if existing:
            self.delete_checked(memory_id)
        self._table.add([desired])

    @staticmethod
    def _checked_row_matches(existing: dict, desired: dict) -> bool:
        if any(existing.get(key) != desired[key] for key in ("text", "tier", "category", "scope")):
            return False
        existing_vector = existing.get("vector")
        desired_vector = desired["vector"]
        if not isinstance(existing_vector, (list, tuple)) or len(existing_vector) != len(
            desired_vector
        ):
            return False
        try:
            return all(
                math.isclose(float(left), float(right), rel_tol=1e-6, abs_tol=1e-7)
                for left, right in zip(existing_vector, desired_vector, strict=True)
            )
        except (TypeError, ValueError):
            return False

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
        try:
            self.delete_checked(memory_id)
        except Exception as e:
            logger.error("LanceDB delete failed for %s: %s", memory_id, e)

    def delete_checked(self, memory_id: str) -> None:
        """Delete a vector row and propagate backend failures to repair workers."""
        if self._table is None:
            raise RuntimeError("lancedb_table_unavailable")
        escaped_memory_id = str(memory_id).replace("'", "''")
        self._table.delete(f"memory_id = '{escaped_memory_id}'")

    def count_rows(self) -> int:
        """Return total rows in the table."""
        if self._table is None:
            return 0
        try:
            return self._table.count_rows()
        except Exception:
            return 0

    def list_memory_ids(self) -> set[str]:
        """Return memory IDs currently present in LanceDB."""
        if self._table is None:
            return set()
        try:
            table = self._table.to_arrow()
            if "memory_id" not in table.column_names:
                return set()
            return {str(mid) for mid in table.column("memory_id").to_pylist() if mid}
        except Exception as e:
            logger.warning("LanceDB list_memory_ids failed: %s", e)
            return set()

    def sync_with_engine(self, engine: object) -> dict:
        """Repair ID-level drift between SQLite memories and LanceDB rows."""
        self._reset_index_diagnostics()
        memories = self._canonical_engine_memories(engine)
        if memories is None:
            return {
                "orphan_deleted": 0,
                "missing_backfilled": 0,
                "missing_skipped": 0,
                "orphan_ids": [],
                "missing_ids": [],
                "canonical_unavailable": True,
            }
        eligible_memories = self._eligible_engine_memories(engine, memories)
        sqlite_ids = set(eligible_memories)
        ldb_ids = self.list_memory_ids()

        orphan_ids = sorted(ldb_ids - sqlite_ids)
        missing_ids = [mid for mid in eligible_memories if mid not in ldb_ids]
        model_name = self._embedding_model_name()
        # Ordinary index material is owned by this repair path. Governed
        # synthesis material is owned by synthesis_maintenance and may be
        # stale while its durable outbox job is pending; do not replace or
        # delete that row as a side effect of ordinary chunking migration.
        stale_ids = [
            mid
            for mid in sorted(sqlite_ids & ldb_ids)
            if str(eligible_memories[mid].get("memory_type") or "").strip().casefold()
            != "synthesis"
            if read_persisted_index_material(eligible_memories[mid], model_name=model_name) is None
            and read_persisted_index_material(eligible_memories[mid]) is not None
        ]

        orphan_deleted = 0
        for mid in orphan_ids:
            if self._delete_repair_row(mid):
                orphan_deleted += 1

        missing_backfilled = 0
        missing_skipped = 0
        for mid in missing_ids:
            if self._insert_engine_memory(engine, mid, eligible_memories.get(mid, {})):
                missing_backfilled += 1
            else:
                missing_skipped += 1

        stale_reindexed = 0
        for mid in stale_ids:
            if self._insert_engine_memory(
                engine,
                mid,
                eligible_memories.get(mid, {}),
                replace_existing=True,
            ):
                stale_reindexed += 1

        # Legacy materialization can change a source embedding hash. Recheck
        # after repair so a synthesis that became stale during this pass cannot
        # remain in the derived index.
        final_eligible_ids = set(self._eligible_engine_memories(engine, memories))
        late_orphan_ids = sorted(self.list_memory_ids() - final_eligible_ids)
        for mid in late_orphan_ids:
            if mid in orphan_ids:
                continue
            if self._delete_repair_row(mid):
                orphan_deleted += 1
        orphan_ids = sorted(set(orphan_ids) | set(late_orphan_ids))

        result = {
            "orphan_deleted": orphan_deleted,
            "missing_backfilled": missing_backfilled,
            "missing_skipped": missing_skipped,
            "orphan_ids": orphan_ids,
            "missing_ids": missing_ids,
        }
        if self._index_failures:
            result["diagnostics"] = list(self._index_failures)
        if stale_ids:
            result["stale_reindexed"] = stale_reindexed
            result["stale_ids"] = stale_ids
        return result

    def clear_all(self) -> int:
        """Delete all rows from the table and return the count that was removed.

        After clearing, the table is empty but still exists with its schema
        and FTS index intact.
        """
        try:
            return self.clear_all_checked()
        except Exception as e:
            logger.error("LanceDB clear_all failed: %s", e)
            return 0

    def clear_all_checked(self) -> int:
        """Delete every vector row and propagate backend failures to repair workers."""
        if self._table is None:
            raise RuntimeError("lancedb_table_unavailable")
        count = self._table.count_rows()
        if count > 0:
            self._table.delete("memory_id IS NOT NULL")
        logger.info("LanceDB: cleared %d rows", count)
        return count

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

        self._reset_index_diagnostics()

        _max_batch = int(_os.environ.get("LDB_REBUILD_MAX_PER_CALL", "200"))

        canonical_memories = self._canonical_engine_memories(engine)
        if canonical_memories is None:
            logger.warning("LanceDB rebuild: canonical SQLite memories unavailable")
            return 0

        eligible_memories = self._eligible_engine_memories(engine, canonical_memories)
        eligible_synthesis_ids = {
            mid
            for mid, memory in eligible_memories.items()
            if str(memory.get("memory_type") or "").strip().casefold() == "synthesis"
        }
        try:
            if eligible_synthesis_ids:
                # Synthesis rows are maintained by the durable synthesis
                # index queue. Preserve currently eligible rows so a model or
                # chunking migration cannot erase a usable derived index.
                removed = 0
                for memory_id in sorted(self.list_memory_ids() - eligible_synthesis_ids):
                    if self._delete_repair_row(memory_id):
                        removed += 1
            else:
                removed = self.clear_all_checked()
        except Exception as exc:
            self._record_index_diagnostic("__table__", "lancedb_clear_failed", failed=True)
            logger.error("LanceDB rebuild: clear failed: %s", exc)
            return 0
        logger.info("LanceDB rebuild: removed %d rows, starting re-index", removed)

        memories = eligible_memories
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

            try:
                if not self._insert_engine_memory(engine, mid, mem_data):
                    continue
                rebuilt += 1
            except Exception as e:
                logger.error("LanceDB rebuild: insert failed for %s: %s", mid, e)

        logger.info("LanceDB rebuild: complete — %d memories re-indexed", rebuilt)
        return rebuilt

    def _insert_engine_memory(
        self,
        engine: object,
        mid: str,
        mem_data: dict,
        *,
        replace_existing: bool = False,
    ) -> bool:
        if not self._memory_is_index_eligible(engine, mid, mem_data):
            return False

        model_name = self._embedding_model_name()
        is_governed_synthesis = (
            str(mem_data.get("memory_type") or "").strip().casefold() == "synthesis"
        )
        migrating_model = False
        try:
            material, needs_persist = resolve_index_material(
                mem_data,
                model_name=model_name,
            )
        except IndexMaterialError as exc:
            if str(exc) != "index_material_model_mismatch":
                self._record_index_diagnostic(mid, str(exc), failed=True)
                logger.warning("LanceDB sync: skipped %s (%s)", mid, exc)
                return False
            persisted = read_persisted_index_material(mem_data)
            if persisted is None or embedding_model_family(
                persisted.model_name
            ) != embedding_model_family(model_name):
                self._record_index_diagnostic(mid, str(exc), failed=True)
                logger.warning("LanceDB sync: skipped %s (%s)", mid, exc)
                return False
            if is_governed_synthesis:
                self._record_index_diagnostic(
                    mid,
                    "synthesis_index_material_migration_deferred",
                    failed=False,
                )
                return False
            material = prepare_index_material(
                mem_data,
                embedder=self._embedder,
                policy=persisted.policy,
                model_name=model_name,
            )
            needs_persist = True
            migrating_model = True
            self._record_index_diagnostic(mid, "index_material_model_migrated", failed=False)
        if needs_persist and not migrating_model:
            material = prepare_index_material(
                mem_data,
                embedder=self._embedder,
                policy=material.policy,
                model_name=model_name,
            )
        if not material.vector_text.strip() or not material.search_text.strip():
            self._record_index_diagnostic(mid, "index_material_incomplete", failed=True)
            return False
        try:
            if needs_persist and not migrating_model:
                self._record_index_diagnostic(
                    mid,
                    "index_material_legacy_materialized",
                    failed=False,
                )
                logger.warning(
                    "LanceDB sync: explicitly materializing pre-v2 index contract for %s",
                    mid,
                )
                if not self._persist_index_material(
                    engine,
                    mid,
                    mem_data,
                    material,
                ):
                    self._record_index_diagnostic(
                        mid,
                        "index_material_persist_failed",
                        failed=True,
                    )
                    return False
            vector = self._embedder.embed(material.vector_text)
            if migrating_model:
                canonical = self._canonical_index_memory(engine, mid)
                if canonical is not None and not self._memory_is_index_eligible(
                    engine, mid, canonical
                ):
                    canonical = None
            else:
                canonical = self._validated_canonical_index_memory(
                    engine,
                    mid,
                    material,
                    model_name=model_name,
                )
            if canonical is None:
                self._record_index_diagnostic(
                    mid,
                    "index_material_changed_during_index",
                    failed=True,
                )
                return False
            try:
                insert = self.replace_checked if replace_existing else self.insert_checked
                insert(
                    memory_id=mid,
                    vector=vector,
                    text=material.search_text,
                    tier=canonical.get("tier", "L1"),
                    category=canonical.get("category", "other"),
                    scope=canonical.get("scope", "global"),
                )
            except Exception as exc:
                self._record_index_diagnostic(mid, "lancedb_insert_failed", failed=True)
                logger.warning("LanceDB sync: insert failed for %s: %s", mid, exc)
                return False
            if migrating_model and not self._persist_index_material(
                engine, mid, mem_data, material
            ):
                self._delete_repair_row(mid)
                self._record_index_diagnostic(mid, "index_material_persist_failed", failed=True)
                return False
            if (
                self._validated_canonical_index_memory(
                    engine,
                    mid,
                    material,
                    model_name=model_name,
                )
                is None
            ):
                self._delete_repair_row(mid)
                self._record_index_diagnostic(
                    mid,
                    "index_material_changed_during_index",
                    failed=True,
                )
                return False
            return True
        except IndexMaterialError as exc:
            self._record_index_diagnostic(mid, str(exc), failed=True)
            logger.warning("LanceDB sync: skipped %s (%s)", mid, exc)
            return False
        except Exception as e:
            self._record_index_diagnostic(mid, "lancedb_backfill_failed", failed=True)
            logger.warning("LanceDB sync: failed to backfill %s: %s", mid, e)
            return False

    def _delete_repair_row(self, memory_id: str) -> bool:
        try:
            self.delete_checked(memory_id)
        except Exception as exc:
            self._record_index_diagnostic(memory_id, "lancedb_delete_failed", failed=True)
            logger.warning("LanceDB repair: delete failed for %s: %s", memory_id, exc)
            return False
        return True

    def _reset_index_diagnostics(self) -> None:
        self._index_diagnostics = []
        self._index_failures = []

    def _record_index_diagnostic(
        self,
        memory_id: str,
        reason: str,
        *,
        failed: bool,
    ) -> None:
        diagnostic = {"memory_id": str(memory_id), "reason": str(reason)}
        if diagnostic not in self._index_diagnostics:
            self._index_diagnostics.append(diagnostic)
        if failed and diagnostic not in self._index_failures:
            self._index_failures.append(diagnostic)

    @property
    def index_diagnostics(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(item) for item in self._index_diagnostics)

    def _validated_canonical_index_memory(
        self,
        engine: object,
        memory_id: str,
        expected_material: IndexMaterial,
        *,
        model_name: str,
    ) -> dict | None:
        canonical = self._canonical_index_memory(engine, memory_id)
        if canonical is None or not self._memory_is_index_eligible(
            engine,
            memory_id,
            canonical,
        ):
            return None
        material, needs_persist = resolve_index_material(
            canonical,
            model_name=model_name,
        )
        if needs_persist or material != expected_material:
            return None
        return canonical

    def _eligible_engine_memories(
        self,
        engine: object,
        memories: dict,
    ) -> dict[str, dict]:
        ordinary: list[tuple[str, dict]] = []
        syntheses: list[tuple[str, dict]] = []
        for mid, mem_data in memories.items():
            if not isinstance(mem_data, dict):
                continue
            if not self._memory_is_index_eligible(engine, mid, mem_data):
                continue
            memory_type = str(mem_data.get("memory_type", "experience")).strip().casefold()
            target = syntheses if memory_type == "synthesis" else ordinary
            target.append((mid, mem_data))
        return dict(sorted(ordinary) + sorted(syntheses))

    def _canonical_engine_memories(self, engine: object) -> dict[str, dict] | None:
        runtime_memories = getattr(engine, "_memories", {}) or {}
        sqlite_store = getattr(engine, "_sqlite", None)
        if sqlite_store is None:
            return runtime_memories

        iter_all = getattr(sqlite_store, "iter_all", None)
        if not callable(iter_all):
            logger.warning("LanceDB repair: canonical SQLite iterator unavailable")
            return None
        try:
            canonical = dict(iter_all())
        except Exception as exc:
            logger.warning("LanceDB repair: canonical SQLite load failed: %s", exc)
            return None

        self._sync_runtime_cache(engine, canonical)
        return canonical

    @staticmethod
    def _sync_runtime_cache(engine: object, canonical: dict[str, dict]) -> None:
        runtime = getattr(engine, "_memories", None)
        if not isinstance(runtime, dict):
            return

        def apply() -> None:
            for memory_id in set(runtime) - set(canonical):
                runtime.pop(memory_id, None)
            for memory_id, row in canonical.items():
                current = runtime.get(memory_id)
                if isinstance(current, dict):
                    current.clear()
                    current.update(row)
                else:
                    runtime[memory_id] = dict(row)

        lock = getattr(engine, "_write_lock", None)
        if lock is None:
            apply()
        else:
            with lock:
                apply()

    @staticmethod
    def _canonical_connection(engine: object):
        sqlite_store = getattr(engine, "_sqlite", None)
        return getattr(sqlite_store, "_conn", None)

    @staticmethod
    def _canonical_index_memory(engine: object, memory_id: str) -> dict | None:
        sqlite_store = getattr(engine, "_sqlite", None)
        if sqlite_store is not None:
            getter = getattr(sqlite_store, "get", None)
            if not callable(getter):
                return None
            try:
                memory = getter(memory_id)
            except Exception:
                return None
            return dict(memory) if isinstance(memory, dict) else None
        runtime = getattr(engine, "_memories", None)
        if not isinstance(runtime, dict):
            return None
        memory = runtime.get(memory_id)
        return dict(memory) if isinstance(memory, dict) else None

    def _memory_is_index_eligible(
        self,
        engine: object,
        memory_id: str,
        memory: dict,
    ) -> bool:
        canonical_type = str(memory.get("memory_type", "experience")).strip().casefold()
        if not canonical_type:
            return False
        conn = self._canonical_connection(engine)
        if canonical_type == "synthesis":
            if conn is None:
                return False
            return synthesis_index_eligible(conn, memory_id)
        if conn is None:
            return True
        try:
            has_control = (
                conn.execute(
                    "SELECT 1 FROM synthesis_artifacts WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                is not None
            )
        except Exception:
            return False
        if has_control:
            return synthesis_index_eligible(conn, memory_id)
        return True

    def _embedding_model_name(self) -> str:
        return effective_embedding_model_name(self._embedder)

    @staticmethod
    def _persist_index_material(
        engine: object,
        memory_id: str,
        memory: dict,
        material: IndexMaterial,
    ) -> bool:
        fields = {
            "embedding_text": material.vector_text,
            "search_text": material.search_text,
            "embedding_hash": material.embedding_hash,
            "metadata_json": metadata_with_index_material(
                memory.get("metadata_json"),
                material,
            ),
        }
        material_fields = (
            "content",
            "memory_type",
            "raw_content",
            "l0_abstract",
            "l1_summary",
            "l2_content",
            "embedding_text",
            "embedding_hash",
            "search_text",
            "metadata_json",
            "project_id",
            "visibility",
            "source_class",
        )

        def persist() -> bool:
            sqlite_store = getattr(engine, "_sqlite", None)
            if sqlite_store is None:
                current = dict(memory)
            else:
                getter = getattr(sqlite_store, "get", None)
                if not callable(getter):
                    return False
                try:
                    current = getter(memory_id)
                except Exception:
                    return False
                if not isinstance(current, dict):
                    return False
                conn = getattr(sqlite_store, "_conn", None)
                if conn is None:
                    return False
                try:
                    has_control = (
                        conn.execute(
                            "SELECT 1 FROM synthesis_artifacts WHERE memory_id = ?",
                            (memory_id,),
                        ).fetchone()
                        is not None
                    )
                except Exception:
                    return False
                if has_control:
                    return False

            if str(current.get("memory_type") or "").strip().casefold() == "synthesis":
                return False
            if any(current.get(field) != memory.get(field) for field in material_fields):
                return False

            if sqlite_store is not None:
                from plastic_promise.core.synthesis import synthesis_content_hash

                patch = getattr(sqlite_store, "patch_ordinary", None)
                if not callable(patch):
                    return False
                try:
                    updated_memory = patch(
                        memory_id,
                        replacements=fields,
                        expected_project_id=str(current.get("project_id") or ""),
                        expected_content_hash=synthesis_content_hash(current.get("content")),
                        expected_embedding_hash=str(current.get("embedding_hash") or ""),
                    )
                except Exception:
                    return False
            else:
                updated_memory = dict(current)
                updated_memory.update(fields)

            memory.update(fields)
            runtime = getattr(engine, "_memories", None)
            if isinstance(runtime, dict):
                runtime[memory_id] = dict(updated_memory)
            return True

        lock = getattr(engine, "_write_lock", None)
        if lock is None:
            return persist()
        with lock:
            return persist()

    def backfill(self, engine: object) -> int:
        """Backfill LanceDB from SQLite for memories missing vectors.

        Called during ContextEngine initialization. Compares eligible IDs,
        because raw row counts can hide missing ordinary or verified memories.

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

        self._reset_index_diagnostics()

        _max_backfill = int(_os.environ.get("LDB_BACKFILL_MAX_PER_CALL", "50"))

        canonical_memories = self._canonical_engine_memories(engine)
        if canonical_memories is None:
            logger.warning("LanceDB backfill: canonical SQLite memories unavailable")
            return 0
        memories = self._eligible_engine_memories(engine, canonical_memories)
        ldb_ids = self.list_memory_ids()
        missing_ids = [mid for mid in memories if mid not in ldb_ids]
        if not missing_ids:
            logger.info("LanceDB backfill: no eligible IDs are missing")
            return 0

        logger.info(
            "LanceDB backfill: %d eligible IDs missing (max %d per call)",
            len(missing_ids),
            _max_backfill,
        )
        backfilled = 0
        for mid in missing_ids:
            if backfilled >= _max_backfill:
                logger.info(
                    "LanceDB backfill: hit per-call limit (%d), deferring remaining", _max_backfill
                )
                break
            try:
                if not self._insert_engine_memory(engine, mid, memories[mid]):
                    continue
                backfilled += 1
                if backfilled % 10 == 0:
                    logger.info("LanceDB backfill: %d/%d done", backfilled, len(memories))
            except Exception as e:
                logger.warning("LanceDB backfill: embed failed for %s — %s (skipping)", mid, e)
        logger.info("LanceDB backfill: %d memories indexed (remaining deferred)", backfilled)
        return backfilled
