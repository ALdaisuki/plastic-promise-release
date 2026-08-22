# 协作子系统架构 (plastic_promise/collaboration/)

> 本文由 2026-08-22 架构漂移审计产出。起因：该子系统以单提交进入发行仓
> (926a1d4, PR#1 sanitized sync) 后无任何叙事文档，长期运营 Agent 对其零认知。
> 权威规范源仍是 `docs/standards/union-six-pr-contract.json`（六 PR 联合交付线）；
> 本文是其协作切片的可读投影，两者冲突时以合同 JSON 为准。

## 定位

25 文件的完整多 Agent 协作底座，实现「主 Agent 只对用户负责、可下发次 Agent、
次 Agent 可再下发、全程预算受控」的组织模型。它是 ProjectWorkBoard
(`collaboration_work_*` 七个 MCP 工具) 的实现层，与 legacy Task Queue
(`task_queue` 表, pending→claimed→executing→done→verified) 是**两套不得互相
代换的状态机**。

## 组件地图

| 模块 | 核心类 | 职责 |
|---|---|---|
| `coordinator_supervisor.py` | `CoordinatorSupervisor` (:785) | 审计四个证据端口 (activity / coordinator / lease+event+git_diff / result_receipt)，`dispatch_eligible` 只消费验证过的完成证明后才下发就绪工作；`max_work_items` 硬上限 |
| `lease_contract.py` | `WorkItem` `WorkLease` `LeaseFence` `LeaseHeartbeat` `LeaseCompletion` | 租约族：fence 令牌防并发抢占，心跳保活，完成需对账 |
| `role_assignment.py` | `RoleAssignmentAuthority` `VerifiedRoleAssignment` | 角色能力合同：Agent 身份由服务器绑定到版本化 role/tool/capability policy；被委托者不得自授 coordinator/reviewer 可见性、不得验收自己的 finding |
| `acceptance_receipt.py` | `ServerAcceptanceSourceRegistry` `ReviewReceipt` | 验收权威：只有服务器认证的 AcceptanceReceipt 能把提交变成 Accepted Work |
| `coordination_plan.py` + `durable_coordination_plan_store.py` | `CoordinationPlanActivation` | 计划激活（plan_sha256 + mandate_digest 绑定，revision 化） |
| `context_projection.py` | `CollaborationContextBudget` (:107) | **预算控制**：max_items ≤64、collaboration 切片字节上限、响应总字节硬顶，越界即抛错 |
| `durable_runtime.py` + durable_*_store.py | — | SQLite 持久层（canonical 单写者） |
| `event_log.py` `activity_update.py` `awareness.py` | — | 事件溯源与活动流 |

## 工作状态机 (durable_runtime.py)

```
proposed → ready → leased → in_progress → submitted → reviewing
    ↑                                                    ↓
    └────────────── rework ←────────────────────── accepted | expired
claimable: proposed/ready/rework · claim 目标: leased/in_progress · 结果源: leased/in_progress
```

## MCP 面 (7 工具)

`collaboration_work_register / claim / heartbeat / complete / review / accept /
list` + `collaboration_lease_heartbeat`。

**当前状态: deferred** —— 服务端返回
`durable_collaboration_authenticated_binding_required` (`server.py:3093`)。
激活前提是建立 durable collaboration 认证绑定（传输身份 ↔ coordination session
↔ project scope），这是六 PR 合同 U6-INV-05（严格项目与会话作用域）的实现。

## 与其他层的边界

- **legacy Hunter Guild**: 兼容层，过渡期保留；正确性路径始终是按项目 cursor
  拉取并校验服务器状态。
- **DSH 子 Agent**: 会话内执行通道（见 CLAUDE.md 进度心跳协议）。协作子系统
  派发的是"工作项"，执行体可以是 DSH 子 Agent——两层正交。
- **sp-stage**: 过程治理 receipt 链，与本子系统的 AcceptanceReceipt 是不同凭证。


## 生产激活环境契约 (2026-08-22 实测沉淀)

协作面从"代码存在"到"实际通电"需要四层全部就位, 缺一层即静默降级:

| 层 | 配置 | 缺失症状 |
|---|---|---|
| 进程身份 | `PP_MCP_RUNTIME_ACTOR=claude` (launchd plist) | `task_runtime_actor_unconfigured` |
| Durable schema | `scripts/collab_schema_migrate.py <canonical-db>` | `durable_collaboration_schema_missing` |
| 健康门禁 | `PP_HEALTH_ALLOW_TEXT_ONLY=1` (env 文件) | `retrieval_lancedb_live_index_blocked` (store_outbox 有 failed/pending 时) |
| Supply 路径 | `PP_FORCE_PYTHON_SUPPLY=1` (env 文件) | `rust_runtime_identity_unavailable` (无编译 Rust 扩展时) |

关键陷阱:
- launchd 改 plist 后必须 bootout+bootstrap; kickstart -k 不重读配置。
- runtime-checkout 的 `run_canonical_runtime.py` 会用 `runtime/plastic-promise.env`
  **覆盖** plist 环境变量 —— 该文件才是运行时环境的权威源。
- AgentSession presence 120s 过期翻 stale。重注册(同 transport 确定性 session_id,
  身份已验证)是唯一合法复活通道: heartbeat 与旧 UPDATE 都拒绝 stale。
- 验收子任务自 af30484 起可由长老直接验收或打回（pending+verify_task 在预检与
  两分支 CAS 均放行）；普通任务仍必须先 done 才能验收。旧『需先 claim』死锁表述已废弃。
- 打回验收子任务会生成 task_type 继承的重派孙委托（escalation_count++，超限升级给 claude）。
- 子任务信任归因回退 payload.original_agent；无目标时响应带 skipped_reason=no_trust_target。

