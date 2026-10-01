# t2 积压成因与清理规则（task_queue 257 + store_outbox 89）

扫描代理 B · 只读分析 · 数据源 `~/.local/share/plastic-promise/mac-server/data/plastic-promise.sqlite3`（mode=ro）。结论：**建议关闭 251 条 task_queue + 79 条 outbox failed = 330 条；保留 6 条 task + 10 条 outbox pending。**

## 0. 关键事实

- 生产权威 = 发行仓 checkout（daemon PID 71289，2026-08-22 14:14 启动）；MCP runtime checkout 相同 revision `8850113`（2026-08-25 04:25，已含去重）。
- MCP /health：`status=ok, degraded=true, vector_ready=false, lancedb_ready=false, lancedb_required=true, retrieval_status=degraded_text_only`。
- task_queue 唯一去重闸门 = 触发器 `trg_task_project_pending_payload_insert`（`core/task_queue_schema.py:109-139`，2026-08-20 提交）——**该触发器已于 2026-08-23 修复为按 project 作用域**，但 2026-08-22 已入库的重复行不会追溯清理（"Existing duplicates are grandfathered"）。

## 1. task_queue 257 pending 分组

| task_type | n | 去重键实际取值 | 唯一指纹 | 判定 |
|---|---|---|---|---|
| investigate_coupling | 198 | `tags` 对 × `expected`/`ratio` 浮点 | 198 | 扫描器噪声，全部关闭 |
| rebalance_domains | 25 | 同 project+domain 完全相同 payload | 24（1 组重复 3 次） | 关闭 22，保留每 project 最新 3 |
| audit_scheduler | 17 | `title` 含 audit_id 时间戳 | 17 | 关闭 16，保留最新 1 |
| fix_latency | 16 | `payload.audit_id` 每轮不同 | 16 | 关闭 15，保留最新 1 |
| fix_guild | 1 | — | 1 | **保留**（真实待修 bug） |

### 1.1 investigate_coupling（198）——去重失效根因

- 扫描器 `cron/scan_coupling.py:81-142` 每轮重算全表 tag 共现；派发 payload 由任务队列注入 `payload_hash = sha256(canonical_json)`（`mcp/tools/task_queue.py:34-59`）。
- 共现对只有 6 个（`branch:HEAD`/`domain:connecting`/`skill:smart-remember`/`task:active` 两两组合，每个 33 条），但 `expected`/`ratio` 每轮随 `total_memories` 增长而变：
  `[branch:HEAD]+[domain:connecting]` → 06:44 `expected=0.6, ratio=5.0`，07:54 `0.3, 10.0` … 共 33 个不同 hash。
- **结论：指纹包含连续浮点派生量，导致同一异常每轮都是"新任务"。** 扫描器节流 600s（`daemons/maintenance_daemon.py:181`），每次运行 6 findings × 每 10 分钟。
- 次因：2026-08-22 运行时触发器按 `(task_type, payload_hash)` 去重（不含 project_id），同 hash 跨项目被误抑制（现 DB 中 `rebalance_domains` 同 hash 落在 `project:reverse-skill` 与 `project:pentest-lqcx`，触发条件回放 `trigger_would_fire=0`）。2026-08-23 已修为按 project 作用域。
- 标签本身为垃圾域（`branch:HEAD` 是 git 分支头，`task:active` 是状态标签），不应触发"跨模块耦合"调查。

### 1.2 其余三类：时间戳型指纹

- `audit_scheduler`：`title` 内嵌 `audit_YYYYMMDD_HHMMSS`（`scan_scheduler_health.py:37,431`），每轮 1 条，17 条中 16 条被后轮取代。
- `fix_latency`：`payload={finding, audit_id}`（同上 :418），`avg_wait_seconds` 恒为 13469.0（同一陈旧测量），每轮 1 条；`rebalance_domains`（`scan_memory_decay.py:351`）：`project:plastic-promise/uncategorized` 22 条占比从 62%→100% 递增（每次 payload 不同故未被拦截）；另 3 条是 3 个不同 project 各自的 `connecting=100%`，其中 2 条 hash 完全相同被触发器漏掉。
- **三者共因：扫描器把"本轮快照"而非"未解决状态"当任务身份。**

## 2. task_queue 安全批量关闭判定规则（dry-run，不执行）

判定必须同时成立：① `status='pending'`；② `source_scan` 为扫描器来源；③ 该指纹已被**更新的**同源同型 pending 任务取代，或属于已判定噪声类；④ `id` 不在保留集。禁止触碰 `pending_review`/`claimed`/`executing` 与 `project:legacy-quarantine`。

```sql
-- dry-run ①②③ 判定：逐类打印将关闭的 id（只读，无 UPDATE）
/* 保留集：每 project 最新一条 rebalance_domains + 最新 audit_scheduler + 最新 fix_latency */
WITH keep AS (
  SELECT id FROM (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY created_at DESC, id DESC) rn
    FROM task_queue WHERE status='pending' AND task_type='rebalance_domains'
  ) WHERE rn=1
  UNION ALL SELECT id FROM (SELECT id FROM task_queue WHERE status='pending'
      AND task_type='audit_scheduler' ORDER BY created_at DESC LIMIT 1)
  UNION ALL SELECT id FROM (SELECT id FROM task_queue WHERE status='pending'
      AND task_type='fix_latency' ORDER BY created_at DESC LIMIT 1)
)
SELECT task_type, COUNT(*) n_close FROM task_queue
WHERE status='pending'
  AND source_scan IS NOT NULL
  AND task_type IN ('investigate_coupling','rebalance_domains','audit_scheduler','fix_latency')
  AND id NOT IN (SELECT id FROM keep)
GROUP BY task_type;
-- 预期：coupling 198 / rebalance 22 / audit_scheduler 16 / fix_latency 15 = 251
```

关闭动作应走状态机而非 DELETE（保留审计痕）：`status='abandoned'` 是 `task_queue` 中真实存在、且被 `scan_scheduler_health` 各维度 SQL 排除在 `total` 之外的终态（`hunter_failure_log.failure_type='abandoned'` 另记）。写语句（**待人工批准后执行**）：

```sql
-- 待批准：BEGIN IMMEDIATE; 单事务，251 行，附 reason
UPDATE task_queue SET status='abandoned', updated_at=datetime('now'),
       result='superseded_by_scanner_dedup_audit_2026-08'
WHERE status='pending' AND id IN (/* 上面 dry-run 选出的 id 列表 */);
```

## 3. store_outbox 79 failed —— 作业身份与永久失败机理

- 作业 = `memory_index` 检查型派生索引 outbox（`memory-index/v3`，字段 `action/expected_embedding_hash/material_revision/memory_id/memory_version/project_id`，`core/traceability.py:18,559`）；由普通记忆写入/重分类事务同提交产生，消费者是 daemon 的 `memory_index_replay` 阶段（`daemons/maintenance_daemon.py:2018`，顺序 5/7）。
- `attempt_count=8 = _MAX_OUTBOX_ATTEMPTS`（`core/synthesis_maintenance.py:47,1091`）→ 每 5 分钟一个 daemon 周期重试一次，8 次后终态。
- 决定性证据：`call_spans` 中该阶段 68 次 span 全部 `selected=N, claimed=N, succeeded=0, failed=N`；06:19:43 → 06:29:43（下一周期）失败 9→11 条，即"重试即失败、每周期新增几条"。
- 三链根因（按证据强度）：
  1. **提供方不可用**：daemon plist 未传 `PLASTIC_LANCEDB_*`（runtime plist 传了），err.log 反复 `ContextEngine: LanceDB identity is not ready … maintenance_required` + `FallbackEmbedder detected — vector operations disabled`。fallback embedder → `model_name='fallback-zero'` → `self._vectors_disabled=True` → `LanceDBStore.replace_checked` 抛 `RuntimeError("lancedb_vectors_disabled")`（`core/lancedb_store.py:154,816`），`delete_checked` 无该守卫但 `self._table` 为 None 抛 `lancedb_table_unavailable`（:888）。
  2. **投影陷阱（记录缺陷，非重试原因）**：`delete_checked` 抛 `RuntimeError` → `_safe_outbox_error` 走 `RuntimeError` 分支，白名单只含 `_PERMANENT_RUNTIME_REASONS`（:1189-1200）→ 真实原因（`lancedb_vectors_disabled`/`lancedb_table_unavailable`/`memory_index_embedder_unavailable`）**全部被投影成 `operation_failed`**。因此 `error_message` 不能区分根因，只能靠 dag 证据。
  3. **双引擎分裂**：runtime（MCP）有完整 LanceDB 路径且 live 索引存在；但 2026-08-24 的 10 条 `memory_store` 回退说明 runtime 侧派发嵌入也不可用（`node_retrieval_runtime_invalid`）。两侧都不是"数据坏了"，而是向量面整体未就绪。
- **能否安全清理：能，但必须按序号而非无差别清空。** 79 条只描述派生向量索引状态：
  - `done` 78 条说明 08-22 06:15 前写入是可索引的（彼时 daemon 内联写路径仍在）；
  - 按 `memory_id` 分组：49 个记忆 → 79 条作业，其中 **30 条已被同 memory_id 的更新作业取代**（superseded），49 条每个记忆各 1 条最新；最新作业中 33 条 `delete`、16 条 `upsert`；
  - 三种最新终态都收敛到"该向量行不得存在"：`action=delete` 只能删行；`expected_embedding_hash == 当前 memories.embedding_hash`（79/79 全部匹配）但 `memory_version` 80276-94020 ≪ 当前 135968，重放时 `_memory_index_job_matches_current` 返回 False → 静默 `return`（即被标记 done 也不会写向量）；
  - 因此不存在"清早了"的语义：**批处理全部标记 `status='done'` 与逐条重放的真实效果一致（都不写向量），而前者可让 replay 阶段恢复 `selected=0`。**
- 清理不应裸 SQL：存在受支持的算子通路 `scripts/manage_index_outbox.py`（`inspect` / `requeue --apply`，`requeue_index_outbox_jobs` 会把 `failed|blocked` 重置为 pending）与 `reconcile_index_outbox`（会**先校验不可变字段摘要**再把快照内作业置 done，`core/index_outbox_reconciliation.py:531-602`）。dry-run 预览：

```sql
-- dry-run：确认清理范围与"是否有非 memory_index 混入"
SELECT status, tool_name, COUNT(*) n, COUNT(DISTINCT json_extract(payload_json,'$.memory_id')) mems
FROM store_outbox WHERE status='failed' GROUP BY status, tool_name;   -- 期望 79 / memory_index / 49
-- dry-run：被取代作业（建议 close 优先级最高）
SELECT COUNT(*) superseded FROM (
  SELECT ROW_NUMBER() OVER (PARTITION BY json_extract(payload_json,'$.memory_id')
    ORDER BY created_at DESC, rowid DESC) rn FROM store_outbox WHERE status='failed') WHERE rn>1;  -- 30
```

## 4. 10 条 pending（memory_store, RetrievalEmbeddingError）

- 9 条 `project:plastic-promise`（2026-08-24）、1 条 `project:pentest-lqcx`（2026-08-22），均为 `memory_store` **本体失败**后的正文兜底（含 pentest 复盘、子代理断流经验等）。路径：`mcp/tools/memory.py:1928-1969` 捕获 `RetrievalEmbeddingError(node_retrieval_runtime_invalid)` → `record_outbox_event(tool_name='memory_store', metadata={'fallback':'store_outbox'})`，并写 `degradation_events(best_effort)`。
- **回放条件（当前不满足，故不可视为可清理）**：
  1. 代码层：`FROM store_outbox` 全仓消费方只认 `passive_memory_proposal`、`memory_index`、`synthesis_index`（`grep` 全量核对）；**`memory_store` 行没有任何 replay 实现**——这是与 79 条 failed 的本质区别（前者有不可逆的代码缺口）；
  2. 运行层：需运行时用完整 LanceDB 路径（`PLASTIC_LANCEDB_PATH/LIVE_ROOT/GENERATION_ROOT`）启动、`vector_ready=true`、`/health.embedding_probe.status=ok`，且受治理节点路由可用（`node_retrieval_runtime_invalid` 消失）；
  3. 必须按项目作用域回放并保留 `project_id/call_id/visibility/source_class`（正文含项目私有内容），不得由通用脚本跨项目重放。
- 处置建议：**保留**；要么修好向量面后补一个受治理的 `memory_store` outbox 回放器，要么人工将正文改走 `memory_sync_files`/`memory_store` 显式重放（有 `dedupe_key` 保护，重复不会双写）。

## 5. 清理优先级与风险结论

| 优先级 | 对象 | 条数 | 动作 | 风险 |
|---|---|---|---|---|
| P0 | outbox failed `memory_index` | 79 | 标记 done / 或 reconcile 快照；**先 30 条 superseded** | 低：派生索引，且当前 LanceDB 未就绪，向量本不存在；不可变字段摘要可不被破坏（改 `status` 不改 payload） |
| P0 | task_queue `audit_scheduler` 被取代 | 16 | → abandoned | 低：审计报告是快照，最新 1 条保留 |
| P0 | task_queue `fix_latency` 被取代 | 15 | → abandoned | 低：avg_wait_seconds 恒为陈旧值，修复动作需重跑而非读旧卡 |
| P1 | task_queue `rebalance_domains` 被取代 | 22 | → abandoned，保留每 project 最新 3 | 低：同 project+domain 已被全覆盖 |
| P1 | task_queue `investigate_coupling` | 198 | → abandoned（**须同时修扫描器**） | 中：关闭本身无损；不修扫描器则每 10 分钟回填 6 条 |
| P2 | outbox pending `memory_store` | 10 | **保留**，修向量面后回放 | 高：正文仅存此处，误删即永久丢失 |
| P2 | task_queue `fix_guild` | 1 | **保留** | — 真实未修 bug（`task_verify` 对 pending 验收子委托报 `task_state_conflict`） |

**根因（须与清理同时修，否则积压复现）**：① 扫描器任务指纹含每轮变化的派生量/时间戳，应改为"稳定域指纹 + 版本化轮次"，并在派发前比对上轮未解决集合；② `_safe_outbox_error` 的 `operation_failed` 投影抹掉了真实失败原因，应把 `lancedb_*` 与 `memory_index_embedder_unavailable` 加入安全原因白名单（否则运维永远只能看到"操作失败"）；③ daemon 与 runtime 的 LanceDB 环境变量不一致，需统一或让 daemon 显式报 `generation_live_index_unavailable`（现该分支因未设 `PLASTIC_LANCEDB_GENERATION_ROOT` 被跳过，导致误用 fallback embedder）；④ `memory_store` outbox 缺回放器，是设计缺口而非运维问题。

*未执行任何写操作；所有 SQL 均为只读预览。*
