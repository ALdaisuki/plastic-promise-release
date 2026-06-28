# Plastic Promise — 约定工程统一架构

**日期**: 2026-06-29
**范式**: 约定工程 (Commitment Engineering) — 内化约定替代外部约束

---

## 一、范式定义

约定工程与约束工程的区别：

| | 约束工程 | 约定工程 |
|---|---------|---------|
| 核心力量 | 外部规则 + 门禁拦截 | 内在认同 + 决策参考 |
| Agent 行为 | 服从（被动） | 践行（主动） |
| 违反后果 | 拒绝执行 | 反事实预演 + 自省修正 |
| 信任模型 | 二元（信任/不信任） | 连续（动态信任分） |
| 成长路径 | 规则累积 | 约定内化（从记忆到本能） |

---

## 二、三层架构

```
约定层 (Convention) — "内化于心"
  ├── 12 条核心原则（激活带后果+建议）
  ├── 原则遗传（跨 Agent 单向扩散）
  └── 原则遵守量化追踪（每条原则被遵循的次数/趋势）

       ↓ 驱动

实践层 (Practice) — "外显于行"
  ├── 上下文自动注入（决策前查阅约定）
  ├── 反思/修复（违反约定 → 自省 → 修正）
  ├── 动态信任（遵守约定 → 信任↑ → 自主权↑）
  └── Issue 生命周期（约定 → 可追踪任务）

       ↓ 反馈

演化层 (Evolution) — "迭代进步"
  ├── 越用越聪明（worth_score 反馈闭环）
  ├── 越用越默契（Agent 行为模式学习）
  ├── 主动成长（curiosity → 探索 → 实践闭环）
  └── 内化约定（从"记住约定"到"活出约定"）

       ↓ 回流

约定层更新（原则遵守历史 + trust 调整 + worth 演化）
```

## 三、模块映射

| 当前模块 | 架构层 | Phase 1 改动 |
|---------|--------|-------------|
| `core/principles.py` | 约定层 | + `PrincipleTracker` 遵守记录 |
| `core/context_engine.py` | 实践层 | + 信任分权重接入 |
| `core/step_auditor.py` | 实践+演化 | + 修复建议生成 + 遵守记录 |
| `loop/soul_loop.py` | **全层枢纽** | post_task 六联闭环 |
| `defense/soul_enforcer.py` | 实践层 | + 信任→检索权重 |
| `defense/soul_audit.py` | 实践层 | + 修复动作执行 |
| `reflection/soul_scarf.py` | 演化层 | 接入 post_task |
| `reflection/soul_curiosity.py` | 演化层 | + 探索→实践闭环 |
| `growth/soul_hormone.py` | 演化层 | 接入 post_task |
| `memory/soul_memory.py` | 基础设施 | Phase 2 持久化 |

## 四、SoulLoop.post_task() 六联闭环

```
post_task(task_description, git_commit):
  1. 约定对齐检查
     for each activated principle:
       check alignment → record PrincipleTracker
       if violated → generate counterfactual + repair suggestion

  2. SCARF 五维自省
     scarf_reflect(task_description) → update Status/Certainty/Autonomy/Relatedness/Fairness

  3. 激素更新
     HormoneEngine.apply_feedback(alignment_result)
       adopted → dopamine↑ cortisol↓
       violation → cortisol↑ dopamine↓

  4. 信任联动
     if overall_score >= 0.80: TrustManager.boost(0.02)
     elif overall_score < 0.40: TrustManager.decay(0.02)
     → 更新 SoulEnforcer 约束衰减阈值

  5. 反思记忆存储 (已有)
     StepAuditor.audit_step() → lesson → memory_store(type="reflection")

  6. CEI 更新
     CEI = weighted_average(原则遵守率, SCARF评分, 信任分, worth趋势)
     → 记录 CEI 历史趋势
```

## 五、Phase 1 实现范围

1. **PrincipleTracker** — 原则遵守计数器（`core/principles.py`）
2. **post_task 六联闭环** — 填充 `loop/soul_loop.py` 空壳
3. **信任→检索权重** — TrustManager.tier → `_text_retrieval` boost
4. **修复建议生成** — `step_auditor.py` 发现低分→自动建议

## 六、后续 Phase

- **Phase 2**: SQLite 持久化, Issue 生命周期, 依赖关系
- **Phase 3**: worth 反馈闭环, 行为模式学习, curiosity 闭环, 原则遵守历史
- **Phase 4**: 上下文 hook, Bridge TODO, SSE 生产化
