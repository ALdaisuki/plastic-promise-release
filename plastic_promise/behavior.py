"""AgentBehaviorTracker — 行为模式学习引擎

Serves 演化层: 追踪 Agent 行为模式，让系统"越用越默契"。
"""

import datetime
from typing import Any, Dict, List


class AgentBehaviorTracker:
    """Tracks Agent behavior patterns across sessions."""

    def __init__(self):
        self._events: List[dict] = []

    def record(
        self,
        task_type: str,
        principles: List[str],
        memory_types: List[str],
        owner: str = "",
    ):
        self._events.append({
            "task_type": task_type,
            "principles": principles,
            "memory_types": memory_types,
            "owner": owner,
            "timestamp": datetime.datetime.now().isoformat(),
        })

    def stats(self) -> dict:
        total = len(self._events)
        if total == 0:
            return {"session_count": 0, "top_task_types": [], "principle_heatmap": {}, "memory_type_distribution": {}}

        task_counts: Dict[str, int] = {}
        principle_counts: Dict[str, int] = {}
        memory_counts: Dict[str, int] = {}

        for e in self._events:
            task_counts[e["task_type"]] = task_counts.get(e["task_type"], 0) + 1
            for p in e["principles"]:
                principle_counts[p] = principle_counts.get(p, 0) + 1
            for m in e["memory_types"]:
                memory_counts[m] = memory_counts.get(m, 0) + 1

        return {
            "session_count": total,
            "top_task_types": sorted(task_counts.items(), key=lambda x: -x[1])[:5],
            "principle_heatmap": principle_counts,
            "memory_type_distribution": memory_counts,
        }

    def pattern(self) -> str:
        s = self.stats()
        if s["session_count"] == 0:
            return "尚未积累足够的行为数据。"
        top_task = s["top_task_types"][0] if s["top_task_types"] else ("未知", 0)
        top_p = max(s["principle_heatmap"].items(), key=lambda x: x[1]) if s["principle_heatmap"] else ("无", 0)
        top_m = max(s["memory_type_distribution"].items(), key=lambda x: x[1]) if s["memory_type_distribution"] else ("无", 0)
        return (
            f"在过去 {s['session_count']} 次交互中：\n"
            f"  最常做 {top_task[0]} 类任务（{top_task[1]}次），\n"
            f"  最常遵循原则「{top_p[0]}」（{top_p[1]}次），\n"
            f"  最常检索 {top_m[0]} 类记忆（{top_m[1]}次）。"
        )
