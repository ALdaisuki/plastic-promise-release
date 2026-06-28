# Plastic Promise

> 记忆是可塑的，灵魂因记忆存在、因约定成长。

**Plastic Promise** 是以「约定工程」(Commitment Engineering) 替代「约束工程」的 AI 行为治理系统。35 个 MCP 工具覆盖记忆、原则、上下文、审计、反思、系统、经验包七大域。

> 📋 完整架构、路线图和当前状态见 **[GOAL.md](GOAL.md)**。

---

## 12 条核心原则

| # | 原则 | 一句话 |
|---|------|--------|
| 1 | 奥卡姆剃刀 | 如无必要，勿增实体 |
| 2 | 全过程可查可透明 | 每步有 git 痕迹、可追溯审计日志 |
| 3 | 自我审计闭环 | 根因→改良→教训→评分 |
| 4 | 上下文驱动决策 | 无上下文不行动，不足时标注而非猜测 |
| 5 | 约定优于约束 | 检验存在不等于有效 |
| 6 | 数据流驱动 | 追踪真实数据流，非假设架构图 |
| 7 | 器官互保 | 每个子系统保护整个系统 |
| 8 | 工具即感官 | LLM 能力边界由工具链决定 |
| 9 | 信任驱动约束 | 动态信任分调节自主权 |
| 10 | 自演化闭环 | 评价驱动行为修正 |
| 11 | 原则遗传 | 核心约定跨 Agent 代际传递 |
| 12 | 代码即文档 | 代码本身是最权威的文档 |

每条原则激活时返回：名称、内容、违反后果、遵循建议——决策参考，非门禁。

---

## 架构

```
MCP Server (35 tools)
├── 记忆域 (7): recall store update forget stats list gc
├── 流水线 (2):  pipeline_status pipeline_process
├── 纠正 (1):    memory_correct
├── 原则域 (4):  activate inherit diffuse evaluate
├── 上下文 (4):  supply inject graph ready
├── 审计域 (5):  run pre_check report trust status
├── 反思域 (3):  scarf_reflect inertia_check feedback
├── 系统域 (6):  stats backup migrate issue_create/transition/list
└── 经验包 (3):  pack_export pack_import pack_recall

core/                       memory/               defense/
├── constants.py             ├── soul_memory.py    ├── soul_audit.py
├── context_engine.py        └── pipeline.py       └── soul_enforcer.py
├── principles.py
├── embedder.py              reflection/           growth/
├── reranker.py              ├── soul_scarf.py     ├── soul_hormone.py
├── noise_filter.py          ├── soul_curiosity.py ├── soul_classifier.py
└── step_auditor.py          └── soul_proprio...   └── skill_extractor.py
                             loop/                 cron/
pack.py  issue.py            └── soul_loop.py      ├── health_scan.py
behavior.py                                          ├── audit_daily.py
                                                     └── closure_guardian.py
```

---

## 快速开始

```bash
pip install mcp uvicorn starlette requests

# Claude Code (stdio，自动)
# .mcp.json 已配置

# 多 Agent SSE (Pi / N.E.K.O)
set AGENT_OWNER=pi
python -m plastic_promise.mcp.server --sse 9020
```

---

## 核心特性

### 记忆流水线 — 必经之路
```
所有记忆: raw → tagged(关键词) → classified(大类L1/L3) → embedded(细分向量) → 主池
Ollama 在线: 实时嵌入。离线: 零向量标注，待恢复后追补。
```

### 分层检索 — 细→类→粗
```
语义向量(细) → 图谱遍历(细) → L1优先级(类) → 文本匹配(粗)
三路融合，Ollama mxbai-embed-large 提供语义维度
```

### 经验包 — 随插随用
```
pack_export → JSON 文件(可 git 分享) → pack_import → 主池
pack_recall(strict=true) → 只从记忆中提取，0匹配返回空（不瞎编）
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| 嵌入 | Ollama mxbai-embed-large (1024d) / OpenAI text-embedding-3-small |
| 引擎 | Python 3.10+ |
| 存储 | SQLite 写穿透（默认开启，重启不丢） |
| 协议 | MCP (stdio + SSE) |

---

## License

MIT
