# Plastic Promise — 项目目标与指令

> **核心范式**: 约定工程 (Commitment Engineering) — 内化约定替代外部约束。

## 一、项目定位

构建 AI 行为治理系统——不是通过规则门禁拦截 Agent（约束工程），而是让 Agent 主动查阅和内化约定（约定工程）。现已扩展为完整的多 Agent 协作框架——Claude 作为项目经理管理 Pi 开发团队。

## 二、架构

```
约定层 — 内化于心
  12 条核心原则（行为域分布: all/governing/building/designing/reflecting）
  原则遗传（跨 Agent 扩散 + principle_inherit）
  原则遵守量化追踪（PrincipleTracker） ✅
  原则反事实评估（principle_evaluate） ✅

实践层 — 外显于行
  多 Agent 开发组（Claude PM + Pi Builder/Fixer/Reviewer） ✅
  自治流水线（标签驱动, 零Token Daemon, 自动衔接） ✅
  step-closure 六联闭环（原则对齐→SCARF→激素→信任→反思→CEI） ✅
  Safety-Net Daemon 全域创新引擎（多Agent调度 + 打回区 + 6维模式识别 + 11扫描器） ✅
  动态信任-自由度矩阵（4档映射到工具权限 + TrustStore 持久化） ✅
  11 维审计（7维基础 + 4维多Agent, 每小时自动 + Tier1 修复） ✅
  Skill 调用链追踪（14 技能映射 + 链完整性检测 + sp-stage 校验） ✅
  SuperPowers 12 阶段流水线（sp-stage 统一入口 + 链约束 + Trae hook） ✅

演化层 — 迭代进步
  worth 反馈闭环（采纳/拒绝/忽略 三态计数器） ✅
  SCARF 五维自省（地位/确定性/自主/关联/公平） ✅
  CEI 复合执行指数 ✅
  Weibull 记忆衰减（L1 β=1.5/3d, L3 β=0.7/90d） ✅
  curiosity 自适应探索 ✅

基础设施
  SQLite 写穿透 + schema_version 迁移链 ✅
  LanceDB 向量存储（ANN + FTS + RRF 混合融合） ✅
  记忆质量管道（6类提取→向量去重→QualityGate→衰减初始化→双写） ✅
  域联邦（7域 + 自演化 + 联邦信号） ✅
  标签状态机（task:pending→done→reviewed） ✅
  双向桥（/notify→SSE /events 实时推送） ✅
  灾难恢复（rebuild_from_memories） ✅
  经验包（流式导出 + version_mapper + strategy, 跨Agent知识传递） ✅
```

## 三、当前状态

### 已完成 (2026-06-29)

**内核：**
- 12 条核心原则（按行为域分布）
- 11 维审计（7维基础 + 4维多Agent）
- 域联邦系统（自演化 + 联邦信号）
- 三层防线（L0 硬边界 + L1 信任约束 + L2 免疫巡检）
- 韧性专项（灾难恢复 + 跨版本 + 静默失效）
- 经验包系统（流式导出 + version_mapper + 跨Agent知识传递）

**多 Agent 开发组：**
- Pi CLI 原生执行（替代自建 agent.py）
- 标签状态机（task:pending→active→done→review→reviewed）
- 零 Token Daemon（SQLite 直查, 多角色管理）
- 自治流水线（Builder→Reviewer 自动衔接, Fixer 修复循环）
- 信任-自由度矩阵（4 档, 权限映射到工具）
- 双向桥（/notify→SSE /events）
- 任务超时恢复（5min task:active reset）
- 定时记忆清理（7 天 GC）
- SuperPowers 流水线映射（planner→builder→fixer→reviewer）

### 已完成 (2026-06-30)
- TODO #6: LanceDB 向量存储 + 混合融合检索 (ANN + FTS + RRF + 优雅降级)
- TODO #7: HTML 仪表盘 (/dashboard, 记忆池 + 身体系统 + 信任分)
- **方向 A**: 记忆生命周期引擎 — Weibull 衰减 (L1 β=1.5/3d, L3 β=0.7/90d) + 访问强化 (间隔重复) + 三因素复合评分 (wilson×0.6 + freshness×0.25 + reinforcement×0.15) + SQLite 迁移 + 存量衰减计算
- **方向 B**: 记忆质量管道 — smart_extractor 接入 pipeline + 向量去重 (cos≥0.85) + QualityGate 四维入池评分 (置信度/相关性/新鲜度/信息密度, 等权0.25) + MemoryGC.merge_similar() 相似合并 (cos≥0.70) + 方向 A-B 全集成 (tier-aware freshness + decay init on store + dedup→AccessReinforcement boost)
- 存量回填策略 (LanceDB 空时自动从 SQLite 回填)
- **Skill Tracking**: SuperPowers 流程可追踪化 — 5 个 MCP 工具 (skill_session_start/complete/trace/audit + skill_auto_track) + SKILL_CHAIN_MAP (14 技能) + skill_session 实体类型 + audit 第八维 skill_trace (权重 0.10) + CLAUDE.md Skill 调用协议
- **信任分持久化**: TrustStore (SQLite trust_scores + trust_history 表) + 时间衰减 (-0.005/天) + SCARF 联动 boost/decay + L0/L1 违规驱动减分 + MCP 重启后不丢失
- **SuperPowers Pipeline**: `sp-stage` MCP 工具 (1 工具 12 阶段统一入口) + 链校验 (SKILL_CHAIN_MAP, 跳步自动拒绝) + Trae hook 桥接 (sp_hook.py → /api/skill-track) + context_supply 原子移除优化 (5~60s → 0.2~0.4s)
- **全链路契约校验**: 对照 CLAUDE.md 逐项检查各子系统实现/约定/缺陷状态，修复 step-closure mode 参数丢失 bug

### 已完成 (2026-07-01)
- **Auto Context Inject**: 统一自动化上下文注入 — 1 个 MCP 工具 (auto_context_inject) + SoulBridge/Pi Daemon/Claude Code 三路径统一 + 自反馈循环 + CLAUDE.md 启动序列简化
- **step-closure**: 六联闭环引擎 — 原则对齐检查 → SCARF 五维自省 → 激素更新 → 信任分联动 → 反思记忆存储（执行者提供 lesson/improvement/root_cause/optimization，结构化格式入池） → CEI 复合指数。支持 full/light 双模式
- **smart-remember**: 智能记忆存储 — 自动去重检查 (相似度 ≥ 0.85 则更新已有记忆)，通过完整质量管道
- **session-init**: 统一会话启动 — 一条调用替代原有 5 步（原则激活 + context_supply + SCARF 基线自省 + memory_store 注入 + domain stats + system stats + defense + memory_gc preview）
- **memory_sync_files**: 文件系统 .md 记忆同步到 MCP 管道
- **Safety-Net Daemon ≤ Phase 1**: 兜底审查 — scan_orphan_steps (孤儿step自动闭环) + scan_unclosed_issues (超时issue自动close) + recover_stuck_tasks (5min超时恢复) + dispatch_fix_task (标签调度pi_fixer)
- **Safety-Net Daemon Phase 2**: 免疫系统化 — scan_duplicate_clusters (SQL GROUP BY 清理 31 条重复记忆) + scan_stale_worth (复活 47 条 (0,0) worth) + scan_tier_migration (126 L1→L2, 3 L2→L3) + scan_category_stuck (LLM队列监控) + scan_self_noise (自身审计报告去重)
- **Safety-Net Daemon Phase 3**: 全域创新引擎 — 标签调度引擎 `dispatch_fix_task` (fixer/reviewer/builder/claude 4路由) + 打回区 `tag_for_redo`+`scan_redo_queue` (12h提醒→24h强调) + `scan_innovation_opportunities` (6维跨域模式识别: 重复Bug/记忆退化/技能链/信任分异常/僵尸域/分类瓶颈) + `tag_audit_finding` (审计发现可追溯化) + `_store_tagged_memory` (统一/notify写入)

**MCP 工具总数: 41（10 域 + SuperPowers）**

### 进行中
- 无。所有已交付。

### 已完成 (2026-07-02)

- **Enterprise Git Governance**: Plastic Promise Flow 企业级 Git 治理框架 — CI/CD workflow (P0: lint/test/security, P1: style/coverage) + PR/Issue 模板 + CODEOWNERS + SECURITY.md + CONTRIBUTING.md + CLAUDE.md Git 治理章节。分支策略 (feat/fix/refactor/docs/perf/chore + worktree/<agent>/)，Squash Merge 线性历史，Conventional Commits 强制规范
- **Embedder 修复**: 默认 provider 从 BAAI/bge-large-zh-v1.5 (3.7GB) 切换到 Ollama mxbai-embed-large (0.7GB)。LanceDB 185 条零向量全部重建为真实 mxbai 向量
- **Rust Engine 诊断**: 发现 Rust ContextEngine 使用 placeholder retriever (全 Noop 组件)，始终返回 0.50 均分。绕过 Rust 路径走 Python LanceDB 真实检索
- **Weibull 衰减激活**: L2 tier 加入 DECAY_CONFIG (beta=1.2, hl=7d) + effective_half_life tier-aware (L1=3d, L2=7d, L3=90d) + RecMem.update_all_decay() 批量衰减更新 + daemon 审计周期自动触发。存量 188 条记录完成 half-life 数据迁移
- **CEI 访问修复**: 新增 get_cei() 模块函数 + 全局 CEI 缓存，post_task 时自动更新
- **Scheduler Health Meta-Audit (scan_scheduler_health)**: 6 维 Hunter Guild 自审计扫描器 — Scanner SNR / Agent timeout / Dispatch latency / Priority balance / Verification throughput / Trend comparison。发现 red 级问题自动生成 fix_* 委托到 Hunter Guild → 走 Git PR 修复流程
- **One-Click Launcher**: ServiceManager 服务编排 + Watchdog 崩溃恢复 + init_and_start.py CLI 一键启动。依赖排序、健康检查、指数退避
- **全系统活性审计**: SCARF (0.72) / Trust (0.966, 50 次调整) / Principles (70 节点, 920 边) / Hormones / CEI — 六联闭环中前两步活，后四步修复激活

**MCP 工具总数: 48（11 域 + SuperPowers）**

## 四、12 条核心约定

| # | 原则 | 域 | 一句话 |
|---|------|------|--------|
| 1 | 奥卡姆剃刀 | all | 如无必要，勿增实体 |
| 2 | 全过程可查可透明 | all | 每步有 git 痕迹、可追溯审计日志 |
| 3 | 自我审计闭环 | reflecting | 根因→改良→教训→评分 |
| 4 | 上下文驱动决策 | designing | 无上下文不行动，不足时标注而非猜测 |
| 5 | 约定优于约束 | governing | 检验存在不等于有效 |
| 6 | 数据流驱动 | designing | 追踪真实数据流，非假设架构图 |
| 7 | 器官互保 | building | 每个子系统保护整个系统 |
| 8 | 工具即感官 | all | LLM 能力边界由工具链决定 |
| 9 | 信任驱动约束 | governing | 动态信任分调节自主权 |
| 10 | 自演化闭环 | reflecting | 评价驱动行为修正 |
| 11 | 原则遗传 | governing | 核心约定跨 Agent 代际传递 |
| 12 | 代码即文档 | building | 代码本身是最权威的文档 |

## 五、多 Agent 标签状态机

```
task:pending  → task:accepted → task:active → task:done → task:review → task:reviewed
    ↑ Claude发布   ↑ Daemon认领   ↑ Pi执行     ↑ 完成    ↑ Reviewer   ↑ Claude验收

task:rejected → Fixer认领 → task:accepted → 修复循环

超时: task:active>5min → pending | task:reviewed>10min → active
清理: task:accepted/reviewed>7天 → 移除标签
```

## 六、信任-自由度矩阵

| 信任分 | 等级 | 写文件 | 发Issue | 分配任务 | 修改原则 |
|--------|------|--------|---------|----------|----------|
| 0.80+ | autonomous | ✅ | ✅ | ✅ | ⚠️审批 |
| 0.60+ | standard | ✅ | ✅ | ❌ | ❌ |
| 0.30+ | restricted | ⚠️审批 | ❌ | ❌ | ❌ |
| 0.00+ | readonly | ❌ | ❌ | ❌ | ❌ |

## 七、操作方法

### 启动团队
```bash
python -m plastic_promise.mcp.server --sse 9020   # 共享记忆
python daemons/pi_daemon.py                                 # 自治流水线
```

### Claude 发任务
```
memory_store(tags=["task:pending","assignee:pi_builder","domain:building"])
→ Daemon 自动检测 → Pi 执行 → Reviewer 自动审查 → Claude 验收
```

### 验收
```
通过: defense(adjust, +0.02, target="pi_builder") → memory_store(task:reviewed)
打回: memory_store(task:rejected, assignee:pi_fixer) → Fixer 自动修复
```

### 当前指令
- 推送到 main 分支
- Daemon 持续运行 (daemons/pi_daemon.py)
- 审计每小时自动执行
