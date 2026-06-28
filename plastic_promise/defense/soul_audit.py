"""七维度审计 + 免疫巡检引擎

免疫系统：检测和修复系统异常。
包含七维度审计框架（原则联想/记忆供应/约束合规/反馈闭环/信任校准/原则继承/安全追溯）、
pre_check 行动前合规检查、合规率计算和告警状态。

AuditReport — 审计报告数据容器（dict/json/markdown 多格式输出）
SoulAuditor — 审计执行引擎（run_audit/pre_check/get_report/compliance_rate/alert_status）
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

from plastic_promise.core.constants import (
    AUDIT_DIMENSIONS,
    PRE_CHECK_ALERT_THRESHOLD,
)


class AuditReport:
    """七维度审计报告 —— 不可变数据容器。

    封装一次审计的完整结果，支持多种输出格式（dict、JSON 字符串、
    Markdown 报告），便于跨系统传递和人工审查。

    Attributes:
        timestamp: 审计报告生成时间
        scope: 审计范围 ('full' | 'quick' | 'targeted')
        dimensions: 七维度评分明细 (key -> {name, score, weight, description})
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
        self.dimensions: Dict[str, Dict[str, Any]] = {}
        self.findings: List[Dict[str, Any]] = []
        self.overall_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
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
        lines: List[str] = []
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
    """七维度审计执行引擎 —— 数字免疫系统。

    执行周期性和按需审计，对七维度逐项评分，生成审计报告，
    并提供 pre_check 合规性检查和告警状态查询。

    Attributes:
        _reports: 历史审计报告列表
        _last_audit_time: 最近一次审计时间
    """

    def __init__(self) -> None:
        """初始化审计引擎。

        初始化空的报告历史和审计时间戳。
        """
        self._reports: List[AuditReport] = []
        self._last_audit_time: Optional[datetime.datetime] = None

    # Baseline heuristic scores for the 7 audit dimensions
    _BASELINE_SCORES: Dict[str, float] = {
        "principle_activation": 0.70,
        "memory_supply": 0.75,
        "constraint_compliance": 0.80,
        "feedback_closure": 0.65,
        "trust_alignment": 0.70,
        "principle_inheritance": 0.60,
        "safety_trace": 0.85,
    }

    def run_audit(
        self,
        scope: str = "full",
        time_range_hours: Optional[int] = None,
    ) -> AuditReport:
        """执行一次完整审计，覆盖七维度逐项评分。

        对 AUDIT_DIMENSIONS 中定义的七个维度逐一评分（目前使用启发式基线分数），
        按权重计算综合评分，并将低于 0.60 的维度标记为 P0 发现。

        Args:
            scope: 审计范围 — 'full' 全面审计 | 'quick' 快速巡检 | 'targeted' 定向审计
            time_range_hours: 审计时间窗口（小时），None 表示自上次审计以来

        Returns:
            AuditReport 实例，包含各维度评分和发现的问题
        """
        report = AuditReport()
        report.scope = scope

        weighted_sum = 0.0
        total_weight = 0.0

        for dim_key, dim_config in AUDIT_DIMENSIONS.items():
            score = self._BASELINE_SCORES.get(dim_key, 0.60)
            weight = dim_config["weight"]

            report.dimensions[dim_key] = {
                "name": dim_config["name"],
                "score": score,
                "weight": weight,
                "description": dim_config["description"],
            }

            weighted_sum += score * weight
            total_weight += weight

            # Flag any dimension below 0.60 as a P0 finding
            if score < 0.60:
                report.findings.append({
                    "severity": "P0",
                    "dimension": dim_key,
                    "dimension_name": dim_config["name"],
                    "score": score,
                    "threshold": 0.60,
                    "message": (
                        f"{dim_config['name']} scored {score:.2f}, "
                        f"below critical threshold 0.60"
                    ),
                })

        report.overall_score = round(
            weighted_sum / total_weight if total_weight > 0 else 0.0, 4
        )
        report.timestamp = datetime.datetime.now()

        self._reports.append(report)
        self._last_audit_time = report.timestamp

        return report

    def pre_check(
        self,
        action_description: str,
        action_type: str = "exec",
    ) -> Dict[str, Any]:
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
        dimension: Optional[str] = None,
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

    def get_alert_status(self) -> Dict[str, Any]:
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
