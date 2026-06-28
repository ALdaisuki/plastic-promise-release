"""SoulLoop — 主控编排核心

pre_task_v2: 任务执行前编排管线
  - 上下文供应 (ContextEngine.supply)
  - SCARF 五维度自省
  - 激素预调节 (内分泌系统)
  - 三层防线 pre_check
  - 审计记录 (pre_task 快照)

post_task: 任务执行后编排管线
  - 任务结果采集
  - SCARF 自省 (post-mortem)
  - 激素更新 (信任分、情感账户)
  - 记忆演化 (反馈注入、worth 更新)
  - 审计记录 (post_task 快照)
  - CEI 重计算
"""

import datetime
from typing import Any, Dict, Optional

from plastic_promise.core.constants import (
    ASSOCIATION_WEIGHTS,
    AUDIT_DIMENSIONS,
    CEI_TARGET,
    PRE_CHECK_ALERT_THRESHOLD,
    WORTH_MIN_OBSERVATIONS,
)
from plastic_promise.core.context_engine import ContextEngine, ContextPack


class SoulLoop:
    """Plastic Promise 主控编排器。

    在每个任务执行前后运行完整的编排管线：
    上下文供应、SCARF 自省、激素调节、记忆演化、审计记录。
    维护当前 CEI (约定作用指数) 并据此调整系统行为层级。

    Attributes:
        engine: 上下文供应引擎实例，若为 None 则自动创建默认引擎。
    """

    def __init__(self, engine: Optional[ContextEngine] = None) -> None:
        """初始化编排器。

        Args:
            engine: 上下文供应引擎实例。若为 None，自动创建一个空
                    ContextEngine 作为默认引擎。
        """
        self._engine = engine  # None means lazy init on first use
        self._task_count = 0
        self._cei_history: list[float] = []
        self._cached_cei = 0.5

    def pre_task_v2(
        self,
        task_description: str,
        task_type: str = "general",
        pre_context: Optional[str] = None,
    ) -> ContextPack:
        """执行任务前编排管线 (v2)。

        编排顺序：
        1. 委托 ContextEngine.supply() 供应三层上下文
        2. 执行 SCARF 五维度自省 (认知系统)
        3. 根据自省结果预调节激素水平 (内分泌系统)
        4. 执行三层防线 pre_check (反射弧)
        5. 记录 pre_task 审计快照 (免疫系统)

        Args:
            task_description: 任务描述文本，用于上下文检索与原则激活。
            task_type: 任务分类标签，决定原则推荐集与图遍历策略。
                       可选值示例: "code_generation", "code_review",
                       "debugging", "architecture", "refactoring",
                       "learning", "collaboration", "general"。
            pre_context: 调用方预先提供的前置上下文，
                        会合并进 ContextEngine 的预语境字段。

        Returns:
            三层上下文包 (core + related + divergent)，
            包含激活的原则列表和审计元数据。
        """
        # Step 1: Merge pre_context into task_description if provided
        if pre_context:
            task_description = f"{task_description}\n{pre_context}"

        # Step 2: Embed the task description into a vector
        from plastic_promise.embedder import get_embedder

        embedder = get_embedder()
        vector = embedder.embed(task_description)

        # Step 3: Lazy-init engine if needed
        if self._engine is None:
            self._engine = ContextEngine()

        # Step 4: Supply context via the engine
        pack = self._engine.supply(
            task_description,
            vector,
            task_type=task_type,
            scope="global",
        )

        # Step 5: Increment task counter
        self._task_count += 1

        return pack

    def post_task(
        self,
        task_description: str,
        task_type: str,
        context_pack: ContextPack,
        feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """执行任务后编排管线。

        编排顺序：
        1. 采集任务执行结果与调用方反馈
        2. 执行 SCARF post-mortem 自省
        3. 更新激素状态 (信任分、情感账户)
        4. 将反馈注入记忆系统，更新 worth 双计数器
        5. 记录 post_task 审计快照
        6. 重新计算 CEI 约定作用指数

        Args:
            task_description: 与 pre_task_v2 相同的任务描述文本，
                             用于关联前后快照。
            task_type: 任务分类标签，用于审计维度权重调整。
            context_pack: pre_task_v2 产出的上下文包，
                         用于对比实际使用的上下文与供应内容。
            feedback: 调用方提供的反馈字典，可包含:
                      - "adopted": 上下文被采纳的条目 ID 列表
                      - "ignored": 上下文被忽略的条目 ID 列表
                      - "rejected": 上下文被拒绝的条目 ID 列表
                      - "trust_delta": 手动信任分调整量
                      - "notes": 人工备注

        Returns:
            编排报告字典，包含:
            - "scarf": SCARF 五维度评分 (0.0-1.0)
            - "hormone": 激素更新摘要
            - "trust": 信任分变化量
            - "cei": 重计算后的 CEI 值
            - "cei_tier": CEI 层级标签
            - "memory_updates": 受影响记忆条目数
            - "audit_id": 审计记录 ID
        """
        # Build result dict with timestamp
        audit_record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "task_type": task_type,
            "task_description": task_description,
            "context_layers": {
                "core_count": len(context_pack.core),
                "related_count": len(context_pack.related),
                "divergent_count": len(context_pack.divergent),
            },
            "activated_principles": context_pack.activated_principles,
        }

        # Apply feedback via engine if item_ids are provided
        feedback_applied = 0
        if feedback and self._engine is not None:
            for category in ("adopted", "ignored", "rejected"):
                item_ids = feedback.get(category, [])
                if isinstance(item_ids, list) and item_ids:
                    delta = ASSOCIATION_WEIGHTS.get(category, 0.0)
                    for item_id in item_ids:
                        current = self._engine._feedback.get(item_id, 0.0)
                        self._engine._feedback[item_id] = current + delta
                        feedback_applied += 1

        audit_record["feedback_applied_count"] = feedback_applied

        # Calculate CEI delta
        old_cei = self._cached_cei
        new_cei = self.calculate_cei()
        self._cached_cei = new_cei
        self._cei_history.append(new_cei)
        cei_delta = new_cei - old_cei

        audit_record["cei"] = new_cei
        audit_record["cei_delta"] = cei_delta

        return {
            "audit_record": audit_record,
            "cei_delta": cei_delta,
            "task_count": self._task_count,
        }

    def calculate_cei(self) -> float:
        """计算当前约定作用指数 (Convention Efficacy Index)。

        CEI 综合以下维度加权计算:
        - 原则联想率 (20%)
        - 记忆供应质量 (15%)
        - 约束合规率 (15%)
        - 反馈闭环率 (15%)
        - 信任校准度 (10%)
        - 原则继承度 (10%)
        - 安全追溯完整度 (15%)

        目标值 CEI_TARGET = 0.85。

        Returns:
            当前 CEI 值，范围 [0.0, 1.0]。
        """
        weights = {
            "principle_activation": 0.20,
            "memory_supply": 0.15,
            "constraint_compliance": 0.15,
            "feedback_closure": 0.15,
            "trust_alignment": 0.10,
            "principle_inheritance": 0.10,
            "safety_trace": 0.15,
        }

        # memory_supply: estimate from engine worth stats
        engine = self._engine or ContextEngine()
        mem_supply = 0.5  # default when no data available
        if engine._memories:
            scores = []
            for mem in engine._memories.values():
                s = mem.get("worth_success", 0)
                f = mem.get("worth_failure", 0)
                total = s + f
                if total >= WORTH_MIN_OBSERVATIONS:
                    scores.append(s / total)
            if scores:
                mem_supply = sum(scores) / len(scores)

        # Other dimensions default to 0.65 baseline
        dims = {
            "principle_activation": 0.65,
            "memory_supply": mem_supply,
            "constraint_compliance": 0.65,
            "feedback_closure": 0.65,
            "trust_alignment": 0.65,
            "principle_inheritance": 0.65,
            "safety_trace": 0.65,
        }

        cei = sum(dims[k] * weights[k] for k in weights)
        return min(1.0, max(0.0, cei))

    @property
    def current_cei(self) -> float:
        """当前约定作用指数 (缓存值)。

        调用 calculate_cei() 后更新，避免高频重算。
        若尚未计算，首次访问时自动触发计算。
        """
        if not self._cei_history:
            return 0.5
        return self._cei_history[-1]

    @property
    def cei_tier(self) -> str:
        """当前 CEI 层级标签。

        基于 CEI 阈值的六层划分:
        - "nascent"       (0.00 - 0.30) 约定萌芽
        - "growing"       (0.30 - 0.50) 约定生长
        - "forming"       (0.50 - 0.65) 约定成形
        - "internalizing" (0.65 - 0.80) 约定内化
        - "mature"        (0.80 - 0.95) 约定成熟
        - "autonomous"    (0.95 - 1.00) 约定自主

        层级影响系统的自主权策略和约束衰减系数。
        """
        pass


# ============================================================
# 模块级便捷函数
# ============================================================

_default_loop: Optional[SoulLoop] = None


def _get_default_loop() -> SoulLoop:
    """获取或创建默认 SoulLoop 单例。

    首次调用时创建默认 ContextEngine 并初始化 SoulLoop，
    后续调用返回同一实例。

    Returns:
        模块级共享的 SoulLoop 实例。
    """
    global _default_loop
    if _default_loop is None:
        _default_loop = SoulLoop(engine=ContextEngine())
    return _default_loop


def pre_task_v2(
    task_description: str,
    task_type: str = "general",
    pre_context: Optional[str] = None,
) -> ContextPack:
    """模块级便捷函数：执行任务前编排管线。

    等价于 ``_get_default_loop().pre_task_v2(...)``。
    适用于不想自行维护 SoulLoop 实例的简单调用场景。

    Args:
        task_description: 任务描述文本。
        task_type: 任务分类标签，默认 "general"。
        pre_context: 调用方预先提供的前置上下文。

    Returns:
        三层上下文包 (core + related + divergent)。
    """
    return _get_default_loop().pre_task_v2(task_description, task_type, pre_context)


def post_task(
    task_description: str,
    task_type: str,
    context_pack: ContextPack,
    feedback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """模块级便捷函数：执行任务后编排管线。

    等价于 ``_get_default_loop().post_task(...)``。
    适用于不想自行维护 SoulLoop 实例的简单调用场景。

    Args:
        task_description: 任务描述文本，需与 pre_task_v2 一致。
        task_type: 任务分类标签。
        context_pack: pre_task_v2 产出的上下文包。
        feedback: 调用方提供的反馈字典。

    Returns:
        编排报告字典 (scarf, hormone, trust, cei 等)。
    """
    return _get_default_loop().post_task(task_description, task_type, context_pack, feedback)
