# Plastic Promise

> 记忆是可塑的，灵魂因记忆存在、因约定成长。

**Plastic Promise** 是以「约定工程」(Commitment Engineering) 替代「约束工程」的 AI 行为治理系统。29 个 MCP 工具覆盖记忆、原则、域联邦、上下文、审计、反思、系统、经验包八大域。内置灾难恢复、跨版本兼容、静默失效防护。

> 📋 完整架构、路线图和当前状态见 **[GOAL.md](GOAL.md)**。

---

## 12 条核心原则（按行为域分布）

| # | 原则 | 一句话 | 域 |
|---|------|--------|------|
| 1 | 奥卡姆剃刀 | 如无必要，勿增实体 | all |
| 2 | 全过程可查可透明 | 每步有 git 痕迹、可追溯审计日志 | all |
| 3 | 自我审计闭环 | 根因→改良→教训→评分 | reflecting |
| 4 | 上下文驱动决策 | 无上下文不行动，不足时标注而非猜测 | designing |
| 5 | 约定优于约束 | 检验存在不等于有效 | governing |
| 6 | 数据流驱动 | 追踪真实数据流，非假设架构图 | designing |
| 7 | 器官互保 | 每个子系统保护整个系统 | building |
| 8 | 工具即感官 | LLM 能力边界由工具链决定 | all |
| 9 | 信任驱动约束 | 动态信任分调节自主权 | governing |
| 10 | 自演化闭环 | 评价驱动行为修正 | reflecting |
| 11 | 原则遗传 | 核心约定跨 Agent 代际传递 | governing |
| 12 | 代码即文档 | 代码本身是最权威的文档 | building |

每条原则激活时返回：名称、内容、违反后果、遵循建议——决策参考，非门禁。

---

## 架构

```
MCP Server (29 tools, 8 domains)
├── 记忆域 (10): recall store update forget stats list gc correct + pipeline
├── 域联邦 (1):  domain(stats|merge|unmerge|rename|rebuild)
├── 原则域 (4):  activate inherit diffuse evaluate
├── 上下文 (4):  supply inject graph ready
├── 审计域 (4):  run pre_check defense(stats|adjust|status)
├── 反思域 (2):  scarf_reflect(mode=inertia) feedback
├── 系统域 (4):  stats issue_create/transition/list system(backup|migrate)
└── 经验包 (3):  pack_export pack_import(strategy) pack_recall

core/                       memory/               defense/
├── constants.py             ├── soul_memory.py    ├── soul_audit.py
├── context_engine.py        └── pipeline.py       └── soul_enforcer.py
├── domain_manager.py  ←── 域联邦核心
├── principles.py            reflection/           growth/
├── embedder.py              ├── soul_scarf.py     ├── soul_hormone.py
├── reranker.py              ├── soul_curiosity.py ├── soul_classifier.py
├── noise_filter.py          └── soul_proprio...   └── skill_extractor.py
└── step_auditor.py
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

### 域联邦系统 — Agent 行为域
```
6 行为域 + 1 通用原则域:
  building / fixing / designing / reflecting / governing / connecting / all

标签自动提取 → 域聚类 → 同名域融合
联邦信号 ≤200 字符，不深入细节
自演化三层闭环: 流水线微进化 + 周期审计 + 检索反馈
```

### 记忆流水线 — 必经之路
```
raw → tagged(语义标签+种子匹配) → classified(域+Tier分配) → embedded(向量) → 主池
Ollama 在线: 实时嵌入。离线: 零向量标注，待恢复后追补。
```

### 分层检索 — 域加权 + 细→类→粗
```
高置信(候选≥5 AND 命中率≥50%): 标签硬过滤 → domain加权 → text+vector精排 → 省token
低置信: 全量软加权 → domain加权 → text+vector → 兜底
同域记忆×1.3 联邦×1.1
```

### 经验包 — 随插随用
```
pack_export → 流式写盘(防OOM) → 冷备份文件
pack_import(strategy="skip"|"replace"|"merge") + version_mapper → 主池
pack_recall(strict=true) → 独立索引，不依赖DomainManager
```

### 韧性 — 可治愈、可兼容、可降级
```
灾难恢复: rebuild_from_memories() 从 tags 全量逆向重建域图谱
跨版本: schema_version 迁移链 + pack 跨版本逃生舱
静默失效: DomainManager 故障 → _dm_ok=False → 全量检索兜底 + pack_recall 保底
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| 嵌入 | Ollama mxbai-embed-large (1024d) / OpenAI text-embedding-3-small / FallbackEmbedder |
| 引擎 | Python 3.10+ |
| 存储 | SQLite 写穿透（默认开启）+ schema_version 版本管理 |
| 并发 | threading.RLock, WAL 模式 |
| 协议 | MCP (stdio + SSE + health 端点) |

---

## License

MIT
