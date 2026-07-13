"""Coupling health scanner — tag anomalies, bridge nodes, implicit dependencies."""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations

from plastic_promise.core.paths import get_db_path
from plastic_promise.core.synthesis import ensure_synthesis_schema
from plastic_promise.core.synthesis_retrieval import ordinary_memory_sql_predicate


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


async def scan_coupling(engine) -> dict:
    """Scan coupling patterns for hidden structural issues:
    1. Tag co-occurrence anomalies — unusual tag pair frequencies
    2. Bridge node growth — entities connecting many otherwise-separate clusters
    3. Implicit dependencies — domains that grow together without explicit links
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_synthesis_schema(conn)
    conn.commit()
    ordinary_guard = ordinary_memory_sql_predicate("memories")
    findings = []

    try:
        # 1. Tag co-occurrence anomalies: find tag pairs that co-occur
        #    significantly more often than expected by chance
        tag_memory_rows = conn.execute(
            "SELECT id, tags FROM memories "
            "WHERE tags IS NOT NULL AND tags != '[]' AND tags != '' "
            f"AND {ordinary_guard}"
        ).fetchall()

        if len(tag_memory_rows) >= 10:
            # Count individual tag frequencies and co-occurrence frequencies
            tag_freq = defaultdict(int)
            cooccur_freq = defaultdict(int)
            total_memories = 0

            for row in tag_memory_rows:
                try:
                    tag_list = json.loads(row["tags"])
                    if not isinstance(tag_list, list) or len(tag_list) < 1:
                        continue
                except (json.JSONDecodeError, TypeError):
                    continue

                total_memories += 1
                for tag in tag_list:
                    if isinstance(tag, str):
                        tag_freq[tag] += 1

                # Count all pairs
                valid_tags = sorted({tag for tag in tag_list if isinstance(tag, str)})
                for t1, t2 in combinations(valid_tags, 2):
                    pair = tuple(sorted([t1, t2]))
                    cooccur_freq[pair] += 1

            # Expected co-occurrence = P(t1) * P(t2) * N
            # Anomaly if actual >> expected
            anomalies = []
            for (t1, t2), actual in cooccur_freq.items():
                if tag_freq[t1] < 3 or tag_freq[t2] < 3 or actual < 3:
                    continue
                expected = (
                    (tag_freq[t1] / total_memories)
                    * (tag_freq[t2] / total_memories)
                    * total_memories
                )
                if expected > 0:
                    ratio = actual / expected
                    anomalies.append((t1, t2, actual, expected, ratio))

            if len(anomalies) >= 3:
                ratios = [a[4] for a in anomalies]
                median_ratio, threshold_ratio = _compute_median_and_threshold(ratios)

                for t1, t2, actual, expected, ratio in anomalies:
                    if ratio > threshold_ratio and ratio > 3.0:
                        findings.append(
                            {
                                "type": "tag_cooccurrence_anomaly",
                                "tags": [t1, t2],
                                "actual": actual,
                                "expected": round(expected, 1),
                                "ratio": round(ratio, 1),
                                "to_agent": "pi_reviewer",
                                "priority": 3,
                                "task_type_field": "investigate_coupling",
                                "title": (
                                    f"Tag异常共现: [{t1}]+[{t2}] 实际{actual}次 "
                                    f"(期望{expected:.1f}, x{ratio:.1f})"
                                ),
                            }
                        )

        # 2. Bridge node growth: entities (from entity_ids) that appear in
        #    memories across many different domains
        entity_domain_rows = conn.execute(
            "SELECT entity_ids, domain FROM memories "
            "WHERE entity_ids IS NOT NULL AND entity_ids != '' "
            "AND domain IS NOT NULL AND domain != '' "
            f"AND {ordinary_guard}"
        ).fetchall()

        if entity_domain_rows:
            entity_domains = defaultdict(set)
            for row in entity_domain_rows:
                try:
                    entities = json.loads(row["entity_ids"])
                    if not isinstance(entities, list):
                        continue
                except (json.JSONDecodeError, TypeError):
                    continue
                for entity in entities:
                    if isinstance(entity, str) and entity:
                        entity_domains[entity].add(row["domain"])

            domain_spans = {
                entity: len(domains)
                for entity, domains in entity_domains.items()
                if len(domains) >= 3
            }

            if len(domain_spans) >= 3:
                spans = list(domain_spans.values())
                median_span, threshold_span = _compute_median_and_threshold(spans)

                for entity, span in sorted(domain_spans.items(), key=lambda x: x[1], reverse=True):
                    if span > threshold_span:
                        findings.append(
                            {
                                "type": "bridge_node",
                                "entity": entity,
                                "domain_span": span,
                                "domains": sorted(entity_domains[entity]),
                                "threshold": round(threshold_span, 1),
                                "to_agent": "pi_reviewer",
                                "priority": 3,
                                "task_type_field": "investigate_coupling",
                                "title": (
                                    f"Bridge节点: '{entity}' 连接{span}个域"
                                    f"(阈值={threshold_span:.1f})"
                                ),
                            }
                        )

        # 3. Implicit dependencies: domains whose memory counts grow in
        #    lockstep over time, suggesting hidden coupling
        fourteen_days_ago = (datetime.now() - timedelta(days=14)).isoformat()
        domain_timeline = conn.execute(
            "SELECT DATE(created_at) as d, domain, COUNT(*) as cnt "
            "FROM memories "
            "WHERE created_at >= ? "
            "AND domain IS NOT NULL AND domain != '' "
            f"AND {ordinary_guard} "
            "GROUP BY d, domain "
            "ORDER BY d",
            (fourteen_days_ago,),
        ).fetchall()

        if len(domain_timeline) >= 10:
            # Build per-domain time series
            domain_series = defaultdict(lambda: defaultdict(int))
            dates_set = set()
            for row in domain_timeline:
                dates_set.add(row["d"])
                domain_series[row["domain"]][row["d"]] = row["cnt"]

            dates = sorted(dates_set)
            if len(dates) >= 5 and len(domain_series) >= 2:
                # Compute correlation between domain growth series
                domain_names = list(domain_series.keys())
                correlated_pairs = []
                for i in range(len(domain_names)):
                    for j in range(i + 1, len(domain_names)):
                        d1, d2 = domain_names[i], domain_names[j]
                        series1 = [domain_series[d1].get(d, 0) for d in dates]
                        series2 = [domain_series[d2].get(d, 0) for d in dates]

                        # Pearson correlation
                        n = len(series1)
                        if n < 3:
                            continue
                        sum1, sum2 = sum(series1), sum(series2)
                        if sum1 == 0 and sum2 == 0:
                            continue
                        mean1, mean2 = sum1 / n, sum2 / n
                        num = sum((series1[k] - mean1) * (series2[k] - mean2) for k in range(n))
                        den1 = sum((series1[k] - mean1) ** 2 for k in range(n))
                        den2 = sum((series2[k] - mean2) ** 2 for k in range(n))
                        if den1 > 0 and den2 > 0:
                            corr = num / ((den1 * den2) ** 0.5)
                            correlated_pairs.append((d1, d2, corr))

                if len(correlated_pairs) >= 3:
                    cors = [abs(c[2]) for c in correlated_pairs]
                    median_cor, threshold_cor = _compute_median_and_threshold(cors)

                    for d1, d2, corr in correlated_pairs:
                        if abs(corr) > max(threshold_cor, 0.7):
                            findings.append(
                                {
                                    "type": "implicit_dependency",
                                    "domains": [d1, d2],
                                    "correlation": round(corr, 3),
                                    "threshold": round(threshold_cor, 3),
                                    "to_agent": "claude",
                                    "priority": 2,
                                    "task_type_field": "investigate_coupling",
                                    "title": (
                                        f"隐式依赖: {d1} 与 {d2} 增长高度相关 (r={corr:.2f})"
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
                    "source_scan": "scan_coupling",
                    "payload": f,
                },
            )
            dispatched += 1
        except Exception:
            pass

    return {"scanner": "scan_coupling", "findings": len(findings), "dispatched": dispatched}
