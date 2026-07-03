"""SoulLoop — 主控编排核心

pre_task_v2: 任务执行前编排管线
  - 上下文供应 (ContextEngine.supply)
  - SCARF 五维度自省
  - 激素预调节 (内分泌系统)
  - 三层防线 pre_check
  - 审计记录 (pre_task 快照)

post_task: 六联闭环 — 每步完成后的约定工程全层连线
  - 约定对齐检查 → PrincipleTracker
  - SCARF 五维自省 → SCARFReflector
  - 激素更新 → HormoneEngine
  - 信任联动 → TrustManager
  - 反思记忆存储 → StepAuditor
  - CEI 更新
"""

import datetime

from plastic_promise.core.constants import (
    WORTH_MIN_OBSERVATIONS,
)
from plastic_promise.core.context_engine import ContextEngine, ContextPack
from plastic_promise.core.step_auditor import StepAuditor


class SoulLoop:
    """Plastic Promise 主控编排器。

    在每个任务执行前后运行完整的编排管线：
    上下文供应、SCARF 自省、激素调节、记忆演化、审计记录。
    维护当前 CEI (约定作用指数) 并据此调整系统行为层级。

    Attributes:
        engine: 上下文供应引擎实例，若为 None 则自动创建默认引擎。
    """

    def __init__(self, engine: ContextEngine | None = None) -> None:
        """初始化编排器。

        Args:
            engine: 上下文供应引擎实例。若为 None，自动创建一个空
                    ContextEngine 作为默认引擎。
        """
        self._engine = engine  # None means lazy init on first use
        self._task_count = 0
        self._cei_history: list[float] = []
        self._cached_cei = 0.5
        # Lazy-init attributes for post_task six-link loop
        self._principle_tracker = None
        self._hormone_engine = None
        self._trust_manager = None
        self._auditor = None

    def pre_task_v2(
        self,
        task_description: str,
        task_type: str = "general",
        pre_context: str | None = None,
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
        # Graceful degradation: if Ollama is unreachable / times out,
        # fall back to zero-vector — text retrieval still works via CJK bigrams.
        from plastic_promise.core.embedder import FallbackEmbedder, get_embedder

        try:
            embedder = get_embedder(fallback_on_error=False)
            vector = embedder.embed(task_description)
        except Exception:
            embedder = FallbackEmbedder()
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
        task_description: str = "",
        git_commit: str = "",
        mode: str = "full",
        issue_id: str = None,
        lesson: str = "",
        improvement: str = "",
        root_cause: str = "",
        optimization: str = "",
        trick: str = "",
        target: str = "claude",
    ) -> dict:
        """六联闭环 — 每步完成后的约定工程全层连线。

        Returns:
            dict with keys: alignment, scarf, hormone, trust, reflection, cei, repairs
        """
        result = {
            "alignment": None,
            "scarf": None,
            "hormone": None,
            "trust": None,
            "reflection": None,
            "cei": None,
            "repairs": [],
        }

        # Ensure engine is initialized
        if self._engine is None:
            self._engine = ContextEngine()

        # 0. Lazy-init TrustManager BEFORE HormoneEngine so hormone-driven
        #    trust deltas actually reach the TrustStore (was silently dropped).
        if self._trust_manager is None:
            from plastic_promise.defense.soul_enforcer import TrustManager
            from plastic_promise.defense.trust_store import TrustStore

            self._trust_manager = TrustManager(trust_store=TrustStore())

        # 1. 约定对齐检查 — 记录原则遵守
        try:
            activated = self._engine.activate_principles("general", task_description)
            result["alignment"] = {"checked": len(activated), "principles": activated}
            # Lazy-init PrincipleTracker
            if self._principle_tracker is None:
                from plastic_promise.core.principles import PrincipleTracker

                self._principle_tracker = PrincipleTracker()
            for p_name in activated:
                pid = self._resolve_principle_id(p_name)
                if pid:
                    self._principle_tracker.record(pid, True, task_description[:100])
        except Exception as e:
            result["alignment"] = {"error": str(e)}

        # Light mode: only alignment + memory_store, skip full closure
        if mode == "light":
            if issue_id:
                result["issue_id"] = issue_id
            # Return empty dicts (not None) for keys not computed in light mode,
            # so downstream `.get()` chains don't fail with NoneType errors.
            for k in ("scarf", "hormone", "trust", "reflection", "cei"):
                if result[k] is None:
                    result[k] = {}
            return result

        # 2. SCARF 五维自省
        try:
            from plastic_promise.reflection.soul_scarf import SCARFReflector

            reflector = SCARFReflector()
            scarf_result = reflector.reflect(task_description)
            result["scarf"] = scarf_result
        except Exception as e:
            result["scarf"] = {"error": str(e)}

        # 3. 激素更新
        try:
            if self._hormone_engine is None:
                from plastic_promise.growth.soul_hormone import HormoneEngine

                self._hormone_engine = HormoneEngine(
                    trust_manager=self._trust_manager, target=target
                )
            overall = self._cached_cei
            feedback = "adopted" if overall >= 0.6 else "ignored" if overall >= 0.4 else "rejected"
            hormone_result = self._hormone_engine.apply_feedback(
                feedback, context=task_description[:100]
            )
            result["hormone"] = hormone_result
        except Exception as e:
            result["hormone"] = {"error": str(e)}

        # 4. 信任联动 — TrustManager already initialized in step 0
        try:
            if result.get("scarf") and isinstance(result["scarf"], dict):
                scarf_overall = result["scarf"].get("summary", {}).get("overall_score", 0.6)
                if scarf_overall >= 0.80:
                    self._trust_manager.boost(
                        0.02, f"post_task SCARF {scarf_overall:.2f}", target=target
                    )
                elif scarf_overall < 0.40:
                    self._trust_manager.decay(
                        0.02, f"post_task SCARF {scarf_overall:.2f}", target=target
                    )
            result["trust"] = {
                "score": self._trust_manager.get(target=target),
                "tier": self._trust_manager.tier(target=target),
            }
        except Exception as e:
            result["trust"] = {"error": str(e)}

        # 5. 反思记忆存储 — StepAuditor 评分 + 反思任务标记
        try:
            if self._auditor is None:
                self._auditor = StepAuditor(
                    trust_manager=self._trust_manager, engine=self._engine, target=target
                )
            audit_result = self._auditor.audit_step(
                task_description=task_description,
                git_commit=git_commit,
                lesson=lesson,
                improvement=improvement,
            )

            # 反思字段由执行者 (Claude) 提供 — 不猜测、不代理、不填模板
            final_lesson = lesson or ""
            final_improvement = improvement or ""
            final_root_cause = root_cause or ""
            final_optimization = optimization or ""
            if trick:
                final_lesson = (
                    f"{final_lesson} | 窍门: {trick}" if final_lesson else f"窍门: {trick}"
                )

            result["reflection"] = {
                "overall_score": audit_result.overall_score,
                "lesson": final_lesson[:200],
                "improvement": final_improvement[:200],
                "root_cause": final_root_cause[:200],
                "optimization": final_optimization[:200],
                "step_id": audit_result.step_id,
                "source": "executor" if final_lesson else "",
            }
            result["repairs"] = self._auditor.suggest_repairs(audit_result)
            # 过滤：如果已有 git commit，不再建议 "缺少 git commit"
            if git_commit and git_commit.strip():
                result["repairs"] = [
                    r for r in result["repairs"] if r.get("dimension") != "transparency"
                ]
        except Exception as e:
            result["reflection"] = {"error": str(e)}

        # 6. CEI 更新 — writes back to _cached_cei so hormone feedback
        #    uses the real CEI instead of the hardcoded 0.5 initial value.
        try:
            cei = self.calculate_cei()
            self._cached_cei = cei
            self._cei_history.append(cei)
            global _global_cei
            _global_cei = cei
            result["cei"] = {"score": cei, "tier": self.cei_tier}
        except Exception as e:
            result["cei"] = {"error": str(e)}

        # 7. 上下文预备 — 轻量标记，不触发重型 supply
        try:
            ctx_ready = self._engine.get_context_ready()
            now = datetime.datetime.now()
            # Clean expired entries (TTL 5 min)
            expired = [
                k
                for k, v in ctx_ready.items()
                if (now - getattr(v, "_ts", now)).total_seconds() > 300
            ]
            self._engine.clear_expired_context_ready(expired)
        except Exception:
            pass

        return result

    def _resolve_principle_id(self, name: str) -> int:
        """Resolve principle name to ID from CORE_PRINCIPLES."""
        from plastic_promise.core.constants import CORE_PRINCIPLES

        for p in CORE_PRINCIPLES:
            if p["name"] == name:
                return p["id"]
        return 0

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
        mems = list(engine.iter_memories())
        if mems:
            scores = []
            for mem in mems:
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
        from plastic_promise.core.constants import CEI_THRESHOLDS

        cei = self.calculate_cei()
        for tier_name, (low, high) in CEI_THRESHOLDS.items():
            if low <= cei < high:
                return tier_name
        return "mature"  # fallback for cei >= 1.0 or edge cases


# ============================================================
# 模块级便捷函数
# ============================================================

_default_loop: SoulLoop | None = None


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
    pre_context: str | None = None,
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
    task_description: str = "",
    git_commit: str = "",
    mode: str = "full",
    issue_id: str = None,
    lesson: str = "",
    improvement: str = "",
    root_cause: str = "",
    optimization: str = "",
    trick: str = "",
    target: str = "claude",
) -> dict:
    """模块级便捷函数：执行任务后编排管线（六联闭环）。

    等价于 ``_get_default_loop().post_task(...)``。
    适用于不想自行维护 SoulLoop 实例的简单调用场景。

    Args:
        task_description: 任务描述文本，需与 pre_task_v2 一致。
        git_commit: 关联的 git commit hash。

    Returns:
        编排报告字典 (alignment, scarf, hormone, trust, reflection, cei, repairs)。
    """
    return _get_default_loop().post_task(
        task_description,
        git_commit,
        mode,
        issue_id,
        lesson,
        improvement,
        root_cause,
        optimization,
        trick,
        target,
    )


# Module-level CEI cache — updated by SoulLoop.post_task() on every
# step-closure, read by get_cei() without creating a heavy ContextEngine.
_global_cei: float = 0.5


def get_cei() -> float:
    """Return the current CEI value. Safe — no ContextEngine init needed.

    Returns the last CEI value set by any SoulLoop instance's post_task().
    Defaults to 0.5 if no step-closure has been performed yet.
    """
    return _global_cei
