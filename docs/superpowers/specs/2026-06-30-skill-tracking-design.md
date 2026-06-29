# Skill Tracking — SuperPowers 流程可追踪化

> 状态: 已确认 | 日期: 2026-06-30 | 方案: B (流程成为可追踪实体)

## 一、动机

SuperPowers 当前没有"执行实例"概念。每个 skill 通过 Skill 工具一次性加载，skill 之间靠约定串联（brainstorming → writing-plans → ...），但：

- **不可见**：Claude 做了几次 brainstorming？每次产出什么？有没有闭环？
- **不可审计**：跳过的 skill、重复执行的 skill、中断的调用链，全部透明
- **不可恢复**：会话中断后无法从上次 skill 状态继续

本设计将每个 skill 执行实例注册为记忆系统中的可追踪实体（entity_type="skill_session"），通过标签状态机追踪进度，使 Claude 的执行过程可审计、可恢复。

## 二、架构

```
SuperPowers Skill 调用 (不可修改的插件)
        │
        ▼
CLAUDE.md 指令层 (新增 "Skill 调用协议")
  ├─ skill_session_start()     ← 调用 Skill 前
  ├─ skill_session_complete()  ← Skill 完成/放弃时
  └─ skill_session_trace()     ← finishing-a-development-branch 前的验收
        │
        ▼
MCP 工具层 (plastic_promise/mcp/tools/skill_tracking.py 新增)
  ├─ skill_session_start      → 创建 task entity + 自动注入上下文
  ├─ skill_session_complete   → 标签转换 + worth_score 更新
  ├─ skill_session_trace      → 查询执行链 + 检测完整性
  └─ skill_session_audit      → 事后扫描缺口
        │
        ▼
现有基础设施 (全部复用，零新存储结构)
  ├─ ContextEngine.register_entity()   → 实体节点
  ├─ MemoryPipeline.store_urgent()     → 持久化
  ├─ Tag 状态机                        → task:pending → active → done
  ├─ audit_run (增强第八维)            → skill_trace 维度
  └─ DomainManager.assign()           → skill→domain 自动映射
```

## 三、数据模型

每个 skill 执行实例 = 一个 SkillSession 实体，复用现有 MemoryRecord + tag 系统：

```
SkillSession (通过标签和 entity_type 区分):
  entity_id:      "skill:brainstorming:2026-06-30T14:23:01.123456"
  entity_type:    "skill_session"
  skill_name:     "brainstorming"
  status:         "active" | "done" | "abandoned"
  parent_skill:   "skill:writing-plans:..." | null
  description:    "为 SuperPowers 记忆集成做脑暴"
  outcome:        "已确认采用方案3" | "still_in_progress" | null
  artifacts:      ["docs/specs/2026-06-30-skill-tracking-design.md"]
  domain:         "designing"              ← Skill→Domain 映射推导
  tags:           ["task:active", "skill:brainstorming", "domain:designing"]
  worth_score:    auto-updated on complete
```

**设计决策**：
- **不新建存储结构** — 复用 `MemoryRecord`，通过 `entity_type="skill_session"` 和 `skill:` 前缀标签区分
- **entity_id 格式** — `skill:<skill_name>:<ISO timestamp μs>`，全局唯一，可排序，可追溯
- **parent_skill** — 记录调用链父节点，格式为 entity_id 引用

## 四、MCP 工具 Schema

### 4.1 skill_session_start

```
参数:
  skill_name:        string (必填) — Skill 名称
  task_description:  string (必填) — 本次执行要做什么
  parent_entity_id:  string | null — 调用链中的父 skill entity_id
  estimated_duration_minutes: int | null — 预估耗时

返回值:
  {
    "entity_id":            "skill:brainstorming:2026-06-30T14:23:01.123456",
    "skill_name":           "brainstorming",
    "status":               "active",
    "domain":               "designing",
    "activated_principles": [{"id": 2, "name": "全过程可查可透明"}, ...],
    "related_memories":     ["mem_abc123", "mem_def456"],
    "tags_applied":         ["task:active", "skill:brainstorming", "domain:designing"],
    "chain_warning":        null | "parent 'X' is not a legal predecessor of 'brainstorming'. Expected one of: [null]"
  }
```

**内部步骤**：
1. `entity_id = f"skill:{skill_name}:{datetime.utcnow().isoformat()}"` (微秒精度)
2. Domain 推导: `domain = SKILL_DOMAIN_MAP[skill_name]`
3. 原则激活: `principle_activate(task_type=DOMAIN_TO_TASK_TYPE[domain], task_description=task_description)`
4. 记忆召回: `memory_recall(query=task_description, task_type=...)`
5. 图谱注入: `context_inject(entity_type="skill_session", entity_id=entity_id, entity_name=skill_name, entity_description=task_description, related_entities=parent_entity_id ? [parent_entity_id] : [])`
6. 持久化: `memory_store(content=f"[SKILL START] {skill_name}: {task_description}", memory_type="experience", source="superpowers", entity_ids=[entity_id], tags=["task:active", f"skill:{skill_name}", f"domain:{domain}"])`
7. **parent 校验**: 检查 parent_entity_id 对应的 skill_name 是否在 `SKILL_CHAIN_MAP` 中为此 skill 的合法前驱。不合法时返回 `chain_warning`，但不拒绝创建。
8. 返回聚合结果

### 4.2 skill_session_complete

```
参数:
  entity_id:  string (必填) — skill_session_start 返回的 ID
  outcome:    string (必填) — 结果摘要 (≤200 字) 或 "still_in_progress"
  artifacts:  string[] | [] — 输出文件路径

返回值:
  {
    "entity_id":              "skill:brainstorming:...",
    "skill_name":             "brainstorming",
    "status":                 "done" | "still_active",
    "duration_ms":            1234567,
    "outcome":                "...",
    "artifacts_registered":   ["docs/specs/2026-06-30-skill-tracking-design.md"],
    "next_skills":            ["writing-plans"],
    "worth_update":           {"previous": 0.70, "delta": +0.02, "new": 0.72},
    "tags_updated":           ["task:active→task:done"]
  }
```

**内部步骤**：
1. 查找 entity_id 对应的 MemoryRecord
2. 如果 outcome == "still_in_progress": 仅更新 `last_accessed = now`（重置孤儿计时器），status 保持 active。跳过后续步骤。
3. 否则: status 转为 done 或 abandoned（outcome 以 "abandoned:" 开头）
4. 标签转换: `task:active` → `task:done` (或 `task:abandoned`)
5. 更新 memory content: 追加 `[SKILL DONE] outcome: ... artifacts: [...]`
6. Artifact 关联: 为每个 artifact 调用 `memory_store(content="产出物: {path}", entity_ids=[entity_id], tags=["type:artifact"])`
7. worth_score 更新: 完成 +0.02（固定增量）。后续版本可根据 outcome 长度/artifact 数量加权。
8. 计算 duration_ms = completed_at - started_at
9. 推导 next_skills = SKILL_CHAIN_MAP[skill_name]
10. 返回结果

**"still_in_progress" 机制**：如果 skill 执行时间超过 30 分钟阈值，Claude 可调用 `skill_session_complete(entity_id, "still_in_progress")` 重置孤儿计时器。不改变 status，仅刷新 last_accessed。

### 4.3 skill_session_trace

```
参数:
  session_scope:  string — "current" (本次会话) | "branch" (当前 git 分支) | "all"
  skill_name:     string | null — 按 skill 名过滤
  status:         string | null — "active" | "done" | "abandoned"

返回值:
  {
    "sessions": [
      {
        "entity_id":       "skill:brainstorming:...",
        "skill_name":      "brainstorming",
        "status":          "done",
        "started_at":      "2026-06-30T14:23:01",
        "completed_at":    "2026-06-30T15:30:00",
        "duration_ms":     4019000,
        "description":     "...",
        "outcome":         "...",
        "parent_skill":    null,
        "child_skills":    ["skill:writing-plans:..."]
      }
    ],
    "chain_complete":  true | false,
    "chain_valid":     true | false,
    "gaps": [
      {"type": "orphan_active", "entity_id": "...", "idle_minutes": 45}
    ],
    "chain_warnings": [
      {"type": "chain_violation", "entity_id": "...", "detail": "writing-plans parent is X, expected brainstorming"}
    ]
  }
```

**内部步骤**：
1. 筛选 `entity_type="skill_session"` 的实体，按 scope 过滤
2. 按 parent_skill 重建调用树（有向图）
3. 完整性检测 (chain_complete): 每个 done/active 的 session 都有合法的子节点，除非是终端 skill
4. 合法性检测 (chain_valid): 所有 parent_skill 引用都符合 SKILL_CHAIN_MAP
5. Gaps 检测:
   - orphan_active: status=active 且 last_accessed > 30 分钟前
   - chain_broken: done 状态但非终端 skill 且无子节点
   - tag_mismatch: status 与 task: 标签不一致

### 4.4 skill_session_audit (事后补全)

```
参数:
  time_range_hours: int — 扫描时间范围
  auto_fix:         bool — 是否自动补录缺失记录 (默认 false)

返回值:
  {
    "scanned_sessions": 5,
    "gaps_found": [
      {
        "type": "missing_start",
        "skill_name": "verification-before-completion",
        "detected_context": "对话中检测到 verification-before-completion 调用",
        "can_auto_fix": true
      }
    ],
    "auto_fixed": [...]  // 仅当 auto_fix=true
  }
```

**内部逻辑**：扫描最近的记忆/对话上下文，检测 Skill 工具调用但无对应 skill_session_start 的情况。启发式匹配（skill 名称 + 时间接近度 < 60s）。可以事后补录。

## 五、Skill 调用链映射

### 5.1 调用链

```
using-superpowers (引导, 可选追踪)
    │
    ├─→ brainstorming ──→ writing-plans ──┬──→ subagent-driven-development ──→ finishing-a-development-branch
    │                                      │
    │                                      └──→ executing-plans ──→ verification-before-completion
    │                                                                        │
    │                                                                        └──→ finishing-a-development-branch
    │
    ├─→ systematic-debugging ──→ test-driven-development ──→ verification-before-completion
    │
    ├─→ requesting-code-review ──→ receiving-code-review (终端)
    │
    └─→ writing-skills (独立, 终端)
```

### 5.2 映射表 (Python)

```python
SKILL_CHAIN_MAP = {
    # 起点 skills (无强制前驱)
    "brainstorming":               {"predecessors": [],           "successors": ["writing-plans"]},
    "systematic-debugging":        {"predecessors": [],           "successors": ["test-driven-development"]},
    "requesting-code-review":      {"predecessors": [],           "successors": ["receiving-code-review"]},
    "writing-skills":              {"predecessors": [],           "successors": []},

    # 中间 skills
    "writing-plans":               {"predecessors": ["brainstorming"],  "successors": ["subagent-driven-development", "executing-plans"]},
    "test-driven-development":     {"predecessors": ["systematic-debugging"], "successors": ["verification-before-completion"]},
    "subagent-driven-development": {"predecessors": ["writing-plans"], "successors": ["finishing-a-development-branch"]},
    "executing-plans":             {"predecessors": ["writing-plans"], "successors": ["verification-before-completion"]},
    "verification-before-completion": {"predecessors": ["test-driven-development", "executing-plans"], "successors": ["finishing-a-development-branch"]},
    "receiving-code-review":       {"predecessors": ["requesting-code-review"], "successors": []},

    # 终端 skills
    "finishing-a-development-branch": {"predecessors": ["subagent-driven-development", "verification-before-completion"], "successors": []},

    # 辅助 skills (松散约束)
    "using-git-worktrees":         {"predecessors": [], "successors": []},
    "dispatching-parallel-agents": {"predecessors": [], "successors": []},
    "using-superpowers":           {"predecessors": [], "successors": []},
}
```

### 5.3 Skill → Domain 映射

```python
SKILL_DOMAIN_MAP = {
    "brainstorming":                  "designing",
    "writing-plans":                  "designing",
    "executing-plans":                "building",
    "subagent-driven-development":    "building",
    "dispatching-parallel-agents":     "building",
    "using-git-worktrees":             "building",
    "test-driven-development":        "building",
    "verification-before-completion": "reflecting",
    "requesting-code-review":         "reflecting",
    "receiving-code-review":          "reflecting",
    "systematic-debugging":           "fixing",
    "finishing-a-development-branch": "governing",
    "writing-skills":                 "designing",
    "using-superpowers":              "governing",
}

DOMAIN_TO_TASK_TYPE = {
    "designing":   "architecture",
    "building":    "code_generation",
    "reflecting":  "code_review",
    "fixing":      "debugging",
    "governing":   "general",
}
```

## 六、Parent 校验规则

**设计原则**：warning 不阻塞。开发过程中可能跳过某些 skill（如跳过 brainstorming 直接 writing-plans），强行拒绝会阻塞正常工作流。

校验逻辑：
1. `skill_session_start` 收到 `parent_entity_id` 时，解析出 parent 的 skill_name
2. 查 `SKILL_CHAIN_MAP[skill_name].predecessors`
3. 如果 parent_skill_name 不在合法前驱列表中，返回 `chain_warning` 字段
4. 无论是否合法，**始终创建 session**（不拒绝）
5. `chain_warning` 在 `skill_session_trace` 和 `audit_run` 中汇总暴露

## 七、孤儿检测与续期

### 7.1 孤儿检测阈值

固定阈值 30 分钟。`skill_session_trace` 检测：

```python
if session.status == "active" and (now - session.last_accessed) > timedelta(minutes=30):
    gaps.append({"type": "orphan_active", ...})
```

### 7.2 续期机制

如果 skill 执行时间超过 30 分钟（如 brainstorming 持续 1 小时），Claude 调用：

```
skill_session_complete(entity_id, outcome="still_in_progress")
```

效果：仅更新 `last_accessed = now`，status 保持 active。`next_skills` 返回空列表。可多次调用续期。

## 八、Worth Score 更新

初始版本：**完成即 +0.02**（固定增量）。

```python
# skill_session_complete 内部
if status_transition == "active→done":
    delta = +0.02
    new_worth = min(1.0, previous_worth + delta)
    # 通过 feedback_apply 更新
    feedback_apply(
        item_id=entity_id,
        feedback_type="adopted",
        task_context=f"Skill {skill_name} completed: {outcome[:100]}"
    )
```

后续版本优化方向：
- 基于 outcome 信息密度加权（字数 > 50 → +0.01 bonus）
- 基于 artifacts 数量加权（> 3 个产出物 → +0.01 bonus）
- 基于 chain 完整性加权（链首 skill 完整闭环 → +0.02 bonus）

## 九、audit_run 第八维增强

在现有七维审计基础上，新增 `skill_trace` 维度：

```python
def _audit_skill_trace(engine, time_range_hours: int):
    """Scan for skill execution gaps."""
    sessions = engine.query_entities_by_type("skill_session", time_range_hours)
    gaps = []
    warnings = []

    for s in sessions:
        # 1. 孤儿 active (30min 阈值)
        if s.status == "active" and (now - s.last_accessed) > timedelta(minutes=30):
            gaps.append({
                "type": "orphan_active",
                "entity_id": s.entity_id,
                "skill_name": s.skill_name,
                "idle_minutes": (now - s.last_accessed).total_seconds() / 60,
                "suggestion": "手动 skill_session_complete(entity_id, outcome)"
            })

        # 2. 链断裂
        if s.status == "done":
            expected = SKILL_CHAIN_MAP.get(s.skill_name, {}).get("successors", [])
            if expected:
                has_child = any(c.parent_skill == s.entity_id for c in sessions)
                if not has_child:
                    warnings.append({
                        "type": "chain_broken",
                        "entity_id": s.entity_id,
                        "skill_name": s.skill_name,
                        "expected_next": expected
                    })

        # 3. 标签不一致
        if s.status == "done" and "task:done" not in s.tags:
            warnings.append({
                "type": "tag_mismatch",
                "entity_id": s.entity_id,
                "actual_tags": s.tags
            })

    # 4. 事后补全扫描: 对话文本中的 Skill 调用无对应 session_start
    # 启发式检测 (skill 名称 + 时间窗口 ±60s)
    extra = _scan_for_missing_starts(engine, time_range_hours)
    gaps.extend(extra)

    return {"gaps": gaps, "warnings": warnings, "total_sessions": len(sessions)}
```

## 十、CLAUDE.md 改动

在现有 CLAUDE.md 末尾新增以下两段：

### 10.1 Skill 调用协议

新增 "## Skill 调用协议 (Session 追踪)" 段:

- **前置指令**：每次调用 Skill 工具前，执行 `skill_session_start(skill_name, task_description, parent_entity_id)`
- **后置指令**：skill 执行完毕时，执行 `skill_session_complete(entity_id, outcome, artifacts)`，outcome ≤200 字
- **超时续期**：执行超 30 分钟时，执行 `skill_session_complete(entity_id, "still_in_progress", [])` 重置计时器
- **放弃**：被中断时，执行 `skill_session_complete(entity_id, "abandoned: <原因>", [])`

### 10.2 分支完成前验收

新增 "## 开发分支完成前验收" 段:

`finishing-a-development-branch` 执行前必须先调用 `skill_session_trace(session_scope="branch")`。

验收标准（全部满足才能继续）：
1. `chain_complete = true` — 所有 skill 形成完整闭环
2. `gaps` 为空 — 无 orphan_active（超 30 分钟未完成的 session）
3. `chain_valid = true` — 调用链合法无 violation
4. 链首为 `brainstorming` / `systematic-debugging` / `requesting-code-review` 之一
5. 链尾为 `finishing-a-development-branch` 或 `receiving-code-review`

验收不通过时的修复路径: orphan_active → 手动 complete 为 abandoned；chain_broken → 检查是否应调用后续 skill；chain_violation → 调用 `skill_session_audit` 评估。

## 十一、实现文件清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `plastic_promise/mcp/tools/skill_tracking.py` | **新增** (~250 行) | 四个工具的实现 |
| `plastic_promise/mcp/server.py` | 修改 (~20 行) | 注册 4 个新工具 + 路由 |
| `plastic_promise/core/constants.py` | 修改 (~60 行) | 新增 SKILL_CHAIN_MAP, SKILL_DOMAIN_MAP, DOMAIN_TO_TASK_TYPE |
| `plastic_promise/mcp/tools/audit_defense.py` | 修改 (~50 行) | audit_run 新增 skill_trace 维度 |
| `CLAUDE.md` | 修改 (~40 行) | 新增 Skill 调用协议 + 验收检查 |
| `GOAL.md` | 修改 (~5 行) | 更新状态 |

## 十二、非功能性约束

- **零新存储结构** — 全部复用 MemoryRecord + tag + entity graph
- **不修改 SuperPowers** — 所有逻辑在 Plastic Promise 侧
- **不阻塞工作流** — parent 校验只 warning，孤儿可续期
- **向后兼容** — 不影响现有 29 个 MCP 工具
