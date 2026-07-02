# Task Quality Hardening — 委托系统信号质量加固

**日期**: 2026-07-03
**状态**: approved
**分支**: feat/task-quality-hardening

## 背景

Hunter Guild 委托系统的首轮安全网扫描产生了 139 条委托，但质量分析发现三个问题：

1. **50% 重复率**（69/139）：同一扫描发现被多次入队
2. **24% 低信号**（34/139）：Shotgun Surgery 检测将系统内部管理标签误报为架构问题
3. 有效委托仅 36 条（26%）

## 根因

| 问题 | 位置 | 原因 |
|------|------|------|
| 重复入队 | `plastic_promise/mcp/tools/task_queue.py:168` | `handle_task_enqueue` 的去重仅覆盖 `research_exemplar` / `verify_exemplar` 两种类型，扫描器产出的 `decouple_domains`、`investigate_coupling` 等全无保护 |
| 低信号误报 | `plastic_promise/cron/scan_architecture.py:121-161` | Shotgun Surgery 检测对所有标签一视同仁，系统管理标签（`task:done`、`branch:main` 等）本就应该跨模块出现 |

## 设计

### A. task_enqueue 去重扩展

**策略**: 时间窗口 (24h) + payload_hash 双重去重

**范围**: 所有 `source_scan` 不为空的入队（即扫描器自动生成的委托）。手工委托（`from_agent` 为 claude/user，无 `source_scan`）不受影响。

**流程**:

```
handle_task_enqueue:
  if source_scan is not None:
    1. 计算 payload_hash
    2. 查询: SELECT id FROM task_queue
       WHERE task_type = ?
         AND status = 'pending'
         AND json_extract(payload, '$.payload_hash') = ?
         AND created_at > datetime('now', '-24 hours')
       LIMIT 1
    3. 命中 → 返回 {status: "duplicate", existing_task_id}
    4. 未命中 → 正常创建
```

#### _compute_payload_hash 通用化

当前实现依赖 `payload.problem` 字段，但扫描器（`scan_architecture`、`scan_coupling` 等）的 payload 结构各异（`type`、`tag`、`tags`、`domains` 等），不包含 `problem` 字段，导致 `_compute_payload_hash` 返回空字符串。

**修改为通用签名哈希**: 对 payload 的所有 key 排序后序列化为 JSON，取 SHA256 前 8 位：

```python
def _compute_payload_hash(payload: dict) -> str:
    """Deterministic hash over sorted payload keys. Works for any payload shape."""
    if not payload:
        return ""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]
```

对所有扫描器类型（`decouple_domains`、`investigate_coupling`、`investigate_memory_influx`、`rebalance_domains` 等）通用适用。

#### 索引策略

```sql
CREATE INDEX IF NOT EXISTS idx_task_dedup
  ON task_queue(task_type, status, created_at);
```

**json_extract 无法被普通 B-tree 索引利用**。SQLite 不支持表达式/函数索引。当前策略是复合索引 `(task_type, status, created_at)` 先缩小候选集（pending 任务通常在 100 条以内），再逐行 `json_extract` 比较。在 246 条记忆、约 100 条 pending 委托的规模下完全够用。

**扩展路径**: 如果任务量增长到 1000+，将 `payload_hash` 提升为 `task_queue` 的独立列 `payload_hash TEXT`，入队时直接写入，查询时走 `= ` 精确匹配 + 普通索引。当前不引入新列是为了避免 schema migration 的复杂度——待数据量真正触及时再重构。

#### 兼容性

手工委托（`from_agent` in (claude, user) 且 `source_scan` IS NULL）跳过去重逻辑，不受影响。现有 `research_exemplar` / `verify_exemplar` 的去重也统一走新路径。

### B. scan_architecture 标签黑名单

**策略**: 内置默认黑名单 + 环境变量可扩展

**内置黑名单**:

```python
DEFAULT_TAG_BLACKLIST = {
    # 系统管理标签 — 跨模块出现是正常行为
    "task:done", "task:pending", "task:active", "task:accepted",
    "task:review", "task:reviewed",
    "branch:main", "status:replaced",
    "llm_pending:true", "llm_classified:true",
    "audit",
    # 元数据分类标签 — 跨域共现是预期行为
    "cat:project", "cat:event", "cat:decision",
    "cat:preference", "cat:fact", "cat:pattern", "cat:entity",
    # 系统来源标签 — 跨域分布是管道设计使然
    "source:file-sync", "source:auto_inject",
}
```

**环境变量扩展**:

```
TAG_BLACKLIST_EXTRA="custom:tag1,another:tag2"
```

**实现位置**: `scan_architecture.py` Shotgun Surgery 循环中，在判断 `len(domains_set) > threshold_spread` 之前先检查 `tag not in blacklist`。

### C. 一次性清理

在实施 A+B 后执行，清理当前 139 条中的 103 条低质量委托：

```sql
-- Step 1: 删除重复（保留每组最早的 1 条）
DELETE FROM task_queue WHERE id NOT IN (
    SELECT min(id) FROM task_queue GROUP BY task_type, title
);

-- Step 2: 删除低信号 Shotgun Surgery
DELETE FROM task_queue
WHERE task_type = 'decouple_domains'
  AND title LIKE '%Shotgun Surgery%'
  AND (
    title LIKE '%task:%'
    OR title LIKE '%branch:%'
    OR title LIKE '%status:%'
    OR title LIKE '%llm_%'
    OR title LIKE '%audit%'
    OR title LIKE '%cat:project%'
    OR title LIKE '%cat:event%'
    OR title LIKE '%cat:decision%'
    OR title LIKE '%cat:preference%'
    OR title LIKE '%cat:fact%'
    OR title LIKE '%cat:pattern%'
    OR title LIKE '%cat:entity%'
    OR title LIKE '%source:file-sync%'
    OR title LIKE '%source:auto_inject%'
  );
```

**预期结果**: 保留 ~36 条有效委托，验证命令：

```sql
SELECT COUNT(*) FROM task_queue WHERE source_scan IS NOT NULL;
```

## 影响范围

| 文件 | 改动 |
|------|------|
| `plastic_promise/mcp/tools/task_queue.py` | 通用化 `_compute_payload_hash`；扩展去重逻辑覆盖所有 source_scan 入队 |
| `plastic_promise/cron/scan_architecture.py` | 新增标签黑名单过滤 |
| `plastic_promise/core/task_queue_schema.py` | 新增 `idx_task_dedup` 索引 DDL |
| `plastic_memory.db` | 运行时: 创建新索引 + 清理低质量数据 |

## 不涉及

- 猎人揭榜/验收/心跳逻辑
- `scan_coupling` / `scan_trust` / `scan_memory_decay` 等其他扫描器
- MCP 工具接口签名变更
- SSE 事件广播逻辑

## 验收标准

1. 同一扫描发现连续两轮安全网周期只产生 1 条委托（不重复）
2. Shotgun Surgery 不再报告 `task:done`、`llm_pending:true`、`cat:preference`、`source:file-sync` 等内部标签
3. 清理后 `task_queue` 中 `source_scan IS NOT NULL` 的委托数 ≈ 36
4. 手工 `task_enqueue`（无 source_scan）行为不变
5. `task_queue` 表新增 `idx_task_dedup` 索引
6. `_compute_payload_hash` 对所有扫描器 payload 结构通用适用
