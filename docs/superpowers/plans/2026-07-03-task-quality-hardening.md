# Task Quality Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除委托系统 50% 重复率和 24% 低信号误报，将 139 条委托清理至 ~36 条有效委托。

**Architecture:** 三个独立改动按 spec 顺序执行 — 先一次性清理现有脏数据 (C)，再修复 scan_architecture 的标签过滤 (B)，最后修复 task_enqueue 的去重逻辑防止未来重复 (A)。A 包含 `_compute_payload_hash` 的通用化重构，使其对所有扫描器 payload 结构适用。

**Tech Stack:** Python 3.13, SQLite3 (plastic_memory.db), asyncio

## Global Constraints

- 手工委托（`from_agent` in (claude, user) 且 `source_scan` IS NULL）不受去重和过滤影响
- 去重窗口：24 小时
- 黑名单可通过环境变量 `TAG_BLACKLIST_EXTRA` 扩展
- 不引入新的数据库列（`payload_hash` 保留在 JSON payload 内）
- 不改动 MCP 工具接口签名

---

### Task 1: 通用化 `_compute_payload_hash`（A 的前置）

**Files:**
- Modify: `plastic_promise/mcp/tools/task_queue.py:30-43`

**Interfaces:**
- Consumes: `hashlib`, `json` (already imported)
- Produces: `_compute_payload_hash(payload: dict) -> str` — 对任意 payload 结构返回确定性 hash

- [ ] **Step 1: 替换 `_compute_payload_hash` 实现**

当前实现（仅支持 `payload.problem` 字段）：
```python
def _compute_payload_hash(payload: dict) -> str:
    """Compute a deterministic hash for dedup based on payload content.

    Uses SHA256 first 8 hex chars of problem + sorted search_hints.
    Returns empty string if payload is None or missing required fields.
    """
    if not payload:
        return ""
    problem = payload.get("problem", "") or payload.get("gap_signal", {}).get("problem", "")
    search_hint = payload.get("search_hint", [])
    if not problem:
        return ""
    seed = f"{problem}|{'|'.join(sorted(search_hint))}"
    return hashlib.sha256(seed.encode()).hexdigest()[:8]
```

替换为通用实现（关键：排除 `payload_hash` 自身，否则入库后注入的 `payload_hash` key 会使原始 payload 和存储 payload 产生不同 hash）：

```python
def _compute_payload_hash(payload: dict) -> str:
    """Compute a deterministic hash for dedup based on payload content.

    Serializes payload with sorted keys to produce a canonical form,
    then returns SHA256 first 8 hex chars. Excludes 'payload_hash' from
    the computation so that injecting the hash into the stored payload
    does not change the hash value.

    Works for any payload shape. Returns empty string if payload is
    None, empty, or contains only 'payload_hash'.
    """
    if not payload:
        return ""
    clean = {k: v for k, v in payload.items() if k != "payload_hash"}
    if not clean:
        return ""
    canonical = json.dumps(clean, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]
```

- [ ] **Step 2: 验证对所有扫描器 payload 通用**

```bash
python -c "
import sys, json, hashlib
sys.path.insert(0, '.')

# Simulate scan_architecture payload
arch_payload = {'type': 'shotgun_surgery', 'tag': 'cat:preference', 'domain_count': 3, 'domains': ['a','b','c'], 'threshold': 2.5}
# Simulate scan_coupling payload
coupling_payload = {'type': 'tag_cooccurrence_anomaly', 'tags': ['t1','t2'], 'actual': 5, 'expected': 0.5, 'ratio': 10.0}
# Simulate scan_memory_decay payload
decay_payload = {'type': 'memory_influx', 'domain': 'uncategorized', 'count': 50, 'rate': 2.3}

from plastic_promise.mcp.tools.task_queue import _compute_payload_hash

h1 = _compute_payload_hash(arch_payload)
h2 = _compute_payload_hash(coupling_payload)
h3 = _compute_payload_hash(decay_payload)
print(f'arch:      {h1} (len={len(h1)})')
print(f'coupling:  {h2} (len={len(h2)})')
print(f'decay:     {h3} (len={len(h3)})')
assert len(h1) == 8 and len(h2) == 8 and len(h3) == 8, 'Hash length != 8'
assert h1 != h2 != h3, 'Different payloads produced same hash'
assert _compute_payload_hash(None) == '', 'None should return empty'
assert _compute_payload_hash({}) == '', 'Empty dict should return empty'
assert _compute_payload_hash({"payload_hash": "abc"}) == '', 'Only payload_hash should return empty'

# Determinism check
assert _compute_payload_hash(arch_payload) == h1, 'Hash not deterministic'

# KEY: payload_hash key must NOT affect the result
# (stored payload has it injected, raw payload does not)
with_hash = {**arch_payload, "payload_hash": "abc12345"}
assert _compute_payload_hash(with_hash) == h1, 'payload_hash key must not affect hash'
print('All assertions passed')
"
```

Expected: `All assertions passed`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/mcp/tools/task_queue.py
git commit -m "refactor(task_queue): universal _compute_payload_hash for all scanner payloads"
```

---

### Task 2: 扩展 task_enqueue 去重覆盖（A 主体）

**Files:**
- Modify: `plastic_promise/mcp/tools/task_queue.py:165-195`

**Interfaces:**
- Consumes: `_compute_payload_hash` (from Task 1), `_get_conn` (existing)
- Produces: `handle_task_enqueue` 对所有 `source_scan` 入队执行 24h payload_hash 去重

- [ ] **Step 1: 替换去重逻辑**

当前逻辑（仅覆盖 `research_exemplar` / `verify_exemplar`）位于 `handle_task_enqueue` 约第 165-195 行。将现有的类型限定去重块替换为通用的 `source_scan` 去重：

删除：
```python
    # ── Dedup check (research_exemplar / verify_exemplar) ───
    # For research-oriented task types, check if a pending task
    # with the same payload_hash already exists.
    if args["task_type"] in ("research_exemplar", "verify_exemplar"):
        payload = args.get("payload")
        if payload:
            phash = _compute_payload_hash(payload)
            if phash:
                dedup_conn = _get_conn()
                existing = dedup_conn.execute(
                    "SELECT id FROM task_queue "
                    "WHERE task_type = ? AND status = 'pending' "
                    "AND json_extract(payload, '$.payload_hash') = ? "
                    "LIMIT 1",
                    (args["task_type"], phash),
                ).fetchone()
                dedup_conn.close()
                if existing:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "duplicate",
                                    "existing_task_id": existing["id"],
                                    "reason": f"Pending {args['task_type']} for this problem already exists",
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
```

替换为：
```python
    # ── Dedup check (all scanner-generated tasks) ─────────
    # For any task with a source_scan (auto-generated by scanners),
    # check if a pending task with the same payload_hash already
    # exists within the 24-hour dedup window.
    source_scan = args.get("source_scan")
    if source_scan is not None:
        payload = args.get("payload")
        if payload:
            phash = _compute_payload_hash(payload)
            if phash:
                dedup_conn = _get_conn()
                existing = dedup_conn.execute(
                    "SELECT id FROM task_queue "
                    "WHERE task_type = ? AND status = 'pending' "
                    "AND json_extract(payload, '$.payload_hash') = ? "
                    "AND created_at > datetime('now', '-24 hours') "
                    "LIMIT 1",
                    (args["task_type"], phash),
                ).fetchone()
                dedup_conn.close()
                if existing:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "duplicate",
                                    "existing_task_id": existing["id"],
                                    "reason": f"Pending {args['task_type']} from {source_scan} already exists (24h window)",
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
```

- [ ] **Step 2: 验证手工委托不受影响**

```bash
python -c "
import json
# Read the modified file and confirm:
# 1. The dedup block is gated on 'source_scan is not None'
# 2. Manual enqueue (no source_scan) bypasses the check entirely
with open('plastic_promise/mcp/tools/task_queue.py') as f:
    content = f.read()
assert 'source_scan is not None' in content, 'Gate missing'
assert 'research_exemplar' not in content or \"args['task_type'] in\" not in content, 'Old type-gate not removed'
print('Gate check passed')
"
```

Expected: `Gate check passed`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/mcp/tools/task_queue.py
git commit -m "feat(task_queue): extend dedup to all scanner-generated tasks (24h + payload_hash)"
```

---

### Task 3: 新增去重索引（A 收尾）

**Files:**
- Modify: `plastic_promise/core/task_queue_schema.py` — TASK_QUEUE_DDL
- Modify: `plastic_memory.db` — 运行时创建索引

**Interfaces:**
- Consumes: `ensure_task_tables` (existing)
- Produces: `idx_task_dedup` 索引 — `(task_type, status, created_at)`

- [ ] **Step 1: 在 TASK_QUEUE_DDL 中添加索引**

在 `plastic_promise/core/task_queue_schema.py` 的 `TASK_QUEUE_DDL` 末尾（`idx_task_parent` 之后）添加：

```python
CREATE INDEX IF NOT EXISTS idx_task_dedup ON task_queue(task_type, status, created_at);
```

插入位置：`TASK_QUEUE_DDL` 字符串中，`CREATE INDEX IF NOT EXISTS idx_task_parent` 之后。

- [ ] **Step 2: 创建索引**

```bash
python -c "
import sqlite3
db = sqlite3.connect('plastic_memory.db')
db.execute('CREATE INDEX IF NOT EXISTS idx_task_dedup ON task_queue(task_type, status, created_at)')
db.commit()
# Verify
indexes = db.execute(\"SELECT name FROM sqlite_master WHERE type='index' AND name='idx_task_dedup'\").fetchall()
print(f'Index created: {len(indexes) > 0}')
db.close()
"
```

Expected: `Index created: True`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/task_queue_schema.py
git commit -m "feat(task_queue): add idx_task_dedup index for dedup queries"
```

---

### Task 4: scan_architecture 标签黑名单（B）

**Files:**
- Modify: `plastic_promise/cron/scan_architecture.py:121-161` (Shotgun Surgery 循环)

**Interfaces:**
- Consumes: `os.environ` (existing import)
- Produces: `_get_tag_blacklist() -> set[str]` — 合并内置 + 环境变量的黑名单

- [ ] **Step 1: 在 Shotgun Surgery 检测前添加黑名单构建函数和过滤**

在 `scan_architecture.py` 的 `_compute_median_and_threshold` 函数之后、`scan_architecture` 函数之前，添加：

```python
# ── Tag blacklist for Shotgun Surgery ──────────────────────

def _get_tag_blacklist() -> set[str]:
    """Return the set of tags excluded from Shotgun Surgery detection.

    Built-in defaults cover system management tags and metadata
    classification tags that are expected to appear across domains.
    Extend via TAG_BLACKLIST_EXTRA env var (comma-separated).
    """
    builtin = {
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
    extra = os.environ.get("TAG_BLACKLIST_EXTRA", "")
    if extra:
        builtin.update(t.strip() for t in extra.split(",") if t.strip())
    return builtin
```

- [ ] **Step 2: 在 Shotgun Surgery 循环中应用过滤**

在 `scan_architecture` 函数内，找到 Shotgun Surgery 段（约第 121 行 `# 3. Shotgun surgery`），在 tag 判断循环中插入黑名单检查。

当前代码（约第 146 行）：
```python
                for tag, domains_set in tag_domains.items():
                    if len(domains_set) > threshold_spread and len(domains_set) >= 3:
```

改为：
```python
                blacklist = _get_tag_blacklist()
                for tag, domains_set in tag_domains.items():
                    if tag in blacklist:
                        continue
                    if len(domains_set) > threshold_spread and len(domains_set) >= 3:
```

- [ ] **Step 3: 验证黑名单导入和逻辑正确**

```bash
python -c "
import sys, os
sys.path.insert(0, '.')
from plastic_promise.cron.scan_architecture import _get_tag_blacklist

bl = _get_tag_blacklist()
print(f'Blacklist size: {len(bl)}')
assert 'task:done' in bl
assert 'cat:preference' in bl
assert 'source:file-sync' in bl
assert 'llm_pending:true' in bl
# Non-blacklisted tags
assert 'custom:important-tag' not in bl
print('Built-in blacklist OK')

# Test env var extension
os.environ['TAG_BLACKLIST_EXTRA'] = 'custom:noise,custom:debug'
bl2 = _get_tag_blacklist()
assert 'custom:noise' in bl2
assert 'custom:debug' in bl2
del os.environ['TAG_BLACKLIST_EXTRA']
print('Env var extension OK')
"
```

Expected: `Built-in blacklist OK` / `Env var extension OK`

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/cron/scan_architecture.py
git commit -m "feat(scan_architecture): add tag blacklist for Shotgun Surgery filtering"
```

---

### Task 5: 一次性清理低质量委托（C）

**Files:**
- Modify: `plastic_memory.db` (task_queue 表数据)

> **注意**: 此任务仅修改数据库中的数据，不涉及代码文件。

- [ ] **Step 1: 备份 task_queue 表**

```bash
python -c "
import sqlite3, shutil, os
from datetime import datetime
# Backup the entire DB before destructive ops
ts = datetime.now().strftime('%Y%m%d_%H%M%S')
backup_path = f'plastic_memory_backup_{ts}.db'
shutil.copy2('plastic_memory.db', backup_path)
# Also export task_queue to JSON for easy inspection
db = sqlite3.connect('plastic_memory.db')
db.row_factory = sqlite3.Row
rows = [dict(r) for r in db.execute('SELECT * FROM task_queue ORDER BY created_at').fetchall()]
with open(f'task_queue_backup_{ts}.json', 'w') as f:
    import json
    json.dump(rows, f, ensure_ascii=False, indent=2)
db.close()
print(f'Backup: {backup_path}')
print(f'JSON:   task_queue_backup_{ts}.json')
print(f'Rows:   {len(rows)}')
"
```

- [ ] **Step 2: 记录清理前状态**

```bash
python -c "
import sqlite3
db = sqlite3.connect('plastic_memory.db')
db.row_factory = sqlite3.Row
print('=== BEFORE CLEANUP ===')
total = db.execute('SELECT count(*) as cnt FROM task_queue').fetchone()['cnt']
scanned = db.execute('SELECT count(*) as cnt FROM task_queue WHERE source_scan IS NOT NULL').fetchone()['cnt']
by_type = db.execute('SELECT task_type, count(*) as cnt FROM task_queue GROUP BY task_type').fetchall()
print(f'Total: {total}, Scanner-generated: {scanned}')
for r in by_type:
    print(f'  {r[\"task_type\"]}: {r[\"cnt\"]}')
db.close()
"
```

- [ ] **Step 3: 删除重复委托（保留每组最早的 1 条）**

```bash
python -c "
import sqlite3
db = sqlite3.connect('plastic_memory.db')
# Count duplicates before
before = db.execute('SELECT count(*) as cnt FROM task_queue').fetchone()[0]
# Delete duplicates
db.execute('''
    DELETE FROM task_queue WHERE id NOT IN (
        SELECT min(id) FROM task_queue GROUP BY task_type, title
    )
''')
db.commit()
after = db.execute('SELECT count(*) as cnt FROM task_queue').fetchone()[0]
print(f'Dedup: {before} -> {after} (removed {before - after})')
db.close()
"
```

Expected: `Dedup: 139 -> 70 (removed 69)`

- [ ] **Step 4: 删除低信号 Shotgun Surgery 委托**

```bash
python -c "
import sqlite3
db = sqlite3.connect('plastic_memory.db')
# Count before
before = db.execute('SELECT count(*) as cnt FROM task_queue').fetchone()[0]
# Delete low-signal Shotgun Surgery tasks
db.execute('''
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
      )
''')
db.commit()
after = db.execute('SELECT count(*) as cnt FROM task_queue').fetchone()[0]
print(f'Low-signal removal: {before} -> {after} (removed {before - after})')
db.close()
"
```

Expected: `Low-signal removal: 70 -> 36 (removed 34)`

- [ ] **Step 5: 验证清理后状态**

```bash
python -c "
import sqlite3
db = sqlite3.connect('plastic_memory.db')
db.row_factory = sqlite3.Row
print('=== AFTER CLEANUP ===')
total = db.execute('SELECT count(*) as cnt FROM task_queue').fetchone()['cnt']
scanned = db.execute('SELECT count(*) as cnt FROM task_queue WHERE source_scan IS NOT NULL').fetchone()['cnt']
by_type = db.execute('SELECT task_type, count(*) as cnt FROM task_queue GROUP BY task_type ORDER BY cnt DESC').fetchall()
by_status = db.execute('SELECT status, count(*) as cnt FROM task_queue GROUP BY status').fetchall()
print(f'Total: {total}, Scanner-generated: {scanned}')
print('By type:')
for r in by_type:
    print(f'  {r[\"task_type\"]}: {r[\"cnt\"]}')
print('By status:')
for r in by_status:
    print(f'  {r[\"status\"]}: {r[\"cnt\"]}')
# Verify no remaining junk
junk = db.execute('''
    SELECT count(*) as cnt FROM task_queue
    WHERE task_type = 'decouple_domains'
      AND title LIKE '%Shotgun Surgery%'
      AND (
        title LIKE '%task:%' OR title LIKE '%llm_%' OR title LIKE '%audit%'
        OR title LIKE '%cat:preference%' OR title LIKE '%source:file-sync%'
      )
''').fetchone()['cnt']
print(f'Remaining low-signal: {junk} (should be 0)')
db.close()
"
```

Expected: `Total: 36`, `Remaining low-signal: 0`

- [ ] **Step 6: 确认（无 git commit — 仅数据变更）**

数据清理通过 SQL 直接修改 `plastic_memory.db`，不涉及代码文件，无需 git commit。

---

### Task 6: 端到端验证

**Files:**
- (无 — 验证现有代码行为正确)

- [ ] **Step 1: 验证 MCP 服务健康**

```bash
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read().decode())"
```

Expected: `{"status":"ok",...}`

- [ ] **Step 2: 验证去重逻辑（手动入队测试）**

```bash
python -c "
import sys, asyncio, json
sys.path.insert(0, '.')
from plastic_promise.mcp.tools.task_queue import handle_task_enqueue

async def test():
    # Simulate a scanner enqueue with source_scan
    args = {
        'task_type': 'decouple_domains',
        'title': 'TEST: God module detection',
        'to_agent': 'pi_builder',
        'priority': 3,
        'source_scan': 'scan_architecture',
        'payload': {'type': 'god_module', 'domain': 'test', 'count': 99},
    }
    # First enqueue should succeed
    r1 = await handle_task_enqueue(None, args)
    print('Enqueue 1:', json.loads(r1[0].text)['status'])
    # Second enqueue with same payload should be duplicate
    r2 = await handle_task_enqueue(None, args)
    print('Enqueue 2:', json.loads(r2[0].text)['status'])
    # Manual enqueue (no source_scan) should always succeed
    args3 = {**args, 'source_scan': None, 'from_agent': 'claude'}
    r3 = await handle_task_enqueue(None, args3)
    print('Manual enqueue:', json.loads(r3[0].text)['status'])

asyncio.run(test())
"
```

Expected: `Enqueue 1: pending` / `Enqueue 2: duplicate` / `Manual enqueue: pending`

- [ ] **Step 3: 清理测试委托**

```bash
python -c "
import sqlite3
db = sqlite3.connect('plastic_memory.db')
db.execute('DELETE FROM task_queue WHERE title LIKE \"%TEST:%\"')
db.commit()
count = db.execute('SELECT count(*) as cnt FROM task_queue WHERE title LIKE \"%TEST:%\"').fetchone()[0]
print(f'Test tasks remaining: {count}')
db.close()
"
```

Expected: `Test tasks remaining: 0`

- [ ] **Step 4: 最终状态确认**

```bash
python -c "
import sqlite3
db = sqlite3.connect('plastic_memory.db')
db.row_factory = sqlite3.Row
total = db.execute('SELECT count(*) as cnt FROM task_queue').fetchone()['cnt']
scanned = db.execute('SELECT count(*) as cnt FROM task_queue WHERE source_scan IS NOT NULL').fetchone()['cnt']
print(f'Final: {total} total, {scanned} scanner-generated')
# Verify index exists
idx = db.execute(\"SELECT name FROM sqlite_master WHERE type='index' AND name='idx_task_dedup'\").fetchall()
print(f'idx_task_dedup: {\"present\" if idx else \"MISSING\"}'  )
db.close()
"
```

Expected: `Final: 36 total, 36 scanner-generated` / `idx_task_dedup: present`
