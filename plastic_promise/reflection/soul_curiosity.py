"""好奇心探索引擎

基于强化学习中 epsilon-greedy 策略的好奇心驱动探索模块。
在"利用已知最优行为"与"探索未知可能"之间动态平衡：
- epsilon 概率下进行随机探索（发现新的方向或知识）
- 1 - epsilon 概率下利用当前最佳策略

提供模块级便捷函数 `curiosity_explore` 以及可实例化的 `CuriosityExplorer` 类。
"""

import random
from typing import Any

from plastic_promise.core.constants import CURIOSITY_EXPLORE_RATE

# 好奇心探索覆盖的八个主题类别
_ALL_TOPIC_CATEGORIES: list[str] = [
    "code_patterns",
    "architecture",
    "testing",
    "performance",
    "security",
    "tools",
    "collaboration",
    "learning",
]

_RATIONALE_TEMPLATES: dict[str, str] = {
    "code_patterns": "发现新的设计模式或编码实践，提高代码可维护性",
    "architecture": "探索系统架构的改进点或模式，增强模块间协同",
    "testing": "寻找未被覆盖的测试场景或新的测试策略",
    "performance": "识别潜在的性能瓶颈或优化机会",
    "security": "主动探查安全边界，发现潜在风险面",
    "tools": "探索新工具或自动化手段，提升工作效率",
    "collaboration": "寻找团队协作中的改善点，增强信息透明度",
    "learning": "接触陌生知识领域，拓宽系统认知边界",
}


class CuriosityExplorer:
    """好奇心探索器。

    使用 epsilon-greedy 策略决定是否在当前情境下进行探索，
    并维护探索历史和统计信息以支持后续分析。

    Attributes:
        explore_rate: 探索概率 epsilon（0.0 ~ 1.0）。
        exploration_history: 历次探索记录的列表。
        stats: 探索统计数据缓存。
    """

    def __init__(
        self,
        explore_rate: float = CURIOSITY_EXPLORE_RATE,
    ) -> None:
        """初始化 CuriosityExplorer。

        Args:
            explore_rate: epsilon-greedy 探索率，取值范围 0.0 ~ 1.0。
                          默认使用 CURIOSITY_EXPLORE_RATE 常量。
        """
        self.explore_rate = explore_rate
        self.explored_topics: set = set()
        self._exploration_history: list = []
        self._explore_count: int = 0

    def should_explore(self) -> bool:
        """基于 epsilon-greedy 策略判断本次是否应进行探索。

        以 explore_rate 概率返回 True（探索），
        以 1 - explore_rate 概率返回 False（利用）。

        Returns:
            bool: True 表示应进行探索，False 表示应利用已知策略。
        """
        return random.random() < self.explore_rate

    def get_exploration_suggestion(
        self,
        current_context: str,
    ) -> dict[str, Any]:
        """根据当前上下文生成探索建议。

        基于当前情境和已有的探索历史，
        推荐可能值得探索的新方向或知识领域。

        Args:
            current_context: 当前上下文描述（任务、场景、问题等）。

        Returns:
            Dict[str, Any]: 探索建议，包含：
                - suggested_topic: 建议探索的主题类别
                - rationale: 建议理由
                - expected_value: 探索的预期价值
        """
        unexplored = [cat for cat in _ALL_TOPIC_CATEGORIES if cat not in self.explored_topics]

        if unexplored:
            suggested_topic = random.choice(unexplored)
            rationale = _RATIONALE_TEMPLATES.get(
                suggested_topic,
                "拓展系统认知，增强应对未知情境的能力",
            )
            expected_value = "发现新的知识领域与改进方向"
        else:
            # 所有类别均已探索过，随机建议一个深入探索
            suggested_topic = random.choice(_ALL_TOPIC_CATEGORIES)
            rationale = f"已覆盖所有类别，建议深入探索 {suggested_topic} 以巩固或发掘更深层次的洞察"
            expected_value = "在已知领域进行更深层次的挖掘"

        return {
            "suggested_topic": suggested_topic,
            "rationale": rationale,
            "expected_value": expected_value,
        }

    def record_exploration(
        self,
        topic: str,
        result: dict[str, Any],
    ) -> None:
        """记录一次探索的结果。

        将探索主题和结果存入历史记录，
        并更新内部统计数据。

        Args:
            topic: 探索主题描述。
            result: 探索结果详情，包含收获、发现、评估等信息。
        """
        self.explored_topics.add(topic)
        self._exploration_history.append(
            {
                "topic": topic,
                "result": result,
            }
        )
        self._explore_count += 1

    def get_exploration_stats(self) -> dict[str, Any]:
        """获取探索统计信息。

        汇总历次探索的统计数据，包括探索次数、
        盲点分析、覆盖比例等指标。

        Returns:
            Dict[str, Any]: 探索统计，包含：
                - total_explorations: 总探索次数
                - topics_covered: 已探索覆盖的主题列表
                - blind_spots: 尚未探索的主题盲区
                - explore_ratio: 已探索主题占比（0.0 ~ 1.0）
        """
        blind_spots = [cat for cat in _ALL_TOPIC_CATEGORIES if cat not in self.explored_topics]
        explore_ratio = (
            len(self.explored_topics) / len(_ALL_TOPIC_CATEGORIES) if _ALL_TOPIC_CATEGORIES else 0.0
        )
        return {
            "total_explorations": self._explore_count,
            "topics_covered": sorted(self.explored_topics),
            "blind_spots": blind_spots,
            "explore_ratio": explore_ratio,
        }


def curiosity_explore(current_context: str) -> dict[str, Any]:
    """模块级别的便捷探索函数。

    创建临时 CuriosityExplorer 对当前上下文进行一次性探索判断和建议，
    适用于不需要维护长期探索历史的轻量使用场景。

    Args:
        current_context: 当前上下文描述。

    Returns:
        Dict[str, Any]: 探索结果，格式与
            CuriosityExplorer.get_exploration_suggestion() 返回值一致。
    """
    explorer = CuriosityExplorer()
    return explorer.get_exploration_suggestion(current_context)


_exploration_log: list[dict[str, Any]] = []
_explore_rate = 0.15


def curiosity_act(suggestion_id: str, outcome: str) -> dict[str, Any]:
    """Record exploration outcome and adapt explore rate.

    Args:
        suggestion_id: ID from curiosity_explore result.
        outcome: "adopted" | "ignored" | "failed"
    """
    global _explore_rate, _exploration_log
    _exploration_log.append(
        {
            "suggestion_id": suggestion_id,
            "outcome": outcome,
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }
    )
    # Adaptive explore rate
    adopted = sum(1 for e in _exploration_log if e["outcome"] == "adopted")
    total = len(_exploration_log)
    adopted_rate = adopted / total if total > 0 else 0.5
    if adopted_rate > 0.7:
        _explore_rate = min(0.30, _explore_rate + 0.02)
    elif adopted_rate < 0.3:
        _explore_rate = max(0.05, _explore_rate - 0.02)
    return {"explore_rate": _explore_rate, "adopted_rate": adopted_rate, "total": total}


def curiosity_stats() -> dict[str, Any]:
    """Return curiosity exploration statistics."""
    adopted = sum(1 for e in _exploration_log if e["outcome"] == "adopted")
    total = len(_exploration_log)
    return {
        "explore_rate": _explore_rate,
        "total_explorations": total,
        "adopted": adopted,
        "ignored": sum(1 for e in _exploration_log if e["outcome"] == "ignored"),
        "failed": sum(1 for e in _exploration_log if e["outcome"] == "failed"),
        "adopted_rate": round(adopted / total, 3) if total > 0 else 0.0,
    }
