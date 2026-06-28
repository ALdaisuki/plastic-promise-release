"""内分泌系统 — 实时反馈激素引擎

信任分联动的情感账户 + 激素调控，驱动 Agent 内部状态变化。
"""

from typing import Any, Dict, Optional

from plastic_promise.core.constants import (
    ASSOCIATION_WEIGHTS,
    TRUST_BOOST_RATE,
    TRUST_DECAY_RATE,
)


class EmotionAccount:
    """情感账户，记录激素存量的收支明细。

    每一次反馈 (deposit / withdraw) 都会产生一条内部流水记录，
    供信任分系统和评价引擎联动使用。
    """

    def __init__(self) -> None:
        """初始化情感账户，余额从零开始。"""
        pass

    def deposit(self, amount: float, reason: str = "") -> float:
        """存入正反馈激素。

        Args:
            amount: 存入量，应为正数。
            reason: 存入原因，用于审计追溯。

        Returns:
            操作后的账户余额。
        """
        pass

    def withdraw(self, amount: float, reason: str = "") -> float:
        """提取负反馈激素（惩罚）。

        Args:
            amount: 提取量，应为正数；内部转为扣除。
            reason: 提取原因，用于审计追溯。

        Returns:
            操作后的账户余额。
        """
        pass

    def get_balance(self) -> float:
        """获取当前情感账户余额。

        Returns:
            当前余额，可能为负数（净惩罚状态）。
        """
        pass


class HormoneEngine:
    """激素引擎 — 将反馈事件转化为内部激素值并联动信任分。

    负责：
    - 将 feedback_type 映射到 ASSOCIATION_WEIGHTS 权重
    - 调用 EmotionAccount 完成存取
    - 联调信任管理器 (如果提供)
    - 基于 TRUST_BOOST_RATE / TRUST_DECAY_RATE 计算信任分增量
    """

    def __init__(self, trust_manager: Optional[Any] = None) -> None:
        """初始化激素引擎。

        Args:
            trust_manager: 可选的信任管理器实例，若提供则反馈结果会
                同步推送到该管理器以联动信任分。
        """
        pass

    def apply_feedback(
        self,
        feedback_type: str,
        intensity: float = 1.0,
        context: str = "",
    ) -> Dict[str, Any]:
        """应用一条反馈，返回本次反馈的完整影响报告。

        Args:
            feedback_type: 反馈类型，应为 ASSOCIATION_WEIGHTS 中的键
                (如 "adopted", "ignored", "rejected")。
            intensity: 反馈强度乘数，默认 1.0。大于 1 表示放大反馈效果。
            context: 触发反馈的上下文描述。

        Returns:
            影响报告字典，包含:
            - feedback_type: str
            - weight: float (关联权重)
            - hormone_change: float (激素变化量)
            - new_balance: float (操作后余额)
            - trust_delta: float (信任分变化量，0 如果无 trust_manager)
            - context: str
        """
        pass

    def get_hormone_status(self) -> Dict[str, Any]:
        """获取当前激素系统状态快照。

        Returns:
            状态字典，包含:
            - balance: float (当前情感账户余额)
            - recent_feedbacks: int (近期反馈次数)
            - dominant_emotion: str (主导情感基调)
            - trust_linked: bool (是否已绑定信任管理器)
        """
        pass
