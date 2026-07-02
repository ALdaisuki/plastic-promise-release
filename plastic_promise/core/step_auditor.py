"""Step Auditor — 每一步的 4 阶段自我审计 + 评分驱动约定理论

核心原则 3: 每一步完成后必须执行四阶段审计：
(1) 根因分析 — 为什么做/为什么出错
(2) 改良措施 — 下次如何做得更好
(3) 教训提炼 — 可迁移的普适规律
(4) 量化评分 — 0.0-1.0 驱动信任分和 CEI

评分维度的三层映射（对应 3 条核心原则）：
- simplicity (35%): 奥卡姆剃刀 — 方案是否最简洁？
- transparency (35%): 全过程可查可透明 — 每步是否有 git 痕迹？
- audit_closure (30%): 自我审计闭环 — 是否完成了根因/改良/教训？
"""

import datetime
import json
import os
import sqlite3
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class StepAuditResult:
    """单步审计结果 — 4 阶段 + 3 维评分"""

    # 四阶段
    root_cause: str  # 根因分析
    improvement: str  # 改良措施
    lesson: str  # 教训提炼（可迁移）

    # 三维评分 (0.0-1.0)
    simplicity_score: float  # 奥卡姆剃刀：步骤是否必要？方案是否最简？
    transparency_score: float  # 可查可透明：是否有 git？是否可追溯？
    audit_closure_score: float  # 审计闭环：根因+改良+教训是否完整？

    # 元数据
    overall_score: float = 0.0  # 加权总分
    step_id: str = ""
    timestamp: str = ""
    task_description: str = ""
    git_commit: str = ""  # 关联的 git commit hash
    audit_log: str = ""  # 审计日志摘要


class StepAuditor:
    """每步执行后的自我审计引擎。

    用法：
        auditor = StepAuditor(trust_manager, cei_tracker)
        result = auditor.audit_step(
            task_description="实现用户认证模块",
            git_commit="abc1234",
            simplicity_rationale="只用了3个函数，无额外依赖",
            transparency_rationale="每步有commit，日志完整",
            root_cause="用户需要安全登录——这是核心需求",
            improvement="下次可以提取认证中间件复用",
            lesson="认证逻辑应独立于业务逻辑",
        )
        # result.overall_score 自动计算
        # trust_manager 自动 boost/decay
        # 审计记录自动保存
    """

    def __init__(self, trust_manager=None, audit_log_path: Optional[str] = None, engine=None):
        self._trust = trust_manager
        self._engine = engine  # optional ContextEngine for reflection memory storage
        self._history: list[StepAuditResult] = []
        self._audit_log_path = audit_log_path or "step_audit_log.jsonl"
        self._load_history()

    # ============================================================
    # 核心方法
    # ============================================================

    def audit_step(
        self,
        task_description: str = "",
        git_commit: str = "",
        root_cause: str = "",
        improvement: str = "",
        lesson: str = "",
        simplicity_rationale: str = "",
        transparency_rationale: str = "",
        audit_closure_rationale: str = "",
    ) -> StepAuditResult:
        """执行完整的 4 阶段审计 + 3 维评分。

        Args:
            task_description: 当前步骤描述
            git_commit: 关联的 git commit hash
            root_cause: 根因分析（为什么做/为什么出错）
            improvement: 改良措施（下次如何更好）
            lesson: 教训提炼（可迁移规律）
            simplicity_rationale: 奥卡姆剃刀评分依据
            transparency_rationale: 透明度评分依据
            audit_closure_rationale: 审计闭环评分依据

        Returns:
            StepAuditResult with overall_score calculated
        """
        # 1. 根因分析
        if not root_cause:
            root_cause = self._derive_root_cause(task_description)

        # 2. 改良措施
        if not improvement:
            improvement = self._derive_improvement(task_description)

        # 3. 教训提炼
        if not lesson:
            lesson = self._derive_lesson(task_description)

        # 4. 量化评分
        simplicity = self._score_simplicity(task_description, simplicity_rationale, git_commit)
        transparency = self._score_transparency(git_commit, transparency_rationale)
        audit_closure = self._score_audit_closure(
            root_cause, improvement, lesson, audit_closure_rationale
        )

        overall = simplicity * 0.35 + transparency * 0.35 + audit_closure * 0.30

        result = StepAuditResult(
            root_cause=root_cause,
            improvement=improvement,
            lesson=lesson,
            simplicity_score=simplicity,
            transparency_score=transparency,
            audit_closure_score=audit_closure,
            overall_score=round(overall, 4),
            step_id=f"step_{len(self._history) + 1:04d}",
            timestamp=datetime.datetime.now().isoformat(),
            task_description=task_description,
            git_commit=git_commit,
            audit_log=self._build_audit_log(
                task_description, root_cause, improvement, lesson, overall
            ),
        )

        # 5. 驱动信任分
        self._apply_trust_delta(result)

        # 6. 保存
        self._history.append(result)
        self._save(result)

        # 7. 反思记忆存储 — 将教训提炼自动存入记忆池（原则 #10 自演化闭环）
        derived_lesson = self._derive_lesson(task_description)
        derived_improvement = self._derive_improvement(task_description)
        if self._engine is not None:
            # 当调用方传入的 lesson/improvement 比自动推导更有价值时，存储调用方的版本
            content_to_store = lesson or derived_lesson
            if content_to_store:
                try:
                    self._engine.register_memory(
                        {
                            "id": f"reflection_{result.step_id}",
                            "content": content_to_store,
                            "memory_type": "reflection",
                            "source": "step_auditor",
                            "tier": "L3",
                        }
                    )
                except Exception:
                    pass
            # 如果有有价值的改良措施，也一并存储
            improvement_to_store = improvement or derived_improvement
            if improvement_to_store and improvement_to_store != content_to_store:
                try:
                    self._engine.register_memory(
                        {
                            "id": f"improvement_{result.step_id}",
                            "content": improvement_to_store,
                            "memory_type": "improvement",
                            "source": "step_auditor",
                            "tier": "L3",
                        }
                    )
                except Exception:
                    pass

        # 域联邦自进化: 每次审计后触发衰减检测
        try:
            from plastic_promise.core.domain_manager import DomainManager
            import os

            db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
            dm = DomainManager(db_path=db_path)
            decayed = dm.decay(agent_id="")
            if decayed:
                result.audit_log = (result.audit_log or "") + (
                    "\n[domain_decay] "
                    + str(len(decayed))
                    + " domains decayed: "
                    + ", ".join(d.get("name", "?") for d in decayed)
                )
        except Exception:
            pass  # 域检测失败不影响主审计流程

        return result

    # ============================================================
    # 评分逻辑
    # ============================================================

    def _score_simplicity(self, task: str, rationale: str, git_commit: str) -> float:
        """奥卡姆剃刀评分。

        评分策略：
        1. 有 rationale → 关键字分析 + 基准加分
        2. 无 rationale → 从历史取基准分
        3. task 为空 → 扣分
        4. 有 git_commit → 轻微加分（有意识的步骤）
        """
        score = self._get_historical_benchmark("simplicity") or 0.70

        if rationale:
            # 关键词加分
            simplicity_keywords = [
                "简洁",
                "最少",
                "核心",
                "必要",
                "精简",
                "最简",
                "剃刀",
                "仅",
                "只用了",
                "simple",
                "minimal",
                "bare",
                "essential",
                "lean",
            ]
            hits = sum(1 for kw in simplicity_keywords if kw.lower() in rationale.lower())
            # 每条关键词 +0.04，上限 1.0
            score = min(1.0, score + hits * 0.04)

            # 惩罚：检测到可能过度设计的信号
            overengineering_signals = [
                "抽象",
                "工厂",
                "建造者",
                "访问者",
                "策略模式",
                "装饰器",
                "abstract",
                "factory",
                "builder",
                "visitor",
                "strategy",
                "decorator",
            ]
            signal_count = sum(
                1 for kw in overengineering_signals if kw.lower() in rationale.lower()
            )
            if signal_count > 1:
                score = max(0.3, score - signal_count * 0.05)

        if not task:
            score -= 0.10
        if git_commit:
            score += 0.03
        return round(max(0.0, min(1.0, score)), 4)

    def _score_transparency(self, git_commit: str, rationale: str) -> float:
        """全过程可查可透明评分。

        评分策略：
        1. git_commit 存在 → 高分 (0.80+)
        2. rationale 提到审计元素 → 加分
        3. 无 git → 上限 0.55
        """
        historical = self._get_historical_benchmark("transparency")

        if git_commit:
            score = max(0.80, historical or 0.80)
        else:
            score = min(0.55, historical or 0.50)

        if rationale:
            transparency_keywords = [
                "日志",
                "记录",
                "trace",
                "log",
                "commit",
                "审计",
                "可查",
                "可验",
                "复现",
                "验证",
                "review",
                "sign-off",
                "approval",
                "test",
            ]
            hits = sum(1 for kw in transparency_keywords if kw.lower() in rationale.lower())
            score = min(1.0, score + hits * 0.03)

        return round(max(0.0, min(1.0, score)), 4)

    def _score_audit_closure(
        self, root_cause: str, improvement: str, lesson: str, rationale: str
    ) -> float:
        """自我审计闭环评分。

        评分策略：
        1. 三项全有 → >= 0.75
        2. 缺一项 → 扣 0.15
        3. rationale 有深度关键词 → 加分
        """
        score = 0.65

        filled_count = sum(
            [
                1 if root_cause else 0,
                1 if improvement else 0,
                1 if lesson else 0,
            ]
        )
        score += filled_count * 0.08

        if rationale:
            closure_keywords = [
                "因果",
                "改进",
                "迁移",
                "规律",
                "下次",
                "避免",
                "根源",
                "深层",
                "root",
                "cause",
                "lesson",
                "improve",
                "avoid",
                "prevent",
            ]
            hits = sum(1 for kw in closure_keywords if kw.lower() in rationale.lower())
            score = min(1.0, score + hits * 0.02)

        return round(max(0.0, min(1.0, score)), 4)

    def _get_historical_benchmark(self, dimension: str) -> Optional[float]:
        """从历史审计记录中获取某维度的平均值作为基准线。

        Args:
            dimension: "simplicity" | "transparency" | "audit_closure"

        Returns:
            历史平均值 (0.0~1.0)，无数据则返回 None
        """
        if not self._history:
            return None

        field_map = {
            "simplicity": "simplicity_score",
            "transparency": "transparency_score",
            "audit_closure": "audit_closure_score",
        }
        field = field_map.get(dimension)
        if not field:
            return None

        scores = [getattr(r, field, None) for r in self._history]
        scores = [s for s in scores if s is not None]
        if not scores:
            return None

        return round(sum(scores) / len(scores), 4)

    # ============================================================
    # 自动推导（当用户未提供时）—— 基于历史模式 + 语义分析
    # ============================================================

    def _derive_root_cause(self, task: str) -> str:
        # 根因由 LLMReflector 生成，此处不填模板
        return ""

    def _derive_improvement(self, task: str) -> str:
        # 优化建议由 LLMReflector 生成，此处不填模板
        return ""

    def _derive_lesson(self, task: str) -> str:
        # 经验教训由 LLMReflector 生成，此处不填模板
        return ""

    def _infer_from_history(self, task: str, field: str = "root_cause") -> str:
        """从历史审计记录中查找相似任务的经验。

        简单的文本相似度匹配：Jaccard 系数 > 0.25 视为相关。
        """
        if len(self._history) < 2:
            return ""

        task_words = set(task.lower().split())
        if len(task_words) < 2:
            return ""

        best_match = ""
        best_score = 0.0

        for r in self._history:
            hist_task = r.task_description or ""
            hist_words = set(hist_task.lower().split())
            if not hist_words:
                continue

            # Jaccard 相似度
            intersection = task_words & hist_words
            union = task_words | hist_words
            score = len(intersection) / len(union) if union else 0

            if score > 0.25 and score > best_score:
                best_score = score
                target = getattr(r, field, "")
                if target and target not in (
                    f"执行任务「{task[:80]}」——根因分析待补充",
                    "",
                    "未提供任务描述",
                ):
                    best_match = target

        return best_match

    # ============================================================
    # 信任分联动
    # ============================================================

    def _apply_trust_delta(self, result: StepAuditResult):
        """根据评分调整信任分。

        - overall >= 0.80 → boost (信任分 +0.02)
        - overall >= 0.60 → 轻微 boost (+0.005)
        - overall < 0.40  → decay (-0.02)
        - overall < 0.20  → 显著 decay (-0.05)
        """
        if self._trust is None:
            return
        try:
            if result.overall_score >= 0.80:
                self._trust.boost(0.02, f"step {result.step_id}: {result.overall_score:.2f}")
            elif result.overall_score >= 0.60:
                self._trust.boost(0.005, f"step {result.step_id}: {result.overall_score:.2f}")
            elif result.overall_score < 0.20:
                self._trust.decay(0.05, f"step {result.step_id}: {result.overall_score:.2f}")
            elif result.overall_score < 0.40:
                self._trust.decay(0.02, f"step {result.step_id}: {result.overall_score:.2f}")
        except Exception:
            pass

    # ============================================================
    # 持久化 + 查询
    # ============================================================

    def _build_audit_log(self, task, root_cause, improvement, lesson, overall) -> str:
        return json.dumps(
            {
                "task": task[:200],
                "root_cause": root_cause[:300],
                "improvement": improvement[:300],
                "lesson": lesson[:300],
                "score": overall,
            },
            ensure_ascii=False,
        )

    def _save(self, result: StepAuditResult):
        try:
            with open(self._audit_log_path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "step_id": result.step_id,
                            "timestamp": result.timestamp,
                            "task": result.task_description[:200],
                            "root_cause": result.root_cause[:300],
                            "improvement": result.improvement[:300],
                            "lesson": result.lesson[:300],
                            "simplicity": result.simplicity_score,
                            "transparency": result.transparency_score,
                            "audit_closure": result.audit_closure_score,
                            "overall": result.overall_score,
                            "git_commit": result.git_commit,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass

    def _load_history(self):
        try:
            with open(self._audit_log_path, "r", encoding="utf-8") as f:
                self._history = []  # lazy — just check file exists
        except FileNotFoundError:
            pass

    # ============================================================
    # 修复建议
    # ============================================================

    def suggest_repairs(self, result: "StepAuditResult") -> list[dict]:
        """Generate repair suggestions for dimensions scoring below 0.60.

        Serves 实践层反思/修复: 发现问题 → 生成修复建议。
        """
        suggestions = []
        if result.simplicity_score < 0.60:
            suggestions.append(
                {
                    "dimension": "simplicity",
                    "current_score": result.simplicity_score,
                    "suggestion": "删除不必要的中间步骤，检查是否存在可以简化的逻辑路径",
                }
            )
        if result.transparency_score < 0.60:
            suggestions.append(
                {
                    "dimension": "transparency",
                    "current_score": result.transparency_score,
                    "suggestion": f"确保每一步有 git commit，当前任务缺少可追溯痕迹",
                }
            )
        if result.audit_closure_score < 0.60:
            suggestions.append(
                {
                    "dimension": "audit_closure",
                    "current_score": result.audit_closure_score,
                    "suggestion": "补充根因分析、改良措施或教训提炼，当前审计不完整",
                }
            )
        return suggestions

    # ============================================================
    # 查询接口
    # ============================================================

    def get_history(self, limit: int = 20) -> list[dict]:
        """获取最近的审计历史。"""
        return [
            {
                "step_id": r.step_id,
                "timestamp": r.timestamp,
                "task": r.task_description[:100],
                "overall": r.overall_score,
                "simplicity": r.simplicity_score,
                "transparency": r.transparency_score,
                "audit_closure": r.audit_closure_score,
                "git_commit": r.git_commit,
            }
            for r in self._history[-limit:]
        ]

    def get_cei(self) -> float:
        """计算当前 CEI（约定作用指数）。

        基于所有历史审计记录的加权平均分。
        """
        if not self._history:
            return 0.50  # 冷启动默认
        return round(sum(r.overall_score for r in self._history) / len(self._history), 4)

    def get_trend(self) -> str:
        """评分趋势：上升/下降/稳定。"""
        if len(self._history) < 3:
            return "待积累"
        recent = [r.overall_score for r in self._history[-3:]]
        if recent[-1] > recent[0] + 0.05:
            return "上升"
        if recent[-1] < recent[0] - 0.05:
            return "下降"
        return "稳定"

    @property
    def step_count(self) -> int:
        return len(self._history)
