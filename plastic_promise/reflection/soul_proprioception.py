"""本体觉与惯性抑制引擎

"本体觉"（Proprioception）原指生物感知自身身体位置和运动的能力。
在数字身体系统中，本体觉负责：
- 检测 Agent 是否陷入重复性任务循环（惯性检测）
- 识别行为模式僵化，提示需要切换注意力或引入变化
- 维护任务历史记录，支持模式分析

提供模块级便捷函数 `inertia_check` 以及可实例化的 `ProprioceptionManager` 类。
"""

from collections import Counter, deque
from typing import Any, Dict, List

from plastic_promise.core.constants import (
    INERTIA_SUPPRESSION_WINDOW,
    INERTIA_SUPPRESSION_THRESHOLD,
)


class ProprioceptionManager:
    """本体觉管理器。

    通过追踪最近任务的相似度来检测行为惯性（重复僵化），
    当连续任务的相似度超过阈值时触发惯性告警。

    Attributes:
        window_size: 滑动窗口大小（最近 N 个任务）。
        threshold: 相似度阈值，超过此值认为陷入惯性。
        recent_tasks: 任务描述的滑动窗口（deque）。
        suppressed_count: 累计惯性抑制次数。
    """

    def __init__(
        self,
        window_size: int = INERTIA_SUPPRESSION_WINDOW,
        threshold: float = INERTIA_SUPPRESSION_THRESHOLD,
    ) -> None:
        """初始化 ProprioceptionManager。

        Args:
            window_size: 连续相似任务检测的滑动窗口大小。
                         默认使用 INERTIA_SUPPRESSION_WINDOW 常量。
            threshold: 判定任务相似度过高的阈值（0.0 ~ 1.0）。
                       默认使用 INERTIA_SUPPRESSION_THRESHOLD 常量。
        """
        self.window_size = window_size
        self.threshold = threshold
        self.recent_tasks: deque = deque(maxlen=window_size)
        self.suppressed_count = 0

    @staticmethod
    def _jaccard_similarity(text1: str, text2: str) -> float:
        """计算两个文本的 Jaccard 词集相似度。

        Args:
            text1: 第一个文本。
            text2: 第二个文本。

        Returns:
            float: 0.0 到 1.0 之间的相似度。
        """
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        if not words1 or not words2:
            return 0.0
        intersection = words1 & words2
        union = words1 | words2
        return len(intersection) / len(union)

    def check_inertia(self, recent_tasks: List[str]) -> Dict[str, Any]:
        """检查近期任务列表是否存在惯性僵化。

        对给定的任务列表计算两两相似度，
        若在窗口内连续任务相似度超过阈值则触发告警。

        Args:
            recent_tasks: 近期任务描述字符串列表。

        Returns:
            Dict[str, Any]: 惯性检查结果，包含：
                - inertia_detected: 是否检测到惯性僵化
                - similarity_score: 平均 Jaccard 相似度
                - max_similarity: 窗口内最大相似度
                - suggestion: 突破惯性的建议（仅在检测到时）
        """
        if len(recent_tasks) < 3:
            return {"inertia_detected": False}

        # 计算两两 Jaccard 相似度
        similarities: List[float] = []
        for i in range(len(recent_tasks)):
            for j in range(i + 1, len(recent_tasks)):
                sim = self._jaccard_similarity(recent_tasks[i], recent_tasks[j])
                similarities.append(sim)

        avg_similarity = sum(similarities) / len(similarities)
        max_similarity = max(similarities)
        inertia_detected = avg_similarity > self.threshold

        result: Dict[str, Any] = {
            "inertia_detected": inertia_detected,
            "similarity_score": avg_similarity,
            "avg_similarity": avg_similarity,
            "max_similarity": max_similarity,
        }

        if inertia_detected:
            self.suppressed_count += 1
            result["suggestion"] = (
                "检测到任务模式惯性僵化（相似度 {:.2%}）。"
                "建议：1) 切换到不同领域的任务；"
                "2) 引入新的方法论或工具；"
                "3) 暂时休息或切换上下文以打破惯性循环。"
            ).format(avg_similarity)

        return result

    def record_task(self, task_description: str) -> None:
        """将一条任务描述计入本体觉历史记录。

        自动维护任务历史列表，保持在窗口大小范围内（deque 自动淘汰）。

        Args:
            task_description: 任务描述字符串。
        """
        self.recent_tasks.append(task_description)

    def get_pattern_analysis(self) -> Dict[str, Any]:
        """分析历史任务的行为模式。

        基于当前记录的任务历史，识别高频模式、
        任务多样性指标和行为趋势。

        Returns:
            Dict[str, Any]: 模式分析结果，包含：
                - dominant_patterns: 识别出的重复行为模式列表
                - variety_score: 任务多样性评分（0~1）
                - suggestions: 突破模式的建议
                - total_tasks: 已记录的任务总数
        """
        # 收集所有词汇并统计频率
        all_words: List[str] = []
        for task in self.recent_tasks:
            all_words.extend(task.lower().split())

        word_counts = Counter(all_words)
        most_common = word_counts.most_common(10)

        # 出现至少 2 次的词视为主导模式词汇
        dominant_patterns = [word for word, count in most_common if count >= 2]

        # 多样性评分 = 唯一词汇数 / 总词汇数
        if all_words:
            variety_score = len(set(all_words)) / len(all_words)
        else:
            variety_score = 0.0

        suggestions: List[str] = []
        if variety_score < 0.5:
            suggestions.append(
                "任务词汇多样性较低，建议尝试不同领域的任务以保持探索活力"
            )
        if len(self.recent_tasks) >= self.window_size:
            suggestions.append(
                f"已达到窗口上限（{self.window_size}），旧任务将被自动淘汰"
            )

        return {
            "dominant_patterns": dominant_patterns,
            "variety_score": variety_score,
            "suggestions": suggestions,
            "total_tasks": len(self.recent_tasks),
            # 向后兼容的别名字段
            "patterns": dominant_patterns,
            "diversity_score": variety_score,
            "trend": "重复模式" if variety_score < 0.5 else "多样化",
        }


def inertia_check(recent_tasks: List[str]) -> Dict[str, Any]:
    """模块级别的便捷惯性检查函数。

    创建临时 ProprioceptionManager 对给定任务列表进行一次性惯性检查，
    适用于不需要维护长期历史状态的轻量使用场景。

    Args:
        recent_tasks: 近期任务描述字符串列表。

    Returns:
        Dict[str, Any]: 惯性检查结果，格式与
            ProprioceptionManager.check_inertia() 返回值一致。
    """
    pm = ProprioceptionManager()
    return pm.check_inertia(recent_tasks)
