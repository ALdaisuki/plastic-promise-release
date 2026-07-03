# Exemplar-Research 混合增强 + 闭环常态化 — 设计规格

> 状态: 已确认 | 日期: 2026-07-03 | 方案: 混合模式 + 闭环内置 | 范围: exemplar-research 先行

## 一、动机

当前 `exemplar-research` 阶段有两个不足：

1. **不查记忆**：handler 的 atoms 已包含 `memory_recall`，但召回结果只解析不利用——instructions 中完全没有已有经验的影子。Claude 执行时从零开始搜索，即使记忆系统已有相关典范分析。
2. **不派发子Agent**：instructions 只建议 `WebSearch`，没有并行搜索策略，也没有子Agent 派发 prompt 模板。
3. **无闭环检查点**：instructions 中没有 step-closure 要求，执行者可能跳过闭环，导致经验无法入库、信任分不更新。

**目标**：将 exemplar-research 升级为"记忆优先 + 缺口驱动派发 + 闭环内置"的混合模式，并作为全链路闭环常态化的先行验证。

## 二、设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 搜索模式 | 混合模式 | 先查记忆，根据召回结果动态决定是否派发外部搜索 |
| 结果融合 | 统一报告 | 不区分来源，生成一份合并的典范分析文档 |
| 实现深度 | 指令增强 | handler 内做记忆搜索并注入结果，外部搜索以指令+模板形式交给 Claude |
| 闭环范围 | exemplar-research 先行 | 验证模式可行后推广到全部13个阶段 |

## 三、架构

```
_exemplar_research_handler(ctx, params, atom_results)
  │
  ├─ 1. 解析 memory_recall 结果 → 提取已有经验
  │     ├─ 核心层 (core): relevance >= 0.70 → "可直接复用"
  │     └─ 关联层 (related): relevance >= 0.45 → "可参考借鉴"
  │
  ├─ 2. 缺口分析 → 决定搜索策略
  │     ├─ core >= 3条 → strategy: "verify_only" (0-1个外部验证)
  │     ├─ core 1-2条 → strategy: "fill_gaps" (1-2个子Agent补缺口)
  │     └─ core 为空   → strategy: "full_search" (2-3个子Agent并行)
  │
  ├─ 3. 构建增强 instructions
  │     ├─ [闭环契约] ← 本阶段闭环节奏规则
  │     ├─ [已有经验] ← 格式化的记忆召回结果
  │     ├─ [搜索策略] ← 缺口分析 + 子Agent数量建议
  │     ├─ [子Agent模板] ← 派发 prompt 模板
  │     └─ [执行步骤] ← 含6个闭环检查点
  │
  └─ 4. 返回 SkillResult
```

## 四、指令结构

### 4.1 [闭环契约]

固化闭环节奏规则，放在 instructions 最顶部确保执行者第一眼看到：

```
## [闭环契约] 本阶段闭环节奏

> 每完成一个实质步骤或 git commit 后，必须执行 step-closure。
> 连续 2 个小提交可合并为 1 次 full 闭环。
> 节奏: 起点 light → 过程 light → 产出 full → 提交 full → 完成 full
> 不跳过。闭环是经验入库和信任分更新的唯一路径。
```

### 4.2 [已有经验]

从 `memory_recall` atom 结果中提取，按相关度分层展示：

- `可直接复用` (core 层, relevance >= 0.70)：标注经验ID、摘要、适用性评估
- `可参考借鉴` (related 层, relevance >= 0.45)：标注差异点
- 无结果时标注 "记忆系统无相关经验，需全量外部搜索"

### 4.3 [搜索策略]

根据缺口分析动态生成：

| 记忆召回结果 | 策略 | 子Agent数量 | 说明 |
|-------------|------|------------|------|
| core >= 3 且来源 >= 2 | verify_only | 0-1 | 多来源高相关经验充足，可选1个外部验证 |
| core >= 3 但来源单一 | fill_gaps | 1 | 数量达标但来源单一，需外部独立视角避免回音壁 |
| core 1-2 条 | fill_gaps | 1-2 | 标注已覆盖维度和缺失维度 |
| core 为空 | full_search | 2-3 | 列出建议的搜索维度 |

### 4.4 [子Agent模板]

为每个建议的子Agent生成派发 prompt 模板，包含：
- Context from Memory System（已有经验摘要 + 缺口描述）
- 搜索任务描述
- 三问法分析要求
- 输出格式规范

### 4.5 [执行步骤]（含闭环检查点）

```
0. 【闭环·起点】step-closure(mode="light", task="开始 exemplar-research: {topic}")

1. 审查 [已有经验] — 逐条评估适用性，标记可直接复用的模式
   【闭环·轻量】如有可复用发现，smart-remember 提升对应记忆 worth

2. (如需) 派发子Agent — 使用 [子Agent模板] 并行派发
   【闭环·轻量】每完成 1 个子Agent搜索并产出结果后执行 step-closure(mode="light")

3. 三问法分析 — 合并记忆经验 + 外部搜索结果，生成统一分析
   a. 它解决了什么问题？
   b. 它怎么解决的？（算法/数据结构/流程）
   c. 哪些部分不能直接用？（语言/架构/约束差异）
   【闭环·产出】step-closure(mode="full", lesson="<三问法关键发现>", improvement="<下次搜索可优化的方向>")

4. 写分析文档 → git commit
   路径: docs/superpowers/specs/engineering-patterns/YYYY-MM-DD-<project>.md
   frontmatter: status=draft
   【闭环·提交】step-closure(mode="full", git_commit=<hash>)

5. 质量审核 + smart-remember 入库 → git commit
   a. task_enqueue(type="verify_exemplar") 自审
   b. 审核通过 → smart-remember(memory_type="exemplar") 双写入记忆池
   c. 更新 INDEX.md
   【闭环·完成】step-closure(mode="full", git_commit=<hash>, lesson="<本阶段核心经验>", improvement="<改进建议>")
```

## 五、实现细节

### 5.1 缺口分析算法

```python
def _analyze_gaps(core_items, related_items, search_hints):
    """分析记忆覆盖度，返回策略和子Agent建议。"""
    if len(core_items) >= 3:
        # 来源多样性检查：3条都来自同一来源 → 视野受限，仍需外部搜索
        sources = {item.get("source", item.get("id", "")) for item in core_items}
        if len(sources) < 2:
            return {
                "strategy": "fill_gaps",
                "agent_count": 1,
                "covered": [item.get("summary", "")[:60] for item in core_items],
                "gaps": ["外部独立验证"],
                "rationale": f"记忆有{len(core_items)}条经验但来源单一({len(sources)}个)，需外部独立视角避免回音壁"
            }
        return {
            "strategy": "verify_only",
            "agent_count": 1,
            "covered": [item.get("summary", "")[:60] for item in core_items],
            "gaps": [],
            "rationale": f"记忆系统已有{len(core_items)}条高相关经验(来源{len(sources)}个)，仅需外部验证"
        }
    elif len(core_items) >= 1:
        # 基于 search_hints 判断哪些维度已有覆盖
        covered_dims = _extract_dimensions(core_items)
        missing_dims = [h for h in search_hints if h not in covered_dims]
        return {
            "strategy": "fill_gaps",
            "agent_count": min(len(missing_dims) or 1, 2),
            "covered": covered_dims,
            "gaps": missing_dims or search_hints[:1],
            "rationale": f"记忆覆盖了 {covered_dims}，缺少 {missing_dims or '外部验证'}"
        }
    else:
        return {
            "strategy": "full_search",
            "agent_count": min(len(search_hints) or 2, 3),
            "covered": [],
            "gaps": search_hints if search_hints else ["实现模式", "架构设计"],
            "rationale": "记忆系统无相关经验，需要全量外部搜索"
        }
```

### 5.2 记忆结果格式化

```python
def _format_memory_results(atom_results):
    """从 memory_recall atom 结果中提取并格式化已有经验。"""
    recall = atom_results.get("memory_recall")
    if not recall:
        return {"directly_usable": [], "for_reference": [], "total": 0}

    data = _parse_atom_result(recall)
    core = data.get("core", []) or []
    related = data.get("related", []) or []

    directly = [
        {
            "id": item.get("id", ""),
            "relevance": item.get("relevance", 0),
            "content": item.get("content", "")[:200],
            "assessment": "高相关，可直接复用核心模式"
        }
        for item in core if item.get("relevance", 0) >= 0.70
    ]

    for_ref = [
        {
            "id": item.get("id", ""),
            "relevance": item.get("relevance", 0),
            "content": item.get("content", "")[:150],
            "difference": "关联但领域/场景不同，参考思路而非直接复用"
        }
        for item in related if item.get("relevance", 0) >= 0.45
    ]

    return {
        "directly_usable": directly,
        "for_reference": for_ref,
        "total": len(directly) + len(for_ref)
    }
```

### 5.3 子Agent Prompt 模板生成

```python
def _build_agent_template(agent_index, dimension, memory_context, gap_description):
    """为单个子Agent生成派发 prompt 模板。"""
    return f"""## Agent {agent_index}: 搜索 {dimension}

Context from Memory System:
{memory_context}

需要补充的知识缺口:
{gap_description}

Task:
1. 使用 WebSearch 搜索 {dimension} 的成熟工程实现
2. 对每个找到的典范执行三问法分析:
   a. 它解决了什么工程问题？
   b. 核心模式是什么？（算法/数据结构/流程）
   c. 哪些部分不能直接复用到当前项目？
3. 输出结构化分析结果（JSON格式，包含 project/problem_solved/core_pattern/not_applicable/reusable_parts）

## 结果回流规则
子Agent 只做搜索和分析，不直接写入记忆池。搜索完成后：
1. 子Agent 将结构化 JSON 结果返回给主Agent（exemplar-research 执行者）
2. 主Agent 合并所有子Agent结果 + 记忆已有经验 → 生成统一分析报告
3. 主Agent 在步骤5统一执行 smart-remember 入库（一条合并后的 exemplar 记忆）
4. 子Agent 不单独写记忆 — 避免碎片化，保证入库的是经过合并审核的完整典范分析
"""
```

## 六、涉及文件

| # | 文件 | 类型 | 行数 | 说明 |
|---|------|------|------|------|
| 1 | `plastic_promise/skills/exemplar_research.py` | 修改 | ~150 | handler 重写：记忆解析 + 缺口分析(含来源多样性) + 策略决策 + 指令生成 + 闭环注入 + 结果回流规则 |
| 2 | `docs/superpowers/specs/2026-07-03-exemplar-research-hybrid-enhancement-design.md` | 新增 | — | 本设计文档 |

## 七、不做的事

- 不新增 MCP 工具或 atom
- 不修改 SuperPowers 插件 (Claude Code skills)
- 不修改其他12个阶段（等 exemplar-research 验证后推广）
- handler 内不实际执行搜索或派发子Agent（保持轻量）
- 不修改 SuperPowers 链约束

## 八、验证方法

1. **单元测试**：调用 `sp-stage(stage="exemplar-research", task_description="Rust 存储引擎设计")`，验证返回的 instructions 包含 `[闭环契约]`、`[已有经验]`、`[搜索策略]`、`[子Agent模板]` 四个段
2. **记忆注入验证**：确认 `memory_recall` 的结果被正确解析并格式化到 `[已有经验]` 段
3. **缺口策略验证**：测试三种场景（充足/部分/空）下的策略决策正确性
4. **端到端验证**：在对话中执行完整的 exemplar-research 流程，确认闭环检查点不遗漏

## 九、推广路线（验证后）

exemplar-research 验证成功后，将闭环契约模式推广到其余12个阶段：
1. 在 `superpowers_stages.py` 的通用 `_stage_handler` 中注入 `closure_policy` 字段
2. 各阶段 SkillResult.data 中增加闭环节奏提示
3. SuperPowers 插件技能读取 `closure_policy` 并在执行时展示
