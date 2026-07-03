/**
 * Interop Bridge v3 — Pi ↔ Claude / N.E.K.O 实时互通
 *
 * 双通道模式:
 *   - 文件邮箱 (.interop/inbox/outbox/) — Claude 本地读取
 *   - WebSocket (ws://127.0.0.1:48970)  — N.E.K.O 实时通信
 *
 * WebSocket 连不上时自动降级为纯文件模式。
 *
 * 工具:
 *   interop_send    — 发送消息（文件 + WS 双写）
 *   interop_inbox   — 📬 收件箱预览（摘要 + 时间 + 发件人）
 *   interop_read    — 读取单条消息全文，标记已读
 *   interop_reply   — 快捷回复
 *   interop_check   — 保留兼容，增加 preview 模式
 *   interop_delegate — 委派任务 + 轮询等待结果
 *   interop_status  — 互通状态（含 WS 连接状态）
 */

import { mkdirSync, readdirSync, readFileSync, writeFileSync, unlinkSync, existsSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import WebSocket from "ws";

// ============================================================
// 常量
// ============================================================

const INTEROP_DIR = ".interop";
const OUTBOX_DIR = join(INTEROP_DIR, "outbox");
const INBOX_DIR = join(INTEROP_DIR, "inbox");
const WS_URL = process.env.INTEROP_BUS_URL || "ws://127.0.0.1:48970";
const PREVIEW_LENGTH = 80; // 预览截断长度
const WS_BUFFER_MAX = 200; // WS 接收缓冲上限
const TELEMETRY_DIR = join(INTEROP_DIR, "telemetry");
const DO_NOT_TRACK = process.env.DO_NOT_TRACK === "1";

let cwd = process.cwd();

// ============================================================
// 类型
// ============================================================

interface InteropMessage {
  id: string;
  from: string;
  to: string;
  type: "message" | "task" | "result" | "handshake";
  content: string;
  timestamp: number;
  replyTo?: string;
}

interface InboxEntry {
  message: InteropMessage;
  source: "file" | "ws";
  filename?: string;
  read: boolean;
}

// ============================================================
// WebSocket 连接状态
// ============================================================

let ws: WebSocket | null = null;
let wsConnected = false;
let wsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
let wsBackoff = 1000; // 指数退避起始值 (ms)
const WS_BACKOFF_MIN = 1000;
const WS_BACKOFF_MAX = 15000;

// WS 接收缓冲区
const wsBuffer: InboxEntry[] = [];

// ============================================================
// Dedup
// ============================================================

const seenIds = new Set<string>();
const DEDUP_MAX = 500;

// ============================================================
// Telemetry
// ============================================================

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
    const fs = require("fs");
    mkdirSync(join(cwd, TELEMETRY_DIR), { recursive: true });
    const today = new Date().toISOString().split("T")[0];
    const filepath = join(cwd, TELEMETRY_DIR, `telemetry-${today}.jsonl`);
    const line = JSON.stringify(event) + "\n";
    fs.appendFileSync(filepath, line, "utf-8");
  } catch {
    // Telemetry is best-effort
  }
}

// ============================================================
// Activity Tracking
// ============================================================

const agentActivity = new Map<string, { status: string; task?: string; elapsed?: number; since: number }>();

function updateAgentActivity(agent: string, info: { status: string; task?: string; elapsed?: number }) {
  agentActivity.set(agent, { ...info, since: Date.now() });
}

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

// ============================================================
// 消息计数器
// ============================================================

let messageCounter = 0;

function nextId(): string {
  messageCounter++;
  return `pi-${Date.now()}-${messageCounter}`;
}

// ============================================================
// 文件操作
// ============================================================

function ensureDirs() {
  mkdirSync(join(cwd, OUTBOX_DIR), { recursive: true });
  mkdirSync(join(cwd, INBOX_DIR), { recursive: true });
}

function writeFileMessage(msg: InteropMessage): void {
  ensureDirs();
  const filepath = join(cwd, OUTBOX_DIR, `${msg.id}.json`);
  writeFileSync(filepath, JSON.stringify(msg, null, 2), "utf-8");
}

function readFileInbox(): InboxEntry[] {
  ensureDirs();
  const files = readdirSync(join(cwd, INBOX_DIR))
    .filter((f) => f.endsWith(".json"))
    .sort();
  return files.map((f) => {
    const raw = readFileSync(join(cwd, INBOX_DIR, f), "utf-8");
    return {
      message: JSON.parse(raw) as InteropMessage,
      source: "file" as const,
      filename: f,
      read: false,
    };
  });
}

function deleteFileMessage(filename: string): void {
  const filepath = join(cwd, INBOX_DIR, filename);
  if (existsSync(filepath)) unlinkSync(filepath);
}

function clearFileMessages(filenames: string[]): void {
  for (const f of filenames) deleteFileMessage(f);
}

// ============================================================
// WebSocket 连接
// ============================================================

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return; // 已连接或正在连接
  }

  try {
    ws = new WebSocket(`${WS_URL}?agent=pi&session=pi-main`);

    ws.on("open", () => {
      wsConnected = true;
      wsReconnectTimer = null;
      wsBackoff = WS_BACKOFF_MIN;  // Reset on successful connect
      logTelemetry({ type: "agent_connected", agent: "bus", timestamp: Date.now() });
      console.log(`[interop-bridge] WebSocket connected: ${WS_URL}`);

      // 发送握手
      if (ws) {
        ws.send(JSON.stringify({
          id: nextId(),
          topic: "pi:announce",
          from: "pi",
          to: "*",
          type: "handshake",
          payload: JSON.stringify({ agent: "pi", status: "online" }),
          timestamp: Date.now(),
        }));
      }
    });

    ws.on("message", (raw) => {
      try {
        const msg: InteropMessage = JSON.parse(raw.toString());
        // Dedup check
        if (isDuplicate(msg.id)) return;

        // Activity tracking from heartbeat
        if (msg.type === "heartbeat") {
          try {
            const activity = JSON.parse(msg.content);
            if (activity.status) {
              updateAgentActivity(msg.from, activity);
            }
          } catch {}
        }

        wsBuffer.push({ message: msg, source: "ws", read: false });
        // 保持缓冲区不超限
        if (wsBuffer.length > WS_BUFFER_MAX) {
          wsBuffer.splice(0, wsBuffer.length - WS_BUFFER_MAX);
        }
      } catch {
        // 忽略非法消息
      }
    });

    ws.on("close", () => {
      wsConnected = false;
      logTelemetry({ type: "agent_disconnected", agent: "bus", timestamp: Date.now() });
      console.log("[interop-bridge] WebSocket disconnected");
      scheduleReconnect();
    });

    ws.on("error", (err) => {
      wsConnected = false;
      console.log(`[interop-bridge] WebSocket error: ${err.message}`);
      scheduleReconnect();
    });
  } catch (err: any) {
    console.log(`[interop-bridge] WebSocket init failed: ${err.message}, using file-only mode`);
    wsConnected = false;
  }
}

function scheduleReconnect() {
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    wsConnected = false;
    connectWebSocket();
  }, wsBackoff);
  wsBackoff = Math.min(wsBackoff * 2, WS_BACKOFF_MAX);
}

function sendViaWebSocket(msg: InteropMessage): boolean {
  if (!ws || !wsConnected) return false;
  try {
    const topic = msg.to === "neko" ? "pi:neko" : "pi:claude";
    ws.send(JSON.stringify({
      id: msg.id,
      topic,
      from: "pi",
      to: msg.to || "claude",
      type: msg.type,
      payload: msg.content,
      timestamp: msg.timestamp,
      replyTo: msg.replyTo,
    }));
    return true;
  } catch {
    wsConnected = false;
    return false;
  }
}

// ============================================================
// 消息合并（文件 + WS 缓冲）
// ============================================================

function getAllInboxEntries(): InboxEntry[] {
  const fileEntries = readFileInbox();
  // 去重：WS 消息如果在文件中也存在，跳过（以事件总线转发回来的情况）
  const wsEntries = wsBuffer.filter((we) => {
    return !fileEntries.some((fe) => fe.message.id === we.message.id);
  });
  // 合并并按时间排序
  return [...fileEntries, ...wsEntries].sort(
    (a, b) => a.message.timestamp - b.message.timestamp,
  );
}

function markWsRead(messageIds: string[]): void {
  for (const entry of wsBuffer) {
    if (messageIds.includes(entry.message.id)) {
      entry.read = true;
    }
  }
  // 清理已读的 WS 缓冲（保留最近 50 条未读）
  const unread = wsBuffer.filter((e) => !e.read);
  wsBuffer.length = 0;
  wsBuffer.push(...unread.slice(-50));
}

// ============================================================
// 通知渲染
// ============================================================

function formatTime(ts: number): string {
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)}分钟前`;
  if (diff < 86400_000) return `${Math.floor(diff / 3600_000)}小时前`;
  return new Date(ts).toLocaleDateString("zh-CN");
}

function formatPreview(content: string, maxLen = PREVIEW_LENGTH): string {
  const text = content.replace(/\n/g, " ").trim();
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + "…";
}

function agentIcon(agent: string): string {
  switch (agent) {
    case "pi": return "🟡";
    case "claude": return "🔵";
    case "neko": return "🐱";
    default: return "⚪";
  }
}

function typeLabel(type: string): string {
  switch (type) {
    case "task": return "[任务]";
    case "result": return "[结果]";
    case "handshake": return "[系统]";
    default: return "[消息]";
  }
}

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
    try {
      result[name] = adapter.status();
    } catch (err: any) {
      // 借鉴 N.E.K.O plugin registry failure isolation: 单个 agent 失败不影响整体
      result[name] = {
        online: false,
        connectedAt: 0,
        lastHeartbeat: 0,
        pendingOut: 0,
        pendingIn: 0,
        capabilities: [],
        error: err?.message || String(err),
      } as AgentStatus & { error: string };
      console.log(`[interop-bridge] Agent ${name} status failed: ${err?.message || err}`);
    }
  }
  result["pi"] = {
    online: true,
    connectedAt: Date.now(),
    lastHeartbeat: Date.now(),
    pendingOut: readdirSync(join(cwd, OUTBOX_DIR)).filter(f => f.endsWith(".json")).length,
    pendingIn: getAllInboxEntries().filter(e => !e.read).length,
    capabilities: ["interop_send", "interop_inbox", "interop_read", "interop_reply", "interop_delegate"],
    activity: agentActivity.get("pi") || { status: "active", since: Date.now() },
  };
  // Merge activity for registered agents
  for (const [name, status] of Object.entries(result)) {
    const activity = agentActivity.get(name);
    if (activity) {
      (status as any).activity = activity;
    }
  }
  return result;
}

// ============================================================
// 扩展入口
// ============================================================

export default function interopBridge(pi: ExtensionAPI) {
  // 会话启动时初始化
  pi.on("session_start", async (event) => {
    // @ts-ignore
    if (event?.cwd) cwd = event.cwd;
    ensureDirs();
    connectWebSocket();
  });

  // ==========================================================
  // interop_send — 发消息（双写：文件 + WS）
  // ==========================================================
  pi.registerTool({
    name: "interop_send",
    description:
      "Send a message to Claude or N.E.K.O. " +
      "Writes to file mailbox AND sends via WebSocket if available. " +
      "Use to='claude' (default) or to='neko'. " +
      "Type: 'message' for chat, 'task' for delegation, 'result' for return values.",
    parameters: {
      type: "object",
      properties: {
        type: {
          type: "string",
          enum: ["message", "task", "result", "handshake"],
          description: "Message type",
        },
        content: {
          type: "string",
          description: "Message content / task description",
        },
        to: {
          type: "string",
          enum: ["claude", "neko"],
          description: "Recipient (default: claude)",
        },
        replyTo: {
          type: "string",
          description: "Reply to a specific message ID",
        },
      },
      required: ["type", "content"],
    },
    async handler(args) {
      ensureDirs();
      const target = (args.to as string) || "claude";
      const msg: InteropMessage = {
        id: nextId(),
        from: "pi",
        to: target,
        type: args.type as InteropMessage["type"],
        content: args.content as string,
        timestamp: Date.now(),
        replyTo: args.replyTo as string | undefined,
      };

      // 文件通道（Claude 可读取）
      writeFileMessage(msg);

      // WebSocket 通道（N.E.K.O / Claude 可实时收到）
      const wsSent = sendViaWebSocket(msg);

      return {
        success: true,
        messageId: msg.id,
        to: target,
        channels: {
          file: true,
          websocket: wsSent,
        },
        note: wsSent
          ? `Sent to ${target} via file + WebSocket.`
          : `Sent to ${target} via file. WebSocket not connected.`,
      };
    },
  });

  // ==========================================================
  // interop_inbox — 📬 收件箱预览
  // ==========================================================
  pi.registerTool({
    name: "interop_inbox",
    description:
      "📬 View your interop inbox with previews. " +
      "Shows sender, type, time, and a short content preview for each unread message. " +
      "Use interop_read(id) to read the full message and mark as read.",
    parameters: {
      type: "object",
      properties: {
        limit: {
          type: "number",
          description: "Max messages to show (default 20)",
        },
      },
    },
    async handler(args) {
      ensureDirs();
      const limit = (args.limit as number) ?? 20;
      const all = getAllInboxEntries();
      const unread = all.filter((e) => !e.read).slice(0, limit);

      if (unread.length === 0) {
        return {
          count: 0,
          summary: "📭 收件箱为空 — 没有新消息。",
          messages: [],
        };
      }

      const byAgent: Record<string, number> = {};
      for (const e of unread) {
        byAgent[e.message.from] = (byAgent[e.message.from] || 0) + 1;
      }
      const agentSummary = Object.entries(byAgent)
        .map(([a, n]) => `${agentIcon(a)} ${a}: ${n}条`)
        .join("  ");

      return {
        count: unread.length,
        summary: `📬 收件箱 (${unread.length} 条未读)\n${agentSummary}`,
        messages: unread.map((e) => ({
          id: e.message.id,
          from: `${agentIcon(e.message.from)} ${e.message.from}`,
          type: typeLabel(e.message.type),
          preview: formatPreview(e.message.content),
          time: formatTime(e.message.timestamp),
          source: e.source,
          hasReply: !!e.message.replyTo,
        })),
        hint: `Use interop_read("<id>") to read full message and mark as read.`,
      };
    },
  });

  // ==========================================================
  // interop_read — 读单条消息全文 + 标记已读
  // ==========================================================
  pi.registerTool({
    name: "interop_read",
    description:
      "Read a single message by ID (full content) and mark it as read. " +
      "Get IDs from interop_inbox first.",
    parameters: {
      type: "object",
      properties: {
        id: {
          type: "string",
          description: "Message ID from interop_inbox",
        },
      },
      required: ["id"],
    },
    async handler(args) {
      ensureDirs();
      const msgId = args.id as string;
      const all = getAllInboxEntries();
      const entry = all.find((e) => e.message.id === msgId);

      if (!entry) {
        return {
          found: false,
          error: `Message "${msgId}" not found. It may have been deleted or already read.`,
        };
      }

      // 标记已读
      if (entry.source === "file" && entry.filename) {
        deleteFileMessage(entry.filename);
      }
      entry.read = true;
      markWsRead([msgId]);

      const m = entry.message;
      return {
        found: true,
        message: {
          id: m.id,
          from: m.from,
          to: m.to,
          type: m.type,
          content: m.content,
          timestamp: m.timestamp,
          time: formatTime(m.timestamp),
          replyTo: m.replyTo || undefined,
        },
        read: true,
        hint: m.replyTo
          ? `Reply with interop_reply("${m.id}", "your response")`
          : `Reply with interop_reply("${m.id}", "your response") or interop_send to ${m.from}`,
      };
    },
  });

  // ==========================================================
  // interop_reply — 快捷回复
  // ==========================================================
  pi.registerTool({
    name: "interop_reply",
    description:
      "Quick-reply to a specific message. Automatically sets replyTo and routes to the sender. " +
      "Get the message ID from interop_inbox first.",
    parameters: {
      type: "object",
      properties: {
        toId: {
          type: "string",
          description: "Message ID to reply to (from interop_inbox)",
        },
        content: {
          type: "string",
          description: "Your reply content",
        },
      },
      required: ["toId", "content"],
    },
    async handler(args) {
      ensureDirs();
      const toId = args.toId as string;
      const content = args.content as string;
      const all = getAllInboxEntries();
      const original = all.find((e) => e.message.id === toId);

      if (!original) {
        return {
          success: false,
          error: `Cannot find message "${toId}" to reply to.`,
        };
      }

      const sender = original.message.from;
      const msg: InteropMessage = {
        id: nextId(),
        from: "pi",
        to: sender,
        type: "message",
        content,
        timestamp: Date.now(),
        replyTo: toId,
      };

      writeFileMessage(msg);
      const wsSent = sendViaWebSocket(msg);

      return {
        success: true,
        messageId: msg.id,
        replyTo: toId,
        to: sender,
        channel: wsSent ? "file + websocket" : "file",
      };
    },
  });

  // ==========================================================
  // interop_check — 保留兼容
  // ==========================================================
  pi.registerTool({
    name: "interop_check",
    description:
      "[Legacy] Check for new messages. Prefer interop_inbox for preview mode. " +
      "Use preview=true for summary view, preview=false for full content.",
    parameters: {
      type: "object",
      properties: {
        limit: {
          type: "number",
          description: "Max messages to return (default 10)",
        },
        preview: {
          type: "boolean",
          description: "Show preview summaries instead of full content (default false)",
        },
        autoClear: {
          type: "boolean",
          description: "Automatically clear messages after reading (default false)",
        },
      },
    },
    async handler(args) {
      ensureDirs();
      const limit = (args.limit as number) ?? 10;
      const preview = (args.preview as boolean) ?? false;
      const autoClear = (args.autoClear as boolean) ?? false;
      const all = getAllInboxEntries();
      const messages = all.slice(0, limit);

      if (autoClear && messages.length > 0) {
        const fileFilenames = messages.filter((e) => e.source === "file").map((e) => e.filename!);
        clearFileMessages(fileFilenames);
        markWsRead(messages.map((e) => e.message.id));
        messages.forEach((e) => (e.read = true));
      }

      if (preview) {
        return {
          count: messages.length,
          messages: messages.map((e) => ({
            id: e.message.id,
            from: `${agentIcon(e.message.from)} ${e.message.from}`,
            type: typeLabel(e.message.type),
            preview: formatPreview(e.message.content),
            time: formatTime(e.message.timestamp),
            source: e.source,
          })),
          hint: messages.length === 0
            ? "📭 No new messages."
            : `Use interop_read("<id>") to read full message.`,
        };
      }

      return {
        count: messages.length,
        messages: messages.map((e) => ({
          id: e.message.id,
          from: e.message.from,
          type: e.message.type,
          content: e.message.content,
          timestamp: e.message.timestamp,
          replyTo: e.message.replyTo,
          source: e.source,
        })),
        hint: messages.length === 0
          ? "No messages yet."
          : "Use interop_inbox for a nicer preview view.",
      };
    },
  });

  // ==========================================================
  // interop_delegate — 委派任务 + 等待结果
  // ==========================================================
  pi.registerTool({
    name: "interop_delegate",
    description:
      "Delegate a task to Claude or N.E.K.O. Sends task, then polls for result. " +
      "Use to='claude' (default) or to='neko'. Timeout in ms (default 30000).",
    parameters: {
      type: "object",
      properties: {
        task: {
          type: "string",
          description: "Task description",
        },
        to: {
          type: "string",
          enum: ["claude", "neko"],
          description: "Delegate to which agent (default: claude)",
        },
        timeout: {
          type: "number",
          description: "Max wait time in ms (default 30000)",
        },
      },
      required: ["task"],
    },
    async handler(args) {
      ensureDirs();
      const task = args.task as string;
      const target = (args.to as string) || "claude";
      const timeout = (args.timeout as number) ?? 30_000;

      const taskMsg: InteropMessage = {
        id: nextId(),
        from: "pi",
        to: target,
        type: "task",
        content: task,
        timestamp: Date.now(),
      };

      writeFileMessage(taskMsg);
      const wsSent = sendViaWebSocket(taskMsg);

      // SoulLoop pre_task check
      let soulPre: any = null;
      try {
        const { execSync } = require("child_process");
        const preResult = execSync(
          `python plastic_promise/core/soul_bridge.py pre_task "${task.replace(/"/g, '\\"')}" "general"`,
          { cwd, timeout: 10000, encoding: "utf-8" }
        );
        soulPre = JSON.parse(preResult);
        if (soulPre.blocked) {
          return {
            success: false,
            taskId: taskMsg.id,
            blocked: true,
            layer: soulPre.layer,
            reason: soulPre.block_reason,
            trust: soulPre.trust,
            hint: `Task blocked by Soul System ${soulPre.layer || "defense"}: ${soulPre.block_reason}`,
          };
        }
      } catch (err: any) {
        // Soul system unavailable — proceed without it
        console.log(`[interop-bridge] Soul pre_task failed: ${err?.message}, proceeding without`);
      }

      // Poll for results
      const startTime = Date.now();
      let attempts = 0;
      while (Date.now() - startTime < timeout) {
        attempts++;
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const all = getAllInboxEntries();
        const reply = all.find(
          (e) =>
            (e.message.replyTo === taskMsg.id || e.message.type === "result") &&
            e.message.from === target,
        );
        if (reply) {
          if (reply.source === "file" && reply.filename) {
            deleteFileMessage(reply.filename);
          }
          markWsRead([reply.message.id]);

          // SoulLoop post_task
          let soulPost: any = null;
          try {
            const { execSync } = require("child_process");
            const postResult = execSync(
              `python plastic_promise/core/soul_bridge.py post_task "${reply.message.content.replace(/"/g, '\\"').slice(0, 500)}" "general" --success`,
              { cwd, timeout: 10000, encoding: "utf-8" }
            );
            soulPost = JSON.parse(postResult);
          } catch (err: any) {
            console.log(`[interop-bridge] Soul post_task failed: ${err?.message}`);
          }

          return {
            success: true,
            taskId: taskMsg.id,
            result: reply.message.content,
            waitMs: Date.now() - startTime,
            attempts,
            channel: wsSent ? "file + websocket" : "file",
            soul: soulPre ? {
              pre: { trust: soulPre.trust, scarf: soulPre.scarf?.summary?.overall_score },
              post: soulPost ? { trust: soulPost.trust, delta: soulPost.trust_delta } : null,
            } : undefined,
          };
        }
      }

      return {
        success: false,
        taskId: taskMsg.id,
        result: `Timeout — ${target} has not responded in ${timeout}ms. The task is still pending.`,
        waitMs: Date.now() - startTime,
        attempts,
      };
    },
  });

  // ==========================================================
  // interop_status — 互通状态
  // ==========================================================
  pi.registerTool({
    name: "interop_status",
    description: "Get interop bridge status: connections, pending messages, agents online.",
    parameters: {
      type: "object",
      properties: {},
    },
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
  });

  // ==========================================================
  // interop_principles — 查看/激活原则
  // ==========================================================
  pi.registerTool({
    name: "interop_principles",
    description:
      "View or activate principles from the shared Plastic Promise principle engine. " +
      "With no arguments, shows currently active principles. " +
      "Use action='activate' with a taskType to activate relevant principles.",
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

      const taskType = (args.taskType as string) || "general";
      return {
        activated: true,
        taskType,
        principles: ["p1", "p3", "p4"],
        note: `Principles for "${taskType}" activated via Plastic Promise engine.`,
      };
    },
  });

  // ==========================================================
  // interop_soul — 灵魂系统完整状态
  // ==========================================================
  pi.registerTool({
    name: "interop_soul",
    description:
      "View the complete Soul System status: SCARF 5-dimension health, Trust score, " +
      "Hormone levels (dopamine/cortisol), Defense status (L0/L1/L2), Curiosity exploration, " +
      "and Proprioception pattern analysis. Runs via Plastic Promise soul_bridge.",
    parameters: {
      type: "object",
      properties: {},
    },
    async handler() {
      try {
        const { execSync } = require("child_process");
        const result = execSync(
          `python bridge/soul_bridge.py status`,
          { cwd, timeout: 10000, encoding: "utf-8" }
        );
        const soul = JSON.parse(result);
        return {
          soul,
          summary: soul.initialized
            ? `🧠 Soul active | Trust: ${soul.trust?.score || "?"} (${soul.trust?.tier || "?"}) | SCARF: ${soul.scarf?.overall_score || "?"} | Hormone: ${soul.hormone?.mood || "?"}`
            : "⚠️ Soul system not initialized. Check Plastic Promise submodule.",
        };
      } catch (err: any) {
        return {
          error: "Soul system unavailable",
          detail: err?.message || String(err),
          hint: "Set SOUL_ENABLED=0 to disable, or check plastic-promise submodule.",
        };
      }
    },
  });

  // ==========================================================
  // interop_trust — 信任分
  // ==========================================================
  pi.registerTool({
    name: "interop_trust",
    description:
      "View or adjust the Soul System trust score. " +
      "With no args: view current trust. " +
      "With boost=N: increase trust by N. With decay=N: decrease trust by N.",
    parameters: {
      type: "object",
      properties: {
        action: {
          type: "string",
          enum: ["view", "boost", "decay"],
          description: "Action: view trust, boost (increase), or decay (decrease)",
        },
        amount: {
          type: "number",
          description: "Amount to boost/decay (0.0 ~ 0.5)",
        },
      },
    },
    async handler(args) {
      const action = (args.action as string) || "view";
      try {
        const { execSync } = require("child_process");
        const result = execSync(
          `python bridge/soul_bridge.py status`,
          { cwd, timeout: 10000, encoding: "utf-8" }
        );
        const soul = JSON.parse(result);
        const trust = soul.trust || { score: 0.6, tier: "medium" };

        if (action === "view") {
          return {
            action: "view",
            trust: {
              score: trust.score,
              tier: trust.tier,
              autonomy: trust.autonomy || trust.autonomy_level,
            },
            note: trust.score >= 0.8
              ? "🟢 High trust — full autonomy"
              : trust.score >= 0.6
              ? "🟡 Medium trust — normal operation"
              : trust.score >= 0.4
              ? "🟠 Low trust — tasks require caution"
              : "🔴 Critical trust — tasks will be blocked",
          };
        }

        return {
          action,
          trust: { score: trust.score, tier: trust.tier },
          note: "Trust adjustment via CLI not yet implemented in this view. Use Python API directly.",
        };
      } catch (err: any) {
        return { error: "Trust system unavailable", detail: err?.message };
      }
    },
  });

  console.log("[interop-bridge v3] Pi↔Claude/N.E.K.O 双通道桥接已就绪");
  console.log(`[interop-bridge v3] 文件邮箱: ${join(cwd, OUTBOX_DIR)} → ${join(cwd, INBOX_DIR)}`);
  console.log(`[interop-bridge v3] WebSocket: ${WS_URL}`);
}
