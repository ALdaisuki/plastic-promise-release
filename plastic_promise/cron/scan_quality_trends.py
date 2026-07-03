"""Quality trends scanner — fix recurrence, rejection rate, worth velocity."""

import os
import sqlite3
from datetime import datetime, timedelta


def _compute_median(values: list[float]) -> float:
    """Compute median of a list of values."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    return sorted_vals[len(sorted_vals) // 2]


async def scan_quality_trends(engine) -> dict:
    """Scan quality trends using metric_history and failure_log:
    1. Fix recurrence — same task_type rejected repeatedly within 14 days
    2. Rejection rate trend — rejection ratio increasing week-over-week
    3. Worth velocity — worth_score declining trend (uses metric_history)
    """
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    findings = []

    try:
        # 1. Fix recurrence: count task_types that got rejected 2+ times
        #    in the last 14 days (by the same agent)
        fourteen_days_ago = (datetime.now() - timedelta(days=14)).isoformat()
        recurrence_rows = conn.execute(
            "SELECT agent_name, task_type, COUNT(*) as reject_count "
            "FROM hunter_failure_log "
            "WHERE failure_type='rejected' AND occurred_at >= ? "
            "GROUP BY agent_name, task_type "
            "HAVING COUNT(*) >= 2",
            (fourteen_days_ago,),
        ).fetchall()

        for row in recurrence_rows:
            # Check if there were any successful fixes in between
            successes = conn.execute(
                "SELECT COUNT(*) FROM hunter_failure_log "
                "WHERE agent_name=? AND task_type=? AND failure_type!='rejected' "
                "AND occurred_at >= ?",
                (row["agent_name"], row["task_type"], fourteen_days_ago),
            ).fetchone()[0]

            findings.append(
                {
                    "type": "fix_recurrence",
                    "agent": row["agent_name"],
                    "task_type": row["task_type"],
                    "reject_count": row["reject_count"],
                    "intervening_successes": successes,
                    "to_agent": "claude",
                    "priority": 2,
                    "task_type_field": "investigate_recurrence",
                    "title": (
                        f"修复反复打回: {row['agent_name']} 在"
                        f"{row['task_type']}上被连续打回{row['reject_count']}次"
                    ),
                }
            )

        # 2. Rejection rate trend: compare last 7d vs previous 7d
        now = datetime.now()
        last_7d_start = (now - timedelta(days=7)).isoformat()
        prev_7d_start = (now - timedelta(days=14)).isoformat()
        prev_7d_end = (now - timedelta(days=7)).isoformat()

        last_7d_total = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log WHERE occurred_at >= ?", (last_7d_start,)
        ).fetchone()[0]

        last_7d_rejected = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log "
            "WHERE failure_type='rejected' AND occurred_at >= ?",
            (last_7d_start,),
        ).fetchone()[0]

        prev_7d_total = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log WHERE occurred_at >= ? AND occurred_at < ?",
            (prev_7d_start, prev_7d_end),
        ).fetchone()[0]

        prev_7d_rejected = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log "
            "WHERE failure_type='rejected' AND occurred_at >= ? AND occurred_at < ?",
            (prev_7d_start, prev_7d_end),
        ).fetchone()[0]

        if prev_7d_total > 0:
            prev_rate = prev_7d_rejected / prev_7d_total
        else:
            prev_rate = 0.0
        if last_7d_total > 0:
            last_rate = last_7d_rejected / last_7d_total
        else:
            last_rate = 0.0

        # Signal if rate increased by >50% AND at least 0.1 absolute increase
        if prev_rate > 0 and last_rate > 0:
            relative_change = (last_rate - prev_rate) / prev_rate
            absolute_change = last_rate - prev_rate
            if relative_change > 0.5 and absolute_change > 0.1:
                findings.append(
                    {
                        "type": "rejection_rate_spike",
                        "prev_rate": round(prev_rate, 3),
                        "last_rate": round(last_rate, 3),
                        "relative_change": round(relative_change, 2),
                        "to_agent": "pi_reviewer",
                        "priority": 2,
                        "task_type_field": "investigate_quality",
                        "title": (
                            f"打回率激增: {prev_rate:.0%}->{last_rate:.0%} (+{relative_change:.0%})"
                        ),
                    }
                )

        # 3. Worth velocity: check metric_history for worth_score trends
        #    worth_score declining over 7+ day window
        worth_rows = conn.execute(
            "SELECT metric_value, computed_at FROM metric_history "
            "WHERE metric_name = 'worth_score' "
            "AND computed_at >= ? "
            "ORDER BY computed_at ASC",
            ((now - timedelta(days=14)).isoformat(),),
        ).fetchall()

        if len(worth_rows) >= 5:
            # Split into first half and second half
            mid = len(worth_rows) // 2
            first_half = [r["metric_value"] for r in worth_rows[:mid]]
            second_half = [r["metric_value"] for r in worth_rows[mid:]]

            first_avg = sum(first_half) / len(first_half)
            second_avg = sum(second_half) / len(second_half)

            if first_avg > 0:
                decline_ratio = (first_avg - second_avg) / first_avg
                # Signal if worth declined >15%
                if decline_ratio > 0.15:
                    findings.append(
                        {
                            "type": "worth_decline",
                            "first_half_avg": round(first_avg, 3),
                            "second_half_avg": round(second_avg, 3),
                            "decline_ratio": round(decline_ratio, 2),
                            "data_points": len(worth_rows),
                            "to_agent": "pi_reviewer",
                            "priority": 3,
                            "task_type_field": "investigate_quality",
                            "title": (
                                f"Worth评分下降: {first_avg:.3f}->{second_avg:.3f} "
                                f"({decline_ratio:.0%})"
                            ),
                        }
                    )

        # 3b. Per-agent worth velocity from metric_history (if available)
        agent_worth_rows = conn.execute(
            "SELECT metric_value, computed_at FROM metric_history "
            "WHERE metric_name LIKE 'worth_score_%' "
            "AND computed_at >= ? "
            "ORDER BY computed_at ASC",
            ((now - timedelta(days=14)).isoformat(),),
        ).fetchall()

        # Group by metric_name (agent)
        from collections import defaultdict

        agent_groups = defaultdict(list)
        for row in agent_worth_rows:
            # Extract agent name from 'worth_score_<agent>'
            metric_parts = row["metric_name"].split("_", 2)
            if len(metric_parts) >= 3:
                agent_name = metric_parts[2]
                agent_groups[agent_name].append(row["metric_value"])

        for agent_name, values in agent_groups.items():
            if len(values) >= 5:
                mid = len(values) // 2
                first_avg = sum(values[:mid]) / len(values[:mid])
                second_avg = sum(values[mid:]) / len(values[mid:])
                if first_avg > 0:
                    decline = (first_avg - second_avg) / first_avg
                    if decline > 0.15:
                        findings.append(
                            {
                                "type": "agent_worth_decline",
                                "agent": agent_name,
                                "first_avg": round(first_avg, 3),
                                "second_avg": round(second_avg, 3),
                                "decline_ratio": round(decline, 2),
                                "to_agent": "claude",
                                "priority": 3,
                                "task_type_field": "investigate_quality",
                                "title": (
                                    f"{agent_name} worth下降: "
                                    f"{first_avg:.3f}->{second_avg:.3f} ({decline:.0%})"
                                ),
                            }
                        )
    finally:
        conn.close()

    # Dispatch findings
    from plastic_promise.mcp.tools.task_queue import handle_task_enqueue

    dispatched = 0
    for f in findings:
        try:
            await handle_task_enqueue(
                engine,
                {
                    "task_type": f["task_type_field"],
                    "title": f["title"],
                    "to_agent": f["to_agent"],
                    "priority": f["priority"],
                    "source_scan": "scan_quality_trends",
                    "payload": f,
                },
            )
            dispatched += 1
        except Exception:
            pass

    return {
        "scanner": "scan_quality_trends",
        "findings": len(findings),
        "dispatched": dispatched,
    }
