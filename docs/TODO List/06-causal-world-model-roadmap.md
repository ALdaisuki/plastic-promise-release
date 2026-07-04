# 06 — Causal World Model Roadmap

> 目标：把 Plastic Promise 从“记忆系统 / 数字员工雏形”升级为“事件-因果-行动闭环系统”，为未来动态因果理解模型、低空经济运营智能体和行业世界模型打基础。

## Strategic Direction

Plastic Promise 的中长期方向不应直接从“世界模型”开卖，而应走：

```text
可信数字员工
  -> 事件记忆
  -> 因果图谱
  -> 行动回放
  -> 行业运营世界模型
```

优先垂域：低空经济运营智能体。

核心切口：

- 低空任务合规检查
- 任务申请与材料生成
- 巡检 / 测绘 / 应急任务编排
- 飞行任务执行记录归档
- 异常复盘与风险归因
- 运营 SOP 审计
- 多 Agent 分工协同
- 行为可追踪、可回放、可问责

## P0 — Internal Causal Foundation

先在 Plastic Promise 自己的 PR、CI、Agent、生产更新流程里落地因果能力。

### TODO

- [ ] 新增 `Event Memory` 数据模型
  - 记录 `time`, `actor`, `state_before`, `action`, `state_after`, `outcome`, `evidence`, `causal_hypothesis`, `confidence`
  - 与现有 `MemoryRecord` 区分：memory 存经验，event 存发生过的过程
  - 支持从 PR、CI、task_queue、step-closure、production restart 自动采样

- [ ] 新增 `CausalGraph`
  - 在 EntityGraph 之外新增因果边
  - 支持关系：`caused`, `increases_risk_of`, `prevents`, `requires`, `invalidates`, `improves`, `correlates_with`
  - 每条边记录 `confidence`, `evidence_ids`, `last_verified_at`, `counterexamples`

- [ ] 改造 `step-closure` 为结构化因果采样器
  - 保留现有 lesson / improvement / root_cause / optimization
  - 增加结构化字段：`event`, `action_taken`, `state_before`, `state_after`, `causal_links`
  - 每次实质产出后自动生成候选 causal edges

- [ ] 新增 `causal_replay` 能力
  - 输入：`task_id`, `pr_id`, `incident_id`, `commit`, `time_range`
  - 输出：timeline、causal chain、counterfactuals、preventive rules、trust adjustment proposal
  - 先支持内部事件：PR 合并、CI 失败、生产重启、Agent 派发、记忆写入

- [ ] 给 Hunter Guild 增加因果归因信任分
  - 区分 Agent 责任、工具/API 抖动、上游状态错误、用户强制推进
  - 避免“失败就扣分”的粗糙机制
  - 支持 `causal_attribution`: `agent_fault`, `external_fault`, `shared_fault`, `not_fault`, `unknown`

### Acceptance Criteria

- [ ] 能回放最近一次 PR 合并全过程
- [ ] 能解释一次 CI failure 的直接原因和修复动作
- [ ] 能把一次生产重启表示成事件链
- [ ] 至少生成 10 条可审计 causal edges
- [ ] 信任分调整能引用 causal evidence，而不是只引用 outcome

## P1 — Causal Operations for Digital Employees

把内部因果能力包装成“可信数字员工”的核心能力。

### TODO

- [ ] 定义数字员工事件协议
  - 每个 Agent 执行任务时必须产生 `start_event`, `decision_event`, `action_event`, `result_event`
  - 每个事件必须有证据：日志、文件 diff、测试结果、外部 API 响应或人工确认

- [ ] 增加数字员工岗位模板
  - 合规员：检查规则、约束、风险
  - 调度员：拆解任务、安排依赖、派发执行
  - 报告员：生成可审计报告
  - 质检员：验证输出、发现遗漏
  - 维护员：处理失败、重试、恢复服务

- [ ] 建立 SOP -> Event -> CausalGraph 映射
  - SOP 不再只是文档，而是可执行检查点
  - 每次 SOP 执行都产生事件链
  - SOP 失败自动生成 causal hypothesis

- [ ] 增加反事实评估
  - 对关键事件生成 “如果当时不这样做会怎样”
  - 用于事故复盘、流程改进、Agent 训练

### Acceptance Criteria

- [ ] 一个完整任务可由多个数字员工协作完成并产生事件链
- [ ] 质检员能基于 causal graph 指出高风险动作
- [ ] 报告员能生成带证据的复盘报告
- [ ] SOP 执行失败能自动沉淀为下一次预防规则

## P2 — Low-Altitude Economy Vertical MVP

不要先碰实时飞控、自动驾驶、空管核心控制层。先做低风险、高价值的运营辅助层。

### MVP Name Candidates

- 低空任务运营 Copilot
- 无人机巡检数字员工
- 低空经济合规调度智能体
- 低空运营因果记忆系统

### Initial Scenarios

- [ ] 飞行任务申请材料生成
- [ ] 空域 / 天气 / 设备状态检查清单
- [ ] 巡检任务 SOP 编排
- [ ] 任务执行记录归档
- [ ] 异常事件复盘
- [ ] 巡检报告生成
- [ ] 维护记录与故障归因
- [ ] 多 Agent 分工：合规、调度、报告、质检、维护

### Low-Altitude Event Schema

- [ ] `flight_task_created`
- [ ] `airspace_checked`
- [ ] `weather_checked`
- [ ] `device_checked`
- [ ] `battery_checked`
- [ ] `operator_confirmed`
- [ ] `mission_started`
- [ ] `mission_delayed`
- [ ] `mission_completed`
- [ ] `anomaly_reported`
- [ ] `maintenance_required`
- [ ] `incident_reviewed`

### Low-Altitude Causal Questions

系统未来应能回答：

- [ ] 为什么这次任务延误？
- [ ] 哪个前置状态提高了风险？
- [ ] 哪条 SOP 没有被执行？
- [ ] 如果提前换电池是否能避免失败？
- [ ] 哪个设备故障率更高？
- [ ] 哪个操作员 / Agent 在此类任务中更可靠？
- [ ] 哪类天气条件最容易导致取消或延误？
- [ ] 哪个流程节点最常导致人工介入？

### Acceptance Criteria

- [ ] 能模拟一条低空巡检任务事件链
- [ ] 能生成任务前合规检查报告
- [ ] 能生成任务后因果复盘报告
- [ ] 能把异常归因到设备、天气、流程、人或 Agent
- [ ] 能输出下一次任务的预防性建议

## P3 — Industry World Model Layer

当事件量和因果边足够多后，再谈世界模型。

### TODO

- [ ] 建立行业状态空间
  - task state
  - airspace state
  - weather state
  - device state
  - operator state
  - compliance state
  - risk state

- [ ] 建立状态转移模型
  - 哪些 action 会改变哪些 state
  - 哪些 state 会提高失败概率
  - 哪些 intervention 能降低风险

- [ ] 建立运营预测能力
  - 任务延误预测
  - 设备维护风险预测
  - SOP 失败概率预测
  - 人工介入概率预测
  - 合规风险预测

- [ ] 建立行业知识包
  - SOP pack
  - incident replay pack
  - causal rule pack
  - agent role pack
  - compliance checklist pack

### Acceptance Criteria

- [ ] 系统能基于历史事件预测下一次任务风险
- [ ] 系统能提出可解释的干预动作
- [ ] 系统能区分相关性和因果假设
- [ ] 系统能通过反例降低错误因果边置信度
- [ ] 系统能把低空运营经验导出为经验包

## Architecture Mapping

| Capability | New / Existing | Likely Modules |
|------------|----------------|----------------|
| Event Memory | New | `memory/`, `core/event_store.py` |
| CausalGraph | New | `core/causal_graph.py` |
| Causal Replay | New | `mcp/tools/causal.py`, `core/causal_replay.py` |
| Step Causal Sampling | Modify | `mcp/tools/reflection.py`, `core/step_auditor.py` |
| Trust Attribution | Modify | `defense/trust_store.py`, `core/hunter_penalty.py` |
| Digital Employee Roles | New / Modify | `skills/`, `mcp/tools/task_queue.py` |
| Low-Altitude MVP | New vertical layer | `domains/low_altitude/` |

## Implementation Order

1. Event Memory first
2. CausalGraph second
3. step-closure causal sampling third
4. causal_replay fourth
5. trust attribution fifth
6. digital employee role templates sixth
7. low-altitude MVP seventh
8. industry world model last

## Non-Goals

- Do not start with realtime flight control.
- Do not build generic “chatbot digital employee”.
- Do not claim “world model” before event and causal data exist.
- Do not let trust score depend only on final success/failure.
- Do not store causal claims without evidence and confidence.
- Do not make SOP execution untraceable.

## Guiding Principle

The near-term product is not “world model”.

The near-term product is:

```text
可审计数字员工 + 事件记忆 + 因果回放
```

The long-term moat is:

```text
行业事件数据 + 因果图谱 + 可解释行动优化
```
