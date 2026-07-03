"""Hunter Penalty Engine — failure consequence system."""

import logging
import os
import sqlite3

from plastic_promise.core.paths import get_db_path

logger = logging.getLogger(__name__)

PENALTY_RULES = {
    "timeout": {
        "base_penalty": -0.01,
        "repeat_threshold": 3,
        "repeat_penalty": -0.03,
        "repeat_action": "trust_review",
        "description": "心跳超时，委托释放回委托板",
    },
    "rejected": {
        "base_penalty": -0.03,
        "repeat_threshold": 999,
        "same_type_threshold": 3,
        "same_type_penalty": -0.05,
        "same_type_action": "ban_type_7d",
        "description": "长老验收不通过，委托被打回",
    },
    "abandoned": {
        "base_penalty": -0.02,
        "repeat_threshold": 5,
        "repeat_penalty": -0.05,
        "repeat_action": "demote_to_D",
        "description": "主动放弃委托",
    },
    "overreach": {
        "base_penalty": -0.04,
        "repeat_threshold": 1,
        "repeat_penalty": 0,
        "repeat_action": "lock_rank_30d",
        "description": "越级揭榜后失败",
    },
}


class HunterPenaltyEngine:
    """Compute and apply penalties for task failures."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = get_db_path()
        self._db_path = os.path.abspath(db_path)

    def compute_penalty(
        self, agent_name: str, failure_type: str, repeat_count: int, same_type_count: int = 0
    ) -> dict:
        """Compute penalty without applying it. Pure function for testability."""
        rule = PENALTY_RULES[failure_type]
        result = {
            "failure_type": failure_type,
            "base_penalty": rule["base_penalty"],
            "repeat_count": repeat_count,
            "upgrade_triggered": False,
            "upgrade_penalty": 0,
            "action": None,
        }

        if repeat_count >= rule["repeat_threshold"]:
            result["upgrade_triggered"] = True
            result["upgrade_penalty"] = rule["repeat_penalty"]
            result["action"] = rule["repeat_action"]

        if failure_type == "rejected" and same_type_count >= rule["same_type_threshold"]:
            result["action"] = rule["same_type_action"]

        return result

    def count_failures(self, agent_name: str, failure_type: str, window_days: int = 30) -> int:
        """Count failures of a given type in the time window."""
        conn = sqlite3.connect(self._db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log "
            "WHERE agent_name=? AND failure_type=? "
            "AND occurred_at >= datetime('now', ?)",
            (agent_name, failure_type, f"-{window_days} days"),
        ).fetchone()[0]
        conn.close()
        return count

    def count_same_type_failures(
        self, agent_name: str, task_type: str, window_days: int = 30
    ) -> int:
        """Count rejected failures for the same task_type."""
        conn = sqlite3.connect(self._db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log "
            "WHERE agent_name=? AND failure_type='rejected' AND task_type=? "
            "AND occurred_at >= datetime('now', ?)",
            (agent_name, task_type, f"-{window_days} days"),
        ).fetchone()[0]
        conn.close()
        return count

    async def apply_penalty(
        self, agent_name: str, task_id: str, task_type: str, failure_type: str, current_trust: float
    ) -> dict:
        """Apply penalty: log + trust adjust + check upgrades."""
        repeat_count = self.count_failures(agent_name, failure_type) + 1
        same_type_count = self.count_same_type_failures(agent_name, task_type)
        penalty = self.compute_penalty(agent_name, failure_type, repeat_count, same_type_count)

        new_trust = current_trust + penalty["base_penalty"]
        if penalty["upgrade_triggered"]:
            new_trust += penalty["upgrade_penalty"]

        # Log
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT INTO hunter_failure_log "
            "(agent_name, task_id, task_type, failure_type, trust_before, trust_after, penalty_applied) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                agent_name,
                task_id,
                task_type,
                failure_type,
                current_trust,
                new_trust,
                penalty["base_penalty"] + penalty["upgrade_penalty"],
            ),
        )
        conn.commit()
        conn.close()

        # Apply trust adjustment
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager

            tm = TrustManager()
            tm.decay(abs(penalty["base_penalty"]), f"{failure_type}: {task_id}", target=agent_name)
            if penalty["upgrade_triggered"]:
                tm.decay(
                    abs(penalty["upgrade_penalty"]),
                    f"{failure_type}_upgrade (x{repeat_count}): {task_id}",
                    target=agent_name,
                )
        except Exception as e:
            logger.warning("Trust adjust failed: %s", e)

        return {
            "penalty_applied": penalty["base_penalty"] + penalty["upgrade_penalty"],
            "trust_before": current_trust,
            "trust_after": new_trust,
            "repeat_count": repeat_count,
            "actions_triggered": [penalty["action"]] if penalty["action"] else [],
        }
