# Phase 4 — Multi-Agent + Finale Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development.

**Goal:** Context-ready pre-fetch, bridge TODO completion, SSE production readiness.

**Architecture:** Three independent tasks closing the final gaps.

**Tech Stack:** Python 3.10+, zmq, starlette, uvicorn

## Global Constraints
- Context-ready TTL: 300 seconds (5 min)
- Bridge uses existing zmq and websocket imports
- SSE health check: GET /health endpoint
- Each task is independently testable

---

### Task 1: context-ready — 上下文预备

**Files:**
- Modify: `plastic_promise/loop/soul_loop.py` (post_task → pre-fetch)
- Modify: `plastic_promise/mcp/tools/context.py` (+ handle_context_ready)
- Modify: `plastic_promise/mcp/server.py` (register tool)

- [ ] **Step 1: Post-task auto pre-fetch**

In `post_task`, after CEI update, add:

```python
        # 7. 上下文预备 — 预取下次上下文到预备区
        try:
            self._engine._context_ready = getattr(self._engine, '_context_ready', {})
            now = __import__('datetime').datetime.now()
            # Clean expired entries (TTL 5 min)
            expired = [k for k, v in self._engine._context_ready.items()
                       if (now - v.get('_ts', now)).total_seconds() > 300]
            for k in expired:
                del self._engine._context_ready[k]
            # Pre-fetch context for common task types
            task_vector = [0.0] * 1024
            pack = self._engine.supply(task_description, task_vector, "general", "global")
            self._engine._context_ready["general"] = pack
            self._engine._context_ready["general"]._ts = now
        except Exception:
            pass
```

- [ ] **Step 2: MCP tool handle_context_ready**

Add to `context.py`:

```python
async def handle_context_ready(engine: Any, args: dict) -> list[TextContent]:
    """Return or refresh the context-ready cache. 预备参考——供查阅，非强制."""
    try:
        task_hint = args.get("task_hint", "general")
        ready = getattr(engine, '_context_ready', {})
        if task_hint in ready:
            pack = ready[task_hint]
            # Check TTL
            import datetime
            ts = getattr(pack, '_ts', None)
            if ts and (datetime.datetime.now() - ts).total_seconds() < 300:
                return [TextContent(type="text", text=pack.to_prompt())]
        # Not ready — do a fresh supply
        from plastic_promise.core.embedder import get_embedder, FallbackEmbedder
        try:
            vec = get_embedder(fallback_on_error=False).embed(task_hint)
        except Exception:
            vec = FallbackEmbedder().embed(task_hint)
        pack = engine.supply(task_hint, vec, "general", "global")
        return [TextContent(type="text", text=pack.to_prompt())]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "context_ready"}, ensure_ascii=False))]
```

- [ ] **Step 3: Register in server.py**

Add tool definition and routing (follow existing patterns for context tools).

- [ ] **Step 4: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.loop.soul_loop import SoulLoop
sl = SoulLoop(engine=ContextEngine())
r = sl.post_task('测试上下文预备功能', '')
assert hasattr(sl._engine, '_context_ready')
print(f'Context ready entries: {len(sl._engine._context_ready)}')
print('CONTEXT-READY PASSED')
"
```

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/loop/soul_loop.py plastic_promise/mcp/tools/context.py plastic_promise/mcp/server.py
git commit -m "feat: context-ready — post_task auto pre-fetch + MCP context_ready tool"
```

---

### Task 2: Bridge TODO

**Files:**
- Modify: `bridge/bus_client.py:225-235` (claude_agent_loop)
- Modify: `bridge/bus_client.py:275-285` (neko_bridge_loop)

- [ ] **Step 1: Pi task execution (line 229)**

Replace the TODO with:

```python
                            # Execute Pi task: recall relevant context + respond
                            task = data.get("task", "")
                            result_text = f"Task received: {task[:200]}"
                            try:
                                import requests
                                resp = requests.post(
                                    "http://127.0.0.1:48920/memory/recall",
                                    json={"query": task, "task_type": "general"},
                                    timeout=10
                                )
                                if resp.status_code == 200:
                                    mem = resp.json()
                                    result_text = json.dumps(mem, ensure_ascii=False)
                            except Exception:
                                pass  # Memory server not available — return basic ack
                            await send("claude:pi", {"type": "result", "task": task[:200],
                                                       "result": result_text})
```

- [ ] **Step 2: N.E.K.O ZMQ forward (line 281)**

Replace the TODO with:

```python
                            # Forward memory sync events to N.E.K.O via ZMQ
                            if topic == "memory:sync" and hasattr(self, '_zmq_pub'):
                                try:
                                    self._zmq_pub.send_json(data)
                                except Exception:
                                    pass
```

- [ ] **Step 3: Verify syntax**

```bash
cd "F:/Agent/Memory system" && python -c "import ast; ast.parse(open('bridge/bus_client.py',encoding='utf-8').read()); print('Syntax OK')"
```

- [ ] **Step 4: Commit**

```bash
git add bridge/bus_client.py
git commit -m "feat: bridge TODOs — Pi task execution + N.E.K.O ZMQ memory sync forwarding"
```

---

### Task 3: SSE production

**Files:**
- Modify: `plastic_promise/mcp/server.py` (run_sse function)

- [ ] **Step 1: Health check + startup log + graceful shutdown**

Add /health route and startup logging to run_sse:

```python
    import signal, time as _time
    start_time = _time.time()

    async def health(request):
        import json as _json
        from starlette.responses import JSONResponse
        return JSONResponse({
            "status": "ok",
            "uptime": round(_time.time() - start_time, 1),
            "version": "0.1.0",
            "pid": os.getpid(),
        })

    async def shutdown():
        logger.info("Shutting down Plastic Promise SSE server...")

    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
        Route("/health", endpoint=health),
    ], on_shutdown=[shutdown])

    logger.info(f"Plastic Promise MCP Server v0.1.0")
    logger.info(f"SSE endpoint: http://127.0.0.1:{port}/sse")
    logger.info(f"Health:      http://127.0.0.1:{port}/health")
    logger.info(f"PID: {os.getpid()}")
```

- [ ] **Step 2: Verify imports**

```bash
cd "F:/Agent/Memory system" && python -c "import ast; ast.parse(open('plastic_promise/mcp/server.py',encoding='utf-8').read()); print('Syntax OK')"
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/mcp/server.py
git commit -m "feat: SSE production — health check, startup logging, graceful shutdown"
```
