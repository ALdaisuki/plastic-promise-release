# Plastic Promise

> 记忆是可塑的，灵魂因记忆存在、因约定成长。

**Plastic Promise** 是以「约定工程」(Commitment Engineering) 替代「约束工程」的 AI 行为治理系统。32 个 MCP 工具覆盖记忆、原则、上下文、审计、反思、系统六大域。

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

每条原则激活时返回：名称、内容、违反后果、遵循建议——作为决策参考，非门禁。

---

## 架构

```
┌─ MCP Server (28 tools) ────────────────────────────┐
│  stdio (Claude Code)  +  SSE :9020 (Pi / N.E.K.O) │
│                                                     │
│  记忆域 (10): recall store update forget stats      │
│              list gc fuzzy_status fuzzy_process      │
│              memory_correct                          │
│  原则域 (4) : activate inherit diffuse evaluate      │
│  上下文 (3): supply inject graph                     │
│  审计域 (5) : run pre_check report trust status      │
│  反思域 (3) : scarf_reflect inertia_check feedback   │
│  系统域 (3) : stats backup migrate                   │
├─────────────────────────────────────────────────────┤
│  core/                                               │
│  ├── constants.py      12 原则 + 7 维审计 + 配置      │
│  ├── context_engine.py 上下文引擎 (6 phase supply)     │
│  ├── principles.py     原则管理 (激活/继承/扩散/评价)  │
│  ├── embedder.py       Ollama + OpenAI + Fallback     │
│  ├── reranker.py       Cross-Encoder 重排序           │
│  ├── noise_filter.py   噪声过滤                        │
│  └── step_auditor.py   4 阶段审计 + 信任分联动         │
│                                                      │
│  memory/                defense/     reflection/       │
│  ├── soul_memory.py     soul_audit   soul_scarf        │
│  └── fuzzy_buffer.py    soul_enforcer soul_curiosity   │
│                         soul_proprioception            │
│  growth/                loop/         cron/             │
│  ├── soul_hormone       soul_loop     health_scan       │
│  ├── soul_classifier                  audit_daily       │
│  └── skill_extractor                  closure_guardian  │
└─────────────────────────────────────────────────────┘
```

---

## 快速开始

```bash
# 1. 安装依赖
pip install mcp uvicorn starlette requests

# 2. Claude Code 模式 (stdio，自动)
# .mcp.json 已配置，Claude Code 自动启动

# 3. 多 Agent SSE 模式 (Pi / N.E.K.O 连接)
set AGENT_OWNER=pi
python -m plastic_promise.mcp.server --sse 9020
# Pi 连接: http://127.0.0.1:9020/sse

# 4. 冷启动 (注入种子记忆)
python scripts/bootstrap.py
```

---

## 核心特性

### 分层检索 — 细→类→粗
```
context_supply / memory_recall:
  细 (graph ×1.0) → 类 (L1 boost ×1.5, tier priority) → 粗 (vector ×0.6)
  三路结果融合，graph 权重最高
```

### 模糊缓存区 — 先存后补
```
Ollama 离线时:
  memory_store → 打临时标签 → 放入 fuzzy buffer (raw 区)
  空闲时: raw → tagged → classified(大类) → embedded(细分) → 迁移主池
```

### 多 Agent 共享域 + 独立域
```
共享域 (所有 Agent 可见):  12 原则 + 实体图谱 + 审计引擎
独立域 (owner 隔离):       记忆检索自动过滤 owner=当前 agent + shared
```

### 实体自动链接
```
memory_store → 自动提取内容中的原则名/实体名 → 写入 entity_ids + 建图边
memory_recall → 沿实体关系遍历 → 返回关联记忆 (source: "entity-link")
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| 嵌入 | Ollama mxbai-embed-large / OpenAI text-embedding-3-small / FallbackEmbedder |
| 引擎 | Python 3.10+ 纯 Python (ContextEngine) |
| 存储 | 内存 dict (生产建议 SQLite / LanceDB) |
| 协议 | MCP (stdio + SSE, 28 tools) |
| 多 Agent | SSE transport + owner 字段隔离 |

---

## License

MIT
