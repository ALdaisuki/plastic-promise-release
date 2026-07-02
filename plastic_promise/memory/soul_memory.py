"""记忆系统核心实现 — 双层三域架构 + L1/L3 分层 + 四系统融合记忆管理。

提供记忆的存储、检索、分层管理、自动演化与垃圾回收能力。

Classes:
    MemoryWorthCalculator — 基于双计数器的记忆价值评估器
    MemoryRecord          — 记忆记录数据模型
    MemoryTierManager     — L1/L3 分层迁移管理器
    RecMem                — 记忆系统主接口（存储/检索/管理）
    EvolveR               — 自演化引擎（衰退/强化/GC调度）
    MemoryGC              — 垃圾回收器（标记/清理）
"""

from __future__ import annotations

import uuid
import datetime
import logging
from typing import Optional, List, Dict, Any

from plastic_promise.core.constants import (
    MEMORY_TIERS,
    MEMORY_HEALTH_THRESHOLD,
    MEMORY_DECAY_THRESHOLD,
    MEMORY_GC_INTERVAL_DAYS,
    WORTH_SUCCESS_WEIGHT,
    WORTH_FAILURE_WEIGHT,
    WORTH_MIN_OBSERVATIONS,
    MERGE_TOP_K,
    MERGE_SIMILARITY_THRESHOLD,
)
from plastic_promise.core.context_engine import ContextEngine, ContextPack


# ============================================================
# MemoryWorthCalculator — 双计数器价值评估
# ============================================================


class MemoryWorthCalculator:
    """基于威尔逊下界的双计数器记忆价值评估器。

    使用 success/failure 双计数器计算每条记忆的 worth_score，
    在观察数不足时使用威尔逊下界平滑，避免小样本高估。
    """

    def __init__(self, min_observations: int = WORTH_MIN_OBSERVATIONS) -> None:
        """初始化价值计算器。

        Args:
            min_observations: 最少观察次数，低于此值使用威尔逊下界平滑。
        """
        self.min_observations = min_observations

    def calculate_worth(
        self,
        success_count: int,
        failure_count: int,
        total_observations: Optional[int] = None,
    ) -> float:
        """根据成功/失败计数计算记忆价值分数。

        使用 WORTH_SUCCESS_WEIGHT 和 WORTH_FAILURE_WEIGHT 进行加权，
        当观察数不足 min_observations 时采用威尔逊下界平滑。

        Args:
            success_count: 成功反馈次数。
            failure_count: 失败反馈次数。
            total_observations: 总观察次数，默认 None 时自动计算。

        Returns:
            范围 [0.0, 1.0] 的价值分数，越高表示记忆越有价值。
        """
        try:
            n = success_count + failure_count
            if n < self.min_observations:
                return 0.5
            n_f = float(n)
            p = success_count / n_f
            z = 1.96
            z2 = z * z
            center = (p + z2 / (2.0 * n_f)) / (1.0 + z2 / n_f)
            margin = z * ((p * (1.0 - p) / n_f + z2 / (4.0 * n_f * n_f)) ** 0.5) / (1.0 + z2 / n_f)
            return max(0.0, (center - margin) * 2.5 - 0.5)
        except Exception:
            return 0.5

    def calculate_composite_score(self, record: "MemoryRecord") -> float:
        """Compute three-factor composite lifecycle score.

        Formula:
          composite = wilson_worth * 0.6 + freshness * 0.25 + reinforcement * 0.15

        Args:
            record: MemoryRecord with decay_multiplier and effective_half_life set.

        Returns:
            Composite score in [0.0, 1.0]. Falls back to pure Wilson worth
            if decay components are unavailable.
        """
        try:
            wilson = self.calculate_worth(record.worth_success, record.worth_failure)
            freshness = 1.0 - getattr(record, "decay_multiplier", 1.0)

            # Compute reinforcement score from half-life fields
            tier = getattr(record, "tier", "L1")
            from plastic_promise.core.constants import DECAY_CONFIG, REINFORCEMENT_CONFIG

            tier_cfg = DECAY_CONFIG.get(tier, DECAY_CONFIG["default"])
            base_hl = tier_cfg["half_life_days"]
            effective_hl = getattr(record, "effective_half_life", base_hl)
            max_hl = base_hl * REINFORCEMENT_CONFIG["max_multiplier"]
            if max_hl > base_hl:
                reinforcement = (effective_hl - base_hl) / (max_hl - base_hl)
                reinforcement = max(0.0, min(1.0, reinforcement))
            else:
                reinforcement = 0.0

            return wilson * 0.6 + freshness * 0.25 + reinforcement * 0.15
        except Exception:
            return self.calculate_worth(record.worth_success, record.worth_failure)

    def update_counters(self, record: "MemoryRecord", feedback_type: str) -> None:
        """根据反馈类型更新 MemoryRecord 的成功/失败计数器。

        支持的 feedback_type:
            - "adopted"  / "success" -> 增加 success 计数
            - "rejected" / "failure" -> 增加 failure 计数
            - "ignored"                 -> 不做计数更新

        Args:
            record: 要更新的记忆记录。
            feedback_type: 反馈类型字符串（adopted/rejected/ignored/success/failure）。
        """
        try:
            ft = feedback_type.strip().lower()
            if ft in ("adopted", "success"):
                record.worth_success += 1
            elif ft in ("rejected", "failure"):
                record.worth_failure += 1
            # "ignored" — no counter update
        except Exception:
            pass


# ============================================================
# MemoryRecord — 记忆记录数据模型
# ============================================================


class MemoryRecord:
    """单条记忆记录，包含内容、元数据、健康统计与层位信息。

    每条记忆拥有唯一 memory_id，通过 worth_success/worth_failure
    双计数器驱动 worth_score 计算，支持 L1/L3 分层迁移。
    """

    def __init__(
        self,
        content: str,
        memory_type: str = "experience",
        source: str = "user",
        memory_id: Optional[str] = None,
        worth_success: int = 0,
        worth_failure: int = 0,
        activation_weight: float = 0.5,
        tier: str = "L1",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        domain: str = "uncategorized",
        entity_ids: Optional[List[str]] = None,
        decay_multiplier: float = 1.0,
        effective_half_life: Optional[float] = None,
    ) -> None:
        """初始化一条记忆记录。

        Args:
            content: 记忆文本内容。
            memory_type: 记忆类型（experience/reflection/principle/feedback）。
            source: 记忆来源（user/agent/system）。
            memory_id: 唯一标识符，默认 None 时自动生成 UUID4。
            worth_success: 成功反馈计数初始值。
            worth_failure: 失败反馈计数初始值。
            activation_weight: 激活权重 [0.0, 1.0]，控制检索时的初始偏置。
            tier: 当前所在层位（L1 工作记忆 / L3 长期记忆）。
            metadata: 附加元数据字典（如标签、关联实体、创建时间等）。
            tags: 语义标签列表。
            domain: 分配的语义域名称。
            entity_ids: 关联实体 ID 列表（如 skill session entity_id）。
            decay_multiplier: 衰减乘数，控制记忆衰退速率。
            effective_half_life: 有效半衰期（天），记忆价值衰减到一半所需天数。
        """
        self.memory_id = memory_id if memory_id is not None else str(uuid.uuid4())
        self.content = content
        self.memory_type = memory_type
        self.source = source
        self.worth_success = worth_success
        self.worth_failure = worth_failure
        self.activation_weight = activation_weight
        self.tier = tier
        self.metadata = metadata if metadata is not None else {}
        self.tags = tags if tags is not None else []
        self.domain = domain
        self.entity_ids = entity_ids if entity_ids is not None else []
        self.created_at = datetime.datetime.now().isoformat()
        self.last_accessed = self.created_at
        self.access_count = 0
        self.decay_multiplier = decay_multiplier
        if effective_half_life is not None:
            self.effective_half_life = effective_half_life
        else:
            _hl_map = {"L1": 3.0, "L2": 7.0, "L3": 90.0}
            self.effective_half_life = _hl_map.get(self.tier, 14.0)

    @property
    def worth_score(self) -> float:
        """计算当前记忆的综合价值分数。

        委托给 MemoryWorthCalculator.calculate_worth，
        组合 worth_success 和 worth_failure 计数器得出最终评分。

        Returns:
            范围 [0.0, 1.0] 的价值分数。
        """
        try:
            calc = MemoryWorthCalculator()
            return calc.calculate_worth(self.worth_success, self.worth_failure)
        except Exception:
            return 0.5

    def to_dict(self) -> Dict[str, Any]:
        """将记忆记录序列化为字典。

        Returns:
            包含所有字段的字典，适用于 JSON 序列化及 Rust 引擎桥接。
        """
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "memory_type": self.memory_type,
            "source": self.source,
            "worth_success": self.worth_success,
            "worth_failure": self.worth_failure,
            "activation_weight": self.activation_weight,
            "tier": self.tier,
            "metadata": dict(self.metadata),
            "tags": list(self.tags),
            "domain": self.domain,
            "entity_ids": list(self.entity_ids),
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "decay_multiplier": self.decay_multiplier,
            "effective_half_life": self.effective_half_life,
            "worth_score": self.worth_score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
        """从字典反序列化创建 MemoryRecord 实例。

        Args:
            data: 包含 MemoryRecord 各字段的字典。

        Returns:
            新创建的 MemoryRecord 实例。
        """
        record = cls(
            content=data.get("content", ""),
            memory_type=data.get("memory_type", "experience"),
            source=data.get("source", "user"),
            memory_id=data.get("memory_id"),
            worth_success=data.get("worth_success", 0),
            worth_failure=data.get("worth_failure", 0),
            activation_weight=data.get("activation_weight", 0.5),
            tier=data.get("tier", "L1"),
            metadata=data.get("metadata", {}),
            tags=data.get("tags", []),
            domain=data.get("domain", "uncategorized"),
            decay_multiplier=data.get("decay_multiplier", 1.0),
            effective_half_life=data.get("effective_half_life"),
        )
        record.created_at = data.get("created_at", record.created_at)
        record.last_accessed = data.get("last_accessed", record.last_accessed)
        record.access_count = data.get("access_count", 0)
        record.entity_ids = data.get("entity_ids", [])
        return record


# ============================================================
# MemoryTierManager — L1/L3 分层迁移管理器
# ============================================================


class MemoryTierManager:
    """管理记忆在 L1（工作记忆）和 L3（长期记忆）之间的迁移。

    根据 worth_score、激活频率和容量上限自动执行晋升/降级/驱逐。
    """

    def __init__(self, rec_mem: Optional[Any] = None) -> None:
        """初始化分层管理器。

        加载 MEMORY_TIERS 配置中的 L1/L3 容量上限和 TTL 策略。

        Args:
            rec_mem: 可选的 RecMem 实例，用于 promote/demote 时检查容量。
        """
        from plastic_promise.core.constants import MEMORY_TIERS

        self.l1_config = MEMORY_TIERS.get("L1", {"max_items": 200, "ttl_hours": 24})
        self.l3_config = MEMORY_TIERS.get("L3", {"max_items": 2000, "ttl_hours": None})
        self.rec_mem = rec_mem

    def classify_tier(self, record: MemoryRecord) -> str:
        """判断一条记忆应归属的层位。

        基于 worth_score 和激活历史综合判断。
        高价值、高频激活的记忆倾向 L3，低价值、低频的倾向 L1。

        Args:
            record: 待分类的记忆记录。

        Returns:
            "L1" 或 "L3"。
        """
        if record is None:
            return "L1"
        try:
            # 使用 composite_score 替代 worth_score
            calc = MemoryWorthCalculator()
            composite = calc.calculate_composite_score(record)
            if composite >= 0.5 and record.access_count >= 3:
                return "L3"
        except Exception:
            pass
        return "L1"

    def should_demote(self, record: MemoryRecord) -> bool:
        """Check if a memory should be demoted from L3 to L1."""
        try:
            calc = MemoryWorthCalculator()
            composite = calc.calculate_composite_score(record)
            dm = getattr(record, "decay_multiplier", 1.0)
            if dm < 0.2:
                return True
            if composite < 0.15:
                return True
        except Exception:
            pass
        return False

    def promote_to_l3(self, record: MemoryRecord) -> None:
        """将记忆从 L1 晋升到 L3 长期记忆。

        检查 L3 容量上限（MEMORY_TIERS["L3"]["max_items"]），
        若 L3 已满则驱逐最低 worth_score 的记录回 L1。

        Args:
            record: 待晋升的记忆记录。
        """
        if record is None:
            return
        l3_max = self.l3_config.get("max_items", 2000)
        # If RecMem available, check L3 count and evict lowest if full
        if self.rec_mem is not None:
            try:
                l3_records = [r for r in self.rec_mem._records.values() if r.tier == "L3"]
                if len(l3_records) >= l3_max:
                    l3_records.sort(key=lambda r: r.worth_score)
                    self.demote_to_l1(l3_records[0])
            except Exception:
                pass
        record.tier = "L3"

    def demote_to_l1(self, record: MemoryRecord) -> None:
        """将记忆从 L3 降级到 L1 工作记忆。

        当记忆 worth_score 低于衰减阈值或长期未激活时触发。

        Args:
            record: 待降级的记忆记录。
        """
        if record is not None:
            record.tier = "L1"

    def evict_l1_overflow(self, records: List[MemoryRecord]) -> List[str]:
        """处理 L1 工作记忆溢出，驱逐超出容量上限的低价值记忆。

        按 worth_score 升序排序，移除最低分的记录直到满足容量限制。
        被驱逐的 L1 记忆直接丢弃（不迁移到 L3）。

        Args:
            records: 当前 L1 层中的所有记忆记录列表。

        Returns:
            被驱逐记忆的 memory_id 列表。
        """
        if not records:
            return []
        l1_max = self.l1_config.get("max_items", 200)
        if len(records) <= l1_max:
            return []
        sorted_records = sorted(records, key=lambda r: r.worth_score)
        overflow = len(sorted_records) - l1_max
        evicted = []
        for r in sorted_records[:overflow]:
            evicted.append(r.memory_id)
            if self.rec_mem is not None:
                try:
                    self.rec_mem.forget(r.memory_id, reason="L1 overflow eviction")
                except Exception:
                    pass
        return evicted


# ============================================================
# RecMem — 记忆系统主接口
# ============================================================


class RecMem:
    """记忆系统主接口，提供存储、检索、更新、遗忘等核心操作。

    内部管理 L1/L3 双层记忆池，通过 ContextEngine 进行上下文供应，
    支持反馈驱动的 worth_score 演化和自动分层迁移。
    """

    def __init__(self, engine: Optional[ContextEngine] = None) -> None:
        """初始化记忆系统。

        Args:
            engine: 上下文供应引擎实例，默认 None 时内部创建新实例。
        """
        try:
            from context_engine_core import ContextEngine as RustContextEngine

            self._engine = engine if engine is not None else RustContextEngine()
        except ImportError:
            self._engine = engine if engine is not None else ContextEngine()
        self._records: dict = {}

    def store(
        self,
        content: str,
        memory_type: str = "experience",
        source: str = "user",
        importance: float = 0.7,
        entity_ids: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        domain: str = "uncategorized",
        category: str = "other",
    ) -> MemoryRecord:
        """存储一条新记忆。

        创建 MemoryRecord 并写入 L1 工作记忆池。
        同时向 ContextEngine 注册该记忆以支持后续检索。
        若 L1 容量超标则触发 evict_l1_overflow。

        Args:
            content: 记忆文本内容。
            memory_type: 记忆类型（experience/reflection/principle/feedback）。
            source: 记忆来源（user/agent/system）。
            importance: 初始重要性 [0.0, 1.0]，影响 activation_weight。
            entity_ids: 关联的实体 ID 列表（可选）。
            tags: 语义标签列表（可选）。
            domain: 分配的语义域名称。
            category: 分类类别（preference/fact/decision/entity/event/pattern/other）。

        Returns:
            新创建的 MemoryRecord 实例。
        """
        try:
            memory_id = str(uuid.uuid4())
            try:
                from context_engine_core import MemoryRecord as RustMemoryRecord

                rust_record = RustMemoryRecord(memory_id, content, memory_type, source)
                rust_record.tier = "L1"
                rust_record.scope = "global"
                rust_record.category = category
                rust_record.importance = importance
                rust_record.entity_ids = entity_ids or []
                rust_record.domain = domain
                rust_record.tags = tags or []
                self._engine.store_memory(rust_record)
            except (ImportError, AttributeError):
                # Fallback: Python engine
                record_dict = {
                    "id": memory_id,
                    "content": content,
                    "memory_type": memory_type,
                    "source": source,
                    "activation_weight": importance,
                    "worth_success": 0,
                    "worth_failure": 0,
                    "tier": "L1",
                    "category": category,
                    "domain": domain,
                    "tags": tags or [],
                    "entity_ids": entity_ids or [],
                }
                self._engine.register_memory(record_dict)

            # Try embedding + storing vector
            try:
                from plastic_promise.core.embedder import get_embedder

                embedder = get_embedder()
                vec = embedder.embed(content)
                _ = vec  # Vector stored via engine internals
            except Exception:
                pass

            record = MemoryRecord(
                content=content,
                memory_type=memory_type,
                source=source,
                memory_id=memory_id,
                activation_weight=importance,
                tier="L1",
                tags=tags or [],
                domain=domain or "uncategorized",
                entity_ids=entity_ids or [],
            )
            self._records[memory_id] = record
            return record
        except Exception:
            record = MemoryRecord(
                content=content,
                memory_type=memory_type,
                source=source,
                activation_weight=importance,
                tier="L1",
                tags=tags or [],
                domain=domain or "uncategorized",
                entity_ids=entity_ids or [],
            )
            self._records[record.memory_id] = record
            return record

    def recall(
        self,
        query: str,
        task_type: str = "general",
        max_results: int = 20,
        min_relevance: float = 0.2,
        include_principles: bool = True,
    ) -> ContextPack:
        """检索与查询相关的记忆，返回分层上下文包。

        委托 ContextEngine.supply() 执行双路检索（文本 + 图遍历），
        经 RRF 融合和符号规则调整后按三层结构打包返回。

        Args:
            query: 检索查询文本（通常为 task_description）。
            task_type: 任务类型（code_generation/code_review/debugging/architecture/
                       refactoring/learning/collaboration/general）。
            max_results: 最大返回条目数。
            min_relevance: 最低相关度阈值，低于此值的记忆被过滤。
            include_principles: 是否在结果中注入核心原则。

        Returns:
            分层上下文包，包含 core/related/divergent 三层 ContextItem。
        """
        try:
            from plastic_promise.core.embedder import get_embedder

            embedder = get_embedder()
            vec = embedder.embed(query)
            self._engine.enable_principles = include_principles
            pack = self._engine.supply(query, vec, task_type, "global")
            return pack
        except Exception:
            return ContextPack()

    def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        importance: Optional[float] = None,
        reset_worth: bool = False,
    ) -> Optional[MemoryRecord]:
        """更新指定记忆的内容或重要性。

        支持部分更新：content 和 importance 均为可选，
        仅更新提供的字段。若 reset_worth=True 则清零计数器。

        Args:
            memory_id: 目标记忆的唯一标识符。
            content: 新的记忆内容，None 表示不修改。
            importance: 新重要性值 [0.0, 1.0]，None 表示不修改。
            reset_worth: 是否重置 worth_success/worth_failure 计数器。

        Returns:
            更新后的 MemoryRecord，若 memory_id 不存在则返回 None。
        """
        try:
            result = self._engine.update_memory(memory_id, content=content, importance=importance)
            if not result:
                return None
            # Update Python-side record
            if memory_id in self._records:
                record = self._records[memory_id]
                if content is not None:
                    record.content = content
                if importance is not None:
                    record.activation_weight = importance
                if reset_worth:
                    record.worth_success = 0
                    record.worth_failure = 0
                return record
            return None
        except Exception:
            if memory_id in self._records:
                record = self._records[memory_id]
                if content is not None:
                    record.content = content
                if importance is not None:
                    record.activation_weight = importance
                if reset_worth:
                    record.worth_success = 0
                    record.worth_failure = 0
                return record
            return None

    def forget(self, memory_id: str, reason: str = "") -> bool:
        """从记忆池中删除指定记忆。

        同时从 ContextEngine 中注销该记忆，确保检索不再返回。

        Args:
            memory_id: 待删除记忆的唯一标识符。
            reason: 删除原因（用于审计日志）。

        Returns:
            True 表示成功删除，False 表示记忆不存在。
        """
        try:
            # Sync delete from LanceDB vector store (A+B: dual-write consistency)
            ldb = getattr(self._engine, "_ldb", None)
            if ldb is not None:
                try:
                    ldb.delete(memory_id)
                except Exception:
                    pass
            result = self._engine.delete_memory(memory_id)
            if memory_id in self._records:
                del self._records[memory_id]
            return result
        except Exception:
            if memory_id in self._records:
                del self._records[memory_id]
                return True
            return False

    def list_records(
        self,
        memory_type: Optional[str] = None,
        source: Optional[str] = None,
        min_worth: Optional[float] = None,
        limit: int = 50,
    ) -> List[MemoryRecord]:
        """列出记忆池中的记录，支持多条件筛选。

        Args:
            memory_type: 按记忆类型过滤，None 表示不过滤。
            source: 按来源过滤，None 表示不过滤。
            min_worth: 最低 worth_score 阈值，None 表示不过滤。
            limit: 最大返回条数。

        Returns:
            按 worth_score 降序排列的记忆记录列表。
        """
        try:
            rust_records = self._engine.list_memories(
                memory_type=memory_type,
                source=source,
                min_worth=min_worth,
                limit=limit,
            )
            result: List[MemoryRecord] = []
            for r in rust_records:
                py_record = MemoryRecord(
                    content=r.content,
                    memory_type=r.memory_type,
                    source=r.source,
                    memory_id=r.id,
                    worth_success=r.worth_success,
                    worth_failure=r.worth_failure,
                    activation_weight=r.importance,
                    tier=r.tier,
                )
                result.append(py_record)
            return result
        except Exception:
            # Fallback: filter from local _records
            result = list(self._records.values())
            if memory_type is not None:
                result = [r for r in result if r.memory_type == memory_type]
            if source is not None:
                result = [r for r in result if r.source == source]
            if min_worth is not None:
                result = [r for r in result if r.worth_score >= min_worth]
            result.sort(key=lambda r: r.worth_score, reverse=True)
            return result[:limit]

    def stats(self) -> Dict[str, Any]:
        """获取记忆池的统计信息。

        Returns:
            字典包含以下键：
            - total: 记忆总数
            - l1_count: L1 工作记忆数量
            - l3_count: L3 长期记忆数量
            - avg_worth: 平均 worth_score
            - health_ratio: 健康记忆占比（worth >= DECAY_THRESHOLD）
            - by_type: 各类型记忆数量分布
            - by_source: 各来源记忆数量分布
        """
        try:
            import json

            json_str = self._engine.memory_stats_json()
            stats = json.loads(json_str)
            # Map Rust stat keys to expected Python keys
            result = {
                "total": stats.get("total", len(self._records)),
                "l1_count": stats.get("by_tier", {}).get("L1", 0),
                "l3_count": stats.get("by_tier", {}).get("L3", 0),
                "avg_worth": stats.get("average_worth", 0.0),
                "health_ratio": stats.get("healthy", 0) / max(stats.get("total", 1), 1),
                "by_type": stats.get("by_type", {}),
                "by_source": stats.get("by_category", {}),
            }
            return result
        except Exception:
            records = list(self._records.values())
            total = len(records)
            if total == 0:
                return {
                    "total": 0,
                    "l1_count": 0,
                    "l3_count": 0,
                    "avg_worth": 0.0,
                    "health_ratio": 1.0,
                    "by_type": {},
                    "by_source": {},
                }
            l1_count = sum(1 for r in records if r.tier == "L1")
            l3_count = sum(1 for r in records if r.tier == "L3")
            avg_worth = sum(r.worth_score for r in records) / total
            healthy = sum(1 for r in records if r.worth_score >= MEMORY_DECAY_THRESHOLD)
            by_type: Dict[str, int] = {}
            by_source: Dict[str, int] = {}
            for r in records:
                by_type[r.memory_type] = by_type.get(r.memory_type, 0) + 1
                by_source[r.source] = by_source.get(r.source, 0) + 1
            return {
                "total": total,
                "l1_count": l1_count,
                "l3_count": l3_count,
                "avg_worth": round(avg_worth, 4),
                "health_ratio": healthy / total,
                "by_type": by_type,
                "by_source": by_source,
            }

    def apply_feedback(
        self,
        memory_id: str,
        feedback_type: str,
        task_context: str = "",
    ) -> Dict[str, Any]:
        """对指定记忆应用反馈，驱动 worth_score 演化。

        将 feedback_type 转发至 MemoryWorthCalculator.update_counters，
        更新目标记忆的成功/失败计数器。

        Args:
            memory_id: 目标记忆的唯一标识符。
            feedback_type: 反馈类型（adopted/ignored/rejected/success/failure）。
            task_context: 触发反馈的任务上下文描述。

        Returns:
            字典包含：
            - memory_id: 记忆标识符
            - old_worth: 更新前的 worth_score
            - new_worth: 更新后的 worth_score
            - delta: worth_score 变化量
            - feedback_type: 应用的反馈类型
        """
        try:
            # Get old worth
            old_worth = 0.0
            try:
                rust_record = self._engine.get_memory(memory_id)
                if rust_record is not None:
                    old_worth = rust_record.worth_score()
                    # Apply feedback on Rust record
                    ft = feedback_type.strip().lower()
                    if ft in ("adopted", "success"):
                        rust_record.record_adopted()
                    elif ft in ("rejected", "failure"):
                        rust_record.record_rejected()
                    # ignored: no change
                    self._engine.store_memory(rust_record)
                    new_worth = rust_record.worth_score()
                else:
                    new_worth = old_worth
            except Exception:
                new_worth = old_worth

            # Also update Python-side record
            if memory_id in self._records:
                py_record = self._records[memory_id]
                old_worth = py_record.worth_score
                ft = feedback_type.strip().lower()
                if ft in ("adopted", "success"):
                    py_record.worth_success += 1
                elif ft in ("rejected", "failure"):
                    py_record.worth_failure += 1
                new_worth = py_record.worth_score

            return {
                "memory_id": memory_id,
                "old_worth": old_worth,
                "new_worth": new_worth,
                "delta": new_worth - old_worth,
                "feedback_type": feedback_type,
            }
        except Exception:
            return {
                "memory_id": memory_id,
                "old_worth": 0.0,
                "new_worth": 0.0,
                "delta": 0.0,
                "feedback_type": feedback_type,
            }

    @property
    def total_count(self) -> int:
        """记忆池中的记忆总数（L1 + L3）。

        Returns:
            非负整数，表示当前存储的记忆总数。
        """
        try:
            s = self.stats()
            return s.get("total", len(self._records))
        except Exception:
            return len(self._records)

    @property
    def health_ratio(self) -> float:
        """健康记忆占比。

        健康记忆定义为 worth_score >= MEMORY_DECAY_THRESHOLD 的记忆。
        该比值用于判定是否触发 GC 和演化周期。

        Returns:
            范围 [0.0, 1.0] 的浮点数，目标值为 1.0。
        """
        try:
            s = self.stats()
            return s.get("health_ratio", 1.0)
        except Exception:
            records = list(self._records.values())
            if not records:
                return 1.0
            healthy = sum(1 for r in records if r.worth_score >= MEMORY_DECAY_THRESHOLD)
            return healthy / len(records)

    def update_all_decay(self) -> int:
        """Recompute and persist decay_multiplier for all records.

        Walks every record in the pool, recomputes the Weibull decay
        multiplier, and persists changes to SQLite.  Returns the number
        of records whose decay value changed by more than 0.001.
        """
        from plastic_promise.core.decay_engine import WeibullDecayCalculator

        wdc = WeibullDecayCalculator()
        now = datetime.datetime.now().isoformat()
        updated = 0

        for r in self._records.values():
            dm = wdc.compute_decay(
                tier=r.tier,
                created_at=r.created_at,
                effective_half_life=getattr(r, "effective_half_life", None),
                current_time_str=now,
            )
            if abs(r.decay_multiplier - dm) > 0.001:
                r.decay_multiplier = dm
                updated += 1

        if updated > 0 and self._engine:
            for r in self._records.values():
                try:
                    self._engine.execute_sql(
                        "UPDATE memories SET decay_multiplier = ? WHERE id = ?",
                        (r.decay_multiplier, r.memory_id),
                    )
                except Exception:
                    pass
            try:
                self._engine.commit_sql()
            except Exception:
                pass

        return updated


# ============================================================
# EvolveR — 自演化引擎
# ============================================================


class EvolveR:
    """自演化引擎，驱动记忆系统的周期性演化。

    负责执行衰退检测、价值衰减、L1↔L3 分层调整和 GC 调度。
    """

    def __init__(
        self,
        rec_mem: RecMem,
        decay_threshold: float = MEMORY_DECAY_THRESHOLD,
    ) -> None:
        """初始化演化引擎。

        Args:
            rec_mem: 关联的记忆系统实例。
            decay_threshold: worth_score 低于此值的记忆被标记为衰退候选。
        """
        self.rec_mem = rec_mem
        self.decay_threshold = decay_threshold
        self.tier_manager = MemoryTierManager(rec_mem)

    def evolve_cycle(self) -> Dict[str, Any]:
        """执行一次完整的演化周期。

        流程：
        1. 遍历 L3 记忆，对低于 decay_threshold 的记录执行降级
        2. 遍历 L1 记忆，对高 worth_score 的记录执行晋升
        3. 对长期未激活的 L1 记忆执行价值衰减
        4. 检查 L1 容量并驱逐溢出
        5. 汇总统计数据并返回

        Returns:
            字典包含：
            - promoted: 晋升至 L3 的记忆数量
            - demoted: 降级至 L1 的记忆数量
            - decayed: 被衰减的记忆数量
            - evicted: 被驱逐的记忆数量
            - health_before: 演化前的 health_ratio
            - health_after: 演化后的 health_ratio
        """
        if self.rec_mem is None:
            return {
                "promoted": 0,
                "demoted": 0,
                "decayed": 0,
                "evicted": 0,
                "health_before": 1.0,
                "health_after": 1.0,
            }
        try:
            # Phase A: 批量更新 decay_multiplier
            try:
                from plastic_promise.core.decay_engine import WeibullDecayCalculator
                import datetime

                wdc = WeibullDecayCalculator()
                records_pre = list(self.rec_mem._records.values()) if self.rec_mem else []
                if records_pre:
                    results = wdc.evaluate_all(records_pre)
                    now = datetime.datetime.now().isoformat()
                    for mid, dm in results:
                        if mid in self.rec_mem._records:
                            self.rec_mem._records[mid].decay_multiplier = dm
                        # Persist to SQLite
                        engine = self.rec_mem._engine if self.rec_mem else None
                        if engine:
                            engine.execute_sql(
                                "UPDATE memories SET decay_multiplier = ? WHERE id = ?", (dm, mid)
                            )
                    if engine:
                        engine.commit_sql()
            except Exception as e:
                logging.warning("EvolveR: decay batch update failed: %s", e)

            health_before = self.rec_mem.health_ratio
            records = list(self.rec_mem._records.values())
            promoted = 0
            demoted = 0

            # Demote L3 low-composite records (use should_demote which checks decay + composite)
            l3_records = [
                r for r in records if r.tier == "L3" and self.tier_manager.should_demote(r)
            ]
            for r in l3_records:
                self.tier_manager.demote_to_l1(r)
                demoted += 1

            # Promote L1 high-composite records
            calc = MemoryWorthCalculator()
            l1_records = [
                r for r in records if r.tier == "L1" and calc.calculate_composite_score(r) >= 0.6
            ]
            for r in l1_records:
                self.tier_manager.promote_to_l3(r)
                promoted += 1

            # Decay stale L1 records
            decayed = self.decay_stale()

            # Evict L1 overflow
            l1_after = [r for r in self.rec_mem._records.values() if r.tier == "L1"]
            evicted = len(self.tier_manager.evict_l1_overflow(l1_after))

            health_after = self.rec_mem.health_ratio
            return {
                "promoted": promoted,
                "demoted": demoted,
                "decayed": decayed,
                "evicted": evicted,
                "health_before": health_before,
                "health_after": health_after,
            }
        except Exception:
            return {
                "promoted": 0,
                "demoted": 0,
                "decayed": 0,
                "evicted": 0,
                "health_before": 1.0,
                "health_after": 1.0,
            }

    def decay_stale(self, days_threshold: int = MEMORY_GC_INTERVAL_DAYS) -> int:
        """对长期未激活的 L1 记忆执行价值衰减。

        检索最后激活时间超过 days_threshold 天的 L1 记忆，
        对其 activation_weight 执行衰减（乘以衰减系数）。
        若 worth_score 降至 MEMORY_DECAY_THRESHOLD 以下，
        则标记为 GC 候选。

        Args:
            days_threshold: 未激活天数阈值，超过此值触发衰减。

        Returns:
            被衰减的记忆数量。
        """
        if self.rec_mem is None:
            return 0
        try:
            cutoff = datetime.datetime.now() - datetime.timedelta(days=days_threshold)
            decayed = 0
            for r in self.rec_mem._records.values():
                if r.tier != "L1":
                    continue
                try:
                    last = datetime.datetime.fromisoformat(r.last_accessed)
                    if last < cutoff:
                        r.activation_weight = max(0.0, r.activation_weight * 0.7)
                        decayed += 1
                except (ValueError, TypeError):
                    pass
            return decayed
        except Exception:
            return 0


# ============================================================
# MemoryGC — 垃圾回收器
# ============================================================


class MemoryGC:
    """记忆系统垃圾回收器。

    负责识别衰退记忆、执行安全回收（支持 dry_run 预演），
    确保记忆池保持在健康容量范围内。
    """

    def __init__(self, rec_mem: RecMem) -> None:
        """初始化垃圾回收器。

        Args:
            rec_mem: 关联的记忆系统实例。
        """
        self.rec_mem = rec_mem
        self._last_collect: Optional[str] = None

    def collect(self, dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
        """执行垃圾回收，清理衰退记忆。

        默认以 dry_run 模式运行（只报告不删除），安全为主。
        force=True 时忽略 GC 间隔限制立即执行。

        回收策略：
        1. 调用 mark_decaying() 标记候选
        2. 对标记的记忆按 worth_score 升序排序
        3. 从最低分开始逐条 forget()
        4. 当 health_ratio >= MEMORY_HEALTH_THRESHOLD/100 时停止

        Args:
            dry_run: True 时仅报告计划不执行删除。
            force: True 时强制立即执行（忽略 GC 间隔限制）。

        Returns:
            字典包含：
            - dry_run: 是否为预演模式
            - candidates: 标记为衰退的记忆 ID 列表
            - removed: 实际删除的记忆数量（dry_run 时为 0）
            - health_before: 回收前的 health_ratio
            - health_after: 回收后的 health_ratio
            - freed_slots: 释放的容量槽位
        """
        try:
            health_before = self.rec_mem.health_ratio if self.rec_mem else 1.0
            candidates = self.mark_decaying()
        except Exception:
            candidates = []

        result = {
            "dry_run": dry_run,
            "candidates_count": len(candidates),
            "candidates": candidates[:50],
            "removed": 0,
            "health_before": health_before if "health_before" in dir() else 1.0,
            "health_after": health_before if "health_before" in dir() else 1.0,
            "freed_slots": 0,
            "merge": {},  # populated below
        }

        # ---- Direction B: Similar memory merge ----
        merge_result = self.merge_similar(threshold=0.70, dry_run=dry_run)
        result["merge"] = merge_result

        if dry_run or not candidates or self.rec_mem is None:
            return result

        # Interval check (skip if forced)
        if not force and self._last_collect is not None:
            try:
                last = datetime.datetime.fromisoformat(self._last_collect)
                interval = datetime.timedelta(days=MEMORY_GC_INTERVAL_DAYS)
                if datetime.datetime.now() - last < interval:
                    return result
            except (ValueError, TypeError):
                pass

        try:
            removed = 0
            for mid in candidates:
                if self.rec_mem.health_ratio >= MEMORY_HEALTH_THRESHOLD / 100.0:
                    break
                self.rec_mem.forget(mid, reason="GC: worth below decay threshold")
                removed += 1

            self._last_collect = datetime.datetime.now().isoformat()
            result["removed"] = removed
            result["health_after"] = self.rec_mem.health_ratio
            result["freed_slots"] = removed
        except Exception:
            pass

        return result

    def mark_decaying(self) -> List[str]:
        """扫描记忆池，标记 worth_score 低于衰减阈值的记忆。

        扫描范围：
        - L1 中 worth_score < MEMORY_DECAY_THRESHOLD 的记录
        - L3 中长期未激活且 worth_score < MEMORY_DECAY_THRESHOLD 的记录

        Returns:
            被标记为衰退的记忆 ID 列表，按 worth_score 升序排列。
        """
        if self.rec_mem is None:
            return []
        try:
            decaying = []
            for r in self.rec_mem._records.values():
                try:
                    if r.worth_score < MEMORY_DECAY_THRESHOLD:
                        decaying.append((r.memory_id, r.worth_score))
                except Exception:
                    pass
            decaying.sort(key=lambda x: x[1])
            return [mid for mid, _ in decaying]
        except Exception:
            return []

    def merge_similar(
        self,
        threshold: float = MERGE_SIMILARITY_THRESHOLD,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Batch-scan the memory pool and merge records with cosine similarity >= threshold.

        Algorithm:
          1. For each memory with a vector, query LanceDB for top-k similar
          2. Filter pairs with similarity >= threshold, skip self-matches and already-merged
          3. Survivor = max(worth_score, ties broken by most recent created_at)
          4. Append merged record's content_abstract to survivor.metadata["merged_from"]
          5. Tag merged record with metadata["merged_into"] = survivor_id
          6. Remove merged record from engine._memories (retrieval layer)
          7. Keep merged record in SQLite for audit trail (cleaned next GC cycle)

        Args:
            threshold: Cosine similarity threshold for merge (default 0.70).
            dry_run: If True, only report -- no records are modified.

        Returns:
            dict with:
                dry_run, candidates_found, would_merge, would_free, merged_pairs, error
        """
        result: Dict[str, Any] = {
            "dry_run": dry_run,
            "candidates_found": 0,
            "would_merge": 0,
            "would_free": 0,
            "merged_pairs": [],
            "error": None,
        }

        if self.rec_mem is None:
            return result

        try:
            engine = getattr(self.rec_mem, "_engine", None)
            if engine is None:
                return result

            ldb = getattr(engine, "_ldb", None)
            if ldb is None:
                result["error"] = "lancedb_unavailable"
                return result
            if getattr(ldb, "_vectors_disabled", False):
                result["candidates_found"] = 0
                result["would_merge"] = 0
                result["would_free"] = 0
                result["merged_pairs"] = []
                result["error"] = "vectors_disabled"
                return result

            memories = getattr(engine, "_memories", {})
            if not memories:
                return result

            # Gather all memories with vectors
            vec_map: Dict[str, list] = {}
            for mid, mem in memories.items():
                vec = mem.get("_vector")
                if vec and not any(v != 0.0 for v in vec):
                    continue  # skip zero vectors (fallback embedder)
                if vec:
                    vec_map[mid] = vec

            if len(vec_map) < 2:
                return result

            # Scan: for each memory, find similar neighbors
            seen_pairs: set = set()
            candidates: list = []  # list of (mid_a, mid_b, similarity)

            for mid, vec in vec_map.items():
                try:
                    similar = ldb.search_similar(vec, k=MERGE_TOP_K)  # MERGE_TOP_K
                except Exception:
                    continue
                for other_id, sim in similar:
                    if other_id == mid:
                        continue  # self-match
                    if sim < threshold:
                        continue
                    pair_key = tuple(sorted([mid, other_id]))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    candidates.append((mid, other_id, sim))

            result["candidates_found"] = len(candidates)

            if not candidates:
                return result

            # Determine survivors and merged records
            already_merged: set = set()
            merged_pairs = []

            for mid_a, mid_b, sim in candidates:
                if mid_a in already_merged or mid_b in already_merged:
                    continue

                # Get Python records for worth_score comparison
                py_a = self.rec_mem._records.get(mid_a)
                py_b = self.rec_mem._records.get(mid_b)

                # Determine survivor — use Direction A composite_score (wilson+decay+reinforcement)
                try:
                    calc = MemoryWorthCalculator()
                    score_a = calc.calculate_composite_score(py_a) if py_a else 0.5
                    score_b = calc.calculate_composite_score(py_b) if py_b else 0.5
                except Exception:
                    score_a = py_a.worth_score if py_a else 0.5
                    score_b = py_b.worth_score if py_b else 0.5

                if score_a > score_b:
                    survivor, merged = mid_a, mid_b
                elif score_b > score_a:
                    survivor, merged = mid_b, mid_a
                else:
                    # Tiebreaker: most recent created_at
                    time_a = py_a.created_at if py_a else ""
                    time_b = py_b.created_at if py_b else ""
                    if time_a >= time_b:
                        survivor, merged = mid_a, mid_b
                    else:
                        survivor, merged = mid_b, mid_a

                # Build merged_from entry
                merged_content = ""
                merged_worth = 0.0
                if merged in self.rec_mem._records:
                    mr = self.rec_mem._records[merged]
                    merged_content = mr.content[:80]
                    merged_worth = mr.worth_score

                merge_entry = {
                    "memory_id": merged,
                    "content_abstract": merged_content,
                    "merged_at": datetime.datetime.now().isoformat(),
                    "worth_score": round(merged_worth, 4),
                }

                merged_pairs.append(
                    {
                        "survivor": survivor,
                        "merged": [merged],
                        "similarity": round(sim, 4),
                    }
                )

                if not dry_run:
                    # Append to survivor metadata
                    if survivor in self.rec_mem._records:
                        surv_rec = self.rec_mem._records[survivor]
                        if "merged_from" not in surv_rec.metadata:
                            surv_rec.metadata["merged_from"] = []
                        surv_rec.metadata["merged_from"].append(merge_entry)

                    # Tag merged record
                    if merged in self.rec_mem._records:
                        self.rec_mem._records[merged].metadata["merged_into"] = survivor

                    # Remove from engine._memories (retrieval layer)
                    if merged in memories:
                        # Fix #3: Persist merged_into to SQLite metadata for audit trail
                        sqlite = getattr(engine, "_sqlite", None)
                        if sqlite is not None:
                            try:
                                mem_data = dict(memories[merged])
                                mem_data["merged_into"] = survivor
                                # Ensure metadata dict exists and carries merged_into
                                if "metadata" not in mem_data:
                                    mem_data["metadata"] = {}
                                if isinstance(mem_data["metadata"], dict):
                                    mem_data["metadata"]["merged_into"] = survivor
                                else:
                                    mem_data["metadata"] = {"merged_into": survivor}
                                sqlite.upsert(merged, mem_data)
                            except Exception:
                                pass
                        del memories[merged]

                    already_merged.add(merged)

            # Deduplicate: count unique merged records
            all_merged = set()
            for pair in merged_pairs:
                for m in pair["merged"]:
                    all_merged.add(m)

            result["would_merge"] = len(merged_pairs)
            result["would_free"] = len(all_merged)
            result["merged_pairs"] = merged_pairs[:20]  # cap preview at 20 pairs

            return result

        except Exception as e:
            result["error"] = str(e)
            return result
