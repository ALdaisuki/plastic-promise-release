<p align="center">
  <h1 align="center">Plastic Promise</h1>
  <p align="center">
    <em>记忆是可塑的，灵魂因记忆存在、因约定成长。</em>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT">
    <img src="https://img.shields.io/badge/mcp-40_tools-orange.svg" alt="MCP: 40 tools">
    <img src="https://img.shields.io/badge/status-alpha-red.svg" alt="Status: Alpha">
  </p>
</p>

---

**Plastic Promise** 是以「约定工程」(Commitment Engineering) 替代「约束工程」的 AI 行为治理系统。40 个 MCP 工具覆盖记忆、原则、域联邦、上下文、审计、反思、系统、经验包、技能追踪、程序化技能十大域。内置多 Agent 自治流水线——Claude PM 管理 Pi Builder/Fixer/Reviewer 团队，标签状态机驱动全自动任务流转。

> 完整架构、路线图和当前状态见 **[GOAL.md](docs/GOAL.md)**。

---

## 目录

- [架构](#架构)
- [快速开始](#快速开始)
- [核心特性](#核心特性)
- [MCP 工具一览](#mcp-工具一览)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [开发指南](#开发指南)
- [License](#license)

---

## 架构

```
┌──────────────────────────────────────────────────┐
│              Plastic Promise MCP Server           │
│  ┌──────────┬──────────┬──────────┬──────────┐  │
│  │ 记忆 (10)│ 原则 (4) │ 上下文(5)│ 审计 (3) │  │
│  ├──────────┼──────────┼──────────┼──────────┤  │
│  │ 自省 (2) │ 管理 (4) │ 经验包(3)│ 联邦 (1) │  │
│  ├──────────┴──────────┼──────────┴──────────┤  │
│  │   技能追踪 (5)       │  程序化技能 (3)     │  │
│  └─────────────────────┴─────────────────────┘  │
│              ↓ 共享 ContextEngine                │
│  ┌──────────────────────────────────────────┐   │
│  │  EntityGraph │ 混合检索 (ANN+BM25+RRF)    │   │
│  │  MemoryWorth │ 符号规则 │ 衰减引擎       │   │
│  └──────────────────────────────────────────┘   │
└──────────────────────┬───────────────────────────┘
                       │
         ┌─────────────┴─────────────┐
         │     SQLite WAL             │
         │     + LanceDB (向量/FTS)   │
         └───────────────────────────┘

┌──────────────────────────────────────────────────┐
│              Multi-Agent Team                     │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐    │
│  │  Claude  │──▶│Pi-Builder│──▶│Pi-Reviewer│    │
│  │   (PM)   │   │(building)│   │(reflecting)│    │
│  └──────────┘   └──────────┘   └─────┬────┘    │
│        ▲                              │          │
│        └────────── Pi-Fixer ◀─────────┘          │
│                  (fixing)                         │
│                                                    │
│  Autonomous Pipeline:                              │
│  daemons/pi_daemon.py (零Token轮询) + daemons/audit_daemon.py │
│  daemons/pi_worker.ps1 (四模式) + /notify → SSE /events         │
└──────────────────────────────────────────────────┘
```

---

## 快速开始

### 安装

```bash
# 核心依赖
pip install -r requirements.txt

# 或完整安装（含开发工具）
pip install -e ".[dev]"

# 可选：构建 Rust 核心引擎
cd rust/context-engine-core && pip install maturin && maturin develop
```

### 启动

```bash
# 1. 启动共享记忆服务器 (SSE 多 Agent 模式)
python -m plastic_promise --sse 9020

# 2. 启动自治流水线
python daemons/pi_daemon.py

# 3. 发任务 (标签驱动, 零 Token)
# 通过 MCP 工具调用:
memory_store(tags=["task:pending", "assignee:pi_builder", "domain:building"])
```

### 接入 Claude Code / Trae

将 `mcp.json` 合并到你的 MCP 配置：

```json
{
  "mcpServers": {
    "plastic-promise": {
      "command": "python",
      "args": ["-m", "plastic_promise.mcp.server"],
      "cwd": "/path/to/Memory system",
      "env": {
        "PP_EMBEDDING_DIM": "384",
        "PP_LANCEDB_PATH": ".data/lancedb",
        "PP_SQLITE_PATH": ".data/memories.db"
      }
    }
  }
}
```

---

## 核心特性

### 约定工程

| 理念 | 说明 |
|------|------|
| **约定优于约束** | Agent 遵守规则不是因为「被禁止」，而是因为「不想让在乎的人失望」 |
| **信任换自主** | 信任分驱动动态约束：高分放宽，低分收紧 |
| **原则自然浮现** | 原则不是靠防火墙强制执行，而是在检索历史决策时自然涌现 |
| **上下文主动供应** | 记忆不是「被查询的档案库」，而是「主动供应上下文的引擎」 |

### 多 Agent 自治流水线

```
task:pending → Daemon 零Token检测 → spawn Pi → task:active
             → Builder 完成 → 自动唤醒 Reviewer → task:review
             → Claude 验收 → task:reviewed / task:rejected → Fixer 自动修复
```

### 标签状态机

```
task:pending → task:accepted → task:active → task:done → task:review → task:reviewed
                   ↑ 超时 5min 恢复              ↑ 超时 10min 恢复
```

### 信任-自由度矩阵

| 信任分 | 等级 | 权限 |
|--------|------|------|
| 0.80+ | `autonomous` | 全权限, 可分配任务 |
| 0.60+ | `standard` | 读写文件, 创建 Issue |
| 0.30+ | `restricted` | 需审批 |
| 0.00+ | `readonly` | 只读 |

### 数字身体系统

| 系统 | 成熟度 | 核心模块 |
|------|--------|----------|
| 记忆系统 | 90% | soul_memory (双层三域 + L1/L3) |
| 反射弧 | 80% | soul_enforcer (三层防线) |
| 运动系统 | 75% | exec/write/edit + ACP |
| 感官系统 | 70% | memory_recall + code_search |
| 免疫系统 | 70% | soul_audit (七维度 + cron) |
| 内分泌系统 | 65% | soul_hormone (评价引擎 + 信任分) |
| 遗传系统 | 60% | soul_principles (单向扩散 + 同步衰减) |
| 自主神经 | 60% | scan_and_fix + HEARTBEAT |
| 认知系统 | 55% | soul_scarf + soul_curiosity |

### 韧性

- **灾难恢复**: `domain(action="rebuild")` 从 tags 重建域图谱
- **跨版本兼容**: `schema_version` 迁移链 + pack 逃生舱
- **静默失效防护**: `_dm_ok` 降级开关 + 标签系统独立于 Issue 表
- **11 维审计**: 每小时自动运行，Tier1 问题自动修复

---

## MCP 工具一览

| 域 | 数量 | 工具 |
|----|------|------|
| 记忆 | 10 | `memory_recall` `memory_store` `memory_update` `memory_forget` `memory_stats` `memory_list` `memory_gc` `memory_correct` `memory_reclassify` `memory_sync_files` |
| 原则 | 4 | `principle_activate` `principle_inherit` `principle_diffuse` `principle_evaluate` |
| 上下文 | 5 | `context_supply` `context_inject` `context_graph` `context_ready` `auto_context_inject` |
| 审计防线 | 3 | `audit_run` `audit_pre_check` `defense` |
| 自省演化 | 2 | `scarf_reflect` `feedback_apply` |
| 系统管理 | 4 | `system` `issue_create` `issue_transition` `issue_list` |
| 经验包 | 3 | `pack_export` `pack_import` `pack_recall` |
| 域联邦 | 1 | `domain` |
| 技能追踪 | 5 | `skill_session_start` `skill_session_complete` `skill_session_trace` `skill_session_audit` `skill_auto_track` |
| 程序化技能 | 3 | `session-init` `smart-remember` `step-closure` |

> 另有 3 个 Prompt 模板和 5 个 Resource 端点。

---

## 技术栈

| 层 | 技术 |
|----|------|
| 语言 | Python 3.10+ / Rust (性能核心) |
| 嵌入 | sentence-transformers (all-MiniLM-L6-v2, 384d) 或 OpenAI |
| 向量存储 | LanceDB (ANN + FTS + RRF 混合融合) |
| 关系存储 | SQLite WAL + schema_version 迁移链 |
| 协议 | MCP (stdio + SSE) |
| 并发 | asyncio + threading.RLock |
| LLM | Pi Agent (deepseek-v4-pro) / Claude |

---

## 项目结构

```
plastic_promise/              # Python 核心包
├── core/                     # 核心引擎
│   ├── constants.py          # 常量、阈值、12 条核心原则
│   ├── context_engine.py     # 上下文供应引擎 (Python 回退)
│   ├── embedder.py           # 嵌入器 (sentence-transformers / OpenAI)
│   ├── decay_engine.py       # 时间衰减引擎 (Weibull)
│   ├── domain_manager.py     # 域联邦管理器
│   ├── noise_filter.py       # 噪声过滤器
│   ├── quality_gate.py       # 质量门控
│   ├── reranker.py           # Cross-encoder 重排序
│   ├── lancedb_store.py      # LanceDB 向量存储
│   └── pack_index.py         # 经验包索引
├── mcp/                      # MCP Server
│   ├── server.py             # 主入口 (38+ 工具路由)
│   ├── tools/                # 11 个工具处理器模块
│   │   ├── memory.py         # 记忆域 (10 工具)
│   │   ├── principles.py     # 原则域 (4 工具)
│   │   ├── context.py        # 上下文域 (5 工具)
│   │   ├── audit_defense.py  # 审计防线域 (3 工具)
│   │   ├── reflection.py     # 自省演化域 (2 工具)
│   │   ├── management.py     # 系统管理域 (7 工具)
│   │   ├── domain.py         # 域联邦 (1 工具)
│   │   ├── skill_tracking.py # 技能追踪 (5 工具)
│   │   ├── reclassify.py     # 记忆重分类
│   │   └── sync.py           # 文件同步
│   ├── resources.py          # 5 个 MCP Resource
│   └── prompts.py            # 3 个 MCP Prompt
├── memory/                   # 记忆系统
│   └── soul_memory.py        # RecMem + EvolveR + MemoryGC
├── loop/                     # 主控编排
│   └── soul_loop.py          # pre_task_v2 + post_task + step-closure
├── principles/               # 原则系统
│   └── soul_principles.py    # 激活/继承/扩散/评估
├── reflection/               # 自省系统
│   ├── soul_scarf.py         # SCARF 五维自省
│   └── soul_proprioception.py # 本体觉 + 惯性抑制
├── growth/                   # 成长系统
│   ├── soul_hormone.py       # 实时反馈激素
│   ├── soul_classifier.py    # 任务分类
│   └── skill_extractor.py    # 技能沉淀
├── defense/                  # 防御系统
│   ├── soul_enforcer.py      # 三层防线
│   └── soul_audit.py         # 七维审计
└── skills/                   # 程序化技能 (Phase 1)

rust/context-engine-core/     # Rust 核心引擎 (PyO3)
├── src/
│   ├── entity_graph.rs       # 实体关联图谱
│   ├── rank_fuser.rs         # RRF 融合 + 符号规则
│   ├── source_tracker.rs     # 来源追溯
│   ├── association_feedback.rs # 自演化反馈
│   ├── memory_worth.rs       # 双计数器
│   ├── context_engine.rs     # 主编排器
│   └── principles.rs         # 原则实体
└── Cargo.toml

daemons/                      # 守护进程 & Worker
├── pi_daemon.py              # 多 Agent 自治流水线 (零 Token 轮询)
├── audit_daemon.py           # 每小时审计 + 记忆清理
├── pi_worker.ps1             # Worker 启动器 (Windows)
├── pi_worker.sh              # Worker 启动器 (Linux/macOS)
├── pi_listener.ps1           # SSE 事件监听器
└── watchdog.ps1              # 进程守护 (自动重启)

tests/                        # 测试
docs/                         # 设计文档
├── GOAL.md                   # 架构总览 & 路线图
├── BUILD_PLAN.md             # 重建计划 (已完成, 历史参考)
└── superpowers/              # 设计文档 (80+ 文件)
scripts/                      # 辅助脚本
├── start-all.bat             # 一键启动 (Windows)
├── start-all.sh              # 一键启动 (Linux/macOS)
└── eco.py                    # 碳足迹计算器
utils/                        # 工具函数
bridge/                       # N.E.K.O 桥接
.data/                        # 运行时数据 (SQLite + LanceDB)
experience_packs/             # 经验包导出
```

---

## 开发指南

```bash
# 安装开发依赖
make dev-install

# 代码检查
make lint

# 代码格式化
make format

# 运行测试
make test

# 完整检查链路
make check

# 安装 pre-commit hooks
make pre-commit-install
```

### 约定

本项目遵循 [Plastic Promise 10 条核心约定](.trae/rules)。所有贡献者应在提交前：

1. 调用 `memory_recall` + `context_supply` 获取上下文
2. 每次实质性产出有 git commit
3. 完成后执行 `step-closure` 闭环

---

## License

[MIT](LICENSE)