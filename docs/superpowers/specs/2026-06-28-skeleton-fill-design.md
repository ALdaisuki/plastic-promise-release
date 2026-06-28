# Skeleton Fill Design — Plastic Promise 全模块骨架补齐

> Date: 2026-06-28
> Status: approved
> Scope: 补齐所有 .py / .rs 文件的完整签名、docstring、类型标注、模块间 import，逻辑留 pass 占位

## 1. Goal

服务器崩溃致全部代码和数据丢失（863 条记忆、13 个核心模块、22 个脚本全部归零）。当前仓库的所有文件——包括 `constants.py`（446行）、`context_engine.py`（323行）、MCP Server 框架和 Rust 骨架——均为本轮对话中基于架构思路新建，并非崩溃前原版的恢复。它们同样是骨架级代码，需要和其他模块一起补全到对话总结中记录的职责范围。本设计定义骨架补齐的范围、结构、深度和验收标准。

## 2. Directory Structure

```
plastic_promise/
├── __init__.py
├── core/                        # 基础层（迁移 + 补全）
│   ├── __init__.py
│   ├── constants.py             # ← 重定位 + 对照总结补全到 ~150 行（当前 446 行已有基础）
│   └── context_engine.py        # ← 重定位 + 对照总结补全到 ~900 行（当前 323 行是精简版）
├── memory/                      # 记忆系统
│   ├── __init__.py
│   └── soul_memory.py
├── loop/                        # 主控编排
│   ├── __init__.py
│   └── soul_loop.py
├── principles/                  # 原则遗传
│   ├── __init__.py
│   └── soul_principles.py
├── reflection/                  # 认知系统
│   ├── __init__.py
│   ├── soul_scarf.py
│   ├── soul_proprioception.py
│   └── soul_curiosity.py
├── defense/                     # 免疫 + 反射弧
│   ├── __init__.py
│   ├── soul_enforcer.py
│   └── soul_audit.py
├── growth/                      # 内分泌 + 技能
│   ├── __init__.py
│   ├── soul_hormone.py
│   ├── soul_classifier.py
│   └── skill_extractor.py
└── mcp/                         # MCP 接口层（补全 + 拆分）
    ├── __init__.py
    ├── server.py                # ← 保留路由，handler 委托到 tools/
    ├── resources.py             # ← 新增
    ├── prompts.py               # ← 新增
    └── tools/
        ├── __init__.py
        ├── memory.py            # 7 handlers: recall/store/update/forget/stats/list/gc
        ├── principles.py        # 4 handlers: activate/inherit/diffuse/evaluate
        ├── context.py           # 3 handlers: supply/inject/graph
        ├── audit_defense.py     # 5 handlers: audit_run/pre_check/report/defense_trust/status
        ├── reflection.py        # 3 handlers: scarf_reflect/inertia_check/feedback_apply
        └── management.py        # 3 handlers: system_stats/backup/migrate
```

## 3. Skeleton Depth (Level B)

Every function/method MUST have:
- Complete type annotations (parameters and return type)
- Full docstring with: one-line summary, `Args:` section, `Returns:` section
- Body: `pass` or `...`

Example:
```python
def memory_recall(
    self,
    query: str,
    task_type: str = "general",
    max_results: int = 20,
    min_relevance: float = 0.2,
    include_principles: bool = True,
) -> dict[str, Any]:
    """Hybrid retrieval of memories via vector search + graph traversal.

    Returns a three-layer context pack (core / related / divergent)
    with activated principles and audit metadata.

    Args:
        query: Search query or task description.
        task_type: Task category for principle activation.
        max_results: Maximum entries per layer.
        min_relevance: Minimum relevance score threshold.
        include_principles: Whether to inject activated principles.

    Returns:
        dict with keys: core, related, divergent, activated_principles, audit.
    """
    pass
```

## 4. Interface Contract

All modules import from two canonical locations:

```python
from plastic_promise.core.constants import (
    CORE_PRINCIPLES, DIGITAL_BODY_SYSTEMS, DEFENSE_LAYERS,
    AUDIT_DIMENSIONS, SCARF_DIMENSIONS, MEMORY_TIERS,
)
from plastic_promise.core.context_engine import ContextEngine, ContextPack
```

No cross-imports between sibling subsystems (memory/loop/principles etc.) during skeleton phase — only `core` imports.

## 5. Execution Strategy

**Approach B: Parallel subagents.** 7 subsystems + 1 MCP tool-split dispatched concurrently.

| Agent | Scope | Files |
|-------|-------|-------|
| A1 | core/ | `__init__.py`, migrate `constants.py`, migrate `context_engine.py` |
| A2 | memory/ | `__init__.py`, `soul_memory.py` |
| A3 | loop/ | `__init__.py`, `soul_loop.py` |
| A4 | principles/ | `__init__.py`, `soul_principles.py` |
| A5 | reflection/ | `__init__.py`, `soul_scarf.py`, `soul_proprioception.py`, `soul_curiosity.py` |
| A6 | defense/ | `__init__.py`, `soul_enforcer.py`, `soul_audit.py` |
| A7 | growth/ | `__init__.py`, `soul_hormone.py`, `soul_classifier.py`, `skill_extractor.py` |
| A8 | mcp/tools/ | 6 tool files + `resources.py` + `prompts.py`, update `server.py` |

## 6. Acceptance Criteria

1. All files exist at their target paths with correct `__init__.py` exports
2. Every public function/method has complete type annotations and docstring
3. `from plastic_promise.core.constants import CORE_PRINCIPLES` works from any module
4. `from plastic_promise.core.context_engine import ContextEngine` works
5. `server.py`'s `call_tool` delegates to tool files (no inline handler logic)
6. All `__init__.py` files export their public API
7. Zero implementation logic beyond `pass` / `...` and return-type placeholders
8. Rust `.rs` files have doc comments (`///`) on all public items
9. `constants.py` covers all categories recorded in对话总结: 九大系统、三层防线、信任分、审计维度、11条原则、SCARF维度、记忆分层
10. `context_engine.py` covers all five components: ContextPack, EntityGraph, RankFuser, SourceTracker, AssociationFeedback — with signatures matching the ContextEngine.supply() pipeline
11. Each `soul_*.py` module exposes the public API implied by its documented responsibilities (no missing major function groups)

## 7. Out of Scope

- Actual implementation logic
- Tests
- Cron job scripts (22 scripts — separate effort)
- Rust crate implementation (skeleton only for .rs files)
