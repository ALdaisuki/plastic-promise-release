/**
 * Interop Event Bus — WebSocket 事件总线 (替代文件邮箱)
 *
 * 借鉴 N.E.K.O agent_event_bus (ZMQ PUB/SUB) 和 outbox 模式，
 * 使用 WebSocket 实现 Pi ↔ Claude ↔ N.E.K.O 实时消息。
 *
 * 启动: npx tsx bridge/event-bus.ts
 * 端口: ws://127.0.0.1:48970
 */

import { WebSocketServer, WebSocket } from "ws";
import { randomUUID } from "node:crypto";

const PORT = 48970;

// ============================================================
// 消息类型
// ============================================================

type Topic =
  | "pi:claude"
  | "claude:pi"
  | "neko:pi"
  | "neko:claude"
  | "pi:neko"
  | "claude:neko"
  | "memory:sync"
  | "memory:changed"
  | "heartbeat"
  | "system:broadcast";

interface BusMessage {
  id: string;
  topic: Topic;
  from: string;
  to: string;
  type: "message" | "task" | "result" | "handshake" | "memory_event" | "heartbeat";
  payload: string;
  timestamp: number;
  replyTo?: string;
}

interface ClientInfo {
  id: string;
  agent: "pi" | "claude" | "neko" | "system";
  ws: WebSocket;
  sessionId: string;
  connectedAt: number;
}

// ============================================================
// 事件总线
// ============================================================

const clients = new Map<string, ClientInfo>();
const messageLog: BusMessage[] = [];
const MAX_LOG = 1000;

const wss = new WebSocketServer({ port: PORT, host: "127.0.0.1" });

wss.on("listening", () => {
  console.log(`[EventBus] WebSocket server on ws://127.0.0.1:${PORT}`);
  console.log("[EventBus] Topics: pi:claude | claude:pi | neko:* | memory:sync | heartbeat");
  console.log("[EventBus] Agents connected: 0");
});

wss.on("connection", (ws, req) => {
  // 从查询参数获取 agent 身份
  const url = new URL(req.url || "/", `http://127.0.0.1:${PORT}`);
  const agent = (url.searchParams.get("agent") || "unknown") as ClientInfo["agent"];
  const sessionId = url.searchParams.get("session") || "default";

  const clientId = randomUUID();
  const client: ClientInfo = {
    id: clientId,
    agent,
    ws,
    sessionId,
    connectedAt: Date.now(),
  };

  clients.set(clientId, client);
  log(`Agent connected: ${agent} (${clientId.slice(0, 8)}) [total: ${clients.size}]`);

  // 发送欢迎消息
  sendTo(ws, {
    id: randomUUID(),
    topic: "system:broadcast",
    from: "bus",
    to: clientId,
    type: "handshake",
    payload: JSON.stringify({
      welcome: "Interop Event Bus v2.0",
      busId: "interop-bus",
      connectedAgents: listAgents(),
    }),
    timestamp: Date.now(),
  });

  // 广播连接事件
  broadcast({
    id: randomUUID(),
    topic: "system:broadcast",
    from: "bus",
    to: "*",
    type: "handshake",
    payload: JSON.stringify({ event: "agent_connected", agent, sessionId }),
    timestamp: Date.now(),
  }, clientId);

  ws.on("message", (raw) => {
    try {
      const msg: BusMessage = JSON.parse(raw.toString());

      // 记录消息
      messageLog.push(msg);
      if (messageLog.length > MAX_LOG) messageLog.shift();

      // 路由消息
      route(msg, clientId);
    } catch (err) {
      log(`Invalid message from ${clientId.slice(0, 8)}: ${err}`);
    }
  });

  ws.on("close", () => {
    clients.delete(clientId);
    log(`Agent disconnected: ${agent} (${clientId.slice(0, 8)}) [total: ${clients.size}]`);
    broadcast({
      id: randomUUID(),
      topic: "system:broadcast",
      from: "bus",
      to: "*",
      type: "handshake",
      payload: JSON.stringify({ event: "agent_disconnected", agent, sessionId }),
      timestamp: Date.now(),
    });
  });

  ws.on("error", (err) => {
    log(`WebSocket error ${clientId.slice(0, 8)}: ${err.message}`);
  });
});

// ============================================================
// 消息路由
// ============================================================

function route(msg: BusMessage, fromClientId: string): void {
  const from = clients.get(fromClientId);
  const fromAgent = from?.agent || "unknown";

  log(`Route: ${fromAgent} -> ${msg.topic} [${msg.type}]`);

  switch (msg.topic) {
    case "pi:claude":
      // Pi 发给 Claude
      forwardToAgent("claude", msg, fromClientId);
      break;

    case "claude:pi":
      // Claude 发给 Pi
      forwardToAgent("pi", msg, fromClientId);
      break;

    case "neko:pi":
      forwardToAgent("pi", msg, fromClientId);
      break;

    case "neko:claude":
      forwardToAgent("claude", msg, fromClientId);
      break;

    case "pi:neko":
    case "claude:neko":
      forwardToAgent("neko", msg, fromClientId);
      break;

    case "memory:sync":
    case "memory:changed":
      // 内存事件广播给所有 Agent
      broadcast(msg, fromClientId);
      break;

    case "heartbeat":
      // 心跳只发给系统
      forwardToAgent("system", msg, fromClientId);
      break;

    case "system:broadcast":
      broadcast(msg, fromClientId);
      break;

    default:
      log(`Unknown topic: ${msg.topic}`);
  }
}

function forwardToAgent(agent: ClientInfo["agent"], msg: BusMessage, excludeId?: string): void {
  let forwarded = 0;
  for (const [id, client] of clients) {
    if (id === excludeId) continue;
    if (client.agent === agent) {
      sendTo(client.ws, msg);
      forwarded++;
    }
  }
  if (forwarded === 0) {
    log(`No ${agent} agent connected, message queued`);
  }
}

function broadcast(msg: BusMessage, excludeId?: string): void {
  for (const [id, client] of clients) {
    if (id === excludeId) continue;
    sendTo(client.ws, msg);
  }
}

function sendTo(ws: WebSocket, msg: BusMessage): void {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

// ============================================================
// 工具方法
// ============================================================

function listAgents(): string[] {
  const agents: string[] = [];
  for (const client of clients.values()) {
    agents.push(`${client.agent}@${client.sessionId}`);
  }
  return agents;
}

function log(msg: string): void {
  const time = new Date().toISOString().split("T")[1].slice(0, 8);
  console.log(`[${time}] ${msg}`);
}

// 优雅关闭
process.on("SIGINT", () => {
  log("Shutting down event bus...");
  wss.close();
  process.exit(0);
});

process.on("SIGTERM", () => {
  log("Shutting down event bus...");
  wss.close();
  process.exit(0);
});
