"""Hunter Rank System — trust score → rank mapping (derived view, never stored)."""

from plastic_promise.core.constants import RANK_THRESHOLDS, RANK_TITLES, RANK_ICONS, RANK_ORDER


def trust_to_rank(trust_score: float) -> dict:
    """Derive hunter rank from trust score. Rank is a view, not stored."""
    for rank in ("S", "A", "B", "C", "D"):
        if trust_score >= RANK_THRESHOLDS[rank]:
            return {"rank": rank, "title": RANK_TITLES[rank], "icon": RANK_ICONS[rank]}
    return {"rank": "D", "title": RANK_TITLES["D"], "icon": RANK_ICONS["D"]}


def priority_to_rank(priority: int) -> str:
    """Map task priority to the minimum rank required to claim it."""
    mapping = {1: "S", 2: "A", 3: "B", 4: "C"}
    return mapping.get(priority, "C")


def can_claim(agent_trust: float, task_priority: int) -> tuple:
    """Check if an agent can claim a task of the given priority.

    Returns (ok: bool, message: str).
    """
    agent_rank = trust_to_rank(agent_trust)
    required_rank = priority_to_rank(task_priority)
    if RANK_ORDER[agent_rank["rank"]] > RANK_ORDER[required_rank]:
        return False, (
            f"!!! 委托推荐{required_rank}级，你的等级为{agent_rank['rank']}级"
            f"（{agent_rank['title']}），建议申请援助"
        )
    return True, "[OK] 等级匹配，可揭榜"
