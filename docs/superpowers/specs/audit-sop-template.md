# SuperPowers 审计标准操作程序 (Audit SOP)

**Date**: 2026-07-03
**Status**: Template — reusable SOP
**Version**: 1.0.0

> **定位**: 人工执行的标准操作程序。在 `receiving-code-review` 和 `verification-before-completion` 阶段之间执行。
> **触发**: PR 进入 "ready for review" 状态时。
> **执行者**: Claude（审查者角色）。
> **适用范围**: 所有 Plastic Promise PR。低风险执行简化版（5项），高风险执行完整版（10项 + audit_run）。

---

## 模板版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-07-03 | 初始版本——四阶段审计流程、双轨模型、信任分联动规则 |

---

## 一、整体评价矩阵

| 维度 | 检查项 | 通过标准 |
|------|--------|---------|
| 流程完整性 | 四阶段覆盖（审查 → 清理 → 验证 → 闭环） | 无跳步 |
| 信任分联动 | 定义了明确的规则表，可执行 | 所有触发条件有对应 delta |
| 清理前验证 | 三步验证（已合并、已推送、无脏文件） | 全部通过才清理 |
| 工作树清单 | 明确区分"待清理"和"保留" | 每项有理由 |
| 验收标准 | 成功标准清晰可验证 | 非主观判断 |

## 二、逐阶段审计

### Phase 1-2: receiving-code-review

| 检查项 | 通过标准 | 常见问题 |
|--------|---------|---------|
| 本地 `git diff` 审查 | diff 无意外文件变更 | stray changes 混入 |
| 线上 `gh pr view` | 含 inline comments | 空白审查 |
| 信任分联动规则 | 清晰定义 delta 表 | 规则缺失 |
| PR 审查顺序 | 小改动优先，大改动后审 | 未指定顺序 |
| PR 依赖关系 | 明确标注有无依赖 | 隐藏依赖导致冲突 |

**审查顺序规则**:
- 独立的 → 小 PR 先审（更快反馈）
- 有依赖的 → 被依赖 PR 先审（先验证基础）

**信任分 Delta 规则（累加制）**:
| 触发条件 | Delta | 说明 |
|---------|-------|------|
| CI 全部通过 | +0.01 | 自动化验证通过 |
| 审查通过无 blocking | +0.02 | 人工审查通过 |
| Minor 建议 | 0.00 | 不影响 |
| Design 建议 | 0.00 | 不影响 |
| Blocking 问题 | -0.03 | 需修复后重审 |
| PR 被拒绝 (CLOSE/REQUEST CHANGES) | -0.05 | 最严重——连修复机会都没给 |

**审查失败阻塞策略**:
- Blocking 问题 → **Phase 3 暂缓**，直到修复
- Nit/design 建议 → Phase 3 继续

### Phase 3: finishing-a-development-branch

| 检查项 | 通过标准 | 常见问题 |
|--------|---------|---------|
| 三重验收 | skill 链 + 记忆质量 + 经验包 | 跳步 |
| 前置验证 | 已合并 + 已推送 + 无脏文件 | 未推送就清理 |
| 清理动作 | `git worktree remove` + 远程分支删除 | 只删本地 |
| 失败跳过 | 单独报告，不阻塞 | 静默失败 |

**Worktree 清理 Pre-flight Checks**:
```bash
# 对每个待清理 worktree:
# 1. 本地分支已合并到 main
git branch --merged main | grep <branch>

# 2. 远程分支存在
git branch -r | grep origin/<branch>

# 3. 无脏文件
git -C <worktree_path> status --short  # 应无输出

# 特殊处理: 远程分支已删除但本地已合并 → 允许删除
if [ -z "$(git branch -r | grep origin/$branch)" ] && \
   [ "$(git branch --merged main | grep $branch)" ]; then
    # 远程分支已被 GitHub 自动删除，本地 worktree 可以安全清理
fi
```

**Worktree Inventory 模板**:
```
待清理:
  worktree-<name>    <path>    merged ✅    remote ✅    dirty ❌  → 删除

保留:
  worktree-<name>    <path>    远程分支已删除 ⚠️    → 确认是否仍需保留
  worktree-<name>    <path>    活跃开发中 ✅         → 保留
```

### Phase 4: verification-before-completion

| 检查项 | 通过标准 | 常见问题 |
|--------|---------|---------|
| Worktree list 验证 | 仅剩活跃工作树 | 僵尸 worktree 残留 |
| PR 审查结论落地 | 评论/approve/request changes | 审查结果未记录 |
| Step-closure 闭环 | mode=full 含四字段反思 | 空反思或模板填充 |

**四字段反思质量检查**:
- `lesson`: 必须是本次具体学到的内容，不能是泛泛的"代码审查很重要"
- `improvement`: 必须可执行，有具体动作动词
- `root_cause`: 必须追溯到根本，不是表面现象
- `optimization`: 必须在下一个迭代中可落地

## 三、执行检查清单

执行审计前，逐项确认：

- [ ] 所有 PR 的 CI 状态是绿色 (✅)
- [ ] 所有 PR 无 merge conflict
- [ ] AUDIT PENDING 标记的含义已明确
- [ ] PR 审查顺序已确定（小→大 或 依赖顺序）
- [ ] Worktree list 已刷新 (`git worktree list`)
- [ ] 保留 worktree 的远程分支仍然存在
- [ ] 信任分 Delta 规则已确认（累加制）
- [ ] 审查失败阻塞策略已确认

## 四、审计报告模板

```markdown
# Audit Report — YYYY-MM-DD

**Date**: YYYY-MM-DD HH:MM
**Auditor**: [审查者名称]
**Risk Level**: low | high
**PR**: #[N] [标题]
**Audit Version**: [SOP 模板版本号]

## 审查范围
- PR #N: <标题> (<files_changed> 文件, <lines_changed> 行)
- PR #M: <标题> (<files_changed> 文件, <lines_changed> 行)

## 逐阶段结果

### Phase 1-2: Code Review
| PR | 状态 | Blocking | Nit | Trust Delta |
|----|------|----------|-----|-------------|
| #N | ✅ | 0 | 2 | +0.03 |
| #M | ⚠️ | 1 | 3 | -0.03 → 修复后重审 |

### Phase 3: Branch Cleanup
- 待清理: <N> 个 worktree
- 保留: <M> 个 worktree
- 清理结果: <N> 成功, 0 失败

### Phase 4: Verification
- Worktree list: 仅剩 <M> 个活跃
- Step-closure: ✅

## 信任分汇总
| Target | Delta | 原因 |
|--------|-------|------|
| default | +0.03 | 审查通过 + CI 通过 |
| pi_builder | -0.03 | blocking 问题 |

## 遗留问题
- [ ] <如果有，列出来>
```
