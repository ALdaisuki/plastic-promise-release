"""Maintenance Daemon — 定时健康审计 + GC + 超时恢复 + 免疫安全网

轻量维护进程。MCP Server 是共享记忆唯一真相源，daemon 通过 /notify
写入审计报告确保 MCP 进程可见。多 Agent 协调通过共享记忆池自治，
不在此调度。

原则 #1 奥卡姆剃刀: 从 pi_daemon(410行)+audit_daemon(226行) 砍掉
Pi CLI 死代码 (~200行)，合并为维护守护进程。

Phase: 免疫系统化 — 记忆池质量工程师:
  - scan_duplicate_clusters()  : 检测并清理完全重复的记忆集群
  - scan_stale_worth()         : 复活 (0,0) worth 记录
  - scan_tier_migration()      : 基于访问活动自动升级 tier
  - scan_category_stuck()      : 监控分类队列健康 + 触发 reclassify
  - scan_orphan_steps()        : 检测孤儿 step，分级自动修复
  - scan_unclosed_issues()     : 检测超时未闭环 issue
  - scan_llm_classify()        : 后台 LLM 分类队列处理
  - scan_self_noise (in run_audit) : daemon 自身审计去重
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

    # 5. Memory Quality — 实时计算记忆池真实健康度
    try:
        conn = sqlite3.connect(DB_PATH)
        total_mem = conn.execute("SELECT COUNT(1) FROM memories").fetchone()[0]
        zero_worth = conn.execute(
            "SELECT COUNT(1) FROM memories WHERE worth_success=0 AND worth_failure=0"
        ).fetchone()[0]
        duplicate_clusters = conn.execute(
            "SELECT COUNT(1) FROM (SELECT content, COUNT(1) as cnt FROM memories "
            "GROUP BY content HAVING cnt > 1)"
        ).fetchone()[0]
        conn.close()
        if total_mem > 0:
            worth_health = 1.0 - (zero_worth / total_mem)
            duplicate_penalty = min(0.3, duplicate_clusters * 0.05)
            scores["memory_quality"] = round(max(0.0, worth_health - duplicate_penalty), 2)
        else:
            scores["memory_quality"] = 1.0
        if zero_worth > 50:
            findings.append({"dim": "memory_quality",
                             "detail": f"{zero_worth}/{total_mem} zero-worth, {duplicate_clusters} dup clusters",
                             "auto_fix": True})
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
    if report_body == _last_audit_report:
        # 与上一轮完全相同 → 仅更新 last_accessed，不存储新记录
        print(f"\n  AUDIT (skipped store: identical to previous)")
    else:
        _last_audit_report = report_body
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


# ── 重复集群清理 ───────────────────────────────────────────
async def scan_duplicate_clusters():
    """直接 SQL GROUP BY 检测完全重复的记忆内容，保留 worth 最高的一条。

    这是对旧 scan_memory_health 的替代——旧逻辑依赖 GC vector merge
    (cos≥0.70)，但完全相同的文本在向量库里未必会被检测到。
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        clusters = conn.execute(
            "SELECT content, COUNT(1) as cnt, GROUP_CONCAT(id) as ids, "
            "GROUP_CONCAT(worth_success || '/' || worth_failure || '|' || id) as worth_info "
            "FROM memories "
            "WHERE content IS NOT NULL AND content != '' "
            "GROUP BY content HAVING cnt > 1 ORDER BY cnt DESC LIMIT 20"
        ).fetchall()
        conn.close()

        cleaned = 0
        for content, cnt, ids_str, worth_info in clusters:
            # 解析每条记录的 worth
            id_list = ids_str.split(",")
            if len(id_list) < 2:
                continue

            best_id = None
            best_worth = -1.0
            for mid in id_list:
                total = 0
                success = 0
                # 从 worth_info 中提取对应 id 的 worth
                for entry in worth_info.split(","):
                    parts = entry.split("|")
                    if len(parts) >= 2 and parts[1] == mid:
                        frac = parts[0].split("/")
                        if len(frac) == 2:
                            try:
                                s, f_val = int(frac[0]), int(frac[1])
                                total = s + f_val
                                success = s
                            except ValueError:
                                pass
                        break
                w = success / total if total > 0 else 0.5
                if w > best_worth:
                    best_worth = w
                    best_id = mid

            # 清理除 best 外的所有重复
            to_forget = [mid for mid in id_list if mid != best_id]
            if not to_forget:
                continue

            for mid in to_forget:
                try:
                    conn2 = sqlite3.connect(DB_PATH)
                    # 软删除：标记为 decaying + 设置 forget reason
                    conn2.execute(
                        "UPDATE memories SET tags = json_set("
                        "  COALESCE(tags, '[]'), '$[#]', 'decaying', '$[#]', "
                        "  'forget:safety-net:duplicate_cluster'"
                        ") WHERE id = ?",
                        (mid,)
                    )
                    # 直接删除（SQLite 层面清除，下次 GC 会清理 LanceDB）
                    conn2.execute("DELETE FROM memories WHERE id = ?", (mid,))
                    conn2.commit()
                    conn2.close()
                    cleaned += 1
                    print(f"  [DUP_CLEAN] forgot duplicate: {mid[:20]}... "
                          f"from cluster of {cnt} (kept {best_id[:20]}...)")
                except Exception as e:
                    print(f"  [DUP_CLEAN] forget failed for {mid[:20]}...: {e}")
                    continue

            if cleaned > 0:
                break  # 一次只清理一个集群

        if cleaned:
            print(f"  [DUP_CLEAN] cleaned {cleaned} duplicates from cluster size {cnt}")

    except Exception as e:
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
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute(
            "SELECT COUNT(1) FROM memories WHERE worth_success=0 AND worth_failure=0"
        ).fetchone()[0]
        if count == 0:
            return

        # 只处理前 20 条（渐进修复，避免一次操作太多）
        rows = conn.execute(
            "SELECT id, last_accessed, created_at FROM memories "
            "WHERE worth_success=0 AND worth_failure=0 LIMIT 20"
        ).fetchall()
        conn.close()

        updated = 0
        now = datetime.now()
        for mid, last_acc, created_str in rows:
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

                conn2 = sqlite3.connect(DB_PATH)
                conn2.execute(
                    "UPDATE memories SET worth_success=?, worth_failure=? WHERE id=?",
                    (new_success, new_failure, mid)
                )
                conn2.commit()
                conn2.close()
                updated += 1
            except Exception:
                pass

        if updated:
            print(f"  [WORTH] revived {updated} stale worth records "
                  f"(remaining: {count - updated})")

    except Exception as e:
        print(f"  [SAFETY_NET] scan_stale_worth error: {e}")


# ── Tier 自动升级 ───────────────────────────────────────────
async def scan_tier_migration():
    """基于访问活动自动升级记忆 tier。

    L1 → L2: last_accessed 7天内
    L2 → L3: access_count > 5 且 last_accessed 3天内
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        now = datetime.now()
        seven_days_ago = (now - timedelta(days=7)).isoformat()
        three_days_ago = (now - timedelta(days=3)).isoformat()

        # L1 → L2: 最近被访问过
        upgraded_l1 = conn.execute(
            "UPDATE memories SET tier='L2' "
            "WHERE tier='L1' AND last_accessed > ?",
            (seven_days_ago,)
        ).rowcount

        # L2 → L3: 高活跃度
        upgraded_l2 = conn.execute(
            "UPDATE memories SET tier='L3' "
            "WHERE tier='L2' AND access_count > 5 AND last_accessed > ?",
            (three_days_ago,)
        ).rowcount

        conn.commit()
        conn.close()

        if upgraded_l1 or upgraded_l2:
            print(f"  [TIER] L1→L2: {upgraded_l1}, L2→L3: {upgraded_l2}")

    except Exception as e:
        print(f"  [SAFETY_NET] scan_tier_migration error: {e}")


# ── 分类队列健康 ───────────────────────────────────────────
async def scan_category_stuck():
    """监控 LLM 分类队列健康：检测长期卡住的 llm_pending 和
    stale 'other' 分类记忆，触发 memory_reclassify。
    """
    try:
        conn = sqlite3.connect(DB_PATH)

        # 1. 检测 llm_pending 卡住情况 (>1h 未处理)
        stuck_pending = conn.execute(
            "SELECT COUNT(1) FROM memories "
            "WHERE tags LIKE '%llm_pending:true%' "
            "AND tags NOT LIKE '%llm_classified:true%'"
        ).fetchone()[0]

        if stuck_pending > 5:
            print(f"  [CAT_STUCK] {stuck_pending} memories stuck in LLM queue "
                  f"— Ollama may be offline or overloaded")

        # 2. 检测大量 other 分类 (可能是分类管道卡住)
        other_count = conn.execute(
            "SELECT COUNT(1) FROM memories WHERE category='other'"
        ).fetchone()[0]

        # 3. 对长期 other (+ 从未被访问) 触发 reclassify
        if other_count > 20:
            rows = conn.execute(
                "SELECT id FROM memories WHERE category='other' "
                "AND (last_accessed IS NULL OR last_accessed = '') "
                "LIMIT 5"
            ).fetchall()
            conn.close()

            if rows:
                from plastic_promise.core.context_engine import ContextEngine
                from plastic_promise.mcp.tools.memory import handle_memory_reclassify
                engine = ContextEngine()
                reclassified = 0
                for (mid,) in rows:
                    try:
                        await handle_memory_reclassify(engine, {"memory_id": mid})
                        reclassified += 1
                    except Exception:
                        pass
                if reclassified:
                    print(f"  [CAT_STUCK] reclassified {reclassified} stale 'other' memories")
        else:
            conn.close()

    except Exception as e:
        print(f"  [SAFETY_NET] scan_category_stuck error: {e}")


# ── LLM 后台分类 ───────────────────────────────────────────
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
        conn = sqlite3.connect(DB_PATH)
        # Find memories tagged for LLM classification (exclude already-classified)
        rows = conn.execute(
            "SELECT id, content, tags, category FROM memories "
            "WHERE tags LIKE '%llm_pending:true%' "
            "AND tags NOT LIKE '%llm_classified:true%' "
            "ORDER BY created_at ASC LIMIT ?",
            (LLM_BATCH_SIZE,)
        ).fetchall()
        conn.close()

        if not rows:
            return

        from plastic_promise.smart_extractor import _llm_classify
        import requests

        classified = 0
        for mid, content, tags_raw, old_category in rows:
            try:
                tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])

                # Call Ollama LLM for classification
                new_cat = _llm_classify(
                    content, OLLAMA_HOST, OLLAMA_MODEL, timeout=15
                )

                if new_cat and new_cat != old_category:
                    # Update SQLite in-place
                    conn2 = sqlite3.connect(DB_PATH)
                    conn2.execute(
                        "UPDATE memories SET category = ? WHERE id = ?",
                        (new_cat, mid)
                    )
                    conn2.commit()
                    conn2.close()
                    print(f"  [LLM_CLASSIFY] {mid[:12]}... {old_category} → {new_cat} "
                          f"({(content or '')[:40]}...)")

                # Replace llm_pending with llm_classified (even if LLM returned same/None)
                tags = [t for t in tags if t != "llm_pending:true"]
                if "llm_classified:true" not in tags:
                    tags.append("llm_classified:true")
                if new_cat and f"cat:{new_cat}" not in tags:
                    tags.append(f"cat:{new_cat}")

                conn2 = sqlite3.connect(DB_PATH)
                conn2.execute(
                    "UPDATE memories SET tags = ? WHERE id = ?",
                    (json.dumps(tags), mid)
                )
                conn2.commit()
                conn2.close()

                # Notify MCP to refresh in-memory cache
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(f"{MCP_URL}/notify", json={
                            "type": "llm_classified",
                            "memory_id": mid,
                            "new_category": new_cat,
                            "ts": datetime.now().isoformat(),
                        }, timeout=3)
                except Exception:
                    pass  # notify is best-effort

                classified += 1

            except Exception as e:
                print(f"  [LLM_CLASSIFY] error for {mid[:12]}...: {e}")

        if classified:
            print(f"  [LLM_CLASSIFY] batch done: {classified} classified")

    except Exception as e:
        print(f"  [SAFETY_NET] scan_llm_classify error: {e}")


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
    # LLM 分类间隔 (秒)，默认 120s 一次
    LLM_CLASSIFY_INTERVAL = int(os.environ.get("LLM_CLASSIFY_INTERVAL", "120"))
    llm_classify_threshold = max(1, LLM_CLASSIFY_INTERVAL // 10)

    print(f"Maintenance Daemon (audit={INTERVAL}s, safety_net={SAFETY_NET_INTERVAL}s, "
          f"llm_classify={LLM_CLASSIFY_INTERVAL}s, PID={os.getpid()})")
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
            # 安全网扫描 — 顺序执行，互不阻塞
            # 优先级：duplicate > worth > tier > category > orphan > issue
            try:
                await scan_duplicate_clusters()
            except Exception:
                pass
            try:
                await scan_stale_worth()
            except Exception:
                pass
            try:
                await scan_tier_migration()
            except Exception:
                pass
            try:
                await scan_category_stuck()
            except Exception:
                pass
            try:
                await scan_orphan_steps()
            except Exception:
                pass
            try:
                await scan_unclosed_issues()
            except Exception:
                pass
            try:
                await scan_llm_classify()
            except Exception:
                pass
        else:
            recover_stuck_tasks()

        await asyncio.sleep(10)  # 10s 粒度

if __name__ == "__main__":
    asyncio.run(main())
