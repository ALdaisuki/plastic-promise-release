# CLAUDE.md — Plastic Promise 操作指令

> 📋 完整架构、当前状态、路线图见 **[GOAL.md](GOAL.md)**。
> 核心范式：**约定工程** — 内化约定替代外部约束。

## 会话启动

每次会话开始，依次执行：

1. `auto_context_inject(task_description="<当前任务>", scope="agent:claude", source="claude_code")` — **统一上下文注入**（含原则激活 + 记忆召回 + 实体追踪 + 注入沉淀，替代原步骤 3/4/5 三步手动调用）
2. `domain(action="stats")` — 域联邦健康度 + 当前活跃域
3. `system(action="stats")` — 记忆池总量 + 衰减分布 + fuzzy buffer 积压
4. `defense(action="get")` — 信任分 + 防线状态
5. `memory_gc(dry_run=True)` — 预览记忆衰减/合并候选（不执行）

> **重要**: 具体任务时重新调用 `context_supply(task_description, task_type, scope)` 获取针对性上下文。
> - 编码/实施 → `task_type="code_generation"`
> - 修复/调试 → `task_type="debugging"`
> - 设计/规划 → `domain_hint="designing"`
> - 审查/复盘 → `domain_hint="reflecting"`
> - 发布/合入 → `domain_hint="governing"`

## MCP 工具 (33 个, 9 域)

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

## 关键约定

- **先查再问** — 决策前先 principle_activate + memory_recall
- **每步有 git** — 可追溯、可复现
- **信任动态** — 信任分影响检索范围 (high=1.3x, critical=0.5x)
- **域联邦** — 同名域自动融合, 信号 ≤200字符不深入细节
- **宪法人人遵守** — issue_validator 管 Claude 也管 Pi
- **快速失败** — DomainManager 不可用时降级为全量检索
- **不重复造轮子** — 先查记忆, 再查网上, 没有再创新

## Skill 调用协议 (Session 追踪) — 强制执行

> ⚠️ **这不是可选的。每次通过 Skill 工具调用 SuperPowers skill 时必须执行。**
> CLAUDE.md 优先级高于 Skill 文件，此协议覆盖所有 skill 的默认行为。

### 前置指令（调用 Skill 工具之前 — 强制执行）

调用任何 SuperPowers skill 时，第一件事就是：

```
skill_session_start(
    skill_name="<brainstorming|writing-plans|subagent-driven-development|...>",
    task_description="<本次要完成的具体任务>",
    parent_entity_id="<上一个 skill 的 entity_id | null>"
)
# 保存返回的 entity_id 供后置指令使用
```

### 后置指令（skill 执行完毕时 — 强制执行）

skill 完成后立即：

```
skill_session_complete(
    entity_id="<start 返回的 id>",
    outcome="<结果摘要 ≤200字>",
    artifacts=["<产出文件路径1>", "<产出文件路径2>"]
)
# outcome 示例: "设计文档已确认，采用方案3" / "15个测试通过，提交 8e53b48"
```

### 超时续期（skill 超过 30 分钟时）

```
skill_session_complete(entity_id="<id>", outcome="still_in_progress", artifacts=[])
# 最多续期 3 次。超过后自动标记 task:overdue
```

### 主动放弃

```
skill_session_complete(entity_id="<id>", outcome="abandoned: <原因>", artifacts=[])
```

### Skill 调用链映射

| 当前 Skill | 合法后续 |
|-----------|---------|
| brainstorming | writing-plans |
| writing-plans | subagent-driven-development, executing-plans |
| executing-plans | verification-before-completion |
| subagent-driven-development | finishing-a-development-branch |
| verification-before-completion | finishing-a-development-branch |
| finishing-a-development-branch | (终端) |
| systematic-debugging | test-driven-development |
| test-driven-development | verification-before-completion |
| requesting-code-review | receiving-code-review |
| receiving-code-review | (终端) |

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
