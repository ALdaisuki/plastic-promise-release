"""ContextEngine Python 回退实现

当 Rust context-engine-core 不可用时使用此纯 Python 版本。
接口与 Rust 版本保持一致，确保上层无感切换。

生产环境应使用 Rust 版本以获得更好性能。
"""

import datetime
import json
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
    """上下文包中的单个条目"""
    id: str
    content: str
    relevance: float
    source: str = ""
    freshness: str = "valid"
    layer: str = "related"
    is_principle: bool = False
    worth_score: float = 0.0

    def to_prompt_line(self) -> str:
        mark = " 🧬" if self.is_principle else ""
        return f"- [{self.relevance:.2f}]{mark} [{self.source}] {self.content[:200]}"


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
            lines.append("## 🧬 激活的核心原则")
            for p in self.activated_principles:
                lines.append(f"- {p}")
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
# ContextEngine — Python 实现
# ============================================================

class ContextEngine:
    """上下文供应引擎 (Python 回退版)

    生产环境请使用 Rust 版: from context_engine_core import ContextEngine
    """

    def __init__(self):
        self._memories: Dict[str, Dict[str, Any]] = {}
        self._graph_nodes: Dict[str, Dict[str, Any]] = {}
        self._graph_edges: List[Dict[str, Any]] = []
        self._feedback: Dict[str, float] = {}  # item_id -> accumulated delta
        self.enable_principles: bool = True
        self._current_time: str = ""

    # ========== 记忆管理 ==========

    def register_memory(self, record: Dict[str, Any]) -> str:
        mid = record.get("id", f"mem_{len(self._memories)}")
        self._memories[mid] = {
            "id": mid,
            "content": record.get("content", ""),
            "memory_type": record.get("memory_type", "experience"),
            "source": record.get("source", "user"),
            "worth_success": record.get("worth_success", 0),
            "worth_failure": record.get("worth_failure", 0),
            "activation_weight": record.get("activation_weight", 0.5),
            "created_at": record.get("created_at", datetime.datetime.now().isoformat()),
        }
        return mid

    def register_memories(self, records: List[Dict[str, Any]]) -> List[str]:
        return [self.register_memory(r) for r in records]

    @property
    def memory_count(self) -> int:
        return len(self._memories)

    def set_current_time(self, iso_timestamp: str):
        self._current_time = iso_timestamp

    # ========== 图管理 ==========

    def load_graph(self, graph_data: Dict[str, Any]):
        self._graph_nodes = graph_data.get("nodes", {})
        self._graph_edges = graph_data.get("edges", [])

    def get_graph(self) -> Dict[str, Any]:
        return {"nodes": self._graph_nodes, "edges": self._graph_edges}

    # ========== 核心方法: supply() ==========

    def supply(
        self,
        task_description: str,
        task_type: str = "general",
        pre_context: str = None,
    ) -> ContextPack:
        """供应上下文"""
        pre_context = pre_context or ""

        # Phase 0: 原则注入
        activated = self._activate_principles(task_type, task_description)

        # Phase 1: 双路检索
        text_results = self._text_retrieval(task_description)
        graph_results = self._graph_traversal(task_type)

        # Phase 2: 简单融合 (取交集/并集，按 score 降序)
        all_results = self._simple_fuse(text_results, graph_results)

        # Phase 3: 符号规则调整
        all_results = self._apply_symbol_rules(all_results, task_description)

        # Phase 4: 反馈权重
        all_results = self._apply_feedback(all_results)

        # Phase 5: 分层
        pack = ContextPack(activated_principles=activated)

        for item_id, score, content, source in all_results:
            is_principle = item_id.startswith("principle:")
            worth = self._memories.get(item_id, {}).get("worth_score", 0.0)
            freshness = self._calc_freshness(item_id)

            item = ContextItem(
                id=item_id,
                content=content,
                relevance=score,
                source=source,
                freshness=freshness,
                is_principle=is_principle,
                worth_score=worth,
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

        # Phase 6: 审计元数据
        pack.audit_metadata = {
            "engine_version": "0.1.0-py",
            "task_type": task_type,
            "principle_injection_count": str(len(activated)),
            "graph_nodes": str(len(self._graph_nodes)),
            "graph_edges": str(len(self._graph_edges)),
            "memory_pool_size": str(len(self._memories)),
        }

        return pack

    # ========== 内部方法 ==========

    def _activate_principles(self, task_type: str, task_description: str) -> List[str]:
        from plastic_promise.core.constants import CORE_PRINCIPLES

        recommendations = {
            "code_generation": [1, 3, 8, 10],
            "code_review": [1, 5, 6, 9],
            "debugging": [1, 5, 10],
            "architecture": [2, 7, 8],
            "refactoring": [5, 6, 7],
            "learning": [1, 10, 11],
            "collaboration": [2, 7, 9],
        }
        ids = recommendations.get(task_type, [1, 2, 3, 4])

        # 关键词额外匹配
        for p in CORE_PRINCIPLES:
            if p["id"] not in ids:
                for kw in p.get("keywords", "").split(","):
                    if kw.strip() in task_description:
                        ids.append(p["id"])
                        break

        result = []
        for p in CORE_PRINCIPLES:
            if p["id"] in ids:
                result.append(p["name"])
        return result

    def _text_retrieval(self, task: str) -> List[tuple]:
        results = []
        task_words = [w for w in task.replace("，", " ").replace("。", " ").split() if len(w) >= 2]
        if not task_words:
            return results

        for mid, mem in self._memories.items():
            score = sum(1.0 for w in task_words if w in mem["content"]) / len(task_words)
            if score > 0:
                results.append((mid, min(score, 1.0), mem["content"][:300], mem["source"]))
        return results

    def _graph_traversal(self, task_type: str) -> List[tuple]:
        results = []
        # 简化版：匹配节点
        target = f"task_type:{task_type}"
        for edge in self._graph_edges:
            if edge.get("from") == target:
                node_id = edge["to"]
                node = self._graph_nodes.get(node_id, {})
                results.append((node_id, edge.get("weight", 0.5),
                                node.get("description", node_id),
                                "graph"))
        return results

    def _simple_fuse(self, text_results, graph_results) -> List[tuple]:
        combined = {}
        for item_id, score, content, source in text_results:
            combined[item_id] = (score, content, source)
        for item_id, score, content, source in graph_results:
            if item_id in combined:
                combined[item_id] = (
                    max(combined[item_id][0], score),
                    combined[item_id][1],
                    combined[item_id][2],
                )
            else:
                combined[item_id] = (score, content, source)
        return [(k, v[0], v[1], v[2]) for k, v in
                sorted(combined.items(), key=lambda x: x[1][0], reverse=True)]

    def _apply_symbol_rules(self, items, task: str) -> List[tuple]:
        result = []
        for item_id, score, content, source in items:
            boost = 1.0
            for category, keywords in SYMBOL_RULE_KEYWORDS.items():
                if any(kw in task for kw in keywords) or any(kw in content for kw in keywords):
                    # 简单 boost
                    if category == "security":
                        boost *= 1.5
                    elif category == "commitment":
                        boost *= 1.4
                    elif category == "quality":
                        boost *= 1.2
            result.append((item_id, min(score * boost, 1.0), content, source))
        return result

    def _apply_feedback(self, items: List[tuple]) -> List[tuple]:
        return [(
            item_id,
            min(1.0, max(0.0, score + self._feedback.get(item_id, 0.0))),
            content,
            source,
        ) for item_id, score, content, source in items]

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
            # 粗略天数
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
