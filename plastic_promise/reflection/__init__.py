"""认知系统（前额叶/探索欲）

包含:
- SCARF 五维度自省 (Status/Certainty/Autonomy/Relatedness/Fairness)
- 本体觉 + 惯性抑制
- 好奇心探索引擎
"""

from plastic_promise.reflection.soul_scarf import SCARFReflector, scarf_reflect
from plastic_promise.reflection.soul_proprioception import (
    ProprioceptionManager,
    inertia_check,
)
from plastic_promise.reflection.soul_curiosity import CuriosityExplorer, curiosity_explore

__all__ = [
    "SCARFReflector",
    "scarf_reflect",
    "ProprioceptionManager",
    "inertia_check",
    "CuriosityExplorer",
    "curiosity_explore",
]
