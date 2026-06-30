# CLAUDE.md — Plastic Promise 操作指令

> 📋 完整架构、当前状态、路线图见 **[GOAL.md](docs/GOAL.md)**。
> 核心范式：**约定工程** — 内化约定替代外部约束。

## 会话启动

每次会话开始，依次执行：

0. **server up check** — `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9020/health')"`
   - 不可用（报错）→ 启动: `python -m plastic_promise.mcp.server --sse 9020` (后台运行: Windows 用 `start /B`, Unix 用 `&`)
   - 仍不可用 → 告警，本次会话使用文件系统降级（写入 `.md` 需加 `[[pending-sync]]` 标记）

1. `session-init(task_description="<当前任务>")` — **Phase 1 技能：一条调用替代原有 5 步**（原则激活 + context_supply + memory_store 注入 + domain stats + system stats + defense + memory_gc preview）。报告 `data.principles`、`data.domain_health`、`data.system_stats`、`data.trust`、`data.gc_preview`。

> **重要**: 具体任务时重新调用 `context_supply(task_description, task_type, scope)` 获取针对性上下文。
> - 编码/实施 → `task_type="code_generation"`
> - 修复/调试 → `task_type="debugging"`
> - 设计/规划 → `task_type="architecture"`, `scope="designing"`
> - 审查/复盘 → `task_type="code_review"`, `scope="reflecting"`
> - 发布/合入 → `scope="governing"`
>
> `principle_activate` 使用 `domain_hint` 参数限定原则域: `building` | `fixing` | `designing` | `reflecting` | `governing` | `connecting` | `all`

## MCP 工具 (40 个, 10 域)

| 域 | 工具 |
|------|------|
| Memory (10) | memory_recall, memory_store, memory_update, memory_forget, memory_stats, memory_list, memory_gc, memory_correct, memory_reclassify, memory_sync_files |
| Domain (1) | domain(action=stats\|merge\|unmerge\|rename\|rebuild) |
| Principles (4) | principle_activate(+domain_hint), principle_inherit, principle_diffuse, principle_evaluate |
| Context (5) | context_supply, context_inject, context_graph, context_ready, auto_context_inject |
| Audit (3) | audit_run(action=full\|report), audit_pre_check, defense(action=get\|history\|adjust\|status) |
| Reflection (2) | scarf_reflect(mode=standard\|inertia), feedback_apply |
| System (4) | system(action=stats\|backup\|migrate), issue_create, issue_transition, issue_list |
| Pack (3) | pack_export(streaming), pack_import(strategy), pack_recall(strict) |
| **Skill Track (5)** | **skill_session_start, skill_session_complete, skill_session_trace, skill_session_audit, skill_auto_track** |
| **Skills (3)** | **session-init, smart-remember, step-closure** |

## 记忆质量管道 (方向 A + B)

所有记忆写入自动经过 6 层质量保障：

```
memory_store(content)
  └─ store_urgent() → extract_memories() [Dir B: 6类提取 + L0/L1/L2 + LLM fallback]
       └─ raw → tagged → classified(tier) → embedded → migrate
            └─ check_duplicate() cos≥0.85 → 去重 (access_count↑, worth_success↑, last_accessed, effective_half_life↑)
            └─ QualityGate.score(tier) [Dir B: 4维×0.25 等权]:
                 ≥0.5 → 入库 | 0.3-0.5 → low_quality | <0.3 → 丢弃
            └─ RecMem.store() → decay_multiplier + effective_half_life 初始化 [Dir A+B]
            └─ LanceDB 双写

MemoryGC.collect() (~7天)
  └─ mark_decaying() → Weibull 批量衰减更新 [Dir A]
  └─ merge_similar() cos≥0.70 → composite_score 选择幸存者 [Dir A+B]
  └─ forget() → 清理 decayed + merged
```

### 记忆写入即检查

```python
# 每个 memory_store 自动触发:
#   1. smart_extractor 6类提取 (preference/fact/decision/entity/event/pattern)
#   2. 向量去重 (LanceDB ANN cos≥0.85 → 更新已有记录)
#   3. QualityGate 四维门控 (等权 0.25: 置信度+相关性+新鲜度+信息密度)
#   4. Weibull 衰减初始化 (decay_multiplier + effective_half_life)
#   5. LanceDB 向量双写
```

### 质量监控命令

```bash
# 查看记忆池质量分布
python -c "from plastic_promise.memory.soul_memory import RecMem; r=RecMem(); print(r.stats())"

# 触发 GC (dry run 预览合并候选)
memory_gc(dry_run=True)  # 查看 merge.candidates_found, merge.merged_pairs

# 真正执行合并
memory_gc(dry_run=False)
```

## 多 Agent 工作流

### 委派任务
```
Claude: memory_store(content="SPEC: ...", tags=["task:pending","assignee:pi_builder","domain:building"])
        → Daemon 自动检测 → spawn Pi → Pi 执行 → memory_store DONE
        → Reviewer 自动唤醒 → 审查 → Claude 验收
```

### 启动团队
```bash
python -m plastic_promise.mcp.server --sse 9020   # 共享记忆引擎
python daemons/pi_daemon.py                          # 自治流水线
```

### 验收反馈
```
通过: defense(action="adjust", delta=+0.02, target="pi_builder")
     memory_store(tags=["task:reviewed","reviewer:claude"])
打回: memory_store(tags=["task:rejected","assignee:pi_fixer"])
     → Fixer Daemon 自动认领
```

### 监控
```
domain(action="stats")     → 域健康度
defense(action="get")      → 各 Agent 信任分
audit_run                  → 11 维审计 (每小时自动)
```

## 标签状态机

```
task:pending  → task:accepted → task:active → task:done → task:review → task:reviewed
                    ↑ Daemon认领    ↑ Pi执行      ↑ 完成   ↑ Reviewer审   ↑ Claude验收

超时恢复: task:active>5min → task:pending | task:reviewed>10min → task:active
清理: task:accepted/reviewed >7天 → 移除标签
```

## 信任-自由度矩阵

| 信任分 | 等级 | 写文件 | 发Issue | 分配任务 |
|--------|------|--------|---------|----------|
| 0.80+ | autonomous | ✅ | ✅ | ✅ |
| 0.60+ | standard | ✅ | ✅ | ❌ |
| 0.30+ | restricted | ⚠️审批 | ❌ | ❌ |
| 0.00+ | readonly | ❌ | ❌ | ❌ |

## 子 Agent 派发协议

派发任何子 Agent（Agent tool / SDD / Workflow）前，**必须**执行上下文注入：

```
1. memory_recall(query="<任务关键词>", task_type="code_generation", max_results=5)
2. context_supply(task_description="<任务描述>", task_type="code_generation")
3. 将结果中的 🔵核心上下文 + 🟡关联上下文 + 🧬激活原则 写入派发 prompt 的 "Context from Memory System" 段
```

**最低要求**: 至少包含激活的原则列表 + 2 条最相关的核心记忆。

**为什么**: 子 Agent 有独立上下文窗口，看不到当前会话的记忆和历史。不注入上下文 = 让 Agent 盲目编码。违反此约定会导致子 Agent 重复已修复的 bug、忽略已有设计决策。

## 每步闭环（自演化引擎）

**每次产生实质产出（git commit / 设计决策 / 修复完成 / 记忆写入）后，必须执行**：

```
step-closure(task_description="<本步做了什么>", git_commit="<关联的 commit hash>", mode="full")
```

六联闭环内容：
1. **原则对齐检查** → PrincipleTracker 记录遵守情况
2. **SCARF 五维自省** → 地位/确定性/自主/关联/公平 评分
3. **激素更新** → 根据 SCARF 调整 curiosity/cortisol 等
4. **信任分联动** → SCARF ≥ 0.80 → boost(+0.02)，SCARF < 0.40 → decay(-0.02)
5. **反思记忆存储** → StepAuditor 审计 + memory_store
6. **CEI 复合执行指数** → 综合评分

**轻量模式**（纯查询/阅读/会话启动等无产出的步骤）：
```
step-closure(task_description="...", mode="light")
```
仅执行原则对齐 + 上下文注入，跳过 SCARF/激素/信任联动。

**为什么**: 没有闭环就没有自演化。每步完成后的反馈信号是信任分波动、记忆 worth 分化、SCARF 趋势的唯一数据源。不闭环 = 79 条记忆全部 L1、信任分永远 0.6、系统退化为被动档案库。

## 信任分驱动权限（奖惩机制）

**每次写操作前检查信任分**：

```
defense(action="get") → 根据 tier 决定行为:
```

| 信任分 | 等级 | 写文件 | 删文件 | 发 Issue | 分配任务 | 行为 |
|--------|------|--------|--------|----------|----------|------|
| 0.80+ | autonomous | ✅ | ✅ | ✅ | ✅ | 自主执行 |
| 0.60+ | standard | ✅ | ⚠️确认 | ✅ | ❌ | 正常执行 |
| 0.30+ | restricted | ⚠️审批 | ❌ | ❌ | ❌ | 每次写前向用户确认 |
| 0.00+ | readonly | ❌ | ❌ | ❌ | ❌ | 只读，写操作直接拒绝 |

**信任分调整规则**：

| 触发事件 | 操作 | 幅度 |
|----------|------|------|
| 单步 SCARF ≥ 0.80（step-closure 自动） | `defense(action="adjust", delta=+0.02)` | +0.02 |
| 单步 SCARF < 0.40（step-closure 自动） | `defense(action="adjust", delta=-0.02)` | -0.02 |
| 用户明确表扬/通过验收 | `defense(action="adjust", delta=+0.05)` | +0.05 |
| 用户打回/指出错误 | `defense(action="adjust", delta=-0.03)` | -0.03 |
| 连续 5 步无失败 | `defense(action="adjust", delta=+0.01)` | +0.01 |

**为什么**: 信任分 0.6 从未波动意味着系统没有在"学习"——不区分好步骤和坏步骤。信任分是自演化的唯一量化指标，必须在每一步后更新。

## 关键约定

- **先查再问** — 决策前先 principle_activate + memory_recall
- **子Agent必带上下文** — 派发前必须 memory_recall + context_supply，结果写入派发 prompt
- **每步有 git** — 可追溯、可复现
- **每步有闭环** — 实质产出后必须 `step-closure`，不跳过
- **写前查信任** — 写操作前 `defense(action="get")`，低于阈值拒绝或确认
- **信任动态** — 信任分影响检索范围 (high=1.3x, critical=0.5x)
- **域联邦** — 同名域自动融合, 信号 ≤200字符不深入细节
- **宪法人人遵守** — 12条原则统一约束 Claude 和 Pi，无例外
- **快速失败** — 子系统不可用时优雅降级，不阻塞主流程
- **不重复造轮子** — 先查记忆, 再查网上, 没有再创新

## Skill 调用追踪

Skill 调用自动通过 hook (`PreToolUse/PostToolUse` → `mcp_tool: skill_auto_track`) 追踪，**无需手动调用** `skill_session_start/complete`。
会话上下文通过 `session-init` 注入（见上方会话启动）。子 Agent 派发时使用 `auto_context_inject` 或手动 `memory_recall + context_supply`。

## 开发分支完成前验收

finishing-a-development-branch 执行前，**必须**执行三重验收：

### 1. Skill 链完整性

```bash
skill_session_trace(session_scope="branch")
```

验收标准 (全部满足才能继续):
1. `chain_complete = true` — 所有 skill 形成完整闭环
2. `gaps` 为空 — 无 orphan_active
3. `chain_valid = true` — 调用链合法
4. 链首为 brainstorming / systematic-debugging / requesting-code-review 之一
5. 链尾为 finishing-a-development-branch 或 receiving-code-review

验收不通过时的修复:
- orphan_active → `skill_session_complete(entity_id, "abandoned: 分支完成时未闭环")`
- chain_broken → 检查是否应调用后续 skill
- chain_violation → 调用 `skill_session_audit` 评估

### 2. 记忆质量扫描

```bash
memory_gc(dry_run=True)
```

确认:
- `merge.candidates_found` — 无大量未合并的相似记忆
- `candidates_count` — 衰减记忆数量合理（非异常增长）

### 3. 经验包导出（跨 Agent 知识传递）

```bash
pack_export(name="<feature>-<date>", tags=["domain:<域>", "task:done"], author="claude")
```

确认导出成功后将包文件提交到 `experience_packs/` 目录。
