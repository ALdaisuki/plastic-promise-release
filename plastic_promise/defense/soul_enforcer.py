"""三层防线 + 信任分管理器

反射弧系统：快速反应，无需经过高层决策的自动防护。
包含 L0 硬边界预检、L1 信任分驱动的动态约束衰减、L2 免疫巡检的违规记录。

TrustManager — 信任分生命周期管理（boost/decay/history/tier/autonomy_level）
SoulEnforcer — 三层防线执行引擎（pre_check/defense_status/violation_log/stats）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import re

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
        if delta < 0:
            raise ValueError(f"boost delta must be non-negative, got {delta}")
        old_trust = self._trust
        self._trust = min(TRUST_MAX, self._trust + delta)
        self._history.append({
            "delta": delta,
            "reason": reason,
            "old_value": old_trust,
            "new_value": self._trust,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": "boost",
        })
        return self._trust

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
        if delta < 0:
            raise ValueError(f"decay delta must be non-negative, got {delta}")
        old_trust = self._trust
        self._trust = max(TRUST_MIN, self._trust - delta)
        self._history.append({
            "delta": -delta,
            "reason": reason,
            "old_value": old_trust,
            "new_value": self._trust,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": "decay",
        })
        return self._trust

    def get(self) -> float:
        """获取当前信任分。

        Returns:
            当前信任分 (0.0 ~ 1.0)
        """
        return self._trust

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取信任分变更历史。

        Args:
            limit: 返回的最大条数，默认 50

        Returns:
            变更记录列表，每条包含 delta, reason, new_value, timestamp 等字段
        """
        if not self._history:
            return []
        return self._history[-limit:]

    @property
    def tier(self) -> str:
        """当前信任等级。

        Returns:
            等级字符串: 'high' | 'medium' | 'low' | 'critical'
        """
        if self._trust >= 0.80:
            return "high"
        elif self._trust >= 0.50:
            return "medium"
        elif self._trust >= 0.30:
            return "low"
        else:
            return "critical"

    @property
    def autonomy_level(self) -> str:
        """当前自主权级别。

        Returns:
            自主权级别: 'full' | 'standard' | 'restricted' | 'minimal'
        """
        _tier = self.tier
        if _tier == "high":
            return "full"
        elif _tier == "medium":
            return "standard"
        elif _tier == "low":
            return "restricted"
        else:
            return "minimal"

    def get_retrieval_boost(self) -> float:
        """Return retrieval weight multiplier based on current trust tier.

        High trust → broader context, more risk tolerance.
        Low trust → narrower scope, conservative retrieval.
        Serves 实践层: 动态信任调节信息获取范围。
        """
        _tier = self.tier
        if _tier == "high":
            return 1.3
        elif _tier == "medium":
            return 1.0
        elif _tier == "low":
            return 0.7
        else:  # critical
            return 0.5


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
            - passed: bool — 是否通过检查
            - layer_checks: Dict — 各层检查结果
            - risk_score: float — 风险评分 (0.0 ~ 1.0)
        """
        # 危险模式列表 (L0 硬边界)
        DANGEROUS_PATTERNS = [
            r'\brm\s+-rf\b',
            r'\bDROP\s+TABLE\b',
            r'\bDROP\s+DATABASE\b',
            r'\bformat\s+[CFD]:',
            r'\bshutdown\b',
            r'\bdel\s+/f\b',
            r'\bDEL\s+/F\b',
            r'\bdd\s+if=',
            r'\bmkfs\.',
            r'\bchmod\s+777\b',
        ]

        description_lower = action_description.lower()
        trust = self.trust_manager.get()

        layer_checks: Dict[str, Any] = {
            "L0": {"checked": True, "passed": True, "message": "L0 hard-boundary check passed"},
            "L1": {"checked": True, "passed": True, "message": "L1 constraint-decay check passed"},
            "L2": {"checked": False, "passed": True, "message": "L2 immune-scan deferred (cron)"},
        }
        warnings: List[str] = []
        passed = True
        risk_score = 0.0

        # ---- L0: 硬边界预检 ----
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, description_lower):
                passed = False
                risk_score = 1.0
                layer_checks["L0"]["passed"] = False
                layer_checks["L0"]["message"] = (
                    f"L0 BLOCKED: dangerous pattern detected — '{pattern}'"
                )
                self._violation_log.append({
                    "action": action_description,
                    "layer": "L0",
                    "reason": f"Dangerous pattern match: {pattern}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return {
                    "passed": False,
                    "layer_checks": layer_checks,
                    "risk_score": risk_score,
                    "warnings": warnings,
                    "trust_used": trust,
                }

        # ---- L1: 约束衰减 ----
        if trust < 0.15:
            passed = False
            risk_score = max(risk_score, 0.85)
            layer_checks["L1"]["passed"] = False
            layer_checks["L1"]["message"] = (
                f"L1 BLOCKED: trust ({trust:.2f}) below critical threshold (0.15)"
            )
            self._violation_log.append({
                "action": action_description,
                "layer": "L1",
                "reason": f"Trust too low: {trust:.2f} < 0.15",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        elif trust < 0.40:
            risk_score = max(risk_score, 0.50)
            layer_checks["L1"]["passed"] = True  # still passed, just warned
            layer_checks["L1"]["message"] = (
                f"L1 WARNING: trust ({trust:.2f}) below 0.40 — constraints tightened"
            )
            warnings.append(f"Trust level low ({trust:.2f}); proceed with caution")

        # Risk score adjusted by trust inverse
        if passed:
            risk_score = round(1.0 - trust, 2)

        return {
            "passed": passed,
            "layer_checks": layer_checks,
            "risk_score": risk_score,
            "warnings": warnings,
            "trust_used": trust,
        }

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
        trust = self.trust_manager.get()
        tier = self.trust_manager.tier

        # Count L0/L1 violations from the log
        l0_violations = sum(1 for v in self._violation_log if v["layer"] == "L0")
        l1_violations = sum(1 for v in self._violation_log if v["layer"] == "L1")
        l2_violations = sum(1 for v in self._violation_log if v["layer"] == "L2")

        return {
            "L0": {
                "name": DEFENSE_LAYERS["L0"]["name"],
                "active": True,
                "rules_count": 10,
                "violations_total": l0_violations,
            },
            "L1": {
                "name": DEFENSE_LAYERS["L1"]["name"],
                "active": True,
                "trust_level": tier,
                "loosen_threshold": DEFENSE_LAYERS["L1"]["trust_threshold_loosen"],
                "tighten_threshold": DEFENSE_LAYERS["L1"]["trust_threshold_tighten"],
                "violations_total": l1_violations,
            },
            "L2": {
                "name": DEFENSE_LAYERS["L2"]["name"],
                "active": False,
                "scan_interval_hours": DEFENSE_LAYERS["L2"]["scan_interval_hours"],
                "violations_total": l2_violations,
            },
            "trust": trust,
            "tier": tier,
            "autonomy_level": self.trust_manager.autonomy_level,
        }

    def log_violation(self, action: str, layer: str, reason: str) -> None:
        """记录一次防线违规事件。

        Args:
            action: 触发违规的操作描述
            layer: 违规发生的防线层级 (L0/L1/L2)
            reason: 违规原因描述
        """
        self._violation_log.append({
            "action": action,
            "layer": layer,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def get_violation_stats(self) -> Dict[str, Any]:
        """获取违规统计数据。

        Returns:
            统计字典，包含:
            - total: int — 总违规次数
            - by_layer: Dict[str, int] — 按防线层级分组计数
            - today: int — 今日违规次数
            - recent: List[Dict] — 最近违规记录
        """
        total = len(self._violation_log)
        by_layer: Dict[str, int] = {}
        today_count = 0
        today_str = datetime.now(timezone.utc).isoformat()[:10]

        for v in self._violation_log:
            layer = v.get("layer", "unknown")
            by_layer[layer] = by_layer.get(layer, 0) + 1
            ts = v.get("timestamp", "")
            if ts.startswith(today_str):
                today_count += 1

        return {
            "total": total,
            "by_layer": by_layer,
            "today": today_count,
            "recent": self._violation_log[-5:],
        }
