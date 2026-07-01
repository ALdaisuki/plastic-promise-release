"""Trust health scanner — rapid drops, stagnant trust, trust erosion detection."""

import sqlite3
import os
from datetime import datetime, timedelta


async def scan_trust(engine) -> dict:
    """Scan trust scores for health signals:
    1. Rapid trust drops — any agent losing >0.15 in 24 hours
    2. Stagnant trust — trust unchanged (<0.01 movement) for 14+ days
    3. Trust tier demotions — agents falling below rank thresholds
    """
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    findings = []

    try:
        now = datetime.now()

        # 1. Rapid trust drops: check trust_history for drops >0.15 in last 24h
        twenty_four_hours_ago = (now - timedelta(hours=24)).isoformat()
        rapid_drops = conn.execute(
            "SELECT target, delta, old_value, new_value, reason, timestamp "
            "FROM trust_history "
            "WHERE timestamp >= ? AND delta <= -0.15 "
            "ORDER BY delta ASC",
            (twenty_four_hours_ago,)
        ).fetchall()

        agent_drops = {}
        for row in rapid_drops:
            if row["target"] not in agent_drops:
                agent_drops[row["target"]] = {
                    "total_drop": 0,
                    "events": [],
                    "old_value": row["old_value"],
                    "new_value": row["new_value"],
                }
            agent_drops[row["target"]]["total_drop"] += abs(row["delta"])
            agent_drops[row["target"]]["events"].append({
                "delta": round(row["delta"], 3),
                "reason": row["reason"][:100],
                "timestamp": row["timestamp"],
            })
            # Track the most recent new_value
            agent_drops[row["target"]]["new_value"] = row["new_value"]

        for agent, info in agent_drops.items():
            if info["total_drop"] > 0.15:
                findings.append({
                    "type": "rapid_trust_drop",
                    "agent": agent,
                    "total_drop": round(info["total_drop"], 3),
                    "current_trust": round(info["new_value"], 3),
                    "events_24h": len(info["events"]),
                    "to_agent": "claude",
                    "priority": 1,
                    "task_type_field": "investigate_trust",
                    "title": (
                        f"信任急剧下降: {agent} 24h跌{info['total_drop']:.2f} "
                        f"({info['old_value']:.2f}->{info['new_value']:.2f})"
                    ),
                })

        # 2. Stagnant trust: agents with <0.01 movement in 14 days
        fourteen_days_ago = (now - timedelta(days=14)).isoformat()

        # Get all targets
        targets = conn.execute(
            "SELECT DISTINCT target FROM trust_scores"
        ).fetchall()

        for row in targets:
            target = row["target"]
            # Get change over 14 days
            changes = conn.execute(
                "SELECT MIN(new_value) as min_val, MAX(new_value) as max_val "
                "FROM trust_history "
                "WHERE target=? AND timestamp >= ?",
                (target, fourteen_days_ago)
            ).fetchone()

            if changes and changes["min_val"] is not None:
                # Require at least 2 data points over 7+ days for meaningful
                # stagnation signal; a single recent entry is not stagnant
                history_count = conn.execute(
                    "SELECT COUNT(*) FROM trust_history "
                    "WHERE target=? AND timestamp >= ?",
                    (target, fourteen_days_ago)
                ).fetchone()[0]

                movement = changes["max_val"] - changes["min_val"]
                if history_count >= 2 and movement < 0.01:
                    # Get current trust score
                    current = conn.execute(
                        "SELECT trust, last_updated FROM trust_scores WHERE target=?",
                        (target,)
                    ).fetchone()

                    if current:
                        findings.append({
                            "type": "stagnant_trust",
                            "agent": target,
                            "current_trust": round(current["trust"], 3),
                            "movement_14d": round(movement, 4),
                            "last_updated": current["last_updated"] or "unknown",
                            "to_agent": "claude",
                            "priority": 3,
                            "task_type_field": "investigate_trust",
                            "title": (
                                f"信任停滞: {target} 14天波动<0.01 "
                                f"(当前{current['trust']:.2f})"
                            ),
                        })
            else:
                # No history at all in last 14 days → also stagnant
                current = conn.execute(
                    "SELECT trust, last_updated FROM trust_scores WHERE target=?",
                    (target,)
                ).fetchone()
                if current and current["trust"] is not None:
                    # Check if score was set >14d ago
                    try:
                        updated = datetime.fromisoformat(
                            current["last_updated"]
                            .replace("Z", "+00:00")
                            .split("+")[0]
                            .split(".")[0]
                        )
                        if (now - updated).days >= 14:
                            findings.append({
                                "type": "stagnant_trust",
                                "agent": target,
                                "current_trust": round(current["trust"], 3),
                                "movement_14d": 0.0,
                                "last_updated": current["last_updated"] or "unknown",
                                "to_agent": "claude",
                                "priority": 3,
                                "task_type_field": "investigate_trust",
                                "title": (
                                    f"信任冻结: {target} 14天无任何变动 "
                                    f"(当前{current['trust']:.2f})"
                                ),
                            })
                    except (ValueError, TypeError):
                        pass

        # 3. Trust tier demotion risk: agents approaching lower tier boundaries
        from plastic_promise.core.constants import RANK_THRESHOLDS

        all_scores = conn.execute(
            "SELECT target, trust FROM trust_scores"
        ).fetchall()

        for row in all_scores:
            trust = row["trust"]
            target = row["target"]
            # Check if just above a tier boundary (within 0.03 margin)
            for rank, threshold in sorted(RANK_THRESHOLDS.items(), key=lambda x: x[1], reverse=True):
                margin = trust - threshold
                if 0 < margin <= 0.03:
                    # Check if trending downward
                    recent_deltas = conn.execute(
                        "SELECT SUM(delta) as total_delta FROM trust_history "
                        "WHERE target=? AND timestamp >= ?",
                        (target, (now - timedelta(days=7)).isoformat())
                    ).fetchone()

                    if recent_deltas and recent_deltas["total_delta"] is not None and recent_deltas["total_delta"] < 0:
                        findings.append({
                            "type": "tier_demotion_risk",
                            "agent": target,
                            "current_trust": round(trust, 3),
                            "tier_at_risk": rank,
                            "threshold": threshold,
                            "margin": round(margin, 3),
                            "trend_7d": round(recent_deltas["total_delta"], 3),
                            "to_agent": "claude",
                            "priority": 2,
                            "task_type_field": "investigate_trust",
                            "title": (
                                f"降级风险: {target} 距{rank}级仅差{margin:.2f} "
                                f"(趋势{recent_deltas['total_delta']:+.2f})"
                            ),
                        })
                    break  # Only report the highest tier at risk
    finally:
        conn.close()

    # Dispatch findings
    from plastic_promise.mcp.tools.task_queue import handle_task_enqueue
    dispatched = 0
    for f in findings:
        try:
            await handle_task_enqueue(engine, {
                "task_type": f["task_type_field"],
                "title": f["title"],
                "to_agent": f["to_agent"],
                "priority": f["priority"],
                "source_scan": "scan_trust",
                "payload": f,
            })
            dispatched += 1
        except Exception:
            pass

    return {"scanner": "scan_trust", "findings": len(findings), "dispatched": dispatched}
