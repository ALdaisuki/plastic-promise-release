# BUILD PLAN — Plastic Promise 重建计划

> 遵循 SuperPowers 方法论: writing-plans → executing-plans → subagent-driven + TDD → code-review → verification → finishing-branch

## 执行策略

- **Python + Rust 双线并行**：基础设施统一后，Python 恢复功能，Rust 重写核心
- **接口先行**：Rust PyO3 暴露与 Python 一致的 API，上层无感切换
- **P0 嵌入 Phase 2**：原则图谱注入 + Memory Worth 双计数器在上下文引擎中同步实现
- **验收驱动**：每段完成后执行对应验收标准，达标再进入下一段

---

## 一段：基础环境与数据模型统一 (Day 1-2)

### Task 1.1: 恢复常量配置与核心原则定义
- **文件**: `plastic_promise/constants.py` (~150行)
- **内容**: 九大系统名称/阈值、三层防线参数、信任分初始值/衰减率、审计维度权重、11条核心原则
- **验收**: 模块可导入；原则 ID/内容/适用域与崩溃前一致；无硬编码

### Task 1.2: 搭建 Rust crate 并定义核心数据模型
- **文件**: `rust/context-engine-core/src/` 各模块骨架
- **结构体**: MemoryRecord (含 worth_success/failure)、Entity、ContextPack、AuditRecord
- **验收**: crate 编译为 cdylib；结构体经序列化往返测试与 Python 一致；MemoryRecord 含双计数器字段

### Task 1.3: Python/Rust 双向序列化通路
- **依赖**: Task 1.2
- **验收**: 来回转换无损；类型错误给出明确异常而非崩溃

---

## 二段：上下文供应引擎核心 — Rust 实现, Python 薄包装 (Day 2-5)

### Task 2.1: EntityGraph + 原则图谱注入
- **文件**: `rust/context-engine-core/src/entity_graph.rs` (~400行)
- **功能**: 节点/边 CRUD、持久化、多跳遍历、inject_principles(task_type) 自动建立任务→原则关联边
- **验收**: inject_principles 后图遍历自然命中相关原则；5/5 场景至少 4 个命中原则

### Task 2.2: RankFuser + 双通道符号规则
- **文件**: `rust/context-engine-core/src/rank_fuser.rs` (~250行)
- **功能**: RRF 融合排序、符号规则同时匹配任务描述和记忆内容
- **验收**: top-K 结果确定性；规则触发率 ≥ 4/5

### Task 2.3: Memory Worth 双计数器
- **文件**: `rust/context-engine-core/src/memory_worth.rs` (~150行)
- **功能**: 采纳/拒绝/忽略时更新计数器；worth_score 计算 (ρ≈0.89)
- **验收**: 计数准确；重启后可恢复；存档前后 worth_score 一致

### Task 2.4: ContextEngine.supply() 主编排器
- **文件**: `rust/context-engine-core/src/context_engine.rs` (~300行)
- **功能**: 双路检索→融合→分层→追溯→审计，返回 ContextPack
- **验收**: supply() 单次 ≤200ms(冷启动)；三层分包含来源追溯和新鲜度标记

### Task 2.5: Python context_engine.py 包装
- **文件**: `plastic_promise/context_engine.py` (~100行)
- **功能**: 薄包装，调用 Rust ContextEngine；兼容 pre_task_v2 接口
- **验收**: pre_task_v2() 直接可用；返回结构体与 Rust 原生一致

---

## 三段：上层模块功能恢复 — Python (Day 5-10)

### Task 3.1: soul_memory.py (~7600行)
- **功能**: RecMem 存储/检索、L1 分层、EvolveR 演化、GC 回收
- **关键**: 记忆检索利用 worth_score 排序；调用 Rust MemoryRecord
- **验收**: 四项功能完整；86+ 条记忆后健康占比 ≥ 80%

### Task 3.2: soul_principles.py (~1200行)
- **功能**: 从 EntityGraph 检索激活原则；work→all / life→all 单向扩散
- **验收**: 扩散后目标域权重符合同步衰减系数

### Task 3.3: soul_loop.py (~958行)
- **功能**: pre_task_v2 + post_task 完整编排；调用上下文供应、SCARF、激素、演化
- **验收**: 完整任务闭环无未捕获异常；audit 记录正常写入

### Task 3.4: soul_scarf.py + soul_proprioception.py (~5026行)
- **功能**: SCARF 五维度自省；本体觉 + 惯性抑制
- **验收**: 自省结果可被审计捕获；连续相似任务惯性抑制生效

### Task 3.5: soul_classifier.py + soul_curiosity.py + skill_extractor.py (~12000行)
- **功能**: 45 关键词分类/ACP 路由；好奇心探索；技能沉淀
- **验收**: 分类准确率 ≥ 90%；技能沉淀无重复

### Task 3.6: soul_hormone.py (~2310行)
- **功能**: 实时反馈激素；评价引擎 + 信任分联动
- **验收**: 信任分波动与行为评价一致；动态约束衰减正确触发

---

## 四段：审计、防线与 Cron 守护 (Day 10-13)

### Task 4.1: soul_enforcer.py (~1024行)
- **验证**: L0/L1/L2 均触发；信任分驱动的 L1↔L0 切换正确

### Task 4.2: soul_audit.py (~5900行)
- **验证**: 七维度/回顾/pre_check 审计完整；合规率 < 50% 自动告警

### Task 4.3: Cron 守护恢复
- **验证**: soul_closure_guardian 300s 内完成；24h 连续无失效

---

## 五段：Claude Code 常态化与工具链 (Day 13-15)

### Task 5.1: 分类器 + ACP 路由
- **验证**: claude --print 和 acpx claude exec 均正常；MCP 注入生效

### Task 5.2: AGENTS.md / SOUL.md 规则
- **验证**: superpowers_followed 自动检测；闭环率 ≥ 70%

---

## 六段：全链路验证与 Rust 替代路线图 (Day 15-17)

### Task 6.1: 原则联想专项审计
- **验证**: 5 场景 ≥ 4/5 命中原则；维度一得分 ≥ 0.80

### Task 6.2: CEI 约定作用指数评测
- **验证**: 20+ 任务后 CEI ≥ 0.85，「约定成熟」

### Task 6.3: Rust 替代路线图
- **输出**: RankFuser/EntityGraph/约束衰减的 Python vs Rust 耗时对比

---

## 七段：全过程 MCP 化 (贯穿全周期)

> 核心原则：Plastic Promise 不是「附带 MCP 接口」，而是 **MCP-native 系统**。
> 所有外部交互、内部模块通信、配置管理全部通过 MCP 协议。

### 架构理念

```
Claude Code / ACP / 外部Agent
        │
        ▼  MCP Protocol (stdio / HTTP)
┌───────────────────────────────────┐
│   Plastic Promise MCP Server      │
│   ┌─────┐┌─────┐┌─────┐┌─────┐  │
│   │记忆 ││原则 ││上下文││审计 │  │ 7 工具组
│   │4+3  ││  4  ││  3  ││  3  │  │ 20+ 工具
│   └─────┘└─────┘└─────┘└─────┘  │
│   ┌─────┐┌─────┐┌─────┐         │
│   │防线 ││自省 ││管理 │         │
│   │  2  ││  3  ││  3  │         │
│   └─────┘└─────┘└─────┘         │
│   ┌──────────────────────────┐  │
│   │     Rust ContextEngine   │  │
│   └──────────────────────────┘  │
└───────────────────────────────────┘
```

### Task 7.1: MCP Server 框架搭建
- **文件**: `plastic_promise/mcp/server.py` (~300行)
- **依赖**: `mcp` Python SDK (Anthropic 官方)
- **内容**: Server 实例化、工具注册、stdio/HTTP transport、错误处理中间件
- **验收**: `python -m plastic_promise.mcp.server` 启动成功；MCP Inspector 可连接

### Task 7.2: 记忆域 MCP 工具 (核心)
- **文件**: `plastic_promise/mcp/tools/memory.py` (~400行)
- **工具**: memory_recall, memory_store, memory_update, memory_forget
- **工具**: memory_stats, memory_list, memory_gc (管理工具)
- **验收**: Claude Code 可通过 `memory_recall` 检索记忆；`memory_store` 写入并返回 ID

### Task 7.3: 原则域 MCP 工具
- **文件**: `plastic_promise/mcp/tools/principles.py` (~250行)
- **工具**: principle_activate, principle_inherit, principle_diffuse, principle_evaluate
- **验收**: 根据任务类型调用 principle_activate 返回 ≥3 条原则

### Task 7.4: 上下文域 MCP 工具
- **文件**: `plastic_promise/mcp/tools/context.py` (~200行)
- **工具**: context_supply (核心 — 调用 ContextEngine.supply()), context_inject, context_graph
- **验收**: context_supply 返回三层 ContextPack JSON，可直接注入 Agent 上下文

### Task 7.5: 审计与防线 MCP 工具
- **文件**: `plastic_promise/mcp/tools/audit.py` + `plastic_promise/mcp/tools/defense.py` (~400行)
- **工具**: audit_run, audit_pre_check, audit_report
- **工具**: defense_trust, defense_status
- **验收**: audit_run 返回七维度评分 JSON；defense_trust 可读写信任分

### Task 7.6: 自省与管理 MCP 工具
- **文件**: `plastic_promise/mcp/tools/reflection.py` + `plastic_promise/mcp/tools/management.py` (~350行)
- **工具**: scarf_reflect, inertia_check, feedback_apply
- **工具**: system_stats, backup, migrate
- **验收**: scarf_reflect 返回五维度结构化评分

### Task 7.7: MCP Resources 与 Prompts
- **文件**: `plastic_promise/mcp/resources.py` + `plastic_promise/mcp/prompts.py`
- **Resources**: 原则列表、审计历史、信任分趋势、记忆统计 作为 MCP Resource 暴露
- **Prompts**: 标准操作流程作为 MCP Prompt 模板（如 "执行七维度审计"、"检查原则联想率"）
- **验收**: `mcp list_resources` 可见全部资源；Prompt 模板可被 Claude 直接引用

### Task 7.8: MCP 全链路集成测试
- **验证**: Claude Code → MCP → ContextEngine.supply() → ContextPack → Agent 决策 全链路跑通
- **验证**: 20 个 MCP 工具全部可调用且返回结构化 JSON
- **验证**: MCP 通信延迟 < 100ms (本地 stdio)
