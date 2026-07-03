"""Exemplar Research -- sp-stage handler for exemplar-research phase.

Part of the SuperPowers 12-stage pipeline. Sits between brainstorming
and using-git-worktrees in the main chain.

Execution flow:
  1. Read gap_signal (if present) or extract search intent from task_description
  2. WebSearch for mature implementations
  3. Three-question analysis (problem / pattern / constraints)
  4. Write analysis doc to engineering-patterns/ (status=draft)
  5. Quality review via verify_exemplar task
  6. On approval -> smart-remember dual-write to memory pool
  7. Complete -> exemplar available in subsequent context_supply calls
"""

import json

from plastic_promise.skills.engine import SkillResult

# ---------------------------------------------------------------------------
# Memory extraction helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Gap analysis + source diversity
# ---------------------------------------------------------------------------


def _check_source_diversity(core_items: list[dict]) -> int:
    """Count unique sources among core memory items.

    Uses item["source"] if present, falls back to item["id"].
    Returns the count of distinct sources.
    """
    if not core_items:
        return 0
    sources = {item.get("source", item.get("id", "")) for item in core_items}
    return len(sources)


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
            "rationale": (f"记忆系统已有{core_count}条高相关经验(来源{sources}个)，仅需外部验证"),
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
            "covered_dims": list(covered_keywords)
            or [item.get("content", "")[:60] for item in core],
            "missing_dims": missing or search_hints[:1],
            "rationale": (
                f"记忆覆盖了{covered_keywords or '部分维度'}，缺少{missing or '外部验证'}"
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


# ---------------------------------------------------------------------------
# Instructions section builders
# ---------------------------------------------------------------------------


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
            pct = int(item["relevance"] * 100)
            lines.append(
                f"{i}. **[{pct}%]** {item['content']}\n"
                f"   - 来源: `{item['id']}`\n"
                f"   - 评估: 高相关，核心模式可直接复用\n"
            )

    if related:
        lines.append("### 可参考借鉴 (中相关)\n")
        for i, item in enumerate(related, 1):
            pct = int(item["relevance"] * 100)
            lines.append(
                f"{i}. **[{pct}%]** {item['content']}\n"
                f"   - 来源: `{item['id']}`\n"
                f"   - 差异: 关联但领域/场景不同，参考思路而非直接复用\n"
            )

    return "\n".join(lines) + "\n"


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


# ---------------------------------------------------------------------------
# Agent templates + execution steps
# ---------------------------------------------------------------------------


def _build_agent_templates(
    gap_analysis: dict,
    memory_items: dict,
    search_query: str,
    gap_severity: str = None,
    gap_problem: str = None,
) -> str:
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
        "- **合并格式**：主Agent将所有子Agent返回的JSON与[已有经验]合并，\n",
        "  按三问法结构组织到同一份分析文档中，\n",
        "  以 `(来源: Agent N)` 标注每个观察的来源\n",
        "- 主Agent 合并所有结果后在步骤5统一执行 smart-remember 入库\n",
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
        severity_labels = {
            "high": "高优先级 — 核心架构决策依赖此搜索",
            "medium": "中优先级",
            "low": "低优先级 — 可选补充",
        }
        severity_hint = f"\n问题严重性: {severity_labels.get(gap_severity, gap_severity)}"
        if gap_problem:
            severity_hint += f"\n原始问题: {gap_problem}"

    for i in range(agent_count):
        dim = missing_dims[i] if i < len(missing_dims) else search_query
        lines.append(f"### Agent {i + 1}: 搜索 {dim}\n")
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
        severity_banner = (
            "> :warning: **高优先级知识缺口** — 此搜索影响核心架构决策，建议优先处理。\n\n"
        )

    steps = [
        "## [执行步骤]\n",
        severity_banner,
        "0. **【闭环·起点】** step-closure("
        'mode="light", task_description="开始 exemplar-research"'
        ")\n",
        "1. **审查 [已有经验]** — 逐条评估适用性，标记可直接复用的模式",
    ]

    if strategy != "verify_only" or agent_count > 0:
        steps.append(
            "   **【闭环·轻量】** 如有可复用发现，"
            "smart-remember 提升对应记忆 worth\n"
            "\n"
            "2. **派发子Agent** — 使用 [子Agent派发模板] 并行派发\n"
            "   **【闭环·轻量】** 每完成 1 个子Agent搜索并产出结果后"
            ' 执行 step-closure(mode="light")\n'
        )
    else:
        steps.append("   **【闭环·轻量】** 如有可复用发现，smart-remember 提升对应记忆 worth\n")

    steps.extend(
        [
            "\n",
            "3. **三问法分析** — 合并记忆经验 + 外部搜索结果，生成统一分析\n"
            "   a. 它解决了什么问题？\n"
            "   b. 它怎么解决的？（算法/数据结构/流程）\n"
            "   c. 哪些部分不能直接用？（语言/架构/约束差异）\n"
            '   **【闭环·产出】** step-closure(mode="full",\n'
            '     lesson="<三问法关键发现>",\n'
            '     improvement="<下次搜索可优化的方向>")\n',
            "\n",
            "4. **写分析文档 → git commit**\n"
            "   路径: docs/superpowers/specs/engineering-patterns/"
            "YYYY-MM-DD-<project>.md\n"
            "   frontmatter: status=draft\n"
            '   **【闭环·提交】** step-closure(mode="full", git_commit=<hash>)\n',
            "\n",
            "5. **质量审核 + smart-remember 入库 → git commit**\n"
            '   a. task_enqueue(type="verify_exemplar") 自审\n'
            '   b. 审核通过 → smart-remember(memory_type="exemplar") 双写入记忆池\n'
            "   c. 更新 INDEX.md\n"
            '   **【闭环·完成】** step-closure(mode="full", git_commit=<hash>,\n'
            '     lesson="<本阶段核心经验>",\n'
            '     improvement="<改进建议>")\n',
        ]
    )

    return "\n".join(steps)


# ---------------------------------------------------------------------------
# Enhanced instructions assembler
# ---------------------------------------------------------------------------


def _build_enhanced_instructions(params: dict, atom_results: dict) -> str:
    """Assemble the complete enhanced instructions from all sections.

    Orchestrates all helper functions and returns a single markdown string
    that becomes data.exemplar.instructions in the SkillResult.
    """
    task_desc = params.get("task_description", "exemplar research")
    gap_signal = params.get("gap_signal")

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


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _exemplar_research_handler(ctx, params, atom_results):
    """Handler for sp-stage exemplar-research.

    ctx: ContextEngine instance
    params: dict with task_description and optional gap_signal
    atom_results: dict with principle_activate and memory_store results
    """
    task_desc = params.get("task_description", "exemplar research")
    gap_signal = params.get("gap_signal", None)

    # -- 1. Determine search target ---------------------------------
    if gap_signal and isinstance(gap_signal, dict):
        problem = gap_signal.get("problem", task_desc)
        search_hints = gap_signal.get("suggested_search", [])
    else:
        problem = task_desc
        # Extract search hints from task_description
        try:
            from plastic_promise.core.exemplar_gap_detector import _extract_keywords

            search_hints = _extract_keywords(task_desc)
        except ImportError:
            search_hints = []

    search_query = " ".join(search_hints) if search_hints else problem

    # -- 2. Parse atom results --------------------------------------
    principle_data = _parse_atom_result(atom_results.get("principle_activate"))
    store_data = _parse_atom_result(atom_results.get("memory_store"))

    # -- 3. Return SkillResult with exemplar context -----------------
    # The actual WebSearch + analysis + doc writing + review
    # is performed by Claude in the conversation after receiving
    # this result. The handler provides the structured context
    # and instructions.

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
                "instructions": _build_enhanced_instructions(params, atom_results),
                "legacy_instructions": (
                    "1. 审查 [已有经验] 和 [搜索策略] 段确定执行路径\n"
                    "2. 根据 [搜索策略] 决定是否派发子Agent\n"
                    "3. 执行三问法分析（合并记忆+外部结果）\n"
                    "4. 写分析文档到 engineering-patterns/\n"
                    "5. 质量审核 + smart-remember 入库\n"
                    "详细步骤见上方 [执行步骤] 段。\n"
                ),
            },
            "transition": "-> exemplar-research -> using-git-worktrees",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )


# ============================================================
# SkillDef registration
# ============================================================

EXEMPLAR_RESEARCH_SKILL_DEF = {
    "name": "sp-exemplar-research",
    "domain": "superpowers_stages",
    "description": "SuperPowers stage: exemplar research -- search mature implementations, three-question analysis, write analysis doc, quality review then store",
    "tier": "P0",
    "atoms": ["principle_activate", "memory_store"],
    "degrade_map": {
        "principle_activate": "skip",
        "memory_store": "warn",
    },
    "handler": _exemplar_research_handler,
    "allowed_callers": ["claude", "pi", "trae"],
}
