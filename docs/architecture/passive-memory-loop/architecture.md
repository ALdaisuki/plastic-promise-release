# Plastic Promise 被动记忆闭环架构

## 1. 目标

把记忆从“智能体主动调用的工具”升级为工作流基础设施，同时保持 Plastic Promise 的治理边界：

- 推理前自动预载上下文，注入调用方自己的系统提示/上下文槽位；
- 推理后异步捕获高价值事实、偏好、决策、行动结果与反思，经过 `smart-remember` 质量管道后再写入；
- 写入时完成去重、标签、关联边、冲突候选与来源证据绑定；
- 继续使用现有向量、BM25、FTS、图遍历与 RRF，不重复建设第二套记忆引擎；
- 用 trace、命中指标和被动链路健康指标验证“自动化真的有效”。

非目标：第一阶段不引入 LangGraph 作为运行时依赖，不让被动写入绕过 `memory_proposals`、trust、project isolation 或审计，不把所有对话原文永久存储。

## 2. 现状盘点

| 能力 | 当前实现 | 判断 |
|---|---|---|
| 推理前检索 | `SoulLoop.pre_task_v2`、`auto_context_inject`、`context_supply` | 已有，被动入口需要统一 |
| 推理后闭环 | `SoulLoop.post_task`、`step-closure`、`closure_runner` | 已有，需增加统一 Hook 事件与自动捕获 |
| 智能写入 | `smart-remember`、`memory_store` quality pipeline、去重/版本/outbox | 已有，冲突/关联需显式成为可观测产物 |
| 混合检索 | LanceDB vector + BM25 + FTS + graph + RRF/fusion policy | 已有，需把 rerank 作为受控可插拔第二阶段 |
| 关联图 | `EntityGraph`、`memory_lineage`、behavior graph | 已有，需在 store 后统一写入 `related_to`/`conflicts_with` |
| 追踪 | `call_spans`、`runtime_events`、`degradation_events`、Dashboard V2 | 已有；Dashboard V2 已提供 project-scoped 被动注入/捕获、proposal/outbox、积压时长与最近 Trace 汇总 |
| 评测 | `recall_quality` 已有 hit@k/MRR/p50/p95/fallback | 已有；Dashboard V2 已展示 hit@1、hit@5、MRR、禁止命中率与耗时，写入后召回等扩展指标仍按阶段推进 |

## 3. 核心架构

```text
LLM/Agent Adapter
  ├─ UserPromptSubmit / BeforeInvoke
  │    └─ PassiveContextHook
  │         ├─ request_scope + project/trust gate
  │         ├─ context_supply(response_mode=compact)
  │         └─ inject into provider-owned system/context slot
  │
  ├─ LLM/tool execution
  │    └─ parent_call_id links all child calls
  │
  └─ Stop / AfterInvoke / StepClosure
       └─ PassiveMemoryHook (async, bounded)
            ├─ extract candidate facts/events (no blind raw transcript write)
            ├─ smart-remember / memory_store
            ├─ dedupe + topic tags + entity links
            ├─ conflict candidates -> proposal/contested state
            ├─ memory_lineage + call_spans + metrics
            └─ outbox -> LanceDB derived index

SQLite canonical state
  ├─ memories / memory versions / proposals / provenance
  ├─ entity graph + lineage
  ├─ call_spans / runtime_events / degradation_events
  └─ store_outbox

LanceDB derived index
  └─ vector + BM25/FTS material, rebuildable from SQLite
```

## 4. 事件契约

被动链路统一使用两个事件，而不是分别实现 Bedrock、Claude、Pi 和 LangGraph 四套逻辑：

```json
{
  "schema_version": "passive-memory-event-v1",
  "event": "before_invoke|after_invoke",
  "call_id": "call_...",
  "parent_call_id": "call_...",
  "request_scope_id": "...",
  "stage_session_id": "...",
  "flow_line_id": "...",
  "project_id": "project:...",
  "actor": "claude|pi|codex|langgraph",
  "task_type": "debugging",
  "user_text": "...",
  "assistant_text": "...",
  "tool_events": [],
  "outcome": "success|failure|cancelled",
  "metadata": {}
}
```

- `before_invoke`：只做轻量请求作用域、信任/项目校验、预载检索与注入；失败时返回最小空上下文并记录 degradation，不阻塞普通任务。
- `after_invoke`：只提交候选记忆到 bounded async queue；队列满、超时或写入失败不能影响主响应。
- `step-closure` 是 `after_invoke` 的高可信来源，优先写入执行者提供的四字段反思；Hook 抽取的未经确认事实走低置信度/提案路径。

## 5. 被动存储策略

### 5.1 采集分层

默认不存完整对话。候选抽取按价值分层：

- L0：用户明确要求“记住”、稳定偏好、项目决策、约束、事实变更；允许进入 `smart-remember`；
- L1：成功/失败结果、根因、可迁移经验、工具链 workaround；优先取 `step-closure`；
- L2：普通闲聊、重复上下文、短期过程日志；仅做 telemetry，不进入长期记忆。

### 5.2 写入管道

`candidate -> policy gate -> project/trust check -> dedupe -> classify -> topic/entity extraction -> relation/conflict analysis -> canonical SQLite transaction -> index outbox -> LanceDB consumer`。

复用现有 `smart-remember` 和 `memory_store`，不要由 Hook 直接写 SQLite。每个候选必须带：`source=passive_hook`、`origin_kind`、`origin_ref/call_id`、`project_id`、`confidence`、`session:<agent>:<id>` 标签。

### 5.3 冲突与关联

- 相似度超过现有去重阈值：更新/合并候选，不新增孤立记忆；
- 同一实体、相反断言或新版本：创建 `conflicts_with`/`supersedes` 候选，并置 `contested` 或 proposal，不能静默覆盖；
- 语义相关但不冲突：创建 `related_to`，同时写 `memory_lineage`；
- `memory_store` 返回的 `memory_id`、版本、outbox ID 和关系数写入 trace metadata，支持重放和审计。

## 6. 被动上下文注入

### 6.1 统一 Hook

`PassiveContextHook.before_invoke(event)` 调用现有 `context_supply`，输入完整 request scope、task type、project policy 和 `response_mode=compact`。返回：

```json
{
  "status": "ready|degraded|skipped",
  "context_text": "...",
  "memory_ids": ["..."],
  "principle_ids": ["..."],
  "trace": {"call_id": "...", "request_scope_id": "..."}
}
```

### 6.2 注入位置

优先级：调用方明确提供的 system/context slot > provider prompt middleware > 用户消息前的内部前缀。禁止把记忆伪装成用户指令；注入文本必须带边界标记、来源和“不覆盖用户/系统指令”的语义。

### 6.3 重复与预算

- 同一 `request_scope_id` 在 5 分钟内复用预载结果；
- 被动注入只使用 compact prompt，不返回 debug 诊断；
- 默认上下文 token/字节预算按 task type 分配，超过预算按 core → related → divergent 截断；
- 注入命中、采用、忽略、冲突过滤结果回写反馈事件，供 worth 与评测使用。

## 7. 混合检索与 Rerank

现有检索已具备 vector、BM25、FTS、graph 和 RRF/fusion policy。实施顺序：

1. 先冻结现有 RRF 配置与数据集，建立被动注入 baseline；
2. 将 rerank 作为 `retrieval_mode` 下的可插拔阶段，只对 top-N 候选运行；
3. 默认使用确定性轻量 rerank（词项覆盖、实体命中、项目/来源/新鲜度、冲突惩罚），不引入新的外部模型依赖；
4. 交叉编码器或 LLM rerank 只在显式 feature flag 和非降级 runtime 下启用；
5. 所有 rerank 决策进入 `pipeline_stats`/trace，不把逐项诊断塞进普通上下文。

## 8. 可观测性与指标

### 8.1 被动链路漏斗

- `passive_before_invocations`：触发预载的调用数；
- `passive_context_injected`：成功注入数；
- `passive_context_degraded/skipped`：降级/跳过数；
- `passive_after_invocations`：触发后处理数；
- `memory_candidates_extracted`、`memory_candidates_accepted/rejected`；
- `memory_write_success/degraded`；
- `relation_created`、`conflict_proposals`；
- queue depth、p50/p95 write latency、outbox lag。

### 8.2 检索质量

沿用现有 `recall_quality` 的 `hit@k`、MRR、forbidden hit、p50/p95、fallback/degradation；新增：

- `preload_hit@k`：预载结果是否包含标注相关记忆；
- `injected_memory_adoption_rate`：后续结果/反馈明确采用的比例；
- `passive_write_recall_rate`：被动写入后在固定窗口内可被检索的比例；
- `conflict_false_positive_rate`：冲突标记经审核后误报比例；
- `duplicate_suppression_rate`：避免孤立重复的比例；
- `context_budget_utilization`：注入预算利用率。

所有指标必须携带 `dataset_revision`、`corpus_hash`、`runtime_mode`、`retrieval_config_hash` 和 `request_scope_id`；benchmark 只做门禁，不直接宣称发布质量。

## 9. 治理与安全

- 被动写入默认 `PP_MEMORY_PROPOSALS=off` 时仍只允许已授权经验路径；公开用户事实/偏好进入 shadow/提案，不得直接成为 verified 记忆；
- 被动注入遵循 project isolation、source class、context gate、trust 和 L0/L1 防线；
- 用户可见的“忘记/纠正/删除”优先级高于 Hook 自动写入；
- 严格禁止 C 盘临时工作树或数据库；工作树、测试输出和持久化均放在 F: 工作区；
- Hook 失败不阻塞主任务，但必须产生 degradation event；连续失败触发熔断和告警。

## 10. 分期实施

### Phase A：统一被动 Hook（最优先）

新增 `PassiveMemoryCoordinator` 与 `before_invoke/after_invoke` 事件适配器；接入 Claude/Code hook、Pi/daemon、现有 `auto_context_inject` 和 `step-closure`。只复用 `context_supply`/`smart-remember`，不改检索算法。

验收：同一调用只触发一次 before/after；主响应不等待 after；call span 可串起 parent/child；失败有 degradation。

### Phase B：预载注入

实现 provider-neutral `context slot` 适配器、compact 投影、request scope cache、token budget；默认 shadow 统计，确认注入位置后再 on。

验收：被动注入成功率、上下文字节预算、项目隔离、关键原则保留率、preload hit@k。

### Phase C：存储时知识整理

把主题标签、实体链接、`related_to`、`conflicts_with`、`supersedes` 结果纳入 store 事务和 outbox 元数据；用户事实使用 proposal/审核状态机。

验收：重复抑制、冲突误报、lineage 可追溯、索引重放一致。

### Phase D：轻量 rerank 与质量门禁

增加确定性 top-N rerank 与固定双语语料评测；再考虑 cross-encoder/LLM rerank。

验收：必需 split 的 MRR/hit@5 不回退，p95 不超过现有门槛，fallback/degradation 不增加。

### Phase E：响应精简

将被动调用默认设为 `response_mode=compact`；debug 诊断改为引用/预算输出，避免把治理资料重复注入模型。

## 11. 明确取舍

- 不直接照搬 AgentDesk 的 LangGraph 节点：当前系统已有 `SkillEngine`、`SoulLoop` 和 MCP 工具链；先做事件协议，LangGraph 只作为一个 adapter；
- 不直接照搬 Bedrock 的 provider-specific hook：采用 `before_invoke/after_invoke` 内核事件，外层分别映射 `UserPromptSubmit/Stop`、Pi hook 和 LangGraph node；
- 不新增第二个向量库或第二套记忆 API：继续以 SQLite 为真相源、LanceDB 为派生索引；
- 不默认启用 LLM 抽取/重排：先确定性、可追踪、可回滚，再开放高成本路径。
