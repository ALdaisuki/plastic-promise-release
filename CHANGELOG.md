# Changelog

All notable changes to Plastic Promise will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-01

### Added

- **40 MCP 工具** 覆盖 10 个域：记忆、原则、域联邦、上下文、审计、反思、系统、经验包、技能追踪、程序化技能
- **双层存储**：SQLite WAL（结构化）+ LanceDB（向量 + BM25 全文检索）
- **上下文供应引擎**：ContextEngine.supply() 双路检索（文本 + 图遍历）→ RRF 融合 → 三层分层
- **Memory Worth 双计数器**：威尔逊下界平滑，ρ ≈ 0.89 相关度
- **EntityGraph 实体关联图谱**：15 节点 + 节点/边 CRUD + 多跳遍历 + 原则注入
- **12 条核心约定** + 原则引擎（激活/继承/扩散/评估）
- **七维审计**：原则联想/记忆供应/约束合规/反馈闭环/信任校准/原则继承/安全追溯
- **三层防线**：L0 硬边界 / L1 信任分驱动约束衰减 / L2 免疫周期扫描
- **SCARF 五维自省**：Status / Certainty / Autonomy / Relatedness / Fairness
- **信任-自由度矩阵**：autonomous → standard → restricted → readonly
- **标签状态机**：task:pending → accepted → active → done → review → reviewed（超时自动恢复）
- **多 Agent 自治流水线**：Pi Builder / Fixer / Reviewer，Claude PM 协调，daemon 零 Token 轮询
- **域联邦系统**：7 域 + 6 行为域 + 1 通用域，merge / unmerge / rename / rebuild
- **经验包系统**：pack_export / pack_import / pack_recall（skip / replace / merge 策略）
- **技能追踪**：skill_session_start / complete / trace / audit / auto_track，父→子链追踪
- **程序化技能（Phase 1）**：session-init / smart-remember / step-closure
- **MCP SSE 多 Agent 共享模式**：/health / /api/stats / /api/issues / /api/trust / /dashboard
- **记忆质量管道**：噪声过滤 → 关键词提取 → 分类 → 嵌入 → 去重 → QualityGate → 衰减 → 双写
- **Fuzzy Buffer 流水线**：raw → tagged → classified → embedded → 主池迁移
- **自演化闭环**：EvolveR + GC + MemoryConsolidator
- **时间衰减引擎**：Weibull 分布，L1/L3 分层衰减，β 参数可配
- **N.E.K.O 桥接适配器**：双向事件总线，跨框架记忆共享
- **Rust 核心引擎**：context-engine-core crate（PyO3 桥接，可选）
- **Trae IDE 接入**：.trae/mcp.json + .trae/rules（10 条核心约定）
- **Web Dashboard**：实时监控面板（记忆池、信任分、身体系统、审计报告）

### Changed

- **从零重建**：服务器崩溃后从对话记录重建全部代码和文档
- 重构记忆系统：从 HashMap 内存方案升级为 SQLite + LanceDB 持久化
- 重构原则系统：从 11 条原则扩展为 12 条，新增「代码即文档」

### Fixed

- MCP Server 导入路径修复（`plastic_promise.constants` → `plastic_promise.core.constants`）
- 重复记忆条目合并（相似度 ≥ 0.85 自动去重）
- LanceDB 删除时同步清理问题（防止孤儿向量条目）

### Infrastructure

- 新增 `pyproject.toml`（项目元数据 + 依赖 + ruff/mypy/pytest 配置）
- 新增 `.editorconfig`（编辑器统一配置）
- 新增 `.pre-commit-config.yaml`（pre-commit hooks）
- 新增 `Makefile`（常用命令快捷入口）
- 新增 `requirements.txt`（pip 依赖清单）
- 新增 `SECURITY.md`（安全策略）
- 新增 `CHANGELOG.md`（本文件）

[0.1.0]: https://github.com/plastic-promise/plastic-promise/releases/tag/v0.1.0