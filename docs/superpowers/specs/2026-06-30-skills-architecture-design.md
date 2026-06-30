# Plastic Promise Skills 模型 — 原子 MCP + 高层 Skill 组合架构

> **状态**: 设计完成，待评审
> **日期**: 2026-06-30
> **核心范式**: 约定工程 — 程序化技能优先，Superpowers 清单互补

## 一、设计目标

将 34 个原子 MCP 工具组合为 8 域高层技能，实现：

1. **程序化优先**: Python 函数组合 MCP 原子，Pi Agent 可确定性调用
2. **Superpowers 互补**: Markdown 清单引导 Claude 灵活使用
3. **P0/P1/P2 分层**: 基于"影响 × 频率"的工具暴露规则
4. **多 Agent 协作**: 技能跨 Agent 共享，信任分驱动权限
5. **自演化闭环**: 每次技能执行产生反馈，驱动系统自我改进

## 二、总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                    调用层 (Invocation Layer)                      │
│  Claude Code    │    Pi Agent (CLI)    │    Daemon (cron/定时)   │
│  Skill Tool     │    SkillEngine.exec  │    SkillEngine.exec     │
└────────────────────────┬─────────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────────┐
│                 编排层 (Orchestration Layer)                      │
│  SkillEngine — 注册 / 执行 / 降级 / 审计 / 重试                  │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────┐       │
│  │ Registry │ Executor │ Degrader │ Auditor  │ Scheduler│       │
│  │ 技能注册  │ 调用链   │ 优雅降级  │ 执行审计  │ P2 调度  │       │
│  └──────────┴──────────┴──────────┴──────────┴──────────┘       │
└────────────────────────┬─────────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────────┐
│               程序化技能层 (Programmatic Skills — 8 域)           │
│  plastic_promise/skills/                                         │
│  ┌──────────┬──────────┬──────────┬──────────┐                  │
│  │ Session  │ Memory   │ Audit    │ Collab   │                  │
│  │ Lifecycle│ Ops      │ &Comply  │ &Delegate│                  │
│  ├──────────┼──────────┼──────────┼──────────┤                  │
│  │ Knowledge│ System   │ Principles│ Self    │                  │
│  │ Pack     │ Health   │ &Gov     │ Evolution│                  │
│  └──────────┴──────────┴──────────┴──────────┘                  │
└────────────────────────┬─────────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────────┐
│             Superpowers 清单层 (Human-AI 柔性接口)                │
│  .claude/skills/plastic-promise/*.md                             │
│  → 引导 Claude 何时调用哪个程序化技能                             │
└────────────────────────┬─────────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────────┐
│              原子 MCP 工具层 (34 tools, 3 tiers)                  │
│  P0 (9):  store │ recall │ supply │ inject │ principle │ ...    │
│  P1 (14): domain │ audit │ pack │ trace │ issue │ ...           │
│  P2 (8):  gc │ backup │ migrate │ audit-gap │ ...              │
└──────────────────────────────────────────────────────────────────┘
```

## 三、MCP 工具优先级 (影响 × 频率)

### P0 — 基础层 (9 个)

所有技能直接调用，失败必须有完整错误处理。失败影响系统可用性。

| 工具 | 频率 | 失败影响 | 典型调用者 |
|------|------|----------|-----------|
| `memory_store` | 极高 | 系统不可用 — 数据无法持久化 | 所有写技能 |
| `memory_recall` | 极高 | 无上下文 — Agent 失去记忆 | 所有读技能 |
| `context_supply` | 高 | 任务无法执行 — 上下文枯竭 | 所有决策技能 |
| `auto_context_inject` | 高 | 上下文+追踪同时失效 | Session Lifecycle |
| `principle_activate` | 高 | 行为失范 — 无原则引导 | 决策前必须调用 |
| `skill_session_start` | 高 | 可观测性缺失 — 技能链断裂 | 引擎自动包裹 |
| `skill_session_complete` | 高 | 同上 | 引擎自动包裹 |
| `memory_stats` | 中 | 诊断失效 — 无法判断系统健康 | System Health |
| `defense` | 中 | 权限判断失效 — 无法决定是否操作 | 写操作前 |

### P1 — 业务层 (14 个)

特定域技能使用，影响操作结果而非系统可用性。失败可降级。

| 工具 | 频率 | 失败影响 | 降级策略 |
|------|------|----------|---------|
| `domain` | 中 | 域分配错误 | `skip` — 使用缓存 |
| `audit_run` | 中 | 审计缺失 | `warn` — 记录跳过 |
| `scarf_reflect` | 中 | 自省缺失 | `warn` — 不影响执行 |
| `pack_export` | 低 | 无法导出 | `warn` — 延后导出 |
| `pack_import` | 低 | 无法导入 | `abort` — 数据完整性 |
| `skill_session_trace` | 中 | 链可见性缺失 | `warn` — 审计降级 |
| `issue_create` | 中 | 协作中断 | `abort` — 任务无法追踪 |
| `issue_transition` | 中 | 状态不同步 | `warn` — 手动修复 |
| `issue_list` | 低 | 视图缺失 | `skip` |
| `memory_update` | 低 | 更新失败 | `abort` — 数据完整性 |
| `memory_forget` | 低 | 删除失败 | `abort` — 数据安全 |
| `memory_correct` | 低 | 纠正失败 | `warn` — 保留原值 |
| `principle_inherit` | 低 | 原则传播中断 | `warn` |
| `principle_diffuse` | 低 | 传播状态未知 | `skip` |
| `principle_evaluate` | 低 | 反事实评估缺失 | `skip` |
| `context_inject` | 中 | 图谱更新失败 | `warn` |
| `context_graph` | 低 | 图谱查询失败 | `skip` |
| `context_ready` | 低 | 缓存未命中 | `fallback:context_supply` |
| `feedback_apply` | 中 | 反馈丢失 | `warn` — worth 不更新 |

### P2 — 管理层 (8 个)

由守护进程或管理员调用，不暴露给普通技能。低频但高影响。

| 工具 | 频率 | 失败影响 | 暴露规则 |
|------|------|----------|---------|
| `memory_gc` | 极低 | 数据积累/误删 | **仅 Daemon 定时调用** |
| `system(backup)` | 极低 | 备份缺失 | **仅管理员调用** |
| `system(migrate)` | 极低 | 数据丢失 | **仅管理员调用，需确认** |
| `skill_session_audit` | 低 | 合规报告缺陷 | **仅 Daemon 定时调用** |
| `pack_recall` | 低 | 严格检索失败 | Knowledge Pack 技能 |
| `audit_pre_check` | 低 | 危险操作漏检 | 写操作前自动 (引擎包裹) |
| `issue_create` | — | — | (已列入 P1) |
| `system(stats)` | 中 | — | (已列入 P1 统计场景) |

## 四、SkillEngine 编排核心

### 4.1 数据结构

```python
# plastic_promise/skills/engine.py

from dataclasses import dataclass, field
from typing import Callable, Any

@dataclass
class SkillDef:
    name: str                          # "session-init"
    domain: str                        # "session_lifecycle"
    description: str                   # 一句话描述
    tier: str                          # "P0" | "P1" | "P2"

    # P0/P1 原子依赖 — Engine 按拓扑序调用
    atoms: list[str]                   # ["auto_context_inject", "domain", ...]

    # 降级映射: 原子工具名 → 降级行为
    degrade_map: dict[str, str]        # {"domain": "skip", "memory_gc": "warn"}

    # 核心处理函数
    handler: Callable                  # async (ctx, params, atom_results) -> SkillResult

    # 调用权限
    allowed_callers: list[str]         # ["claude", "pi", "daemon"]

    # 多 Agent 相关
    cross_agent: bool = False          # 是否涉及多 Agent 协作
    trust_required: float = 0.0        # 最低信任分 (0.0 = 无限制)

@dataclass
class SkillResult:
    skill_name: str
    success: bool
    data: dict
    atom_results: dict[str, Any]       # 每个原子的执行结果
    degrade_log: list[str]             # 降级记录
    audit_trail: dict                   # skill_session entity_id + duration
    errors: list[str]
```

### 4.2 核心 API

```python
class SkillEngine:
    """程序化技能编排引擎。

    职责:
    1. 技能注册 — 按 8 域组织，声明依赖的 P0/P1 工具
    2. 执行链 — P0 原子 → 业务逻辑 → 审计记录
    3. 降级路径 — 当某个工具不可用时，按 degrade_map 降级
    4. 审计追踪 — 每次技能执行自动包裹 skill_session_start/complete
    5. P2 调度 — memory_gc/system backup 等仅通过定时器触发
    """

    def __init__(self, engine: ContextEngine):
        self._ctx = engine
        self._registry: dict[str, SkillDef] = {}

    def register(self, skill_def: SkillDef) -> None:
        """注册一个技能定义到对应域。"""

    async def exec(self, skill_name: str, params: dict = None,
                   caller: str = "claude") -> SkillResult:
        """执行一个技能。

        执行流程:
        1. 查找 SkillDef，验证 caller 在 allowed_callers 中
        2. skill_session_start(entity_id) — 创建追踪实体
        3. 按 atoms 列表顺序调用 P0/P1 工具
        4. 每个原子调用包裹 try/except → 按 degrade_map 降级
        5. 调用 SkillDef.handler(ctx, params, atom_results)
        6. skill_session_complete(entity_id) — 标记完成
        7. 返回 SkillResult (含 audit_trail)
        """

    async def exec_chain(self, skill_names: list[str],
                         params: dict = None) -> list[SkillResult]:
        """按依赖顺序执行技能链。使用 SKILL_CHAIN_MAP 约束顺序。"""

    def schedule(self, skill_name: str, cron_expr: str) -> str:
        """为 P2 技能注册定时调度。返回 job_id。
        仅在 skill_name 的 tier == "P2" 时允许。
        """
```

### 4.3 降级行为定义

| 降级行为 | 含义 | 适用场景 |
|---------|------|---------|
| `"skip"` | 跳过该原子，继续执行 | 非关键工具（如 `domain stats`） |
| `"warn"` | 跳过但记录警告到 degrade_log | 中等影响工具 |
| `"abort"` | 中止技能，返回错误 | P0 核心工具失败 |
| `"fallback:<tool>"` | 使用替代工具或降级路径 | 有替代路径时 |

### 4.4 执行链路追踪

每次 `SkillEngine.exec()` 自动生成追踪数据：

```python
# 引擎自动执行 (对技能开发者透明)
entity_id = await skill_session_start(skill_name, task_description)
try:
    for atom in skill_def.atoms:
        try:
            atom_result = await call_atom(atom, params)
        except AtomError as e:
            action = skill_def.degrade_map.get(atom, "abort")
            if action == "abort": raise
            elif action == "skip": continue
            elif action == "warn": degrade_log.append(...)
            elif action.startswith("fallback:"): ...

    result = await skill_def.handler(ctx, params, atom_results)
finally:
    await skill_session_complete(entity_id, outcome=...)
```

## 五、8 域技能目录

### 域 1: Session Lifecycle (会话生命周期)

**文件**: `plastic_promise/skills/session_lifecycle.py`

| 技能 | 原子依赖 | 说明 | 调用者 |
|------|---------|------|--------|
| `session-init` | `auto_context_inject` → `domain(stats)` → `system(stats)` → `defense(get)` → `memory_gc(dry_run)` | CLAUDE.md 步骤 0-5 的完整封装 | claude, pi |
| `session-close` | `skill_session_trace`(branch) → `memory_gc(dry_run)` → `pack_export` | 会话结束，导出经验包 | claude |

**`session-init` 执行链**:

```
1. auto_context_inject(task_description, source="claude_code")
   → entity_id, context_pack, activated_principles, inject_memory_id
2. domain(action="stats")
   → 域联邦健康度 + 当前活跃域
3. system(action="stats")
   → 记忆池总量 + 衰减分布 + fuzzy buffer 积压
4. defense(action="get")
   → 信任分 + 防线状态
5. memory_gc(dry_run=True)
   → 衰减记忆预览 + 合并候选

降级路径:
- MCP 服务器离线 → fallback: 文件系统降级 (写 .md + [[pending-sync]])
- domain 失败 → skip
- system 失败 → skip (不影响核心功能)
- defense 失败 → fallback:readonly (假设最低信任分)
- memory_gc 失败 → skip
```

### 域 2: Memory Operations (记忆操作)

**文件**: `plastic_promise/skills/memory_operations.py`

| 技能 | 原子依赖 | 说明 |
|------|---------|------|
| `smart-remember` | `memory_recall`(去重检查) → `memory_store` | 记忆前自动去重 + 质量门控 |
| `context-aware-recall` | `context_supply` → `memory_recall`(补充) → `principle_activate` | 上下文驱动检索 + 原则对齐 |
| `correct-memory` | `memory_recall`(找到目标) → `memory_correct` → `feedback_apply` | 人类纠正 + 自演化触发 |
| `forget-memory` | `memory_recall`(确认存在) → `defense(get)` → `memory_forget` | 删除前权限检查 |

**`smart-remember` 流程**:

```
输入: content, memory_type, source
  1. principle_activate(task_type)              ← 激活相关原则
  2. memory_recall(query=content, max_results=5) ← 去重检查
  3a. 已有相似 (cos ≥ 0.85) → memory_update(已有ID) ← 强化而非重复
  3b. 无重复 → memory_store(content, tags)         ← 完整质量管道
  4. 返回 {stored/updated, memory_id, pipeline_stats}
```

### 域 3: Audit & Compliance (审计与合规)

**文件**: `plastic_promise/skills/audit_compliance.py`

| 技能 | 原子依赖 | 说明 |
|------|---------|------|
| `pre-commit-audit` | `audit_pre_check` → `skill_session_trace`(branch) | 提交前危险检查 + 链完整性 |
| `branch-closure-check` | `skill_session_trace`(branch) → `memory_gc`(dry_run) → `pack_export` | 分支完成前三重验收 |
| `full-audit` | `audit_run`(full) → `memory_stats` → `defense(get)` | 完整 7 维审计 + 报告 |

**`branch-closure-check` — 对应 CLAUDE.md 三重验收**:

```
1. skill_session_trace(session_scope="branch")
   → 验收: chain_complete=true, gaps=[], chain_valid=true
   → 链首: brainstorming|systematic-debugging|requesting-code-review
   → 链尾: finishing-a-development-branch|receiving-code-review
2. memory_gc(dry_run=True)
   → 验收: merge.candidates_found 合理, candidates_count 无异常增长
3. pack_export(name="<feature>-<date>", tags=["domain:<域>", "task:done"])
   → 导出到 experience_packs/
```

### 域 4: Collaboration & Delegation (协作与委派)

**文件**: `plastic_promise/skills/collaboration.py`

| 技能 | 原子依赖 | 说明 |
|------|---------|------|
| `delegate-to-pi` | `defense(get)` → `memory_store`(task:pending) → `issue_create` | Claude 委派任务 |
| `review-and-accept` | `skill_session_trace` → `defense(adjust,+0.02)` → `feedback_apply`(adopted) → `memory_store`(task:reviewed) | Claude 验收 |
| `reject-and-reassign` | `memory_store`(task:rejected) → `feedback_apply`(rejected) → `defense(adjust,-0.01)` | 打回重做 |
| `claim-task` | `memory_recall`(task:pending) → `memory_store`(task:accepted) → `issue_transition` | Pi/Daemon 认领 |

**`delegate-to-pi` 流程 (对齐现有标签状态机)**:

```
输入: spec, assignee, domain, principle_id
  1. defense(get) → 检查调用者信任分 ≥ 0.60
  2. memory_store(
       content="SPEC: {spec}",
       tags=["task:pending", "assignee:{assignee}", "domain:{domain}"]
     )
  3. issue_create(title, principle_id, blocked_by=[...])
  4. 返回 {memory_id, issue_id}
  → Daemon 自动检测 task:pending → spawn Pi → 开始执行
```

### 域 5: Knowledge Packaging (知识打包)

**文件**: `plastic_promise/skills/knowledge_pack.py`

| 技能 | 原子依赖 | 说明 |
|------|---------|------|
| `export-experience` | `memory_list`(筛选) → `pack_export`(streaming) | 按 domain+tags 导出 |
| `import-and-merge` | `pack_import`(merge) → `memory_stats`(验证) | 导入外部经验包 |
| `strict-recall` | `pack_recall`(strict=true) | 严格模式，不编造 |

### 域 6: System Health (系统健康)

**文件**: `plastic_promise/skills/system_health.py`

| 技能 | 原子依赖 | 说明 | 调用者 |
|------|---------|------|--------|
| `health-check` | `memory_stats` → `domain(stats)` → `system(stats)` → `defense(status)` | 完整健康快照 | claude, pi, daemon |
| `scheduled-gc` | `memory_gc`(dry_run=false) → `system(stats)` | **仅 Daemon 定时调用** | daemon |
| `system-backup` | `system(backup)` | **仅管理员调用** | admin |

**P2 调度器**:

```python
# plastic_promise/cron/skill_scheduler.py
class SkillScheduler:
    """P2 技能定时调度器 — 替代直接暴露危险工具"""

    async def start(self):
        self._engine.schedule("scheduled-gc", "3 3 * * 0")     # 每周日 03:03
        self._engine.schedule("health-check", "7 9 * * *")     # 每天 09:07
        self._engine.schedule("system-backup", "17 2 * * 0")   # 每周日 02:17
```

### 域 7: Principles & Governance (原则与治理)

**文件**: `plastic_promise/skills/principles_gov.py`

| 技能 | 原子依赖 | 说明 |
|------|---------|------|
| `align-principles` | `principle_activate`(task_type) → `principle_evaluate`(反事实) | 决策前原则对齐 |
| `inherit-work-principles` | `principle_inherit`(work→all) | 工作域原则跨域遗传 |
| `governance-check` | `principle_diffuse` → `defense(get)` → `skill_session_trace` | 治理全景快照 |
| `evaluate-violation` | `principle_evaluate`(principle_id, scenario) | 单条原则反事实评估 |

**`align-principles` 流程**:

```
输入: task_description, task_type
  1. principle_activate(task_type, task_description)
     → {activated: [{id, name, content, consequence, recommendation}], count}
  2. 对每条激活原则:
     principle_evaluate(principle_id, scenario=task_description)
     → "如果违反原则X，在当前场景下会怎样？"
  3. 返回 {principles, counterfactuals, recommendation_summary}
```

### 域 8: Self Evolution (自演化闭环)

**文件**: `plastic_promise/skills/self_evolution.py`

| 技能 | 原子依赖 | 说明 |
|------|---------|------|
| `evolve-from-feedback` | `skill_session_trace` → `audit_run`(report) → `scarf_reflect` → `defense(adjust)` → `memory_store`(演化记录) | 完整六联闭环 |
| `optimize-skill-chain` | `skill_session_trace`(all) → `memory_gc`(dry_run) → `memory_store`(优化建议) | 技能链瓶颈分析 |
| `close-the-loop` | `audit_run`(report) → `memory_correct`(自动) → `principle_inherit` | 审计→修正→传播 |

**`evolve-from-feedback` — 封装现有 `SoulLoop.post_task()`**:

```
输入: task_description, git_commit, mode ("light"|"full")
  1. alignment: principle_activate + PrincipleTracker.record
  2. scarf: SCARFReflector.reflect(task_description) → 五维评分
  3. hormone: HormoneEngine.apply_feedback(adopted/ignored/rejected)
  4. trust: TrustManager → SCARF≥0.80 → boost(+0.02) | <0.40 → decay(-0.02)
  5. reflection: StepAuditor.audit_step → memory_store
  6. cei: 复合执行指数 (feedback_closure 0.15 + trust_alignment 0.10 + ...)
  7. 返回 {alignment, scarf, hormone, trust, reflection, cei, repairs}
```

## 六、技能间依赖关系

```
session-init ─────────────────────────────────────────────┐
    │                                                      │
    ▼                                                      │
align-principles ←── (决策前调用) ─────────────────────────┤
    │                                                      │
    ├──→ smart-remember ───→ context-aware-recall          │
    ├──→ delegate-to-pi ───→ review-and-accept             │
    ├──→ pre-commit-audit ─→ branch-closure-check          │
    ├──→ export-experience ─→ import-and-merge             │
    │                                                      │
    ▼                                                      │
evolve-from-feedback ←── (每步完成后)                      │
    │                                                      │
    ├──→ scarf_reflect → defense(adjust) → memory_store    │
    │                                                      │
    ▼                                                      │
health-check ──→ scheduled-gc (daemon only)                │
    │                                                      │
    ▼                                                      │
session-close ──→ pack_export ────────────────────────────┘
```

## 七、Superpowers 清单格式

每个程序化技能对应一个 markdown 文件，放在 `.claude/skills/plastic-promise/`：

```markdown
---
name: session-init
description: 会话启动 — 封装 CLAUDE.md 步骤 0-5
domain: session_lifecycle
tier: P0
programmatic_skill: session_lifecycle.session-init
allowed_callers: [claude, pi]
---

# Session Init

## 何时调用
每次 Claude Code 会话开始，在进行任何操作之前。

## 做什么
调用 `SkillEngine.exec("session-init")`，依次执行：
1. Server up check (MCP 9020 健康检查)
2. auto_context_inject → 原则激活 + 记忆召回 + 实体追踪
3. domain(stats) → 域联邦健康度
4. system(stats) → 记忆池总量 + fuzzy buffer
5. defense(get) → 信任分 + 防线状态
6. memory_gc(dry_run) → 衰减预览

## 降级路径
- MCP 服务器不可用 → 文件系统降级 (写 .md + [[pending-sync]])
- domain 失败 → skip，使用上次缓存
- system 失败 → skip
- defense 失败 → fallback:readonly

## 对应程序化技能
`plastic_promise/skills/session_lifecycle.py::skill_session_init`
```

## 八、错误处理与降级矩阵

| 场景 | 降级策略 | 技能行为 |
|------|---------|---------|
| MCP 服务器 9020 离线 | `abort` → `fallback:file` | 写 `.md` 文件 + `[[pending-sync]]` 标记 |
| `memory_recall` 超时 | `fallback:empty_context` | 返回空上下文包，继续执行 |
| `memory_store` 失败 | `abort` | 中止当前技能（数据完整性） |
| `domain(stats)` 失败 | `skip` | 跳过域统计，记录 degrade_log |
| `defense(get)` 失败 | `fallback:readonly` | 假设最低信任分 (readonly 模式) |
| `principle_activate` 失败 | `fallback:hardcoded` | 使用静态原则列表 [1,2,3,4] |
| `context_supply` 失败 | `fallback:text_search` | 降级为纯文本检索 (无向量) |
| `pack_export` 失败 | `warn` | 记录警告，延后导出 |
| `skill_session_trace` 失败 | `warn` | 审计降级，不影响主流程 |

## 九、与现有代码的集成点

| 现有组件 | 集成方式 | 变更程度 |
|---------|---------|---------|
| `auto_context_inject` | 成为 `session-init` 的第一个原子调用 | **零变更** — 直接复用 handler |
| `SoulLoop.post_task()` | 成为 `evolve-from-feedback` 的 handler | 包装为 SkillDef，不改内部逻辑 |
| `SKILL_CHAIN_MAP` | `SkillEngine.exec_chain()` 使用 | 扩展为包含 8 域技能链定义 |
| `skill_session_start/complete` | SkillEngine 自动包裹 | **零变更** — 引擎层调用 |
| `pi_daemon.py` | 改为调用 `SkillEngine.exec("claim-task")` | 轻量 — daemon 调用技能而非直接操作标签 |
| CLAUDE.md 步骤 0-5 | 替换为 `SkillEngine.exec("session-init")` | 6 个 MCP 调用 → 1 个技能调用 |
| `handle_auto_context_inject` | 技能链的原型参考 | **零变更** — 已经是 4 步链模式 |

## 十、实现路线图

| 阶段 | 内容 | 产出 | 估时 |
|------|------|------|------|
| **Phase 1** | SkillEngine 核心 + `session-init` + `smart-remember` | `engine.py`, `session_lifecycle.py`, `memory_operations.py`(partial) | 2-3h |
| **Phase 2** | 域 4 (协作委派) + 域 7 (原则治理) | `collaboration.py`, `principles_gov.py` | 2h |
| **Phase 3** | 域 3 (审计合规) + 域 8 (自演化) | `audit_compliance.py`, `self_evolution.py` | 2h |
| **Phase 4** | 域 2 完整化 + 域 5 (知识打包) | `memory_operations.py`(full), `knowledge_pack.py` | 1.5h |
| **Phase 5** | 域 6 (系统健康) + P2 调度器 + Superpowers 清单 | `system_health.py`, `skill_scheduler.py`, 8 个 `.md` 清单 | 2h |
| **Phase 6** | pi_daemon 集成 + CLAUDE.md 更新 + 端到端测试 | 集成测试 + 文档 | 1.5h |

## 十一、设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 程序化 vs 声明式 | 程序化优先，Superpowers 互补 | Pi 需要确定性执行；Claude 需要柔性引导 |
| P2 工具暴露 | 仅通过 Daemon 调度器 | `memory_gc` 和 `system(migrate)` 高影响，直接暴露风险大 |
| 技能包裹 skill_session | 引擎自动包裹 | 零摩擦追踪，对齐现有 skill_auto_track hook |
| 降级策略 | 声明式 degrade_map | 每个技能定义自己的容忍度，引擎统一执行 |
| 错误模型 | 故障快速 + 部分降级 | P0 失败 = abort；P1 失败 = degrade；P2 失败 = daemon 重试 |
