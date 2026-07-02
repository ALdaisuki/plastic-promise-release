---
name: superpowers-worktrees-not-optional
description: Using-git-worktrees is mandatory in SuperPowers flow — skipping it leads to PR creation failure
metadata:
  type: experience
---

SuperPowers 流程必须走完完整链：brainstorming → exemplar-research → using-git-worktrees → writing-plans。worktrees 是强制必经阶段，跳过会导致直接在 main 上开发，无法创建 PR（GH 报 "No commits between main and feature-branch"）。越急越要走流程，捷径反而浪费时间。

**Why:** 2026-07-03 session-init 优化任务跳过了 using-git-worktrees，17 commits 直接落在 main 上，最后无法创建 PR，只能接受直接提交。

**How to apply:** 每次 SuperPowers 流程开始前，检查 SKILL_CHAIN_MAP 确认 using-git-worktrees 是 writing-plans 的前置依赖。不要在 main 上做任何实质性开发。
