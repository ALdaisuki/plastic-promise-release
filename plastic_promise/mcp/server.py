"""Plastic Promise MCP Server — 全过程 MCP 化入口

启动方式:
    python -m plastic_promise.mcp.server              # stdio 模式 (Claude Code 直接调用)
    python -m plastic_promise.mcp.server --sse 9020   # SSE 模式 (多 Agent 共享)

架构:
    MCP Server
    ├── 7 个工具组 (tools/)
    ├── Resources (resources.py)
    └── Prompts (prompts.py)

所有工具共享 ContextEngine 单例，通过依赖注入传递给各工具模块。
"""

import importlib.util
import json
import logging
import os
import sys
from typing import Any

# 确保项目根在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# ---------------------------------------------------------------------------
# 全局 ContextEngine 代理 (Rust 不可用时回退到 Python mock)
# ---------------------------------------------------------------------------
from collections import deque

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)

from plastic_promise import __version__
from plastic_promise.core.constants import (
    CORE_PRINCIPLES,
)
from plastic_promise.launcher.default_environment import configure_default_environment
from plastic_promise.launcher.runtime_mode import RUNTIME_MODE_KEYS

PLASTIC_PROMISE_VERSION = __version__
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_engine = None  # 延迟初始化
_skill_engine = None  # 延迟初始化 — SkillEngine 单例
_closure_history: deque = deque(maxlen=5)  # 滑动窗口: 最近5次闭环 {scarf, trust, cei}


def get_engine():
    """获取 ContextEngine 单例（Python 主引擎，Rust 加速器就绪后切换）

    Python ContextEngine 拥有完整的数据管道：
    - SQLite 持久化 (plastic_memory.db)
    - LanceDB 向量检索
    - BM25 + RRF 混合检索
    - 原则注入 + 图谱遍历

    Rust context_engine_core 目前是占位实现（:memory: 存储、Noop 检索器），
    待 retriever backends 实现后通过 supply() 中的 _supply_rust 路径切换。
    """
    global _engine
    if _engine is not None:
        return _engine

    # Python 主引擎 — 完整数据管道
    from plastic_promise.core.context_engine import ContextEngine as PyEngine

    _engine = PyEngine()
    logging.info("ContextEngine: Python 核心已加载 (SQLite + LanceDB)")

    # 预导入 Rust 加速器（如果可用），供 _supply_rust 路径使用
    try:
        if importlib.util.find_spec("context_engine_core") is None:
            raise ImportError("context_engine_core")

        logging.info("ContextEngine: Rust 加速器可用（待 supply 路径启用）")
    except ImportError:
        logging.info("ContextEngine: Rust 加速器不可用（需编译 context_engine_core）")

    return _engine


def get_skill_engine():
    """获取 SkillEngine 单例，自动注册所有 Phase 1 技能。"""
    global _skill_engine
    if _skill_engine is not None:
        return _skill_engine

    from plastic_promise.skills.engine import SkillEngine
    from plastic_promise.skills.memory_operations import skill_smart_remember
    from plastic_promise.skills.session_lifecycle import skill_session_init
    from plastic_promise.skills.superpowers_stages import SKILL_DEFS as _SP_DEFS

    _skill_engine = SkillEngine(get_engine())
    _skill_engine.register(skill_session_init)
    _skill_engine.register(skill_smart_remember)
    # 批量注册 12 个 SuperPowers 阶段技能
    for _name, _def in _SP_DEFS.items():
        _skill_engine.register(_def)
    sp_names = ", ".join(_SP_DEFS.keys())
    logging.info(f"SkillEngine: Phase 1 技能已注册 (session-init, smart-remember, {sp_names})")
    return _skill_engine


# ---------------------------------------------------------------------------
# MCP Server 实例
# ---------------------------------------------------------------------------

server = Server("plastic-promise", version=PLASTIC_PROMISE_VERSION)

_CODEX_DISCOVERY_HINTS = {
    "session-init": (
        "Plastic Promise MCP; Codex tool_search discovery; bootstrap; session init; "
        "startup; principles; SCARF; trust; chain_state."
    ),
    "sp-stage": (
        "Plastic Promise MCP; Codex tool_search discovery; SuperPowers; workflow stage; "
        "brainstorming; TDD; verification; skill chain."
    ),
    "memory_recall": (
        "Plastic Promise MCP; Codex tool_search discovery; memory recall; memory_recall; "
        "retrieve memories; context; agent memory."
    ),
    "context_supply": (
        "Plastic Promise MCP; Codex tool_search discovery; context supply; context_supply; "
        "three-layer context pack; task context."
    ),
    "defense": (
        "Plastic Promise MCP; Codex tool_search discovery; trust; defense; permissions; "
        "trust score; autonomy."
    ),
    "step-closure": (
        "Plastic Promise MCP; Codex tool_search discovery; step closure; step_closure; "
        "SCARF reflection; trust feedback; CEI."
    ),
    "runtime_mode": (
        "Plastic Promise MCP; Codex tool_search discovery; runtime mode; hot update; "
        "launcher mode; Rust acceleration; light normal full."
    ),
    "commercial_audit_export": (
        "Plastic Promise MCP; Codex tool_search discovery; commercial audit export; "
        "call spans; degradation events; store outbox; traceability bundle."
    ),
}

_REQUEST_SCOPE_PROPERTIES = {
    "stage_session_id": {
        "type": "string",
        "description": "SuperPowers stage/session scope id for isolating concurrent heavy calls",
    },
    "flow_line_id": {
        "type": "string",
        "description": "Flow line id within stage_session_id; pairs with stage-style workflow isolation",
    },
    "request_id": {
        "type": "string",
        "description": "Caller supplied per-call request id; omitted values are generated server-side",
    },
}

_PROJECT_CONTEXT_PROPERTIES = {
    "project_id": {
        "type": "string",
        "description": "Canonical project identity, e.g. project:plastic-promise",
    },
    "project_policy": {
        "type": "string",
        "enum": ["strict", "balanced", "open"],
        "description": "Project isolation policy for recall/context layers",
    },
}

_PROVENANCE_PROPERTIES = {
    "visibility": {
        "type": "string",
        "enum": ["project", "global", "shared", "private"],
        "description": "Memory visibility boundary",
    },
    "source_class": {
        "type": "string",
        "description": "Memory source class such as user_fact, code_fact, experience, prompt, telemetry",
    },
    "origin_kind": {"type": "string", "description": "Origin kind for provenance"},
    "origin_uri": {"type": "string", "description": "Origin URI for provenance"},
    "origin_ref": {"type": "string", "description": "Origin reference for provenance"},
    "origin_hash": {"type": "string", "description": "Origin content hash for provenance"},
    "parent_memory_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Parent memory ids used to derive this memory",
    },
    "metadata_json": {"type": "object", "description": "Structured provenance metadata"},
    "call_id": {"type": "string", "description": "Trace call id"},
    "parent_call_id": {"type": "string", "description": "Parent trace call id"},
}


def _with_codex_discovery_hints(tools: list[Tool]) -> list[Tool]:
    """Append English discovery terms for clients that search deferred MCP metadata."""
    by_name = {tool.name: tool for tool in tools}
    for name, hint in _CODEX_DISCOVERY_HINTS.items():
        tool = by_name.get(name)
        if tool is None:
            continue
        marker = "Codex/tool_search discovery:"
        if marker not in (tool.description or ""):
            tool.description = f"{tool.description} {marker} {hint}"
    return tools

# ---------------------------------------------------------------------------
# 能力声明
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    """声明所有 MCP 工具"""
    tools: list[Tool] = []

    # === 记忆域 ===
    tools.extend(
        [
            Tool(
                name="memory_recall",
                description="混合检索记忆（文本+图遍历双通道），返回三层上下文包。strict=True 时无匹配返回空。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索查询 / 任务描述"},
                        "task_type": {
                            "type": "string",
                            "description": "任务类型: code_generation/code_review/debugging/architecture/refactoring/learning/collaboration",
                        },
                        "max_results": {"type": "integer", "description": "最大返回数 (默认 20)"},
                        "min_relevance": {
                            "type": "number",
                            "description": "最低关联分数 (默认 0.2)",
                        },
                        "include_principles": {
                            "type": "boolean",
                            "description": "是否注入原则 (默认 true)",
                        },
                        "strict": {
                            "type": "boolean",
                            "description": "严格模式: 无匹配时返回空 (默认 false)",
                        },
                        "debug": {
                            "type": "boolean",
                            "description": "调试模式: 返回 pipeline_stats 与 per_item_stats (默认 false)",
                        },
                        "scope": {
                            "type": "string",
                            "description": "检索范围: global (默认) 或 domain 限定",
                        },
                        "domain_hint": {
                            "type": "string",
                            "description": "域联邦提示域；用于生成跨域信号",
                        },
                        "federation": {
                            "type": "boolean",
                            "description": "是否生成跨域联邦信号 (默认 true)",
                        },
                        "pack": {
                            "type": "string",
                            "description": "兼容字段；预留给经验包限定检索",
                        },
                        **_PROJECT_CONTEXT_PROPERTIES,
                        **_REQUEST_SCOPE_PROPERTIES,
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="memory_store",
                description="存储一条记忆到 Plastic Promise 记忆池。自动分类 (task/experience/principle/code) 并建立实体关联。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "记忆内容"},
                        "memory_type": {
                            "type": "string",
                            "description": "类型: task/experience/principle/code",
                        },
                        "source": {
                            "type": "string",
                            "description": "来源: user/system/previous_output",
                        },
                        "entity_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关联实体 ID 列表",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "自定义标签 (task:pending, assignee:pi_builder 等)",
                        },
                        **_PROJECT_CONTEXT_PROPERTIES,
                        **_PROVENANCE_PROPERTIES,
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
                        "dry_run": {
                            "type": "boolean",
                            "description": "仅预览，不实际删除 (默认 true)",
                        },
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
                        "mark_as": {
                            "type": "string",
                            "description": "质量标记: corrected / deprecated / wrong",
                        },
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
                        "resume_from": {
                            "type": "integer",
                            "description": "断点续传游标 (从第几条开始)",
                        },
                        "dry_run": {"type": "boolean", "description": "仅预览不执行 (默认 false)"},
                    },
                },
            ),
        ]
    )

    # === 原则域 ===
    tools.extend(
        [
            Tool(
                name="principle_activate",
                description="根据任务类型自动激活相关核心原则。返回原则列表及其关联权重。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_type": {"type": "string", "description": "任务类型"},
                        "task_description": {
                            "type": "string",
                            "description": "任务描述（用于关键词匹配）",
                        },
                        "max_principles": {"type": "integer", "description": "最多返回原则数"},
                        "domain_hint": {
                            "type": "string",
                            "description": "可选，限定域: building|fixing|designing|reflecting|governing|connecting|all",
                            "enum": [
                                "building",
                                "fixing",
                                "designing",
                                "reflecting",
                                "governing",
                                "connecting",
                                "all",
                            ],
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Optional project id for project-level principle overlays",
                        },
                    },
                    "required": ["task_type"],
                    "additionalProperties": False,
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
        ]
    )

    # === 上下文域 ===
    tools.extend(
        [
            Tool(
                name="context_supply",
                description="【核心工具】调用 ContextEngine.supply()，返回三层结构化上下文包：核心层/关联层/发散层。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_description": {
                            "type": "string",
                            "description": "当前任务的完整自然语言描述（含前文上下文）",
                        },
                        "task_type": {"type": "string", "description": "任务类型标签"},
                        "scope": {
                            "type": "string",
                            "description": "检索范围: global (默认) 或 domain 限定",
                        },
                        **_PROJECT_CONTEXT_PROPERTIES,
                        **_REQUEST_SCOPE_PROPERTIES,
                    },
                    "required": ["task_description"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="context_inject",
                description="手动向 EntityGraph 注入原则关联边，或注册新实体节点。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_type": {
                            "type": "string",
                            "description": "实体类型: task/principle/code_module/memory",
                        },
                        "entity_id": {"type": "string"},
                        "entity_name": {"type": "string"},
                        "entity_description": {"type": "string"},
                        "related_entities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关联实体 ID",
                        },
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
                        "query_type": {
                            "type": "string",
                            "description": "查询类型: node_info/traverse/full_graph/neighbors",
                            "enum": ["node_info", "traverse", "full_graph", "neighbors"],
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="auto_context_inject",
                description="统一自动化上下文注入：skill_session_start→SoulLoop.pre_task_v2→memory_store→skill_session_complete。优雅降级，绝不阻塞。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_description": {
                            "type": "string",
                            "description": "当前任务的完整自然语言描述",
                        },
                        "task_type": {
                            "type": "string",
                            "description": "任务类型标签 (默认 general)",
                        },
                        "source": {
                            "type": "string",
                            "description": "来源: pi_agent|claude_code|manual (默认 manual)",
                        },
                        "scope": {
                            "type": "string",
                            "description": "检索范围: global (默认) 或 domain 限定",
                        },
                    },
                    "required": ["task_description"],
                },
            ),
        ]
    )

    # === 审计与防线 ===
    tools.extend(
        [
            Tool(
                name="audit_run",
                description="执行七维审计: action=full(默认)|report",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "full|report",
                            "enum": ["full", "report"],
                        },
                        "scope": {
                            "type": "string",
                            "description": "审计范围: full/quick/principles_only/memory_only",
                        },
                        "time_range_hours": {
                            "type": "integer",
                            "description": "审计时间范围（小时）",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="audit_pre_check",
                description="实时合规检查：对即将执行的操作进行 L0 硬边界和 L1 约束衰减检查。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action_description": {"type": "string", "description": "操作描述"},
                        "action_type": {
                            "type": "string",
                            "description": "操作类型: exec/write/edit/delete/read",
                            "enum": ["exec", "write", "edit", "delete", "read"],
                        },
                    },
                    "required": ["action_description"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="defense",
                description="防线管理: action=get|history|adjust|status",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "get|history|adjust|status",
                            "enum": ["get", "history", "adjust", "status"],
                        },
                        "delta": {"type": "number", "description": "调整量 (±0.01 ~ ±0.10)"},
                        "reason": {"type": "string", "description": "调整原因"},
                        "target": {"type": "string", "description": "信任分目标 (空串=当前 Agent)"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            ),
        ]
    )

    # === 自省与演化 ===
    tools.extend(
        [
            Tool(
                name="scarf_reflect",
                description="SCARF 五维自省: mode=standard|inertia",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "context": {"type": "string", "description": "当前上下文/最近行为描述"},
                        "dimensions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "指定维度 (空=全部)",
                        },
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
                        "feedback_type": {
                            "type": "string",
                            "description": "反馈类型: adopted/ignored/rejected",
                        },
                        "task_context": {"type": "string", "description": "触发反馈的任务上下文"},
                    },
                    "required": ["item_id", "feedback_type"],
                },
            ),
        ]
    )

    # === 管理域 ===
    tools.extend(
        [
            Tool(
                name="system",
                description=(
                    "系统工具: action=stats|backup|migrate|benchmark。stats 含模糊缓存"
                    "积压计数；benchmark 提供检索性能历史/显式运行。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["stats", "backup", "migrate", "benchmark"],
                            "description": "stats|backup|migrate|benchmark",
                        },
                        "format": {"type": "string", "description": "导出格式: json/sqlite"},
                        "source_path": {"type": "string", "description": "源数据路径"},
                        "source_type": {
                            "type": "string",
                            "description": "源类型: lancedb/json/csv",
                        },
                        "include_audit_history": {"type": "boolean"},
                        "dry_run": {"type": "boolean", "description": "仅预览，不实际导入"},
                        "run": {
                            "type": "boolean",
                            "description": "benchmark: true 执行检索探针，false 仅读历史",
                        },
                        "queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "benchmark: 检索探针查询列表",
                        },
                        "repeat": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "benchmark: 每条查询重复次数",
                        },
                        "benchmark_name": {
                            "type": "string",
                            "description": "benchmark: 历史分组名称，默认 retrieval",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "benchmark: 历史汇总最多读取的近期样本数",
                        },
                        "baseline_name": {
                            "type": "string",
                            "description": "benchmark: baseline 名称，默认 default",
                        },
                        "set_baseline": {
                            "type": "boolean",
                            "description": "benchmark: 将当前摘要保存为 baseline",
                        },
                        "gate": {
                            "type": "boolean",
                            "description": "benchmark: 对当前摘要执行回归门禁",
                        },
                        "tolerance_ratio": {
                            "type": "number",
                            "minimum": 0,
                            "description": "benchmark: baseline 允许退化比例，默认 0.20",
                        },
                        "max_p50_ms": {
                            "type": "number",
                            "minimum": 0,
                            "description": "benchmark: p50 绝对上限",
                        },
                        "max_p95_ms": {
                            "type": "number",
                            "minimum": 0,
                            "description": "benchmark: p95 绝对上限",
                        },
                        "max_p99_ms": {
                            "type": "number",
                            "minimum": 0,
                            "description": "benchmark: p99 绝对上限",
                        },
                    },
                    "required": ["action"],
                },
            ),
            Tool(
                name="runtime_mode",
                description=(
                    "Get or hot-update the current MCP runtime mode. Modes: light, "
                    "normal, rust-normal, full, rust-full."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["get", "set"],
                            "description": "get|set",
                        },
                        "mode": {
                            "type": "string",
                            "enum": list(RUNTIME_MODE_KEYS),
                            "description": "Required when action=set.",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
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
                        "blocks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "此 Issue 阻塞的 Issue ID 列表",
                        },
                        "blocked_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "阻塞此 Issue 的 Issue ID 列表",
                        },
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
                        "state": {
                            "type": "string",
                            "description": "目标状态: in_progress/resolved/closed",
                        },
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
                        "state": {
                            "type": "string",
                            "description": "筛选状态: open/in_progress/resolved/closed",
                        },
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
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags to filter memories by",
                        },
                        "memory_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Specific memory IDs to include",
                        },
                        "author": {
                            "type": "string",
                            "description": "Author identifier (default: claude)",
                        },
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
                        "owner": {
                            "type": "string",
                            "description": "Owner to assign to imported memories",
                        },
                        "strategy": {"type": "string", "description": "skip|replace|merge"},
                    },
                    "required": ["path"],
                },
            ),
        ]
    )

    # === 域联邦域 ===
    tools.extend(
        [
            Tool(
                name="domain",
                description="域联邦统一入口: action=stats|merge|unmerge|rename|rebuild",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "stats|merge|unmerge|rename|rebuild",
                        },
                        "source": {"type": "string", "description": "源域 (merge/unmerge)"},
                        "target": {"type": "string", "description": "目标域 (merge)"},
                        "old_name": {"type": "string", "description": "旧域名 (rename)"},
                        "new_name": {"type": "string", "description": "新域名 (rename)"},
                    },
                    "required": ["action"],
                },
            ),
        ]
    )

    # === 任务队列域 ===
    tools.extend(
        [
            Tool(
                name="task_enqueue",
                description="Hunter Guild 委托上架 — 将任务挂到公会板上。自动验证提交者等级权限，C级猎人挂A/B级委托需Claude审批。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_type": {
                            "type": "string",
                            "description": "任务类型: fix_memory/gc_*/build_*/refactor_*/review_*/investigate_*",
                        },
                        "title": {"type": "string", "description": "任务标题"},
                        "to_agent": {"type": "string", "description": "目标 Agent"},
                        "priority": {
                            "type": "integer",
                            "description": "优先级: 1=S级 2=A级 3=B级 4=C级 (默认 3)",
                        },
                        "from_agent": {"type": "string", "description": "提交者 (默认 daemon)"},
                        "from_trust_score": {
                            "type": "number",
                            "description": "提交者信任分 (非 daemon/claude 时需提供)",
                        },
                        "description": {"type": "string", "description": "任务描述"},
                        "domain": {"type": "string", "description": "域"},
                        "memory_id": {"type": "string", "description": "关联记忆 ID"},
                        "principle_id": {"type": "string", "description": "关联原则 ID"},
                        "source_scan": {"type": "string", "description": "来源扫描器"},
                        "parent_task_id": {"type": "string", "description": "父任务 ID"},
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "超时秒数 (默认 300)",
                        },
                        "max_escalations": {
                            "type": "integer",
                            "description": "最大升级次数 (默认 3)",
                        },
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
                        "artifacts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "产物路径列表",
                        },
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
                        "verdict": {
                            "type": "string",
                            "description": "验收结论: accepted | rejected | reassigned",
                        },
                        "verified_by": {"type": "string", "description": "验收者 (默认 claude)"},
                        "comment": {"type": "string", "description": "验收评语"},
                        "reassign_to_agent": {
                            "type": "string",
                            "description": "重派目标 Agent (默认原 to_agent)",
                        },
                    },
                    "required": ["task_id", "verdict"],
                },
            ),
            Tool(
                name="task_inbox",
                description="Hunter Guild 委托板查看 — 显示可接委托、我的进行中任务和等级匹配度。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string", "description": "查看委托板的猎人名称"},
                        "trust_score": {"type": "number", "description": "猎人当前信任分"},
                        "filter_status": {
                            "type": "string",
                            "description": "pending | my_active | pending_review | all (默认 pending)",
                        },
                        "limit": {"type": "integer", "description": "返回数量上限 (默认 20)"},
                    },
                    "required": ["agent_name", "trust_score"],
                },
            ),
            Tool(
                name="task_heartbeat",
                description="Hunter Guild 委托心跳 — 猎人汇报任务仍在执行，避免超时释放。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "委托 ID"},
                        "agent_name": {"type": "string", "description": "揭榜猎人名称"},
                    },
                    "required": ["task_id", "agent_name"],
                },
            ),
            Tool(
                name="task_abandon",
                description="Hunter Guild 主动弃单 — 放弃已揭榜委托并记录信任分惩罚。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "委托 ID"},
                        "agent_name": {"type": "string", "description": "揭榜猎人名称"},
                        "reason": {"type": "string", "description": "弃单原因"},
                    },
                    "required": ["task_id", "agent_name"],
                },
            ),
        ]
    )

    # === 技能追踪域 ===
    tools.extend(
        [
            Tool(
                name="skill_session_start",
                description="创建技能执行实例实体，自动激活关联原则并建立父→子链追踪。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "技能名称"},
                        "task_description": {"type": "string", "description": "本次执行的任务描述"},
                        "parent_entity_id": {
                            "type": "string",
                            "description": "父技能会话的 entity_id",
                        },
                        "estimated_duration_minutes": {
                            "type": "integer",
                            "description": "预估耗时（分钟）",
                        },
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
                        "outcome": {
                            "type": "string",
                            "description": "结果: still_in_progress / abandoned: <原因> / 留空=正常完成",
                        },
                        "artifacts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "产物路径列表",
                        },
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
                        "session_scope": {
                            "type": "string",
                            "description": "查询范围: current|branch|all (默认 all)",
                        },
                        "skill_name": {"type": "string", "description": "按技能名称筛选"},
                        "status": {
                            "type": "string",
                            "description": "按状态筛选: active|done|abandoned",
                        },
                    },
                },
            ),
            Tool(
                name="skill_session_audit",
                description="事后间隙扫描：检测技能记忆中提到但缺少 session 实体的技能，支持自动补录修复。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "time_range_hours": {
                            "type": "integer",
                            "description": "审计时间范围（小时）",
                        },
                        "auto_fix": {
                            "type": "boolean",
                            "description": "自动补录缺失的 session (默认 false)",
                        },
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
                description="会话启动 — 轻量引导：原则激活 + SCARF 基线 + 域/系统健康快照 + 信任分 + GC 预览 + chain_state。任务上下文需显式调用 context_supply。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_description": {"type": "string", "description": "当前任务描述"},
                        "task_type": {
                            "type": "string",
                            "description": "任务类型: general/code_generation/debugging/architecture",
                        },
                        "context_mode": {
                            "type": "string",
                            "enum": ["none", "light", "full"],
                            "description": "启动上下文模式：none=只提示延迟；light=1-2条轻量记忆预览；full=显式运行完整 context_supply",
                        },
                        "context_timeout_s": {
                            "type": "number",
                            "description": "context_mode light/full 的超时秒数上限",
                        },
                        "scope": {
                            "type": "string",
                            "description": "context_mode=full 时传给 context_supply 的检索范围",
                        },
                        "stage_session_id": {
                            "type": "string",
                            "description": "SuperPowers stage chain scope id; omitted means session-init allocates one",
                        },
                        "flow_line_id": {
                            "type": "string",
                            "description": "SuperPowers flow line id to pair with stage_session_id; omitted defaults to the selected route",
                        },
                        "route": {
                            "type": "string",
                            "description": "Default SuperPowers route profile for workflow_contract; built-ins include normal-development, audit-review, and bug-hunt",
                        },
                        "agent_name": {
                            "type": "string",
                            "description": "Agent identity used when allocating a stage_session_id",
                        },
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
                        "memory_type": {
                            "type": "string",
                            "description": "类型: task/experience/principle/code",
                        },
                        "source": {
                            "type": "string",
                            "description": "来源: user/system/claude_code",
                        },
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
                        "git_commit": {
                            "type": "string",
                            "description": "关联的 git commit hash (可选)",
                        },
                        "mode": {
                            "type": "string",
                            "description": "light (仅对齐+注入) | full (完整六联闭环+LLM反思，默认)",
                        },
                        "lesson": {
                            "type": "string",
                            "description": "经验教训 — 执行者自己反思：本次学到了什么？",
                        },
                        "improvement": {
                            "type": "string",
                            "description": "优化建议 — 下次如何做得更好？",
                        },
                        "root_cause": {
                            "type": "string",
                            "description": "根因分析 — 如果存在问题，根本原因是什么？",
                        },
                        "optimization": {
                            "type": "string",
                            "description": "优化动作 — 立即可执行的一个具体改进",
                        },
                        "trick": {"type": "string", "description": "窍门/技巧 (可选)"},
                        "target": {
                            "type": "string",
                            "default": "claude",
                            "description": "信任分追踪目标 (claude/pi_builder/pi_reviewer 等)",
                        },
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
                        "commit_range": {
                            "type": "string",
                            "description": "审查的 git commit 范围, 如 HEAD~3..HEAD",
                        },
                        "review_output": {
                            "type": "string",
                            "description": "LLM 审查输出文本 (JSON 格式, evaluate/apply/full 时需要)",
                        },
                        "author_target": {
                            "type": "string",
                            "description": "被审查的 agent trust target (默认 pi_builder)",
                        },
                        "reviewer_target": {
                            "type": "string",
                            "description": "审查者 agent trust target (默认 pi_reviewer)",
                        },
                        "spec_path": {
                            "type": "string",
                            "description": "spec 文件路径 (可选, 用于 spec 合规检查)",
                        },
                    },
                    "required": ["action"],
                },
            ),
            # === 商业审计域 ===
            Tool(
                name="commercial_audit_export",
                description="Export a project-filterable commercial audit bundle from persisted call spans, degradation events, and optional store outbox records.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Optional canonical project id filter, e.g. project:plastic-promise",
                        },
                        "since": {
                            "type": "string",
                            "description": "Optional inclusive ISO-8601 lower time bound",
                        },
                        "until": {
                            "type": "string",
                            "description": "Optional inclusive ISO-8601 upper time bound",
                        },
                        "include_outbox": {
                            "type": "boolean",
                            "description": "Include durable memory_store outbox records in the export",
                        },
                        "export_otlp": {
                            "type": "boolean",
                            "description": "Best-effort export of matching trace rows to an OTLP/HTTP JSON endpoint",
                        },
                        "otlp_endpoint": {
                            "type": "string",
                            "description": "Optional OTLP HTTP base URL or /v1/traces endpoint",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            # === 插件市场域 (市场管理) ===
            Tool(
                name="market_list",
                description="列出市场中的插件包。支持按类型和可升级状态筛选。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "筛选类型: knowledge/workflow/capability/adapter",
                        },
                        "upgradable": {
                            "type": "boolean",
                            "description": "仅显示可升级的已安装包",
                        },
                    },
                },
            ),
            Tool(
                name="market_install",
                description="从市场安装一个插件包。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_upgrade",
                description="检查或升级插件到远程最新版本。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_remove",
                description="卸载已安装的插件包。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_enable",
                description="启用一个已禁用的插件。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_disable",
                description="禁用一个已启用的插件。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_status",
                description="显示所有已安装插件的状态。",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            # === SuperPowers 流水线阶段技能 (统一入口) ===
            Tool(
                name="sp-stage",
                description="SuperPowers 流水线统一阶段入口。stage 参数对应 SuperPowers 标准阶段: using-superpowers | audit | brainstorming | exemplar-research | writing-plans | executing-plans | subagent-driven-development | test-driven-development | verification-before-completion | finishing-a-development-branch | requesting-code-review | receiving-code-review | systematic-debugging | using-git-worktrees | dispatching-parallel-agents | writing-skills。自动触发 skill_session_start/complete 追踪。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "stage": {
                            "type": "string",
                            "description": "SuperPowers 阶段名称",
                            "enum": [
                                "using-superpowers",
                                "audit",
                                "brainstorming",
                                "exemplar-research",
                                "writing-plans",
                                "executing-plans",
                                "subagent-driven-development",
                                "test-driven-development",
                                "verification-before-completion",
                                "finishing-a-development-branch",
                                "requesting-code-review",
                                "receiving-code-review",
                                "systematic-debugging",
                                "using-git-worktrees",
                                "dispatching-parallel-agents",
                                "writing-skills",
                            ],
                        },
                        "task_description": {"type": "string", "description": "当前阶段任务描述"},
                        "stage_session_id": {
                            "type": "string",
                            "description": "SuperPowers stage chain scope id returned by session-init",
                        },
                        "flow_line_id": {
                            "type": "string",
                            "description": "Optional flow line id for isolating concurrent SuperPowers routes within a stage_session_id",
                        },
                        "route": {
                            "type": "string",
                            "description": "Advisory SuperPowers route profile for stage summaries; built-ins include normal-development, audit-review, and bug-hunt",
                        },
                        "agent_name": {
                            "type": "string",
                            "description": "Agent identity for diagnostics when no stage_session_id is supplied",
                        },
                    },
                    "required": ["stage", "task_description"],
                },
            ),
        ]
    )

    _with_codex_discovery_hints(tools)

    # Compatibility aliases for clients that normalize tool names into identifiers.
    alias_targets = {
        "session_init": "session-init",
        "smart_remember": "smart-remember",
        "step_closure": "step-closure",
        "sp_stage": "sp-stage",
    }
    by_name = {tool.name: tool for tool in tools}
    for alias, target in alias_targets.items():
        original = by_name.get(target)
        if original is not None and alias not in by_name:
            tools.append(
                Tool(
                    name=alias,
                    description=f"Compatibility alias for {target}. {original.description}",
                    inputSchema=original.inputSchema,
                )
            )

    # Project/provenance schema fields are added by name to avoid widening
    # unrelated action schemas that share the same local shape.
    by_name = {tool.name: tool for tool in tools}
    for tool_name in ("memory_recall", "context_supply", "memory_store", "review_run"):
        schema = by_name[tool_name].inputSchema
        schema.setdefault("properties", {}).update(_PROJECT_CONTEXT_PROPERTIES)
    by_name["memory_store"].inputSchema["properties"].update(_PROVENANCE_PROPERTIES)
    by_name["review_run"].inputSchema["properties"]["allow_project_unknown"] = {
        "type": "boolean",
        "description": "Allow prepare/full without project_id and accept degraded review guard behavior",
    }

    return tools


# ---------------------------------------------------------------------------
# 闭环仪表盘摘要格式化
# ---------------------------------------------------------------------------


def _format_closure_dashboard(result: dict, history: deque) -> str:
    """Build a human-readable step-closure dashboard from post_task result.

    Features:
    - Trend arrows (↗↘→) comparing current vs previous closure
    - Sigma marker (!) for values beyond ±2σ of sliding window
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
            std = variance**0.5
            if std > 0 and abs(current - mean) > 2 * std:
                tag += " !!!"
        return tag

    # Extract actual trust/hormone deltas from this closure (not trend vs history)
    hormone = result.get("hormone", {})
    trust_delta = hormone.get("trust_delta", 0)
    scarf_trend = trend(scarf_overall, "scarf")
    trust_trend = trend(trust_score, "trust")
    cei_trend = trend(cei_score, "cei")

    source_tag = " [LLM]" if source == "llm" else " [执行者]" if source == "executor" else ""

    lines = []
    lines.append("")
    lines.append(f"╔══ Step #{step_n} {'(baseline)' if is_first else ''} ═══════════════════╗")
    lines.append(f"║  SCARF {scarf_overall:.2f}  {bar(scarf_overall)}  ({scarf_trend})")
    lines.append(
        f"║  Trust {trust_score:.3f}  {bar(trust_score)}  (adjust: {trust_delta:+.3f}; trend: {trust_trend})"
    )
    lines.append(f"║  CEI   {cei_score:.2f}  {bar(cei_score)}  ({cei_tier} · {cei_trend})")
    lines.append("║  ──────────────────────────────────────────────")

    # Show SCARF dimension bars if available
    dims_shown = 0
    for dim_name in ["Status", "Certainty", "Autonomy", "Relatedness", "Fairness"]:
        if dim_name in scarf and isinstance(scarf[dim_name], dict):
            s = scarf[dim_name].get("score", 0)
            lines.append(f"║  {dim_name[:4]:4s} {s:.2f} {bar(s)}")
            dims_shown += 1

    lines.append("║  ──────────────────────────────────────────────")

    # Show reflection fields (LLM or template generated)
    if lesson:
        label = "[经验]" if source == "llm" else "[教训]"
        lines.append(f"║  {label}: {lesson[:80]}{'…' if len(lesson) > 80 else ''}{source_tag}")
        source_tag = ""  # only show tag once
    if improvement:
        lines.append(f"║  [优化]: {improvement[:80]}{'…' if len(improvement) > 80 else ''}")
    if root_cause:
        lines.append(f"║  [根因]: {root_cause[:80]}{'…' if len(root_cause) > 80 else ''}")
    if optimization:
        lines.append(f"║  [动作]: {optimization[:80]}{'…' if len(optimization) > 80 else ''}")

    # Show repair suggestions if any
    repairs = result.get("repairs", [])
    if repairs:
        lines.append("║  ──────────────────────────────────────────────")
        for r in repairs[:3]:
            dim = r.get("dimension", "?")
            sug = r.get("suggestion", "")
            lines.append(f"║  !!! {dim}: {sug[:70]}{'…' if len(sug) > 70 else ''}")

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
        elif name == "runtime_mode":
            from plastic_promise.mcp.tools.runtime import handle_runtime_mode

            return await handle_runtime_mode(engine, arguments)
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
        elif name == "task_inbox":
            from plastic_promise.mcp.tools.task_queue import handle_task_inbox

            return await handle_task_inbox(engine, arguments)
        elif name == "task_heartbeat":
            from plastic_promise.mcp.tools.task_queue import handle_task_heartbeat

            return await handle_task_heartbeat(engine, arguments)
        elif name == "task_abandon":
            from plastic_promise.mcp.tools.task_queue import handle_task_abandon

            return await handle_task_abandon(engine, arguments)

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
        elif name in ("session-init", "session_init"):
            se = get_skill_engine()
            result = await se.exec("session-init", arguments, caller="claude")
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "skill": result.skill_name,
                            "success": result.success,
                            "data": result.data,
                            "degrade_log": result.degrade_log,
                            "errors": result.errors,
                            "audit_trail": result.audit_trail,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
        elif name in ("smart-remember", "smart_remember"):
            se = get_skill_engine()
            result = await se.exec("smart-remember", arguments, caller="claude")
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "skill": result.skill_name,
                            "success": result.success,
                            "data": result.data,
                            "degrade_log": result.degrade_log,
                            "errors": result.errors,
                            "audit_trail": result.audit_trail,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
        elif name in ("step-closure", "step_closure"):
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
            target = arguments.get("target", "claude")
            result = await asyncio.to_thread(
                post_task,
                task_desc,
                git_commit,
                mode,
                None,  # issue_id
                lesson,
                improvement,
                root_cause,
                optimization,
                trick,
                target,
            )

            def safe_serialize(obj):
                if isinstance(obj, dict):
                    return {k: safe_serialize(v) for k, v in obj.items()}
                elif isinstance(obj, (list, tuple)):
                    return [safe_serialize(i) for i in obj]
                elif hasattr(obj, "__dict__"):
                    return {
                        k: safe_serialize(v)
                        for k, v in obj.__dict__.items()
                        if not k.startswith("_")
                    }
                elif callable(obj) and not isinstance(
                    obj, (str, int, float, bool, list, dict, type(None))
                ):
                    return str(obj)
                else:
                    try:
                        json.dumps(obj)
                        return obj
                    except (TypeError, ValueError):
                        return str(obj)

            safe = safe_serialize(result)

            # Record closure in sliding window for trend tracking
            _closure_history.append(
                {
                    "scarf": safe.get("scarf", {}).get("summary", {}).get("overall_score", 0),
                    "trust": safe.get("trust", {}).get("score", 0),
                    "cei": safe.get("cei", {}).get("score", 0),
                }
            )

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
                    (lesson_text and len(lesson_text) > 5)
                    or (improvement_text and len(improvement_text) > 5)
                    or (root_cause_text and len(root_cause_text) > 5)
                    or (optimization_text and len(optimization_text) > 5)
                )
                if any_content:
                    try:
                        from plastic_promise.skills.engine import SkillEngine
                        from plastic_promise.skills.memory_operations import skill_smart_remember
                        from plastic_promise.skills.session_lifecycle import skill_session_init
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

                        sr_result = await sr_engine.exec(
                            "smart-remember",
                            {
                                "content": structured_content,
                                "memory_type": "reflection",
                                "source": "step-closure",
                                "scope": "global",
                                "tags": tags,
                            },
                            caller="claude",
                        )
                        if sr_result.success and sr_result.data:
                            smart_memory_id = sr_result.data.get("memory_id", "")
                    except Exception as e:
                        logging.warning(f"step-closure smart-remember exception: {e}")

            # Build dashboard summary + JSON body
            dashboard = _format_closure_dashboard(safe, _closure_history)
            if smart_memory_id:
                dashboard += f"\n  [记忆] 反思已入池: {smart_memory_id[:20]}..."
            return [TextContent(type="text", text=dashboard)]

        # === 审查域 ===
        elif name == "commercial_audit_export":
            from plastic_promise.mcp.tools.commercial_audit import handle_commercial_audit_export

            return await handle_commercial_audit_export(engine, arguments)

        elif name == "review_run":
            from plastic_promise.mcp.tools.review import handle_review_run

            return await handle_review_run(engine, arguments)

        # === SuperPowers 流水线阶段技能 (统一入口) ===
        elif name in ("sp-stage", "sp_stage"):
            stage = arguments.get("stage", "")
            task_desc = arguments.get("task_description", "")
            stage_session_id = arguments.get("stage_session_id") or arguments.get("stage_id")
            flow_line_id = str(arguments.get("flow_line_id") or arguments.get("flow_id") or "").strip()
            flow_line_id = flow_line_id or None
            route_id = str(arguments.get("route") or "").strip() or None
            public_stage_session_id = stage_session_id or "default"
            flow_scope_id = (
                f"{public_stage_session_id}::flow:{flow_line_id}"
                if flow_line_id
                else public_stage_session_id
            )
            # ── Chain validation: reject invalid non-root stage transitions ──
            from plastic_promise.core.constants import (
                SKILL_CHAIN_MAP as _CHAIN_MAP,
            )
            from plastic_promise.core.constants import (
                normalize_stage_name,
            )
            from plastic_promise.skills.superpowers_stages import attach_stage_guidance
            from plastic_promise.mcp.tools.skill_tracking import (
                get_current_stage,
                set_current_stage,
            )

            lookup_stage = normalize_stage_name(stage)
            current = get_current_stage(flow_scope_id)
            lookup_current = normalize_stage_name(current)
            if lookup_current and lookup_current != lookup_stage:
                target_chain = _CHAIN_MAP.get(lookup_stage) or _CHAIN_MAP.get(
                    f"sp-{lookup_stage}", {}
                )
                target_is_root = bool(target_chain) and target_chain.get("predecessors", []) == []

                # Root stages intentionally start independent chains. This prevents one
                # Agent's review/debug flow from blocking another Agent's new flow via the
                # process-wide fallback current_stage.
                if not target_is_root:
                    chain = _CHAIN_MAP.get(lookup_current) or _CHAIN_MAP.get(
                        f"sp-{lookup_current}", {}
                    )
                    valid_next = chain.get("successors", [])
                    valid_next_normalized = [normalize_stage_name(s) for s in valid_next]
                    if lookup_stage not in valid_next_normalized:
                        return [
                            TextContent(
                                type="text",
                                text=json.dumps(
                                    {
                                        "error": "chain_violation",
                                        "message": f"Stage '{stage}' is not a valid successor of '{lookup_current}'. Valid next stages: {valid_next_normalized}",
                                        "current_stage": lookup_current,
                                        "valid_next": valid_next_normalized,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        ]
            # ── End chain validation ──
            # ── Stage existence validation: reject unknown/empty stages ──
            if not lookup_stage or lookup_stage not in _CHAIN_MAP:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "invalid_stage",
                                "message": f"Unknown stage: '{stage}'. Valid stages: {sorted(_CHAIN_MAP.keys())}",
                                "requested_stage": stage,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            se = get_skill_engine()
            skill_name = f"sp-{lookup_stage}"
            stage_params = {
                "task_description": task_desc,
                "stage_session_id": flow_scope_id,
            }
            if flow_line_id:
                stage_params["flow_line_id"] = flow_line_id
            if route_id:
                stage_params["route"] = route_id
            result = await se.exec(skill_name, stage_params, caller="trae")
            if not result.success:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"stage": stage, "success": False, "errors": result.errors},
                            ensure_ascii=False,
                        ),
                    )
                ]
            set_current_stage(
                lookup_stage,
                stage_session_id=flow_scope_id,
                parent_entity_id=getattr(result, "audit_trail", {}).get("entity_id") or None,
            )
            result_data = attach_stage_guidance(
                result.data if isinstance(result.data, dict) else {},
                lookup_stage,
                closed=result.data.get("closed") if isinstance(result.data, dict) else None,
                route_id=route_id,
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "stage": stage,
                            "success": True,
                            "stage_session_id": public_stage_session_id,
                            "flow_line_id": flow_line_id,
                            "flow_scope_id": flow_scope_id,
                            "data": result_data,
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        elif name == "memory_sync_files":
            from plastic_promise.mcp.tools.memory import handle_memory_sync_files

            return await handle_memory_sync_files(engine, arguments)

        # ── Market tools ──
        elif name == "market_list":
            from plastic_promise.mcp.tools.market import handle_market_list

            return await handle_market_list(engine, arguments)

        elif name == "market_install":
            from plastic_promise.mcp.tools.market import handle_market_install

            return await handle_market_install(engine, arguments)

        elif name == "market_upgrade":
            from plastic_promise.mcp.tools.market import handle_market_upgrade

            return await handle_market_upgrade(engine, arguments)

        elif name == "market_remove":
            from plastic_promise.mcp.tools.market import handle_market_remove

            return await handle_market_remove(engine, arguments)

        elif name == "market_enable":
            from plastic_promise.mcp.tools.market import handle_market_enable

            return await handle_market_enable(engine, arguments)

        elif name == "market_disable":
            from plastic_promise.mcp.tools.market import handle_market_disable

            return await handle_market_disable(engine, arguments)

        elif name == "market_status":
            from plastic_promise.mcp.tools.market import handle_market_status

            return await handle_market_status(engine, arguments)

        # ── Dynamic plugin tool dispatch ──
        else:
            # Check if a loaded plugin provides this tool
            try:
                from plastic_promise.extensions.loader import PluginLoader as _PluginLoader

                _pl = _PluginLoader()
                _pl.discover()
                _pl.activate_all()
                if name in _pl.get_tools():
                    result = _pl.call_plugin_tool(name, arguments)
                    if result is not None:
                        return [
                            TextContent(
                                type="text",
                                text=json.dumps(result, ensure_ascii=False),
                            )
                        ]
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {"error": f"Plugin tool '{name}' returned no result"},
                                ensure_ascii=False,
                            ),
                        )
                    ]
            except Exception:
                pass

            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False),
                )
            ]
    except Exception as e:
        logging.exception(f"Tool {name} failed")
        return [
            TextContent(
                type="text", text=json.dumps({"error": str(e), "tool": name}, ensure_ascii=False)
            )
        ]


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
                    f"对每条原则给出：[OK] 对齐 / [WARN] 部分对齐 / [FAIL] 冲突。\n"
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

    configure_default_environment(_PROJECT_ROOT)

    if len(sys.argv) >= 3 and sys.argv[1] == "--sse":
        # SSE 模式 — 供 Pi 和其他 Agent 通过 HTTP 连接
        os.environ.setdefault("PLASTIC_MCP_TRANSPORT", "sse")
        port = int(sys.argv[2])
        await run_sse(port)
    else:
        # stdio 模式 — 供 Claude Code 本地调用
        os.environ.setdefault("PLASTIC_MCP_TRANSPORT", "stdio")
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
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    logger = logging.getLogger("plastic-promise-sse")
    import time as _time

    start_time = _time.time()

    sse = SseServerTransport("/messages")
    streamable_http = StreamableHTTPSessionManager(app=server)

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
        async with sse.connect_sse(request.scope, request.receive, request._send) as (
            read_stream,
            write_stream,
        ):
            init_options = server.create_initialization_options()
            await server.run(read_stream, write_stream, init_options, raise_exceptions=False)
        return _NoOpResponse()

    async def handle_events(request: Request):
        """SSE event stream — push notifications to connected clients.

        Uses raw ASGI send to avoid Starlette StreamingResponse lifecycle conflicts.
        """
        import json as _json

        # Send SSE headers manually
        await request._send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-cache"),
                    (b"connection", b"keep-alive"),
                ],
            }
        )

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
                body = b'data: {"type":"heartbeat"}\n\n'
                try:
                    await request._send(
                        {"type": "http.response.body", "body": body, "more_body": True}
                    )
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
                    for mem in engine.iter_memories():
                        mid = mem.get("id", "")
                        if mid and "audit" in mem.get("tags", []):
                            mtags = list(mem.get("tags", []))
                            if "status:replaced" not in mtags:
                                mtags.append("status:replaced")
                                engine.update_memory_fields(mid, tags=mtags)
                    engine.register_memory(
                        {
                            "content": report_text,
                            "memory_type": "reflection",
                            "tags": [
                                "audit",
                                "domain:governing",
                                f"score:{event.get('overall', 0):.2f}",
                            ],
                            "source": "maintenance_daemon",
                        }
                    )
                except Exception:
                    pass

            # Refresh in-memory cache when daemon updates classification via SQLite
            if event.get("type") == "llm_classified":
                try:
                    engine = get_engine()
                    mid = event.get("memory_id", "")
                    new_category = event.get("new_category", "")
                    if mid and engine.memory_exists(mid):
                        engine.update_memory_fields(mid, category=new_category)
                        # Update tags to reflect classification state
                        mem = engine.get_memory_dict(mid)
                        tags = list(mem.get("tags", [])) if mem else []
                        if "llm_pending:true" in tags:
                            tags.remove("llm_pending:true")
                        if "llm_classified:true" not in tags:
                            tags.append("llm_classified:true")
                        if new_category and f"cat:{new_category}" not in tags:
                            tags.append(f"cat:{new_category}")
                        engine.update_memory_fields(mid, tags=tags)
                except Exception:
                    pass

            return JSONResponse({"ok": True})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def health(request):
        from starlette.responses import JSONResponse

        return JSONResponse(
            {
                "status": "ok",
                "uptime": round(_time.time() - start_time, 1),
                "version": PLASTIC_PROMISE_VERSION,
                "pid": os.getpid(),
            }
        )

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
            return JSONResponse(
                {
                    "memory": stats,
                    "body_systems": systems,
                    "uptime": round(_time.time() - start_time, 1),
                    "version": PLASTIC_PROMISE_VERSION,
                }
            )
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
            from plastic_promise.mcp.tools.audit_defense import handle_audit_run, handle_defense

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

            result = await handle_skill_auto_track(
                engine,
                {
                    "phase": body.get("phase", "start"),
                    "skill_name": body.get("skill_name", ""),
                },
            )
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
<h1><span class="status status-ok" id="status-dot"></span>Plastic Promise Dashboard <small style="color:#8b949e">v__PLASTIC_PROMISE_VERSION__</small></h1>

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
        return HTMLResponse(
            html.replace("__PLASTIC_PROMISE_VERSION__", PLASTIC_PROMISE_VERSION)
        )

    async def shutdown():
        logger.info("Shutting down Plastic Promise SSE server...")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        async with streamable_http.run():
            try:
                yield
            finally:
                await shutdown()

    async def handle_messages(request: Request):
        """Wrap sse.handle_post_message as a Starlette Route endpoint.

        sse.handle_post_message is an ASGI app that sends its own response
        via request._send.  Starlette's request_response wrapper would try
        to call the return value as a Response, so we return a no-op sentinel.
        """
        await sse.handle_post_message(request.scope, request.receive, request._send)
        return _NoOpResponse()

    async def handle_mcp(request: Request):
        """Streamable HTTP MCP endpoint used by Codex and modern MCP clients."""
        await streamable_http.handle_request(request.scope, request.receive, request._send)
        return _NoOpResponse()

    app = Starlette(
        routes=[
            Route("/mcp", endpoint=handle_mcp, methods=["GET", "POST", "DELETE"]),
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
        ],
        lifespan=lifespan,
    )

    logger.info("Plastic Promise MCP Server v%s", PLASTIC_PROMISE_VERSION)
    logger.info(f"Streamable HTTP endpoint: http://127.0.0.1:{port}/mcp")
    logger.info(f"SSE endpoint: http://127.0.0.1:{port}/sse")
    logger.info(f"Health:      http://127.0.0.1:{port}/health")
    logger.info(f"PID: {os.getpid()}")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    asyncio.run(main())
