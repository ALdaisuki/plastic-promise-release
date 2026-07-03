"""ContextEngine Python 回退实现

当 Rust context-engine-core 不可用时使用此纯 Python 版本。
接口与 Rust 版本保持一致，确保上层无感切换。

生产环境应使用 Rust 版本以获得更好性能。
"""

import datetime
import json
import logging
import math
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

from plastic_promise.core.constants import (
    CONTEXT_LAYERS,
    PRINCIPLE_INHERITANCE_DECAY,
    SYMBOL_RULE_KEYWORDS,
)
from plastic_promise.core.paths import get_db_path

# ============================================================
# 数据模型 (与 Rust 结构体一一对应)
# ============================================================


@dataclass
class ContextItem:
    """上下文包中的单个条目 — 含完整生命轨迹 (P3a + P3b)"""

    id: str
    content: str
    relevance: float
    source: str = ""
    freshness: str = "valid"
    layer: str = "related"
    is_principle: bool = False
    worth_score: float = 0.0
    is_auto_recall: bool = True  # True = internal retrieval, False = user-initiated
    # P3a: 发散联想灵感质量
    novelty_score: float = 0.0  # 与检索集中其他项的不相似度 [0,1]
    confidence: float = 0.5  # 检索置信度（来源质量+worth+相关性）
    inspiration_score: float = 0.0  # novelty * confidence（灵感综合分）
    # P3b: 生命轨迹
    adoption_count: int = 0  # 被采纳次数 (← worth_success)
    rejection_count: int = 0  # 被拒绝次数 (← worth_failure)
    times_retrieved: int = 0  # 被检索次数 (← access_count)
    decay_status: str = "healthy"  # fresh|healthy|stale|decaying|expired

    def to_prompt_line(self) -> str:
        """Render one context item with life-trajectory annotations (P3b)."""
        mark = " [PRINCIPLE]" if self.is_principle else ""
        traj = ""
        if self.adoption_count > 0 or self.rejection_count > 0:
            traj = f" [OK:{self.adoption_count} FAIL:{self.rejection_count}]"
        if self.decay_status in ("stale", "decaying", "expired"):
            traj += f" [DECAY:{self.decay_status}]"
        return f"- [{self.relevance:.2f}]{mark}{traj} [{self.source}] {self.content[:200]}"


@dataclass
class ContextPack:
    """三层上下文包"""

    core: list[ContextItem] = field(default_factory=list)
    related: list[ContextItem] = field(default_factory=list)
    divergent: list[ContextItem] = field(default_factory=list)
    activated_principles: list[str] = field(default_factory=list)
    audit_metadata: dict[str, str] = field(default_factory=dict)
    gap_signal: Optional["GapSignal"] = None  # knowledge-gap detection signal

    def to_prompt(self) -> str:
        lines = []
        if self.activated_principles:
            lines.append("## [PRINCIPLES] 核心约定参考（约定优于约束——决策前主动查阅）")
            from plastic_promise.core.constants import CORE_PRINCIPLES

            for name in self.activated_principles:
                # Find principle by name and show full reference
                match = next((p for p in CORE_PRINCIPLES if p["name"] == name), None)
                if match:
                    lines.append(f"### {name}")
                    lines.append(f"> {match['content']}")
                    lines.append(f"**[!!!] 违反后果**：{match.get('consequence', '未知后果')}")
                else:
                    lines.append(f"- {name}")
            lines.append("")
        if self.core:
            lines.append("## [CORE] 核心上下文（必读）")
            for item in self.core:
                lines.append(item.to_prompt_line())
            lines.append("")
        if self.related:
            lines.append("## [RELATED] 关联上下文（参考）")
            for item in self.related:
                lines.append(item.to_prompt_line())
            lines.append("")
        if self.divergent:
            lines.append("## [DIVERGENT] 发散联想（灵感）")
            for item in self.divergent:
                lines.append(item.to_prompt_line())
            lines.append("")
        return "\n".join(lines)

    @property
    def total_items(self) -> int:
        return len(self.core) + len(self.related) + len(self.divergent)


# ============================================================
# MemoryRecord — Python 实现 (与 Rust context_engine_core.MemoryRecord 接口一致)
# ============================================================


class MemoryRecord:
    """Python MemoryRecord — mirrors context_engine_core.MemoryRecord interface.

    Used as fallback when the Rust core is unavailable. Provides the same
    attributes and methods so tool handlers work transparently.
    """

    def __init__(
        self,
        id: str = "",
        content: str = "",
        memory_type: str = "experience",
        source: str = "user",
        owner: str = "",
    ):
        self.id = id
        self.content = content
        self.memory_type = memory_type
        self.source = source
        self.owner: str = owner or os.environ.get("AGENT_OWNER", "")
        self.scope: str = "global"  # deprecated — use domain
        self.category: str = "other"  # deprecated — use domain
        self.tags: list[str] = []  # NEW: 多标签
        self.domain: str = "uncategorized"  # NEW: 域标签
        self.importance: float = 0.7
        self.entity_ids: list[str] = []
        self.created_at: str = ""
        self.access_count: int = 0
        self.worth_success: int = 0
        self.worth_failure: int = 0
        self.tier: str = "L2"
        self.decay_multiplier: float = 1.0
        self.effective_half_life: float = 3.0

    def worth_score(self) -> float:
        """Calculate worth score from success/failure observations.

        Uses a Bayesian-like smoothing: (success + 1) / (total + 2).
        Returns 0.5 when no observations exist.
        """
        total = self.worth_success + self.worth_failure
        if total == 0:
            return 0.5
        return (self.worth_success + 1.0) / (total + 2.0)

    def record_adopted(self):
        """Record a positive adoption observation."""
        self.worth_success += 1

    def record_rejected(self):
        """Record a negative rejection observation."""
        self.worth_failure += 1

    def record_ignored(self):
        """Record a neutral ignore observation (weak negative)."""
        self.worth_failure += 0.5

    @property
    def total_observations(self) -> float:
        return self.worth_success + self.worth_failure


class GraphInfo:
    """Lightweight graph info object with node_count/edge_count attributes."""

    def __init__(self, nodes: dict, edges: list):
        self.node_count = len(nodes)
        self.edge_count = len(edges)
        self._nodes = nodes
        self._edges = edges

    def get(self, key: str):
        """Dict-like access for backward compatibility."""
        if key == "nodes":
            return self._nodes
        if key == "edges":
            return self._edges
        return None


# ============================================================
# ContextEngine — Python 实现
# ============================================================


class ContextEngine:
    """上下文供应引擎 (Python 回退版)

    生产环境请使用 Rust 版: from context_engine_core import ContextEngine
    """

    def __init__(self, use_sqlite: bool = None):
        self._graph_nodes: dict[str, dict[str, Any]] = {}
        self._graph_edges: list[dict[str, Any]] = []
        self._feedback: dict[
            str, float
        ] = {}  # item_id -> accumulated delta (P2: 替换为 worth_score)
        self.enable_principles: bool = True
        self._current_time: str = ""
        self._memories: dict[str, dict[str, Any]] = {}
        self._principle_anchors: dict[int, list[float]] = {}  # P1: 原则锚点向量

        # Heavy components — lazy-initialized by _ensure_heavy_init()
        self._dm: Any = None
        self._dm_ok: bool = False
        # _last_rerank_status removed — unified reranker handles its own diagnostics
        self._domain_hint: str | None = None
        self._embedder: Any = None
        self._ldb: Any = None

        # SQLite write-through — persists every mutation to disk (default ON)
        if use_sqlite is None:
            use_sqlite = os.environ.get("AGENT_USE_SQLITE", "1") != "0"
        self._sqlite = _SQLiteStorage() if use_sqlite else None
        if self._sqlite:
            # Load existing from disk
            for mid, data in self._sqlite.iter_all():
                self._memories[mid] = data

        # P0: Rebuild principle↔memory graph edges from persisted memories
        self._rebuild_graph_from_memories()

        # Heavy init deferred to first supply() call
        self._heavy_init_done = False
        self._heavy_init_lock = threading.Lock()
        # Write serialization lock — all write paths acquire this.
        # RLock (reentrant) because increment_field calls update_memory_fields,
        # and both acquire the lock.
        self._write_lock = threading.RLock()

        # Rust engine integration — stateless accelerator for supply()
        self._rust_healthy: bool | None = None  # None = unchecked, True = healthy, None on failure
        self._rust_health_checked_at: float = 0.0  # epoch timestamp of last health check
        self._rust_health_ttl: float = 300.0  # cache TTL in seconds (5 minutes)
        self._rust_engine_instance = None  # cached Rust engine instance (reused)
        self._rust_lock = threading.Lock()  # protects all _rust_* fields from concurrent access

    def _rebuild_graph_from_memories(self):
        """Rebuild graph edges from persisted memories on init.

        After loading memories from SQLite, reconstruct:
        1. ``memory → entity`` edges (relation: "references") so
           ``_graph_traversal`` can pull memories linked to activated entities.
        2. ``skill_session`` entity nodes for any skill-tagged memories
           that reference entity_ids not yet in the graph.
        """
        # Pass 1: memory → entity references edges
        for mid, mem in self._memories.items():
            entity_ids = mem.get("entity_ids", [])
            if not entity_ids:
                continue
            for eid in entity_ids:
                # Determine the correct node prefix for this entity
                if eid.startswith("skill:"):
                    node_id = f"skill_session:{eid}"
                elif eid.startswith("principle:") or eid.startswith("task:"):
                    node_id = eid  # already prefixed
                else:
                    # Generic entity — store as-is or skip unknown patterns
                    node_id = eid

                edge = {
                    "from": mid,
                    "to": node_id,
                    "relation": "references",
                    "weight": 0.6,
                }
                if edge not in self._graph_edges:
                    self._graph_edges.append(edge)

        # Pass 2: ensure skill_session nodes exist for orphan entity_ids
        for mid, mem in self._memories.items():
            entity_ids = mem.get("entity_ids", [])
            tags = mem.get("tags", [])
            for eid in entity_ids:
                if not eid.startswith("skill:"):
                    continue
                node_id = f"skill_session:{eid}"
                if node_id in self._graph_nodes:
                    continue
                # Extract skill name from entity_id
                # Format: skill:<skill_name>:<timestamp>
                parts = eid.split(":")
                skill_name = parts[1] if len(parts) >= 2 else "unknown"
                self._graph_nodes[node_id] = {
                    "type": "skill_session",
                    "name": skill_name,
                    "description": mem.get("content", "")[:200],
                }

        # Pass 3: principle ↔ memory edges (P0 deep grammar)
        total_edges = 0
        for mid, mem in self._memories.items():
            total_edges += self._build_principle_edges_for_memory(mid, mem)
        if total_edges > 0:
            logging.info(
                "_rebuild_graph_from_memories: created %d principle↔memory edges from %d memories",
                total_edges,
                len(self._memories),
            )

    def _ensure_heavy_init(self):
        """Lazy-initialize heavy components: DomainManager, LanceDB, embedder, principle anchors.

        Called once on first supply() call. Avoids expensive embedding/DB init at ContextEngine
        construction time — critical for fast session-init and high-concurrency scenarios.

        Thread-safe: uses a lock to prevent race conditions when concurrent
        requests both hit the first supply() call simultaneously.
        """
        # Fast path — already initialized by another thread
        if self._heavy_init_done:
            return
        with self._heavy_init_lock:
            # Double-check after acquiring lock — another thread may have finished init
            if self._heavy_init_done:
                return

            # DB path — used by both DomainManager and LanceDBStore
            db_path = get_db_path()

            # DomainManager for domain-weighted retrieval
            try:
                from plastic_promise.core.domain_manager import DomainManager

                self._dm = DomainManager(db_path=db_path)
                self._dm_ok = True
            except Exception as e:
                logging.error(f"DomainManager init failed: {e} — domain features disabled")
                self._dm = None
                self._dm_ok = False

            # P1: Store embedder for principle anchor computation and intent matching
            if self._embedder is None:
                try:
                    from plastic_promise.core.embedder import get_embedder

                    self._embedder = get_embedder(fallback_on_error=True)
                except Exception:
                    logging.warning(
                        "ContextEngine: embedder unavailable — intent matching disabled"
                    )

            # Initialize LanceDB vector store
            if self._ldb is None:
                try:
                    from plastic_promise.core.lancedb_store import LanceDBStore

                    ldb_path = os.environ.get(
                        "PLASTIC_LANCEDB_PATH",
                        os.path.join(
                            os.path.dirname(db_path or "plastic_memory.db"),
                            "plastic_memory.lancedb",
                        ),
                    )
                    self._ldb = LanceDBStore(
                        ldb_path, self._embedder or get_embedder(fallback_on_error=True)
                    )
                    self._ldb.backfill(self)
                    # Ghost-vector detection: if LanceDB has more rows than SQLite,
                    # there are stale test/pollution vectors — rebuild from SQLite
                    ldb_count = self._ldb.count_rows()
                    sqlite_count = len(self._memories)
                    if ldb_count > sqlite_count:
                        logging.warning(
                            "ContextEngine: LanceDB has %d rows but SQLite has %d memories"
                            " — rebuilding to remove %d ghost vectors",
                            ldb_count,
                            sqlite_count,
                            ldb_count - sqlite_count,
                        )
                        self._ldb.rebuild_all(self)
                    logging.info("ContextEngine: LanceDBStore ready (backfill complete)")
                except Exception as e:
                    logging.warning(
                        "ContextEngine: LanceDBStore init failed — vector search disabled: %s", e
                    )
                    self._ldb = None

            # P1: Build principle anchor embeddings for intent matching (cached by embedder)
            self._build_principle_anchors()

            self._current_time: str = ""
            # Mark as done AFTER all init completes — prevents other threads from
            # using half-initialized components
            self._heavy_init_done = True

    # ========== 记忆管理 ==========

    def register_memory(self, record: dict[str, Any]) -> str:
        mid = record.get("id", f"mem_{len(self._memories)}")
        data = {
            "id": mid,
            "content": record.get("content", ""),
            "memory_type": record.get("memory_type", "experience"),
            "source": record.get("source", "user"),
            "owner": record.get("owner", os.environ.get("AGENT_OWNER", "")),
            "tier": record.get("tier", "L1"),
            "scope": record.get("scope", "global"),  # deprecated — use domain
            "category": record.get("category", "other"),  # deprecated — use domain
            "tags": record.get("tags", []),
            "domain": record.get("domain", "uncategorized"),
            "entity_ids": record.get("entity_ids", []),
            "worth_success": record.get("worth_success", 0),
            "worth_failure": record.get("worth_failure", 0),
            "activation_weight": record.get("activation_weight", 0.5),
            "created_at": record.get("created_at", datetime.datetime.now().isoformat()),
            "decay_multiplier": record.get("decay_multiplier", 1.0),
            "effective_half_life": record.get("effective_half_life", 3.0),
            "last_accessed": record.get("last_accessed", datetime.datetime.now().isoformat()),
        }
        self._memories[mid] = data
        if self._sqlite:
            self._sqlite.upsert(mid, data)
        # P0: Auto-create principle↔memory graph edges for new memories
        self._build_principle_edges_for_memory(mid, data)
        return mid

    def register_memories(self, records: list[dict[str, Any]]) -> list[str]:
        return [self.register_memory(r) for r in records]

    @property
    def memory_count(self) -> int:
        return len(self._memories)

    # ========== 记忆只读访问 (Rust Core Boundary: 4 read-access methods) ==========

    def memory_exists(self, mid: str) -> bool:
        """Check if a memory id exists in the pool."""
        return mid in self._memories

    def get_memory_dict(self, mid: str) -> dict | None:
        """Get a memory record as a dict (deep copy).

        Returns a copy so callers can read fields freely,
        but mutations have NO effect on engine state.
        Use update_memory_fields() to modify data.
        """
        import copy

        mem = self._memories.get(mid)
        if mem is None:
            return None
        return copy.deepcopy(mem)

    def memory_ids(self) -> list[str]:
        """Return all memory IDs in the pool."""
        return list(self._memories.keys())

    def get_memories_batch(self, mids: list[str]) -> list[dict]:
        """Get multiple memory records by id. Missing ids are skipped."""
        import copy

        results = []
        for mid in mids:
            mem = self._memories.get(mid)
            if mem is not None:
                results.append(copy.deepcopy(mem))
        return results

    def set_current_time(self, iso_timestamp: str):
        self._current_time = iso_timestamp

    # ========== P0: 原则↔记忆图谱边 (深层语法) ==========

    def _build_principle_edges_for_memory(self, memory_id: str, memory_data: dict) -> int:
        """Create bidirectional principle↔memory edges based on keyword overlap.

        For each of the 12 core principles, computes the overlap between
        the principle's keywords and the memory content. If at least
        PRINCIPLE_EDGE_MIN_KEYWORD_HITS keywords match, creates two edges:

            principle:{id} → memory:{id}  (relation="governs")
            memory:{id} → principle:{id}  (relation="embodies")

        Edge weight = PRINCIPLE_EDGE_BASE_WEIGHT + PRINCIPLE_EDGE_SCALE_WEIGHT * keyword_ratio
        capped at 1.0. Edges already existing are skipped (dedup).

        Returns:
            Number of new edges created.
        """
        from plastic_promise.core.constants import (
            CORE_PRINCIPLES,
            PRINCIPLE_EDGE_BASE_WEIGHT,
            PRINCIPLE_EDGE_MIN_KEYWORD_HITS,
            PRINCIPLE_EDGE_SCALE_WEIGHT,
        )

        content = memory_data.get("content", "")
        if not content:
            return 0

        edges_created = 0
        for p in CORE_PRINCIPLES:
            keywords = p.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",")]
            if not keywords:
                continue

            # Count keyword hits in memory content
            hits = sum(1 for kw in keywords if kw in content)
            if hits < PRINCIPLE_EDGE_MIN_KEYWORD_HITS:
                continue

            keyword_ratio = min(hits / len(keywords), 1.0)
            weight = min(
                PRINCIPLE_EDGE_BASE_WEIGHT + PRINCIPLE_EDGE_SCALE_WEIGHT * keyword_ratio, 1.0
            )
            principle_node = f"principle:{p['id']}"
            memory_node = memory_id

            # Ensure principle node exists in graph
            if principle_node not in self._graph_nodes:
                self._graph_nodes[principle_node] = {
                    "type": "principle",
                    "name": p["name"],
                    "description": p["content"],
                    "domain": p.get("domain", "all"),
                }

            # Edge 1: principle → memory (governs)
            edge_g = {
                "from": principle_node,
                "to": memory_node,
                "relation": "governs",
                "weight": weight,
            }
            if edge_g not in self._graph_edges:
                self._graph_edges.append(edge_g)
                edges_created += 1

            # Edge 2: memory → principle (embodies)
            edge_e = {
                "from": memory_node,
                "to": principle_node,
                "relation": "embodies",
                "weight": weight,
            }
            if edge_e not in self._graph_edges:
                self._graph_edges.append(edge_e)
                edges_created += 1

        return edges_created

    def _build_principle_anchors(self):
        """P1: Pre-compute embedding vectors for each principle's content.

        Called once at engine init. Stores results in self._principle_anchors
        as {principle_id: list[float]}. If the embedder is unavailable, the
        dict is left empty and intent matching gracefully degrades to
        keyword-only mode.
        """
        if self._embedder is None:
            self._principle_anchors = {}
            return

        from plastic_promise.core.constants import CORE_PRINCIPLES

        anchors: dict[int, list[float]] = {}
        consecutive_failures = 0
        try:
            for p in CORE_PRINCIPLES:
                if consecutive_failures >= 2:
                    logging.info(
                        "_build_principle_anchors: circuit open after 2 failures — "
                        "skipping remaining %d principles",
                        len(CORE_PRINCIPLES) - len(anchors),
                    )
                    break
                try:
                    vec = self._embedder.embed(p["content"])
                    if vec and any(v != 0.0 for v in vec):
                        anchors[p["id"]] = vec
                        consecutive_failures = 0
                except Exception:
                    consecutive_failures += 1
                    logging.debug(
                        "_build_principle_anchors: embed failed for principle %d (%d/%d failures)",
                        p["id"],
                        consecutive_failures,
                        2,
                    )
            if anchors:
                logging.info(
                    "_build_principle_anchors: computed %d/%d principle anchors",
                    len(anchors),
                    len(CORE_PRINCIPLES),
                )
        except Exception as e:
            logging.warning("_build_principle_anchors failed: %s — intent matching disabled", e)
            anchors = {}

        self._principle_anchors = anchors

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors.

        Returns 0.0 if either vector is zero-length.
        """
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ========== 图管理 ==========

    def load_graph(self, graph_data: dict[str, Any]):
        self._graph_nodes = graph_data.get("nodes", {})
        self._graph_edges = graph_data.get("edges", [])

    def get_graph(self) -> GraphInfo:
        return GraphInfo(self._graph_nodes, self._graph_edges)

    # ========== Graph CRUD (6 methods, added by Task 4) ==========

    def add_graph_edge(
        self, source: str, target: str, relation: str = "references", weight: float = 0.5
    ) -> bool:
        """Add an edge to the entity graph. No-op if duplicate exists.

        Returns True if the edge was added, False if it already existed.
        """
        edge = {
            "from": source,
            "to": target,
            "relation": relation,
            "weight": weight,
        }
        if edge not in self._graph_edges:
            self._graph_edges.append(edge)
            return True
        return False

    def remove_graph_edge(self, source: str, target: str, relation: str = None) -> int:
        """Remove matching edges. Returns number of edges removed."""
        before = len(self._graph_edges)
        self._graph_edges[:] = [
            e
            for e in self._graph_edges
            if not (
                e.get("from") == source
                and e.get("to") == target
                and (relation is None or e.get("relation") == relation)
            )
        ]
        return before - len(self._graph_edges)

    def has_graph_edge(self, edge_dict: dict) -> bool:
        """Check if an exact edge dict exists in the graph."""
        return edge_dict in self._graph_edges

    def get_graph_node(self, node_id: str) -> dict | None:
        """Get a graph node by id. Returns a deep copy."""
        import copy

        node = self._graph_nodes.get(node_id)
        if node is None:
            return None
        return copy.deepcopy(node)

    def list_graph_nodes(self, node_type: str = None) -> list[dict]:
        """List graph nodes, optionally filtered by type field."""
        import copy

        results = []
        for nid, node in self._graph_nodes.items():
            if node_type and node.get("type") != node_type:
                continue
            node_copy = copy.deepcopy(node)
            node_copy["id"] = nid
            results.append(node_copy)
        return results

    def list_graph_edges(self, relation: str = None) -> list[dict]:
        """List graph edges, optionally filtered by relation."""
        if relation is None:
            return list(self._graph_edges)
        return [e for e in self._graph_edges if e.get("relation") == relation]

    # ========== Memory CRUD (Python fallback) ==========

    def store_memory(self, record: MemoryRecord) -> str:
        """Store a MemoryRecord into the in-memory pool.

        Returns the memory id (generates one if record.id is empty).
        """
        mid = record.id or f"mem_{len(self._memories):08d}"
        record.id = mid
        data = {
            "id": mid,
            "content": record.content,
            "memory_type": record.memory_type,
            "source": record.source,
            "scope": record.scope,
            "category": record.category,
            "importance": record.importance,
            "entity_ids": record.entity_ids,
            "created_at": record.created_at or datetime.datetime.now().isoformat(),
            "access_count": record.access_count,
            "worth_success": record.worth_success,
            "worth_failure": record.worth_failure,
            "owner": record.owner,
            "tier": record.tier,
            "tags": record.tags,
            "domain": record.domain,
            "decay_multiplier": getattr(record, "decay_multiplier", 1.0),
            "effective_half_life": getattr(record, "effective_half_life", 3.0),
            "last_accessed": getattr(record, "last_accessed", datetime.datetime.now().isoformat()),
        }
        self._memories[mid] = data
        if self._sqlite:
            self._sqlite.upsert(mid, data)
        # P0: Auto-create principle↔memory graph edges
        self._build_principle_edges_for_memory(mid, data)
        return mid

    def get_memory(self, memory_id: str):
        """Retrieve a single MemoryRecord by id. Returns None if not found."""
        mem = self._memories.get(memory_id)
        if mem is None:
            return None
        record = MemoryRecord(
            id=mem["id"],
            content=mem["content"],
            memory_type=mem.get("memory_type", "experience"),
            source=mem.get("source", "user"),
        )
        record.scope = mem.get("scope", "global")
        record.category = mem.get("category", "other")
        record.owner = mem.get("owner", "")
        record.importance = mem.get("importance", 0.7)
        record.entity_ids = mem.get("entity_ids", [])
        record.created_at = mem.get("created_at", "")
        record.access_count = mem.get("access_count", 0)
        record.worth_success = mem.get("worth_success", 0)
        record.worth_failure = mem.get("worth_failure", 0)
        record.tier = mem.get("tier", "L2")
        record.tags = mem.get("tags", [])
        record.domain = mem.get("domain", "uncategorized")
        record.decay_multiplier = mem.get("decay_multiplier", 1.0)
        record.effective_half_life = mem.get("effective_half_life", 3.0)
        return record

    def update_memory(self, memory_id: str, content=None, importance=None, category=None) -> bool:
        """Update a memory's fields. Returns True if the memory exists."""
        mem = self._memories.get(memory_id)
        if mem is None:
            return False
        if content is not None:
            mem["content"] = content
        if importance is not None:
            mem["importance"] = importance
        if category is not None:
            mem["category"] = category
        if self._sqlite:
            self._sqlite.upsert(memory_id, mem)
        return True

    def update_memory_fields(self, mid: str, **fields) -> bool:
        """Update arbitrary fields of a memory record.

        Unlike update_memory() which only handles content/importance/category,
        this method handles ALL fields: tags, domain, tier, worth_success,
        worth_failure, access_count, last_accessed, decay_multiplier,
        effective_half_life, entity_ids.

        All writes go through the _write_lock for thread safety.
        """
        with self._write_lock:
            if mid not in self._memories:
                return False
            mem = self._memories[mid]
            for key, value in fields.items():
                if key in ("tags", "entity_ids"):
                    mem[key] = list(value)  # defensive copy
                else:
                    mem[key] = value
            if self._sqlite:
                self._sqlite.upsert(mid, mem)
            return True

    def increment_field(self, mid: str, field: str, delta: float = 1) -> bool:
        """Atomically increment a numeric field.

        Convenience wrapper around update_memory_fields for the common
        pattern: engine._memories[mid]["access_count"] += 1

        Note: Uses RLock so increment_field calling update_memory_fields
        within the same lock is safe.
        """
        with self._write_lock:
            if mid not in self._memories:
                return False
            current = self._memories[mid].get(field, 0)
            mem = self._memories[mid]
            mem[field] = current + delta
            if self._sqlite:
                self._sqlite.upsert(mid, mem)
            return True

    # ========== Batch Updates with SAVEPOINT atomicity (Task 5) ==========

    def _maybe_adjust_tier(self, mid: str) -> None:
        """Real-time tier promotion based on access_count thresholds.

        Called during _text_retrieval after access_count increment.
        Only promotes (L1→L2, L2→L3) — demotion is handled by evolve_cycle.
        Gated by PP_TIER_AUTO_PROMOTE env var (default on).
        """
        if os.environ.get("PP_TIER_AUTO_PROMOTE", "1") != "1":
            return
        mem = self._memories.get(mid)
        if not mem:
            return
        access = mem.get("access_count", 0)
        tier = mem.get("tier", "L1")
        new_tier = tier
        if tier == "L1" and access >= 5:
            new_tier = "L2"
        elif tier == "L2" and access >= 20:
            new_tier = "L3"
        if new_tier != tier:
            mem["tier"] = new_tier
            if self._sqlite:
                self._sqlite.upsert(mid, mem)

    def batch_update(self, updates: list[dict]) -> int:
        """Apply multiple memory field updates atomically.

        Args:
            updates: [{"id": "mem_001", "tags": [...], "domain": "code"}, ...]
                Each dict MUST contain "id". Other keys are field updates.

        Returns:
            Number of records updated.

        If any update fails, ALL changes are rolled back via SAVEPOINT.
        Thread-safe: acquires _write_lock.

        Note: Uses _sqlite.batch() context manager to suppress auto-commit
        from _SQLiteStorage.upsert() — without it the SAVEPOINT would be
        consumed by the first implicit commit and subsequent RELEASE would
        fail with "no such savepoint".
        """
        with self._write_lock:
            if not self._sqlite:
                return self._batch_update_in_memory(updates)

            with self._sqlite.batch():
                self._sqlite._conn.execute("SAVEPOINT batch_update")
                try:
                    count = 0
                    for upd in updates:
                        upd_copy = dict(upd)  # don't mutate caller's dict
                        mid = upd_copy.pop("id")
                        if mid in self._memories:
                            self._memories[mid].update(upd_copy)
                            self._sqlite.upsert(mid, self._memories[mid])
                            count += 1
                    self._sqlite._conn.execute("RELEASE batch_update")
                    return count
                except Exception:
                    self._sqlite._conn.execute("ROLLBACK TO batch_update")
                    raise

    def _batch_update_in_memory(self, updates: list[dict]) -> int:
        """Fallback batch_update when SQLite is unavailable."""
        count = 0
        for upd in updates:
            upd_copy = dict(upd)
            mid = upd_copy.pop("id")
            if mid in self._memories:
                self._memories[mid].update(upd_copy)
                count += 1
        return count

    def begin_batch(self):
        """Begin a manual batch transaction. Acquires _write_lock.

        Suppresses auto-commit from _SQLiteStorage.upsert() by incrementing
        _batch_depth, so the SAVEPOINT survives across multiple writes.
        """
        self._write_lock.acquire()
        if self._sqlite:
            self._sqlite._batch_depth += 1
            self._sqlite._conn.execute("SAVEPOINT manual_batch")

    def commit_batch(self):
        """Commit a manual batch transaction. Releases _write_lock."""
        try:
            if self._sqlite:
                self._sqlite._conn.execute("RELEASE manual_batch")
                self._sqlite._batch_depth -= 1
                if self._sqlite._batch_depth <= 0:
                    self._sqlite._batch_depth = 0
                    self._sqlite._conn.commit()
        finally:
            self._write_lock.release()

    def rollback_batch(self):
        """Rollback a manual batch transaction. Releases _write_lock."""
        try:
            if self._sqlite:
                self._sqlite._conn.execute("ROLLBACK TO manual_batch")
                self._sqlite._batch_depth -= 1
                if self._sqlite._batch_depth <= 0:
                    self._sqlite._batch_depth = 0
        finally:
            self._write_lock.release()

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory by id. Returns True if it existed."""
        if memory_id in self._memories:
            del self._memories[memory_id]
            if self._sqlite:
                self._sqlite.delete(memory_id)
            return True
        return False

    def list_memories(
        self, memory_type=None, source=None, min_worth=None, limit=50, scope=None, offset=0
    ) -> list:
        """List memories with optional filters and offset pagination.

        Args:
            memory_type: Optional filter by memory type.
            source: Optional filter by source.
            min_worth: Optional minimum worth score filter.
            limit: Maximum number of records to return (default 50).
            scope: Optional domain filter.
            offset: Number of matching records to skip before returning
                    results (default 0). Used by list_memories_paginated()
                    for offset pagination.

        Returns:
            A list of MemoryRecord objects matching the filter criteria.
        """
        # Refresh in-memory cache from SQLite first (catches external writes)
        self._reload_from_sqlite()

        results = []
        skip = offset
        for mid, mem in self._memories.items():
            if memory_type and mem.get("memory_type") != memory_type:
                continue
            if source and mem.get("source") != source:
                continue
            if scope and mem.get("scope") != scope:
                continue
            if min_worth is not None:
                s = mem.get("worth_success", 0)
                f = mem.get("worth_failure", 0)
                total = s + f
                ws = (s + 1.0) / (total + 2.0) if total > 0 else 0.5
                if ws < min_worth:
                    continue
            if skip > 0:
                skip -= 1
                continue
            results.append(self.get_memory(mid))
            if len(results) >= limit:
                break
        return results

    def list_memories_paginated(
        self,
        memory_type: str = None,
        source: str = None,
        min_worth: float = None,
        scope: str = None,
        page_size: int = 200,
    ):
        """Yield MemoryRecords one page at a time via offset pagination.

        Avoids allocating a full list for large result sets.
        For 10K records at page_size=200: ~50 PyO3 boundary crossings.

        Consistency: Uses offset-based pagination — NOT guaranteed consistent
        under concurrent writes. Records inserted or deleted between pages may
        cause duplicates or omissions. Suitable for snapshot operations
        (pack_export, memory_gc, memory_stats). Real-time retrieval uses
        supply() via LanceDB ANN + text matching, not pagination.

        Yields:
            MemoryRecord objects, one at a time.
        """
        offset = 0
        while True:
            page = self.list_memories(
                memory_type=memory_type,
                source=source,
                min_worth=min_worth,
                limit=page_size,
                scope=scope,
                offset=offset,
            )
            if not page:
                break
            yield from page
            if len(page) < page_size:
                break
            offset += len(page)

    def iter_memories(self, scope=None, page_size=200) -> "Iterator[dict]":
        """Iterate memory records as dicts, one page at a time.

        Uses offset-based pagination over the in-memory dict keys.
        NOT consistent under concurrent writes — suitable for snapshots
        (pack_export, memory_stats) not real-time retrieval under load.

        Args:
            scope: Optional domain filter (applied in Python after yield).
                   Pass None for all memories.
            page_size: Number of records per page (default 200).

        Yields:
            Deep copies of memory dicts, one at a time.
        """
        import copy

        all_ids = list(self._memories.keys())
        offset = 0
        while offset < len(all_ids):
            page_ids = all_ids[offset : offset + page_size]
            for mid in page_ids:
                mem = self._memories.get(mid)
                if mem is None:
                    continue
                if scope and mem.get("scope", "global") != scope:
                    continue
                yield copy.deepcopy(mem)
            offset += page_size

    def _reload_from_sqlite(self):
        """Sync in-memory cache with SQLite: load new or updated memories."""
        if not self._sqlite:
            return
        try:
            for mid, data in self._sqlite.iter_all():
                self._memories[mid] = data  # overwrite existing to catch external updates
        except Exception:
            pass  # graceful degradation

    def memory_stats_json(self, scope=None) -> str:
        """Return memory pool statistics as a JSON string.

        Compatible with the Rust ContextEngine.memory_stats_json() interface.
        """
        # Refresh in-memory cache from SQLite first (catches external writes)
        self._reload_from_sqlite()

        total = len(self._memories)
        by_type: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        healthy = 0
        decaying = 0
        worth_sum = 0.0
        active_count = 0
        dormant_count = 0
        active_worth_sum = 0.0

        for mid, mem in self._memories.items():
            if scope and mem.get("scope") != scope:
                continue
            mt = mem.get("memory_type", "unknown")
            by_type[mt] = by_type.get(mt, 0) + 1
            mc = mem.get("category", "other")
            by_category[mc] = by_category.get(mc, 0) + 1
            mtier = mem.get("tier", "L2")
            by_tier[mtier] = by_tier.get(mtier, 0) + 1
            ws = mem.get("worth_success", 0)
            wf = mem.get("worth_failure", 0)
            total_obs = ws + wf
            ws_val = (ws + 1.0) / (total_obs + 2.0) if total_obs > 0 else 0.5
            worth_sum += ws_val
            if total_obs > 0:
                active_count += 1
                active_worth_sum += ws_val
            else:
                dormant_count += 1
            if ws_val >= 0.15:
                healthy += 1
            else:
                decaying += 1

        return json.dumps(
            {
                "total": total,
                "healthy": healthy,
                "decaying": decaying,
                "by_type": by_type,
                "by_category": by_category,
                "by_tier": by_tier,
                "average_worth": round(worth_sum / total, 3) if total > 0 else 0.0,
                "worth_breakdown": {
                    "active": active_count,
                    "dormant": dormant_count,
                    "active_avg_worth": round(active_worth_sum / active_count, 3)
                    if active_count > 0
                    else None,
                },
            },
            ensure_ascii=False,
        )

    # ========== 核心方法: supply() ==========

    def _embed(self, task_description: str) -> list[float]:
        """Generate embedding vector for task description.

        Uses the existing embedder from heavy_init. Returns zero vector
        if embedder is unavailable (graceful degradation).
        """
        try:
            self._ensure_heavy_init()
            if hasattr(self, "_embedder") and self._embedder:
                return self._embedder.embed(task_description)
        except Exception:
            pass
        return [0.0] * 1024  # fallback: zero vector

    def supply(
        self,
        task_description: str,
        task_vector: list[float] = None,
        task_type: str = "general",
        scope: str = "global",
    ) -> ContextPack:
        """Supply context for a task. Rust-accelerated when available.

        Consistency: Returns a snapshot of the memory pool at call time.
        Concurrent writes (batch_update, register_memory) may not be
        reflected — this is eventual consistency by design. Retrieval
        results are advisory, not transactional.

        IMPORTANT: _supply_python is the ORIGINAL independent Python
        implementation. It does NOT call back into supply() — no recursion.
        """
        # Generate embedding if not provided (backward compatibility)
        if task_vector is None:
            task_vector = self._embed(task_description)

        # Ensure vector is non-empty — if embedder fails (Ollama down, etc.),
        # use zero vector as fallback so downstream code never sees None
        if task_vector is None or len(task_vector) == 0:
            task_vector = [0.0] * 1024  # fallback: mxbai-embed-large dim

        # PP_FORCE_PYTHON_SUPPLY=1 bypasses Rust entirely.
        # PP_PREFER_RUST_SUPPLY=1 enables Rust as primary (off by default
        # until Rust retriever backends — VectorIndex + FtsIndex — are real).
        prefer_rust = os.environ.get("PP_PREFER_RUST_SUPPLY", "0") == "1"
        force_python = os.environ.get("PP_FORCE_PYTHON_SUPPLY", "0") == "1"

        if force_python or not prefer_rust:
            return self._supply_python(task_description, task_vector, task_type, scope)

        # Rust accelerator — enabled via PP_PREFER_RUST_SUPPLY=1.
        # Falls back to Python if Rust engine is unavailable or throws.
        if self._check_rust_health():
            try:
                return self._supply_rust(task_description, task_vector, task_type, scope)
            except Exception as e:
                logger.warning("Rust supply failed, falling back to Python: %s", e)
                with self._rust_lock:
                    self._rust_healthy = None
                    self._rust_engine_instance = None

        return self._supply_python(task_description, task_vector, task_type, scope)

    @staticmethod
    def _apply_decay_awareness(
        score: float,
        mem: dict | None,
        current_time_str: str,
        trust_boost: float,
    ) -> float:
        """Two-formula decay-aware relevance adjustment with trust modulation.

        Formula A (additive recency): fresh memories get up to +0.1 bonus.
          boost = exp(-age_days / recency_hl) * 0.1
          score = clamp01(score + boost, floor=score)

        Formula B (multiplicative time decay): old memories penalized, floor 0.5x.
          factor = 0.5 + 0.5 * exp(-age_days / effective_half_life)
          score = clamp01(score * factor, floor=score * 0.5)

        Trust modulation: high-trust agents get wider recency window.
          trust_mod = 1.0 + (trust_boost - 1.0) * 0.5
          recency_hl = 14.0 * trust_mod

        Pure computation — reads existing fields, zero I/O.
        Gated by PP_DECAY_IN_RANKING env var (default on).
        """
        if os.environ.get("PP_DECAY_IN_RANKING", "1") != "1":
            return score
        if not mem:
            return score
        created_at = mem.get("created_at", "")
        if not created_at:
            return score
        try:
            created = datetime.datetime.fromisoformat(created_at)
            now = datetime.datetime.fromisoformat(
                current_time_str or datetime.datetime.now().isoformat()
            )
            age_days = (now - created).total_seconds() / 86400.0
            if age_days <= 0:
                return score
        except Exception:
            return score

        # Trust modulation: gentle scaling around CortexReach default (14 days)
        trust_mod = 1.0 + (trust_boost - 1.0) * 0.5

        # Formula A: additive recency boost
        recency_hl = 14.0 * trust_mod
        boost = math.exp(-age_days / recency_hl) * 0.1
        score = min(1.0, score + boost)

        # Formula B: multiplicative time decay with access-reinforced half-life
        effective_hl = mem.get("effective_half_life", 60.0)
        factor = 0.5 + 0.5 * math.exp(-age_days / effective_hl)
        score = max(score * 0.5, score * factor)

        return score

    @staticmethod
    def _apply_length_norm(score: float, content: str, anchor: int = 500) -> float:
        """Normalize score by document length to prevent long documents from dominating.

        Formula: score *= 1 / (1 + 0.5 * log2(len / anchor))
        Floor: score * 0.3 (never reduce below 30% of original).
        Short documents (<= anchor chars) are not penalized.
        """
        char_len = len(content)
        if char_len <= anchor:
            return score
        ratio = char_len / anchor
        log_ratio = math.log2(ratio)
        factor = 1.0 / (1.0 + 0.5 * log_ratio)
        return max(score * factor, score * 0.3)

    def _apply_mmr(self, items: list, threshold: float = 0.85, penalty: float = 0.70) -> list:
        """Greedy MMR diversity: demote items with similar content.

        Uses two-stage dedup:
        1. Exact content match (first 200 chars): score *= 0.50 (hard demote)
        2. Vector cosine similarity (if LanceDB vectors available): score *= penalty

        Soft-demotion (not removal): demoted items are deferred to the end,
        preserving them for lower layers.
        """
        if len(items) <= 1:
            return items

        items_sorted = sorted(items, key=lambda x: x.relevance, reverse=True)
        selected: list = []
        deferred: list = []
        seen_contents: set[str] = set()  # first 80 chars
        seen_prefixes: set[str] = set()  # first 40 chars for template detection
        vec_cache: dict = {}  # cache vectors per supply() call to avoid repeated LanceDB lookups

        for item in items_sorted:
            if getattr(item, "is_principle", False):
                selected.append(item)
                continue

            # Stage 1: content dedup — full match (80 chars) + prefix match (25 chars for templates)
            content_key = item.content[:80].strip().lower()
            prefix_key = item.content[:20].strip().lower()
            if (content_key and content_key in seen_contents) or (
                prefix_key and prefix_key in seen_prefixes
            ):
                item.relevance *= 0.50  # hard penalty for near-dup patterns
                deferred.append(item)
                continue
            seen_contents.add(content_key)
            if prefix_key:
                seen_prefixes.add(prefix_key)

            # Stage 2: vector-based MMR — real cosine diversity checking
            if self._ldb and os.environ.get("PP_MMR_VECTOR", "1") == "1":
                try:
                    item_vec = self._ldb.get_vector(item.id)
                    if item_vec:
                        max_sim = 0.0
                        # Compare against last 5 selected items (O(5n) not O(n²))
                        for sel in selected[-5:]:
                            sel_vec = vec_cache.get(sel.id) or self._ldb.get_vector(sel.id)
                            if sel_vec:
                                vec_cache[sel.id] = sel_vec
                                sim = self._cosine_similarity(item_vec, sel_vec)
                                max_sim = max(max_sim, sim)
                        if max_sim > 0.85:
                            item.relevance *= 0.70  # demote, not remove
                            deferred.append(item)
                            continue
                except Exception:
                    pass  # vector MMR failure → fall through to content-only
            # Without vectors or if MMR disabled, rely on content-based dedup only
            selected.append(item)

        deferred.sort(key=lambda x: x.relevance, reverse=True)
        return selected + deferred

    def _supply_python(
        self,
        task_description: str,
        task_vector: list[float],
        task_type: str = "general",
        scope: str = "global",
    ) -> ContextPack:
        """供应上下文

        Args:
            task_description: 当前任务的完整自然语言描述。
                (调用方应将 pre_context 内容追加到 task_description 后再传入)
            task_vector: 由 Python embedder 生成的 embedding 向量 (list[float])。
                Rust 端通过此参数接收向量，不自行调用 embedding API。
            task_type: 任务类型标签，用于原则匹配和图遍历。
            scope: 检索范围 — "global" 搜索全部记忆，或限定特定 domain。

        Returns:
            ContextPack: 三层上下文包 (core / related / divergent)
        """
        # Lazy-heavy-init: first supply() call triggers DomainManager, LanceDB, embedder, anchors
        self._ensure_heavy_init()

        # Phase 0: 原则注入 + 图谱自动注入
        activated = self._activate_principles(task_type, task_description)
        if self.enable_principles:
            self._inject_activated_to_graph(activated, task_type)

        # Trust-aware retrieval: higher trust → broader context
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager

            tm = TrustManager()
            trust_boost = max(tm.get_retrieval_boost(), 0.80)
        except Exception:
            trust_boost = 1.0

        # Phase 1: 三路分层检索 — 细→类→粗
        # 细 (graph): 原则关联图谱 — 最精确的信号
        graph_results = self._graph_traversal(task_type)

        # 类 (tier): 文本匹配 + L1 工作记忆优先级提升
        self._domain_hint = scope if scope and scope != "global" else None

        # Query expansion: inject domain-relevant synonyms for BM25 text search.
        # Vector search uses raw query — semantic models handle synonyms natively.
        expanded_query = task_description
        if os.environ.get("PP_QUERY_EXPANSION", "1") == "1":
            try:
                from plastic_promise.core.query_expander import expand_query

                expanded_query = expand_query(task_description, self._domain_hint)
            except Exception:
                pass  # expansion failure never blocks retrieval

        text_results = self._text_retrieval(expanded_query, trust_boost)

        # 粗 (vector): 语义向量相关性 (零向量时跳过)
        vector_results = (
            self._vector_retrieval(task_vector) if any(v != 0.0 for v in task_vector) else []
        )

        # Phase 2: Hybrid fusion (vector + text) then layer with graph
        if vector_results:
            vector_weight = float(os.environ.get("PP_VECTOR_WEIGHT", "0.50"))
            fused_results = self._hybrid_fuse(
                vector_results, text_results, vector_weight=vector_weight
            )
        else:
            # No vector available (Ollama down / zero vector) — use text only
            fused_results = [
                (mid, score * 0.8, content, source) for mid, score, content, source in text_results
            ]
        all_results = self._layered_fuse(graph_results, fused_results, [])

        # P2: Evolve edge weights based on feedback patterns
        self._apply_edge_feedback()

        # Phase 3-5 fused: symbol rules + feedback + ContextItem building (was 3 passes, now 1)
        from plastic_promise.core.constants import (
            FEEDBACK_SCORE_MULTIPLIER_MIN,
            FEEDBACK_SCORE_MULTIPLIER_RANGE,
        )

        pack = ContextPack(activated_principles=activated)
        current_time_str = datetime.datetime.now().isoformat()  # single timestamp for decay calc

        for item_id, score, content, source in all_results:
            # --- Symbol rule boost (was _apply_symbol_rules) ---
            boost = 1.0
            for category, keywords in SYMBOL_RULE_KEYWORDS.items():
                if any(kw in task_description for kw in keywords) or any(
                    kw in content for kw in keywords
                ):
                    if category == "security":
                        boost *= 1.5
                    elif category == "commitment":
                        boost *= 1.4
                    elif category == "quality":
                        boost *= 1.2
            score = min(score * boost, 1.0)

            # --- Feedback multiplier (was _apply_feedback) ---
            mem = self._memories.get(item_id, {}) if not item_id.startswith("principle:") else {}
            if mem:
                ws = mem.get("worth_success", 0)
                wf = mem.get("worth_failure", 0)
                total = ws + wf
                worth = (ws + 1.0) / (total + 2.0) if total > 0 else 0.5
            else:
                worth = 0.5
            multiplier = FEEDBACK_SCORE_MULTIPLIER_MIN + FEEDBACK_SCORE_MULTIPLIER_RANGE * worth
            score = score * multiplier

            # --- Decay-aware ranking (Phase 1.3) ---
            score = self._apply_decay_awareness(score, mem, current_time_str, trust_boost)

            # --- Length normalization (Phase 1.5) ---
            score = ContextEngine._apply_length_norm(score, content)

            # --- ContextItem construction (was separate Phase 5 loop) ---
            is_principle = item_id.startswith("principle:")
            worth_score = mem.get("worth_score", 0.0) if mem else 0.0
            freshness = self._calc_freshness(item_id)

            item = ContextItem(
                id=item_id,
                content=content,
                relevance=score,
                source=source,
                freshness=freshness,
                is_principle=is_principle,
                worth_score=worth_score,
                novelty_score=0.0,
                confidence=0.5,
                inspiration_score=0.0,
                adoption_count=mem.get("worth_success", 0) if mem else 0,
                rejection_count=int(mem.get("worth_failure", 0)) if mem else 0,
                times_retrieved=mem.get("access_count", 0) if mem else 0,
                decay_status=self._calc_decay_status(item_id, mem) if mem else "healthy",
            )

            # Principles are already listed in activated_principles — skip from layers
            if not is_principle:
                if score >= CONTEXT_LAYERS["core"]["min_relevance"]:
                    item.layer = "core"
                    pack.core.append(item)
                elif score >= CONTEXT_LAYERS["related"]["min_relevance"]:
                    item.layer = "related"
                    pack.related.append(item)
                elif score >= CONTEXT_LAYERS["divergent"]["min_relevance"]:
                    item.layer = "divergent"
                    pack.divergent.append(item)

        # P3a: MMR diversity + optional rerank, then re-layer
        if pack.core or pack.related or pack.divergent:
            all_items = pack.core + pack.related + pack.divergent
            # Rerank (Phase 1.6, optional — PP_RECALL_RERANK=1)
            # Unified reranker (Phase 1.6): multi-provider chain, default ON
            from plastic_promise.core.reranker import MultiProviderReranker

            all_items = MultiProviderReranker().rerank(task_description, all_items)
            # MMR diversity (Phase 1.4)
            all_items = self._apply_mmr(all_items, threshold=0.85, penalty=0.70)
            # Re-distribute to layers based on adjusted relevance
            pack.core.clear()
            pack.related.clear()
            pack.divergent.clear()
            for item in all_items:
                if not item.is_principle:
                    if item.relevance >= CONTEXT_LAYERS["core"]["min_relevance"]:
                        item.layer = "core"
                        pack.core.append(item)
                    elif item.relevance >= CONTEXT_LAYERS["related"]["min_relevance"]:
                        item.layer = "related"
                        pack.related.append(item)
                    elif item.relevance >= CONTEXT_LAYERS["divergent"]["min_relevance"]:
                        item.layer = "divergent"
                        pack.divergent.append(item)
            # Compute divergent quality
            if pack.divergent:
                all_retrieved = pack.core + pack.related + pack.divergent
                pack.divergent = self._compute_divergent_quality(pack.divergent, all_retrieved)

        # Phase 6: 审计元数据
        pack.audit_metadata = {
            "engine_version": "0.1.0-py",
            "task_type": task_type,
            "principle_injection_count": str(len(activated)),
            "graph_nodes": str(len(self._graph_nodes)),
            "graph_edges": str(len(self._graph_edges)),
            "memory_pool_size": str(len(self._memories)),
            "vector_search": "active" if vector_results else "fallback_text_only",
            "ldb_rows": str(self._ldb.count_rows()) if self._ldb else "0",
            "rerank_status": "multi-provider",
        }

        # ── Exemplar gap detection ─────────────────────────
        # Middleware: detect knowledge gaps before returning.
        # Graceful degradation: if the detector fails, we still
        # return the pack — gap_signal is optional enrichment.
        try:
            from plastic_promise.core.exemplar_gap_detector import detect_gap

            pack.gap_signal = detect_gap(task_description, pack)
        except Exception:
            pass  # gap detection failure must not block context_supply

        return pack

    # ========== 实体注册 ==========

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        entity_description: str = "",
        related_entities: list[str] = None,
    ) -> dict:
        """Register an entity node and optionally create edges to related entities.

        Args:
            entity_type: One of "principle", "task", "memory", "code_module", "skill_session".
            entity_id: Unique identifier for this entity.
            entity_name: Human-readable name.
            entity_description: Optional description text.
            related_entities: Optional list of entity IDs to link to.

        Returns:
            dict with keys: node_id, type, edges_created
        """
        # Validate entity_type
        valid_types = {"principle", "task", "memory", "code_module", "skill_session"}
        if entity_type not in valid_types:
            raise ValueError(
                f"Unknown entity_type '{entity_type}'. Valid: {', '.join(sorted(valid_types))}"
            )

        node_id = f"{entity_type}:{entity_id}"
        is_new = node_id not in self._graph_nodes

        # Create or update node
        self._graph_nodes[node_id] = {
            "type": entity_type,
            "name": entity_name,
            "description": entity_description or "",
        }

        # Create edges to related entities
        edges_created = 0
        if related_entities:
            for related_id in related_entities:
                edge = {
                    "from": node_id,
                    "to": related_id,
                    "relation": "supports",
                    "weight": PRINCIPLE_INHERITANCE_DECAY,
                }
                # Avoid exact duplicate edges
                if edge not in self._graph_edges:
                    self._graph_edges.append(edge)
                    edges_created += 1

        return {
            "node_id": node_id,
            "type": entity_type,
            "name": entity_name,
            "is_new": is_new,
            "edges_created": edges_created,
        }

    def query_graph(
        self,
        query_type: str,
        start_node: str = None,
        max_hops: int = 3,
    ) -> dict:
        """Query the entity association graph.

        Args:
            query_type: "node_info" | "traverse" | "full_graph" | "neighbors"
            start_node: Node ID for node_info/traverse/neighbors queries.
            max_hops: Max traversal depth (clamped to [1, 10]).

        Returns:
            dict with nodes, edges, and optional traversal_path.
        """
        max_hops = max(1, min(max_hops, 10))

        if query_type == "full_graph":
            return {
                "nodes": dict(self._graph_nodes),
                "edges": list(self._graph_edges),
            }

        if query_type == "node_info":
            if not start_node or start_node not in self._graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                }
            node = self._graph_nodes[start_node]
            in_edges = [e for e in self._graph_edges if e.get("to") == start_node]
            out_edges = [e for e in self._graph_edges if e.get("from") == start_node]
            return {
                "nodes": {start_node: node},
                "edges": in_edges + out_edges,
                "in_degree": len(in_edges),
                "out_degree": len(out_edges),
            }

        if query_type == "neighbors":
            if not start_node or start_node not in self._graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                }
            neighbor_ids = set()
            edges = []
            for e in self._graph_edges:
                if e.get("from") == start_node:
                    neighbor_ids.add(e.get("to"))
                    edges.append(e)
                elif e.get("to") == start_node:
                    neighbor_ids.add(e.get("from"))
                    edges.append(e)
            nodes = {
                nid: self._graph_nodes[nid] for nid in neighbor_ids if nid in self._graph_nodes
            }
            return {"nodes": nodes, "edges": edges, "neighbor_count": len(nodes)}

        if query_type == "traverse":
            if not start_node or start_node not in self._graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                    "traversal_path": [],
                }
            # BFS traversal
            visited = set()
            queue = [(start_node, 0)]
            traversal_path = []
            all_nodes = {}
            all_edges = []

            from collections import deque

            q = deque([(start_node, 0)])
            while q:
                current, depth = q.popleft()
                if current in visited or depth > max_hops:
                    continue
                visited.add(current)
                traversal_path.append(current)
                if current in self._graph_nodes:
                    all_nodes[current] = self._graph_nodes[current]
                # Follow outgoing edges
                for e in self._graph_edges:
                    if e.get("from") == current:
                        all_edges.append(e)
                        target = e.get("to")
                        if target and target not in visited:
                            q.append((target, depth + 1))

            return {
                "nodes": all_nodes,
                "edges": all_edges,
                "traversal_path": traversal_path,
                "hops": max_hops,
            }

        return {
            "error": f"Unknown query_type '{query_type}'. "
            f"Valid: node_info, traverse, full_graph, neighbors"
        }

    # ========== Public wrappers for internal methods (Task 10) ==========

    def ensure_heavy_init(self):
        """Public wrapper for _ensure_heavy_init."""
        self._ensure_heavy_init()

    def activate_principles(self, task_type: str, task_description: str) -> list[str]:
        """Public wrapper for _activate_principles."""
        return self._activate_principles(task_type, task_description)

    def text_retrieval(self, task: str, trust_boost: float = 1.0) -> list[tuple]:
        """Public wrapper for _text_retrieval."""
        return self._text_retrieval(task, trust_boost)

    def apply_edge_feedback_for_memory(self, memory_id: str):
        """Public wrapper for _apply_edge_feedback_for_memory."""
        self._apply_edge_feedback_for_memory(memory_id)

    def get_context_ready(self) -> dict:
        """Get the context_ready cache dict."""
        if not hasattr(self, "_context_ready"):
            self._context_ready = {}
        return self._context_ready

    def clear_expired_context_ready(self, expired_keys: list):
        """Remove expired entries from context_ready cache."""
        if hasattr(self, "_context_ready"):
            for k in expired_keys:
                self._context_ready.pop(k, None)

    def get_fuzzy_buffer(self):
        """Get or None the fuzzy buffer pipeline."""
        return getattr(self, "_fuzzy_buffer", None)

    def set_fuzzy_buffer(self, fb):
        """Set the fuzzy buffer pipeline."""
        self._fuzzy_buffer = fb

    def get_rec_mem(self):
        """Get or None the RecMem instance."""
        return getattr(self, "_rec_mem", None)

    def set_rec_mem(self, rm):
        """Set the RecMem instance."""
        self._rec_mem = rm

    def get_issue_manager(self):
        """Get or create the IssueManager."""
        if not hasattr(self, "_issue_manager") or self._issue_manager is None:
            from plastic_promise.issue import IssueManager

            self._issue_manager = IssueManager()
        return self._issue_manager

    def execute_sql(self, sql: str, params: tuple = ()):
        """Execute raw SQL through the internal SQLite connection. Use sparingly."""
        if self._sqlite:
            return self._sqlite._conn.execute(sql, params)
        return None

    def commit_sql(self):
        """Commit any pending SQLite transaction."""
        if self._sqlite:
            self._sqlite.commit()

    # ---- Rust engine health probe -------------------------------------------

    def _check_rust_health(self) -> bool | None:
        """Probe Rust core availability. Caches result for TTL seconds.

        Thread-safe: acquires _rust_lock to protect _rust_engine_instance
        and health state against concurrent MCP/Daemon access.

        On failure: sets _rust_healthy = None (NOT False) to force
        immediate re-probe on the next supply() call. This avoids the
        defect where setting healthy=False traps the system in a
        degraded state until TTL expires.
        """
        with self._rust_lock:
            now = time.time()
            # Return cached result if within TTL
            if (
                self._rust_healthy is not None
                and (now - self._rust_health_checked_at) < self._rust_health_ttl
            ):
                return self._rust_healthy

            try:
                # Ensure Rust .pyd is on sys.path — MCP server doesn't inherit
                # the working directory's PYTHONPATH
                import sys as _sys

                _rust_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "rust",
                    "context-engine-core",
                    "target",
                    "release",
                )
                if _rust_path not in _sys.path:
                    _sys.path.insert(0, _rust_path)
                from context_engine_core import ContextEngine as RustEngine

                # Smoke test: supply with empty memories — validates import + PyO3 bridge
                engine = RustEngine()
                engine.set_current_time(datetime.datetime.now().isoformat())
                pack = engine.supply("test", [0.0] * 1024, "general", "global", [])
                # Validate response shape — must have core + related attributes
                assert hasattr(pack, "core"), "Rust ContextPack missing 'core'"
                assert hasattr(pack, "related"), "Rust ContextPack missing 'related'"
                assert hasattr(pack, "divergent"), "Rust ContextPack missing 'divergent'"
                assert hasattr(pack, "activated_principles"), (
                    "Rust ContextPack missing 'activated_principles'"
                )
                self._rust_engine_instance = engine
                self._rust_healthy = True
            except Exception as e:
                logger.warning("Rust engine health check failed: %s", e)
                # Set to None (not False) — forces immediate re-probe on next supply()
                self._rust_healthy = None
                self._rust_engine_instance = None

            self._rust_health_checked_at = now
            return self._rust_healthy

    def reset_rust_health(self):
        """Force re-probe Rust health on next supply() call.

        Use when: Rust .pyd was deployed, environment changed,
        or health was falsely marked as failed.
        """
        with self._rust_lock:
            self._rust_healthy = None
            self._rust_health_checked_at = 0.0
            self._rust_engine_instance = None
        logger.info("Rust health reset — will re-probe on next supply()")

    def _convert_rust_pack(self, rust_pack) -> ContextPack:
        """Convert Rust PyO3 ContextPack to Python ContextPack.

        Rust returns PyO3 objects with .core/.related/.divergent/
        .activated_principles/.audit_metadata. We convert to the
        Python dataclass-based format that callers expect.

        Preserves audit_metadata from Rust (engine_version, timings,
        graph stats, etc.) for observability.
        """
        pack = ContextPack()
        pack.core = [
            ContextItem(
                id=item.id,
                content=item.content,
                relevance=item.relevance,
                source=item.source,
                freshness=item.freshness,
                layer=item.layer,
                is_principle=item.is_principle,
                worth_score=item.worth_score,
            )
            for item in rust_pack.core
        ]
        pack.related = [
            ContextItem(
                id=item.id,
                content=item.content,
                relevance=item.relevance,
                source=item.source,
                freshness=item.freshness,
                layer=item.layer,
                is_principle=item.is_principle,
                worth_score=item.worth_score,
            )
            for item in rust_pack.related
        ]
        pack.divergent = [
            ContextItem(
                id=item.id,
                content=item.content,
                relevance=item.relevance,
                source=item.source,
                freshness=item.freshness,
                layer=item.layer,
                is_principle=item.is_principle,
                worth_score=item.worth_score,
            )
            for item in rust_pack.divergent
        ]
        pack.activated_principles = list(rust_pack.activated_principles)
        # Preserve audit metadata from Rust for observability
        # Use isinstance guard: PyO3 may return PyDict wrapper, not plain dict
        if hasattr(rust_pack, "audit_metadata") and rust_pack.audit_metadata:
            if isinstance(rust_pack.audit_metadata, dict):
                pack.audit_metadata = dict(rust_pack.audit_metadata)
            else:
                # PyDict or other mapping — convert safely
                pack.audit_metadata = dict(rust_pack.audit_metadata)
        else:
            pack.audit_metadata = {}

        return pack

    def _supply_rust(
        self, task_description: str, task_vector: list, task_type: str, scope: str
    ) -> ContextPack:
        """Rust-accelerated supply path (Phase 2 primary).

        Rust engine reads from its own read-only SQLite connection to
        plastic_memory.db. Memories are NOT passed from Python — the
        Rust engine is self-contained. Passes empty list for backward
        compatibility with the PyO3 signature.
        """
        from context_engine_core import ContextEngine as RustEngine

        # Ensure Rust engine finds the real database
        db_path = get_db_path()
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
        os.environ["PLASTIC_DB_PATH"] = db_path

        rust = RustEngine()
        rust.set_current_time(datetime.datetime.now().isoformat())

        # Load all vectors from LanceDB at once for Rust enrichment
        vector_lookup: dict[str, list[float]] = {}
        if self._ldb:
            try:
                all_rows = self._ldb._table.search().limit(9999).to_list()
                for row in all_rows:
                    mid = row.get("memory_id", "")
                    vec = row.get("vector", [])
                    if mid and vec and len(vec) == 1024:
                        vector_lookup[mid] = list(vec)
            except Exception:
                pass

        with self._write_lock:
            memories = []
            for mid in self._memories:
                mem = dict(self._memories[mid])
                mem["_vector"] = vector_lookup.get(mid, [])
                memories.append(mem)

        rust_pack = rust.supply(task_description, task_vector, task_type, scope, memories)
        return self._convert_rust_pack(rust_pack)

    # ========== 内部方法 ==========

    def _inject_activated_to_graph(self, activated_names: list[str], task_type: str) -> int:
        """Write activated principles into the entity graph.

        Called automatically during supply() Phase 0. Creates/updates
        principle nodes and adds task_type -> principle edges so
        _graph_traversal has data to work with.

        Args:
            activated_names: List of principle names from _activate_principles().
            task_type: Task type label for the source edge.

        Returns:
            Number of edges created.
        """
        from plastic_promise.core.constants import CORE_PRINCIPLES

        edges_created = 0
        for p in CORE_PRINCIPLES:
            if p["name"] not in activated_names:
                continue

            node_id = f"principle:{p['id']}"
            # Ensure principle node exists
            if node_id not in self._graph_nodes:
                self._graph_nodes[node_id] = {
                    "type": "principle",
                    "name": p["name"],
                    "description": p["content"],
                    "domain": p["domain"],
                }

            # Create edge: task_type -> principle
            edge = {
                "from": f"task_type:{task_type}",
                "to": node_id,
                "relation": "activates",
                "weight": 0.85,
            }
            if edge not in self._graph_edges:
                self._graph_edges.append(edge)
                edges_created += 1

        return edges_created

    def _activate_principles(self, task_type: str, task_description: str) -> list[str]:
        """P1: Three-channel principle activation.

        Channel 1 — Static task-type mapping: differentiated principle
        recommendations per task type (from TASK_TYPE_PRINCIPLE_MAP).

        Channel 2 — Keyword matching: literal keyword overlap between
        task description and principle keywords (preserved from v1).

        Channel 3 — Intent vector matching: cosine similarity between
        task embedding and pre-computed principle anchor embeddings.
        Surfaces principles whose intent aligns with the task even when
        no keyword matches (e.g., "拆成小类" → Occam's Razor).

        Falls back to channels 1+2 when embedder is unavailable.
        """
        from plastic_promise.core.constants import (
            CORE_PRINCIPLES,
            PRINCIPLE_INTENT_THRESHOLD,
            TASK_TYPE_PRINCIPLE_MAP,
        )

        activated_ids: set[int] = set()

        # Channel 1: Static task-type mapping (differentiated per task type)
        static_ids = TASK_TYPE_PRINCIPLE_MAP.get(task_type, [1, 2, 3])
        activated_ids.update(static_ids)

        # Channel 2: Keyword matching (literal)
        for p in CORE_PRINCIPLES:
            if p["id"] in activated_ids:
                continue
            keywords = p.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",")]
            for kw in keywords:
                if kw.strip() in task_description:
                    activated_ids.add(p["id"])
                    break

        # Channel 3: Intent vector matching (semantic)
        if self._principle_anchors and self._embedder is not None:
            try:
                task_vec = self._embedder.embed(task_description)
                if task_vec and any(v != 0.0 for v in task_vec):
                    for pid, anchor_vec in self._principle_anchors.items():
                        if pid in activated_ids:
                            continue
                        sim = self._cosine_similarity(task_vec, anchor_vec)
                        if sim >= PRINCIPLE_INTENT_THRESHOLD:
                            activated_ids.add(pid)
            except Exception:
                pass  # Intent matching is best-effort; degrade gracefully

        # Resolve IDs to names
        result = []
        for p in CORE_PRINCIPLES:
            if p["id"] in activated_ids:
                result.append(p["name"])
        return result

    # ========== BM25 helpers (static methods on ContextEngine) ==========

    _EN_STOPWORDS = frozenset(
        {
            "the",
            "a",
            "an",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "shall",
            "can",
            "need",
            "dare",
            "ought",
            "used",
            "to",
            "of",
            "in",
            "for",
            "on",
            "with",
            "at",
            "by",
            "from",
            "as",
            "into",
            "through",
            "during",
            "before",
            "after",
            "above",
            "below",
            "between",
            "under",
            "again",
            "further",
            "then",
            "once",
            "here",
            "there",
            "when",
            "where",
            "why",
            "how",
            "all",
            "both",
            "each",
            "few",
            "more",
            "most",
            "other",
            "some",
            "such",
            "only",
            "own",
            "same",
            "so",
            "than",
            "too",
            "very",
            "just",
            "because",
            "but",
            "and",
            "or",
            "if",
            "while",
            "about",
            "not",
            "this",
            "that",
            "these",
            "those",
            "it",
            "its",
        }
    )

    @staticmethod
    def _porter_stem(word: str) -> str:
        """Minimal Porter stemmer for common English suffixes."""
        w = word.lower()
        if len(w) <= 3:
            return w
        if w.endswith("sses") or w.endswith("ies"):
            w = w[:-2]
        elif w.endswith("ss"):
            pass
        elif w.endswith("s"):
            w = w[:-1]
        if w.endswith("eed") and len(w) > 4:
            w = w[:-1]
        elif w.endswith("ed") and not w.endswith("eed") and len(w) > 3:
            w = w[:-2]
        elif w.endswith("ing") and len(w) > 4:
            w = w[:-3]
        for suffix in (
            "ement",
            "ment",
            "ence",
            "ance",
            "able",
            "ible",
            "ment",
            "ent",
            "ant",
            "ism",
            "ate",
            "iti",
            "ous",
            "ive",
            "ize",
            "ion",
            "al",
            "er",
            "ic",
            "ou",
            "ly",
        ):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
        return w

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Tokenize text for BM25. CJK->bigram, English->split+stem+stopword."""
        if not text or not text.strip():
            return []
        # CJK detection: require >30% CJK chars to avoid false positives
        # from garbled/mixed-encoding text
        cjk_chars = sum(1 for c in text if "一" <= c <= "鿿")
        has_cjk = (cjk_chars / max(len(text), 1)) > 0.3
        if has_cjk:
            chars = [c for c in text if not c.isspace()]
            return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
        words = text.lower().split()
        return [
            ContextEngine._porter_stem(w.strip(".,!?;:()[]{}\"'-"))
            for w in words
            if len(w) >= 3 and w.lower() not in ContextEngine._EN_STOPWORDS
        ]

    @staticmethod
    def _compute_idf(doc_freq: dict[str, int], total_docs: int) -> dict[str, float]:
        """IDF = log((N - df + 0.5) / (df + 0.5) + 1)."""
        return {
            term: math.log((total_docs - df + 0.5) / (df + 0.5) + 1.0)
            for term, df in doc_freq.items()
        }

    @staticmethod
    def _bm25_score(
        query_terms: list[str],
        doc_terms: list[str],
        idf: dict[str, float],
        avg_doc_len: float,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> float:
        """Okapi BM25 score for one document."""
        doc_len = len(doc_terms)
        tf_counts = Counter(doc_terms)
        score = 0.0
        for term in query_terms:
            if term not in idf:
                continue
            tf = tf_counts.get(term, 0)
            if tf == 0:
                continue
            numerator = tf * (k1 + 1.0)
            denominator = tf + k1 * (1.0 - b + b * doc_len / avg_doc_len)
            score += idf[term] * numerator / denominator
        return score

    def _text_retrieval(self, task: str, trust_boost: float = 1.0) -> list[tuple]:
        """BM25 text retrieval with IDF weighting (Okapi BM25, k1=1.2, b=0.75).

        Replaces the old word-overlap matching. Builds document frequency table
        from self._memories on each call — fast enough at 192-doc scale (<5ms).
        """
        results = []
        query_terms = ContextEngine._tokenize(task)
        if not query_terms:
            return results

        current_owner = os.environ.get("AGENT_OWNER", "")
        domain_hint = getattr(self, "_domain_hint", None)
        dm = getattr(self, "_dm", None)
        has_dm = dm is not None and domain_hint and domain_hint != "all"
        hint_dm = dm.domains.get(domain_hint) if has_dm else None

        # --- Build DF table and pre-tokenize docs ---
        doc_terms: dict[str, list[str]] = {}
        doc_freq: dict[str, int] = {}
        eligible: list[str] = []

        for mid, mem in self._memories.items():
            mem_owner = mem.get("owner", "")
            if current_owner and mem_owner not in (current_owner, "shared", ""):
                continue
            content = mem.get("content", "")
            if not content.strip():
                continue
            tokens = ContextEngine._tokenize(content)
            if not tokens:
                continue
            doc_terms[mid] = tokens
            eligible.append(mid)
            unique_terms = set(tokens)
            for term in unique_terms:
                doc_freq[term] = doc_freq.get(term, 0) + 1

        if not eligible:
            return results

        total_docs = len(eligible)
        avg_doc_len = (
            sum(len(t) for t in doc_terms.values()) / total_docs if total_docs > 0 else 1.0
        )
        idf = ContextEngine._compute_idf(doc_freq, total_docs)

        # Score each document
        for mid in eligible:
            mem = self._memories[mid]
            tokens = doc_terms[mid]
            raw_score = ContextEngine._bm25_score(query_terms, tokens, idf, avg_doc_len)

            if raw_score <= 0:
                continue

            # Normalize BM25 score to [0, 1] using sigmoid with temperature 3
            score = 1.0 / (1.0 + math.exp(-raw_score / 3.0))

            # Tier boost
            tier = mem.get("tier", "L2")
            if tier == "L1":
                score = min(score * 1.5 * trust_boost, 1.0)
            elif tier == "L3":
                score = score * 0.8 * trust_boost

            # Domain boost
            if has_dm:
                mem_domain = mem.get("domain", "uncategorized")
                if mem_domain == domain_hint:
                    score = min(score * 1.3, 1.0)
                elif hint_dm:
                    mem_tags = set(mem.get("tags", []))
                    if mem_tags & hint_dm.tags:
                        score = min(score * 1.1, 1.0)

            content = mem["content"]
            results.append((mid, min(score, 1.0), content[:300], mem["source"]))

        # Deferred access tracking
        for mid, _, _, _ in results:
            mem = self._memories.get(mid)
            if mem:
                mem["access_count"] = mem.get("access_count", 0) + 1
                if mem.get("access_count", 0) >= 5:
                    mem["worth_success"] = mem.get("worth_success", 0) + 1
                # Real-time tier promotion: check after access_count increment
                self._maybe_adjust_tier(mid)

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def _vector_retrieval(self, task_vector: list[float]) -> list[tuple]:
        """Semantic vector retrieval via LanceDB ANN search.

        Falls back to empty list if LanceDB is unavailable.
        """
        if self._ldb is None:
            return []
        try:
            raw_results = self._ldb.search(
                vector=task_vector,
                k=20,
                scope=getattr(self, "_domain_hint", None),
            )
            # Convert LanceDB results to internal tuple format
            return [
                (mid, score, text[:300], "vector")
                for mid, score, text, _tier, _scope in raw_results
            ]
        except Exception as e:
            logging.warning("_vector_retrieval LanceDB failed, returning empty: %s", e)
            return []

    def _hybrid_fuse(
        self,
        vector_results: list[tuple],
        text_results: list[tuple],
        vector_weight: float = 0.7,
    ) -> list[tuple]:
        """Fuse vector and text retrieval results with weighted combination.

        Formula: fusedScore = vectorScore * 0.7 + textScore * 0.3
        BM25 high-score bypass: if text score >= 0.75, promote via 0.9 weight.

        Args:
            vector_results: [(id, score, content, source), ...] from LanceDB.
            text_results: [(id, score, content, source), ...] from _text_retrieval.
            vector_weight: Weight for vector scores (default 0.7).

        Returns:
            Fused result list sorted by combined score descending.
        """
        combined: dict[str, tuple[float, str, str]] = {}

        # Vector channel: weight × vector_weight
        for mid, score, content, source in vector_results:
            combined[mid] = (score * vector_weight, content, source)

        # Text channel: weight × (1 - vector_weight), with BM25 bypass
        text_weight = 1.0 - vector_weight
        for mid, score, content, source in text_results:
            w = score * text_weight
            # BM25 high-score bypass: keyword results >= 0.75 override semantic.
            # >= 0.90: exact text match → keep full score (no vector dilution)
            if score >= 0.90:
                w = score  # pure BM25, no vector dilution
            elif score >= 0.75:
                w = max(w, score * 0.9)
            if mid in combined:
                existing_score, existing_content, existing_source = combined[mid]
                combined[mid] = (max(existing_score, w), existing_content, existing_source)
            else:
                combined[mid] = (w, content, source)

        return [
            (mid, score, content, source)
            for mid, (score, content, source) in sorted(
                combined.items(), key=lambda x: x[1][0], reverse=True
            )
        ]

    def _graph_traversal(self, task_type: str) -> list[tuple]:
        """Fine-grained: principle association + entity link + deep-grammar traversal.

        P0 enhancement — follows three edge types, two passes (was three):
          Pass 1: task_type → principle (activates) — populates principle_nodes
          Pass 2: memory → entity (references) + principle → memory (governs)
        """
        results = []
        visited = set()
        principle_nodes = set()
        target = f"task_type:{task_type}"

        # Pass 1: task_type → principles — must run first to populate principle_nodes
        for edge in self._graph_edges:
            src = edge.get("from", "")
            if src == target:
                dst = edge.get("to", "")
                visited.add(dst)
                if dst.startswith("principle:"):
                    principle_nodes.add(dst)
                node = self._graph_nodes.get(dst, {})
                results.append(
                    (dst, edge.get("weight", 0.5), node.get("description", dst), "graph")
                )

        # Pass 2: references + governs — now principle_nodes is fully populated
        for edge in self._graph_edges:
            rel = edge.get("relation", "")
            src = edge.get("from", "")
            dst = edge.get("to", "")

            if rel == "references":
                if dst in visited and src in self._memories:
                    mem = self._memories[src]
                    results.append((src, 0.6, mem.get("content", "")[:300], "entity-link"))
            elif rel == "governs":
                if src in principle_nodes and dst in self._memories:
                    mem = self._memories[dst]
                    results.append(
                        (dst, edge.get("weight", 0.3), mem.get("content", "")[:300], "graph")
                    )

        return results

    def _layered_fuse(self, graph_results, text_results, vector_results) -> list[tuple]:
        """分层融合: text_results already fused via _hybrid_fuse — use scores as-is."""
        combined = {}
        # 类: fused text+vector results — keep scores unchanged
        for item_id, score, content, source in text_results:
            combined[item_id] = (score, content, source)

        # 细: graph traversal results — capped at 0.5 to not override retrieval
        for item_id, score, content, source in graph_results:
            w = min(score, 0.50)
            if item_id in combined:
                pass  # already have higher-quality retrieval result
            else:
                combined[item_id] = (w, content, source)

        return [
            (k, v[0], v[1], v[2])
            for k, v in sorted(combined.items(), key=lambda x: x[1][0], reverse=True)
        ]

    def _apply_symbol_rules(self, items, task: str) -> list[tuple]:
        result = []
        for item_id, score, content, source in items:
            boost = 1.0
            for category, keywords in SYMBOL_RULE_KEYWORDS.items():
                if any(kw in task for kw in keywords) or any(kw in content for kw in keywords):
                    if category == "security":
                        boost *= 1.5
                    elif category == "commitment":
                        boost *= 1.4
                    elif category == "quality":
                        boost *= 1.2
            result.append((item_id, min(score * boost, 1.0), content, source))
        return result

    def _apply_feedback(self, items: list[tuple]) -> list[tuple]:
        """P2: Apply feedback using MemoryRecord.worth_score as single source of truth.

        Old formula: score + self._feedback.get(item_id, 0.0)  — stale dict, never synced.
        New formula: score * (MULTIPLIER_MIN + MULTIPLIER_RANGE * worth_score)

        A memory with worth_score=1.0 (frequently adopted) retains full score.
        A memory with worth_score=0.5 (no observations) gets a slight neutral discount.
        A memory with worth_score=0.0 (frequently rejected) gets a 30% discount.
        """
        from plastic_promise.core.constants import (
            FEEDBACK_SCORE_MULTIPLIER_MIN,
            FEEDBACK_SCORE_MULTIPLIER_RANGE,
        )

        result = []
        for item_id, score, content, source in items:
            mem = self._memories.get(item_id, {})
            if mem:
                ws = mem.get("worth_success", 0)
                wf = mem.get("worth_failure", 0)
                total = ws + wf
                worth = (ws + 1.0) / (total + 2.0) if total > 0 else 0.5
            else:
                worth = 0.5  # No memory record → neutral
            multiplier = FEEDBACK_SCORE_MULTIPLIER_MIN + FEEDBACK_SCORE_MULTIPLIER_RANGE * worth
            adjusted = min(1.0, score * multiplier)
            result.append((item_id, adjusted, content, source))
        return result

    def _apply_edge_feedback(self):
        """P2: Evolve graph edge weights based on memory adoption patterns.

        For each edge whose relation involves memories (governs, embodies, references),
        update the edge weight via EMA: new_weight = (1-α)*old_weight + α*worth_score.
        Clamp to [FEEDBACK_EDGE_WEIGHT_MIN, FEEDBACK_EDGE_WEIGHT_MAX].

        Called at the end of each supply() call — lightweight and reactive.
        """
        from plastic_promise.core.constants import (
            FEEDBACK_EDGE_EMA_ALPHA,
            FEEDBACK_EDGE_WEIGHT_MAX,
            FEEDBACK_EDGE_WEIGHT_MIN,
        )

        memory_relations = {"governs", "embodies", "references"}
        alpha = FEEDBACK_EDGE_EMA_ALPHA

        for edge in self._graph_edges:
            if edge.get("relation") not in memory_relations:
                continue

            # Determine which node is the memory
            memory_id = None
            if (
                edge["from"].startswith("memory:")
                or not edge["from"].startswith("principle:")
                and not edge["from"].startswith("task_type:")
            ):
                memory_id = edge["from"]
            elif (
                edge["to"].startswith("memory:")
                or not edge["to"].startswith("principle:")
                and not edge["to"].startswith("task_type:")
            ):
                memory_id = edge["to"]

            if memory_id and memory_id in self._memories:
                mem = self._memories[memory_id]
                ws = mem.get("worth_success", 0)
                wf = mem.get("worth_failure", 0)
                total = ws + wf
                worth = (ws + 1.0) / (total + 2.0) if total > 0 else 0.5

                old_weight = edge.get("weight", 0.5)
                new_weight = (1.0 - alpha) * old_weight + alpha * worth
                edge["weight"] = max(
                    FEEDBACK_EDGE_WEIGHT_MIN, min(FEEDBACK_EDGE_WEIGHT_MAX, new_weight)
                )

    def _apply_edge_feedback_for_memory(self, memory_id: str):
        """P2: Update all graph edges involving a specific memory.

        Called after handle_feedback_apply() updates a MemoryRecord's worth counters.
        Only recomputes edges connected to the given memory_id — O(E) but focused.
        """
        from plastic_promise.core.constants import (
            FEEDBACK_EDGE_EMA_ALPHA,
            FEEDBACK_EDGE_WEIGHT_MAX,
            FEEDBACK_EDGE_WEIGHT_MIN,
        )

        if memory_id not in self._memories:
            return

        mem = self._memories[memory_id]
        ws = mem.get("worth_success", 0)
        wf = mem.get("worth_failure", 0)
        total = ws + wf
        worth = (ws + 1.0) / (total + 2.0) if total > 0 else 0.5
        alpha = FEEDBACK_EDGE_EMA_ALPHA

        memory_relations = {"governs", "embodies", "references"}
        for edge in self._graph_edges:
            if edge.get("relation") not in memory_relations:
                continue
            if edge.get("from") == memory_id or edge.get("to") == memory_id:
                old_weight = edge.get("weight", 0.5)
                new_weight = (1.0 - alpha) * old_weight + alpha * worth
                edge["weight"] = max(
                    FEEDBACK_EDGE_WEIGHT_MIN, min(FEEDBACK_EDGE_WEIGHT_MAX, new_weight)
                )

    def _calc_freshness(self, item_id: str) -> str:
        mem = self._memories.get(item_id, {})
        created = mem.get("created_at", "")
        if not created or not self._current_time:
            return "valid"
        try:
            created_date = created[:10]
            now_date = self._current_time[:10]
            if created_date == now_date:
                return "fresh"
            created_parts = created_date.split("-")
            now_parts = now_date.split("-")
            if len(created_parts) == 3 and len(now_parts) == 3:
                created_days = (
                    int(created_parts[0]) * 365 + int(created_parts[1]) * 30 + int(created_parts[2])
                )
                now_days = int(now_parts[0]) * 365 + int(now_parts[1]) * 30 + int(now_parts[2])
                diff = now_days - created_days
                if diff <= 1:
                    return "fresh"
                elif diff <= 7:
                    return "valid"
                elif diff <= 30:
                    return "stale"
                else:
                    return "expired"
        except (ValueError, IndexError):
            pass
        return "valid"

    def _calc_decay_status(self, item_id: str, mem: dict) -> str:
        """P3b: Compute decay status label from memory age and tier.

        Uses a simplified Weibull-inspired decay model:
          fresh: created today or decay_multiplier >= 0.90
          healthy: decay >= 0.60
          stale: decay >= 0.30
          decaying: decay >= 0.10
          expired: below 0.10
        """
        from plastic_promise.core.constants import DECAY_CONFIG, DECAY_STATUS_THRESHOLDS

        created = mem.get("created_at", "")
        tier = mem.get("tier", "L1")
        if not created or not self._current_time:
            return "healthy"

        try:
            created_date = created[:10]
            now_date = self._current_time[:10]
            created_parts = created_date.split("-")
            now_parts = now_date.split("-")
            if len(created_parts) != 3 or len(now_parts) != 3:
                return "healthy"

            created_days = (
                int(created_parts[0]) * 365 + int(created_parts[1]) * 30 + int(created_parts[2])
            )
            now_days = int(now_parts[0]) * 365 + int(now_parts[1]) * 30 + int(now_parts[2])
            age_days = max(0, now_days - created_days)

            tier_config = DECAY_CONFIG.get(tier, DECAY_CONFIG["default"])
            half_life = tier_config.get("half_life_days", 14)
            # Simple exponential decay: decay = 2^(-age/half_life)
            decay = 2.0 ** (-age_days / half_life) if half_life > 0 else 1.0

            for label, threshold in sorted(
                DECAY_STATUS_THRESHOLDS.items(), key=lambda x: x[1], reverse=True
            ):
                if decay >= threshold:
                    return label
            return "expired"
        except (ValueError, IndexError):
            return "healthy"

    def _compute_divergent_quality(
        self,
        divergent_items: list[ContextItem],
        all_retrieved: list[ContextItem],
    ) -> list[ContextItem]:
        """P3a: Score divergent items on novelty + confidence, filter noise.

        For each divergent item:
          novelty = 1.0 - max_content_similarity (via embedder or fallback)
          confidence = 0.4*worth_score + 0.3*source_quality + 0.3*relevance
          inspiration = novelty * confidence

        Items with inspiration < DIVERGENT_QUALITY_THRESHOLD are removed.
        Surviving items get their novelty/confidence/inspiration fields populated.
        """
        from plastic_promise.core.constants import (
            DIVERGENT_QUALITY_THRESHOLD,
            SOURCE_QUALITY_MAP,
        )

        if not divergent_items:
            return divergent_items

        for item in divergent_items:
            # Confidence: blend worth_score, source quality, relevance
            source_quality = SOURCE_QUALITY_MAP.get(item.source, 0.5)
            confidence = 0.4 * item.worth_score + 0.3 * source_quality + 0.3 * item.relevance
            item.confidence = confidence

            # Novelty: compute via embedder if available, else fallback
            if self._embedder is not None and len(all_retrieved) > 1:
                try:
                    item_vec = self._embedder.embed(item.content)
                    max_sim = 0.0
                    for other in all_retrieved:
                        if other.id == item.id:
                            continue
                        other_vec = self._embedder.embed(other.content) if self._embedder else None
                        if other_vec:
                            sim = self._cosine_similarity(item_vec, other_vec)
                            max_sim = max(max_sim, sim)
                    item.novelty_score = 1.0 - max_sim
                except Exception:
                    # Fallback: domain-based heuristic — different source = more novel
                    item.novelty_score = 0.3 if item.source not in ("graph", "entity-link") else 0.6
            else:
                # No embedder: heuristic — vector-sourced items are more novel
                item.novelty_score = 0.4 if item.source in ("graph", "text") else 0.6

            item.inspiration_score = item.novelty_score * item.confidence

        # Filter: keep only items above quality threshold
        return [
            item
            for item in divergent_items
            if item.inspiration_score >= DIVERGENT_QUALITY_THRESHOLD
        ]


# ============================================================
# SQLite 持久化存储 — 写穿透模式
# ============================================================


class _SQLiteStorage:
    """SQLite write-through backend for ContextEngine memories.

    Enabled via AGENT_USE_SQLITE=1 env var. Every memory mutation
    (register/store/update/delete) is persisted to disk immediately.

    Batch mode: use ``with storage.batch():`` context manager to defer
    commits until the block exits. Within a batch, commits are skipped
    and a single commit is issued at exit.
    """

    def __init__(self, db_path: str = None):
        import sqlite3

        if db_path is None:
            db_path = get_db_path()
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "  id TEXT PRIMARY KEY,"
            "  content TEXT,"
            "  memory_type TEXT,"
            "  source TEXT,"
            "  owner TEXT,"
            "  tier TEXT,"
            "  scope TEXT,"
            "  category TEXT,"
            "  importance REAL,"
            "  entity_ids TEXT,"
            "  created_at TEXT,"
            "  access_count INTEGER,"
            "  worth_success INTEGER,"
            "  worth_failure INTEGER,"
            "  activation_weight REAL,"
            "  last_accessed TEXT"
            ")"
        )
        self._conn.commit()
        self._batch_depth = 0  # nesting counter for batch mode

        # 迁移: 新增 tags 和 domain 列 (SQLite ALTER TABLE 不支持 IF NOT EXISTS)
        try:
            self._conn.execute("ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            pass  # 列已存在
        try:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN domain TEXT NOT NULL DEFAULT 'uncategorized'"
            )
        except Exception:
            pass  # 列已存在
        # 迁移: 新增 decay_multiplier 和 effective_half_life 列
        try:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN decay_multiplier REAL NOT NULL DEFAULT 1.0"
            )
        except Exception:
            pass  # 列已存在
        try:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN effective_half_life REAL NOT NULL DEFAULT 3.0"
            )
        except Exception:
            pass  # 列已存在
        # 迁移: 新增 last_accessed 列 (Fix: pipeline writes to this column for decay tracking)
        try:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN last_accessed TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass  # 列已存在
        # 迁移: memory_version 表 — Rust 引擎用版本号检测 BM25 索引是否需要刷新
        self._conn.execute("CREATE TABLE IF NOT EXISTS memory_version (version INTEGER DEFAULT 0)")
        self._conn.execute("INSERT OR IGNORE INTO memory_version (version) VALUES (0)")
        self._conn.commit()

        # 存量迁移: 对已有记忆一次性计算真实衰减值
        try:
            from plastic_promise.core.decay_engine import WeibullDecayCalculator

            decay_calc = WeibullDecayCalculator()
            now = datetime.datetime.now().isoformat()
            rows = self._conn.execute(
                "SELECT id, tier, created_at FROM memories WHERE decay_multiplier = 1.0"
            ).fetchall()
            if rows:
                for row in rows:
                    mid, tier, created_at = row
                    dm = decay_calc.compute_decay(
                        tier=tier or "L1",
                        created_at=created_at or now,
                        current_time_str=now,
                    )
                    self._conn.execute(
                        "UPDATE memories SET decay_multiplier = ? WHERE id = ?", (dm, mid)
                    )
                self._conn.commit()
                logging.info("Bulk decay migration: %d memories updated", len(rows))
        except Exception as e:
            logging.warning("Bulk decay migration skipped: %s", e)

    def upsert(self, mid: str, data: dict):
        """Insert or update a memory record."""
        import json

        self._conn.execute(
            "INSERT OR REPLACE INTO memories (id, content, memory_type, source, owner, "
            "tier, scope, category, tags, domain, importance, entity_ids, created_at, access_count, "
            "worth_success, worth_failure, activation_weight, decay_multiplier, effective_half_life, "
            "last_accessed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mid,
                data.get("content", ""),
                data.get("memory_type", "experience"),
                data.get("source", "user"),
                data.get("owner", ""),
                data.get("tier", "L1"),
                data.get("scope", "global"),
                data.get("category", "other"),
                json.dumps(data.get("tags", [])),
                data.get("domain", "uncategorized"),
                data.get("importance", 0.7),
                json.dumps(data.get("entity_ids", [])),
                data.get("created_at", ""),
                data.get("access_count", 0),
                data.get("worth_success", 0),
                data.get("worth_failure", 0),
                data.get("activation_weight", 0.5),
                data.get("decay_multiplier", 1.0),
                data.get("effective_half_life", 3.0),
                data.get("last_accessed", ""),
            ),
        )
        if self._batch_depth <= 0:
            self._conn.commit()
            # Increment memory_version so Rust engine knows to refresh BM25 index
            self._conn.execute("UPDATE memory_version SET version = version + 1")
            self._conn.commit()

    def get(self, mid: str) -> dict | None:
        """Retrieve a single memory record."""
        row = self._conn.execute(
            "SELECT id, content, memory_type, source, owner, tier, scope, category, "
            "tags, domain, importance, entity_ids, created_at, access_count, "
            "worth_success, worth_failure, activation_weight, "
            "decay_multiplier, effective_half_life, last_accessed "
            "FROM memories WHERE id = ?",
            (mid,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete(self, mid: str):
        """Delete a memory record."""
        self._conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
        if self._batch_depth <= 0:
            self._conn.commit()
            self._conn.execute("UPDATE memory_version SET version = version + 1")
            self._conn.commit()

    def iter_all(self):
        """Iterate all memory records."""
        rows = self._conn.execute(
            "SELECT id, content, memory_type, source, owner, tier, scope, category, "
            "tags, domain, importance, entity_ids, created_at, access_count, "
            "worth_success, worth_failure, activation_weight, "
            "decay_multiplier, effective_half_life, last_accessed FROM memories"
        ).fetchall()
        for row in rows:
            d = self._row_to_dict(row)
            yield d["id"], d

    def commit(self):
        """Explicit commit (useful after batch operations)."""
        self._conn.commit()

    class _BatchContext:
        """Context manager for batch writes — defers commits until exit."""

        def __init__(self, storage):
            self._storage = storage

        def __enter__(self):
            self._storage._batch_depth += 1
            return self

        def __exit__(self, *args):
            self._storage._batch_depth -= 1
            if self._storage._batch_depth <= 0:
                self._storage._batch_depth = 0
                self._storage._conn.commit()

    def batch(self):
        """Return a context manager that batches writes into a single commit.

        Usage:
            with storage.batch():
                storage.upsert(id1, data1)
                storage.upsert(id2, data2)
                # ... more writes ...
            # single commit() here
        """
        return self._BatchContext(self)

    def _row_to_dict(self, row) -> dict:
        """Convert a SQLite row to a dict matching the in-memory format."""
        import json

        return {
            "id": row[0],
            "content": row[1],
            "memory_type": row[2],
            "source": row[3],
            "owner": row[4],
            "tier": row[5],
            "scope": row[6],
            "category": row[7],
            "tags": json.loads(row[8]) if row[8] else [],
            "domain": row[9] or "uncategorized",
            "importance": row[10],
            "entity_ids": json.loads(row[11]) if row[11] else [],
            "created_at": row[12],
            "access_count": row[13] or 0,
            "worth_success": row[14] or 0,
            "worth_failure": row[15] or 0,
            "activation_weight": row[16] or 0.5,
            "decay_multiplier": row[17] if len(row) > 17 else 1.0,
            "effective_half_life": row[18] if len(row) > 18 else 3.0,
            "last_accessed": row[19] if len(row) > 19 else "",
        }
