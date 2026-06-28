# Phase 2: N.E.K.O Patterns Integration — Design Spec

**Date**: 2026-06-28
**Status**: Approved
**Scope**: agent-interop Phase 2 — 借鉴 N.E.K.O 8 个模式，三轨并行

---

## 1. 目标

将 N.E.K.O 的工程实践和架构模式应用于 agent-interop，提升可靠性、可扩展性和可观测性。

## 2. 整体架构

三轨并行，互不阻塞：

```
轨道 1: 工程基础设施
  ┌──────────┐   ┌──────────────┐   ┌──────────┐
  │ Launcher │   │ WS Slot 管理 │   │ 消息去重 │
  │ 一键启动  │   │ 自动重连/心跳│   │ 双通道去重│
  └──────────┘   └──────────────┘   └──────────┘

轨道 2: 架构增强
  ┌──────────────┐   ┌──────────────┐   ┌──────────┐
  │ Agent 注册   │   │ HTTP 通信    │   │ 角色系统 │
  │ 接口+发现    │   │ 内网 REST    │   │ 原则映射 │
  └──────────────┘   └──────────────┘   └──────────┘

轨道 3: 可观测性
  ┌──────────┐   ┌──────────────┐
  │  遥测    │   │  活动追踪    │
  │ 匿名用量 │   │ Agent 状态   │
  └──────────┘   └──────────────┘
```

### 2.1 接口依赖

- 轨道 1 不依赖轨道 2/3，可独立交付
- 轨道 2 的 Agent 注册需轨道 1 的启动器加载
- 轨道 3 的遥测数据通过轨道 2 的 HTTP 通道上报（回退到本地文件）

---

## 3. 轨道 1：工程基础设施

### 3.1 启动器（Launcher）

**目标**：一条命令启动全部服务。

**文件**：`start-all.sh` / `start-all.bat`

```
start-all.sh              # 启动全部
start-all.sh --no-neko    # 不启动 N.E.K.O 适配器
start-all.sh --status     # 查看各服务状态
```

**内部逻辑**：

```
1. 检测端口 48970 是否已被占用 → 跳过 event-bus
2. 启动 npx tsx bridge/event-bus.ts & → 记录 PID
3. 等待 WS 端口就绪（最多 5s）
4. 启动 python bridge/neko_adapter.py & → 记录 PID
5. 等待 ZMQ 连接就绪
6. trap EXIT SIGINT → kill 所有 PID
7. PID 文件写入 .interop/.pid/
```

### 3.2 WS 连接健壮性（_WSSlot 模式）

**借鉴**：N.E.K.O `cross_server.py` 的 `_WSSlot` / `_slot_maintainer` / `_mark_dead`

**应用位置**：
- `bridge/neko_adapter.py`：Python 端 WS 重连
- `.pi/extensions/interop-bridge.ts`：TypeScript 端 WS 重连

**核心模式**：

| N.E.K.O 模式 | 说明 | 应用到 |
|---|---|---|
| `_WSSlot` | 封装 WS + 死信事件 `dead_event` | `neko_adapter.py` `WSSlot` 类 |
| `_slot_maintainer` | 事件驱动重连 + 指数退避 (0.25s→1.5s) | 替换现有 setTimeout 重连 |
| `_mark_dead` | 断线标记，唤醒 maintainer | 替换现有 onclose 处理 |
| 指数退避 | `backoff = min(backoff * 2, max)` | 1s → 2s → 4s → 8s → max 15s |

### 3.3 消息去重

**问题**：双通道（文件 + WS）可能导致同一条消息出现两次。

**方案**：内存去重窗口

```typescript
const seenIds = new Set<string>();  // 保留最近 500 条 ID

function isDuplicate(id: string): boolean {
  if (seenIds.has(id)) return true;
  seenIds.add(id);
  if (seenIds.size > 500) {
    const arr = [...seenIds];
    seenIds.clear();
    arr.slice(-250).forEach(id => seenIds.add(id));
  }
  return false;
}
```

**应用位置**：
- `interop-bridge.ts`：WS 接收消息时去重
- `neko_adapter.py`：ZMQ→WS 转发时去重

---

## 4. 轨道 2：架构增强

### 4.1 Agent 注册机制

**目标**：统一 Agent 接口，新 agent 类型接入只需实现接口。

**接口定义**：

```typescript
interface AgentAdapter {
  name: string;                          // "claude", "neko", "pi"
  capabilities: string[];                // ["code_gen", "browser_use", ...]
  connect(): Promise<void>;
  disconnect(): Promise<void>;
  send(msg: InteropMessage): Promise<boolean>;
  onMessage(handler: (msg: InteropMessage) => void): void;
  status(): AgentStatus;
}

interface AgentStatus {
  online: boolean;
  connectedAt: number;
  lastHeartbeat: number;
  pendingOut: number;
  pendingIn: number;
}
```

**Agent 注册表**（在 `interop-bridge.ts`）：
```
{ pi: PiAdapter, claude: ClaudeAdapter, neko: NekoAdapter }
```

**能力发现**：Agent 连接时广播 capabilities，其他 agent 可通过 `interop_status` 查看。

### 4.2 HTTP 内网通信

**目标**：`neko_adapter.py` 可直接通过 HTTP 操作 Plastic Promise 记忆。

**架构**：

```
neko_adapter.py ── HTTP POST ──→ Plastic Promise (localhost:48920)
  /memory/store    ←→  memory_store
  /memory/recall   ←→  memory_recall
  /context/supply  ←→  context_supply
```

**实现**：在 Plastic Promise MCP Server 加一个可选的 HTTP wrapper（Flask 或 FastAPI），或复用现有 transport。选中 `--http-port` 启动参数。

### 4.3 角色系统

**目标**：将 N.E.K.O 角色卡概念映射到 Plastic Promise 原则引擎。

**映射关系**：

| N.E.K.O 角色卡 | Plastic Promise | 说明 |
|---|---|---|
| 基础人设（性格/喜好） | `principle_activate` | 按任务类型激活原则集 |
| 系统指令 | `context_supply` 注入 | 每次上下文供应附带角色设定 |
| 记忆持久化 | `memory_store` 事实/反思 | 角色相关记忆分类存储 |

**`interop_status` 增强**：显示当前激活的原则集名称。

---

## 5. 轨道 3：可观测性

### 5.1 遥测

**借鉴**：N.E.K.O `token_tracker.py`

**原则**：
- 绝不收集消息内容、API Key、用户身份
- 仅统计：消息数量、类型、延迟、错误次数
- 环境变量 `DO_NOT_TRACK=1` 完全关闭

**数据结构**：

```typescript
interface TelemetryEvent {
  type: "message_sent" | "task_delegated" | "agent_connected" | "agent_disconnected" | "error";
  agent: string;
  timestamp: number;
  duration?: number;          // 任务耗时（仅 task_delegated）
  errorType?: string;         // 仅 error 类型
}
```

**存储**：本地 JSONL 文件 `.interop/telemetry.jsonl`，每日轮转。

### 5.2 活动追踪

**目标**：每个 Agent 当前状态可视化。

**`interop_status` 输出增强**：

```
🐱 neko    [忙碌] 正在执行浏览器任务 "截图 example.com" (12s)
🔵 claude  [空闲] 上次活跃 2分钟前
🟡 pi      [活跃] 正在交互
```

**实现**：Agent 在 heartbeat 消息中附带 `activity` 字段：

```json
{
  "type": "heartbeat",
  "activity": { "status": "busy", "task": "截图 example.com", "elapsed": 12 }
}
```

---

## 6. 文件规划

```
F:\Agent\agent-interop\
├── start-all.sh                    ← ★ 新增：Linux/macOS 启动器
├── start-all.bat                   ← ★ 新增：Windows 启动器
├── .interop/
│   ├── .pid/                       ← ★ 新增：PID 文件
│   └── telemetry.jsonl             ← ★ 新增：遥测数据
├── bridge/
│   ├── neko_adapter.py             ← 修改：_WSSlot 去重
│   └── bus_client.py               ← 修改：WSSlot 重连
├── .pi/
│   └── extensions/
│       └── interop-bridge.ts       ← 修改：去重 + 遥测 + 活动
├── plastic-promise/
│   └── http_server.py              ← ★ 新增：HTTP wrapper
└── README.md                       ← 修改：Phase 2 文档
```

## 7. 配置

```bash
# 新增环境变量
DO_NOT_TRACK=1                      # 关闭遥测
PLASTIC_PROMISE_HTTP_PORT=48920     # Plastic Promise HTTP 端口
INTEROP_START_NO_NEKO=1             # 启动器跳过 N.E.K.O
```

## 8. 技术决策记录

- **Shell 脚本而非 Node.js 启动器**：简单直接，不引入新依赖，Unix/Windows 各一份
- **内存去重而非持久化**：消息 ID 碰撞概率极低，500 条窗口足够覆盖短期重复
- **遥测本地存储**：先不连外部服务，JSONL 文件轻量、可后期分析
- **HTTP wrapper 而非 MCP stdio**：HTTP 更适合 Python 客户端，MCP stdio 保留给 Claude Code
- **活动追踪通过 heartbeat 附带**：复用现有通道，不新增消息类型
