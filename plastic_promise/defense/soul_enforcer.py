"""三层防线 + 信任分管理器

反射弧系统：快速反应，无需经过高层决策的自动防护。
包含 L0 硬边界预检、L1 信任分驱动的动态约束衰减、L2 免疫巡检的违规记录。

TrustManager — 信任分生命周期管理（boost/decay/history/tier/autonomy_level）
SoulEnforcer — 三层防线执行引擎（pre_check/defense_status/violation_log/stats）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from plastic_promise.core.constants import (
    DEFENSE_LAYERS,
    TRUST_BOOST_RATE,
    TRUST_DECAY_RATE,
    TRUST_INITIAL,
    TRUST_MAX,
    TRUST_MIN,
    TRUST_TIER_CRITICAL,
    TRUST_TIER_HIGH,
    TRUST_TIER_LOW,
    TRUST_TIER_MEDIUM,
)


class TrustManager:
    """信任分管理器 —— 数字内分泌系统的核心引擎。

    管理信任分的增长、衰减、历史追溯，并根据当前信任分推导
    信任等级 (tier) 和自主权级别 (autonomy_level)。

    Attributes:
        _trust: 当前信任分 (0.0 ~ 1.0)
        _history: 信任分变更记录，每条记录包含 (delta, reason, new_value, timestamp)
    """

    def __init__(self, initial_trust: float = TRUST_INITIAL) -> None:
        """初始化信任分管理器。

        Args:
            initial_trust: 初始信任分，默认使用 TRUST_INITIAL (0.60)
        """
        self._trust: float = initial_trust
        self._history: List[Dict[str, Any]] = []

    def boost(self, delta: float, reason: str = "") -> float:
        """增加信任分 —— 因高信任行为而奖励。

        Args:
            delta: 增加量，默认使用 TRUST_BOOST_RATE
            reason: 加分原因的简短描述

        Returns:
            更新后的信任分

        Raises:
            ValueError: 如果 delta 为负数
        """
        pass

    def decay(self, delta: float = TRUST_DECAY_RATE, reason: str = "") -> float:
        """衰减信任分 —— 因低信任行为或时间流逝而降低。

        Args:
            delta: 衰减量，默认使用 TRUST_DECAY_RATE
            reason: 减分原因的简短描述

        Returns:
            更新后的信任分

        Raises:
            ValueError: 如果 delta 为负数
        """
        pass

    def get(self) -> float:
        """获取当前信任分。

        Returns:
            当前信任分 (0.0 ~ 1.0)
        """
        pass

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取信任分变更历史。

        Args:
            limit: 返回的最大条数，默认 50

        Returns:
            变更记录列表，每条包含 delta, reason, new_value, timestamp 等字段
        """
        pass

    @property
    def tier(self) -> str:
        """当前信任等级。

        Returns:
            等级字符串: 'high' | 'medium' | 'low' | 'critical'
        """
        pass

    @property
    def autonomy_level(self) -> str:
        """当前自主权级别。

        Returns:
            自主权级别: 'full' | 'standard' | 'restricted' | 'minimal'
        """
        pass


class SoulEnforcer:
    """三层防线执行引擎 —— 数字反射弧。

    在行动执行前进行 L0/L1/L2 三阶预检，记录违规日志，
    并提供防御状态和违规统计的查询接口。

    Attributes:
        trust_manager: 关联的信任分管理器
        _violation_log: 违规记录列表
    """

    def __init__(self, trust_manager: Optional[TrustManager] = None) -> None:
        """初始化 SoulEnforcer。

        Args:
            trust_manager: 信任分管理器实例。若为 None 则自动创建默认实例。
        """
        self.trust_manager: TrustManager = trust_manager or TrustManager()
        self._violation_log: List[Dict[str, Any]] = []

    def pre_check(self, action_description: str, action_type: str = "exec") -> Dict[str, Any]:
        """行动前三阶预检 —— L0 硬边界 → L1 约束衰减 → L2 免疫巡检。

        按优先级顺序执行三层防御检查，返回检查结果和是否允许执行。

        Args:
            action_description: 待执行操作的描述
            action_type: 操作类型，如 'exec', 'write', 'delete', 'query' 等

        Returns:
            检查结果字典，包含:
            - allowed: bool — 是否允许执行
            - layer: str — 做出裁决的防线层级
            - reason: str — 允许/拒绝的原因
            - trust_used: float — 检查时的信任分
            - warnings: List[str] — 非阻塞性警告
        """
        pass

    def get_defense_status(self) -> Dict[str, Any]:
        """获取当前三层防线整体状态。

        Returns:
            状态字典，包含:
            - L0: Dict — 硬边界状态 (active, rules_count, violations_today)
            - L1: Dict — 约束衰减状态 (trust_level, loosen_threshold, tighten_threshold)
            - L2: Dict — 免疫巡检状态 (last_scan, next_scan, issues_found)
            - trust: float — 当前信任分
            - tier: str — 当前信任等级
        """
        pass

    def log_violation(self, action: str, layer: str, reason: str) -> None:
        """记录一次防线违规事件。

        Args:
            action: 触发违规的操作描述
            layer: 违规发生的防线层级 (L0/L1/L2)
            reason: 违规原因描述
        """
        pass

    def get_violation_stats(self) -> Dict[str, Any]:
        """获取违规统计数据。

        Returns:
            统计字典，包含:
            - total: int — 总违规次数
            - by_layer: Dict[str, int] — 按防线层级分组计数
            - today: int — 今日违规次数
            - recent: List[Dict] — 最近违规记录
        """
        pass
