# Audit Workflow — SOP + Implementation Plan

> Combined: SOP template (reusable reference) + Design spec (7 resolutions) + Implementation (7 tasks)
> Estimated: 2h

## SOP Template (reusable)

```
# SuperPowers 审计标准操作程序 (Audit SOP)
**Date**: 2026-07-03 | **Version**: 1.0.0

执行对象: 所有 Plastic Promise PR
风险分级: 低风险 (5项嵌入) | 高风险 (10项独立 audit 阶段)
触发条件: files>=10 或 lines>=500 或 AUDIT_PENDING/BREAKING_CHANGE/SECURITY/CROSS_MODULE 标签

## 低风险 SOP (嵌入 receiving-code-review)
1. 设计原则 — 是否符合核心约定？
2. 信任分影响 — 是否有信任分逻辑变更？
3. 测试覆盖 — 是否有对应测试？
4. Breaking Change — 是否标记并说明？
5. 依赖变更 — 是否新增外部依赖？

## 高风险 SOP (独立 audit 阶段)
+ 低风险5项 + 以下5项:
6. 架构影响 — 模块边界或数据流是否改变？
7. 安全隐患 — 是否涉及 auth/permissions/encryption？
8. 跨模块影响 — 3+模块？下游消费者已识别？
9. API 兼容性 — 是否破坏现有 API？
10. 回滚与文档 — 回滚方案？文档更新？

## 信任分 Delta (累加制)
low-risk pass +0.01 | high-risk pass +0.02 | blocking -0.02 | blocking in audit -0.03 | PR rejected -0.05

## 审计通过条件
audit_run 总分 >= 0.60 AND 10项检查无 blocking
```

---

## Implementation Tasks

### T1: SKILL_CHAIN_MAP — add audit + dual exit

File: `plastic_promise/core/constants.py`

```python
"receiving-code-review": {
    "predecessors": ["requesting-code-review"],
    "successors": ["audit", "verification-before-completion"],
},
"audit": {
    "predecessors": ["receiving-code-review"],
    "successors": ["verification-before-completion"],
},
```

### T2: STAGE_ATOMS — audit stage with degradation

File: `plastic_promise/skills/superpowers_stages.py`

Add `"audit"` to STAGE_ATOMS, STAGE_TAGS_MAP, STAGE_DESCRIPTIONS, STAGE_DOMAIN_MAP, STAGE_DEGRADE.

### T3: Risk classification + audit handler

File: `plastic_promise/skills/audit_handler.py` (NEW)

`_is_high_risk_pr()` — code files >= 10 or lines >= 500 or HIGH_RISK_LABELS.
`_audit_handler()` — runs 10-item checklist, computes trust delta, stores report as memory_type="audit".

### T4: Server — register "audit" in sp-stage enum

File: `plastic_promise/mcp/server.py`

### T5: SKILL.md

File: `.agents/skills/audit/SKILL.md` (NEW)

### T6: Test

Low-risk PR flow: receiving-code-review → verification (audit skipped).
High-risk PR flow: receiving-code-review → audit → verification.

### T7: Commit and PR
