"""Daily audit — aggregate memory stats and generate a summary report.

Runs every 24 hours. Stores summary as a reflection memory.
"""

import datetime
import json
from typing import Any


def run(engine: Any = None) -> dict:
    """Generate daily audit summary.

    Args:
        engine: ContextEngine instance.

    Returns:
        dict with daily audit report.
    """
    now = datetime.datetime.now().isoformat()
    report: dict = {
        "timestamp": now,
        "date": now[:10],
        "memory_stats": {},
        "recommendation": "",
    }

    if engine is not None:
        try:
            stats_str = engine.memory_stats_json()
            if isinstance(stats_str, str):
                report["memory_stats"] = json.loads(stats_str)
            else:
                report["memory_stats"] = stats_str or {}
        except Exception:
            pass

        try:
            total = report["memory_stats"].get("total", 0)
            healthy = report["memory_stats"].get("healthy", 0)
            decaying = report["memory_stats"].get("decaying", 0)
            if total > 0:
                health_ratio = healthy / total
                if health_ratio < 0.80:
                    report["recommendation"] = (
                        f"Memory health below 80% ({health_ratio:.0%}). Consider running GC."
                    )
                elif decaying > total * 0.15:
                    report["recommendation"] = (
                        f"High decay rate ({decaying}/{total}). Review worth thresholds."
                    )
                else:
                    report["recommendation"] = "Memory pool healthy."
        except Exception:
            report["recommendation"] = "Unable to compute recommendations."

    return report
