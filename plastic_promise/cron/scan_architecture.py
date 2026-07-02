"""Architecture health scanner — domain cycles, god modules, shotgun surgery."""

import sqlite3
import os
import json
from datetime import datetime, timedelta
from collections import defaultdict


def _compute_median_and_threshold(values: list[float]) -> tuple[float, float]:
    """Compute median and dynamic threshold (median + 2*std)."""
    if len(values) < 2:
        return (values[0] if values else 0.0, 0.0)
    sorted_vals = sorted(values)
    median = sorted_vals[len(sorted_vals) // 2]
    mean = sum(sorted_vals) / len(sorted_vals)
    variance = sum((v - mean) ** 2 for v in sorted_vals) / len(sorted_vals)
    std = variance**0.5
    threshold = median + 2 * std
    return (median, threshold)


# ── Tag blacklist for Shotgun Surgery ──────────────────────

def _get_tag_blacklist() -> set[str]:
    """Return the set of tags excluded from Shotgun Surgery detection.

    Built-in defaults cover system management tags and metadata
    classification tags that are expected to appear across domains.
    Extend via TAG_BLACKLIST_EXTRA env var (comma-separated).
    """
    builtin = {
        # 系统管理标签 — 跨模块出现是正常行为
        "task:done", "task:pending", "task:active", "task:accepted",
        "task:review", "task:reviewed",
        "branch:main", "status:replaced",
        "llm_pending:true", "llm_classified:true",
        "audit",
        # 元数据分类标签 — 跨域共现是预期行为
        "cat:project", "cat:event", "cat:decision",
        "cat:preference", "cat:fact", "cat:pattern", "cat:entity",
        # 系统来源标签 — 跨域分布是管道设计使然
        "source:file-sync", "source:auto_inject",
    }
    extra = os.environ.get("TAG_BLACKLIST_EXTRA", "")
    if extra:
        builtin.update(t.strip() for t in extra.split(",") if t.strip())
    return builtin


async def scan_architecture(engine) -> dict:
    """Scan architecture for structural signals:
    1. Domain cycles — domains that reference each other bidirectionally
    2. God modules — domains with disproportionate memory count (dynamic threshold)
    3. Shotgun surgery — tag clusters that appear in many domains simultaneously
    """
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    findings = []

    try:
        # 1. Domain cycles: detect bidirectional domain references in memory content
        #    We look at entity_ids (JSON array) for cross-domain references
        domain_counts = conn.execute(
            "SELECT domain, COUNT(*) as cnt FROM memories "
            "WHERE domain IS NOT NULL AND domain != '' "
            "GROUP BY domain"
        ).fetchall()

        if len(domain_counts) >= 2:
            domains = [r["domain"] for r in domain_counts]
            # Check entity_ids for cross-domain references
            cross_refs = defaultdict(set)
            for domain in domains:
                rows = conn.execute(
                    "SELECT entity_ids, content FROM memories "
                    "WHERE domain=? AND entity_ids IS NOT NULL AND entity_ids != ''",
                    (domain,),
                ).fetchall()
                for row in rows:
                    try:
                        entities = json.loads(row["entity_ids"]) if row["entity_ids"] else []
                    except (json.JSONDecodeError, TypeError):
                        # Fallback: try to extract domain names from content
                        entities = []
                        for d in domains:
                            if d != domain and d in (row["content"] or ""):
                                entities.append(d)

                    for entity in entities:
                        if isinstance(entity, str):
                            for d in domains:
                                if d != domain and d in entity:
                                    cross_refs[domain].add(d)

            # Bidirectional = cycle
            detected_cycles = set()
            for domain_a in cross_refs:
                for domain_b in cross_refs[domain_a]:
                    if domain_a in cross_refs.get(domain_b, set()):
                        pair = tuple(sorted([domain_a, domain_b]))
                        if pair not in detected_cycles:
                            detected_cycles.add(pair)
                            findings.append(
                                {
                                    "type": "domain_cycle",
                                    "domains": list(pair),
                                    "task_type": "decouple_domains",
                                    "to_agent": "pi_builder",
                                    "priority": 2,
                                    "title": f"域循环依赖: {pair[0]} <-> {pair[1]}",
                                }
                            )

        # 2. God modules: domains with disproportionate memory count
        domain_sizes = conn.execute(
            "SELECT domain, COUNT(*) as cnt, "
            "AVG(worth_success) as avg_worth, "
            "AVG(activation_weight) as avg_weight "
            "FROM memories "
            "WHERE domain IS NOT NULL AND domain != '' "
            "GROUP BY domain"
        ).fetchall()

        if len(domain_sizes) >= 3:
            counts = [r["cnt"] for r in domain_sizes]
            median, threshold = _compute_median_and_threshold(counts)

            for row in domain_sizes:
                if row["cnt"] > threshold and row["cnt"] > 5:
                    findings.append(
                        {
                            "type": "god_module",
                            "domain": row["domain"],
                            "count": row["cnt"],
                            "threshold": round(threshold, 1),
                            "avg_worth": round(row["avg_worth"] or 0, 2),
                            "task_type": "decouple_domains",
                            "to_agent": "pi_builder",
                            "priority": 3,
                            "title": (
                                f"God模块检测: {row['domain']} ({row['cnt']}条记忆, "
                                f"阈值={threshold:.0f})"
                            ),
                        }
                    )

        # 3. Shotgun surgery: tags that appear across many domains
        tag_domain_rows = conn.execute(
            "SELECT tags, domain FROM memories "
            "WHERE tags IS NOT NULL AND tags != '[]' AND tags != '' "
            "AND domain IS NOT NULL AND domain != ''"
        ).fetchall()

        if tag_domain_rows:
            tag_domains = defaultdict(set)
            for row in tag_domain_rows:
                try:
                    tag_list = json.loads(row["tags"])
                    if not isinstance(tag_list, list):
                        continue
                except (json.JSONDecodeError, TypeError):
                    continue
                for tag in tag_list:
                    if isinstance(tag, str):
                        tag_domains[tag].add(row["domain"])

            # Dynamic threshold on domain spread
            spread_counts = [len(doms) for doms in tag_domains.values()]
            if len(spread_counts) >= 3:
                median_spread, threshold_spread = _compute_median_and_threshold(spread_counts)
                blacklist = _get_tag_blacklist()
                for tag, domains_set in tag_domains.items():
                    if tag in blacklist:
                        continue
                    if len(domains_set) > threshold_spread and len(domains_set) >= 3:
                        findings.append(
                            {
                                "type": "shotgun_surgery",
                                "tag": tag,
                                "domain_count": len(domains_set),
                                "domains": sorted(domains_set),
                                "threshold": round(threshold_spread, 1),
                                "task_type": "decouple_domains",
                                "to_agent": "pi_builder",
                                "priority": 3,
                                "title": (
                                    f"Shotgun Surgery: tag '{tag}' 横跨{len(domains_set)}个域"
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
                    "source_scan": "scan_architecture",
                    "payload": f,
                },
            )
            dispatched += 1
        except Exception:
            pass

    return {"scanner": "scan_architecture", "findings": len(findings), "dispatched": dispatched}
