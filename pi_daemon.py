"""Pi Daemon — 零 LLM 标签轮询 Worker

直接查 SQLite tags，不调 LLM。只在检测到 task:pending 时才 spawn Pi。
Token 节省: ~8640 次/天 → <10 次/天。
"""

import asyncio
import subprocess
import sys
import os
import httpx
import shutil
import time

ROLE = os.environ.get("PI_ROLE", sys.argv[1] if len(sys.argv) > 1 else "pi_builder")
DOMAIN = os.environ.get("PI_DOMAIN", sys.argv[2] if len(sys.argv) > 2 else "building")
INTERVAL = int(os.environ.get("PI_INTERVAL", "10"))
PI_CMD = shutil.which("pi") or shutil.which("pi.cmd") or r"D:\npm-global\pi.cmd"

# Daemon shares plastic_memory.db with SSE server — reads tags via sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def get_pending_task(role: str) -> tuple[str, str] | None:
    """零 LLM — 直读 SQLite 返回 (task_content, task_id) 或 None。"""
    import sqlite3, json
    conn = sqlite3.connect(
        os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    )
    rows = conn.execute(
        "SELECT id, content, tags FROM memories "
        "WHERE tags LIKE '%task:pending%' OR tags LIKE '%task:rejected%'"
    ).fetchall()
    conn.close()
    for (mid, content, tags_raw) in rows:
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
        except Exception:
            continue
        wanted = ("task:pending", "task:rejected")
        if any(t in tags for t in wanted) and f"assignee:{role}" in tags:
            return content, mid
    return None


def mark_active(role: str, task_tags: list[str]) -> list[str]:
    """将 task:pending 替换为 task:active，加 owner:<role>。"""
    new_tags = ["task:active", f"owner:{role}"]
    for t in task_tags:
        if t not in ("task:pending", "task:active", "task:done", "task:reviewed"):
            if not t.startswith("owner:"):
                new_tags.append(t)
    return new_tags


async def execute_task(task_content: str, task_id: str):
    proc = await asyncio.create_subprocess_exec(
        PI_CMD, "--print",
        f"You are {ROLE}, domain {DOMAIN}. "
        f"Task: {task_content}. "
        f"Execute it using write/edit/bash. "
        f"When done, call memory_store(content='{ROLE} DONE: <summary>', memory_type='experience', "
        f"domain='{DOMAIN}', tags=['task:done','owner:{ROLE}','domain:{DOMAIN}']).",
        "--session-id", f"{ROLE}_daemon",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = (stdout + stderr).decode("utf-8", errors="replace")[-500:]
    return output.strip()


def mark_task_accepted(task_id: str):
    """将 task:pending → task:accepted，防重复执行。最终验收由 Claude 完成。"""
    import sqlite3, json
    conn = sqlite3.connect(
        os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    )
    row = conn.execute("SELECT tags FROM memories WHERE id = ?", (task_id,)).fetchone()
    if row:
        tags = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
        new_tags = ["task:accepted" if t == "task:pending" else t for t in tags]
        conn.execute("UPDATE memories SET tags = ? WHERE id = ?",
                     (json.dumps(new_tags), task_id))
        conn.commit()
    conn.close()


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
    print(f"Pi Daemon: {ROLE} (domain={DOMAIN}, poll={INTERVAL}s)")
    print(f"Mode: zero-LLM tag check (task:pending + assignee:{ROLE})")

    while True:
        task = get_pending_task(ROLE)
        if task:
            content, task_id = task
            print(f"[{_now()}] TASK {task_id[:20]}... → executing")
            result = await execute_task(content, task_id)
            print(result or "DONE")
            mark_task_accepted(task_id)
            await notify_state_change({
                "type": "tag_transition",
                "from_tag": "task:pending",
                "to_tag": "task:accepted",
                "agent": ROLE,
                "domain": DOMAIN,
                "task_id": task_id,
                "tags": ["task:accepted", f"owner:{ROLE}", f"domain:{DOMAIN}"],
            })
        else:
            print(f"[{_now()}] idle.", end="\r")
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
