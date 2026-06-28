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
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class StepAuditResult:
    """单步审计结果 — 4 阶段 + 3 维评分"""
    # 四阶段
    root_cause: str         # 根因分析
    improvement: str        # 改良措施
    lesson: str             # 教训提炼（可迁移）

    # 三维评分 (0.0-1.0)
    simplicity_score: float      # 奥卡姆剃刀：步骤是否必要？方案是否最简？
    transparency_score: float    # 可查可透明：是否有 git？是否可追溯？
    audit_closure_score: float   # 审计闭环：根因+改良+教训是否完整？

    # 元数据
    overall_score: float = 0.0   # 加权总分
    step_id: str = ""
    timestamp: str = ""
    task_description: str = ""
    git_commit: str = ""         # 关联的 git commit hash
    audit_log: str = ""          # 审计日志摘要


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

    def __init__(self, trust_manager=None, audit_log_path: Optional[str] = None):
        self._trust = trust_manager
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
        audit_closure = self._score_audit_closure(root_cause, improvement, lesson, audit_closure_rationale)

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
            audit_log=self._build_audit_log(task_description, root_cause, improvement, lesson, overall),
        )

        # 5. 驱动信任分
        self._apply_trust_delta(result)

        # 6. 保存
        self._history.append(result)
        self._save(result)

        return result

    # ============================================================
    # 评分逻辑
    # ============================================================

    def _score_simplicity(self, task: str, rationale: str, git_commit: str) -> float:
        """奥卡姆剃刀评分。

        评分标准：
        - 0.9-1.0: 步骤必要且方案极简，无冗余
        - 0.7-0.8: 步骤必要，有轻微冗余但可接受
        - 0.5-0.6: 步骤可能非必要，或有明显简化空间
        - 0.3-0.4: 步骤冗余，方案复杂化
        - 0.0-0.2: 严重违反剃刀原则，过度设计
        """
        # 启发式：默认 0.75，有理据时提高
        score = 0.75
        if rationale:
            # 检查是否提到了简约/最少/核心等关键词
            simplicity_keywords = ["简洁", "最少", "核心", "必要", "精简", "最简", "剃刀", "仅", "只用了"]
            hits = sum(1 for kw in simplicity_keywords if kw in rationale)
            score = min(1.0, 0.75 + hits * 0.05)
        if not task:
            score -= 0.1  # 缺少任务描述扣分
        if git_commit:
            score += 0.05  # 有 git 痕迹轻微加分（说明是有意识的步骤）
        return round(max(0.0, min(1.0, score)), 4)

    def _score_transparency(self, git_commit: str, rationale: str) -> float:
        """全过程可查可透明评分。

        评分标准：
        - git_commit 存在 → 至少 0.80（有痕迹）
        - 无 git → 最多 0.50（缺乏追溯）
        - rationale 提到日志/记录/验证 → 加分
        """
        score = 0.50  # 基础分
        if git_commit:
            score = 0.85  # 有 git commit 大幅加分
        if rationale:
            transparency_keywords = ["日志", "记录", "trace", "log", "commit", "审计", "可查", "可验", "复现"]
            hits = sum(1 for kw in transparency_keywords if kw.lower() in rationale.lower())
            score = min(1.0, score + hits * 0.03)
        return round(max(0.0, min(1.0, score)), 4)

    def _score_audit_closure(self, root_cause: str, improvement: str, lesson: str, rationale: str) -> float:
        """自我审计闭环评分。

        评分标准：
        - 根因+改良+教训全部非空 → 至少 0.70
        - 缺少任一 → 扣 0.20
        - rationale 有深度分析 → 加分
        """
        score = 0.70
        if not root_cause:
            score -= 0.20
        if not improvement:
            score -= 0.20
        if not lesson:
            score -= 0.20
        if rationale:
            closure_keywords = ["因果", "改进", "迁移", "规律", "下次", "避免", "根源", "深层"]
            hits = sum(1 for kw in closure_keywords if kw in rationale)
            score = min(1.0, score + hits * 0.03)
        return round(max(0.0, min(1.0, score)), 4)

    # ============================================================
    # 自动推导（当用户未提供时）
    # ============================================================

    def _derive_root_cause(self, task: str) -> str:
        if not task:
            return "未提供任务描述"
        return f"执行任务「{task[:80]}」——根因分析待补充"

    def _derive_improvement(self, task: str) -> str:
        return f"改进方向：提取「{task[:40]}」中的可复用模式，减少下次同类任务的步骤数"

    def _derive_lesson(self, task: str) -> str:
        return f"教训：每个步骤都有可优化的空间——复盘「{task[:40]}」的执行路径"

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
        return json.dumps({
            "task": task[:200],
            "root_cause": root_cause[:300],
            "improvement": improvement[:300],
            "lesson": lesson[:300],
            "score": overall,
        }, ensure_ascii=False)

    def _save(self, result: StepAuditResult):
        try:
            with open(self._audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
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
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _load_history(self):
        try:
            with open(self._audit_log_path, "r", encoding="utf-8") as f:
                self._history = []  # lazy — just check file exists
        except FileNotFoundError:
            pass

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
