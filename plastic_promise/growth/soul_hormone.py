"""内分泌系统 — 实时反馈激素引擎

信任分联动的情感账户 + 激素调控，驱动 Agent 内部状态变化。
"""

import time
from typing import Any

from plastic_promise.core.constants import (
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
        self.balance: float = 0.0
        self._transactions: list[dict[str, Any]] = list()

    def deposit(self, amount: float, reason: str = "") -> float:
        """存入正反馈激素。

        Args:
            amount: 存入量，应为正数。
            reason: 存入原因，用于审计追溯。

        Returns:
            操作后的账户余额。
        """
        self.balance += amount
        self._transactions.append(
            {
                "type": "deposit",
                "amount": amount,
                "reason": reason,
                "balance_after": self.balance,
                "timestamp": time.time(),
            }
        )
        return self.balance

    def withdraw(self, amount: float, reason: str = "") -> float:
        """提取负反馈激素（惩罚）。

        Args:
            amount: 提取量，应为正数；内部转为扣除。
            reason: 提取原因，用于审计追溯。

        Returns:
            操作后的账户余额。
        """
        self.balance -= amount
        self._transactions.append(
            {
                "type": "withdraw",
                "amount": amount,
                "reason": reason,
                "balance_after": self.balance,
                "timestamp": time.time(),
            }
        )
        return self.balance

    def get_balance(self) -> float:
        """获取当前情感账户余额。

        Returns:
            当前余额，可能为负数（净惩罚状态）。
        """
        return self.balance

    def get_transaction_count(self) -> int:
        """获取历史交易笔数。"""
        return len(self._transactions)

    def get_recent_transactions(self, n: int = 10) -> list[dict[str, Any]]:
        """获取最近 n 笔交易记录。"""
        return self._transactions[-n:] if self._transactions else []


class HormoneEngine:
    """激素引擎 — 将反馈事件转化为内部激素值并联动信任分。

    负责：
    - 将 feedback_type 映射到 dopamine / cortisol 变化
    - 调用 EmotionAccount 完成存取
    - 联调信任管理器 (如果提供)
    - 基于 TRUST_BOOST_RATE / TRUST_DECAY_RATE 计算信任分增量
    """

    def __init__(self, trust_manager: Any | None = None, target: str = "claude") -> None:
        """初始化激素引擎。

        Args:
            trust_manager: 可选的信任管理器实例，若提供则反馈结果会
                同步推送到该管理器以联动信任分。
            target: 信任分追踪目标 (claude/pi_builder 等)。
        """
        self.account = EmotionAccount()
        self.dopamine: float = 0.5  # 中性基线
        self.cortisol: float = 0.3  # 低基线
        self.trust_manager = trust_manager
        self.target = target

    def apply_feedback(
        self,
        feedback_type: str,
        intensity: float = 1.0,
        context: str = "",
    ) -> dict[str, Any]:
        """应用一条反馈，返回本次反馈的完整影响报告。

        Args:
            feedback_type: 反馈类型 ("adopted", "ignored", "rejected")。
            intensity: 反馈强度乘数，默认 1.0。大于 1 表示放大反馈效果。
            context: 触发反馈的上下文描述。

        Returns:
            影响报告字典，包含:
            - hormone_changes: Dict[str, float] 各激素变化量
            - trust_delta: float 信任分变化量 (0 如果无 trust_manager)
            - account_balance: float 操作后情感账户余额
            - context: str
        """
        dopamine_delta = 0.0
        cortisol_delta = 0.0

        if feedback_type == "adopted":
            dopamine_delta = 0.1 * intensity
            cortisol_delta = -0.05 * intensity
            self.account.deposit(intensity * 0.1, reason=f"adopted: {context}")
        elif feedback_type == "ignored":
            cortisol_delta = 0.03 * intensity
            self.account.withdraw(intensity * 0.03, reason=f"ignored: {context}")
        elif feedback_type == "rejected":
            cortisol_delta = 0.2 * intensity
            dopamine_delta = -0.1 * intensity
            self.account.withdraw(intensity * 0.2, reason=f"rejected: {context}")
        else:
            # Unknown feedback type — treat as neutral, no hormone changes
            return {
                "hormone_changes": {"dopamine": 0.0, "cortisol": 0.0},
                "trust_delta": 0.0,
                "account_balance": self.account.get_balance(),
                "context": context,
            }

        self.dopamine += dopamine_delta
        self.cortisol += cortisol_delta

        # Clamp to [0, 1]
        self.dopamine = max(0.0, min(1.0, self.dopamine))
        self.cortisol = max(0.0, min(1.0, self.cortisol))

        # Compute trust delta
        trust_delta = self._compute_trust_delta(feedback_type, intensity)

        return {
            "hormone_changes": {
                "dopamine": dopamine_delta,
                "cortisol": cortisol_delta,
            },
            "trust_delta": trust_delta,
            "account_balance": self.account.get_balance(),
            "context": context,
        }

    def _compute_trust_delta(self, feedback_type: str, intensity: float) -> float:
        """根据反馈类型计算信任分变化量。

        如果有绑定 trust_manager，也会同步推送。
        """
        delta = 0.0
        if feedback_type == "adopted":
            delta = TRUST_BOOST_RATE * intensity
        elif feedback_type == "rejected":
            delta = -TRUST_DECAY_RATE * intensity
        elif feedback_type == "ignored":
            delta = -TRUST_DECAY_RATE * 0.3 * intensity  # mild decay

        # Sync to trust_manager if available
        if self.trust_manager is not None and delta != 0.0:
            try:
                if delta > 0:
                    self.trust_manager.boost(abs(delta), target=self.target)
                else:
                    self.trust_manager.decay(abs(delta), target=self.target)
            except AttributeError:
                # trust_manager does not support boost/decay; ignore
                pass

        return delta

    def get_hormone_status(self) -> dict[str, Any]:
        """获取当前激素系统状态快照。

        Returns:
            状态字典，包含:
            - dopamine: float
            - cortisol: float
            - d_c_ratio: float (dopamine / cortisol, 天花板 10.0)
            - mood: str ("positive" / "neutral" / "stressed")
        """
        # Avoid division by zero
        if self.cortisol > 0:
            d_c_ratio = min(self.dopamine / self.cortisol, 10.0)
        else:
            d_c_ratio = 10.0 if self.dopamine > 0 else 1.0

        # Mood classification
        if d_c_ratio > 2.0:
            mood = "positive"
        elif d_c_ratio >= 1.0:
            mood = "neutral"
        else:
            mood = "stressed"

        return {
            "dopamine": round(self.dopamine, 4),
            "cortisol": round(self.cortisol, 4),
            "d_c_ratio": round(d_c_ratio, 4),
            "mood": mood,
        }
