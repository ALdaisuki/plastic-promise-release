"""Memory pool health scanner — zombie memories, influx, distribution imbalance."""

import json
import os
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta

from plastic_promise.core.paths import get_db_path


def _load_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        tags = json.loads(raw)
    except Exception:
        return []
    return tags if isinstance(tags, list) else []


def _store_tags(conn: sqlite3.Connection, memory_id: str, tags: list[str]) -> None:
    conn.execute(
        "UPDATE memories SET tags = ? WHERE id = ?",
        (json.dumps(sorted(set(tags)), ensure_ascii=False), memory_id),
    )


def _worth(success: int | None, failure: int | None) -> float:
    ws = int(success or 0)
    wf = int(failure or 0)
    total = ws + wf
    return (ws + 1.0) / (total + 2.0) if total > 0 else 0.5


def _run_lifecycle_maintenance(conn: sqlite3.Connection) -> dict:
    """Mark lifecycle state transitions without hard-deleting memories."""
    lifecycle = {
        "stale_marked": 0,
        "conflicts_marked": 0,
        "forgotten_candidates": 0,
    }

    stale_rows = conn.execute(
        """
        SELECT id, tags, worth_success, worth_failure
        FROM memories
        WHERE decay_multiplier < 0.2
          AND COALESCE(worth_failure, 0) >= COALESCE(worth_success, 0)
          AND tags NOT LIKE '%status:forgotten%'
          AND tags NOT LIKE '%status:replaced%'
        LIMIT 50
        """
    ).fetchall()
    for row in stale_rows:
        tags = _load_tags(row["tags"])
        tags.extend(["status:forgotten", "decay:pending", "lifecycle:stale"])
        _store_tags(conn, row["id"], tags)
        conn.execute(
            """
            UPDATE memories
            SET importance = 0.0,
                activation_weight = 0.0,
                worth_success = 0,
                worth_failure = MAX(COALESCE(worth_failure, 0), 10),
                decay_multiplier = 0.0,
                last_accessed = ?
            WHERE id = ?
            """,
            (datetime.now().isoformat(), row["id"]),
        )
        lifecycle["stale_marked"] += 1

    duplicate_contents = conn.execute(
        """
        SELECT content
        FROM memories
        WHERE content IS NOT NULL
          AND TRIM(content) != ''
          AND tags NOT LIKE '%status:replaced%'
          AND tags NOT LIKE '%status:forgotten%'
        GROUP BY content
        HAVING COUNT(*) > 1
        LIMIT 20
        """
    ).fetchall()
    for duplicate in duplicate_contents:
        rows = conn.execute(
            """
            SELECT id, tags, worth_success, worth_failure, access_count, created_at
            FROM memories
            WHERE content = ?
              AND tags NOT LIKE '%status:replaced%'
              AND tags NOT LIKE '%status:forgotten%'
            """,
            (duplicate["content"],),
        ).fetchall()
        if len(rows) < 2:
            continue
        ranked = sorted(
            rows,
            key=lambda row: (
                _worth(row["worth_success"], row["worth_failure"]),
                int(row["access_count"] or 0),
                row["created_at"] or "",
            ),
            reverse=True,
        )
        survivor_id = ranked[0]["id"]
        for loser in ranked[1:]:
            tags = _load_tags(loser["tags"])
            tags.extend(["status:replaced", "lifecycle:conflict", f"replaced_by:{survivor_id}"])
            _store_tags(conn, loser["id"], tags)
            conn.execute(
                "UPDATE memories SET worth_failure = MAX(COALESCE(worth_failure, 0), 10) "
                "WHERE id = ?",
                (loser["id"],),
            )
            lifecycle["conflicts_marked"] += 1

    lifecycle["forgotten_candidates"] = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE tags LIKE '%status:forgotten%'"
    ).fetchone()[0]
    conn.commit()
    return lifecycle


async def scan_memory_decay(engine) -> dict:
    """Scan memory pool for decay signals:
    1. Zombie memories (L3 + 30d inactive)
    2. Memory influx (24h spike, dynamic threshold median+2σ)
    3. Domain imbalance (>60%, queried from memories table directly)
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    findings = []
    lifecycle = {"stale_marked": 0, "conflicts_marked": 0, "forgotten_candidates": 0}

    try:
        # 1. Zombie detection: L3 tier, 30+ days no access
        thirty_days = (datetime.now() - timedelta(days=30)).isoformat()
        zombies = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tier='L3' "
            "AND (last_accessed IS NULL OR last_accessed = '' OR last_accessed < ?)",
            (thirty_days,),
        ).fetchone()[0]

        if zombies > 5:
            findings.append(
                {
                    "type": "zombie_memories",
                    "count": zombies,
                    "task_type": "gc_cleanup",
                    "to_agent": "pi_fixer",
                    "priority": 4,
                    "title": f"僵尸记忆清理: {zombies} 条L3记忆超30天未访问",
                }
            )

        # 2. Memory influx: count new memories in last 24h
        yesterday = (datetime.now() - timedelta(hours=24)).isoformat()
        influx = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE created_at > ?", (yesterday,)
        ).fetchone()[0]

        # Dynamic threshold: median + 2*std of daily counts over 7 days
        daily_counts = conn.execute(
            "SELECT DATE(created_at) as d, COUNT(*) as cnt "
            "FROM memories WHERE created_at > datetime('now', '-7 days') "
            "GROUP BY d ORDER BY cnt"
        ).fetchall()
        if len(daily_counts) >= 3:
            counts = [r[1] for r in daily_counts]
            counts.sort()
            median = counts[len(counts) // 2]
            mean = sum(counts) / len(counts)
            variance = sum((c - mean) ** 2 for c in counts) / len(counts)
            std = variance**0.5
            threshold = median + 2 * std
            if influx > threshold and influx > 10:
                findings.append(
                    {
                        "type": "memory_influx",
                        "count": influx,
                        "threshold": round(threshold, 1),
                        "task_type": "investigate_memory_influx",
                        "to_agent": "claude",
                        "priority": 2,
                        "title": f"记忆涌入异常: 24h新增{influx}条 (阈值={threshold:.0f})",
                    }
                )

        # 3. Domain imbalance: query memories table directly (NOT domain_stats)
        domain_counts = conn.execute(
            "SELECT domain, COUNT(*) as cnt FROM memories "
            "WHERE domain IS NOT NULL AND domain != '' "
            "GROUP BY domain"
        ).fetchall()
        if domain_counts:
            total = sum(r[1] for r in domain_counts)
            for domain, cnt in domain_counts:
                ratio = cnt / total if total > 0 else 0
                if ratio > 0.6:
                    findings.append(
                        {
                            "type": "domain_imbalance",
                            "domain": domain,
                            "ratio": round(ratio, 2),
                            "task_type": "rebalance_domains",
                            "to_agent": "pi_builder",
                            "priority": 3,
                            "title": f"记忆分布失衡: {domain} 占比 {ratio:.0%}",
                        }
                    )
        # 4. Routine maintenance: decay recalculation + lifecycle evolution
        routine = {}
        if os.environ.get("PP_PERIODIC_MAINTENANCE", "1") == "1":
            rm = None
            try:
                from plastic_promise.memory.soul_memory import EvolveR, RecMem

                rm = RecMem()
                updated = rm.update_all_decay()
                routine["decay_updated"] = updated
                evolver = EvolveR(rm)
                evolve_report = evolver.evolve_cycle()
                routine["evolved"] = True
                routine["evolve_report"] = {
                    "promoted": evolve_report.get("promoted", 0),
                    "demoted": evolve_report.get("demoted", 0),
                    "decayed": evolve_report.get("decayed", 0),
                }
            except Exception as e:
                routine["error"] = str(e)
            finally:
                sqlite = getattr(getattr(rm, "_engine", None), "_sqlite", None)
                rm_conn = getattr(sqlite, "_conn", None)
                if rm_conn is not None:
                    with suppress(Exception):
                        rm_conn.close()

        # 4b. Lifecycle state transitions: stale/conflict marking only.
        lifecycle = _run_lifecycle_maintenance(conn)

        # 5. Decay anomaly detection: frequently accessed but heavily decayed
        anomalies = conn.execute(
            "SELECT id, access_count, decay_multiplier, worth_success, worth_failure "
            "FROM memories "
            "WHERE decay_multiplier < 0.2 AND access_count > 10 "
            "AND tier != 'L1' "
            "LIMIT 20"
        ).fetchall()
        for row in anomalies:
            findings.append(
                {
                    "type": "decay_anomaly",
                    "memory_id": row["id"],
                    "access_count": row["access_count"],
                    "decay_multiplier": round(row["decay_multiplier"], 3),
                    "task_type": "fix_memory",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                    "title": (
                        f"衰减异常: 记忆{row['id'][:8]} "
                        f"访问{row['access_count']}次但decay={row['decay_multiplier']:.3f}"
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
                    "task_type": f["task_type"],
                    "title": f["title"],
                    "to_agent": f["to_agent"],
                    "priority": f["priority"],
                    "source_scan": "scan_memory_decay",
                    "payload": f,
                },
            )
            dispatched += 1
        except Exception:
            pass

    return {
        "scanner": "scan_memory_decay",
        "findings": len(findings),
        "dispatched": dispatched,
        "lifecycle": lifecycle,
    }
