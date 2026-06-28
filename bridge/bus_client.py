"""
Interop Bus Client — Python WebSocket 客户端

Claude Code 和 N.E.K.O 通过此客户端连接到 Interop Event Bus。
支持: 发送消息 / 接收消息 / 委派任务 / 心跳

借鉴 N.E.K.O agent_event_bus (ZMQ) 和 outbox 模式。
"""

import asyncio
import json
import os
import sys
import time
import uuid
from typing import Callable, Optional, Dict, Any

try:
    import websockets
except ImportError:
    print("[InteropClient] websockets not installed: pip install websockets")
    sys.exit(1)

BUS_URL = os.environ.get("INTEROP_BUS_URL", "ws://127.0.0.1:48970")
AGENT_TYPE = os.environ.get("INTEROP_AGENT", "claude")  # claude / neko
SESSION_ID = os.environ.get("INTEROP_SESSION", str(uuid.uuid4())[:8])


class InteropClient:
    """WebSocket 事件总线客户端"""

    def __init__(self, agent_type: str = "claude", session_id: str = ""):
        self.agent_type = agent_type
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.ws = None
        self.connected = False
        self.handlers: Dict[str, list[Callable]] = {}
        self._recv_task = None
        self.message_id_counter = 0

    # ============================================================
    # 连接管理
    # ============================================================

    async def connect(self):
        """连接到事件总线"""
        url = f"{BUS_URL}?agent={self.agent_type}&session={self.session_id}"
        try:
            self.ws = await websockets.connect(url)
            self.connected = True
            self._recv_task = asyncio.create_task(self._recv_loop())
            print(f"[InteropClient] Connected as {self.agent_type}@{self.session_id} to {BUS_URL}")
        except Exception as e:
            print(f"[InteropClient] Connection failed: {e}")
            raise

    async def disconnect(self):
        """断开连接"""
        self.connected = False
        if self._recv_task:
            self._recv_task.cancel()
        if self.ws:
            await self.ws.close()
        print("[InteropClient] Disconnected")

    async def _recv_loop(self):
        """接收消息循环"""
        while self.connected:
            try:
                raw = await self.ws.recv()
                msg = json.loads(raw)
                await self._dispatch(msg)
            except websockets.ConnectionClosed:
                self.connected = False
                break
            except Exception as e:
                print(f"[InteropClient] Recv error: {e}")
                continue

    async def _dispatch(self, msg: dict):
        """分发消息到注册的处理器"""
        topic = msg.get("topic", "")
        msg_type = msg.get("type", "")

        # 通用处理器
        for handler in self.handlers.get("*", []):
            await self._safe_call(handler, msg)

        # Topic 处理器
        for handler in self.handlers.get(topic, []):
            await self._safe_call(handler, msg)

        # Type 处理器
        for handler in self.handlers.get(f"type:{msg_type}", []):
            await self._safe_call(handler, msg)

    async def _safe_call(self, handler, msg):
        try:
            result = handler(msg)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            print(f"[InteropClient] Handler error: {e}")

    # ============================================================
    # 消息发送
    # ============================================================

    async def send_message(self, to: str, content: str, reply_to: str = "") -> str:
        """发送普通消息"""
        msg_id = self._next_id()
        topic = f"{self.agent_type}:{to}"
        await self._send({
            "id": msg_id,
            "topic": topic,
            "from": self.agent_type,
            "to": to,
            "type": "message",
            "payload": content,
            "timestamp": int(time.time() * 1000),
            "replyTo": reply_to,
        })
        return msg_id

    async def delegate_task(self, to: str, task: str, timeout: float = 30.0) -> Optional[str]:
        """委派任务并等待结果"""
        msg_id = self._next_id()
        topic = f"{self.agent_type}:{to}"

        # 注册一次性响应处理器
        future = asyncio.Future()

        async def wait_reply(msg):
            if msg.get("replyTo") == msg_id and not future.done():
                future.set_result(msg.get("payload", ""))

        self.handlers.setdefault("result", []).append(wait_reply)

        await self._send({
            "id": msg_id,
            "topic": topic,
            "from": self.agent_type,
            "to": to,
            "type": "task",
            "payload": task,
            "timestamp": int(time.time() * 1000),
        })

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return None
        finally:
            if wait_reply in self.handlers.get("result", []):
                self.handlers["result"].remove(wait_reply)

    async def send_result(self, to: str, result: str, reply_to: str):
        """返回任务结果"""
        topic = f"{self.agent_type}:{to}"
        await self._send({
            "id": self._next_id(),
            "topic": topic,
            "from": self.agent_type,
            "to": to,
            "type": "result",
            "payload": result,
            "timestamp": int(time.time() * 1000),
            "replyTo": reply_to,
        })

    async def heartbeat(self):
        """发送心跳"""
        await self._send({
            "id": self._next_id(),
            "topic": "heartbeat",
            "from": self.agent_type,
            "to": "bus",
            "type": "heartbeat",
            "payload": "alive",
            "timestamp": int(time.time() * 1000),
        })

    async def _send(self, msg: dict):
        """底层发送"""
        if self.ws and self.connected:
            await self.ws.send(json.dumps(msg, ensure_ascii=False))

    def _next_id(self) -> str:
        self.message_id_counter += 1
        return f"{self.agent_type}-{int(time.time()*1000)}-{self.message_id_counter}"

    # ============================================================
    # 事件注册
    # ============================================================

    def on_message(self, handler: Callable):
        """收到任何消息时调用"""
        self.handlers.setdefault("*", []).append(handler)

    def on_topic(self, topic: str, handler: Callable):
        """收到指定 topic 时调用"""
        self.handlers.setdefault(topic, []).append(handler)

    def on_type(self, msg_type: str, handler: Callable):
        """收到指定 type 时调用"""
        self.handlers.setdefault(f"type:{msg_type}", []).append(handler)

    def on_task(self, handler: Callable):
        """收到 task 类型消息时调用"""
        self.on_type("task", handler)

    def on_result(self, handler: Callable):
        """收到 result 类型消息时调用"""
        self.on_type("result", handler)


# ============================================================
# Claude Code 集成示例
# ============================================================

async def claude_agent_loop():
    """Claude Code 作为 Agent 连接到事件总线"""
    client = InteropClient(agent_type="claude", session_id="claude-main")

    # 处理来自 Pi 的任务
    async def handle_task(msg):
        print(f"\n[Claude] Received task from {msg['from']}: {msg['payload'][:200]}")
        # TODO: 这里执行任务
        result = f"Task completed: {msg['payload'][:50]}..."
        await client.send_result(msg["from"], result, msg["id"])

    client.on_task(handle_task)

    # 处理来自 Pi 的消息
    async def handle_message(msg):
        print(f"\n[Claude] Message from {msg['from']}: {msg['payload'][:200]}")

    client.on_type("message", handle_message)

    await client.connect()

    # 发送握手
    await client.send_message("pi", f"Claude Code connected, session: {client.session_id}")

    # 心跳
    async def heartbeat_loop():
        while client.connected:
            await asyncio.sleep(30)
            await client.heartbeat()

    asyncio.create_task(heartbeat_loop())

    # 保持连接
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await client.disconnect()


# ============================================================
# N.E.K.O 桥接入口
# ============================================================

async def neko_bridge_loop():
    """
    N.E.K.O 桥接到 Interop Event Bus。

    N.E.K.O 本身使用 ZMQ event bus，此模块作为适配器：
    - 连接到 Interop WebSocket
    - 将 Pi/Claude 消息转发到 N.E.K.O 的 ZMQ 通道
    - 将 N.E.K.O 的 ZMQ 消息转发到 Interop WebSocket
    """
    client = InteropClient(agent_type="neko", session_id="neko-bridge")

    # 处理来自 Pi/Claude 的消息
    async def handle_all(msg):
        topic = msg.get("topic", "")
        if topic in ("pi:neko", "claude:neko"):
            print(f"[NekoBridge] Forwarding to N.E.K.O ZMQ: {msg['id']}")
            # TODO: 转发到 N.E.K.O 的 ZMQ SESSION_PUB
            # zmq_pub.send_json({"type": "external_agent_message", **msg})

    client.on_message(handle_all)

    await client.connect()
    print(f"[NekoBridge] N.E.K.O bridge active on {BUS_URL}")
    await client.send_message("bus", f"N.E.K.O bridge connected, session: {client.session_id}")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await client.disconnect()


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Interop Bus Client")
    parser.add_argument("--agent", default="claude", choices=["claude", "neko", "test"])
    parser.add_argument("--session", default="")
    parser.add_argument("--url", default=BUS_URL)
    args = parser.parse_args()

    if args.url:
        BUS_URL = args.url

    if args.agent == "neko":
        asyncio.run(neko_bridge_loop())
    elif args.agent == "test":
        async def test():
            client = InteropClient(agent_type="test", session_id="test")
            await client.connect()
            await client.send_message("bus", "Hello from test client!")
            await asyncio.sleep(2)
            await client.disconnect()
        asyncio.run(test())
    else:
        asyncio.run(claude_agent_loop())
