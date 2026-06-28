"""原则遗传系统（DNA/基因遗传）

核心约定跨 Agent 代际传递:
work->all / life->all 单向扩散 + 同步衰减。
"""

from plastic_promise.principles.soul_principles import (
    PrincipleManager,
    principle_activate,
    principle_inherit,
    principle_diffuse,
    principle_evaluate,
)

__all__ = [
    "PrincipleManager",
    "principle_activate",
    "principle_inherit",
    "principle_diffuse",
    "principle_evaluate",
]
