# Dual-Track Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed structured audit into SuperPowers workflow — low-risk PRs get 5-item embedded SOP, high-risk PRs get mandatory 10-item standalone audit stage.

**Architecture:** Add `audit` sp-stage between `receiving-code-review` and `verification-before-completion`. Automatic risk classification at receiving-code-review time. Trust delta cumulative. Audit reports stored as `memory_type="audit"`.

**Tech Stack:** Python 3.13, sp-stage handler pattern (mirrors PR #11 governance injection)

## Global Constraints

- `_is_high_risk_pr()` recalculated on every receiving-code-review — no caching
- Audit reports stored with `source="audit"`, `domain="audit"` — excluded from normal context_supply
- `audit_run` degrades to 3-dim light version on timeout
- Trust deltas are cumulative across stages
- Existing tests pass unchanged
- Audit SOP template is a markdown file, not code — human-readable reference

---

## File Structure

| File | Change | Responsibility |
|------|--------|---------------|
| `plastic_promise/core/constants.py` | MODIFY | SKILL_CHAIN_MAP — add `audit` stage, dual-exit for receiving-code-review |
| `plastic_promise/skills/superpowers_stages.py` | MODIFY | STAGE_ATOMS for audit, receiving-code-review, STAGE_DEGRADE |
| `plastic_promise/skills/audit_handler.py` | **NEW** | `_audit_handler` — risk classification + 10-item checklist + trust delta |
| `plastic_promise/mcp/server.py` | MODIFY | Register `audit` in sp-stage enum, chain validation |
| `.agents/skills/audit/SKILL.md` | **NEW** | Claude Code Skill registration |
| `docs/superpowers/specs/2026-07-03-dual-track-audit-design.md` | **DONE** | Design spec |
| `docs/superpowers/specs/audit-sop-template.md` | **DONE** | Audit SOP template (human reference) |

---

## Task 1: SKILL_CHAIN_MAP — Add audit stage + dual exit

- [ ] **Step 1: Update receiving-code-review successors**
  File: `plastic_promise/core/constants.py`
  ```python
  "receiving-code-review": {
      "predecessors": ["requesting-code-review"],
      "successors": ["audit", "verification-before-completion"],  # dual exit
  },
  ```
- [ ] **Step 2: Add audit stage**
  ```python
  "audit": {
      "predecessors": ["receiving-code-review"],
      "successors": ["verification-before-completion"],
  },
  ```
- [ ] **Step 3: Verify chain**
  ```bash
  python -c "from plastic_promise.core.constants import SKILL_CHAIN_MAP; assert 'audit' in SKILL_CHAIN_MAP; assert 'audit' in SKILL_CHAIN_MAP['receiving-code-review']['successors']"
  ```

---

## Task 2: STAGE_ATOMS — Audit stage with degradation

- [ ] **Step 1: Update receiving-code-review atoms**
  File: `plastic_promise/skills/superpowers_stages.py`
  ```python
  "receiving-code-review": [
      "defense", "principle_activate", "memory_recall",
      "audit_run", "memory_store", "step_closure_full",
  ],
  ```
- [ ] **Step 2: Add audit stage atoms**
  ```python
  "audit": [
      "defense", "principle_activate", "audit_run",
      "memory_recall", "memory_store", "step_closure_full",
  ],
  ```
- [ ] **Step 3: Add STAGE_TAGS_MAP entry**
  ```python
  "audit": ["stage:audit", "domain:governing", "task:verify"],
  ```
- [ ] **Step 4: Add STAGE_DESCRIPTIONS entry**
  ```python
  "audit": "SuperPowers 阶段: 审计 — 高风险PR完整审计 (10项检查 + audit_run)",
  ```
- [ ] **Step 5: Add STAGE_DOMAIN_MAP entry**
  ```python
  "audit": "governing",
  ```
- [ ] **Step 6: Update STAGE_DEGRADE**
  ```python
  "audit_run": "fallback:audit_run_light",
  ```

---

## Task 3: Audit Handler — Risk classification + 10-item checklist

- [ ] **Step 1: Create audit_handler.py**
  File: `plastic_promise/skills/audit_handler.py` (NEW, ~200 lines)
  ```python
  HIGH_RISK_LABELS = {"AUDIT_PENDING", "BREAKING_CHANGE", "SECURITY", "CROSS_MODULE"}
  NON_CODE_EXTENSIONS = {".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".lock"}

  def _is_high_risk_pr(pr_meta):
      if pr_meta.get("labels", set()) & HIGH_RISK_LABELS:
          return True
      code_files = sum(1 for f in pr_meta.get("files", [])
                       if not any(f.endswith(ext) for ext in NON_CODE_EXTENSIONS))
      return code_files >= 10 or pr_meta.get("lines_changed", 0) >= 500

  async def _audit_handler(ctx, params, atom_results):
      # 1. Parse PR metadata from params
      # 2. Call _is_high_risk_pr()
      # 3. If low-risk: return {"risk": "low", "audit_skipped": True}
      # 4. If high-risk: run 10-item checklist
      #    - For each check: record status (pass/nit/blocking)
      #    - Compute trust delta
      #    - Store audit report as memory (source="audit", domain="audit")
      # 5. Return SkillResult with audit pass/block + trust delta
  ```
- [ ] **Step 2: Register handler in SkillEngine**
  File: `plastic_promise/skills/superpowers_stages.py`
  ```python
  elif _stage_name == "audit":
      from plastic_promise.skills.audit_handler import _audit_handler
      _handler = _audit_handler
  ```
- [ ] **Step 3: Test risk classification**
  ```python
  assert _is_high_risk_pr({"files": ["a.py", "b.py", "c.md"], "lines_changed": 100}) == False  # 2 code files
  assert _is_high_risk_pr({"files": ["a.py"]*11, "lines_changed": 100}) == True  # 11 files
  assert _is_high_risk_pr({"files": ["a.py"], "lines_changed": 600}) == True  # 600 lines
  assert _is_high_risk_pr({"files": ["a.md"]*20, "lines_changed": 1000}) == False  # all docs
  assert _is_high_risk_pr({"files": ["a.py"], "labels": {"AUDIT_PENDING"}}) == True
  ```

---

## Task 4: Server — Register audit in sp-stage

- [ ] **Step 1: Add "audit" to sp-stage enum**
  File: `plastic_promise/mcp/server.py`, line ~955
  Add `"audit"` to the `stage` parameter's `enum` list
- [ ] **Step 2: Verify registration**
  ```bash
  python -c "from plastic_promise.mcp.server import server; print('audit registered')"
  ```

---

## Task 5: Claude Code Skill — SKILL.md

- [ ] **Step 1: Create audit SKILL.md**
  File: `.agents/skills/audit/SKILL.md` (NEW, ~80 lines)
  - HARD-GATE: Do NOT skip audit for high-risk PRs
  - Checklist: 10 items from low-risk/high-risk SOP
  - Trust delta table
  - Pass/block decision rule
- [ ] **Step 2: Verify skill loading**
  Restart session, check `Skill` tool shows `audit` or `superpowers:audit`

---

## Task 6: Integration test

- [ ] **Step 1: Low-risk PR flow**
  ```bash
  sp-stage receiving-code-review  # should auto-detect low risk, skip audit
  sp-stage verification-before-completion  # should succeed (no audit required)
  ```
- [ ] **Step 2: High-risk PR flow**
  ```bash
  sp-stage receiving-code-review  # should detect high risk
  sp-stage audit                  # mandatory — cannot skip
  sp-stage verification-before-completion  # only after audit PASS
  ```
- [ ] **Step 3: AUDIT_PENDING label**
  ```
  gh pr edit #15 --add-label AUDIT_PENDING
  sp-stage receiving-code-review  # should route to audit
  ```
- [ ] **Step 4: Blocking issue flow**
  ```
  sp-stage audit --blocking=true  # should return request_changes, NOT enter verification
  ```

---

## Task 7: Commit and PR

- [ ] **Step 1: Commit**
  ```bash
  git add -A && git commit -m "feat(audit): dual-track audit model — low-risk SOP + high-risk standalone stage

  - Add audit sp-stage between receiving-code-review and verification
  - Automatic risk classification (files>=10, lines>=500, labels)
  - Low-risk: 5-item SOP embedded in receiving-code-review
  - High-risk: 10-item standalone audit with pass/block decision
  - Trust delta cumulative: +0.01 low-risk, +0.02 high-risk pass
  - Audit reports stored as memory_type=audit, excluded from context_supply
  - audit_run degrades to 3-dim light version on timeout"
  ```
- [ ] **Step 2: Create PR**
  ```bash
  gh pr create --title "feat(audit): dual-track audit model" ...
  ```
