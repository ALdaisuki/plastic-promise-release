# 数据存储层清单（02）

> 勘察对象：生产主库 `~/.local/share/plastic-promise/mac-server/data/plastic-promise.sqlite3`（27MB，WAL 模式只读勘察，行数为 2025-08-25 快照 COUNT(*)）；仓库：plastic-promise-release。

## 一、SQLite 生产主库表清单（84 张，按域分组）

| 表 | 行数 | 写入方 | 读取方 |
|---|---|---|---|
| memories / projects / memory_version | 84 / 0 / 1 | core/context_engine.py（记忆管线 upsert）、memory/pipeline.py | recall/context_supply、mcp/tools/memory.py |
| domains / audit_log / schema_version | 18 / 2 / 3 | core/domain_manager.py | 域联邦 domain 工具、dashboard |
| behavior_graph_nodes / behavior_graph_edges | **9084** / **34275** | context_engine（context_inject 实体注册） | context_graph 多跳遍历、context_supply 关联层 |
| call_spans / degradation_events / runtime_events / memory_lineage / store_outbox | 1783 / 79 / 1421 / 78 / 158 | core/traceability.py（调用埋点） | commercial_audit_export、index_outbox_reconciliation |
| task_queue / task_subscriptions / hunter_failure_log / metric_history | 259 / 9 / 1 / 0 | core/task_queue_schema.py、mcp/tools/task_queue.py | Hunter Guild task_inbox/claim/verify |
| trust_scores / trust_history | 4 / 44 | defense/trust_store.py（review_run 联动） | defense get/history、task 等级校验 |
| collaboration_*（37 张：events/agents/work_items/leases/receipts/role_*/coordination_*/coordinator_*/outbox/cursors…） | 除 events=4、agents=1、agent_sessions=2、work_items=1 外**全为 0**（5 张 *_schema 各 1 行） | collaboration/durable_runtime.py、durable_role_store.py、durable_coordination_plan_store.py、event_log.py、deployment/collaboration_schema_migration.py | collaboration_work_* MCP 工具、step_closure |
| official_workflow_state / instances / receipts | 8 / 1 / 9 | core/workflow_state.py | sp-stage 路由校验 |
| derived_work_jobs / attempts / daily_admissions / accelerator_audit_events | 640 / 691 / 0 / 0 | core/derived_work.py、deployment/sqlite_migrations.py | 维护 daemon 派生工作调度 |
| inference_nodes / latency_samples / reservations / audit_events / identity_receipts | 1 / 449 / 0 / 4 / 6 | deployment/sqlite_migrations.py（控制面心跳） | server_status 健康、deployment_center |
| memory_proposals / signals / scores / promotion_tasks | 全 0 | core/proposal_promotion*.py（受控晋升提案 outbox） | proposal_promotion 决策 |
| pp_migration_*（journal/grants/operations/leases/phases） | 仅 journal_schema=1 | deployment/migration_journal.py | 发行管线迁移校验 |
| deployment_migration_journal / index_generation_reconciliation | 4 / 5 | deployment/sqlite_migrations.py、core/index_outbox_reconciliation.py | 发布对账 |
| synthesis_artifacts / project_principle_overlays / production_evidence_attestations | 0 / 0 / 0 | core/synthesis.py、principle_overlays.py、sqlite_migrations.py | 对应 MCP 工具 |

注：仓库内还有独立于主库的 schema——knowledge/*（repository.py 13 张 knowledge_* 表）、core/knowledge_base.py、benchmark.py、shield_scan_store.py、evolution_*、control_plane/store.py（control_state/revisions/activations）均未落在生产主库（独立 DB 或未启用）；`data/semantic_chunk_enrichment.db` 为语义富化缓存独立 SQLite。

## 二、LanceDB 集合

唯一集合 **memory_vectors.lance**（向量+FTS 复合索引），三套根目录共 <320KB：

- `mac-server/lancedb/` — 初始根（24K，仅元数据/manifest，无数据 fragment）
- `mac-server/lancedb-live/` — 在线索引根：manifest.json + index/memory_vectors.lance（28K）
- `mac-server/lancedb-generations/` — 世代归档（260K）：generations/gen-20260818-compact-v2-2560{,-r2,-r3,-r4,-final} 共 5 代，`current -> selections/<hash>` 符号链接切换

代码调用点：core/lancedb_store.py（LanceDBStore：connect/upsert/search_similar/FTS/compaction/read_only 门控）；core/lancedb_generation.py（GenerationManager，manifest v2 + 质量门禁 promotion-gate）；core/lancedb_artifact.py（verify_lancedb_artifact 校验）；knowledge/lancedb_shadow.py（影子重建）；接线在 context_engine.py（_ldb/_resolve_generation_lancedb_path/_lancedb_sync_status）；消费方：memory/pipeline.py（写入去重 check_duplicate + upsert）、proposal_promotion.py（search_similar k=12）、synthesis_maintenance.py（replace/delete_checked）、mcp/server.py 与 skills/session_lifecycle.py（健康探针）。

## 三、双写一致性说明

SQLite 为 canonical 权威，LanceDB 是纯派生索引（可全量重建）。写入路径：pipeline 先落 SQLite，再经 LanceDBStore upsert 向量行；失败不回滚 SQLite，而是记入 `store_outbox`（158 行积压待补偿），由 index_outbox_reconciliation 定期对账 `index_generation_reconciliation`。在线态通过 `_lancedb_sync_status.live_lag` 暴露给 /health（vector_ready 门控：lag 异常即降级 retrieval）。索引换代走离线影子重建 → 质量门禁 → GenerationManager promote → current 符号链接原子切换 → 旧代留存于 lancedb-generations 支持回滚；server 进程只读权威，重建由维护 daemon 承担。

## 四、深度优化候选

1. **WAL 常驻增长**：-wal 固定 ~4.0MB（4,169,472B）且 -shm 活跃，长期不收缩；建议核查 wal_autocheckpoint 阈值与长读事务，必要时维护窗内 PASSIVE/TRUNCATE checkpoint。
2. **图谱-记忆倒挂**：9084 节点 / 34275 边 vs 84 条记忆（~100:1 膨胀），实体注册无收敛；建议按 worth/访问频度做节点修剪、别名合并与 TTL 回收。
3. **冷热分层缺失**：call_spans(1783)/runtime_events(1421)/derived_work_attempts(691) 只增无保留策略；建议按时间分区 + 归档表（如 90 天外迁 JSONL/压缩库）。
4. **协作面冷表**：37 张 collaboration_* 近乎空表却常驻主库 schema；建议延迟建表或拆分至可选模块库，减小主库迁移面积。
5. **LanceDB 世代冗余**：5 个 generation 目录仅 1 个被 current 引用；建议 promotion 成功后自动 GC 旧代（保留 N=2），并明确 lancedb/ 与 lancedb-live/ 双根的弃用路径。
