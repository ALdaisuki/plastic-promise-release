# Exemplar Research — Task Dedup & Scanner Noise Filtering

**日期**: 2026-07-03
**关联 Spec**: `2026-07-03-task-quality-hardening-design.md`

## 参考实现

| # | 项目 | 领域 | 成熟度 |
|---|------|------|--------|
| 1 | celery-singleton | Task dedup (Redis) | 生产验证, MIT |
| 2 | Manual idempotency keys | Task dedup (Redis) | 生产验证 |
| 3 | OWASP-BLT atomic UPSERT | Task dedup (SQLite) | 社区讨论, 设计阶段 |
| 4 | SonarQube suppression | Scanner noise filtering | 企业级, >137M issues |

---

## Reference 1: celery-singleton

**Source**: `steinitzu/celery-singleton` (MIT, GitHub)

### Q1: What exactly does it do?

Lock key = `prefix + hash(task_name + json.dumps(args, sort_keys=True))`.
Uses Redis `SETNX` (atomic) to acquire lock before enqueue.
Duplicate calls return the SAME `AsyncResult` — task queued once.

```
┌──────────────┐    SETNX(key, task_id)    ┌───────┐
│  Producer A  │ ─────────────────────────→│ Redis │
│  do_stuff(1) │   ← OK (lock acquired)    │       │
└──────────────┘                           │       │
                                           │       │
┌──────────────┐    SETNX(key, task_id)    │       │
│  Producer B  │ ─────────────────────────→│       │
│  do_stuff(1) │   ← FAIL (exists)         └───────┘
└──────────────┘   → returns same AsyncResult
```

Key design decisions:
- `unique_on=['username']` — restrict hash to specific args, ignore others
- `raise_on_duplicate=True` — exception instead of silent return
- `lock_expiry=10` — auto-release after N seconds (crash safety)
- `clear_locks()` on `worker_ready` signal — deadlock recovery
- Pluggable backend (`BaseBackend` → `RedisBackend`)

### Q2: How does our context differ?

| celery-singleton | Plastic Promise |
|------------------|-----------------|
| Redis as lock store | SQLite (`plastic_memory.db`) |
| In-memory lock (fast, volatile) | Persistent DB row (durable, slower) |
| Lock = prevent concurrent EXECUTION | Dedup = prevent duplicate ENQUEUE |
| Task args are function params | Task payload is arbitrary dict |
| No time window — lock lasts until task done | 24h time window — same issue may reappear |
| No governance layer | Trust scores, principles, audit trail |

### Q3: What to adapt vs skip?

**Adapt:**
- `unique_on` → our `source_scan` gate: only auto-generated tasks are deduped; manual tasks bypass
- Hash generation: `json.dumps(payload, sort_keys=True)` → `hashlib.sha256()[8:]` — we already do this
- `raise_on_duplicate` → our `{status: "duplicate", existing_task_id}` return

**Redesign:**
- Lock expiry → time window (24h). Our tasks are persistent DB rows, not ephemeral locks. After 24h the same scanner finding SHOULD be re-reportable.
- `clear_locks()` on startup → our dedup window is SQL `created_at > datetime('now', '-24 hours')` — auto-expires, no manual cleanup needed.

**Skip:**
- Redis backend — we use SQLite exclusively, no external dependencies
- `worker_ready` signal — no Celery workers in our architecture

---

## Reference 2: Manual Idempotency Keys

**Source**: Production clinical imaging system (dev.to article)

### Q1: What exactly does it do?

```python
def make_idempotency_key(task_name: str, payload: dict) -> str:
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"task:{task_name}:{payload_hash}"

class IdempotentTask(Task):
    def __call__(self, *args, **kwargs):
        key = make_idempotency_key(self.name, kwargs)
        lock_acquired = r.set(key, "processing", nx=True, ex=300)
        if not lock_acquired:
            return {"status": "skipped", "reason": "duplicate"}
        try:
            result = self.run(*args, **kwargs)
            r.set(key, "completed", ex=86400)  # mark done for 24h
            return result
        except Exception:
            r.delete(key)  # release so it can retry
            raise
```

Critical production lessons:
1. **Lock expiry > 2× p99 task duration** — prevents zombie lock from blocking retry
2. **Idempotency key MUST be deterministic from payload**, not from task ID
3. **On failure, release lock** — different from "completed" state

### Q2: How does our context differ?

| Manual pattern | Plastic Promise |
|----------------|-----------------|
| Redis for atomic lock | SQLite with multi-row queries |
| Lock + complete are separate Redis keys | Single DB row state: pending → dup check |
| Failure = delete lock | Our tasks stay pending indefinitely |
| 300s lock, 86400s completion marker | Our unified 24h window |

### Q3: What to adapt vs skip?

**Adapt:**
- Deterministic hash from sorted JSON: EXACTLY our `_compute_payload_hash` pattern
- `payload_hash` exclusion: when computing hash, strip `payload_hash` key (we do this)
- 24h completion window = our dedup window

**Redesign:**
- Redis `SETNX` → SQLite `SELECT ... WHERE payload_hash = ? AND created_at > now-24h`
- Failure handling: our tasks don't "fail" — they're either pending, claimed, or done. The dedup only applies to `status='pending'`.

**Skip:**
- Redis TTL-based expiry — we use SQL `WHERE` clauses on `created_at`
- Separate "processing" vs "completed" states — our `status` column already tracks this

---

## Reference 3: OWASP-BLT Atomic UPSERT (SQLite-native)

**Source**: `OWASP-BLT/BLT-NetGuardian` Issue #14

### Q1: What exactly does it do?

```sql
INSERT INTO task_hashes (task_hash, created_at)
VALUES (?, ?)
ON CONFLICT(task_hash) DO UPDATE
  SET created_at = excluded.created_at
  WHERE created_at <= datetime('now', '-24 hours')
```

Key insight: **expired row refresh**. When a hash from >24h ago is re-encountered, don't insert a new row OR reject — UPDATE the old row's timestamp. This means:
- Same task within 24h → rejected (ON CONFLICT fires, WHERE clause doesn't match → no-op → row unchanged)
- Same task after 24h → accepted (WHERE clause matches → timestamp refreshed)

This is atomic — no TOCTOU race.

### Q2: How does our context differ?

| OWASP pattern | Plastic Promise |
|---------------|-----------------|
| Separate `task_hashes` table | Hash stored in `task_queue.payload` JSON |
| Atomic UPSERT via UNIQUE constraint | SELECT-then-INSERT (not atomic) |
| Hash-only table (lightweight) | Full task row (payload + metadata) |

### Q3: What to adapt vs skip?

**Adapt:**
- Time window concept: 24h WHERE clause on `created_at`
- Expired row refresh: our SELECT-based approach naturally "re-accepts" after 24h because the WHERE clause won't match stale rows

**Redesign:**
- Atomic UPSERT → SELECT-then-INSERT. In our context (single daemon, sequential scanners), race conditions are not a concern. The daemon runs scanners sequentially on one thread — no concurrent enqueues.
- Separate table → inline JSON. Our payload_hash lives in the payload column — less normalized but avoids schema migration.

**Skip:**
- UNIQUE constraint on hash — we have composite dedup (type + hash + time), can't use simple UNIQUE
- Separate `task_hashes` table — YAGNI at our scale (246 memories, ~100 pending tasks)

---

## Reference 4: SonarQube Issue Suppression

**Source**: SonarSource docs, >137M reviewed issues

### Q1: What exactly does it do?

Multi-layered noise reduction:

```
Layer 1: Deep semantic analysis (AST/CFG/DFG/taint)
  → Only flag when confident (e.g., SQLi only when tainted input reaches sink)

Layer 2: Framework auto-detection
  → Recognize Django auto-escaping → suppress XSS rules

Layer 3: Project-level exclusions
  → Exclude test/, vendor/, generated code

Layer 4: Rule-level suppression
  → "Won't Fix" / "False Positive" manual tagging
  → Per-rule file exclusions

Layer 5: Quality Profiles
  → Activate/deactivate rulesets per project
```

The key insight: **suppression is layered, not binary**. Each layer filters a different class of noise.

### Q2: How does our context differ?

| SonarQube | Plastic Promise scan_architecture |
|-----------|-----------------------------------|
| Multi-language AST analysis | SQL queries on memory tags |
| Framework auto-detection | No framework context |
| Project-level exclusions (files) | No file-level concept |
| Rule-level manual suppression | No UI for manual tagging |
| Quality profiles per project | Single deployment, no profiles |

### Q3: What to adapt vs skip?

**Adapt:**
- **Layered suppression**: Our blacklist is Layer 1. Future layers could be:
  - Layer 2: Tag prefix rules (`task:*`, `branch:*`, `llm_*`)
  - Layer 3: Dynamic threshold (only flag if domain_count > median + 2σ, already implemented)
- **Built-in defaults + user extensibility**: SonarQube's Quality Profiles concept → our `TAG_BLACKLIST_EXTRA` env var

**Redesign:**
- Manual "Won't Fix" tagging → our dedup + 24h window already handles this: if a finding is noise, it won't re-appear until the next day. Accept the first report, then ignore duplicates.

**Skip:**
- Code-level `// nosemgrep` comments — our scanners operate on DB data, not source files
- Per-project profiles — single deployment
- AI-powered triage (Semgrep Assistant) — out of scope for now

---

## Synthesis: Patterns We're Adopting

| Pattern | Source | Integration Point | Adaptation |
|---------|--------|-------------------|------------|
| Deterministic hash from `json.dumps(payload, sort_keys=True)` | Ref #2 | `_compute_payload_hash()` | Exclude `payload_hash` key from hash |
| Time-windowed dedup (24h) | Ref #3 | `handle_task_enqueue` dedup query | `created_at > datetime('now', '-24 hours')` |
| Scope-gated dedup (only auto-generated tasks) | Ref #1 `unique_on` | `if source_scan is not None` gate | Only scanner tasks, not manual enqueues |
| Layered suppression (blacklist as Layer 1) | Ref #4 | `_get_tag_blacklist()` in scan_architecture | Built-in + `TAG_BLACKLIST_EXTRA` env var |
| Duplicate return (not exception) | Ref #1 `raise_on_duplicate=False` | `{status: "duplicate", existing_task_id}` | Non-breaking for callers |

## Patterns We're NOT Adopting (with reasons)

| Pattern | Source | Reason |
|---------|--------|--------|
| Redis SETNX atomic lock | Ref #1, #2 | No Redis in our stack; SQLite is the single source of truth |
| Atomic UPSERT (ON CONFLICT) | Ref #3 | Single-threaded daemon → no race condition; composite dedup key → can't use simple UNIQUE |
| Separate dedup hash table | Ref #3 | YAGNI at our scale; inline JSON works for <1000 pending tasks |
| Code-level `// nosemgrep` comments | Ref #4 | Scanners operate on DB data, not source files |
| AI-powered triage (LLM) | Ref #4 | Out of scope; governance overhead for LLM calls in scanner loop |
