"""Pi Daemon — 自治流水线: 多角色 + 自动衔接 + 信任执行

单进程管理所有 Agent 角色。标签驱动流水线阶段自动衔接。
原则 #7 器官互保: 上游完成→下游自动触发。
原则 #9 信任驱动: 信任分不足 → 跳过执行。
"""

import asyncio
import json
import subprocess
import sys
import os
import httpx
import shutil
import time

INTERVAL = int(os.environ.get("PI_INTERVAL", "10"))
AUDIT_INTERVAL = int(os.environ.get("AUDIT_INTERVAL_SECONDS", "300"))
_PI_BIN = shutil.which("pi") or shutil.which("pi.cmd")
PI_CMD = _PI_BIN if _PI_BIN else None  # None = no Pi CLI, daemon runs audit-only mode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Also add project root so plastic_promise is importable
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

from plastic_promise.defense.soul_enforcer import TrustManager
_TRUST_MGR = TrustManager()  # 进程级单例，跨循环保持信任分

# 角色注册表 — 定义流水线阶段衔接
AGENT_ROLES = {
    "pi_builder":  {"domain": "building",   "trigger": ["task:pending"], "output": "task:active"},
    "pi_fixer":    {"domain": "fixing",     "trigger": ["task:rejected"], "output": "task:fixed"},
    "pi_reviewer": {"domain": "reflecting", "trigger": ["task:active", "task:done"], "output": "task:review"},
}

# 自动衔接: 当某个 output tag 出现时，哪个角色应该被唤醒
AUTO_CHAIN = {
    "task:active":  "pi_reviewer",  # Builder 完成 → Reviewer 自动审查
    "task:rejected": "pi_fixer",    # Claude 打回 → Fixer 自动修复
}


def get_pending_task():
    """多角色扫描 — 返回 (role, cfg, content, task_id) 或 None。"""
    import sqlite3, json
    conn = sqlite3.connect(
        os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    )
    rows = conn.execute(
        "SELECT id, content, tags FROM memories "
        "WHERE tags LIKE '%task:%'"
    ).fetchall()
    conn.close()

    for (mid, content, tags_raw) in rows:
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
        except Exception:
            continue

        for role, cfg in AGENT_ROLES.items():
            assignee_tag = f"assignee:{role}"
            # Check: task has a trigger tag AND is assigned to this role
            has_trigger = any(f"task:{t}" in tags if not t.startswith("task:") else t in tags
                            for t in cfg["trigger"])
            # Also match auto-chain: output tag without explicit assignee
            is_auto_chained = False
            for trigger_tag in cfg["trigger"]:
                if trigger_tag in tags and (assignee_tag in tags or f"owner:pi_builder" in tags):
                    is_auto_chained = True
                    break

            if is_auto_chained:
                # Add assignee if auto-chained
                if assignee_tag not in tags:
                    new_tags = list(tags) + [assignee_tag]
                    conn2 = sqlite3.connect(os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db"))
                    conn2.execute("UPDATE memories SET tags = ? WHERE id = ?",
                                  (json.dumps(new_tags), mid))
                    conn2.commit()
                    conn2.close()
                return role, cfg, content, mid

    return None


def get_trust_tier(role: str, tm=None) -> dict:
    """返回 Agent 的信任分 + 自由度等级 + 权限。"""
    from plastic_promise.core.issue_validator import get_tier, check_permission, get_tier_info
    from plastic_promise.defense.soul_enforcer import TrustManager
    if tm is None:
        tm = TrustManager()
    trust = tm.get(role)
    tier_info = get_tier_info(trust)
    tier = tier_info["tier"]
    return {
        "trust": trust,
        "tier": tier,
        "motto": tier_info["motto"],
        "can_write": check_permission(tier, "write_file") != "denied",
        "can_bash": check_permission(tier, "run_bash") != "denied",
        "needs_review": check_permission(tier, "write_file") == "needs_review",
    }


def mark_task_active(task_id: str, role: str):
    """task:pending → task:active + 时间戳。Pi 崩溃则超时恢复。"""
    import sqlite3, json
    ts = f"ts:{time.strftime('%Y%m%dT%H%M%S')}"
    conn = sqlite3.connect(os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db"))
    row = conn.execute("SELECT tags FROM memories WHERE id = ?", (task_id,)).fetchone()
    if row:
        tags = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
        new_tags = []
        for t in tags:
            if t.startswith("ts:"):
                continue  # 移除旧时间戳
            if t in ("task:pending", "task:rejected"):
                new_tags.append("task:active")
            else:
                new_tags.append(t)
        new_tags.append(ts)
        conn.execute("UPDATE memories SET tags = ? WHERE id = ?",
                     (json.dumps(new_tags), task_id))
        conn.commit()
    conn.close()


def mark_task_accepted(task_id: str):
    """中间态 tag → task:accepted（最终完成）。"""
    import sqlite3, json
    conn = sqlite3.connect(os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db"))
    row = conn.execute("SELECT tags FROM memories WHERE id = ?", (task_id,)).fetchone()
    if row:
        tags = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
        new_tags = []
        for t in tags:
            if t.startswith("ts:"):
                continue
            if t in ("task:pending", "task:rejected", "task:active", "task:done", "task:review"):
                new_tags.append("task:accepted")
            else:
                new_tags.append(t)
        conn.execute("UPDATE memories SET tags = ? WHERE id = ?",
                     (json.dumps(new_tags), task_id))
        conn.commit()
    conn.close()


def recover_stuck_tasks():
    """超时恢复: task:active>5min 或 task:reviewed>10min → 重置为 task:pending。"""
    import sqlite3, json
    from datetime import datetime, timedelta
    conn = sqlite3.connect(os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db"))
    rows = conn.execute(
        "SELECT id, tags FROM memories WHERE tags LIKE '%task:active%' OR tags LIKE '%task:reviewed%'"
    ).fetchall()

    now = datetime.now()
    for (mid, tags_raw) in rows:
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
        except Exception:
            continue

        # 提取时间戳
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

        if "task:active" in tags and elapsed > 300:  # 5 min
            new_tags = ["task:pending" if t in ("task:active",) else t for t in tags if not t.startswith("ts:")]
            conn.execute("UPDATE memories SET tags = ? WHERE id = ?", (json.dumps(new_tags), mid))
            print(f"  [RECOVER] task:active timed out ({elapsed:.0f}s) → reset to pending")
        elif "task:reviewed" in tags and elapsed > 600:  # 10 min
            new_tags = ["task:active" if t in ("task:reviewed",) else t for t in tags if not t.startswith("ts:")]
            conn.execute("UPDATE memories SET tags = ? WHERE id = ?", (json.dumps(new_tags), mid))
            print(f"  [RECOVER] task:reviewed timed out ({elapsed:.0f}s) → reset to active")
    conn.commit()
    conn.close()


def cleanup_old_memories():
    """清理 7 天前的 task:accepted / task:reviewed 已验收记忆。"""
    import sqlite3, json
    from datetime import datetime, timedelta
    conn = sqlite3.connect(os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db"))
    rows = conn.execute(
        "SELECT id, tags FROM memories WHERE tags LIKE '%task:accepted%' OR tags LIKE '%task:reviewed%'"
    ).fetchall()

    cutoff = datetime.now() - timedelta(days=7)
    removed = 0
    for (mid, tags_raw) in rows:
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
            conn.execute("UPDATE memories SET tags = ? WHERE id = ?", (json.dumps(new_tags), mid))
            removed += 1

    if removed:
        print(f"  [CLEANUP] {removed} old task memories cleaned (>7 days)")
    conn.commit()
    conn.close()


async def inject_context(task_content: str, domain: str, role: str = "", mcp_client=None) -> dict | None:
    """Call auto_context_inject via MCP before task execution.

    Args:
        task_content: Task description
        domain: Agent domain (building/fixing/reflecting)
        role: Agent role (pi_builder/pi_fixer/pi_reviewer) — included in source
        mcp_client: Optional MCP SSE client
    """
    try:
        from plastic_promise.core.constants import DOMAIN_TO_TASK_TYPE
        task_type = DOMAIN_TO_TASK_TYPE.get(domain, "general")
        source = f"pi_agent:{role}" if role else "pi_agent"

        if mcp_client:
            result = await mcp_client.call_tool("auto_context_inject", {
                "task_description": task_content,
                "task_type": task_type,
                "source": source,
            })
            return json.loads(result[0].text) if result else None
        else:
            from plastic_promise.mcp.tools.context import handle_auto_context_inject
            from plastic_promise.core.context_engine import ContextEngine
            engine = ContextEngine()
            result = await handle_auto_context_inject(engine, {
                "task_description": task_content,
                "task_type": task_type,
                "source": source,
            })
            return json.loads(result[0].text)
    except Exception:
        return None  # Graceful degradation


async def _run_and_finish(role: str, cfg: dict, content: str, task_id: str, restriction: str = None):
    """Fire-and-forget: 执行任务 + 标记完成 + 推送通知。"""
    # Inject context before execution (non-blocking, graceful degradation)
    await inject_context(content, cfg["domain"], role=role)
    result = await execute_task(role, cfg, content, task_id, restriction)
    print(f"  [{_now()}] {role} DONE: {result.strip()[-150:] or 'ok'}")
    mark_task_accepted(task_id)
    await notify_state_change({
        "type": "tag_transition",
        "from_tag": "task:active",
        "to_tag": cfg["output"],
        "agent": role,
        "domain": cfg["domain"],
        "task_id": task_id,
        "tags": [cfg["output"], f"owner:{role}", f"domain:{cfg['domain']}"],
    })


async def execute_task(role: str, cfg: dict, task_content: str, task_id: str, restriction: str = None):
    if PI_CMD is None:
        return "[DAEMON] Pi CLI not installed — task execution skipped (audit-only mode)"

    domain = cfg["domain"]
    output_tag = cfg["output"]

    restriction_prompt = ""
    if restriction:
        restriction_prompt = (
            f"TRUST RESTRICTION: {restriction}. "
            f"You are in restricted mode — use read + memory_recall only. "
            f"Do NOT write, edit, or run bash. "
            f"If the task requires write/bash, respond 'NEEDS_APPROVAL' instead. "
        )

    session_id = f"{role}_{int(time.time())}"
    proc = await asyncio.create_subprocess_exec(
        PI_CMD, "--print",
        f"You are {role}, domain {domain}. {restriction_prompt}"
        f"FRESH SESSION — ignore all previous context. "
        f"Task: {task_content}. "
        f"Execute it using write/edit/bash. "
        f"When done, you MUST call memory_store with EXACTLY these tags: "
        f"['{output_tag}','owner:{role}','domain:{domain}']. "
        f"CRITICAL: use '{output_tag}' — do NOT use 'task:done' or any other task tag. "
        f"The pipeline auto-chaining depends on this exact tag.",
        "--session-id", session_id,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (stdout + stderr).decode("utf-8", errors="replace")[-500:]


async def notify_state_change(event: dict):
    """推送标签状态变更到 SSE /notify → /events 广播。"""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                "http://127.0.0.1:9020/notify",
                json=event,
                timeout=5
            )
    except Exception:
        pass


def _now():
    return time.strftime("%H:%M:%S")


async def main():
    # Write PID file for watchdog
    _pid_path = os.path.join(_project_root, "pi_daemon.pid")
    with open(_pid_path, "w") as f:
        f.write(str(os.getpid()))

    _mode = "audit-only" if PI_CMD is None else "full pipeline"
    print(f"Pi Daemon: {_mode.upper()} (poll={INTERVAL}s, audit={AUDIT_INTERVAL}s, PID={os.getpid()})")
    if PI_CMD is None:
        print(f"  ⚠ Pi CLI not found — task dispatch disabled, running audit/recovery/cleanup only")
        print(f"  Install Pi CLI to enable: pip install pi-agent  (or set PI_CMD env var)")
    else:
        print(f"Roles: {', '.join(AGENT_ROLES.keys())}")
        print(f"Auto-chain: {', '.join(f'{k}→{v}' for k,v in AUTO_CHAIN.items())}")

    _cleanup_counter = 0
    # 冷启动: 30s 后执行首次审计（daemon 启动即获得基线）
    await asyncio.sleep(30)
    from audit_daemon import run_audit
    await run_audit()
    while True:
        # Batch 1: task dispatch (only when Pi CLI is available)
        if PI_CMD is not None:
            task = get_pending_task()
            if task:
                role, cfg, content, task_id = task
                tier = get_trust_tier(role, tm=_TRUST_MGR)

                if tier["tier"] == "readonly":
                    print(f"  [BLOCKED] {role} trust={tier['trust']:.2f} readonly — cannot execute")
                    await asyncio.sleep(INTERVAL)
                    continue

                restriction = None
                if tier["needs_review"]:
                    restriction = "restricted: read+memory_recall only, no write/bash without Claude approval"
                elif not tier["can_write"]:
                    print(f"  [BLOCKED] {role} trust={tier['trust']:.2f} — no write permission")
                    await asyncio.sleep(INTERVAL)
                    continue

                print(f"[{_now()}] {role}({cfg['domain']}) trust={tier['trust']:.2f} [{tier['tier']}] ← {task_id[:20]}...")
                mark_task_active(task_id, role)
                # 并发执行 — 不阻塞其他角色的扫描
                asyncio.create_task(_run_and_finish(role, cfg, content, task_id, restriction))
                # 防止同一任务重复认领
                await asyncio.sleep(1)
                continue
            else:
                print(f"[{_now()}] idle.", end="\r")

        # Batch 2: 超时恢复 + 定期清理
        recover_stuck_tasks()
        _cleanup_counter += 1
        # 审计间隔: AUDIT_INTERVAL 秒，带 jitter 防惊群
        _audit_threshold = AUDIT_INTERVAL // INTERVAL
        if _cleanup_counter >= _audit_threshold:
            import random
            # 10% 概率跳过本次审计（jitter），防止所有实例同时审计
            if random.random() < 0.1:
                _cleanup_counter = _audit_threshold - 1  # 下次循环再触发
            else:
                cleanup_old_memories()
                from audit_daemon import run_audit
                await run_audit()
                _cleanup_counter = 0
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
