# CLAUDE.md — Plastic Promise 操作指令

> 📋 完整架构、当前状态、路线图见 **[GOAL.md](GOAL.md)**。
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
> - 设计/规划 → `domain_hint="designing"`
> - 审查/复盘 → `domain_hint="reflecting"`
> - 发布/合入 → `domain_hint="governing"`

## MCP 工具 (36 个, 10 域)

| 域 | 工具 |
|------|------|
| Memory (10) | memory_recall, memory_store, memory_update, memory_forget, memory_stats, memory_list, memory_gc, memory_correct, fuzzy_status, fuzzy_process |
| Domain (1) | domain(action=stats\|merge\|unmerge\|rename\|rebuild) |
| Principles (4) | principle_activate(+domain_hint), principle_inherit, principle_diffuse, principle_evaluate |
| Context (4) | context_supply, context_inject, context_graph, context_ready |
| Audit (4) | audit_run(action=full\|report), audit_pre_check, defense(action=get\|history\|adjust\|status) |
| Reflection (2) | scarf_reflect(mode=standard\|inertia), feedback_apply |
| System (4) | system(action=stats\|backup\|migrate), issue_create, issue_transition, issue_list |
| Pack (3) | pack_export(streaming), pack_import(strategy), pack_recall(strict) |
| **Skill Track (4)** | **skill_session_start, skill_session_complete, skill_session_trace, skill_session_audit** |
| **Skills (2)** | **session-init, smart-remember** |

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
python pi_daemon.py                                 # 自治流水线
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

## 关键约定

- **先查再问** — 决策前先 principle_activate + memory_recall
- **子Agent必带上下文** — 派发前必须 memory_recall + context_supply，结果写入派发 prompt
- **每步有 git** — 可追溯、可复现
- **信任动态** — 信任分影响检索范围 (high=1.3x, critical=0.5x)
- **域联邦** — 同名域自动融合, 信号 ≤200字符不深入细节
- **宪法人人遵守** — issue_validator 管 Claude 也管 Pi
- **快速失败** — DomainManager 不可用时降级为全量检索
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
