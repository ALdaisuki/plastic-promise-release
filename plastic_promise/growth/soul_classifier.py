"""任务分类器 — 45关键词 + ACP 路由

基于 CLASSIFIER_KEYWORDS 的关键词匹配，将用户指令分类为不同任务类型，
并根据分类得分决定路由目标 (Claude Code 或 ACP)。
"""

from typing import Any, Dict, List

from plastic_promise.core.constants import (
    CLASSIFIER_KEYWORDS,
    CLASSIFIER_THRESHOLD_ACP,
    CLASSIFIER_THRESHOLD_CLAUDE,
)


class TaskClassifier:
    """基于关键词的任务分类与路由引擎。

    分类结果包含：
    - 匹配到的关键词及类别
    - 分类得分
    - 路由建议 (claude / acp / ambiguous)
    """

    def __init__(self) -> None:
        """初始化分类器，预加载关键词索引。"""
        pass

    def classify(self, instruction: str) -> Dict[str, Any]:
        """对单条指令进行分类。

        Args:
            instruction: 用户指令文本。

        Returns:
            分类结果字典，包含:
            - instruction: str (原始指令)
            - matched_keywords: List[str] (匹配到的关键词)
            - score: int (匹配得分)
            - route: str (路由建议)
            - categories: Dict[str, int] (各类别命中次数)
        """
        pass

    def route(self, instruction: str) -> str:
        """对单条指令执行路由决策。

        Args:
            instruction: 用户指令文本。

        Returns:
            路由目标字符串，取值为: "acp", "claude", 或 "ambiguous"。
            阈值定义在 constants.CLASSIFIER_THRESHOLD_ACP 和
            constants.CLASSIFIER_THRESHOLD_CLAUDE。
        """
        pass

    def batch_classify(self, instructions: List[str]) -> List[Dict[str, Any]]:
        """批量分类多条指令。

        Args:
            instructions: 用户指令文本列表。

        Returns:
            与每条指令对应的分类结果列表，元素结构与 classify() 相同。
        """
        pass

    @property
    def accuracy_stats(self) -> Dict[str, Any]:
        """分类准确率统计。

        Returns:
            统计字典，包含:
            - total_classified: int
            - routes: Dict[str, int] (各路由目标计数)
            - avg_score: float
        """
        pass


def classify_task(instruction: str) -> Dict[str, Any]:
    """模块级便捷函数 — 使用默认分类器实例对单条指令分类。

    Args:
        instruction: 用户指令文本。

    Returns:
        分类结果字典，结构与 TaskClassifier.classify() 相同。
    """
    pass
