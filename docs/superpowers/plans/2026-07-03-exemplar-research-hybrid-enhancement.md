# Exemplar-Research 混合增强 + 闭环常态化 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写 `_exemplar_research_handler`，将 exemplar-research 升级为"记忆优先 + 缺口驱动派发 + 闭环内置"的混合模式。

**Architecture:** 单一文件改动 (`plastic_promise/skills/exemplar_research.py`)，handler 从 ~80 行扩展到 ~150 行。新增 5 个纯函数辅助工具（无副作用、无新依赖），handler 编排调用它们生成增强版 instructions。不新增 MCP 工具、不修改 atom 配置、不修改 SuperPowers 插件。

**Tech Stack:** Python 3.10+ (async/await, dataclasses 已有); 无新依赖

## Global Constraints

- 不新增 MCP 工具或 atom — 复用现有 `memory_recall` + `principle_activate` + `memory_store`
- 不修改 `STAGE_ATOMS` 配置 — exemplar-research 的 atoms 列表不变
- 不修改 SuperPowers 插件 (Claude Code skills) — 仅改后端 handler
- handler 内不执行网络请求、不派发实际子Agent — 保持轻量
- 遵循奥卡姆剃刀：exemplar-research 先行，验证后再推广全链路

## File Structure

| 文件 | 职责 |
|------|------|
| `plastic_promise/skills/exemplar_research.py` | handler + 辅助函数（全部在本文件内） |
| `tests/test_exemplar_research.py` | handler 输出验证测试（新增） |

### 模块内部结构

```
exemplar_research.py
├── _parse_atom_result()           # 解析 atom 返回的 TextContent
├── _extract_memory_items()        # 从 memory_recall 提取 core/related
├── _check_source_diversity()      # 来源多样性检查
├── _analyze_gaps()                # 缺口分析 → 策略决策
├── _format_memory_section()       # 生成 [已有经验] markdown
├── _format_strategy_section()     # 生成 [搜索策略] markdown
├── _build_agent_templates()       # 生成子Agent prompt 模板
├── _build_closure_contract()      # 生成 [闭环契约] markdown
├── _build_execution_steps()       # 生成 [执行步骤] markdown
├── _build_enhanced_instructions() # 组装全部段 → 最终 instructions
├── _exemplar_research_handler()   # 【修改】编排调用，返回 SkillResult
└── EXEMPLAR_RESEARCH_SKILL_DEF    # 【不变】
```

---

### Task 1: Memory Data Extraction Helpers

**Files:**
- Modify: `plastic_promise/skills/exemplar_research.py:16-18` (after existing imports)

**Interfaces:**
- Produces: `_parse_atom_result(result) -> dict`
- Produces: `_extract_memory_items(atom_results: dict) -> dict[str, list[dict]]`

- [ ] **Step 1: Add `_parse_atom_result` helper**

Replace the inline `parse()` lambda (currently at line ~48-54) with a module-level function:

```python
def _parse_atom_result(result) -> dict:
    """Parse atom result list[TextContent] into a dict.

    Handles three cases:
    1. Normal: result[0].text is valid JSON
    2. Degraded: result[0].text is non-JSON text (wrap in {"raw": ...})
    3. Missing: result is None or empty (return {})
    """
    if not result:
        return {}
    try:
        if hasattr(result[0], "text"):
            try:
                return json.loads(result[0].text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": result[0].text}
    except (IndexError, TypeError):
        pass
    return {}
```

- [ ] **Step 2: Add `_extract_memory_items` helper**

```python
def _extract_memory_items(atom_results: dict) -> dict:
    """Extract core and related items from memory_recall atom result.

    Returns:
        {
            "core": [{"id": str, "relevance": float, "content": str, "source": str}, ...],
            "related": [...],
            "total_core": int,
            "total_related": int,
            "has_results": bool,
        }
    """
    recall_data = _parse_atom_result(atom_results.get("memory_recall"))

    # memory_recall returns ContextPack-style JSON with core/related/divergent layers
    core_raw = recall_data.get("core", []) or []
    related_raw = recall_data.get("related", []) or []

    core = [
        {
            "id": item.get("id", ""),
            "relevance": item.get("relevance", 0.0),
            "content": (item.get("content", "") or "")[:200],
            "source": item.get("source", item.get("id", "")),
            "tags": item.get("tags", []),
        }
        for item in core_raw
        if item.get("relevance", 0) >= 0.70
    ]

    related = [
        {
            "id": item.get("id", ""),
            "relevance": item.get("relevance", 0.0),
            "content": (item.get("content", "") or "")[:150],
            "source": item.get("source", item.get("id", "")),
            "tags": item.get("tags", []),
        }
        for item in related_raw
        if item.get("relevance", 0) >= 0.45
    ]

    return {
        "core": core,
        "related": related,
        "total_core": len(core),
        "total_related": len(related),
        "has_results": bool(core or related),
    }
```

- [ ] **Step 3: Run existing tests to verify no regression**

```bash
python -m pytest tests/test_skill_tracking.py -v -k "exemplar" 2>&1
```

Expected: any existing exemplar-related tests still pass (or no tests found — acceptable baseline).

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/skills/exemplar_research.py
git commit -m "refactor(exemplar-research): extract memory parse helpers"
```

---

### Task 2: Gap Analysis + Source Diversity

**Files:**
- Modify: `plastic_promise/skills/exemplar_research.py` (append after Task 1 helpers)

**Interfaces:**
- Consumes: `_extract_memory_items` return dict
- Produces: `_check_source_diversity(core_items: list[dict]) -> int`
- Produces: `_analyze_gaps(memory_items: dict, search_hints: list[str]) -> dict`

- [ ] **Step 1: Add `_check_source_diversity`**

```python
def _check_source_diversity(core_items: list[dict]) -> int:
    """Count unique sources among core memory items.

    Uses item["source"] if present, falls back to item["id"].
    Returns the count of distinct sources.
    """
    if not core_items:
        return 0
    sources = {item.get("source", item.get("id", "")) for item in core_items}
    return len(sources)
```

- [ ] **Step 2: Add `_analyze_gaps`**

```python
def _analyze_gaps(memory_items: dict, search_hints: list[str]) -> dict:
    """Analyze memory coverage gaps and decide search strategy.

    Args:
        memory_items: output of _extract_memory_items()
        search_hints: keywords extracted from task_description

    Returns:
        {
            "strategy": "verify_only" | "fill_gaps" | "full_search",
            "agent_count": int,
            "covered_dims": list[str],
            "missing_dims": list[str],
            "rationale": str,
            "memory_sufficient": bool,
        }
    """
    core = memory_items.get("core", [])
    core_count = memory_items.get("total_core", 0)

    if core_count >= 3:
        sources = _check_source_diversity(core)
        if sources < 2:
            return {
                "strategy": "fill_gaps",
                "agent_count": 1,
                "covered_dims": [item.get("content", "")[:60] for item in core],
                "missing_dims": ["外部独立验证"],
                "rationale": (
                    f"记忆有{core_count}条高相关经验但来源单一"
                    f"({sources}个)，需外部独立视角避免回音壁"
                ),
                "memory_sufficient": False,
            }
        return {
            "strategy": "verify_only",
            "agent_count": 1,
            "covered_dims": [item.get("content", "")[:60] for item in core],
            "missing_dims": [],
            "rationale": (
                f"记忆系统已有{core_count}条高相关经验"
                f"(来源{sources}个)，仅需外部验证"
            ),
            "memory_sufficient": True,
        }

    if core_count >= 1:
        covered_keywords = set()
        for item in core:
            for hint in search_hints:
                if hint.lower() in (item.get("content", "") or "").lower():
                    covered_keywords.add(hint)
        missing = [h for h in search_hints if h not in covered_keywords]
        return {
            "strategy": "fill_gaps",
            "agent_count": min(len(missing) or 1, 2),
            "covered_dims": list(covered_keywords) or [item.get("content", "")[:60] for item in core],
            "missing_dims": missing or search_hints[:1],
            "rationale": (
                f"记忆覆盖了{covered_keywords or '部分维度'}，"
                f"缺少{missing or '外部验证'}"
            ),
            "memory_sufficient": False,
        }

    return {
        "strategy": "full_search",
        "agent_count": min(len(search_hints) or 2, 3),
        "covered_dims": [],
        "missing_dims": search_hints if search_hints else ["实现模式", "架构设计"],
        "rationale": "记忆系统无相关经验，需要全量外部搜索",
        "memory_sufficient": False,
    }
```

- [ ] **Step 3: Import sanity check**

```bash
python -c "from plastic_promise.skills.exemplar_research import _analyze_gaps, _check_source_diversity; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/skills/exemplar_research.py
git commit -m "feat(exemplar-research): add gap analysis with source diversity check"
```

---

### Task 3: Instructions Section Builders

**Files:**
- Modify: `plastic_promise/skills/exemplar_research.py` (append after Task 2)

**Interfaces:**
- Consumes: `_extract_memory_items` return, `_analyze_gaps` return
- Produces: `_format_memory_section(memory_items: dict) -> str`
- Produces: `_format_strategy_section(gap_analysis: dict) -> str`
- Produces: `_build_closure_contract() -> str`

- [ ] **Step 1: Add `_format_memory_section`**

```python
def _format_memory_section(memory_items: dict) -> str:
    """Generate [已有经验] markdown section from memory recall results."""
    core = memory_items.get("core", [])
    related = memory_items.get("related", [])

    if not core and not related:
        return (
            "## [已有经验] 来自记忆系统\n\n"
            "> 记忆系统中暂无与本任务高度相关的经验。\n"
            "> 将触发全量外部搜索。\n"
        )

    lines = ["## [已有经验] 来自记忆系统\n"]

    if core:
        lines.append("### 可直接复用 (高相关)\n")
        for i, item in enumerate(core, 1):
            lines.append(
                f"{i}. **[{item['relevance']:.0%}]** {item['content']}\n"
                f"   - 来源: `{item['id']}`\n"
                f"   - 评估: 高相关，核心模式可直接复用\n"
            )

    if related:
        lines.append("### 可参考借鉴 (中相关)\n")
        for i, item in enumerate(related, 1):
            lines.append(
                f"{i}. **[{item['relevance']:.0%}]** {item['content']}\n"
                f"   - 来源: `{item['id']}`\n"
                f"   - 差异: 关联但领域/场景不同，参考思路而非直接复用\n"
            )

    return "\n".join(lines) + "\n"
```

- [ ] **Step 2: Add `_format_strategy_section`**

```python
def _format_strategy_section(gap_analysis: dict) -> str:
    """Generate [搜索策略] markdown section from gap analysis."""
    strategy = gap_analysis["strategy"]
    agent_count = gap_analysis["agent_count"]
    rationale = gap_analysis["rationale"]
    missing = gap_analysis.get("missing_dims", [])

    strategy_labels = {
        "verify_only": "验证模式 — 记忆经验充足，仅需外部验证",
        "fill_gaps": "补缺模式 — 记忆覆盖部分维度，针对性搜索缺口",
        "full_search": "全量搜索 — 记忆无覆盖，并行搜索多个维度",
    }

    lines = [
        "## [搜索策略] 缺口驱动\n",
        f"> 策略: **{strategy}** — {strategy_labels.get(strategy, strategy)}",
        f"> 理由: {rationale}",
        f"> 建议派发子Agent: **{agent_count}个**",
    ]

    if missing:
        lines.append(f"> 搜索维度: {' | '.join(missing)}")

    return "\n".join(lines) + "\n"
```

- [ ] **Step 3: Add `_build_closure_contract`**

```python
def _build_closure_contract() -> str:
    """Generate [闭环契约] markdown section."""
    return (
        "## [闭环契约] 本阶段闭环节奏\n"
        "\n"
        "> 每完成一个实质步骤或 git commit 后，必须执行 step-closure。\n"
        "> 连续 2 个小提交可合并为 1 次 full 闭环。\n"
        "> 节奏: **起点 light → 过程 light → 产出 full → 提交 full → 完成 full**\n"
        "> **不跳过。闭环是经验入库和信任分更新的唯一路径。**\n"
    )
```

- [ ] **Step 4: Verify imports**

```bash
python -c "from plastic_promise.skills.exemplar_research import _format_memory_section, _format_strategy_section, _build_closure_contract; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/skills/exemplar_research.py
git commit -m "feat(exemplar-research): add instruction section builders"
```

---

### Task 4: Agent Templates + Execution Steps

**Files:**
- Modify: `plastic_promise/skills/exemplar_research.py` (append after Task 3)

**Interfaces:**
- Consumes: `_analyze_gaps` return, `_extract_memory_items` return
- Produces: `_build_agent_templates(gap_analysis: dict, memory_items: dict, search_query: str, gap_severity: str, gap_problem: str) -> str`
- Produces: `_build_execution_steps(gap_analysis: dict, gap_severity: str) -> str`

- [ ] **Step 1: Add `_build_agent_templates`**

```python
def _build_agent_templates(gap_analysis: dict, memory_items: dict, search_query: str,
                           gap_severity: str = None, gap_problem: str = None) -> str:
    """Generate [子Agent派发模板] markdown section.

    Args:
        gap_analysis: output of _analyze_gaps()
        memory_items: output of _extract_memory_items()
        search_query: the composed search query string
        gap_severity: from gap_signal, if present ("high"|"medium"|"low")
        gap_problem: from gap_signal, if present (original problem statement)
    """
    strategy = gap_analysis["strategy"]
    agent_count = gap_analysis["agent_count"]
    missing_dims = gap_analysis.get("missing_dims", [])
    covered = gap_analysis.get("covered_dims", [])

    if strategy == "verify_only" and agent_count == 0:
        return (
            "## [子Agent派发]\n\n"
            "> 记忆经验充足，无需派发外部搜索子Agent。\n"
            "> 可选：派发 1 个验证Agent确认记忆经验的时效性。\n"
        )

    lines = [
        "## [子Agent派发模板]\n",
        "### 结果回流规则（重要）\n",
        "- 子Agent 只做搜索和分析，**不直接写入记忆池**\n",
        "- 子Agent 将结构化 JSON 结果返回给主Agent\n",
        "- 主Agent 合并所有结果后在步骤5统一执行 smart-remember 入库\n",
        "- **合并格式**：主Agent将所有子Agent返回的JSON与[已有经验]合并，\n",
        "  按三问法结构组织到同一份分析文档中，\n",
        "  以 `(来源: Agent N)` 标注每个观察的来源\n",
        "- 避免碎片化，保证入库的是经过合并审核的完整典范分析\n",
    ]

    # Build memory context for sub-agents
    memory_context = "已有经验:\n"
    if covered:
        for item in covered[:3]:
            memory_context += f"- {str(item)[:120]}\n"
    else:
        memory_context += "- (无相关记忆)\n"

    # Severity hint for sub-agent prompts
    severity_hint = ""
    if gap_severity:
        severity_labels = {"high": "高优先级 — 核心架构决策依赖此搜索", "medium": "中优先级", "low": "低优先级 — 可选补充"}
        severity_hint = f"\n问题严重性: {severity_labels.get(gap_severity, gap_severity)}"
        if gap_problem:
            severity_hint += f"\n原始问题: {gap_problem}"

    for i in range(agent_count):
        dim = missing_dims[i] if i < len(missing_dims) else search_query
        lines.append(f"### Agent {i+1}: 搜索 {dim}\n")
        lines.append("```\n")
        lines.append(f"Context from Memory System:\n{memory_context}\n")
        lines.append(f"需要补充的知识缺口: {dim}{severity_hint}\n\n")
        lines.append("Task:\n")
        lines.append(f"1. 使用 WebSearch 搜索 {dim} 的成熟工程实现\n")
        lines.append("2. 对每个找到的典范执行三问法分析:\n")
        lines.append("   a. 它解决了什么工程问题？\n")
        lines.append("   b. 核心模式是什么？（算法/数据结构/流程）\n")
        lines.append("   c. 哪些部分不能直接复用到当前项目？\n")
        lines.append("3. 输出结构化 JSON:\n")
        lines.append('   {"project": "...", "problem_solved": "...",\n')
        lines.append('    "core_pattern": "...", "not_applicable": "...",\n')
        lines.append('    "reusable_parts": [...]}\n')
        lines.append("4. 将 JSON 结果返回给主Agent，不要直接写入记忆池\n")
        lines.append("```\n")

    return "\n".join(lines)
```

- [ ] **Step 2: Add `_build_execution_steps`**

```python
def _build_execution_steps(gap_analysis: dict, gap_severity: str = None) -> str:
    """Generate [执行步骤] markdown section with embedded closure checkpoints.

    Args:
        gap_analysis: output of _analyze_gaps()
        gap_severity: from gap_signal, if present — affects urgency hints
    """
    strategy = gap_analysis["strategy"]
    agent_count = gap_analysis["agent_count"]

    # Severity-aware urgency banner
    severity_banner = ""
    if gap_severity == "high":
        severity_banner = "> :warning: **高优先级知识缺口** — 此搜索影响核心架构决策，建议优先处理。\n\n"

    steps = [
        "## [执行步骤]\n",
        severity_banner,
        "0. **【闭环·起点】** step-closure(mode=\"light\", task_description=\"开始 exemplar-research\")\n",
        "1. **审查 [已有经验]** — 逐条评估适用性，标记可直接复用的模式",
    ]

    if strategy != "verify_only" or agent_count > 0:
        steps.append(
            "   **【闭环·轻量】** 如有可复用发现，smart-remember 提升对应记忆 worth\n"
            "\n"
            "2. **派发子Agent** — 使用 [子Agent派发模板] 并行派发\n"
            "   **【闭环·轻量】** 每完成 1 个子Agent搜索并产出结果后执行 step-closure(mode=\"light\")\n"
        )
    else:
        steps.append(
            "   **【闭环·轻量】** 如有可复用发现，smart-remember 提升对应记忆 worth\n"
        )

    steps.extend([
        "\n",
        "3. **三问法分析** — 合并记忆经验 + 外部搜索结果，生成统一分析\n"
        "   a. 它解决了什么问题？\n"
        "   b. 它怎么解决的？（算法/数据结构/流程）\n"
        "   c. 哪些部分不能直接用？（语言/架构/约束差异）\n"
        "   **【闭环·产出】** step-closure(mode=\"full\",\n"
        "     lesson=\"<三问法关键发现>\",\n"
        "     improvement=\"<下次搜索可优化的方向>\")\n",
        "\n",
        "4. **写分析文档 → git commit**\n"
        "   路径: docs/superpowers/specs/engineering-patterns/YYYY-MM-DD-<project>.md\n"
        "   frontmatter: status=draft\n"
        "   **【闭环·提交】** step-closure(mode=\"full\", git_commit=<hash>)\n",
        "\n",
        "5. **质量审核 + smart-remember 入库 → git commit**\n"
        "   a. task_enqueue(type=\"verify_exemplar\") 自审\n"
        "   b. 审核通过 → smart-remember(memory_type=\"exemplar\") 双写入记忆池\n"
        "   c. 更新 INDEX.md\n"
        "   **【闭环·完成】** step-closure(mode=\"full\", git_commit=<hash>,\n"
        "     lesson=\"<本阶段核心经验>\",\n"
        "     improvement=\"<改进建议>\")\n",
    ])

    return "\n".join(steps)
```

- [ ] **Step 3: Verify imports**

```bash
python -c "from plastic_promise.skills.exemplar_research import _build_agent_templates, _build_execution_steps; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/skills/exemplar_research.py
git commit -m "feat(exemplar-research): add agent templates + execution steps with closure checkpoints"
```

---

### Task 5: Instructions Assembler + Handler Rewrite

**Files:**
- Modify: `plastic_promise/skills/exemplar_research.py:_exemplar_research_handler` (replace current implementation)

**Interfaces:**
- Consumes: all helpers from Tasks 1-4
- Modifies: `_exemplar_research_handler` return value changes — `data.exemplar.instructions` becomes the enhanced multi-section format
- Produces: `_build_enhanced_instructions(params, atom_results) -> str`

- [ ] **Step 1: Add `_build_enhanced_instructions` assembler**

```python
def _build_enhanced_instructions(params: dict, atom_results: dict) -> str:
    """Assemble the complete enhanced instructions from all sections.

    Orchestrates all helper functions and returns a single markdown string
    that becomes data.exemplar.instructions in the SkillResult.
    """
    task_desc = params.get("task_description", "exemplar research")
    gap_signal = params.get("gap_signal", None)

    # Determine search hints
    if gap_signal and isinstance(gap_signal, dict):
        search_hints = gap_signal.get("suggested_search", [])
    else:
        try:
            from plastic_promise.core.exemplar_gap_detector import _extract_keywords
            search_hints = _extract_keywords(task_desc)
        except ImportError:
            search_hints = []

    search_query = " ".join(search_hints) if search_hints else task_desc

    # Extract memory items
    memory_items = _extract_memory_items(atom_results)

    # Analyze gaps
    gap_analysis = _analyze_gaps(memory_items, search_hints)

    # Extract gap_signal metadata for severity-aware downstream rendering
    gap_severity = None
    gap_problem = None
    if gap_signal and isinstance(gap_signal, dict):
        gap_severity = gap_signal.get("severity")
        gap_problem = gap_signal.get("problem")

    # Build all sections
    sections = [
        _build_closure_contract(),
        _format_memory_section(memory_items),
        _format_strategy_section(gap_analysis),
        _build_agent_templates(gap_analysis, memory_items, search_query, gap_severity, gap_problem),
        _build_execution_steps(gap_analysis, gap_severity),
    ]

    return "\n".join(sections)
```

- [ ] **Step 2: Rewrite `_exemplar_research_handler`**

Replace the entire function body with:

```python
async def _exemplar_research_handler(ctx, params, atom_results):
    """Handler for sp-stage exemplar-research.

    Enhanced: injects memory recall results + gap-driven search strategy
    + closure contract + sub-agent dispatch templates into instructions.
    """
    task_desc = params.get("task_description", "exemplar research")
    gap_signal = params.get("gap_signal", None)

    # Determine problem statement and search hints
    if gap_signal and isinstance(gap_signal, dict):
        problem = gap_signal.get("problem", task_desc)
        search_hints = gap_signal.get("suggested_search", [])
    else:
        problem = task_desc
        try:
            from plastic_promise.core.exemplar_gap_detector import _extract_keywords
            search_hints = _extract_keywords(task_desc)
        except ImportError:
            search_hints = []

    search_query = " ".join(search_hints) if search_hints else problem

    # Parse atom results
    principle_data = _parse_atom_result(atom_results.get("principle_activate"))
    store_data = _parse_atom_result(atom_results.get("memory_store"))

    # Build enhanced instructions
    enhanced_instructions = _build_enhanced_instructions(params, atom_results)

    # Legacy instructions (kept for backward compatibility in data.instructions)
    legacy_instructions = (
        "1. 审查 [已有经验] 和 [搜索策略] 段确定执行路径\n"
        "2. 根据 [搜索策略] 决定是否派发子Agent\n"
        "3. 执行三问法分析（合并记忆+外部结果）\n"
        "4. 写分析文档到 engineering-patterns/\n"
        "5. 质量审核 + smart-remember 入库\n"
        "详细步骤见上方 [执行步骤] 段。\n"
    )

    return SkillResult(
        skill_name="sp-exemplar-research",
        success=True,
        data={
            "stage": "exemplar-research",
            "domain": "designing",
            "tags": ["stage:exemplar-research", "domain:designing", "task:research"],
            "principles": principle_data.get("activated", []),
            "memory_id": store_data.get("memory_id", ""),
            "exemplar": {
                "problem": problem,
                "search_query": search_query,
                "search_hints": search_hints,
                "gap_signal": gap_signal,
                "instructions": enhanced_instructions,
                "legacy_instructions": legacy_instructions,
            },
            "transition": "-> exemplar-research -> using-git-worktrees",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )
```

- [ ] **Step 3: Verify handler imports and structure**

```bash
python -c "
from plastic_promise.skills.exemplar_research import (
    _exemplar_research_handler,
    _build_enhanced_instructions,
    EXEMPLAR_RESEARCH_SKILL_DEF,
)
print('All imports OK')
print(f'Skill atoms: {EXEMPLAR_RESEARCH_SKILL_DEF[\"atoms\"]}')
" 2>&1
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/skills/exemplar_research.py
git commit -m "feat(exemplar-research): rewrite handler with hybrid search + closure normalization"
```

---

### Task 6: Tests

**Files:**
- Create: `tests/test_exemplar_research.py`

**Interfaces:**
- Tests: `_extract_memory_items` with empty/mock data
- Tests: `_analyze_gaps` all three strategies + source diversity edge case
- Tests: `_build_enhanced_instructions` contains all required sections
- Tests: handler returns SkillResult with expected structure

- [ ] **Step 1: Write test file**

```python
"""Tests for exemplar_research skill handler and helpers."""
import json
import pytest
from plastic_promise.skills.exemplar_research import (
    _parse_atom_result,
    _extract_memory_items,
    _check_source_diversity,
    _analyze_gaps,
    _format_memory_section,
    _format_strategy_section,
    _build_closure_contract,
    _build_agent_templates,
    _build_execution_steps,
    _build_enhanced_instructions,
)


class TestParseAtomResult:
    def test_empty_result(self):
        assert _parse_atom_result(None) == {}
        assert _parse_atom_result([]) == {}

    def test_valid_json(self):
        class FakeText:
            def __init__(self, text):
                self.text = text
        result = [FakeText('{"core": [{"id": "1", "relevance": 0.9}]}')]
        parsed = _parse_atom_result(result)
        assert parsed["core"][0]["id"] == "1"

    def test_invalid_json(self):
        class FakeText:
            def __init__(self, text):
                self.text = text
        result = [FakeText("not json")]
        parsed = _parse_atom_result(result)
        assert parsed["raw"] == "not json"


class TestExtractMemoryItems:
    def test_empty_atom_results(self):
        items = _extract_memory_items({})
        assert items["has_results"] is False
        assert items["total_core"] == 0

    def test_filters_by_relevance(self):
        items = _extract_memory_items({
            "memory_recall": [
                type("Fake", (), {
                    "text": json.dumps({
                        "core": [
                            {"id": "1", "relevance": 0.95, "content": "high"},
                            {"id": "2", "relevance": 0.60, "content": "low"},
                        ],
                        "related": [
                            {"id": "3", "relevance": 0.50, "content": "mid"},
                            {"id": "4", "relevance": 0.30, "content": "vlow"},
                        ],
                    })
                })()
            ]
        })
        assert items["total_core"] == 1  # only 0.95 passes >=0.70
        assert items["total_related"] == 1  # only 0.50 passes >=0.45
        assert items["core"][0]["content"] == "high"


class TestSourceDiversity:
    def test_unique_sources(self):
        items = [{"id": "a", "source": "s1"}, {"id": "b", "source": "s2"}, {"id": "c", "source": "s1"}]
        assert _check_source_diversity(items) == 2

    def test_single_source(self):
        items = [{"id": "a", "source": "s1"}, {"id": "b", "source": "s1"}]
        assert _check_source_diversity(items) == 1

    def test_empty(self):
        assert _check_source_diversity([]) == 0

    def test_fallback_to_id(self):
        items = [{"id": "a"}, {"id": "b"}]
        assert _check_source_diversity(items) == 2


class TestAnalyzeGaps:
    def test_full_search_when_empty(self):
        result = _analyze_gaps({"core": [], "related": [], "total_core": 0}, ["rust", "storage"])
        assert result["strategy"] == "full_search"
        assert result["agent_count"] >= 2

    def test_fill_gaps_when_partial(self):
        result = _analyze_gaps(
            {"core": [{"id": "1", "relevance": 0.85, "content": "rust pattern"}], "total_core": 1},
            ["rust", "storage"],
        )
        assert result["strategy"] == "fill_gaps"

    def test_verify_only_with_diverse_sources(self):
        core = [
            {"id": "1", "source": "proj_a", "content": "pattern 1"},
            {"id": "2", "source": "proj_b", "content": "pattern 2"},
            {"id": "3", "source": "proj_c", "content": "pattern 3"},
        ]
        result = _analyze_gaps({"core": core, "total_core": 3}, ["test"])
        assert result["strategy"] == "verify_only"

    def test_source_monoculture_downgrades_to_fill_gaps(self):
        core = [
            {"id": "1", "source": "same_project", "content": "p1"},
            {"id": "2", "source": "same_project", "content": "p2"},
            {"id": "3", "source": "same_project", "content": "p3"},
        ]
        result = _analyze_gaps({"core": core, "total_core": 3}, ["test"])
        assert result["strategy"] == "fill_gaps"
        assert "来源单一" in result["rationale"]


class TestFormatSections:
    def test_memory_section_with_results(self):
        items = {
            "core": [{"id": "m1", "relevance": 0.90, "content": "test memory"}],
            "related": [],
        }
        section = _format_memory_section(items)
        assert "[已有经验]" in section
        assert "test memory" in section
        assert "90%" in section

    def test_memory_section_empty(self):
        section = _format_memory_section({"core": [], "related": []})
        assert "暂无" in section

    def test_strategy_section(self):
        gap = {"strategy": "fill_gaps", "agent_count": 2, "rationale": "test", "missing_dims": ["rust"]}
        section = _format_strategy_section(gap)
        assert "fill_gaps" in section
        assert "rust" in section

    def test_closure_contract(self):
        contract = _build_closure_contract()
        assert "[闭环契约]" in contract
        assert "step-closure" in contract
        assert "不跳过" in contract


class TestAgentTemplates:
    def test_verify_only_no_agents(self):
        gap = {"strategy": "verify_only", "agent_count": 0, "missing_dims": []}
        result = _build_agent_templates(gap, {"core": [], "related": []}, "test query")
        assert "无需派发" in result

    def test_full_search_generates_templates(self):
        gap = {"strategy": "full_search", "agent_count": 2, "missing_dims": ["rust", "storage"], "covered_dims": []}
        result = _build_agent_templates(gap, {"core": [], "related": []}, "rust storage")
        assert "Agent 1" in result
        assert "Agent 2" in result
        assert "结果回流规则" in result
        assert "不直接写入记忆池" in result


class TestExecutionSteps:
    def test_steps_include_closure_checkpoints(self):
        gap = {"strategy": "full_search", "agent_count": 2}
        steps = _build_execution_steps(gap)
        assert "闭环·起点" in steps
        assert "闭环·产出" in steps
        assert "闭环·提交" in steps
        assert "闭环·完成" in steps

    def test_verify_only_skips_agent_step(self):
        gap = {"strategy": "verify_only", "agent_count": 0}
        steps = _build_execution_steps(gap)
        assert "派发子Agent" not in steps

    def test_high_severity_adds_urgency_banner(self):
        gap = {"strategy": "full_search", "agent_count": 2}
        steps = _build_execution_steps(gap, gap_severity="high")
        assert "高优先级" in steps

    def test_low_severity_no_banner(self):
        gap = {"strategy": "full_search", "agent_count": 2}
        steps = _build_execution_steps(gap, gap_severity="low")
        assert "高优先级" not in steps


class TestBuildEnhancedInstructions:
    def test_contains_all_sections(self):
        atom_results = {}
        instructions = _build_enhanced_instructions(
            {"task_description": "Rust storage engine design"},
            atom_results,
        )
        assert "[闭环契约]" in instructions
        assert "[已有经验]" in instructions
        assert "[搜索策略]" in instructions
        assert "[子Agent派发" in instructions
        assert "[执行步骤]" in instructions

    def test_with_gap_signal(self):
        atom_results = {}
        instructions = _build_enhanced_instructions(
            {
                "task_description": "test",
                "gap_signal": {"suggested_search": ["distributed", "consensus"]},
            },
            atom_results,
        )
        assert "distributed" in instructions.lower()
        assert "consensus" in instructions.lower()

    def test_gap_signal_severity_passed_through(self):
        """Verify gap_signal severity flows through to execution steps."""
        atom_results = {}
        instructions = _build_enhanced_instructions(
            {
                "task_description": "critical architecture decision",
                "gap_signal": {
                    "suggested_search": ["raft", "paxos"],
                    "severity": "high",
                    "problem": "选择分布式一致性算法的核心架构决策",
                },
            },
            atom_results,
        )
        assert "高优先级" in instructions
        assert "raft" in instructions.lower()
        assert "paxos" in instructions.lower()
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest tests/test_exemplar_research.py -v 2>&1
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_exemplar_research.py
git commit -m "test(exemplar-research): add unit tests for hybrid search + closure helpers"
```

---

### Task 7: End-to-End Verification + Step-Closure

**Files:**
- (no code changes — verification only)

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/ -v --tb=short 2>&1
```

Expected: no regressions. All existing + new tests pass.

- [ ] **Step 2: Integration smoke test — call handler directly**

```bash
python -c "
import asyncio, json
from plastic_promise.skills.exemplar_research import _exemplar_research_handler

async def test():
    result = await _exemplar_research_handler(
        ctx=None,
        params={'task_description': 'Rust 存储引擎设计', 'gap_signal': None},
        atom_results={},
    )
    data = result.data
    instructions = data['exemplar']['instructions']
    
    # Verify all sections present
    checks = [
        ('[闭环契约]', 'closure contract'),
        ('[已有经验]', 'memory section'),
        ('[搜索策略]', 'strategy section'),
        ('[执行步骤]', 'execution steps'),
    ]
    for keyword, label in checks:
        assert keyword in instructions, f'MISSING: {label}'
    
    print('All sections present')
    print(f'Instructions length: {len(instructions)} chars')
    print('---First 500 chars---')
    print(instructions[:500])

asyncio.run(test())
" 2>&1
```

Expected: "All sections present" with instructions containing all 4 required sections.

- [ ] **Step 3: Final step-closure**

```bash
git add -A && git status
```

Confirm only expected files changed, then:

```bash
git commit -m "chore(exemplar-research): final verification — all sections present in enhanced instructions"
```

- [ ] **Step 4: Run step-closure via MCP**

```
step-closure(
  task_description="exemplar-research 混合增强 + 闭环常态化实施完成: 重写 handler ~150行, 新增5个辅助函数 + 9个测试类",
  git_commit="<final-commit-hash>",
  mode="full",
  lesson="纯函数辅助 + 编排 handler 模式适合指令生成类增强: 每个 helper 独立可测, handler 只做编排",
  improvement="推广到其他阶段时抽取公共的 _build_closure_contract() 和缺口分析逻辑到共享模块",
  root_cause="当前 exemplar-research 不查记忆不派发子Agent, 搜索结果依赖执行者即兴发挥",
  optimization="将 _build_closure_contract 和 _analyze_gaps 抽取到 plastic_promise.skills.closure_utils 供其他阶段复用"
)
```

---

## Self-Review Checklist

1. **Spec coverage:** Each section in the spec is implemented:
   - [闭环契约] → `_build_closure_contract()` + Task 3
   - [已有经验] → `_extract_memory_items()` + `_format_memory_section()` + Tasks 1, 3
   - [搜索策略] → `_analyze_gaps()` + `_format_strategy_section()` + Tasks 2, 3
   - [子Agent模板] → `_build_agent_templates()` + Task 4
   - [执行步骤] → `_build_execution_steps()` + Task 4
   - 来源多样性检查 → `_check_source_diversity()` + Task 2
   - 结果回流规则 → embedded in `_build_agent_templates()` + Task 4

2. **Placeholder scan:** No TBD/TODO/incomplete sections. All code is concrete and complete.

3. **Type consistency:** All function signatures are consistent across tasks. `_build_enhanced_instructions` consumes exactly what Tasks 1-4 produce.
