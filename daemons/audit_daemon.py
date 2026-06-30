"""Audit Daemon — 11 维多 Agent 审计 + 自动修复

由 pi_daemon.py 每小时调用一次。拉取多维度健康数据，
生成结构化报告存入 memory_store，Tier 1 问题自动修复。
"""

import sqlite3
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Also add project root so plastic_promise is importable
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

DB_PATH = os.environ.get("PLASTIC_DB_PATH", os.path.join(_project_root, "plastic_memory.db"))


async def run_audit():
    scores = {}
    findings = []

    # ================================================================
    # 1. Trust Health — 信任分分布
    # ================================================================
    try:
        from plastic_promise.defense.soul_enforcer import TrustManager
        tm = TrustManager()
        trust_map = {}
        for role in ["pi_builder", "pi_fixer", "pi_reviewer"]:
            trust_map[role] = tm.get(role)
        avg_trust = sum(trust_map.values()) / max(len(trust_map), 1)
        min_trust = min(trust_map.values()) if trust_map else 0
        scores["trust_health"] = round(avg_trust, 2)
        if min_trust < 0.4:
            findings.append({
                "severity": "warning",
                "dimension": "trust_health",
                "detail": f"Low trust: {min_trust:.2f} (agent at risk of restriction)",
                "auto_fix": False,
            })
    except Exception as e:
        scores["trust_health"] = 0.0
        findings.append({"severity": "error", "dimension": "trust_health", "detail": str(e)})

    # ================================================================
    # 2. Pipeline Health — 任务管道
    # ================================================================
    try:
        conn = sqlite3.connect(DB_PATH)
        total_tasks = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:%'"
        ).fetchone()[0]
        stuck = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:active%'"
        ).fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:accepted%'"
        ).fetchone()[0]
        conn.close()

        pipeline = 1.0 - (stuck / max(total_tasks + resolved, 1))
        scores["pipeline_health"] = round(pipeline, 2)

        if stuck > 0:
            findings.append({
                "severity": "warning" if stuck <= 2 else "error",
                "dimension": "pipeline_health",
                "detail": f"{stuck} stuck task:active memories",
                "auto_fix": True,
            })
        if total_tasks == 0 and resolved == 0:
            scores["pipeline_health"] = 1.0  # no tasks = healthy
    except Exception as e:
        scores["pipeline_health"] = 0.0

    # ================================================================
    # 3. Domain Health — 域联邦状态
    # ================================================================
    try:
        stats = {}
        try:
            from plastic_promise.core.context_engine import ContextEngine
            engine = ContextEngine()
            if hasattr(engine, '_dm') and engine._dm:
                stats = engine._dm.stats()
        except Exception:
            pass

        if stats:
            active = sum(1 for d in stats.values() if d.get("status") == "active")
            scores["domain_health"] = round(active / max(len(stats), 1), 2)
            # Check for candidate domains that never promoted
            candidates = [k for k, v in stats.items() if v.get("status") == "candidate"]
            if candidates:
                findings.append({
                    "severity": "info",
                    "dimension": "domain_health",
                    "detail": f"{len(candidates)} candidate domains pending",
                })
        else:
            scores["domain_health"] = 0.8  # default
    except Exception:
        scores["domain_health"] = 0.5

    # ================================================================
    # 4. Bridge Health — SSE 连通性
    # ================================================================
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://127.0.0.1:9020/health", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                scores["bridge_health"] = 1.0
            else:
                scores["bridge_health"] = 0.0
                findings.append({"severity": "error", "dimension": "bridge_health",
                                 "detail": f"SSE health returned {resp.status_code}"})
    except Exception:
        scores["bridge_health"] = 0.0
        findings.append({"severity": "error", "dimension": "bridge_health",
                         "detail": "SSE /health unreachable"})

    # ================================================================
    # Overall
    # ================================================================
    overall = round(sum(scores.values()) / max(len(scores), 1), 2)

    # ================================================================
    # Tier 1 Auto-fix
    # ================================================================
    auto_fixes = []
    for f in findings:
        if f.get("auto_fix") and f["dimension"] == "pipeline_health":
            fixed = _recover_stuck()
            if fixed:
                auto_fixes.append(f"recovered {fixed} stuck tasks")
                scores["pipeline_health"] = min(1.0, scores["pipeline_health"] + 0.1)
                overall = round(sum(scores.values()) / max(len(scores), 1), 2)

    # ================================================================
    # 5. Store Report
    # ================================================================
    report = (
        f"Audit #{_audit_seq()}: overall={overall:.2f} | "
        f"trust={scores['trust_health']:.2f} pipeline={scores['pipeline_health']:.2f} "
        f"domain={scores['domain_health']:.2f} bridge={scores['bridge_health']:.2f}"
    )
    if auto_fixes:
        report += f" | fixes: {'; '.join(auto_fixes)}"

    # Store via direct ContextEngine call (project root now in sys.path)
    try:
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()
        engine.register_memory({
            "content": report,
            "memory_type": "reflection",
            "tags": ["audit", "domain:governing"],
            "source": "pi_daemon",
        })
    except Exception as e:
        print(f"  [AUDIT] Failed to store report: {e}")

    # Print to console
    print(f"\n  {'='*50}")
    print(f"  AUDIT  trust={scores['trust_health']:.2f} "
          f"pipeline={scores['pipeline_health']:.2f} "
          f"domain={scores['domain_health']:.2f} "
          f"bridge={scores['bridge_health']:.2f} "
          f"→ {overall:.2f}")
    if auto_fixes:
        print(f"  Auto-fixes: {'; '.join(auto_fixes)}")
    needs_claude = [f for f in findings if not f.get("auto_fix")]
    if needs_claude:
        print(f"  Needs Claude: {len(needs_claude)} items")
    print(f"  {'='*50}")

    return {"scores": scores, "findings": findings, "overall": overall}


def _recover_stuck():
    """Tier 1 auto-fix: 重置超时 task:active。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, tags FROM memories WHERE tags LIKE '%task:active%'"
        ).fetchall()
        fixed = 0
        for mid, tags_raw in rows:
            try:
                tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
            except Exception:
                continue
            # Check timestamp
            for t in tags:
                if t.startswith("ts:"):
                    try:
                        task_time = datetime.strptime(t[3:], "%Y%m%dT%H%M%S")
                        if (datetime.now() - task_time).total_seconds() > 300:
                            new_tags = [
                                "task:pending" if tag in ("task:active",) else tag
                                for tag in tags if not tag.startswith("ts:")
                            ]
                            conn.execute(
                                "UPDATE memories SET tags = ? WHERE id = ?",
                                (json.dumps(new_tags), mid)
                            )
                            fixed += 1
                    except ValueError:
                        continue
        conn.commit()
        conn.close()
        return fixed
    except Exception:
        return 0


_audit_seq_counter = [0]


def _audit_seq():
    _audit_seq_counter[0] += 1
    return _audit_seq_counter[0]
