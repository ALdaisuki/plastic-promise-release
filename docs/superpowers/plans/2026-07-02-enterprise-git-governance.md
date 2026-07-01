# Enterprise Git Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish enterprise-grade Git governance framework — CI/CD pipeline, PR/Issue templates, CODEOWNERS, SECURITY.md, CONTRIBUTING.md, and CLAUDE.md updates — per the Plastic Promise Flow spec.

**Architecture:** 9 config/template files + 1 CLAUDE.md update. All files are static config (.yml, .md) with no runtime dependencies. GitHub reads these files on PR open / Issue open / push — no deployment needed beyond `git push`.

**Tech Stack:** GitHub Actions YAML, Markdown, conventional commits format.

## Global Constraints

- All file paths under `.github/` follow GitHub's expected layout
- PR template at `.github/PULL_REQUEST_TEMPLATE.md` (not `docs/`)
- Issue templates at `.github/ISSUE_TEMPLATE/` with `config.yml` for the chooser
- CODEOWNERS must reference valid GitHub usernames (`@ALdaisuki` is the sole human)
- All commit messages follow `docs:` or `chore:` conventional commit format
- No emoji in any project file (user preference)

---

### Task 1: Create `.github/` Directory Structure and CI Workflow

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: GitHub Actions workflow triggered on `pull_request` to `main` and `push` to `main`

- [ ] **Step 1: Create parent directories**

```bash
mkdir -p .github/workflows .github/ISSUE_TEMPLATE
```

- [ ] **Step 2: Write CI workflow file**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

jobs:
  # ── Stage 1: P0 — MUST PASS ──────────────────────

  lint-python:
    name: "P0: Python lint & type check"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install ruff mypy
      - run: ruff check plastic_promise/
      - run: mypy plastic_promise/ --ignore-missing-imports

  test-python:
    name: "P0: Python unit tests"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v --tb=short

  security-python:
    name: "P0: Python security scan"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install bandit
      - run: bandit -r plastic_promise/ -ll

  check-rust:
    name: "P0: Rust compile & test"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
      - run: cargo build --manifest-path rust/context-engine-core/Cargo.toml
      - run: cargo test --manifest-path rust/context-engine-core/Cargo.toml

  security-rust:
    name: "P0: Rust security audit"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
      - run: cargo install cargo-audit
      - run: cargo audit --manifest-path rust/context-engine-core/Cargo.toml

  # ── Stage 2: P1 — SHOULD PASS ──────────────────────

  format-check:
    name: "P1: Code style"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install ruff
      - run: ruff format --check plastic_promise/ tests/

  coverage:
    name: "P1: Test coverage"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]" pytest-cov
      - run: pytest tests/ --cov=plastic_promise --cov-report=term --cov-fail-under=70
```

- [ ] **Step 3: Verify YAML syntax**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Expected: silent success (no parse error).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "chore: add CI workflow — Python lint/test/security + Rust build/test/audit"
```

---

### Task 2: Create PR Template

**Files:**
- Create: `.github/PULL_REQUEST_TEMPLATE.md`

**Interfaces:**
- Produces: Template auto-populated when any PR is opened on GitHub

- [ ] **Step 1: Write PR template**

Create `.github/PULL_REQUEST_TEMPLATE.md`:

```markdown
## Associated Delegation
Task ID: <!-- Hunter Guild task_id, or N/A -->

## Change Type
- [ ] feat: New feature
- [ ] fix: Bug fix
- [ ] refactor: Refactor
- [ ] docs: Documentation
- [ ] perf: Performance
- [ ] test: Test
- [ ] chore: Build/CI/tooling

## Summary
<!-- One-line description of what this change does -->

## Verification
<!-- CI status + manual verification steps performed -->

## Impact
<!-- Affected modules, APIs, DB migrations, configuration changes -->
```

- [ ] **Step 2: Verify file is at correct path**

```bash
test -f .github/PULL_REQUEST_TEMPLATE.md && echo "OK: Template exists"
```

Expected: `OK: Template exists`

- [ ] **Step 3: Commit**

```bash
git add .github/PULL_REQUEST_TEMPLATE.md
git commit -m "docs: add PR template with Hunter Guild task association"
```

---

### Task 3: Create Issue Templates and Config

**Files:**
- Create: `.github/ISSUE_TEMPLATE/bug_report.md`
- Create: `.github/ISSUE_TEMPLATE/feature_request.md`
- Create: `.github/ISSUE_TEMPLATE/delegation.md`
- Create: `.github/ISSUE_TEMPLATE/config.yml`

**Interfaces:**
- Produces: GitHub issue chooser with 3 template options

- [ ] **Step 1: Write bug report template**

Create `.github/ISSUE_TEMPLATE/bug_report.md`:

```markdown
---
name: Bug Report
about: Report a bug or unexpected behavior
title: "fix: "
labels: ["bug", "task:pending"]
assignees: []
---

## Description
<!-- What happened? What did you expect to happen? -->

## Reproduction Steps
1.
2.
3.

## Environment
- OS: <!-- Windows / macOS / Linux -->
- Python version: <!-- e.g. 3.11 -->
- Commit: <!-- git rev-parse HEAD -->

## Additional Context
<!-- Logs, screenshots, related memories or issues -->
```

- [ ] **Step 2: Write feature request template**

Create `.github/ISSUE_TEMPLATE/feature_request.md`:

```markdown
---
name: Feature Request
about: Propose a new feature or enhancement
title: "feat: "
labels: ["enhancement", "task:pending"]
assignees: []
---

## Motivation
<!-- What problem does this solve? Why is it valuable? -->

## Proposed Solution
<!-- How should it work? -->

## Alternatives Considered
<!-- What other approaches did you think about? -->

## Scope
<!-- What modules / APIs / docs would be affected? -->
```

- [ ] **Step 3: Write delegation template**

Create `.github/ISSUE_TEMPLATE/delegation.md`:

```markdown
---
name: Hunter Guild Delegation
about: Create a manual delegation for the Hunter Guild system
title: ""
labels: ["delegation"]
assignees: []
---

## Delegation
- **Task Type:** <!-- fix_memory / gc_* / build_* / refactor_* / review_* / investigate_* -->
- **Target Agent:** <!-- pi_builder / pi_fixer / pi_reviewer / claude -->
- **Priority:** <!-- 1=S / 2=A / 3=B / 4=C -->
- **Domain:** <!-- building / fixing / designing / reflecting / governing -->

## Description
<!-- What needs to be done -->

## Associated Principles
<!-- Which core principles (1-13) does this relate to? -->

## Acceptance Criteria
<!-- How do we know this is done? -->
```

- [ ] **Step 4: Write template chooser config**

Create `.github/ISSUE_TEMPLATE/config.yml`:

```yaml
blank_issues_enabled: true
contact_links:
  - name: Plastic Promise Documentation
    url: https://github.com/ALdaisuki/plastic-promise/blob/main/docs/GOAL.md
    about: Read the project goals, architecture, and current status
  - name: Security Vulnerability
    url: https://github.com/ALdaisuki/plastic-promise/security/advisories/new
    about: Report a security vulnerability (see SECURITY.md)
```

- [ ] **Step 5: Verify all templates exist**

```bash
for f in bug_report feature_request delegation config; do
  test -f ".github/ISSUE_TEMPLATE/${f}.yml" -o -f ".github/ISSUE_TEMPLATE/${f}.md" && echo "OK: $f" || echo "MISSING: $f"
done
```

Expected: `OK` for all four files.

- [ ] **Step 6: Commit**

```bash
git add .github/ISSUE_TEMPLATE/
git commit -m "docs: add issue templates — bug report, feature request, Hunter Guild delegation"
```

---

### Task 4: Create CODEOWNERS

**Files:**
- Create: `.github/CODEOWNERS`

**Interfaces:**
- Produces: GitHub enforces required reviews from listed owners on matching paths

- [ ] **Step 1: Write CODEOWNERS**

Create `.github/CODEOWNERS`:

```
# Global owner — Claude (human maintainer)
*                           @ALdaisuki

# Core engine — requires Claude + 1 Reviewer
plastic_promise/memory/     @ALdaisuki
plastic_promise/loop/       @ALdaisuki
plastic_promise/principles/ @ALdaisuki

# Rust engine — requires Claude approve
rust/                       @ALdaisuki

# Security-sensitive — requires Claude sole approve
plastic_promise/defense/soul_enforcer.py @ALdaisuki
plastic_promise/defense/trust_store.py   @ALdaisuki

# Docs and config — lower barrier
docs/          @ALdaisuki
*.md           @ALdaisuki
```

- [ ] **Step 2: Verify valid syntax**

```bash
# CODEOWNERS has no strict schema; verify no empty lines in patterns
grep -v '^#' .github/CODEOWNERS | grep -v '^$' | head -5
```

Expected: shows the ownership lines with `@ALdaisuki`.

- [ ] **Step 3: Commit**

```bash
git add .github/CODEOWNERS
git commit -m "chore: add CODEOWNERS — Claude as global maintainer + security-sensitive paths"
```

---

### Task 5: Create SECURITY.md

**Files:**
- Create: `SECURITY.md`

**Interfaces:**
- Produces: GitHub displays this on the Security tab, linked from issue templates

- [ ] **Step 1: Write SECURITY.md**

Create `SECURITY.md`:

```markdown
# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest main branch | Yes |
| tagged releases (v*.*.*) | Yes |
| feature branches | No |

## Reporting a Vulnerability

**Do not open a public Issue for security vulnerabilities.**

Instead, report via:

1. **GitHub Security Advisory** (preferred): [Report a vulnerability](https://github.com/ALdaisuki/plastic-promise/security/advisories/new)
2. **Direct contact**: Email the maintainer at the address listed on the GitHub profile

### What to include

- Description of the vulnerability
- Steps to reproduce
- Affected versions / commits
- Potential impact
- Any suggested fixes (optional)

### Response timeline

- **Acknowledgment**: within 48 hours
- **Status update**: within 5 business days
- **Resolution**: depends on severity (critical: 72h target, high: 7d, medium: 14d, low: next release)

## Security Practices

This project follows defense-in-depth principles:

- **L0 Hard Boundary**: Dangerous operations (shell exec, file delete) require explicit confirmation
- **L1 Trust Constraints**: Trust score gating for all write operations
- **L2 Immune Patrol**: Daemon scanners detect anomalies in code quality, memory health, and trust patterns
- **Dependency Audit**: Run `cargo audit` (Rust) and `bandit` (Python) in CI
- **Immutable History**: `main` branch protected; force push forbidden; all changes via reviewed PR

## Dependency Security

- Python: `bandit -r plastic_promise/ -ll` runs on every PR
- Rust: `cargo audit` runs on every PR
- Dependencies are pinned with version ranges in `pyproject.toml` and `Cargo.toml`
- Review diffs to lockfiles (`Cargo.lock`) for unexpected changes
```

- [ ] **Step 2: Verify file content**

```bash
grep -c "Reporting a Vulnerability" SECURITY.md
```

Expected: `1` (section exists).

- [ ] **Step 3: Commit**

```bash
git add SECURITY.md
git commit -m "docs: add SECURITY.md — vulnerability reporting, supported versions, security practices"
```

---

### Task 6: Create CONTRIBUTING.md

**Files:**
- Create: `CONTRIBUTING.md`

**Interfaces:**
- Produces: GitHub auto-links this from new PR and Issue pages

- [ ] **Step 1: Write CONTRIBUTING.md**

Create `CONTRIBUTING.md`:

````markdown
# Contributing to Plastic Promise

## Project Overview

Plastic Promise is an AI behavior governance system built on **Commitment Engineering** — agents internalize conventions rather than being gated by external rules. It combines:

- **12 Core Principles** — behavioral constitution for AI agents
- **Hunter Guild** — delegation-based multi-agent task scheduling
- **Memory System** — LanceDB-backed vector memory with quality pipeline and decay
- **SuperPowers Pipeline** — 12-stage development workflow with chain constraints

Architecture and current status: [docs/GOAL.md](docs/GOAL.md)

## Development Setup

```bash
git clone git@github.com:ALdaisuki/plastic-promise.git
cd plastic-promise
pip install -e ".[dev]"
```

For Rust engine development:
```bash
cd rust/context-engine-core
cargo build
cargo test
```

## Branch Convention

| Prefix | Usage |
|--------|-------|
| `feat/` | New feature |
| `fix/` | Bug fix |
| `refactor/` | Refactor (no behavior change) |
| `docs/` | Documentation |
| `perf/` | Performance |
| `chore/` | Build/CI/tooling |
| `worktree/<agent>/` | Agent worktree isolation |

Branch names: lowercase, `-` separated. Agent branches are auto-generated by Daemon as `<type>/<task_id>-<slug>`.

## Commit Convention

All commits follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>
```

Examples:
```
feat(memory): add Weibull decay calculator
fix(context): resolve LanceDB empty-retriever fallback
refactor(rust): make supply() stateless
docs: add CI workflow documentation
```

## Pull Request Flow

1. **Create branch** from `main`
2. **Develop** with frequent commits following the convention above
3. **Open PR** — CI runs automatically (P0: lint/test/security, P1: style/coverage)
4. **Code Review** — at least 1 reviewer approve required (CI agents: Pi Reviewer; humans: maintainer)
5. **Squash Merge** into `main` — one commit per PR, linear history

## Hunter Guild Delegation System

Multi-agent task orchestration with trust-score-gated permissions:

| Trust Score | Level | Hunter Rank | Merge Permission |
|-------------|-------|-------------|------------------|
| >= 0.80 | autonomous | S Legendary | Autonomous merge |
| >= 0.65 | standard | A Senior | Needs 1 approve |
| >= 0.50 | standard | B Regular | Needs 1 approve |
| >= 0.35 | restricted | C Apprentice | Needs 2 approves |
| < 0.35 | readonly | D Demoted | Cannot submit PRs |

Trust scores adjust based on: CI pass/fail, review outcome, scanner findings, SCARF self-reflection.

## Code Review Guidelines

- **nit**: Minor issues (naming, formatting) — fix but no trust penalty
- **design**: Architectural concerns — -0.005 trust per instance
- **blocking**: Security/bug risks — -0.01 trust per instance, must fix
- **praise**: Positive feedback — +0.005 trust per instance

## Testing

```bash
# Full suite
pytest tests/ -v

# With coverage
pytest tests/ --cov=plastic_promise --cov-report=term

# Rust tests
cargo test --manifest-path rust/context-engine-core/Cargo.toml
```

Tests are required for all new features and bug fixes. Coverage target: >= 80%.

## Questions?

Open an Issue using one of the templates, or check the [design specs](docs/superpowers/specs/) for architectural decisions.
````

- [ ] **Step 2: Verify file content**

```bash
grep -c "Branch Convention" CONTRIBUTING.md && grep -c "Commit Convention" CONTRIBUTING.md
```

Expected: `1` `1` (both sections exist).

- [ ] **Step 3: Commit**

```bash
git add CONTRIBUTING.md
git commit -m "docs: add CONTRIBUTING.md — setup, conventions, Hunter Guild, review guidelines"
```

---

### Task 7: Update CLAUDE.md with Git Governance Rules

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: Existing CLAUDE.md content
- Produces: New section "Git Governance" appended before "关键约定"

- [ ] **Step 1: Read current CLAUDE.md to find insertion point**

Open `CLAUDE.md` and locate the `## 关键约定` section. The new section will be inserted just before it.

- [ ] **Step 2: Insert Git Governance section before "关键约定"**

Add the following section before `## 关键约定`:

```markdown
## Git 治理规范 (Enterprise Git Governance)

本项目遵循 Plastic Promise Flow 企业级 Git 治理框架。完整规范见 [Enterprise Git Governance Spec](docs/superpowers/specs/2026-07-02-enterprise-git-governance-design.md)。

### 分支策略

| 前缀 | 用途 | 映射委托类型 |
|------|------|-------------|
| `feat/` | 新功能 | `build_*` |
| `fix/` | Bug 修复 | `fix_memory` / `fix_*` |
| `refactor/` | 重构（不改行为） | `refactor_*` |
| `docs/` | 文档 | `docs_*` |
| `perf/` | 性能优化 | `perf_*` |
| `chore/` | 构建/CI/工具 | `chore_*` |
| `worktree/<agent>/` | Agent 工作隔离 | — |

- `main` 为唯一长期分支，始终可部署
- 分支名全小写，`-` 分隔
- Agent 分支由 Daemon 自动生成: `<type>/<task_id>-<slug>`
- 合并使用 **Squash Merge**，保持线性历史
- 分支超过 7 天未合并 → Daemon 通知 → 24h 后自动删除 → 委托设为 abandoned → 信任分 -0.02

### 提交规范

所有 commit 必须遵循 Conventional Commits:

```
<type>(<scope>): <subject>
```

| Type | 用途 |
|------|------|
| `feat:` | 新功能 |
| `fix:` | Bug 修复 |
| `refactor:` | 重构 |
| `docs:` | 文档 |
| `perf:` | 性能 |
| `test:` | 测试 |
| `chore:` | 构建/CI/工具 |
| `revert:` | 回滚 |

- `scope` 可选，`subject` 英文小写开头，不加句号
- 每次提交应为逻辑完整的最小单元

### PR 流程

```
创建分支 → 开发 → 提交 → git push → 创建 PR
  → CI 自动运行 (P0: lint/test/security, P1: style/coverage)
  → Code Review (至少 1 人 approve)
  → Squash Merge → task_verify → 闭环
```

- PR 必须关联 Hunter Guild 委托 (task_id)
- CI P0 失败 → 阻止合并 → 自动生成 fix_ci 委托 (30分钟窗口)
- 审查评论分类: nit/design/blocking/praise，影响信任分

### 信任分全生命周期联动

| 事件 | 信任分变动 |
|------|-----------|
| 扫描器发现问题 | -0.01 ~ -0.03 (追溯责任人) |
| CI P0 失败 | -0.02 |
| CI P1 警告 | -0.005 |
| CI 全部通过 | +0.01 |
| PR 合并 | +0.02 |
| 审查打回 | -0.03 |
| 分支超时未合并 | -0.02 |
```

- [ ] **Step 3: Verify the new section exists**

```bash
grep -c "Git 治理规范" CLAUDE.md
```

Expected: `1`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add Git governance rules to CLAUDE.md — branch strategy, commits, PR flow, trust linkage"
```

---

### Task 8: Final Integration Verification

**Files:**
- Verify: all created files exist at correct paths

**Interfaces:**
- Consumes: all files from Tasks 1-7

- [ ] **Step 1: Verify all files exist**

```bash
echo "=== Checking all governance files ==="
for f in \
  .github/workflows/ci.yml \
  .github/PULL_REQUEST_TEMPLATE.md \
  .github/ISSUE_TEMPLATE/bug_report.md \
  .github/ISSUE_TEMPLATE/feature_request.md \
  .github/ISSUE_TEMPLATE/delegation.md \
  .github/ISSUE_TEMPLATE/config.yml \
  .github/CODEOWNERS \
  SECURITY.md \
  CONTRIBUTING.md; do
  if [ -f "$f" ]; then echo "OK: $f"; else echo "MISSING: $f"; fi
done
```

Expected: `OK` for all 9 files.

- [ ] **Step 2: Verify CLAUDE.md has governance section**

```bash
grep "Git 治理规范" CLAUDE.md && echo "OK: CLAUDE.md updated"
```

Expected: `OK: CLAUDE.md updated`

- [ ] **Step 3: Verify CI YAML is valid**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('OK: CI YAML valid')"
```

Expected: `OK: CI YAML valid`

- [ ] **Step 4: Final commit (if any outstanding changes)**

```bash
git status
```

If clean: no commit needed. If dirty: review and commit remaining changes.

- [ ] **Step 5: Push to GitHub**

```bash
git push origin main
```
