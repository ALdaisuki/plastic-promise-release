"""Scheduler health meta-audit scanner — 6-dimension audit of the dispatch system itself.

Dimensions:
  1. Scanner SNR — source_scan reject rates, auto-throttle noisy scanners
  2. Agent timeout — hunter_failure_log timeout aggregation
  3. Dispatch latency — task_queue claim wait time
  4. Priority balance — priority distribution health
  5. Verification throughput — verification cadence
  6. Trend comparison — compare vs previous audit from memory pool
"""

import sqlite3
import os
import json
from datetime import datetime


async def scan_scheduler_health(engine) -> dict:
    """Audit the dispatch system itself across 6 dimensions.

    Returns dict with:
      - scanner: "scan_scheduler_health"
      - findings: total issue count
      - dispatched: number of tasks enqueued
      - auto_actions: list of auto-throttle actions taken
    """
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    audit_id = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    auto_actions = []
    dimensions = {}

    try:
        # ── Dimension 1: Scanner SNR ──────────────────────────────
        snr_rows = conn.execute("""
            SELECT source_scan,
                   COUNT(*) as total,
                   SUM(CASE WHEN verify_verdict='rejected' THEN 1 ELSE 0 END) as rejected,
                   ROUND(CAST(SUM(CASE WHEN verify_verdict='rejected' THEN 1 ELSE 0 END) AS REAL)
                         / MAX(COUNT(*), 1), 2) as reject_rate
            FROM task_queue
            WHERE created_at >= datetime('now', '-7 days')
              AND source_scan IS NOT NULL
              AND status IN ('verified', 'reassigned')
            GROUP BY source_scan
            ORDER BY reject_rate DESC
        """).fetchall()

        snr_top3 = []
        for row in snr_rows[:3]:
            entry = {
                "scanner": row["source_scan"],
                "reject_rate": row["reject_rate"],
                "total": row["total"],
                "rejected": row["rejected"],
                "level": "green",
            }
            if row["reject_rate"] > 0.50 and row["total"] >= 10:
                entry["level"] = "red"
                auto_actions.append({
                    "scanner": row["source_scan"],
                    "total": row["total"],
                    "rejected": row["rejected"],
                    "rate": row["reject_rate"],
                    "action": "throttle_double",
                })
            elif row["reject_rate"] >= 0.30:
                entry["level"] = "yellow"
            snr_top3.append(entry)

        dimensions["scanner_snr"] = {
            "top3": snr_top3,
            "auto_actions": [a["scanner"] for a in auto_actions],
        }

        # ── Dimension 2: Agent Timeout Rate ────────────────────────
        timeout_rows = conn.execute("""
            SELECT hf.agent_name as claimed_by,
                   COUNT(DISTINCT hf.task_id) as timeout_tasks,
                   ROUND(AVG(CAST(tq.escalation_count AS REAL)), 1) as avg_escalation
            FROM hunter_failure_log hf
            JOIN task_queue tq ON hf.task_id = tq.id
            WHERE hf.failure_type = 'timeout'
              AND hf.occurred_at >= datetime('now', '-7 days')
            GROUP BY hf.agent_name
            ORDER BY timeout_tasks DESC
        """).fetchall()

        timeout_top3 = []
        for row in timeout_rows[:3]:
            entry = {
                "agent": row["claimed_by"],
                "timeout_tasks": row["timeout_tasks"],
                "avg_escalation": row["avg_escalation"],
                "level": "green",
            }
            if row["timeout_tasks"] > 5:
                entry["level"] = "red"
            elif row["timeout_tasks"] >= 2:
                entry["level"] = "yellow"
            timeout_top3.append(entry)

        dimensions["agent_timeout"] = {"top3": timeout_top3}

        # ── Dimension 3: Dispatch Latency ──────────────────────────
        latency_rows = conn.execute("""
            SELECT task_type,
                   ROUND(AVG((julianday(claimed_at) - julianday(created_at)) * 86400), 0)
                       as avg_wait_seconds,
                   COUNT(*) as total
            FROM task_queue
            WHERE status IN ('claimed', 'executing', 'done', 'verified')
              AND claimed_at IS NOT NULL
              AND created_at >= datetime('now', '-7 days')
            GROUP BY task_type
            ORDER BY avg_wait_seconds DESC
        """).fetchall()

        latency_top3 = []
        for row in latency_rows[:3]:
            avg_wait = row["avg_wait_seconds"] or 0
            entry = {
                "task_type": row["task_type"],
                "avg_wait_seconds": avg_wait,
                "total": row["total"],
                "level": "green",
            }
            if avg_wait > 3600:
                entry["level"] = "red"
            elif avg_wait >= 600:
                entry["level"] = "yellow"
            latency_top3.append(entry)

        dimensions["dispatch_latency"] = {"top3": latency_top3}

        # ── Dimension 4: Priority Balance ──────────────────────────
        priority_rows = conn.execute("""
            SELECT priority,
                   COUNT(*) as total,
                   ROUND(CAST(COUNT(*) AS REAL) /
                     (SELECT COUNT(*) FROM task_queue
                      WHERE created_at >= datetime('now', '-7 days')), 2) as pct
            FROM task_queue
            WHERE created_at >= datetime('now', '-7 days')
            GROUP BY priority
            ORDER BY priority
        """).fetchall()

        distribution = {}
        for row in priority_rows:
            distribution[str(row["priority"])] = row["pct"]

        level = "green"
        p1_pct = distribution.get("1", 0)
        p4_pct = distribution.get("4", 0)
        if p1_pct > 0.50:
            level = "red"
        elif p4_pct > 0.80:
            level = "yellow"

        dimensions["priority_balance"] = {
            "distribution": distribution,
            "level": level,
        }

        # ── Dimension 5: Verification Throughput ───────────────────
        verify_rows = conn.execute("""
            SELECT verified_by,
                   COUNT(*) as verified_total,
                   COUNT(DISTINCT DATE(verified_at)) as active_days,
                   ROUND(CAST(COUNT(*) AS REAL)
                         / MAX(CAST(COUNT(DISTINCT DATE(verified_at)) AS REAL), 1), 1)
                       as avg_per_day
            FROM task_queue
            WHERE status = 'verified'
              AND verified_at >= datetime('now', '-7 days')
            GROUP BY verified_by
        """).fetchall()

        avg_per_day = 0.0
        active_days = 0
        if verify_rows:
            avg_per_day = verify_rows[0]["avg_per_day"]
            active_days = verify_rows[0]["active_days"]

        vp_level = "green"
        if avg_per_day > 20:
            vp_level = "yellow"
        elif active_days < 2 and verify_rows:
            vp_level = "yellow"

        dimensions["verification_throughput"] = {
            "avg_per_day": avg_per_day,
            "active_days": active_days,
            "level": vp_level,
        }

        # ── Dimension 6: Trend Comparison ──────────────────────────
        is_first_audit = False
        previous_audit_id = None
        improvements = []
        degradations = []
        follow_up_tasks = []

        prev_rows = conn.execute("""
            SELECT id, content, created_at FROM memories
            WHERE memory_type = 'experience'
              AND content LIKE '%"audit_id"%'
              AND content LIKE '%"scanner":"scan_scheduler_health"%'
              AND created_at >= datetime('now', '-14 days')
            ORDER BY created_at DESC
            LIMIT 1
        """).fetchall()

        if prev_rows:
            try:
                prev_content = json.loads(prev_rows[0]["content"]) if prev_rows[0]["content"] else {}
                if isinstance(prev_content, dict) and "audit_id" in prev_content:
                    previous_audit_id = prev_content["audit_id"]
                    prev_dims = prev_content.get("dimensions", {})

                    # Compare SNR
                    prev_snr = prev_dims.get("scanner_snr", {}).get("top3", [])
                    prev_snr_map = {s["scanner"]: s["reject_rate"] for s in prev_snr}
                    for s in snr_top3:
                        prev_rate = prev_snr_map.get(s["scanner"])
                        if prev_rate is not None:
                            delta = s["reject_rate"] - prev_rate
                            if delta < -0.1:
                                improvements.append(
                                    f"scanner_snr {s['scanner']}: {prev_rate}→{s['reject_rate']}"
                                )
                            elif delta > 0.1:
                                degradations.append(
                                    f"scanner_snr {s['scanner']}: {prev_rate}→{s['reject_rate']}"
                                )

                    # Compare timeout
                    prev_timeout = prev_dims.get("agent_timeout", {}).get("top3", [])
                    prev_timeout_map = {t["agent"]: t["timeout_tasks"] for t in prev_timeout}
                    for t in timeout_top3:
                        prev_count = prev_timeout_map.get(t["agent"], 0)
                        if t["timeout_tasks"] < prev_count:
                            improvements.append(
                                f"agent_timeout {t['agent']}: {prev_count}→{t['timeout_tasks']}"
                            )
                        elif t["timeout_tasks"] > prev_count:
                            degradations.append(
                                f"agent_timeout {t['agent']}: {prev_count}→{t['timeout_tasks']}"
                            )
                            if t["timeout_tasks"] > 5:
                                follow_up_tasks.append({
                                    "task_type": "review_agent_timeout",
                                    "agent": t["agent"],
                                    "reason": f"timeout increased {prev_count}→{t['timeout_tasks']}",
                                })

                    # Compare latency
                    prev_lat = prev_dims.get("dispatch_latency", {}).get("top3", [])
                    prev_lat_map = {l["task_type"]: l["avg_wait_seconds"] for l in prev_lat}
                    for l in latency_top3:
                        prev_wait = prev_lat_map.get(l["task_type"], 0)
                        if prev_wait > 0 and l["avg_wait_seconds"] > prev_wait * 2:
                            degradations.append(
                                f"dispatch_latency {l['task_type']}: {prev_wait}s→{l['avg_wait_seconds']}s"
                            )
                            follow_up_tasks.append({
                                "task_type": "review_dispatch_latency",
                                "for_task_type": l["task_type"],
                                "reason": f"latency 2x+ increase {prev_wait}s→{l['avg_wait_seconds']}s",
                            })
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        else:
            is_first_audit = True

        dimensions["trends"] = {
            "compared_to": previous_audit_id,
            "improvements": improvements,
            "degradations": degradations,
            "follow_up_tasks": follow_up_tasks,
        }

    finally:
        conn.close()

    # ── Build findings list and dispatch audit report ──
    findings = []
    total_issues = 0

    for s in snr_top3:
        if s["level"] in ("red", "yellow"):
            total_issues += 1
            findings.append({
                "dimension": "scanner_snr",
                "level": s["level"],
                "data": s,
            })
    for t in timeout_top3:
        if t["level"] in ("red", "yellow"):
            total_issues += 1
            findings.append({
                "dimension": "agent_timeout",
                "level": t["level"],
                "data": t,
            })
    for l in latency_top3:
        if l["level"] in ("red", "yellow"):
            total_issues += 1
            findings.append({
                "dimension": "dispatch_latency",
                "level": l["level"],
                "data": l,
            })
    if dimensions["priority_balance"]["level"] in ("red", "yellow"):
        total_issues += 1
        findings.append({
            "dimension": "priority_balance",
            "level": dimensions["priority_balance"]["level"],
            "data": dimensions["priority_balance"],
        })
    if dimensions["verification_throughput"]["level"] in ("red", "yellow"):
        total_issues += 1
        findings.append({
            "dimension": "verification_throughput",
            "level": dimensions["verification_throughput"]["level"],
            "data": dimensions["verification_throughput"],
        })
    if degradations:
        total_issues += 1
        findings.append({
            "dimension": "trends",
            "level": "yellow",
            "data": {"degradations": degradations},
        })

    # Build audit report payload
    report = {
        "audit_id": audit_id,
        "is_first_audit": is_first_audit,
        "previous_audit_id": previous_audit_id,
        "scanner": "scan_scheduler_health",
        "generated_at": datetime.now().isoformat(),
        "dimensions": dimensions,
        "auto_actions": auto_actions,
        "findings_summary": {
            "total_issues": total_issues,
            "auto_actions_count": len(auto_actions),
            "is_first_audit": is_first_audit,
        },
    }

    # Dispatch audit report to Claude via task_enqueue
    from plastic_promise.mcp.tools.task_queue import handle_task_enqueue

    dispatched = 0
    try:
        await handle_task_enqueue(engine, {
            "task_type": "audit_scheduler",
            "title": f"Scheduler Health Audit {audit_id}"
                     f"{' (first)' if is_first_audit else ''} — {total_issues} findings",
            "to_agent": "claude",
            "priority": 3,
            "source_scan": "scan_scheduler_health",
            "description": (
                f"6-dimension scheduler self-audit complete.\n"
                f"Findings: {total_issues} | Auto-actions: {len(auto_actions)}\n"
                f"SNR: {len(snr_top3)} scanners | "
                f"Timeout: {len(timeout_top3)} agents | "
                f"Latency: {len(latency_top3)} task types\n"
            ),
            "payload": report,
        })
        dispatched += 1
    except Exception:
        pass

    # Dispatch auto-throttle notification separately if needed
    if auto_actions:
        try:
            await handle_task_enqueue(engine, {
                "task_type": "notify_throttle_change",
                "title": f"Auto-throttle: {len(auto_actions)} scanner(s) throttled",
                "to_agent": "claude",
                "priority": 2,
                "source_scan": "scan_scheduler_health",
                "description": json.dumps(auto_actions, ensure_ascii=False, indent=2),
                "payload": {
                    "auto_actions": auto_actions,
                    "rollback": "domain(action='reset_throttle', scanner='<name>')",
                },
            })
            dispatched += 1
        except Exception:
            pass

    return {
        "scanner": "scan_scheduler_health",
        "findings": total_issues,
        "dispatched": dispatched,
        "auto_actions": auto_actions,
    }
