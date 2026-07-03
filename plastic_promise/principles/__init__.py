"""原则遗传系统（DNA/基因遗传）

核心约定跨 Agent 代际传递:
work->all / life->all 单向扩散 + 同步衰减。

注意: 核心实现已迁移至 plastic_promise.core.principles。
此模块保留为兼容性重导出。
"""

from plastic_promise.core.principles import (
    PrincipleManager,
    principle_activate,
    principle_diffuse,
    principle_evaluate,
    principle_inherit,
)

__all__ = [
    "PrincipleManager",
    "principle_activate",
    "principle_inherit",
    "principle_diffuse",
    "principle_evaluate",
]
