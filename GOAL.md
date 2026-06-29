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

实践层 — 外显于行
  多 Agent 开发组（Claude PM + Pi Builder/Fixer/Reviewer） ✅
  自治流水线（标签驱动, 零Token Daemon, 自动衔接） ✅
  post_task 六联闭环（light委派+full验收） ✅
  动态信任-自由度矩阵（4档映射到工具权限） ✅
  11 维审计（每小时自动 + Tier1 修复） ✅

演化层 — 迭代进步
  worth 反馈闭环 ✅
  AgentBehaviorTracker ✅
  curiosity 自适应探索 ✅
  PrincipleTracker 趋势分析 ✅

基础设施
  SQLite 写穿透 + schema_version 迁移链 ✅
  域联邦（7域 + 自演化 + 联邦信号） ✅
  标签状态机（task:pending→done→reviewed） ✅
  双向桥（/notify→SSE /events 实时推送） ✅
  灾难恢复（rebuild_from_memories） ✅
  经验包（流式导出 + version_mapper + strategy） ✅
```

## 三、当前状态

### 已完成 (2026-06-29)

**内核：**
- 29 个 MCP 工具（8 域）
- 12 条核心原则（按行为域分布）
- 7 维审计 + 5 维 SCARF 自省 + 11 维多 Agent 审计
- 域联邦系统（自演化 + 联邦信号）
- 三层防线（L0 + L1 信任约束 + L2 免疫巡检）
- 韧性专项（灾难恢复 + 跨版本 + 静默失效）

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

### 进行中
- 无。所有已交付。

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
python pi_daemon.py                                 # 自治流水线
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
- Daemon 持续运行 (pi_daemon.py)
- 审计每小时自动执行
