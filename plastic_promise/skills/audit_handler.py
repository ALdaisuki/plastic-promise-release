"""Dual-track audit handler — risk classification + 10-item checklist.

Low-risk PRs (<10 code files, <500 lines, no high-risk labels): audit skipped.
High-risk PRs: mandatory 10-item audit with pass/block decision.
"""

import json as _json
import time as _time
from contextlib import suppress

# Risk classification thresholds
HIGH_RISK_LABELS = {"AUDIT_PENDING", "BREAKING_CHANGE", "SECURITY", "CROSS_MODULE"}
NON_CODE_EXTENSIONS = {".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".lock"}
HIGH_RISK_CODE_FILES = 10
HIGH_RISK_LINES = 500

# Trust score deltas (cumulative)
TRUST_LOW_RISK_PASS = 0.01
TRUST_HIGH_RISK_PASS = 0.02
TRUST_BLOCKING = -0.02
TRUST_AUDIT_BLOCKING = -0.03
TRUST_REJECTED = -0.05

# 10-item audit checklist
AUDIT_CHECKS = [
    ("design_principles", "设计原则 — 是否符合奥卡姆剃刀、全过程可查等核心约定？"),
    ("trust_impact", "信任分影响 — 是否涉及信任分调整逻辑？调整是否合理？"),
    ("test_coverage", "测试覆盖 — 是否有对应的单元/集成测试？"),
    ("breaking_change", "Breaking Change — 是否标记并在PR描述中说明影响？"),
    ("dependency_change", "依赖变更 — 是否新增或修改外部依赖？是否合理？"),
    ("architecture_impact", "架构影响 — 是否改变模块边界或数据流向？"),
    ("security", "安全隐患 — 是否涉及 auth/permissions/encryption？"),
    ("cross_module", "跨模块影响 — 3+模块？下游消费者已识别？"),
    ("api_compatibility", "API 兼容性 — 是否破坏现有API？迁移路径？"),
    ("rollback_docs", "回滚与文档 — 回滚方案？相关文档已更新？"),
]


def _is_high_risk(pr_meta: dict) -> bool:
    """Automatic risk classification. Re-evaluated on every receiving-code-review.

    Args:
        pr_meta: dict with keys: files (list[str]), lines_changed (int), labels (set[str])

    Returns:
        True if this PR requires the standalone audit stage.
    """
    # Label-based: any high-risk label triggers audit
    labels = set(pr_meta.get("labels", []))
    if labels & HIGH_RISK_LABELS:
        return True
    # Size-based: count code files only (exclude docs, config, lockfiles)
    files = pr_meta.get("files", [])
    code_files = sum(1 for f in files if not any(f.endswith(ext) for ext in NON_CODE_EXTENSIONS))
    if code_files >= HIGH_RISK_CODE_FILES:
        return True
    return pr_meta.get("lines_changed", 0) >= HIGH_RISK_LINES


async def _audit_handler(ctx, params: dict, atom_results: dict):
    """Audit stage handler — runs 10-item checklist for high-risk PRs.

    Called by sp-stage:audit. Stores audit report as memory_type="audit"
    with domain="audit" (excluded from normal context_supply).

    Returns:
        SkillResult with audit pass/block decision and trust delta.
    """
    from plastic_promise.skills.engine import SkillResult

    pr_meta = params.get("pr_meta", {})

    # Risk classification (re-evaluated every call — no caching)
    high_risk = _is_high_risk(pr_meta)

    if not high_risk:
        return SkillResult(
            skill_name="sp-audit",
            success=True,
            data={
                "stage": "audit",
                "risk": "low",
                "audit_skipped": True,
                "trust_delta": 0.0,
                "transition": "→ verification (audit skipped — low risk)",
            },
            atom_results={},
            degrade_log=[],
            audit_trail={"risk": "low"},
            errors=[],
        )

    # ── High-risk: run 10-item checklist ──
    results = []
    blocking_count = 0
    nit_count = 0

    for check_id, check_desc in AUDIT_CHECKS:
        # Each check is evaluated by the reviewing agent via sp-stage params.
        # The handler expects params to contain check results when called
        # after human/agent review.
        check_result = params.get(f"check_{check_id}", "pass")  # default pass
        if check_result == "blocking":
            blocking_count += 1
        elif check_result == "nit":
            nit_count += 1
        results.append({"check": check_id, "description": check_desc, "result": check_result})

    # ── Audit decision ──
    passed = blocking_count == 0
    trust_delta = TRUST_HIGH_RISK_PASS if passed else TRUST_AUDIT_BLOCKING

    # ── Store audit report as memory (isolated from normal context) ──
    pr_number = pr_meta.get("number", "unknown")
    report = {
        "pr": pr_number,
        "risk": "high",
        "passed": passed,
        "blocking_count": blocking_count,
        "nit_count": nit_count,
        "checks": results,
        "trust_delta": trust_delta,
    }
    with suppress(Exception):
        ctx.create_ordinary_if_absent(
            {
                "id": f"audit_pr{pr_number}_{int(_time.time())}",
                "content": _json.dumps(report, ensure_ascii=False),
                "memory_type": "audit",
                "source": "audit",
                "tags": [
                    f"pr:{pr_number}",
                    "audit:completed" if passed else "audit:blocked",
                    "risk:high",
                ],
                "domain": "audit",
                "tier": "L2",
            }
        )

    return SkillResult(
        skill_name="sp-audit",
        success=passed,
        data={
            "stage": "audit",
            "risk": "high",
            "passed": passed,
            "blocking_count": blocking_count,
            "nit_count": nit_count,
            "trust_delta": trust_delta,
            "checks": results,
            "transition": "→ verification" if passed else "→ request_changes",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={"risk": "high", "passed": passed},
        errors=[] if passed else [f"{blocking_count} blocking issues found"],
    )
