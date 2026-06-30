"""Maintenance Daemon — 定时健康审计 + GC + 超时恢复

轻量维护进程。MCP Server 是共享记忆唯一真相源，daemon 通过 /notify
写入审计报告确保 MCP 进程可见。多 Agent 协调通过共享记忆池自治，
不在此调度。

原则 #1 奥卡姆剃刀: 从 pi_daemon(410行)+audit_daemon(226行) 砍掉
Pi CLI 死代码 (~200行)，合并为 ~180 行维护守护进程。
"""

import asyncio
import httpx
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta

# Path setup
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

DB_PATH = os.environ.get("PLASTIC_DB_PATH", os.path.join(_project_root, "plastic_memory.db"))
INTERVAL = int(os.environ.get("AUDIT_INTERVAL_SECONDS", "300"))
MCP_URL = "http://127.0.0.1:9020"

# ── 超时恢复 ──────────────────────────────────────────────
def recover_stuck_tasks():
    """task:active > 5min 或 task:reviewed > 10min → 重置."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, tags FROM memories "
        "WHERE tags LIKE '%task:active%' OR tags LIKE '%task:reviewed%'"
    ).fetchall()

    now = datetime.now()
    for mid, tags_raw in rows:
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
        except Exception:
            continue

        ts_str = None
        for t in tags:
            if t.startswith("ts:"):
                ts_str = t[3:]
                break
        if not ts_str:
            continue
        try:
            task_time = datetime.strptime(ts_str, "%Y%m%dT%H%M%S")
        except ValueError:
            continue

        elapsed = (now - task_time).total_seconds()
        new_tags = None
        if "task:active" in tags and elapsed > 300:
            new_tags = ["task:pending" if t == "task:active" else t
                        for t in tags if not t.startswith("ts:")]
            print(f"  [RECOVER] task:active {elapsed:.0f}s → pending")
        elif "task:reviewed" in tags and elapsed > 600:
            new_tags = ["task:active" if t == "task:reviewed" else t
                        for t in tags if not t.startswith("ts:")]
            print(f"  [RECOVER] task:reviewed {elapsed:.0f}s → active")

        if new_tags:
            conn.execute("UPDATE memories SET tags = ? WHERE id = ?",
                         (json.dumps(new_tags), mid))
    conn.commit()
    conn.close()

# ── 旧标签清理 ────────────────────────────────────────────
def cleanup_old_tags():
    """移除 7 天前的 task:* 标签."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, tags FROM memories "
        "WHERE tags LIKE '%task:accepted%' OR tags LIKE '%task:reviewed%'"
    ).fetchall()

    cutoff = datetime.now() - timedelta(days=7)
    removed = 0
    for mid, tags_raw in rows:
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
        except Exception:
            continue
        ts_str = None
        for t in tags:
            if t.startswith("ts:"):
                ts_str = t[3:]
                break
        if not ts_str:
            continue
        try:
            task_time = datetime.strptime(ts_str, "%Y%m%dT%H%M%S")
        except ValueError:
            continue
        if task_time < cutoff:
            new_tags = [t for t in tags
                        if not t.startswith("task:") and not t.startswith("ts:")]
            conn.execute("UPDATE memories SET tags = ? WHERE id = ?",
                         (json.dumps(new_tags), mid))
            removed += 1
    if removed:
        print(f"  [CLEANUP] {removed} old task tags removed (>7 days)")
    conn.commit()
    conn.close()

# ── 健康审计 ──────────────────────────────────────────────
async def run_audit():
    """四维健康审计: trust + pipeline + domain + bridge."""
    scores = {}
    findings = []

    # 1. Trust
    try:
        from plastic_promise.defense.soul_enforcer import TrustManager
        tm = TrustManager()
        trust_vals = [tm.get(r) for r in ("pi_builder", "pi_fixer", "pi_reviewer", "default")]
        scores["trust"] = round(sum(trust_vals) / len(trust_vals), 2)
        if min(trust_vals) < 0.4:
            findings.append({"dim": "trust", "detail": f"min={min(trust_vals):.2f}"})
    except Exception as e:
        scores["trust"] = 0.0
        findings.append({"dim": "trust", "detail": str(e)[:80]})

    # 2. Pipeline
    # Health = 1 - stuck / (total_active + resolved)
    # resolved = done + reviewed tasks (positive signal, prevents small-N bias)
    try:
        conn = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:%'").fetchone()[0]
        stuck = conn.execute("SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:active%'").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:done%' OR tags LIKE '%task:reviewed%'"
        ).fetchone()[0]
        conn.close()
        denom = max(total + resolved, 1)
        scores["pipeline"] = round(1.0 - stuck / denom, 2)
        if stuck > 0:
            findings.append({"dim": "pipeline", "detail": f"{stuck} stuck active of {total} ({total + resolved} moving)",
                             "auto_fix": True})
    except Exception:
        scores["pipeline"] = 1.0

    # 3. Domain
    try:
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()
        dm = getattr(engine, '_dm', None)
        if dm:
            ds = dm.stats()
            active = sum(1 for d in ds.values() if d.get("status") == "active")
            scores["domain"] = round(active / max(len(ds), 1), 2)
        else:
            scores["domain"] = 0.8
    except Exception:
        scores["domain"] = 0.5

    # 4. Bridge (SSE connectivity)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{MCP_URL}/health", timeout=3)
            scores["bridge"] = 1.0 if r.status_code == 200 else 0.0
    except Exception:
        scores["bridge"] = 0.0
        findings.append({"dim": "bridge", "detail": "/health unreachable"})

    overall = round(sum(scores.values()) / max(len(scores), 1), 2)

    # Auto-fix: recover stuck pipeline tasks
    auto_fixes = []
    for f in findings:
        if f.get("auto_fix") and f["dim"] == "pipeline":
            recover_stuck_tasks()
            auto_fixes.append("recovered stuck tasks")

    # Store audit report via MCP /notify (ensures MCP process sees it)
    report = (
        f"AUDIT trust={scores['trust']:.2f} pipeline={scores['pipeline']:.2f} "
        f"domain={scores['domain']:.2f} bridge={scores['bridge']:.2f} "
        f"→ {overall:.2f}"
    )
    if auto_fixes:
        report += f" | fixes: {'; '.join(auto_fixes)}"

    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{MCP_URL}/notify", json={
                "type": "audit_report",
                "content": report,
                "scores": scores,
                "overall": overall,
                "fixes": auto_fixes,
                "ts": datetime.now().isoformat(),
            }, timeout=5)
    except Exception:
        pass  # notify is best-effort

    # Console display
    dims = " ".join(f"{k}={v:.2f}" for k, v in scores.items())
    print(f"\n  AUDIT {dims} → {overall:.2f}")
    if auto_fixes:
        print(f"  Fixes: {'; '.join(auto_fixes)}")

    return {"scores": scores, "overall": overall}

# ── 主循环 ────────────────────────────────────────────────
_audit_seq = [0]

async def main():
    _pid_path = os.path.join(_project_root, "maintenance_daemon.pid")
    with open(_pid_path, "w") as f:
        f.write(str(os.getpid()))

    print(f"Maintenance Daemon (audit={INTERVAL}s, PID={os.getpid()})")
    print(f"  DB: {DB_PATH}")
    print(f"  MCP: {MCP_URL}")

    tick = 0
    audit_threshold = max(1, INTERVAL // 10)  # 每 INTERVAL 秒审计一次

    # 冷启动: 30s 后首次审计
    await asyncio.sleep(30)
    await run_audit()

    while True:
        tick += 1
        if tick >= audit_threshold:
            _audit_seq[0] += 1
            if random.random() < 0.1:  # 10% jitter
                tick = audit_threshold - 1
            else:
                cleanup_old_tags()
                await run_audit()
                tick = 0
        else:
            recover_stuck_tasks()

        await asyncio.sleep(10)  # 10s 粒度

if __name__ == "__main__":
    asyncio.run(main())
