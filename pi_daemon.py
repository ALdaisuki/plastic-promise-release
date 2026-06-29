"""Pi Daemon — SSE 事件驱动 Worker (零轮询)

监听 Plastic Promise /events SSE 端点。
检测到新 TASK 记忆 → 唤醒对应 Pi → 执行 → 回到监听。
"""

import asyncio
import subprocess
import sys
import os

ROLE = os.environ.get("PI_ROLE", sys.argv[1] if len(sys.argv) > 1 else "pi_builder")
DOMAIN = os.environ.get("PI_DOMAIN", sys.argv[2] if len(sys.argv) > 2 else "building")
SSE_URL = os.environ.get("PI_SSE_URL", "http://127.0.0.1:9020/events")


async def listen_and_work():
    import httpx

    print(f"Pi Daemon: {ROLE} (domain={DOMAIN})")
    print(f"Listening: {SSE_URL}")

    _busy = False
    _last_event = [asyncio.get_event_loop().time()]  # mutable to share across closures

    async with httpx.AsyncClient(timeout=httpx.Timeout(300)) as client:
        # SSE listener task
        async def sse_listen():
            while True:
                try:
                    async with client.stream("GET", SSE_URL) as response:
                        print(f"Connected. Waiting for tasks...")
                        async for line in response.aiter_lines():
                            _last_event[0] = asyncio.get_event_loop().time()
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if not data_str or data_str == '{"type":"heartbeat"}':
                                continue

                            import json
                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            etype = event.get("type", "")
                            if (etype == "memory_stored" and f"TASK for {ROLE}" in event.get("content_preview", "")) or \
                               (etype == "issue_transition" and event.get("owner") == ROLE):
                                if not _busy:
                                    _busy = True
                                    print(f"\n[{_now()}] SSE event → waking {ROLE}")
                                    await execute_task()
                                    _busy = False
                                    print(f"[{_now()}] Done. Listening...")
                except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                    print(f"SSE disconnected: {e}. Reconnecting in 5s...")
                    await asyncio.sleep(5)

        # Polling fallback: if no SSE event for 10s, poll directly
        async def poll_fallback():
            while True:
                await asyncio.sleep(10)
                if _busy:
                    continue
                elapsed = asyncio.get_event_loop().time() - _last_event[0]
                if elapsed < 10:
                    continue  # SSE is working, skip poll
                print(f"[{_now()}] SSE silent for {elapsed:.0f}s → polling fallback...")
                await execute_task()
                _last_event[0] = asyncio.get_event_loop().time()

        await asyncio.gather(sse_listen(), poll_fallback())


async def execute_task():
    proc = await asyncio.create_subprocess_exec(
        "pi", "--print",
        f"You are {ROLE}, domain {DOMAIN}. Claude assigned you a task. "
        f"1. Call memory_recall(domain_hint='{DOMAIN}', query='TASK for {ROLE} pending') to find it. "
        f"2. Execute using write/edit/bash. "
        f"3. Call memory_store(content='{ROLE} DONE: <summary>', memory_type='experience', domain='{DOMAIN}', tags=['done','{ROLE}']).",
        "--session-id", f"{ROLE}_daemon",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=os.path.dirname(os.path.abspath(__file__)) or "."
    )
    stdout, stderr = await proc.communicate()
    output = (stdout + stderr).decode("utf-8", errors="replace")[-500:]
    print(output)


def _now():
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    asyncio.run(listen_and_work())
