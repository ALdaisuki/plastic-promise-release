"""Pi Daemon — 自治流水线: 多角色 + 自动衔接 + 信任执行

单进程管理所有 Agent 角色。标签驱动流水线阶段自动衔接。
原则 #7 器官互保: 上游完成→下游自动触发。
原则 #9 信任驱动: 信任分不足 → 跳过执行。
"""

import asyncio
import subprocess
import sys
import os
import httpx
import shutil
import time

INTERVAL = int(os.environ.get("PI_INTERVAL", "10"))
PI_CMD = shutil.which("pi") or shutil.which("pi.cmd") or r"D:\npm-global\pi.cmd"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 角色注册表 — 定义流水线阶段衔接
AGENT_ROLES = {
    "pi_builder":  {"domain": "building",   "trigger": ["task:pending"], "output": "task:active"},
    "pi_fixer":    {"domain": "fixing",     "trigger": ["task:rejected"], "output": "task:fixed"},
    "pi_reviewer": {"domain": "reflecting", "trigger": ["task:active"],   "output": "task:review"},
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
        "WHERE tags LIKE '%task:pending%' OR tags LIKE '%task:rejected%' OR tags LIKE '%task:active%'"
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


def can_execute(role: str) -> bool:
    """信任分检查 — trust < 0.3 则拒绝执行 (只读)。"""
    from plastic_promise.core.issue_validator import get_tier, check_permission
    from plastic_promise.defense.soul_enforcer import TrustManager
    tm = TrustManager()
    trust = tm.get(role)
    tier = get_tier(trust)
    ok = check_permission(tier, "write_file") != "denied"
    if not ok:
        print(f"  [BLOCKED] {role} trust={trust:.2f} tier={tier} — skip")
    return ok


def mark_task_accepted(task_id: str):
    """将 trigger tag → task:accepted，防重复执行。"""
    import sqlite3, json
    conn = sqlite3.connect(
        os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    )
    row = conn.execute("SELECT tags FROM memories WHERE id = ?", (task_id,)).fetchone()
    if row:
        tags = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
        new_tags = []
        for t in tags:
            if t in ("task:pending", "task:rejected", "task:active"):
                new_tags.append("task:accepted")
            else:
                new_tags.append(t)
        conn.execute("UPDATE memories SET tags = ? WHERE id = ?",
                     (json.dumps(new_tags), task_id))
        conn.commit()
    conn.close()


async def execute_task(role: str, cfg: dict, task_content: str, task_id: str):
    domain = cfg["domain"]
    output_tag = cfg["output"]
    proc = await asyncio.create_subprocess_exec(
        PI_CMD, "--print",
        f"You are {role}, domain {domain}. "
        f"Task: {task_content}. "
        f"Execute it using write/edit/bash. "
        f"When done, call memory_store(content='{role} DONE: <summary>', memory_type='experience', "
        f"domain='{domain}', tags=['{output_tag}','owner:{role}','domain:{domain}']).",
        "--session-id", f"{role}_daemon",
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
    print(f"Pi Daemon: Autonomous Pipeline (poll={INTERVAL}s)")
    print(f"Roles: {', '.join(AGENT_ROLES.keys())}")
    print(f"Auto-chain: {', '.join(f'{k}→{v}' for k,v in AUTO_CHAIN.items())}")

    while True:
        task = get_pending_task()
        if task:
            role, cfg, content, task_id = task
            if not can_execute(role):
                await asyncio.sleep(INTERVAL)
                continue
            print(f"[{_now()}] {role}({cfg['domain']}) ← {task_id[:20]}...")
            result = await execute_task(role, cfg, content, task_id)
            print(result.strip()[-200:] or "DONE")
            mark_task_accepted(task_id)
            await notify_state_change({
                "type": "tag_transition",
                "from_tag": "task:pending",
                "to_tag": cfg["output"],
                "agent": role,
                "domain": cfg["domain"],
                "task_id": task_id,
                "tags": [cfg["output"], f"owner:{role}", f"domain:{cfg['domain']}"],
            })
        else:
            print(f"[{_now()}] idle.", end="\r")
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
