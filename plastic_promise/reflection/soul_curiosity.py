"""好奇心探索引擎

基于强化学习中 epsilon-greedy 策略的好奇心驱动探索模块。
在"利用已知最优行为"与"探索未知可能"之间动态平衡：
- epsilon 概率下进行随机探索（发现新的方向或知识）
- 1 - epsilon 概率下利用当前最佳策略

提供模块级便捷函数 `curiosity_explore` 以及可实例化的 `CuriosityExplorer` 类。
"""

from typing import Any, Dict

from plastic_promise.core.constants import CURIOSITY_EXPLORE_RATE


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
        pass

    def should_explore(self) -> bool:
        """基于 epsilon-greedy 策略判断本次是否应进行探索。

        以 explore_rate 概率返回 True（探索），
        以 1 - explore_rate 概率返回 False（利用）。

        Returns:
            bool: True 表示应进行探索，False 表示应利用已知策略。
        """
        pass

    def get_exploration_suggestion(
        self,
        current_context: str,
    ) -> Dict[str, Any]:
        """根据当前上下文生成探索建议。

        基于当前情境和已有的探索历史，
        推荐可能值得探索的新方向或知识领域。

        Args:
            current_context: 当前上下文描述（任务、场景、问题等）。

        Returns:
            Dict[str, Any]: 探索建议，包含：
                - should_explore: 是否建议探索
                - suggested_direction: 建议的探索方向
                - rationale: 建议理由
                - risk_level: 探索风险级别（low/medium/high）
        """
        pass

    def record_exploration(
        self,
        topic: str,
        result: Dict[str, Any],
    ) -> None:
        """记录一次探索的结果。

        将探索主题和结果存入历史记录，
        并更新内部统计数据。

        Args:
            topic: 探索主题描述。
            result: 探索结果详情，包含收获、发现、评估等信息。
        """
        pass

    def get_exploration_stats(self) -> Dict[str, Any]:
        """获取探索统计信息。

        汇总历次探索的统计数据，包括探索次数、
        各方向分布、平均收获等指标。

        Returns:
            Dict[str, Any]: 探索统计，包含：
                - total_explorations: 总探索次数
                - topics: 探索过的主题分布
                - avg_reward: 探索的平均收获评分
                - explore_utilize_ratio: 探索/利用的实际比例
                - most_valuable_topic: 最有价值的探索主题
        """
        pass


def curiosity_explore(current_context: str) -> Dict[str, Any]:
    """模块级别的便捷探索函数。

    创建临时 CuriosityExplorer 对当前上下文进行一次性探索判断和建议，
    适用于不需要维护长期探索历史的轻量使用场景。

    Args:
        current_context: 当前上下文描述。

    Returns:
        Dict[str, Any]: 探索结果，格式与
            CuriosityExplorer.get_exploration_suggestion() 返回值一致。
    """
    pass
