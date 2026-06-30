"""Reflection data structures — 反思生成的数据模型

反思不在 step-closure 时同步生成。
任务上下文以 task:pending 标签入池，由 daemon 通过标签状态机
分发给 Pi Agent 异步分析，填充 lesson/improvement/root_cause/optimization。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ============================================================
# Data classes
# ============================================================


@dataclass
class ReflectionContext:
    """LLMReflector 输入上下文 — 聚合运行时所有信号。"""

    task_description: str = ""
    scarf_scores: Dict[str, float] = field(default_factory=dict)
    scarf_assessments: Dict[str, str] = field(default_factory=dict)
    scarf_overall: float = 0.65
    trust_score: float = 0.60
    cei_score: float = 0.50
    cei_tier: str = "forming"
    git_commit: str = ""
    alignment_principles: List[str] = field(default_factory=list)
    hormone_dopamine: float = 0.5
    hormone_cortisol: float = 0.3


@dataclass
class ReflectionResult:
    """LLMReflector 输出 — 结构化反思。"""

    lesson: str = ""
    improvement: str = ""
    root_cause: str = ""
    optimization: str = ""
    source: str = ""       # "llm" | ""
    model: str = ""


# ============================================================
# 反思由 daemon 通过标签机异步派发给 Pi Agent 生成。
# 此处仅定义数据结构。step-closure 时留空，不在此处阻塞。
# ============================================================
