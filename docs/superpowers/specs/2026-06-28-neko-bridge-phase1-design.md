# Phase 1: N.E.K.O Bridge Adapter — Design Spec

**Date**: 2026-06-28
**Status**: Approved
**Scope**: agent-interop Phase 1 — N.E.K.O 桥接层

---

## 1. 目标

将 N.E.K.O 接入 agent-interop 三方互通体系，实现 Pi ↔ Claude ↔ N.E.K.O 实时通信。

Phase 1 聚焦基础设施连通，Phase 2 做 Agent 任务委派，Phase 3 做记忆深度整合。

## 2. 架构

### 2.1 整体拓扑

```
┌─────────────────────────────────────────────────────────────────┐
│                      N.E.K.O Adapter                             │
│                                                                  │
│  ┌──────────────────────┐          ┌─────────────────────────┐  │
│  │    N.E.K.O ZMQ       │          │   Interop WebSocket      │  │
│  │                      │  neko-   │                          │  │
│  │  SUB ── SESSION_PUB  │ adapter  │  ws://127.0.0.1:48970   │  │
│  │       (tcp:48961)    │  .py     │  agent=neko             │  │
│  │                      │          │                          │  │
│  │  PUSH ── AGENT_PUSH  │          │  Topics:                │  │
│  │       (tcp:48962)    │          │  neko:pi | neko:claude  │  │
│  └──────────────────────┘          └─────────────────────────┘  │
│                                                                  │
│  消息流向:                                                       │
│  N.E.K.O → (SUB) → 翻译 → (WS send) → Pi / Claude               │
│  Pi / Claude → (WS recv) → 翻译 → (PUSH) → N.E.K.O agent_server │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 进程模型

- **独立进程**：`python bridge/neko-adapter.py`，与 `event-bus.ts` 并行运行
- **双线程**：ZMQ 后台接收线程 + WebSocket asyncio 事件循环
- **复用**：直接引用 `bus-client.py` 中的 `InteropClient` 类

### 2.3 组件清单

| 组件 | 文件 | 类型 | 说明 |
|------|------|------|------|
| N.E.K.O Adapter | `bridge/neko-adapter.py` | 新增 | 独立进程，ZMQ↔WebSocket 双向桥接 |
| Event Bus | `bridge/event-bus.ts` | 已有 | WebSocket 服务器，无需修改 |
| WS Client | `bridge/bus-client.py` | 已有 | `InteropClient` 类被 adapter 复用 |
| 环境变量模板 | `.env.example` | 新增 | 配置项文档 |

## 3. 消息映射

### 3.1 N.E.K.O → Pi/Claude（事件转发）

| N.E.K.O ZMQ 事件 | WS Topic | Type | 说明 |
|---|---|---|---|
| `session_lifecycle` | `neko:status` | `handshake` | Agent 上线/下线 |
| `voice_transcript_observed` | `neko:voice` | `message` | 实时语音转写文本 |
| `agent_result` | `neko:result` | `result` | 任务执行结果 |
| 其他 session_event | `neko:event` | `message` | 通用事件透传 |

### 3.2 Pi/Claude → N.E.K.O（任务注入）

| WS Topic | WS Type | 翻译后 ZMQ 事件 | 通道 |
|---|---|---|---|
| `pi:neko` / `claude:neko` | `task` | `analyze_request` | ANALYZE_PUSH (tcp:48963) |
| `pi:neko` / `claude:neko` | `message` | session_event | SESSION_PUB 已订阅 |

### 3.3 ZMQ 通道分配

| 通道 | 环境变量 | 默认端口 | 方向 | 用途 |
|------|----------|----------|------|------|
| SESSION_PUB | `NEKO_ZMQ_SESSION_PUB_PORT` | 48961 | main → adapter (SUB) | 接收 N.E.K.O 事件 |
| AGENT_PUSH | `NEKO_ZMQ_AGENT_PUSH_PORT` | 48962 | adapter → main (PUSH) | 通用消息注入 |
| ANALYZE_PUSH | `NEKO_ZMQ_ANALYZE_PUSH_PORT` | 48963 | adapter → agent (PUSH) | 任务委派 |

### 3.4 数据序列化

- ZMQ 侧：`orjson` 序列化（与 N.E.K.O 保持一致）
- WebSocket 侧：`json` 序列化（与 `InteropClient` 保持一致）

## 4. 借鉴 N.E.K.O 多 Agent 模式

### 4.1 能力发现与广播

N.E.K.O `agent_server` 启动时通过 ZMQ 广播 `session_lifecycle` 事件。Adapter 翻译为 WebSocket `capability_broadcast`：

```json
{
  "id": "neko-adapter-001",
  "topic": "neko:announce",
  "from": "neko",
  "to": "*",
  "type": "handshake",
  "payload": {
    "agent": "neko",
    "capabilities": ["browser_use", "computer_use", "openclaw", "openfang"],
    "status": "online"
  },
  "timestamp": 1719576000000
}
```

### 4.2 任务委派（analyze_request 模式）

借鉴 N.E.K.O `publish_analyze_request_reliably` 模式：
- 每条任务带 `event_id`，等待 ack 确认送达
- 超时重试（0.8s ack 超时，最多 1 次重试）
- Agent 离线时返回 `agent_unavailable`

对应关系：
| N.E.K.O 模式 | agent-interop 实现 |
|---|---|
| `event_type: analyze_request` | WS `type: task` on `pi:neko` / `claude:neko` |
| `event_id` + ack 等待 | `replyTo` + result 回执 |
| 超时重试 | `delegate_task(timeout, retries)` |
| `external_intent` 预判 | Phase 2 添加 |

### 4.3 多 Agent 路由表

Adapter 维护实时路由表，追踪所有 Agent 在线状态：

```
┌────────┬──────────┬─────────────────────────────┐
│ Agent  │  Status  │  Capabilities               │
├────────┼──────────┼─────────────────────────────┤
│ neko   │  online  │  browser_use, cua,          │
│        │          │  openclaw, openfang         │
│ pi     │  online  │  context_supply,            │
│        │          │  principle_activate         │
│ claude │  online  │  code_gen, file_edit        │
└────────┴──────────┴─────────────────────────────┘
```

### 4.4 Sink 订阅模式

借鉴 N.E.K.O `register_*` / `dispatch_*` 模式，adapter 提供开放的事件订阅接口：

```python
adapter = NekoAdapter()
adapter.subscribe(topic="voice_transcript", handler=handle_voice)
adapter.subscribe(topic="agent_result", handler=handle_result)
```

## 5. 数据流

### 5.1 路径 1：N.E.K.O 状态事件 → Pi/Claude

```
N.E.K.O ZMQ PUB (48961)
    │ SUB recv
    ▼
Adapter: 过滤 event_type
    │
    ├── session_lifecycle  → 提取 agent 状态、能力清单
    │                         翻译为 neko:announce → WS send
    │
    ├── voice_transcript   → 提取 transcript 文本
    │                         翻译为 neko:voice → WS send
    │
    └── agent_result       → 提取 result payload
                              翻译为 neko:result → WS send
```

### 5.2 路径 2：Pi/Claude 任务 → N.E.K.O

```
Pi/Claude: interop_send(task)
    │ WS recv (type=task, topic=pi:neko)
    ▼
Adapter: 构建 analyze_request
    │ event_id = uuid
    │ 记录 pending (event_id → asyncio.Future)
    │ PUSH → ANALYZE_PUSH (tcp:48963)
    ▼
N.E.K.O agent_server: 收到，ack 回执
    │
    ▼ (ack 到达)
Adapter: resolve future → 任务已送达
    │ (超时 0.8s 未收到 ack)
    ▼
重试 1 次，仍未到达 → 通知发送方 timeout
```

### 5.3 路径 3：N.E.K.O 任务结果 → Pi/Claude

```
N.E.K.O agent: emit task result
    │ SUB recv
    ▼
Adapter: 匹配 replyTo（event_id）
    │ WS send → neko:result (type=result, replyTo=original_id)
    ▼
Pi/Claude: interop_check() 收到结果
```

## 6. 错误处理

| 场景 | 策略 | 说明 |
|------|------|------|
| ZMQ 连接断开 | 指数退避重连 | 1s → 2s → 4s → 8s → max 15s |
| WebSocket 断开 | 复用 InteropClient 重连 | bus-client.py 已有 |
| 消息格式非法 | 记录日志 + 丢弃 | 不崩溃 |
| 任务超时无 ack | 通知发送方 timeout | 不阻塞后续消息 |
| adapter 进程崩溃 | 外部 supervisor 重启 | 或手动重新运行 |
| N.E.K.O agent 离线 | 返回 agent_unavailable | 任务不入队 |
| 优雅关闭 (Ctrl+C) | 停止新任务 → 等待完成 (5s) → 关闭 socket | linger=0 |

## 7. 配置

### 7.1 环境变量

```bash
INTEROP_BUS_URL=ws://127.0.0.1:48970        # WebSocket 总线地址
NEKO_ZMQ_SESSION_PUB_PORT=48961             # N.E.K.O ZMQ PUB 端口
NEKO_ZMQ_AGENT_PUSH_PORT=48962              # N.E.K.O ZMQ AGENT PUSH
NEKO_ZMQ_ANALYZE_PUSH_PORT=48963            # N.E.K.O ZMQ ANALYZE PUSH
NEKO_ADAPTER_LOG_LEVEL=INFO                 # 日志级别 (DEBUG/INFO/WARN)
```

### 7.2 启动参数

```bash
python bridge/neko-adapter.py \
  --bus-url ws://127.0.0.1:48970 \
  --session neko-bridge-v2 \
  --log-level INFO
```

### 7.3 启动顺序

```
1. npx tsx bridge/event-bus.ts        # WebSocket 事件总线
2. python bridge/neko-adapter.py      # N.E.K.O 桥接适配器
3. (N.E.K.O 由用户正常启动)
4. (Pi / Claude 连接 WebSocket)
```

## 8. 文件规划

```
F:\Agent\agent-interop\
├── bridge/
│   ├── event-bus.ts          ← 已有，无需修改
│   ├── bus-client.py         ← 已有，被 adapter 复用
│   ├── sync-coordinator.ts   ← 已有，无需修改
│   └── neko-adapter.py       ← ★ 新增
├── .env.example              ← ★ 新增
└── README.md                 ← 更新说明
```

## 9. 后续阶段预览

| Phase | 内容 | 依赖 |
|-------|------|------|
| Phase 1 | N.E.K.O 桥接适配器（本设计） | — |
| Phase 2 | Agent 任务委派系统 | Phase 1 连通 |
| Phase 3 | 五维记忆 ↔ Plastic Promise 深度映射 | Phase 1 连通 |

## 10. 技术决策记录

- **独立进程而非插件集成**：N.E.K.O 源码未完整 clone，ZMQ 协议是公开接口，独立进程解耦更安全
- **复用 InteropClient**：bus-client.py 已实现 WebSocket 连接、消息路由、心跳、任务委派，adapter 直接继承
- **orjson vs json**：ZMQ 侧用 orjson 保持与 N.E.K.O 一致；WS 侧用 json 保持与现有 InteropClient 一致
- **ZMQ SUB 而非 PULL**：SESSION_PUB 是广播通道，必须用 SUB 接收；ANALYZE_PUSH 是点对点，用 PUSH 发送
