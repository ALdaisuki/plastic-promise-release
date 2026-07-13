"""Maintenance Daemon — 定时健康审计 + GC + 超时恢复 + 免疫安全网

轻量维护进程。MCP Server 是共享记忆唯一真相源，daemon 通过 /notify
写入审计报告确保 MCP 进程可见。多 Agent 协调通过共享记忆池自治，
不在此调度。

原则 #1 奥卡姆剃刀: 从 pi_daemon(410行)+audit_daemon(226行) 砍掉
Pi CLI 死代码 (~200行)，合并为维护守护进程。

Phase: 免疫系统化 + 标签调度 + 全域创新 + Hunter Guild Dispatch — 记忆池质量工程师 + 多Agent调度 + 模式识别:
  - 5 discovery scanners (Task 8+9): scan_trust scan_architecture scan_quality_trends scan_coupling scan_memory_decay
  - AdaptiveThrottle: continuous empty scans double interval (max 8x), hit resets
  - scan_task_heartbeats(): claimed/executing task timeout → release + penalty + escalate
  - MCP /health connectivity check on startup (5 retries, 5s apart)
  - 标签调度引擎: dispatch_fix_task(fixer|reviewer|builder|claude) + tag_for_redo + tag_audit_finding
  - scan_redo_queue()          : 打回区超时升级 (12h→claude提醒, 24h→强制task:pending)
  - scan_duplicate_clusters()  : 检测并清理完全重复的记忆集群
  - scan_stale_worth()         : 复活 (0,0) worth 记录
  - scan_tier_migration()      : 基于访问活动自动升级 tier
  - scan_category_stuck()      : 监控分类队列健康 + 触发 reclassify
  - scan_innovation_opportunities() : 跨域模式识别(recurring_fix/worth_decline/trust/僵尸域/瓶颈)
  - scan_orphan_steps()        : 检测孤儿 step，分级自动修复
  - scan_unclosed_issues()     : 检测超时未闭环 issue
  - scan_llm_classify()        : 后台 LLM 分类队列处理
  - scan_self_noise (in run_audit) : daemon 自身审计去重
"""

# Bootstrap imports intentionally follow the project-root sys.path setup below.
# ruff: noqa: E402, N806, B007

import argparse
import asyncio
import inspect
import json
import os
import secrets
import sqlite3
import sys
import time
from collections.abc import Sequence
from contextlib import redirect_stdout, suppress
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
if __name__ == "__main__":
    from plastic_promise.launcher.default_environment import configure_default_environment

    configure_default_environment(_project_root)
_run_dir = os.path.abspath(
    os.environ.get("PP_MAINTENANCE_RUN_DIR", os.path.join(_project_root, "var", "run"))
)
_pid_path = os.path.join(_run_dir, "maintenance_daemon.pid")
_heartbeat_path = os.path.join(_run_dir, "maintenance_daemon.heartbeat")

# Hunter Guild — 5 discovery scanners (Task 8)
from plastic_promise.core.maintenance_scheduler import (
    AdaptiveThrottle,
    MaintenanceDeadline,
    MaintenanceRegistry,
)
from plastic_promise.core.paths import get_db_path
from plastic_promise.core.project_context import infer_project_context
from plastic_promise.core.synthesis import ensure_synthesis_schema, synthesis_content_hash
from plastic_promise.core.synthesis_maintenance import (
    replay_memory_index_jobs,
    replay_synthesis_index_jobs,
    scan_synthesis_integrity,
)
from plastic_promise.core.synthesis_retrieval import (
    available_ordinary_memory_sql_predicate,
    ordinary_memory_sql_predicate,
)
from plastic_promise.core.traceability import (
    ensure_traceability_schema,
    new_call_id,
    record_call_span,
)
from plastic_promise.cron.scan_architecture import scan_architecture
from plastic_promise.cron.scan_coupling import scan_coupling
from plastic_promise.cron.scan_data_quality import scan_data_quality
from plastic_promise.cron.scan_memory_decay import scan_memory_decay
from plastic_promise.cron.scan_quality_trends import scan_quality_trends
from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
from plastic_promise.cron.scan_trust import scan_trust
from plastic_promise.launcher.service_manager import (
    canonical_source_root,
    resolve_source_revision,
    write_maintenance_heartbeat,
)

DB_PATH = get_db_path()
INTERVAL = int(os.environ.get("AUDIT_INTERVAL_SECONDS", "300"))
MCP_URL = "http://127.0.0.1:9020"


def _connect_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    ensure_synthesis_schema(conn)
    conn.commit()
    return conn


def _patch_ordinary_tags(
    memory_id: str,
    *,
    expected_tags: list[str],
    replacement_tags: list[str],
) -> bool:
    """Apply a tag-only canonical patch without overwriting concurrent changes."""
    return _patch_ordinary_fields(
        memory_id,
        replacements={"tags": replacement_tags},
        expected_tags=expected_tags,
    )


def _patch_ordinary_fields(
    memory_id: str,
    *,
    replacements: dict,
    expected_snapshot: dict | None = None,
    expected_project_id: str | None = None,
    expected_content_hash: str | None = None,
    expected_embedding_hash: str | None = None,
    expected_tags: list[str] | None = None,
    publish_index: bool = False,
) -> bool:
    """Patch one observed ordinary row and optionally queue its checked projection."""
    from plastic_promise.core.context_engine import OrdinaryMemoryConflict, _SQLiteStorage
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    storage = _SQLiteStorage(DB_PATH)
    try:
        with storage.batch():
            canonical = storage.patch_ordinary(
                memory_id,
                replacements=replacements,
                expected_snapshot=expected_snapshot,
                expected_project_id=expected_project_id,
                expected_content_hash=expected_content_hash,
                expected_embedding_hash=expected_embedding_hash,
                expected_tags=expected_tags,
                bump_memory_version=True,
            )
            if publish_index:
                enqueue_memory_index_upsert(
                    storage._conn,
                    memory_id=memory_id,
                    project_id=str(canonical.get("project_id") or ""),
                    expected_embedding_hash=str(canonical.get("embedding_hash") or ""),
                    call_id=f"maintenance:field-patch:{time.time_ns()}",
                )
        return True
    except (OrdinaryMemoryConflict, ValueError):
        return False
    finally:
        storage._conn.close()


# ═══════════════════════════════════════════════════════════════
# Adaptive Throttle — continuous empty scans double interval (max 8x)
# ═══════════════════════════════════════════════════════════════


# Scanner throttles — trust anomalies checked more frequently (300s base)
_scanner_throttles = {
    "scan_architecture": AdaptiveThrottle(600),
    "scan_quality_trends": AdaptiveThrottle(600),
    "scan_coupling": AdaptiveThrottle(600),
    "scan_trust": AdaptiveThrottle(300),
    "scan_memory_decay": AdaptiveThrottle(600),
    "scan_data_quality": AdaptiveThrottle(600),
    "scan_scheduler_health": AdaptiveThrottle(3600),
}


# ── 超时恢复 ──────────────────────────────────────────────
def recover_stuck_tasks():
    """task:active > 5min 或 task:reviewed > 10min → 重置."""
    conn = _connect_memory_db()
    ordinary_guard = ordinary_memory_sql_predicate("memories")
    rows = conn.execute(
        "SELECT id, tags FROM memories "
        "WHERE (tags LIKE '%task:active%' OR tags LIKE '%task:reviewed%') "
        f"AND {ordinary_guard}"
    ).fetchall()

    now = datetime.now()
    changed = 0
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
            new_tags = [
                "task:pending" if t == "task:active" else t for t in tags if not t.startswith("ts:")
            ]
            print(f"  [RECOVER] task:active {elapsed:.0f}s → pending")
        elif "task:reviewed" in tags and elapsed > 600:
            new_tags = [
                "task:active" if t == "task:reviewed" else t
                for t in tags
                if not t.startswith("ts:")
            ]
            print(f"  [RECOVER] task:reviewed {elapsed:.0f}s → active")

        if new_tags:
            changed += int(
                _patch_ordinary_tags(
                    mid,
                    expected_tags=list(tags),
                    replacement_tags=new_tags,
                )
            )
    conn.close()


# ── 旧标签清理 ────────────────────────────────────────────
def cleanup_old_tags():
    """移除 7 天前的 task:* 标签."""
    conn = _connect_memory_db()
    ordinary_guard = ordinary_memory_sql_predicate("memories")
    rows = conn.execute(
        "SELECT id, tags FROM memories "
        "WHERE (tags LIKE '%task:accepted%' OR tags LIKE '%task:reviewed%') "
        f"AND {ordinary_guard}"
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
            new_tags = [t for t in tags if not t.startswith("task:") and not t.startswith("ts:")]
            removed += int(
                _patch_ordinary_tags(
                    mid,
                    expected_tags=list(tags),
                    replacement_tags=new_tags,
                )
            )
    if removed:
        print(f"  [CLEANUP] {removed} old task tags removed (>7 days)")
    conn.close()


# ── 健康审计 ──────────────────────────────────────────────
_last_audit_report = ""  # 自身去重：daemon 不制造重复噪音


async def run_audit():
    """五维健康审计: trust + pipeline + domain + bridge + memory_quality."""
    global _last_audit_report
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
        conn = _connect_memory_db()
        ordinary_guard = ordinary_memory_sql_predicate("memories")
        total = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:%' AND {ordinary_guard}"
        ).fetchone()[0]
        stuck = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE tags LIKE '%task:active%' AND {ordinary_guard}"
        ).fetchone()[0]
        conn.close()
        denom = max(total, 1)
        scores["pipeline"] = round(1.0 - stuck / denom, 2)
        if stuck > 0:
            findings.append(
                {
                    "dim": "pipeline",
                    "detail": f"{stuck} stuck active of {total} total tasks",
                    "auto_fix": True,
                }
            )
    except Exception:
        scores["pipeline"] = 1.0

    # 3. Domain
    try:
        from plastic_promise.core.context_engine import ContextEngine

        engine = ContextEngine()
        dm = getattr(engine, "_dm", None)
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

    # 5. Memory Quality — 实时计算记忆池真实健康度
    try:
        conn = _connect_memory_db()
        ordinary_guard = ordinary_memory_sql_predicate("memories")
        total_mem = conn.execute(
            f"SELECT COUNT(1) FROM memories WHERE {ordinary_guard}"
        ).fetchone()[0]
        zero_worth = conn.execute(
            "SELECT COUNT(1) FROM memories WHERE worth_success=0 AND worth_failure=0 "
            f"AND {ordinary_guard}"
        ).fetchone()[0]
        duplicate_clusters = conn.execute(
            "SELECT COUNT(1) FROM (SELECT content, COUNT(1) as cnt FROM memories "
            f"WHERE {ordinary_guard} GROUP BY content HAVING cnt > 1)"
        ).fetchone()[0]
        conn.close()
        if total_mem > 0:
            worth_health = 1.0 - (zero_worth / total_mem)
            duplicate_penalty = min(0.3, duplicate_clusters * 0.05)
            scores["memory_quality"] = round(max(0.0, worth_health - duplicate_penalty), 2)
        else:
            scores["memory_quality"] = 1.0
        if zero_worth > 50:
            findings.append(
                {
                    "dim": "memory_quality",
                    "detail": f"{zero_worth}/{total_mem} zero-worth, {duplicate_clusters} dup clusters",
                    "auto_fix": True,
                }
            )
    except Exception:
        scores["memory_quality"] = 0.5

    overall = round(sum(scores.values()) / max(len(scores), 1), 2)

    # Auto-fix: recover stuck pipeline tasks
    auto_fixes = []
    for f in findings:
        if f.get("auto_fix") and f["dim"] == "pipeline":
            recover_stuck_tasks()
            auto_fixes.append("recovered stuck tasks")

    # Store audit report — 自身去重：内容相同则不重复存储
    report = (
        f"AUDIT trust={scores['trust']:.2f} pipeline={scores['pipeline']:.2f} "
        f"domain={scores['domain']:.2f} bridge={scores['bridge']:.2f} "
        f"mem_q={scores['memory_quality']:.2f} → {overall:.2f}"
    )
    if auto_fixes:
        report += f" | fixes: {'; '.join(auto_fixes)}"

    report_body = report  # 不含时间戳的纯内容用于去重比较
    notification = {"status": "skipped", "reason": "identical_committed_report"}
    if report_body == _last_audit_report:
        # 与上一轮完全相同 → 仅更新 last_accessed，不存储新记录
        print("\n  AUDIT (skipped store: identical to previous)")
    else:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{MCP_URL}/notify",
                    json={
                        "type": "audit_report",
                        "content": report,
                        "scores": scores,
                        "overall": overall,
                        "fixes": auto_fixes,
                        "ts": datetime.now().isoformat(),
                    },
                    timeout=5,
                )
            response.raise_for_status()
            outcome = response.json()
            if isinstance(outcome, dict) and outcome.get("ok") is True:
                _last_audit_report = report_body
                notification = {"status": "committed", "reason": ""}
            else:
                if isinstance(outcome, dict):
                    persistence = outcome.get("audit_persistence")
                    persistence_reason = (
                        persistence.get("reason") if isinstance(persistence, dict) else ""
                    )
                    reason = (
                        outcome.get("error")
                        or outcome.get("reason")
                        or persistence_reason
                        or "notify_not_committed"
                    )
                else:
                    reason = "notify_response_invalid"
                notification = {"status": "failed", "reason": str(reason)}
                print(f"  [AUDIT] notify not committed: {reason}")
        except Exception as exc:
            notification = {"status": "failed", "reason": exc.__class__.__name__}
            print(f"  [AUDIT] notify failed: {exc}")

    # Console display
    dims = " ".join(f"{k}={v:.2f}" for k, v in scores.items())
    print(f"\n  AUDIT {dims} → {overall:.2f}")
    if auto_fixes:
        print(f"  Fixes: {'; '.join(auto_fixes)}")

    return {"scores": scores, "overall": overall, "notification": notification}


# ═══════════════════════════════════════════════════════════════
# 安全网扫描器 (Safety-Net Daemon)
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 标签调度引擎 — 多 Agent 调度 + 打回区 + 审计记忆化
# ═══════════════════════════════════════════════════════════════

# 调度目标 → Agent 映射
_DISPATCH_MAP = {
    "fixer": {"assignee": "pi_fixer", "domain": "fixing"},
    "reviewer": {"assignee": "pi_reviewer", "domain": "reflecting"},
    "builder": {"assignee": "pi_builder", "domain": "building"},
    "claude": {"assignee": "claude", "domain": "governing"},
}


async def _store_tagged_memory(
    content: str, tags: list, memory_type: str = "experience", target_id: str = ""
):
    """通过 /notify 将带标签的记忆写入 MCP 记忆池，使其可被其他 Agent 召回。

    这是 daemon 的"出声"机制 — 不直接写 SQLite，而是通过 MCP 接口确保
    索引、embedding、质量管道全部触发。
    """
    payload_tags = list(tags)
    if target_id:
        payload_tags.append(f"target:{target_id}")
    payload_tags.append(f"ts:{datetime.now().strftime('%Y%m%dT%H%M%S')}")
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{MCP_URL}/notify",
                json={
                    "type": "memory_store",
                    "content": content,
                    "memory_type": memory_type,
                    "tags": payload_tags,
                    "source": "maintenance_daemon",
                    "ts": datetime.now().isoformat(),
                },
                timeout=5,
            )
        return True
    except Exception as e:
        print(f"  [TAG] store error: {e}")
        return False


async def dispatch_fix_task(
    task_type: str,
    detail: str,
    target_id: str = "",
    assignee: str = "fixer",
    severity: str = "warning",
    redo: bool = False,
):
    """标签驱动的多 Agent 调度引擎 — 通过 /notify 写入带标签的记忆。

    Args:
        task_type: 任务类型 (close_orphan_step / correct_memory / close_stale_issue / gc_run
                    / review_memory / rebuild_memory / classify_stuck)
        detail: 任务描述
        target_id: 目标实体 ID（memory_id / entity_id / issue_id）
        assignee: 调度目标 — "fixer" | "reviewer" | "builder" | "claude"
        severity: "critical" | "warning" | "info"
        redo: 是否同时加入打回区 (redo:required)
    """
    agent = _DISPATCH_MAP.get(assignee, _DISPATCH_MAP["fixer"])
    tags = [
        "task:pending",
        f"assignee:{agent['assignee']}",
        f"domain:{agent['domain']}",
        f"type:{task_type}",
        f"dispatch:{assignee}",
        f"severity:{severity}",
    ]
    if redo:
        tags.append("redo:required")
        tags.append(f"redo:assigned:{agent['assignee']}")

    stored = await _store_tagged_memory(
        content=f"[DISPATCH] {task_type}: {detail}",
        tags=tags,
        memory_type="task",
        target_id=target_id,
    )
    if stored:
        tag_info = f"{assignee}({'redo+' if redo else ''}sev:{severity})"
        print(f"  [DISPATCH] {task_type} → {tag_info} ({detail[:80]})")


async def tag_for_redo(
    memory_id: str, reason: str, assignee: str = "reviewer", severity: str = "warning"
):
    """标记记忆进入打回区 — 写入 redo:required 标签，等待 Agent 认领审查。

    打回区生命周期:
      redo:required → redo:assigned:<agent> → redo:done | redo:escalated
    超时 24h → scan_redo_queue 自动升级为 task:pending
    """
    tags = [
        "redo:required",
        f"redo:assigned:{_DISPATCH_MAP.get(assignee, _DISPATCH_MAP['reviewer'])['assignee']}",
        f"reason:{reason[:60].replace(' ', '_')}",
        f"severity:{severity}",
    ]
    stored = await _store_tagged_memory(
        content=f"[REDO] {reason}: memory_id={memory_id}",
        tags=tags,
        memory_type="task",
        target_id=memory_id,
    )
    if stored:
        print(f"  [REDO] tagged memory for {assignee} review: {memory_id[:20]}... ({reason[:60]})")


async def tag_audit_finding(dimension: str, detail: str, severity: str = "info"):
    """将审计发现存储为可追溯的带标签记忆。

    替代纯 print() 审计输出 — 使 Claude 和其他 Agent 可通过
    memory_recall 回溯 daemon 发现过什么问题。
    """
    tags = [
        "audit:flagged",
        f"audit_dim:{dimension}",
        f"severity:{severity}",
        "source:maintenance_daemon",
    ]
    await _store_tagged_memory(
        content=f"[AUDIT_FINDING] {dimension}: {detail}",
        tags=tags,
        memory_type="reflection",
    )


# ── 打回区扫描 ──────────────────────────────────────────────
async def scan_redo_queue():
    """扫描打回区：超时未处理的 redo 条目自动升级为 task:pending。

    生命周期:
      - redo:required > 24h → 升级为 task:pending + dispatch:fixer (不再等 Reviewer)
      - redo:required > 12h → 追加 dispatch:claude 标签 (提醒 Claude 注意)
    """
    try:
        conn = _connect_memory_db()
        ordinary_guard = ordinary_memory_sql_predicate("memories")
        rows = conn.execute(
            "SELECT id, tags, created_at FROM memories "
            "WHERE tags LIKE '%redo:required%' "
            "AND tags NOT LIKE '%redo:done%' "
            "AND tags NOT LIKE '%redo:escalated%' "
            f"AND {ordinary_guard}"
        ).fetchall()
        conn.close()

        if not rows:
            return

        now = datetime.now()
        escalated = 0
        for mid, tags_raw, created_str in rows:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
                age_h = (now - created_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                continue

            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])

            if age_h > 24:
                # 超时升级 → task:pending（强制调度）
                new_tags = [t for t in tags if t != "redo:required"]
                new_tags.append("redo:escalated")
                new_tags.append("task:pending")
                new_tags.append("assignee:pi_fixer")
                new_tags.append("domain:fixing")
                changed = _patch_ordinary_tags(
                    mid,
                    expected_tags=list(tags),
                    replacement_tags=new_tags,
                )
                escalated += int(changed)
                print(
                    f"  [REDO_QUEUE] escalated stale redo → task:pending "
                    f"({age_h:.0f}h, {mid[:20]}...)"
                )
            elif age_h > 12:
                # 提醒 Claude
                if "dispatch:claude" not in tags:
                    tags.append("dispatch:claude")
                    changed = _patch_ordinary_tags(
                        mid,
                        expected_tags=[tag for tag in tags if tag != "dispatch:claude"],
                        replacement_tags=tags,
                    )
                    if not changed:
                        continue
                    print(
                        f"  [REDO_QUEUE] added claude attention: {mid[:20]}... (age={age_h:.0f}h)"
                    )

        if escalated:
            print(f"  [REDO_QUEUE] escalated {escalated} stale redo items")

    except Exception as e:
        print(f"  [SAFETY_NET] scan_redo_queue error: {e}")


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
            handle_skill_session_complete,
            handle_skill_session_trace,
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
            await handle_skill_session_complete(
                engine,
                {
                    "entity_id": eid,
                    "outcome": f"abandoned: safety-net auto-close after {idle_m:.0f}min idle",
                },
            )
            print(
                f"  [SAFETY_NET] auto-closed orphan step: {skill_name} "
                f"({eid[:30]}..., idle={idle_m:.0f}min)"
            )
        elif idle_m > 30:
            # Tier 2: 发任务给 Pi
            await dispatch_fix_task(
                "close_orphan_step",
                f"孤儿 step: {skill_name} idle={idle_m:.0f}min, entity_id={eid}",
                target_id=eid,
            )
    except Exception as e:
        print(f"  [SAFETY_NET] scan_orphan_steps error: {e}")


# ── 重复集群清理 ───────────────────────────────────────────
async def scan_duplicate_clusters(engine=None):
    """直接 SQL GROUP BY 检测完全重复的记忆内容，保留 worth 最高的一条。

    这是对旧 scan_memory_health 的替代——旧逻辑依赖 GC vector merge
    (cos≥0.70)，但完全相同的文本在向量库里未必会被检测到。
    """
    try:
        conn = None
        mutation_engine = engine
        conn = _connect_memory_db()
        conn.row_factory = sqlite3.Row
        available_guard = available_ordinary_memory_sql_predicate("memories")
        eligible_guard = " AND ".join(
            (
                "typeof(memories.id) = 'text' AND TRIM(memories.id) != ''",
                "typeof(memories.content) = 'text' AND TRIM(memories.content) != ''",
                "typeof(memories.project_id) = 'text' AND TRIM(memories.project_id) != ''",
                "typeof(memories.embedding_hash) = 'text' AND TRIM(memories.embedding_hash) != ''",
                "typeof(memories.created_at) = 'text' AND TRIM(memories.created_at) != ''",
                "typeof(memories.worth_success) IN ('integer', 'real') "
                "AND memories.worth_success >= 0 "
                "AND memories.worth_success < 1.0e308",
                "typeof(memories.worth_failure) IN ('integer', 'real') "
                "AND memories.worth_failure >= 0 "
                "AND memories.worth_failure < 1.0e308",
                "(memories.worth_success + memories.worth_failure) < 1.0e308",
                "typeof(memories.access_count) = 'integer' AND memories.access_count >= 0",
                "typeof(memories.tags) = 'text' AND json_valid(memories.tags) "
                "AND json_type(CASE WHEN json_valid(memories.tags) "
                "THEN memories.tags ELSE 'null' END) = 'array'",
                "typeof(memories.metadata_json) = 'text' "
                "AND json_valid(memories.metadata_json) "
                "AND json_type(CASE WHEN json_valid(memories.metadata_json) "
                "THEN memories.metadata_json ELSE 'null' END) = 'object'",
            )
        )
        clusters = conn.execute(
            "SELECT content, project_id, COUNT(1) AS cnt "
            "FROM memories "
            f"WHERE {eligible_guard} "
            f"AND {available_guard} "
            "GROUP BY project_id, content HAVING cnt > 1 "
            "ORDER BY cnt DESC, project_id, content LIMIT 20"
        ).fetchall()

        owns_engine = mutation_engine is None
        if owns_engine:
            from plastic_promise.core.context_engine import ContextEngine

            mutation_engine = ContextEngine()

        cleaned = 0
        try:

            def observed_precondition(row):
                return {
                    "access_count": row["access_count"],
                    "content_hash": synthesis_content_hash(row["content"]),
                    "created_at": row["created_at"],
                    "decay_multiplier": row["decay_multiplier"],
                    "effective_half_life": row["effective_half_life"],
                    "embedding_hash": row["embedding_hash"],
                    "metadata_json": json.loads(row["metadata_json"]),
                    "project_id": row["project_id"],
                    "tags": json.loads(row["tags"]),
                    "tier": row["tier"],
                    "worth_failure": row["worth_failure"],
                    "worth_success": row["worth_success"],
                }

            for cluster in clusters:
                rows = conn.execute(
                    "SELECT id, content, project_id, tags, metadata_json, "
                    "worth_success, worth_failure, decay_multiplier, "
                    "access_count, created_at, tier, effective_half_life, "
                    "embedding_hash FROM memories "
                    "WHERE content = ? AND project_id = ? "
                    f"AND {eligible_guard} "
                    f"AND {available_guard}",
                    (cluster["content"], cluster["project_id"]),
                ).fetchall()
                cnt = len(rows)
                # 解析每条记录的 worth
                id_list = [row["id"] for row in rows]
                candidate_by_id = {row["id"]: row for row in rows}
                if len(id_list) < 2:
                    continue

                try:

                    def ranking_key(row):
                        success = float(row["worth_success"])
                        failure = float(row["worth_failure"])
                        total = success + failure
                        worth = success / total if total > 0 else 0.5
                        return (
                            worth,
                            int(row["access_count"]),
                            row["created_at"],
                            row["id"],
                        )

                    best_id = max(rows, key=ranking_key)["id"]
                except (OverflowError, TypeError, ValueError):
                    continue

                # SQL discovers candidates; the coordinator owns every write.
                to_forget = [mid for mid in id_list if mid != best_id]
                if not to_forget:
                    continue

                for index, mid in enumerate(to_forget):
                    try:
                        candidate = candidate_by_id[mid]
                        observed = observed_precondition(candidate)
                        survivor = observed_precondition(candidate_by_id[best_id])
                        mutation_engine.mutate_ordinary_source(
                            mid,
                            operation="forgotten",
                            reason="safety-net:duplicate_cluster",
                            actor="maintenance_daemon",
                            call_id=f"maintenance:duplicate:{time.time_ns()}:{index}",
                            expected_project_id=observed.pop("project_id"),
                            expected_content_hash=observed.pop("content_hash"),
                            expected_source_snapshot=observed,
                            expected_peer_snapshots={best_id: survivor},
                            require_source_available=True,
                        )
                        cleaned += 1
                        print(
                            f"  [DUP_CLEAN] forgot duplicate: {mid[:20]}... "
                            f"from cluster of {cnt} (kept {best_id[:20]}...)"
                        )
                    except Exception as e:
                        print(f"  [DUP_CLEAN] forget failed for {mid[:20]}...: {e}")
                        continue

                if cleaned > 0:
                    break  # 一次只清理一个集群
        finally:
            conn.close()
            if owns_engine:
                storage = getattr(mutation_engine, "_sqlite", None)
                owned_conn = getattr(storage, "_conn", None)
                if owned_conn is not None:
                    owned_conn.close()

        if cleaned:
            print(f"  [DUP_CLEAN] cleaned {cleaned} duplicates from cluster size {cnt}")

    except Exception as e:
        if conn is not None:
            conn.close()
        if engine is None and mutation_engine is not None:
            owned_storage = getattr(mutation_engine, "_sqlite", None)
            owned_conn = getattr(owned_storage, "_conn", None)
            if owned_conn is not None:
                owned_conn.close()
        print(f"  [SAFETY_NET] scan_duplicate_clusters error: {e}")


# ── Worth 复活 ──────────────────────────────────────────────
async def scan_stale_worth():
    """复活 worth 系统：对 (0,0) 记忆基于 last_accessed 计算真实 worth。

    规则:
      - last_accessed 7 天内且被访问过 → worth_success=1
      - last_accessed 空/超过 30 天 → worth_failure=1
      - 否则 → worth_success=1 (默认偏乐观)
    """
    try:
        conn = _connect_memory_db()
        ordinary_guard = ordinary_memory_sql_predicate("memories")
        count = conn.execute(
            "SELECT COUNT(1) FROM memories WHERE worth_success=0 AND worth_failure=0 "
            f"AND {ordinary_guard}"
        ).fetchone()[0]
        if count == 0:
            return

        # 只处理前 20 条（渐进修复，避免一次操作太多）
        rows = conn.execute(
            "SELECT id, content, project_id, embedding_hash, last_accessed, created_at, "
            "worth_success, worth_failure FROM memories "
            "WHERE worth_success=0 AND worth_failure=0 "
            f"AND {ordinary_guard} LIMIT 20"
        ).fetchall()
        conn.close()

        updated = 0
        now = datetime.now()
        for (
            mid,
            content,
            project_id,
            embedding_hash,
            last_acc,
            created_str,
            worth_success,
            worth_failure,
        ) in rows:
            try:
                if last_acc and last_acc.strip():
                    try:
                        last_dt = datetime.fromisoformat(last_acc)
                        days_since = (now - last_dt).days
                    except (ValueError, TypeError):
                        days_since = 999
                else:
                    # 从未被访问 → 检查创建时间
                    try:
                        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        if created_dt.tzinfo:
                            created_dt = created_dt.replace(tzinfo=None)
                        days_since = (now - created_dt).days
                    except (ValueError, TypeError):
                        days_since = 999

                if days_since <= 7:
                    new_success, new_failure = 1, 0
                elif days_since > 30:
                    new_success, new_failure = 0, 1
                else:
                    new_success, new_failure = 1, 0  # 默认乐观

                changed = _patch_ordinary_fields(
                    mid,
                    replacements={
                        "worth_success": new_success,
                        "worth_failure": new_failure,
                    },
                    expected_snapshot={
                        "worth_success": worth_success,
                        "worth_failure": worth_failure,
                        "last_accessed": last_acc,
                        "created_at": created_str,
                    },
                    expected_project_id=str(project_id or ""),
                    expected_content_hash=synthesis_content_hash(content),
                    expected_embedding_hash=str(embedding_hash or ""),
                )
                updated += int(changed)
            except Exception:
                pass

        if updated:
            print(f"  [WORTH] revived {updated} stale worth records (remaining: {count - updated})")

    except Exception as e:
        print(f"  [SAFETY_NET] scan_stale_worth error: {e}")


# ── Tier 自动升级 ───────────────────────────────────────────
async def scan_tier_migration():
    """基于访问活动自动升级记忆 tier。

    L1 → L2: last_accessed 7天内
    L2 → L3: access_count > 5 且 last_accessed 3天内
    """
    try:
        ordinary_guard = ordinary_memory_sql_predicate("memories")
        now = datetime.now()
        seven_days_ago = (now - timedelta(days=7)).isoformat()
        three_days_ago = (now - timedelta(days=3)).isoformat()

        def candidates(tier, *, cutoff, require_access=False):
            conn = _connect_memory_db()
            try:
                access_predicate = "AND access_count > 5 " if require_access else ""
                return conn.execute(
                    "SELECT id, content, project_id, embedding_hash, tier, "
                    "last_accessed, access_count FROM memories "
                    "WHERE tier = ? AND last_accessed > ? "
                    f"{access_predicate}AND {ordinary_guard}",
                    (tier, cutoff),
                ).fetchall()
            finally:
                conn.close()

        def promote(rows, target_tier):
            return sum(
                int(
                    _patch_ordinary_fields(
                        mid,
                        replacements={"tier": target_tier},
                        expected_snapshot={
                            "tier": tier,
                            "last_accessed": last_accessed,
                            "access_count": access_count,
                        },
                        expected_project_id=str(project_id or ""),
                        expected_content_hash=synthesis_content_hash(content),
                        expected_embedding_hash=str(embedding_hash or ""),
                        publish_index=True,
                    )
                )
                for (
                    mid,
                    content,
                    project_id,
                    embedding_hash,
                    tier,
                    last_accessed,
                    access_count,
                ) in rows
            )

        upgraded_l1 = promote(candidates("L1", cutoff=seven_days_ago), "L2")
        upgraded_l2 = promote(
            candidates("L2", cutoff=three_days_ago, require_access=True),
            "L3",
        )

        if upgraded_l1 or upgraded_l2:
            print(f"  [TIER] L1→L2: {upgraded_l1}, L2→L3: {upgraded_l2}")

    except Exception as e:
        print(f"  [SAFETY_NET] scan_tier_migration error: {e}")


# ── 分类队列健康 ───────────────────────────────────────────
_category_stuck_cursors: dict[str, str] = {}


async def scan_category_stuck():
    """监控 LLM 分类队列健康：检测长期卡住的 llm_pending 和
    stale 'other' 分类记忆，触发 memory_reclassify。
    """
    try:
        project_id = infer_project_context({}).project_id
        if not project_id or project_id == "project:unknown":
            return
        conn = _connect_memory_db()
        ordinary_guard = ordinary_memory_sql_predicate("memories")

        # 1. 检测 llm_pending 卡住情况 (>1h 未处理)
        stuck_pending = conn.execute(
            "SELECT COUNT(1) FROM memories "
            "WHERE tags LIKE '%llm_pending:true%' "
            "AND tags NOT LIKE '%llm_classified:true%' "
            f"AND {ordinary_guard} AND project_id = ?",
            (project_id,),
        ).fetchone()[0]

        if stuck_pending > 5:
            print(
                f"  [CAT_STUCK] {stuck_pending} memories stuck in LLM queue "
                f"— Ollama may be offline or overloaded"
            )

        # 2. 检测大量 other 分类 (可能是分类管道卡住)
        other_count = conn.execute(
            f"SELECT COUNT(1) FROM memories WHERE category='other' "
            f"AND {ordinary_guard} AND project_id = ?",
            (project_id,),
        ).fetchone()[0]

        # 3. 对长期 other (+ 从未被访问) 触发 reclassify
        if other_count > 20:
            cursor = _category_stuck_cursors.get(project_id, "")
            cursor_clause = "AND id > ? " if cursor else ""
            params = (project_id, cursor) if cursor else (project_id,)
            rows = conn.execute(
                "SELECT id FROM memories WHERE category='other' "
                "AND (last_accessed IS NULL OR last_accessed = '') "
                f"AND {ordinary_guard} AND project_id = ? "
                f"{cursor_clause}ORDER BY id ASC LIMIT 5",
                params,
            ).fetchall()
            if not rows and cursor:
                _category_stuck_cursors.pop(project_id, None)
                rows = conn.execute(
                    "SELECT id FROM memories WHERE category='other' "
                    "AND (last_accessed IS NULL OR last_accessed = '') "
                    f"AND {ordinary_guard} AND project_id = ? "
                    "ORDER BY id ASC LIMIT 5",
                    (project_id,),
                ).fetchall()
            conn.close()

            from plastic_promise.core.context_engine import ContextEngine
            from plastic_promise.mcp.server import _mutation_runtime_context
            from plastic_promise.mcp.tools.memory import handle_memory_reclassify

            engine = ContextEngine()
            reclassified = 0
            for (memory_id,) in rows:
                try:
                    arguments = {"memory_id": memory_id, "project_id": project_id}
                    result = await handle_memory_reclassify(
                        engine,
                        arguments,
                        _runtime_context=_mutation_runtime_context(
                            "memory_reclassify",
                            arguments,
                        ),
                    )
                    payload = json.loads(result[0].text)
                    reclassified += int(payload.get("reclassified", 0) or 0)
                except Exception:
                    continue
            if rows and len(rows) == 5:
                _category_stuck_cursors[project_id] = str(rows[-1][0])
            else:
                _category_stuck_cursors.pop(project_id, None)
            if reclassified:
                print(f"  [CAT_STUCK] reclassified {reclassified} stale 'other' memories")
        else:
            conn.close()

    except Exception as e:
        print(f"  [SAFETY_NET] scan_category_stuck error: {e}")


# ── LLM 后台分类 ───────────────────────────────────────────
_LLM_CLASSIFY_CURSOR_VERSION = "llm-classify-cursor/v1"


def _llm_classify_cursor_path() -> str:
    return os.environ.get("PP_LLM_CLASSIFY_CURSOR_PATH") or os.path.join(
        _run_dir,
        "llm-classify-cursor-v1.json",
    )


def _llm_classify_cursor_binding(project_id: str) -> dict[str, str]:
    return {
        "version": _LLM_CLASSIFY_CURSOR_VERSION,
        "db_path": os.path.realpath(os.path.abspath(os.fspath(DB_PATH))),
        "project_id": project_id,
    }


def _load_llm_classify_cursor(project_id: str) -> tuple[str, str] | None:
    try:
        with open(_llm_classify_cursor_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    binding = _llm_classify_cursor_binding(project_id)
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in binding.items()
    ):
        return None
    cursor = payload.get("cursor")
    if cursor is None:
        return None
    if not isinstance(cursor, dict):
        return None
    created_at = cursor.get("created_at")
    memory_id = cursor.get("id")
    if not isinstance(created_at, str) or not created_at.strip():
        return None
    if not isinstance(memory_id, str) or not memory_id.strip():
        return None
    return created_at, memory_id


def _store_llm_classify_cursor(
    project_id: str,
    cursor: tuple[str, str] | None,
) -> None:
    path = _llm_classify_cursor_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        **_llm_classify_cursor_binding(project_id),
        "cursor": ({"created_at": cursor[0], "id": cursor[1]} if cursor is not None else None),
    }
    temporary = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def scan_llm_classify():
    """后台 LLM 分类：处理标记为 llm_pending 的记忆。

    队列机制:
      - memory_store / memory_reclassify 将低置信度/other 记忆标记为 llm_pending:true
      - 本扫描器定期取 N 条 (默认3)，调用 Ollama LLM 做 6 类文字分类
      - 成功分类后: 更新 category + 替换标签为 llm_classified:true
      - 每个周期最多处理 LLM_BATCH_SIZE 条，避免阻塞其他审计任务

    响应时间: Ollama API 调用约 2-5s/条 (qwen2.5:3b), 不阻塞其他扫描器。
    """
    LLM_BATCH_SIZE = int(os.environ.get("LLM_CLASSIFY_BATCH", "3"))
    OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")

    try:
        runtime_project_id = infer_project_context({}).project_id
        if runtime_project_id in {"", "project:unknown"} or LLM_BATCH_SIZE <= 0:
            return

        conn = _connect_memory_db()
        available_guard = available_ordinary_memory_sql_predicate("memories")
        tag_document = "CASE WHEN json_valid(memories.tags) THEN memories.tags ELSE '[]' END"

        def select_candidates(cursor):
            cursor_clause = ""
            params = [runtime_project_id]
            if cursor is not None:
                cursor_clause = "AND (created_at > ? OR (created_at = ? AND id > ?)) "
                params.extend([cursor[0], cursor[0], cursor[1]])
            params.append(LLM_BATCH_SIZE)
            return conn.execute(
                "SELECT id, content, tags, category, project_id, created_at "
                "FROM memories "
                "WHERE typeof(id) = 'text' "
                "AND typeof(content) = 'text' "
                "AND typeof(tags) = 'text' "
                "AND typeof(category) = 'text' "
                "AND typeof(project_id) = 'text' "
                "AND typeof(created_at) = 'text' "
                "AND project_id = ? "
                "AND TRIM(COALESCE(id, '')) != '' "
                "AND TRIM(COALESCE(content, '')) != '' "
                "AND TRIM(COALESCE(category, '')) != '' "
                "AND TRIM(COALESCE(created_at, '')) != '' "
                f"AND json_type({tag_document}) = 'array' "
                f"AND EXISTS (SELECT 1 FROM json_each({tag_document}) "
                "            WHERE type = 'text' AND value = 'llm_pending:true') "
                f"AND NOT EXISTS (SELECT 1 FROM json_each({tag_document}) "
                "                WHERE type = 'text' AND value = 'llm_classified:true') "
                f"AND NOT EXISTS (SELECT 1 FROM json_each({tag_document}) "
                "                WHERE type != 'text') "
                f"AND {available_guard} "
                f"{cursor_clause}"
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                tuple(params),
            ).fetchall()

        try:
            cursor = _load_llm_classify_cursor(runtime_project_id)
            rows = select_candidates(cursor)
            if not rows and cursor is not None:
                _store_llm_classify_cursor(runtime_project_id, None)
                rows = select_candidates(None)
        finally:
            conn.close()

        if not rows:
            return

        from plastic_promise.smart_extractor import CATEGORY_KEYWORDS, _llm_classify

        allowed_categories = frozenset(CATEGORY_KEYWORDS)
        classified = 0
        for mid, content, tags_raw, old_category, project_id, created_at in rows:
            try:
                _store_llm_classify_cursor(
                    runtime_project_id,
                    (str(created_at), str(mid)),
                )
            except Exception as exc:
                print(f"  [LLM_CLASSIFY] cursor persistence failed: {exc}")
                return
            try:
                tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
                if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                    continue
                expected_tags = list(tags)

                # Call Ollama LLM for classification
                new_cat = _llm_classify(content, OLLAMA_HOST, OLLAMA_MODEL, timeout=15)
                new_cat = str(new_cat or "").strip().casefold()
                if new_cat not in allowed_categories:
                    continue

                if new_cat and new_cat != old_category:
                    print(
                        f"  [LLM_CLASSIFY] {mid[:12]}... {old_category} → {new_cat} "
                        f"({(content or '')[:40]}...)"
                    )

                event = {
                    "type": "llm_classified",
                    "memory_id": mid,
                    "new_category": new_cat,
                    "expected_project_id": project_id,
                    "expected_content_hash": synthesis_content_hash(content),
                    "expected_tags": expected_tags,
                    "expected_category": old_category,
                    "ts": datetime.now().isoformat(),
                }
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{MCP_URL}/notify",
                        json=event,
                        timeout=3,
                    )
                if response.status_code >= 400:
                    continue
                try:
                    outcome = response.json()
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(outcome, dict) or not outcome.get("ok"):
                    continue

                classified += 1

            except Exception as e:
                print(f"  [LLM_CLASSIFY] error for {mid[:12]}...: {e}")

        if classified:
            print(f"  [LLM_CLASSIFY] batch done: {classified} classified")

    except Exception as e:
        print(f"  [SAFETY_NET] scan_llm_classify error: {e}")


# ── 全域模式识别 & 创新提案 ────────────────────────────────
async def scan_innovation_opportunities():
    """跨域模式识别：从记忆池中检测可优化模式并自动提案。

    检测维度:
      1. 重复 Bug 模式 — 同一类 fix/error 反复出现 → 提出架构改良
      2. 记忆池退化趋势 — worth 持续下降/衰减加速 → 提出 GC 参数调优
      3. 技能链断裂 — 多个 session 缺 step → 提出流程改进
      4. 信任分异常 — 某 Agent 信任分持续下降 → 提出行为审计
      5. 僵尸域 — 域访问量归零 > 3天 → 提出域联邦合并
      6. 跨域创新 — 两个不相关域的标签突然共现 → 提出交叉创新点
    """
    INNOVATION_THRESHOLD = int(os.environ.get("INNOVATION_THRESHOLD", "3"))

    try:
        conn = _connect_memory_db()
        ordinary_guard = ordinary_memory_sql_predicate("memories")
        proposals = []

        # 1. 重复 fix/bug 模式 — 同一类 type:correct_memory 记忆 > 阈值
        fix_tasks = conn.execute(
            "SELECT COUNT(1) as cnt, SUBSTR(content, 1, 80) as preview "
            "FROM memories WHERE (tags LIKE '%type:correct_memory%' "
            "OR tags LIKE '%type:close_orphan_step%') "
            f"AND {ordinary_guard} "
            "GROUP BY preview HAVING cnt >= ? ORDER BY cnt DESC LIMIT 3",
            (INNOVATION_THRESHOLD,),
        ).fetchall()
        for cnt, preview in fix_tasks:
            proposals.append(
                {
                    "type": "recurring_fix",
                    "detail": f"同一类修复出现 {cnt} 次: {preview}",
                    "suggestion": "考虑从根因修复：架构 review 或代码重构",
                    "assignee": "reviewer",
                    "severity": "warning" if cnt < 5 else "critical",
                }
            )

        # 2. 记忆池退化趋势 — 最近 100 条记忆的 worth 趋势
        worth_trend = conn.execute(
            "SELECT AVG(CAST(worth_success AS REAL) / "
            "MAX(1, worth_success + worth_failure)) as avg_worth "
            "FROM (SELECT worth_success, worth_failure FROM memories "
            "WHERE worth_success + worth_failure > 0 "
            f"AND {ordinary_guard} "
            "ORDER BY created_at DESC LIMIT 50)"
        ).fetchone()
        if worth_trend and worth_trend[0] is not None:
            avg_w = worth_trend[0]
            if avg_w < 0.45:
                proposals.append(
                    {
                        "type": "worth_decline",
                        "detail": f"最近 50 条记忆平均 worth={avg_w:.2f} (<0.45)",
                        "suggestion": "建议 memory_gc 清理 + 提高 quality_gate 阈值",
                        "assignee": "fixer",
                        "severity": "critical" if avg_w < 0.35 else "warning",
                    }
                )

        # 3. 技能链断裂 — 检测多个 orphan step
        orphan_count = conn.execute(
            "SELECT COUNT(1) FROM memories WHERE tags LIKE '%task:active%' "
            "AND tags NOT LIKE '%task:done%' "
            f"AND {ordinary_guard}"
        ).fetchone()[0]
        if orphan_count > 5:
            proposals.append(
                {
                    "type": "skill_chain_gap",
                    "detail": f"检测到 {orphan_count} 个未闭环的 task:active",
                    "suggestion": "建议审查 DAEMON 超时恢复参数，排查 Pi 执行卡死",
                    "assignee": "reviewer",
                    "severity": "warning",
                }
            )

        # 4. 信任分异常 — 检查 trust_history 趋势
        try:
            trust_row = conn.execute(
                "SELECT target, trust FROM trust_scores "
                "WHERE trust < 0.5 ORDER BY last_updated DESC LIMIT 5"
            ).fetchall()
            if trust_row:
                low_agents = [f"{r[0]}({r[1]:.2f})" for r in trust_row]
                proposals.append(
                    {
                        "type": "trust_decline",
                        "detail": f"低信任分 Agent: {', '.join(low_agents)}",
                        "suggestion": "建议 Claude 审计对应 Agent 的行为记录",
                        "assignee": "claude",
                        "severity": "critical",
                    }
                )
        except Exception:
            pass

        # 5. 僵尸域 — 域标签 > 3天未活动
        try:
            three_days_ago = (datetime.now() - timedelta(days=3)).isoformat()
            zombie_domains = conn.execute(
                "SELECT DISTINCT domain FROM domain_stats "
                "WHERE last_active < ? AND status='active' LIMIT 5",
                (three_days_ago,),
            ).fetchall()
            if zombie_domains:
                zd = [r[0] for r in zombie_domains]
                proposals.append(
                    {
                        "type": "zombie_domain",
                        "detail": f"僵尸域: {', '.join(zd)} (3天无活动)",
                        "suggestion": "建议 domain(action='merge') 合并到活跃域",
                        "assignee": "claude",
                        "severity": "info",
                    }
                )
        except Exception:
            pass

        # 6. 分类管道瓶颈 — 大量 other 未分类
        other_stale = conn.execute(
            "SELECT COUNT(1) FROM memories WHERE category='other' AND created_at < ? "
            f"AND {ordinary_guard}",
            ((datetime.now() - timedelta(hours=2)).isoformat(),),
        ).fetchone()[0]
        if other_stale > 15:
            proposals.append(
                {
                    "type": "classify_bottleneck",
                    "detail": f"{other_stale} 条记忆超过 2h 仍为 'other' 分类",
                    "suggestion": "Ollama 分类管道可能卡住，建议检查 LLM 服务",
                    "assignee": "fixer",
                    "severity": "warning",
                }
            )

        conn.close()

        # 输出提案 — 通过标签调度分发
        for i, prop in enumerate(proposals):
            assignee = prop["assignee"]
            severity = prop["severity"]
            proposal_id = f"innov-{datetime.now().strftime('%Y%m%d%H%M%S')}-{i}"

            await dispatch_fix_task(
                task_type=f"innovation:{prop['type']}",
                detail=f"{prop['detail']} | 建议: {prop['suggestion']}",
                target_id=proposal_id,
                assignee=assignee,
                severity=severity,
            )

        if proposals:
            print(f"  [INNOVATE] detected {len(proposals)} cross-domain patterns")

    except Exception as e:
        print(f"  [SAFETY_NET] scan_innovation_opportunities error: {e}")


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
                await handle_issue_transition(
                    engine,
                    {
                        "issue_id": issue_id,
                        "to_status": "closed",
                        "comment": f"safety-net: auto-closed stale after {age_h:.0f}h",
                    },
                )
                print(
                    f"  [SAFETY_NET] auto-closed stale issue: "
                    f"#{issue_id} ({title}) age={age_h:.0f}h"
                )
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
# Hunter Guild — 任务心跳监控 (Task 9)
# ═══════════════════════════════════════════════════════════════


async def scan_task_heartbeats():
    """Check all claimed/executing tasks for heartbeat timeout.

    Overdue tasks (heartbeat_at + timeout_seconds < now) are released
    back to pending. After 3 escalations, task is re-assigned to claude
    with priority=1. Timeout penalty is applied via HunterPenaltyEngine.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        # Ensure task_queue table exists
        from plastic_promise.core.task_queue_schema import ensure_task_tables

        ensure_task_tables(conn)

        overdue = conn.execute("""
            SELECT id, claimed_by, to_agent, escalation_count, timeout_seconds
            FROM task_queue
            WHERE status IN ('claimed','executing')
            AND datetime(heartbeat_at, '+' || timeout_seconds || ' seconds') < datetime('now')
        """).fetchall()

        if not overdue:
            conn.close()
            return

        for task_id, claimed_by, to_agent, esc_count, timeout_sec in overdue:
            if esc_count + 1 >= 3:
                conn.execute(
                    "UPDATE task_queue SET status='pending', claimed_by=NULL, "
                    "to_agent='claude', priority=1, "
                    "escalation_count=escalation_count+1, "
                    "last_escalation_at=datetime('now') WHERE id=?",
                    (task_id,),
                )
            else:
                conn.execute(
                    "UPDATE task_queue SET status='pending', claimed_by=NULL, "
                    "escalation_count=escalation_count+1, "
                    "last_escalation_at=datetime('now') WHERE id=?",
                    (task_id,),
                )
            conn.commit()
            print(
                f"  [HEARTBEAT] task {task_id[:20]}... overdue → released "
                f"(escalation={esc_count + 1})"
            )

            # Apply timeout penalty
            try:
                from plastic_promise.core.hunter_penalty import HunterPenaltyEngine
                from plastic_promise.defense.soul_enforcer import TrustManager

                tm = TrustManager()
                current = tm.get(claimed_by)
                engine = HunterPenaltyEngine()
                asyncio.ensure_future(
                    engine.apply_penalty(claimed_by, task_id, "unknown", "timeout", current)
                )
            except Exception:
                pass
        conn.close()

    except Exception as e:
        print(f"  [SAFETY_NET] scan_task_heartbeats error: {e}")


# ═══════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════
def expire_pending_memory_proposals(engine):
    """Run the optional Task 7 proposal expiry hook without weakening later stages."""
    try:
        from plastic_promise.core.memory_proposals import expire_memory_proposals
    except (ImportError, AttributeError):
        return {"skipped": "proposal_expiry_unavailable"}
    return expire_memory_proposals(engine)


def _engine_connection(engine):
    sqlite_store = getattr(engine, "_sqlite", None)
    return getattr(sqlite_store, "_conn", None)


def _trace_connection(engine):
    primary = _engine_connection(engine)
    if primary is None:
        return None
    database = primary.execute("PRAGMA database_list").fetchone()
    database_path = str(database[2] or "").strip() if database else ""
    if not database_path:
        return primary
    trace = sqlite3.connect(database_path, check_same_thread=False, timeout=30.0)
    ensure_traceability_schema(trace)
    trace.commit()
    return trace


def _result_counts(result):
    source = result if isinstance(result, dict) else getattr(result, "__dict__", {})
    return {str(key): value for key, value in source.items() if type(value) in (bool, int, float)}


async def run_governed_maintenance_cycle(engine=None, *, outer_parent_call_id=None):
    """Run and durably trace the six governed maintenance stages."""
    if engine is None:
        from plastic_promise.core.context_engine import ContextEngine

        engine = ContextEngine()

    cycle_call_id = new_call_id()
    parent_call_id = str(outer_parent_call_id or "")
    report = {
        "cycle_call_id": cycle_call_id,
        "status": "running",
        "order": [],
        "results": {},
        "errors": {},
    }
    primary_conn = _engine_connection(engine)
    conn = _trace_connection(engine)
    if conn is not None:
        ensure_traceability_schema(conn)
        record_call_span(
            conn,
            call_id=cycle_call_id,
            parent_call_id=parent_call_id,
            tool_name="maintenance_daemon",
            stage_name="maintenance_cycle",
            caller="maintenance-daemon",
            status="running",
            metadata={"stage_count": 6, "completed_count": 0, "error_count": 0},
        )

    stages = (
        ("memory_lifecycle", lambda: scan_memory_decay(engine)),
        ("proposal_expiry", lambda: expire_pending_memory_proposals(engine)),
        ("synthesis_integrity", lambda: scan_synthesis_integrity(engine)),
        ("memory_index_replay", lambda: replay_memory_index_jobs(engine)),
        ("synthesis_index_replay", lambda: replay_synthesis_index_jobs(engine)),
        ("audit", run_audit),
    )
    for order, (stage, runner) in enumerate(stages, 1):
        report["order"].append(stage)
        stage_status = "success"
        stage_metadata = {"order": order}
        try:
            result = runner()
            if inspect.isawaitable(result):
                result = await result
            if primary_conn is not None and primary_conn.in_transaction:
                primary_conn.rollback()
                raise RuntimeError("maintenance_stage_left_open_transaction")
            report["results"][stage] = result
            stage_metadata["counts"] = _result_counts(result)
        except Exception as exc:
            if primary_conn is not None and primary_conn.in_transaction:
                primary_conn.rollback()
            error_class = exc.__class__.__name__
            report["errors"][stage] = error_class
            stage_status = "error"
            stage_metadata["error_class"] = error_class
        if conn is not None:
            record_call_span(
                conn,
                call_id=new_call_id(),
                parent_call_id=cycle_call_id,
                tool_name="maintenance_daemon",
                stage_name=stage,
                caller="maintenance-daemon",
                status=stage_status,
                degraded=stage_status == "error",
                metadata=stage_metadata,
            )

    success_count = len(stages) - len(report["errors"])
    if not report["errors"]:
        report["status"] = "success"
    elif success_count:
        report["status"] = "partial"
    else:
        report["status"] = "error"
    if conn is not None:
        record_call_span(
            conn,
            call_id=cycle_call_id,
            parent_call_id=parent_call_id,
            tool_name="maintenance_daemon",
            stage_name="maintenance_cycle",
            caller="maintenance-daemon",
            status=report["status"],
            degraded=bool(report["errors"]),
            metadata={
                "stage_count": len(stages),
                "completed_count": success_count,
                "error_count": len(report["errors"]),
                "error_classes": dict(report["errors"]),
            },
        )
    if conn is not None and conn is not primary_conn:
        conn.close()
    return report


def _maintenance_engine():
    from plastic_promise.core.context_engine import ContextEngine

    return ContextEngine()


async def _isolated_job(results, name, runner):
    try:
        result = runner()
        if inspect.isawaitable(result):
            result = await result
        results[name] = result
    except Exception as exc:
        results[name] = {"error_class": exc.__class__.__name__}


async def run_safety_net_cycle(engine=None):
    """Run safety-net scanners without allowing one failure to stop later work."""
    engine = engine or _maintenance_engine()
    results = {}
    discovery = (
        ("scan_trust", lambda: scan_trust(engine)),
        ("scan_architecture", lambda: scan_architecture(engine)),
        ("scan_quality_trends", lambda: scan_quality_trends(engine)),
        ("scan_coupling", lambda: scan_coupling(engine)),
        ("scan_memory_decay", lambda: scan_memory_decay(engine)),
    )
    for name, runner in discovery:
        await _isolated_job(results, name, runner)
        outcome = results[name]
        findings = outcome.get("findings", 0) if isinstance(outcome, dict) else 0
        throttle = _scanner_throttles[name]
        throttle.on_hit() if findings else throttle.on_empty()

    jobs = (
        ("task_heartbeats", scan_task_heartbeats),
        ("innovation", scan_innovation_opportunities),
        ("duplicate_clusters", lambda: scan_duplicate_clusters(engine)),
        ("stale_worth", scan_stale_worth),
        ("tier_migration", scan_tier_migration),
        ("category_stuck", scan_category_stuck),
        ("redo_queue", scan_redo_queue),
        ("orphan_steps", scan_orphan_steps),
        ("unclosed_issues", scan_unclosed_issues),
        ("llm_classify", scan_llm_classify),
        ("stuck_tasks", recover_stuck_tasks),
    )
    for name, runner in jobs:
        await _isolated_job(results, name, runner)
    return results


async def _run_scheduler_health_job(throttle=None):
    result = await scan_scheduler_health(_maintenance_engine())
    throttle = throttle or _scanner_throttles["scan_scheduler_health"]
    throttle.on_hit() if result.get("findings", 0) else throttle.on_empty()
    return result


async def _run_data_quality_job(throttle=None):
    result = scan_data_quality(_maintenance_engine())
    throttle = throttle or _scanner_throttles["scan_data_quality"]
    throttle.on_hit() if len(result) else throttle.on_empty()
    return {"findings": len(result)}


async def _run_audit_job():
    cleanup_old_tags()
    return await run_audit()


def build_maintenance_registry(
    *,
    now,
    heartbeat_path=_heartbeat_path,
    startup_replay_cycle_id,
    engine=None,
):
    """Eagerly construct the stable independent maintenance job registry."""
    safety_interval = int(os.environ.get("SAFETY_NET_INTERVAL", "600"))
    data_quality_interval = int(os.environ.get("DATA_QUALITY_INTERVAL", "600"))
    scheduler_interval = int(os.environ.get("SCHEDULER_HEALTH_INTERVAL", "3600"))
    intervals = {
        "audit": INTERVAL,
        "governed_maintenance": INTERVAL,
        "safety_net": safety_interval,
        "heartbeat": 10,
        "scheduler_health": scheduler_interval,
        "scan_data_quality": data_quality_interval,
    }
    throttles = {name: AdaptiveThrottle(seconds) for name, seconds in intervals.items()}

    async def heartbeat():
        write_maintenance_heartbeat(
            heartbeat_path,
            pid=os.getpid(),
            startup_replay_cycle_id=startup_replay_cycle_id,
        )
        return {"pid": os.getpid(), "startup_replay_cycle_id": startup_replay_cycle_id}

    runners = {
        "audit": _run_audit_job,
        "governed_maintenance": lambda: run_governed_maintenance_cycle(engine),
        "safety_net": lambda: run_safety_net_cycle(engine),
        "heartbeat": heartbeat,
        "scheduler_health": lambda: _run_scheduler_health_job(throttles["scheduler_health"]),
        "scan_data_quality": lambda: _run_data_quality_job(throttles["scan_data_quality"]),
    }
    jobs = [
        MaintenanceDeadline(
            name=name,
            interval=throttles[name],
            next_deadline=now + throttles[name].current,
            runner=runners[name],
        )
        for name in intervals
    ]
    return MaintenanceRegistry(jobs)


def _supported_mcp_url(value):
    parsed = urlparse(str(value or ""))
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.path.rstrip("/") != "/mcp"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("unsupported MCP URL")
    return parsed.geturl()


def parse_daemon_args(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(description="Plastic Promise maintenance daemon")
    parser.add_argument(
        "--mcp-url",
        type=_supported_mcp_url,
        default="http://127.0.0.1:9020/mcp",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--source-root", default=_project_root)
    parser.add_argument("--source-revision", default=resolve_source_revision(_project_root))
    args = parser.parse_args(argv)
    current_revision = resolve_source_revision(_project_root)
    if canonical_source_root(args.source_root) != canonical_source_root(_project_root):
        parser.error("maintenance_source_root_mismatch")
    if current_revision is None or args.source_revision != current_revision:
        parser.error("maintenance_source_revision_mismatch")
    validation = validate_daemon_once_arguments(vars(args))
    if validation.get("ok") is not True:
        parser.error(str(validation["error"]))
    return args


def validate_daemon_once_arguments(arguments):
    once = arguments.get("once") is True
    emit_json = arguments.get("json") is True
    mcp_url = arguments.get("mcp_url")
    if once:
        try:
            _supported_mcp_url(mcp_url)
        except (argparse.ArgumentTypeError, ValueError):
            return {"ok": False, "error": "daemon_once_arguments_invalid"}
        if not emit_json:
            return {"ok": False, "error": "daemon_once_arguments_invalid"}
    elif emit_json:
        return {"ok": False, "error": "daemon_once_arguments_invalid"}
    return {"ok": True}


def _mcp_health_url(mcp_url):
    parsed = urlparse(mcp_url)
    return parsed._replace(path="/health", params="", query="", fragment="").geturl()


def _close_maintenance_engine(engine):
    for attribute in ("_ldb", "_dm", "_code_index", "_rust_engine_instance"):
        resource = getattr(engine, attribute, None)
        close = getattr(resource, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
    sqlite_store = getattr(engine, "_sqlite", None)
    close = getattr(sqlite_store, "close", None)
    if callable(close):
        with suppress(Exception):
            close()
    else:
        conn = getattr(sqlite_store, "_conn", None)
        if conn is not None:
            with suppress(Exception):
                conn.close()


async def run_warmup(engine, *, mcp_url):
    mcp_ok = False
    for attempt in range(5):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(_mcp_health_url(mcp_url), timeout=3)
                if response.status_code == 200:
                    mcp_ok = True
                    break
        except Exception:
            pass
        if attempt < 4:
            await asyncio.sleep(5)
    if not mcp_ok:
        print("  WARNING: MCP /health unreachable; local maintenance will continue")

    try:
        from plastic_promise.core.task_queue_schema import ensure_task_tables

        conn = sqlite3.connect(DB_PATH)
        ensure_task_tables(conn)
        conn.close()
    except Exception:
        pass
    return await run_governed_maintenance_cycle(
        engine,
        outer_parent_call_id=f"daemon-startup:{os.getpid()}",
    )


async def run_forever(registry):
    while True:
        now = time.monotonic()
        outcomes = await registry.run_due(now)
        for outcome in outcomes:
            if outcome.get("status") == "error":
                print(
                    f"  [MAINTENANCE] {outcome['name']} error: "
                    f"{outcome.get('error_class', 'unknown')}"
                )
        delay = registry.next_delay(time.monotonic(), maximum=10.0)
        await asyncio.sleep(max(0.1, delay))


def _replay_failed(result, stage):
    errors = result.get("errors") if isinstance(result, dict) else None
    if isinstance(errors, dict) and stage in errors:
        return True
    results = result.get("results") if isinstance(result, dict) else None
    replay = results.get(stage) if isinstance(results, dict) else None
    failed = replay.get("failed") if isinstance(replay, dict) else getattr(replay, "failed", None)
    return type(failed) is not int or failed != 0


async def daemon_main(argv: Sequence[str] | None = None) -> int:
    args = parse_daemon_args(argv)
    os.environ.setdefault("PLASTIC_PROCESS_GENERATION", secrets.token_hex(16))
    global MCP_URL
    MCP_URL = args.mcp_url.removesuffix("/mcp")
    os.makedirs(_run_dir, exist_ok=True)
    engine = _maintenance_engine()
    try:
        now = time.monotonic()
        if args.once:
            registry = build_maintenance_registry(
                now=now,
                heartbeat_path=_heartbeat_path,
                startup_replay_cycle_id=f"daemon-once:{os.getpid()}",
                engine=engine,
            )
            for job in registry.jobs:
                if job.name == "governed_maintenance":
                    job.next_deadline = now
            with redirect_stdout(sys.stderr):
                outcomes = await registry.run_due(now)
            governed = next(
                (item for item in outcomes if item.get("name") == "governed_maintenance"),
                None,
            )
            result = governed.get("result") if isinstance(governed, dict) else None
            success = (
                isinstance(governed, dict)
                and governed.get("status") == "success"
                and isinstance(result, dict)
                and not _replay_failed(result, "memory_index_replay")
                and not _replay_failed(result, "synthesis_index_replay")
            )
            payload = {
                "schema": "daemon-once/v1",
                "ok": success,
                "pid": os.getpid(),
                "mcp_url": args.mcp_url,
                "cycle": result,
            }
            print(json.dumps(payload, ensure_ascii=False, default=str))
            return 0 if success else 1

        with open(_pid_path, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        startup = await run_warmup(engine, mcp_url=args.mcp_url)
        startup_cycle_id = startup["cycle_call_id"]
        write_maintenance_heartbeat(
            _heartbeat_path,
            pid=os.getpid(),
            startup_replay_cycle_id=startup_cycle_id,
        )
        registry = build_maintenance_registry(
            now=now,
            heartbeat_path=_heartbeat_path,
            startup_replay_cycle_id=startup_cycle_id,
            engine=engine,
        )
        print(
            f"Maintenance Daemon (independent deadlines, PID={os.getpid()}, "
            f"startup_cycle={startup_cycle_id})"
        )
        print(f"  DB: {DB_PATH}")
        print(f"  MCP: {MCP_URL}")
        await run_forever(registry)
        return 0
    finally:
        _close_maintenance_engine(engine)


async def main():
    return await daemon_main([])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(daemon_main()))
