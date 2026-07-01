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

from collections import deque

_engine = None  # 延迟初始化
_skill_engine = None  # 延迟初始化 — SkillEngine 单例
_closure_history: deque = deque(maxlen=5)  # 滑动窗口: 最近5次闭环 {scarf, trust, cei}


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


def get_skill_engine():
    """获取 SkillEngine 单例，自动注册所有 Phase 1 技能。"""
    global _skill_engine
    if _skill_engine is not None:
        return _skill_engine

    from plastic_promise.skills.engine import SkillEngine
    from plastic_promise.skills.session_lifecycle import skill_session_init
    from plastic_promise.skills.memory_operations import skill_smart_remember
    from plastic_promise.skills.superpowers_stages import SKILL_DEFS as _SP_DEFS

    _skill_engine = SkillEngine(get_engine())
    _skill_engine.register(skill_session_init)
    _skill_engine.register(skill_smart_remember)
    # 批量注册 12 个 SuperPowers 阶段技能
    for _name, _def in _SP_DEFS.items():
        _skill_engine.register(_def)
    sp_names = ", ".join(_SP_DEFS.keys())
    logging.info(
        f"SkillEngine: Phase 1 技能已注册 "
        f"(session-init, smart-remember, {sp_names})"
    )
    return _skill_engine


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
            description="混合检索记忆（文本+图遍历双通道），返回三层上下文包。strict=True 时无匹配返回空。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询 / 任务描述"},
                    "task_type": {"type": "string", "description": "任务类型: code_generation/code_review/debugging/architecture/refactoring/learning/collaboration"},
                    "max_results": {"type": "integer", "description": "最大返回数 (默认 20)"},
                    "min_relevance": {"type": "number", "description": "最低关联分数 (默认 0.2)"},
                    "include_principles": {"type": "boolean", "description": "是否注入原则 (默认 true)"},
                    "strict": {"type": "boolean", "description": "严格模式: 无匹配时返回空 (默认 false)"},
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
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "自定义标签 (task:pending, assignee:pi_builder 等)"},
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
        Tool(
            name="memory_reclassify",
            description="强制已有记忆重跑分类管线（tier/domain/category）。批量处理，支持断点续传。",
            inputSchema={
                "type": "object",
                "properties": {
                    "batch_size": {"type": "integer", "description": "每批处理数量 (默认 50)"},
                    "resume_from": {"type": "integer", "description": "断点续传游标 (从第几条开始)"},
                    "dry_run": {"type": "boolean", "description": "仅预览不执行 (默认 false)"},
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
                    "domain_hint": {"type": "string", "description": "可选，限定域: building|fixing|designing|reflecting|governing|connecting|all"},
                },
                "required": ["task_type"],
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
        Tool(
            name="auto_context_inject",
            description="统一自动化上下文注入：skill_session_start→SoulLoop.pre_task_v2→memory_store→skill_session_complete。优雅降级，绝不阻塞。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {"type": "string", "description": "当前任务的完整自然语言描述"},
                    "task_type": {"type": "string", "description": "任务类型标签 (默认 general)"},
                    "source": {"type": "string", "description": "来源: pi_agent|claude_code|manual (默认 manual)"},
                    "scope": {"type": "string", "description": "检索范围: global (默认) 或 domain 限定"},
                },
                "required": ["task_description"],
            },
        ),
    ])

    # === 审计与防线 ===
    tools.extend([
        Tool(
            name="audit_run",
            description="执行七维审计: action=full(默认)|report",
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "审计范围: full/quick/principles_only/memory_only"},
                    "time_range_hours": {"type": "integer", "description": "审计时间范围（小时）"},
                    "action": {"type": "string", "description": "full|report"},
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
            name="defense",
            description="防线管理: action=get|history|adjust|status",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "get|history|adjust|status"},
                    "delta": {"type": "number", "description": "调整量 (±0.01 ~ ±0.10)"},
                    "reason": {"type": "string", "description": "调整原因"},
                },
                "required": ["action"],
            },
        ),
    ])

    # === 自省与演化 ===
    tools.extend([
        Tool(
            name="scarf_reflect",
            description="SCARF 五维自省: mode=standard|inertia",
            inputSchema={
                "type": "object",
                "properties": {
                    "context": {"type": "string", "description": "当前上下文/最近行为描述"},
                    "dimensions": {"type": "array", "items": {"type": "string"}, "description": "指定维度 (空=全部)"},
                    "mode": {"type": "string", "description": "standard|inertia"},
                },
                "required": ["context"],
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
            name="system",
            description="系统工具: action=stats|backup|migrate。stats 含模糊缓存积压计数。",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "stats|backup|migrate"},
                    "format": {"type": "string", "description": "导出格式: json/sqlite"},
                    "source_path": {"type": "string", "description": "源数据路径"},
                    "source_type": {"type": "string", "description": "源类型: lancedb/json/csv"},
                    "include_audit_history": {"type": "boolean"},
                    "dry_run": {"type": "boolean", "description": "仅预览，不实际导入"},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="issue_create",
            description="创建新 Issue，关联原则和依赖关系。服务实践层：约定→任务→追踪。",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue 标题"},
                    "description": {"type": "string", "description": "详细描述"},
                    "principle_id": {"type": "integer", "description": "关联原则 ID (1-12)"},
                    "memory_ids": {"type": "array", "items": {"type": "string"}},
                    "blocks": {"type": "array", "items": {"type": "string"}, "description": "此 Issue 阻塞的 Issue ID 列表"},
                    "blocked_by": {"type": "array", "items": {"type": "string"}, "description": "阻塞此 Issue 的 Issue ID 列表"},
                    "owner": {"type": "string", "description": "Agent owner"},
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="issue_transition",
            description="推进 Issue 状态: open→in_progress→resolved→closed。自动检查依赖。",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "state": {"type": "string", "description": "目标状态: in_progress/resolved/closed"},
                    "reason": {"type": "string"},
                },
                "required": ["issue_id", "state"],
            },
        ),
        Tool(
            name="issue_list",
            description="列出 Issue，支持按状态和 owner 筛选。",
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {"type": "string", "description": "筛选状态: open/in_progress/resolved/closed"},
                    "owner": {"type": "string", "description": "筛选 owner"},
                },
            },
        ),
        Tool(
            name="pack_export",
            description="Export memories as a shareable JSON experience pack. Filter by tags or memory IDs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Pack name (used as filename)"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to filter memories by"},
                    "memory_ids": {"type": "array", "items": {"type": "string"}, "description": "Specific memory IDs to include"},
                    "author": {"type": "string", "description": "Author identifier (default: claude)"},
                    "description": {"type": "string", "description": "Pack description"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="pack_import",
            description="导入经验包。strategy: skip(默认)|replace|merge。merge 时 domain 以包内为准。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the JSON pack file"},
                    "owner": {"type": "string", "description": "Owner to assign to imported memories"},
                    "strategy": {"type": "string", "description": "skip|replace|merge"},
                },
                "required": ["path"],
            },
        ),
    ])

    # === 域联邦域 ===
    tools.extend([
        Tool(
            name="domain",
            description="域联邦统一入口: action=stats|merge|unmerge|rename|rebuild",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "stats|merge|unmerge|rename|rebuild"},
                    "source": {"type": "string", "description": "源域 (merge/unmerge)"},
                    "target": {"type": "string", "description": "目标域 (merge)"},
                    "old_name": {"type": "string", "description": "旧域名 (rename)"},
                    "new_name": {"type": "string", "description": "新域名 (rename)"},
                },
                "required": ["action"],
            },
        ),
    ])

    # === 任务队列域 ===
    tools.extend([
        Tool(
            name="task_enqueue",
            description="Hunter Guild 委托上架 — 将任务挂到公会板上。自动验证提交者等级权限，C级猎人挂A/B级委托需Claude审批。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_type": {"type": "string", "description": "任务类型: fix_memory/gc_*/build_*/refactor_*/review_*/investigate_*"},
                    "title": {"type": "string", "description": "任务标题"},
                    "to_agent": {"type": "string", "description": "目标 Agent"},
                    "priority": {"type": "integer", "description": "优先级: 1=S级 2=A级 3=B级 4=C级 (默认 3)"},
                    "from_agent": {"type": "string", "description": "提交者 (默认 daemon)"},
                    "from_trust_score": {"type": "number", "description": "提交者信任分 (非 daemon/claude 时需提供)"},
                    "description": {"type": "string", "description": "任务描述"},
                    "domain": {"type": "string", "description": "域"},
                    "memory_id": {"type": "string", "description": "关联记忆 ID"},
                    "principle_id": {"type": "string", "description": "关联原则 ID"},
                    "source_scan": {"type": "string", "description": "来源扫描器"},
                    "parent_task_id": {"type": "string", "description": "父任务 ID"},
                    "timeout_seconds": {"type": "integer", "description": "超时秒数 (默认 300)"},
                    "max_escalations": {"type": "integer", "description": "最大升级次数 (默认 3)"},
                    "payload": {"type": "object", "description": "附加数据"},
                },
                "required": ["task_type", "title", "to_agent"],
            },
        ),
        Tool(
            name="task_claim",
            description="Hunter Guild 委托揭榜 — 猎人认领公会板上的委托。原子操作，先到先得。自动检查等级匹配，force=True 可越级揭榜(会记录)。",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "揭榜猎人名称"},
                    "task_id": {"type": "string", "description": "要认领的委托 ID"},
                    "trust_score": {"type": "number", "description": "猎人当前信任分"},
                    "force": {"type": "boolean", "description": "强制越级揭榜 (默认 false)"},
                },
                "required": ["agent_name", "task_id", "trust_score"],
            },
        ),
        Tool(
            name="task_complete",
            description="Hunter Guild 委托完成 — 猎人提交已完成委托，自动创建验收子任务给 Claude。只有揭榜猎人才能提交完成。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "委托 ID"},
                    "agent_name": {"type": "string", "description": "提交完成的猎人名称"},
                    "result": {"type": "string", "description": "完成结果描述"},
                    "artifacts": {"type": "array", "items": {"type": "string"}, "description": "产物路径列表"},
                },
                "required": ["task_id", "agent_name", "result"],
            },
        ),
        Tool(
            name="task_verify",
            description="Hunter Guild 委托验收 — 长老验收已完成委托。accepted 信任分+0.02，rejected/reassigned 信任分-0.03 并自动重派。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "待验收的委托 ID"},
                    "verdict": {"type": "string", "description": "验收结论: accepted | rejected | reassigned"},
                    "verified_by": {"type": "string", "description": "验收者 (默认 claude)"},
                    "comment": {"type": "string", "description": "验收评语"},
                    "reassign_to_agent": {"type": "string", "description": "重派目标 Agent (默认原 to_agent)"},
                },
                "required": ["task_id", "verdict"],
            },
        ),
    ])

    # === 技能追踪域 ===
    tools.extend([
        Tool(
            name="skill_session_start",
            description="创建技能执行实例实体，自动激活关联原则并建立父→子链追踪。",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "技能名称"},
                    "task_description": {"type": "string", "description": "本次执行的任务描述"},
                    "parent_entity_id": {"type": "string", "description": "父技能会话的 entity_id"},
                    "estimated_duration_minutes": {"type": "integer", "description": "预估耗时（分钟）"},
                },
                "required": ["skill_name", "task_description"],
            },
        ),
        Tool(
            name="skill_session_complete",
            description="标记技能执行完成，自动处理标签状态转换和 worth 更新，支持 still_in_progress/abandoned/normal 三种结果。",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "技能会话 entity_id"},
                    "outcome": {"type": "string", "description": "结果: still_in_progress / abandoned: <原因> / 留空=正常完成"},
                    "artifacts": {"type": "array", "items": {"type": "string"}, "description": "产物路径列表"},
                },
                "required": ["entity_id", "outcome"],
            },
        ),
        Tool(
            name="skill_session_trace",
            description="追踪技能执行链：查询、完整性检测、违反警告。支持当前/分支/全部范围。",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_scope": {"type": "string", "description": "查询范围: current|branch|all (默认 all)"},
                    "skill_name": {"type": "string", "description": "按技能名称筛选"},
                    "status": {"type": "string", "description": "按状态筛选: active|done|abandoned"},
                },
            },
        ),
        Tool(
            name="skill_session_audit",
            description="事后间隙扫描：检测技能记忆中提到但缺少 session 实体的技能，支持自动补录修复。",
            inputSchema={
                "type": "object",
                "properties": {
                    "time_range_hours": {"type": "integer", "description": "审计时间范围（小时）"},
                    "auto_fix": {"type": "boolean", "description": "自动补录缺失的 session (默认 false)"},
                },
            },
        ),
        Tool(
            name="skill_auto_track",
            description="Hook 调用的自动 Skill 追踪（PreToolUse/PostToolUse → mcp_tool），零摩擦追踪每次 Skill 调用。",
            inputSchema={
                "type": "object",
                "required": ["phase", "skill_name"],
                "properties": {
                    "phase": {"type": "string", "description": "'start' | 'complete'"},
                    "skill_name": {"type": "string", "description": "Skill 名称"},
                },
            },
        ),
        Tool(
            name="memory_sync_files",
            description="同步文件系统 .md 记忆到 MCP 管道。扫描目录、解析 frontmatter、去重、标记已同步。",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_dir": {"type": "string", "description": ".md 记忆文件目录路径"},
                    "dry_run": {"type": "boolean", "description": "仅扫描不写入 (默认 false)"},
                },
                "required": ["source_dir"],
            },
        ),
        # === Skills 域 (程序化技能 — Phase 1) ===
        Tool(
            name="session-init",
            description="会话启动 — 封装 CLAUDE.md 步骤 0-5：原则激活 + 上下文注入 + 域健康 + 信任分 + GC 预览。替代原有的 6 个独立 MCP 调用。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {"type": "string", "description": "当前任务描述"},
                    "task_type": {"type": "string", "description": "任务类型: general/code_generation/debugging/architecture"},
                },
                "required": ["task_description"],
            },
        ),
        Tool(
            name="smart-remember",
            description="智能记忆存储 — 自动去重检查（相似度 ≥ 0.85 则更新已有记忆），通过完整质量管道（分类+向量+门控）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容"},
                    "memory_type": {"type": "string", "description": "类型: task/experience/principle/code"},
                    "source": {"type": "string", "description": "来源: user/system/claude_code"},
                },
                "required": ["content", "memory_type"],
            },
        ),
        Tool(
            name="step-closure",
            description="每步完成后的六联闭环：原则对齐检查 → SCARF 五维自省 → 激素更新 → 信任分联动 → LLM反思生成(经验/优化/根因) → CEI 复合指数。mode=light 仅做对齐+注入，mode=full 走完整六联。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {"type": "string", "description": "本步操作描述"},
                    "git_commit": {"type": "string", "description": "关联的 git commit hash (可选)"},
                    "mode": {"type": "string", "description": "light (仅对齐+注入) | full (完整六联闭环+LLM反思，默认)"},
                    "lesson": {"type": "string", "description": "经验教训 — 执行者自己反思：本次学到了什么？"},
                    "improvement": {"type": "string", "description": "优化建议 — 下次如何做得更好？"},
                    "root_cause": {"type": "string", "description": "根因分析 — 如果存在问题，根本原因是什么？"},
                    "optimization": {"type": "string", "description": "优化动作 — 立即可执行的一个具体改进"},
                    "trick": {"type": "string", "description": "窍门/技巧 (可选)"},
                },
                "required": ["task_description"],
            },
        ),
        # === 审查域 ===
        Tool(
            name="review_run",
            description="执行结构化代码审查 — 三阶段管线 (prepare→evaluate→apply)。获取 git diff + 12原则检查 + 安全审查 + 信任分联动 + 发现入池 + fix任务创建。支持 action=prepare|evaluate|apply|full。",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "审查阶段: prepare(获取diff+生成prompt) | evaluate(解析审查输出) | apply(信任分+记忆+fix任务) | full(完整管线)",
                        "enum": ["prepare", "evaluate", "apply", "full"],
                    },
                    "commit_range": {"type": "string", "description": "审查的 git commit 范围, 如 HEAD~3..HEAD"},
                    "review_output": {"type": "string", "description": "LLM 审查输出文本 (JSON 格式, evaluate/apply/full 时需要)"},
                    "author_target": {"type": "string", "description": "被审查的 agent trust target (默认 pi_builder)"},
                    "reviewer_target": {"type": "string", "description": "审查者 agent trust target (默认 pi_reviewer)"},
                    "spec_path": {"type": "string", "description": "spec 文件路径 (可选, 用于 spec 合规检查)"},
                },
                "required": ["action"],
            },
        ),
        # === SuperPowers 流水线阶段技能 (统一入口) ===
        Tool(
            name="sp-stage",
            description="SuperPowers 流水线统一阶段入口。stage 参数对应 SuperPowers 标准阶段: brainstorming | writing-plans | executing-plans | subagent-driven-development | test-driven-development | verification-before-completion | finishing-a-development-branch | requesting-code-review | receiving-code-review | systematic-debugging | using-git-worktrees | dispatching-parallel-agents。自动触发 skill_session_start/complete 追踪。",
            inputSchema={
                "type": "object",
                "properties": {
                    "stage": {
                        "type": "string",
                        "description": "SuperPowers 阶段名称",
                        "enum": ["brainstorming", "writing-plans", "executing-plans", "subagent-driven-development", "test-driven-development", "verification-before-completion", "finishing-a-development-branch", "requesting-code-review", "receiving-code-review", "systematic-debugging", "using-git-worktrees", "dispatching-parallel-agents"],
                    },
                    "task_description": {"type": "string", "description": "当前阶段任务描述"},
                },
                "required": ["stage", "task_description"],
            },
        ),
    ])

    return tools


# ---------------------------------------------------------------------------
# 闭环仪表盘摘要格式化
# ---------------------------------------------------------------------------

def _format_closure_dashboard(result: dict, history: deque) -> str:
    """Build a human-readable step-closure dashboard from post_task result.

    Features:
    - Trend arrows (↗↘→) comparing current vs previous closure
    - Sigma marker (⚡) for values beyond ±2σ of sliding window
    - First-closure graceful degradation
    - Reflection fields: lesson, improvement, root_cause, optimization
    """
    scarf = result.get("scarf", {})
    scarf_overall = scarf.get("summary", {}).get("overall_score", 0)
    trust_data = result.get("trust", {})
    trust_score = trust_data.get("score", 0)
    cei = result.get("cei", {})
    cei_score = cei.get("score", 0)
    cei_tier = cei.get("tier", "?")
    reflection = result.get("reflection", {})
    lesson = reflection.get("lesson", "")
    improvement = reflection.get("improvement", "")
    root_cause = reflection.get("root_cause", "")
    optimization = reflection.get("optimization", "")
    source = reflection.get("source", "")

    step_n = len(history) + 1  # history hasn't been updated yet
    is_first = len(history) == 0

    def bar(v):
        filled = int(max(0, min(v, 1)) * 10)
        return "█" * filled + "░" * (10 - filled)

    def trend(current, key):
        """Compare current value against previous closure history."""
        if is_first:
            return "-- baseline"
        prev = history[-1].get(key, 0)
        delta = current - prev
        arrow = "↗" if delta > 0.01 else "↘" if delta < -0.01 else "→"
        tag = f"{arrow} {delta:+.3f}"
        # Sigma check: is current beyond ±2σ of window?
        if len(history) >= 3:
            vals = [h.get(key, 0) for h in history]
            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            std = variance ** 0.5
            if std > 0 and abs(current - mean) > 2 * std:
                tag += " ⚡"
        return tag

    scarf_trend = trend(scarf_overall, "scarf")
    trust_trend = trend(trust_score, "trust")
    cei_trend = trend(cei_score, "cei")

    source_tag = " [🤖LLM]" if source == "llm" else " [🧑执行者]" if source == "executor" else ""

    lines = []
    lines.append("")
    lines.append(f"╔══ Step #{step_n} {'(baseline)' if is_first else ''} ═══════════════════╗")
    lines.append(f"║  SCARF {scarf_overall:.2f}  {bar(scarf_overall)}  ({scarf_trend})")
    lines.append(f"║  Trust {trust_score:.3f}  {bar(trust_score)}  ({trust_trend})")
    lines.append(f"║  CEI   {cei_score:.2f}  {bar(cei_score)}  ({cei_tier} · {cei_trend})")
    lines.append(f"║  ──────────────────────────────────────────────")

    # Show SCARF dimension bars if available
    dims_shown = 0
    for dim_name in ["Status", "Certainty", "Autonomy", "Relatedness", "Fairness"]:
        if dim_name in scarf and isinstance(scarf[dim_name], dict):
            s = scarf[dim_name].get("score", 0)
            lines.append(f"║  {dim_name[:4]:4s} {s:.2f} {bar(s)}")
            dims_shown += 1

    lines.append(f"║  ──────────────────────────────────────────────")

    # Show reflection fields (LLM or template generated)
    if lesson:
        label = "💡 经验" if source == "llm" else "💡 教训"
        lines.append(f"║  {label}: {lesson[:80]}{'…' if len(lesson) > 80 else ''}{source_tag}")
        source_tag = ""  # only show tag once
    if improvement:
        lines.append(f"║  📐 优化: {improvement[:80]}{'…' if len(improvement) > 80 else ''}")
    if root_cause:
        lines.append(f"║  🔍 根因: {root_cause[:80]}{'…' if len(root_cause) > 80 else ''}")
    if optimization:
        lines.append(f"║  🎯 动作: {optimization[:80]}{'…' if len(optimization) > 80 else ''}")

    # Show repair suggestions if any
    repairs = result.get("repairs", [])
    if repairs:
        lines.append(f"║  ──────────────────────────────────────────────")
        for r in repairs[:3]:
            dim = r.get("dimension", "?")
            sug = r.get("suggestion", "")
            lines.append(f"║  🔧 {dim}: {sug[:70]}{'…' if len(sug) > 70 else ''}")

    lines.append(f"╚{'═' * 52}╝")

    return "\n".join(lines)


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
        elif name == "memory_list":
            from plastic_promise.mcp.tools.memory import handle_memory_list
            return await handle_memory_list(engine, arguments)
        elif name == "memory_gc":
            from plastic_promise.mcp.tools.memory import handle_memory_gc
            return await handle_memory_gc(engine, arguments)
        elif name == "memory_correct":
            from plastic_promise.mcp.tools.memory import handle_memory_correct
            return await handle_memory_correct(engine, arguments)
        elif name == "memory_reclassify":
            from plastic_promise.mcp.tools.memory import handle_memory_reclassify
            return await handle_memory_reclassify(engine, arguments)
        # Principle domain
        elif name == "principle_activate":
            from plastic_promise.mcp.tools.principles import handle_principle_activate
            return await handle_principle_activate(engine, arguments)
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
        elif name == "auto_context_inject":
            from plastic_promise.mcp.tools.context import handle_auto_context_inject
            return await handle_auto_context_inject(engine, arguments)

        # Audit and defense
        elif name == "audit_run":
            from plastic_promise.mcp.tools.audit_defense import handle_audit_run
            return await handle_audit_run(engine, arguments)
        elif name == "audit_pre_check":
            from plastic_promise.mcp.tools.audit_defense import handle_audit_pre_check
            return await handle_audit_pre_check(engine, arguments)
        elif name == "defense":
            from plastic_promise.mcp.tools.audit_defense import handle_defense
            return await handle_defense(engine, arguments)

        # Reflection
        elif name == "scarf_reflect":
            from plastic_promise.mcp.tools.reflection import handle_scarf_reflect
            return await handle_scarf_reflect(engine, arguments)
        elif name == "feedback_apply":
            from plastic_promise.mcp.tools.reflection import handle_feedback_apply
            return await handle_feedback_apply(engine, arguments)

        # Management
        elif name == "system":
            from plastic_promise.mcp.tools.management import handle_system
            return await handle_system(engine, arguments)
        elif name == "issue_create":
            from plastic_promise.mcp.tools.management import handle_issue_create
            return await handle_issue_create(engine, arguments)
        elif name == "issue_transition":
            from plastic_promise.mcp.tools.management import handle_issue_transition
            return await handle_issue_transition(engine, arguments)
        elif name == "issue_list":
            from plastic_promise.mcp.tools.management import handle_issue_list
            return await handle_issue_list(engine, arguments)
        elif name == "pack_export":
            from plastic_promise.mcp.tools.management import handle_pack_export
            return await handle_pack_export(engine, arguments)
        elif name == "pack_import":
            from plastic_promise.mcp.tools.management import handle_pack_import
            return await handle_pack_import(engine, arguments)
        # Domain federation
        elif name == "domain":
            from plastic_promise.mcp.tools.domain import handle_domain
            return await handle_domain(engine, arguments)

        # Task queue
        elif name == "task_enqueue":
            from plastic_promise.mcp.tools.task_queue import handle_task_enqueue
            return await handle_task_enqueue(engine, arguments)
        elif name == "task_claim":
            from plastic_promise.mcp.tools.task_queue import handle_task_claim
            return await handle_task_claim(engine, arguments)
        elif name == "task_complete":
            from plastic_promise.mcp.tools.task_queue import handle_task_complete
            return await handle_task_complete(engine, arguments)
        elif name == "task_verify":
            from plastic_promise.mcp.tools.task_queue import handle_task_verify
            return await handle_task_verify(engine, arguments)

        # Skill tracking
        elif name == "skill_session_start":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start
            return await handle_skill_session_start(engine, arguments)
        elif name == "skill_session_complete":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete
            return await handle_skill_session_complete(engine, arguments)
        elif name == "skill_session_trace":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace
            return await handle_skill_session_trace(engine, arguments)
        elif name == "skill_session_audit":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_audit
            return await handle_skill_session_audit(engine, arguments)
        elif name == "skill_auto_track":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_auto_track
            return await handle_skill_auto_track(engine, arguments)

        # === Skills 域 (Phase 1) ===
        elif name == "session-init":
            se = get_skill_engine()
            result = await se.exec("session-init", arguments, caller="claude")
            return [TextContent(type="text", text=json.dumps(
                {"skill": result.skill_name, "success": result.success,
                 "data": result.data, "degrade_log": result.degrade_log,
                 "errors": result.errors, "audit_trail": result.audit_trail},
                ensure_ascii=False, indent=2))]
        elif name == "smart-remember":
            se = get_skill_engine()
            result = await se.exec("smart-remember", arguments, caller="claude")
            return [TextContent(type="text", text=json.dumps(
                {"skill": result.skill_name, "success": result.success,
                 "data": result.data, "degrade_log": result.degrade_log,
                 "errors": result.errors, "audit_trail": result.audit_trail},
                ensure_ascii=False, indent=2))]
        elif name == "step-closure":
            import asyncio
            from plastic_promise.loop.soul_loop import post_task
            task_desc = arguments.get("task_description", "")
            git_commit = arguments.get("git_commit", "")
            mode = arguments.get("mode", "full")
            lesson = arguments.get("lesson", "")
            improvement = arguments.get("improvement", "")
            root_cause = arguments.get("root_cause", "")
            optimization = arguments.get("optimization", "")
            trick = arguments.get("trick", "")
            result = await asyncio.to_thread(
                post_task, task_desc, git_commit, mode,
                None,  # issue_id
                lesson, improvement, root_cause, optimization, trick,
            )

            def safe_serialize(obj):
                if isinstance(obj, dict):
                    return {k: safe_serialize(v) for k, v in obj.items()}
                elif isinstance(obj, (list, tuple)):
                    return [safe_serialize(i) for i in obj]
                elif hasattr(obj, '__dict__'):
                    return {k: safe_serialize(v) for k, v in obj.__dict__.items()
                            if not k.startswith('_')}
                elif callable(obj) and not isinstance(obj, (str, int, float, bool, list, dict, type(None))):
                    return str(obj)
                else:
                    try:
                        json.dumps(obj)
                        return obj
                    except (TypeError, ValueError):
                        return str(obj)
            safe = safe_serialize(result)

            # Record closure in sliding window for trend tracking
            _closure_history.append({
                "scarf": safe.get("scarf", {}).get("summary", {}).get("overall_score", 0),
                "trust": safe.get("trust", {}).get("score", 0),
                "cei": safe.get("cei", {}).get("score", 0),
            })

            # 5. 反思持久化 — 执行者 (Claude) 提供的 lesson/improvement/root_cause/optimization
            #    合并为一条结构化记忆，通过 smart-remember 走完整分类管线入池
            smart_memory_id = None
            if mode == "full":
                # 读取执行者传入的反思字段
                lesson_text = arguments.get("lesson", "")
                improvement_text = arguments.get("improvement", "")
                root_cause_text = arguments.get("root_cause", "")
                optimization_text = arguments.get("optimization", "")
                trick_text = arguments.get("trick", "")

                if trick_text and lesson_text:
                    lesson_text = f"{lesson_text} | 窍门: {trick_text}"

                # 至少有一个字段有内容才入库
                any_content = (
                    (lesson_text and len(lesson_text) > 5) or
                    (improvement_text and len(improvement_text) > 5) or
                    (root_cause_text and len(root_cause_text) > 5) or
                    (optimization_text and len(optimization_text) > 5)
                )
                if any_content:
                    try:
                        from plastic_promise.skills.engine import SkillEngine
                        from plastic_promise.skills.session_lifecycle import skill_session_init
                        from plastic_promise.skills.memory_operations import skill_smart_remember
                        from plastic_promise.skills.superpowers_stages import SKILL_DEFS as _SP_DEFS
                        sr_engine = SkillEngine(get_engine())
                        sr_engine.register(skill_session_init)
                        sr_engine.register(skill_smart_remember)
                        for _name, _def in _SP_DEFS.items():
                            sr_engine.register(_def)

                        # 组装一条结构化反思记忆
                        parts = []
                        if lesson_text:
                            parts.append(f"[经验] {lesson_text}")
                        if improvement_text:
                            parts.append(f"[优化] {improvement_text}")
                        if root_cause_text:
                            parts.append(f"[根因] {root_cause_text}")
                        if optimization_text:
                            parts.append(f"[动作] {optimization_text}")
                        structured_content = "\n".join(parts)

                        step_id = safe.get("reflection", {}).get("step_id", "")
                        tags = ["closure", "domain:reflecting", f"step:{step_id}"]

                        sr_result = await sr_engine.exec("smart-remember", {
                            "content": structured_content,
                            "memory_type": "reflection",
                            "source": "step-closure",
                            "scope": "global",
                            "tags": tags,
                        }, caller="claude")
                        if sr_result.success and sr_result.data:
                            smart_memory_id = sr_result.data.get("memory_id", "")
                    except Exception as e:
                        logging.warning(f"step-closure smart-remember exception: {e}")

            # Build dashboard summary + JSON body
            dashboard = _format_closure_dashboard(safe, _closure_history)
            if smart_memory_id:
                dashboard += f"\n  💾 反思已入池: {smart_memory_id[:20]}..."
            return [TextContent(type="text", text=dashboard)]

        # === 审查域 ===
        elif name == "review_run":
            from plastic_promise.mcp.tools.review import handle_review_run
            return await handle_review_run(engine, arguments)

        # === SuperPowers 流水线阶段技能 (统一入口) ===
        elif name == "sp-stage":
            stage = arguments.get("stage", "")
            task_desc = arguments.get("task_description", "")
            # ── Chain validation: reject invalid stage transitions ──
            from plastic_promise.mcp.tools.skill_tracking import get_current_stage
            from plastic_promise.core.constants import SKILL_CHAIN_MAP as _CHAIN_MAP
            current = get_current_stage()
            if current and current != stage:
                # Strip "sp-" prefix for lookup if needed
                lookup_current = current.replace("sp-", "")
                lookup_stage = stage.replace("sp-", "")
                chain = _CHAIN_MAP.get(lookup_current) or _CHAIN_MAP.get(f"sp-{lookup_current}", {})
                valid_next = chain.get("successors", [])
                # Normalize: remove sp- prefix for comparison
                valid_next_normalized = [s.replace("sp-", "") for s in valid_next]
                if lookup_stage not in valid_next and lookup_stage not in valid_next_normalized:
                    return [TextContent(type="text", text=json.dumps({
                        "error": "chain_violation",
                        "message": f"Stage '{stage}' is not a valid successor of '{current}'. Valid next stages: {valid_next}",
                        "current_stage": current,
                        "valid_next": valid_next,
                    }, ensure_ascii=False))]
            # ── End chain validation ──
            se = get_skill_engine()
            skill_name = f"sp-{stage}" if not stage.startswith("sp-") else stage
            result = await se.exec(skill_name, {"task_description": task_desc}, caller="trae")
            if not result.success:
                return [TextContent(type="text", text=json.dumps(
                    {"stage": stage, "success": False, "errors": result.errors},
                    ensure_ascii=False))]
            return [TextContent(type="text", text=json.dumps(
                {"stage": stage, "success": True, "data": result.data},
                ensure_ascii=False))]

        elif name == "memory_sync_files":
            from plastic_promise.mcp.tools.memory import handle_memory_sync_files
            return await handle_memory_sync_files(engine, arguments)

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
            description="13 条核心原则的完整定义",
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
                    content=f"对于以下决策，逐一检查是否与 13 条核心原则对齐：\n\n"
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
    """MCP Server 启动入口 — 支持 stdio 和 SSE 双模式"""
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "--sse":
        # SSE 模式 — 供 Pi 和其他 Agent 通过 HTTP 连接
        port = int(sys.argv[2])
        await run_sse(port)
    else:
        # stdio 模式 — 供 Claude Code 本地调用
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options()
            await server.run(
                read_stream,
                write_stream,
                init_options,
                raise_exceptions=False,
            )


async def run_sse(port: int = 9020):
    """启动 SSE (Server-Sent Events) 传输 — 多 Agent 共享记忆入口。

    Pi、N.E.K.O 等外部 Agent 通过 HTTP SSE 连接到这个端口，
    共享同一个 Plastic Promise 记忆池。
    """
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route, Mount

    logger = logging.getLogger("plastic-promise-sse")
    import signal
    import time as _time
    start_time = _time.time()

    sse = SseServerTransport("/messages")

    # Notification queue — issue transitions push here, /events streams
    import asyncio as _asyncio
    _notify_queue: _asyncio.Queue = _asyncio.Queue()

    def notify_issue_change(data: dict):
        """Push issue state change to all SSE event listeners."""
        try:
            _notify_queue.put_nowait(data)
        except Exception:
            pass

    class _NoOpResponse(Response):
        """Sentinel response — the SSE transport already handled the send via request._send."""
        async def __call__(self, scope, receive, send):
            pass  # response already sent by SSE transport — do nothing

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            init_options = server.create_initialization_options()
            await server.run(
                read_stream, write_stream, init_options, raise_exceptions=False
            )
        return _NoOpResponse()

    async def handle_events(request: Request):
        """SSE event stream — push notifications to connected clients.

        Uses raw ASGI send to avoid Starlette StreamingResponse lifecycle conflicts.
        """
        import json as _json

        # Send SSE headers manually
        await request._send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream"),
                (b"cache-control", b"no-cache"),
                (b"connection", b"keep-alive"),
            ],
        })

        # Send initial connected event
        body = f"data: {_json.dumps({'type': 'connected'})}\n\n".encode()
        await request._send({"type": "http.response.body", "body": body, "more_body": True})

        # Event loop — exits on client disconnect
        while True:
            disconnected = await request.is_disconnected()
            if disconnected:
                break
            try:
                data = await _asyncio.wait_for(_notify_queue.get(), timeout=1)
                body = f"data: {_json.dumps(data, ensure_ascii=False)}\n\n".encode()
                await request._send({"type": "http.response.body", "body": body, "more_body": True})
            except _asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                body = b"data: {\"type\":\"heartbeat\"}\n\n"
                try:
                    await request._send({"type": "http.response.body", "body": body, "more_body": True})
                except Exception:
                    break

        # Clean shutdown
        try:
            await request._send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:
            pass

    async def handle_notify(request: Request):
        """接收外部推送并广播到 SSE /events。Daemon/Worker 状态变更入口。"""
        import json as _json
        from starlette.responses import JSONResponse
        try:
            body = await request.body()
            event = _json.loads(body.decode())
            await _notify_queue.put(event)

            # Persist audit reports as memories
            if event.get("type") == "audit_report":
                try:
                    engine = get_engine()
                    report_text = event.get("content", "")
                    # Mark existing audit memories as replaced
                    for mid, mem in engine._memories.items():
                        if isinstance(mem, dict) and "audit" in mem.get("tags", []):
                            mtags = list(mem.get("tags", []))
                            if "status:replaced" not in mtags:
                                mtags.append("status:replaced")
                                engine._memories[mid]["tags"] = mtags
                    engine.register_memory({
                        "content": report_text,
                        "memory_type": "reflection",
                        "tags": ["audit", "domain:governing", f"score:{event.get('overall',0):.2f}"],
                        "source": "maintenance_daemon",
                    })
                except Exception:
                    pass

            # Refresh in-memory cache when daemon updates classification via SQLite
            if event.get("type") == "llm_classified":
                try:
                    engine = get_engine()
                    mid = event.get("memory_id", "")
                    new_category = event.get("new_category", "")
                    if mid and mid in engine._memories:
                        engine._memories[mid]["category"] = new_category
                        # Update tags to reflect classification state
                        tags = list(engine._memories[mid].get("tags", []))
                        if "llm_pending:true" in tags:
                            tags.remove("llm_pending:true")
                        if "llm_classified:true" not in tags:
                            tags.append("llm_classified:true")
                        if new_category and f"cat:{new_category}" not in tags:
                            tags.append(f"cat:{new_category}")
                        engine._memories[mid]["tags"] = tags
                except Exception:
                    pass

            return JSONResponse({"ok": True})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def health(request):
        import json as _json
        from starlette.responses import JSONResponse
        return JSONResponse({
            "status": "ok",
            "uptime": round(_time.time() - start_time, 1),
            "version": "0.1.0",
            "pid": os.getpid(),
        })

    async def api_stats(request):
        """Return memory pool + body system statistics."""
        import json as _json
        from starlette.responses import JSONResponse
        try:
            engine = get_engine()
            stats_raw = engine.memory_stats_json()
            stats = _json.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
            from plastic_promise.core.constants import DIGITAL_BODY_SYSTEMS
            systems = {}
            for k, v in DIGITAL_BODY_SYSTEMS.items():
                systems[k] = {
                    "name": v.get("name", k),
                    "maturity": v.get("maturity", 0.0),
                }
            return JSONResponse({
                "memory": stats,
                "body_systems": systems,
                "uptime": round(_time.time() - start_time, 1),
                "version": "0.1.0",
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_issues(request):
        """Return active issue list."""
        import json as _json
        from starlette.responses import JSONResponse
        try:
            engine = get_engine()
            from plastic_promise.mcp.tools.management import handle_issue_list
            result = await handle_issue_list(engine, {})
            data = _json.loads(result[0].text) if result else {"issues": []}
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_trust(request):
        """Return trust/defense status."""
        import json as _json
        from starlette.responses import JSONResponse
        try:
            engine = get_engine()
            from plastic_promise.mcp.tools.audit_defense import handle_defense, handle_audit_run
            result = await handle_defense(engine, {"action": "get"})
            data = _json.loads(result[0].text) if result else {}
            # Add audit summary
            try:
                audit_result = await handle_audit_run(engine, {"action": "report"})
                audit_data = _json.loads(audit_result[0].text) if audit_result else {}
            except Exception:
                audit_data = {"message": "No audit run yet"}
            data["audit_summary"] = audit_data
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_skill_track(request):
        """Lightweight HTTP endpoint for skill_auto_track (used by hook scripts)."""
        import json as _json
        from starlette.responses import JSONResponse
        try:
            body = await request.json()
            engine = get_engine()
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_auto_track
            result = await handle_skill_auto_track(engine, {
                "phase": body.get("phase", "start"),
                "skill_name": body.get("skill_name", ""),
            })
            data = _json.loads(result[0].text) if result else {}
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard(request):
        """Serve the monitoring dashboard HTML page."""
        from starlette.responses import HTMLResponse
        html = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Plastic Promise Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#c9d1d9;padding:24px}
h1{font-size:20px;margin-bottom:4px}
.status{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}
.status-ok{background:#3fb950}.status-err{background:#f85149}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-top:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.card h3{font-size:13px;color:#8b949e;margin-bottom:8px;text-transform:uppercase}
.card .value{font-size:28px;font-weight:700}
.bar{margin-top:8px;height:6px;border-radius:3px;background:#21262d;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;transition:width .5s}
.bar-high{background:#3fb950}.bar-mid{background:#d29922}.bar-low{background:#f85149}
.section{margin-top:24px}
.section h2{font-size:16px;border-bottom:1px solid #30363d;padding-bottom:8px;margin-bottom:12px}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #21262d;font-size:13px}
th{color:#8b949e}
.tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px}
.tag-ok{background:#1b3823;color:#3fb950}.tag-warn{background:#332b00;color:#d29922}
.footer{color:#484f58;font-size:12px;margin-top:32px}
</style>
</head>
<body>
<h1><span class="status status-ok" id="status-dot"></span>Plastic Promise Dashboard <small style="color:#8b949e">v0.1.0</small></h1>

<div class="grid" id="stats-grid">
  <div class="card"><h3>Memories</h3><div class="value" id="mem-total">-</div></div>
  <div class="card"><h3>Decaying</h3><div class="value" id="mem-decaying">-</div></div>
  <div class="card"><h3>Trust Score</h3><div class="value" id="trust-score">-</div></div>
  <div class="card"><h3>Active Issues</h3><div class="value" id="issues-count">-</div></div>
</div>

<div class="section"><h2>Body Systems</h2>
<div id="body-systems"></div>
</div>

<div class="section"><h2>Defense</h2>
<div id="defense-info"></div>
</div>

<div class="section"><h2>Audit</h2>
<div id="audit-info"></div>
</div>

<div class="footer">Auto-refreshes every 5s &middot; Plastic Promise</div>

<script>
async function fetchJSON(url) {
  try { const r = await fetch(url); return r.ok ? r.json() : null; }
  catch { return null; }
}

function barColor(v) { return v>=0.7?'bar-high':v>=0.5?'bar-mid':'bar-low'; }

async function refresh() {
  const [stats, issues, trust] = await Promise.all([
    fetchJSON('/api/stats'), fetchJSON('/api/issues'), fetchJSON('/api/trust')
  ]);

  if (!stats) { document.getElementById('status-dot').className='status status-err'; return; }
  document.getElementById('status-dot').className='status status-ok';

  document.getElementById('mem-total').textContent = stats.memory?.total || 0;
  document.getElementById('mem-decaying').textContent = stats.memory?.decaying || 0;

  // Body systems
  const systems = stats.body_systems || {};
  let sysHTML = '';
  for (const [key, s] of Object.entries(systems)) {
    const pct = Math.round(s.maturity*100);
    sysHTML += `<div style="display:flex;align-items:center;margin-bottom:6px">
      <span style="width:140px;font-size:13px">${s.name}</span>
      <div class="bar" style="flex:1"><div class="bar-fill ${barColor(s.maturity)}" style="width:${pct}%"></div></div>
      <span style="width:40px;text-align:right;font-size:13px">${pct}%</span></div>`;
  }
  document.getElementById('body-systems').innerHTML = sysHTML;

  // Trust
  if (trust) {
    document.getElementById('trust-score').textContent = (trust.trust||0).toFixed(2);
    const tier = trust.tier || 'unknown';
    document.getElementById('defense-info').innerHTML = `
      <span class="tag tag-${tier==='high'?'ok':'warn'}">${tier} tier</span>
      <span style="margin-left:12px">Target: ${trust.target||'default'}</span>`;
  }

  // Issues
  if (issues) {
    const count = issues.count || issues.issues?.length || 0;
    document.getElementById('issues-count').textContent = count;
  }

  // Audit
  if (trust?.audit_summary) {
    document.getElementById('audit-info').innerHTML = '<pre style="font-size:12px;color:#8b949e">' +
      JSON.stringify(trust.audit_summary, null, 2).slice(0, 500) + '</pre>';
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
        return HTMLResponse(html)

    async def shutdown():
        logger.info("Shutting down Plastic Promise SSE server...")

    async def handle_messages(request: Request):
        """Wrap sse.handle_post_message as a Starlette Route endpoint.

        sse.handle_post_message is an ASGI app that sends its own response
        via request._send.  Starlette's request_response wrapper would try
        to call the return value as a Response, so we return a no-op sentinel.
        """
        await sse.handle_post_message(request.scope, request.receive, request._send)
        return _NoOpResponse()

    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
        Route("/events", endpoint=handle_events, methods=["GET"]),
        Route("/notify", endpoint=handle_notify, methods=["POST"]),
        Route("/health", endpoint=health),
        Route("/api/stats", endpoint=api_stats),
        Route("/api/issues", endpoint=api_issues),
        Route("/api/trust", endpoint=api_trust),
        Route("/api/skill-track", endpoint=api_skill_track, methods=["POST"]),
        Route("/dashboard", endpoint=dashboard),
    ], on_shutdown=[shutdown])

    logger.info(f"Plastic Promise MCP Server v0.1.0")
    logger.info(f"SSE endpoint: http://127.0.0.1:{port}/sse")
    logger.info(f"Health:      http://127.0.0.1:{port}/health")
    logger.info(f"PID: {os.getpid()}")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    asyncio.run(main())
