# Plastic Promise — 项目目标与指令

> **核心范式**: 约定工程 (Commitment Engineering) — 内化约定替代外部约束。约定，是比约束更深的力量。

---

## 一、项目定位

构建 AI 行为治理系统——不是通过规则门禁拦截 Agent（约束工程），而是让 Agent 主动查阅和内化约定（约定工程）。

```
约定与约束的区别:
  约束: "你不能这样做" → Agent 被动服从 → 绕过去
  约定: "这样做会违反我们共同的约定，后果是..." → Agent 主动践行 → 内化
```

## 二、三层架构

```
约定层 — 内化于心
  12 条核心原则（激活带后果+建议）
  原则遗传（跨 Agent 扩散）
  原则遵守量化追踪 ← Phase 1 ✅

实践层 — 外显于行
  上下文预备（决策前查阅约定）
  反思/修复（post_task 六联闭环） ← Phase 1 ✅
  动态信任（遵守→信任↑→自主权↑→检索范围↑） ← Phase 1 ✅
  Issue 生命周期（约定→任务→追踪） ← Phase 2

演化层 — 迭代进步
  越用越聪明（worth 反馈闭环） ← Phase 3 ✅
  越用越默契（行为模式学习） ← Phase 3 ✅
  主动成长（curiosity→探索→实践） ← Phase 3 ✅
  内化约定（原则遵守历史量化） ← Phase 3 ✅

基础设施
  永久记忆存储（SQLite 写穿透） ← Phase 2 ✅
  依赖关系管理（blocks/blockedBy + 循环检测） ← Phase 2 ✅
  Issue 生命周期（open→in_progress→resolved→closed） ← Phase 2 ✅
  上下文预备（post_task 预取 + MCP 工具） ← Phase 4 ✅
  Bridge TODO（Pi 任务 + N.E.K.O ZMQ） ← Phase 4 ✅
  SSE 生产化（health + 日志 + 优雅关闭） ← Phase 4 ✅
```

## 三、当前状态

### 已完成
- 经验包系统（export/import/recall + 三条铁律） ✅
- 39 个 MCP 工具（韧性格局合并后 → 29 个） ✅
- 域联邦系统（6 行为域 + 1 通用原则域） ✅
- 12 条核心原则，按行为域分布（all/governing/building/designing/reflecting） ✅
- 域自演化三层闭环（流水线微进化 + 周期审计 + 检索反馈） ✅
- 灾难恢复：rebuild_from_memories() 从 tags 逆向重建域图谱 ✅
- 跨版本兼容：schema_version 迁移链 + pack 跨版本逃生舱 ✅
- 静默失效防护：DomainManager 降级开关 _dm_ok ✅
- 流式 pack_export（防 OOM）+ pack_import strategy 参数 ✅
- 联邦信号实时生成（不持久化） ✅
- 7 维审计 + 5 维 SCARF 自省
- 三层防线（L0 + L1 信任约束 + L2 免疫巡检）
- 分层检索（细=graph → 类=L1 boost + 域加权 → 粗=text+vector）
- 记忆流水线（raw→tagged(语义标签)→classified(域+Tier)→embedded→migrate）
- 记忆纠正（memory_correct — 人类可编辑）
- 实体自动链接（提取 → 图边 → 遍历）
- 多 Agent owner 隔离（共享域 + 独立域）
- SSE 传输 + 健康检查 + 优雅关闭
- FallbackEmbedder（零向量降级）
- L1/L3 分层 + EvolveR + MemoryGC
- SQLite 写穿透持久化 + schema_version 版本管理
- post_task 六联闭环（约定对齐→SCARF→激素→信任→反思→CEI）
- PrincipleTracker 原则遵守量化 + 趋势分析
- 信任分接入检索权重
- 修复建议自动生成
- worth 反馈闭环（access_count + EvolveR 联动）
- AgentBehaviorTracker 行为模式学习
- curiosity 自适应探索闭环
- Issue 生命周期 + 依赖管理
- 上下文预备（post_task 自动预取 + context_ready MCP）
- Bridge Pi 任务执行 + N.E.K.O ZMQ 转发

## 四、12 条核心约定

在每次决策前主动查阅（`principle_activate`）：

1. **奥卡姆剃刀** — 如无必要，勿增实体
2. **全过程可查可透明** — 每步有 git 痕迹
3. **自我审计闭环** — 根因→改良→教训→评分
4. **上下文驱动决策** — 无上下文不行动
5. **约定优于约束** — 检验存在不等于有效
6. **数据流驱动** — 追踪真实数据流
7. **器官互保** — 每个子系统保护整个系统
8. **工具即感官** — LLM 能力边界由工具链决定
9. **信任驱动约束** — 动态信任分调节自主权
10. **自演化闭环** — 评价驱动行为修正
11. **原则遗传** — 核心约定跨代传递
12. **代码即文档** — 代码本身是最权威的文档

## 五、操作方法

### 开发流程
1. 查阅原则 + 记忆 → 获取上下文
2. 设计 → brainstorming → spec → writing-plans
3. 实现 → subagent-driven-development
4. 每步 post_task → 记录约定对齐 + 反思 + CEI

### 实施约定
- 每次决策前先 `principle_activate` + `memory_recall`
- 每次完成后 `post_task(task_description, git_commit)`
- 信任分下降 → 缩小检索范围，增加审查
- 审计低分 → 自动生成修复建议
- 原则遵守率定期审视

### 当前指令
- 韧性专项：灾难恢复 + 跨版本兼容 + 静默失效防护 设计中
- 工具精简：39 → 29 个 MCP 工具
- 推送到 main 分支

## 六、关键接口

```python
# 约定层
PrincipleTracker().record(pid, adhered, context)
PrincipleTracker().stats()  # -> {pid: {adhered, violated, rate}}

# 实践层
SoulLoop().post_task(description, git_commit)  # -> 六联结果
TrustManager().get_retrieval_boost()  # -> 1.3/1.0/0.7/0.5
StepAuditor().suggest_repairs(result)  # -> [修复建议]
DomainManager().assign(tags)  # -> 域名字符串
DomainManager().stats()  # -> 全域统计 + 谱系

# 演化层
EvolveR(rec_mem).evolve_cycle()  # -> 演化统计
MemoryPipeline().process_pipeline()  # -> raw→tagged→classified→embedded→migrate

# 韧性
DomainManager().rebuild_from_memories(engine)  # -> 从 tags 全量重建域图谱
DomainManager().decay()  # -> 域衰减/萎缩检测
```

## 七、记忆准则

每次会话应将关键决策、Bug 修复、API 约定写入记忆：
- `memory_store(content, memory_type, owner)`
- 记录类型：`experience`（经验）/ `reflection`（反思）/ `knowledge`（知识）
- API 约定写入 `C:\Users\ALdai\.claude\projects\F--Agent-Memory-system\memory\`
