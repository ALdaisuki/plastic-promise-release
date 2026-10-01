# 工具面审计 · 2026-10-02

> 触发：用户观察「提供的工具太多」+「系统只是摆在那里」。
> 方法：`tool_usage_events` 40 天真实遥测（1078 次调用）× `list_tools()` 静态声明面 × handler 绑定普查，三方对账。

## 结论摘要

| 指标 | 数值 | 判读 |
|---|---|---|
| 声明工具 | **62** | list_tools 实际暴露 |
| 被真实调用过 | **16** | 25.8% |
| **零调用** | **46** | **74.2% 是死面** |
| 40 天总调用 | 1078 | 覆盖 30/39 天 |
| 其中 hook 驱动 | **949（88.0%）** | auto_context_inject 664 + audit_pre_check 285 |
| 仪式类 | 83（7.7%） | step-closure / session-init / scarf / sp-stage |
| **记忆业务** | **16（1.5%）** | memory_store 9 + context_supply 7 |
| 协作/任务 | 4（0.4%） | 猎人公会 40 天只被碰过 4 次 |
| 调用者 | 100% `claude` | 无多 agent 流量 |

**一句话**：系统 88% 的流量是「机器跟自己说话」（hook 自动注入 + 审计预检），真正的人机记忆业务占 1.5%，74% 的工具从未被碰过。

## 发现

### F1 · 工具面 74% 是死的（62 声明 / 16 使用 / 46 零调用）
零调用清单（46）：
```
memory_recall, memory_update, memory_forget, memory_list, memory_gc, memory_correct,
memory_reclassify, memory_sync_files, principle_activate, context_inject, context_graph,
mgp_shadow_bridge, feedback_apply, system, runtime_mode, issue_transition, issue_list,
pack_export, pack_import, domain, task_enqueue, task_claim, task_complete, task_verify,
task_inbox, task_heartbeat, task_abandon, collaboration_work_list, collaboration_lease_heartbeat,
collaboration_work_accept, skill_session_start, skill_session_complete, skill_session_trace,
skill_session_audit, skill_auto_track, smart-remember, review_run, knowledge_search,
commercial_audit_export, market_install, market_upgrade, market_remove, market_enable,
market_disable, market_status, audit_rollover
```
被调用的 16（真需求）：
```
auto_context_inject(664) audit_pre_check(285) step-closure(61) audit_run(21)
session-init(16) memory_store(9) context_supply(7) defense(4) sp-stage(3)
scarf_reflect(2) principle_evaluate(1) issue_create(1)
collaboration_work_register(1) collaboration_work_claim(1) collaboration_work_review(1) market_list(1)
```

### F2 · 流量结构：机器自说自话占绝对多数
- auto_context_inject 664 次 = cadence 插件每 5 步注入（自动化主路径，符合设计）
- audit_pre_check 285 次 = 每次写操作前的合规预检（hook 驱动）
- 两者合计 88%：是有意的自动化开销，但**没有一次产生新记忆或改善决策**

### F3 · 记忆业务面几乎无使用（16 次 / 40 天）
memory_store 9 次里多次还失败（stored=false，embedding 断链）。**memory_recall 零调用**——检索面从未被主动用过一次。

### F4 · 猎人公会实质未运行
40 天 task_* 七件套调用 **0 次**，仅 collaboration_work_* 三次。但 task_queue 有 **257 条 pending**（198 条 investigate_coupling + 25 条空池误派 rebalance_domains）——**派单在产生，消费在枯萎**。

### F5 · 别名让声明面虚胖
session-init/session_init、step-closure/step_closure、smart-remember/smart_remember、sp-stage/sp_stage 双份注册：8 个条目承载 4 个能力。

### F6 · 静默缺陷（维护守护自己报的，无人处理）
```
[critical] embedder_health: returns zero vectors despite not FallbackEmbedder
[high]     principle_injection: Principles are strings, not dicts -- content not injected
[low]      rust_engine_health: 'ContextEngine' has no attribute 'check_rust_health'
[high]     zero_vector_ratio: LanceDB store not initialized
```
principle_injection 失效意味着**原则注入从一开始就是摆设**。

### F7 · schema 与文档漂移
报表脚本按文档查 memory_records / issues / semantic_jobs 三表**均不存在**，但 issue_create 等工具调用成功——实际表名与文档不符。

## 重构建议（按依赖排序）

### T1 · 工具面瘦身（立即，收益最大）
- 保留 16 个真实使用面 + 明确核心能力（memory_store/recall、issue/task 七件套、context_supply、defense）
- 归档 46 个零调用面：不删实现，改为 `PP_TOOL_PROFILE=core` 下不注册；full 档保留
- 别名归一：4 对别名收敛为 canonical 名 + 内部映射，声明面 62 → ~20

### T2 · 积压与噪音治理
- task_queue 257 条 pending：按 task_type + 内容指纹去重后批量关闭（198 条标题全不同 = 指纹失效，需按扫描器维度归并）
- store_outbox 79 条 attempt=8 永久失败：确认 memory-index/v3 是否仍需要，不需要则清

### T3 · 静默缺陷修复（3 项代码缺陷）
- principle_injection 类型修复（str → dict）
- check_rust_health 调用点与实现对齐
- schema 漂移：文档与代码对齐（或补迁移）

### T4 · 计算节点恢复（运维，需网络可达）
- WSL 宿主 192.168.5.6 失联 → 向量链路全灭；恢复后 store_outbox 10 条 pending 自动回放

## 验收指标（重构后）
| 指标 | 当前 | 目标 |
|---|---|---|
| 零调用工具数 | 46 | **0**（归档后声明面 = 使用面） |
| 声明工具数 | 62 | **≤22** |
| 记忆业务占比 | 1.5% | >10% |
| 噪音委托率 | 198 条无内容区分 | <5% |
