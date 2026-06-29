# CLAUDE.md — Plastic Promise 操作指令

> 📋 完整架构、当前状态、路线图见 **[GOAL.md](GOAL.md)**。
> 核心范式：**约定工程** — 内化约定替代外部约束。

## 会话启动

每次会话开始，依次执行：

1. `principle_activate(task_type="general")` — 查阅 12 条核心约定
2. `memory_recall(query="<当前任务关键词>", domain_hint=None)` — 查阅相关记忆
3. `system(action="stats")` — 检查记忆池健康度 + 流水线状态
4. `memory_store(content="会话启动：<目标任务>", memory_type="experience")` — 记录会话
5. `defense(action="get")` — 信任分 + 防线状态

## MCP 工具 (29 个, 8 域)

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
