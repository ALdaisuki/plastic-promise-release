# Auto Context Inject — 统一自动化上下文注入

> 状态: 已确认 | 日期: 2026-07-01 | 方案: MCP 工具统一 (方案 2)

## 一、动机

### 问题

Plastic Promise 的自动化上下文注入能力（SoulBridge.pre_task → SoulLoop.pre_task_v2 → ContextEngine.supply）目前**只有 Pi Agent 路径能享受**。Claude Code 路径依赖 CLAUDE.md 指令 + 手动 MCP 调用，摩擦大，实际使用率极低。

更根本的问题：两条路径是**分离的**——Pi Agent 和 Claude Code 各自有不同的上下文注入代码路径，且我们刚完成的 skill_session_start 追踪系统与 SoulBridge 没有打通。

### 目标

1. **统一入口**：Pi Agent、Claude Code Hook、SoulBridge Python API 三条路径共用同一个 MCP 工具 `auto_context_inject`
2. **与记忆系统深度绑定**：注入记录自身沉淀为记忆，形成自反馈循环——下次相似任务自动召回上次的注入上下文
3. **与 Skill Tracking 打通**：每次自动注入自动创建 skill_session entity（skill_name 带 `auto_inject:` 前缀），纳入审计链
4. **简化 CLAUDE.md**：一个 `auto_context_inject` 替代 3 个手动 MCP 调用

## 二、架构

```
                        ┌──────────────────────────────────┐
                        │  auto_context_inject (NEW)        │
                        │  mcp/tools/context.py             │
                        │                                   │
                        │  1. skill_session_start()         │
                        │     → entity 创建 + 原则激活      │
                        │  2. SoulLoop.pre_task_v2()        │
                        │     → ContextEngine.supply()      │
                        │  3. memory_store()                │
                        │     → 注入记录沉淀为记忆          │
                        └──────────┬───────────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
                    ▼              ▼              ▼
          ┌─────────────┐  ┌───────────┐  ┌──────────────┐
          │ Pi Agent    │  │ Claude    │  │ SoulBridge   │
          │ (pi_daemon) │  │ Code Hook │  │ .pre_task()  │
          │             │  │           │  │              │
          │ MCP call    │  │ MCP call  │  │ Python API   │
          │ via SSE     │  │ via CLI   │  │ (直接调handler│
          └─────────────┘  └───────────┘  └──────────────┘
```

**关键设计决策**：

- **MCP 工具是唯一入口**——不新增 CLI 命令，不新增独立模块
- **SoulBridge 直接 Python 调用 handler**——与 `skill_session_start` 内部调用 `handle_principle_activate` 相同的 lazy import 模式，不走网络
- **零新存储结构**——全部复用 MemoryRecord + entity graph + tag 系统

## 三、MCP 工具 Schema

### auto_context_inject

```
参数:
  task_description: string (必填) — 当前任务描述
  task_type:        string — 任务类型 (默认 "general")
  source:           string — "pi_agent" | "claude_code" | "manual" (默认 "manual")
  scope:            string — 检索范围 (默认 "global")。Claude Code Hook 传入 "agent:claude" 防止与 Pi Agent 记忆互污

内部步骤:
  1. skill_session_start(skill_name=derive_from(source), task_description)
     → entity_id = "skill:auto_inject:pi_agent:2026-07-01T..." 
     → 原则激活 + 记忆召回 + entity 图谱注册
     → domain fallback: auto_inject:* → "reflecting" (审计快照本质)
  2. SoulLoop.pre_task_v2(task_description, task_type)
     → ContextPack (core/related/divergent + activated_principles)
     → 异常降级时: 调用 principle_activate(task_type="general") 作为保底，确保原则不空
  3. memory_store(
       content="[AUTO INJECT] {task_description}\ncore_items: {len(core)}\nactivated_principles: {names}",
       memory_type="experience",
       source="auto_inject",
       entity_ids=[entity_id],
       tags=["auto_inject", f"source:{source}", f"skill:auto_inject:{source}"]
     )
     → content 保留完整 task_description 原文，确保下次检索可命中
     → 注入记录沉淀为记忆，形成自反馈循环
  4. skill_session_complete(entity_id, outcome="注入完成", artifacts=[])
     → 自动完成追踪实体（auto_inject 是一次性操作，注入即完成）
     → duration_ms 极短（<2s）是预期行为；审计时孤儿检测自动跳过 auto_inject: 前缀
  5. 返回聚合结果

返回值:
  {
    "entity_id":          "skill:auto_inject:claude_code:2026-07-01T18:00:00",
    "skill_name":         "auto_inject:claude_code",
    "context_pack":       { "core": [...], "related": [...], "divergent": [...] },
    "principles":         [{"id":2, "name":"全过程可查可透明"}, ...],
    "inject_memory_id":   "mem_abc123",
    "stats":              { "memory_pool_size": 35, "fuzzy_buffer_backlog": 0 }
  }
```

### source → skill_name 派生规则

```
"pi_agent"     → "auto_inject:pi_agent"
"claude_code"  → "auto_inject:claude_code"
"manual"       → "auto_inject:manual"
```

带 `auto_inject:` 前缀的 skill_name 与 SuperPowers skill 区分，entity_type 仍是 `skill_session`，在 `skill_session_trace` 中可独立筛选。

### 异常处理

| 场景 | 行为 |
|------|------|
| `skill_session_start` 失败 | 不阻塞——注入记录仍存储，entity 链不完整但可事后补录 |
| `SoulLoop.pre_task_v2` 失败（embedding 不可用） | 降级为纯文本检索（FallbackEmbedder），返回降级标记 |
| `memory_store` 失败 | 不阻塞——上下文包已返回给调用方，注入记录可事后通过 audit 补录 |
| 全部三个内部调用失败 | 返回 `{"error": "...", "partial": true}`，不抛异常 |

**设计原则**：优雅降级，永不阻塞。自动注入是"信息供应"，不是"门禁"。

## 四、三条路径的接入

### 路径 1: SoulBridge（内部 Python 调用）

```python
# bridge/soul_bridge.py — pre_task() 方法

async def pre_task(self, task: str, task_type: str = "general"):
    from plastic_promise.mcp.tools.context import handle_auto_context_inject
    
    result = await handle_auto_context_inject(self._engine, {
        "task_description": task,
        "task_type": task_type,
        "source": "pi_agent",
    })
    data = json.loads(result[0].text)
    
    # 现有返回结构兼容：context 从 context_pack 提取
    if data.get("context_pack"):
        data["context"] = {"summary": str(data["context_pack"])[:200]}
    return data
```

改动面：`bridge/soul_bridge.py` 的 `pre_task()` 方法，~15 行替换。向后兼容——返回结构不变。

### 路径 2: Claude Code Hook（MCP CLI 调用）

```bash
# hooks/session-start — 在现有注入 using-superpowers/SKILL.md 代码之后追加

# 调用 auto_context_inject (MCP 工具统一入口)
# scope: "agent:claude" 防止 Pi Agent 记忆与 Claude Code 记忆互相污染
claude mcp call plastic-promise auto_context_inject \
  '{"task_description":"会话启动","task_type":"general","scope":"agent:claude","source":"claude_code"}' \
  2>/dev/null || true  # 优雅降级: 失败不阻塞会话启动
```

改动面：`hooks/session-start` 脚本，~3 行追加。Hook 已被 SuperPowers 的 `hooks/hooks.json` 在 `startup|clear|compact` 事件上触发。

### 路径 3: Pi Daemon（SSE MCP 调用）

```python
# pi_daemon.py — 在 execute_task() 之前调用

async def inject_context(task_content: str, domain: str, mcp_client):
    """通过 MCP SSE 调用 auto_context_inject"""
    try:
        from plastic_promise.core.constants import DOMAIN_TO_TASK_TYPE
        task_type = DOMAIN_TO_TASK_TYPE.get(domain, "general")
        result = await mcp_client.call_tool("auto_context_inject", {
            "task_description": task_content,
            "task_type": task_type,
            "source": "pi_agent",
        })
        return json.loads(result[0].text) if result else None
    except Exception:
        return None  # 优雅降级
```

改动面：`pi_daemon.py`，~10 行新增。在 `execute_task()` 调用前插入 `await inject_context(...)`。

## 五、自反馈循环

注入记录通过 `memory_store` 进入记忆池后，形成自我强化循环：

```
第一次注入 (池子空):
  auto_context_inject("修复 JWT 认证 bug")
    → skill_session_start → entity 创建
    → SoulLoop.pre_task_v2 → 检索相关记忆（返回空）
    → memory_store("注入记录: 修复 JWT 认证 bug") ← 沉淀

第二次注入 (相似任务):
  auto_context_inject("修复 OAuth 认证 bug")
    → skill_session_start → entity 创建
    → SoulLoop.pre_task_v2 → 检索相关记忆
        → 命中上次注入记录！← 自反馈
        → 返回上次的原则激活、上下文摘要
    → memory_store("注入记录: 修复 OAuth 认证 bug")
```

注入记录标签结构：
```
tags: ["auto_inject", "source:claude_code", "skill:auto_inject:claude_code",
       "task:done", "domain:general"]
```

可通过以下方式检索注入历史：
- `memory_list(tags=["auto_inject"])` — 列出所有注入记录
- `skill_session_trace(skill_name="auto_inject:claude_code")` — 按源头追溯 Claude Code 的注入链

## 六、CLAUDE.md 启动序列简化

### 当前（5 步）

```
1. principle_activate(task_type="general")
2. memory_recall(query="<当前任务关键词>")
3. system(action="stats")
4. memory_store(content="会话启动：<目标任务>")
5. defense(action="get")
```

### 简化后（3 步）

```
1. auto_context_inject(task_description="<当前任务>", source="claude_code")
2. system(action="stats")
3. defense(action="get")
```

`auto_context_inject` 内部已包含 principle_activate + memory_recall + memory_store（替代原步骤 1、2、4），同时**额外**完成 skill_session_start 追踪和注入记录沉淀。

### 与 Skill 调用协议的关系

| | auto_context_inject | skill_session_start |
|------|------|------|
| 时机 | 会话启动 | 每次 Skill 调用前 |
| 范围 | 整个会话 | 单个 skill 执行 |
| 追踪 entity | `skill:auto_inject:claude_code:...` | `skill:brainstorming:...` |
| 调用方式 | Hook 自动 | CLAUDE.md 指令手动 |
| 互补性 | 覆盖启动上下文 | 覆盖 skill 内追踪 |

两者互补，不冲突。`auto_context_inject` 不替代 `skill_session_start`——前者管会话级上下文，后者管 skill 级追踪。

## 七、实现文件清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `plastic_promise/mcp/tools/context.py` | +90 行 | 新增 `handle_auto_context_inject` handler（含 principle 降级保底 + content 完整原文保留） |
| `plastic_promise/mcp/server.py` | +15 行 | 注册 `auto_context_inject` 工具 + call_tool 路由 |
| `plastic_promise/mcp/tools/skill_tracking.py` | +20 行 | `auto_inject:` 前缀: domain→reflecting, 跳过 parent 校验, 孤儿检测自动忽略 |
| `bridge/soul_bridge.py` | ~15 行替换 | `pre_task()` 改为调用 handler（Python 直接调用） |
| `pi_daemon.py` | +10 行 | `execute_task()` 前调用 `auto_context_inject` |
| `hooks/session-start` | +3 行 | 追加 `claude mcp call auto_context_inject`（含 scope:"agent:claude"） |
| `CLAUDE.md` | 替换启动序列 (~10 行) | 5 步 → 3 步 |
| `tests/test_auto_context_inject.py` | **新增** (~150 行) | 测试 handler + 自反馈循环 + 三条路径 |

**总计**：~300 行新增/替换，零新依赖，零新存储结构，MCP 工具总数：34（33 → 34）。

## 八、非功能性约束

- **零新存储结构** — 全部复用 MemoryRecord + entity graph + tag
- **优雅降级** — 任何内部组件失败不阻塞，返回降级标记
- **向后兼容** — SoulBridge.pre_task() 返回结构不变
- **不修改 SuperPowers** — 所有逻辑在 Plastic Promise 侧
- **MCP 工具是唯一入口** — 不新增 CLI 命令
- **深度绑定** — 注入记录自身进入记忆池，形成自反馈循环
- **原则对齐** — 不违背任何 12 条核心约定。奥卡姆剃刀（零新实体）、约定优于约束（信息供应非门禁）、上下文驱动决策（自动化上下文供给）、器官互保（统一入口形成网状防护）
