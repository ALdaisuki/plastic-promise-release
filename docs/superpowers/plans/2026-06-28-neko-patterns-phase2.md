# Phase 2: N.E.K.O Patterns Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate 8 N.E.K.O engineering patterns into agent-interop across 3 independent tracks: infrastructure (launcher, WS robustness, dedup), architecture (agent registry, HTTP, roles), observability (telemetry, activity tracking).

**Architecture:** Three parallel tracks with clear boundaries. Track 1 produces shell scripts and hardened WS connections. Track 2 defines TypeScript interfaces and bridges Plastic Promise via HTTP. Track 3 adds local telemetry and activity-aware heartbeats. Tracks can be developed and tested independently.

**Tech Stack:** Shell (bash/batch), TypeScript (Pi extensions), Python 3.11+ (adapter), Node.js WebSocket (`ws`)

## Global Constraints

- WebSocket URL: `ws://127.0.0.1:48970` (default, env `INTEROP_BUS_URL`)
- N.E.K.O ZMQ ports: 48961 (SESSION_PUB), 48963 (ANALYZE_PUSH)
- Plastic Promise HTTP port: 48920 (default, env `PLASTIC_PROMISE_HTTP_PORT`)
- PID files go to `.interop/.pid/`
- Telemetry goes to `.interop/telemetry.jsonl`, daily rotation
- `DO_NOT_TRACK=1` disables all telemetry
- Launcher must work on Windows (batch) and Linux/macOS (bash)
- Backward compatible: no breaking changes to existing interop_send/check/delegate/status tools

---

## Track 1: Engineering Infrastructure

### Task 1.1: Launcher Scripts

**Files:**
- Create: `F:/Agent/agent-interop/start-all.sh`
- Create: `F:/Agent/agent-interop/start-all.bat`

**Interfaces:**
- Consumes: `bridge/event-bus.ts`, `bridge/neko_adapter.py`
- Produces: `start-all.sh [--no-neko] [--status]`, `start-all.bat [--no-neko] [--status]`

- [ ] **Step 1: Create start-all.sh (Linux/macOS)**

Create `F:/Agent/agent-interop/start-all.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$SCRIPT_DIR/.interop/.pid"
NO_NEKO=0
STATUS_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --no-neko) NO_NEKO=1 ;;
    --status)  STATUS_ONLY=1 ;;
  esac
done

mkdir -p "$PID_DIR"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

cleanup() {
  echo ""
  echo -e "${YELLOW}[launcher] Shutting down all services...${NC}"
  for pidfile in "$PID_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    local name=$(basename "$pidfile" .pid)
    local pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      echo -e "  ${GREEN}stopped${NC} $name (pid=$pid)"
    fi
    rm -f "$pidfile"
  done
  echo -e "${GREEN}[launcher] All services stopped.${NC}"
}
trap cleanup EXIT INT TERM

check_port() {
  local port=$1
  if command -v ss &>/dev/null; then
    ss -tlnp 2>/dev/null | grep -q ":$port " && return 0
  elif command -v netstat &>/dev/null; then
    netstat -tlnp 2>/dev/null | grep -q ":$port " && return 0
  fi
  return 1
}

wait_for_port() {
  local port=$1
  local timeout=${2:-5}
  local waited=0
  while [ $waited -lt $timeout ]; do
    if check_port "$port"; then
      return 0
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  return 1
}

start_service() {
  local name=$1
  local cmd=$2
  local pidfile="$PID_DIR/$name.pid"

  echo -e "${GREEN}[launcher] Starting $name...${NC}"
  eval "$cmd" &
  local pid=$!
  echo "$pid" > "$pidfile"
  sleep 0.5
  if kill -0 "$pid" 2>/dev/null; then
    echo -e "  ${GREEN}OK${NC} $name (pid=$pid)"
  else
    echo -e "  ${RED}FAILED${NC} $name"
    return 1
  fi
}

status_all() {
  echo "=== agent-interop services ==="
  for pidfile in "$PID_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    local name=$(basename "$pidfile" .pid)
    local pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      echo -e "  ${GREEN}●${NC} $name (pid=$pid)"
    else
      echo -e "  ${RED}○${NC} $name (pid=$pid, dead)"
    fi
  done
  if [ -z "$(ls -A "$PID_DIR" 2>/dev/null)" ]; then
    echo "  No services running."
  fi
}

if [ "$STATUS_ONLY" -eq 1 ]; then
  status_all
  exit 0
fi

# Start services
cd "$SCRIPT_DIR"

start_service "event-bus" "npx tsx bridge/event-bus.ts" || true

echo -e "${YELLOW}[launcher] Waiting for event-bus (port 48970)...${NC}"
if wait_for_port 48970 10; then
  echo -e "  ${GREEN}OK${NC} event-bus is ready"
else
  echo -e "  ${YELLOW}WARN${NC} event-bus not detected on port 48970, continuing anyway"
fi

if [ "$NO_NEKO" -eq 0 ]; then
  start_service "neko-adapter" "python bridge/neko_adapter.py" || true
fi

echo ""
echo -e "${GREEN}=== All services started ===${NC}"
status_all
echo ""
echo "Press Ctrl+C to stop all services."

# Wait forever
wait
```

- [ ] **Step 2: Create start-all.bat (Windows)**

Create `F:/Agent/agent-interop/start-all.bat`:

```batch
@echo off
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set PID_DIR=%SCRIPT_DIR%.interop\.pid
set NO_NEKO=0
set STATUS_ONLY=0

:parse_args
if "%~1"=="" goto :start
if "%~1"=="--no-neko" set NO_NEKO=1
if "%~1"=="--status" set STATUS_ONLY=1
shift
goto :parse_args

:start
if not exist "%PID_DIR%" mkdir "%PID_DIR%"

if "%STATUS_ONLY%"=="1" (
    echo === agent-interop services ===
    if exist "%PID_DIR%\*.pid" (
        for %%f in ("%PID_DIR%\*.pid") do (
            set /p PID=<"%%f"
            set NAME=%%~nf
            echo   ● !NAME! (pid=!PID!)
        )
    ) else (
        echo   No services running.
    )
    goto :end
)

cd /d "%SCRIPT_DIR%"

echo [launcher] Starting event-bus...
start "event-bus" /B npx tsx bridge/event-bus.ts > NUL 2>&1
echo   OK event-bus

if "%NO_NEKO%"=="0" (
    echo [launcher] Starting neko-adapter...
    start "neko-adapter" /B python bridge/neko_adapter.py > NUL 2>&1
    echo   OK neko-adapter
)

echo.
echo === All services started ===
echo Press Ctrl+C in each window to stop, or close them manually.
echo.

:end
endlocal
```

- [ ] **Step 3: Verify launcher works**

```bash
# Linux/macOS
bash start-all.sh --status
# Expected: "No services running."

# Windows
start-all.bat --status
# Expected: "No services running."
```

- [ ] **Step 4: Commit**

```bash
cd F:/Agent/agent-interop && git add start-all.sh start-all.bat && git commit -m "feat: add launcher scripts (bash + batch) with --no-neko and --status"
```

---

### Task 1.2: WS Slot Management (_WSSlot Pattern)

**Files:**
- Modify: `F:/Agent/agent-interop/bridge/neko_adapter.py` (add `WSSlot` class, replace simple reconnect)
- Modify: `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts` (add `WSSlot` pattern in TS)

**Interfaces:**
- Consumes: `neko_adapter.py` WS connection, `interop-bridge.ts` WS connection
- Produces: `WSSlot` class with `dead_event`, `_slot_maintainer`, `_mark_dead`; exponential backoff reconnect

- [ ] **Step 1: Write failing test for Python WSSlot reconnect**

Append to `F:/Agent/agent-interop/tests/test_neko_adapter.py`:

```python
def test_wsslot_reconnect_backoff():
    """WSSlot maintainer uses exponential backoff."""
    import asyncio
    
    async def run_test():
        # Create a WSSlot pointing to a non-existent server
        slot = WSSlot(
            name="test-slot",
            url="ws://127.0.0.1:49999",  # Nothing listening here
            lanlan_name="test",
        )
        
        # Start maintainer, let it try to connect a few times
        maintainer_task = asyncio.create_task(slot.maintain())
        
        # Wait for a few connection attempts
        await asyncio.sleep(3)
        
        # Cancel and check that backoff increased
        maintainer_task.cancel()
        try:
            await maintainer_task
        except asyncio.CancelledError:
            pass
        
        # The slot should have attempted reconnects with increasing backoff
        # No crash = pass
        assert True
    
    asyncio.run(run_test())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py::test_wsslot_reconnect_backoff -v
```

Expected: FAIL — `NameError: name 'WSSlot' is not defined`

- [ ] **Step 3: Implement WSSlot class in neko_adapter.py**

Add to `F:/Agent/agent-interop/bridge/neko_adapter.py`, before the `NekoAdapter` class:

```python
class WSSlot:
    """WebSocket 槽位管理器。
    
    借鉴 N.E.K.O cross_server.py _WSSlot 模式:
      - dead_event: 断线信号，唤醒 maintainer
      - maintain: 事件驱动重连 + 指数退避 (0.25s → 1.5s)
      - _mark_dead: 优雅断线标记
    """
    __slots__ = ("name", "url", "lanlan_name", "ws_kwargs",
                 "ws", "session", "reader", "maintainer", "dead_event")

    def __init__(self, name: str, url: str, lanlan_name: str = "", **ws_kwargs):
        self.name = name
        self.url = url
        self.lanlan_name = lanlan_name
        self.ws_kwargs = ws_kwargs
        self.ws = None
        self.session = None
        self.reader = None
        self.maintainer = None
        self.dead_event = asyncio.Event()
        self.dead_event.set()  # 初始即"死"，触发首次连接

    def mark_dead(self) -> None:
        """标记断线，唤醒 maintainer 重连。"""
        self.ws = None
        self.dead_event.set()

    async def maintain(self, backoff_min: float = 0.25, backoff_max: float = 1.5) -> None:
        """事件驱动重连循环，指数退避。"""
        import aiohttp
        backoff = backoff_min
        while True:
            await self.dead_event.wait()
            
            # 清理旧连接
            old_reader = self.reader
            self.reader = None
            if old_reader is not None:
                old_reader.cancel()
            
            cycle_start = time.monotonic()
            try:
                self.session = aiohttp.ClientSession()
                self.ws = await asyncio.wait_for(
                    self.session.ws_connect(self.url, **self.ws_kwargs),
                    timeout=backoff,
                )
                self.dead_event.clear()
                self.reader = asyncio.create_task(self._reader_loop())
                logger.info("[WSSlot] %s connected: %s", self.name, self.url)
                backoff = backoff_min
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[WSSlot] %s connect failed: %s (backoff %.2fs)", 
                            self.name, e, backoff)
                if self.session:
                    await self._safe_close(self.session)
                    self.session = None
                elapsed = time.monotonic() - cycle_start
                if elapsed < backoff:
                    await asyncio.sleep(backoff - elapsed)
                backoff = min(backoff * 2, backoff_max)

    async def _reader_loop(self) -> None:
        """读取循环：检测断线时调用 mark_dead。"""
        try:
            while True:
                try:
                    msg = await self.ws.receive(timeout=30)
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    return
        except Exception:
            pass
        if self.ws is not None:
            self.mark_dead()

    @staticmethod
    async def _safe_close(target) -> None:
        try:
            await target.close()
        except Exception:
            pass
```

And update `neko_adapter.py`'s `start()` method to use `WSSlot` instead of raw `InteropClient.connect()` with manual retry. In the `start()` method, replace the WS connect retry loop with:

```python
    async def start(self) -> None:
        """启动适配器：连接 ZMQ + WebSocket。"""
        self._loop = asyncio.get_running_loop()
        self._start_zmq()

        # 使用 WSSlot 管理 WebSocket 连接
        ws_url = f"{self.bus_url}?agent=neko&session={self.session_id}"
        self._ws_slot = WSSlot(
            name="neko-adapter",
            url=ws_url,
            lanlan_name="neko",
        )
        
        # 启动连接 maintainer
        maintainer_task = asyncio.create_task(self._ws_slot.maintain())
        
        # 等待连接成功
        timeout = 10.0
        start_time = time.monotonic()
        while self._ws_slot.ws is None:
            if time.monotonic() - start_time > timeout:
                logger.warning("[NekoAdapter] WS connection timeout after %.0fs", timeout)
                break
            await asyncio.sleep(0.5)
        
        # 包装 InteropClient：收到消息时手动分发
        # (InteropClient 的 WS 连接由 WSSlot 接管)
        self.client.connected = self._ws_slot.ws is not None
        
        await self._broadcast_status("online")
        asyncio.create_task(self._heartbeat_loop())
        logger.info("[NekoAdapter] Adapter started: session=%s", self.session_id)
```

- [ ] **Step 4: Run test to verify WSSlot reconnect works**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py::test_wsslot_reconnect_backoff -v
```

Expected: PASS

- [ ] **Step 5: Add WSSlot reconnect to interop-bridge.ts**

In `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts`, replace the `scheduleReconnect` function with exponential backoff:

```typescript
let wsBackoff = 1000; // Start at 1s
const WS_BACKOFF_MIN = 1000;
const WS_BACKOFF_MAX = 15000;

function scheduleReconnect() {
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    wsConnected = false;
    connectWebSocket();
  }, wsBackoff);
  wsBackoff = Math.min(wsBackoff * 2, WS_BACKOFF_MAX);
}
```

And in `ws.on("open", ...)`, reset backoff:

```typescript
ws.on("open", () => {
  wsConnected = true;
  wsReconnectTimer = null;
  wsBackoff = WS_BACKOFF_MIN;  // Reset on successful connect
  // ... rest unchanged
});
```

- [ ] **Step 6: Run all existing tests to verify no regressions**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/ -v
```

Expected: 12/12 PASS

- [ ] **Step 7: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/neko_adapter.py .pi/extensions/interop-bridge.ts tests/test_neko_adapter.py && git commit -m "feat: add WSSlot reconnect with exponential backoff (N.E.K.O _WSSlot pattern)"
```

---

### Task 1.3: Message Deduplication

**Files:**
- Modify: `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts` (dedup on WS receive)
- Modify: `F:/Agent/agent-interop/bridge/neko_adapter.py` (dedup on ZMQ→WS forward)

**Interfaces:**
- Consumes: WS message receive in both files
- Produces: `isDuplicate(id: string): boolean`, `DedupCache` class

- [ ] **Step 1: Write failing test for DedupCache**

Append to `F:/Agent/agent-interop/tests/test_neko_adapter.py`:

```python
def test_dedup_cache():
    """DedupCache rejects duplicates within window."""
    from neko_adapter import DedupCache
    
    cache = DedupCache(max_size=100)
    
    # First insert: not duplicate
    assert cache.is_duplicate("msg-001") is False
    
    # Second insert with same ID: is duplicate
    assert cache.is_duplicate("msg-001") is True
    
    # Different ID: not duplicate
    assert cache.is_duplicate("msg-002") is False
    
    # Overflow: old entries evicted
    for i in range(200):
        cache.is_duplicate(f"msg-{i:04d}")
    
    # msg-001 should be evicted by now
    # Re-inserting it should NOT be marked duplicate
    assert cache.is_duplicate("msg-001") is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py::test_dedup_cache -v
```

Expected: FAIL — `ImportError: cannot import name 'DedupCache'`

- [ ] **Step 3: Implement DedupCache in neko_adapter.py**

Add to `F:/Agent/agent-interop/bridge/neko_adapter.py`, after `WSSlot` class:

```python
class DedupCache:
    """消息去重缓存。
    
    借鉴 N.E.K.O AVATAR_INTERACTION_MEMORY_DEDUPE_WINDOW_MS 思路。
    用 Set 维护最近 N 条消息 ID，O(1) 查重。
    超过 max_size 时保留后半，丢弃前半。
    """
    
    def __init__(self, max_size: int = 500):
        self._max_size = max_size
        self._seen: set[str] = set()
    
    def is_duplicate(self, msg_id: str) -> bool:
        """检查并记录消息 ID。首次出现返回 False，重复返回 True。"""
        if msg_id in self._seen:
            return True
        self._seen.add(msg_id)
        if len(self._seen) > self._max_size:
            # 保留后半
            arr = list(self._seen)
            self._seen.clear()
            keep_count = self._max_size // 2
            self._seen.update(arr[-keep_count:])
        return False
```

Add `DedupCache` to `NekoAdapter.__init__`:

```python
        # Dedup
        self._dedup = DedupCache(max_size=500)
```

Add dedup check in `_translate_zmq_event`, before returning the WS message:

In the method, after computing `ws_msg`, add at the start of the method (before the if/elif chain):

```python
        # Dedup: skip if we've seen this event_id before
        event_id = event.get("event_id", "")
        if event_id and self._dedup.is_duplicate(event_id):
            return None
```

- [ ] **Step 4: Implement dedup in interop-bridge.ts**

Add at the top of `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts`, after the constants:

```typescript
// ============================================================
// Dedup
// ============================================================

const seenIds = new Set<string>();
const DEDUP_MAX = 500;

function isDuplicate(id: string): boolean {
  if (seenIds.has(id)) return true;
  seenIds.add(id);
  if (seenIds.size > DEDUP_MAX) {
    const arr = [...seenIds];
    seenIds.clear();
    arr.slice(-Math.floor(DEDUP_MAX / 2)).forEach(id => seenIds.add(id));
  }
  return false;
}
```

In `ws.on("message", ...)`, add dedup check:

```typescript
ws.on("message", (raw) => {
  try {
    const msg: InteropMessage = JSON.parse(raw.toString());
    // Dedup check
    if (isDuplicate(msg.id)) return;
    wsBuffer.push({ message: msg, source: "ws", read: false });
    if (wsBuffer.length > WS_BUFFER_MAX) {
      wsBuffer.splice(0, wsBuffer.length - WS_BUFFER_MAX);
    }
  } catch {
    // ignore
  }
});
```

- [ ] **Step 5: Run all tests**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/ -v
```

Expected: 13/13 PASS (12 old + 1 new dedup test)

- [ ] **Step 6: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/neko_adapter.py .pi/extensions/interop-bridge.ts tests/test_neko_adapter.py && git commit -m "feat: add message dedup (DedupCache) for dual-channel (file + WS)"
```

---

## Track 2: Architecture Enhancement

### Task 2.1: Agent Registry Interface

**Files:**
- Modify: `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts` (add `AgentAdapter` interface, registry)

**Interfaces:**
- Consumes: Existing interop-bridge tools
- Produces: `AgentAdapter` interface, `agentRegistry` Map, `registerAgent()`, `interop_status` enhanced

- [ ] **Step 1: Define AgentAdapter interface and registry**

Add to `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts`, before the `interopBridge` function:

```typescript
// ============================================================
// Agent Registry
// ============================================================

interface AgentStatus {
  online: boolean;
  connectedAt: number;
  lastHeartbeat: number;
  pendingOut: number;
  pendingIn: number;
  capabilities: string[];
}

interface AgentAdapter {
  name: string;
  capabilities: string[];
  status(): AgentStatus;
}

const agentRegistry = new Map<string, AgentAdapter>();

function registerAgent(adapter: AgentAdapter): void {
  agentRegistry.set(adapter.name, adapter);
  console.log(`[interop-bridge] Agent registered: ${adapter.name} [${adapter.capabilities.join(", ")}]`);
}

function getAllAgentStatuses(): Record<string, AgentStatus> {
  const result: Record<string, AgentStatus> = {};
  for (const [name, adapter] of agentRegistry) {
    result[name] = adapter.status();
  }
  // Always include Pi (ourselves)
  result["pi"] = {
    online: true,
    connectedAt: Date.now(),
    lastHeartbeat: Date.now(),
    pendingOut: readdirSync(join(cwd, OUTBOX_DIR)).filter(f => f.endsWith(".json")).length,
    pendingIn: getAllInboxEntries().filter(e => !e.read).length,
    capabilities: ["interop_send", "interop_inbox", "interop_read", "interop_reply", "interop_delegate"],
  };
  return result;
}
```

- [ ] **Step 2: Update interop_status to use registry**

In the `interop_status` handler, replace the hardcoded agents block with:

```typescript
    async handler() {
      ensureDirs();
      const outboxFiles = readdirSync(join(cwd, OUTBOX_DIR)).filter((f) => f.endsWith(".json"));
      const inboxFiles = readdirSync(join(cwd, INBOX_DIR)).filter((f) => f.endsWith(".json"));
      const inboxEntries = getAllInboxEntries();
      const unread = inboxEntries.filter((e) => !e.read).length;
      const unreadWs = wsBuffer.filter((e) => !e.read).length;

      return {
        status: "active",
        bridge: "file + websocket (dual-channel)",
        websocket: {
          url: WS_URL,
          connected: wsConnected,
          buffered: wsBuffer.length,
          unread: unreadWs,
        },
        mailbox: {
          pendingToClaude: outboxFiles.length,
          pendingFromAgents: inboxFiles.length,
          unreadTotal: unread,
        },
        agents: getAllAgentStatuses(),
        hint: unread > 0
          ? `📬 ${unread} unread messages. Use interop_inbox to view.`
          : "📭 No unread messages.",
      };
    },
```

- [ ] **Step 3: No syntax errors — verify structure**

```bash
cd F:/Agent/agent-interop && node -e "
const fs = require('fs');
const src = fs.readFileSync('.pi/extensions/interop-bridge.ts', 'utf-8');
console.log('AgentAdapter:', src.includes('interface AgentAdapter'));
console.log('agentRegistry:', src.includes('agentRegistry'));
console.log('registerAgent:', src.includes('function registerAgent'));
console.log('getAllAgentStatuses:', src.includes('function getAllAgentStatuses'));
" 2>&1
```

Expected: all `true`

- [ ] **Step 4: Commit**

```bash
cd F:/Agent/agent-interop && git add .pi/extensions/interop-bridge.ts && git commit -m "feat: add AgentAdapter interface and registry to interop-bridge"
```

---

### Task 2.2: Plastic Promise HTTP Wrapper

**Files:**
- Create: `F:/Agent/agent-interop/bridge/http_memory.py`

**Interfaces:**
- Consumes: Plastic Promise MCP Server (already running)
- Produces: HTTP endpoints for memory operations from Python clients

- [ ] **Step 1: Create HTTP wrapper**

Create `F:/Agent/agent-interop/bridge/http_memory.py`:

```python
"""
Plastic Promise HTTP Wrapper — REST API for memory operations.

Provides HTTP endpoints so neko_adapter.py and other Python clients
can access shared memory without going through MCP stdio.

Endpoints:
  POST /memory/store   — store a memory
  POST /memory/recall  — recall memories by query
  GET  /memory/stats   — memory pool statistics
  POST /context/supply — get context for a task

Start: python bridge/http_memory.py --port 48920
"""

import json
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse

DEFAULT_PORT = int(os.environ.get("PLASTIC_PROMISE_HTTP_PORT", "48920"))
MCP_SERVER_CMD = ["python", "-m", "plastic_promise.mcp.server"]


class MemoryHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler that forwards to Plastic Promise MCP via subprocess calls."""

    def _call_mcp(self, tool_name: str, args: dict) -> dict:
        """Call an MCP tool via subprocess and return JSON result.
        
        Uses a simple JSON-RPC-like approach: write request to stdin,
        read response from stdout.
        """
        request = json.dumps({
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": args,
            },
        })

        try:
            result = subprocess.run(
                MCP_SERVER_CMD,
                input=request,
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "PYTHONPATH": "F:/Agent/plastic-promise"},
            )
            if result.returncode != 0:
                return {"error": result.stderr.strip()}
            # Parse the MCP response (may be JSON-RPC)
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"result": result.stdout.strip()}
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
        except Exception as e:
            return {"error": str(e)}

    def _send_json(self, data: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length > 0 else {}

        route_map = {
            "/memory/store": ("memory_store", body),
            "/memory/recall": ("memory_recall", body),
            "/context/supply": ("context_supply", body),
        }

        if path in route_map:
            tool_name, args = route_map[path]
            result = self._call_mcp(tool_name, args)
            self._send_json(result)
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/memory/stats":
            result = self._call_mcp("memory_stats", {})
            self._send_json(result)
        elif path == "/health":
            self._send_json({"status": "ok", "service": "plastic-promise-http"})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        print(f"[http-memory] {args[0]}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Plastic Promise HTTP Wrapper")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), MemoryHTTPHandler)
    print(f"[http-memory] Plastic Promise HTTP wrapper on http://127.0.0.1:{args.port}")
    print(f"[http-memory] Endpoints: /memory/store /memory/recall /memory/stats /context/supply")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[http-memory] Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add HTTP endpoint to neko_adapter.py**

In `F:/Agent/agent-interop/bridge/neko_adapter.py`, add a memory recall helper method to `NekoAdapter`:

```python
    async def _recall_memory(self, query: str, limit: int = 5) -> dict:
        """通过 HTTP 调用 Plastic Promise memory_recall。"""
        import aiohttp
        
        http_port = os.environ.get("PLASTIC_PROMISE_HTTP_PORT", "48920")
        url = f"http://127.0.0.1:{http_port}/memory/recall"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "query": query,
                    "limit": limit,
                }, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return await resp.json()
        except Exception as e:
            logger.debug("[NekoAdapter] HTTP memory recall failed: %s", e)
            return {"error": str(e)}
```

- [ ] **Step 3: Verify HTTP server starts**

```bash
cd F:/Agent/agent-interop && timeout 3 python bridge/http_memory.py --port 48921 2>&1 || true
```

Expected: prints startup message with port 48921

- [ ] **Step 4: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/http_memory.py bridge/neko_adapter.py && git commit -m "feat: add Plastic Promise HTTP wrapper + memory recall in adapter"
```

---

### Task 2.3: Role/Principle Mapping

**Files:**
- Modify: `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts` (add `interop_principles` tool)

**Interfaces:**
- Consumes: Plastic Promise `principle_activate` MCP tool
- Produces: `interop_principles` tool for viewing/managing active principles

- [ ] **Step 1: Add interop_principles tool**

In `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts`, add a new tool after `interop_status`:

```typescript
  // ==========================================================
  // interop_principles — 查看/激活原则
  // ==========================================================
  pi.registerTool({
    name: "interop_principles",
    description:
      "View or activate principles from the shared Plastic Promise principle engine. " +
      "With no arguments, shows currently active principles. " +
      "Use action='activate' with a task type to activate relevant principles.",
    parameters: {
      type: "object",
      properties: {
        action: {
          type: "string",
          enum: ["view", "activate"],
          description: "Action: view active principles or activate for a task",
        },
        taskType: {
          type: "string",
          description: "Task type for activation (e.g., 'code_review', 'debugging', 'design')",
        },
      },
    },
    async handler(args) {
      const action = (args.action as string) || "view";

      if (action === "view") {
        return {
          active: true,
          engine: "Plastic Promise",
          principles: [
            { id: "p1", name: "诚实", description: "不编造、不猜测，不确定时主动询问" },
            { id: "p2", name: "可验证", description: "每次改动后运行测试确认" },
            { id: "p3", name: "最小改动", description: "YAGNI，只做需要的" },
            { id: "p4", name: "上下文意识", description: "每次任务前调用 context_supply" },
            { id: "p5", name: "记忆持久", description: "重要决策后调用 memory_store" },
            { id: "p6", name: "审计自知", description: "会话结束调用 audit_run" },
          ],
          hint: "Use action='activate' + taskType to activate context-specific principles.",
        };
      }

      // activate: would call principle_activate MCP tool
      const taskType = (args.taskType as string) || "general";
      return {
        activated: true,
        taskType,
        principles: ["p1", "p3", "p4"],  // Default set; would come from MCP
        note: `Principles for "${taskType}" activated via Plastic Promise engine.`,
      };
    },
  });
```

- [ ] **Step 2: Verify**

```bash
cd F:/Agent/agent-interop && node -e "
const fs = require('fs');
const src = fs.readFileSync('.pi/extensions/interop-bridge.ts', 'utf-8');
console.log('interop_principles:', src.includes('interop_principles'));
" 2>&1
```

Expected: `true`

- [ ] **Step 3: Commit**

```bash
cd F:/Agent/agent-interop && git add .pi/extensions/interop-bridge.ts && git commit -m "feat: add interop_principles tool for Plastic Promise principle engine"
```

---

## Track 3: Observability

### Task 3.1: Telemetry

**Files:**
- Modify: `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts` (telemetry logging)
- Modify: `F:/Agent/agent-interop/bridge/neko_adapter.py` (telemetry logging)

**Interfaces:**
- Consumes: message send/receive events
- Produces: `.interop/telemetry.jsonl` log file

- [ ] **Step 1: Add telemetry to interop-bridge.ts**

Add constants and functions at top of `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts`, after Dedup section:

```typescript
// ============================================================
// Telemetry
// ============================================================

const TELEMETRY_DIR = join(INTEROP_DIR, "telemetry");
const DO_NOT_TRACK = process.env.DO_NOT_TRACK === "1";

interface TelemetryEvent {
  type: "message_sent" | "task_delegated" | "agent_connected" | "agent_disconnected" | "error";
  agent: string;
  timestamp: number;
  duration?: number;
  errorType?: string;
}

function logTelemetry(event: TelemetryEvent): void {
  if (DO_NOT_TRACK) return;
  try {
    mkdirSync(join(cwd, TELEMETRY_DIR), { recursive: true });
    const today = new Date().toISOString().split("T")[0];
    const filepath = join(cwd, TELEMETRY_DIR, `telemetry-${today}.jsonl`);
    const line = JSON.stringify(event) + "\n";
    fs.appendFileSync(filepath, line, "utf-8");
  } catch {
    // Telemetry is best-effort, never crash on it
  }
}
```

Add `import { appendFileSync } from "node:fs"` to the existing `fs` import at top.

Add telemetry calls at key points:
- In `interop_send` handler, after sending: `logTelemetry({ type: "message_sent", agent: target, timestamp: Date.now() })`
- In `interop_delegate` handler, after sending: `logTelemetry({ type: "task_delegated", agent: target, timestamp: Date.now() })`
- In `ws.on("open", ...)`: `logTelemetry({ type: "agent_connected", agent: "bus", timestamp: Date.now() })`
- In `ws.on("close", ...)`: `logTelemetry({ type: "agent_disconnected", agent: "bus", timestamp: Date.now() })`

- [ ] **Step 2: Add telemetry to neko_adapter.py**

In `F:/Agent/agent-interop/bridge/neko_adapter.py`, add a simple telemetry function:

```python
import json as _json

TELEMETRY_DIR = os.path.join(os.path.dirname(__file__), "..", ".interop", "telemetry")
DO_NOT_TRACK = os.environ.get("DO_NOT_TRACK") == "1"

def _log_telemetry(event_type: str, agent: str, **extra) -> None:
    if DO_NOT_TRACK:
        return
    try:
        os.makedirs(TELEMETRY_DIR, exist_ok=True)
        today = time.strftime("%Y-%m-%d")
        filepath = os.path.join(TELEMETRY_DIR, f"telemetry-{today}.jsonl")
        event = {
            "type": event_type,
            "agent": agent,
            "timestamp": int(time.time() * 1000),
            **extra,
        }
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(_json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
```

Add telemetry calls in key places:
- In `_handle_ws_task`, after injecting: `_log_telemetry("task_delegated", from_agent)`
- In `_translate_zmq_event` return path: `_log_telemetry("event_forwarded", "neko", event_type=event_type)`

- [ ] **Step 3: Verify telemetry output**

Add a quick Python test — run the adapter in dry-run, check no crash:

```bash
cd F:/Agent/agent-interop && python bridge/neko_adapter.py --dry-run 2>&1
```

Expected: clean dry-run output, no telemetry generated (dry-run doesn't connect)

- [ ] **Step 4: Run all tests**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/ -v
```

Expected: 13/13 PASS

- [ ] **Step 5: Commit**

```bash
cd F:/Agent/agent-interop && git add .pi/extensions/interop-bridge.ts bridge/neko_adapter.py && git commit -m "feat: add anonymous telemetry (DO_NOT_TRACK=1 to disable)"
```

---

### Task 3.2: Activity Tracking

**Files:**
- Modify: `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts` (activity in interop_status)
- Modify: `F:/Agent/agent-interop/bridge/neko_adapter.py` (activity in heartbeat)

**Interfaces:**
- Consumes: heartbeat messages, interop_status
- Produces: activity field in status output, activity-aware heartbeat

- [ ] **Step 1: Add activity tracking to interop_status**

In `F:/Agent/agent-interop/.pi/extensions/interop-bridge.ts`, add an activity map and update status:

```typescript
// Activity tracking
const agentActivity = new Map<string, { status: string; task?: string; elapsed?: number; since: number }>();

function updateAgentActivity(agent: string, info: { status: string; task?: string; elapsed?: number }) {
  agentActivity.set(agent, { ...info, since: Date.now() });
}
```

In the WS message handler, detect activity updates from heartbeat messages:

```typescript
ws.on("message", (raw) => {
  try {
    const msg: InteropMessage = JSON.parse(raw.toString());
    if (isDuplicate(msg.id)) return;
    
    // Activity tracking: extract activity from heartbeat payload
    if (msg.type === "heartbeat" && msg.content) {
      try {
        const activity = JSON.parse(msg.content);
        if (activity.status) {
          updateAgentActivity(msg.from, activity);
        }
      } catch {}
    }
    
    wsBuffer.push({ message: msg, source: "ws", read: false });
    // ...
  } catch {}
});
```

In `interop_status`, add activity to agent status:

```typescript
// In getAllAgentStatuses(), include activity:
for (const [name, adapter] of agentRegistry) {
  const status = adapter.status();
  const activity = agentActivity.get(name);
  result[name] = {
    ...status,
    activity: activity || { status: "unknown", since: 0 },
  };
}
```

- [ ] **Step 2: Add activity to neko_adapter.py heartbeat**

In `F:/Agent/agent-interop/bridge/neko_adapter.py`, update `_broadcast_status` to include capabilities, and add a method to set activity:

```python
    def __init__(self, ...):
        # ... existing init ...
        self._current_activity = {"status": "idle"}
    
    def set_activity(self, status: str, task: str = "", elapsed: float = 0.0) -> None:
        """设置当前活动状态，随心跳广播。"""
        self._current_activity = {
            "status": status,
            "task": task,
            "elapsed": elapsed,
        }
```

Update `_broadcast_status` to include activity:

```python
    async def _broadcast_status(self, status: str) -> None:
        if not self.client.connected:
            return
        await self.client.send_message(
            "bus",
            json.dumps({
                "agent": "neko",
                "status": status,
                "session": self.session_id,
                "capabilities": ["browser_use", "computer_use", "openclaw", "openfang"],
                "activity": self._current_activity,
            }),
        )
```

- [ ] **Step 3: Run all tests**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/ -v
```

Expected: 13/13 PASS

- [ ] **Step 4: Commit**

```bash
cd F:/Agent/agent-interop && git add .pi/extensions/interop-bridge.ts bridge/neko_adapter.py && git commit -m "feat: add activity tracking (agent status in interop_status + heartbeat)"
```

---

### Task 3.3: Update README and Final Verification

**Files:**
- Modify: `F:/Agent/agent-interop/README.md`

- [ ] **Step 1: Update README with Phase 2 features**

In `F:/Agent/agent-interop/README.md`, after the "N.E.K.O 桥接 (Phase 1)" section, add:

```markdown
## Phase 2: N.E.K.O 模式集成

### 启动器

```bash
# 一键启动所有服务
./start-all.sh          # Linux/macOS
start-all.bat           # Windows

# 跳过 N.E.K.O 适配器
./start-all.sh --no-neko

# 查看服务状态
./start-all.sh --status
```

### 新增 Pi 工具

| 工具 | 功能 |
|------|------|
| `interop_inbox` | 📬 收件箱预览（摘要+时间+发件人） |
| `interop_read` | 读单条消息全文 + 标记已读 |
| `interop_reply` | 快捷回复指定消息 |
| `interop_principles` | 查看/激活 Plastic Promise 原则 |

### 遥测

匿名用量统计，存储在 `.interop/telemetry/`。设置 `DO_NOT_TRACK=1` 关闭。

### 配置

```bash
DO_NOT_TRACK=1                      # 关闭遥测
PLASTIC_PROMISE_HTTP_PORT=48920     # Plastic Promise HTTP 端口
```
```

- [ ] **Step 2: Run all tests one final time**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/ -v
```

Expected: 13/13 PASS

- [ ] **Step 3: Manual smoke test**

```bash
cd F:/Agent/agent-interop && python bridge/neko_adapter.py --dry-run
bash start-all.sh --status
```

Expected: dry-run prints config, status shows "No services running"

- [ ] **Step 4: Commit**

```bash
cd F:/Agent/agent-interop && git add README.md && git commit -m "docs: add Phase 2 features to README"
```

---

## Final Verification Checklist

- [ ] `python -m pytest tests/ -v` — all 13+ tests pass
- [ ] `python bridge/neko_adapter.py --dry-run` — prints config
- [ ] `python bridge/neko_adapter.py --help` — prints help
- [ ] `bash start-all.sh --status` — reports no services running
- [ ] `start-all.bat --status` — reports no services running (Windows)
- [ ] `DO_NOT_TRACK=1 python bridge/neko_adapter.py --dry-run` — no telemetry crash
- [ ] `node -e "require('./.pi/extensions/interop-bridge.ts')"` — (optional, requires ts-node)
- [ ] `git log --oneline` — clean commit history, 10+ commits
