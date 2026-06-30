# Plastic Promise — 全链路架构文档

> 版本: 0.2.0 | 日期: 2026-06-30 | 工具: 40 个 MCP 工具 / 10 域

---

## 一、系统全景

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Plastic Promise 全链路                              │
│                                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐              │
│  │ 会话启动  │───→│ 上下文供应 │───→│ 任务执行  │───→│ 每步闭环  │───→ 自演化   │
│  │session-  │    │context-  │    │memory/   │    │step-     │    ↑        │
│  │init      │    │supply    │    │skills    │    │closure   │    │ 反馈    │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘    │ 循环    │
│       │              │               │               │           │        │
│       ▼              ▼               ▼               ▼           │        │
│  ┌──────────────────────────────────────────────────────────┐    │        │
│  │                    记忆质量管道 (Direction A + B)          │    │        │
│  │  raw → tagged → classified → embedded → migrate → GC     │────┘        │
│  └──────────────────────────────────────────────────────────┘             │
│                                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐              │
│  │ 原则治理  │    │ 审计合规  │    │ 信任防线  │    │ 多Agent  │              │
│  │principles│    │audit     │    │defense   │    │collab    │              │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 二、模块清单与数据流

### 2.1 10 域 40 个 MCP 工具

| # | 域 | 工具数 | 工具列表 | 做功状态 |
|---|------|--------|------|---------|
| 1 | **Memory** | 10 | memory_recall, memory_store, memory_update, memory_forget, memory_stats, memory_list, memory_gc, memory_correct, fuzzy_status, fuzzy_process | ✅ 高频 |
| 2 | **Context** | 5 | context_supply, context_inject, context_graph, context_ready, auto_context_inject | ✅ 中频 |
| 3 | **Principles** | 4 | principle_activate, principle_inherit, principle_diffuse, principle_evaluate | ⚠️ 低频 |
| 4 | **Audit** | 4 | audit_run, audit_pre_check, defense(get\|history\|adjust\|status) | ⚠️ 中频 |
| 5 | **Reflection** | 2 | scarf_reflect, feedback_apply | ⚠️ 低频 |
| 6 | **Domain** | 1 | domain(stats\|merge\|unmerge\|rename\|rebuild) | ✅ 中频 |
| 7 | **System** | 4 | system(stats\|backup\|migrate), issue_create, issue_transition, issue_list | ✅ 中频 |
| 8 | **Pack** | 3 | pack_export, pack_import, pack_recall | ⚠️ 低频 |
| 9 | **Skill Track** | 5 | skill_session_start, skill_session_complete, skill_session_trace, skill_session_audit, skill_auto_track | ✅ 自动 |
| 10 | **Skills** | 3 | session-init, smart-remember, step-closure | ✅ Phase 1 |

### 2.2 代码模块映射

```
plastic_promise/
├── core/                          # 内核
│   ├── context_engine.py          # ContextEngine — 记忆池 + 图谱 + 检索
│   ├── constants.py               # 12 原则 + 防线层级 + SKILL_CHAIN_MAP + 阈值
│   ├── embedder.py                # 向量嵌入 + FallbackEmbedder
│   ├── decay_engine.py            # Weibull 衰减引擎 (Direction A)
│   ├── quality_gate.py            # 四维入池门控 (Direction B)
│   ├── reranker.py                # Cross-encode 重排序
│   ├── noise_filter.py            # 噪声过滤器
│   ├── principles.py              # PrincipleTracker — 原则遵守追踪
│   ├── step_auditor.py            # StepAuditor — 步骤审计
│   ├── pack_index.py              # 经验包流式导出/导入
│   └── issue_validator.py         # Issue 宪法校验
│
├── memory/                        # 记忆系统
│   ├── soul_memory.py             # RecMem + MemoryTierManager + EvolveR + MemoryGC
│   ├── pipeline.py                # MemoryPipeline — raw→tagged→classified→embedded→migrate
│   └── lancedb_store.py           # LanceDB 向量存储 + 混合检索
│
├── mcp/                           # MCP 服务层
│   ├── server.py                  # MCP Server — 40 工具注册 + 分发路由
│   └── tools/
│       ├── memory.py              # 记忆域 8 工具 handler
│       ├── context.py             # 上下文域 5 工具 handler (含 auto_context_inject)
│       ├── principles.py          # 原则域 4 工具 handler
│       ├── audit_defense.py       # 审计+防线 4 工具 handler
│       ├── reflection.py          # 反思域 2 工具 handler
│       ├── domain.py              # 域联邦 handler
│       ├── management.py          # 系统+Issue+Pack handler
│       ├── skill_tracking.py      # Skill 追踪 5 工具 handler
│       └── sync.py                # 文件同步 handler
│
├── skills/                        # 程序化技能 (Phase 1)
│   ├── engine.py                  # SkillEngine + SkillDef + SkillResult + AtomRegistry
│   ├── session_lifecycle.py       # session-init 技能
│   └── memory_operations.py       # smart-remember 技能
│
├── defense/                       # 防线系统
│   ├── soul_audit.py              # SoulAuditor — 多维审计
│   └── soul_enforcer.py           # TrustManager — 信任分管理
│
├── reflection/                    # 自省系统
│   ├── soul_scarf.py              # SCARFReflector — 五维自省
│   ├── soul_proprioception.py     # ProprioceptionManager — 惯性检测
│   └── soul_curiosity.py          # 自适应探索
│
├── growth/                        # 演化系统
│   ├── soul_hormone.py            # HormoneEngine — 激素引擎
│   ├── skill_extractor.py         # smart_extractor — 6 类提取
│   └── soul_classifier.py         # 分类管线
│
├── loop/                          # 闭环编排
│   └── soul_loop.py               # SoulLoop — pre_task_v2 + post_task 六联闭环
│
├── cron/                          # 定时任务
│   ├── soul_closure_guardian.py   # 闭环保卫
│   ├── health_scan.py             # 健康扫描
│   └── audit_daily.py             # 每日审计
│
└── bridge/                        # 跨系统桥接
    ├── soul_bridge.py             # SoulBridge — Claude↔Pi 双向桥
    ├── bus_client.py              # 消息总线客户端
    └── neko_adapter.py            # Neko 适配器
```

---

## 三、完整数据流链路

### 3.1 会话启动链路

```
CLAUDE.md 步骤 0 (server check)
  │ python health check http://127.0.0.1:9020
  │ 不可用 → 启动 MCP 服务器
  │
  ▼
CLAUDE.md 步骤 1 (session-init skill)
  │
  ├─① principle_activate(task_type, task_description)
  │    └─→ 匹配 12 原则 → 返回激活列表 + 违反后果 + 遵循建议
  │
  ├─② scarf_reflect(task_description)
  │    └─→ SCARFReflector.reflect() → 五维自省基线
  │         ├─ 关键词匹配 (中英文正/负信号)
  │         ├─ Embedding 语义相似度回退 (余弦相似度 ±0.15 微调)
  │         └─→ Status/Certainty/Autonomy/Relatedness/Fairness 评分
  │
  ├─③ context_supply(task_description, task_type)
  │    └─→ ContextEngine.supply()
  │         ├─ embedder.embed(query) → 向量
  │         ├─ LanceDB ANN 检索
  │         ├─ BM25 文本检索
  │         ├─ RRF 融合排序
  │         ├─ cross-encode rerank
  │         ├─ 图谱遍历扩展
  │         └─→ ContextPack (核心/关联/发散 三层)
  │
  ├─④ memory_store(content="[AUTO INJECT]...")
  │    └─→ noise_filter → fuzzy_buffer.store_urgent()
  │         └─→ pipeline: raw → tagged → classified → embedded → migrate
  │              ├─ smart_extractor: 6 类提取 (preference/fact/decision/entity/event/pattern)
  │              ├─ QualityGate: 四维门控 (置信度+相关性+新鲜度+信息密度, ≥0.5入池)
  │              ├─ 去重检查: LanceDB ANN cos≥0.85 → 强化已有
  │              ├─ Weibull 衰减初始化 (L1 β=1.5/3d, L3 β=0.7/90d)
  │              └─→ SQLite + LanceDB 双写
  │
  ├─⑤ domain(action="stats")
  │    └─→ DomainManager.stats() → 8 域健康度 + 融合信号
  │
  ├─⑥ system(action="stats")
  │    └─→ memory_stats + graph_stats + fuzzy_buffer 积压 + 数字身体系统快照
  │
  ├─⑦ defense(action="get")
  │    └─→ TrustManager.get() → 信任分 + tier + autonomy_level
  │
  └─⑧ memory_gc(dry_run=True)
       └─→ MemoryGC.collect(dry_run=True)
            ├─ mark_decaying: Weibull 批量衰减
            ├─ merge_similar: cos≥0.70 合并候选
            └─→ 预览报告 (不执行)
```

### 3.2 任务执行链路

```
决策前 (align-principles)
  ├─ principle_activate(task_type, task_description)
  └─ principle_evaluate(principle_id) → 反事实预演

记忆操作 (smart-remember / memory_store)
  ├─ smart-remember:
  │    ├─ principle_activate
  │    ├─ memory_recall → 去重检查 (relevance ≥ DEDUP_SIMILARITY_THRESHOLD=0.85)
  │    ├─ 有重复 → handle_memory_update (强化已有)
  │    └─ 无重复 → memory_store (经过完整质量管道)
  │
  └─ memory_store (原始路径):
       └─→ noise_filter → fuzzy_buffer → 质量管道 → 双写

写操作前 (defense check)
  └─ defense(action="get")
       ├─ trust ≥ 0.80 → 自主执行
       ├─ trust ≥ 0.60 → 正常执行
       ├─ trust ≥ 0.30 → 向用户确认
       └─ trust < 0.30 → 拒绝

子 Agent 派发 (context injection)
  ├─ memory_recall(query, task_type) → 核心记忆
  ├─ context_supply(task_description) → 上下文包
  └─→ 写入派发 prompt 的 "Context from Memory System" 段

多 Agent 协作 (delegate-to-pi — Phase 2)
  ├─ defense(get) → 检查信任分 ≥ 0.60
  ├─ memory_store(tags=["task:pending","assignee:pi_builder"])
  ├─ issue_create(title, principle_id)
  └─→ Daemon 自动检测 → spawn Pi → 执行 → Reviewer 审查 → Claude 验收
```

### 3.3 每步闭环链路 (step-closure)

执行者（Claude）调用时提供反思四字段——不填模板、不委托 Agent：
```
step-closure(
  task_description, git_commit, mode="full",
  lesson, improvement, root_cause, optimization
)
  │
  ├─① 原则对齐检查 (alignment)
  │    └─→ principle_activate → PrincipleTracker.record(pid, obeyed, context)
  │
  ├─② SCARF 五维自省
  │    └─→ SCARFReflector.reflect(task_description)
  │         ├─ 关键词匹配 (中英文正/负信号)
  │         ├─ Embedding 语义相似度回退 (无关键词时)
  │         └─→ Status/Certainty/Autonomy/Relatedness/Fairness 评分
  │
  ├─③ 激素更新 (hormone)
  │    └─→ HormoneEngine.apply_feedback(adopted/ignored/rejected)
  │         基于 CEI 决定反馈类型 (≥0.6 adopted, ≥0.4 ignored, <0.4 rejected)
  │         → dopamine/cortisol 波动 → trust_delta → TrustStore 持久化
  │
  ├─④ 信任分联动 (trust)
  │    └─→ SCARF overall ≥ 0.80 → TrustManager.boost(+0.02)
  │         SCARF overall < 0.40 → TrustManager.decay(-0.02)
  │         激素 trust_delta 同步写入 (不再被 None trust_manager 丢弃)
  │
  ├─⑤ 反思记忆存储 (reflection)
  │    └─→ 执行者提供的四字段 → 结构化格式:
  │         "[经验] ...\n[优化] ...\n[根因] ...\n[动作] ..."
  │         → smart-remember 走完整质量管线 (分类→去重→嵌入→门控→入池)
  │
  └─⑥ CEI 复合执行指数 (cei)
       └─→ 加权计算 → 写回 _cached_cei (不再卡在 0.5)
            → 反馈类型动态切换 → 自演化回路闭合
```

### 3.4 审计链路

```
定时审计 (cron — 每小时)
  ├─ audit_run(action="full")
  │    └─→ SoulAuditor.run_audit()
  │         7 维审计:
  │          ├─ 原则遵守率
  │          ├─ 记忆健康度
  │          ├─ 技能链完整性
  │          ├─ 信任分趋势
  │          ├─ SCARF 趋势
  │          ├─ 域融合信号
  │          └─ 激素水平
  │         → 审计报告 → memory_store
  │
  ├─ 每日审计 (audit_daily.py)
  └─ 健康扫描 (health_scan.py)

分支完成前 (branch-closure-check — Phase 3)
  ├─ skill_session_trace(session_scope="branch")
  │    ├─ chain_complete? gaps为空? chain_valid?
  │    └─→ 不通过 → 修复建议
  ├─ memory_gc(dry_run=True)
  │    └─→ merge 候选数合理?
  └─ pack_export → experience_packs/

操作前实时检查 (audit_pre_check)
  └─→ L0 硬边界: rm -rf / DROP TABLE / format / shutdown → block
       L1 约束衰减: trust-score 驱动
       L2 免疫巡检: 定期扫描
```

### 3.5 记忆生命周期链路

```
存储 (Direction B — 质量管道)
  memory_store(content)
    └─ noise_filter.is_noise() → 过滤
    └─ fuzzy_buffer.store_urgent()
         ├─ extract_memories() [6类提取: preference/fact/decision/entity/event/pattern]
         ├─ tag (关键词标注)
         ├─ classify (大类分 L1/L3)
         ├─ embed (向量嵌入)
         └─ migrate → QualityGate
              ├─ score ≥ 0.5 → 入池
              ├─ score 0.3-0.5 → low_quality
              └─ score < 0.3 → 丢弃
    └─ check_duplicate() cos≥0.85 → 去重 (强化已有)
    └─ RecMem.store() → decay_multiplier + effective_half_life 初始化
    └─ LanceDB 双写

衰减 (Direction A — Weibull 曲线)
  MemoryGC.collect() (~7天)
    ├─ mark_decaying()
    │    └─ Weibull 批量衰减
    │         L1: β=1.5, half_life=3天 → 快速衰减
    │         L2: β=1.0, half_life=30天 → 中速衰减
    │         L3: β=0.7, half_life=90天 → 缓慢衰减
    │
    ├─ merge_similar() cos≥0.70
    │    └─ composite_score 选择幸存者 (wilson×0.6 + freshness×0.25 + reinforcement×0.15)
    │
    └─ forget() → 清理 decayed + merged

强化 (访问时)
  AccessReinforcement.compute_boost(access_count, interval_days)
    └─→ effective_half_life ↑, worth_score ↑
```

### 3.6 信任分全链路

```
初始值: 0.60 (standard tier)

每次 step-closure (自动)
  ├─ SCARF ≥ 0.80 → defense(action="adjust", delta=+0.02)
  └─ SCARF < 0.40 → defense(action="adjust", delta=-0.02)

用户反馈 (手动)
  ├─ 验收通过 → defense(action="adjust", delta=+0.05)
  ├─ 打回错误 → defense(action="adjust", delta=-0.03)
  └─ 连续 5 步无失败 → defense(action="adjust", delta=+0.01)

信任分影响:
  ├─ 检索范围: high(1.3x) / critical(0.5x)
  ├─ 工具权限: 写文件 / 删文件 / 发 Issue / 分配任务
  ├─ 激素水平: trust ≥ 0.80 → curiosity↑, cortisol↓
  └─ 自主权: 阈值决定是否可自主决策

防线联动:
  L0 → 硬边界 (不受信任分影响)
  L1 → 约束衰减 (信任分 ≥ 0.80 → 放松, < 0.30 → 收紧)
  L2 → 免疫巡检 (定期, 独立于信任分)
```

---

## 四、技能编排层

### 4.1 架构分层

```
┌─────────────────────────────────┐
│  MCP 工具层 (40 tools)           │  ← 原子操作
│  memory_store / principle_activate / defense / ...    │
├─────────────────────────────────┤
│  SkillEngine                    │  ← 编排引擎
│  register() / exec() / degrade  │
├─────────────────────────────────┤
│  程序化技能 (8 域)               │  ← 组合逻辑
│  session-init / smart-remember / step-closure / ...   │
├─────────────────────────────────┤
│  Superpowers 清单               │  ← AI 引导
│  .claude/skills/plastic-promise/*.md                  │
└─────────────────────────────────┘
```

### 4.2 已实现技能 (Phase 1)

| 技能 | 原子数 | 降级策略 | 作用 |
|------|--------|---------|------|
| `session-init` | 7 | domain/system/memory_gc: skip, defense: warn | 会话启动单次调用 |
| `smart-remember` | 3 | principle_activate: skip, memory_recall: fallback→store, memory_store: abort | 去重存储 |
| `step-closure` | — (直接调用 post_task) | — | 每步六联闭环 |

### 4.3 待实现技能 (Phase 2-6)

| Phase | 域 | 技能 |
|-------|-----|------|
| 2 | 协作委派 | delegate-to-pi, review-and-accept, reject-and-reassign, claim-task |
| 2 | 原则治理 | align-principles, inherit-work-principles, governance-check, evaluate-violation |
| 3 | 审计合规 | pre-commit-audit, branch-closure-check, full-audit |
| 3 | 自演化 | evolve-from-feedback, optimize-skill-chain, close-the-loop |
| 4 | 记忆操作 | context-aware-recall, correct-memory, forget-memory |
| 4 | 知识打包 | export-experience, import-and-merge, strict-recall |
| 5 | 系统健康 | health-check, scheduled-gc, system-backup |

---

## 五、数据持久化

```
SQLite (主存储)
  ├─ memories 表: id, content, memory_type, tier, worth_score, tags, entity_ids, ...
  ├─ schema_version 迁移链
  └─ 写穿透: upsert → 立即持久化

LanceDB (向量存储)
  ├─ 向量索引: ANN 检索
  ├─ FTS 索引: 全文搜索 (BM25)
  ├─ RRF 融合: 向量 + 文本 混合排序
  └─ 回填: 空时自动从 SQLite 回填

经验包 (跨 Agent 知识传递)
  ├─ pack_export: 流式 gzip JSON 导出
  ├─ pack_import: skip/replace/merge 策略
  └─ version_mapper: 跨版本兼容
```

---

## 六、当前系统状态

| 指标 | 值 | 趋势 |
|------|-----|------|
| MCP 工具 | 40 个 / 10 域 | ↑ (Phase 1 +3) |
| 记忆总量 | 79 条 | → |
| 记忆 tier | 全部 L1 | → (闭环激活后分化) |
| 信任分 | 0.6 | → (step-closure 激活后波动) |
| 技能链完整度 | 4/4 broken | → (Phase 3 审计修复) |
| SCARF 调用 | 0 次 | → (step-closure 激活后增加) |
| post_task 闭环 | 0 次 | → (step-closure 激活后每步) |
| 原则遵守追踪 | 0 次 | → (step-closure 激活后追踪) |
| 技能实现 | Phase 1/6 | → |

---

## 七、关键约定速查

| # | 约定 | 触发时机 | 违反后果 |
|---|------|---------|---------|
| 1 | session-init | 会话启动 | 无上下文，Agent 盲目操作 |
| 2 | step-closure | 每步实质产出后 | 信任分停滞，记忆不分化，系统不演化 |
| 3 | defense check | 写操作前 | 可能越权操作 |
| 4 | smart-remember | 记忆存储 | 重复记忆，存储膨胀 |
| 5 | 子Agent上下文注入 | 派发前 | 子Agent 重复已修复的 bug |
| 6 | 每步有 git | 代码变更后 | 无法追溯 |
| 7 | branch-closure-check | 分支完成前 | 技能链断裂未发现 |
| 8 | pack_export | 里程碑后 | 知识不可跨 Agent 传递 |
