"""Pi Daemon — 轻量轮询 Worker (10s 间隔)

Pi 原生不支持持久 SSE 连接。用短间隔轮询保持简单可靠。
每次轮询只是一个 memory_recall 调用，返回极快。
"""

import asyncio
import subprocess
import sys
import os
import shutil

ROLE = os.environ.get("PI_ROLE", sys.argv[1] if len(sys.argv) > 1 else "pi_builder")
DOMAIN = os.environ.get("PI_DOMAIN", sys.argv[2] if len(sys.argv) > 2 else "building")
INTERVAL = int(os.environ.get("PI_INTERVAL", "10"))
PI_CMD = shutil.which("pi") or shutil.which("pi.cmd") or r"D:\npm-global\pi.cmd"


async def execute_task():
    proc = await asyncio.create_subprocess_exec(
        PI_CMD, "--print",
        f"You are {ROLE}, domain {DOMAIN}. "
        f"1. Call memory_recall(domain_hint='{DOMAIN}', query='TASK for {ROLE} pending') to find tasks. "
        f"2. Execute using write/edit/bash. "
        f"3. Call memory_store(content='{ROLE} DONE: <summary>', memory_type='experience', domain='{DOMAIN}', tags=['done','{ROLE}']). "
        f"If no pending tasks found, just reply IDLE.",
        "--session-id", f"{ROLE}_daemon",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = (stdout + stderr).decode("utf-8", errors="replace")[-500:]
    print(output.strip() or "IDLE")


def _now():
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


async def main():
    print(f"Pi Daemon: {ROLE} (domain={DOMAIN}, poll={INTERVAL}s)")
    while True:
        print(f"[{_now()}] checking...", end=" ", flush=True)
        await execute_task()
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
