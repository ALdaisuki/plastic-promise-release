"""Plastic Promise MCP Server — 全过程 MCP 化入口

启动方式:
    python -m plastic_promise.mcp.server              # stdio 模式 (Claude Code 直接调用)
    python -m plastic_promise.mcp.server --http 9020  # HTTP 模式 (外部调试)

架构:
    MCP Server
    ├── 7 个工具组 (tools/)
    ├── Resources (resources.py)
    └── Prompts (prompts.py)

所有工具共享 ContextEngine 单例，通过依赖注入传递给各工具模块。
"""

import sys
import os
import json
import logging
from typing import Any

# 确保项目根在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    Resource,
    Prompt,
    PromptMessage,
    GetPromptResult,
)

from plastic_promise.core.constants import (
    CORE_PRINCIPLES,
    DEFENSE_LAYERS,
    AUDIT_DIMENSIONS,
    SCARF_DIMENSIONS,
)

# ---------------------------------------------------------------------------
# 全局 ContextEngine 代理 (Rust 不可用时回退到 Python mock)
# ---------------------------------------------------------------------------

_engine = None  # 延迟初始化


def get_engine():
    """获取 ContextEngine 单例（Rust 优先，Python 回退）"""
    global _engine
    if _engine is not None:
        return _engine

    try:
        from context_engine_core import ContextEngine as RustEngine
        _engine = RustEngine()
        logging.info("ContextEngine: Rust 核心已加载")
    except ImportError:
        logging.warning("ContextEngine: Rust 不可用，使用 Python Mock")
        from plastic_promise.core.context_engine import ContextEngine as PyEngine
        _engine = PyEngine()
    return _engine


# ---------------------------------------------------------------------------
# MCP Server 实例
# ---------------------------------------------------------------------------

server = Server("plastic-promise", version="0.1.0")

# ---------------------------------------------------------------------------
# 能力声明
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    """声明所有 MCP 工具"""
    tools: list[Tool] = []

    # === 记忆域 ===
    tools.extend([
        Tool(
            name="memory_recall",
            description="混合检索记忆（文本+图遍历双通道），返回三层上下文包。支持关键词、任务类型、时间范围过滤。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询 / 任务描述"},
                    "task_type": {"type": "string", "description": "任务类型: code_generation/code_review/debugging/architecture/refactoring/learning/collaboration"},
                    "max_results": {"type": "integer", "description": "最大返回数 (默认 20)"},
                    "min_relevance": {"type": "number", "description": "最低关联分数 (默认 0.2)"},
                    "include_principles": {"type": "boolean", "description": "是否注入原则 (默认 true)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_store",
            description="存储一条记忆到 Plastic Promise 记忆池。自动分类 (task/experience/principle/code) 并建立实体关联。",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容"},
                    "memory_type": {"type": "string", "description": "类型: task/experience/principle/code"},
                    "source": {"type": "string", "description": "来源: user/system/previous_output"},
                    "entity_ids": {"type": "array", "items": {"type": "string"}, "description": "关联实体 ID 列表"},
                },
                "required": ["content", "memory_type"],
            },
        ),
        Tool(
            name="memory_update",
            description="更新已有记忆的内容或元数据。更新后重置 worth 计数以重新评估。",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆 ID"},
                    "content": {"type": "string", "description": "新内容"},
                    "reset_worth": {"type": "boolean", "description": "是否重置 worth 计数器"},
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_forget",
            description="软删除记忆（标记为衰退，7天后 GC 清理）。不会立即删除，可恢复。",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆 ID"},
                    "reason": {"type": "string", "description": "删除原因"},
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memory_stats",
            description="获取记忆池统计信息：总量、健康/衰退分布、类型分布、worth 分布。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="memory_list",
            description="按条件列出记忆：类型、来源、时间范围、worth 范围。",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "description": "筛选类型"},
                    "source": {"type": "string", "description": "筛选来源"},
                    "min_worth": {"type": "number", "description": "最低 worth_score"},
                    "limit": {"type": "integer", "description": "返回数量上限"},
                },
            },
        ),
        Tool(
            name="memory_gc",
            description="手动触发垃圾回收：清除 worth_score 低于阈值且超过 7 天未访问的衰退记忆。",
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "description": "仅预览，不实际删除 (默认 true)"},
                    "force": {"type": "boolean", "description": "强制删除所有标记记忆"},
                },
            },
        ),
        Tool(
            name="fuzzy_status",
            description="查看模糊缓存区统计：各区数量、待处理量、最旧条目时间。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="fuzzy_process",
            description="手动触发模糊缓存区处理流水线：raw→tagged→embedded→classified→迁移到主记忆池。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="memory_correct",
            description="人类纠正记忆：编辑内容、标记为错误/已废弃/已纠正。服务于原则 2（可查可透明）和原则 3（审计闭环）。",
            inputSchema={
                "type": "object",
                "required": ["memory_id"],
                "properties": {
                    "memory_id": {"type": "string", "description": "目标记忆 ID"},
                    "content": {"type": "string", "description": "纠正后的新内容 (可选)"},
                    "mark_as": {"type": "string", "description": "质量标记: corrected / deprecated / wrong"},
                    "reason": {"type": "string", "description": "纠正原因说明"},
                },
            },
        ),
    ])

    # === 原则域 ===
    tools.extend([
        Tool(
            name="principle_activate",
            description="根据任务类型自动激活相关核心原则。返回原则列表及其关联权重。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_type": {"type": "string", "description": "任务类型"},
                    "task_description": {"type": "string", "description": "任务描述（用于关键词匹配）"},
                    "max_principles": {"type": "integer", "description": "最多返回原则数"},
                },
                "required": ["task_type"],
            },
        ),
        Tool(
            name="principle_inherit",
            description="触发原则单向扩散：work→all 或 life→all，权重按同步衰减系数 (0.70) 传播。",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_domain": {"type": "string", "description": "源域: work/life"},
                    "target_domain": {"type": "string", "description": "目标域: all"},
                    "principle_ids": {"type": "array", "items": {"type": "string"}, "description": "要扩散的原则 ID 列表（空=全部）"},
                },
                "required": ["source_domain", "target_domain"],
            },
        ),
        Tool(
            name="principle_diffuse",
            description="查询原则在域间的传播状态：当前激活域、传播路径、衰减后的权重。",
            inputSchema={
                "type": "object",
                "properties": {
                    "principle_id": {"type": "string", "description": "原则 ID (空=全部)"},
                },
            },
        ),
        Tool(
            name="principle_evaluate",
            description="反事实评估：对指定原则进行「如果违反会怎样」的预演，为 Agent 提供非强制但充分的决策依据。",
            inputSchema={
                "type": "object",
                "properties": {
                    "principle_id": {"type": "string", "description": "原则 ID"},
                    "scenario": {"type": "string", "description": "当前决策场景描述"},
                },
                "required": ["principle_id", "scenario"],
            },
        ),
    ])

    # === 上下文域 ===
    tools.extend([
        Tool(
            name="context_supply",
            description="【核心工具】调用 ContextEngine.supply()，返回三层结构化上下文包：🔵核心层/🟡关联层/🟢发散层。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {"type": "string", "description": "当前任务的完整自然语言描述（含前文上下文）"},
                    "task_type": {"type": "string", "description": "任务类型标签"},
                    "scope": {"type": "string", "description": "检索范围: global (默认) 或 domain 限定"},
                },
                "required": ["task_description"],
            },
        ),
        Tool(
            name="context_inject",
            description="手动向 EntityGraph 注入原则关联边，或注册新实体节点。",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "description": "实体类型: task/principle/code_module/memory"},
                    "entity_id": {"type": "string"},
                    "entity_name": {"type": "string"},
                    "entity_description": {"type": "string"},
                    "related_entities": {"type": "array", "items": {"type": "string"}, "description": "关联实体 ID"},
                },
                "required": ["entity_type", "entity_id", "entity_name"],
            },
        ),
        Tool(
            name="context_graph",
            description="查询实体关联图谱：节点列表、边关系、多跳遍历、激活路径可视化数据。",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_node": {"type": "string", "description": "起始节点 ID"},
                    "max_hops": {"type": "integer", "description": "最大跳数 (默认 3)"},
                    "query_type": {"type": "string", "description": "查询类型: traverse/node_info/edge_list/activated_principles"},
                },
            },
        ),
    ])

    # === 审计与防线 ===
    tools.extend([
        Tool(
            name="audit_run",
            description="执行七维度审计，返回结构化评分报告（原则联想/记忆供应/约束合规/反馈闭环/信任校准/原则继承/安全追溯）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "审计范围: full/quick/principles_only/memory_only"},
                    "time_range_hours": {"type": "integer", "description": "审计时间范围（小时）"},
                },
            },
        ),
        Tool(
            name="audit_pre_check",
            description="实时合规检查：对即将执行的操作进行 L0 硬边界和 L1 约束衰减检查。",
            inputSchema={
                "type": "object",
                "properties": {
                    "action_description": {"type": "string", "description": "操作描述"},
                    "action_type": {"type": "string", "description": "操作类型: exec/write/edit/delete/read"},
                },
                "required": ["action_description"],
            },
        ),
        Tool(
            name="audit_report",
            description="获取最近一次审计报告全文或指定维度的详细分析。",
            inputSchema={
                "type": "object",
                "properties": {
                    "dimension": {"type": "string", "description": "维度名 (空=全部)"},
                    "format": {"type": "string", "description": "输出格式: json/markdown/summary"},
                },
            },
        ),
        Tool(
            name="defense_trust",
            description="查看当前信任分及其变化历史，或手动调整信任分（需要原因备注）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "操作: get/history/adjust"},
                    "delta": {"type": "number", "description": "调整量 (±0.01 ~ ±0.10)"},
                    "reason": {"type": "string", "description": "调整原因"},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="defense_status",
            description="获取三层防线当前状态：L0 硬边界/L1 约束衰减（含信任分驱动的切换状态）/L2 免疫巡检。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ])

    # === 自省与演化 ===
    tools.extend([
        Tool(
            name="scarf_reflect",
            description="执行 SCARF 五维度自省：Status/Certainty/Autonomy/Relatedness/Fairness，返回结构化评分和建议。",
            inputSchema={
                "type": "object",
                "properties": {
                    "context": {"type": "string", "description": "当前上下文/最近行为描述"},
                    "dimensions": {"type": "array", "items": {"type": "string"}, "description": "指定维度 (空=全部)"},
                },
                "required": ["context"],
            },
        ),
        Tool(
            name="inertia_check",
            description="惯性抑制检测：检查最近 N 个任务是否过于相似，给出探索建议。",
            inputSchema={
                "type": "object",
                "properties": {
                    "recent_tasks": {"type": "array", "items": {"type": "string"}, "description": "最近任务描述列表"},
                },
            },
        ),
        Tool(
            name="feedback_apply",
            description="向记忆或上下文条目手动应用反馈：adopted/ignored/rejected，更新 worth 计数器和自演化权重。",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "条目 ID"},
                    "feedback_type": {"type": "string", "description": "反馈类型: adopted/ignored/rejected"},
                    "task_context": {"type": "string", "description": "触发反馈的任务上下文"},
                },
                "required": ["item_id", "feedback_type"],
            },
        ),
    ])

    # === 管理域 ===
    tools.extend([
        Tool(
            name="system_stats",
            description="获取 Plastic Promise 系统整体统计：九大系统健康度/CEI 指数/记忆池状态/信任分趋势/图规模。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="system_backup",
            description="导出 Plastic Promise 完整状态：记忆池/原则图谱/信任分/审计历史。",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "导出格式: json/sqlite"},
                    "include_audit_history": {"type": "boolean"},
                },
            },
        ),
        Tool(
            name="system_migrate",
            description="从其他记忆系统迁移数据到 Plastic Promise（兼容 memory-lancedb / memory-lancedb-pro 格式）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {"type": "string", "description": "源数据路径"},
                    "source_type": {"type": "string", "description": "源类型: lancedb/json/csv"},
                    "dry_run": {"type": "boolean", "description": "仅预览，不实际导入"},
                },
                "required": ["source_path", "source_type"],
            },
        ),
    ])

    return tools


# ---------------------------------------------------------------------------
# 工具调用路由
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Route MCP tool calls to handler modules.

    Each tool domain is delegated to its own module under
    plastic_promise.mcp.tools.* for clean separation of concerns.
    Handlers are lazily imported on first call.
    """
    engine = get_engine()

    try:
        # Memory domain
        if name == "memory_recall":
            from plastic_promise.mcp.tools.memory import handle_memory_recall
            return await handle_memory_recall(engine, arguments)
        elif name == "memory_store":
            from plastic_promise.mcp.tools.memory import handle_memory_store
            return await handle_memory_store(engine, arguments)
        elif name == "memory_update":
            from plastic_promise.mcp.tools.memory import handle_memory_update
            return await handle_memory_update(engine, arguments)
        elif name == "memory_forget":
            from plastic_promise.mcp.tools.memory import handle_memory_forget
            return await handle_memory_forget(engine, arguments)
        elif name == "memory_stats":
            from plastic_promise.mcp.tools.memory import handle_memory_stats
            return await handle_memory_stats(engine, arguments)
        elif name == "memory_list":
            from plastic_promise.mcp.tools.memory import handle_memory_list
            return await handle_memory_list(engine, arguments)
        elif name == "memory_gc":
            from plastic_promise.mcp.tools.memory import handle_memory_gc
            return await handle_memory_gc(engine, arguments)
        elif name == "fuzzy_status":
            from plastic_promise.mcp.tools.memory import handle_fuzzy_status
            return await handle_fuzzy_status(engine, arguments)
        elif name == "fuzzy_process":
            from plastic_promise.mcp.tools.memory import handle_fuzzy_process
            return await handle_fuzzy_process(engine, arguments)
        elif name == "memory_correct":
            from plastic_promise.mcp.tools.memory import handle_memory_correct
            return await handle_memory_correct(engine, arguments)

        # Principle domain
        elif name == "principle_activate":
            from plastic_promise.mcp.tools.principles import handle_principle_activate
            return await handle_principle_activate(engine, arguments)
        elif name == "principle_inherit":
            from plastic_promise.mcp.tools.principles import handle_principle_inherit
            return await handle_principle_inherit(engine, arguments)
        elif name == "principle_diffuse":
            from plastic_promise.mcp.tools.principles import handle_principle_diffuse
            return await handle_principle_diffuse(engine, arguments)
        elif name == "principle_evaluate":
            from plastic_promise.mcp.tools.principles import handle_principle_evaluate
            return await handle_principle_evaluate(engine, arguments)

        # Context domain
        elif name == "context_supply":
            from plastic_promise.mcp.tools.context import handle_context_supply
            return await handle_context_supply(engine, arguments)
        elif name == "context_inject":
            from plastic_promise.mcp.tools.context import handle_context_inject
            return await handle_context_inject(engine, arguments)
        elif name == "context_graph":
            from plastic_promise.mcp.tools.context import handle_context_graph
            return await handle_context_graph(engine, arguments)

        # Audit and defense
        elif name == "audit_run":
            from plastic_promise.mcp.tools.audit_defense import handle_audit_run
            return await handle_audit_run(engine, arguments)
        elif name == "audit_pre_check":
            from plastic_promise.mcp.tools.audit_defense import handle_audit_pre_check
            return await handle_audit_pre_check(engine, arguments)
        elif name == "audit_report":
            from plastic_promise.mcp.tools.audit_defense import handle_audit_report
            return await handle_audit_report(engine, arguments)
        elif name == "defense_trust":
            from plastic_promise.mcp.tools.audit_defense import handle_defense_trust
            return await handle_defense_trust(engine, arguments)
        elif name == "defense_status":
            from plastic_promise.mcp.tools.audit_defense import handle_defense_status
            return await handle_defense_status(engine, arguments)

        # Reflection
        elif name == "scarf_reflect":
            from plastic_promise.mcp.tools.reflection import handle_scarf_reflect
            return await handle_scarf_reflect(engine, arguments)
        elif name == "inertia_check":
            from plastic_promise.mcp.tools.reflection import handle_inertia_check
            return await handle_inertia_check(engine, arguments)
        elif name == "feedback_apply":
            from plastic_promise.mcp.tools.reflection import handle_feedback_apply
            return await handle_feedback_apply(engine, arguments)

        # Management
        elif name == "system_stats":
            from plastic_promise.mcp.tools.management import handle_system_stats
            return await handle_system_stats(engine, arguments)
        elif name == "system_backup":
            from plastic_promise.mcp.tools.management import handle_system_backup
            return await handle_system_backup(engine, arguments)
        elif name == "system_migrate":
            from plastic_promise.mcp.tools.management import handle_system_migrate
            return await handle_system_migrate(engine, arguments)

        else:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Unknown tool: {name}"}, ensure_ascii=False))]
    except Exception as e:
        logging.exception(f"Tool {name} failed")
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": name}, ensure_ascii=False))]


# ===================================================================
# Resources
# ===================================================================

@server.list_resources()
async def list_resources() -> list[Resource]:
    """声明 MCP Resources — 系统数据的只读视图"""
    return [
        Resource(
            uri="plastic-promise://principles",
            name="核心原则列表",
            description="11 条核心原则的完整定义",
            mimeType="application/json",
        ),
        Resource(
            uri="plastic-promise://systems",
            name="九大数字身体系统",
            description="九大系统的名称、类比、成熟度和模块组成",
            mimeType="application/json",
        ),
        Resource(
            uri="plastic-promise://trust-history",
            name="信任分变化历史",
            description="信任分随时间变化的时序数据",
            mimeType="application/json",
        ),
        Resource(
            uri="plastic-promise://audit-latest",
            name="最新审计报告",
            description="最近一次七维度审计的完整报告",
            mimeType="application/json",
        ),
        Resource(
            uri="plastic-promise://memory-stats",
            name="记忆池统计",
            description="记忆总量、健康/衰退分布、类型分布、worth 分布",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    """读取 MCP Resource"""
    if uri == "plastic-promise://principles":
        return json.dumps(CORE_PRINCIPLES, ensure_ascii=False, indent=2)
    elif uri == "plastic-promise://systems":
        from plastic_promise.core.constants import DIGITAL_BODY_SYSTEMS
        return json.dumps(DIGITAL_BODY_SYSTEMS, ensure_ascii=False, indent=2)
    elif uri == "plastic-promise://trust-history":
        return json.dumps({"trust_history": [], "current_trust": 0.60}, ensure_ascii=False)
    elif uri == "plastic-promise://audit-latest":
        return json.dumps({"message": "No audit run yet"}, ensure_ascii=False)
    elif uri == "plastic-promise://memory-stats":
        return json.dumps({"total_memories": 0, "healthy": 0, "decaying": 0}, ensure_ascii=False)
    return json.dumps({"error": f"Unknown resource: {uri}"})


# ===================================================================
# Prompts
# ===================================================================

@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    """声明 MCP Prompts — 标准操作流程模板"""
    return [
        Prompt(
            name="run-full-audit",
            description="执行完整的七维度审计流程",
            arguments=[
                {"name": "scope", "description": "审计范围: full/quick"},
            ],
        ),
        Prompt(
            name="check-principle-alignment",
            description="检查当前决策是否与核心原则对齐",
            arguments=[
                {"name": "decision", "description": "当前决策描述"},
            ],
        ),
        Prompt(
            name="daily-reflection",
            description="每日 SCARF 自省 + 记忆演化检查",
            arguments=[],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    """获取 MCP Prompt 模板"""
    if name == "run-full-audit":
        scope = (arguments or {}).get("scope", "full")
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=f"请执行{scope}范围的七维度审计。\n\n"
                    f"审计维度：原则联想/记忆供应/约束合规/反馈闭环/信任校准/原则继承/安全追溯。\n"
                    f"返回每个维度的评分（0.0-1.0）、发现的问题、建议的修复措施。\n"
                    f"如果评分低于 0.60，标记为 P0 并立即告警。",
                )
            ]
        )
    elif name == "check-principle-alignment":
        decision = (arguments or {}).get("decision", "")
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=f"对于以下决策，逐一检查是否与 11 条核心原则对齐：\n\n"
                    f"决策: {decision}\n\n"
                    f"对每条原则给出：✅ 对齐 / ⚠️ 部分对齐 / ❌ 冲突。\n"
                    f"如果冲突，说明「如果违反会怎样」的反事实预演。",
                )
            ]
        )
    elif name == "daily-reflection":
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content="执行每日 SCARF 自省。\n\n"
                    "1. 对过去 24 小时的行为进行五维度评分（Status/Certainty/Autonomy/Relatedness/Fairness）\n"
                    "2. 检查记忆池健康度：新增/衰退/GC 数量\n"
                    "3. 检查信任分变化趋势\n"
                    "4. 如有维度低于 0.50，给出改进建议",
                )
            ]
        )
    return GetPromptResult(messages=[PromptMessage(role="user", content=f"Unknown prompt: {name}")])


# ===================================================================
# 启动入口
# ===================================================================

async def main():
    """MCP Server 启动入口"""
    async with stdio_server() as (read_stream, write_stream):
        # create_initialization_options() 自动检测已注册的 handler
        # (list_tools/list_prompts/list_resources) 并设置对应 capabilities
        init_options = server.create_initialization_options()
        await server.run(
            read_stream,
            write_stream,
            init_options,
            raise_exceptions=False,
        )


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
