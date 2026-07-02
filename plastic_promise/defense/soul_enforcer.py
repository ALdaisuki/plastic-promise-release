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
    """信任分管理器 — 数字内分泌系统的核心引擎，支持多 Agent。

    管理信任分的增长、衰减、历史追溯，并根据当前信任分推导
    信任等级 (tier) 和自主权级别 (autonomy_level)。

    target="" 表示默认 Agent (Claude 自己)。target="pi_builder" 等
    为多 Agent 场景的独立信任分追踪。

    If *trust_store* is provided, all read/write operations are delegated
    to the SQLite-backed TrustStore for persistence across restarts.
    Without it, falls back to in-memory only (legacy / test mode).

    Attributes:
        _trusts: Dict[str, float] — agent_id → trust (0.0 ~ 1.0)
        _history: 信任分变更记录
        _store: Optional TrustStore for persistence
    """

    def __init__(self, initial_trust: float = TRUST_INITIAL, trust_store: Any = None) -> None:
        self._trusts: Dict[str, float] = {}
        self._history: List[Dict[str, Any]] = []
        self._store = trust_store  # None = in-memory only (legacy)

    def _trust(self, target: str = "") -> float:
        if self._store:
            return self._store.get(target)["trust"]
        return self._trusts.get(target, TRUST_INITIAL)

    def _set_trust(self, target: str, value: float):
        self._trusts[target] = max(TRUST_MIN, min(TRUST_MAX, value))

    def boost(self, delta: float, reason: str = "", *, target: str) -> float:
        if delta < 0:
            raise ValueError(f"boost delta must be non-negative, got {delta}")
        old = self._trust(target)
        new = min(TRUST_MAX, old + delta)
        self._set_trust(target, new)
        self._history.append(
            {
                "delta": delta,
                "reason": reason,
                "target": target,
                "old_value": old,
                "new_value": new,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "direction": "boost",
            }
        )
        if self._store:
            self._store.save(target, new, self.tier(target), self.autonomy_level(target))
            self._store.log_history(target, delta, reason, old, new, "boost")
        return new

    def decay(self, delta: float = TRUST_DECAY_RATE, reason: str = "", *, target: str) -> float:
        if delta < 0:
            raise ValueError(f"decay delta must be non-negative, got {delta}")
        old = self._trust(target)
        new = max(TRUST_MIN, old - delta)
        self._set_trust(target, new)
        self._history.append(
            {
                "delta": -delta,
                "reason": reason,
                "target": target,
                "old_value": old,
                "new_value": new,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "direction": "decay",
            }
        )
        if self._store:
            self._store.save(target, new, self.tier(target), self.autonomy_level(target))
            self._store.log_history(target, -delta, reason, old, new, "decay")
        return new

    def adjust(self, delta: float, reason: str = "", *, target: str = "") -> float:
        if delta >= 0:
            return self.boost(delta, reason, target=target)
        return self.decay(-delta, reason, target=target)

    def get(self, target: str = "") -> float:
        if self._store:
            return self._store.get(target)["trust"]
        return self._trusts.get(target, TRUST_INITIAL)

    def history(self, target: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        if self._store:
            return self._store.history(target, limit)
        if not self._history:
            return []
        return self._history[-limit:]

    def tier(self, target: str = "") -> str:
        t = self._trust(target)
        if t >= 0.80:
            return "high"
        elif t >= 0.50:
            return "medium"
        elif t >= 0.30:
            return "low"
        return "critical"

    def autonomy_level(self, target: str = "") -> str:
        _t = self.tier(target)
        if _t == "high":
            return "full"
        elif _t == "medium":
            return "standard"
        elif _t == "low":
            return "restricted"
        return "minimal"

    def get_retrieval_boost(self) -> float:
        """Return retrieval weight multiplier based on current trust tier.

        High trust → broader context, more risk tolerance.
        Low trust → narrower scope, conservative retrieval.
        Serves 实践层: 动态信任调节信息获取范围。
        """
        _tier = self.tier()
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

    def __init__(self, trust_manager: Optional[TrustManager] = None, target: str = "claude") -> None:
        """初始化 SoulEnforcer。

        Args:
            trust_manager: 信任分管理器实例。若为 None 则自动创建默认实例。
            target: 信任分追踪目标 (claude/pi_builder/pi_reviewer 等)。
        """
        self.trust_manager: TrustManager = trust_manager or TrustManager()
        self._default_target: str = target
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
            r"\brm\s+-rf\b",
            r"\bDROP\s+TABLE\b",
            r"\bDROP\s+DATABASE\b",
            r"\bformat\s+[CFD]:",
            r"\bshutdown\b",
            r"\bdel\s+/f\b",
            r"\bDEL\s+/F\b",
            r"\bdd\s+if=",
            r"\bmkfs\.",
            r"\bchmod\s+777\b",
        ]

        description_lower = action_description.lower()
        trust = self.trust_manager.get(target=self._default_target)

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
                self._violation_log.append(
                    {
                        "action": action_description,
                        "layer": "L0",
                        "reason": f"Dangerous pattern match: {pattern}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                # Violation-driven decay: L0 violation → -0.05
                if self.trust_manager:
                    try:
                        self.trust_manager.decay(0.05, f"L0 violation: {pattern}", target=self._default_target)
                    except Exception:
                        pass
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
            self._violation_log.append(
                {
                    "action": action_description,
                    "layer": "L1",
                    "reason": f"Trust too low: {trust:.2f} < 0.15",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            # Violation-driven decay: L1 critical trust → -0.02
            if self.trust_manager:
                try:
                    self.trust_manager.decay(0.02, f"L1 critical trust: {trust:.2f}", target=self._default_target)
                except Exception:
                    pass
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
        trust = self.trust_manager.get(target=self._default_target)
        tier = self.trust_manager.tier(target=self._default_target)

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
        self._violation_log.append(
            {
                "action": action,
                "layer": layer,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

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
