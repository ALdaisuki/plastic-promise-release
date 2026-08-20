# 被动记忆闭环实施说明

## 1. 先做什么

本项目不需要先引入 LangGraph。当前已有 `SoulLoop`、`SkillEngine`、MCP tools、`auto_context_inject`、`smart-remember` 和 closure runner，最小变更是补一个内部事件协调器，再为各接入面提供 adapter：

```text
provider hook/node -> PassiveMemoryCoordinator -> existing PP primitives
```

建议新增模块：

- `plastic_promise/passive_memory/events.py`：事件 dataclass、schema、scope 绑定；
- `plastic_promise/passive_memory/coordinator.py`：before/after 编排、队列、熔断、metrics；
- `plastic_promise/passive_memory/adapters.py`：Claude/Pi/LangGraph/MCP adapter；
- `plastic_promise/passive_memory/policy.py`：候选记忆分层、proposal/trust/project gate；
- `plastic_promise/passive_memory/metrics.py`：被动漏斗和 preload quality 记录。

## 2. Before Hook

```python
async def before_invoke(event):
    scope = build_request_scope(event.to_args(), "passive_context")
    decision = await governance_check(scope, event)
    if decision.skip:
        record_event("passive_context_skipped", scope, decision.reason)
        return MinimumContext(status="skipped")

    pack = await context_supply(
        task_description=event.task_description,
        task_type=event.task_type,
        project_id=scope.project_id,
        project_policy=scope.project_policy,
        response_mode="compact",
        stage_session_id=scope.stage_session_id,
        flow_line_id=scope.flow_line_id,
        request_id=scope.request_id,
    )
    context_text = render_compact_context(pack, byte_budget=budget_for(event.task_type))
    record_preload_span(scope, pack)
    return ContextInjection(text=context_text, memory_ids=pack.memory_ids)
```

关键点：

- 同一 `request_scope_id` 做短 TTL cache，避免 session-init、auto_context_inject、context_supply 三次重复检索；
- Hook 只返回注入内容和少量元数据，完整诊断留在 trace；
- 注入失败只返回最小安全上下文，不阻塞主 LLM 调用；
- 记忆边界明确标识为参考资料，不得覆盖系统/用户指令。

## 3. After Hook

```python
async def after_invoke(event):
    if not policy.should_capture(event):
        return
    candidate = extract_candidate(event)
    if candidate is None:
        record_event("passive_candidate_skipped", event.scope, "low_value")
        return
    await bounded_queue.put(candidate)
```

队列消费者调用现有 `smart-remember`，而不是直接调用 SQLite：

```text
candidate
 -> source/project/trust/proposal gate
 -> smart-remember
 -> existing dedupe/classification/quality pipeline
 -> relation/conflict enrichment
 -> canonical SQLite + memory_lineage
 -> store_outbox
 -> LanceDB vector/FTS derived update
```

写入必须异步、有最大耗时和最大队列深度；队列满时只记 `passive_write_dropped`，不能影响用户响应。`step-closure` 仍是最高可信度路径，Hook 自动抽取的偏好和事实默认低置信度或进入 proposal。

## 4. 存储时增强

当前项目已有去重、分类、worth、版本和 outbox。新增关系整理时，建议保持一个事务边界：

1. 根据 canonical memory 查找 top-N 相似/实体命中候选；
2. 计算 `related_to`、`supersedes`、`conflicts_with`；
3. 冲突不静默覆盖，写入 proposal/contested 状态与 `memory_lineage`；
4. 同一事务写 canonical 记忆、关系元数据、call span；
5. 提交后只通过 outbox 更新 LanceDB。

不要把 LanceDB 当冲突真相源；索引损坏时允许从 SQLite 重建。

## 5. 检索与 rerank

当前 `ContextEngine` 已有 `bm25`、`vector`、`fts`、`graph`、RRF/fusion policy 和 retrieval explain。实施时只增加一个可选 rerank 阶段：

```text
candidate windows -> RRF/fusion -> deterministic top-N rerank -> layer/gate -> prompt
```

第一版 rerank 特征：实体精确命中、标题/标签覆盖、项目匹配、来源可信度、新鲜度、冲突惩罚、worth。候选排序必须在 trace 中记录 `rerank_config_hash`，但普通 `context_supply` 不返回逐项分数。

第二版才评估 cross-encoder/LLM rerank，并且必须受 runtime/feature flag 控制；没有真实模型时不允许把 fallback benchmark 当发布质量结论。

## 6. Hook 映射

| 接入面 | Before | After | 备注 |
|---|---|---|---|
| Claude Code | `UserPromptSubmit` | `Stop`/post-turn | 只做 adapter，PP 内核不依赖 Claude |
| Pi Agent | `pre_task_v2` | `post_task`/closure runner | 可直接复用现有 SoulLoop |
| LangGraph | `memory_retrieve` node | `memory_write` node | 只在外部集成时启用，不让其成为核心依赖 |
| MCP 客户端 | `auto_context_inject` | `step-closure`/`smart-remember` | 保持兼容旧工具调用 |
| Daemon | 任务领取前 | 任务完成后 | 必须带 agent/project/session 标签 |

## 7. 评测实现

先复用 `tests/fixtures/recall_quality` 和现有 `recall_quality` 计算：

- 固定 `dataset_revision`、`corpus_hash`、模型/维度、runtime/warmup/repeat；
- 对 before hook 记录 `relevant_memory_ids`、注入 memory IDs、context budget；
- 对 after hook 记录写入 memory ID、首次可召回时间、重复抑制与关系结果；
- 统一写入 `call_spans.metadata_json`，后续 dashboard 聚合。

最低门禁：

| 指标 | Phase A 门槛 |
|---|---:|
| `preload_hook_success_rate` | ≥ 99%（允许明确 skip） |
| `passive_write_enqueue_success_rate` | ≥ 99% |
| 主调用额外 p95 延迟 | before ≤ 300 ms（命中缓存除外）；after 不阻塞 |
| `passive_write_recall_rate@5` | ≥ 95%（固定窗口内） |
| `duplicate_suppression_rate` | ≥ 80% |
| `conflict_false_positive_rate` | ≤ 10% |
| project isolation violations | 0 |
| untraceable passive writes | 0 |

检索质量继续使用 `hit@k`、MRR、forbidden hit、p50/p95、fallback/degradation；任何 rerank 变更必须通过必需 split，不只看 deterministic benchmark。

## 8. Feature flags 与回滚

当前实现使用以下开关：

```text
PP_PASSIVE_CONTEXT=on|shadow|off
PP_PASSIVE_CONTEXT_MAX_CHARS=1000
PP_PASSIVE_MEMORY=on|shadow|off
PP_PASSIVE_MEMORY_MAX_QUEUE=256
PP_PASSIVE_MEMORY_MAX_ATTEMPTS=5
PP_PASSIVE_MEMORY_RETRY_BASE_SECONDS=2
PP_PASSIVE_MEMORY_RETRY_MAX_SECONDS=300
PP_PASSIVE_MEMORY_PROCESSING_TIMEOUT_SECONDS=300
PP_MEMORY_PROPOSALS=on|shadow|off
PP_BM25_PRESERVATION=1
PP_BM25_PRESERVATION_THRESHOLD=0.72
PP_BM25_PRESERVATION_LIMIT=2
```

### Codex hook adapter

`.codex/hooks.json` registers three project hooks:

| Codex event | Adapter action | Persistence |
|---|---|---|
| `UserPromptSubmit` | Call preload only when context is non-`off`; save turn state only when capture and proposals are non-`off` | Ephemeral turn JSON only when capture can run |
| `Stop` | Join and call capture only when capture and proposals are non-`off`; otherwise discard matching temporary state | Governed outbox/proposal path |
| `SessionEnd` | Remove remaining temporary state for the session without calling MCP | No long-term write |

The adapter is `plastic_promise.passive_memory.codex_hook`. It uses the existing Streamable HTTP MCP endpoint, accepts an optional bearer token from the environment, never shells out to Codex, and always returns `continue=true` on transport, timeout, state, or payload failures. Project hooks use `.venv/bin/python` or `.venv\\Scripts\\python.exe`, so they cannot accidentally load the package under an unsupported system interpreter. The default state path is `var/codex-hooks`, so Windows development in this repository does not place hook state or databases on `C:`.

Hook correlation deliberately avoids `transcript_path`. Secret-shaped prompts are redacted before either the atomic temporary file or MCP call. The remaining prompt is retained only until the matching `Stop` succeeds or the state TTL expires. The directory is owner-only, each file is mode 0600, and bounded cleanup advances a persisted cursor even when a batch contains no expired file. Assistant output is sent as audit context but is not promoted as user-authored memory content by the passive-memory coordinator.

On macOS, `scripts/manage_codex_hook_cleanup_launchd.py install` writes and loads a per-user LaunchAgent. It runs `--cleanup-states` at load and every 15 minutes with the project `.venv`, an absolute state path, no provider credential, and no network call. `uninstall` boots out the job and removes its plist. This timer is independent of the Maintenance Daemon and remains safe while that daemon is disabled.

### Dashboard phase 2

Dashboard V2 now projects the passive loop through two project-scoped views:

- `GET /api/dashboard/v2/passive-memory` returns bounded context-injection and capture events, proposal/outbox status, oldest active outbox age, and trace-only live recall quality aggregates.
- `GET /api/dashboard/v2/memory-proposals` returns a cursor-bound proposal queue filtered by server-owned project scope, status, and category.

The dashboard remains read-only unless `PP_DASHBOARD_REVIEW_ACTIONS=1`. When the exact gate is enabled and the HTTP server supplies the review adapter, `POST /api/dashboard/v2/memory-proposals/{proposal_id}/review` accepts only same-origin JSON with `X-PP-Dashboard-Action: proposal-review-v1`. The route first proves that the proposal is visible in the active dashboard project, then delegates to `feedback_apply`. Actor, call ID, project authority, trust score, trust tier, and defense decision are generated by the MCP server and cannot be supplied by the browser.

Proposal adoption therefore reuses the existing atomic promotion, canonical SQLite write, lineage, relationship organization, and index-outbox path. Rejection reuses the existing evidence requirements and content redaction. Disabling the dashboard review gate removes the POST route but preserves all proposal, outbox, lineage, and trace evidence.

`PP_PASSIVE_CONTEXT`、`PP_PASSIVE_MEMORY` 和 `PP_MEMORY_PROPOSALS` 均默认 `off`。上下文只有在 `shadow|on` 时才调用 MCP；只有被动捕获与 proposal 两个门都非 `off` 时才保存短期 turn state 并允许 Stop 调用 MCP。关闭捕获后，Stop 和 SessionEnd 仍会清理已有临时状态，但不会调用 MCP。两者必须同时为 `on` 才会把明确用户事实送入 durable outbox 和审核队列。

回滚顺序：

```text
PP_PASSIVE_CONTEXT=off
PP_PASSIVE_MEMORY=off
PP_MEMORY_PROPOSALS=off
PP_BM25_PRESERVATION=0
PP_DASHBOARD_REVIEW_ACTIONS=0
```

保留 SQLite 中的 call span、proposal、lineage、冲突和 outbox 记录，不删除控制证据。

## 9. 测试分层

1. `test_passive_memory_events.py`：事件 schema、scope、幂等 key、敏感字段裁剪；
2. `test_passive_context_hook.py`：compact 注入、预算、cache、degraded fallback、项目隔离；
3. `test_passive_memory_hook.py`：队列满/超时/重试、候选分层、主调用不阻塞；
4. `test_memory_relation_enrichment.py`：related/conflict/supersedes 与 lineage；
5. `test_passive_metrics.py`：漏斗、hit@k、MRR、write recall、outbox lag；
6. live smoke：`before -> LLM/tool simulation -> after -> recall -> context`，再跑 `step-closure`。

## 10. 与当前响应精简工作的结合

被动调用必须默认使用 `response_mode=compact`，否则 `context_supply(debug=true)` 会把约 138 KB 检索诊断灌回模型。完整 audit、channel rankings、per-item stats 只写 trace 或通过 `diagnostics.ref` 提供。这样“被动注入”与“响应精简”是同一个控制面，而不是两套互相放大的输出。

## 11. 六 PR 待办：Hook 项目作用域修复

这次修复登记为后续六 PR 的共同基础项，当前只在本地完成，不改变服务器状态：

- **PR1 — 作用域与身份契约**：Hook 从 `cwd`、`working_directory`、`workdir`、`workspace_root` 或 `project_root` 读取 workspace；缺少 workspace 且没有显式项目 ID 时必须 fail closed 为 `project:unknown`，不得回退到固定运行仓库。
- **PR2 — workflow/request scope/receipt 隔离**：将项目摘要纳入 Hook call ID、turn-state 文件名和显式 `request_scope_id`，防止同一 session/turn/request 在不同项目间碰撞；turn state 持久化 `project_id` 并在读取、SessionEnd 清理时校验。
- **PR3 — memory lifecycle 与跨项目迁移**：继续把 project scope 作为 canonical 写入和召回的硬过滤；跨项目共享只能通过有证据的派生/共享关系迁移。
- **PR4 — 知识域与自循环治理**：域命名、关联、晋升、冲突和过时维护都必须携带 project scope，派生索引不能扩大可见范围。
- **PR5 — Hook/Agent 治理链隔离**：完整开发链、skills、Pi/其他 Agent adapter 的 stage、proposal、outbox 和 closure 证据全部继承项目作用域；shadow/degraded 路径也要保留同一 scope。
- **PR6 — 隔离指标与发布保护**：增加 project-isolation violations、cross-project forbidden-hit、scope collision、unknown-project 降级率指标，并以 shadow/canary、备份和可回滚配置作为生产门禁。

当前实现将 turn state schema 升级为 `codex-hook-turn-v3`。旧 state 文件只会在 TTL/清理流程中逐步淘汰，不会被新项目读取为当前项目状态。

当前已落地的下一阶段最小垂直切片见
[`project-scope-security-finding-contract.md`](../project-scope-security-finding-contract.md)：
`SecurityFinding` 已以纯领域对象实现项目作用域、DeepSec finding 安全状态/新鲜度双轴、
accepted-risk 期限、秘密拒绝和不可变 transition；`ShieldScanStore` 已复用
`DerivedWorkStore` 落地本地 SQLite durable outbox、项目/扫描修订分区批领、租约 fencing、
有界重试、原子 finding-version 完成和 lineage 作用域校验。remediation/rescan/closure
已增加本地证据门禁：`resolved` 只能由新扫描修订的 `rescan_passed=true` 生成，仍存在的
finding 记录为 `recurring`。在此基础上，低/中风险且新鲜的修复模式会进入独立的
`security_remediation_candidates` ledger；候选默认是 `pending_validation`，只有来自不同项目的
同 finding/同脱敏模式/已通过 rescan 的证据才能标记 `shadowed`，并且不会直接写入 canonical
memory。candidate 通过本地 canary 后，才可由
`ShieldCandidateProposalBridge` 生成一个带 server provenance 的 `system` 影子提案，进入既有
`ProposalAutomation` 评分投影；该提案仍是 `pending`，不会自动 adopt，也不会写入 canonical
memory。失败 canary 会保留 candidate 并转为 `rolled_back`，记录有限的失败原因和指标。Dashboard
新增只读的项目级 candidate projection，所有 evidence 在投影时继续脱敏。DeepSec 执行 worker、
向量证据计算、generation promotion 仍按六 PR 顺序推进，尚未接入生产，也未执行生产数据库
迁移、服务重启或 Maintenance 启动。
