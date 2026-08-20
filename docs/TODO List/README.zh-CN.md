# TODO List — Plastic Promise 路线图状态

> 当前尚未完成或仅部分完成工作的路线图索引；“目标态”仅表示计划或未验证状态，不表示已交付。
> 基线对照来源：[CortexReach/memory-lancedb-pro](https://github.com/CortexReach/memory-lancedb-pro)，分析日期为 2026-07-03。
> 状态更新：2026-08-14。

> [联合六 PR 合同](../standards/union-six-pr-contract.json) revision `2026-08-18.1`
> 是可组合部署与项目协作联合交付线的范围权威。只有每个 `delivery_scope`、
> `collaboration_scope` 与 `required_evidence` 条目均通过，PR 才算完成；任一单侧完成都
> 不等于 PR 完成。本文只报告工作与证据状态，不能重新定义合同范围，也不能证明
> runtime/production 状态。

只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。

本目录现在将带日期的研究与当前路线图状态分开维护。较早的对照文件可能描述已经实现或部分实现的缺口；应以本 README 作为当前索引。

## 状态说明

| 状态 | 含义 |
|---|---|
| 完成 | 源码证据表明该事项已实现。 |
| 部分完成 | 已有部分实现，但范围尚未完成或仍需验证。 |
| 已规划 | 尚无经过验证的实现；仍在路线图中。 |
| 实验性 | 已存在，但不得视为稳定或公开契约。 |
| 需要验证 | 文档或 worktree 备注声称有进展，但当前源码证据不足。 |

## 路线图状态

| ID | 领域 | 状态 | 源码证据 | 下一步行动 |
|---|---|---|---|---|
| R1 | 查询扩展 | 完成 | `plastic_promise/core/query_expander.py` | 保持测试与文档一致。 |
| R2 | 多提供商重排序器 | 部分完成 | `plastic_promise/core/reranker.py`、`tests/test_vertical_slice_units.py` | 已覆盖提供商顺序、本地默认模型、主机规范化和回退解析；继续为托管提供商完善隐私与文档工作。 |
| D1 | 三端点可组合部署 | 进行中 | `docs/architecture/three-endpoint-deployment/`、`docs/roadmap/composable-deployment.md`、`plastic_promise/deployment/endpoint_contract.py`、`plastic_promise/deployment/container_artifacts.py`、`plastic_promise/deployment/migration_journal.py`、`plastic_promise/local_inference_node/`、`plastic_promise/core/node_governance.py` | PR 1 提供堆叠式路由接缝；PR 2 提供 V2 endpoint contract；PR 3 提供 `ContainerArtifactCompiler` 与受保护的不推送 OCI 构建验证；PR 4 提供只读 Deployment Center 与 source-level working-set/awareness projection。当前 PR 5 源码增加备份门控 migration contract、durable collaboration store、可跨重启的 Hook continuation、服务器拥有的 work issuance、普通 tool reconcile、有界 Stop progress/submitted event、Maintenance composition、shadow/inject awareness、只读 collaboration Dashboard 与原子 pending-only promotion enqueue。PR 6 仍负责目标发行、安装、升级、回滚与最终跨端点证据。live mutable migration adapter、真实 browser/runtime evidence、production activation 与 publication 仍属目标或未验证。整条链不宣称已执行生产迁移、LanceDB promotion、Maintenance transition、MCP restart、registry publication 或稳定发行。SQLite 仍只归 `pp-server-backend`，LanceDB 仍是派生存储，部署交互仍位于 `pp-local-edge`。 |
| D2 | 维护者发行 Builder | 进行中 | `docs/release-builder.md`、`docs/adr/0006-maintainer-request-triggered-release-builder.md` | 实现请求/回执、Windows worker、证据、部署、同步和文档六个切片。`Project Steward / user-project build adapters` 仅保留为未来目标。 |
| C1 | 项目级多 Agent 协作 | 进行中 / 待证据 | `plastic_promise/collaboration/`、`plastic_promise/mcp/server.py`、`plastic_promise/passive_memory/codex_hook.py`、`plastic_promise/mcp/dashboard_v2/`、`daemons/maintenance_daemon.py` | 当前源码包含 PR1–PR4 地基，以及 PR5 durable store、可跨重启的认证 Hook continuation、服务器拥有的有界 work issuance/operation、普通 tool reconcile、有界 Stop progress/submitted event、类型化 stage/result receipt、Maintenance composition、shadow/inject awareness、只读 topology/work/timeline projection，以及 accepted-result 到 pending-only outbox 的原子 promotion enqueue，但不构成整个 PR 完成声明。真实 browser/runtime smoke、production activation、migration 执行与受治理 review/runtime/production receipt 仍待完成。PR6 负责最终跨 Agent E2E 与 shadow-only Workflow Composer 证据。状态始终以联合证据台账为准。 |
| R3 | 衰减感知检索排序 | 部分完成 | `plastic_promise/core/context_engine.py`、`plastic_promise/core/decay_engine.py` | 验证排序同时应用加性新近度和乘性衰减。 |
| R4 | 向量 MMR 多样性 | 部分完成 | `plastic_promise/core/context_engine.py`、`plastic_promise/core/lancedb_store.py` | 验证真实向量查询路径和切片交互。 |
| R5 | 管道追踪 / 分数历史 | 已规划 | 文档检查中没有经过验证的公开 trace 对象。 | 设计由环境变量控制的低开销追踪。 |
| R6 | 实时层级晋升 / 降级 | 部分完成 | 已有上下文/层级逻辑，但完整降级与配置行为仍需验证。 | 确认阈值并补充测试。 |
| R7 | 分类感知合并规则 | 已规划 | 文档检查中没有经过验证的分类规则引擎。 | 为每种记忆类别实现 merge/update/append 规则。 |
| R8 | 长记忆内容切块 | 部分完成 | `plastic_promise/core/embedder.py`、`tests/test_embedder.py` | 已实现 embedding 请求切块；LanceDB 父/子切片 schema 迁移仍在规划中。 |
| R9 | 记忆压缩 | 已规划 | `MemoryGC.merge_similar()` 已存在，但尚未验证渐进式 LLM 压缩、冷却和归档。 | 增加压缩设计与保守的推广门禁。 |
| R10 | 提取节流 | 已规划 | 文档检查中没有经过验证的滑动窗口节流。 | 为 LLM 回退提取增加限流器。 |
| R11 | 会话恢复 | 部分完成 | PR 1 worktree 中的启动过期 claim 恢复已要求显式项目或系统权限；孤儿/缺失向量恢复仍未验证。 | 完成过期 claim 的聚焦迁移/恢复审查，再在后续存储切片加入孤儿向量和缺失向量 reconcile。 |
| R12 | 性能基准测试 | 完成 | `plastic_promise/core/benchmark.py`、`system(action=benchmark)`、`tests/test_performance_benchmark.py` | 视需要将发行版专属基线接入 CI。 |
| R13 | 仅 emoji 噪声检测 | 完成 | `plastic_promise/core/noise_filter.py`、`tests/test_recall_quality_quick_fixes.py`、`tests/test_vertical_slice_units.py` | 已验证仅 emoji、emoji 加空白、reaction wrapper 和包含有效文本的混合内容。 |
| R14 | 双层铁律 | 已规划 | 已有步骤闭环；尚未验证派生原则提取。 | 增加可选的派生原则层。 |
| R15 | Obsidian vault 导出 | 已规划 | `pack_export` 支持 JSON；尚未验证 Markdown vault 导出。 | 设计 Markdown/YAML 导出命令。 |
| R16 | 配置驱动的层级 / 衰减 | 已规划 | 衰减常量似乎写在代码中。 | 增加经 schema 验证的配置与环境变量覆盖。 |
| R17 | 多提供商 embedding 与密钥轮换 | 已规划 | 已有默认本地 Ollama/回退路径；尚未验证提供商/密钥轮换。 | 研究不会破坏向量维度的提供商抽象。 |
| R18 | Rust 原则注入一致性 | 部分完成 | 已由 `cargo test --manifest-path rust/context-engine-core/Cargo.toml` 和 `python -B -m pytest -p no:cacheprovider tests/test_rust_release_import.py::test_release_context_engine_core_import_contract -q` 验证；证据显示 activated principles 非空且 `principle_injection_count` 匹配，并不代表完整原则集合/内容一致。 | 在关闭 R18 前，将 Rust 激活行为与权威 Python 任务类型映射进行比较。 |
| R19 | Rust 图遍历一致性 | 已规划 | 尚未验证 Rust 图加载一致性。 | 序列化/加载图，或从 Rust 查询 SQLite。 |
| R20 | Rust 后端路径处理 | 完成 | 已由 `cargo test --manifest-path rust/context-engine-core/Cargo.toml` 和 `python -B -m pytest -p no:cacheprovider tests/test_rust_integration.py::test_supply_rust_preserves_memory_db_path_for_new_with_backends tests/test_rust_integration.py::test_supply_rust_uses_new_with_backends_and_project_context tests/test_rust_integration.py::test_debug_supply_uses_rust_path_when_rust_is_preferred -q` 验证；证据覆盖 `new_with_backends` 路径处理、`_supply_rust` 保留 `:memory:` 和项目感知 snapshot 上下文。 | 无。 |
| R21 | Rust 持久化 LanceDB 后端 | 已规划 | 路线图中将 Rust LanceDbStore 描述为 HashMap-backed。 | 在依赖约束允许后替换占位实现。 |
| R22 | 因果世界模型基础 | 已规划 | 仅为战略路线图。 | 从内部 PR/CI/task 事件的事件记忆和因果图开始。 |

## 本目录中的文件

| 文件 | 当前作用 |
|---|---|
| [01-comparison-analysis.md](01-comparison-analysis.md) | 针对 CortexReach 的带日期基线对照；不作为完成状态的当前真相。 |
| [02-retrieval-enhancement.md](02-retrieval-enhancement.md) | 检索路线图；多项内容已完成或部分完成，应与本索引一并阅读。 |
| [03-smart-extraction-upgrade.md](03-smart-extraction-upgrade.md) | 当前智能提取和生命周期路线图。 |
| [04-infrastructure-gaps.md](04-infrastructure-gaps.md) | 当前基础设施与完善路线图。 |
| [05-integration-roadmap.md](05-integration-roadmap.md) | 集成地图；实现状态变化时更新。 |
| [06-rust-engine-gaps.md](06-rust-engine-gaps.md) | 当前 Rust 一致性路线图。 |
| [07-causal-world-model-roadmap.md](07-causal-world-model-roadmap.md) | 战略性的因果 / 事件 / 世界模型路线图。 |

## 当前实施顺序

```text
1. 验证已完成的检索声明
   -> 查询扩展、重排序器、衰减排序、向量 MMR

2. 完成记忆生命周期质量
   -> 分类感知合并、切块、压缩、提取节流

3. 增加基础设施安全性
   -> 会话恢复、基准测试、追踪输出、配置驱动衰减

4. 关闭剩余 Rust 一致性缺口
   -> 图遍历、LanceDB 持久化

5. 启动因果基础
   -> 事件记忆、因果图、回放、信任归因
```

## 路线图政策

- 保留带日期的研究，但要明确标记为基线研究。
- 在当前源码验证前，不得将仅存在于 worktree 的声明表述为已完成。
- 使用文本状态标记（`[P0]`、`[P1]`、完成、部分完成），不要使用 emoji。
- 每个关闭事项都应引用源码文件、测试或发行说明。
- 新战略事项必须使用唯一编号，并出现在本 README 索引中。
