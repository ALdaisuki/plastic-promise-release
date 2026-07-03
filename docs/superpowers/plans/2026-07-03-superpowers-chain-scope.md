# SuperPowers Chain Scope Implementation Plan

> **For agentic workers:** Follow the SuperPowers chain. Do not execute implementation before this plan exists. Steps use checkbox syntax for tracking.

**Goal:** 修复多 Agent 并发时 SuperPowers 全局 `current_stage` 导致 root 流程互相阻塞的问题。

**Architecture:** 先做 root-stage bypass 热修，保留非 root 硬链约束；后续再做 `chain_id` scoped state 正式隔离。

**Tech Stack:** Python, pytest, MCP Server

## Global Constraints

- 不修改 MCP `sp-stage` 入参 schema，避免破坏已有调用方。
- 不取消链约束，只允许 `predecessors=[]` 的 root stage 开新链。
- 不在本次热修中重构 `skill_auto_track` 的状态存储。
- 新增测试必须同时覆盖 allow 与 reject。

---

### Task 1: 实现 root-stage bypass

**Files:**
- Modify: `plastic_promise/mcp/server.py`

**Interfaces:**
- Consumes: `SKILL_CHAIN_MAP`
- Produces: `sp-stage` chain validation that allows root stages to start independent chains

- [x] Step 1: 在 `sp-stage` 校验中解析目标 stage 的 chain 定义。
- [x] Step 2: 判断 `target_chain.predecessors == []`。
- [x] Step 3: 如果目标是 root stage，跳过 current successor 硬阻断。
- [x] Step 4: 如果目标不是 root stage，保留原有 `chain_violation` 行为。

---

### Task 2: 添加回归测试

**Files:**
- Add: `tests/test_sp_stage_chain_validation.py`

- [x] Step 1: mock `get_current_stage()` 返回 `requesting-code-review`。
- [x] Step 2: mock `get_skill_engine()` 避免执行真实 SkillEngine。
- [x] Step 3: 验证 `systematic-debugging` 成功作为 root 新链启动。
- [x] Step 4: 验证 `test-driven-development` 仍被拒绝。

---

### Task 3: 验证

- [x] Step 1: 运行新增测试文件。
- [x] Step 2: 运行相关 skill tracking / MCP 测试。
- [x] Step 3: 如测试失败，修复后重跑。

---

### Task 4: 收尾

- [ ] Step 1: 执行 `verification-before-completion`。
- [ ] Step 2: 执行代码审查。
- [ ] Step 3: 执行 finishing 前的三重验收：skill trace、memory_gc dry run、经验包导出。
- [ ] Step 4: 汇总变更和剩余风险。
