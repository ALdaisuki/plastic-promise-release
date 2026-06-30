"""Maintenance Daemon — 定时健康审计 + GC + 超时恢复 + 安全网扫描

轻量维护进程。MCP Server 是共享记忆唯一真相源，daemon 通过 /notify
写入审计报告确保 MCP 进程可见。多 Agent 协调通过共享记忆池自治，
不在此调度。

原则 #1 奥卡姆剃刀: 从 pi_daemon(410行)+audit_daemon(226行) 砍掉
Pi CLI 死代码 (~200行)，合并为 ~180 行维护守护进程。

Phase: Safety-Net Daemon — 新增三个安全网扫描器:
  - scan_orphan_steps()     : 检测孤儿 step，分级自动修复
  - scan_memory_health()    : 检测低 worth / 重复 / 衰减记忆
  - scan_unclosed_issues()  : 检测超时未闭环 issue
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
    try:
        conn = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:%'").fetchone()[0]
        stuck = conn.execute("SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:active%'").fetchone()[0]
        conn.close()
        denom = max(total, 1)
        scores["pipeline"] = round(1.0 - stuck / denom, 2)
        if stuck > 0:
            findings.append({"dim": "pipeline", "detail": f"{stuck} stuck active of {total} total tasks",
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


# ═══════════════════════════════════════════════════════════════
# 安全网扫描器 (Safety-Net Daemon)
# ═══════════════════════════════════════════════════════════════

# ── 修复任务发布器 ─────────────────────────────────────────
async def dispatch_fix_task(task_type: str, detail: str, target_id: str = ""):
    """发布修复任务到 /notify，由 Pi Daemon 通过标签调度认领。

    Args:
        task_type: 任务类型 (close_orphan_step / correct_memory / close_stale_issue / gc_run)
        detail: 任务描述
        target_id: 目标实体 ID（可选，如 entity_id / memory_id / issue_id）
    """
    tags = [
        "task:pending",
        "assignee:pi_fixer",
        "domain:fixing",
        f"type:{task_type}",
        f"ts:{datetime.now().strftime('%Y%m%dT%H%M%S')}",
    ]
    if target_id:
        tags.append(f"target:{target_id}")
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{MCP_URL}/notify", json={
                "type": "fix_task",
                "task_type": task_type,
                "content": detail,
                "tags": tags,
                "source": "safety_net_daemon",
                "ts": datetime.now().isoformat(),
            }, timeout=5)
        print(f"  [SAFETY_NET] dispatched: {task_type} → pi_fixer ({detail[:80]})")
    except Exception as e:
        print(f"  [SAFETY_NET] dispatch error: {e}")


# ── 孤儿 step 扫描 ─────────────────────────────────────────
async def scan_orphan_steps():
    """检测孤儿 step (status=active 但 >30min 未更新) 并自动修复。

    分级处理:
      - idle > 120min → 自动 skill_session_complete(abandoned)
      - idle > 30min  → 发 task:pending 让 Pi 处理
    """
    try:
        from plastic_promise.core.context_engine import ContextEngine
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_trace,
            handle_skill_session_complete,
        )
        engine = ContextEngine()
        trace_args = {"session_scope": "all", "include_auto_inject": False}
        result_list = await handle_skill_session_trace(engine, trace_args)
        if not result_list:
            return
        data = json.loads(result_list[0].text)
        gaps = data.get("gaps", [])
        orphans = [g for g in gaps if g.get("type") == "orphan_active"]
        if not orphans:
            return

        # 只处理最老的孤儿（安全限制：一次一条）
        orphan = max(orphans, key=lambda g: g.get("idle_minutes", 0))
        eid = orphan["entity_id"]
        idle_m = orphan["idle_minutes"]
        skill_name = orphan.get("skill_name", "unknown")

        if idle_m > 120:
            # Tier 1: 自动关闭
            await handle_skill_session_complete(engine, {
                "entity_id": eid,
                "outcome": f"abandoned: safety-net auto-close after {idle_m:.0f}min idle",
            })
            print(f"  [SAFETY_NET] auto-closed orphan step: {skill_name} "
                  f"({eid[:30]}..., idle={idle_m:.0f}min)")
        elif idle_m > 30:
            # Tier 2: 发任务给 Pi
            await dispatch_fix_task(
                "close_orphan_step",
                f"孤儿 step: {skill_name} idle={idle_m:.0f}min, entity_id={eid}",
                target_id=eid,
            )
    except Exception as e:
        print(f"  [SAFETY_NET] scan_orphan_steps error: {e}")


# ── 记忆健康扫描 ───────────────────────────────────────────
async def scan_memory_health():
    """检测问题记忆（低 worth / 重复 / 大量衰减）并自动修复。

    分级处理:
      - worth < 0.15              → 自动 memory_forget
      - 重复记忆 (merge 候选)     → 自动 forget 低分那条
      - worth 0.15~0.30           → 发 task:pending 让 Pi 审查
      - 大量衰减 (candidates>10)  → 发 task:pending 让 Pi 跑 GC
    """
    try:
        from plastic_promise.core.context_engine import ContextEngine
        from plastic_promise.mcp.tools.memory import handle_memory_gc, handle_memory_forget
        engine = ContextEngine()

        # 1. 扫描低 worth 记忆 (SQLite 直查)
        conn = sqlite3.connect(DB_PATH)
        low_rows = conn.execute(
            "SELECT id, content, worth_score FROM memories "
            "WHERE worth_score < 0.3 ORDER BY worth_score ASC LIMIT 5"
        ).fetchall()
        conn.close()

        fixed = False
        for mid, content, worth in low_rows:
            if fixed:
                break

            if worth < 0.15:
                # Tier 1: 直接清理
                try:
                    await handle_memory_forget(engine, {
                        "memory_id": mid,
                        "reason": "safety-net: worth too low (<0.15)",
                    })
                    print(f"  [SAFETY_NET] auto-forgot low-worth memory: "
                          f"{mid[:20]}... (worth={worth:.2f})")
                    fixed = True
                except Exception as e:
                    print(f"  [SAFETY_NET] forget failed: {e}")
            elif worth < 0.30:
                # Tier 2: 发任务给 Pi
                preview = (content or "")[:60]
                await dispatch_fix_task(
                    "correct_memory",
                    f"低 worth 记忆: {preview} (worth={worth:.2f})",
                    target_id=mid,
                )
                fixed = True

        if fixed:
            return

        # 2. 扫描重复/衰减记忆 (MCP memory_gc dry_run)
        gc_result_list = await handle_memory_gc(engine, {"dry_run": True})
        if not gc_result_list:
            return
        gc_data = json.loads(gc_result_list[0].text)
        merge_candidates = gc_data.get("merge", {}).get("merged_pairs", [])
        decay_count = gc_data.get("candidates_count", 0)

        # 处理重复记忆：只处理第一对
        if merge_candidates:
            pair = merge_candidates[0]
            c1 = pair.get("candidate_1", {})
            c2 = pair.get("candidate_2", {})
            w1 = c1.get("worth_score", 0.5)
            w2 = c2.get("worth_score", 0.5)
            loser = c1 if w1 < w2 else c2
            loser_id = loser.get("id", "")
            if loser_id:
                try:
                    await handle_memory_forget(engine, {
                        "memory_id": loser_id,
                        "reason": "safety-net: duplicate (merged by GC)",
                    })
                    print(f"  [SAFETY_NET] auto-forgot duplicate memory: {loser_id[:20]}...")
                    return
                except Exception as e:
                    print(f"  [SAFETY_NET] forget duplicate error: {e}")

        # 大量衰减记忆 → 发任务让 Pi 审查后跑 GC
        if decay_count > 10:
            await dispatch_fix_task(
                "gc_run",
                f"大量衰减记忆: {decay_count} candidates，建议审查后执行 memory_gc(dry_run=false)",
                target_id="",
            )

    except Exception as e:
        print(f"  [SAFETY_NET] scan_memory_health error: {e}")


# ── 未闭环 issue 扫描 ──────────────────────────────────────
async def scan_unclosed_issues():
    """检测长时间未闭环的 issue 并自动修复。

    分级处理:
      - open > 48h → 自动 issue_transition → closed (stale)
      - open > 24h → 发 task:pending 让 Pi 处理
    """
    try:
        from plastic_promise.core.context_engine import ContextEngine
        from plastic_promise.mcp.tools.management import (
            handle_issue_list,
            handle_issue_transition,
        )
        engine = ContextEngine()
        result_list = await handle_issue_list(engine, {"status": "open"})
        if not result_list:
            return
        data = json.loads(result_list[0].text)
        issues = data.get("issues", []) if isinstance(data, dict) else []
        if not issues:
            return

        now = datetime.now()
        for issue in issues:
            created_str = issue.get("created_at", "")
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if created.tzinfo is not None:
                    created = created.replace(tzinfo=None)
                age_h = (now - created).total_seconds() / 3600
            except (ValueError, TypeError):
                continue

            issue_id = issue.get("id", "")
            title = issue.get("title", "")[:60]

            if age_h > 48:
                await handle_issue_transition(engine, {
                    "issue_id": issue_id,
                    "to_status": "closed",
                    "comment": f"safety-net: auto-closed stale after {age_h:.0f}h",
                })
                print(f"  [SAFETY_NET] auto-closed stale issue: "
                      f"#{issue_id} ({title}) age={age_h:.0f}h")
                return  # 一次只处理一个
            elif age_h > 24:
                await dispatch_fix_task(
                    "close_stale_issue",
                    f"超时未闭环 issue #{issue_id}: {title} (age={age_h:.0f}h)",
                    target_id=str(issue_id),
                )
                return
    except Exception as e:
        print(f"  [SAFETY_NET] scan_unclosed_issues error: {e}")


# ═══════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════
_audit_seq = [0]

async def main():
    _pid_path = os.path.join(_project_root, "maintenance_daemon.pid")
    with open(_pid_path, "w") as f:
        f.write(str(os.getpid()))

    # 安全网扫描间隔 (秒)，可通过环境变量覆盖
    SAFETY_NET_INTERVAL = int(os.environ.get("SAFETY_NET_INTERVAL", "600"))
    safety_net_threshold = max(1, SAFETY_NET_INTERVAL // 10)

    print(f"Maintenance Daemon (audit={INTERVAL}s, safety_net={SAFETY_NET_INTERVAL}s, "
          f"PID={os.getpid()})")
    print(f"  DB: {DB_PATH}")
    print(f"  MCP: {MCP_URL}")

    tick = 0
    audit_threshold = max(1, INTERVAL // 10)  # 每 INTERVAL 秒审计一次

    # 冷启动: 30s 后首次审计，60s 后首次安全网扫描
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
        elif tick % safety_net_threshold == 0:
            # 安全网扫描 — 三个扫描器顺序执行，互不阻塞
            try:
                await scan_orphan_steps()
            except Exception:
                pass  # 单个扫描器失败不影响后续
            try:
                await scan_memory_health()
            except Exception:
                pass
            try:
                await scan_unclosed_issues()
            except Exception:
                pass
        else:
            recover_stuck_tasks()

        await asyncio.sleep(10)  # 10s 粒度

if __name__ == "__main__":
    asyncio.run(main())
