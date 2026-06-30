"""ContextEngine Python 回退实现

当 Rust context-engine-core 不可用时使用此纯 Python 版本。
接口与 Rust 版本保持一致，确保上层无感切换。

生产环境应使用 Rust 版本以获得更好性能。
"""

import datetime
import json
import logging
import os
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from plastic_promise.core.constants import (
    CONTEXT_LAYERS,
    SYMBOL_RULE_KEYWORDS,
    ASSOCIATION_WEIGHTS,
    PRINCIPLE_INHERITANCE_DECAY,
)


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
    novelty_score: float = 0.0       # 与检索集中其他项的不相似度 [0,1]
    confidence: float = 0.5           # 检索置信度（来源质量+worth+相关性）
    inspiration_score: float = 0.0    # novelty * confidence（灵感综合分）
    # P3b: 生命轨迹
    adoption_count: int = 0           # 被采纳次数 (← worth_success)
    rejection_count: int = 0          # 被拒绝次数 (← worth_failure)
    times_retrieved: int = 0          # 被检索次数 (← access_count)
    decay_status: str = "healthy"     # fresh|healthy|stale|decaying|expired

    def to_prompt_line(self) -> str:
        """Render one context item with life-trajectory annotations (P3b)."""
        mark = " 🧬" if self.is_principle else ""
        traj = ""
        if self.adoption_count > 0 or self.rejection_count > 0:
            traj = f" [✓{self.adoption_count}✗{self.rejection_count}]"
        if self.decay_status in ("stale", "decaying", "expired"):
            traj += f" ⚠{self.decay_status}"
        return f"- [{self.relevance:.2f}]{mark}{traj} [{self.source}] {self.content[:200]}"


@dataclass
class ContextPack:
    """三层上下文包"""
    core: List[ContextItem] = field(default_factory=list)
    related: List[ContextItem] = field(default_factory=list)
    divergent: List[ContextItem] = field(default_factory=list)
    activated_principles: List[str] = field(default_factory=list)
    audit_metadata: Dict[str, str] = field(default_factory=dict)

    def to_prompt(self) -> str:
        lines = []
        if self.activated_principles:
            lines.append("## 🧬 核心约定参考（约定优于约束——决策前主动查阅）")
            from plastic_promise.core.constants import CORE_PRINCIPLES
            for name in self.activated_principles:
                # Find principle by name and show full reference
                match = next((p for p in CORE_PRINCIPLES if p["name"] == name), None)
                if match:
                    lines.append(f"### {name}")
                    lines.append(f"> {match['content']}")
                    lines.append(f"**⚠️ 违反后果**：指标失真，系统健康度不可信" if match["id"] == 1 else
                                f"**⚠️ 违反后果**：Agent退化为最小合规，失去内在动机" if match["id"] == 2 else
                                f"**⚠️ 违反后果**：记忆退化为被动档案库，上下文枯竭" if match["id"] == 3 else
                                f"**⚠️ 违反后果**：原则形同虚设，行为与约定脱节" if match["id"] == 4 else
                                f"**⚠️ 违反后果**：虚假安全感，机制存在但不产生效果" if match["id"] == 5 else
                                f"**⚠️ 违反后果**：数据流断裂，系统各自为战" if match["id"] == 6 else
                                f"**⚠️ 违反后果**：单点故障扩散，连锁崩溃" if match["id"] == 7 else
                                f"**⚠️ 违反后果**：LLM失去感官，退化为纯文本补全" if match["id"] == 8 else
                                f"**⚠️ 违反后果**：自主权错配，高分冒险低分难行" if match["id"] == 9 else
                                f"**⚠️ 违反后果**：反馈信号丢失，行为漂移偏离约定" if match["id"] == 10 else
                                f"**⚠️ 违反后果**：约定无法跨代传递，新Agent从零训练" if match["id"] == 11 else
                                f"**⚠️ 违反后果**：代码腐化，维护成本指数增长，新人无法上手")
                else:
                    lines.append(f"- {name}")
            lines.append("")
        if self.core:
            lines.append("## 🔵 核心上下文（必读）")
            for item in self.core:
                lines.append(item.to_prompt_line())
            lines.append("")
        if self.related:
            lines.append("## 🟡 关联上下文（参考）")
            for item in self.related:
                lines.append(item.to_prompt_line())
            lines.append("")
        if self.divergent:
            lines.append("## 🟢 发散联想（灵感）")
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

    def __init__(self, id: str = "", content: str = "",
                 memory_type: str = "experience", source: str = "user",
                 owner: str = ""):
        self.id = id
        self.content = content
        self.memory_type = memory_type
        self.source = source
        self.owner: str = owner or os.environ.get("AGENT_OWNER", "")
        self.scope: str = "global"           # deprecated — use domain
        self.category: str = "other"         # deprecated — use domain
        self.tags: list[str] = []             # NEW: 多标签
        self.domain: str = "uncategorized"    # NEW: 域标签
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
        self._graph_nodes: Dict[str, Dict[str, Any]] = {}
        self._graph_edges: List[Dict[str, Any]] = []
        self._feedback: Dict[str, float] = {}  # item_id -> accumulated delta (P2: 替换为 worth_score)
        self.enable_principles: bool = True
        self._current_time: str = ""
        self._memories: Dict[str, Dict[str, Any]] = {}
        self._principle_anchors: Dict[int, List[float]] = {}  # P1: 原则锚点向量

        # Heavy components — lazy-initialized by _ensure_heavy_init()
        self._dm: Any = None
        self._dm_ok: bool = False
        self._domain_hint: Optional[str] = None
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
                elif eid.startswith("principle:"):
                    node_id = eid  # already prefixed
                elif eid.startswith("task:"):
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
            logging.info("_rebuild_graph_from_memories: created %d principle↔memory edges from %d memories",
                         total_edges, len(self._memories))

    def _ensure_heavy_init(self):
        """Lazy-initialize heavy components: DomainManager, LanceDB, embedder, principle anchors.

        Called once on first supply() call. Avoids expensive embedding/DB init at ContextEngine
        construction time — critical for fast session-init and high-concurrency scenarios.
        """
        if self._heavy_init_done:
            return
        self._heavy_init_done = True

        # DB path — used by both DomainManager and LanceDBStore
        db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")

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
                logging.warning("ContextEngine: embedder unavailable — intent matching disabled")

        # Initialize LanceDB vector store
        if self._ldb is None:
            try:
                from plastic_promise.core.lancedb_store import LanceDBStore
                ldb_path = os.environ.get("PLASTIC_LANCEDB_PATH",
                                           os.path.join(os.path.dirname(db_path or "plastic_memory.db"),
                                                        "plastic_memory.lancedb"))
                self._ldb = LanceDBStore(ldb_path, self._embedder or get_embedder(fallback_on_error=True))
                self._ldb.backfill(self)
                logging.info("ContextEngine: LanceDBStore ready (backfill complete)")
            except Exception as e:
                logging.warning("ContextEngine: LanceDBStore init failed — vector search disabled: %s", e)
                self._ldb = None

        # P1: Build principle anchor embeddings for intent matching (cached by embedder)
        self._build_principle_anchors()

        self._current_time: str = ""

    # ========== 记忆管理 ==========

    def register_memory(self, record: Dict[str, Any]) -> str:
        mid = record.get("id", f"mem_{len(self._memories)}")
        data = {
            "id": mid,
            "content": record.get("content", ""),
            "memory_type": record.get("memory_type", "experience"),
            "source": record.get("source", "user"),
            "owner": record.get("owner", os.environ.get("AGENT_OWNER", "")),
            "tier": record.get("tier", "L1"),
            "scope": record.get("scope", "global"),       # deprecated — use domain
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

    def register_memories(self, records: List[Dict[str, Any]]) -> List[str]:
        return [self.register_memory(r) for r in records]

    @property
    def memory_count(self) -> int:
        return len(self._memories)

    def set_current_time(self, iso_timestamp: str):
        self._current_time = iso_timestamp

    # ========== P0: 原则↔记忆图谱边 (深层语法) ==========

    def _build_principle_edges_for_memory(
        self, memory_id: str, memory_data: dict
    ) -> int:
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
            PRINCIPLE_EDGE_MIN_KEYWORD_HITS,
            PRINCIPLE_EDGE_BASE_WEIGHT,
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
            weight = min(PRINCIPLE_EDGE_BASE_WEIGHT + PRINCIPLE_EDGE_SCALE_WEIGHT * keyword_ratio, 1.0)
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

        anchors: Dict[int, List[float]] = {}
        try:
            for p in CORE_PRINCIPLES:
                vec = self._embedder.embed(p["content"])
                if vec and any(v != 0.0 for v in vec):
                    anchors[p["id"]] = vec
            if anchors:
                logging.info("_build_principle_anchors: computed %d/%d principle anchors",
                            len(anchors), len(CORE_PRINCIPLES))
        except Exception as e:
            logging.warning("_build_principle_anchors failed: %s — intent matching disabled", e)
            anchors = {}

        self._principle_anchors = anchors

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
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

    def load_graph(self, graph_data: Dict[str, Any]):
        self._graph_nodes = graph_data.get("nodes", {})
        self._graph_edges = graph_data.get("edges", [])

    def get_graph(self) -> GraphInfo:
        return GraphInfo(self._graph_nodes, self._graph_edges)

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

    def update_memory(self, memory_id: str, content=None,
                      importance=None, category=None) -> bool:
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

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory by id. Returns True if it existed."""
        if memory_id in self._memories:
            del self._memories[memory_id]
            if self._sqlite:
                self._sqlite.delete(memory_id)
            return True
        return False

    def list_memories(self, memory_type=None, source=None,
                      min_worth=None, limit=50, scope=None) -> list:
        """List memories with optional filters.

        Returns a list of MemoryRecord objects matching the filter criteria.
        """
        results = []
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
            results.append(self.get_memory(mid))
            if len(results) >= limit:
                break
        return results

    def memory_stats_json(self, scope=None) -> str:
        """Return memory pool statistics as a JSON string.

        Compatible with the Rust ContextEngine.memory_stats_json() interface.
        """
        total = len(self._memories)
        by_type: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        healthy = 0
        decaying = 0
        worth_sum = 0.0

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
            if ws_val >= 0.15:
                healthy += 1
            else:
                decaying += 1

        return json.dumps({
            "total": total,
            "healthy": healthy,
            "decaying": decaying,
            "by_type": by_type,
            "by_category": by_category,
            "by_tier": by_tier,
            "average_worth": round(worth_sum / total, 3) if total > 0 else 0.0,
        }, ensure_ascii=False)

    # ========== 核心方法: supply() ==========

    def supply(
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
            trust_boost = tm.get_retrieval_boost()
        except Exception:
            trust_boost = 1.0

        # Phase 1: 三路分层检索 — 细→类→粗
        # 细 (graph): 原则关联图谱 — 最精确的信号
        graph_results = self._graph_traversal(task_type)

        # 类 (tier): 文本匹配 + L1 工作记忆优先级提升
        self._domain_hint = scope if scope and scope != "global" else None
        text_results = self._text_retrieval(task_description, trust_boost)

        # 粗 (vector): 语义向量相关性 (零向量时跳过)
        vector_results = self._vector_retrieval(task_vector) if any(v != 0.0 for v in task_vector) else []

        # Phase 2: Hybrid fusion (vector + text) then layer with graph
        if vector_results:
            fused_results = self._hybrid_fuse(vector_results, text_results, vector_weight=0.7)
        else:
            # No vector available (Ollama down / zero vector) — use text only
            fused_results = [(mid, score * 0.8, content, source) for mid, score, content, source in text_results]
        all_results = self._layered_fuse(graph_results, fused_results, [])

        # P2: Evolve edge weights based on feedback patterns
        self._apply_edge_feedback()

        # Phase 3-5 fused: symbol rules + feedback + ContextItem building (was 3 passes, now 1)
        from plastic_promise.core.constants import (
            FEEDBACK_SCORE_MULTIPLIER_MIN,
            FEEDBACK_SCORE_MULTIPLIER_RANGE,
        )
        pack = ContextPack(activated_principles=activated)

        for item_id, score, content, source in all_results:
            # --- Symbol rule boost (was _apply_symbol_rules) ---
            boost = 1.0
            for category, keywords in SYMBOL_RULE_KEYWORDS.items():
                if any(kw in task_description for kw in keywords) or any(kw in content for kw in keywords):
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

            if score >= CONTEXT_LAYERS["core"]["min_relevance"]:
                item.layer = "core"
                pack.core.append(item)
            elif score >= CONTEXT_LAYERS["related"]["min_relevance"]:
                item.layer = "related"
                pack.related.append(item)
            elif score >= CONTEXT_LAYERS["divergent"]["min_relevance"]:
                item.layer = "divergent"
                pack.divergent.append(item)

        # P3a: Compute divergent quality and filter low-inspiration items
        if pack.divergent:
            all_retrieved = pack.core + pack.related + pack.divergent
            pack.divergent = self._compute_divergent_quality(
                pack.divergent, all_retrieved
            )

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
        }

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
                f"Unknown entity_type '{entity_type}'. "
                f"Valid: {', '.join(sorted(valid_types))}"
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
                nid: self._graph_nodes[nid]
                for nid in neighbor_ids
                if nid in self._graph_nodes
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

    # ========== 内部方法 ==========

    def _inject_activated_to_graph(
        self, activated_names: List[str], task_type: str
    ) -> int:
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

    def _activate_principles(self, task_type: str, task_description: str) -> List[str]:
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
            TASK_TYPE_PRINCIPLE_MAP,
            PRINCIPLE_INTENT_THRESHOLD,
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

    def _text_retrieval(self, task: str, trust_boost: float = 1.0) -> List[tuple]:
        """粗匹配: CJK bigram / word split + L1 tier boost (大类优先)."""
        results = []
        import re
        has_cjk = bool(re.search(r'[一-鿿]', task))

        if has_cjk:
            task_bigrams = set()
            for i in range(len(task) - 1):
                bigram = task[i:i+2]
                if not re.search(r'[\s，。！？、；：,.!?;:\s]', bigram):
                    task_bigrams.add(bigram)
        else:
            task_bigrams = set(task.lower().split())

        if not task_bigrams:
            return results

        current_owner = os.environ.get("AGENT_OWNER", "")
        # Hoist repeated lookups outside hot loop
        domain_hint = getattr(self, '_domain_hint', None)
        dm = getattr(self, '_dm', None)
        has_dm = dm is not None and domain_hint and domain_hint != "all"
        hint_dom = dm.domains.get(domain_hint) if has_dm else None

        accessed: list[str] = []  # defer access tracking to post-loop

        for mid, mem in self._memories.items():
            # Owner filter: only own memories + shared (owner="shared" or owner="")
            mem_owner = mem.get("owner", "")
            if current_owner and mem_owner not in (current_owner, "shared", ""):
                continue

            content = mem["content"]
            if has_cjk:
                hits = sum(1.0 for bg in task_bigrams if bg in content)
                score = hits / len(task_bigrams)
            else:
                hits = sum(1.0 for w in task_bigrams if w.lower() in content.lower())
                score = hits / len(task_bigrams) if task_bigrams else 0

            if score > 0:
                # 大类优先: L1 working memory gets 1.5× boost
                tier = mem.get("tier", "L2")
                if tier == "L1":
                    score = min(score * 1.5 * trust_boost, 1.0)
                elif tier == "L3":
                    score = score * 0.8 * trust_boost  # long-term slightly de-prioritized

                # 域加权: 同域 ×1.3, 融合域 (同标签) ×1.1
                if has_dm:
                    mem_domain = mem.get("domain", "uncategorized")
                    if mem_domain == domain_hint:
                        score = min(score * 1.3, 1.0)
                    elif hint_dom:
                        mem_tags = set(mem.get("tags", []))
                        if mem_tags & hint_dom.tags:
                            score = min(score * 1.1, 1.0)

                results.append((mid, min(score, 1.0), content[:300], mem["source"]))
                accessed.append(mid)

        # Deferred access tracking — batch update outside hot loop
        for mid in accessed:
            mem = self._memories.get(mid)
            if mem:
                mem["access_count"] = mem.get("access_count", 0) + 1
                if mem.get("access_count", 0) >= 5:
                    mem["worth_success"] = mem.get("worth_success", 0) + 1
        return results

    def _vector_retrieval(self, task_vector: list[float]) -> List[tuple]:
        """Semantic vector retrieval via LanceDB ANN search.

        Falls back to empty list if LanceDB is unavailable.
        """
        if self._ldb is None:
            return []
        try:
            raw_results = self._ldb.search(
                vector=task_vector,
                k=20,
                scope=getattr(self, '_domain_hint', None),
            )
            # Convert LanceDB results to internal tuple format
            return [(mid, score, text[:300], "vector") for mid, score, text, _tier, _scope in raw_results]
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
            # BM25 high-score bypass: keyword results >= 0.75 override semantic
            if score >= 0.75:
                w = max(w, score * 0.9)
            if mid in combined:
                existing_score, existing_content, existing_source = combined[mid]
                combined[mid] = (max(existing_score, w), existing_content, existing_source)
            else:
                combined[mid] = (w, content, source)

        return [(mid, score, content, source)
                for mid, (score, content, source) in
                sorted(combined.items(), key=lambda x: x[1][0], reverse=True)]

    def _graph_traversal(self, task_type: str) -> List[tuple]:
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
                results.append((dst, edge.get("weight", 0.5),
                                node.get("description", dst), "graph"))

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
                    results.append((dst, edge.get("weight", 0.3),
                                    mem.get("content", "")[:300], "graph"))

        return results

    def _layered_fuse(self, graph_results, text_results, vector_results) -> List[tuple]:
        """分层融合: 细(graph ×1.0) > 类(text+L1 ×0.8) > 粗(vector ×0.6)."""
        combined = {}
        # 细: graph results — highest weight
        for item_id, score, content, source in graph_results:
            combined[item_id] = (score * 1.0, content, source, "graph")

        # 类: text with L1 tier boost already applied — medium weight
        for item_id, score, content, source in text_results:
            w = score * 0.8
            if item_id in combined:
                combined[item_id] = (max(combined[item_id][0], w), combined[item_id][1], combined[item_id][2], combined[item_id][3])
            else:
                combined[item_id] = (w, content, source, "text")

        # 粗: vector similarity — lowest weight
        for item_id, score, content, source in vector_results:
            w = score * 0.6
            if item_id in combined:
                combined[item_id] = (max(combined[item_id][0], w), combined[item_id][1], combined[item_id][2], combined[item_id][3])
            else:
                combined[item_id] = (w, content, source, "vector")

        return [(k, v[0], v[1], v[2]) for k, v in
                sorted(combined.items(), key=lambda x: x[1][0], reverse=True)]

    def _apply_symbol_rules(self, items, task: str) -> List[tuple]:
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

    def _apply_feedback(self, items: List[tuple]) -> List[tuple]:
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
            FEEDBACK_EDGE_WEIGHT_MIN,
            FEEDBACK_EDGE_WEIGHT_MAX,
        )

        memory_relations = {"governs", "embodies", "references"}
        alpha = FEEDBACK_EDGE_EMA_ALPHA

        for edge in self._graph_edges:
            if edge.get("relation") not in memory_relations:
                continue

            # Determine which node is the memory
            memory_id = None
            if edge["from"].startswith("memory:") or not edge["from"].startswith("principle:") and not edge["from"].startswith("task_type:"):
                memory_id = edge["from"]
            elif edge["to"].startswith("memory:") or not edge["to"].startswith("principle:") and not edge["to"].startswith("task_type:"):
                memory_id = edge["to"]

            if memory_id and memory_id in self._memories:
                mem = self._memories[memory_id]
                ws = mem.get("worth_success", 0)
                wf = mem.get("worth_failure", 0)
                total = ws + wf
                worth = (ws + 1.0) / (total + 2.0) if total > 0 else 0.5

                old_weight = edge.get("weight", 0.5)
                new_weight = (1.0 - alpha) * old_weight + alpha * worth
                edge["weight"] = max(FEEDBACK_EDGE_WEIGHT_MIN,
                                    min(FEEDBACK_EDGE_WEIGHT_MAX, new_weight))

    def _apply_edge_feedback_for_memory(self, memory_id: str):
        """P2: Update all graph edges involving a specific memory.

        Called after handle_feedback_apply() updates a MemoryRecord's worth counters.
        Only recomputes edges connected to the given memory_id — O(E) but focused.
        """
        from plastic_promise.core.constants import (
            FEEDBACK_EDGE_EMA_ALPHA,
            FEEDBACK_EDGE_WEIGHT_MIN,
            FEEDBACK_EDGE_WEIGHT_MAX,
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
                edge["weight"] = max(FEEDBACK_EDGE_WEIGHT_MIN,
                                    min(FEEDBACK_EDGE_WEIGHT_MAX, new_weight))

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
                created_days = int(created_parts[0]) * 365 + int(created_parts[1]) * 30 + int(created_parts[2])
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
        from plastic_promise.core.constants import DECAY_STATUS_THRESHOLDS, DECAY_CONFIG

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

            created_days = int(created_parts[0]) * 365 + int(created_parts[1]) * 30 + int(created_parts[2])
            now_days = int(now_parts[0]) * 365 + int(now_parts[1]) * 30 + int(now_parts[2])
            age_days = max(0, now_days - created_days)

            tier_config = DECAY_CONFIG.get(tier, DECAY_CONFIG["default"])
            half_life = tier_config.get("half_life_days", 14)
            # Simple exponential decay: decay = 2^(-age/half_life)
            decay = 2.0 ** (-age_days / half_life) if half_life > 0 else 1.0

            for label, threshold in sorted(DECAY_STATUS_THRESHOLDS.items(),
                                           key=lambda x: x[1], reverse=True):
                if decay >= threshold:
                    return label
            return "expired"
        except (ValueError, IndexError):
            return "healthy"

    def _compute_divergent_quality(
        self,
        divergent_items: List[ContextItem],
        all_retrieved: List[ContextItem],
    ) -> List[ContextItem]:
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
        return [item for item in divergent_items
                if item.inspiration_score >= DIVERGENT_QUALITY_THRESHOLD]


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
            db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
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
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"
            )
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
                        "UPDATE memories SET decay_multiplier = ? WHERE id = ?",
                        (dm, mid)
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

    def get(self, mid: str) -> dict | None:
        """Retrieve a single memory record."""
        row = self._conn.execute(
            "SELECT id, content, memory_type, source, owner, tier, scope, category, "
            "tags, domain, importance, entity_ids, created_at, access_count, "
            "worth_success, worth_failure, activation_weight, "
            "decay_multiplier, effective_half_life, last_accessed "
            "FROM memories WHERE id = ?", (mid,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete(self, mid: str):
        """Delete a memory record."""
        self._conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
        if self._batch_depth <= 0:
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
