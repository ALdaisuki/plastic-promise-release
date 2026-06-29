# Plastic Promise

> 记忆是可塑的，灵魂因记忆存在、因约定成长。

**Plastic Promise** 是以「约定工程」替代「约束工程」的 AI 行为治理系统。29 个 MCP 工具覆盖记忆、原则、域联邦、上下文、审计、反思、系统、经验包八大域。内置完整多 Agent 开发组——Claude PM 管理 Pi Builder/Fixer/Reviewer 团队，标签状态机驱动自治流水线。

> 📋 完整架构、路线图和当前状态见 **[GOAL.md](GOAL.md)**。

---

## 架构

```
MCP Server (29 tools, 8 domains)          Multi-Agent Team
├── 记忆域 (10)                             ├── Claude (PM, governing+designing)
├── 域联邦 (1)                              ├── Pi-Builder (building)
├── 原则域 (4)                              ├── Pi-Fixer (fixing)
├── 上下文 (4)                              └── Pi-Reviewer (reflecting)
├── 审计域 (4)                                    │
├── 反思域 (2)                              Autonomous Pipeline
├── 系统域 (4)                              ├── pi_daemon.py (零Token轮询)
└── 经验包 (3)                              ├── audit_daemon.py (11维审计)
                                            ├── pi_worker.ps1 (四模式)
                                            └── /notify → SSE /events (双向桥)
共享记忆池 (SQLite WAL)
├── 标签状态机 (task:pending→active→done→reviewed)
├── 信任-自由度矩阵 (autonomous/standard/restricted/readonly)
├── 域联邦 (7域, 6行为+1通用)
└── 12条核心约定 (原则引擎 + PrincipleTracker)
```

---

## 快速开始

```bash
pip install mcp uvicorn starlette requests httpx

# 1. 启动共享记忆服务器
python -m plastic_promise.mcp.server --sse 9020

# 2. 启动自治流水线 (单进程管理 Builder+Fixer+Reviewer)
python pi_daemon.py

# 3. 发任务 (标签驱动, 零Token)
memory_store(tags=["task:pending","assignee:pi_builder","domain:building"])
```

---

## 核心特性

### 多 Agent 自治流水线
```
task:pending → Daemon 零Token检测 → spawn Pi → task:active
             → Builder完成 → 自动唤醒 Reviewer → task:review
             → Claude验收 → task:reviewed / task:rejected → Fixer自动修复
```

### 标签状态机
```
task:pending → task:accepted → task:active → task:done → task:review → task:reviewed
                                  ↑ 超时5min恢复            ↑ 超时10min恢复
```

### 信任-自由度矩阵
| 信任分 | 等级 | 权限 |
|--------|------|------|
| 0.80+ | Autonomous | 全权限, 可分配任务 |
| 0.60+ | Standard | 读写文件, 创建Issue |
| 0.30+ | Restricted | 需审批 |
| 0.00+ | ReadOnly | 只读 |

### 11 维审计
每小时自动运行——trust/pipeline/domain/bridge 四维多Agent维度 + 现有七维。Tier1问题自动修复。

### 韧性
- 灾难恢复: `domain(action="rebuild")` 从 tags 重建域图谱
- 跨版本: schema_version 迁移链 + pack 逃生舱
- 静默失效: `_dm_ok` 降级开关 + 标签系统独立于 Issue 表

---

## 技术栈

| 层 | 技术 |
|----|------|
| LLM | Pi Agent (deepseek-v4-pro) |
| 嵌入 | Ollama mxbai-embed-large |
| 引擎 | Python 3.10+ |
| 存储 | SQLite WAL + schema_version |
| 协议 | MCP (SSE + stdio) |
| 并发 | asyncio + threading.RLock |

---

## License

MIT
