"""Issue Validator — 宪法校验 + 信任-自由度矩阵

规则:
  1. Issue context 必须包含 files, interfaces, acceptance
  2. 信任分 → 离散自由度 → 工具权限映射
  3. 校验不区分角色 — Claude 和 Pi 同等约束
"""

REQUIRED_CONTEXT = ["files", "interfaces", "acceptance"]

# 信任 → 自由度映射
TRUST_TIERS = [
    (0.80, "autonomous", "放手干，结果负责"),
    (0.60, "standard",   "常规操作，需周知"),
    (0.30, "restricted", "关键操作需审批"),
    (0.00, "readonly",   "只能看，不能动"),
]

# 自由度 → 工具权限映射 (* 后缀 = 需审批)
ACTION_PERMISSIONS = {
    "read":           ["readonly", "restricted", "standard", "autonomous"],
    "memory_recall":  ["readonly", "restricted", "standard", "autonomous"],
    "issue_list":     ["readonly", "restricted", "standard", "autonomous"],
    "write_file":     ["restricted*", "standard", "autonomous"],
    "run_bash":       ["restricted*", "standard", "autonomous"],
    "issue_create":   ["standard", "autonomous"],
    "issue_close":    ["standard*", "autonomous"],
    "assign_task":    ["autonomous"],
    "modify_principle": ["autonomous*"],
}


def validate_issue_context(issue: dict) -> dict:
    """校验 Issue context 是否完整。

    Args:
        issue: 含 context 字段的 Issue dict。

    Returns:
        {"valid": True} 或 {"error": "NEEDS_CONTEXT: 缺少 [...]"}
    """
    context = issue.get("context", {})
    if not isinstance(context, dict):
        return {"error": "NEEDS_CONTEXT: context 必须是一个对象"}
    missing = [k for k in REQUIRED_CONTEXT if not context.get(k)]
    if missing:
        return {"error": f"NEEDS_CONTEXT: 缺少 {missing}。请补全后重新创建。"}
    return {"valid": True}


def get_tier(trust_score: float) -> str:
    """将连续信任分映射为离散自由度等级。

    Args:
        trust_score: 0.0-1.0 的信任分。

    Returns:
        "autonomous" | "standard" | "restricted" | "readonly"
    """
    for threshold, name, _ in TRUST_TIERS:
        if trust_score >= threshold:
            return name
    return "readonly"


def get_tier_info(trust_score: float) -> dict:
    """返回完整的自由度信息（含 motto）。"""
    for threshold, name, motto in TRUST_TIERS:
        if trust_score >= threshold:
            return {"tier": name, "threshold": threshold, "motto": motto}
    return {"tier": "readonly", "threshold": 0.0, "motto": "只能看，不能动"}


def check_permission(tier: str, action: str) -> str:
    """检查指定自由度的 Agent 是否有权执行某操作。

    Args:
        tier: 自由度等级 ("autonomous" 等)。
        action: 操作名 ("write_file" 等)。

    Returns:
        "granted" | "needs_review" | "denied"
    """
    allowed = ACTION_PERMISSIONS.get(action, [])
    if tier in allowed:
        return "granted"
    if f"{tier}*" in allowed:
        return "needs_review"
    return "denied"


def validate_deliverable(issue: dict) -> dict:
    """校验已解决 Issue 的 deliverable 是否完整。

    在 issue_transition -> resolved 时调用，确保：
      1. context 仍包含所有 REQUIRED_CONTEXT 字段
      2. context 包含 "deliverable" 字段（实际创建/修改的文件列表）

    Args:
        issue: 含 context 字段的 Issue dict。

    Returns:
        {"valid": True} 或 {"error": "..."}
    """
    context = issue.get("context", {})
    if not isinstance(context, dict):
        return {"error": "NEEDS_CONTEXT: context 必须是一个对象"}

    missing = [k for k in REQUIRED_CONTEXT if not context.get(k)]
    if missing:
        return {"error": f"NEEDS_CONTEXT: 缺少 {missing}。请补全后重新创建。"}

    deliverable = context.get("deliverable")
    if deliverable is None:
        return {"error": "NEEDS_DELIVERABLE: 缺少 deliverable 字段。请列出实际创建/修改的文件。"}
    if not isinstance(deliverable, list):
        return {"error": "NEEDS_DELIVERABLE: deliverable 必须是一个文件路径列表。"}
    if len(deliverable) == 0:
        return {"error": "NEEDS_DELIVERABLE: deliverable 列表不能为空。请至少列出一个文件。"}

    return {"valid": True}
