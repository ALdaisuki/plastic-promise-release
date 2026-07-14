"""ContextEngine Python 回退实现

当 Rust context-engine-core 不可用时使用此纯 Python 版本。
接口与 Rust 版本保持一致，确保上层无感切换。

生产环境应使用 Rust 版本以获得更好性能。
"""

import contextlib
import copy
import datetime
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import TYPE_CHECKING, Any, Optional

from plastic_promise.core.behavior_graph import graph_edge, graph_node, validate_node_type
from plastic_promise.core.constants import (
    CONTEXT_LAYERS,
    PP_SOURCE_FILTER,
    PRINCIPLE_INHERITANCE_DECAY,
    SOURCE_DOWNWEIGHT,
    SOURCE_EXCLUDE,
    SYMBOL_RULE_KEYWORDS,
)
from plastic_promise.core.fusion_policy import (
    FusionConfig,
    FusionDecision,
    load_fusion_config,
    weighted_rrf,
)
from plastic_promise.core.paths import get_db_path
from plastic_promise.core.retrieval_planner import RetrievalPlan, plan_retrieval

logger = logging.getLogger(__name__)


class OrdinaryMemoryConflict(RuntimeError):
    """Reject an unsafe or conflicting ordinary-memory mutation."""


_ORDINARY_JSON_PATCH_FIELDS = frozenset(
    {
        "tags",
        "entity_ids",
        "parent_memory_ids",
        "metadata_json",
    }
)
_ORDINARY_NUMERIC_INCREMENT_FIELDS = frozenset(
    {
        "access_count",
        "worth_success",
        "worth_failure",
    }
)
_ORDINARY_SCALAR_PATCH_FIELDS = frozenset(
    {
        "content",
        "memory_type",
        "source",
        "owner",
        "tier",
        "scope",
        "category",
        "domain",
        "importance",
        "created_at",
        "access_count",
        "worth_success",
        "worth_failure",
        "activation_weight",
        "decay_multiplier",
        "effective_half_life",
        "last_accessed",
        "project_id",
        "visibility",
        "source_class",
        "created_by_call_id",
        "origin_kind",
        "origin_uri",
        "origin_ref",
        "origin_hash",
        "raw_content",
        "l0_abstract",
        "l1_summary",
        "l2_content",
        "embedding_text",
        "embedding_hash",
        "search_text",
    }
)
_ORDINARY_PATCH_COLUMN_ORDER = (
    "content",
    "memory_type",
    "source",
    "owner",
    "tier",
    "scope",
    "category",
    "tags",
    "domain",
    "importance",
    "entity_ids",
    "created_at",
    "access_count",
    "worth_success",
    "worth_failure",
    "activation_weight",
    "decay_multiplier",
    "effective_half_life",
    "last_accessed",
    "project_id",
    "visibility",
    "source_class",
    "created_by_call_id",
    "origin_kind",
    "origin_uri",
    "origin_ref",
    "origin_hash",
    "parent_memory_ids",
    "metadata_json",
    "raw_content",
    "l0_abstract",
    "l1_summary",
    "l2_content",
    "embedding_text",
    "embedding_hash",
    "search_text",
)
_ORDINARY_NUMERIC_REPLACEMENT_FIELDS = frozenset(
    {
        "importance",
        "access_count",
        "worth_success",
        "worth_failure",
        "activation_weight",
        "decay_multiplier",
        "effective_half_life",
    }
)
_RETRIEVAL_VISIBLE_PATCH_FIELDS = frozenset(
    (_ORDINARY_SCALAR_PATCH_FIELDS | _ORDINARY_JSON_PATCH_FIELDS)
    - {"last_accessed", "created_by_call_id"}
)
_ORDINARY_AVAILABILITY_PATCH_FIELDS = frozenset({"tags", "metadata_json"})
_ORDINARY_INDEX_PROJECTION_PATCH_FIELDS = frozenset({"tier", "scope", "category"})

if TYPE_CHECKING:
    from collections.abc import Iterator

    from plastic_promise.core.exemplar_gap_detector import GapSignal

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
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    pipeline_stats: dict[str, Any] = field(default_factory=dict)
    per_item_stats: list[dict[str, Any]] = field(default_factory=list)
    channel_rankings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    channel_states: dict[str, dict[str, Any]] = field(default_factory=dict)
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
        mode = self.audit_metadata.get("mode")
        budget = self.audit_metadata.get("budget")
        raw_evidence = self.audit_metadata.get("raw_evidence")
        if mode or budget or raw_evidence:
            lines.append("## [RETRIEVAL_PLAN]")
            if mode:
                lines.append(f"- mode: {mode}")
            if isinstance(budget, dict):
                lines.append(f"- budget: {json.dumps(budget, ensure_ascii=False)}")
                lines.append(f"- raw_evidence_budget: {budget.get('raw_evidence', 0)}")
            if isinstance(raw_evidence, list) and raw_evidence:
                lines.append("- raw_evidence:")
                for evidence in raw_evidence[:5]:
                    source = evidence.get("source", "?")
                    item_id = evidence.get("id", "?")
                    score = evidence.get("score", 0.0)
                    content = str(evidence.get("content", ""))[:120]
                    lines.append(f"  - [{source}] {item_id} score={score:.3f} {content}")
            lines.append("")

        recommender = self.audit_metadata.get("context_recommender")
        if isinstance(recommender, dict):
            recommendations = recommender.get("recommendations")
            if isinstance(recommendations, list) and recommendations:
                lines.append("## [CONTEXT_RECOMMENDER]")
                for rec in recommendations[:5]:
                    reasons = ",".join(rec.get("reasons", []))
                    lines.append(
                        f"- {rec.get('id', '?')} score={rec.get('score', 0.0):.3f} "
                        f"layer={rec.get('layer', '')} reasons={reasons}"
                    )
                lines.append("")

        request_scope = self.audit_metadata.get("request_scope")
        if isinstance(request_scope, dict) and request_scope.get("request_scope_id"):
            lines.append("## [REQUEST_SCOPE] Audit Trace")
            lines.append(f"- request_scope_id: {request_scope['request_scope_id']}")
            lines.append(f"- stage_session_id: {request_scope.get('stage_session_id', '')}")
            lines.append(f"- flow_line_id: {request_scope.get('flow_line_id', '')}")
            lines.append(f"- request_id: {request_scope.get('request_id', '')}")
            lines.append("")

        return "\n".join(lines)

    @property
    def total_items(self) -> int:
        return len(self.core) + len(self.related) + len(self.divergent)


NONCANONICAL_CONTEXT_PREFIXES = (
    "principle:",
    "code:",
    "mcp_tool:",
    "task_state:",
    "bilingual_synonym:",
)
_SYNTHESIS_FALLBACK_HARD_DENY_REASONS = frozenset(
    {
        "candidate_lookup_error",
        "candidate_missing",
        "candidate_state_unavailable",
        "candidate_type_invalid",
        "candidate_type_mismatch",
        "canonical_gate_unavailable",
        "control_missing",
        "memory_version_invalid",
        "memory_version_mismatch",
        "retrieval_disabled",
        "synthesis_validation_error",
        "transaction_open",
        "verification_evidence_missing",
    }
)
_VERIFIED_SYNTHESIS_FALLBACK_REASONS = frozenset(
    {
        "candidate_payload_mismatch",
        "contradiction_open",
        "source_fingerprint_mismatch",
        "source_hash_mismatch",
        "source_missing",
        "source_revision_mismatch",
        "source_superseded",
        "source_synthesis_invalid",
        "source_unavailable",
        "support_count_mismatch",
        "synthesis_provenance_unavailable",
    }
)
_SYNTHESIS_REVIEW_TASKS = frozenset({"audit", "code_audit", "code_review", "review"})


def resolve_project_metadata(engine: Any, item_id: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve project fields with canonical SQLite precedence when available."""
    sqlite_storage = getattr(engine, "_sqlite", None) if engine is not None else None
    conn = getattr(sqlite_storage, "_conn", None)
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT project_id, visibility, source_class FROM memories WHERE id = ?",
                (item_id,),
            ).fetchone()
        except Exception:
            return None, "error"
        if row is None:
            return None, "canonical_missing"
        project_id, visibility, source_class = row
        if (
            not isinstance(project_id, str)
            or not project_id.strip()
            or visibility not in {"project", "shared", "global", "private"}
            or not isinstance(source_class, str)
            or not source_class.strip()
        ):
            return None, "error"
        return {
            "project_id": project_id,
            "visibility": visibility,
            "source_class": source_class,
        }, "canonical"

    memories = getattr(engine, "_memories", None) if engine is not None else None
    memory = memories.get(item_id) if isinstance(memories, dict) else None
    if not isinstance(memory, dict):
        return None, "runtime_missing"
    metadata: dict[str, Any] = {}
    nested = memory.get("metadata") or memory.get("metadata_json")
    if isinstance(nested, dict):
        metadata.update(nested)
    metadata.update(memory)
    return metadata, "runtime"


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


# Persistent storage and public admission live in this facade. Rust is a
# snapshot-ranking accelerator and must not become an alternate write path.
class _RustSynthesisFallback(RuntimeError):
    """Signal that Python must rank a snapshot containing admitted synthesis."""


class _RustFusionFallback(RuntimeError):
    """Signal that the requested fusion policy requires Python."""


class ContextEngine:
    """上下文供应引擎 (Python 回退版)

    生产环境请使用 Rust 版: from context_engine_core import ContextEngine
    """

    def __init__(self, use_sqlite: bool = None):
        # Fail at engine startup when the environment-only index fault seam is invalid.
        from plastic_promise.core.synthesis_maintenance import (
            validate_test_index_failure_configuration,
        )

        validate_test_index_failure_configuration()
        self._graph_nodes: dict[str, dict[str, Any]] = {}
        self._graph_edges: list[dict[str, Any]] = []
        self._edge_feedback_base_weights: dict[tuple[str, ...], float] = {}
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
        self._code_index: Any = None
        self._code_index_root: str = ""
        # Canonical snapshots and ordinary writes share one replacement lock.
        self._write_lock = threading.RLock()
        self._manual_batch_state: dict[str, Any] | None = None
        self._loaded_memory_version: int | None = None
        self.canonical_sync_ok: bool = True

        # SQLite write-through — persists every mutation to disk (default ON)
        if use_sqlite is None:
            use_sqlite = os.environ.get("AGENT_USE_SQLITE", "1") != "0"
        self._sqlite = _SQLiteStorage() if use_sqlite else None
        if self._sqlite:
            self._refresh_canonical_cache_if_changed(force=True)
        else:
            self._rebuild_graph_from_memories()

        # P0: Rebuild principle↔memory graph edges from persisted memories

        # Heavy init deferred to first supply() call
        self._heavy_init_done = False
        self._heavy_init_lock = threading.Lock()
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
        edge_identities = {self._graph_edge_identity(edge) for edge in self._graph_edges}
        governed_ids = self._governed_synthesis_ids(self._memories)
        for mid, mem in self._memories.items():
            if mid in governed_ids:
                continue
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
                identity = self._graph_edge_identity(edge)
                if identity not in edge_identities:
                    self._graph_edges.append(edge)
                    edge_identities.add(identity)

        # Pass 2: ensure skill_session nodes exist for orphan entity_ids
        for mid, mem in self._memories.items():
            if mid in governed_ids:
                continue
            entity_ids = mem.get("entity_ids", [])
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
            if mid in governed_ids:
                continue
            total_edges += self._build_principle_edges_for_memory(
                mid,
                mem,
                edge_identities=edge_identities,
            )
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

            # Initialize LanceDB vector store. Stdio MCP processes are short-lived and
            # can run concurrently, so they skip LanceDB by default to avoid blocking
            # user-facing context_supply on connect/open_table/FTS maintenance.
            ldb_init_setting = os.environ.get("LDB_INIT_ON_HEAVY_INIT")
            if ldb_init_setting is None:
                ldb_init_enabled = os.environ.get("PLASTIC_MCP_TRANSPORT") != "stdio"
            else:
                ldb_init_enabled = ldb_init_setting == "1"

            if not ldb_init_enabled:
                logging.info(
                    "ContextEngine: LanceDBStore init deferred "
                    "(set LDB_INIT_ON_HEAVY_INIT=1 to enable during init)"
                )
            elif self._ldb is None:
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
                    backfill_on_init = os.environ.get("LDB_BACKFILL_ON_INIT", "0") == "1"
                    rebuild_on_init = os.environ.get("LDB_REBUILD_ON_INIT", "0") == "1"
                    if backfill_on_init:
                        self._ldb.backfill(self)
                    else:
                        logging.info(
                            "ContextEngine: LanceDB backfill deferred "
                            "(set LDB_BACKFILL_ON_INIT=1 to run during init)"
                        )
                    if rebuild_on_init and hasattr(self._ldb, "sync_with_engine"):
                        try:
                            sync_result = self._ldb.sync_with_engine(self)
                            self._lancedb_sync_status = {"success": True, **sync_result}
                            logging.info(
                                "ContextEngine: LanceDB sync repaired orphans=%s, missing=%s, skipped=%s",
                                sync_result.get("orphan_deleted", 0),
                                sync_result.get("missing_backfilled", 0),
                                sync_result.get("missing_skipped", 0),
                            )
                        except Exception as e:
                            self._lancedb_sync_status = {"success": False, "error": str(e)}
                            logging.warning(
                                "ContextEngine: LanceDB sync degraded; continuing startup: %s",
                                e,
                            )
                    # Ghost-vector detection: if LanceDB has more rows than SQLite,
                    # there are stale test/pollution vectors — rebuild from SQLite
                    ldb_count = self._ldb.count_rows()
                    sqlite_count = len(self._memories)
                    if ldb_count > sqlite_count:
                        if rebuild_on_init:
                            logging.warning(
                                "ContextEngine: LanceDB has %d rows but SQLite has %d memories"
                                " - rebuilding to remove %d ghost vectors",
                                ldb_count,
                                sqlite_count,
                                ldb_count - sqlite_count,
                            )
                            self._ldb.rebuild_all(self)
                        else:
                            logging.warning(
                                "ContextEngine: LanceDB has %d rows but SQLite has %d memories"
                                " - rebuild deferred for %d ghost vectors "
                                "(set LDB_REBUILD_ON_INIT=1 to run during init)",
                                ldb_count,
                                sqlite_count,
                                ldb_count - sqlite_count,
                            )
                    logging.info("ContextEngine: LanceDBStore ready")
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

    def _governed_synthesis_ids(self, ids) -> set[str]:
        requested = {str(memory_id) for memory_id in ids}
        governed = {
            memory_id
            for memory_id in requested
            if str((self._memories.get(memory_id) or {}).get("memory_type") or "")
            .strip()
            .casefold()
            == "synthesis"
        }
        conn = getattr(self._sqlite, "_conn", None)
        if conn is None:
            return governed
        try:
            governed.update(
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM memories "
                    "WHERE LOWER(TRIM(COALESCE(memory_type, ''))) = 'synthesis' "
                    "UNION SELECT memory_id FROM synthesis_artifacts"
                ).fetchall()
                if str(row[0]) in requested
            )
        except Exception:
            return requested
        return governed

    def _synthesis_memory_reserved(self, memory_id: str) -> bool:
        current = self._memories.get(memory_id)
        conn = getattr(self._sqlite, "_conn", None)
        if conn is None:
            return str((current or {}).get("memory_type") or "").strip().casefold() == "synthesis"
        from plastic_promise.core.synthesis_retrieval import (
            is_governed_synthesis_memory,
        )

        return is_governed_synthesis_memory(
            conn,
            memory_id,
            memory_type=(current or {}).get("memory_type"),
        )

    def _create_ordinary_memory(
        self,
        memory_id: str,
        data: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        if self._sqlite is None:
            current = self._memories.get(memory_id)
            if current is None:
                return copy.deepcopy(data), True
            if current != data:
                raise OrdinaryMemoryConflict("ordinary_memory_already_exists")
            return copy.deepcopy(current), False
        create = getattr(self._sqlite, "create_ordinary_if_absent", None)
        if not callable(create):
            raise OrdinaryMemoryConflict("ordinary_create_sqlite_required")
        return create(memory_id, data)

    def patch_ordinary_memory(
        self,
        memory_id: str,
        *,
        replacements: Mapping[str, Any] | None = None,
        increments: Mapping[str, int | float] | None = None,
        expected_project_id: str | None = None,
        require_source_available: bool = False,
        expected_tags: list[str] | tuple[str, ...] | None = None,
        expected_category: str | None = None,
        expected_content_hash: str | None = None,
        expected_embedding_hash: str | None = None,
        expected_snapshot: Mapping[str, Any] | None = None,
        bump_memory_version: bool | None = None,
        index_upsert_call_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply one field-scoped patch to the canonical ordinary row."""
        with self._write_lock:
            replacement_fields = set(replacements) if isinstance(replacements, Mapping) else set()
            automatic_index_upsert = (
                bool(replacement_fields & _ORDINARY_INDEX_PROJECTION_PATCH_FIELDS)
                and index_upsert_call_id is None
            )
            if index_upsert_call_id is not None:
                if not isinstance(index_upsert_call_id, str) or not index_upsert_call_id.strip():
                    raise OrdinaryMemoryConflict("ordinary_patch_index_call_id_invalid")
                index_upsert_call_id = index_upsert_call_id.strip()
            storage = self._sqlite
            patch = getattr(storage, "patch_ordinary", None)
            if storage is None:
                return self._patch_in_memory_ordinary(
                    memory_id,
                    replacements=replacements,
                    increments=increments,
                    expected_project_id=expected_project_id,
                    require_source_available=require_source_available,
                    expected_tags=expected_tags,
                    expected_category=expected_category,
                    expected_content_hash=expected_content_hash,
                    expected_embedding_hash=expected_embedding_hash,
                    expected_snapshot=expected_snapshot,
                    bump_memory_version=bump_memory_version,
                )
            if not callable(patch):
                raise OrdinaryMemoryConflict("ordinary_patch_sqlite_required")

            conn = getattr(storage, "_conn", None)
            caller_transaction_open = bool(conn is not None and conn.in_transaction)
            patch_kwargs = {
                "replacements": replacements,
                "increments": increments,
                "expected_project_id": expected_project_id,
                "require_source_available": require_source_available,
                "expected_tags": expected_tags,
                "expected_category": expected_category,
                "expected_content_hash": expected_content_hash,
                "expected_embedding_hash": expected_embedding_hash,
                "expected_snapshot": expected_snapshot,
                "bump_memory_version": bump_memory_version,
                "preserve_source_availability": True,
            }
            if index_upsert_call_id is not None or automatic_index_upsert:

                def enqueue_projection(canonical: dict[str, Any]) -> None:
                    from plastic_promise.core.memory_index import (
                        read_persisted_index_material,
                    )
                    from plastic_promise.core.traceability import (
                        enqueue_memory_index_upsert,
                    )

                    material = read_persisted_index_material(canonical)
                    project_id = str(canonical.get("project_id") or "").strip()
                    embedding_hash = (
                        material.embedding_hash
                        if material is not None
                        else str(canonical.get("embedding_hash") or "").strip()
                    )
                    if index_upsert_call_id is not None and material is None:
                        raise OrdinaryMemoryConflict("ordinary_patch_index_material_invalid")
                    if not embedding_hash and automatic_index_upsert:
                        return
                    if not project_id or not embedding_hash:
                        raise OrdinaryMemoryConflict("ordinary_patch_index_material_invalid")
                    enqueue_memory_index_upsert(
                        conn,
                        memory_id=memory_id,
                        project_id=project_id,
                        expected_embedding_hash=embedding_hash,
                        call_id=(
                            index_upsert_call_id or f"ordinary-patch:{memory_id}:{uuid.uuid4().hex}"
                        ),
                    )

                patch_kwargs["after_patch"] = enqueue_projection
            canonical = patch(memory_id, **patch_kwargs)
            if not caller_transaction_open and not (conn is not None and conn.in_transaction):
                self._memories[memory_id] = copy.deepcopy(canonical)
            elif self._manual_batch_state is not None:
                self._manual_batch_state.setdefault("pending_memory_deletes", set()).discard(
                    memory_id
                )
                self._manual_batch_state.setdefault("pending_memory_updates", {})[memory_id] = (
                    copy.deepcopy(canonical)
                )
            return canonical

    def _patch_in_memory_ordinary(
        self,
        memory_id: str,
        *,
        replacements: Mapping[str, Any] | None,
        increments: Mapping[str, int | float] | None,
        expected_project_id: str | None,
        require_source_available: bool,
        expected_tags: list[str] | tuple[str, ...] | None,
        expected_category: str | None,
        expected_content_hash: str | None,
        expected_embedding_hash: str | None,
        expected_snapshot: Mapping[str, Any] | None,
        bump_memory_version: bool | None,
    ) -> dict[str, Any]:
        """Apply the same narrow CAS contract for an explicitly in-memory engine."""
        if replacements is not None and not isinstance(replacements, Mapping):
            raise OrdinaryMemoryConflict("ordinary_patch_field_not_allowed")
        if increments is not None and not isinstance(increments, Mapping):
            raise OrdinaryMemoryConflict("ordinary_patch_field_not_allowed")
        if expected_snapshot is not None and not isinstance(expected_snapshot, Mapping):
            raise OrdinaryMemoryConflict("ordinary_patch_expected_snapshot_invalid")
        replacement_values = dict(replacements or {})
        increment_values = dict(increments or {})
        if not replacement_values and not increment_values:
            raise OrdinaryMemoryConflict("ordinary_patch_empty")
        replacement_fields = set(replacement_values)
        increment_fields = set(increment_values)
        if (
            not replacement_fields <= (_ORDINARY_SCALAR_PATCH_FIELDS | _ORDINARY_JSON_PATCH_FIELDS)
            or not increment_fields <= _ORDINARY_NUMERIC_INCREMENT_FIELDS
        ):
            raise OrdinaryMemoryConflict("ordinary_patch_field_not_allowed")
        if replacement_fields & increment_fields:
            raise OrdinaryMemoryConflict("ordinary_patch_field_conflict")
        if bump_memory_version is not None and not isinstance(bump_memory_version, bool):
            raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
        if not isinstance(require_source_available, bool):
            raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
        if expected_project_id is not None:
            expected_project_id = str(expected_project_id).strip()
            if not expected_project_id:
                raise OrdinaryMemoryConflict("ordinary_patch_expected_project_required")
        if expected_tags is not None and (
            not isinstance(expected_tags, (list, tuple))
            or not all(isinstance(tag, str) for tag in expected_tags)
        ):
            raise OrdinaryMemoryConflict("ordinary_patch_expected_tags_invalid")
        if (
            "memory_type" in replacement_values
            and str(replacement_values["memory_type"] or "").strip().casefold() == "synthesis"
        ):
            raise OrdinaryMemoryConflict("ordinary_memory_reserved")

        for field_name, value in replacement_values.items():
            if field_name in _ORDINARY_NUMERIC_REPLACEMENT_FIELDS and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
            if isinstance(value, float) and not math.isfinite(value):
                raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
            if field_name in _ORDINARY_JSON_PATCH_FIELDS:
                try:
                    json.dumps(value, ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise OrdinaryMemoryConflict("ordinary_patch_value_invalid") from exc
        for value in increment_values.values():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise OrdinaryMemoryConflict("ordinary_patch_increment_invalid")

        expected_values = dict(expected_snapshot or {})
        if not set(expected_values) <= (_ORDINARY_SCALAR_PATCH_FIELDS - {"content"}):
            raise OrdinaryMemoryConflict("ordinary_patch_expected_snapshot_invalid")
        current = self._memories.get(memory_id)
        if not isinstance(current, dict):
            raise OrdinaryMemoryConflict("ordinary_patch_target_not_found")
        if str(current.get("memory_type") or "").strip().casefold() == "synthesis":
            raise OrdinaryMemoryConflict("ordinary_memory_reserved")

        from plastic_promise.core.synthesis import synthesis_content_hash
        from plastic_promise.core.synthesis_retrieval import _source_is_available

        cas_mismatch = (
            (
                expected_project_id is not None
                and str(current.get("project_id") or "").strip() != str(expected_project_id).strip()
            )
            or (
                expected_content_hash is not None
                and synthesis_content_hash(current.get("content")) != expected_content_hash
            )
            or (
                expected_embedding_hash is not None
                and str(current.get("embedding_hash") or "") != expected_embedding_hash
            )
            or (
                expected_tags is not None and list(current.get("tags") or []) != list(expected_tags)
            )
            or (
                expected_category is not None
                and str(current.get("category") or "other") != expected_category
            )
            or any(
                current.get(field_name) != value for field_name, value in expected_values.items()
            )
            or (require_source_available and not _source_is_available(current))
        )
        if cas_mismatch:
            raise OrdinaryMemoryConflict("ordinary_patch_cas_mismatch")

        canonical = copy.deepcopy(current)
        canonical.update(copy.deepcopy(replacement_values))
        for field_name, value in increment_values.items():
            current_value = canonical.get(field_name, 0)
            if isinstance(current_value, bool) or not isinstance(current_value, (int, float)):
                raise OrdinaryMemoryConflict("ordinary_patch_increment_invalid")
            canonical[field_name] = current_value + value
        if replacement_fields & _ORDINARY_AVAILABILITY_PATCH_FIELDS:
            try:
                availability_changed = _source_is_available(current) != _source_is_available(
                    canonical
                )
            except Exception as exc:
                raise OrdinaryMemoryConflict("ordinary_patch_availability_invalid") from exc
            if availability_changed:
                raise OrdinaryMemoryConflict(
                    "ordinary_patch_availability_change_requires_coordinator"
                )
        self._memories[memory_id] = canonical
        return copy.deepcopy(canonical)

    def apply_ordinary_feedback(
        self,
        memory_id: str,
        feedback_type: str,
        *,
        expected_project_id: str | None = None,
        require_source_available: bool = False,
    ) -> dict[str, Any]:
        """Persist one ordinary-memory feedback observation atomically.

        Worth counters influence ranking and graph feedback. Unlike a generic
        counter increment, this operation therefore always advances the
        canonical snapshot version so other engine processes reload it.
        """
        normalized = str(feedback_type or "").strip().casefold()
        increments: dict[str, int | float]
        if normalized == "adopted":
            increments = {"worth_success": 1}
        elif normalized == "rejected":
            increments = {"worth_failure": 1}
        elif normalized == "ignored":
            increments = {"worth_failure": 0.5}
        else:
            raise OrdinaryMemoryConflict("ordinary_feedback_type_invalid")
        return self.patch_ordinary_memory(
            memory_id,
            increments=increments,
            expected_project_id=expected_project_id,
            require_source_available=require_source_available,
            bump_memory_version=True,
        )

    def reset_ordinary_worth(self, memory_id: str) -> dict[str, Any]:
        """Reset ordinary-memory feedback counters without hydrating a record."""
        return self.patch_ordinary_memory(
            memory_id,
            replacements={"worth_success": 0, "worth_failure": 0},
            bump_memory_version=True,
        )

    def reinforce_ordinary_duplicate(
        self,
        memory_id: str,
        *,
        entity_ids: list[str] | tuple[str, ...],
        last_accessed: str,
        expected_project_id: str,
        expected_visibility: str,
        expected_source_class: str,
        expected_memory_type: str,
    ) -> dict[str, Any]:
        """Atomically merge duplicate provenance and reinforce its worth.

        Deduplication is multi-process work. The entity-id union and numeric
        reinforcement therefore have to read the canonical row only after the
        SQLite write transaction has been acquired.
        """
        expected_binding = {
            "project_id": str(expected_project_id or "").strip(),
            "visibility": str(expected_visibility or "").strip(),
            "source_class": str(expected_source_class or "").strip(),
            "memory_type": str(expected_memory_type or "").strip(),
        }
        if not all(expected_binding.values()):
            raise OrdinaryMemoryConflict("ordinary_duplicate_binding_required")
        with self._write_lock:
            storage = self._sqlite
            if storage is None or not callable(getattr(storage, "patch_ordinary", None)):
                raise OrdinaryMemoryConflict("ordinary_patch_sqlite_required")
            conn = storage._conn
            caller_transaction_open = bool(conn.in_transaction)
            with storage.batch():
                current = storage.get(memory_id)
                if current is None:
                    raise OrdinaryMemoryConflict("ordinary_patch_target_not_found")
                if any(
                    str(current.get(field) or "").strip() != expected
                    for field, expected in expected_binding.items()
                ):
                    raise OrdinaryMemoryConflict("ordinary_patch_cas_mismatch")
                existing_ids = [
                    str(value) for value in current.get("entity_ids", []) if str(value).strip()
                ]
                known_ids = set(existing_ids)
                merged_ids = existing_ids + sorted(
                    {
                        str(value)
                        for value in entity_ids
                        if str(value).strip() and str(value) not in known_ids
                    }
                )
                current_last_accessed = str(current.get("last_accessed") or "").strip()
                requested_last_accessed = str(last_accessed or "").strip()
                effective_last_accessed = requested_last_accessed or current_last_accessed
                if current_last_accessed and requested_last_accessed:
                    try:

                        def parse_timestamp(value: str) -> datetime.datetime:
                            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
                            parsed = datetime.datetime.fromisoformat(normalized)
                            if parsed.tzinfo is None:
                                return parsed.astimezone(datetime.timezone.utc)
                            return parsed.astimezone(datetime.timezone.utc)

                        if parse_timestamp(current_last_accessed) >= parse_timestamp(
                            requested_last_accessed
                        ):
                            effective_last_accessed = current_last_accessed
                    except (TypeError, ValueError):
                        effective_last_accessed = max(
                            current_last_accessed,
                            requested_last_accessed,
                        )

                new_access_count = int(current.get("access_count", 0) or 0) + 1
                effective_half_life = current.get("effective_half_life")
                try:
                    from plastic_promise.core.constants import DECAY_CONFIG
                    from plastic_promise.core.decay_engine import AccessReinforcement

                    tier = str(current.get("tier") or "L1")
                    base_half_life = DECAY_CONFIG.get(tier, DECAY_CONFIG["default"])[
                        "half_life_days"
                    ]
                    _, effective_half_life = AccessReinforcement().compute_boost(
                        access_count=new_access_count,
                        last_accessed=effective_last_accessed,
                        base_half_life=base_half_life,
                        is_auto_recall=False,
                        current_time_str=effective_last_accessed,
                    )
                except Exception:
                    pass
                replacements: dict[str, Any] = {
                    "entity_ids": merged_ids,
                    "last_accessed": effective_last_accessed,
                }
                if effective_half_life is not None:
                    replacements["effective_half_life"] = effective_half_life
                canonical = storage.patch_ordinary(
                    memory_id,
                    replacements=replacements,
                    increments={"access_count": 1, "worth_success": 1},
                    expected_project_id=expected_binding["project_id"],
                    expected_snapshot={
                        field: expected_binding[field]
                        for field in ("visibility", "source_class", "memory_type")
                    },
                    require_source_available=True,
                    bump_memory_version=True,
                )
            if not caller_transaction_open and not conn.in_transaction:
                self._memories[memory_id] = copy.deepcopy(canonical)
            elif self._manual_batch_state is not None:
                self._manual_batch_state.setdefault("pending_memory_deletes", set()).discard(
                    memory_id
                )
                self._manual_batch_state.setdefault("pending_memory_updates", {})[memory_id] = (
                    copy.deepcopy(canonical)
                )
            return canonical

    def _delete_ordinary_memory(self, memory_id: str) -> bool:
        if self._sqlite is None:
            return True
        guarded = getattr(self._sqlite, "delete_ordinary", None)
        if callable(guarded):
            return bool(guarded(memory_id))
        if getattr(self._sqlite, "_conn", None) is not None:
            return False
        legacy = getattr(self._sqlite, "delete", None)
        if not callable(legacy):
            return False
        return legacy(memory_id) is not False

    def _persist_ordinary_graph_node(
        self,
        node_id: str,
        node: dict[str, Any],
        *,
        reservation_ids: tuple[str, ...] = (),
    ) -> bool:
        if self._sqlite is None:
            return True
        guarded = getattr(self._sqlite, "upsert_graph_node_ordinary", None)
        if callable(guarded):
            return bool(
                guarded(
                    node_id,
                    node,
                    reservation_ids=reservation_ids,
                )
            )
        if hasattr(self._sqlite, "_conn"):
            return False
        legacy = getattr(self._sqlite, "upsert_graph_node", None)
        if not callable(legacy):
            return False
        return legacy(node_id, node) is not False

    def _public_memory_ids(
        self,
        ids,
        *,
        allow_review: bool = False,
        extra_types: dict[str, object] | None = None,
    ) -> tuple[str, ...]:
        """Return ids safe for a public read before content is inspected."""
        ordered_ids = tuple(dict.fromkeys(str(memory_id) for memory_id in ids if memory_id))
        if not ordered_ids:
            return ()
        memory_types = {
            memory_id: (self._memories.get(memory_id) or {}).get("memory_type")
            for memory_id in ordered_ids
        }
        memory_types.update(extra_types or {})
        conn = getattr(self._sqlite, "_conn", None)
        if conn is None:
            if self._sqlite is not None and hasattr(self._sqlite, "_conn"):
                return ()
            return tuple(
                memory_id
                for memory_id in ordered_ids
                if str(memory_types.get(memory_id) or "").strip().casefold() != "synthesis"
            )

        from plastic_promise.core.synthesis_retrieval import (
            evaluate_public_memory_ids,
            read_memory_version,
        )

        try:
            current_memory_version = read_memory_version(conn)
        except Exception:
            current_memory_version = -1
        memory_version = current_memory_version
        if (
            self._loaded_memory_version is not None
            and current_memory_version != self._loaded_memory_version
        ):
            memory_version = self._loaded_memory_version
        if not self.canonical_sync_ok or conn.in_transaction:
            memory_version = -1
        return evaluate_public_memory_ids(
            conn,
            ordered_ids,
            allow_review=allow_review,
            memory_version=memory_version,
            memory_types=memory_types,
        ).items

    def _stable_public_memory_read(
        self,
        ids,
        *,
        allow_review: bool = False,
        include_rows: bool,
    ):
        """Admit public ids and optionally copy rows at one canonical version."""
        ordered_ids = tuple(dict.fromkeys(str(memory_id) for memory_id in ids if memory_id))
        if not ordered_ids:
            return ()
        with self._write_lock:
            conn = getattr(self._sqlite, "_conn", None)
            stable_version: int | None = None
            version_available = False
            if conn is not None:
                try:
                    from plastic_promise.core.synthesis_retrieval import read_memory_version

                    if conn.in_transaction:
                        version_available = False
                    else:
                        stable_version = read_memory_version(conn)
                        version_available = True
                except Exception:
                    # Legacy databases with an invalid version still expose
                    # ordinary rows; _public_memory_ids keeps synthesis closed.
                    version_available = False

            visible_ids = self._public_memory_ids(
                ordered_ids,
                allow_review=allow_review,
            )
            if not version_available:
                governed_ids = self._governed_synthesis_ids(visible_ids)
                visible_ids = tuple(
                    memory_id for memory_id in visible_ids if memory_id not in governed_ids
                )
            result = (
                tuple(
                    (memory_id, copy.deepcopy(self._memories[memory_id]))
                    for memory_id in visible_ids
                    if memory_id in self._memories
                )
                if include_rows
                else visible_ids
            )

            if conn is not None and version_available:
                try:
                    if conn.in_transaction or read_memory_version(conn) != stable_version:
                        return ()
                except Exception:
                    return ()
            return result

    def _public_memory_snapshot(
        self,
        ids,
        *,
        allow_review: bool = False,
    ) -> tuple[tuple[str, dict[str, Any]], ...]:
        """Admit and copy public rows against one stable canonical version."""
        return self._stable_public_memory_read(
            ids,
            allow_review=allow_review,
            include_rows=True,
        )

    def _stable_public_memory_ids(
        self,
        ids,
        *,
        allow_review: bool = False,
    ) -> tuple[str, ...]:
        """Return public ids only when their canonical version stays stable."""
        return self._stable_public_memory_read(
            ids,
            allow_review=allow_review,
            include_rows=False,
        )

    def _public_graph_snapshot(self) -> tuple[dict[str, dict], list[dict]]:
        """Copy and gate the graph before any public traversal or serialization."""
        with self._write_lock:
            conn = getattr(self._sqlite, "_conn", None)
            stable_version = None
            if self._sqlite is not None and conn is None and hasattr(self._sqlite, "_conn"):
                # Lightweight graph-only test adapters have no canonical
                # connection. A real SQLite backend always exposes _conn,
                # so only the latter must fail closed.
                return {}, []
            if conn is not None:
                try:
                    from plastic_promise.core.synthesis_retrieval import read_memory_version

                    if conn.in_transaction:
                        return {}, []
                    stable_version = read_memory_version(conn)
                except Exception:
                    return {}, []
            graph_ids = set(self._graph_nodes)
            extra_types: dict[str, object] = {}
            for node_id, node in self._graph_nodes.items():
                metadata = node.get("metadata", {})
                if node.get("source_kind") == "synthesis" or (
                    isinstance(metadata, dict) and metadata.get("governed") is True
                ):
                    extra_types[node_id] = "synthesis"
            for edge in self._graph_edges:
                source = str(edge.get("from") or "")
                target = str(edge.get("to") or "")
                graph_ids.update((source, target))
                if edge.get("source_kind") == "synthesis":
                    extra_types[source] = "synthesis"
            visible = set(self._public_memory_ids(graph_ids, extra_types=extra_types))
            public_nodes = {
                node_id: copy.deepcopy(node)
                for node_id, node in self._graph_nodes.items()
                if node_id in visible
            }
            public_edges = [
                copy.deepcopy(edge)
                for edge in self._graph_edges
                if str(edge.get("from") or "") in visible and str(edge.get("to") or "") in visible
            ]
            governed = self._governed_synthesis_ids(public_nodes)
            for node_id in governed:
                public_nodes[node_id]["description"] = ""
            if stable_version is not None:
                try:
                    if conn.in_transaction or read_memory_version(conn) != stable_version:
                        return {}, []
                except Exception:
                    return {}, []
        return public_nodes, public_edges

    def create_ordinary_if_absent(
        self,
        record: Mapping[str, Any] | MemoryRecord,
    ) -> str:
        """Create one ordinary memory or accept an identical canonical replay."""
        with self._write_lock:
            if isinstance(record, Mapping):
                memory_id = str(record.get("id", f"mem_{len(self._memories)}"))
                requested_type = str(record.get("memory_type") or "").strip().casefold()
            else:
                memory_id = str(getattr(record, "id", "") or f"mem_{len(self._memories):08d}")
                requested_type = str(getattr(record, "memory_type", "") or "").strip().casefold()
            if requested_type == "synthesis":
                from plastic_promise.core.synthesis import SynthesisConflict

                raise SynthesisConflict("synthesis_requires_governed_store")
            if self._synthesis_memory_reserved(memory_id):
                from plastic_promise.core.synthesis import SynthesisConflict

                raise SynthesisConflict("synthesis_memory_reserved")
            try:
                if isinstance(record, Mapping):
                    return self._register_memory_locked(dict(record))
                return self._store_memory_locked(record)
            except OrdinaryMemoryConflict as exc:
                if str(exc) != "ordinary_memory_reserved":
                    raise
                from plastic_promise.core.synthesis import SynthesisConflict

                raise SynthesisConflict("synthesis_memory_reserved") from exc

    def register_memory(self, record: dict[str, Any]) -> str:
        return self.create_ordinary_if_absent(record)

    def _register_memory_locked(self, record: dict[str, Any]) -> str:
        mid = record.get("id", f"mem_{len(self._memories)}")
        metadata_json = record.get("metadata_json", {})
        metadata_json = metadata_json if isinstance(metadata_json, dict) else {}
        get_canonical = getattr(self._sqlite, "get", None)
        existing = get_canonical(mid) if callable(get_canonical) else self._memories.get(mid)
        existing = existing if isinstance(existing, dict) else {}
        created_at = (
            record.get("created_at")
            or existing.get("created_at")
            or datetime.datetime.now().isoformat()
        )

        def index_field(name: str) -> str:
            return str(record.get(name) or metadata_json.get(name) or "")

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
            "created_at": created_at,
            "decay_multiplier": record.get("decay_multiplier", 1.0),
            "effective_half_life": record.get("effective_half_life", 3.0),
            "last_accessed": record.get("last_accessed")
            or existing.get("last_accessed")
            or created_at,
            "project_id": record.get("project_id", "project:legacy-global"),
            "visibility": record.get("visibility", "project"),
            "source_class": record.get("source_class", "experience"),
            "created_by_call_id": record.get("created_by_call_id", ""),
            "origin_kind": record.get("origin_kind", ""),
            "origin_uri": record.get("origin_uri", ""),
            "origin_ref": record.get("origin_ref", ""),
            "origin_hash": record.get("origin_hash", ""),
            "parent_memory_ids": record.get("parent_memory_ids", []),
            "metadata_json": metadata_json,
            "raw_content": index_field("raw_content"),
            "l0_abstract": index_field("l0_abstract"),
            "l1_summary": index_field("l1_summary"),
            "l2_content": index_field("l2_content"),
            "embedding_text": index_field("embedding_text"),
            "embedding_hash": index_field("embedding_hash"),
            "search_text": index_field("search_text"),
        }
        canonical, created = self._create_ordinary_memory(mid, data)
        if self._manual_batch_state is not None:
            self._manual_batch_state.setdefault("pending_memory_deletes", set()).discard(mid)
            if created:
                self._manual_batch_state.setdefault("pending_memory_updates", {})[mid] = (
                    copy.deepcopy(canonical)
                )
        else:
            self._memories[mid] = copy.deepcopy(canonical)
        # P0: Auto-create principle↔memory graph edges for new memories
        if created:
            self._build_principle_edges_for_memory(mid, canonical)
        return mid

    def register_memories(self, records: list[dict[str, Any]]) -> list[str]:
        return [self.create_ordinary_if_absent(record) for record in records]

    @property
    def memory_count(self) -> int:
        return len(self._stable_public_memory_ids(self._memories))

    # ========== 记忆只读访问 (Rust Core Boundary: 4 read-access methods) ==========

    def memory_exists(self, mid: str) -> bool:
        """Check if a memory id exists in the pool."""
        return mid in self._stable_public_memory_ids([mid]) and mid in self._memories

    def get_memory_dict(self, mid: str) -> dict | None:
        """Get a memory record as a dict (deep copy).

        Returns a copy so callers can read fields freely,
        but mutations have NO effect on engine state.
        Use update_memory_fields() to modify data.
        """
        snapshot = self._public_memory_snapshot([mid])
        return snapshot[0][1] if snapshot else None

    def get_memory_dict_for_review(self, mid: str) -> dict | None:
        """Return a canonically valid draft/contested synthesis for explicit review."""
        snapshot = self._public_memory_snapshot([mid], allow_review=True)
        return snapshot[0][1] if snapshot else None

    def _get_memory_dict_unchecked(self, mid: str) -> dict | None:
        """Copy raw runtime state for lifecycle internals; never expose as a tool API."""
        mem = self._memories.get(mid)
        if mem is None:
            return None
        return copy.deepcopy(mem)

    def memory_ids(self) -> list[str]:
        """Return all memory IDs in the pool."""
        return list(self._stable_public_memory_ids(self._memories))

    def _refresh_canonical_cache_if_changed(self, force: bool = False) -> bool:
        """Replace memory and graph caches from one committed SQLite snapshot."""
        sqlite = self._sqlite
        if sqlite is None:
            self.canonical_sync_ok = True
            return False
        conn = sqlite._conn
        with self._write_lock:
            try:
                transaction_open = conn.in_transaction
            except Exception:
                self.canonical_sync_ok = False
                return False
            if transaction_open:
                if force:
                    self.canonical_sync_ok = False
                return False
            try:
                from plastic_promise.core.synthesis_retrieval import read_memory_version

                conn.execute("BEGIN")
                version = read_memory_version(conn)
                if not force and self.canonical_sync_ok and version == self._loaded_memory_version:
                    conn.rollback()
                    self.canonical_sync_ok = True
                    return False
                memories = dict(sqlite.iter_all())
                graph_nodes = dict(sqlite.iter_graph_nodes())
                graph_edges = list(sqlite.iter_graph_edges())
                conn.rollback()
            except Exception as exc:
                if conn.in_transaction:
                    conn.rollback()
                self.canonical_sync_ok = False
                logger.warning("Canonical cache refresh failed: %s", exc)
                if self._memories or self._graph_nodes or self._graph_edges:
                    return False
                try:
                    conn.execute("BEGIN")
                    memories = dict(sqlite.iter_all())
                    graph_nodes = dict(sqlite.iter_graph_nodes())
                    graph_edges = list(sqlite.iter_graph_edges())
                    conn.rollback()
                except Exception as snapshot_exc:
                    if conn.in_transaction:
                        conn.rollback()
                    logger.warning(
                        "Degraded canonical snapshot load failed: %s",
                        snapshot_exc,
                    )
                    return False
                if not self._install_canonical_snapshot(
                    memories,
                    graph_nodes,
                    graph_edges,
                ):
                    return False
                self._loaded_memory_version = None
                return False
            if not self._install_canonical_snapshot(memories, graph_nodes, graph_edges):
                self.canonical_sync_ok = False
                return False
            self._loaded_memory_version = version
            self.canonical_sync_ok = True
            return True

    def _install_canonical_snapshot(
        self,
        memories: dict[str, dict[str, Any]],
        graph_nodes: dict[str, dict[str, Any]],
        graph_edges: list[dict[str, Any]],
    ) -> bool:
        previous_state = (
            self._memories,
            self._graph_nodes,
            self._graph_edges,
            self._edge_feedback_base_weights,
        )
        try:
            self._memories = memories
            self._graph_nodes = graph_nodes
            self._graph_edges = graph_edges
            self._edge_feedback_base_weights = {}
            self._rebuild_graph_from_memories()
            self._reapply_canonical_edge_feedback()
        except Exception as exc:
            (
                self._memories,
                self._graph_nodes,
                self._graph_edges,
                self._edge_feedback_base_weights,
            ) = previous_state
            self.canonical_sync_ok = False
            logger.warning("Canonical graph overlay rebuild failed: %s", exc)
            return False
        return True

    def get_memories_batch(self, mids: list[str]) -> list[dict]:
        """Get multiple memory records by id. Missing ids are skipped."""
        return [memory for _memory_id, memory in self._public_memory_snapshot(mids)]

    def set_current_time(self, iso_timestamp: str):
        self._current_time = iso_timestamp

    # ========== P0: 原则↔记忆图谱边 (深层语法) ==========

    @staticmethod
    def _graph_edge_identity(edge: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(edge.get("from") or ""),
            str(edge.get("to") or ""),
            str(edge.get("relation") or ""),
        )

    def _build_principle_edges_for_memory(
        self,
        memory_id: str,
        memory_data: dict,
        *,
        edge_identities: set[tuple[str, str, str]] | None = None,
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
            PRINCIPLE_EDGE_BASE_WEIGHT,
            PRINCIPLE_EDGE_MIN_KEYWORD_HITS,
            PRINCIPLE_EDGE_SCALE_WEIGHT,
        )

        content = memory_data.get("content", "")
        if not content:
            return 0
        if edge_identities is None:
            edge_identities = {self._graph_edge_identity(edge) for edge in self._graph_edges}

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
            edge_g_identity = self._graph_edge_identity(edge_g)
            if edge_g_identity not in edge_identities:
                self._graph_edges.append(edge_g)
                edge_identities.add(edge_g_identity)
                edges_created += 1

            # Edge 2: memory → principle (embodies)
            edge_e = {
                "from": memory_node,
                "to": principle_node,
                "relation": "embodies",
                "weight": weight,
            }
            edge_e_identity = self._graph_edge_identity(edge_e)
            if edge_e_identity not in edge_identities:
                self._graph_edges.append(edge_e)
                edge_identities.add(edge_e_identity)
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
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ========== 图管理 ==========

    def load_graph(self, graph_data: dict[str, Any]):
        with self._write_lock:
            if self._sqlite is not None:
                if getattr(self._sqlite, "_conn", None) is not None:
                    self._refresh_canonical_cache_if_changed(force=True)
                return
            self._graph_nodes = graph_data.get("nodes", {})
            self._graph_edges = graph_data.get("edges", [])
            self._edge_feedback_base_weights = {}

    def get_graph(self) -> GraphInfo:
        nodes, edges = self._public_graph_snapshot()
        return GraphInfo(nodes, edges)

    # ========== Graph CRUD (6 methods, added by Task 4) ==========

    def add_graph_edge(
        self,
        source: str,
        target: str,
        relation: str = "references",
        weight: float = 0.5,
        metadata: dict[str, Any] | None = None,
        source_kind: str = "",
        evidence_id: str = "",
    ) -> bool:
        with self._write_lock:
            if (
                str(source_kind or "").strip().casefold() == "synthesis"
                or self._synthesis_memory_reserved(source)
                or self._synthesis_memory_reserved(target)
            ):
                return False
            return self._add_graph_edge_locked(
                source,
                target,
                relation=relation,
                weight=weight,
                metadata=metadata,
                source_kind=source_kind,
                evidence_id=evidence_id,
            )

    def _add_graph_edge_locked(
        self,
        source: str,
        target: str,
        relation: str = "references",
        weight: float = 0.5,
        metadata: dict[str, Any] | None = None,
        source_kind: str = "",
        evidence_id: str = "",
    ) -> bool:
        """Add an edge to the entity graph. No-op if duplicate exists.

        Returns True if the edge was added, False if it already existed.
        """
        edge = graph_edge(
            source,
            target,
            relation,
            weight,
            metadata=metadata,
            source_kind=source_kind,
            evidence_id=evidence_id,
        )
        if not self._has_graph_edge_unchecked(edge):
            if self._sqlite:
                guarded = getattr(self._sqlite, "upsert_graph_edge_ordinary", None)
                if callable(guarded):
                    if not guarded(edge):
                        return False
                else:
                    if hasattr(self._sqlite, "_conn"):
                        return False
                    legacy = getattr(self._sqlite, "upsert_graph_edge", None)
                    if not callable(legacy) or legacy(edge) is False:
                        return False
            self._graph_edges.append(edge)
            return True
        return False

    def remove_graph_edge(self, source: str, target: str, relation: str = None) -> int:
        """Remove matching edges. Returns number of edges removed."""
        with self._write_lock:
            if self._synthesis_memory_reserved(source) or self._synthesis_memory_reserved(target):
                return 0
            before = len(self._graph_edges)
            removed = []
            remaining = []
            for edge in self._graph_edges:
                matches = (
                    edge.get("from") == source
                    and edge.get("to") == target
                    and (relation is None or edge.get("relation") == relation)
                )
                (removed if matches else remaining).append(edge)
            if self._sqlite:
                guarded = getattr(self._sqlite, "delete_graph_edges_ordinary", None)
                if callable(guarded):
                    if guarded(source, target, relation) <= 0:
                        return 0
                else:
                    if hasattr(self._sqlite, "_conn"):
                        return 0
                    legacy = getattr(self._sqlite, "delete_graph_edges", None)
                    if not callable(legacy) or legacy(source, target, relation) <= 0:
                        return 0
            for edge in removed:
                self._edge_feedback_base_weights.pop(self._edge_feedback_key(edge), None)
            self._graph_edges[:] = remaining
            return before - len(remaining)

    def has_graph_edge(self, edge_dict: dict) -> bool:
        """Check if an exact edge dict exists in the graph."""
        _, edges = self._public_graph_snapshot()
        return self._has_graph_edge_unchecked(edge_dict, edges=edges)

    def _has_graph_edge_unchecked(self, edge_dict: dict, *, edges=None) -> bool:
        """Check raw graph state after the caller has established admission."""
        candidate_edges = self._graph_edges if edges is None else edges
        if edge_dict in candidate_edges:
            return True
        source = edge_dict.get("from")
        target = edge_dict.get("to")
        relation = edge_dict.get("relation")
        return any(
            edge.get("from") == source
            and edge.get("to") == target
            and edge.get("relation") == relation
            for edge in candidate_edges
        )

    def get_graph_node(self, node_id: str) -> dict | None:
        """Get a graph node by id. Returns a deep copy."""
        nodes, _ = self._public_graph_snapshot()
        node = nodes.get(node_id)
        if node is None:
            return None
        return copy.deepcopy(node)

    def list_graph_nodes(self, node_type: str = None) -> list[dict]:
        """List graph nodes, optionally filtered by type field."""
        nodes, _ = self._public_graph_snapshot()
        results = []
        for nid, node in nodes.items():
            if node_type and node.get("type") != node_type:
                continue
            node_copy = copy.deepcopy(node)
            node_copy["id"] = nid
            results.append(node_copy)
        return results

    def list_graph_edges(self, relation: str = None) -> list[dict]:
        """List graph edges, optionally filtered by relation."""
        _, edges = self._public_graph_snapshot()
        if relation is None:
            return edges
        return [e for e in edges if e.get("relation") == relation]

    # ========== Memory CRUD (Python fallback) ==========

    def store_memory(self, record: MemoryRecord) -> str:
        """Store a MemoryRecord into the in-memory pool.

        Returns the memory id (generates one if record.id is empty).
        """
        return self.create_ordinary_if_absent(record)

    def _store_memory_locked(self, record: MemoryRecord) -> str:
        mid = record.id or f"mem_{len(self._memories):08d}"
        record.id = mid
        meta = getattr(record, "metadata", {}) or {}
        metadata_json = meta.get("metadata_json", {})
        metadata_json = metadata_json if isinstance(metadata_json, dict) else {}
        get_canonical = getattr(self._sqlite, "get", None)
        existing = get_canonical(mid) if callable(get_canonical) else self._memories.get(mid)
        existing = existing if isinstance(existing, dict) else {}
        created_at = (
            record.created_at or existing.get("created_at") or datetime.datetime.now().isoformat()
        )

        def index_field(name: str) -> str:
            return str(metadata_json.get(name) or "")

        data = {
            "id": mid,
            "content": record.content,
            "memory_type": record.memory_type,
            "source": record.source,
            "scope": record.scope,
            "category": record.category,
            "importance": record.importance,
            "entity_ids": record.entity_ids,
            "created_at": created_at,
            "access_count": record.access_count,
            "worth_success": record.worth_success,
            "worth_failure": record.worth_failure,
            "owner": record.owner,
            "tier": record.tier,
            "tags": record.tags,
            "domain": record.domain,
            "decay_multiplier": getattr(record, "decay_multiplier", 1.0),
            "effective_half_life": getattr(record, "effective_half_life", 3.0),
            "last_accessed": (
                getattr(record, "last_accessed", "") or existing.get("last_accessed") or created_at
            ),
            "project_id": meta.get("project_id", "project:legacy-global"),
            "visibility": meta.get("visibility", "project"),
            "source_class": meta.get("source_class", "experience"),
            "created_by_call_id": meta.get("created_by_call_id", ""),
            "origin_kind": meta.get("origin_kind", ""),
            "origin_uri": meta.get("origin_uri", ""),
            "origin_ref": meta.get("origin_ref", ""),
            "origin_hash": meta.get("origin_hash", ""),
            "parent_memory_ids": meta.get("parent_memory_ids", []),
            "metadata_json": metadata_json,
            "raw_content": index_field("raw_content"),
            "l0_abstract": index_field("l0_abstract"),
            "l1_summary": index_field("l1_summary"),
            "l2_content": index_field("l2_content"),
            "embedding_text": index_field("embedding_text"),
            "embedding_hash": index_field("embedding_hash"),
            "search_text": index_field("search_text"),
        }
        canonical, created = self._create_ordinary_memory(mid, data)
        if self._manual_batch_state is not None:
            self._manual_batch_state.setdefault("pending_memory_deletes", set()).discard(mid)
            if created:
                self._manual_batch_state.setdefault("pending_memory_updates", {})[mid] = (
                    copy.deepcopy(canonical)
                )
        else:
            self._memories[mid] = copy.deepcopy(canonical)
        # P0: Auto-create principle↔memory graph edges
        if created:
            self._build_principle_edges_for_memory(mid, canonical)
        return mid

    def get_memory(self, memory_id: str):
        """Retrieve a single MemoryRecord by id. Returns None if not found."""
        snapshot = self._public_memory_snapshot([memory_id])
        if not snapshot:
            return None
        return self._memory_record_from_dict(snapshot[0][1])

    def _get_memory_unchecked(self, memory_id: str):
        """Hydrate raw runtime state after the caller has established admission."""
        mem = self._memories.get(memory_id)
        if mem is None:
            return None
        return self._memory_record_from_dict(mem)

    @staticmethod
    def _memory_record_from_dict(mem: dict[str, Any]):
        """Build one public record from an already copied memory payload."""
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
        """Update one ordinary source through its canonical mutation owner."""
        if content is not None:
            if importance is not None or category is not None:
                return False
            return self._mutate_ordinary_source_internal(
                memory_id,
                operation="replace_content",
                content=content,
                reason="context_engine:update_memory",
                action="update",
            )
        fields = {
            key: value
            for key, value in {"importance": importance, "category": category}.items()
            if value is not None
        }
        if not fields:
            return False
        return self.update_memory_fields(memory_id, **fields)

    def mutate_ordinary_source(
        self,
        memory_id: str,
        *,
        operation: str,
        content: str | None = None,
        reason: str,
        actor: str,
        call_id: str,
        expected_project_id: str | None = None,
        expected_content_hash: str | None = None,
        expected_source_snapshot: Mapping[str, Any] | None = None,
        expected_peer_snapshots: Mapping[str, Mapping[str, Any]] | None = None,
        peer_metadata_replacements: Mapping[str, Mapping[str, Any]] | None = None,
        require_source_available: bool = False,
        metadata_replacements: Mapping[str, int | float] | None = None,
        policy_replacements: Mapping[str, Any] | None = None,
    ):
        """Apply one coordinated ordinary-source content or lifecycle change.

        Existing source rows are never changed through whole-record storage.
        The coordinator owns the canonical transaction, dependent synthesis
        invalidation, durable repair jobs, and post-commit cache publication.
        """
        from plastic_promise.core.ordinary_memory_mutation import (
            OrdinaryMemoryMutationCoordinator,
            OrdinaryMemoryMutationError,
        )

        normalized_operation = str(operation or "").strip().casefold()
        coordinator = OrdinaryMemoryMutationCoordinator(self)
        if normalized_operation == "replace_content":
            if content is None:
                raise OrdinaryMemoryMutationError("ordinary_source_content_required")
            return coordinator.replace_content(
                memory_id,
                content=content,
                reason=reason,
                actor=actor,
                call_id=call_id,
                expected_project_id=expected_project_id,
                expected_content_hash=expected_content_hash,
                expected_source_snapshot=expected_source_snapshot,
                expected_peer_snapshots=expected_peer_snapshots,
                peer_metadata_replacements=peer_metadata_replacements,
                require_source_available=require_source_available,
                metadata_replacements=metadata_replacements,
                policy_replacements=policy_replacements,
            )
        if normalized_operation in {"wrong", "deprecated", "forgotten"}:
            if content is not None:
                raise OrdinaryMemoryMutationError("ordinary_source_content_not_allowed")
            if metadata_replacements is not None:
                raise OrdinaryMemoryMutationError("ordinary_source_metadata_replacements_invalid")
            if policy_replacements is not None:
                raise OrdinaryMemoryMutationError("ordinary_source_policy_replacements_invalid")
            return coordinator.mark_unavailable(
                memory_id,
                state=normalized_operation,
                reason=reason,
                actor=actor,
                call_id=call_id,
                expected_project_id=expected_project_id,
                expected_content_hash=expected_content_hash,
                expected_source_snapshot=expected_source_snapshot,
                expected_peer_snapshots=expected_peer_snapshots,
                peer_metadata_replacements=peer_metadata_replacements,
                require_source_available=require_source_available,
            )
        raise OrdinaryMemoryMutationError("ordinary_source_operation_invalid")

    def _mutate_ordinary_source_internal(
        self,
        memory_id: str,
        *,
        operation: str,
        content: str | None = None,
        reason: str,
        action: str,
    ) -> bool:
        """Run a compatibility mutation with process-owned audit evidence."""
        try:
            if self._synthesis_memory_reserved(memory_id):
                return False
        except Exception:
            return False
        preconditions = self._ordinary_source_mutation_preconditions(memory_id)
        if preconditions is None:
            return False
        call_id = f"internal:context_engine:{action}:{uuid.uuid4().hex}"
        try:
            self.mutate_ordinary_source(
                memory_id,
                operation=operation,
                content=content,
                reason=reason,
                actor="context_engine",
                call_id=call_id,
                **preconditions,
            )
        except Exception:
            return False
        return True

    def _ordinary_source_mutation_preconditions(
        self,
        memory_id: str,
    ) -> dict[str, Any] | None:
        """Capture one admitted canonical row for an internal mutation CAS."""
        try:
            self._refresh_canonical_cache_if_changed()
            canonical = self.get_memory_dict_for_review(memory_id)
        except Exception:
            return None
        if not isinstance(canonical, dict):
            return None
        project_id = str(canonical.get("project_id") or "").strip()
        tags = canonical.get("tags")
        metadata = canonical.get("metadata_json")
        if (
            not project_id
            or not isinstance(tags, (list, tuple))
            or not isinstance(metadata, Mapping)
        ):
            return None
        from plastic_promise.core.synthesis import synthesis_content_hash

        return {
            "expected_project_id": project_id,
            "expected_content_hash": synthesis_content_hash(canonical.get("content")),
            "expected_source_snapshot": {
                "category": canonical.get("category"),
                "metadata_json": copy.deepcopy(dict(metadata)),
                "tags": list(tags),
                "worth_failure": canonical.get("worth_failure"),
                "worth_success": canonical.get("worth_success"),
            },
            "require_source_available": True,
        }

    def update_memory_fields(self, mid: str, **fields) -> bool:
        """Patch allowed ordinary-memory metadata fields.

        Content is coordinator-owned and must be the only requested field.
        Every admitted metadata update is delegated to the field-scoped
        canonical patch primitive.
        """
        if "content" in fields:
            if len(fields) != 1:
                return False
            return self._mutate_ordinary_source_internal(
                mid,
                operation="replace_content",
                content=fields["content"],
                reason="context_engine:update_memory_fields",
                action="update",
            )
        with self._write_lock:
            if not fields or self._ordinary_write_requests_synthesis(fields):
                return False
            if self._synthesis_memory_reserved(mid):
                return False
            try:
                self.patch_ordinary_memory(mid, replacements=fields)
            except OrdinaryMemoryConflict:
                return False
            return True

    def increment_field(self, mid: str, field: str, delta: float = 1) -> bool:
        """Atomically increment a numeric field.

        Convenience wrapper around update_memory_fields for the common
        pattern: engine._memories[mid]["access_count"] += 1

        Note: Uses RLock so increment_field calling update_memory_fields
        within the same lock is safe.
        """
        with self._write_lock:
            if self._synthesis_memory_reserved(mid):
                return False
            try:
                self.patch_ordinary_memory(
                    mid,
                    increments={field: delta},
                    bump_memory_version=True,
                )
            except OrdinaryMemoryConflict:
                return False
            return True

    # ========== Batch Updates with SAVEPOINT atomicity (Task 5) ==========

    @staticmethod
    def _ordinary_write_requests_synthesis(fields: dict[str, Any]) -> bool:
        return str(fields.get("memory_type") or "").strip().casefold() == "synthesis"

    def _maybe_adjust_tier(self, mid: str, candidate: dict[str, Any]) -> dict[str, Any]:
        """Persist a tier promotion before returning a candidate for publication."""
        if os.environ.get("PP_TIER_AUTO_PROMOTE", "1") != "1":
            return candidate
        access = candidate.get("access_count", 0)
        tier = candidate.get("tier", "L1")
        new_tier = tier
        if tier == "L1" and access >= 5:
            new_tier = "L2"
        elif tier == "L2" and access >= 20:
            new_tier = "L3"
        if new_tier == tier:
            return candidate
        try:
            current = self._memories.get(mid, {})
            increments = {
                field: candidate.get(field, 0) - current.get(field, 0)
                for field in ("access_count", "worth_success", "worth_failure")
                if candidate.get(field, 0) != current.get(field, 0)
            }
            return self.patch_ordinary_memory(
                mid,
                replacements={"tier": new_tier},
                increments=increments or None,
            )
        except OrdinaryMemoryConflict:
            return candidate

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

            staged: dict[str, dict[str, Any]] = {}
            for upd in updates:
                upd_copy = dict(upd)
                mid = upd_copy.pop("id", "")
                if not mid or not upd_copy or self._ordinary_write_requests_synthesis(upd_copy):
                    continue
                if self._synthesis_memory_reserved(mid):
                    continue
                if "content" in upd_copy:
                    continue
                if mid not in self._memories:
                    canonical_get = getattr(self._sqlite, "get", None)
                    if not callable(canonical_get) or canonical_get(mid) is None:
                        continue
                staged.setdefault(mid, {}).update(upd_copy)

            persisted: dict[str, dict[str, Any]] = {}
            caller_transaction_open = bool(self._sqlite._conn.in_transaction)
            with self._sqlite.batch():
                for mid, fields in staged.items():
                    persisted[mid] = self.patch_ordinary_memory(
                        mid,
                        replacements=fields,
                    )
            if not caller_transaction_open and not self._sqlite._conn.in_transaction:
                for mid, candidate in persisted.items():
                    self._memories[mid] = candidate
            return len(persisted)

    def _batch_update_in_memory(self, updates: list[dict]) -> int:
        """Fallback batch_update when SQLite is unavailable."""
        staged: dict[str, dict[str, Any]] = {}
        count = 0
        for upd in updates:
            upd_copy = dict(upd)
            mid = upd_copy.pop("id")
            if self._ordinary_write_requests_synthesis(upd_copy):
                continue
            if self._synthesis_memory_reserved(mid):
                continue
            current = staged.get(mid, self._memories.get(mid))
            if current is None:
                continue
            candidate = dict(current)
            candidate.update(upd_copy)
            staged[mid] = candidate
            count += 1
        self._memories.update(staged)
        return count

    def begin_batch(self):
        """Begin a manual batch and retain a rollback snapshot of runtime state."""
        self._write_lock.acquire()
        try:
            if self._manual_batch_state is not None:
                raise RuntimeError("manual_batch_already_active")
            if self._sqlite and self._sqlite._conn.in_transaction:
                raise RuntimeError("manual_batch_requires_clean_transaction")
            state = {
                "memories": copy.deepcopy(self._memories),
                "graph_nodes": copy.deepcopy(self._graph_nodes),
                "graph_edges": copy.deepcopy(self._graph_edges),
                "edge_feedback_base_weights": dict(self._edge_feedback_base_weights),
                "loaded_memory_version": self._loaded_memory_version,
                "canonical_sync_ok": self.canonical_sync_ok,
                "batch_context": None,
                "pending_memory_updates": {},
                "pending_memory_deletes": set(),
            }
            if self._sqlite:
                batch_context = self._sqlite.batch()
                batch_context.__enter__()
                state["batch_context"] = batch_context
            self._manual_batch_state = state
        except BaseException:
            self._write_lock.release()
            raise

    def commit_batch(self):
        """Commit a manual batch, restoring runtime if durability fails."""
        state = self._manual_batch_state
        if state is None:
            raise RuntimeError("manual_batch_not_active")
        try:
            batch_context = state.get("batch_context")
            if batch_context is not None:
                batch_context.__exit__(None, None, None)
            pending_deletes = set(state.get("pending_memory_deletes", set()))
            pending_updates = dict(state.get("pending_memory_updates", {}))
            for memory_id in pending_deletes:
                pending_updates.pop(memory_id, None)
            for memory_id, canonical in pending_updates.items():
                self._memories[memory_id] = copy.deepcopy(canonical)
            for memory_id in pending_deletes:
                self._memories.pop(memory_id, None)
        except BaseException:
            self._restore_manual_batch_state(state)
            raise
        finally:
            self._manual_batch_state = None
            self._write_lock.release()

    def rollback_batch(self):
        """Rollback a manual batch and restore the matching runtime snapshot."""
        state = self._manual_batch_state
        if state is None:
            raise RuntimeError("manual_batch_not_active")
        try:
            batch_context = state.get("batch_context")
            if batch_context is not None:
                batch_context.__exit__(RuntimeError, RuntimeError("manual_batch_rollback"), None)
        finally:
            self._restore_manual_batch_state(state)
            self._manual_batch_state = None
            self._write_lock.release()

    def _restore_manual_batch_state(self, state: dict[str, Any]) -> None:
        self._memories = state["memories"]
        self._graph_nodes = state["graph_nodes"]
        self._graph_edges = state["graph_edges"]
        self._edge_feedback_base_weights = state["edge_feedback_base_weights"]
        self._loaded_memory_version = state["loaded_memory_version"]
        self.canonical_sync_ok = state["canonical_sync_ok"]

    def delete_memory(self, memory_id: str) -> bool:
        """Tombstone a committed source or cancel a pending in-batch create."""
        with self._write_lock:
            if self._synthesis_memory_reserved(memory_id):
                return False
            state = self._manual_batch_state
            if state is not None:
                pending_updates = state.get("pending_memory_updates", {})
                existed_before_batch = memory_id in state.get("memories", {})
                if memory_id not in pending_updates or existed_before_batch:
                    return False
                if not self._delete_ordinary_memory(memory_id):
                    return False
                pending_updates.pop(memory_id, None)
                state.setdefault("pending_memory_deletes", set()).add(memory_id)
                return True

        return self._mutate_ordinary_source_internal(
            memory_id,
            operation="forgotten",
            reason="context_engine:delete_memory",
            action="delete",
        )

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
        visible_ids = self._stable_public_memory_ids(self._memories)
        page_size = 200
        for page_start in range(0, len(visible_ids), page_size):
            page_ids = visible_ids[page_start : page_start + page_size]
            for _mid, mem in self._public_memory_snapshot(page_ids):
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
                results.append(self._memory_record_from_dict(mem))
                if len(results) >= limit:
                    return results
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
        self._reload_from_sqlite()
        all_ids = self._stable_public_memory_ids(self._memories)
        offset = 0
        while offset < len(all_ids):
            page_ids = all_ids[offset : offset + page_size]
            for _mid, mem in self._public_memory_snapshot(page_ids):
                if scope and mem.get("scope", "global") != scope:
                    continue
                yield mem
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

        snapshot = self._public_memory_snapshot(self._memories)
        total = 0
        by_type: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        healthy = 0
        decaying = 0
        worth_sum = 0.0
        active_count = 0
        dormant_count = 0
        active_worth_sum = 0.0

        for _mid, mem in snapshot:
            if scope and mem.get("scope") != scope:
                continue
            total += 1
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
        debug: bool = False,
        project_id: str = "project:legacy-global",
        project_policy: str = "balanced",
        project_degraded: bool = False,
        retrieval_mode: str | None = None,
        fusion_policy: str | None = None,
    ) -> ContextPack:
        """Supply context for a task. Rust-accelerated when available.

        Consistency: Returns a snapshot of the memory pool at call time.
        Concurrent writes (batch_update, register_memory) may not be
        reflected — this is eventual consistency by design. Retrieval
        results are advisory, not transactional.

        IMPORTANT: _supply_python is the ORIGINAL independent Python
        implementation. It does NOT call back into supply() — no recursion.
        """
        self._refresh_canonical_cache_if_changed()
        self._ensure_heavy_init()

        # Generate embedding if not provided (backward compatibility)
        if task_vector is None:
            task_vector = self._embed(task_description)

        # Ensure vector is non-empty — if embedder fails (Ollama down, etc.),
        # use zero vector as fallback so downstream code never sees None
        if task_vector is None or len(task_vector) == 0:
            task_vector = [0.0] * 1024  # fallback: mxbai-embed-large dim

        retrieval_plan = plan_retrieval(
            task_type=task_type,
            scope=scope,
            project_policy=project_policy,
            retrieval_mode=retrieval_mode,
            has_vector=any(v != 0.0 for v in task_vector),
            has_graph=bool(self._graph_edges),
            has_fts=(
                self._ldb is not None
                and os.environ.get("PP_FTS_DISABLED", "") != "1"
                and os.environ.get("PP_FTS_FUSION", "1") == "1"
            ),
        )

        requested_policy = str(
            fusion_policy or os.environ.get("PP_RETRIEVAL_FUSION_POLICY", "legacy-auto")
        ).strip()
        fusion_config = load_fusion_config(requested_policy, retrieval_plan)

        # PP_FORCE_PYTHON_SUPPLY=1 bypasses Rust entirely.
        # PP_PREFER_RUST_SUPPLY=0 disables Rust primary; default is Rust-first
        # with automatic Python fallback if the extension is unavailable.
        prefer_rust = os.environ.get("PP_PREFER_RUST_SUPPLY", "1") == "1"
        force_python = os.environ.get("PP_FORCE_PYTHON_SUPPLY", "0") == "1"
        requested_runtime = "python" if force_python or not prefer_rust else "rust"

        def decision(runtime: str, reason: str) -> FusionDecision:
            return FusionDecision(
                requested_policy=requested_policy,
                effective_policy=requested_policy,
                requested_runtime=requested_runtime,
                effective_runtime=runtime,
                candidate_id=requested_policy if fusion_config is not None else "",
                capability_reason=reason,
            )

        policy_python_reason = ""
        if requested_policy == "max-v1":
            policy_python_reason = "policy_requires_python:max-v1"
        elif fusion_config is not None and "fts" in retrieval_plan.fusion_channels:
            policy_python_reason = "rust_capability_missing:fts"

        if force_python or not prefer_rust or policy_python_reason:
            reason = policy_python_reason or (
                "runtime_forced:python" if force_python else "runtime_preferred:python"
            )
            return self._supply_python(
                task_description,
                task_vector,
                task_type,
                scope,
                debug=debug,
                retrieval_plan=retrieval_plan,
                project_id=project_id,
                project_policy=project_policy,
                project_degraded=project_degraded,
                fusion_config=fusion_config,
                fusion_decision=decision("python", reason),
            )

        # Rust accelerator — enabled via PP_PREFER_RUST_SUPPLY=1.
        # Falls back to Python if Rust engine is unavailable or throws.
        if self._check_rust_health():
            try:
                pack = self._supply_rust(
                    task_description,
                    task_vector,
                    task_type,
                    scope,
                    project_id=project_id,
                    project_policy=project_policy,
                    project_degraded=project_degraded,
                    fusion_config=fusion_config,
                )
                pack.audit_metadata["retrieval_fusion"] = self._fusion_audit_metadata(
                    decision("rust", "rust_capability_satisfied"),
                    fusion_config,
                )
                self._enrich_pack_with_code_memory(
                    pack,
                    task_description,
                    retrieval_plan,
                )
                return self._finalize_supply_pack(
                    pack,
                    retrieval_plan,
                    task_type=task_type,
                    project_id=project_id,
                    project_policy=project_policy,
                )
            except _RustSynthesisFallback:
                pack = self._supply_python(
                    task_description,
                    task_vector,
                    task_type,
                    scope,
                    debug=debug,
                    retrieval_plan=retrieval_plan,
                    project_id=project_id,
                    project_policy=project_policy,
                    project_degraded=project_degraded,
                    fusion_config=fusion_config,
                    fusion_decision=decision("python", "admitted_governed_synthesis"),
                )
                pack.audit_metadata.setdefault(
                    "rust_fallback_reason", "admitted_governed_synthesis"
                )
                return pack
            except _RustFusionFallback as exc:
                pack = self._supply_python(
                    task_description,
                    task_vector,
                    task_type,
                    scope,
                    debug=debug,
                    retrieval_plan=retrieval_plan,
                    project_id=project_id,
                    project_policy=project_policy,
                    project_degraded=project_degraded,
                    fusion_config=fusion_config,
                    fusion_decision=decision("python", str(exc)),
                )
                pack.audit_metadata.setdefault("rust_fallback_reason", str(exc))
                return pack
            except Exception as e:
                logger.warning("Rust supply failed, falling back to Python: %s", e)
                with self._rust_lock:
                    self._rust_healthy = None
                    self._rust_engine_instance = None

        return self._supply_python(
            task_description,
            task_vector,
            task_type,
            scope,
            debug=debug,
            retrieval_plan=retrieval_plan,
            project_id=project_id,
            project_policy=project_policy,
            project_degraded=project_degraded,
            fusion_config=fusion_config,
            fusion_decision=decision("python", "rust_unavailable_or_failed"),
        )

    @staticmethod
    def _fusion_audit_metadata(
        decision: FusionDecision,
        config: FusionConfig | None,
    ) -> dict[str, Any]:
        metadata = asdict(decision)
        if config is None:
            metadata["algorithm"] = (
                "weighted-max-v1"
                if decision.effective_policy == "max-v1"
                else "legacy-route-dependent"
            )
            if decision.effective_policy == "legacy-auto" and decision.effective_runtime == "rust":
                metadata["compatibility"] = "unweighted-rrf-k60"
            return metadata
        metadata.update(
            {
                "algorithm": "weighted-rrf-v1",
                "k": config.k,
                "channels": list(config.channels),
                "weights": dict(config.weights),
                "windows": dict(config.windows),
                "config_hash": config.config_hash,
            }
        )
        return metadata

    @staticmethod
    def _raw_evidence_from_results(
        result_sets: list[list[tuple[str, float, str, str]]],
        limit: int,
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for result_set in result_sets:
            for item_id, score, content, source in result_set:
                key = (item_id, source)
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    {
                        "id": item_id,
                        "source": source,
                        "score": round(float(score), 6),
                        "content": str(content)[:300],
                    }
                )
        evidence.sort(key=lambda item: item["score"], reverse=True)
        return evidence[:limit]

    @staticmethod
    def _raw_evidence_from_items(items: list[ContextItem], limit: int) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "source": item.source,
                "score": round(float(item.relevance), 6),
                "content": item.content[:300],
            }
            for item in sorted(items, key=lambda item: item.relevance, reverse=True)[:limit]
        ]

    @staticmethod
    def _attach_retrieval_plan(
        pack: ContextPack,
        retrieval_plan: RetrievalPlan,
        raw_evidence: list[dict[str, Any]] | None = None,
    ) -> None:
        pack.audit_metadata = dict(getattr(pack, "audit_metadata", {}) or {})
        pack.audit_metadata["retrieval_plan"] = retrieval_plan.to_dict()
        pack.audit_metadata["mode"] = retrieval_plan.mode
        pack.audit_metadata["budget"] = dict(retrieval_plan.budget)
        pack.audit_metadata["raw_evidence"] = list(raw_evidence or [])

    @staticmethod
    def _synthesis_overfetch_factor() -> int:
        try:
            factor = int(os.environ.get("PP_SYNTHESIS_OVERFETCH_FACTOR", "2"))
        except (TypeError, ValueError):
            factor = 2
        return max(1, min(factor, 4))

    @staticmethod
    def _synthesis_source_fallback_allowed(
        conn: Any,
        memory_id: str,
        *,
        reasons: set[str],
        task_type: str,
        retrieval_mode: str,
    ) -> bool:
        normalized_reasons = {
            str(reason or "").strip() for reason in reasons if str(reason or "").strip()
        }
        if (
            conn is None
            or not normalized_reasons
            or normalized_reasons & _SYNTHESIS_FALLBACK_HARD_DENY_REASONS
        ):
            return False
        try:
            if conn.in_transaction:
                return False
            row = conn.execute(
                "SELECT status FROM synthesis_artifacts WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        except Exception:
            return False
        if row is None or len(row) != 1 or not isinstance(row[0], str):
            return False

        status = row[0].strip().casefold()
        normalized_task = str(task_type or "").strip().casefold()
        normalized_mode = str(retrieval_mode or "").strip().casefold()
        if status == "verified":
            return bool(normalized_reasons & _VERIFIED_SYNTHESIS_FALLBACK_REASONS)
        has_lifecycle_fallback_reason = bool(
            normalized_reasons & (_VERIFIED_SYNTHESIS_FALLBACK_REASONS | {"status_not_allowed"})
        )
        if status == "contested":
            return has_lifecycle_fallback_reason and (
                normalized_mode == "audit" or normalized_task in _SYNTHESIS_REVIEW_TASKS
            )
        if status == "stale":
            return has_lifecycle_fallback_reason and (
                normalized_mode == "audit" or normalized_task == "debugging"
            )
        return False

    @staticmethod
    def _candidate_budget(retrieval_plan: RetrievalPlan) -> int:
        layer_budget = sum(
            max(0, int(retrieval_plan.budget.get(layer, 0)))
            for layer in ("core", "related", "divergent")
        )
        return max(1, layer_budget) * ContextEngine._synthesis_overfetch_factor()

    @staticmethod
    def _call_with_optional_limit(callable_obj, *args, limit: int):
        try:
            return callable_obj(*args, limit=limit)
        except TypeError as exc:
            if "limit" not in str(exc):
                raise
            return callable_obj(*args)

    def _gate_memory_ids(self, ids: list[str]):
        """Apply the canonical synthesis evaluator without mutating lifecycle state."""
        from plastic_promise.core.synthesis_retrieval import (
            SynthesisGateResult,
            evaluate_public_memory_ids,
            read_memory_version,
        )

        ordered_ids = list(dict.fromkeys(str(item_id) for item_id in ids if item_id))
        noncanonical_prefixes = (
            "principle:",
            "code:",
            "mcp_tool:",
            "task_state:",
            "bilingual_synonym:",
        )
        recognized_noncanonical_ids = {
            item_id for item_id in ordered_ids if item_id.startswith(noncanonical_prefixes)
        }

        conn = getattr(getattr(self, "_sqlite", None), "_conn", None)
        if conn is None:
            if self._sqlite is not None and hasattr(self._sqlite, "_conn"):
                return SynthesisGateResult(
                    (),
                    tuple(ordered_ids),
                    tuple(
                        {"id": item_id, "reason": "canonical_gate_unavailable"}
                        for item_id in ordered_ids
                    ),
                )
            admitted: list[str] = []
            dropped: list[str] = []
            degradations: list[dict[str, str]] = []
            for item_id in ordered_ids:
                memory = self._memories.get(item_id)
                memory_type = str((memory or {}).get("memory_type", "")).strip().casefold()
                if memory is None and item_id in recognized_noncanonical_ids:
                    admitted.append(item_id)
                elif memory_type == "synthesis":
                    dropped.append(item_id)
                    degradations.append({"id": item_id, "reason": "canonical_gate_unavailable"})
                elif memory is not None and memory_type:
                    admitted.append(item_id)
                else:
                    dropped.append(item_id)
                    degradations.append({"id": item_id, "reason": "candidate_state_unavailable"})
            return SynthesisGateResult(tuple(admitted), tuple(dropped), tuple(degradations))

        try:
            current_memory_version = read_memory_version(conn)
        except Exception:
            current_memory_version = -1
        memory_version = current_memory_version
        if (
            self._loaded_memory_version is not None
            and current_memory_version != self._loaded_memory_version
        ):
            memory_version = self._loaded_memory_version
        if not self.canonical_sync_ok or conn.in_transaction:
            memory_version = -1

        result = evaluate_public_memory_ids(
            conn,
            ordered_ids,
            allow_review=False,
            memory_version=memory_version,
            memory_types={
                item_id: (
                    memory.get("memory_type")
                    if isinstance(memory := self._memories.get(item_id), Mapping)
                    else None
                )
                for item_id in ordered_ids
            },
        )
        degradation_by_id = {row["id"]: row["reason"] for row in result.degradations}
        stable_noncanonical_ids: set[str] = set()
        for item_id in recognized_noncanonical_ids:
            if degradation_by_id.get(item_id) != "candidate_missing":
                continue
            try:
                has_control = (
                    conn.execute(
                        "SELECT 1 FROM synthesis_artifacts WHERE memory_id = ?",
                        (item_id,),
                    ).fetchone()
                    is not None
                )
            except Exception:
                continue
            if not has_control:
                stable_noncanonical_ids.add(item_id)
        admitted_set = set(result.items) | stable_noncanonical_ids
        admitted = tuple(item_id for item_id in ordered_ids if item_id in admitted_set)
        dropped_ids = tuple(
            item_id for item_id in result.dropped_ids if item_id not in stable_noncanonical_ids
        )
        degradations = tuple(
            row for row in result.degradations if row["id"] not in stable_noncanonical_ids
        )
        return SynthesisGateResult(
            admitted,
            dropped_ids,
            degradations,
            tuple(
                memory_id
                for memory_id in result.admitted_synthesis_ids
                if memory_id in admitted_set
            ),
        )

    def _filter_synthesis_result_tuples(
        self,
        results: list[tuple[str, float, str, str]],
    ) -> tuple[list[tuple[str, float, str, str]], tuple[dict[str, str], ...]]:
        decision = self._gate_memory_ids([row[0] for row in results])
        admitted = set(decision.items)
        return [row for row in results if row[0] in admitted], decision.degradations

    def _hydrate_ranked_memory_ids(
        self,
        rows,
        *,
        retrieval_source: str,
    ) -> tuple[list[tuple[str, float, str, str]], tuple[dict[str, str], ...]]:
        """Admit derived-index ids before hydrating canonical display content."""
        decision = self._gate_memory_ids([str(row[0]) for row in rows])
        admitted = set(decision.items)
        results: list[tuple[str, float, str, str]] = []
        with self._write_lock:
            for row in rows:
                memory_id = str(row[0])
                if memory_id not in admitted:
                    continue
                memory = self._memories.get(memory_id)
                if not isinstance(memory, dict):
                    continue
                results.append(
                    (
                        memory_id,
                        float(row[1]),
                        str(memory.get("content") or "")[:300],
                        retrieval_source,
                    )
                )
        return results, decision.degradations

    def _project_item_allowed(
        self,
        item: Any,
        layer: str,
        *,
        project_id: str,
        project_policy: str,
    ) -> bool:
        item_id = str(getattr(item, "id", "") or "")
        metadata, state = resolve_project_metadata(self, item_id)
        is_noncanonical = item_id.startswith(NONCANONICAL_CONTEXT_PREFIXES)
        if state == "error":
            return False
        if state == "canonical_missing":
            return is_noncanonical
        if state == "runtime_missing":
            if is_noncanonical:
                return True
            item_metadata = getattr(item, "metadata", None)
            metadata = dict(item_metadata) if isinstance(item_metadata, dict) else {}
        if not isinstance(metadata, dict):
            return False

        item_project = str(metadata.get("project_id") or "project:legacy-global")
        visibility = str(metadata.get("visibility") or "project")
        source_class = str(metadata.get("source_class") or "experience")
        if source_class in {"telemetry", "prompt"} and layer in {"core", "related"}:
            return False
        if layer == "divergent" and project_policy != "strict":
            return visibility in {"shared", "global"} or item_project == project_id
        return item_project == project_id or visibility == "global"

    @staticmethod
    def _value_mentions_dropped(
        value: Any,
        dropped_ids: set[str],
        dropped_contents: tuple[str, ...],
    ) -> bool:
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, str):
            if value in dropped_ids:
                return True
            return any(content and content in value for content in dropped_contents)
        if isinstance(value, dict):
            item_id = value.get("id")
            if isinstance(item_id, str) and item_id in dropped_ids:
                return True
            return any(
                ContextEngine._value_mentions_dropped(item, dropped_ids, dropped_contents)
                for item in value.values()
            )
        if isinstance(value, (list, tuple)):
            return any(
                ContextEngine._value_mentions_dropped(item, dropped_ids, dropped_contents)
                for item in value
            )
        return False

    @staticmethod
    def _sanitize_dropped_values(
        value: Any,
        dropped_ids: set[str],
        dropped_contents: tuple[str, ...],
    ) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, list):
            return [
                ContextEngine._sanitize_dropped_values(item, dropped_ids, dropped_contents)
                for item in value
                if not ContextEngine._value_mentions_dropped(item, dropped_ids, dropped_contents)
            ]
        if isinstance(value, tuple):
            return tuple(
                ContextEngine._sanitize_dropped_values(item, dropped_ids, dropped_contents)
                for item in value
                if not ContextEngine._value_mentions_dropped(item, dropped_ids, dropped_contents)
            )
        if isinstance(value, dict):
            item_id = value.get("id")
            if isinstance(item_id, str) and item_id in dropped_ids:
                return {}
            return {
                key: ContextEngine._sanitize_dropped_values(item, dropped_ids, dropped_contents)
                for key, item in value.items()
                if not ContextEngine._value_mentions_dropped(item, dropped_ids, dropped_contents)
            }
        return value

    @staticmethod
    def _metadata_items(value: Any) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, dict):
            item_id = value.get("id")
            if isinstance(item_id, str) and item_id:
                content = value.get("content")
                items.append((item_id, str(content) if isinstance(content, str) else ""))
            for nested in value.values():
                items.extend(ContextEngine._metadata_items(nested))
        elif isinstance(value, (list, tuple)):
            for nested in value:
                items.extend(ContextEngine._metadata_items(nested))
        return items

    def _finalize_supply_pack(
        self,
        pack: ContextPack,
        retrieval_plan: RetrievalPlan,
        *,
        task_type: str,
        project_id: str,
        project_policy: str,
    ) -> ContextPack:
        """Install the final fail-closed gate and attach public retrieval metadata."""
        self._refresh_canonical_cache_if_changed()
        pack.audit_metadata = dict(getattr(pack, "audit_metadata", {}) or {})
        pack.channel_states = dict(getattr(pack, "channel_states", {}) or {})
        for channel in retrieval_plan.channels:
            if channel in pack.channel_states:
                continue
            evidence_only = channel not in retrieval_plan.fusion_channels
            pack.channel_states[channel] = {
                "planned": True,
                "enabled": evidence_only,
                "available": evidence_only,
                "executed": evidence_only,
                "participating": False,
                "evidence_only": evidence_only,
                "reason": "evidence_only" if evidence_only else "unavailable",
                "result_count": 0,
            }
        existing_gate = pack.audit_metadata.pop("synthesis_retrieval", None)
        pack.audit_metadata.pop("synthesis_provenance", None)
        existing_candidate_ids: list[str] = []
        if isinstance(existing_gate, dict):
            rows = existing_gate.get("degradations")
            if isinstance(rows, list):
                existing_candidate_ids = [
                    str(row.get("id", ""))
                    for row in rows
                    if isinstance(row, dict) and row.get("id")
                ]
        layered_items = [*pack.core, *pack.related, *pack.divergent]
        ranking_candidate_ids = [
            str(row.get("memory_id") or "")
            for rows in dict(getattr(pack, "channel_rankings", {}) or {}).values()
            for row in rows
            if isinstance(row, dict) and row.get("memory_id")
        ]
        items_by_id = {item.id: item for item in layered_items}
        layer_by_id = {
            item.id: layer
            for layer in ("core", "related", "divergent")
            for item in getattr(pack, layer, [])
        }
        metadata_items: list[tuple[str, str]] = []
        for surface in (
            getattr(pack, "per_item_stats", []),
            getattr(pack, "pipeline_stats", {}),
            pack.audit_metadata,
            pack.gap_signal,
        ):
            metadata_items.extend(self._metadata_items(surface))
        candidate_ids = [item.id for item in layered_items]
        candidate_ids.extend(ranking_candidate_ids)
        candidate_ids.extend(item_id for item_id, _content in metadata_items)
        candidate_ids.extend(existing_candidate_ids)
        decision = self._gate_memory_ids(candidate_ids)
        payload_mismatch_ids: set[str] = set()
        admitted_by_gate = set(decision.items)
        sqlite_get = getattr(getattr(self, "_sqlite", None), "get", None)
        for item_id in admitted_by_gate:
            try:
                canonical = (
                    sqlite_get(item_id) if callable(sqlite_get) else self._memories.get(item_id)
                )
            except Exception:
                payload_mismatch_ids.add(item_id)
                continue
            if not isinstance(canonical, dict):
                if not item_id.startswith(NONCANONICAL_CONTEXT_PREFIXES):
                    payload_mismatch_ids.add(item_id)
                continue
            canonical_type = str(canonical.get("memory_type") or "").strip().casefold()
            if canonical_type != "synthesis":
                continue
            canonical_content = str(canonical.get("content") or "")
            allowed_content = {
                canonical_content,
                canonical_content[:500],
                canonical_content[:300],
                canonical_content[:120],
            }
            payload_content = [item.content for item in layered_items if item.id == item_id]
            payload_content.extend(
                content
                for metadata_id, content in metadata_items
                if metadata_id == item_id and content
            )
            if any(content not in allowed_content for content in payload_content):
                payload_mismatch_ids.add(item_id)
        decision_degradations = [
            *decision.degradations,
            *(
                {"id": item_id, "reason": "candidate_payload_mismatch"}
                for item_id in sorted(payload_mismatch_ids)
            ),
        ]
        project_blocked_ids = {
            item_id
            for item_id in dict.fromkeys(candidate_ids)
            if not self._project_item_allowed(
                items_by_id.get(item_id)
                or ContextItem(item_id, "", 0.0, source="metadata", layer="related"),
                layer_by_id.get(item_id, "related"),
                project_id=project_id,
                project_policy=project_policy,
            )
        }
        from plastic_promise.core.retrieval_planner import (
            requires_synthesis_source_expansion,
        )
        from plastic_promise.core.synthesis_retrieval import (
            expand_synthesis_sources,
            synthesis_provenance,
        )

        provenance_by_id: dict[str, dict[str, Any]] = {}
        provenance_failed_ids: set[str] = set()
        conn = getattr(getattr(self, "_sqlite", None), "_conn", None)
        for item_id in decision.admitted_synthesis_ids:
            if item_id in project_blocked_ids or item_id in payload_mismatch_ids:
                continue
            provenance = synthesis_provenance(conn, item_id) if conn is not None else {}
            if provenance:
                provenance_by_id[item_id] = provenance
            else:
                provenance_failed_ids.add(item_id)
                decision_degradations.append(
                    {"id": item_id, "reason": "synthesis_provenance_unavailable"}
                )
        pre_expansion_admitted_ids = (
            set(decision.items) - project_blocked_ids - payload_mismatch_ids - provenance_failed_ids
        )
        selected_layer_ids: list[str] = []
        fallback_layer_ids: list[str] = []
        for layer in ("core", "related", "divergent"):
            budget = max(0, int(retrieval_plan.budget.get(layer, 0)))
            layer_items = list(getattr(pack, layer, []))
            admitted_layer_ids = [
                item.id for item in layer_items if item.id in pre_expansion_admitted_ids
            ]
            project_allowed_layer_ids = [
                item.id for item in layer_items if item.id not in project_blocked_ids
            ]
            selected_layer_ids.extend(admitted_layer_ids[:budget])
            fallback_layer_ids.extend(project_allowed_layer_ids[:budget])
        degradation_reasons_by_id: dict[str, set[str]] = {}
        for row in decision_degradations:
            item_id = str(row.get("id") or "")
            reason = str(row.get("reason") or "")
            if item_id and reason:
                degradation_reasons_by_id.setdefault(item_id, set()).add(reason)
        selected_governed_ids = tuple(
            item_id
            for item_id in dict.fromkeys(selected_layer_ids)
            if self._synthesis_memory_reserved(item_id)
        )
        fallback_governed_ids = tuple(
            item_id
            for item_id in dict.fromkeys(fallback_layer_ids)
            if item_id not in selected_governed_ids
            and self._synthesis_memory_reserved(item_id)
            and self._synthesis_source_fallback_allowed(
                conn,
                item_id,
                reasons=degradation_reasons_by_id.get(item_id, set()),
                task_type=task_type,
                retrieval_mode=retrieval_plan.mode,
            )
        )
        raw_evidence_budget = max(
            0,
            int(retrieval_plan.budget.get("raw_evidence", 0)),
        )
        high_impact_expansion = requires_synthesis_source_expansion(
            retrieval_plan,
            task_type=task_type,
        )
        allowed_source_evidence: list[dict[str, Any]] = []
        if high_impact_expansion:
            expansion_targets = tuple(
                item_id
                for item_id in (*selected_governed_ids, *fallback_governed_ids)
                if item_id not in project_blocked_ids
            )
            fallback_governed_id_set = set(fallback_governed_ids)
            expansion_limit = raw_evidence_budget * self._synthesis_overfetch_factor()
            source_hydration_failed_ids: set[str] = set()
            for synthesis_id in expansion_targets:
                source_evidence = (
                    expand_synthesis_sources(
                        conn,
                        [synthesis_id],
                        limit=expansion_limit,
                    )
                    if conn is not None and expansion_limit > 0
                    else []
                )
                if synthesis_id in fallback_governed_id_set and not (
                    self._synthesis_source_fallback_allowed(
                        conn,
                        synthesis_id,
                        reasons=degradation_reasons_by_id.get(synthesis_id, set()),
                        task_type=task_type,
                        retrieval_mode=retrieval_plan.mode,
                    )
                ):
                    continue
                allowed_for_synthesis: list[dict[str, Any]] = []
                for evidence in source_evidence:
                    try:
                        source_item = ContextItem(
                            str(evidence.get("id") or ""),
                            str(evidence.get("content") or ""),
                            float(evidence.get("score") or 0.0),
                            source=str(evidence.get("source") or "synthesis_source"),
                            layer="related",
                        )
                    except (TypeError, ValueError):
                        continue
                    if source_item.id and self._project_item_allowed(
                        source_item,
                        "related",
                        project_id=project_id,
                        project_policy=project_policy,
                    ):
                        allowed_for_synthesis.append(evidence)
                if (
                    synthesis_id in provenance_by_id
                    and expansion_limit > 0
                    and not allowed_for_synthesis
                ):
                    source_hydration_failed_ids.add(synthesis_id)
                allowed_source_evidence.extend(allowed_for_synthesis)
            for synthesis_id in source_hydration_failed_ids:
                provenance_by_id.pop(synthesis_id, None)
                provenance_failed_ids.add(synthesis_id)
                decision_degradations.append(
                    {
                        "id": synthesis_id,
                        "reason": "synthesis_source_hydration_unavailable",
                    }
                )
        admitted_ids = (
            set(decision.items) - project_blocked_ids - payload_mismatch_ids - provenance_failed_ids
        )
        dropped_ids = (
            set(decision.dropped_ids)
            | project_blocked_ids
            | payload_mismatch_ids
            | provenance_failed_ids
        )
        governed_metadata_ids = {
            item_id
            for item_id in dict.fromkeys(candidate_ids)
            if self._synthesis_memory_reserved(item_id)
        }
        metadata_redaction_ids = dropped_ids | governed_metadata_ids
        dropped_contents = tuple(
            dict.fromkeys(
                [item.content for item in layered_items if item.id in dropped_ids and item.content]
                + [
                    content
                    for item_id, content in metadata_items
                    if item_id in dropped_ids and content
                ]
            )
        )

        for layer in ("core", "related", "divergent"):
            budget = max(0, int(retrieval_plan.budget.get(layer, 0)))
            admitted_layer = [item for item in getattr(pack, layer, []) if item.id in admitted_ids]
            setattr(pack, layer, admitted_layer[:budget])

        admitted_rankings: dict[str, list[dict[str, Any]]] = {}
        for channel, rows in dict(getattr(pack, "channel_rankings", {}) or {}).items():
            admitted_rows = [
                dict(row)
                for row in rows
                if isinstance(row, dict) and str(row.get("memory_id") or "") in admitted_ids
            ]
            for rank, row in enumerate(admitted_rows, start=1):
                row["rank"] = rank
            admitted_rankings[str(channel)] = admitted_rows
        pack.channel_rankings = admitted_rankings

        pack.per_item_stats = [
            row
            for row in list(getattr(pack, "per_item_stats", []) or [])
            if not self._value_mentions_dropped(row, dropped_ids, dropped_contents)
        ]

        pack.pipeline_stats = self._sanitize_dropped_values(
            dict(getattr(pack, "pipeline_stats", {}) or {}),
            metadata_redaction_ids,
            dropped_contents,
        )
        pack.audit_metadata = self._sanitize_dropped_values(
            pack.audit_metadata,
            metadata_redaction_ids,
            dropped_contents,
        )

        recommender = pack.audit_metadata.get("context_recommender")
        if recommender is not None:
            pack.audit_metadata["context_recommender"] = self._sanitize_dropped_values(
                recommender,
                metadata_redaction_ids,
                dropped_contents,
            )

        combined_degradations = decision_degradations
        if combined_degradations:
            unique_degradations = {
                (row["id"], row["reason"]): {"id": row["id"], "reason": row["reason"]}
                for row in combined_degradations
            }
            pack.audit_metadata["synthesis_retrieval"] = {
                "degradations": list(unique_degradations.values())
            }

        if pack.gap_signal is not None:
            gap_value: Any = pack.gap_signal
            gap_type = type(gap_value)
            if is_dataclass(gap_value):
                sanitized = self._sanitize_dropped_values(
                    asdict(gap_value), metadata_redaction_ids, dropped_contents
                )
                try:
                    pack.gap_signal = gap_type(**sanitized)
                except Exception:
                    pack.gap_signal = sanitized
            else:
                pack.gap_signal = self._sanitize_dropped_values(
                    gap_value, metadata_redaction_ids, dropped_contents
                )

        public_memory_ids = set(self._public_memory_ids(self._memories))
        public_graph_nodes, public_graph_edges = self._public_graph_snapshot()
        public_ldb_rows = 0
        if self._ldb is not None:
            list_ids = getattr(self._ldb, "list_memory_ids", None)
            if callable(list_ids):
                try:
                    public_ldb_rows = len(set(list_ids()) & public_memory_ids)
                except Exception:
                    public_ldb_rows = 0
        pack.audit_metadata["memory_pool_size"] = str(len(public_memory_ids))
        pack.audit_metadata["graph_nodes"] = str(len(public_graph_nodes))
        pack.audit_metadata["graph_edges"] = str(len(public_graph_edges))
        pack.audit_metadata["ldb_rows"] = str(public_ldb_rows)

        final_items = [*pack.core, *pack.related, *pack.divergent]
        selected_synthesis_ids = tuple(
            item.id for item in final_items if item.id in provenance_by_id
        )
        selected_provenance = {
            item_id: provenance_by_id[item_id] for item_id in selected_synthesis_ids
        }
        if selected_provenance:
            pack.audit_metadata["synthesis_provenance"] = selected_provenance

        if high_impact_expansion:
            ordinary_items = [
                item for item in final_items if not self._synthesis_memory_reserved(item.id)
            ]
            ranked_evidence = self._raw_evidence_from_items(
                ordinary_items,
                raw_evidence_budget,
            )
            raw_evidence: list[dict[str, Any]] = []
            seen_evidence_ids: set[str] = set()
            for evidence in [*allowed_source_evidence, *ranked_evidence]:
                evidence_id = str(evidence.get("id") or "")
                if not evidence_id or evidence_id in seen_evidence_ids:
                    continue
                seen_evidence_ids.add(evidence_id)
                raw_evidence.append(evidence)
                if len(raw_evidence) >= raw_evidence_budget:
                    break
        else:
            raw_evidence = self._raw_evidence_from_items(
                final_items,
                raw_evidence_budget,
            )
        self._attach_retrieval_plan(
            pack,
            retrieval_plan,
            raw_evidence=raw_evidence,
        )
        return pack

    @staticmethod
    def _merge_ranked_results(
        primary: list[tuple[str, float, str, str]],
        extra: list[tuple[str, float, str, str]],
    ) -> list[tuple[str, float, str, str]]:
        combined: dict[str, tuple[float, str, str]] = {
            item_id: (score, content, source) for item_id, score, content, source in primary
        }
        for item_id, score, content, source in extra:
            existing = combined.get(item_id)
            if existing is None or score > existing[0]:
                combined[item_id] = (score, content, source)
        return [
            (item_id, score, content, source)
            for item_id, (score, content, source) in sorted(
                combined.items(), key=lambda item: item[1][0], reverse=True
            )
        ]

    def _code_memory_retrieval(
        self,
        task_description: str,
        retrieval_plan: RetrievalPlan,
        limit: int | None = None,
    ) -> list[tuple[str, float, str, str]]:
        if os.environ.get("PP_CODE_MEMORY_ENABLED", "1") != "1":
            return []
        if retrieval_plan.mode not in {"code", "mix", "audit"}:
            return []
        try:
            from plastic_promise.core.code_memory import search_code_index

            index = self._ensure_code_memory_index()
            return search_code_index(
                index,
                task_description,
                limit=limit or retrieval_plan.budget.get("raw_evidence", 12),
            )
        except Exception as exc:
            logger.warning("code_memory retrieval skipped: %s", exc)
            return []

    def _enrich_pack_with_code_memory(
        self,
        pack: ContextPack,
        task_description: str,
        retrieval_plan: RetrievalPlan,
    ) -> None:
        code_results = self._code_memory_retrieval(task_description, retrieval_plan)
        if self._code_index is not None:
            pack.audit_metadata = dict(getattr(pack, "audit_metadata", {}) or {})
            pack.audit_metadata["code_memory"] = self._code_index.to_audit()
        if not code_results:
            return

        code_items = [
            ContextItem(
                id=item_id,
                content=content,
                relevance=float(score),
                source=source,
                freshness="fresh",
                layer="related",
                worth_score=1.0,
                confidence=0.8,
            )
            for item_id, score, content, source in code_results
        ]
        related_budget = int(
            retrieval_plan.budget.get("related", len(pack.related) + len(code_items))
        )
        pack.related = sorted(
            [*pack.related, *code_items],
            key=lambda item: item.relevance,
            reverse=True,
        )[:related_budget]

    def _ensure_code_memory_index(self):
        from pathlib import Path

        from plastic_promise.core.code_memory import build_code_index

        root = str(Path(os.environ.get("PP_CODE_MEMORY_ROOT", os.getcwd())).resolve())
        max_files = int(os.environ.get("PP_CODE_MEMORY_MAX_FILES", "400"))
        if self._code_index is not None and self._code_index_root == f"{root}:{max_files}":
            return self._code_index

        index = build_code_index(root, max_files=max_files)
        self._code_index = index
        self._code_index_root = f"{root}:{max_files}"
        self._register_code_memory_graph(index)
        return index

    def _register_code_memory_graph(self, index) -> None:
        with self._write_lock:
            self._register_code_memory_graph_locked(index)

    def _register_code_memory_graph_locked(self, index) -> None:
        for node in index.nodes:
            node_id = node.get("id", "")
            if not node_id:
                continue
            if self._synthesis_memory_reserved(node_id):
                continue
            node_data = {k: v for k, v in node.items() if k != "id"}
            if not self._persist_ordinary_graph_node(node_id, node_data):
                continue
            self._graph_nodes[node_id] = node_data
        for edge in index.edges:
            self.add_graph_edge(
                str(edge.get("from") or ""),
                str(edge.get("to") or ""),
                relation=str(edge.get("relation") or "references"),
                weight=float(edge.get("weight", 0.5)),
                metadata=(edge.get("metadata") if isinstance(edge.get("metadata"), dict) else None),
                source_kind=str(edge.get("source_kind") or ""),
                evidence_id=str(edge.get("evidence_id") or ""),
            )

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

    def _python_channel_states(
        self,
        retrieval_plan: RetrievalPlan,
        *,
        rank_sources: Mapping[str, list[tuple]],
        graph_results: list[tuple],
        code_results: list[tuple],
        retrieval_degradations: list[dict[str, str]],
    ) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        degradation_by_channel = {
            str(row.get("channel") or ""): str(row.get("reason") or "unavailable")
            for row in retrieval_degradations
            if isinstance(row, dict) and row.get("channel")
        }
        evidence_results = {
            "graph": graph_results,
            "code": code_results,
            "audit": graph_results,
            "principle": graph_results,
        }
        for channel in retrieval_plan.channels:
            evidence_only = channel not in retrieval_plan.fusion_channels
            enabled = True
            if channel == "fts":
                enabled = (
                    os.environ.get("PP_FTS_DISABLED", "") != "1"
                    and os.environ.get("PP_FTS_FUSION", "1") == "1"
                )
            available = enabled
            if channel == "graph":
                available = enabled and bool(self._graph_edges)
            if channel in degradation_by_channel:
                available = False
            executed = enabled and available
            participating = bool(
                not evidence_only and enabled and available and channel in rank_sources
            )
            if evidence_only:
                reason = "evidence_only"
            elif not enabled:
                reason = "disabled"
            elif not available:
                reason = degradation_by_channel.get(channel, "unavailable")
            elif participating:
                reason = "participating"
            else:
                reason = "not_executed"
            result_count = len(evidence_results.get(channel, rank_sources.get(channel, [])))
            states[channel] = {
                "planned": True,
                "enabled": enabled,
                "available": available,
                "executed": executed,
                "participating": participating,
                "evidence_only": evidence_only,
                "reason": reason,
                "result_count": result_count,
            }
        return states

    def _supply_python(
        self,
        task_description: str,
        task_vector: list[float],
        task_type: str = "general",
        scope: str = "global",
        debug: bool = False,
        retrieval_plan: RetrievalPlan | None = None,
        project_id: str = "project:legacy-global",
        project_policy: str = "balanced",
        project_degraded: bool = False,
        fusion_config: FusionConfig | None = None,
        fusion_decision: FusionDecision | None = None,
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

        if retrieval_plan is None:
            retrieval_plan = plan_retrieval(
                task_type=task_type,
                scope=scope,
                project_policy=project_policy,
                has_vector=any(v != 0.0 for v in task_vector),
                has_graph=bool(self._graph_edges),
                has_fts=(
                    self._ldb is not None
                    and os.environ.get("PP_FTS_DISABLED", "") != "1"
                    and os.environ.get("PP_FTS_FUSION", "1") == "1"
                ),
            )
        candidate_limit = self._candidate_budget(retrieval_plan)

        # Phase 1: 三路分层检索 — 细→类→粗
        # 细 (graph): 原则关联图谱 — 最精确的信号
        graph_results = self._graph_traversal(task_type)[:candidate_limit]

        # 类 (tier): 文本匹配 + L1 工作记忆优先级提升
        domain_hint = scope if scope and scope != "global" else None

        # Query expansion: inject domain-relevant synonyms for BM25 text search.
        # Vector search uses raw query — semantic models handle synonyms natively.
        expanded_query = task_description
        if os.environ.get("PP_QUERY_EXPANSION", "1") == "1":
            try:
                from plastic_promise.core.query_expander import expand_query

                expanded_query = expand_query(task_description, domain_hint)
            except Exception:
                pass  # expansion failure never blocks retrieval

        text_results = self._text_retrieval(expanded_query, trust_boost, domain_hint)[
            :candidate_limit
        ]

        # 粗 (vector): 语义向量相关性 (零向量时跳过)
        vector_results = []
        if any(v != 0.0 for v in task_vector):
            vector_results = self._call_with_optional_limit(
                self._vector_retrieval,
                task_vector,
                domain_hint,
                limit=candidate_limit,
            )

        # FTS: LanceDB full-text search as third retrieval channel
        fts_results = self._call_with_optional_limit(
            self._fts_retrieval,
            task_description,
            scope,
            limit=candidate_limit,
        )
        retrieval_degradations: list[dict[str, str]] = []
        if self._ldb is not None:
            consume_diagnostics = getattr(self._ldb, "consume_search_diagnostics", None)
            if callable(consume_diagnostics):
                try:
                    retrieval_degradations.extend(consume_diagnostics())
                except Exception as exc:
                    retrieval_degradations.append(
                        {
                            "channel": "fts",
                            "reason": "retrieval_diagnostics_unavailable",
                            "error_class": exc.__class__.__name__,
                        }
                    )

        canonical_hot_enabled = os.environ.get("PP_CANONICAL_HOT_LOOKUP", "0") == "1"
        canonical_hot_enforce = os.environ.get("PP_CANONICAL_HOT_ENFORCE", "0") == "1"
        try:
            canonical_hot_limit = int(os.environ.get("PP_CANONICAL_HOT_LIMIT", "12"))
        except (TypeError, ValueError):
            canonical_hot_limit = 12
        canonical_hot_hits = []
        canonical_hot_results: list[tuple[str, float, str, str]] = []
        if canonical_hot_enabled:
            try:
                from plastic_promise.core.canonical_hot_memory import (
                    canonical_hits_to_results,
                    lookup_canonical_hot,
                )

                hot_code_index = None
                if (
                    os.environ.get("PP_CANONICAL_HOT_CODE_INDEX", "1") == "1"
                    and os.environ.get("PP_CODE_MEMORY_ENABLED", "1") == "1"
                ):
                    hot_code_index = self._ensure_code_memory_index()
                canonical_hot_hits = lookup_canonical_hot(
                    task_description,
                    code_index=hot_code_index,
                    domain_hint=domain_hint,
                    limit=min(
                        candidate_limit,
                        canonical_hot_limit * self._synthesis_overfetch_factor(),
                    ),
                )
                canonical_hot_results = canonical_hits_to_results(canonical_hot_hits)
            except Exception as exc:
                logger.warning("canonical_hot lookup skipped: %s", exc)

        code_results = self._call_with_optional_limit(
            self._code_memory_retrieval,
            task_description,
            retrieval_plan,
            limit=candidate_limit,
        )

        # Phase 2: explicit versioned fusion, then layer with graph evidence.
        if fusion_config is not None:
            channel_results = {
                "vector": vector_results,
                "bm25": text_results,
                "fts": fts_results,
            }
            rankings = {
                channel: [(str(row[0]), float(row[1])) for row in channel_results[channel]]
                for channel in fusion_config.channels
            }
            fused_scores = weighted_rrf(rankings, fusion_config)
            hydrated = {
                str(row[0]): (str(row[2]), str(row[3]))
                for channel in fusion_config.channels
                for row in channel_results[channel]
            }
            fused_results = [
                (memory_id, score, *hydrated[memory_id])
                for memory_id, score in fused_scores
                if memory_id in hydrated
            ]
        elif vector_results:
            vector_weight = float(os.environ.get("PP_VECTOR_WEIGHT", "0.50"))
            fused_results = self._hybrid_fuse(
                vector_results,
                text_results,
                vector_weight=vector_weight,
                fts_results=fts_results,
            )
        else:
            # No vector available (Ollama down / zero vector) — fuse text + fts
            fused: dict[str, tuple[float, str, str]] = {}
            for mid, score, content, source in text_results:
                fused[mid] = (score * 0.8, content, source)
            for mid, score, content, source in fts_results:
                w = score * 0.8
                if score >= 0.85:
                    w = score
                if mid in fused:
                    existing_score, existing_content, existing_source = fused[mid]
                    fused[mid] = (max(existing_score, w), existing_content, existing_source)
                else:
                    fused[mid] = (w, content, source)
            fused_results = [
                (mid, score, content, source)
                for mid, (score, content, source) in sorted(
                    fused.items(), key=lambda x: x[1][0], reverse=True
                )
            ]
        all_results = self._layered_fuse(graph_results, fused_results, [])
        all_results = self._merge_ranked_results(all_results, code_results)
        if canonical_hot_enforce:
            all_results = self._merge_ranked_results(all_results, canonical_hot_results)
        all_results, synthesis_degradations = self._filter_synthesis_result_tuples(
            all_results[:candidate_limit]
        )

        # P2: Evolve edge weights based on feedback patterns
        self._apply_edge_feedback()

        # Phase 3-5 fused: symbol rules + feedback + ContextItem building (was 3 passes, now 1)
        from plastic_promise.core.constants import (
            FEEDBACK_SCORE_MULTIPLIER_MIN,
            FEEDBACK_SCORE_MULTIPLIER_RANGE,
            HARD_MIN_SCORE,
        )

        vector_score_map = {mid: score for mid, score, _content, _source in vector_results}
        text_score_map = {mid: score for mid, score, _content, _source in text_results}
        fts_score_map = {mid: score for mid, score, _content, _source in fts_results}
        graph_score_map = {mid: score for mid, score, _content, _source in graph_results}
        debug_items_by_id: dict[str, dict] = {}
        after_noise_filter = 0
        after_source_filter = 0
        after_hard_score_filter = 0
        context_gate_enabled = os.environ.get("PP_CONTEXT_GATE", "0") == "1"
        context_gate_enforce = os.environ.get("PP_CONTEXT_GATE_ENFORCE", "0") == "1"
        context_gate_summary: dict[str, Any] = {
            "enabled": context_gate_enabled,
            "enforce": context_gate_enforce,
            "items_evaluated": 0,
            "decisions": {},
        }
        candidate_evidence_cls = None
        evaluate_context_gate = None
        if context_gate_enabled:
            from plastic_promise.core.context_gate import (
                CandidateEvidence as CandidateEvidenceClass,
            )
            from plastic_promise.core.context_gate import (
                evaluate_context_gate,
            )

            candidate_evidence_cls = CandidateEvidenceClass

        pack = ContextPack(activated_principles=activated)
        current_time_str = datetime.datetime.now().isoformat()  # single timestamp for decay calc

        for item_id, score, content, source in all_results:
            fused_score = score
            mem = self._memories.get(item_id, {}) if not item_id.startswith("principle:") else {}
            memory_source = mem.get("source", source) if mem else source
            source_penalty = 1.0

            if mem and ContextEngine._is_forgotten_memory(mem):
                continue

            # --- Drop low-information write-side legacy noise during recall too. ---
            if ContextEngine._is_recall_noise(content):
                continue
            after_noise_filter += 1

            # --- Source/type filtering: downweight or exclude noisy memory sources. ---
            source_penalty = ContextEngine._source_penalty_for(memory_source)
            if source_penalty is None:
                continue
            score *= source_penalty
            after_source_filter += 1

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
            worth = ContextEngine._calc_worth_score_from_memory(mem) if mem else 0.5
            multiplier = FEEDBACK_SCORE_MULTIPLIER_MIN + FEEDBACK_SCORE_MULTIPLIER_RANGE * worth
            score = score * multiplier

            # --- Decay-aware ranking (Phase 1.3) ---
            before_decay = score
            score = self._apply_decay_awareness(score, mem, current_time_str, trust_boost)
            decay_multiplier = (score / before_decay) if before_decay else 1.0

            # --- Length normalization (Phase 1.5) ---
            before_length = score
            score = ContextEngine._apply_length_norm(score, content)
            length_norm_factor = (score / before_length) if before_length else 1.0

            # --- Hard minimum score threshold (Phase Quality Gate) ---
            hard_min_score = float(os.environ.get("PP_HARD_MIN_SCORE", str(HARD_MIN_SCORE)))
            if hard_min_score > 0 and score < hard_min_score:
                continue
            after_hard_score_filter += 1

            # --- ContextItem construction (was separate Phase 5 loop) ---
            is_principle = item_id.startswith("principle:")
            worth_score = ContextEngine._calc_worth_score_from_memory(mem) if mem else 0.0
            freshness = self._calc_freshness(item_id)
            gate_result = None
            if context_gate_enabled and candidate_evidence_cls and evaluate_context_gate:
                source_class = (
                    mem.get("source_class")
                    or ("code" if source == "code_memory" else "")
                    or ("principle" if is_principle else "")
                    or memory_source
                    or "experience"
                )
                candidate_kind = (
                    "principle"
                    if is_principle
                    else "mcp_tool"
                    if item_id.startswith("mcp_tool:")
                    else "code_symbol"
                    if item_id.startswith("code:")
                    else mem.get("memory_type", "memory")
                    if mem
                    else "memory"
                )
                decay_status = self._calc_decay_status(item_id, mem) if mem else "healthy"
                freshness_score = {
                    "fresh": 1.0,
                    "valid": 0.85,
                    "healthy": 0.85,
                    "stale": 0.55,
                    "decaying": 0.30,
                    "expired": 0.0,
                }.get(decay_status if mem else freshness, 0.75)
                status = str(mem.get("status", mem.get("correction_status", ""))) if mem else ""
                conflict_score = 0.0
                status_lower = status.lower()
                if status_lower in {"obsolete", "corrected", "deprecated", "rejected"}:
                    conflict_score = 1.0
                elif mem and mem.get("project_id") not in {
                    "",
                    None,
                    project_id,
                    "project:legacy-global",
                }:
                    conflict_score = 0.5

                gate_result = evaluate_context_gate(
                    candidate_evidence_cls(
                        id=item_id,
                        content=content,
                        source=memory_source,
                        retrieval_source=source,
                        base_score=score,
                        kind=candidate_kind,
                        project_id=mem.get("project_id", "project:legacy-global")
                        if mem
                        else "project:legacy-global",
                        visibility=mem.get("visibility", "project") if mem else "global",
                        source_class=source_class,
                        worth_score=worth if mem else worth_score or 0.5,
                        freshness_score=freshness_score,
                        conflict_score=conflict_score,
                        canonical_key=item_id if source == "canonical_hot" else None,
                        status=status,
                    ),
                    task_type=task_type,
                    retrieval_mode=retrieval_plan.mode,
                    project_id=project_id,
                    project_policy=project_policy,
                    project_degraded=project_degraded,
                )
                context_gate_summary["items_evaluated"] += 1
                decisions = context_gate_summary["decisions"]
                decisions[gate_result.decision] = decisions.get(gate_result.decision, 0) + 1
                if context_gate_enforce and gate_result.decision in {"block", "raw_only"}:
                    continue

            debug_items_by_id[item_id] = {
                "id": item_id,
                "content": content[:120],
                "vector_score": vector_score_map.get(item_id),
                "bm25_score": text_score_map.get(item_id),
                "fts_score": fts_score_map.get(item_id),
                "graph_score": graph_score_map.get(item_id),
                "fused_score": fused_score,
                "worth": worth,
                "decay_multiplier": decay_multiplier,
                "length_norm_factor": length_norm_factor,
                "source_penalty": source_penalty,
                "final_score": score,
                "source": memory_source,
                "retrieval_source": source,
                "memory_type": mem.get("memory_type", "") if mem else "",
                "tier": mem.get("tier", "") if mem else "",
                "category": mem.get("category", mem.get("domain", "")) if mem else "",
            }
            if gate_result is not None:
                debug_items_by_id[item_id]["gate_score"] = gate_result.gate_score
                debug_items_by_id[item_id]["gate_decision"] = gate_result.decision
                debug_items_by_id[item_id]["gate_reasons"] = list(gate_result.reasons)

            item = ContextItem(
                id=item_id,
                content=content,
                relevance=score,
                source=memory_source,
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
            # Unified reranker (Phase 1.6): multi-provider chain, default ON;
            # disable with PP_RERANK_DISABLED=1.
            from plastic_promise.core.reranker import MultiProviderReranker

            all_items = MultiProviderReranker().rerank(task_description, all_items)
            # MMR diversity (Phase 1.4)
            all_items = self._apply_mmr(all_items, threshold=0.85, penalty=0.70)
            hard_min_score = float(os.environ.get("PP_HARD_MIN_SCORE", str(HARD_MIN_SCORE)))
            if hard_min_score > 0:
                all_items = [item for item in all_items if item.relevance >= hard_min_score]
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

        final_items = pack.core + pack.related + pack.divergent
        if debug:
            rank_sources = {
                "vector": vector_results,
                "bm25": text_results,
                "fts": fts_results,
            }
            pack.channel_rankings = {
                channel: [
                    {
                        "memory_id": str(row[0]),
                        "score": float(row[1]),
                        "rank": rank,
                    }
                    for rank, row in enumerate(
                        sorted(
                            rank_sources[channel],
                            key=lambda item: (-float(item[1]), str(item[0])),
                        )[: retrieval_plan.channel_windows[channel]],
                        start=1,
                    )
                ]
                for channel in retrieval_plan.fusion_channels
            }
            pack.channel_states = self._python_channel_states(
                retrieval_plan,
                rank_sources=rank_sources,
                graph_results=graph_results,
                code_results=code_results,
                retrieval_degradations=retrieval_degradations,
            )
            pack.pipeline_stats = {
                "vector_count": len(vector_results),
                "bm25_count": len(text_results),
                "fts_count": len(fts_results),
                "graph_count": len(graph_results),
                "fused_count": len(all_results),
                "after_noise_filter": after_noise_filter,
                "after_source_filter": after_source_filter,
                "after_hard_score_filter": after_hard_score_filter,
                "after_mmr": len(final_items),
                "core_count": len(pack.core),
                "related_count": len(pack.related),
                "divergent_count": len(pack.divergent),
                "canonical_hot_count": len(canonical_hot_hits),
                "context_gate_evaluated": context_gate_summary["items_evaluated"],
            }
            pack.per_item_stats = []
            for item in final_items:
                stats = dict(debug_items_by_id.get(item.id, {}))
                if stats:
                    stats["final_score"] = item.relevance
                    stats["layer"] = item.layer
                    pack.per_item_stats.append(stats)

        # Phase 6: 审计元数据
        pack.audit_metadata = {
            "engine_version": "0.1.0-py",
            "task_type": task_type,
            "principle_injection_count": str(len(activated)),
            "graph_nodes": str(len(self._graph_nodes)),
            "graph_edges": str(len(self._graph_edges)),
            "memory_pool_size": str(len(self._memories)),
            "vector_search": "active" if vector_results else "fallback_text_only",
            "fts_search": "active" if fts_results else "inactive",
            "ldb_rows": str(self._ldb.count_rows()) if self._ldb else "0",
            "rerank_status": "multi-provider",
            "code_memory": self._code_index.to_audit()
            if self._code_index is not None
            else {"enabled": False},
            "canonical_hot": {
                "enabled": canonical_hot_enabled,
                "enforce": canonical_hot_enforce,
                "hits": len(canonical_hot_hits),
                "keys": [hit.key for hit in canonical_hot_hits],
                "limit": canonical_hot_limit,
            },
            "context_gate": context_gate_summary,
            "retrieval_degradations": retrieval_degradations,
        }
        if fusion_decision is not None:
            pack.audit_metadata["retrieval_fusion"] = self._fusion_audit_metadata(
                fusion_decision,
                fusion_config,
            )
        if synthesis_degradations:
            pack.audit_metadata["synthesis_retrieval"] = {
                "degradations": list(synthesis_degradations)
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

        return self._finalize_supply_pack(
            pack,
            retrieval_plan,
            task_type=task_type,
            project_id=project_id,
            project_policy=project_policy,
        )

    # ========== 实体注册 ==========

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        entity_description: str = "",
        related_entities: list[str] = None,
        metadata: dict[str, Any] | None = None,
        source_kind: str = "",
    ) -> dict:
        with self._write_lock:
            return self._register_entity_locked(
                entity_type,
                entity_id,
                entity_name,
                entity_description=entity_description,
                related_entities=related_entities,
                metadata=metadata,
                source_kind=source_kind,
            )

    def _register_entity_locked(
        self,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        entity_description: str = "",
        related_entities: list[str] = None,
        metadata: dict[str, Any] | None = None,
        source_kind: str = "",
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
        validate_node_type(entity_type)

        node_id = f"{entity_type}:{entity_id}"
        reservation_ids = tuple(dict.fromkeys((str(entity_id), node_id)))
        if any(self._synthesis_memory_reserved(candidate) for candidate in reservation_ids):
            from plastic_promise.core.synthesis import SynthesisConflict

            raise SynthesisConflict("synthesis_graph_node_reserved")
        is_new = node_id not in self._graph_nodes

        # Create or update node
        node = graph_node(
            node_id,
            entity_type,
            entity_name,
            entity_description or "",
            source_kind=source_kind,
            metadata=metadata,
        )
        node_data = {k: v for k, v in node.items() if k != "id"}
        if not self._persist_ordinary_graph_node(
            node_id,
            node_data,
            reservation_ids=reservation_ids,
        ):
            from plastic_promise.core.synthesis import SynthesisConflict

            raise SynthesisConflict("synthesis_graph_node_reserved")
        self._graph_nodes[node_id] = node_data

        # Create edges to related entities
        edges_created = 0
        if related_entities:
            for related_id in related_entities:
                if self.add_graph_edge(
                    node_id,
                    related_id,
                    relation="supports",
                    weight=PRINCIPLE_INHERITANCE_DECAY,
                    source_kind=source_kind,
                ):
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
        graph_nodes, graph_edges = self._public_graph_snapshot()

        if query_type == "full_graph":
            return {
                "nodes": graph_nodes,
                "edges": graph_edges,
            }

        if query_type == "node_info":
            if not start_node or start_node not in graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                }
            node = graph_nodes[start_node]
            in_edges = [e for e in graph_edges if e.get("to") == start_node]
            out_edges = [e for e in graph_edges if e.get("from") == start_node]
            return {
                "nodes": {start_node: node},
                "edges": in_edges + out_edges,
                "in_degree": len(in_edges),
                "out_degree": len(out_edges),
            }

        if query_type == "neighbors":
            if not start_node or start_node not in graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                }
            neighbor_ids = set()
            edges = []
            for e in graph_edges:
                if e.get("from") == start_node:
                    neighbor_ids.add(e.get("to"))
                    edges.append(e)
                elif e.get("to") == start_node:
                    neighbor_ids.add(e.get("from"))
                    edges.append(e)
            nodes = {nid: graph_nodes[nid] for nid in neighbor_ids if nid in graph_nodes}
            return {"nodes": nodes, "edges": edges, "neighbor_count": len(nodes)}

        if query_type == "traverse":
            if not start_node or start_node not in graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                    "traversal_path": [],
                }
            # BFS traversal
            visited = set()
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
                if current in graph_nodes:
                    all_nodes[current] = graph_nodes[current]
                # Follow outgoing edges
                for e in graph_edges:
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

    @property
    def lancedb_store(self):
        """Return the active derived vector store, if initialized."""
        return self._ldb

    def refresh_runtime_mode(self, initialize_heavy: bool = False):
        """Refresh cached runtime state after launcher/MCP mode changes."""
        self.reset_rust_health()
        if not initialize_heavy:
            with self._heavy_init_lock:
                if os.environ.get("LDB_INIT_ON_HEAVY_INIT") != "1":
                    self._ldb = None
            return

        with self._heavy_init_lock:
            self._heavy_init_done = False
            if os.environ.get("LDB_INIT_ON_HEAVY_INIT") == "1":
                self._ldb = None
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

    def _rust_backend_paths(self) -> tuple[str, str]:
        """Return the canonical backend paths shared by probe and live supply."""
        conn = getattr(getattr(self, "_sqlite", None), "_conn", None)
        if conn is not None:
            try:
                main_database = next(
                    (
                        str(row[2] or ":memory:")
                        for row in conn.execute("PRAGMA database_list").fetchall()
                        if len(row) >= 3 and row[1] == "main"
                    ),
                    "",
                )
            except Exception as exc:
                raise RuntimeError("canonical SQLite path unavailable") from exc
            if not main_database:
                raise RuntimeError("canonical SQLite path unavailable")
            db_path = main_database
        elif self._sqlite is not None:
            raise RuntimeError("canonical SQLite backend unavailable")
        else:
            db_path = get_db_path()

        if db_path != ":memory:" and not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
        lancedb_path = getattr(self._ldb, "_path", "") or os.environ.get(
            "PLASTIC_LANCEDB_PATH",
            os.path.join(
                os.path.dirname(db_path or "plastic_memory.db"),
                "plastic_memory.lancedb",
            ),
        )
        return db_path, lancedb_path

    def _new_rust_engine(self, rust_engine_cls):
        """Construct Rust with the same canonical backends used for snapshots."""
        db_path, lancedb_path = self._rust_backend_paths()
        if hasattr(rust_engine_cls, "new_with_backends"):
            return rust_engine_cls.new_with_backends(db_path, lancedb_path)
        raise RuntimeError("Rust engine lacks explicit canonical backend constructor")

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

                # Smoke test: use the same canonical backends as live snapshots.
                engine = self._new_rust_engine(RustEngine)
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

        def convert_item(item):
            if ContextEngine._is_recall_noise(getattr(item, "content", "")):
                return None
            return ContextItem(
                id=item.id,
                content=item.content,
                relevance=item.relevance,
                source=item.source,
                freshness=item.freshness,
                layer=item.layer,
                is_principle=item.is_principle,
                worth_score=item.worth_score,
            )

        def convert_layer(items):
            converted = []
            for item in items:
                context_item = convert_item(item)
                if context_item is not None:
                    converted.append(context_item)
            return converted

        pack = ContextPack()
        pack.core = convert_layer(rust_pack.core)
        pack.related = convert_layer(rust_pack.related)
        pack.divergent = convert_layer(rust_pack.divergent)
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
        pack.audit_metadata.setdefault("engine_mode", "snapshot")

        if hasattr(rust_pack, "pipeline_stats") and rust_pack.pipeline_stats:
            pack.pipeline_stats = dict(rust_pack.pipeline_stats)
        if hasattr(rust_pack, "per_item_stats") and rust_pack.per_item_stats:
            pack.per_item_stats = [dict(row) for row in rust_pack.per_item_stats]
        if hasattr(rust_pack, "channel_rankings") and rust_pack.channel_rankings:
            pack.channel_rankings = {
                str(channel): [
                    {
                        "memory_id": str(dict(row).get("memory_id") or ""),
                        "score": float(dict(row).get("score") or 0.0),
                        "rank": int(dict(row).get("rank") or 0),
                    }
                    for row in rows
                ]
                for channel, rows in dict(rust_pack.channel_rankings).items()
            }
        if hasattr(rust_pack, "channel_states") and rust_pack.channel_states:
            pack.channel_states = {
                str(channel): {
                    key: (
                        value == "true"
                        if key
                        in {
                            "planned",
                            "enabled",
                            "available",
                            "executed",
                            "participating",
                            "evidence_only",
                        }
                        else int(value)
                        if key == "result_count"
                        else value
                    )
                    for key, value in dict(state).items()
                }
                for channel, state in dict(rust_pack.channel_states).items()
            }

        fusion_json = pack.audit_metadata.pop("retrieval_fusion_json", "")
        if fusion_json:
            try:
                pack.audit_metadata["retrieval_fusion"] = json.loads(fusion_json)
            except json.JSONDecodeError as exc:
                raise _RustFusionFallback("rust_fusion_audit_invalid") from exc

        return pack

    def _supply_rust(
        self,
        task_description: str,
        task_vector: list,
        task_type: str,
        scope: str,
        *,
        project_id: str = "project:legacy-global",
        project_policy: str = "balanced",
        project_degraded: bool = False,
        fusion_config: FusionConfig | None = None,
    ) -> ContextPack:
        """Rust-accelerated supply path.

        Current mode is snapshot-fed: Python owns storage and passes a
        call-time memory/vector snapshot into Rust. Rust owns deterministic
        ranking math for this opt-in path, while Python remains the write
        authority and fallback implementation.
        """
        self._refresh_canonical_cache_if_changed()
        with self._write_lock:
            snapshot_decision = self._gate_memory_ids(list(self._memories))
            admitted_snapshot_ids = set(snapshot_decision.items)
            if snapshot_decision.admitted_synthesis_ids:
                # Rust intentionally ranks ordinary snapshot rows only. Preserve
                # SQLite-approved synthesis by returning to the Python authority
                # before either snapshot bodies or derived vectors are read.
                raise _RustSynthesisFallback("admitted_governed_synthesis")
            memories = [
                dict(self._memories[mid]) for mid in self._memories if mid in admitted_snapshot_ids
            ]

        from context_engine_core import ContextEngine as RustEngine

        rust = self._new_rust_engine(RustEngine)
        rust.set_current_time(datetime.datetime.now().isoformat())

        # Enrich only admitted rows. Derived vector state never decides admission.
        vector_lookup: dict[str, list[float]] = {}
        if self._ldb and admitted_snapshot_ids:
            get_vectors = getattr(self._ldb, "get_vectors", None)
            if callable(get_vectors):
                try:
                    vector_lookup = {
                        str(memory_id): list(vector)
                        for memory_id, vector in get_vectors(admitted_snapshot_ids).items()
                        if vector and len(vector) == 1024
                    }
                except Exception:
                    vector_lookup = {}
            else:
                # Compatibility for alternate stores that only expose single-row lookup.
                get_vector = getattr(self._ldb, "get_vector", None)
                if callable(get_vector):
                    for memory_id in admitted_snapshot_ids:
                        try:
                            vector = get_vector(memory_id)
                        except Exception:
                            continue
                        if vector and len(vector) == 1024:
                            vector_lookup[memory_id] = list(vector)
        for memory in memories:
            memory["_vector"] = vector_lookup.get(str(memory.get("id", "")), [])

        fusion_config_json = None
        if fusion_config is not None:
            fusion_config_json = json.dumps(
                {
                    "k": fusion_config.k,
                    "channels": list(fusion_config.channels),
                    "weights": dict(fusion_config.weights),
                    "windows": dict(fusion_config.windows),
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        if hasattr(rust, "supply_with_project_context"):
            try:
                rust_pack = rust.supply_with_project_context(
                    task_description,
                    task_vector,
                    task_type,
                    scope,
                    memories,
                    project_id,
                    project_policy,
                    project_degraded,
                    fusion_config_json,
                )
            except TypeError as exc:
                if fusion_config is not None:
                    raise _RustFusionFallback(
                        "rust_capability_missing:fusion_config_boundary"
                    ) from exc
                rust_pack = rust.supply_with_project_context(
                    task_description,
                    task_vector,
                    task_type,
                    scope,
                    memories,
                    project_id,
                    project_policy,
                    project_degraded,
                )
        else:
            if fusion_config is not None:
                raise _RustFusionFallback("rust_capability_missing:fusion_config_boundary")
            rust_pack = rust.supply(
                task_description,
                task_vector,
                task_type,
                scope,
                memories,
            )
        pack = self._convert_rust_pack(rust_pack)
        if snapshot_decision.degradations:
            pack.audit_metadata["synthesis_retrieval"] = {
                "degradations": list(snapshot_decision.degradations)
            }
        return pack

    # ========== 内部方法 ==========

    def _inject_activated_to_graph(self, activated_names: list[str], task_type: str) -> int:
        with self._write_lock:
            return self._inject_activated_to_graph_locked(activated_names, task_type)

    def _inject_activated_to_graph_locked(self, activated_names: list[str], task_type: str) -> int:
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

    def _text_retrieval(
        self,
        task: str,
        trust_boost: float = 1.0,
        domain_hint: str | None = None,
    ) -> list[tuple]:
        """BM25 text retrieval with IDF weighting (Okapi BM25, k1=1.2, b=0.75).

        Replaces the old word-overlap matching. Builds document frequency table
        from self._memories on each call — fast enough at 192-doc scale (<5ms).
        """
        results = []
        query_terms = ContextEngine._tokenize(task)
        if not query_terms:
            return results

        current_owner = os.environ.get("AGENT_OWNER", "")
        dm = getattr(self, "_dm", None)
        has_dm = dm is not None and domain_hint and domain_hint != "all"
        hint_dm = dm.domains.get(domain_hint) if has_dm else None

        # --- Build DF table and pre-tokenize docs ---
        doc_terms: dict[str, list[str]] = {}
        doc_freq: dict[str, int] = {}
        eligible: list[str] = []

        visible_ids = self._public_memory_ids(self._memories)
        for mid in visible_ids:
            mem = self._memories.get(mid)
            if mem is None:
                continue
            mem_owner = mem.get("owner", "")
            if current_owner and mem_owner not in (current_owner, "shared", ""):
                continue
            if ContextEngine._is_forgotten_memory(mem):
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

        # Deferred access tracking. Governed synthesis is immutable through this
        # ordinary read-path telemetry, and promotions publish only after the
        # canonical write succeeds.
        with self._write_lock:
            for mid, _, _, _ in results:
                if self._synthesis_memory_reserved(mid):
                    continue
                current = self._memories.get(mid)
                if current is None:
                    continue
                candidate = dict(current)
                candidate["access_count"] = candidate.get("access_count", 0) + 1
                if candidate["access_count"] >= 5:
                    candidate["worth_success"] = candidate.get("worth_success", 0) + 1
                candidate = self._maybe_adjust_tier(mid, candidate)
                self._memories[mid] = candidate

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def _vector_retrieval(
        self,
        task_vector: list[float],
        scope: str | None = None,
        limit: int | None = None,
    ) -> list[tuple]:
        """Semantic vector retrieval via LanceDB ANN search.

        Falls back to empty list if LanceDB is unavailable.
        """
        if self._ldb is None:
            return []
        try:
            raw_results = self._ldb.search(
                vector=task_vector,
                k=limit or 20,
                scope=scope,
            )
            return self._hydrate_ranked_memory_ids(
                raw_results,
                retrieval_source="vector",
            )[0]
        except Exception as e:
            logging.warning("_vector_retrieval LanceDB failed, returning empty: %s", e)
            return []

    def _fts_retrieval(
        self,
        query: str,
        scope: str = "global",
        limit: int | None = None,
    ) -> list[tuple]:
        """LanceDB full-text search retrieval channel.

        Uses the native LanceDB FTS index on the 'text' column.
        Returns results in the standard 4-element tuple format.

        Gated by PP_FTS_FUSION env var (default on). Skipped entirely
        when PP_FTS_DISABLED=1 or LanceDB is unavailable.
        """
        if os.environ.get("PP_FTS_DISABLED", "") == "1":
            return []
        if os.environ.get("PP_FTS_FUSION", "1") != "1":
            return []
        if self._ldb is None:
            return []
        try:
            fts_scope = scope if scope and scope != "global" else None
            raw_results = self._ldb.fts_search(query, k=limit or 20, scope=fts_scope)
            return self._hydrate_ranked_memory_ids(
                raw_results,
                retrieval_source="fts",
            )[0]
        except Exception as e:
            logging.warning("_fts_retrieval LanceDB failed, returning empty: %s", e)
            return []

    def _hybrid_fuse(
        self,
        vector_results: list[tuple],
        text_results: list[tuple],
        vector_weight: float = 0.7,
        fts_results: list[tuple] | None = None,
    ) -> list[tuple]:
        """Fuse vector, text, and optional FTS retrieval results with weighted combination.

        Formula: fusedScore = vectorScore * vector_weight + textScore * (1-vector_weight)
        BM25 high-score bypass: if text score >= 0.75, promote via 0.9 weight.
        FTS channel shares the text weight with BM25 and gets its own preservation floor.

        Args:
            vector_results: [(id, score, content, source), ...] from LanceDB.
            text_results: [(id, score, content, source), ...] from _text_retrieval.
            vector_weight: Weight for vector scores (default 0.7).
            fts_results: Optional [(id, score, content, source), ...] from _fts_retrieval.

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

        # FTS channel: same text_weight as BM25, with preservation floor
        if fts_results:
            for mid, score, content, source in fts_results:
                w = score * text_weight
                # FTS high-score bypass: >= 0.85 keeps full score (no dilution)
                if score >= 0.85:
                    w = score
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
        graph_nodes, graph_edges = self._public_graph_snapshot()

        # Pass 1: task_type → principles — must run first to populate principle_nodes
        for edge in graph_edges:
            src = edge.get("from", "")
            if src == target:
                dst = edge.get("to", "")
                visited.add(dst)
                if dst.startswith("principle:"):
                    principle_nodes.add(dst)
                node = graph_nodes.get(dst, {})
                results.append(
                    (dst, edge.get("weight", 0.5), node.get("description", dst), "graph")
                )

        # Pass 2: references + governs — now principle_nodes is fully populated
        for edge in graph_edges:
            rel = edge.get("relation", "")
            src = edge.get("from", "")
            dst = edge.get("to", "")

            if rel == "references":
                if dst in visited and src in self._memories:
                    mem = self._memories[src]
                    results.append((src, 0.6, mem.get("content", "")[:300], "entity-link"))
            elif rel == "governs" and src in principle_nodes and dst in self._memories:
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

    @staticmethod
    def _source_penalty_for(source: str) -> float | None:
        """Return source penalty multiplier, or None when source is excluded."""
        if os.environ.get("PP_SOURCE_FILTER", "1" if PP_SOURCE_FILTER else "0") != "1":
            return 1.0
        if not source or source == "unknown":
            return 1.0
        excludes = set(SOURCE_EXCLUDE)
        excludes.update(
            s.strip() for s in os.environ.get("PP_SOURCE_EXCLUDE", "").split(",") if s.strip()
        )
        if source in excludes:
            return None
        downweight = dict(SOURCE_DOWNWEIGHT)
        downweight.update(
            {
                "maintenance_daemon": float(os.environ.get("PP_SOURCE_DAEMON_WEIGHT", "0.3")),
                "superpowers": float(os.environ.get("PP_SOURCE_SUPERPOWERS_WEIGHT", "0.3")),
                "step-closure": float(os.environ.get("PP_SOURCE_STEP_CLOSURE_WEIGHT", "0.3")),
                "step_closure": float(os.environ.get("PP_SOURCE_STEP_CLOSURE_WEIGHT", "0.3")),
                "step_auditor": float(os.environ.get("PP_SOURCE_STEP_AUDITOR_WEIGHT", "0.3")),
                "skill_session": float(os.environ.get("PP_SOURCE_SKILL_SESSION_WEIGHT", "0.1")),
                "auto_context_inject": float(os.environ.get("PP_SOURCE_AUTO_INJECT_WEIGHT", "0.3")),
                "auto_inject": float(os.environ.get("PP_SOURCE_AUTO_INJECT_WEIGHT", "0.3")),
            }
        )
        return downweight.get(source, 1.0)

    @staticmethod
    def _is_recall_noise(content: str) -> bool:
        """Return True for low-information memories that should not enter recall."""
        try:
            from plastic_promise.core.noise_filter import is_noise

            return is_noise(content)
        except Exception:
            return False

    @staticmethod
    def _is_forgotten_memory(mem: dict | None) -> bool:
        """Return True for soft-deleted records that must not enter recall."""
        if not mem:
            return False
        tags = mem.get("tags", []) or []
        if isinstance(tags, str):
            try:
                parsed_tags = json.loads(tags)
                tags = parsed_tags if isinstance(parsed_tags, list) else [tags]
            except Exception:
                tags = [tags]
        return bool(set(tags) & {"status:forgotten", "status:deleted", "decay:pending"})

    @staticmethod
    def _calc_worth_score_from_memory(mem: dict | None) -> float:
        """Return the surfaced worth score from counters when no field exists."""
        if not mem:
            return 0.0
        explicit = mem.get("worth_score", None)
        if explicit is not None:
            try:
                return float(explicit)
            except (TypeError, ValueError):
                pass
        ws = mem.get("worth_success", 0)
        wf = mem.get("worth_failure", 0)
        total = ws + wf
        return (ws + 1.0) / (total + 2.0) if total > 0 else 0.5

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
        with self._write_lock:
            self._apply_edge_feedback_locked()

    @staticmethod
    def _edge_feedback_memory_id(edge: dict[str, Any]) -> str | None:
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source and (
            source.startswith("memory:")
            or not source.startswith("principle:")
            and not source.startswith("task_type:")
        ):
            return source
        if target and (
            target.startswith("memory:")
            or not target.startswith("principle:")
            and not target.startswith("task_type:")
        ):
            return target
        return None

    @staticmethod
    def _edge_feedback_key(edge: dict[str, Any]) -> tuple[str, ...]:
        return (
            str(edge.get("id") or ""),
            str(edge.get("from") or ""),
            str(edge.get("relation") or ""),
            str(edge.get("to") or ""),
            str(edge.get("source_kind") or ""),
            str(edge.get("evidence_id") or ""),
        )

    def _feedback_adjusted_edge_weight(self, edge: dict[str, Any], worth: float) -> float:
        from plastic_promise.core.constants import (
            FEEDBACK_EDGE_EMA_ALPHA,
            FEEDBACK_EDGE_WEIGHT_MAX,
            FEEDBACK_EDGE_WEIGHT_MIN,
        )

        key = self._edge_feedback_key(edge)
        base_weight = self._edge_feedback_base_weights.setdefault(
            key,
            float(edge.get("weight", 0.5)),
        )
        adjusted = (1.0 - FEEDBACK_EDGE_EMA_ALPHA) * base_weight
        adjusted += FEEDBACK_EDGE_EMA_ALPHA * worth
        return max(FEEDBACK_EDGE_WEIGHT_MIN, min(FEEDBACK_EDGE_WEIGHT_MAX, adjusted))

    def _apply_edge_feedback_locked(self):
        """P2: Evolve graph edge weights based on memory adoption patterns.

        For each edge whose relation involves memories (governs, embodies, references),
        update the edge weight via EMA: new_weight = (1-α)*old_weight + α*worth_score.
        Clamp to [FEEDBACK_EDGE_WEIGHT_MIN, FEEDBACK_EDGE_WEIGHT_MAX].

        Called at the end of each supply() call — lightweight and reactive.
        """
        memory_relations = {"governs", "embodies", "references"}

        for edge in self._graph_edges:
            if edge.get("relation") not in memory_relations:
                continue

            memory_id = self._edge_feedback_memory_id(edge)

            if memory_id and memory_id in self._memories:
                mem = self._memories[memory_id]
                ws = mem.get("worth_success", 0)
                wf = mem.get("worth_failure", 0)
                total = ws + wf
                if total <= 0:
                    continue
                worth = (ws + 1.0) / (total + 2.0) if total > 0 else 0.5
                edge["weight"] = self._feedback_adjusted_edge_weight(edge, worth)

    def _apply_edge_feedback_for_memory(self, memory_id: str):
        with self._write_lock:
            self._apply_edge_feedback_for_memory_locked(memory_id)

    def _apply_edge_feedback_for_memory_locked(self, memory_id: str):
        """P2: Update all graph edges involving a specific memory.

        Called after handle_feedback_apply() updates a MemoryRecord's worth counters.
        Only recomputes edges connected to the given memory_id — O(E) but focused.
        """
        if memory_id not in self._memories:
            return

        mem = self._memories[memory_id]
        ws = mem.get("worth_success", 0)
        wf = mem.get("worth_failure", 0)
        total = ws + wf
        if total <= 0:
            return
        worth = (ws + 1.0) / (total + 2.0) if total > 0 else 0.5

        memory_relations = {"governs", "embodies", "references"}
        for edge in self._graph_edges:
            if edge.get("relation") not in memory_relations:
                continue
            if edge.get("from") == memory_id or edge.get("to") == memory_id:
                edge["weight"] = self._feedback_adjusted_edge_weight(edge, worth)

    def _reapply_canonical_edge_feedback(self) -> None:
        self._edge_feedback_base_weights = {}
        observed: dict[str, float] = {}
        for memory_id, memory in self._memories.items():
            worth_success = float(memory.get("worth_success") or 0)
            worth_failure = float(memory.get("worth_failure") or 0)
            total = worth_success + worth_failure
            if total <= 0:
                continue
            worth = (worth_success + 1.0) / (total + 2.0)
            observed[memory_id] = worth

        memory_relations = {"governs", "embodies", "references"}
        for edge in self._graph_edges:
            if edge.get("relation") not in memory_relations:
                continue
            memory_id = self._edge_feedback_memory_id(edge)
            if memory_id not in observed:
                continue
            worth = observed[memory_id]
            edge["weight"] = self._feedback_adjusted_edge_weight(edge, worth)

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


def _ensure_memory_version_schema(conn) -> None:
    """Migrate legacy version rows to one constrained singleton without committing."""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_version'"
    ).fetchone()
    if table_exists is None:
        conn.execute(
            """
            CREATE TABLE memory_version (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                version INTEGER NOT NULL
                    CHECK(typeof(version) = 'integer' AND version >= 0)
            )
            """
        )
        conn.execute("INSERT INTO memory_version (singleton, version) VALUES (1, 0)")
        return

    columns = conn.execute("PRAGMA table_info(memory_version)").fetchall()
    column_names = {str(row[1]) for row in columns}
    if "version" not in column_names:
        raise ValueError("memory_version_invalid")
    rows = conn.execute("SELECT version, typeof(version) FROM memory_version").fetchall()
    for version, storage_type in rows:
        if storage_type != "integer" or type(version) is not int or version < 0:
            raise ValueError("memory_version_invalid")
    canonical_version = max((row[0] for row in rows), default=0)

    if "singleton" in column_names:
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memory_version'"
        ).fetchone()
        normalized_sql = "".join(str(table_sql_row[0] or "").casefold().split())
        singleton_rows = conn.execute("SELECT singleton, version FROM memory_version").fetchall()
        singleton_column = next(row for row in columns if row[1] == "singleton")
        if (
            singleton_rows == [(1, canonical_version)]
            and int(singleton_column[5]) == 1
            and "check(singleton=1)" in normalized_sql
            and "check(typeof(version)='integer'andversion>=0)" in normalized_sql
        ):
            return

    migration_name = "memory_version_legacy_migration"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (migration_name,),
    ).fetchone():
        raise RuntimeError("memory_version_migration_conflict")

    conn.execute("SAVEPOINT ensure_memory_version_schema")
    try:
        conn.execute(f"ALTER TABLE memory_version RENAME TO {migration_name}")
        conn.execute(
            """
            CREATE TABLE memory_version (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                version INTEGER NOT NULL
                    CHECK(typeof(version) = 'integer' AND version >= 0)
            )
            """
        )
        conn.execute(
            "INSERT INTO memory_version (singleton, version) VALUES (1, ?)",
            (canonical_version,),
        )
        conn.execute(f"DROP TABLE {migration_name}")
        conn.execute("RELEASE SAVEPOINT ensure_memory_version_schema")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ensure_memory_version_schema")
        conn.execute("RELEASE SAVEPOINT ensure_memory_version_schema")
        raise


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
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
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
        self._batch_rollback_only = False
        self._batch_owns_transaction = False
        self._batch_savepoint = ""

        # 迁移: 新增 tags 和 domain 列 (SQLite ALTER TABLE 不支持 IF NOT EXISTS)
        with contextlib.suppress(Exception):
            self._conn.execute("ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        with contextlib.suppress(Exception):
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN domain TEXT NOT NULL DEFAULT 'uncategorized'"
            )
        # 迁移: 新增 decay_multiplier 和 effective_half_life 列
        with contextlib.suppress(Exception):
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN decay_multiplier REAL NOT NULL DEFAULT 1.0"
            )
        with contextlib.suppress(Exception):
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN effective_half_life REAL NOT NULL DEFAULT 3.0"
            )
        # 迁移: 新增 last_accessed 列 (Fix: pipeline writes to this column for decay tracking)
        with contextlib.suppress(Exception):
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN last_accessed TEXT NOT NULL DEFAULT ''"
            )

        def add_column(name: str, ddl: str) -> None:
            with contextlib.suppress(Exception):
                self._conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")

        add_column("project_id", "TEXT NOT NULL DEFAULT 'project:legacy-global'")
        add_column("visibility", "TEXT NOT NULL DEFAULT 'project'")
        add_column("source_class", "TEXT NOT NULL DEFAULT 'experience'")
        add_column("created_by_call_id", "TEXT NOT NULL DEFAULT ''")
        add_column("origin_kind", "TEXT NOT NULL DEFAULT ''")
        add_column("origin_uri", "TEXT NOT NULL DEFAULT ''")
        add_column("origin_ref", "TEXT NOT NULL DEFAULT ''")
        add_column("origin_hash", "TEXT NOT NULL DEFAULT ''")
        add_column("parent_memory_ids", "TEXT NOT NULL DEFAULT '[]'")
        add_column("metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        add_column("raw_content", "TEXT NOT NULL DEFAULT ''")
        add_column("l0_abstract", "TEXT NOT NULL DEFAULT ''")
        add_column("l1_summary", "TEXT NOT NULL DEFAULT ''")
        add_column("l2_content", "TEXT NOT NULL DEFAULT ''")
        add_column("embedding_text", "TEXT NOT NULL DEFAULT ''")
        add_column("embedding_hash", "TEXT NOT NULL DEFAULT ''")
        add_column("search_text", "TEXT NOT NULL DEFAULT ''")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS projects ("
            "project_id TEXT PRIMARY KEY,"
            "display_name TEXT NOT NULL,"
            "root_uri TEXT,"
            "aliases_json TEXT NOT NULL DEFAULT '[]',"
            "default_visibility TEXT NOT NULL DEFAULT 'project',"
            "created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL,"
            "metadata_json TEXT NOT NULL DEFAULT '{}'"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS behavior_graph_nodes ("
            "id TEXT PRIMARY KEY,"
            "node_type TEXT NOT NULL,"
            "name TEXT NOT NULL DEFAULT '',"
            "description TEXT NOT NULL DEFAULT '',"
            "source_kind TEXT NOT NULL DEFAULT '',"
            "metadata_json TEXT NOT NULL DEFAULT '{}',"
            "schema_version TEXT NOT NULL DEFAULT 'behavior-graph/v1',"
            "updated_at TEXT NOT NULL"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS behavior_graph_edges ("
            "id TEXT PRIMARY KEY,"
            "source TEXT NOT NULL,"
            "target TEXT NOT NULL,"
            "relation TEXT NOT NULL,"
            "weight REAL NOT NULL DEFAULT 0.5,"
            "source_kind TEXT NOT NULL DEFAULT '',"
            "evidence_id TEXT NOT NULL DEFAULT '',"
            "metadata_json TEXT NOT NULL DEFAULT '{}',"
            "schema_version TEXT NOT NULL DEFAULT 'behavior-graph/v1',"
            "updated_at TEXT NOT NULL,"
            "UNIQUE(source, target, relation)"
            ")"
        )
        from plastic_promise.core.memory_proposals import ensure_memory_proposal_schema
        from plastic_promise.core.synthesis import ensure_synthesis_schema
        from plastic_promise.core.traceability import ensure_traceability_schema

        ensure_traceability_schema(self._conn)
        ensure_synthesis_schema(self._conn)
        ensure_memory_proposal_schema(self._conn)
        # 迁移: memory_version 表 — Rust 引擎用版本号检测 BM25 索引是否需要刷新
        try:
            _ensure_memory_version_schema(self._conn)
        except ValueError:
            logger.warning("Invalid legacy memory_version state preserved; synthesis fails closed")
        self._conn.commit()

        # 存量迁移: 对已有记忆一次性计算真实衰减值
        try:
            from plastic_promise.core.decay_engine import WeibullDecayCalculator
            from plastic_promise.core.synthesis_retrieval import (
                increment_memory_version_if_present,
                ordinary_memory_sql_predicate,
            )

            decay_calc = WeibullDecayCalculator()
            now = datetime.datetime.now().isoformat()
            ordinary_guard = ordinary_memory_sql_predicate("memories")
            rows = self._conn.execute(
                "SELECT id, tier, created_at FROM memories "
                f"WHERE decay_multiplier = 1.0 AND {ordinary_guard}"
            ).fetchall()
            if rows:
                updated = 0
                for row in rows:
                    mid, tier, created_at = row
                    dm = decay_calc.compute_decay(
                        tier=tier or "L1",
                        created_at=created_at or now,
                        current_time_str=now,
                    )
                    cursor = self._conn.execute(
                        "UPDATE memories SET decay_multiplier = ? "
                        f"WHERE id = ? AND {ordinary_guard}",
                        (dm, mid),
                    )
                    updated += cursor.rowcount
                if updated:
                    increment_memory_version_if_present(self._conn)
                self._conn.commit()
                logging.info("Bulk decay migration: %d memories updated", updated)
        except Exception as e:
            self._conn.rollback()
            logging.warning("Bulk decay migration skipped: %s", e)

    def _rollback_quietly(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.rollback()

    def _rollback_savepoint_quietly(self, savepoint: str) -> None:
        with contextlib.suppress(Exception):
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        with contextlib.suppress(Exception):
            self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")

    def _commit_or_rollback(self) -> None:
        try:
            self._conn.commit()
        except BaseException:
            self._rollback_quietly()
            raise

    def _execute_write(
        self,
        sql: str,
        params: tuple = (),
        *,
        bump_memory_version: bool = False,
        bump_only_if_changed: bool = False,
    ):
        try:
            cursor = self._conn.execute(sql, params)
            changed = max(0, int(cursor.rowcount)) > 0
            if bump_memory_version and (not bump_only_if_changed or changed):
                self._increment_memory_version()
            if self._batch_depth <= 0:
                self._commit_or_rollback()
            return cursor
        except BaseException:
            if self._batch_depth <= 0:
                self._rollback_quietly()
            else:
                self._batch_rollback_only = True
            raise

    def _begin_batch_scope(self) -> None:
        if self._batch_depth == 0:
            self._batch_rollback_only = False
            self._batch_owns_transaction = not self._conn.in_transaction
            self._batch_savepoint = ""
            try:
                if self._batch_owns_transaction:
                    self._conn.execute("BEGIN IMMEDIATE")
                else:
                    self._batch_savepoint = (
                        f"storage_batch_{threading.get_ident()}_{time.time_ns()}"
                    )
                    self._conn.execute(f"SAVEPOINT {self._batch_savepoint}")
            except BaseException:
                self._batch_owns_transaction = False
                self._batch_savepoint = ""
                raise
        self._batch_depth += 1

    def _end_batch_scope(self, failed: bool) -> None:
        if self._batch_depth <= 0:
            raise RuntimeError("storage_batch_not_active")
        if failed:
            self._batch_rollback_only = True
        self._batch_depth -= 1
        if self._batch_depth > 0:
            return

        owns_transaction = self._batch_owns_transaction
        savepoint = self._batch_savepoint
        rollback_only = self._batch_rollback_only
        rejected_commit = rollback_only and not failed
        try:
            if rollback_only:
                if owns_transaction:
                    self._rollback_quietly()
                else:
                    self._rollback_savepoint_quietly(savepoint)
            elif owns_transaction:
                self._commit_or_rollback()
            else:
                try:
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException:
                    self._rollback_savepoint_quietly(savepoint)
                    raise
            if rejected_commit:
                raise RuntimeError("storage_batch_rollback_only")
        finally:
            self._batch_depth = 0
            self._batch_rollback_only = False
            self._batch_owns_transaction = False
            self._batch_savepoint = ""

    def upsert(self, mid: str, data: dict) -> bool:
        """Insert or update a memory record without lifecycle ownership checks."""
        return self._upsert(mid, data, ordinary_only=False)

    def upsert_ordinary(self, mid: str, data: dict) -> bool:
        """Compatibility creation facade for an ordinary canonical binding."""
        self.create_ordinary_if_absent(mid, data)
        return True

    @staticmethod
    def _ordinary_write_payload(mid: str, data: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple]:
        metadata_json = data.get("metadata_json", {})
        if isinstance(metadata_json, str):
            try:
                metadata_json = json.loads(metadata_json) if metadata_json.strip() else {}
            except (TypeError, json.JSONDecodeError):
                metadata_json = {}
        if not isinstance(metadata_json, dict):
            metadata_json = {}

        def summary_field(name: str) -> str:
            value = data.get(name, "")
            if value:
                return str(value)
            return str(metadata_json.get(name, "") or "")

        columns = (
            "id",
            "content",
            "memory_type",
            "source",
            "owner",
            "tier",
            "scope",
            "category",
            "tags",
            "domain",
            "importance",
            "entity_ids",
            "created_at",
            "access_count",
            "worth_success",
            "worth_failure",
            "activation_weight",
            "decay_multiplier",
            "effective_half_life",
            "last_accessed",
            "project_id",
            "visibility",
            "source_class",
            "created_by_call_id",
            "origin_kind",
            "origin_uri",
            "origin_ref",
            "origin_hash",
            "parent_memory_ids",
            "metadata_json",
            "raw_content",
            "l0_abstract",
            "l1_summary",
            "l2_content",
            "embedding_text",
            "embedding_hash",
            "search_text",
        )
        values = (
            mid,
            data.get("content", ""),
            data.get("memory_type", "experience"),
            data.get("source", "user"),
            data.get("owner", ""),
            data.get("tier", "L1"),
            data.get("scope", "global"),
            data.get("category", "other"),
            json.dumps(data.get("tags", []), ensure_ascii=False),
            data.get("domain", "uncategorized"),
            data.get("importance", 0.7),
            json.dumps(data.get("entity_ids", []), ensure_ascii=False),
            data.get("created_at", ""),
            data.get("access_count", 0),
            data.get("worth_success", 0),
            data.get("worth_failure", 0),
            data.get("activation_weight", 0.5),
            data.get("decay_multiplier", 1.0),
            data.get("effective_half_life", 3.0),
            data.get("last_accessed", ""),
            data.get("project_id", "project:legacy-global"),
            data.get("visibility", "project"),
            data.get("source_class", "experience"),
            data.get("created_by_call_id", ""),
            data.get("origin_kind", ""),
            data.get("origin_uri", ""),
            data.get("origin_ref", ""),
            data.get("origin_hash", ""),
            json.dumps(data.get("parent_memory_ids", []), ensure_ascii=False),
            json.dumps(metadata_json, ensure_ascii=False),
            summary_field("raw_content"),
            summary_field("l0_abstract"),
            summary_field("l1_summary"),
            summary_field("l2_content"),
            summary_field("embedding_text"),
            summary_field("embedding_hash"),
            summary_field("search_text"),
        )
        return columns, values

    @staticmethod
    def _canonical_binding(columns: tuple[str, ...], values: tuple) -> tuple[tuple[str, Any], ...]:
        json_columns = {"tags", "entity_ids", "parent_memory_ids", "metadata_json"}
        binding: list[tuple[str, Any]] = []
        for column, value in zip(columns, values, strict=True):
            if column in json_columns:
                try:
                    parsed = json.loads(value) if isinstance(value, str) else value
                    value = json.dumps(
                        parsed,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise OrdinaryMemoryConflict("ordinary_memory_binding_invalid") from exc
            binding.append((column, value))
        return tuple(binding)

    def create_ordinary_if_absent(
        self,
        mid: str,
        data: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Create once and compare every persisted field on replay."""
        from plastic_promise.core.synthesis_retrieval import is_governed_synthesis_memory

        mid = str(mid or "").strip()
        if not mid:
            raise OrdinaryMemoryConflict("ordinary_memory_id_required")
        columns, values = self._ordinary_write_payload(mid, data)
        requested_type = str(values[columns.index("memory_type")] or "").strip().casefold()
        if requested_type == "synthesis":
            raise OrdinaryMemoryConflict("ordinary_memory_reserved")
        expected_binding = self._canonical_binding(columns, values)
        column_sql = ", ".join(columns)
        placeholders = ",".join("?" for _column in columns)
        sql = (
            f"INSERT INTO memories ({column_sql}) SELECT {placeholders} "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM synthesis_artifacts WHERE memory_id = ?"
            ") ON CONFLICT(id) DO NOTHING"
        )

        with self.batch():
            cursor = self._execute_write(
                sql,
                (*values, mid),
                bump_memory_version=True,
                bump_only_if_changed=True,
            )
            created = max(0, int(cursor.rowcount)) > 0
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            row = self._conn.execute(
                f"SELECT {quoted_columns} FROM memories WHERE id = ?",  # noqa: S608
                (mid,),
            ).fetchone()
            if row is None or is_governed_synthesis_memory(
                self._conn,
                mid,
                memory_type=row[columns.index("memory_type")] if row else None,
            ):
                raise OrdinaryMemoryConflict("ordinary_memory_reserved")
            if not created and self._canonical_binding(columns, tuple(row)) != expected_binding:
                raise OrdinaryMemoryConflict("ordinary_memory_already_exists")
            canonical = self._row_to_dict(row)
        return canonical, created

    def _upsert(self, mid: str, data: dict, *, ordinary_only: bool) -> bool:
        columns, values = self._ordinary_write_payload(mid, data)
        column_sql = ", ".join(columns)
        placeholders = ",".join("?" for _column in columns)
        params = values
        if ordinary_only:
            from plastic_promise.core.synthesis_retrieval import (
                ordinary_memory_sql_predicate,
            )

            assignments = ", ".join(
                f"{column} = excluded.{column}" for column in columns if column != "id"
            )
            sql = (
                f"INSERT INTO memories ({column_sql}) SELECT {placeholders} "
                "WHERE LOWER(TRIM(COALESCE(?, ''))) <> 'synthesis' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM synthesis_artifacts WHERE memory_id = ?"
                ") ON CONFLICT(id) DO UPDATE SET "
                f"{assignments} WHERE {ordinary_memory_sql_predicate('memories')} "
                "AND LOWER(TRIM(COALESCE(excluded.memory_type, ''))) <> 'synthesis'"
            )
            params = (*values, data.get("memory_type", "experience"), mid)
        else:
            sql = f"INSERT OR REPLACE INTO memories ({column_sql}) VALUES ({placeholders})"
        cursor = self._execute_write(
            sql,
            params,
            bump_memory_version=True,
            bump_only_if_changed=ordinary_only,
        )
        return max(0, int(cursor.rowcount)) > 0

    def _increment_memory_version(self) -> None:
        from plastic_promise.core.synthesis_retrieval import read_memory_version

        try:
            read_memory_version(self._conn)
        except Exception:
            return
        self._conn.execute("UPDATE memory_version SET version = version + 1")

    @staticmethod
    def _ordinary_patch_mapping(
        value: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise OrdinaryMemoryConflict("ordinary_patch_field_not_allowed")
        return dict(value)

    @staticmethod
    def _ordinary_patch_replacement_value(field: str, value: Any) -> Any:
        if field in _ORDINARY_JSON_PATCH_FIELDS:
            try:
                return json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise OrdinaryMemoryConflict("ordinary_patch_value_invalid") from exc
        if field in _ORDINARY_NUMERIC_REPLACEMENT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
            if isinstance(value, float) and not math.isfinite(value):
                raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
        elif isinstance(value, float) and not math.isfinite(value):
            raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
        return value

    def patch_ordinary(
        self,
        mid: str,
        *,
        replacements: Mapping[str, Any] | None = None,
        increments: Mapping[str, int | float] | None = None,
        expected_project_id: str | None = None,
        require_source_available: bool = False,
        expected_tags: list[str] | tuple[str, ...] | None = None,
        expected_category: str | None = None,
        expected_content_hash: str | None = None,
        expected_embedding_hash: str | None = None,
        expected_snapshot: Mapping[str, Any] | None = None,
        bump_memory_version: bool | None = None,
        preserve_source_availability: bool = False,
        after_patch: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Patch exactly one canonical ordinary row without hydrating defaults."""
        replacement_values = self._ordinary_patch_mapping(replacements)
        increment_values = self._ordinary_patch_mapping(increments)
        if not replacement_values and not increment_values:
            raise OrdinaryMemoryConflict("ordinary_patch_empty")

        replacement_fields = set(replacement_values)
        increment_fields = set(increment_values)
        if (
            not replacement_fields <= (_ORDINARY_SCALAR_PATCH_FIELDS | _ORDINARY_JSON_PATCH_FIELDS)
            or not increment_fields <= _ORDINARY_NUMERIC_INCREMENT_FIELDS
        ):
            raise OrdinaryMemoryConflict("ordinary_patch_field_not_allowed")
        if replacement_fields & increment_fields:
            raise OrdinaryMemoryConflict("ordinary_patch_field_conflict")
        if (
            "memory_type" in replacement_values
            and str(replacement_values["memory_type"] or "").strip().casefold() == "synthesis"
        ):
            raise OrdinaryMemoryConflict("ordinary_memory_reserved")
        if bump_memory_version is not None and not isinstance(bump_memory_version, bool):
            raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
        if not isinstance(require_source_available, bool):
            raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
        if not isinstance(preserve_source_availability, bool):
            raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
        if after_patch is not None and not callable(after_patch):
            raise OrdinaryMemoryConflict("ordinary_patch_value_invalid")
        if expected_project_id is not None:
            expected_project_id = str(expected_project_id).strip()
            if not expected_project_id:
                raise OrdinaryMemoryConflict("ordinary_patch_expected_project_required")
        normalized_expected_tags: list[str] | None = None
        if expected_tags is not None:
            if not isinstance(expected_tags, (list, tuple)) or not all(
                isinstance(tag, str) for tag in expected_tags
            ):
                raise OrdinaryMemoryConflict("ordinary_patch_expected_tags_invalid")
            normalized_expected_tags = list(expected_tags)
        expected_scalar_values = self._ordinary_patch_mapping(expected_snapshot)
        if not set(expected_scalar_values) <= (_ORDINARY_SCALAR_PATCH_FIELDS - {"content"}):
            raise OrdinaryMemoryConflict("ordinary_patch_expected_snapshot_invalid")
        compiled_expected_snapshot = {
            field: self._ordinary_patch_replacement_value(field, expected_scalar_values[field])
            for field in _ORDINARY_PATCH_COLUMN_ORDER
            if field in expected_scalar_values
        }

        compiled_replacements = {
            field: self._ordinary_patch_replacement_value(
                field,
                replacement_values[field],
            )
            for field in _ORDINARY_PATCH_COLUMN_ORDER
            if field in replacement_values
        }
        compiled_increments: dict[str, int | float] = {}
        for column in _ORDINARY_PATCH_COLUMN_ORDER:
            if column not in increment_values:
                continue
            value = increment_values[column]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OrdinaryMemoryConflict("ordinary_patch_increment_invalid")
            if isinstance(value, float) and not math.isfinite(value):
                raise OrdinaryMemoryConflict("ordinary_patch_increment_invalid")
            compiled_increments[column] = value

        should_bump = (
            bool(replacement_fields & _RETRIEVAL_VISIBLE_PATCH_FIELDS)
            if bump_memory_version is None
            else bump_memory_version
        )
        from plastic_promise.core.synthesis import synthesis_content_hash
        from plastic_promise.core.synthesis_retrieval import (
            _source_is_available,
            available_ordinary_memory_sql_predicate,
            ordinary_memory_sql_predicate,
        )

        canonical: dict[str, Any] | None = None
        with self.batch():
            target = self._conn.execute(
                "SELECT content, memory_type, embedding_hash, project_id, tags, category, "
                "EXISTS(SELECT 1 FROM synthesis_artifacts "
                "WHERE synthesis_artifacts.memory_id = memories.id) "
                "FROM memories WHERE id = ?",
                (mid,),
            ).fetchone()
            if target is None:
                raise OrdinaryMemoryConflict("ordinary_patch_target_not_found")
            if str(target[1] or "").strip().casefold() == "synthesis" or bool(target[6]):
                raise OrdinaryMemoryConflict("ordinary_memory_reserved")
            if (
                (
                    expected_content_hash is not None
                    and synthesis_content_hash(target[0]) != expected_content_hash
                )
                or (
                    expected_embedding_hash is not None
                    and str(target[2] or "") != expected_embedding_hash
                )
                or (
                    expected_project_id is not None
                    and str(target[3] or "").strip() != expected_project_id
                )
            ):
                raise OrdinaryMemoryConflict("ordinary_patch_cas_mismatch")
            if normalized_expected_tags is not None:
                try:
                    current_tags = json.loads(target[4])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise OrdinaryMemoryConflict("ordinary_patch_cas_mismatch") from exc
                if (
                    not isinstance(current_tags, list)
                    or not all(isinstance(tag, str) for tag in current_tags)
                    or current_tags != normalized_expected_tags
                ):
                    raise OrdinaryMemoryConflict("ordinary_patch_cas_mismatch")
            if expected_category is not None and (str(target[5] or "") != str(expected_category)):
                raise OrdinaryMemoryConflict("ordinary_patch_cas_mismatch")
            current = self.get(mid)
            if current is None or any(
                current.get(field) != expected for field, expected in expected_scalar_values.items()
            ):
                raise OrdinaryMemoryConflict("ordinary_patch_cas_mismatch")
            if require_source_available:
                try:
                    source_available = bool(current is not None and _source_is_available(current))
                except Exception as exc:
                    raise OrdinaryMemoryConflict("ordinary_patch_source_unavailable") from exc
                if not source_available:
                    raise OrdinaryMemoryConflict("ordinary_patch_source_unavailable")
            if preserve_source_availability and (
                replacement_fields & _ORDINARY_AVAILABILITY_PATCH_FIELDS
            ):
                candidate = copy.deepcopy(current)
                candidate.update(copy.deepcopy(replacement_values))
                try:
                    availability_changed = _source_is_available(current) != _source_is_available(
                        candidate
                    )
                except Exception as exc:
                    raise OrdinaryMemoryConflict("ordinary_patch_availability_invalid") from exc
                if availability_changed:
                    raise OrdinaryMemoryConflict(
                        "ordinary_patch_availability_change_requires_coordinator"
                    )

            assignments: list[str] = []
            params: list[Any] = []
            for column in _ORDINARY_PATCH_COLUMN_ORDER:
                if column in compiled_replacements:
                    assignments.append(f"{column} = ?")
                    params.append(compiled_replacements[column])
                elif column in compiled_increments:
                    assignments.append(f"{column} = COALESCE({column}, 0) + ?")
                    params.append(compiled_increments[column])

            predicates = ["id = ?", ordinary_memory_sql_predicate("memories")]
            if require_source_available:
                predicates.append(available_ordinary_memory_sql_predicate("memories"))
            params.append(mid)
            if expected_content_hash is not None:
                predicates.append("content IS ?")
                params.append(target[0])
            if expected_embedding_hash is not None:
                predicates.append("embedding_hash IS ?")
                params.append(target[2])
            if expected_project_id is not None:
                predicates.append("project_id IS ?")
                params.append(target[3])
            if normalized_expected_tags is not None:
                predicates.append("tags IS ?")
                params.append(target[4])
            if expected_category is not None:
                predicates.append("category IS ?")
                params.append(target[5])
            for field, expected in compiled_expected_snapshot.items():
                predicates.append(f"{field} IS ?")
                params.append(expected)
            cursor = self._conn.execute(
                f"UPDATE memories SET {', '.join(assignments)} WHERE {' AND '.join(predicates)}",
                tuple(params),
            )
            if cursor.rowcount != 1:
                reason = (
                    "ordinary_patch_cas_mismatch"
                    if expected_content_hash is not None
                    or expected_embedding_hash is not None
                    or expected_project_id is not None
                    or normalized_expected_tags is not None
                    or expected_category is not None
                    or compiled_expected_snapshot
                    else (
                        "ordinary_patch_source_unavailable"
                        if require_source_available
                        else "ordinary_patch_row_count"
                    )
                )
                raise OrdinaryMemoryConflict(reason)
            if should_bump:
                self._increment_memory_version()
            canonical = self.get(mid)
            if canonical is None:
                raise OrdinaryMemoryConflict("ordinary_patch_row_count")
            if after_patch is not None:
                after_patch(canonical)
        return canonical

    def get(self, mid: str) -> dict | None:
        """Retrieve a single memory record."""
        row = self._conn.execute(
            "SELECT id, content, memory_type, source, owner, tier, scope, category, "
            "tags, domain, importance, entity_ids, created_at, access_count, "
            "worth_success, worth_failure, activation_weight, "
            "decay_multiplier, effective_half_life, last_accessed, "
            "project_id, visibility, source_class, created_by_call_id, origin_kind, "
            "origin_uri, origin_ref, origin_hash, parent_memory_ids, metadata_json, "
            "raw_content, l0_abstract, l1_summary, l2_content, embedding_text, embedding_hash, "
            "search_text "
            "FROM memories WHERE id = ?",
            (mid,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete(self, mid: str) -> bool:
        """Delete a memory record."""
        cursor = self._execute_write(
            "DELETE FROM memories WHERE id = ?",
            (mid,),
            bump_memory_version=True,
            bump_only_if_changed=True,
        )
        return max(0, int(cursor.rowcount)) > 0

    def delete_ordinary(self, mid: str) -> bool:
        """Atomically delete only an ordinary, unreserved memory row."""
        from plastic_promise.core.synthesis_retrieval import ordinary_memory_sql_predicate

        cursor = self._execute_write(
            f"DELETE FROM memories WHERE id = ? AND {ordinary_memory_sql_predicate('memories')}",
            (mid,),
            bump_memory_version=True,
            bump_only_if_changed=True,
        )
        return max(0, int(cursor.rowcount)) > 0

    def iter_all(self):
        """Iterate all memory records."""
        rows = self._conn.execute(
            "SELECT id, content, memory_type, source, owner, tier, scope, category, "
            "tags, domain, importance, entity_ids, created_at, access_count, "
            "worth_success, worth_failure, activation_weight, "
            "decay_multiplier, effective_half_life, last_accessed, "
            "project_id, visibility, source_class, created_by_call_id, origin_kind, "
            "origin_uri, origin_ref, origin_hash, parent_memory_ids, metadata_json, "
            "raw_content, l0_abstract, l1_summary, l2_content, embedding_text, embedding_hash, "
            "search_text "
            "FROM memories"
        ).fetchall()
        for row in rows:
            d = self._row_to_dict(row)
            yield d["id"], d

    def commit(self):
        """Explicit commit (useful after batch operations)."""
        self._commit_or_rollback()

    def upsert_graph_node(self, node_id: str, node: dict) -> bool:
        """Persist a graph node without lifecycle ownership checks."""
        return self._upsert_graph_node(node_id, node, ordinary_only=False)

    def upsert_graph_node_ordinary(
        self,
        node_id: str,
        node: dict,
        *,
        reservation_ids: tuple[str, ...] = (),
    ) -> bool:
        """Atomically persist a node only when no supplied id is governed."""
        return self._upsert_graph_node(
            node_id,
            node,
            ordinary_only=True,
            reservation_ids=reservation_ids,
        )

    def _upsert_graph_node(
        self,
        node_id: str,
        node: dict,
        *,
        ordinary_only: bool,
        reservation_ids: tuple[str, ...] = (),
    ) -> bool:
        import json

        values = (
            node_id,
            node.get("type", ""),
            node.get("name", ""),
            node.get("description", ""),
            node.get("source_kind", ""),
            json.dumps(node.get("metadata", {}), ensure_ascii=False),
            node.get("schema_version", "behavior-graph/v1"),
            datetime.datetime.now().isoformat(),
        )
        params = values
        if ordinary_only:
            guarded_ids = tuple(
                dict.fromkeys(
                    str(candidate) for candidate in (node_id, *reservation_ids) if candidate
                )
            )
            placeholders = ",".join("?" for _candidate in guarded_ids)
            reservation_guard = (
                "NOT EXISTS (SELECT 1 FROM memories AS governed_memory "
                f"WHERE governed_memory.id IN ({placeholders}) "
                "AND LOWER(TRIM(COALESCE(governed_memory.memory_type, ''))) = 'synthesis') "
                "AND NOT EXISTS (SELECT 1 FROM synthesis_artifacts AS governed_control "
                f"WHERE governed_control.memory_id IN ({placeholders}))"
            )
            sql = (
                "INSERT INTO behavior_graph_nodes ("
                "id, node_type, name, description, source_kind, metadata_json, schema_version, "
                "updated_at) SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE "
                f"{reservation_guard} ON CONFLICT(id) DO UPDATE SET "
                "node_type = excluded.node_type, name = excluded.name, "
                "description = excluded.description, source_kind = excluded.source_kind, "
                "metadata_json = excluded.metadata_json, schema_version = excluded.schema_version, "
                "updated_at = excluded.updated_at"
            )
            params = (*values, *guarded_ids, *guarded_ids)
        else:
            sql = (
                "INSERT OR REPLACE INTO behavior_graph_nodes ("
                "id, node_type, name, description, source_kind, metadata_json, schema_version, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            )
        cursor = self._execute_write(
            sql,
            params,
            bump_memory_version=True,
            bump_only_if_changed=ordinary_only,
        )
        return max(0, int(cursor.rowcount)) > 0

    def upsert_graph_edge(self, edge: dict) -> bool:
        """Persist an edge without lifecycle ownership checks."""
        return self._upsert_graph_edge(edge, ordinary_only=False)

    def upsert_graph_edge_ordinary(self, edge: dict) -> bool:
        """Atomically persist an edge only when both endpoints are ordinary."""
        return self._upsert_graph_edge(edge, ordinary_only=True)

    def _upsert_graph_edge(self, edge: dict, *, ordinary_only: bool) -> bool:
        import json

        edge_id = edge.get("id") or f"{edge.get('from')}|{edge.get('relation')}|{edge.get('to')}"
        values = (
            edge_id,
            edge.get("from", ""),
            edge.get("to", ""),
            edge.get("relation", ""),
            float(edge.get("weight", 0.5)),
            edge.get("source_kind", ""),
            edge.get("evidence_id", ""),
            json.dumps(edge.get("metadata", {}), ensure_ascii=False),
            edge.get("schema_version", "behavior-graph/v1"),
            datetime.datetime.now().isoformat(),
        )
        if ordinary_only:
            endpoint_predicate = (
                "NOT EXISTS (SELECT 1 FROM memories AS governed_memory "
                "WHERE governed_memory.id IN ({source}, {target}) "
                "AND LOWER(TRIM(COALESCE(governed_memory.memory_type, ''))) = 'synthesis') "
                "AND NOT EXISTS (SELECT 1 FROM synthesis_artifacts AS governed_control "
                "WHERE governed_control.memory_id IN ({source}, {target}))"
            )
            incoming_guard = endpoint_predicate.format(source="?", target="?")
            existing_guard = endpoint_predicate.format(
                source="behavior_graph_edges.source",
                target="behavior_graph_edges.target",
            )
            excluded_guard = endpoint_predicate.format(
                source="excluded.source",
                target="excluded.target",
            )
            sql = (
                "INSERT INTO behavior_graph_edges ("
                "id, source, target, relation, weight, source_kind, evidence_id, metadata_json, "
                "schema_version, updated_at) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE "
                f"{incoming_guard} ON CONFLICT(id) DO UPDATE SET "
                "source = excluded.source, target = excluded.target, relation = excluded.relation, "
                "weight = excluded.weight, source_kind = excluded.source_kind, "
                "evidence_id = excluded.evidence_id, metadata_json = excluded.metadata_json, "
                "schema_version = excluded.schema_version, updated_at = excluded.updated_at WHERE "
                f"{existing_guard} AND {excluded_guard}"
            )
            params = (
                *values,
                edge.get("from", ""),
                edge.get("to", ""),
                edge.get("from", ""),
                edge.get("to", ""),
            )
        else:
            sql = (
                "INSERT OR REPLACE INTO behavior_graph_edges ("
                "id, source, target, relation, weight, source_kind, evidence_id, metadata_json, "
                "schema_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            params = values
        cursor = self._execute_write(
            sql,
            params,
            bump_memory_version=True,
            bump_only_if_changed=True,
        )
        return max(0, int(cursor.rowcount)) > 0

    def delete_graph_edges(
        self,
        source: str,
        target: str,
        relation: str | None = None,
    ) -> int:
        if relation is None:
            cursor = self._execute_write(
                "DELETE FROM behavior_graph_edges WHERE source = ? AND target = ?",
                (source, target),
                bump_memory_version=True,
                bump_only_if_changed=True,
            )
        else:
            cursor = self._execute_write(
                "DELETE FROM behavior_graph_edges WHERE source = ? AND target = ? AND relation = ?",
                (source, target, relation),
                bump_memory_version=True,
                bump_only_if_changed=True,
            )
        deleted = max(0, int(cursor.rowcount))
        return deleted

    def delete_graph_edges_ordinary(
        self,
        source: str,
        target: str,
        relation: str | None = None,
    ) -> int:
        endpoint_guard = (
            "NOT EXISTS (SELECT 1 FROM memories AS governed_memory "
            "WHERE governed_memory.id IN (behavior_graph_edges.source, "
            "behavior_graph_edges.target) "
            "AND LOWER(TRIM(COALESCE(governed_memory.memory_type, ''))) = 'synthesis') "
            "AND NOT EXISTS (SELECT 1 FROM synthesis_artifacts AS governed_control "
            "WHERE governed_control.memory_id IN (behavior_graph_edges.source, "
            "behavior_graph_edges.target))"
        )
        sql = (
            f"DELETE FROM behavior_graph_edges WHERE source = ? AND target = ? AND {endpoint_guard}"
        )
        params: tuple = (source, target)
        if relation is not None:
            sql += " AND relation = ?"
            params = (source, target, relation)
        cursor = self._execute_write(
            sql,
            params,
            bump_memory_version=True,
            bump_only_if_changed=True,
        )
        return max(0, int(cursor.rowcount))

    def iter_graph_nodes(self):
        rows = self._conn.execute(
            "SELECT id, node_type, name, description, source_kind, metadata_json, schema_version "
            "FROM behavior_graph_nodes"
        ).fetchall()
        for row in rows:
            yield (
                row[0],
                {
                    "type": row[1],
                    "name": row[2],
                    "description": row[3],
                    "source_kind": row[4],
                    "metadata": self._json_dict_or_empty(row[5]),
                    "schema_version": row[6],
                },
            )

    def iter_graph_edges(self):
        rows = self._conn.execute(
            "SELECT id, source, target, relation, weight, source_kind, evidence_id, "
            "metadata_json, schema_version FROM behavior_graph_edges"
        ).fetchall()
        for row in rows:
            yield {
                "id": row[0],
                "from": row[1],
                "to": row[2],
                "relation": row[3],
                "weight": row[4],
                "source_kind": row[5],
                "evidence_id": row[6],
                "metadata": self._json_dict_or_empty(row[7]),
                "schema_version": row[8],
            }

    class _BatchContext:
        """Context manager for batch writes — defers commits until exit."""

        def __init__(self, storage):
            self._storage = storage

        def __enter__(self):
            self._storage._begin_batch_scope()
            return self

        def __exit__(self, exc_type, _exc, _tb):
            self._storage._end_batch_scope(exc_type is not None)
            return False

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

    @staticmethod
    def _json_list_or_empty(value) -> list:
        import json

        if not value:
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _json_dict_or_empty(value) -> dict:
        import json

        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

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
            "project_id": row[20] if len(row) > 20 else "project:legacy-global",
            "visibility": row[21] if len(row) > 21 else "project",
            "source_class": row[22] if len(row) > 22 else "experience",
            "created_by_call_id": row[23] if len(row) > 23 else "",
            "origin_kind": row[24] if len(row) > 24 else "",
            "origin_uri": row[25] if len(row) > 25 else "",
            "origin_ref": row[26] if len(row) > 26 else "",
            "origin_hash": row[27] if len(row) > 27 else "",
            "parent_memory_ids": self._json_list_or_empty(row[28]) if len(row) > 28 else [],
            "metadata_json": self._json_dict_or_empty(row[29]) if len(row) > 29 else {},
            "raw_content": row[30] if len(row) > 30 else "",
            "l0_abstract": row[31] if len(row) > 31 else "",
            "l1_summary": row[32] if len(row) > 32 else "",
            "l2_content": row[33] if len(row) > 33 else "",
            "embedding_text": row[34] if len(row) > 34 else "",
            "embedding_hash": row[35] if len(row) > 35 else "",
            "search_text": row[36] if len(row) > 36 else "",
        }


_SQLiteMemoryStore = _SQLiteStorage
