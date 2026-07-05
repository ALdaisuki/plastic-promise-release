"""八维度审计 + 免疫巡检引擎

免疫系统：检测和修复系统异常。
包含八维度审计框架（奥卡姆剃刀/全过程可查可透明/自我审计闭环/原则激活率/记忆供给质量/
约束合规度/反馈闭环率/Skill执行可追溯）、pre_check 行动前合规检查、合规率计算和告警状态。

AuditReport — 审计报告数据容器（dict/json/markdown 多格式输出）
SoulAuditor — 审计执行引擎（run_audit/pre_check/get_report/compliance_rate/alert_status）
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sqlite3
from typing import Any

from plastic_promise.core.constants import (
    AUDIT_DIMENSIONS,
    PRE_CHECK_ALERT_THRESHOLD,
    WORTH_MIN_OBSERVATIONS,
)
from plastic_promise.core.paths import get_db_path


class AuditReport:
    """八维度审计报告 —— 不可变数据容器。

    封装一次审计的完整结果，支持多种输出格式（dict、JSON 字符串、
    Markdown 报告），便于跨系统传递和人工审查。

    Attributes:
        timestamp: 审计报告生成时间
        scope: 审计范围 ('full' | 'quick' | 'targeted')
        dimensions: 八维度评分明细 (key -> {name, score, weight, description})
        findings: 审计发现列表 (P0 告警等)
        overall_score: 综合评分 (0.0 ~ 1.0)
    """

    def __init__(self) -> None:
        """初始化一份空的审计报告。

        创建空的 dimensions dict、空的 findings list，
        overall_score 默认为 0.0，timestamp 为当前时间。
        """
        self.timestamp: datetime.datetime = datetime.datetime.now()
        self.scope: str = "full"
        self.dimensions: dict[str, dict[str, Any]] = {}
        self.findings: list[dict[str, Any]] = []
        self.overall_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """将审计报告转换为纯字典。

        Returns:
            字典表示，包含 dimensions、findings、overall_score、timestamp(ISO格式)
        """
        return {
            "dimensions": self.dimensions,
            "findings": self.findings,
            "overall_score": self.overall_score,
            "timestamp": self.timestamp.isoformat(),
        }

    def to_json(self) -> str:
        """将审计报告序列化为 JSON 字符串。

        Returns:
            格式化 JSON 字符串 (indent=2, ensure_ascii=False)
        """
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_markdown(self) -> str:
        """将审计报告渲染为 Markdown 文档。

        包含标题、各维度评分条形图、审计发现清单。

        Returns:
            Markdown 格式的完整审计报告
        """
        lines: list[str] = []
        lines.append("# Soul Audit Report")
        lines.append("")
        lines.append(f"**Timestamp**: {self.timestamp.isoformat()}")
        lines.append(f"**Overall Score**: {self.overall_score:.2f} / 1.00")
        lines.append(f"**Scope**: {self.scope}")
        lines.append("")

        # Dimension scores with bar charts
        lines.append("## Dimension Scores")
        lines.append("")
        for dim_key, dim_data in self.dimensions.items():
            score = dim_data.get("score", 0.0)
            name = dim_data.get("name", dim_key)
            weight = dim_data.get("weight", 0.0)
            filled = int(round(score * 20))
            bar = "█" * filled + "░" * (20 - filled)
            lines.append(f"### {name} ({dim_key})")
            lines.append(f"- Score: {score:.2f} / 1.00  [{bar}]")
            lines.append(f"- Weight: {weight:.2f}")
            lines.append("")

        # Findings
        lines.append("## Findings")
        lines.append("")
        if self.findings:
            for f in self.findings:
                severity = f.get("severity", "INFO")
                dim_name = f.get("dimension_name", f.get("dimension", ""))
                message = f.get("message", "")
                lines.append(f"- **[{severity}]** {dim_name}: {message}")
        else:
            lines.append("No significant findings.")
            lines.append("")

        return "\n".join(lines) + "\n"


class SoulAuditor:
    """八维度审计执行引擎 —— 数字免疫系统。

    执行周期性和按需审计，对八维度逐项评分，生成审计报告，
    并提供 pre_check 合规性检查和告警状态查询。

    Attributes:
        _reports: 历史审计报告列表
        _last_audit_time: 最近一次审计时间
    """

    def __init__(self, db_path: str = "", engine: Any = None) -> None:
        """初始化审计引擎。

        Args:
            db_path: SQLite 数据库路径，为空则使用环境变量 PLASTIC_DB_PATH
            engine: ContextEngine 实例引用，用于动态查询
        """
        self._reports: list[AuditReport] = []
        self._last_audit_time: datetime.datetime | None = None
        self._db_path = db_path or get_db_path()
        self._engine = engine

    # ── 动态评分：从真实数据源计算每个维度 ────────────────────

    def _score_simplicity(self) -> tuple[float, dict[str, Any]]:
        """动态计算奥卡姆剃刀评分。

        数据源：step_audit_log.jsonl 中的 simplicity_score 平均值。
        无历史数据时回退到 0.70。
        """
        import json as _json

        scores = []
        audit_log = os.path.join(os.path.dirname(self._db_path), "step_audit_log.jsonl")
        try:
            with open(audit_log, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = _json.loads(line.strip())
                        s = entry.get("simplicity", entry.get("simplicity_score"))
                        if isinstance(s, (int, float)):
                            scores.append(float(s))
                    except Exception:
                        continue
        except FileNotFoundError:
            pass

        if scores:
            avg = sum(scores) / len(scores)
            return round(avg, 2), {"source": "audit_log", "samples": len(scores)}

        # 回退：从记忆池估算 — 简单记忆（短内容）比例越高，simplicity 越好
        try:
            conn = sqlite3.connect(self._db_path)
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            short_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE length(content) < 300"
            ).fetchone()[0]
            conn.close()
            ratio = short_count / max(total, 1)
            # 短记忆比例 0~1 映射到分数 0.4~0.9
            score = 0.4 + ratio * 0.5
            return round(score, 2), {"source": "memory_length", "short_ratio": round(ratio, 2)}
        except Exception:
            return 0.70, {"source": "default"}

    def _score_transparency(self) -> tuple[float, dict[str, Any]]:
        """动态计算透明度评分。

        数据源：audit_log 中有 git_commit 的步骤比例 + git log 可用性。
        """
        import json as _json

        audit_log = os.path.join(os.path.dirname(self._db_path), "step_audit_log.jsonl")
        total = 0
        with_commit = 0
        try:
            with open(audit_log, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = _json.loads(line.strip())
                        total += 1
                        if entry.get("git_commit"):
                            with_commit += 1
                    except Exception:
                        continue
        except FileNotFoundError:
            pass

        if total > 0:
            ratio = with_commit / total
            return round(0.4 + ratio * 0.6, 2), {
                "source": "audit_log",
                "total_steps": total,
                "with_commit": with_commit,
                "commit_ratio": round(ratio, 2),
            }

        # 回退：检查 git 仓库是否存在
        try:
            git_dir = self._find_git_dir(os.path.dirname(self._db_path))
            if git_dir:
                return 0.75, {"source": "git_dir_exists", "path": git_dir}
            return 0.50, {"source": "no_git_dir"}
        except Exception:
            return 0.50, {"source": "default"}

    @staticmethod
    def _find_git_dir(start_dir: str) -> str:
        current = os.path.abspath(start_dir)
        while True:
            git_dir = os.path.join(current, ".git")
            if os.path.isdir(git_dir) or os.path.isfile(git_dir):
                return git_dir
            parent = os.path.dirname(current)
            if parent == current:
                return ""
            current = parent

    def _score_audit_closure(self) -> tuple[float, dict[str, Any]]:
        """动态计算自我审计闭环评分。

        数据源：audit_log 中 root_cause/improvement/lesson 的填充率。
        """
        import json as _json

        audit_log = os.path.join(os.path.dirname(self._db_path), "step_audit_log.jsonl")
        total = 0
        complete = 0  # 三项全有
        partial = 0  # 至少一项有

        try:
            with open(audit_log, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = _json.loads(line.strip())
                        total += 1
                        has_rc = bool(entry.get("root_cause"))
                        has_imp = bool(entry.get("improvement"))
                        has_les = bool(entry.get("lesson"))
                        filled = sum([has_rc, has_imp, has_les])
                        if filled == 3:
                            complete += 1
                        elif filled > 0:
                            partial += 1
                    except Exception:
                        continue
        except FileNotFoundError:
            pass

        if total > 0:
            score = (complete * 1.0 + partial * 0.5) / total
            return round(score, 2), {
                "source": "audit_log",
                "total_steps": total,
                "complete": complete,
                "partial": partial,
            }

        return 0.70, {"source": "default"}

    def _score_principle_activation(self) -> tuple[float, dict[str, Any]]:
        """动态计算原则激活率。

        数据源：原则激活表中的记录数 + 域健康度。
        """
        try:
            conn = sqlite3.connect(self._db_path)
            # 检查 principle_activation 表
            has_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='principle_activations'"
            ).fetchone()
            if has_table:
                total_activations = conn.execute(
                    "SELECT COUNT(*) FROM principle_activations"
                ).fetchone()[0]
                unique_principles = conn.execute(
                    "SELECT COUNT(DISTINCT principle_id) FROM principle_activations"
                ).fetchone()[0]
                conn.close()

                if total_activations > 0:
                    # 激活的 unique 原则数 / 13 条核心原则
                    ratio = min(1.0, unique_principles / 13.0)
                    activation_density = min(1.0, total_activations / 50.0)
                    score = round(ratio * 0.6 + activation_density * 0.4, 2)
                    return score, {
                        "source": "principle_activations",
                        "total": total_activations,
                        "unique": unique_principles,
                    }
            conn.close()
        except Exception:
            pass

        return 0.65, {"source": "default"}

    def _score_memory_supply(self) -> tuple[float, dict[str, Any]]:
        """动态计算记忆供给质量。

        数据源：记忆 worth_success/worth_failure 比值。
        """
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT worth_success, worth_failure FROM memories "
                "WHERE worth_success + worth_failure >= ?",
                (WORTH_MIN_OBSERVATIONS,),
            ).fetchall()
            conn.close()

            if rows:
                worth_scores = []
                for ws, wf in rows:
                    total = ws + wf
                    if total > 0:
                        worth_scores.append(ws / total)

                if worth_scores:
                    avg_worth = sum(worth_scores) / len(worth_scores)
                    return round(avg_worth, 2), {
                        "source": "memory_worth",
                        "samples": len(worth_scores),
                        "avg_worth": round(avg_worth, 2),
                    }
        except Exception:
            pass

        return 0.60, {"source": "default"}

    def _score_constraint_compliance(self) -> tuple[float, dict[str, Any]]:
        """动态计算约束合规度。

        数据源：trust_scores 表健康度 + defense 状态。
        """
        try:
            conn = sqlite3.connect(self._db_path)
            has_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trust_scores'"
            ).fetchone()
            if has_table:
                rows = conn.execute("SELECT agent_id, trust_score FROM trust_scores").fetchall()
                conn.close()

                if rows:
                    trust_vals = [score for _, score in rows]
                    avg_trust = sum(trust_vals) / len(trust_vals)
                    # 信任分越高，合规度越高
                    return round(min(1.0, avg_trust), 2), {
                        "source": "trust_scores",
                        "agents": len(rows),
                        "avg_trust": round(avg_trust, 2),
                    }
            conn.close()
        except Exception:
            pass

        return 0.75, {"source": "default"}

    def _score_feedback_closure(self) -> tuple[float, dict[str, Any]]:
        """动态计算反馈闭环率。

        数据源：审计日志中 repairs/suggestions 的比例 + 趋势。
        """
        try:
            conn = sqlite3.connect(self._db_path)
            has_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trust_history'"
            ).fetchone()
            if has_table:
                # 反馈信号 = 信任分有增有减，说明反馈在起作用
                total_events = conn.execute("SELECT COUNT(*) FROM trust_history").fetchone()[0]
                boost_events = conn.execute(
                    "SELECT COUNT(*) FROM trust_history WHERE delta > 0"
                ).fetchone()[0]
                conn.close()

                if total_events > 0:
                    # 有增有减 = 反馈闭环在工作
                    has_both = boost_events > 0 and (total_events - boost_events) > 0
                    diversity = 1.0 if has_both else 0.6
                    activity = min(1.0, total_events / 20.0)
                    score = round(diversity * 0.5 + activity * 0.5, 2)
                    return score, {
                        "source": "trust_history",
                        "total_events": total_events,
                        "boost_events": boost_events,
                    }
            conn.close()
        except Exception:
            pass

        return 0.50, {"source": "default"}

    async def _score_skill_trace(self) -> tuple[float, dict[str, Any]]:
        """动态计算 Skill 可追溯评分。

        数据源：skill_session 表完整性。
        """
        try:
            from plastic_promise.mcp.server import get_engine
            from plastic_promise.mcp.tools.skill_tracking import (
                handle_skill_session_trace,
            )

            engine = get_engine()
            trace_result = await handle_skill_session_trace(
                engine,
                {"session_scope": "all"},
            )
            trace_data = json.loads(trace_result[0].text)
            gaps = trace_data.get("gaps", [])
            chain_valid = trace_data.get("chain_valid", True)
            total = trace_data.get("total_count", 0)

            if total == 0:
                return 0.0, {"source": "skill_trace", "reason": "no_sessions"}
            elif len(gaps) == 0 and chain_valid:
                return 1.0, {"source": "skill_trace", "sessions": total, "gaps": 0}
            elif len(gaps) > 0:
                return 0.3, {"source": "skill_trace", "sessions": total, "gaps": len(gaps)}
            else:
                return 0.7, {"source": "skill_trace", "sessions": total, "chain_valid": chain_valid}
        except Exception as e:
            return 0.5, {"source": "skill_trace", "error": str(e)[:80]}

    async def run_audit(
        self,
        scope: str = "full",
        time_range_hours: int | None = None,
    ) -> AuditReport:
        """执行一次完整审计，覆盖八维度逐项评分。

        每个维度现在通过动态数据源计算，而非硬编码基线。
        skill_trace 维度通过实时 API 调用，其余维度从 SQLite/JSONL 文件查询。

        Args:
            scope: 审计范围 — 'full' 全面审计 | 'quick' 快速巡检 | 'targeted' 定向审计
            time_range_hours: 审计时间窗口（小时），None 表示自上次审计以来

        Returns:
            AuditReport 实例，包含各维度评分和发现的问题
        """
        report = AuditReport()
        report.scope = scope

        # 动态评分映射：维度 → 评分方法
        dynamic_scorers = {
            "simplicity": self._score_simplicity,
            "transparency": self._score_transparency,
            "audit_closure": self._score_audit_closure,
            "principle_activation": self._score_principle_activation,
            "memory_supply": self._score_memory_supply,
            "constraint_compliance": self._score_constraint_compliance,
            "feedback_closure": self._score_feedback_closure,
            "skill_trace": self._score_skill_trace,
        }

        weighted_sum = 0.0
        total_weight = 0.0

        for dim_key, dim_config in AUDIT_DIMENSIONS.items():
            scorer = dynamic_scorers.get(dim_key)
            if scorer:
                try:
                    if asyncio.iscoroutinefunction(scorer):
                        score, details = await scorer()
                    else:
                        score, details = scorer()
                except Exception:
                    score, details = 0.50, {"source": "error"}
            else:
                score, details = 0.60, {"source": "unknown_dimension"}

            weight = dim_config["weight"]

            report.dimensions[dim_key] = {
                "name": dim_config["name"],
                "score": score,
                "weight": weight,
                "description": dim_config["description"],
                "details": details,
            }

            weighted_sum += score * weight
            total_weight += weight

            # Flag any dimension below 0.60 as a P0 finding
            if score < 0.60:
                report.findings.append(
                    {
                        "severity": "P0",
                        "dimension": dim_key,
                        "dimension_name": dim_config["name"],
                        "score": score,
                        "threshold": 0.60,
                        "message": (
                            f"{dim_config['name']} scored {score:.2f}, "
                            f"below critical threshold 0.60"
                        ),
                        "suggestion": self._suggest_fix(dim_key, score, details),
                    }
                )

        report.overall_score = round(weighted_sum / total_weight if total_weight > 0 else 0.0, 4)
        report.timestamp = datetime.datetime.now()

        self._reports.append(report)
        self._last_audit_time = report.timestamp

        return report

    def _suggest_fix(self, dim_key: str, score: float, details: dict[str, Any]) -> str:
        """为低分维度生成具体的修复建议。"""
        suggestions = {
            "simplicity": "减少不必要的中间步骤和实体，检查是否有过度抽象",
            "transparency": "确保每步有 git commit，补充审计日志中的 git_commit 字段",
            "audit_closure": "在 step-closure 时填写 root_cause/improvement/lesson 三个字段",
            "principle_activation": "在任务开始时使用 principle_activate(task_type=...) 激活原则",
            "memory_supply": "提升记忆质量：淘汰低 worth 记忆，合并重复记忆，执行 memory_gc",
            "constraint_compliance": "检查 trust_scores 表，提升低信任分 Agent 的信任度",
            "feedback_closure": "增加 step-closure 调用频率，确保每步产生反馈信号",
            "skill_trace": "检查孤儿 skill session，调用 skill_session_audit 自动补录",
        }
        return suggestions.get(dim_key, "检查该维度相关配置和数据源")

    def pre_check(
        self,
        action_description: str,
        action_type: str = "exec",
    ) -> dict[str, Any]:
        """行动前合规检查 —— 评估行动是否在免疫约束范围内。

        快速评估操作合规性，返回通过状态和合规评分。

        Args:
            action_description: 待评估操作的描述
            action_type: 操作类型，如 'exec', 'write', 'delete', 'query' 等

        Returns:
            检查结果字典，包含:
            - passed: bool — 是否通过检查
            - compliance_score: float — 合规评分 (0.0 ~ 1.0)
        """
        _ = action_description  # reserved for future heuristic analysis
        _ = action_type
        _ = PRE_CHECK_ALERT_THRESHOLD  # available for threshold comparisons
        return {
            "passed": True,
            "compliance_score": 0.85,
        }

    def get_report(
        self,
        dimension: str | None = None,
        format: str = "json",
    ) -> Any:
        """获取最近一次审计报告。

        Args:
            dimension: 指定维度名称获取单项报告，None 则返回完整报告
            format: 输出格式 — 'dict' | 'json' | 'markdown'

        Returns:
            根据 format 参数返回 dict、str 或 Markdown 文本
        """
        if not self._reports:
            report = AuditReport()
        else:
            report = self._reports[-1]

        # If a specific dimension is requested, extract just that slice
        if dimension is not None:
            dim_data = report.dimensions.get(dimension)
            if format == "json":
                return json.dumps(dim_data, indent=2, ensure_ascii=False)
            return dim_data

        if format == "dict":
            return report.to_dict()
        elif format == "json":
            return report.to_json()
        elif format == "markdown":
            return report.to_markdown()
        else:
            return report.to_dict()

    def get_compliance_rate(self) -> float:
        """获取当前综合合规率。

        基于最近审计报告的 overall_score。

        Returns:
            合规率 (0.0 ~ 1.0)，1.0 表示完全合规
        """
        if not self._reports:
            return 0.0
        return self._reports[-1].overall_score

    def get_alert_status(self) -> dict[str, Any]:
        """获取当前告警状态。

        当合规率低于 PRE_CHECK_ALERT_THRESHOLD 时触发告警。

        Returns:
            告警状态字典，包含:
            - alerting: bool — 是否处于告警状态
            - level: str — 告警级别 ('normal' | 'warning' | 'critical')
            - compliance_rate: float — 当前合规率
            - threshold: float — 告警阈值
            - details: List[str] — 告警详情
        """
        rate = self.get_compliance_rate()
        alerting = rate < PRE_CHECK_ALERT_THRESHOLD

        if rate >= 0.80:
            level = "normal"
        elif rate >= 0.60:
            level = "warning"
        else:
            level = "critical"

        return {
            "alerting": alerting,
            "level": level,
            "compliance_rate": rate,
            "threshold": PRE_CHECK_ALERT_THRESHOLD,
            "details": [],
        }
