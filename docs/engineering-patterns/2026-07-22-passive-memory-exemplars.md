# 被动记忆闭环典范研究

## 研究范围

本研究服务于两条必须一起落地的工作线：

1. 将 `memory_recall`、`context_supply`、`session-init`、`sp-stage` 的普通响应投影为模型真正需要的最小信息；
2. 将推理前记忆预载和推理后记忆捕获内化为 Hook/工作流事件，而不是继续依赖智能体主动调用工具。

源码核验基线：

| 项目 | Revision | 研究重点 |
|---|---|---|
| `justForever17/adaptive-agent-mcp` | `000ec8895d3998522c3283c7d692880f25e68341` | 会话初始化、作用域、混合检索、可选 rerank、图谱 |
| `hack-wu/memory-lancedb-mcp` | `58eaebf7e2437eda457406e89fe289b7ab2947be` | MCP 生命周期桥 |
| `CortexReach/memory-lancedb-pro` tag `v1.1.0-beta.10` | `63495671fde55f2c8e3d6eb95267381d1889cca9` | 自动预载、自动捕获、治理过滤、冲突和关联、rerank |
| `Project-N-E-K-O/N.E.K.O` | `1f51afa5336e879a4da3d994ef4b413427151a69` | BM25 + embedding + RRF、事实去重、反思晋升、事件回放 |

结论先行：Plastic Promise 不应引入第二套记忆引擎。应复用现有 SQLite 真相源、LanceDB 派生索引、`ContextEngine`、proposal、lineage、outbox 和 trace，只吸收生命周期事件、严格上下文预算、存储时关系/冲突状态机以及可量化漏斗。

## 典范一：adaptive-agent-mcp

### Q1：它具体做了什么？

1. `initialize_session()` 把当前时间、全部偏好、可用 scope 和最近两天日志拼成一个启动响应；每份日志超过 500 字符就截断。实现见 `adaptive_agent_mcp/src/tools/session.py:9`、`:76`、`:83`。
2. 它并未实现真正的被动 Hook。自动性主要来自工具描述中的“每次对话必须首先调用”和“任务完成时主动记录”，仍依赖智能体遵守提示。见 `adaptive_agent_mcp/src/tools/session.py:11`、`:29`。
3. `query_knowledge()` 并行使用向量搜索与 SQLite FTS，按 `1 / (k + rank + 1)` 做 RRF；scope 允许“目标 scope + global”，之后可调用 Cohere-compatible rerank 服务重新排序。见 `adaptive_agent_mcp/src/tools/memory.py:293`、`:301`、`:306`、`:309`、`:323`、`:361`。
4. 搜索结果先返回短正文；完整正文另有按 ID 读取路径。这种“先 header/snippet，后正文”的分层减少了普通上下文负担。
5. `GraphStore` 使用 NetworkX 保存实体和显式三元组关系，但 `add_entity`、`add_relation`、`add_triple` 都是调用方主动触发，不是每次存储自动关联。见 `adaptive_agent_mcp/src/graph_store.py:166`、`:203`、`:242`。

### Q2：与 Plastic Promise 有何不同？

- Plastic Promise 已有 `session-init`、`context_supply`、`memory_recall`、项目隔离和 trust gate，不需要复制新的会话初始化工具。
- Plastic Promise 的长期记忆写入受 proposal、来源证据、综合记忆状态和审计约束；“宁可多记录也不要遗漏”会直接放大噪声、隐私和错误事实风险。
- Plastic Promise 已有 EntityGraph、memory lineage 和 outbox，比独立 JSON/NetworkX 图更适合作为关联真相源。
- Plastic Promise 的主要问题不是缺少检索算法，而是普通响应携带完整 audit，以及自动入口仍未统一。

### Q3：适配、重设计、跳过什么？

**适配**

- scope-aware 检索：继续使用 `project_id`、`request_scope_id`、`stage_session_id`，被动 Hook 必须继承这些字段。
- header/snippet-first：`compact` 响应只给 ID、短摘要、最终分数和最小来源；正文与完整诊断按需展开。
- 可选 rerank：只对 RRF 后 top-N 执行，并保留 lexical exact-hit 保护。

**重设计**

- 将“必须先调用初始化工具”改为 adapter 触发的 `before_invoke`。
- 将主动 `append_daily_log/update_preference` 改为 `after_invoke` 异步候选审计；稳定事实、偏好默认进入低置信度或 proposal，不能直接成为 verified 记忆。

**跳过**

- 不复制 Markdown/JSON 作为第二真相源。
- 不采用“宁可多记”的写入策略。
- 不新增独立 NetworkX 图数据库。

## 典范二：memory-lancedb-mcp + memory-lancedb-pro

### Q1：它具体做了什么？

#### 生命周期桥

`memory-lancedb-mcp` 将宿主生命周期显式映射为 MCP 可调用动作：

- prompt 前调用 `triggerAutoRecall()`，聚合各 handler 的 `prependContext`，并返回 `ephemeral: true`，明确禁止把注入上下文当作持久会话内容。见 `src/lifecycle.ts:52`、`:70`、`:87`。
- agent 结束后调用 `triggerAutoCapture()`；注释和实现约定为 fire-and-forget。见 `src/lifecycle.ts:97`、`:109`。
- MCP 层公开 `_lifecycle_auto_recall`、`_lifecycle_auto_capture`、`_lifecycle_session_end`，说明“平台原生 Hook”和“MCP 显式桥”可以共享一套内部事件。

#### 被动预载

核心实现位于 `memory-lancedb-pro/index.ts`：

1. `before_prompt_build` 自动触发检索，整条链有 3 秒超时，避免 embedding/rerank 阻塞主会话。见 `index.ts:2165`、`:2166`。
2. 检索 query 最长 1,000 字符；默认预取 3 条、总计 600 字符、单条 180 字符。见 `index.ts:2195`、`:2207`。
3. 同一记忆默认 8 个 turn 内不重复注入。见 `index.ts:2223`。
4. 只注入 `state=confirmed` 且不在 archive/reflection 层、未被 suppression 的记忆。见 `index.ts:2258`。
5. 注入内容仅使用 L0 abstract，并严格执行条数和字符预算。见 `index.ts:2284`、`:2302`。

#### 被动捕获

1. `agent_end` 在主 Hook 返回后后台执行；失败任务或空消息不处理。见 `index.ts:2421`、`:2428`、`:2433`。
2. 默认只捕获 user 消息，只有 `captureAssistant=true` 才处理 assistant；注入过的 `<relevant-memories>` 和不可信数据 envelope 会先剥离，避免“召回内容再次被存储”。见 `index.ts:2461`、`:826`、`:846`。
3. smart extractor 先做 embedding 噪声过滤，再做六类候选抽取；无持久化结果时才降级到 regex capture。见 `index.ts:2569`、`:2602`。
4. regex fallback 每轮最多保存 2 条，向量相似度大于 0.90 时跳过；新记忆状态为 `pending/working`，不是直接 confirmed。见 `index.ts:2636`、`:2649`、`:2662`、`:2685`。

#### 存储时知识整理

`SmartExtractor` 使用“两阶段去重”：先向量召回候选，再让 LLM 决定 `create/merge/support/contextualize/contradict/supersede`。破坏性决定缺少明确 match 时回退到 create，避免误覆盖。见 `src/smart-extractor.ts:602`、`:678`、`:703`。

- `supersede` 创建新记录，并在两边写 `supersedes/superseded_by` 关系；旧事实保留历史而不是物理覆盖。见 `src/smart-extractor.ts:964`、`:977`。
- `contextualize` 创建 working/pending 记录并写 `contextualizes` 边。见 `src/smart-extractor.ts:1031`、`:1061`。
- `contradict` 更新原记忆的反证统计，另建一条 `contradicts` 记录。见 `src/smart-extractor.ts:1078`、`:1092`、`:1125`。

#### 混合检索与 rerank

`Retriever` 支持 vector + BM25 和第二阶段 rerank。cross-encoder 默认 Jina `jina-reranker-v3`；最终分数为 60% rerank + 40% 原融合分。失败时回退 lightweight cosine，使用 70% 原分 + 30% cosine。见 `src/retriever.ts:731`、`:741`、`:791`、`:836`。

高 BM25 命中不会因 reranker 低分被删除：BM25 ≥ 0.75 和 ≥ 0.6 分别应用更高 preservation floor。见 `src/retriever.ts:859`。相似结果再经过默认 0.85 的 MMR-style 延后处理。见 `src/retriever.ts:1090`、`:1102`。

### Q2：与 Plastic Promise 有何不同？

- `memory-lancedb-pro` 直接以 LanceDB store 承担主要状态；Plastic Promise 明确要求 SQLite 是正文、生命周期、审核和来源真相源，LanceDB 只能重建。
- 其 fallback 去重明确 fail-open；Plastic Promise 对冲突、用户事实和高影响记忆应 fail-closed 或进入 proposal。
- 其 Hook 针对 OpenClaw；Plastic Promise 需要兼容 Claude/Pi/Codex/LangGraph，因此事件契约必须与宿主解耦。
- Plastic Promise 已经有 `step-closure` 这一高可信来源，不能让普通对话抽取与执行者结构化反思同权。

### Q3：适配、重设计、跳过什么？

**适配**

- `before_invoke -> ephemeral compact injection`。
- `after_invoke -> bounded fire-and-forget queue`。
- query、item、总上下文三重预算和预载超时。
- 注入 envelope 剥离、同 turn 去重、confirmed-only 检索。
- `related_to/contextualizes/conflicts_with/supersedes` 的双向 lineage。
- RRF 后 top-N 可选 rerank，以及 exact lexical hit preservation floor。

**重设计**

- Hook 只提交候选；真正写入必须复用 `smart-remember/memory_store`、proposal、SQLite transaction 和 outbox。
- 冲突判断先使用确定性实体/slot/时间规则，再把无法确定的冲突送入 proposal 或 `contested`，不允许 LLM 单独执行破坏性覆盖。
- 后台任务必须有 durable queue/outbox 或至少可观测重试；单纯进程内 fire-and-forget 不能保证进程退出时不丢失。

**跳过**

- 不把 LanceDB 升格为长期记忆真相源。
- 不让 regex fallback 绕过治理直接确认记忆。
- 不采用外部 rerank API 作为默认必需依赖。

## 典范三：Project N.E.K.O

### Q1：它具体做了什么？

#### 混合检索

`memory/hybrid_recall.py` 对 active facts、active reflections 和 archive facts 建立统一候选池：

1. BM25 与 cosine 两路并行；任一路失败时保留另一路结果。
2. BM25 使用标准 Okapi 参数 `k1=1.5`、`b=0.75`。见 `memory/hybrid_recall.py:95`。
3. 每路 top 4，融合后 top 8；cosine 阈值 0.3、BM25 阈值 0.1、RRF `k=60`。见 `config/memory_settings.py:142`。
4. RRF 公式为 `Σ 1 / (k + rank_i)`，按 doc ID 去重。见 `memory/hybrid_recall.py:330`。
5. 它有意不在热路径增加 LLM rerank，以避免额外延迟和不稳定性；RRF 输出保留 `_rrf_score` 供观测。

#### 事实去重

`FactDedupResolver` 的第一层是 hash/FTS exact dedup；只有有可比较 embedding 的事实才进入向量候选。cosine 阈值为 0.85，每个新事实最多取 3 对，每批最多 20 对。见 `memory/fact_dedup.py:39`、`:90`、`:96`、`:102`。

向量只负责提名候选，不直接合并。LLM 决策只有 `merge/replace/keep_both`；实现有 pair 冲突保护，避免同批互相删除或重复消费。见 `memory/fact_dedup.py:19`、`:587`、`:608`。

#### 分层晋升

反思不是创建后立即进入 persona。它经历候选、证据累积、confirmed、promoted/denied 等状态；时间驱动晋升仍需通过 persona 写入约束，角色卡冲突会进入 denied。见 `memory/reflection/promotion.py:284`、`:296`、`:307`。

证据合并使用 `max(reinforcement)` 与 `max(disputation)`，避免重复 witness 被累计放大。见 `memory/_reflection/transitions.py:45`。

#### 可恢复事件链

`EventLog.record_and_save()` 在同一锁内完成 load、append event、mutate、save、advance sentinel。启动时 `Reconciler` 从 sentinel 后回放；未知事件或 handler 失败时立即停止并保留 sentinel，以防跨过因果依赖造成 view 分叉。见 `memory/event_log.py:440`、`:529`、`:548`。

### Q2：与 Plastic Promise 有何不同？

- N.E.K.O 的三层记忆围绕角色/persona 演化；Plastic Promise 还必须处理项目隔离、原则、trust、跨 Agent 审计和治理综合记忆。
- N.E.K.O 的文件事件日志/视图适合其本地结构；Plastic Promise 已有 SQLite transaction、runtime events 和 store outbox，不需要复制文件型日志。
- Plastic Promise 已有更丰富的检索通道和 Rust/Python 双热路径；直接采用 N.E.K.O 的固定阈值不能代替版本化双语语料 live gate。

### Q3：适配、重设计、跳过什么？

**适配**

- BM25 与 vector 独立失败、独立计时，RRF 对缺失通道自然退化。
- 向量只提名冲突/重复候选，不直接执行破坏性动作。
- 反思/事实分层晋升与终态保留。
- 事件 sentinel/outbox 的“失败不越过”语义。
- hit@k、MRR 之外保留候选数、各通道命中、RRF 最终排名和降级原因。

**重设计**

- 把文件 sentinel 映射为 SQLite outbox offset/idempotency key。
- 把 persona 的 promoted/denied 映射为现有 memory proposal、verified/contested/stale 和 lineage 状态。
- 阈值只作为 shadow 初值，必须经过 Plastic Promise 固定双语语料和真实模型验证。

**跳过**

- 不复制文件型 canonical store。
- 不把固定 `0.3/0.1/0.85` 当作生产结论。
- 不在被动预载热路径默认调用 LLM rerank。

## 统一适配方案

### 1. 一个内部生命周期，多个 adapter

定义稳定内部事件：

- `before_invoke`：项目/trust gate → `context_supply(response_mode="compact")` → ephemeral 注入；
- `after_invoke`：剥离注入 envelope → 候选抽取 → bounded async queue → governed store；
- `step-closure`：作为 `after_invoke` 的高可信结构化来源，保持优先级最高。

Claude Hook、Pi/SoulLoop、Codex adapter、LangGraph 节点和 MCP 生命周期桥都只做事件翻译，不复制记忆逻辑。

### 2. 响应投影是被动链路的前置条件

- `compact`：供模型/被动 Hook 使用，只返回决策所需字段；
- `standard`：供人类交互和兼容客户端使用；
- `debug`：完整诊断写 Trace，普通响应只返回 `diagnostics.ref` 和摘要。

若继续把 `audit_metadata`、per-item stats 和镜像 `data` 注入模型，被动检索会把现有 138 KB/295 KB 响应放大到每个 turn，因此不能先做自动化再做投影。

### 3. 存储时知识整理

建议状态决策：

```text
candidate
  -> exact duplicate: suppress / worth update
  -> similar support: related_to + evidence increment
  -> same slot newer fact: supersedes + old stale
  -> unresolved contradiction: conflicts_with + proposal/contested
  -> independent fact: create pending/working
```

破坏性动作必须同时满足：确定性 slot/entity 对齐、来源证据完整、项目 scope 一致；否则只建立关系，不覆盖旧记忆。

### 4. 混合检索与 rerank

保留当前 vector + BM25/FTS + graph + RRF；新增 rerank 只处理融合后的 top-N：

- 默认 `off`；
- `deterministic` 可使用 exact match、slot、时间、worth、来源可信度；
- `model` 必须受 timeout、feature flag 和 fallback 保护；
- 高 BM25 exact hit 设置 preservation floor，避免专有名词被语义模型压低。

### 5. 可观测性

每个 `before_invoke/after_invoke` 共享 `call_id/parent_call_id/request_scope_id`，至少记录：

- 检索：channel latency、candidate count、RRF rank、rerank delta、fallback reason；
- 注入：pre-budget/post-budget item/char/token、dedup filtered、confirmed filtered；
- 写入：candidate/extracted/admitted/proposed/stored/indexed；
- 质量：hit@k、MRR、preload hit@k、write-to-later-recall、duplicate suppression、conflict precision、outbox lag。

完整诊断进入 `call_spans/runtime_events`，MCP 普通响应只返回 summary 和 trace reference。

## 优先级与改动边界

| 优先级 | 改动 | 预估范围 | Feature flag |
|---|---|---:|---|
| P0 | `memory_recall/context_supply/session-init/sp-stage` 响应投影 | 4 个 handler + schema + tests | `response_mode` / `guidance_level` |
| P0 | `before_invoke/after_invoke` 统一事件和 compact 预载 | adapter + Hook tests | `PP_PASSIVE_CONTEXT` |
| P0 | 移除 `auto_context_inject` 将注入快照写回长期记忆 | 1 个 handler + regression test | 无，修复自污染 |
| P1 | store 后关系、冲突、标签 enrichment | quality pipeline + lineage tests | `PP_MEMORY_ENRICHMENT` |
| P1 | BM25 强命中跨 rerank/MMR 保留；托管精排保持显式启用 | retrieval pipeline + live gate | `PP_BM25_PRESERVATION*` / `PP_RERANK_PROVIDERS` |
| P1 | passive-memory trace 与 hit@k/MRR/forbidden-hit 指标 | public call trace + tests | 请求级 `relevance_labels` / `response_mode=debug` |

LangGraph 仅提供 adapter：`before_model -> before_invoke`，`step-closure/after_model -> after_invoke`。第一阶段不增加核心运行时依赖。

## 质量审核

- [x] 所有项目均核对源码 revision，不依赖 README 推断实现。
- [x] 记录了精确默认值、阈值、公式、状态和失败语义。
- [x] 区分“可直接适配、需治理重设计、应跳过”。
- [x] 未建议新增第二真相源或第二套记忆引擎。
- [x] 将响应精简与被动注入作为同一控制面处理。
- [x] 明确自动捕获不能回存已注入上下文。
- [x] 明确 deterministic benchmark 不能替代真实模型 live gate。