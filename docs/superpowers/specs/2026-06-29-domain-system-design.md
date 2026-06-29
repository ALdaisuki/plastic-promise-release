# Domain System — 记忆与原则的域联邦设计

> 状态: 待评审 | 日期: 2026-06-29

## 一、问题

- 原则有 `domain` 字段（work/life/all），但 work/life 为空壳，12 条全堆在 all
- 记忆没有 `domain` 字段，检索是扁平全量扫描
- 现有字段 `scope` 和 `category` 与 domain 功能重叠但从未真正使用
- 系统缺乏记忆-原则之间的语义组织层

## 二、目标

1. 记忆和原则各自拥有域空间，同名域自动联邦融合
2. 域基于 Agent 实际行为聚类，不模仿人类生活分类
3. 动态自进化：域可创建、合并、衰减、萎缩
4. 高置信标签检索省 token，低置信 fallback 全量兜底
5. 遵循最高原则：奥卡姆剃刀（零净增字段）、全过程可查（audit log）
6. 为后续高并发预留设计空间

## 三、初始行为域

基于 Agent 实际行为聚类（22 条已有记忆 + 系统 task_type 体系）：

| 域 | 说明 | 初始标签种子 | 来源 |
|----|------|-------------|------|
| **building** | 代码生成、功能实现 | coding, implement, generate, build | code_generation, refactoring |
| **fixing** | 调试、修 bug、排查 | debug, fix, error, bug, trace | debugging |
| **designing** | 架构设计、系统规划 | architect, design, plan, structure | architecture, code_review |
| **reflecting** | 自我审计、SCARF、教训 | audit, scar, reflect, lesson, review | learning |
| **governing** | 原则遵守、信任、约束 | principle, trust, govern, policy | (独有) |
| **connecting** | 多 Agent 通信、桥接 | bridge, agent, message, sync | collaboration |
| **all** | 通用型原则 | (不可分配记忆) | general |

## 四、原则域分配

12 条原则从 all 分散到 4 个域：

### all（通用型，3 条）
不可融合、不可分配记忆、跨所有行为生效：
- **1. 奥卡姆剃刀** — 任何行为都需要
- **2. 全过程可查可透明** — 任何行为都需要
- **8. 工具即感官** — 通用认知

### governing（治理，3 条）
- **5. 约定优于约束** — 治理核心
- **9. 信任驱动约束** — 治理核心
- **11. 原则遗传** — 跨代传递

### building（构建，2 条）
- **7. 器官互保** — 子系统防护
- **12. 代码即文档** — 编码实践

### designing（设计，2 条）
- **4. 上下文驱动决策** — 设计需要上下文
- **6. 数据流驱动** — 需要追踪数据流

### reflecting（反思，2 条）
- **3. 自我审计闭环** — 反思核心
- **10. 自演化闭环** — 反思核心

### fixing 和 connecting
无专属原则，通过联邦信号从兄弟域获取指导：
- fixing ← reflecting（根因分析）+ building（代码理解）
- connecting ← governing（信任传递）+ all（可追溯通信）

## 五、数据模型

### MemoryRecord 变化

```
新增:
  tags: list[str]     — 多标签，流水线 tagged 阶段生成
  domain: str         — 域标签，classified 阶段聚类得出，默认 ""（未分类）

删除:
  scope: str          — 功能被 domain 完全覆盖
  category: str       — 功能被 domain 完全覆盖

结果: 净零字段增长
```

### SQLite 变化

```sql
-- memories 表
ALTER TABLE memories ADD COLUMN tags TEXT;       -- JSON array
ALTER TABLE memories ADD COLUMN domain TEXT DEFAULT '';  -- '' = 未分类，流水线 classified 阶段填充
-- scope 和 category 列保留但标记 deprecated，不删除（向后兼容）

-- 新表
CREATE TABLE domains (
    name TEXT PRIMARY KEY,
    score REAL,
    tags TEXT,           -- JSON array
    merged_from TEXT,    -- JSON array，谱系追溯
    created_at TEXT,
    last_active TEXT
);

CREATE TABLE domain_signals (
    source_domain TEXT,
    target_domain TEXT,
    signal TEXT,         -- ≤200 字符摘要
    updated_at TEXT,
    PRIMARY KEY (source_domain, target_domain)
);
```

### 标签索引（纯内存）

```python
# 启动时从 SQLite 重建，O(n) 一次性扫描
tag_index: dict[str, set[str]]   # tag → set[memory_id]
# 例: {"coding": {m1,m3}, "pipeline": {m5,m7}}
```

### DomainManager

```python
class DomainInfo:
    name: str
    score: float           # 0.0-1.0，动态
    tags: set[str]         # 域下所有标签
    merged_from: list[str] # 合并谱系
    parent: str | None     # 被合并到的目标域
    memory_count: int
    principle_ids: list[int]

class DomainManager:
    domains: dict[str, DomainInfo]        # 域注册表
    tag_to_domain: dict[str, str]         # 标签→域反向索引

    def assign(self, tags: list[str]) -> str: ...
    def merge(self, source: str, target: str) -> None: ...
    def unmerge(self, source: str) -> None: ...
    def decay(self) -> list[str]: ...     # 返回萎缩的域列表
    def signal(self, from_d: str, to_d: str, msg: str) -> None: ...
```

## 六、流水线改动

```
raw → tagged → classified → embedded → migrate
        ↑            ↑
   多标签提取     tier + domain
   (规则+LLM)    (标签聚类→域分配)
```

### tagged 阶段: `_extract_semantic_tags`

```python
def _extract_semantic_tags(self, content: str) -> list[str]:
    # 1. 规则层（免费）: CJK bigram + 关键词正则
    # 2. 语义层（可选，Ollama 可用时）: LLM 提取 3-5 个语义标签
    #    提示词: "为这段内容生成3-5个标签，用逗号分隔。标签描述领域/主题/技术栈。"
    # 3. 合并去重，上限 10 个
```

### classified 阶段: domain 聚类

```python
def _assign_domain(self, tags: list[str]) -> str:
    # 1. 对每个 tag，查 tag_to_domain 映射
    # 2. 取匹配数最多的 domain（排除 all）
    # 3. 最高分 >0.3 → 归入；≤0.3 → 候选新域
    # 4. 候选新域累积标签 ≥3 个且记忆 ≥5 条 → 正式域
```

### 标签索引同步

classified 阶段处理每条记录后，同步更新 `tag_index` 和 `tag_to_domain`。全内存操作，无 SQL 开销。

## 七、检索层

### 高置信 / 低置信分层

```
query → 提取 tags
         │
         ├─ 候选集 C = tag_index 命中并集
         │
         ├─ 高置信判定（满足其一即可）:
         │     |C| ≥ 5           → 标签覆盖面够大
         │     命中率 ≥ 60%      → 大多数 query tags 在索引中
         │
         ├─ 高置信: 只排 C
         │    精排 = text(C) + vector(C) + 域加权
         │    省 token（候选集远小于全量）
         │
         └─ 低置信: fallback 全量
              精排 = text(全量) + vector(全量) + 标签软加权 + 域软加权
              不省 token，兜底保召回
```

### 联邦信号注入

```
检索 domain="building" 时:
  ├─ 返回 building 域记忆（优先）
  ├─ 返回 building 域原则
  └─ 追加融合域信号摘要:
       fixing → building: "根因分析完成，3条新发现"
       (只出信号，不出 fixing 的原始记忆)
```

### memory_recall 参数变化

```
新增:
  domain_hint: str = None     # 可选，有域倾向时传入
  federation: bool = True     # 是否追加联邦信号
```

## 八、自进化三层闭环

### 第一层：流水线微进化

每次 `process_pipeline` 触发：
- 新标签自动加入 `tag_index`
- 标签聚类与已有域重叠度检测
- 候选新域累积（≥3 标签 + ≥5 记忆 → 转正，score=0.5）

### 第二层：周期审计结构进化

`audit_run` 或 `health_scan` 钩子触发：
- **重叠度检测**: 两域标签重叠 >40% → 建议合并
- **衰减检测**: 7 天无新增 + access_count 零增长 → score 衰减
- **萎缩处理**: score <0.1 → 并入最相似兄弟域
- **curiosity 探索**: 随机采样两个域各 1 条记忆，测试是否该合并

### 第三层：检索反馈闭环

每次 `memory_recall` 结果被标记 adopted/ignored/rejected：
- **adopted** → 匹配域 score +0.01，标签强化
- **ignored** → 匹配域 score -0.005
- **rejected** → 匹配域 score -0.02，触发域重评估
- **低置信查询**（命中 <60%）→ tags 记入待观察池
  - 待观察池同一标签出现 ≥3 次 → 触发新域候选检测

### 安全保证

```
所有进化操作:
  ├─ 域合并 → merged_from 保留谱系（可 unmerge）
  ├─ 域萎缩 → 记忆迁入兄弟域，不删除
  ├─ 标签变更 → 旧标签保留 30 天作为别名
  └─ 所有变更写入 audit log（原则 #2: 全过程可查可透明）
```

## 九、新增 MCP 工具

| 工具 | 用途 |
|------|------|
| `domain_stats` | 查看所有域：标签数、记忆数、得分、谱系、原则数 |
| `domain_merge` | 手动合并两个域（覆盖自动阈值） |
| `domain_unmerge` | 手动解除合并（从 merged_from 谱系恢复） |

## 十、改动面

| 文件 | 改动 |
|------|------|
| `memory/pipeline.py` | tagged: 升级标签提取；classified: 新增 domain 聚类 |
| `core/context_engine.py` | MemoryRecord: +tags +domain -scope -category；SQLite schema；检索加权 |
| `core/constants.py` | 12 条原则 redistributed 到 4 个域 |
| `memory/soul_memory.py` | 兼容 tags/domain 字段 |
| `mcp/tools/memory.py` | memory_recall 新增 domain_hint + federation 参数 |
| `mcp/tools/principles.py` | principle_activate 返回新增 domain 信息 |
| 新: `core/domain_manager.py` | DomainManager + DomainInfo + 标签索引 |
| 新: `mcp/tools/domain.py` | domain_stats / domain_merge / domain_unmerge 处理函数 |
| `mcp/server.py` | 注册 3 个新 domain 工具 |
| `mcp/tools/__init__.py` | 导出 domain 模块 |

## 十一、不做什么

- 不建独立的信号总线/消息队列（domain_signals 表足够）
- 不迁 Rust（DomainManager 是协调逻辑，非计算密集）
- 不删 scope/category 列（保留向后兼容，标记 deprecated）
- 不给 fixing 和 connecting 强行分配原则
- 不做硬标签过滤（只做软加权 + 高/低置信分层）
