# N.E.K.O Bridge Adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `bridge/neko-adapter.py` — an independent process that bridges N.E.K.O's ZMQ event bus with the agent-interop WebSocket event bus.

**Architecture:** NekoAdapter runs as a standalone Python process. It opens two ZMQ sockets (SUB to SESSION_PUB for receiving N.E.K.O events, PUSH to ANALYZE_PUSH for injecting tasks) and reuses the existing `InteropClient` class from `bus-client.py` for WebSocket communication. A background thread receives ZMQ messages and dispatches them to the asyncio event loop for translation and forwarding.

**Tech Stack:** Python 3.11+, pyzmq, orjson, websockets (already installed), asyncio

## Global Constraints

- N.E.K.O ZMQ SESSION_PUB port: 48961 (default, configurable via `NEKO_ZMQ_SESSION_PUB_PORT`)
- N.E.K.O ZMQ ANALYZE_PUSH port: 48963 (default, configurable via `NEKO_ZMQ_ANALYZE_PUSH_PORT`)
- Interop WebSocket URL: `ws://127.0.0.1:48970` (default, configurable via `INTEROP_BUS_URL`)
- Adapter identifies as agent `neko` on the WebSocket bus
- All ZMQ messages serialized with orjson, all WS messages with json
- Must handle N.E.K.O not running (graceful retry, not crash)

---

### Task 1: Project Setup — Dependencies and Config

**Files:**
- Create: `bridge/__init__.py`
- Create: `F:/Agent/agent-interop/.env.example`
- Modify: `F:/Agent/agent-interop/bridge/bus-client.py` (add `__all__` or make importable)

**Interfaces:**
- Consumes: (none)
- Produces: `InteropClient` class importable from `bridge.bus-client`; `.env.example` for reference

- [ ] **Step 1: Make bridge a proper Python package**

Create `F:/Agent/agent-interop/bridge/__init__.py`:

```python
"""Agent Interop Bridge — WebSocket + ZMQ adapters."""
```

- [ ] **Step 2: Verify InteropClient can be imported from sibling module**

Add this test at the bottom of `bus-client.py` is already self-contained as a script, but we need to verify it can be imported as a module. Run:

```bash
cd F:/Agent/agent-interop && python -c "import sys; sys.path.insert(0, 'bridge'); from bus_client import InteropClient; print('OK:', InteropClient.__name__)"
```

Expected: `OK: InteropClient`

- [ ] **Step 3: Verify pyzmq and orjson are available**

```bash
python -c "import zmq; print('zmq:', zmq.__version__); import orjson; print('orjson:', orjson.__version__)"
```

Expected: prints version numbers for both. If missing, install:

```bash
pip install pyzmq orjson
```

- [ ] **Step 4: Create .env.example**

Create `F:/Agent/agent-interop/.env.example`:

```bash
# Interop Event Bus
INTEROP_BUS_URL=ws://127.0.0.1:48970

# N.E.K.O ZMQ Ports
NEKO_ZMQ_SESSION_PUB_PORT=48961
NEKO_ZMQ_ANALYZE_PUSH_PORT=48963

# Adapter
NEKO_ADAPTER_LOG_LEVEL=INFO
```

- [ ] **Step 5: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/__init__.py .env.example && git commit -m "chore: add bridge package init and env template"
```

---

### Task 2: ZMQ Connection Layer — Connect to N.E.K.O Event Bus

**Files:**
- Create: `F:/Agent/agent-interop/bridge/neko-adapter.py`

**Interfaces:**
- Consumes: (none)
- Produces: `NekoAdapter.__init__(bus_url, zmq_pub_port, zmq_analyze_port, session_id)`, `NekoAdapter._start_zmq()`, `NekoAdapter._stop_zmq()`, `NekoAdapter._zmq_recv_loop()`

- [ ] **Step 1: Write the failing test**

Create `F:/Agent/agent-interop/tests/test_neko_adapter.py`:

```python
"""Tests for N.E.K.O adapter ZMQ layer."""
import pytest
import zmq
import time
import threading
import orjson

# Ensure bridge is importable
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'bridge'))

from neko_adapter import NekoAdapter


def test_zmq_connect_and_receive_message():
    """Adapter SUB socket receives messages from a test PUB socket."""
    TEST_PUB_PORT = 48990  # Use a non-conflicting port for testing

    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=TEST_PUB_PORT,
        zmq_analyze_port=48991,
        session_id="test",
    )

    # Start ZMQ (don't start WS for this test)
    adapter._start_zmq()

    # Create a test publisher
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://127.0.0.1:{TEST_PUB_PORT}")

    # ZMQ PUB needs a brief moment for subscribers to connect (slow joiner)
    time.sleep(0.1)

    # Collect received events
    received = []
    original_handler = adapter._handle_zmq_event
    adapter._handle_zmq_event = lambda event: received.append(event)

    # Publish a test event
    test_event = {"event_type": "session_lifecycle", "agent": "test_agent", "status": "online"}
    pub.send(orjson.dumps(test_event))

    # Wait for delivery
    time.sleep(0.2)

    assert len(received) == 1
    assert received[0]["event_type"] == "session_lifecycle"
    assert received[0]["agent"] == "test_agent"

    # Cleanup
    adapter._handle_zmq_event = original_handler
    adapter._stop_zmq()
    pub.close()
    ctx.term()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py::test_zmq_connect_and_receive_message -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'neko_adapter'`

- [ ] **Step 3: Write minimal NekoAdapter with ZMQ layer**

Create `F:/Agent/agent-interop/bridge/neko-adapter.py`:

```python
"""
N.E.K.O Adapter — ZMQ ↔ WebSocket 桥接器

连接 N.E.K.O 的 ZMQ 事件总线和 Interop 的 WebSocket 事件总线，
实现 Pi ↔ Claude ↔ N.E.K.O 三方实时通信。

借鉴 N.E.K.O neko_event_bus.py (ZMQ PUB/SUB + PUSH/PULL) 和
cross_server.py (跨服务器转发) 模式。

启动: python bridge/neko-adapter.py
"""

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import orjson

try:
    import zmq
except ImportError:
    print("[NekoAdapter] pyzmq not installed: pip install pyzmq")
    sys.exit(1)

# Import InteropClient from sibling module
sys.path.insert(0, os.path.dirname(__file__))
from bus_client import InteropClient

logger = logging.getLogger("NekoAdapter")

# ============================================================
# 默认端口（与 N.E.K.O neko_event_bus.py 保持一致）
# ============================================================

DEFAULT_SESSION_PUB_PORT = int(os.environ.get("NEKO_ZMQ_SESSION_PUB_PORT", "48961"))
DEFAULT_ANALYZE_PUSH_PORT = int(os.environ.get("NEKO_ZMQ_ANALYZE_PUSH_PORT", "48963"))
DEFAULT_BUS_URL = os.environ.get("INTEROP_BUS_URL", "ws://127.0.0.1:48970")


class NekoAdapter:
    """N.E.K.O ↔ Interop WebSocket 桥接适配器。

    ZMQ 侧:
      - SUB socket → SESSION_PUB (接收 N.E.K.O 广播事件)
      - PUSH socket → ANALYZE_PUSH (向 agent_server 注入任务)

    WebSocket 侧:
      - 复用 InteropClient, 以 agent_type="neko" 连接
    """

    def __init__(
        self,
        bus_url: str = DEFAULT_BUS_URL,
        zmq_pub_port: int = DEFAULT_SESSION_PUB_PORT,
        zmq_analyze_port: int = DEFAULT_ANALYZE_PUSH_PORT,
        session_id: str = "",
    ):
        self.bus_url = bus_url
        self.zmq_pub_addr = f"tcp://127.0.0.1:{zmq_pub_port}"
        self.zmq_analyze_addr = f"tcp://127.0.0.1:{zmq_analyze_port}"
        self.session_id = session_id or str(uuid.uuid4())[:8]

        # ZMQ state
        self.zmq_ctx: Any = None
        self.zmq_sub: Any = None
        self.zmq_push: Any = None
        self._zmq_thread: Optional[threading.Thread] = None
        self._zmq_stop = threading.Event()
        self._zmq_ready = False

        # WebSocket client (created on start)
        self.client = InteropClient(agent_type="neko", session_id=self.session_id)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Routing
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.subscribers: Dict[str, List[Callable]] = {}

    # ============================================================
    # ZMQ 连接
    # ============================================================

    def _start_zmq(self) -> None:
        """打开 ZMQ SUB 和 PUSH socket，启动后台接收线程。"""
        self.zmq_ctx = zmq.Context()

        # SUB: 接收 N.E.K.O SESSION_PUB 广播
        self.zmq_sub = self.zmq_ctx.socket(zmq.SUB)
        self.zmq_sub.setsockopt(zmq.LINGER, 1000)
        self.zmq_sub.setsockopt(zmq.RCVTIMEO, 1000)
        self.zmq_sub.connect(self.zmq_pub_addr)
        self.zmq_sub.setsockopt_string(zmq.SUBSCRIBE, "")

        # PUSH: 向 N.E.K.O ANALYZE_PUSH 注入任务
        self.zmq_push = self.zmq_ctx.socket(zmq.PUSH)
        self.zmq_push.setsockopt(zmq.LINGER, 1000)
        self.zmq_push.connect(self.zmq_analyze_addr)

        self._zmq_stop.clear()
        self._zmq_ready = True
        self._zmq_thread = threading.Thread(
            target=self._zmq_recv_loop,
            name="neko-adapter-zmq",
            daemon=True,
        )
        self._zmq_thread.start()
        logger.info(
            "[NekoAdapter] ZMQ connected: SUB=%s PUSH=%s",
            self.zmq_pub_addr,
            self.zmq_analyze_addr,
        )

    def _stop_zmq(self) -> None:
        """关闭 ZMQ socket 和接收线程。"""
        self._zmq_stop.set()
        self._zmq_ready = False

        if self._zmq_thread is not None:
            self._zmq_thread.join(timeout=2.0)
            self._zmq_thread = None

        for sock in (self.zmq_sub, self.zmq_push):
            if sock is not None:
                try:
                    sock.close(linger=0)
                except Exception:
                    pass

        if self.zmq_ctx is not None:
            ctx = self.zmq_ctx
            self.zmq_ctx = None
            try:
                ctx.term()
            except Exception:
                pass

        logger.debug("[NekoAdapter] ZMQ stopped")

    def _zmq_recv_loop(self) -> None:
        """后台线程：从 ZMQ SUB 接收 N.E.K.O 事件。"""
        while not self._zmq_stop.is_set():
            try:
                raw = self.zmq_sub.recv()
                event = orjson.loads(raw)
                if isinstance(event, dict):
                    # 将事件派发到 asyncio 事件循环
                    if self._loop is not None and not self._loop.is_closed():
                        asyncio.run_coroutine_threadsafe(
                            self._handle_zmq_event(event), self._loop
                        )
            except zmq.Again:
                continue
            except Exception as e:
                if not self._zmq_stop.is_set():
                    logger.debug("[NekoAdapter] ZMQ recv error: %s", e)
                    time.sleep(0.05)

    async def _handle_zmq_event(self, event: Dict[str, Any]) -> None:
        """处理从 ZMQ 收到的 N.E.K.O 事件。子类或后续任务扩展。"""
        event_type = event.get("event_type", "unknown")
        logger.debug("[NekoAdapter] ZMQ event: %s", event_type)
        # 事件翻译在后续任务中实现
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py::test_zmq_connect_and_receive_message -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/neko-adapter.py tests/test_neko_adapter.py && git commit -m "feat: add NekoAdapter ZMQ connection layer with test"
```

---

### Task 3: N.E.K.O Event → WebSocket Message Translation

**Files:**
- Modify: `F:/Agent/agent-interop/bridge/neko-adapter.py` (extend `_handle_zmq_event`)
- Modify: `F:/Agent/agent-interop/tests/test_neko_adapter.py` (add translation tests)

**Interfaces:**
- Consumes: `NekoAdapter._handle_zmq_event(event: dict)` from Task 2
- Produces: `NekoAdapter._translate_zmq_event(event: dict) -> Optional[dict]`

- [ ] **Step 1: Write the failing test**

Append to `F:/Agent/agent-interop/tests/test_neko_adapter.py`:

```python
def test_translate_session_lifecycle():
    """session_lifecycle → neko:announce with capabilities."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    zmq_event = {
        "event_type": "session_lifecycle",
        "agent": "agent_server",
        "status": "online",
        "capabilities": ["browser_use", "computer_use", "openclaw"],
    }

    ws_msg = adapter._translate_zmq_event(zmq_event)

    assert ws_msg is not None
    assert ws_msg["topic"] == "neko:announce"
    assert ws_msg["type"] == "handshake"
    assert ws_msg["from"] == "neko"
    assert ws_msg["to"] == "*"

    payload = ws_msg["payload"]
    assert isinstance(payload, dict)
    assert payload["agent"] == "neko"
    assert payload["status"] == "online"
    assert "browser_use" in payload["capabilities"]


def test_translate_voice_transcript():
    """voice_transcript_observed → neko:voice."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    zmq_event = {
        "event_type": "voice_transcript_observed",
        "lanlan_name": "小天",
        "transcript": "今天天气真好",
    }

    ws_msg = adapter._translate_zmq_event(zmq_event)

    assert ws_msg is not None
    assert ws_msg["topic"] == "neko:voice"
    assert ws_msg["type"] == "message"
    payload = ws_msg["payload"]
    assert isinstance(payload, dict)
    assert payload["transcript"] == "今天天气真好"
    assert payload["lanlan_name"] == "小天"


def test_translate_agent_result():
    """agent_result → neko:result."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    zmq_event = {
        "event_type": "agent_result",
        "event_id": "abc123",
        "result": "Browser task completed",
        "status": "success",
    }

    ws_msg = adapter._translate_zmq_event(zmq_event)

    assert ws_msg is not None
    assert ws_msg["topic"] == "neko:result"
    assert ws_msg["type"] == "result"
    assert ws_msg["replyTo"] == "abc123"
    payload = ws_msg["payload"]
    assert payload["result"] == "Browser task completed"
    assert payload["status"] == "success"


def test_translate_unknown_event():
    """Unknown event_type → neko:event passthrough."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    zmq_event = {
        "event_type": "some_custom_event",
        "data": "hello",
    }

    ws_msg = adapter._translate_zmq_event(zmq_event)

    assert ws_msg is not None
    assert ws_msg["topic"] == "neko:event"
    assert ws_msg["type"] == "message"
    assert ws_msg["payload"] == zmq_event
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py -k "translate" -v
```

Expected: FAIL — `AttributeError: 'NekoAdapter' object has no attribute '_translate_zmq_event'`

- [ ] **Step 3: Implement _translate_zmq_event**

Add this method to `NekoAdapter` class in `bridge/neko-adapter.py`, after `_handle_zmq_event`:

```python
    def _translate_zmq_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """将 N.E.K.O ZMQ 事件翻译为 Interop WebSocket 消息。

        返回 None 表示此事件不需要转发。
        """
        event_type = event.get("event_type", "")

        if event_type == "session_lifecycle":
            # Agent 上线/下线 → 能力广播
            status = event.get("status", "unknown")
            capabilities = event.get("capabilities", [])
            return {
                "id": self._next_id(),
                "topic": "neko:announce",
                "from": "neko",
                "to": "*",
                "type": "handshake",
                "payload": {
                    "agent": "neko",
                    "status": status,
                    "capabilities": capabilities,
                },
                "timestamp": int(time.time() * 1000),
            }

        elif event_type == "voice_transcript_observed":
            # 实时语音 → 文本消息
            transcript = event.get("transcript", "")
            if not transcript:
                return None
            return {
                "id": self._next_id(),
                "topic": "neko:voice",
                "from": "neko",
                "to": "*",
                "type": "message",
                "payload": {
                    "lanlan_name": event.get("lanlan_name", ""),
                    "transcript": transcript,
                },
                "timestamp": int(time.time() * 1000),
            }

        elif event_type == "agent_result":
            # 任务执行结果 → 结果回执
            return {
                "id": self._next_id(),
                "topic": "neko:result",
                "from": "neko",
                "to": "*",
                "type": "result",
                "payload": {
                    "result": event.get("result", ""),
                    "status": event.get("status", "unknown"),
                },
                "replyTo": event.get("event_id", ""),
                "timestamp": int(time.time() * 1000),
            }

        else:
            # 未知事件 → 通用透传
            return {
                "id": self._next_id(),
                "topic": "neko:event",
                "from": "neko",
                "to": "*",
                "type": "message",
                "payload": event,
                "timestamp": int(time.time() * 1000),
            }

    def _next_id(self) -> str:
        """生成消息 ID，复刻 InteropClient 格式。"""
        return self.client._next_id()
```

Now update `_handle_zmq_event` to use translation and broadcast:

```python
    async def _handle_zmq_event(self, event: Dict[str, Any]) -> None:
        """处理从 ZMQ 收到的 N.E.K.O 事件。"""
        event_type = event.get("event_type", "unknown")
        logger.debug("[NekoAdapter] ZMQ event: %s id=%s", event_type, event.get("event_id", ""))

        # 1. 翻译为 WebSocket 消息
        ws_msg = self._translate_zmq_event(event)
        if ws_msg is None:
            return

        # 2. 通过 WebSocket 广播
        if self.client.connected:
            try:
                await self.client._send(ws_msg)
                logger.info("[NekoAdapter] Forwarded: %s → %s", event_type, ws_msg["topic"])
            except Exception as e:
                logger.warning("[NekoAdapter] Failed to forward %s: %s", event_type, e)

        # 3. 通知订阅者
        for handler in self.subscribers.get(event_type, []):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.debug("[NekoAdapter] Subscriber error for %s: %s", event_type, e)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py -k "translate" -v
```

Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/neko-adapter.py tests/test_neko_adapter.py && git commit -m "feat: add ZMQ event → WS message translation with tests"
```

---

### Task 4: WebSocket Task → N.E.K.O analyze_request Injection

**Files:**
- Modify: `F:/Agent/agent-interop/bridge/neko-adapter.py` (add WS handler registration, analyze_request injection)
- Modify: `F:/Agent/agent-interop/tests/test_neko_adapter.py` (add injection tests)

**Interfaces:**
- Consumes: `InteropClient._send(msg)` from bus-client.py; `NekoAdapter._zmq_recv_loop` event dispatch
- Produces: `NekoAdapter._handle_ws_task(msg)` → ZMQ PUSH `analyze_request`

- [ ] **Step 1: Write the failing test**

Append to `F:/Agent/agent-interop/tests/test_neko_adapter.py`:

```python
def test_ws_task_to_zmq_analyze_request():
    """WS task → ZMQ analyze_request with ack handling."""
    import asyncio

    TEST_ANALYZE_PORT = 48992

    async def run_test():
        adapter = NekoAdapter(
            bus_url="ws://127.0.0.1:48999",
            zmq_pub_port=48990,
            zmq_analyze_port=TEST_ANALYZE_PORT,
            session_id="test",
        )

        # Start ZMQ
        adapter._start_zmq()

        # Create a test PULL socket to receive the analyze_request
        ctx = zmq.Context()
        pull = ctx.socket(zmq.PULL)
        pull.bind(f"tcp://127.0.0.1:{TEST_ANALYZE_PORT}")
        pull.setsockopt(zmq.RCVTIMEO, 2000)

        # Simulate an incoming WS task from Pi
        ws_task = {
            "id": "pi-1719576000000-1",
            "topic": "pi:neko",
            "from": "pi",
            "to": "neko",
            "type": "task",
            "payload": "Browse to example.com and take a screenshot",
            "timestamp": int(time.time() * 1000),
        }

        # Call the handler directly
        await adapter._handle_ws_task(ws_task)

        # Verify the ZMQ message was sent
        raw = pull.recv()
        zmq_msg = orjson.loads(raw)

        assert zmq_msg["event_type"] == "analyze_request"
        assert zmq_msg["trigger"] == "pi_task"
        assert zmq_msg["lanlan_name"] == "pi"
        assert isinstance(zmq_msg["messages"], list)
        assert len(zmq_msg["messages"]) > 0
        assert zmq_msg["messages"][0]["content"] == ws_task["payload"]
        assert "event_id" in zmq_msg

        # Cleanup
        adapter._stop_zmq()
        pull.close()
        ctx.term()

    asyncio.run(run_test())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py::test_ws_task_to_zmq_analyze_request -v
```

Expected: FAIL — `AttributeError: 'NekoAdapter' object has no attribute '_handle_ws_task'`

- [ ] **Step 3: Implement _handle_ws_task and WS handler registration**

Add a new method `_register_ws_handlers` and `_handle_ws_task` to `NekoAdapter` in `bridge/neko-adapter.py`. Insert after the `__init__` method (before `_start_zmq`):

```python
    # ============================================================
    # WebSocket 消息处理（Pi/Claude → N.E.K.O）
    # ============================================================

    def _register_ws_handlers(self) -> None:
        """注册 WebSocket 消息处理器。"""
        # 处理来自 Pi/Claude 的任务
        self.client.on_task(self._handle_ws_task)

        # 处理来自 Pi/Claude 的消息
        self.client.on_type("message", self._handle_ws_message)

        # 处理握手
        self.client.on_topic("system:broadcast", self._handle_ws_system)

    async def _handle_ws_task(self, msg: Dict[str, Any]) -> None:
        """处理来自 Pi/Claude 的任务委托，注入到 N.E.K.O agent_server。

        WS task → ZMQ analyze_request (PUSH to ANALYZE_PUSH)
        """
        topic = msg.get("topic", "")
        from_agent = msg.get("from", "unknown")
        task_content = msg.get("payload", "")
        original_id = msg.get("id", "")

        logger.info(
            "[NekoAdapter] Task received: from=%s topic=%s content=%.100s",
            from_agent, topic, task_content,
        )

        # 只在 target 为 neko 时处理
        to_agent = msg.get("to", "")
        if to_agent not in ("neko", "*"):
            return

        # 构建 analyze_request（遵循 N.E.K.O 协议）
        event_id = str(uuid.uuid4())
        analyze_request = {
            "event_type": "analyze_request",
            "event_id": event_id,
            "trigger": f"{from_agent}_task",
            "lanlan_name": from_agent,
            "messages": [
                {
                    "role": "user",
                    "content": str(task_content),
                }
            ],
            "source_agent": from_agent,
            "source_id": original_id,
        }

        # PUSH 到 N.E.K.O agent_server
        if self.zmq_push is not None and self._zmq_ready:
            try:
                self.zmq_push.send(orjson.dumps(analyze_request), zmq.NOBLOCK)
                logger.info(
                    "[NekoAdapter] Task injected: event_id=%s source=%s",
                    event_id, from_agent,
                )
            except Exception as e:
                logger.warning("[NekoAdapter] Failed to inject task: %s", e)
                # 通知发送方失败
                await self.client.send_result(
                    from_agent,
                    f"Error: Failed to inject task to N.E.K.O: {e}",
                    original_id,
                )
        else:
            logger.warning("[NekoAdapter] ZMQ not ready, task not injected")
            await self.client.send_result(
                from_agent,
                "Error: N.E.K.O agent not available",
                original_id,
            )

    async def _handle_ws_message(self, msg: Dict[str, Any]) -> None:
        """处理来自 Pi/Claude 的普通消息。"""
        topic = msg.get("topic", "")
        from_agent = msg.get("from", "")

        # 只记录非 neko 目标的消息
        if from_agent == "neko":
            return  # 不处理自己发出的消息

        logger.debug(
            "[NekoAdapter] WS message: from=%s topic=%s payload=%.100s",
            from_agent, topic, str(msg.get("payload", ""))[:100],
        )

    async def _handle_ws_system(self, msg: Dict[str, Any]) -> None:
        """处理系统广播（agent 上线/下线通知），更新路由表。"""
        payload = msg.get("payload", {})
        if isinstance(payload, str):
            try:
                import json
                payload = json.loads(payload)
            except Exception:
                return

        event = payload.get("event", "")
        agent = payload.get("agent", "")
        session = payload.get("sessionId", "")

        if not agent:
            return

        if event == "agent_connected":
            self.agents[agent] = {
                "status": "online",
                "session": session,
                "connected_at": time.time(),
            }
            logger.info("[NekoAdapter] Agent online: %s", agent)
        elif event == "agent_disconnected":
            if agent in self.agents:
                self.agents[agent]["status"] = "offline"
            logger.info("[NekoAdapter] Agent offline: %s", agent)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py::test_ws_task_to_zmq_analyze_request -v
```

Expected: PASS

- [ ] **Step 5: Run all tests to check no regressions**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py -v
```

Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/neko-adapter.py tests/test_neko_adapter.py && git commit -m "feat: add WS task → ZMQ analyze_request injection with test"
```

---

### Task 5: Adapter Lifecycle — Start, Stop, and Error Handling

**Files:**
- Modify: `F:/Agent/agent-interop/bridge/neko-adapter.py` (add `start()`, `stop()`, reconnect, graceful shutdown)
- Modify: `F:/Agent/agent-interop/tests/test_neko_adapter.py` (add lifecycle tests)

**Interfaces:**
- Consumes: All previous methods
- Produces: `NekoAdapter.start()`, `NekoAdapter.stop()`, `NekoAdapter.run()`

- [ ] **Step 1: Write failing lifecycle test**

Append to `F:/Agent/agent-interop/tests/test_neko_adapter.py`:

```python
def test_adapter_start_and_stop():
    """Adapter.start() and .stop() lifecycle without actual WS connection."""
    import asyncio

    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test-lifecycle",
    )

    # Verify handlers are registered
    assert len(adapter.client.handlers.get("type:task", [])) == 1
    assert len(adapter.client.handlers.get("type:message", [])) == 1

    # Verify ZMQ starts and stops cleanly
    adapter._start_zmq()
    assert adapter._zmq_ready is True
    assert adapter._zmq_thread is not None
    assert adapter._zmq_thread.is_alive()

    adapter._stop_zmq()
    # After stop, thread should be joined and not alive
    adapter._zmq_thread.join(timeout=1.0)
    assert adapter._zmq_ready is False


def test_error_handling_invalid_zmq_message():
    """Adapter handles malformed ZMQ messages without crashing."""
    import asyncio

    async def run_test():
        adapter = NekoAdapter(
            bus_url="ws://127.0.0.1:48999",
            zmq_pub_port=48990,
            zmq_analyze_port=48991,
            session_id="test",
        )

        # Send a malformed event (missing event_type)
        event = {"bad": "data", "no_event_type": True}
        # This should not raise
        await adapter._handle_zmq_event(event)

        # Send None-like event
        await adapter._handle_zmq_event({})

        # No exception means success
        assert True

    asyncio.run(run_test())
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py -k "lifecycle or error_handling" -v
```

Expected: FAIL — `test_adapter_start_and_stop` should fail because `client.handlers` won't have the registered handlers yet without `_register_ws_handlers()` being called in `__init__`

- [ ] **Step 3: Implement lifecycle methods**

Add to `NekoAdapter.__init__` in `bridge/neko-adapter.py`, at the end of the method after `self.subscribers: Dict[str, List[Callable]] = {}`:

```python
        # Register WebSocket handlers
        self._register_ws_handlers()
```

Now add `start()`, `stop()`, and `run()` methods to `NekoAdapter` class. Insert after `_zmq_recv_loop`:

```python
    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        """启动适配器：连接 ZMQ + WebSocket。"""
        self._loop = asyncio.get_running_loop()
        self._start_zmq()

        # 连接 WebSocket（带重试）
        retry_delay = 1.0
        max_delay = 15.0
        while not self.client.connected:
            try:
                await self.client.connect()
                logger.info("[NekoAdapter] WebSocket connected as neko@%s", self.session_id)
            except Exception as e:
                logger.warning(
                    "[NekoAdapter] WebSocket connection failed: %s (retry in %.1fs)",
                    e, retry_delay,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)

        # 广播上线
        await self._broadcast_status("online")

        # 启动心跳
        asyncio.create_task(self._heartbeat_loop())

        logger.info("[NekoAdapter] Adapter started: session=%s", self.session_id)

    async def stop(self) -> None:
        """优雅关闭适配器。"""
        logger.info("[NekoAdapter] Shutting down...")

        # 广播下线
        if self.client.connected:
            try:
                await self._broadcast_status("offline")
            except Exception:
                pass

        # 停止接收新任务、等待进行中的任务
        self._zmq_stop.set()

        # 断开 WebSocket
        if self.client.connected:
            try:
                await asyncio.wait_for(self.client.disconnect(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("[NekoAdapter] WS disconnect timed out")

        # 关闭 ZMQ
        self._stop_zmq()

        logger.info("[NekoAdapter] Adapter stopped")

    async def _broadcast_status(self, status: str) -> None:
        """广播 N.E.K.O adapter 状态到所有 Agent。"""
        if not self.client.connected:
            return
        await self.client.send_message(
            "bus",
            json.dumps({
                "agent": "neko",
                "status": status,
                "session": self.session_id,
                "capabilities": ["browser_use", "computer_use", "openclaw", "openfang"],
            }),
        )

    async def _heartbeat_loop(self) -> None:
        """每 30 秒发送一次心跳。"""
        while self.client.connected:
            await asyncio.sleep(30)
            try:
                await self.client.heartbeat()
            except Exception as e:
                logger.debug("[NekoAdapter] Heartbeat failed: %s", e)
                break

    async def run(self) -> None:
        """主入口：启动后保持运行直到收到停止信号。"""
        await self.start()
        try:
            # 保持运行
            stop_event = asyncio.Event()
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()
```

We also need `import json` at the top of the file. Add after the other imports:

```python
import json
```

- [ ] **Step 4: Run lifecycle tests**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py -k "lifecycle or error_handling" -v
```

Expected: PASS (2 tests)

- [ ] **Step 5: Run all tests**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py -v
```

Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/neko-adapter.py tests/test_neko_adapter.py && git commit -m "feat: add adapter lifecycle (start/stop/heartbeat) with tests"
```

---

### Task 6: CLI Entry Point and Integration Test

**Files:**
- Modify: `F:/Agent/agent-interop/bridge/neko-adapter.py` (add `main()` and `__main__` block)
- Create: `F:/Agent/agent-interop/tests/test_neko_adapter_cli.py`

**Interfaces:**
- Consumes: Full `NekoAdapter` class
- Produces: `python bridge/neko-adapter.py --help` and `python bridge/neko-adapter.py` executable

- [ ] **Step 1: Write CLI integration test**

Create `F:/Agent/agent-interop/tests/test_neko_adapter_cli.py`:

```python
"""CLI integration test for neko-adapter.py."""
import subprocess
import sys
import os


def test_cli_help():
    """--help prints usage and exits cleanly."""
    script = os.path.join(os.path.dirname(__file__), "..", "bridge", "neko-adapter.py")
    result = subprocess.run(
        [sys.executable, script, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--bus-url" in result.stdout
    assert "--session" in result.stdout


def test_cli_defaults_smoke():
    """Adapter with --dry-run flag prints config and exits."""
    script = os.path.join(os.path.dirname(__file__), "..", "bridge", "neko-adapter.py")
    result = subprocess.run(
        [sys.executable, script, "--dry-run"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "session=" in result.stdout
    assert "bus_url=" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter_cli.py -v
```

Expected: FAIL (no `--help` / `__main__` block yet)

- [ ] **Step 3: Add CLI entry point**

Add to the end of `F:/Agent/agent-interop/bridge/neko-adapter.py`:

```python
# ============================================================
# CLI 入口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="N.E.K.O Adapter — ZMQ ↔ WebSocket bridge for agent-interop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bridge/neko-adapter.py
  python bridge/neko-adapter.py --bus-url ws://localhost:48970 --session neko-main
  python bridge/neko-adapter.py --log-level DEBUG
  python bridge/neko-adapter.py --dry-run
        """,
    )
    parser.add_argument(
        "--bus-url",
        default=DEFAULT_BUS_URL,
        help=f"Interop WebSocket URL (default: {DEFAULT_BUS_URL})",
    )
    parser.add_argument(
        "--zmq-pub-port",
        type=int,
        default=DEFAULT_SESSION_PUB_PORT,
        help=f"N.E.K.O ZMQ SESSION_PUB port (default: {DEFAULT_SESSION_PUB_PORT})",
    )
    parser.add_argument(
        "--zmq-analyze-port",
        type=int,
        default=DEFAULT_ANALYZE_PUSH_PORT,
        help=f"N.E.K.O ZMQ ANALYZE_PUSH port (default: {DEFAULT_ANALYZE_PUSH_PORT})",
    )
    parser.add_argument(
        "--session",
        default="",
        help="Session ID for this adapter instance",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print configuration and exit without connecting",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.dry_run:
        print(f"[NekoAdapter] Dry run — configuration:")
        print(f"  bus_url={args.bus_url}")
        print(f"  zmq_pub_port={args.zmq_pub_port}")
        print(f"  zmq_analyze_port={args.zmq_analyze_port}")
        print(f"  session={args.session or '<auto>'}")
        print(f"  log_level={args.log_level}")
        return

    # Create and run adapter
    adapter = NekoAdapter(
        bus_url=args.bus_url,
        zmq_pub_port=args.zmq_pub_port,
        zmq_analyze_port=args.zmq_analyze_port,
        session_id=args.session,
    )

    try:
        asyncio.run(adapter.run())
    except KeyboardInterrupt:
        print("\n[NekoAdapter] Interrupted, shutting down...")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run CLI tests**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter_cli.py -v
```

Expected: PASS (2 tests)

- [ ] **Step 5: Manual smoke test — verify help works**

```bash
cd F:/Agent/agent-interop && python bridge/neko-adapter.py --help
```

Expected: prints help with all arguments

- [ ] **Step 6: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/neko-adapter.py tests/test_neko_adapter_cli.py && git commit -m "feat: add CLI entry point with --help, --dry-run, and argparse"
```

---

### Task 7: Subscribe Pattern and Agent Routing Table

**Files:**
- Modify: `F:/Agent/agent-interop/bridge/neko-adapter.py` (add `subscribe()` method)
- Modify: `F:/Agent/agent-interop/tests/test_neko_adapter.py` (add subscribe/routing tests)

**Interfaces:**
- Consumes: `NekoAdapter._handle_zmq_event` subscriber dispatch
- Produces: `NekoAdapter.subscribe(event_type, handler)`, `NekoAdapter.get_agents()`

- [ ] **Step 1: Write failing tests**

Append to `F:/Agent/agent-interop/tests/test_neko_adapter.py`:

```python
def test_subscribe_receives_events():
    """subscribe() registers a handler that receives ZMQ events."""
    import asyncio

    received_events = []

    async def run_test():
        adapter = NekoAdapter(
            bus_url="ws://127.0.0.1:48999",
            zmq_pub_port=48990,
            zmq_analyze_port=48991,
            session_id="test",
        )

        # Register subscriber
        def my_handler(event):
            received_events.append(event)

        adapter.subscribe("voice_transcript_observed", my_handler)

        # Simulate receiving a ZMQ event
        zmq_event = {
            "event_type": "voice_transcript_observed",
            "lanlan_name": "test",
            "transcript": "hello world",
        }
        await adapter._handle_zmq_event(zmq_event)

        assert len(received_events) == 1
        assert received_events[0]["transcript"] == "hello world"

        # A different event_type should NOT trigger the handler
        zmq_event2 = {"event_type": "other_event", "data": "x"}
        await adapter._handle_zmq_event(zmq_event2)

        assert len(received_events) == 1  # Still 1, handler not called

    asyncio.run(run_test())


def test_get_agents_routing_table():
    """get_agents() returns current routing table."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    # Initially empty
    agents = adapter.get_agents()
    assert isinstance(agents, dict)

    # Simulate agent connecting
    adapter.agents["pi"] = {"status": "online", "session": "pi-main"}
    adapter.agents["claude"] = {"status": "online", "session": "claude-main"}

    agents = adapter.get_agents()
    assert "pi" in agents
    assert "claude" in agents
    assert agents["pi"]["status"] == "online"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py -k "subscribe or get_agents" -v
```

Expected: FAIL — `AttributeError: 'NekoAdapter' object has no attribute 'subscribe'` or `'get_agents'`

- [ ] **Step 3: Implement subscribe() and get_agents()**

Add to `NekoAdapter` class in `bridge/neko-adapter.py`, after `_register_ws_handlers`:

```python
    def subscribe(self, event_type: str, handler: Callable) -> None:
        """注册对指定 N.E.K.O 事件类型的订阅。

        借鉴 N.E.K.O register_user_utterance_sink 模式。
        当 ZMQ 收到对应 event_type 时，handler 会被调用。

        Args:
            event_type: N.E.K.O 事件类型 (如 "voice_transcript_observed")
            handler: 回调函数，签名为 handler(event: dict) -> None
        """
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        if handler not in self.subscribers[event_type]:
            self.subscribers[event_type].append(handler)
        logger.debug("[NekoAdapter] Subscribed to %s", event_type)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        """取消订阅。"""
        if event_type in self.subscribers and handler in self.subscribers[event_type]:
            self.subscribers[event_type].remove(handler)
            logger.debug("[NekoAdapter] Unsubscribed from %s", event_type)

    def get_agents(self) -> Dict[str, Dict[str, Any]]:
        """获取当前多 Agent 路由表。

        Returns:
            {agent_name: {status, session, connected_at, ...}, ...}
        """
        return dict(self.agents)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/test_neko_adapter.py -k "subscribe or get_agents" -v
```

Expected: PASS (2 tests)

- [ ] **Step 5: Run all tests**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/ -v
```

Expected: PASS (all tests: 7 + 2 + 2 = 11 tests)

- [ ] **Step 6: Commit**

```bash
cd F:/Agent/agent-interop && git add bridge/neko-adapter.py tests/test_neko_adapter.py && git commit -m "feat: add subscribe/unsubscribe pattern and agent routing table with tests"
```

---

### Task 8: Update README and Final Verification

**Files:**
- Modify: `F:/Agent/agent-interop/README.md`

- [ ] **Step 1: Update README with N.E.K.O adapter section**

In `F:/Agent/agent-interop/README.md`, after the "使用" section and before "Plastic Promise 近期更新", add:

```markdown
## N.E.K.O 桥接 (Phase 1)

N.E.K.O Adapter 将 N.E.K.O 接入 agent-interop 三方互通体系。

### 架构

```
N.E.K.O ZMQ (48961/48963) ←→ neko-adapter.py ←→ Interop WebSocket (48970) ←→ Pi / Claude
```

### 启动

```bash
# 终端 1 — 事件总线
npx tsx bridge/event-bus.ts

# 终端 2 — N.E.K.O 适配器
pip install pyzmq orjson   # 首次安装依赖
python bridge/neko-adapter.py

# 终端 3 — N.E.K.O（正常启动）
# 终端 4 — Pi 或 Claude Code 连接 WebSocket
```

### 配置

```bash
# 环境变量（可选，有默认值）
INTEROP_BUS_URL=ws://127.0.0.1:48970
NEKO_ZMQ_SESSION_PUB_PORT=48961
NEKO_ZMQ_ANALYZE_PUSH_PORT=48963
```

### 消息流

| 方向 | 事件 |
|------|------|
| N.E.K.O → Pi/Claude | `neko:announce` (上下线), `neko:voice` (语音), `neko:result` (任务结果) |
| Pi/Claude → N.E.K.O | `pi:neko` / `claude:neko` (type=task → analyze_request) |

### 测试

```bash
python -m pytest tests/ -v
```
```

- [ ] **Step 2: Run all tests one final time**

```bash
cd F:/Agent/agent-interop && python -m pytest tests/ -v
```

Expected: all 11 tests PASS

- [ ] **Step 3: Verify dry-run works end-to-end**

```bash
cd F:/Agent/agent-interop && python bridge/neko-adapter.py --dry-run --session test-final
```

Expected: prints configuration and exits cleanly

- [ ] **Step 4: Commit**

```bash
cd F:/Agent/agent-interop && git add README.md && git commit -m "docs: add N.E.K.O bridge adapter usage to README"
```

---

### Final Verification Checklist

After all tasks complete, verify:

- [ ] `python -m pytest tests/ -v` — all 11 tests pass
- [ ] `python bridge/neko-adapter.py --help` — prints help
- [ ] `python bridge/neko-adapter.py --dry-run` — prints config
- [ ] `python -c "from bridge.neko_adapter import NekoAdapter; print('import OK')"` — module importable
- [ ] `git log --oneline` shows 8 clean commits
