"""
N.E.K.O Adapter -- ZMQ <-> WebSocket 桥接器

连接 N.E.K.O 的 ZMQ 事件总线和 Interop 的 WebSocket 事件总线，
实现 Pi ↔ Claude ↔ N.E.K.O 三方实时通信。

借鉴 N.E.K.O neko_event_bus.py (ZMQ PUB/SUB + PUSH/PULL) 和
cross_server.py (跨服务器转发) 模式。

启动: python bridge/neko-adapter.py
"""

import asyncio
import json
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

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger("NekoAdapter")

# ============================================================
# 默认端口（与 N.E.K.O neko_event_bus.py 保持一致）
# ============================================================

DEFAULT_SESSION_PUB_PORT = int(os.environ.get("NEKO_ZMQ_SESSION_PUB_PORT", "48961"))
DEFAULT_ANALYZE_PUSH_PORT = int(os.environ.get("NEKO_ZMQ_ANALYZE_PUSH_PORT", "48963"))
DEFAULT_BUS_URL = os.environ.get("INTEROP_BUS_URL", "ws://127.0.0.1:48970")

# ============================================================
# 遥测
# ============================================================

TELEMETRY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".interop", "telemetry")
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
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


class WSSlot:
    """WebSocket 槽位管理器。

    借鉴 N.E.K.O cross_server.py _WSSlot 模式:
      - dead_event: 断线信号，唤醒 maintainer
      - maintain: 事件驱动重连 + 指数退避
      - mark_dead: 优雅断线标记
    """
    __slots__ = ("name", "url", "lanlan_name",
                 "ws", "reader", "maintainer", "dead_event")

    def __init__(self, name: str, url: str, lanlan_name: str = ""):
        self.name = name
        self.url = url
        self.lanlan_name = lanlan_name
        self.ws = None
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
        if websockets is None:
            logger.warning("[WSSlot] websockets not installed, cannot maintain")
            return

        backoff = backoff_min
        while True:
            await self.dead_event.wait()

            # 清理旧 reader
            old_reader = self.reader
            self.reader = None
            if old_reader is not None:
                old_reader.cancel()

            cycle_start = time.monotonic()
            try:
                self.ws = await asyncio.wait_for(
                    websockets.connect(self.url),
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
                elapsed = time.monotonic() - cycle_start
                if elapsed < backoff:
                    await asyncio.sleep(backoff - elapsed)
                backoff = min(backoff * 2, backoff_max)

    async def _reader_loop(self) -> None:
        """读取循环：检测断线时调用 mark_dead。"""
        try:
            while self.ws is not None:
                try:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=30)
                    # 消息由上层处理，这里只检测断线
                    logger.debug("[WSSlot] %s received message", self.name)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
                except asyncio.CancelledError:
                    return
        except Exception:
            pass
        if self.ws is not None:
            self.mark_dead()


class DedupCache:
    """消息去重缓存。

    用 OrderedDict 维护最近 N 条消息 ID，O(1) 查重，按插入顺序淘汰。
    超过 max_size 时丢弃最早的一半。
    """

    def __init__(self, max_size: int = 500):
        self._max_size = max_size
        self._seen: dict[str, bool] = {}  # Ordered since Python 3.7

    def is_duplicate(self, msg_id: str) -> bool:
        """检查并记录消息 ID。首次出现返回 False，重复返回 True。"""
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = True
        if len(self._seen) > self._max_size:
            # 丢弃最早的一半
            keys = list(self._seen.keys())
            keep_count = self._max_size // 2
            to_remove = keys[:-keep_count]
            for k in to_remove:
                del self._seen[k]
        return False


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

        # Dedup
        self._dedup = DedupCache(max_size=500)

        # Activity tracking
        self._current_activity = {"status": "idle"}

        # Register WebSocket handlers
        self._register_ws_handlers()

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
                _log_telemetry("task_delegated", from_agent)
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
                    self._on_zmq_event(event)
            except zmq.Again:
                continue
            except Exception as e:
                if not self._zmq_stop.is_set():
                    logger.debug("[NekoAdapter] ZMQ recv error: %s", e)
                    time.sleep(0.05)

    def _on_zmq_event(self, event: Dict[str, Any]) -> None:
        """同步 ZMQ 事件入口。有事件循环时派发到 asyncio，否则直接回调。"""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._handle_zmq_event(event), self._loop
            )
        else:
            # 无事件循环时同步调用（用于测试）
            self._handle_zmq_event_sync(event)

    def _handle_zmq_event_sync(self, event: Dict[str, Any]) -> None:
        """同步事件处理（测试用）。可被子类或测试覆盖。"""
        pass

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """注册对指定 N.E.K.O 事件类型的订阅。

        借鉴 N.E.K.O register_user_utterance_sink 模式。
        当 ZMQ 收到对应 event_type 时，handler 会被调用。
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
        """获取当前多 Agent 路由表。"""
        return dict(self.agents)

    def set_activity(self, status: str, task: str = "", elapsed: float = 0.0) -> None:
        """设置当前活动状态，随心跳广播。"""
        self._current_activity = {
            "status": status,
            "task": task,
            "elapsed": elapsed,
        }

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
                "activity": self._current_activity,
            }),
        )

    async def _heartbeat_loop(self) -> None:
        """心跳循环，借鉴 N.E.K.O activity_guess_gate 退避模式。

        HEARTBEAT_BASE=30s, MULTIPLIER=4.0, CAP=900s。
        活跃变化时重置到 BASE，持续稳定时退避到 CAP。
        """
        base = float(os.environ.get("HEARTBEAT_BASE_SECONDS", "30"))
        multiplier = float(os.environ.get("HEARTBEAT_MULTIPLIER", "4.0"))
        cap = float(os.environ.get("HEARTBEAT_CAP_SECONDS", "900"))
        if multiplier <= 1.0:
            logger.warning("[NekoAdapter] HEARTBEAT_MULTIPLIER must be > 1, using 4.0")
            multiplier = 4.0

        interval = base
        last_activity = dict(self._current_activity)

        while True:
            await asyncio.sleep(interval)

            # 断连抑制：无人监听时跳过心跳
            if not self.client.connected:
                continue

            # 检查活动是否变化
            current = dict(self._current_activity)
            if current != last_activity:
                interval = base  # 活动变化，重置退避
                last_activity = current
            elif interval < cap:
                interval = min(interval * multiplier, cap)

            try:
                await self.client.heartbeat()
                logger.debug("[NekoAdapter] Heartbeat (interval=%.0fs)", interval)
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

    def _translate_zmq_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """将 N.E.K.O ZMQ 事件翻译为 Interop WebSocket 消息。

        返回 None 表示此事件不需要转发。
        """
        # Dedup: skip if we've seen this event_id before
        event_id = event.get("event_id", "")
        if event_id and self._dedup.is_duplicate(event_id):
            return None

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


# ============================================================
# CLI 入口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="N.E.K.O Adapter -- ZMQ <-> WebSocket bridge for agent-interop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bridge/neko_adapter.py
  python bridge/neko_adapter.py --bus-url ws://localhost:48970 --session neko-main
  python bridge/neko_adapter.py --log-level DEBUG
  python bridge/neko_adapter.py --dry-run
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
