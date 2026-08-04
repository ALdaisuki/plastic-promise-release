---
title: MCP 工具响应投影与调试分层典范研究
date: 2026-07-21
status: reviewed
scope: MCP tool response payloads
---

# MCP 工具响应投影与调试分层典范研究

## 问题

Plastic Promise 的 MCP 工具会直接把格式化 JSON 放入 `TextContent`。一次对运行中 58 工具服务的隔离链路测量显示：`memory_recall` 的普通响应为 294,760 UTF-8 字节，`context_supply(debug=true)` 为 137,740 UTF-8 字节；前者把多数业务字段同时置于顶层和 `data`，后者把可供 Agent 使用的 prompt、原始条目和检索诊断一次性返回。

本研究只讨论响应表达，不改变检索、审计、持久化或治理语义。外部网络 DNS 在本次环境中不可用，以下结论均从本地已安装的成熟实现源码验证。

## 典范一：MCP Python SDK 1.28.1

### Q1：它具体做了什么？

MCP 的 `CallToolResult` 在 `.venv/Lib/site-packages/mcp/types.py:1363` 中将 `content` 与 `structuredContent` 定义为并列输出通道；`.venv/Lib/site-packages/mcp/types.py:1110` 说明 `structuredContent` 应符合工具的 `outputSchema`。

低层服务在 `.venv/Lib/site-packages/mcp/server/lowlevel/server.py:498` 处理工具输出：直接返回 `dict` 时，第 554–557 行会同时生成 `structuredContent` 和格式化 JSON `TextContent`；返回 `(content, structured_content)` 时，第 551–553 行允许调用方分别控制两个通道；第 565–575 行在定义 `outputSchema` 时验证结构化输出。

可复用模式是：使用受 schema 约束的结构化主结果、只把一行摘要放入文本通道、绝不把完整 JSON 同时复制到两个通道。

### Q2：与 Plastic Promise 有何不同？

Plastic Promise 的 handler 普遍返回 `list[TextContent]`，例如 `plastic_promise/mcp/tools/context.py:352` 和 `plastic_promise/mcp/tools/memory.py:836`。它兼容旧客户端，但无法利用 `outputSchema`，也没有区分 Agent 的决策结果和操作者诊断。直接改为返回 `dict` 会触发 SDK 的自动文本复制，反而增加传输量。项目还必须保留项目隔离、审计追踪和请求作用域。

### Q3：应适配还是跳过？

- **适配**：统一返回 `(summary_text, structured_payload)` 或显式 `CallToolResult`；文本只保留状态、计数、关键 ID 与诊断句柄。
- **重新设计**：定义带版本的 `outputSchema`，用 `response_mode=compact|standard|debug` 明确选择投影。
- **跳过**：不让现有 handler 直接返回 `dict`，因为 SDK 会把完整 dict 再序列化到 `TextContent`。

## 典范二：Pydantic 2.13.4 序列化投影

### Q1：它具体做了什么？

`pydantic.BaseModel.model_dump` 在 `.venv/Lib/site-packages/pydantic/main.py:427` 定义 `include`、`exclude`、`exclude_unset`、`exclude_defaults` 和 `exclude_none`。第 455–460 行说明这些选择如何控制字段集合、未赋值字段、默认值及空值；第 479–486 行将选择传入底层序列化器。

模式是“完整内部对象 + 有意图的外部投影”：内部状态可以完整保存，响应则按调用目的呈现。

### Q2：与 Plastic Promise 有何不同？

Plastic Promise 多数载荷是普通 `dict`，并且部分默认值对审计至关重要：`degraded`、`minimum_result` 和 `warnings` 不能因为空就被无条件隐藏。顶层 envelope 和内部 `data` 也可能是兼容性契约。

### Q3：应适配还是跳过？

- **适配**：按用途维护白名单投影；紧凑响应总是保留 `success`、`degraded`、`warnings`、`minimum_result`、`trace.call_id` 和必要的 `request_scope_id`。
- **重新设计**：把 `debug` 变成诊断附加层，不再切换成另一个语义不同的响应形状；诊断应有 `diagnostics_level` 和每通道条数上限。
- **跳过**：不采用通用 `exclude_defaults=True` 作为治理响应策略。

## 对本项目的适配

| 工具 | `compact` 主结果 | 移入 `diagnostics` | 禁止行为 |
|---|---|---|---|
| `memory_recall` | 三层条目的 ID、摘要、相关度、来源；原则名；请求作用域；治理状态 | 通道排名、逐项分数、完整原始证据、预算明细 | 顶层与 `data` 双写同一对象 |
| `context_supply` | Agent prompt、请求作用域与治理状态 | 原始条目、审计元数据、pipeline/per-item stats | 无预算地同时返回 prompt 与可重建它的全文条目 |
| `session-init` | 信任等级、原则 ID/名称、上下文状态、下一调用、阶段 ID | 可用路由目录、GC 详情、组件原始统计 | 同时返回值完全相等的 `context`、`context_status` |
| `sp-stage` | 阶段、结果、下一阶段、所需产物 | 完整派发模板、历史回放、全部路线说明 | 顶层 `stage` 与 `data.stage` 重复 |

建议契约：

```json
{
  "schema_version": "response-v1",
  "success": true,
  "data": {"...": "tool-specific compact fields"},
  "governance": {
    "degraded": false,
    "warnings": [],
    "minimum_result": "",
    "trace": {"call_id": "...", "request_scope_id": "..."}
  },
  "diagnostics": {"level": "debug", "ref": "..."}
}
```

`standard` 保留当前 schema（但移除严格相等的重复副本），`compact` 面向 Agent 调用，`debug` 只在显式传入时附加受限诊断。大体积原始证据应通过 `diagnostics.ref` 关联到审计/商业导出，不进入每轮模型上下文。

## 质量审核

- 两项典范均给出本机可读源码路径与行号。
- 明确指出 MCP SDK 直接返回 `dict` 的文本复制行为，避免错误迁移。
- 紧凑投影仍保留降级、警告、最小结果、调用与请求作用域信息。
- 每个工具可在同一输入下比较 `standard` 与 `compact` 的字节数、字段覆盖与关键决策等价性。
