"""Daily audit — 完整八维度审计 + 生成可追溯报告。

每天执行一次完整 SoulAuditor 审计，将结果存储为 reflection 记忆，
并对低分维度自动生成修复建议。

依赖 SoulAuditor 的动态评分引擎，而非独立统计。
"""

import asyncio
import datetime
import json
from typing import Any


def run_sync(engine: Any = None) -> dict:
    """同步执行每日审计（便捷入口）。

    Args:
        engine: ContextEngine 实例。

    Returns:
        dict with daily audit report.
    """
    return asyncio.run(_run_async(engine))


async def _run_async(engine: Any = None) -> dict:
    """异步执行每日审计。"""
    now = datetime.datetime.now()
    report: dict = {
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "memory_stats": {},
        "audit_scores": {},
        "recommendations": [],
    }

    # ── 1. 八维度完整审计 (SoulAuditor) ──
    try:
        from plastic_promise.defense.soul_audit import SoulAuditor

        auditor = SoulAuditor()
        audit_report = await auditor.run_audit(scope="full")

        report["audit_scores"] = {
            dim: {
                "name": detail["name"],
                "score": detail["score"],
                "weight": detail["weight"],
                "source": detail.get("details", {}).get("source", "unknown"),
            }
            for dim, detail in audit_report.dimensions.items()
        }
        report["overall_score"] = audit_report.overall_score
        report["findings"] = [
            {
                "severity": f.get("severity"),
                "dimension": f.get("dimension"),
                "message": f.get("message"),
                "suggestion": f.get("suggestion", ""),
            }
            for f in audit_report.findings
        ]

        # 为低分维度生成建议
        for f in audit_report.findings:
            report["recommendations"].append(
                f"[{f['dimension']}] {f.get('suggestion', f['message'])}"
            )
    except Exception as e:
        report["audit_scores"] = {"error": str(e)[:200]}

    # ── 2. 记忆池统计 ──
    if engine is not None:
        try:
            stats_str = engine.memory_stats_json() if hasattr(engine, "memory_stats_json") else None
            if isinstance(stats_str, str):
                report["memory_stats"] = json.loads(stats_str)
            elif stats_str:
                report["memory_stats"] = stats_str

            # 记忆健康度建议
            total = report["memory_stats"].get("total", 0)
            healthy = report["memory_stats"].get("healthy", 0)
            decaying = report["memory_stats"].get("decaying", 0)
            if total > 0:
                health_ratio = healthy / total
                if health_ratio < 0.80:
                    report["recommendations"].append(
                        f"记忆健康度 {health_ratio:.0%} < 80%，建议执行 memory_gc(dry_run=false)"
                    )
                if decaying > total * 0.15:
                    report["recommendations"].append(
                        f"衰减记忆 {decaying}/{total} 超过 15%，建议审查 worth 阈值"
                    )
        except Exception:
            report["memory_stats"] = {"error": "stats unavailable"}
    else:
        report["memory_stats"] = {"note": "no engine provided"}

    # ── 3. 存储审计报告为记忆 ──
    create_ordinary = getattr(engine, "create_ordinary_if_absent", None)
    if callable(create_ordinary):
        try:
            audit_summary = (
                f"日审计 {report['date']}: overall={report.get('overall_score', 'N/A')}, "
                f"findings={len(report.get('findings', []))}"
            )
            create_ordinary(
                {
                    "id": f"daily_audit_{report['date'].replace('-', '')}",
                    "content": json.dumps(
                        {
                            "summary": audit_summary,
                            "overall_score": report.get("overall_score"),
                            "findings": report.get("findings", []),
                            "recommendations": report.get("recommendations", []),
                        },
                        ensure_ascii=False,
                    ),
                    "memory_type": "reflection",
                    "source": "audit_daily",
                    "tier": "L2",
                }
            )
            report["stored"] = True
        except Exception:
            report["stored"] = False

    if not report.get("recommendations"):
        report["recommendations"].append("系统运行正常，无需干预")

    return report


def run(engine: Any = None) -> dict:
    """Generate daily audit summary (backward-compatible sync entry).

    Args:
        engine: ContextEngine instance.

    Returns:
        dict with daily audit report.
    """
    return asyncio.run(_run_async(engine))
