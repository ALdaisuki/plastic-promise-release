# Engineering Exemplar-Driven Development — 设计规格

> 状态: 已确认 | 更新: 2026-07-02 | 方案: 方案 3（中庸方案）| 审阅: 通过（含 6 项澄清）

## 一、动机

当前开发流程中，"搜索成熟实现 → 提取模式 → 适配"是一个随机行为——有时做、有时不做，完全依赖开发者的记忆和习惯。本项目已经在实践中验证了这个模式的价值（参考 memory-lancedb-pro 的检索管道、TrustHandoff 的委托验证模式），但没有系统化。

**目标**：将"典范驱动开发"变成 SuperPowers 流水线的一等公民阶段，让"搜索典范"成为遇到工程问题时的第一步而非最后一步。

## 二、架构概览

```
context_supply 调用
       │
       ▼
exemplar_gap_detector  ← 新：缺口检测中间件
       │
       ├─ gap_signal == null → 正常流程（不变）
       │
       └─ gap_signal != null → Claude 看到信号
                                    │
                         ┌──────────┴──────────┐
                         │ 立即执行              │ auto_task → task_enqueue
                         │ exemplar-research    │ (research_exemplar)
                         └──────────┬──────────┘
                                    │
                              ┌─────┴─────┐
                              │ 记忆池     │
                              │ exemplar   │
                              │ 类型记忆   │
                              └─────┬─────┘
                                    │
                        下次 context_supply 自动召回
                        gap_signal = null ← 缺口已填补
```

**核心约束**：
- gap_detector 只做检测+信号，不做搜索，不产生副作用
- exemplar_research 只做研究+写入，不修改现有管道
- 委托系统是异步桥——gap_signal 可以当场执行，也可以入委托板等后续处理
- 记忆池是闭环终点——典范入库后，下次相似查询不再产生 gap_signal

## 三、组件设计

### 3.1 exemplar_gap_detector.py（~60 行，新文件）

**路径**：`plastic_promise/core/exemplar_gap_detector.py`

**职责**：context_supply 返回路径的中间件，检测知识缺口，构建 gap_signal。

**GapSignal 数据结构**：

```python
@dataclass
class GapSignal:
    type: str              # "exemplar_needed"
    problem: str           # 原始查询
    suggested_search: list[str]  # 2-3 个搜索关键词
    auto_task: bool        # 是否自动生成委托
    severity: str          # "high" | "medium" | "low"
```

**触发条件**（三层精细判断）：

```python
def detect_gap(query: str, pack: ContextPack) -> GapSignal | None:
    """在 context_supply 返回前调用。不做搜索，不产生副作用。"""
    if not _is_tech_query(query):
        return None
    # 核心层有结果 → 信息充足，不触发
    if pack.core:
        return None
    # related 层有 >= 3 条且全部 relevance > 0.45 → 质量足够，不触发
    if len(pack.related) >= 3 and all(
        getattr(item, 'relevance', 0) > 0.45 for item in pack.related
    ):
        return None
    # 核心层空 + related 不足 → 真正需要外部知识
    return GapSignal(
        type="exemplar_needed",
        problem=query,
        suggested_search=_extract_keywords(query),
        auto_task=True,
        severity="medium",
    )
```

这样区分了三种情况：
- `core` 非空 → 系统已有高质量信息，无缺口
- `core` 空但 `related` 有 ≥3 条且每条 relevance > 0.45 → 关联信息足够，无需外部研究
- `core` 空且 `related` 不足 → 知识缺口，触发 gap_signal

**ContextPack 修改**：新增可选字段 `gap_signal: GapSignal | None = None`。

**技术关键词列表**（`TECH_KEYWORDS`）：storage、engine、agent、memory、retrieval、api、schema、protocol、distributed、consensus、replication、caching、queue、stream、index、embedding、vector、pipeline、router、gateway、proxy、cache、lock、transaction、snapshot。

**关键词提取**（`_extract_keywords`）— 简单启发式，不引入新依赖：

```python
def _extract_keywords(query: str) -> list[str]:
    """从查询中提取 2-3 个搜索关键词。简单启发式，不引入 spacy/nltk。"""
    # 1. 分词（英文按空格 + 标点，中文按单字 bigram 已是复合词）
    # 2. 过滤停用词（中英文常用停用词表 ~200 词）
    # 3. 保留含大写字母的英文词（CAP 名词优先，如 "Rust", "SQLite"）
    # 4. 保留技术关键词表命中的词
    # 5. 合并相邻保留词为复合短语（如 "Rust" + "storage" + "engine" → "Rust storage engine"）
    # 6. 按优先级排序：复合短语 > 技术关键词 > 大写名词 > 普通名词
    # 7. 取前 3 个
    return keywords[:3]
```

**输出示例**：
- `"Rust 存储引擎怎么设计"` → `["Rust storage engine", "Rust", "storage"]`
- `"事件溯源 event sourcing 实现"` → `["event sourcing", "event", "sourcing"]`
- `"怎么提高 LanceDB 检索性能"` → `["LanceDB retrieval", "LanceDB", "retrieval"]`

**边界情况**：
- `total_items > 0` 但所有结果来自 related 层且得分 < 0.5 → 不触发 gap_signal。设计意图：有结果即视为系统能提供可用上下文。如果后续发现需要调整，可放宽条件为 `pack.core == [] and len(pack.related) < 2`。
- gap_signal 不持久化。忽略 → 下次相同查询再次触发。这是设计意图，不是缺陷。

**集成点**：`context_engine.py` 的 `supply()` 方法，在 `return pack` 之前加一行：

```python
pack.gap_signal = detect_gap(task_description, pack)
```

### 3.2 exemplar_research.py（~120 行，新文件）

**路径**：`plastic_promise/skills/exemplar_research.py`

**职责**：sp-stage `exemplar-research` 的阶段逻辑。遵循现有 sp-stage 模式。

**执行流程**：

```
1. skill_session_start("exemplar-research", task_description)
2. principle_activate("designing")  → 激活相关原则
3. 读取 gap_signal（如有）或从 task_description 提取搜索目标
4. WebSearch 搜索成熟实现
5. 三问法分析:
   a. 它解决了什么问题？
   b. 它怎么解决的？（提取核心模式：算法、数据结构、流程）
   c. 哪些部分不能直接用？（语言差异、架构差异、约束差异）
6. 写分析文档到 docs/superpowers/specs/engineering-patterns/（status=draft）
7. 质量审核（新增）:
   a. task_enqueue(type="verify_exemplar", to_agent="claude") 生成自审委托
   b. Claude 自审三问法完整性、代码片段可运行性、适配建议可行性
   c. task_verify(accepted) → 文档 status 更新为 reviewed
   d. task_verify(rejected) → 文档 status 保持 draft，标注修正项
8. 审核通过 → smart-remember(memory_type="exemplar") 双写入记忆池
9. 完成 → 典范自动出现在后续 context_supply 的召回结果中
```

**为什么需要审核步骤**：对齐约定工程的核心哲学——信息通过质量审核才能进入系统。典范分析的错误或不完整会通过记忆管道放大，导致后续设计决策基于错误前提。自审委托确保每条入库的 exemplar 都经过验证。

**三问法输出格式**（JSON，存到分析文档 frontmatter）：

```json
{
  "project": "典范项目名",
  "problem_solved": "它解决了什么工程问题",
  "core_pattern": "核心模式描述（算法/数据结构/流程）",
  "not_applicable": "不能直接复用的部分及原因",
  "reusable_parts": ["可复用点1", "可复用点2"],
  "adaptation_notes": "适配到本项目的具体建议"
}
```

**关键设计决策**：
- 步骤 4 用内置 `WebSearch` 工具，不需要新依赖
- 步骤 7 走 `smart-remember`（自动去重、分类、质量门控），确保 `memory_type="exemplar"` 被正确索引到 LanceDB 并可通过 `memory_recall` 召回
- 产出的典范适配方案不另存文件——通过记忆管道自动流入 writing-plans 的 `context_supply` 召回结果

### 3.3 engineering-patterns/ 目录结构

**路径**：`docs/superpowers/specs/engineering-patterns/`

```
engineering-patterns/
├── INDEX.md                          # 典范索引（按问题域分类）
├── _template.md                      # 分析文档模板
├── 2026-07-02-memory-lancedb-pro.md  # 每条典范一个文件
├── 2026-07-02-trusthandoff.md
└── ...
```

**INDEX.md 结构**（含 status 标记）：

```markdown
# 工程典范索引

## 存储引擎
- [memory-lancedb-pro](2026-07-02-memory-lancedb-pro.md) — 混合检索管道 (BM25+RRF+MMR) ✅ 已适配
- [rust-rrf-impl](2026-07-02-rust-rrf.md) — RRF 融合实现 ⚠️ 待验证

## 委托系统
- [TrustHandoff](2026-07-02-trusthandoff.md) — 可验证委托信任层 ✅ 已适配
- [ReDel](2026-07-02-redel.md) — 递归Agent生成 📝 草稿

## Agent通信
- [CrewAI](2026-07-02-crewai.md) — Agent间通信协议 🗑️ 已废弃
```

**status 标记**：

| 标记 | 含义 |
|------|------|
| 📝 草稿 | 分析文档已写，尚未审核 |
| ⚠️ 待验证 | 审核通过但尚未在实际项目中适配 |
| ✅ 已适配 | 模式已在项目中成功应用 |
| 🗑️ 已废弃 | 不再适用或被更好的典范替代 |

**分析文档模板**（`_template.md`）：

```markdown
---
project: <典范项目名>
url: <GitHub/论文链接>
date_analyzed: <YYYY-MM-DD>
status: draft          # draft | reviewed | adopted | deprecated
tags: []
---

# <项目名> — <一句话总结>

## 解决了什么问题？
## 怎么解决的？（核心模式）
## 哪些部分不能直接用？
## 可复用模式（代码片段/算法/数据结构）
## 适配到本项目的建议
## 审核记录
- [ ] 三问法完整性检查
- [ ] 代码片段可运行性验证
- [ ] 适配建议可行性评估
```

**命名规范**：`YYYY-MM-DD-<project-name>.md`，项目名用小写 + 连字符，如 `2026-07-02-memory-lancedb-pro.md`。

### 3.4 task_enqueue 增强

**新增 task_type**：`research_exemplar`

```python
task_enqueue(
    task_type="research_exemplar",
    title="研究 {topic} 的工程典范",
    to_agent="claude",
    priority=3,  # B级，低优先级，不阻塞核心委托
    description="当前 {module} 需要 {capability}。"
                "现有 {code_path} 缺少 {missing_component}。",
    payload={
        "problem": "...",
        "context": "...",
        "search_hint": ["关键词1", "关键词2"],
        "gap_signal": {...}  # 原始 gap_signal
    }
)
```

**去重检查**（新增，~8 行）：基于 `payload_hash` 的可靠去重。

```python
import hashlib

def _compute_payload_hash(problem: str, search_hint: list[str]) -> str:
    """SHA256 前 8 位，基于 problem + search_hint。"""
    seed = f"{problem}|{'|'.join(sorted(search_hint))}"
    return hashlib.sha256(seed.encode()).hexdigest()[:8]

# 入队前检查
payload_hash = _compute_payload_hash(problem, search_hint)
existing = conn.execute(
    "SELECT id FROM task_queue "
    "WHERE task_type = ? AND to_agent = ? AND status = 'pending' "
    "AND payload_hash = ? LIMIT 1",
    ("research_exemplar", "claude", payload_hash),
).fetchone()
if existing:
    return {"status": "duplicate", "existing_task_id": existing["id"]}
```

`payload_hash` 需要新增为 `task_queue` 表的字段（或存储在 payload JSON 中作为索引键）。存在则返回已有 task_id，不重复创建。

**委托参数说明**：
- `priority=3`（B 级）：不阻塞 S/A 级核心委托
- `to_agent="claude"`：典范研究需要综合判断力，由 Claude 执行
- `timeout_seconds=600`：研究需要搜索+阅读+分析，比普通委托更长

**新增 task_type**：`verify_exemplar` — 典范分析质量审核

```python
task_enqueue(
    task_type="verify_exemplar",
    title="审核 {topic} 的典范分析文档",
    to_agent="claude",
    priority=3,
    description="审核三问法完整性、代码片段可运行性、适配建议可行性",
    payload={
        "exemplar_doc_path": "docs/superpowers/specs/engineering-patterns/2026-07-02-xxx.md",
        "memory_id": "...",  # 待审核的 exemplar 记忆 ID（status=review_pending）
        "checklist": [
            "三问法完整性检查",
            "代码片段可运行性验证",
            "适配建议可行性评估",
        ],
    }
)
```

审核通过 → `task_verify(accepted)` → 文档 status 更新为 `reviewed` → 记忆正式入库。
审核打回 → `task_verify(rejected)` → 文档 status 保持 `draft`，标注修正项，返回 exemplar-research 修正。

### 3.5 SuperPowers 链约束更新

**SKILL_CHAIN_MAP 新增**：

```python
"exemplar-research": {
    "requires": ["brainstorming"],       # 前置：必须先 brainstorm
    "next": ["using-git-worktrees"],     # 后置：必须进入 worktrees
    "auxiliary": False,                  # 主链阶段，不可跳过
    "reentrant": True,                   # 允许从其他阶段切回
}
```

**新主链**：

```
brainstorming → exemplar-research → worktrees → writing-plans → ...
                                              ↑
                     reentrant: 任何阶段可切回 exemplar-research
                     完成后恢复到切出点（_current_stage）
```

**reentrant 语义**：
- writing-plans 或 executing-plans 阶段发现设计缺口 → 可切回 exemplar-research
- 完成 exemplar-research 全部步骤后，**恢复到切出点**（不重置主链，不回到 brainstorming）
- 恢复时 context_supply 再次调用，新入库的典范自动出现在上下文
- `_current_stage` 在切入时保存，完成时恢复。这由 sp-stage handler 的状态管理负责

**sp-stage 阶段映射**：

```python
# superpowers_stages.py
"exemplar-research": {
    "skill_name": "exemplar-research",
    "domain": "designing",
    "actor": "claude",
    "output": "典范分析文档 + 记忆入库 + 适配方案",
}
```

## 四、数据流（端到端）

### 路径 A：自动检测 → 委托

```
1. writing-plans 调用 context_supply("Rust 存储引擎设计")
2. 召回归空 + tech_query=True
3. gap_signal = {type:"exemplar_needed", problem:"Rust 存储引擎设计",
                 suggested_search:["Rust","storage","engine"], auto_task:true}
4. Claude 看到 gap_signal → 决定是否执行 exemplar-research
5. auto_task=True → task_enqueue(type="research_exemplar", ...)
6. 委托入板，等待 Claude 认领执行
```

### 路径 B：手动触发

```
1. sp-stage("exemplar-research", "研究事件溯源存储模式")
2. 无 gap_signal → 从 task_description 提取搜索意图
3. WebSearch → 三问法 → 写文档（status=draft）
4. task_enqueue(type="verify_exemplar") → Claude 自审
5. task_verify(accepted) → status=reviewed → smart-remember 入库
6. 完成，典范入库
```

### 闭环验证

```
1. 下次 context_supply("Rust 存储引擎")
2. 召回 exemplar 类型记忆（之前入库的典范）
3. gap_signal = null  ← 缺口已填补
4. 🔵核心上下文 包含典范模式 → writing-plans 直接使用
```

## 五、实施清单

| # | 文件 | 类型 | 行数 | 备注 |
|---|------|------|------|------|
| 1 | `plastic_promise/core/exemplar_gap_detector.py` | 新文件 | ~80 | 三层触发 + 启发式关键词提取 |
| 2 | `plastic_promise/skills/exemplar_research.py` | 新文件 | ~150 | 含审核步骤 + verify_exemplar 委托 |
| 3 | `plastic_promise/core/context_engine.py` | 修改 (+3行) | — | gap_signal 字段 |
| 4 | `plastic_promise/mcp/tools/task_queue.py` | 修改 (+10行) | — | payload_hash 去重 + research_exemplar + verify_exemplar |
| 5 | `plastic_promise/skills/superpowers_stages.py` | 修改 (+15行) | — | exemplar-research 阶段注册 + 链约束 |
| 6 | `docs/superpowers/specs/engineering-patterns/INDEX.md` | 新文件 | ~40 | 含 status 标记 |
| 7 | `docs/superpowers/specs/engineering-patterns/_template.md` | 新文件 | ~25 | 含 status + 审核记录 |

## 六、不做的事（明确边界）

- **不新增 MCP 工具**：典范研究走现有 sp-stage + memory_store + task_enqueue 管道。`research_exemplar` 和 `verify_exemplar` 是新增 task_type，复用现有 `task_enqueue`/`task_verify` 工具
- **不改 EntityGraph**：典范不新建实体节点类型，用标签 (`memory_type="exemplar"`) 关联
- **不新建 Daemon 扫描器**：先用半自动模式验证有效，后续可升级（预留 `scan_knowledge_gaps` 接口）
- **不修改 context_supply 的检索逻辑**：只附加 gap_signal 字段，不改变检索评分/排序
- **gap_signal 不持久化**：即时信号，不存储，不累积

## 七、升级路径（预留）

当典范驱动模式被充分验证后，可平滑升级到深度整合方案：

1. **EntityGraph 集成**：`exemplar` 作为实体节点类型，关联 `principle` / `code_module` / `task`
2. **exemplar MCP 工具**：`exemplar_search`、`exemplar_store`、`exemplar_link`
3. **scan_knowledge_gaps Daemon 扫描器**：定期扫描 context_supply 空召回日志，聚合相似缺口，自动生成委托
4. **context_graph 遍历**：支持查询"这个设计受哪个典范影响"
