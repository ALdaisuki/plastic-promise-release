# Plastic Promise

> 塑性灵魂：记忆是可塑的，灵魂因记忆存在、因约定成长。

**Plastic Promise** 是一个以「约定工程」(Commitment Engineering) 替代「约束工程」的 AI 行为治理系统。LLM 是神经中枢，Plastic Promise 是它的完整数字身体。

---

## 三条核心原则

| # | 原则 | 内容 |
|---|------|------|
| **1** | **奥卡姆剃刀** | 如无必要，勿增实体。每一步只做当前最必要的事。 |
| **2** | **全过程可查可透明** | 每步有完整 git 痕迹、可追溯审计日志、可验证中间产物。 |
| **3** | **自我审计闭环** | 根因分析 → 改良措施 → 教训提炼 → 量化评分 → 驱动信任分和 CEI。 |

---

## 架构

```
┌─ MCP Server ─────────────────────────────────────┐
│  25 tools (memory/principles/context/audit/       │
│  reflection/management) + 5 Resources + 3 Prompts │
├─ Python 编排层 ───────────────────────────────────┤
│  core/       constants + ContextEngine wrapper     │
│  memory/     soul_memory (RecMem + MemoryWorth)    │
│  loop/       soul_loop (pre_task_v2 + post_task)  │
│  principles/ soul_principles (activate/inherit)   │
│  reflection/ soul_scarf + proprioception +        │
│              soul_curiosity                       │
│  defense/    soul_enforcer (L0/L1) + soul_audit   │
│  growth/     soul_hormone + classifier + skill    │
│  embedder.py Ollama mxbai-embed-large (1024d)     │
│  step_auditor.py 每步审计 + 评分闭环                │
├─ Rust 引擎 ───────────────────────────────────────┤
│  storage/    StorageBackend(SQLite) + VectorIndex │
│              + FtsIndex (cosine + word-overlap)    │
│  retrieval/  HybridRetriever + fusion + diversity │
│  domain/     Tier(4层) + WeibullDecay +           │
│              WilsonWorth + EvolveRConsolidator     │
│  context_engine.rs  supply() 6-phase pipeline     │
│  entity_graph.rs   原则图谱注入                     │
├─ Bridge ──────────────────────────────────────────┤
│  bridge/     neko_adapter + soul_bridge +          │
│              bus_client + http_memory              │
└────────────────────────────────────────────────────┘
```

---

## 快速开始

```bash
# 1. 启动 Ollama（嵌入模型）
ollama serve
ollama pull mxbai-embed-large

# 2. 冷启动：注入原则 + 种子记忆
python bootstrap.py

# 3. 验证检索
python -c "
from plastic_promise.embedder import get_embedder
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
v = get_embedder().embed('Rust性能优化')
pack = e.supply('Rust性能优化', v, 'code_generation', 'global')
print(f'core={len(pack.core)} related={len(pack.related)} divergent={len(pack.divergent)}')
"

# 4. 评分闭环
python -c "
from plastic_promise.step_auditor import StepAuditor
a = StepAuditor()
r = a.audit_step('实现功能X', 'abc1234', root_cause='用户需要', improvement='提取模式', lesson='解耦')
print(f'Score: {r.overall_score}, CEI: {a.get_cei()}')
"
```

---

## 项目结构

```
plastic_promise/               # Python (41 files)
├── core/                      # 基础层
│   ├── constants.py           # 3原则 + 3维审计 + 九大系统
│   └── context_engine.py      # Python 回退引擎 (Rust 优先)
├── memory/soul_memory.py      # RecMem + MemoryWorth + EvolveR
├── loop/soul_loop.py          # pre_task_v2/post_task + CEI
├── principles/soul_principles.py  # 原则激活/继承/扩散/评估
├── reflection/                # SCARF + 本体觉 + 好奇心
├── defense/                   # 三层防线 + 审计
├── growth/                    # 激素 + 分类器 + 技能沉淀
├── mcp/                       # MCP Server + 6 tool files
├── cron/                      # closure_guardian + health_scan + audit_daily
├── embedder.py                # Ollama (mxbai-embed-large 1024d)
├── step_auditor.py            # 每步 4 阶段审计 + 信任分联动
├── adaptive_retrieval.py      # 自适应检索门控
├── noise_filter.py            # 噪声过滤
├── smart_extractor.py         # 6 分类记忆提取
└── reranker.py                # Cross-Encoder 重排序

rust/context-engine-core/      # Rust 引擎 (8 files)
└── src/
    ├── storage/               # StorageBackend(SQLite) + VectorIndex + FtsIndex
    ├── retrieval/             # HybridRetriever + fusion + diversity
    ├── domain/                # Tier + WeibullDecay + WilsonWorth + EvolveR
    ├── context_engine.rs      # supply() 6-phase pipeline
    ├── entity_graph.rs        # 原则图谱注入 + 多跳遍历
    └── lib.rs                 # PyO3 暴露 11 个 Python 类

bridge/                        # N.E.K.O 互操作桥
├── neko_adapter.py            # WebSocket + file 双通道适配器
├── soul_bridge.py             # SoulLoop 对接
├── bus_client.py              # 事件总线客户端
├── http_memory.py             # HTTP 记忆接口
├── event-bus.ts               # TypeScript 事件总线
└── sync-coordinator.ts        # TypeScript 同步协调器
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| 嵌入 | Ollama mxbai-embed-large (1024d, 本地) |
| 引擎 | Rust (PyO3 0.20 + ABI3, rusqlite 0.31, chrono 0.4) |
| 编排 | Python 3.13 |
| 存储 | SQLite (WAL) + 内存向量索引 (LanceDB 待接入) |
| 协议 | MCP (stdio, 25 tools) |
| 桥接 | WebSocket + file 双通道 (N.E.K.O interop) |

---

## 当前状态

| 组件 | 状态 |
|------|------|
| Rust 引擎 (HybridRetriever + Domain + Storage) | ✅ 完成 |
| Python 11 模块核心方法 | ✅ 完成 |
| MCP Server (16/25 handlers) | ✅ 核心可用 |
| 3 原则 + StepAuditor 评分闭环 | ✅ 完成 |
| Bridge 设施 (neko + soul) | ✅ 完成 |
| P1 增强 (adaptive/noise/extract/rerank/cron) | ✅ 完成 |
| Ollama 嵌入 | ✅ 已接入 |
| 冷启动 (3 原则 + 18 种子记忆) | ✅ 完成 |
| LanceDB 实际链接 | 🟡 待 VS BuildTools C++ 工作负载 |
| 辅助方法填充 | 🟡 骨架就绪 |
| 系统调度 (cron 定时) | 🟡 模块就绪 |

---

## License

MIT
