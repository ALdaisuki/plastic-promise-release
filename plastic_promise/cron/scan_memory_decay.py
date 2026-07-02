"""Memory pool health scanner — zombie memories, influx, distribution imbalance."""

import sqlite3
import os
from datetime import datetime, timedelta


async def scan_memory_decay(engine) -> dict:
    """Scan memory pool for decay signals:
    1. Zombie memories (L3 + 30d inactive)
    2. Memory influx (24h spike, dynamic threshold median+2σ)
    3. Domain imbalance (>60%, queried from memories table directly)
    """
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    findings = []

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

    return {"scanner": "scan_memory_decay", "findings": len(findings), "dispatched": dispatched}
