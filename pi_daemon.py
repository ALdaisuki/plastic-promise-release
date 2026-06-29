"""Pi Daemon — 零 LLM 标签轮询 Worker

直接查 SQLite tags，不调 LLM。只在检测到 task:pending 时才 spawn Pi。
Token 节省: ~8640 次/天 → <10 次/天。
"""

import asyncio
import subprocess
import sys
import os
import shutil
import time

ROLE = os.environ.get("PI_ROLE", sys.argv[1] if len(sys.argv) > 1 else "pi_builder")
DOMAIN = os.environ.get("PI_DOMAIN", sys.argv[2] if len(sys.argv) > 2 else "building")
INTERVAL = int(os.environ.get("PI_INTERVAL", "10"))
PI_CMD = shutil.which("pi") or shutil.which("pi.cmd") or r"D:\npm-global\pi.cmd"

# 引擎共享——与 SSE 服务器使用相同的 plastic_memory.db
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plastic_promise.core.context_engine import ContextEngine

_engine = None

def get_engine():
    global _engine
    if _engine is None:
        _engine = ContextEngine()
    return _engine


def has_pending_task(role: str, domain: str) -> bool:
    """零 LLM 检查——直读 SQLite memories 表查 task:pending 标签。"""
    import sqlite3, json
    conn = sqlite3.connect(
        os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    )
    rows = conn.execute("SELECT tags FROM memories").fetchall()
    conn.close()
    for (tags_raw,) in rows:
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
        except Exception:
            continue
        if "task:pending" in tags and f"assignee:{role}" in tags:
            return True
        if "task:pending" in tags and "assignee:any" in tags:
            return True
    return False


def mark_active(role: str, task_tags: list[str]) -> list[str]:
    """将 task:pending 替换为 task:active，加 owner:<role>。"""
    new_tags = ["task:active", f"owner:{role}"]
    for t in task_tags:
        if t not in ("task:pending", "task:active", "task:done", "task:reviewed"):
            if not t.startswith("owner:"):
                new_tags.append(t)
    return new_tags


async def execute_task():
    proc = await asyncio.create_subprocess_exec(
        PI_CMD, "--print",
        f"You are {ROLE}, domain {DOMAIN}. "
        f"1. Call memory_recall(domain_hint='{DOMAIN}', query='task:active AND owner:{ROLE}') "
        f"   to find your claimed task. If none found, check task:pending. "
        f"2. Execute using write/edit/bash. "
        f"3. Call memory_store(content='{ROLE} DONE: <summary>', memory_type='experience', "
        f"   domain='{DOMAIN}', tags=['task:done','owner:{ROLE}','domain:{DOMAIN}']) "
        f"   AND also mark the original task memory with task:done. "
        f"4. If no tasks found, just reply IDLE.",
        "--session-id", f"{ROLE}_daemon",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = (stdout + stderr).decode("utf-8", errors="replace")[-500:]
    return output.strip()


def _now():
    return time.strftime("%H:%M:%S")


async def main():
    print(f"Pi Daemon: {ROLE} (domain={DOMAIN}, poll={INTERVAL}s)")
    print(f"Mode: zero-LLM tag check (task:pending + assignee:{ROLE})")

    while True:
        if has_pending_task(ROLE, DOMAIN):
            print(f"[{_now()}] TASK FOUND → waking {ROLE}")
            result = await execute_task()
            print(result or "DONE")
            # Reload engine to pick up new memories
            global _engine
            _engine = None
        else:
            print(f"[{_now()}] idle.", end="\r")
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
