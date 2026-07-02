---
name: audit
description: "MUST use for high-risk PRs — 10-item structured audit checklist with pass/block decision. Triggered automatically when PR has >=10 code files, >=500 lines changed, or AUDIT_PENDING/BREAKING_CHANGE/SECURITY/CROSS_MODULE label."
---

# Audit — Structured PR Review

## Overview

Structured audit for high-risk Pull Requests. Runs a 10-item checklist covering design principles, security, cross-module impact, API compatibility, and rollback planning. Produces a pass/block decision with trust score delta.

**Core principle:** High-risk changes deserve structured scrutiny. Low-risk changes get a lighter 5-item check embedded in code review.

## Risk Classification (automatic)

A PR is HIGH-RISK if ANY of:
- >= 10 code files changed (excluding .md, .json, .lock)
- >= 500 lines changed
- Labeled: AUDIT_PENDING, BREAKING_CHANGE, SECURITY, or CROSS_MODULE

## Checklist

### Low-Risk SOP (5 items, embedded in receiving-code-review)
1. Design principles — core conventions respected?
2. Trust score impact — trust logic changed?
3. Test coverage — tests for new behavior?
4. Breaking change — documented in PR?
5. Dependency change — new deps? Reasonable?

### High-Risk SOP (10 items, standalone audit stage)
Items 1-5 from above, plus:
6. Architecture impact — module boundaries or data flow changed?
7. Security — auth, permissions, encryption?
8. Cross-module impact — 3+ modules? downstream consumers?
9. API compatibility — breaking API? migration path?
10. Rollback + docs — rollback plan? docs updated?

## Trust Score Delta (cumulative)

| Event | Delta |
|-------|-------|
| Low-risk pass (no blocking) | +0.01 |
| High-risk pass (no blocking) | +0.02 |
| Blocking (review stage) | -0.02 |
| Blocking (audit stage) | -0.03 |
| PR rejected | -0.05 |

## Pass Condition

BOTH must be true:
1. audit_run overall score >= 0.60 (automated 7-dimension audit)
2. Zero blocking issues in 10-item checklist

Nit issues do NOT block. They are recorded but allow the audit to pass.

## Red Flags

- Skipping audit for a high-risk PR
- Marking a blocking issue as "nit" to bypass the gate
- Audit report not stored as memory (breaks traceability)
- Trust delta not recorded

## Process

```
receiving-code-review → _is_high_risk_pr()
  ├─ low risk → skip audit → verification
  └─ high risk → audit (10 checks) → pass/block → verification
```
