# Domain System — 记忆与原则的域联邦设计

> 状态: 已评审修订 | 日期: 2026-06-29 | 评审轮次: 1

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
6. 线程安全，为后续高并发预留设计空间

## 三、初始行为域

基于 Agent 实际行为聚类（22 条已有记忆 + 系统 task_type 体系）：

| 域 | 说明 | 初始标签种子 | 初始得分 | 来源 |
|----|------|-------------|---------|------|
| **building** | 代码生成、功能实现 | coding, implement, generate, build | 1.0 | code_generation, refactoring |
| **fixing** | 调试、修 bug、排查 | debug, fix, error, bug, trace | 1.0 | debugging |
| **designing** | 架构设计、系统规划 | architect, design, plan, structure | 1.0 | architecture, code_review |
| **reflecting** | 自我审计、SCARF、教训 | audit, scar, reflect, lesson, review | 1.0 | learning |
| **governing** | 原则遵守、信任、约束 | principle, trust, govern, policy | 1.0 | (独有) |
| **connecting** | 多 Agent 通信、桥接 | bridge, agent, message, sync | 1.0 | collaboration |
| **all** | 通用型原则 | (不可分配记忆) | 1.0 (锁定) | general |

预定义域初始得分 = 1.0，自动发现域初始得分 = 0.3。

## 四、原则域分配

12 条原则从 all 分散到 4 个域，**同步更新 `core/constants.py` 中每条原则的 `domain` 字段**：

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
  domain: str         — 域标签，classified 阶段聚类得出，默认 "uncategorized"

删除:
  scope: str          — 功能被 domain 完全覆盖（列保留，标记 deprecated）
  category: str       — 功能被 domain 完全覆盖（列保留，标记 deprecated）

结果: 净零字段增长
```

**"uncategorized" 语义**：流水线未处理或无法分类的记忆。参与低置信 fallback 检索，不参与高置信检索。不与其他域合并。永不自动分配进 "all"。

### SQLite 变化

```sql
-- memories 表
ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memories ADD COLUMN domain TEXT NOT NULL DEFAULT 'uncategorized';
-- scope 和 category 列保留但标记 deprecated，不删除（向后兼容）

-- 域注册表
CREATE TABLE domains (
    name TEXT PRIMARY KEY,
    score REAL NOT NULL DEFAULT 0.3,
    tags TEXT NOT NULL DEFAULT '[]',       -- JSON array
    merged_from TEXT NOT NULL DEFAULT '[]',-- JSON array，谱系追溯
    parent TEXT,                            -- 被合并到的目标域
    status TEXT NOT NULL DEFAULT 'active', -- active / merged / atrophied
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed TEXT,
    created_at TEXT NOT NULL,
    last_active TEXT NOT NULL
);

-- 联邦信号
CREATE TABLE domain_signals (
    source_domain TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    signal TEXT NOT NULL,                   -- ≤200 字符摘要
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_domain, target_domain)
);

-- 审计日志
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    operation TEXT NOT NULL,                -- domain_merge / domain_create / domain_decay / tag_alias / domain_rename
    detail TEXT NOT NULL                    -- JSON
);
```

### 标签索引（纯内存）

```python
# 启动时从 SQLite 重建，O(n) 一次性扫描
tag_index: dict[str, set[str]]       # tag → set[memory_id]
# 例: {"coding": {m1,m3}, "pipeline": {m5,m7}}

# 别名映射（旧标签保留 30 天）
alias_map: dict[str, tuple[str, float]]
# key=旧标签, value=(主标签, 过期时间戳)
# 定期清理过期项
```

### DomainManager

```python
import threading

class DomainInfo:
    name: str
    score: float           # 0.0-1.0，动态
    tags: set[str]         # 域下所有标签
    merged_from: list[str] # 合并谱系
    parent: str | None     # 被合并到的目标域
    status: str            # active / merged / atrophied
    memory_count: int
    principle_ids: list[int]
    access_count: int
    last_accessed: str
    created_at: str
    last_active: str

class DomainManager:
    _lock: threading.Lock              # 保护所有写操作
    domains: dict[str, DomainInfo]     # 域注册表（含预定义域）
    tag_to_domain: dict[str, set[str]] # 标签→域集合（一对多）
    candidate_domains: dict[str, Counter]
    # candidate_domains["<新域名>"] = Counter({"coding":3, "build":2})

    def assign(self, tags: list[str]) -> str:
        """线程安全。返回域名字符串。"""
        ...

    def merge(self, source: str, target: str) -> None:
        """线程安全。合并后 source.status='merged', target.merged_from 追加。"""
        ...

    def unmerge(self, source: str) -> None:
        """线程安全。从 merged_from 谱系恢复。"""
        ...

    def rename(self, old: str, new: str) -> None:
        """线程安全。更新所有记忆和原则的 domain 字段。"""
        ...

    def decay(self) -> list[str]:
        """线程安全。返回萎缩的域列表。"""
        ...

    def signal(self, from_d: str, to_d: str, msg: str) -> None:
        """线程安全。更新联邦信号。"""
        ...

    def stats(self) -> dict:
        """只读，不加锁。返回所有域统计。"""
        ...
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

### classified 阶段: domain 聚类 + tie-breaking

```python
def _assign_domain(self, tags: list[str]) -> str:
    # 1. 对每个 tag，查 tag_to_domain 映射（一对多）
    # 2. 统计每个域匹配的标签数
    # 3. 按以下优先级 tie-break（依次比较）:
    #    a) 匹配标签数最多
    #    b) 域 score 最高
    #    c) 域创建时间最早
    # 4. 最高分 >0.3 → 归入该域
    # 5. 最高分 ≤0.3 → 进入候选新域流程:
    #    - candidate_domains 中累积标签计数
    #    - 候选域标签数 ≥2 且关联记忆数 ≥3 → 进入观察期
    #    - 观察期内记忆数 ≥5 → 转正为正式域 (score=0.5)
    #    - 新域名称: 取出现最多的标签作为域名
    # 6. 完全无法匹配 → 返回 "uncategorized"
```

### 标签索引同步

classified 阶段处理每条记录后，同步更新 `tag_index`、`tag_to_domain` 和 `candidate_domains`。全内存操作，无 SQL 开销。每次 `process_pipeline` 调用后即时更新候选域计数器。

## 七、检索层

### 高置信 / 低置信分层（复合条件）

```
query → 提取 tags
         │
         ├─ 候选集 C = tag_index 命中记忆的并集
         │
         ├─ 高置信判定（必须同时满足 OR 覆盖面极广）:
         │     (|C| ≥ 5 AND 命中率 ≥ 50%)     ← tags 有一定覆盖面
         │     OR |C| ≥ 20                      ← 候选集够大，默认可信
         │     阈值可通过配置文件调整
         │
         ├─ domain="" (uncategorized) 的记忆:
         │     ├─ 高置信检索 → 排除
         │     └─ 低置信检索 → 参与
         │
         ├─ 高置信: 只排 C
         │    精排 = text(C) + vector(C) + 域加权
         │    省 token（候选集远小于全量）
         │
         └─ 低置信: fallback 全量
              精排 = text(全量) + vector(全量) + 标签软加权 + 域软加权
              不省 token，兜底保召回
```

### 联邦信号生成与注入

信号在检索时自动生成（非周期任务）：

```
检索 domain="building" 时:
  │
  ├─ 1. 返回 building 域记忆（优先）
  ├─ 2. 返回 building 域原则（来自 constants.py）
  ├─ 3. 若检索结果中包含跨域记忆（如 fixing 域的记忆被命中）:
  │       → 自动生成信号: "building 检索命中 fixing 记忆 3 条"
  │       → 写入 domain_signals 表
  │       → 检索结果末尾追加一条 signal 类型记录（摘要≤200字符，不影响排序权重）
  └─ 4. 同名域联邦融合:
         若指定 domain="building":
           返回 building 域记忆 + building 域原则
           + 其他域中与 building 有信号关联的原则（联邦可见性）
```

### 原则与记忆联邦融合

```
指定域 D 时:
  ├─ D 域记忆（本域）
  ├─ D 域原则（同名原则域）
  ├─ 融合域信号摘要（跨域可见但不深入细节）
  └─ 域 D 下记忆和原则相互关联 → 检索权重 +0.1
```

### memory_recall 参数变化

```
新增:
  domain_hint: str = None     # 可选，有域倾向时传入
  federation: bool = True     # 是否追加联邦信号
```

## 八、自进化三层闭环

### 第一层：流水线微进化

每次 `process_pipeline` 处理完成后触发：
- 新标签自动加入 `tag_index`（内存）
- 各域标签与候选新域标签重叠度检测
- `candidate_domains` 计数器更新（跨记忆状态，内存 Counter）
- 候选域达到转正条件（≥2 标签 + ≥5 记忆，含观察期）→ 创建正式域 (score=0.5)
- 每次处理单条记忆，开销可控

### 第二层：周期审计结构进化

触发周期：**每次 `audit_run` 调用时**（CLAUDE.md 约定 Agent 定期调用）或 **每 100 条记忆处理后自动触发**。

- **重叠度检测**: 使用 **Jaccard 相似度** |A∩B| / |A∪B| > 0.4 → 建议合并
  - 自动生成建议，人工通过 `domain_merge` 确认
- **衰减检测**: 7 天无新增 **AND** `domains.access_count` 零增长 → score 每 7 天 ×0.8
- **萎缩处理**: score < 0.1 时：
  - 找 Jaccard 相似度最高的兄弟域
  - 批量更新记忆 domain → 兄弟域
  - 被合并域 status='merged', parent=兄弟域
  - 记录 audit_log
- **curiosity 探索**: 随机采样两个域各 1 条记忆，测试是否该合并

在定期审计时异步执行，避免阻塞主流程。

### 第三层：检索反馈闭环

依赖机制：`memory_recall` 返回候选列表时，每条结果携带 `domain` 和 `memory_id`。上层 Agent 通过 `feedback_apply` 回调标记 adopted/ignored/rejected。

- **adopted** → 匹配域 score +0.01，domains.access_count +1, domains.last_accessed 更新
- **ignored** → 匹配域 score -0.005
- **rejected** → 匹配域 score -0.02，触发域重评估（检查是否域标签漂移）
- **低置信查询**（命中 <60%）→ tags 记入待观察池
  - 待观察池同一标签出现 ≥3 次 → 触发新域候选检测
- 若无回调机制 → 该层退化为仅靠 access_count 自增（domains.access_count 在每次检索命中时 +1）

### 安全保证

```
所有进化操作:
  ├─ 域合并 → merged_from 保留谱系（可 unmerge），audit_log 写入记录
  ├─ 域萎缩 → 记忆迁入兄弟域，不删除，audit_log 写入记录
  ├─ 域重命名 → domain_rename 自动更新所有关联记忆和原则
  ├─ 标签变更 → alias_map 保留旧标签 30 天作为别名，定期清理过期项
  └─ 所有变更写入 audit_log（原则 #2: 全过程可查可透明）
```

## 九、新增 MCP 工具

| 工具 | 用途 |
|------|------|
| `domain_stats` | 查看所有域：标签数、记忆数、原则数、得分、合并谱系、最后活跃时间、status |
| `domain_merge` | 手动合并两个域（覆盖自动阈值） |
| `domain_unmerge` | 手动解除合并（从 merged_from 谱系恢复） |
| `domain_rename` | 重命名域，自动更新所有关联记忆和原则的 domain 字段 |

## 十、线程安全与高并发

- **DomainManager**: `threading.Lock` 保护所有写操作（assign / merge / unmerge / rename / decay / signal）
- **读操作**: 不加锁（Python dict 读是线程安全的），仅在 `stats()` 时做快照复制
- **定期审计**: 异步执行（`threading.Thread` 或 `audit_run` 钩子内），不阻塞主流程
- **标签索引**: 纯内存 HashMap，单机可支撑万级记忆
- **扩展预留**: 若规模扩大到十万级以上，tag_index 可迁移至 Redis（接口一致，替换实现即可）。SQLite WAL 模式已开启，读写不互斥

## 十一、改动面

| 文件 | 改动 |
|------|------|
| `core/constants.py` | 12 条原则 `domain` 字段更新（all→governing/building/designing/reflecting） |
| `memory/pipeline.py` | tagged: 升级标签提取；classified: 新增 domain 聚类 + tie-breaking + candidate_domains |
| `core/context_engine.py` | MemoryRecord: +tags +domain -scope(category)标记deprecated；SQLite 建表/迁移；检索加权 + 置信度分层 |
| `core/domain_manager.py` (新) | DomainManager + DomainInfo + candidate_domains + alias_map + Lock |
| `memory/soul_memory.py` | tags/domain 字段兼容（序列化/反序列化） |
| `mcp/tools/memory.py` | memory_recall 新增 domain_hint + federation 参数；检索结果追加信号记录 |
| `mcp/tools/principles.py` | principle_activate 返回新增 domain 信息 |
| `mcp/tools/domain.py` (新) | domain_stats / domain_merge / domain_unmerge / domain_rename |
| `mcp/server.py` | 注册 4 个新 domain 工具 |
| `mcp/tools/__init__.py` | 导出 domain 模块 |
| `core/step_auditor.py` | audit_run 钩子触发域重叠度检测 + 衰减检测 |
| 迁移脚本 | SQLite ALTER TABLE + domains/domain_signals/audit_log 建表 |

## 十二、不做什么

- 不建独立的信号总线/消息队列（domain_signals 表足够）
- 不迁 Rust（DomainManager 是协调逻辑，非计算密集）
- 不删 scope/category 列（保留向后兼容，标记 deprecated）
- 不给 fixing 和 connecting 强行分配原则
- 不做硬标签过滤（只做软加权 + 高/低置信分层）
- 记忆永远不会被自动分配进 "all" 域
