"""Memory pool health scanner — zombie memories, influx, distribution imbalance."""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta

from plastic_promise.core.paths import get_db_path
from plastic_promise.core.synthesis import ensure_synthesis_schema, synthesis_content_hash
from plastic_promise.core.synthesis_retrieval import (
    available_ordinary_memory_sql_predicate,
    ordinary_memory_sql_predicate,
)


def _worth(success: int | float | None, failure: int | float | None) -> float:
    ws = float(success or 0.0)
    wf = float(failure or 0.0)
    total = ws + wf
    return (ws + 1.0) / (total + 2.0) if total > 0 else 0.5


def _run_lifecycle_maintenance(conn: sqlite3.Connection, engine) -> dict:
    """Discover lifecycle candidates and delegate canonical state changes."""
    ensure_synthesis_schema(conn)
    conn.commit()
    ordinary_guard = ordinary_memory_sql_predicate("memories")
    available_guard = available_ordinary_memory_sql_predicate("memories")
    eligible_guard = " AND ".join(
        (
            "typeof(memories.id) = 'text' AND TRIM(memories.id) != ''",
            "typeof(memories.content) = 'text' AND TRIM(memories.content) != ''",
            "typeof(memories.project_id) = 'text' AND TRIM(memories.project_id) != ''",
            "typeof(memories.embedding_hash) = 'text' AND TRIM(memories.embedding_hash) != ''",
            "typeof(memories.created_at) = 'text' AND TRIM(memories.created_at) != ''",
            "typeof(memories.worth_success) IN ('integer', 'real') "
            "AND memories.worth_success >= 0 "
            "AND memories.worth_success < 1.0e308",
            "typeof(memories.worth_failure) IN ('integer', 'real') "
            "AND memories.worth_failure >= 0 "
            "AND memories.worth_failure < 1.0e308",
            "(memories.worth_success + memories.worth_failure) < 1.0e308",
            "typeof(memories.access_count) = 'integer' AND memories.access_count >= 0",
            "typeof(memories.tags) = 'text' AND json_valid(memories.tags) "
            "AND json_type(CASE WHEN json_valid(memories.tags) "
            "THEN memories.tags ELSE 'null' END) = 'array'",
            "typeof(memories.metadata_json) = 'text' "
            "AND json_valid(memories.metadata_json) "
            "AND json_type(CASE WHEN json_valid(memories.metadata_json) "
            "THEN memories.metadata_json ELSE 'null' END) = 'object'",
        )
    )
    lifecycle = {
        "stale_marked": 0,
        "conflicts_marked": 0,
        "forgotten_candidates": 0,
    }
    mutate = getattr(engine, "mutate_ordinary_source", None)

    def observed_precondition(row) -> dict:
        try:
            tags = json.loads(row["tags"])
            metadata = json.loads(row["metadata_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return {
            "access_count": row["access_count"],
            "content_hash": synthesis_content_hash(row["content"]),
            "created_at": row["created_at"],
            "decay_multiplier": row["decay_multiplier"],
            "embedding_hash": row["embedding_hash"],
            "metadata_json": metadata,
            "project_id": row["project_id"],
            "tags": tags,
            "worth_failure": row["worth_failure"],
            "worth_success": row["worth_success"],
        }

    def mark_forgotten(
        row,
        *,
        reason: str,
        transition: str,
        survivor=None,
    ) -> bool:
        if not callable(mutate):
            return False
        observed = observed_precondition(row)
        if not observed:
            return False
        peer_snapshots = {}
        if survivor is not None:
            peer = observed_precondition(survivor)
            if not peer:
                return False
            peer_snapshots[survivor["id"]] = peer
        try:
            mutate(
                row["id"],
                operation="forgotten",
                reason=reason,
                actor="scan_memory_decay",
                call_id=(f"internal:scan_memory_decay:lifecycle:{transition}:{uuid.uuid4().hex}"),
                expected_project_id=observed.pop("project_id"),
                expected_content_hash=observed.pop("content_hash"),
                expected_source_snapshot=observed,
                expected_peer_snapshots=peer_snapshots,
                require_source_available=True,
            )
        except Exception:
            return False
        return True

    stale_rows = conn.execute(
        f"""
        SELECT id, content, project_id, tags, metadata_json, worth_success,
               worth_failure, decay_multiplier, access_count, created_at,
               embedding_hash
        FROM memories
        WHERE decay_multiplier < 0.2
          AND COALESCE(worth_failure, 0) >= COALESCE(worth_success, 0)
          AND {eligible_guard}
          AND {available_guard}
        ORDER BY created_at, id
        LIMIT 50
        """
    ).fetchall()
    for row in stale_rows:
        if mark_forgotten(
            row,
            reason="lifecycle:stale",
            transition="stale",
        ):
            lifecycle["stale_marked"] += 1

    duplicate_contents = conn.execute(
        f"""
        SELECT content, project_id
        FROM memories
        WHERE {eligible_guard}
          AND {available_guard}
        GROUP BY project_id, content
        HAVING COUNT(*) > 1
        ORDER BY project_id, content
        LIMIT 20
        """
    ).fetchall()
    for duplicate in duplicate_contents:
        rows = conn.execute(
            f"""
            SELECT id, content, project_id, tags, metadata_json, worth_success,
                   worth_failure, decay_multiplier, access_count, created_at,
                   embedding_hash
            FROM memories
            WHERE content = ?
              AND project_id = ?
              AND {eligible_guard}
              AND {available_guard}
            """,
            (duplicate["content"], duplicate["project_id"]),
        ).fetchall()
        if len(rows) < 2:
            continue
        try:
            ranked = sorted(
                rows,
                key=lambda row: (
                    _worth(row["worth_success"], row["worth_failure"]),
                    int(row["access_count"]),
                    row["created_at"],
                    row["id"],
                ),
                reverse=True,
            )
        except (OverflowError, TypeError, ValueError):
            continue
        survivor_id = ranked[0]["id"]
        for loser in ranked[1:]:
            if mark_forgotten(
                loser,
                reason=f"lifecycle:duplicate_replacement:{survivor_id}",
                transition="duplicate",
                survivor=ranked[0],
            ):
                lifecycle["conflicts_marked"] += 1

    lifecycle["forgotten_candidates"] = conn.execute(
        f"SELECT COUNT(*) FROM memories WHERE tags LIKE '%status:forgotten%' AND {ordinary_guard}"
    ).fetchone()[0]
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
    ensure_synthesis_schema(conn)
    conn.commit()
    ordinary_guard = ordinary_memory_sql_predicate("memories")
    findings = []
    lifecycle = {"stale_marked": 0, "conflicts_marked": 0, "forgotten_candidates": 0}

    try:
        # 1. Zombie detection: L3 tier, 30+ days no access
        thirty_days = (datetime.now() - timedelta(days=30)).isoformat()
        zombies = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tier='L3' "
            "AND (last_accessed IS NULL OR last_accessed = '' OR last_accessed < ?) "
            f"AND {ordinary_guard}",
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
            f"SELECT COUNT(*) FROM memories WHERE created_at > ? AND {ordinary_guard}",
            (yesterday,),
        ).fetchone()[0]

        # Dynamic threshold: median + 2*std of daily counts over 7 days
        daily_counts = conn.execute(
            "SELECT DATE(created_at) as d, COUNT(*) as cnt "
            "FROM memories WHERE created_at > datetime('now', '-7 days') "
            f"AND {ordinary_guard} "
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
            f"AND {ordinary_guard} "
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

                # The scanner's engine owns the canonical SQLite snapshot.
                # Passing it through both makes periodic lifecycle work real
                # and keeps RecMem on the guarded Python mutation path.
                rm = RecMem(engine)
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
                # ``engine`` is owned by the scanner caller.  Do not close its
                # canonical SQLite connection after routine maintenance.
                pass

        # 4b. Lifecycle candidates: canonical unavailable transitions.
        lifecycle = _run_lifecycle_maintenance(conn, engine)

        # 5. Decay anomaly detection: frequently accessed but heavily decayed
        anomalies = conn.execute(
            "SELECT id, access_count, decay_multiplier, worth_success, worth_failure "
            "FROM memories "
            "WHERE decay_multiplier < 0.2 AND access_count > 10 "
            "AND tier != 'L1' "
            f"AND {ordinary_guard} "
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
