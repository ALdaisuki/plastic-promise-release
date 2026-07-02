# Dual-Track Audit Model — Design Spec

**Date**: 2026-07-03
**Status**: Design — approved
**Scope**: Embed structured audit into SuperPowers workflow as a first-class governance stage

## Problem

PR audits are currently ad-hoc. There is no formal process, no risk-based triage, no standardized checklist. High-risk PRs slip through with the same level of scrutiny as single-line fixes. Low-risk PRs get over-audited, wasting reviewer time.

## Architecture

Dual-track model based on automatic risk classification:

```
                     ALL PRs
                       │
              receiving-code-review
              │  简化 SOP (5 checks)
              │  设计原则 | 信任分 | 测试 | Breaking | 依赖
              │
              ├─ LOW-RISK  ──→  verification  ──→  finishing
              │
              └─ HIGH-RISK  ──→  audit (10 checks)  ──→  verification  ──→  finishing
                                   │
                                   ├─ PASS  → AUDIT_COMPLETED
                                   └─ BLOCK  → request_changes, return to review
```

## Components

### 1. Risk Classification

Automatic triage via `_is_high_risk_pr()`:

```python
HIGH_RISK_LABELS = {
    "AUDIT_PENDING",      # manually flagged
    "BREAKING_CHANGE",    # breaking API or behavior
    "SECURITY",           # touches auth, permissions, encryption
    "CROSS_MODULE",       # affects 3+ modules
}

NON_CODE_EXTENSIONS = {".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".lock"}

def _is_high_risk_pr(pr_meta):
    # Label-based
    if pr_meta.labels & HIGH_RISK_LABELS:
        return True
    # Size-based (code files only — exclude docs, config, lockfiles)
    code_files = sum(1 for f in pr_meta.files
                     if not any(f.endswith(ext) for ext in NON_CODE_EXTENSIONS))
    return code_files >= 10 or pr_meta.lines_changed >= 500
```

**Risk determination is idempotent**: re-running against the same PR always produces the same result.

### 2. Low-Risk SOP (embedded in receiving-code-review)

5 checks. Executed during `receiving-code-review` for ALL PRs.

| # | Check | Question | Failure response |
|---|-------|----------|-----------------|
| 1 | Design principles | Does this change respect core principles (Occam's Razor, traceability)? | nit — suggest improvement |
| 2 | Trust score impact | Does it modify trust calculation? If yes, is delta reasonable? | blocking if trust logic is broken |
| 3 | Test coverage | Are there tests for new behavior? Do existing tests pass? | blocking if untested logic |
| 4 | Breaking change | Is breaking behavior documented in PR description? | blocking if unmarked breaking change |
| 5 | Dependency change | Any new/modified external dependencies? Reasonable? | nit if undocumented |

**Output**: `review_result` with per-check status (passed/nit/blocking) and trust delta.

### 3. High-Risk SOP (independent audit stage)

10 checks. Only executed when `_is_high_risk_pr()` returns True.

| # | Check | Question | Failure response |
|---|-------|----------|-----------------|
| 1 | Design principles | All 12 core principles considered? Any violations? | blocking if principle violation |
| 2 | Architecture impact | Module boundaries or data flow changed? | nit — document the impact |
| 3 | Security | Auth, permissions, encryption, injection vectors? | blocking for security gaps |
| 4 | Cross-module impact | 3+ modules touched? Downstream consumers identified? | blocking if undocumented |
| 5 | Performance | Performance-sensitive path changed? Benchmark needed? | nit — suggest benchmark |
| 6 | Dependency change | External dep version bump? License check? | blocking for vulnerability |
| 7 | API compatibility | Breaking API change? Migration path documented? | blocking for unmarked break |
| 8 | Data migration | Schema change? Migration script? Rollback plan? | blocking for missing migration |
| 9 | Rollback plan | Can this be rolled back safely? How? | nit — document rollback |
| 10 | Documentation | Related docs updated? Spec/plan/examples? | nit — list missing docs |

**Output**: `audit_result` with per-check status, trust delta, and pass/block decision.

### 4. Audit Pass/Fail Decision

```
PASS  = zero blocking issues → AUDIT_COMPLETED → enter verification
BLOCK = ≥1 blocking issues → request_changes → return to requesting-code-review

Nit issues do NOT block. They are recorded but allow the audit to pass.
```

### 5. Label Flow

```
AUDIT_PENDING  ──→  audit starts
                   │
                   ├─ PASS  → remove AUDIT_PENDING, add AUDIT_COMPLETED
                   └─ BLOCK  → AUDIT_PENDING stays, PR gets request_changes
```

### 6. SKILL_CHAIN_MAP Update

```python
# constants.py
"receiving-code-review": {
    "predecessors": ["requesting-code-review"],
    "successors": ["audit", "verification-before-completion"],  # dual exit
},
"audit": {                                                      # NEW
    "predecessors": ["receiving-code-review"],
    "successors": ["verification-before-completion"],
},
```

Chain enforcement:
- `sp-stage: audit` is callable ONLY after `receiving-code-review`
- When `_is_high_risk_pr()` is False, `audit` is skipped and chain goes directly to `verification-before-completion`
- When True, `audit` is mandatory — `verification-before-completion` is not accessible until `audit` completes with PASS

### 7. STAGE_ATOMS (governance injection pattern)

```python
# superpowers_stages.py
"receiving-code-review": [
    "defense", "principle_activate", "memory_recall",
    "audit_run",          # existing — runs 7-dimension audit
    "memory_store",
    "step_closure_full",
],
"audit": [                # NEW stage
    "defense",            # trust check before auditing
    "principle_activate", # activate governing principles
    "audit_run",          # 7-dimension + skill_trace audit
    "memory_recall",      # recall previous audit patterns
    "memory_store",       # store audit report
    "step_closure_full",  # full closure (SCARF + trust adjustment)
],
```

### 8. Audit Report Storage

Audit results stored as `memory_type="audit"` with structured metadata:

```python
{
    "content": "# Audit Report — 2026-07-03\n...",
    "memory_type": "audit",
    "tags": [
        "audit:completed",  # or audit:blocked
        f"pr:{pr_number}",
        "risk:high",
    ],
    "tier": "L2",
}
```

## Data Flow

```
receiving-code-review
  ├─ 简化 SOP 5 checks → review_result (trust_delta, blocking_count)
  ├─ _is_high_risk_pr() → True/False
  │
  ├─ [False] → verification-before-completion → finishing
  │
  └─ [True] → audit
                ├─ 完整 SOP 10 checks → audit_result
                ├─ PASS  → AUDIT_COMPLETED → verification → finishing
                └─ BLOCK → request_changes → back to review
```

## Verification

```bash
# Low-risk PR: audit stage skipped
sp-stage receiving-code-review → directly enters verification

# High-risk PR: audit stage mandatory
sp-stage receiving-code-review → sp-stage audit (cannot skip) → verification

# AUDIT_PENDING label: auto-routed to audit
gh pr edit #15 --add-label AUDIT_PENDING
→ receiving-code-review → _is_high_risk_pr() detects label → audit required
```

## Constraints

- Risk classification is automatic — no human decision needed
- File-type filtering prevents docs-only PRs from being over-classified
- Audit results are stored as memories for future retrieval (pattern recognition)
- Trust delta is cumulative across low-risk SOP + high-risk SOP
- All checks have explicit pass/block/nit outcomes — no ambiguity
- Chain enforcement prevents skipping audit for high-risk PRs

## Audit Review Resolutions (7 items from spec review)

### R1: Risk re-evaluation on PR update

`_is_high_risk_pr()` is recalculated on every `receiving-code-review` invocation — no caching. If a PR grows beyond the threshold mid-review, it is re-classified as high-risk and the audit stage becomes mandatory.

### R2: Audit report recall and reuse

Before starting a fresh audit, query existing audit reports for the same PR:

```python
existing = memory_recall(query=f"audit pr:{pr_number}", memory_type="audit", max_results=1)
if existing and existing[0].relevance >= 0.90 and existing[0].blocking_count == 0:
    # Reuse — skip redundant audit
```

Only reuse if: same PR, high relevance match, zero blocking issues in previous report. Blocked audits always re-run.

### R3: Audit degradation path

If `audit_run` times out (Ollama down, 10s timeout):

```python
# STAGE_DEGRADE for audit stage
"audit_run": "fallback:audit_run_light",  # 7-dim → 3-dim simplified
```

Fallback `audit_run_light`: runs only transparency + constraint_compliance + skill_trace (3 dimensions instead of 7). The 10-item human checklist is unaffected — degradation only affects the automated dimension.

### R4: Trust score delta values

| Stage | Event | Delta |
|-------|-------|-------|
| receiving-code-review | Low-risk pass (no blocking) | +0.01 |
| receiving-code-review | Blocking issue found | -0.02 |
| audit | High-risk pass (no blocking) | +0.02 |
| audit | Blocking issue found | -0.03 |
| audit | Nit only (no blocking) | 0.00 |
| Any stage | PR rejected (CLOSE / REQUEST CHANGES + denied) | -0.05 |

Deltas are cumulative. Example: low-risk pass +0.01 + CI pass +0.01 = +0.02 total.

### R5: AUDIT_PENDING label permissions

| Trigger | Who | When |
|---------|-----|------|
| Auto-add | CI / sp-stage handler | `_is_high_risk_pr()` returns True — added on first `receiving-code-review` |
| Manual-add | PR author or reviewer | Via `gh pr edit --add-label AUDIT_PENDING` |
| Auto-remove | sp-stage: audit handler | Audit passes → remove `AUDIT_PENDING`, add `AUDIT_COMPLETED` |

### R6: Audit report access control

```python
memory_store(
    content=audit_report,
    memory_type="audit",
    source="audit",       # dedicated source — not "user" or "system"
    tags=["audit:completed", f"pr:{pr_number}"],
    domain="audit",        # separate domain — excluded from normal context_supply
)
```

Audit reports are recalled ONLY when `memory_type="audit"` is explicitly requested. Normal `context_supply` with `scope="building"` or `scope="designing"` does NOT return audit memories.

### R7: Relationship between 10-item checklist and audit_run

| | audit_run (automated) | 10-item checklist (human/Agent) |
|---|---|---|
| **Scope** | System health: 7 dimensions, quantitative scores | Code change: design/security/performance, qualitative pass/block/nit |
| **Executor** | `audit_run` MCP tool → SoulAuditor | Reviewing Agent, guided by SOP template |
| **Output** | Composite score (0.0-1.0) per dimension | Per-check status + trust delta + blocking count |
| **Pass threshold** | Overall score >= 0.60 | Zero blocking issues |

Audit passes only when **BOTH** conditions are met:
1. `audit_run` overall score >= 0.60
2. 10-item checklist has zero blocking issues
