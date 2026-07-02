# PR Audit + Worktree Merge — Design Spec

**Date:** 2026-07-03
**Branch:** `fix/hunter-guild-stats-ui`
**Scope:** 审计 PR #16 + PR #15，清理 5 个已合并工作树
**Flow:** SuperPowers 完整四阶段流水线

## Context

当前仓库有 10 个 git worktree，其中 5 个已合并到 main 但未清理。同时有 2 个 OPEN PR 需要审查：
- PR #16 (`fix/hunter-guild-stats-ui`): Hunter Guild + Stats + UI 综合修复
- PR #15 (`feat/vertical-slice-7-units`): 7-unit vertical slice，标记 AUDIT PENDING

## Architecture

```
Phase 1: receiving-code-review (PR #16)
  ├── 本地: git diff main..HEAD
  ├── 线上: gh pr view 16 + 审查评论
  └── 信任分联动

Phase 2: receiving-code-review (PR #15)
  ├── 本地: git diff main..feat/vertical-slice-7-units
  ├── 线上: gh pr view 15 + AUDIT PENDING 决议
  └── 信任分联动

Phase 3: finishing-a-development-branch (5 merged worktrees)
  ├── 三重验收: skill链 + 记忆质量 + 经验包
  ├── 前置验证: 每分支确认已合并/已推送/无脏文件
  ├── 清理: git worktree remove + 远程分支删除
  └── 失败分支跳过，单独报告

Phase 4: verification-before-completion
  ├── git worktree list 验证
  ├── PR 审查结论落地确认
  └── step-closure 闭环
```

## Trust Score Linkage Rules

审查阶段的信任分联动规则：

| Trigger | Action | Delta |
|---------|--------|-------|
| 审查发现 blocking 问题 | `defense(action="adjust", delta=-0.03)` | -0.03 |
| 审查通过无 blocking 问题 | `defense(action="adjust", delta=+0.02)` | +0.02 |
| 发现 nit/design 建议 | 不调整信任分，记录到审查评论 | 0 |
| CI 检查全部通过 | `defense(action="adjust", delta=+0.01)` | +0.01 |

## Worktree Cleanup Pre-flight Checks

每个待删除工作树在删除前执行三步验证：

1. **已合并检查**: `git branch --merged main` 包含该分支
2. **远程已推送**: `git branch -r` 包含 `origin/<branch>`
3. **无脏文件**: worktree 目录下 `git status --porcelain` 为空

验证失败 → 跳过该 worktree，记录原因，继续处理下一个。

## Worktree Inventory

### To Clean (merged to main → safe to remove)

| Branch | Worktree Path | Remote |
|--------|--------------|--------|
| `worktree-fix+data-quality-chain` | `.claude/worktrees/fix+data-quality-chain` | origin push done |
| `worktree-hunter-guild-dispatch` | `.claude/worktrees/hunter-guild-dispatch` | local only |
| `worktree-one-click-launcher` | `.claude/worktrees/one-click-launcher` | origin push done |
| `worktree-recall-quality-fix` | `.claude/worktrees/recall-quality-fix` | origin push done |
| `worktree-scheduler-health-meta-audit` | `.claude/worktrees/scheduler-health-meta-audit` | origin push done |

### Keep (not merged to main → preserve)

| Branch | Worktree Path |
|--------|--------------|
| `worktree-feat+code-memory-plugin` | `.claude/worktrees/feat+code-memory-plugin` |
| `worktree-feat+exemplar-driven-development` | `.claude/worktrees/feat+exemplar-driven-development` |
| `worktree-feat+vertical-slice-8-units` | `.claude/worktrees/feat+vertical-slice-8-units` |
| `worktree-rust-engine-phase2` | `.claude/worktrees/rust-engine-phase2` |

## Final Verification Checklist

Phase 4 完成前必须全部通过：

- [ ] `git worktree list` 仅剩活跃工作树（5 个保留 + 当前工作树）
- [ ] PR #16 审查结论已落地（评论 / approve / request changes）
- [ ] PR #15 AUDIT PENDING 已决议
- [ ] 远程已合并分支已清理
- [ ] `step-closure` 已执行（mode=full，含四字段反思）

## Success Criteria

1. 2 个 OPEN PR 均完成审查并产出结论
2. 5 个已合并工作树全部清理（本地 + 远程）
3. 信任分根据审查结果正确调整
4. 所有步骤有 git trace + step-closure 闭环
5. 工作树列表干净，仅保留活跃开发分支
