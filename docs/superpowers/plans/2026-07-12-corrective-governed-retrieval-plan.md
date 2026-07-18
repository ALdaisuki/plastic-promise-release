# Corrective Governed Retrieval Completion Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Keep the checkboxes current. Do not combine RED, GREEN, review, and commit evidence across tasks.

**Goal:** Close the remaining correctness and evidence gaps in governed retrieval: lossless canonical ordinary-memory mutation, synchronous dependent invalidation, checked durable index recovery, independent daemon scheduling, deterministic versioned fusion, and a real held-out bilingual quality proof.

The release verification suite consumes this plan as the authoritative source
for its preregistered recall constraints.

**Architecture:** SQLite stays authoritative. Existing ordinary rows change only through a field-scoped transaction at `_SQLiteStorage`; a source-mutation coordinator owns retrieval-visible content or availability changes and writes lineage, dependent synthesis state, and checked outbox jobs before commit. LanceDB remains a derived projection repaired after commit. The daemon uses independent monotonic deadlines and persists ordered cycle evidence. Fusion is an explicit policy over admitted vector/BM25/FTS rankings, while graph and provenance remain separate evidence layers. Quality claims require complete constituent rankings, a frozen calibration-selected WRRF manifest, and public HTTP MCP runs on a new held-out corpus.

**Tech Stack:** Python 3.10+, SQLite, LanceDB >= 0.34.0, MCP Streamable HTTP, pytest, Ruff, the existing Plastic Promise ContextEngine/MemoryPipeline/daemon stack, and the existing Rust `context-engine-core` extension.

**Starting point:** branch `codex/governed-synthesis-retrieval`, commit `f5ebdaa`; baseline evidence is 1331 Python tests passed with 22 skipped and 52 Rust tests passed in the worktree `.venv`.

## Non-Negotiable Invariants

- Never implement an existing-row mutation as `get_memory() -> mutate MemoryRecord -> store_memory()`.
- `_SQLiteStorage.patch_ordinary()` compiles SQL only from fixed field allowlists and uses SQL arithmetic for counters.
- `wrong`, `deprecated`, `forgotten`, and content replacement stale every currently verified dependent and enqueue all ordinary/synthesis index work in the same SQLite transaction.
- No LanceDB side effect occurs before the canonical transaction commits. Failed checked work remains durable and replayable.
- Whole-record `store_memory()`/`register_memory()` become creation-only only after every caller is classified and migrated; identical create replay uses `create_ordinary_if_absent()`.
- The default fusion policy remains `legacy-auto` until the frozen candidate passes the public held-out gate. `max-v1` is the comparable Python weighted-max baseline. A runnable WRRF policy is always the deterministic immutable ID `wrrf-v1:<64-lowercase-hex-config-hash>`; the CLI shorthand `wrrf-v1` is permitted only with `--candidate-manifest` and is normalized to that exact manifest ID before any HTTP MCP process or query is started. A bare token without a manifest, a malformed hash, or an explicit hash differing from the manifest fails closed.
- `fusion_channels` contains only `vector`, `bm25`, and `fts`. Graph, code, audit, principle, canonical-hot, and synthesis-source expansion never enter WRRF.
- Constituent metrics use complete admitted pre-fusion rankings, never `per_item_stats` reconstructed from fused survivors.
- V1 is calibration-only. Candidate selection completes and the manifest is written before any held-out result is read.
- Every focused Python command uses the worktree interpreter and `--no-cov`; each task lists its exact paths.
- Generated `.artifacts/` and `.pytest-*` directories are user/runtime state. Do not delete, reset, or commit them.

## Preregistered Fusion Search

This grid is fixed before implementation or calibration results are inspected:

```json
{
  "schema": "wrrf-calibration-grid/v1",
  "k_values": [2],
  "weight_sets": [
    {"vector": 0.55, "bm25": 0.30, "fts": 0.15},
    {"vector": 0.60, "bm25": 0.25, "fts": 0.15},
    {"vector": 0.65, "bm25": 0.20, "fts": 0.15}
  ],
  "channel_windows": [
    {"vector": 20, "bm25": 20, "fts": 20},
    {"vector": 32, "bm25": 24, "fts": 16}
  ],
  "primary_quality_metric": "overall.fused.mrr",
  "minimum_primary_delta": 0.01,
  "required_split_tolerance": 0.0,
  "max_p95_ratio": 1.20,
  "selection_order": [
    "maximize minimum required-split fused MRR",
    "maximize overall fused hit@5",
    "maximize overall fused MRR",
    "minimize p95 latency",
    "lexicographically smallest canonical config JSON"
  ]
}
```

The selector rejects any configuration that regresses overall or required-split fused MRR/hit@5 below `max-v1`, falls below the best enabled constituent, increases forbidden hits, degrades, or violates the latency budget. It selects exactly one survivor; no survivor means the experiment fails without retuning.

## File Map

- Create `plastic_promise/core/ordinary_memory_mutation.py`: typed mutation results, correction preparation, transaction coordinator, and dependent invalidation orchestration.
- Create `plastic_promise/core/fusion_policy.py`: policy/config parsing, canonical hashing, weighted RRF, channel state, and runtime capability decisions.
- Create `plastic_promise/core/maintenance_scheduler.py`: injectable independent monotonic deadline registry.
- Create `plastic_promise/core/recall_experiment.py`: calibration grid, frozen candidate manifest, held-out/comparability validation, and best-constituent gates.
- Create `scripts/smoke_restart_recovery.py`: real MCP/daemon restart and checked-index recovery proof.
- Create `scripts/http_mcp_harness.py`: shared process lifecycle and Streamable HTTP tool client used by recovery and quality scripts.
- Create `tests/test_ordinary_memory_mutation.py`, `tests/test_fusion_policy.py`, `tests/test_maintenance_scheduler.py`, `tests/test_smoke_restart_recovery.py`, and `tests/test_recall_experiment.py`.
- Create `tests/fixtures/recall_quality/wrrf-v1-grid.json`, `tests/fixtures/recall_quality/wrrf-v1-golden.json`, and `tests/fixtures/recall_quality/v2-heldout.json`.
- Modify `plastic_promise/core/context_engine.py`, `synthesis.py`, `synthesis_retrieval.py`, `synthesis_maintenance.py`, `traceability.py`, `retrieval_planner.py`, and `recall_quality.py`.
- Modify `plastic_promise/memory/pipeline.py`, `plastic_promise/memory/soul_memory.py`, and every classified ordinary-memory mutation/create caller.
- Modify `plastic_promise/mcp/server.py`, `plastic_promise/mcp/tools/memory.py`, and `plastic_promise/mcp/tools/reflection.py` to supply server-owned mutation evidence.
- Modify `daemons/maintenance_daemon.py`, `plastic_promise/launcher/service_manager.py`, and `scripts/init_and_start.py`.
- Modify `rust/context-engine-core/src/retrieval/fusion.rs`, `rust/context-engine-core/src/context_engine.rs`, and Rust/Python parity tests.
- Extend focused regression files named in each task; update operational documentation only after behavior is verified.

## Acceptance Ownership

| Design acceptance | Owning tasks | Required evidence |
|---|---|---|
| 1-2 | 4-6, 14 | Existing draft/verify gates remain green; only verified compact synthesis is searchable. |
| 3 | 4-5, 14 | Changed/wrong/deprecated/forgotten sources synchronously stale dependents; contradiction/supersession regressions stay green. |
| 4 | 4, 14 | Refresh still creates a new draft revision requiring verification. |
| 5 | 2, 6, 14 | Proposal/adoption routing remains governed with no direct-write bypass. |
| 6 | 9-11, 14 | Python/Rust status parity and whole-request capability routing. |
| 7 | 11, 14 | High-impact source evidence remains bounded and traceable outside fusion. |
| 8 | 11-13, 14 | Reproducible bilingual hit@k/MRR/forbidden/latency/degradation reports and splits. |
| 9 | 6, 9, 14 | Gates-off compatibility, with only unconditional correction/scheduler fixes. |
| 10 | 3-5 | Auditable wrong/deprecated tombstones and fully reindexed current correction lineage. |
| 11 | 3-5 | Source patch, lineage, dependent stale state, and both index job classes share one transaction. |
| 12 | 0-6 | Private/non-default-project compact rows preserve every non-target canonical field. |
| 13 | 7 | Independent reachable monotonic schedules plus durable parent/child evidence. |
| 14 | 8 | Real HTTP MCP/daemon PID death, restart, replay, and current-only `recovery-smoke/v1`. |
| 15 | 9-13 | Frozen candidate, Python/Rust golden parity, public held-out baseline and best-constituent pass. |
| 16 | 14 | Full Python/Rust suites under LanceDB >= 0.34.0; older-API fallback fails. |

## Execution Order

Execute canonical mutation in strict order `0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6`; Task 3's durable delete/upsert contract must exist before Task 4 relies on it, and existing-ID rejection is last. Scheduler/recovery then runs `7 -> 8`. Fusion/quality runs `9 -> 10 -> 11 -> 12 -> 13`; complete channel evidence must exist before calibration, and the public live runner must freeze the V1-selected manifest before its first held-out retrieval. Task 14 starts only after all three chains are green. Tasks 7-8 may be implemented in parallel with 9-13 only after Task 6 if agents use non-overlapping files and each task receives its own review/commit.

---

### Task 0: Freeze the Ordinary-Memory Writer Inventory

**Files:**
- Create: `docs/engineering-patterns/2026-07-12-ordinary-memory-caller-inventory.md`
- Create: `tests/test_ordinary_memory_callers.py`

**Inventory contract:**

The document records every production caller of `store_memory`, `register_memory`, `_persist_ordinary_memory`, `upsert_ordinary`, `update_memory`, `update_memory_fields`, `increment_field`, `batch_update`, `delete_memory`, and direct raw SQL `INSERT`/`UPDATE`/`DELETE`/`REPLACE` against `memories` or retrieval-visible memory columns. Each row contains exact path/symbol, whether an existing ID is possible, current semantics, target owner (`create_ordinary_if_absent`, `patch_ordinary_memory`, or `ContextEngine.mutate_ordinary_source`), focused regression test, and migration task.

- [ ] **Step 1: Capture the complete caller set with exact searches**

```powershell
rg -n "\.store_memory\(|\.register_memory\(|_persist_ordinary_memory\(|\.upsert_ordinary\(" plastic_promise daemons scripts -g "*.py"
rg -n "\.update_memory\(|update_memory_fields\(|increment_field\(|batch_update\(|delete_memory\(" plastic_promise daemons scripts -g "*.py"
rg -n -i "\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+memories\b" plastic_promise daemons scripts -g "*.py"
```

The baseline must explicitly classify direct lossy paths in `mcp/tools/reflection.py`, `mcp/tools/memory.py`, and `memory/soul_memory.py`; indirect whole-row writes in ContextEngine; RecMem caller-supplied IDs; pack replace/import; deterministic skill IDs; lifecycle scan SQL; `maintenance_daemon.py` duplicate-cluster cleanup's current `DELETE FROM memories`; and the already-safe proposal promotion insert. The daemon duplicate cleanup is an existing-ID availability mutation and must migrate to `ContextEngine.mutate_ordinary_source(..., operation="forgotten", reason="safety-net:duplicate_cluster")`, never retain a raw `DELETE` or direct retrieval-visible update.

- [ ] **Step 2: Add an AST-backed inventory regression**

`tests/test_ordinary_memory_callers.py` parses production Python files and compares `(path, enclosing_symbol, called_method)` tuples to the committed inventory. It fails on an unclassified writer or a classified row without a target API/test. It does not assert line numbers, so formatting does not create noise.

- [ ] **Step 3: Verify and independently review the inventory**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_callers.py -q --no-cov
```

Expected: the inventory test passes against the known baseline and reports zero unclassified writers. This is an audit snapshot, not proof that migration is complete; Task 6 changes every row status to migrated before enabling existing-ID rejection.

- [ ] **Step 4: Commit the inventory before implementation**

```powershell
git add docs/engineering-patterns/2026-07-12-ordinary-memory-caller-inventory.md tests/test_ordinary_memory_callers.py
git commit -m "test: inventory ordinary memory writers"
```

---

### Task 1: Field-Scoped Canonical Ordinary-Memory Patch

**Files:**
- Modify: `plastic_promise/core/context_engine.py:659, 898-967, 1440-1621, 5217-5690`
- Create: `tests/test_ordinary_memory_mutation.py`
- Test: `tests/test_memory_record.py`
- Test: `tests/test_project_memory_schema.py`

**Interfaces:**

```python
class OrdinaryMemoryConflict(RuntimeError): ...

_ORDINARY_SCALAR_PATCH_FIELDS: frozenset[str]
_ORDINARY_JSON_PATCH_FIELDS: frozenset[str]
_ORDINARY_NUMERIC_INCREMENT_FIELDS: frozenset[str]
_RETRIEVAL_VISIBLE_PATCH_FIELDS: frozenset[str]

def _SQLiteStorage.patch_ordinary(
    self,
    mid: str,
    *,
    replacements: Mapping[str, Any] | None = None,
    increments: Mapping[str, int | float] | None = None,
    expected_content_hash: str | None = None,
    expected_embedding_hash: str | None = None,
    bump_memory_version: bool | None = None,
) -> dict[str, Any]: ...

def ContextEngine.patch_ordinary_memory(
    self,
    memory_id: str,
    *,
    replacements: Mapping[str, Any] | None = None,
    increments: Mapping[str, int | float] | None = None,
    expected_content_hash: str | None = None,
    expected_embedding_hash: str | None = None,
    bump_memory_version: bool | None = None,
) -> dict[str, Any]: ...
```

`patch_ordinary()` must require exactly one ordinary, unreserved row, serialize JSON fields canonically, apply increments as `column = COALESCE(column, 0) + ?`, apply either/both content/index CAS predicates, infer the version bump from retrieval-visible replacements unless explicitly set, and return `_row_to_dict()` from the committed row. The ContextEngine wrapper holds `_write_lock`, delegates once, and replaces only `self._memories[memory_id]` from the canonical result.

- [ ] **Step 1: Write the RED preservation, allowlist, CAS, and concurrency tests**

```python
def test_patch_ordinary_preserves_every_untargeted_canonical_column(engine, rich_row):
    before = canonical_row(engine, rich_row["id"])
    after = engine.patch_ordinary_memory(
        rich_row["id"],
        increments={"worth_success": 1},
        replacements={"last_accessed": "2026-07-12T00:00:00Z"},
    )
    assert after["worth_success"] == before["worth_success"] + 1
    assert_untargeted_columns_identical(before, after, {"worth_success", "last_accessed"})

def test_patch_ordinary_rejects_unknown_fields_without_mutation(engine, rich_row):
    before = canonical_row(engine, rich_row["id"])
    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_field_not_allowed"):
        engine.patch_ordinary_memory(rich_row["id"], replacements={"project_owner": "x"})
    assert canonical_row(engine, rich_row["id"]) == before

def test_patch_ordinary_cas_rejects_stale_index_material(engine, rich_row):
    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_cas_mismatch"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements={"content": "new"},
            expected_embedding_hash="stale-hash",
        )

def test_numeric_increments_are_atomic_across_two_sqlite_connections(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    first = seeded_engine(db_path, worth_success=2)
    second = ContextEngine(use_sqlite=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(increment_n_times, first, "m1", 25),
            pool.submit(increment_n_times, second, "m1", 25),
        ]
        assert [future.result() for future in futures] == [25, 25]
    assert canonical_row(first, "m1")["worth_success"] == 52
```

- [ ] **Step 2: Run RED and record the expected missing API**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py -q --no-cov
```

Expected: collection/import failure for `OrdinaryMemoryConflict` or `AttributeError: '_SQLiteStorage' object has no attribute 'patch_ordinary'`. Do not weaken assertions to reach GREEN.

- [ ] **Step 3: Implement the fixed allowlist compiler and canonical refresh**

Keep SQL identifiers code-owned. Reject an empty patch, bool numeric increments, non-finite numbers, attempts to mutate `id`, `memory_type` to synthesis, reserved IDs, and rows affected other than one. Use the existing batch/savepoint ownership rules; never commit a caller-owned transaction.

- [ ] **Step 4: Prove byte preservation and atomic arithmetic**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py tests/test_memory_record.py tests/test_project_memory_schema.py -q --no-cov
.venv\Scripts\python.exe -m ruff check plastic_promise/core/context_engine.py tests/test_ordinary_memory_mutation.py
```

Expected: all pass. Inspect the SQLite row, not a public `MemoryRecord`, in preservation assertions.

- [ ] **Step 5: Commit the primitive without migrating callers**

```powershell
git add plastic_promise/core/context_engine.py tests/test_ordinary_memory_mutation.py tests/test_memory_record.py tests/test_project_memory_schema.py
git commit -m "feat: add field-scoped ordinary memory patches"
```

---

### Task 2: Migrate Feedback, Worth Reset, RecMem, and Metadata-Only Updates

**Files:**
- Modify: `plastic_promise/mcp/tools/reflection.py:370-520`
- Modify: `plastic_promise/mcp/tools/memory.py:1503-1568`
- Modify: `plastic_promise/memory/soul_memory.py:1030-1095`
- Modify: `plastic_promise/core/context_engine.py:1553-1703`
- Modify only classified metadata callers in `plastic_promise/memory/pipeline.py`, `plastic_promise/mcp/tools/skill_tracking.py`, `plastic_promise/mcp/server.py`, and `plastic_promise/core/pack_index.py`
- Test: `tests/test_memory_proposals.py`
- Test: `tests/test_skill_tracking.py`
- Test: `tests/test_pipeline_quality.py`
- Test: `tests/test_soul_memory_governance.py`
- Test: `tests/test_ordinary_memory_mutation.py`

**Interfaces:**

```python
def ContextEngine.apply_ordinary_feedback(
    self, memory_id: str, feedback_type: Literal["adopted", "ignored", "rejected"]
) -> dict[str, Any]: ...

def ContextEngine.reset_ordinary_worth(self, memory_id: str) -> dict[str, Any]: ...
```

`ignored` preserves the existing half-failure semantics using a numeric SQL increment. `update_memory_fields()` remains compatibility-facing but delegates only allowed non-content fields to `patch_ordinary_memory`; content is rejected with `ordinary_content_requires_coordinator` until Task 4 installs the coordinator.

- [x] **Step 1: Add RED tests that seed private compact-v2 non-default-project rows**

```python
@pytest.mark.parametrize("feedback_type,success_delta,failure_delta", [
    ("adopted", 1, 0), ("rejected", 0, 1), ("ignored", 0, 0.5),
])
async def test_feedback_changes_only_declared_counters(
    engine, rich_compact_private_row, feedback_type, success_delta, failure_delta
):
    before = canonical_row(engine, rich_compact_private_row["id"])
    result = json_result(await handle_feedback_apply(
        engine, {"item_id": before["id"], "feedback_type": feedback_type}
    ))
    after = canonical_row(engine, before["id"])
    assert result["updated"] is True
    assert after["worth_success"] == before["worth_success"] + success_delta
    assert after["worth_failure"] == before["worth_failure"] + failure_delta
    assert_untargeted_columns_identical(
        before, after, {"worth_success", "worth_failure", "last_accessed"}
    )

```

Add three separate tests with these exact assertions:

- `test_memory_update_reset_worth_preserves_index_and_provenance`: counters become zero; every project/visibility/provenance/raw/L0/L1/L2/index field stays byte-identical.
- `test_recmem_feedback_uses_atomic_engine_feedback_not_store_memory`: a spy makes `store_memory` raise, feedback still commits exactly one counter increment through `apply_ordinary_feedback`, and the Python-side RecMem cache matches the committed score.
- `test_metadata_update_rejects_content_until_coordinator_is_installed`: `update_memory_fields(content=...)` returns/raises the stable coordinator-required reason and leaves SQLite/cache unchanged; allowed tag/domain patches still pass.

- [x] **Step 2: Run RED and verify the lossy whole-row paths are exercised**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py tests/test_memory_proposals.py tests/test_soul_memory_governance.py -q --no-cov
```

Expected: preservation assertions fail because `reflection.py` and RecMem still call `store_memory(record)`, or the new `apply_ordinary_feedback` API is absent.

- [x] **Step 3: Replace read/modify/write counters and reset paths**

Return the committed row's worth score. Keep graph feedback synchronization after the canonical commit. Remove every fallback that stores a hydrated record as an update. Convert `increment_field()` and metadata-only `update_memory_fields()` to one patch call; batch updates remain one SQLite batch but execute field-scoped statements.

- [x] **Step 4: Run focused compatibility and static caller searches**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py tests/test_memory_proposals.py tests/test_skill_tracking.py tests/test_pipeline_quality.py tests/test_soul_memory_governance.py -q --no-cov
rg -n "get_memory\(|store_memory\(" plastic_promise/mcp/tools/reflection.py plastic_promise/memory/soul_memory.py plastic_promise/mcp/tools/memory.py
```

Expected: tests pass. The search may still show creation and content-correction lines scheduled for Tasks 4-6, but ordinary feedback and worth reset must no longer form a get/store pair.

- [x] **Step 5: Commit the narrow mutation migration**

```powershell
git add plastic_promise/core/context_engine.py plastic_promise/mcp/tools/reflection.py plastic_promise/mcp/tools/memory.py plastic_promise/memory/soul_memory.py plastic_promise/memory/pipeline.py plastic_promise/mcp/tools/skill_tracking.py plastic_promise/mcp/server.py plastic_promise/core/pack_index.py tests/test_ordinary_memory_mutation.py tests/test_memory_proposals.py tests/test_skill_tracking.py tests/test_pipeline_quality.py tests/test_soul_memory_governance.py
git commit -m "fix: preserve canonical fields during ordinary feedback"
```

Before committing, stage only tests actually changed in this task; do not stage generated pytest directories.

---

### Task 3: Checked Ordinary Index Upsert/Delete Contract and Fault Boundary

**Files:**
- Modify: `plastic_promise/core/traceability.py:310-422`
- Modify: `plastic_promise/core/synthesis_maintenance.py:396-720`
- Modify: `plastic_promise/core/context_engine.py`
- Modify: `plastic_promise/core/lancedb_store.py`
- Test: `tests/test_synthesis_maintenance.py`
- Test: `tests/test_lancedb_store.py`
- Test: `tests/test_release_resilience_outbox.py`

**Interfaces:**

```python
MEMORY_INDEX_JOB_SCHEMA = "memory-index/v3"

def enqueue_memory_index_job(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    project_id: str,
    action: Literal["upsert", "delete"],
    expected_embedding_hash: str,
    call_id: str,
) -> str: ...

def enqueue_memory_index_upsert(
    conn: sqlite3.Connection, *, memory_id: str, project_id: str,
    expected_embedding_hash: str, call_id: str,
) -> str: ...
def enqueue_memory_index_delete(
    conn: sqlite3.Connection, *, memory_id: str, project_id: str,
    expected_embedding_hash: str, call_id: str,
) -> str: ...

def consume_test_index_failure(*, action: str, memory_id: str) -> None: ...
```

V3 payload keys are exactly `action`, `expected_embedding_hash`, `material_revision`, `memory_id`, `memory_version`, and `project_id`; call ID remains a canonical outbox column. The dedupe key includes action, memory version, expected hash, and project. Replay keeps backward-compatible support for existing valid `memory-index/v2` upserts, while all new writes use V3. Replay reloads canonical SQLite state and exact `IndexMaterial` under the index-state lock. A stale upsert/delete becomes a safe no-op; it never overwrites or removes newer material.

- [x] **Step 1: Add RED checked-delete, stale-job, lease, and marker tests**

```python
def test_failure_marker_requires_test_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("PP_TEST_INDEX_FAIL_MARKER", str(tmp_path / "marker.json"))
    monkeypatch.delenv("PP_TEST_MODE", raising=False)
    with pytest.raises(RuntimeError, match="index_failure_marker_requires_test_mode"):
        validate_test_index_failure_configuration()

```

Add the remaining tests as separate functions with exact outcomes:

- `test_checked_delete_is_bound_to_blocked_state_and_exact_material`: V3 delete removes the matching blocked row's vector and completes its outbox row.
- `test_old_delete_cannot_remove_newer_corrected_vector`: after enqueueing delete for hash A and committing current hash B, replay keeps B and marks the old job done as a stale no-op.
- `test_old_upsert_cannot_resurrect_wrong_or_deprecated_memory`: replay of an A upsert against a current tombstone deletes/keeps absent the vector and never calls `replace_checked`.
- `test_memory_index_v3_rejects_unknown_or_incomplete_payload`: each invalid row returns to pending with `ValueError` evidence and no derived side effect.
- `test_historical_memory_index_v2_upsert_still_replays`: an exact committed V2 payload reaches done and materializes the canonical vector.
- `test_failure_marker_is_atomically_consumed_once`: two concurrent matching side effects yield exactly one injected failure, decrement `remaining` to zero, and leave valid JSON or remove the exhausted marker atomically.

- [x] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_synthesis_maintenance.py tests/test_release_resilience_outbox.py -q --no-cov
```

Expected: delete enqueue is missing and V2 replay rejects `action=delete`.

- [x] **Step 3: Implement V3 enqueue/replay and the environment-only failure seam**

Validate the failure marker at process/engine startup. The JSON marker schema is `test-index-failure/v1` with exact `action`, `memory_id`, and positive integer `remaining`. Consume it with an atomic same-directory replace before raising `InjectedIndexFailure`; no MCP field or public function may enable it. Call the hook immediately before real `replace_checked()`/`delete_checked()`.

Rollback: do not deploy a V2-only replay consumer while V3 rows remain pending. Drain or replay those rows with the V3-capable consumer first; valid historical V2 upserts remain supported. Unset `PP_TEST_INDEX_FAIL_MARKER` and `PP_TEST_MODE` to disable the test-only failure seam.

- [x] **Step 4: Prove idempotence and stale-worker safety**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_synthesis_maintenance.py tests/test_lancedb_store.py tests/test_release_resilience_outbox.py -q --no-cov
.venv\Scripts\python.exe -m ruff check plastic_promise/core/traceability.py plastic_promise/core/synthesis_maintenance.py
```

Expected: all pass, including existing lease-loss and synthesis-index tests.

- [x] **Step 5: Commit the derived-index contract**

```powershell
git add plastic_promise/core/traceability.py plastic_promise/core/synthesis_maintenance.py plastic_promise/core/lancedb_store.py tests/test_synthesis_maintenance.py tests/test_lancedb_store.py tests/test_release_resilience_outbox.py
git commit -m "feat: add checked ordinary index delete replay"
```

---

### Task 4: Transactional Source Mutation and Immediate Dependent Invalidation

**Files:**
- Create: `plastic_promise/core/ordinary_memory_mutation.py`
- Modify: `plastic_promise/core/synthesis.py:399-438, 800-848, 1259-1359`
- Modify: `plastic_promise/core/synthesis_retrieval.py:37-54, 839-900`
- Modify: `plastic_promise/core/context_engine.py`
- Modify: `plastic_promise/memory/pipeline.py:30-40, 250-365, 453-506`
- Test: `tests/test_ordinary_memory_mutation.py`
- Test: `tests/test_synthesis_store.py`
- Test: `tests/test_synthesis_maintenance.py`
- Test: `tests/test_synthesis_retrieval_gate.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class OrdinaryMutationResult:
    memory_id: str
    operation: str
    previous_content_hash: str
    current_content_hash: str
    committed_memory_version: int
    ordinary_index_job_id: str
    stale_synthesis_ids: tuple[str, ...]
    synthesis_index_job_ids: tuple[str, ...]

def MemoryPipeline.prepare_correction(
    self, current: Mapping[str, Any], new_content: str
) -> PreparedMemory: ...

def SynthesisStore.stale_verified_dependents(
    self, source_id: str, *, reason: str, actor: str, call_id: str
) -> tuple[tuple[str, int, str], ...]: ...

class OrdinaryMemoryMutationCoordinator:
    def __init__(self, engine: Any): ...
    def replace_content(self, memory_id: str, *, content: str, reason: str,
                        actor: str, call_id: str) -> OrdinaryMutationResult: ...
    def mark_unavailable(self, memory_id: str, *, state: Literal["wrong", "deprecated", "forgotten"],
                         reason: str, actor: str, call_id: str) -> OrdinaryMutationResult: ...

def ContextEngine.mutate_ordinary_source(
    self, memory_id: str, *, operation: Literal["replace_content", "wrong", "deprecated", "forgotten"],
    content: str | None = None, reason: str, actor: str, call_id: str,
) -> OrdinaryMutationResult: ...
```

`ContextEngine.mutate_ordinary_source()` is the sole production facade for existing-source content/availability changes; it owns runtime context normalization, delegates to the coordinator, and returns `OrdinaryMutationResult`. `prepare_correction()` has no persistence side effect and preserves the row's policy/model while rebuilding raw/L0/L1/L2, embedding/search text, hash, vector, quality result, and `memory_index` metadata. The coordinator opens one `BEGIN IMMEDIATE`/savepoint under the engine write lock, verifies the ordinary row, applies its field patch, records correction lineage, stales verified dependents, increments global memory version exactly once, then enqueues ordinary and synthesis jobs bound to that version/revision before commit. The bulk dependent helper neither commits nor bumps independently when the caller owns the transaction. The coordinator updates the in-memory cache only from the committed row, then opportunistically replays the newly created jobs.

- [x] **Step 1: Add RED transaction and lifecycle tests**

```python
def test_corrected_content_commits_material_lineage_jobs_and_stales_dependents(engine, verified_graph):
    before = canonical_row(engine, verified_graph.source_id)
    result = coordinator(engine).replace_content(
        before["id"], content="materially corrected evidence", reason="user correction",
        actor="codex", call_id="call-correct-1",
    )
    after = canonical_row(engine, before["id"])
    assert after["content"] == "materially corrected evidence"
    assert after["embedding_hash"] != before["embedding_hash"]
    assert json.loads(after["metadata_json"])["quality"]["status"] == "current"
    assert verified_graph.artifact_id in result.stale_synthesis_ids
    assert synthesis_status(engine, verified_graph.artifact_id) == ("stale", "source_changed")
    assert_pending_bound_jobs(engine, result)

```

Add four independent tests with exact outcomes:

- `test_unavailable_state_is_a_persistent_tombstone_and_stales_dependents`, parameterized over `(wrong, source_wrong)`, `(deprecated, source_deprecated)`, and `(forgotten, source_forgotten)`: the source row remains auditable but unavailable, every verified dependent is stale, and checked ordinary/synthesis deletes are pending in the same commit.
- `test_lineage_failure_rolls_back_source_dependent_and_outbox`: monkeypatch `record_memory_lineage` to raise and assert byte-identical source/control/edge/outbox tables.
- `test_outbox_failure_rolls_back_source_dependent_and_lineage`: monkeypatch V3 enqueue to raise and assert the same all-or-nothing rollback.
- `test_scanner_is_defense_in_depth_not_required_for_immediate_block`: make the scanner raise if called, perform the mutation, and prove public recall already excludes source/dependent before any scan.

- [x] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py tests/test_synthesis_store.py tests/test_synthesis_maintenance.py -q --no-cov
```

Expected: coordinator/import failures and existing source correction leaves verified dependents unchanged until a scanner runs.

- [x] **Step 3: Implement preparation, same-transaction invalidation, lineage, and jobs**

Use stable reasons exactly `source_changed`, `source_wrong`, `source_deprecated`, and `source_forgotten`. `corrected` is an operation that requires non-empty changed content and stores quality `current`; it is never a blocked persisted state. `wrong`/`deprecated` are auditable rows, not hard deletes. Do not call public getters while the transaction is open.

- [x] **Step 4: Verify rollback, normal-recall exclusion, and scanner compatibility**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py tests/test_synthesis_store.py tests/test_synthesis_maintenance.py tests/test_synthesis_retrieval_gate.py -q --no-cov
.venv\Scripts\python.exe -m ruff check plastic_promise/core/ordinary_memory_mutation.py plastic_promise/core/synthesis.py plastic_promise/memory/pipeline.py
```

Expected: all pass. Query SQLite after the handler returns to prove invalidation preceded scanner/restart.

Task 4 closure evidence (2026-07-12): focused mutation tests passed `100`; the
fresh seven-file transaction/retrieval/route compatibility shard passed `523`;
Ruff and `git diff --check` passed. Independent review found and then verified
the fixes for transaction ownership, correction CAS breadth, recursive
dependent invalidation, and explicit quality-decision admission; the final
re-review had zero blockers.

High-risk checklist:

| # | Check | Result | Evidence |
| --- | --- | --- | --- |
| 1 | Design principles | PASS | One coordinator owns the source transition; SQLite remains canonical and LanceDB remains derived. |
| 2 | Trust-score impact | PASS | No trust or defense implementation changed. |
| 3 | Test coverage | PASS | Rollback, cross-connection CAS, transaction ownership, transitive stale state, checked jobs, getters, and final supply gates are exercised. |
| 4 | Breaking change | PASS | No MCP route changes in Task 4; stricter rejection is limited to tombstones and malformed explicit state. |
| 5 | Dependency change | PASS | No dependency or lockfile changes. |
| 6 | Architecture impact | PASS | The planned engine/pipeline/synthesis/retrieval boundaries are preserved. |
| 7 | Security | PASS | SQL values remain parameterized, field identifiers are code-owned, and actor/call evidence is required. |
| 8 | Cross-module impact | PASS | The same committed version binds the source patch, lineage, recursive stale state, and both outbox job classes. |
| 9 | API compatibility | PASS | Existing public read shapes remain stable and legal pipeline/review quality states remain admitted. |
| 10 | Rollback and docs | PASS | Failure injection proves all-or-nothing rollback; durable jobs survive replay failure; exemplar research documents the recovery boundary. |

Audit decision: **PASS** with zero Task 4 blockers. MCP `audit_run(full)` scored
`0.6974`, above the required `0.60`. It separately reported a system-wide
`memory_supply=0.0` finding because the scorer includes intentionally
unavailable high-failure history; this is recorded as an out-of-scope platform
health follow-up, not hidden or relabeled. `sp-stage` audit-review tracking was
attempted through both aliases and the audit stage, but each compound call
exceeded the MCP 120-second limit; the independent full audit call completed.

- [x] **Step 5: Commit the canonical coordinator**

```powershell
git add plastic_promise/core/ordinary_memory_mutation.py plastic_promise/core/synthesis.py plastic_promise/core/synthesis_retrieval.py plastic_promise/core/context_engine.py plastic_promise/memory/pipeline.py tests/test_ordinary_memory_mutation.py tests/test_synthesis_store.py tests/test_synthesis_maintenance.py tests/test_synthesis_retrieval_gate.py
git commit -m "feat: invalidate synthesis in source mutation transactions"
```

---

### Task 5: Route Every Content/Availability Mutation Through the Coordinator

**Files:**
- Modify: `plastic_promise/mcp/server.py:1810-1925, 1963-2076`
- Modify: `plastic_promise/mcp/tools/memory.py:1503-1689, 1977-2076`
- Modify: `plastic_promise/mcp/tools/reflection.py`
- Modify: `plastic_promise/mcp/tools/skill_tracking.py:890-1000`
- Modify: `plastic_promise/memory/soul_memory.py:830-900, 1030-1095, 1140+`
- Modify: `plastic_promise/cron/scan_memory_decay.py:43+`
- Modify: `daemons/maintenance_daemon.py:699-738`
- Modify: `plastic_promise/core/context_engine.py:1553-1799`
- Modify: `plastic_promise/core/ordinary_memory_mutation.py`
- Modify: `plastic_promise/core/tool_manifest.py`
- Modify: `plastic_promise/skills/memory_operations.py`
- Test: `tests/test_memory_operations.py`
- Test: `tests/test_synthesis_mcp_routing.py`
- Test: `tests/test_skill_tracking.py`
- Test: `tests/test_synthesis_store.py`
- Test: `tests/test_scanners.py`
- Test: `tests/test_memory_merge.py`
- Test: `tests/test_ordinary_memory_mutation.py`
- Create: `tests/test_server_notify.py`
- Test: `tests/test_tool_manifest_graph.py`

**Interfaces:**

```python
def _mutation_runtime_context(tool_name: str) -> dict[str, Any]: ...

async def handle_memory_update(
    engine: Any, args: dict, *, _runtime_context: dict[str, Any] | None = None
) -> list[TextContent]: ...
async def handle_memory_forget(
    engine: Any, args: dict, *, _runtime_context: dict[str, Any] | None = None
) -> list[TextContent]: ...
async def handle_memory_correct(
    engine: Any, args: dict, *, _runtime_context: dict[str, Any] | None = None
) -> list[TextContent]: ...
```

The server creates actor/call/project/trust evidence for `memory_update`, `memory_forget`, `memory_correct`, `feedback_apply`, and both public `smart-remember` aliases; caller-declared actor/call fields remain audit-only. Internal `update_memory_fields(content=...)`, skill-session completion, RecMem update/forget, lifecycle scans, and maintenance duplicate-cluster cleanup call `ContextEngine.mutate_ordinary_source()` with generated internal call evidence. Lifecycle scanners and duplicate cleanup may discover candidates in SQL but may not directly write forgotten/replaced tags or delete ordinary rows. Audit report rollover uses the internal `audit_rollover` capability at the initial `0.60` trust level without weakening public `memory_forget` (`0.80`, critical).

- [x] **Step 1: Add RED public-route and bypass tests**

```python
async def test_memory_correct_rejects_ambiguous_operations_without_mutation(engine):
    before = canonical_row(engine, "source-1")
    for args in (
        {"memory_id": "source-1", "mark_as": "corrected"},
        {"memory_id": "source-1", "mark_as": "wrong", "content": "replacement"},
        {"memory_id": "source-1", "mark_as": "corrected", "content": before["content"]},
    ):
        payload = json_result(await handle_memory_correct(engine, args, _runtime_context=TRUSTED))
        assert payload["corrected"] is False
        assert canonical_row(engine, "source-1") == before

```

Add five separate route/bypass tests:

- `test_public_memory_correct_stales_dependent_before_response`: response contains the dependent ID and SQLite status is stale before the await returns.
- `test_memory_update_content_invalidates_but_importance_only_does_not`: content creates lineage/jobs/stale state; importance-only changes one column and creates none.
- `test_memory_forget_persists_tombstone_and_checked_delete`: the row remains in SQLite, public getters reject it, and a durable delete job exists without a direct `ldb.delete` call.
- `test_skill_content_update_cannot_bypass_source_invalidation`: deterministic skill content change routes through the coordinator and stales a seeded verified dependent.
- `test_lifecycle_scan_uses_coordinator_instead_of_direct_sql`: spy on `mutate_ordinary_source`, assert one call per discovered forgotten/replaced candidate, and reject any direct update to retrieval-visible columns.
- `test_duplicate_cluster_cleanup_tombstones_through_coordinator_without_raw_delete`: seed two duplicate ordinary sources sharing a verified dependent; run the duplicate-cleanup cycle with the lower-worth source selected; assert a `forgotten` tombstone remains, its dependent is already `stale` before scanner/restart, checked ordinary/synthesis jobs are durable in the same SQLite transaction, and an SQL trace spy observes no `DELETE FROM memories` or direct `UPDATE memories SET` from the daemon.

- [x] **Step 2: Run RED against all known bypasses**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_memory_operations.py tests/test_synthesis_mcp_routing.py tests/test_skill_tracking.py tests/test_scanners.py tests/test_safety_net_daemon.py -q --no-cov
```

Expected: ambiguous correction currently mutates worth/deletes rows, skill/scanner content changes bypass immediate invalidation, and duplicate-cluster cleanup directly updates then deletes the canonical row.

- [x] **Step 3: Install server-owned evidence and coordinator routing**

Make public results explicit: committed canonical operation, pending/completed job IDs, stale dependents, and stable failure reason. A post-commit checked-index failure returns `committed=true` plus pending job evidence rather than misreporting canonical rollback. Remove `memory_forget`'s direct LanceDB delete and record fallback. Replace duplicate-cluster cleanup's raw tag update plus raw `DELETE FROM memories` with a bounded call to `ContextEngine.mutate_ordinary_source(operation="forgotten", reason="safety-net:duplicate_cluster", actor="maintenance_daemon", call_id=<cycle child span>)`; it preserves the tombstone, lineage, transactional stale dependents, and checked jobs. Preserve importance/category-only behavior through Task 1's narrow patch. If preparation fails, perform no SQLite mutation.

- [x] **Step 4: Search for and close direct content/unavailability writes**

```powershell
rg -n "store_memory\(|update_memory_fields\([^\n]*content" plastic_promise daemons scripts -g "*.py"
rg -n -i "\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+memories\b" plastic_promise daemons scripts -g "*.py"
.venv\Scripts\python.exe -m pytest tests/test_memory_operations.py tests/test_synthesis_mcp_routing.py tests/test_skill_tracking.py tests/test_synthesis_store.py tests/test_scanners.py tests/test_safety_net_daemon.py -q --no-cov
```

Expected: each search result is either creation, a test, a coordinator implementation, or a reviewed metadata-only write. Add its classification to the caller inventory table in Task 6 before continuing.

Implementation and verification evidence (2026-07-12):

- Public update/correct/forget, feedback, skill completion, RecMem, lifecycle scanners,
  duplicate cleanup, GC merge, audit rollover, LLM classification, and smart duplicate
  replacement now route through canonical field patches or the source coordinator. Exact
  duplicates are zero-write; similar smart duplicates require server-owned
  `memory_update` authority before canonical read or mutation.
- GC candidate discovery rejects empty or different projects. The coordinator separately
  checks declared source/peer project equality before the transaction and canonical
  source/peer equality inside it. A forged same-project peer declaration is rejected after
  transaction start with source, peer, lineage, version, outbox, and cache unchanged.
- The stable 17-file high-risk matrix passed `688 passed`; an independent read-only reviewer
  returned **PASS** with zero blocking findings and independently ran `95 passed` focused
  regressions. Focused smart authority tests passed `22 passed`; notify/manifest tests passed
  `30 passed` before the final matrix.
- The AST caller inventory passed `5 passed` and remains exactly 51 normalized tuples / 57
  occurrences. Operational daemon/cron/MCP/memory/skill modules contain no
  `DELETE FROM memories`; remaining raw SQL matches the reviewed inventory.
- Key changed implementation and regression files pass Ruff, all changed production modules
  pass `py_compile`, and `git diff --check` is clean apart from existing LF/CRLF notices.
  A whole-file Ruff run still reports 40 pre-existing daemon/bootstrap and old inline-import
  findings; zero-context diff inspection confirms the new Task 5 lines did not introduce
  those findings.
- The same local `SoulAuditor.run_audit()` implementation used by MCP `audit_run` scored
  `0.6987`, above the required `0.60`. Both MCP full and quick transports timed out at 120s,
  so the local implementation was used as the explicit transport fallback. The report also
  retained the system-wide `memory_supply=0.02` finding; this is a shared-pool health follow-up,
  not hidden or relabeled as a Task 5 code finding.

High-risk 10-item audit: **PASS**, zero blockers. Core conventions and trust boundaries are
preserved; tests cover every new behavior and rollback boundary; the release raises the
declared LanceDB floor from `0.6.0` to `0.34.0`, so compatibility impact is documented and
covered by the governed retrieval matrix; architecture changes centralize writes behind
existing canonical owners; localhost
notification authority remains server-owned; cross-module consumers and both aliases are
covered; public schemas remain backward compatible; rollback is branch-level revert plus
durable outbox replay; this plan and the caller inventory document the final data flow. The
residual non-blocking risk is the absence of a real HTTP transport integration test; handler,
route mapping, server-owned authority, daemon retry, and persistence ordering are covered
directly.

The audit report was stored through the local `memory_store` handler fallback as
`fuzzy_15cb868f7f99`. The required high-risk PASS trust delta was recorded for `codex` as
`0.8835 -> 0.9035` (`+0.02`, tier `high`).

- [x] **Step 5: Commit public and internal mutation routing**

```powershell
git add plastic_promise/mcp/server.py plastic_promise/mcp/tools/memory.py plastic_promise/mcp/tools/reflection.py plastic_promise/mcp/tools/skill_tracking.py plastic_promise/memory/soul_memory.py plastic_promise/cron/scan_memory_decay.py daemons/maintenance_daemon.py plastic_promise/core/context_engine.py tests/test_memory_operations.py tests/test_synthesis_mcp_routing.py tests/test_skill_tracking.py tests/test_synthesis_store.py tests/test_scanners.py tests/test_safety_net_daemon.py
git commit -m "fix: route source mutations through canonical invalidation"
```

---

### Task 6: Complete Caller Migration and Enforce Creation-Only Whole-Record Storage

**Files:**
- Modify: `plastic_promise/core/context_engine.py:898-967, 1440-1512, 5507-5641`
- Modify: `plastic_promise/memory/soul_memory.py:605-710`
- Modify: `plastic_promise/core/pack_index.py:125-172`
- Modify: `plastic_promise/pack.py`
- Modify: `plastic_promise/mcp/tools/management.py`
- Modify: creation callers returned by the inventory search
- Create: `docs/engineering-patterns/2026-07-12-ordinary-memory-caller-inventory.md`
- Test: `tests/test_ordinary_memory_mutation.py`
- Test: `tests/test_memory_proposals.py`
- Test: `tests/test_pipeline_quality.py`
- Test: `tests/test_skill_tracking.py`
- Create: `tests/test_pack_index.py`

**Interfaces:**

```python
def _SQLiteStorage.create_ordinary_if_absent(
    self, mid: str, data: Mapping[str, Any]
) -> tuple[dict[str, Any], bool]: ...

def ContextEngine.create_ordinary_if_absent(
    self, record: Mapping[str, Any] | MemoryRecord
) -> str: ...
```

The SQLite operation is `INSERT ... ON CONFLICT DO NOTHING`, then reloads the existing canonical row and compares a canonical ordinary binding over every persisted field. Identical replay returns `(row, False)`; a mismatch raises `OrdinaryMemoryConflict("ordinary_memory_already_exists")`. After all callers are migrated, `register_memory()` and `store_memory()` delegate to this operation and therefore cannot replace existing rows.

- [x] **Step 1: Re-run Task 0's inventory and update every migration status**

The document must classify every production call returned by:

```powershell
rg -n "\.store_memory\(|\.register_memory\(|_persist_ordinary_memory\(|\.upsert_ordinary\(" plastic_promise daemons scripts -g "*.py"
rg -n "\.update_memory\(|update_memory_fields\(|increment_field\(|batch_update\(|delete_memory\(" plastic_promise daemons scripts -g "*.py"
rg -n -i "\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+memories\b" plastic_promise daemons scripts -g "*.py"
```

Update the existing columns for exact path/symbol, caller, existing-ID possibility, operation owner, migration API, focused test, and status. Explicitly retain RecMem caller-supplied IDs, pack `replace`, skill-session deterministic IDs, proposal promotion's transaction-local insert, pipeline creation, bootstrap/import paths, and daemon/cron paths. Classify maintenance duplicate cleanup as migrated to `ContextEngine.mutate_ordinary_source(..., operation="forgotten")`; no direct daemon `DELETE FROM memories` may remain. The AST/raw-SQL inventory must report zero unclassified and zero unmigrated replacement or availability writers before Step 4 enables rejection.

- [x] **Step 2: Add RED create/replay/rejection and pack compatibility tests**

```python
def test_create_ordinary_if_absent_is_idempotent_only_for_identical_binding(engine, rich_row):
    assert engine.create_ordinary_if_absent(rich_row) == rich_row["id"]
    assert engine.create_ordinary_if_absent(dict(rich_row)) == rich_row["id"]
    conflicting = {**rich_row, "content": "different"}
    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_memory_already_exists"):
        engine.create_ordinary_if_absent(conflicting)

```

Add four separate compatibility tests:

- `test_store_and_register_reject_existing_id_without_changing_row`: both public creation APIs raise the stable conflict and leave the full canonical row identical.
- `test_pack_replace_uses_source_mutation_and_preserves_unowned_fields`: replace changes only declared pack content/tags/domain, records invalidation if needed, and preserves project/provenance/index fields not owned by the import.
- `test_skill_session_start_replay_is_idempotent`: identical deterministic start returns the same ID without version/content drift; conflicting replay does not replace the row.
- `test_proposal_promotion_insert_contract_remains_atomic`: promotion still inserts only on dedup miss and rolls back proposal/memory/outbox together on failure.

- [x] **Step 3: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py tests/test_memory_proposals.py tests/test_pipeline_quality.py tests/test_skill_tracking.py tests/test_pack_index.py -q --no-cov
```

Expected: existing IDs are replaced by `_upsert_ordinary`, or `create_ordinary_if_absent` is absent.

- [x] **Step 4: Migrate classified callers, then enable rejection in a separate code change**

Pack `skip` remains unchanged. Pack `merge` uses a tag/domain patch. Pack `replace` uses the source coordinator for content plus declared pack-owned metadata; it never passes a partial record to a create API. RecMem creation and deterministic skill-session startup use create-if-absent. Do not alter proposal promotion's already-safe transaction-local insert.

- [x] **Step 5: Run the inventory gate and compatibility suite**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py tests/test_memory_proposals.py tests/test_pipeline_quality.py tests/test_skill_tracking.py tests/test_pack_index.py tests/test_memory_sync.py tests/test_skills_phase1_e2e.py -q --no-cov
rg -n "\.store_memory\(|\.register_memory\(" plastic_promise daemons scripts -g "*.py"
```

Expected: every remaining production call is marked creation-only in the committed inventory and has an exact test. No unchecked existing-ID replacement remains.

- [ ] **Step 6: Commit creation-only enforcement separately**

```powershell
git add docs/engineering-patterns/2026-07-12-ordinary-memory-caller-inventory.md plastic_promise/core/context_engine.py plastic_promise/memory/soul_memory.py plastic_promise/core/pack_index.py plastic_promise/pack.py plastic_promise/mcp/tools/management.py plastic_promise/mcp/tools/skill_tracking.py plastic_promise/memory/pipeline.py tests/test_ordinary_memory_callers.py tests/test_ordinary_memory_mutation.py tests/test_memory_proposals.py tests/test_pipeline_quality.py tests/test_skill_tracking.py tests/test_pack_index.py tests/test_memory_sync.py tests/test_skills_phase1_e2e.py
git commit -m "refactor: make whole-record ordinary storage creation only"
```

---

### Task 7: Independent Monotonic Deadline Registry and Durable Cycle Evidence

**Files:**
- Create: `plastic_promise/core/maintenance_scheduler.py`
- Modify: `daemons/maintenance_daemon.py:88-118, 1329-1590`
- Modify: `plastic_promise/core/traceability.py:425-515`
- Modify: `plastic_promise/launcher/service_manager.py:198-235`
- Modify: `scripts/init_and_start.py:44-63`
- Create: `tests/test_maintenance_scheduler.py`
- Modify: `tests/test_safety_net_daemon.py`
- Modify: `tests/test_launcher.py`

**Interfaces:**

```python
@dataclass
class MaintenanceDeadline:
    name: str
    interval: AdaptiveThrottle
    next_deadline: float
    runner: Callable[[], Awaitable[Any]]
    last_outcome: Mapping[str, Any] | None = None

class MaintenanceRegistry:
    def __init__(self, jobs: Sequence[MaintenanceDeadline]): ...
    async def run_due(self, now: float) -> tuple[Mapping[str, Any], ...]: ...
    def next_delay(self, now: float, *, maximum: float = 10.0) -> float: ...

async def run_governed_maintenance_cycle(
    engine: Any = None, *, outer_parent_call_id: str | None = None
) -> dict[str, Any]: ...

def TraceabilityStore.get_cycle_span_tree(cycle_call_id: str) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]: ...
```

Each deadline advances by whole current intervals until strictly greater than the captured monotonic `now`; a large jump runs a job once, not once per missed period. The stable registry eagerly constructs all throttles with the real `AdaptiveThrottle(base_seconds)` signature. Ordinary/synthesis index replay is immediately due at process startup. `run_governed_maintenance_cycle()` creates a new `cycle_call_id` for its root `maintenance_cycle` span and returns it as `result["cycle_call_id"]`; its root's `parent_call_id` is the optional `outer_parent_call_id`, never itself. Each of the six stage spans uses `parent_call_id=cycle_call_id`. `get_cycle_span_tree()` reads the root by `call_id`, reads children by `parent_call_id`, sorts children by `metadata.order`, and rejects self-parenting, any non-six child count, a child whose `parent_call_id` differs from the root ID, or duplicate/noncontiguous order. Wall clock appears only in heartbeat and persisted trace evidence.

- [x] **Step 1: Add RED reachability and large-jump tests with an injected clock**

```python
async def test_independent_deadlines_cannot_reset_or_starve_one_another():
    calls = []
    registry = registry_for_test(now=0.0, calls=calls)
    await registry.run_due(300.0)
    await registry.run_due(600.0)
    await registry.run_due(3600.0)
    assert_required_jobs_reached(calls, {
        "audit", "governed_maintenance", "safety_net", "heartbeat",
        "scheduler_health", "scan_data_quality",
    })

```

Also add:

- `test_large_clock_jump_runs_each_due_job_at_most_once_and_advances_future`: jump from 0 to 86,400 seconds; every due runner count is one and each deadline is greater than 86,400.
- `test_registry_eagerly_constructs_scheduler_health_throttle`: the stable registry contains scheduler health at construction and every `AdaptiveThrottle` was called with exactly one integer base interval.

- [x] **Step 2: Add RED parent/ordered-child span and health tests**

```python
async def test_governed_cycle_persists_parent_and_ordered_children_after_reopen(
    tmp_path: Path,
) -> None:
    trace_db = tmp_path / "traceability.sqlite"
    engine = governed_engine_for_test(trace_db)
    result = await run_governed_maintenance_cycle(engine, outer_parent_call_id="daemon-run-42")
    assert result["status"] == "success"
    assert result["cycle_call_id"] != "daemon-run-42"

    reopened = TraceabilityStore(trace_db)
    root, children = reopened.get_cycle_span_tree(result["cycle_call_id"])
    assert root["call_id"] == result["cycle_call_id"]
    assert root["stage"] == "maintenance_cycle"
    assert root["parent_call_id"] == "daemon-run-42"
    assert [span["stage"] for span in children] == [
        "memory_lifecycle",
        "proposal_expiry",
        "synthesis_integrity",
        "memory_index_replay",
        "synthesis_index_replay",
        "audit",
    ]
    assert root["status"] == "success"
    assert [span["metadata"]["order"] for span in children] == list(range(1, 7))
    assert all(span["parent_call_id"] == result["cycle_call_id"] for span in children)

async def test_governed_cycle_marks_parent_partial_and_continues_after_middle_failure(
    tmp_path: Path,
) -> None:
    engine = governed_engine_for_test(
        tmp_path / "traceability.sqlite",
        failing_stage="synthesis_integrity",
    )
    result = await run_governed_maintenance_cycle(engine, outer_parent_call_id="daemon-run-43")
    root, children = TraceabilityStore(engine.trace_db).get_cycle_span_tree(result["cycle_call_id"])
    assert result["status"] == "partial"
    assert root["status"] == "partial"
    assert child_span(children, "synthesis_integrity")["status"] == "error"
    assert [child_span(children, stage)["status"] for stage in (
        "memory_index_replay", "synthesis_index_replay", "audit",
    )] == ["success", "success", "success"]

def test_health_rejects_fresh_heartbeat_when_reported_pid_is_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_heartbeat(
        tmp_path / "maintenance-heartbeat.json",
        schema="maintenance-heartbeat/v1",
        pid=424242,
        updated_at=utc_now(),
        startup_replay_cycle_id="startup-cycle",
    )
    monkeypatch.setattr(service_manager, "pid_is_alive", lambda pid: False)
    health = read_maintenance_health(tmp_path / "maintenance-heartbeat.json")
    assert health["healthy"] is False
    assert health["reason"] == "maintenance_pid_not_alive"
```

Put the first two tests in `tests/test_maintenance_scheduler.py` and the PID-first health test in `tests/test_launcher.py`. Their fixtures must use a real temporary SQLite trace store, not an in-memory mock, so reopening proves persistence. The root and its six children are queried through the explicit root-plus-children tree API, rather than a children-only query that can ambiguously include the root. Add `test_cycle_span_tree_rejects_self_parent_or_wrong_child_linkage` by inserting a self-parented root and a child linked to another cycle; both must raise `invalid_maintenance_cycle_span_tree`. Later failures must never suppress later stages.

- [x] **Step 3: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_maintenance_scheduler.py tests/test_safety_net_daemon.py tests/test_launcher.py -q --no-cov
```

Expected: current shared `tick` starves safety-net work, the lazy scheduler throttle raises a signature error, cycle spans are absent, and heartbeat freshness can mask a dead PID.

- [x] **Step 4: Implement the registry, stage evidence, and PID-first health**

Persist one root `call_spans` row with a fresh `call_id=cycle_call_id` and ordered child rows for `memory_lifecycle`, `proposal_expiry`, `synthesis_integrity`, `memory_index_replay`, `synthesis_index_replay`, and `audit`, each with `parent_call_id=cycle_call_id`. An optional daemon/request parent is stored only on the root's `parent_call_id`. Root status is `success`, `partial`, or `error`; metadata includes order, counts, and error classes. Persist and consume the root-plus-children tree via the strict query contract above. Heartbeat schema is `maintenance-heartbeat/v1` with PID, UTC update, and completed startup replay cycle ID. Check PID liveness before heartbeat age.

- [x] **Step 5: Verify scheduling without sleeps**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_maintenance_scheduler.py tests/test_safety_net_daemon.py tests/test_launcher.py -q --no-cov
.venv\Scripts\python.exe -m ruff check plastic_promise/core/maintenance_scheduler.py daemons/maintenance_daemon.py plastic_promise/launcher/service_manager.py
```

- [ ] **Step 6: Commit scheduler correctness**

```powershell
git add plastic_promise/core/maintenance_scheduler.py daemons/maintenance_daemon.py plastic_promise/core/traceability.py plastic_promise/launcher/service_manager.py scripts/init_and_start.py tests/test_maintenance_scheduler.py tests/test_safety_net_daemon.py tests/test_launcher.py
git commit -m "fix: schedule maintenance with independent deadlines"
```

---

### Task 8: One-Shot Daemon and Real Cross-Process Recovery Smoke

**Files:**
- Create: `scripts/http_mcp_harness.py`
- Create: `scripts/smoke_restart_recovery.py`
- Modify: `daemons/maintenance_daemon.py:1388+`
- Modify: `plastic_promise/core/synthesis_maintenance.py`
- Create: `tests/test_smoke_restart_recovery.py`
- Modify: `tests/test_smoke_http_mcp.py`
- Modify: `tests/test_launcher.py`

**Interfaces:**

```python
async def daemon_main(argv: Sequence[str] | None = None) -> int: ...
# CLI: python daemons/maintenance_daemon.py [--mcp-url URL] [--once] [--json]

@dataclass
class ManagedProcess:
    process: subprocess.Popen[str]
    command: tuple[str, ...]
    stdout_path: Path
    stderr_path: Path

async def run_recovery_smoke(args: argparse.Namespace) -> dict[str, Any]: ...
# CLI: python scripts/smoke_restart_recovery.py --artifact-dir DIR --json
```

One-shot initializes the same engine/registry, skips warm-up and infinite loop, executes one governed cycle, emits one JSON object, closes SQLite/LanceDB/MCP resources, and exits 0 only when required replay stages do not fail.

- [x] **Step 1: Write RED parser, process, artifact, and assertion tests**

```python
def test_daemon_once_parser_requires_supported_mcp_url_and_json_contract() -> None:
    with pytest.raises(SystemExit):
        parse_daemon_args(["--once", "--mcp-url", "not-a-url", "--json"])
    result = validate_daemon_once_arguments(
        {"once": True, "mcp_url": "http://127.0.0.1:9020/mcp", "json": True}
    )
    assert result == {"ok": True}
    assert validate_daemon_once_arguments({"once": True, "json": False})["error"] == (
        "daemon_once_arguments_invalid"
    )

@pytest.mark.asyncio
async def test_daemon_once_reuses_registry_once_and_skips_warmup_and_forever_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    registry = recording_registry(calls)
    monkeypatch.setattr(maintenance_daemon, "build_maintenance_registry", lambda *args: registry)
    monkeypatch.setattr(maintenance_daemon, "run_warmup", lambda *args: calls.append("warmup"))
    monkeypatch.setattr(maintenance_daemon, "run_forever", lambda *args: calls.append("forever"))
    exit_code = await daemon_main(["--once", "--mcp-url", "http://127.0.0.1:9020/mcp", "--json"])
    assert exit_code == 0
    assert calls == ["registry.run_due"]
    assert registry.run_due_count == 1

def test_recovery_smoke_validator_rejects_missing_or_mismatched_pid_evidence() -> None:
    artifact = complete_recovery_smoke_v1()
    artifact["processes"]["daemon_once"]["pid"] = artifact["processes"]["mcp_restart"]["pid"]
    assert validate_recovery_smoke(artifact)["error"] == "recovery_pid_evidence_invalid"

def test_recovery_smoke_validator_rejects_missing_checked_outbox_transition() -> None:
    artifact = complete_recovery_smoke_v1()
    artifact["outbox"]["ordinary"]["before"] = artifact["outbox"]["ordinary"]["after"]
    assert validate_recovery_smoke(artifact)["error"] == "recovery_outbox_transition_missing"

def test_recovery_smoke_validator_rejects_missing_current_revision_only_results() -> None:
    artifact = complete_recovery_smoke_v1()
    artifact["final_public_results"]["memory_recall"]["memory_ids"].append("revision-1")
    assert validate_recovery_smoke(artifact)["error"] == "recovery_current_revision_missing"
```

Put the CLI parser/registry reuse tests in `tests/test_launcher.py`, and the three artifact validators in `tests/test_smoke_restart_recovery.py`. `complete_recovery_smoke_v1()` must include distinct old/restart/one-shot PIDs, PID-death observations, pending-to-done ordinary and synthesis outbox transitions, revision-1/revision-2 material hashes, and public `memory_recall` plus `context_supply` current-only results before each mutation. The four validator categories mutate one condition at a time and assert these stable errors: `daemon_once_arguments_invalid`, `recovery_pid_evidence_invalid`, `recovery_outbox_transition_missing`, and `recovery_current_revision_missing`.

Unit tests may fake processes only for validators and cleanup. They do not satisfy acceptance.

- [x] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_smoke_restart_recovery.py tests/test_smoke_http_mcp.py tests/test_launcher.py -q --no-cov
```

Expected: missing script/parser/validator APIs.

- [x] **Step 3: Implement the exact `recovery-smoke/v1` sequence**

Use a free port and isolated absolute SQLite/LanceDB paths. Start MCP and long daemon with the design's environment allowlist and `max-v1`/Python route. Require `/health.pid == Popen.pid` and heartbeat PID/startup replay equality. Through public MCP calls create two sources and verified revision 1, create the exact failure marker after source ID discovery, call public `memory_correct`, and assert canonical commit + pending jobs + immediate synthesis blocking. Terminate and `wait()` both processes, prove PIDs dead and port closed, restart a distinct MCP PID, run distinct `--once` daemon PID, prove jobs done, refresh/verify revision 2, restart again, and prove only revision 2 appears in `memory_recall` and `context_supply`.

- [x] **Step 4: Emit complete reproducible evidence**

The artifact records schema, canonical paths, sanitized commands/environment keys, old/new PIDs, health/heartbeat snapshots, readiness/death checks, call/source/synthesis IDs, outbox rows before/after, revisions/material hashes, final public results, assertion table, log paths, and SHA-256 log hashes. Always terminate owned processes in `finally`.

- [x] **Step 5: Run unit tests, then the real subprocess proof**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_smoke_restart_recovery.py tests/test_smoke_http_mcp.py tests/test_launcher.py -q --no-cov
.venv\Scripts\python.exe scripts/smoke_restart_recovery.py --artifact-dir .artifacts/recovery-smoke-20260712 --json
```

Expected: command exits 0; artifact schema is `recovery-smoke/v1`; old PIDs are dead, restart PIDs differ, checked jobs move pending to done, and final public results contain only current material. Treat fallback, mock-only evidence, or an old LanceDB API as failure.

- [ ] **Step 6: Commit code/tests, not generated runtime evidence**

```powershell
git add scripts/http_mcp_harness.py scripts/smoke_restart_recovery.py daemons/maintenance_daemon.py plastic_promise/core/synthesis_maintenance.py tests/test_smoke_restart_recovery.py tests/test_smoke_http_mcp.py tests/test_launcher.py
git commit -m "test: prove checked index recovery across process restart"
```

---

### Task 9: Versioned Python Fusion Policy and Truthful Channel State

**Files:**
- Create: `plastic_promise/core/fusion_policy.py`
- Modify: `plastic_promise/core/retrieval_planner.py:42-61, 121-143`
- Modify: `plastic_promise/core/context_engine.py:79-90, 2010-2126, 3187-3610, 4636-4698`
- Create: `tests/test_fusion_policy.py`
- Modify: `tests/test_retrieval_planner.py`
- Modify: `tests/test_recall_pipeline_upgrade.py`
- Modify: `tests/test_rust_integration.py`

**Interfaces:**

```python
FUSION_CHANNEL_ORDER = ("vector", "bm25", "fts")

@dataclass(frozen=True)
class FusionConfig:
    k: int
    weights: Mapping[str, float]
    windows: Mapping[str, int]
    channels: tuple[str, ...]
    config_hash: str

@dataclass(frozen=True)
class FusionDecision:
    requested_policy: str
    effective_policy: str
    requested_runtime: str
    effective_runtime: str
    candidate_id: str
    capability_reason: str

def load_fusion_config(
    candidate_id: str, plan: RetrievalPlan, env: Mapping[str, str] = os.environ
) -> FusionConfig | None: ...
def resolve_cli_fusion_policy(
    policy: str, candidate_manifest: FrozenCandidateManifest | None
) -> str: ...
def weighted_rrf(
    rankings: Mapping[str, Sequence[tuple[str, float]]], config: FusionConfig
) -> list[tuple[str, float]]: ...
def canonical_fusion_config_hash(payload: Mapping[str, Any]) -> str: ...
```

`RetrievalPlan` gains ordered `fusion_channels: tuple[str, ...]` and `channel_windows: dict[str, int]`, derived only from planned vector/BM25/FTS channels in canonical order. Candidate runs read typed `PP_RETRIEVAL_RRF_K`, `PP_RETRIEVAL_RRF_WEIGHTS_JSON`, and `PP_RETRIEVAL_RRF_WINDOWS_JSON`; windows must exactly cover `fusion_channels` and remain within the planner's maximum candidate budget. `load_fusion_config()` accepts only `legacy-auto`, `max-v1`, or a fully hashed `wrrf-v1:<64-lowercase-hex>` candidate ID, recomputes its canonical hash, and rejects a mismatch. `resolve_cli_fusion_policy()` is the only bare-token adapter: `wrrf-v1` requires a parsed frozen manifest and returns `manifest.candidate_id`; an explicit hashed policy requires that same ID. The normalized immutable ID is written to both requested/effective policy fields and passed to MCP. `ContextPack` gains `channel_states: dict[str, dict[str, Any]]`. Every planned channel reports `planned`, `enabled`, `available`, `executed`, `participating`, `evidence_only`, and stable reason. Requested/effective policy and runtime are distinct audit fields.

- [x] **Step 1: Add RED policy validation, formula, tie, and planner tests**

```python
def test_weighted_rrf_uses_one_based_rank_and_id_tie_break():
    config = fusion_config(k=2, weights={"vector": 0.6, "bm25": 0.4}, windows={"vector": 3, "bm25": 3})
    result = weighted_rrf(
        {"vector": [("b", 99.0), ("a", 0.1)], "bm25": [("a", 500.0), ("b", 1.0)]},
        config,
    )
    assert result == sorted(result, key=lambda row: (-row[1], row[0]))
    assert dict(result)["a"] == pytest.approx(0.6 / 4 + 0.4 / 3)
    assert dict(result)["b"] == pytest.approx(0.6 / 3 + 0.4 / 4)

```

Add four independent tests:

- `test_wrrf_invalid_configuration_or_rankings_fail_closed`, parameterized over K zero, NaN/negative weight, missing/extra weight, duplicate ID, unknown channel, and over-limit window; assert the exact `FusionConfigurationError` reason.
- `test_bare_wrrf_cli_policy_requires_manifest_and_normalizes_before_mcp`: bare `wrrf-v1` without a manifest raises `fusion_candidate_manifest_required`; with a manifest it returns exactly its `wrrf-v1:<64hex>` ID; a different explicit hash raises `fusion_candidate_manifest_mismatch`. Spy on the process launch/query boundary and assert it sees only the normalized ID, never the bare token.
- `test_retrieval_plan_fusion_channels_excludes_graph_and_evidence_layers`: a mix plan exposes `("vector", "bm25", "fts")` only, while graph/code/audit/principle states are evidence-only.
- `test_legacy_auto_preserves_current_route_dependent_behavior`: force Python and Rust separately and compare results/audit labels to the pre-change compatibility fixtures.
- `test_max_v1_forces_full_channel_python_weighted_max`: even with healthy Rust, effective runtime is Python, effective policy is max-v1, and scores/order equal the existing `_hybrid_fuse` reference.

- [x] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_fusion_policy.py tests/test_retrieval_planner.py tests/test_recall_pipeline_upgrade.py -q --no-cov
```

Expected: module/fields are absent and Python only has implicit `_hybrid_fuse` behavior.

- [x] **Step 3: Implement explicit policy resolution without changing the default**

`legacy-auto` calls existing Python weighted-max or Rust K=60 logic unchanged. `max-v1` selects the existing full-channel Python algorithm explicitly. Only a normalized `wrrf-v1:<64hex>` canonicalizes each channel by `(-raw_score, memory_id)`, uses raw scores only for that within-channel order, validates one unique ID per bounded ranking, and sorts fused output exactly by `(-score, memory_id)`. Invalid candidate configuration or an unbound bare token fails before retrieval; an unselected candidate does not affect `legacy-auto`.

- [x] **Step 4: Verify policy metadata and graph separation**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_fusion_policy.py tests/test_retrieval_planner.py tests/test_recall_pipeline_upgrade.py tests/test_rust_integration.py -q --no-cov
.venv\Scripts\python.exe -m ruff check plastic_promise/core/fusion_policy.py plastic_promise/core/retrieval_planner.py plastic_promise/core/context_engine.py
```

- [ ] **Step 5: Commit Python fusion contracts**

```powershell
git add plastic_promise/core/fusion_policy.py plastic_promise/core/retrieval_planner.py plastic_promise/core/context_engine.py tests/test_fusion_policy.py tests/test_retrieval_planner.py tests/test_recall_pipeline_upgrade.py tests/test_rust_integration.py
git commit -m "feat: add explicit versioned fusion policies"
```

---

### Task 10: Rust WRRF Parity and Whole-Request Capability Routing

**Files:**
- Modify: `rust/context-engine-core/src/retrieval/fusion.rs:64-142`
- Modify: `rust/context-engine-core/src/context_engine.rs:157-194, 752-780, 1140-1180`
- Modify: `rust/context-engine-core/src/lib.rs`
- Modify: `plastic_promise/core/context_engine.py:2050-2126, 4080-4165`
- Create: `tests/fixtures/recall_quality/wrrf-v1-golden.json`
- Modify: `tests/test_fusion_policy.py`
- Modify: `tests/test_rust_python_parity.py`
- Modify: `tests/test_rust_integration.py`
- Modify: `rust/context-engine-core/tests/integration_test.rs`

**Interfaces:**

```rust
pub struct WrrfConfig {
    pub k: u32,
    pub channels: Vec<String>,
    pub weights: HashMap<String, f64>,
    pub windows: HashMap<String, usize>,
}

pub fn weighted_rrf_fuse(
    channel_results: &[(String, Vec<(String, f64)>)],
    config: &WrrfConfig,
) -> Result<Vec<(String, f64)>, String>;
```

Expose a small PyO3 testable wrapper for shared golden fixtures. `k` is a positive JSON integer in both implementations: Python rejects bool, float, non-finite, and values below one; Rust deserializes to `u32` and rejects fractional, negative, overflow, and zero values with the same stable `invalid_k:must_be_positive_integer` class before fusion. Convert `k` to `f64` only in the denominator. Pass the canonical configuration through an optional `fusion_config_json` argument on the Rust supply boundary. Rust may execute `wrrf-v1` only when `fusion_channels` is fully supported by its snapshot route (currently vector + BM25). `max-v1` and any WRRF plan containing FTS route the whole request to Python before Rust reads/ranks candidates. Graph/evidence-only channels do not force fallback.

- [x] **Step 1: Add the shared golden fixture and RED parity tests**

The fixture includes exact rankings/config/expected scores for: one-based rank, zero weight, missing item in one channel, input scores ignored, deterministic ID tie, duplicate ID rejection, missing/extra weight rejection, non-finite/negative values, invalid K (including `2.5`, `true`, `0`, and a `u32` overflow), and window truncation.

```python
@pytest.mark.parametrize("case", load_wrrf_golden_cases())
def test_python_and_rust_wrrf_have_exact_order_and_score_parity(rust_core, case):
    py = weighted_rrf(case.rankings, case.config)
    rs = rust_core.weighted_rrf_fuse(case.rankings, case.config)
    assert [row[0] for row in rs] == [row[0] for row in py] == case.expected_ids
    assert [row[1] for row in rs] == pytest.approx([row[1] for row in py], abs=1e-15)
```

Also add four route tests with exact assertions:

- `test_wrrf_plan_with_fts_routes_entire_request_to_python_with_capability_reason`: Rust supply spy is never called and audit reason is `rust_capability_missing:fts`.
- `test_two_channel_wrrf_may_use_rust_and_reports_exact_effective_policy`: vector/BM25 request calls Rust and reports the hashed candidate plus Rust runtime.
- `test_graph_presence_does_not_force_wrrf_rust_fallback`: graph remains evidence-only and Rust still handles the two fusion channels.
- `test_legacy_rust_k60_is_never_labeled_max_or_wrrf`: effective policy is legacy-auto and compatibility metadata states K=60 unweighted RRF.
- `test_python_and_rust_reject_the_same_non_integral_k_payloads`: parameterize the shared fixture's `2.5`, boolean, zero, negative, and overflow inputs; both entry points fail before ranking with `invalid_k:must_be_positive_integer` (overflow may retain an appended representational detail after the stable prefix).

- [x] **Step 2: Run RED in both runtimes**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_fusion_policy.py tests/test_rust_python_parity.py tests/test_rust_integration.py -q --no-cov
cargo test --manifest-path rust/context-engine-core/Cargo.toml retrieval::fusion
```

Expected: missing Rust WRRF API and capability routing metadata.

- [x] **Step 3: Implement identical validation, formula, float representation, and tie order**

Keep existing `rrf_fuse()` and K=60 behavior for `legacy-auto`. Do not rename legacy Rust output as a comparable versioned policy. Add complete pre-fusion vector/BM25 rankings to the Rust pack only after canonical admission. Because Rust `audit_metadata` remains `HashMap<String, String>`, serialize the nested fusion audit as `retrieval_fusion_json`; `_convert_rust_pack()` validates/decodes it into the same Python structure and fails closed on malformed JSON.

- [x] **Step 4: Run parity and full Rust tests**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_fusion_policy.py tests/test_rust_python_parity.py tests/test_rust_integration.py -q --no-cov
cargo fmt --manifest-path rust/context-engine-core/Cargo.toml -- --check
cargo test --manifest-path rust/context-engine-core/Cargo.toml
```

Expected: golden scores/orders match and all 52+ existing Rust tests remain green.

- [ ] **Step 5: Commit parity/routing**

```powershell
git add rust/context-engine-core/src rust/context-engine-core/tests tests/fixtures/recall_quality/wrrf-v1-golden.json plastic_promise/core/context_engine.py tests/test_fusion_policy.py tests/test_rust_python_parity.py tests/test_rust_integration.py
git commit -m "feat: add Python Rust weighted RRF parity"
```

---

### Task 11: Complete Admitted Pre-Fusion Rankings and Best-Constituent Gates

**Files:**
- Modify: `plastic_promise/core/context_engine.py:79-90, 3187-3610, 4080-4094`
- Modify: `rust/context-engine-core/src/context_engine.rs:157-194, 752-780, 1140-1180`
- Modify: `plastic_promise/core/recall_quality.py:105-215, 377-430, 593-649, 905-1049`
- Modify: `scripts/benchmark_recall_quality.py:373-442, 825-869, 1104-1415`
- Modify: `plastic_promise/mcp/tools/memory.py` and `plastic_promise/mcp/tools/context.py` serialization only if the existing debug serializer omits the new fields
- Modify: `tests/test_recall_quality_benchmark.py`
- Modify: `tests/test_rust_integration.py`
- Modify: `tests/test_rust_python_parity.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ChannelRankingItem:
    memory_id: str
    score: float
    rank: int

ContextPack.channel_rankings: dict[str, list[dict[str, Any]]]
ContextPack.channel_states: dict[str, dict[str, Any]]

@dataclass(frozen=True)
class ChannelMetricSummary:
    overall: MetricSlice
    by_language: Mapping[str, MetricSlice]
    by_group: Mapping[str, MetricSlice]

MetricSummary.channels: Mapping[str, ChannelMetricSummary]

def evaluate_best_constituent_gate(
    summary: MetricSummary,
    *,
    required_languages: Sequence[str] = ("en", "zh", "cross-lingual"),
    required_groups: Sequence[str] = ("token-overlap", "partial-overlap", "zero-overlap"),
    tolerance: float = 0.0,
) -> dict[str, Any]: ...
```

Channel rankings are bounded by the declared per-channel window, independently admitted by project/status/source rules, and captured before fusion, graph layering, source expansion, score filters, reranking, MMR, or fused truncation. They include candidates absent from the final fused list. The final common admission gate synchronously removes any newly blocked ID from both fused output and every debug ranking before serialization. Rank-producing channels are `vector`, `bm25`, and `fts`; graph/provenance are represented only in state as `evidence_only=true`.

- [x] **Step 1: Replace the current survivor-reconstruction test with RED complete-ranking tests**

```python
def test_debug_contract_preserves_constituent_candidate_absent_from_fused_survivors(engine):
    pack = supply_fixture_where_vector_only_candidate_is_cut_after_fusion(engine, debug=True)
    fused_ids = ids(pack.core + pack.related + pack.divergent)
    vector_ids = [row["id"] for row in pack.channel_rankings["vector"]]
    assert "vector-only-tail" in vector_ids
    assert "vector-only-tail" not in fused_ids

```

Add four independent tests:

- `test_channel_rankings_are_admitted_before_content_or_scores_are_exposed`: seed wrong/private-cross-project IDs at channel rank 1 and assert neither ID/content/score appears in debug output.
- `test_every_planned_channel_has_complete_state_and_stable_reason`: every channel state has all six booleans plus a non-empty reason when any transition is false.
- `test_zero_weight_constituent_still_participates_in_quality_gate`: a zero-weight enabled BM25 ranking that beats fused makes the intra-report gate fail.
- `test_planned_enabled_unavailable_channel_fails_comparability`: an unavailable planned FTS channel fails before metric comparison rather than disappearing.

- [x] **Step 2: Run RED and confirm the existing false evidence path**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_recall_quality_benchmark.py -k "channel or constituent or comparability" -q --no-cov
```

Expected: `_channels_from_pack()` cannot find `vector-only-tail` because it reads only final `per_item_stats`.

- [x] **Step 3: Populate complete rankings in Python/Rust and consume them directly**

Delete the benchmark's `per_item_stats` reconstruction logic. Preserve `per_item_stats` for item diagnostics only. Normalize report channel name to `bm25`; accept historical `lexical` only while parsing older non-acceptance reports, never in new candidate manifests. In `_finalize_supply_pack()`, intersect rankings with final canonically admitted IDs so stale/forbidden content cannot gain metric credit during a concurrent change.

- [x] **Step 4: Add intra-report overall and split gates**

For MRR and hit@5, compare fused to the best enabled + available + executed rank channel overall and for each required language/group split. Zero fusion weight does not hide a channel. Missing planned enabled channels fail the report before quality comparison.

- [x] **Step 5: Verify complete evidence and parity**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_recall_quality_benchmark.py tests/test_rust_integration.py tests/test_rust_python_parity.py -q --no-cov
cargo test --manifest-path rust/context-engine-core/Cargo.toml
```

- [ ] **Step 6: Commit complete constituent evidence**

```powershell
git add plastic_promise/core/context_engine.py plastic_promise/core/recall_quality.py scripts/benchmark_recall_quality.py plastic_promise/mcp/tools/memory.py plastic_promise/mcp/tools/context.py rust/context-engine-core/src/context_engine.rs tests/test_recall_quality_benchmark.py tests/test_rust_integration.py tests/test_rust_python_parity.py
git commit -m "fix: measure complete pre-fusion channel rankings"
```

---

### Task 12: Calibration/Manifest Contracts and an Unopened Held-Out Corpus

**Files:**
- Create: `plastic_promise/core/recall_experiment.py`
- Create: `tests/fixtures/recall_quality/wrrf-v1-grid.json`
- Create: `tests/fixtures/recall_quality/v2-heldout.json`
- Modify: `tests/fixtures/recall_quality/v1.json` (add immutable `evidence_role=calibration`; preserve corpus/case hashes unless their canonical content actually changes)
- Modify: `scripts/benchmark_recall_quality.py:44-64, 74-160, 930-1018, 1104-1415`
- Modify: `plastic_promise/core/recall_quality.py`
- Create: `tests/test_recall_experiment.py`
- Modify: `tests/test_recall_quality_benchmark.py`

**Interfaces:**

```python
EXPERIMENT_MANIFEST_SCHEMA = "recall-experiment/v1"
CALIBRATION_GRID_SCHEMA = "wrrf-calibration-grid/v1"
RECALL_QUALITY_REPORT_SCHEMA = "recall-quality-report/v2"

@dataclass(frozen=True)
class FrozenCandidateManifest:
    candidate_id: str
    candidate_dimension: str
    calibration_fingerprint: str
    heldout_fingerprint: str
    source_commit: str
    dirty_fingerprint: str
    fusion_config: FusionConfig
    retrieval_configuration: Mapping[str, Any]
    embedding_configuration: Mapping[str, Any]
    dependency_versions: Mapping[str, str]
    runtime_route: str

def select_calibration_candidate(
    reports: Sequence[Mapping[str, Any]], grid: Mapping[str, Any]
) -> Mapping[str, Any]: ...
def freeze_candidate_manifest(
    *, selected_report: Mapping[str, Any], grid: Mapping[str, Any],
    calibration: RecallDataset, heldout: RecallDataset,
    source_commit: str, dirty_fingerprint: str,
    retrieval_configuration: Mapping[str, Any],
    embedding_configuration: Mapping[str, Any],
    dependency_versions: Mapping[str, str], runtime_route: str,
    candidate_dimension: str = "fusion_policy",
) -> FrozenCandidateManifest: ...
def validate_heldout_separation(calibration: RecallDataset, heldout: RecallDataset) -> None: ...
```

`RecallDataset` gains `evidence_role: Literal["calibration", "held-out"]`; the existing V1 contract is permanently `calibration`. V2 reports include dataset role/hashes, candidate dimension/policy/ID/manifest hash, requested/effective policy/runtime, every channel's overall/language/group metrics, and each case's channel states plus complete pre-fusion rankings.

The benchmark CLI separates index text from fusion:

```text
--index-text-policy legacy|compact-v2
--fusion-policy legacy-auto|max-v1|wrrf-v1|wrrf-v1:<64-lowercase-hex>
--fusion-grid PATH
--calibrate
--freeze-manifest PATH
--candidate-manifest PATH
--heldout-dataset PATH
```

Keep `--candidate legacy|compact-v2` as a deprecated compatibility alias for index-text-only diagnostic tests; acceptance reports use explicit fields.

- [x] **Step 1: Write the exact preregistered grid and add RED schema/selector tests**

The JSON content must be byte-for-byte equivalent to the `Preregistered Fusion Search` section above. Tests assert canonical serialization and selection order, not only membership.

Tests assert: the parsed grid exactly equals the preregistered JSON in this plan; shuffled reports select the same config by the five-key lexicographic objective; zero survivors raises `no_calibration_candidate` without writing a manifest; and candidate ID equals `wrrf-v1:` plus SHA-256 of canonical config JSON.

- [x] **Step 2: Author and validate the untouched bilingual held-out corpus without retrieving it**

Create new English, Chinese, and cross-lingual cases covering token/partial/zero overlap, identifiers, same-domain distractors, forbidden IDs, draft/contested/stale synthesis, and source-change invalidation. Do not copy a V1 query/relevant-ID pair. Validate stable corpus/case hashes and separation only; do not call a retriever.

Use exact tests `test_v2_heldout_is_bilingual_separate_and_covers_required_scenarios`, `test_v1_is_rejected_as_heldout_evidence`, `test_manifest_must_exist_before_heldout_result_can_be_loaded`, and `test_calibration_freeze_hashes_but_never_retrieves_heldout`. The final test passes a held-out case callback that immediately calls `pytest.fail("held-out retrieval during calibration")`; calibration may parse/hash the held-out dataset for the manifest, but it must invoke only calibration callbacks, persist the immutable manifest, and make zero HTTP MCP/retriever calls for each held-out case. They assert all required language/group/status scenarios, no duplicated normalized query/relevant-ID pair, role `held-out`, stable hashes, V1 role `calibration`, and refusal before a valid manifest path/hash is supplied.

- [x] **Step 3: Run RED, then implement schemas, selector, and fail-closed manifest validation**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_recall_experiment.py tests/test_recall_quality_benchmark.py -k "grid or manifest or heldout or calibration" -q --no-cov
```

Expected RED: missing experiment module/fixture/CLI and no boundary preventing a calibration path from querying held-out cases. GREEN must reject missing hashes, dirty/source drift, unapproved dimensions, runtime/config differences, and unknown dependency versions.

- [x] **Step 4: Exercise selector math without producing acceptance evidence**

```powershell
.venv\Scripts\python.exe scripts/benchmark_recall_quality.py --dataset tests/fixtures/recall_quality/v1.json --backend deterministic --index-text-policy legacy --fusion-grid tests/fixtures/recall_quality/wrrf-v1-grid.json --calibrate --output .artifacts/wrrf-v1-calibration-math.json
```

Expected: metric/selection math is deterministic and the report is explicitly non-publishable. Do not freeze the acceptance manifest from a deterministic or direct-engine route. Task 13 first installs the public MCP runner, then performs the sole live V1 selection and manifest freeze before reading held-out results.

- [ ] **Step 5: Commit code, grid, and unopened held-out corpus**

```powershell
git add plastic_promise/core/recall_experiment.py plastic_promise/core/recall_quality.py scripts/benchmark_recall_quality.py tests/fixtures/recall_quality/v1.json tests/fixtures/recall_quality/wrrf-v1-grid.json tests/fixtures/recall_quality/v2-heldout.json tests/test_recall_experiment.py tests/test_recall_quality_benchmark.py
git commit -m "feat: freeze WRRF experiments before held-out evaluation"
```

Do not query the held-out fixture in this task. Runtime manifests/reports belong under `.artifacts/` and are never committed.

---

### Task 13: Public HTTP MCP Held-Out Quality Runner and Strict Comparator

**Files:**
- Modify: `scripts/http_mcp_harness.py`
- Modify: `scripts/benchmark_recall_quality.py:147-455, 668-914, 1104-1415`
- Modify: `plastic_promise/core/recall_experiment.py`
- Modify: `plastic_promise/core/recall_quality.py`
- Modify: `plastic_promise/mcp/tools/memory.py` debug response serialization if required
- Modify: `tests/test_recall_quality_benchmark.py`
- Modify: `tests/test_recall_experiment.py`
- Modify: `tests/test_smoke_http_mcp.py`

**Interfaces:**

```python
async def _http_live_backend(
    dataset: RecallDataset,
    *,
    index_text_policy: str,
    fusion_policy: str,
    candidate_manifest: FrozenCandidateManifest | None,
    paths: LivePaths,
) -> tuple[Callable[[RecallCase], Awaitable[dict[str, Any]]], dict[str, Any], dict[str, Any]]: ...

def compare_fusion_reports(
    baseline_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    *, manifest: FrozenCandidateManifest,
    tolerances: Mapping[str, float],
) -> dict[str, Any]: ...
```

`--backend live` now means a real spawned Streamable HTTP MCP process and public tools. Preserve the old direct engine adapter only as `--backend engine-diagnostic`, always `publishable_claim=false`. The harness seeds ordinary records through public `memory_store`, creates synthesis through the existing public synthesis route, verifies through public governed feedback, and maps actual IDs back to fixture IDs. Every measured query uses public `memory_recall` and `context_supply` with debug enabled and strict project policy.

- [x] **Step 1: Add RED process/public-surface and effective-policy tests**

Add six exact tests:

- `test_live_backend_requires_health_pid_equal_spawned_process`: mismatched health PID aborts before seeding.
- `test_engine_diagnostic_report_can_never_be_publishable`: otherwise complete direct-engine evidence remains diagnostic.
- `test_live_report_requires_public_recall_and_context_calls_for_every_case`: spy counts equal case count for both tools and zero direct engine retrieval calls.
- `test_requested_and_effective_policy_runtime_must_match`: either mismatch makes the report non-publishable.
- `test_comparator_allows_only_manifest_candidate_dimension`: mutate each unapproved source/config/runtime field and assert comparability failure naming that path.
- `test_comparator_gates_fused_against_baseline_and_best_constituent_per_split`: parameterize overall plus every required language/group slice and both MRR/hit@5; each isolated regression fails the named check.

- [x] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_recall_quality_benchmark.py tests/test_recall_experiment.py tests/test_smoke_http_mcp.py -q --no-cov
```

Expected: current `live` adapter directly constructs `ContextEngine`, hardcodes `legacy|compact-v2`, and reconstructs channels from survivors.

- [x] **Step 3: Implement isolated public MCP baseline/candidate runs**

Both processes use the same held-out dataset hash, source commit/dirty fingerprint, legacy index text, query expansion, embedding model/dimension, dependency versions, channel windows, and runtime route. The only allowed difference is `fusion_policy/config` declared by the manifest. Report `requested_policy`, `effective_policy`, `requested_runtime`, `effective_runtime`, candidate ID, channel states/rankings, public call counts, server PID, and sanitized logs.

- [x] **Step 4: Implement strict adoption gates**

Require candidate overall MRR and hit@5 >= `max-v1`, every required split within tolerance, fused >= best enabled constituent overall and per split, forbidden-hit non-increase, no fallback/degradation, p95 within budget, store-recall-supply smoke pass, and at least one preregistered primary quality metric improvement. Planned enabled unavailable channels make reports incomparable.

- [ ] **Step 5: Select/freeze on public V1, then run the held-out experiment exactly once**

```powershell
.venv\Scripts\python.exe scripts/benchmark_recall_quality.py --dataset tests/fixtures/recall_quality/v1.json --backend live --index-text-policy legacy --fusion-grid tests/fixtures/recall_quality/wrrf-v1-grid.json --calibrate --heldout-dataset tests/fixtures/recall_quality/v2-heldout.json --freeze-manifest .artifacts/wrrf-v1-candidate-manifest.json --warmup 1 --repeat 3 --output .artifacts/wrrf-v1-calibration-live.json
.venv\Scripts\python.exe scripts/benchmark_recall_quality.py --dataset tests/fixtures/recall_quality/v2-heldout.json --backend live --index-text-policy legacy --fusion-policy max-v1 --candidate-manifest .artifacts/wrrf-v1-candidate-manifest.json --warmup 1 --repeat 3 --output .artifacts/recall-quality-heldout-max-v1.json
.venv\Scripts\python.exe scripts/benchmark_recall_quality.py --dataset tests/fixtures/recall_quality/v2-heldout.json --backend live --index-text-policy legacy --fusion-policy wrrf-v1 --candidate-manifest .artifacts/wrrf-v1-candidate-manifest.json --warmup 1 --repeat 3 --output .artifacts/recall-quality-heldout-wrrf-v1.json
.venv\Scripts\python.exe scripts/benchmark_recall_quality.py --gate --baseline .artifacts/recall-quality-heldout-max-v1.json --candidate-report .artifacts/recall-quality-heldout-wrrf-v1.json --candidate-manifest .artifacts/wrrf-v1-candidate-manifest.json --tolerance 0 --max-p95-ratio 1.20
```

The first command may read only the held-out bytes needed to fingerprint the manifest; it must not issue an HTTP MCP/retriever query for a held-out case. The candidate command's bare `wrrf-v1` is valid solely because `--candidate-manifest` is supplied and must normalize to its exact immutable `wrrf-v1:<64-hex-config-hash>` before server launch; the resulting report's requested/effective policy fields both contain that hash-qualified ID. Expected: the first command selects exactly one immutable candidate on public V1 and writes a manifest bound to the unopened held-out hash/runtime. The remaining commands exit 0 and candidate passes every gate. If selection or held-out fails, retain reports, keep `legacy-auto`, and do not mark the measured fusion-improvement goal complete. Do not edit the grid or retune after reading held-out output.

- [ ] **Step 6: Commit the public runner and comparator**

```powershell
git add scripts/http_mcp_harness.py scripts/benchmark_recall_quality.py plastic_promise/core/recall_experiment.py plastic_promise/core/recall_quality.py plastic_promise/mcp/tools/memory.py tests/test_recall_quality_benchmark.py tests/test_recall_experiment.py tests/test_smoke_http_mcp.py
git commit -m "feat: gate fusion quality through public HTTP MCP"
```

---

### Task 14: Full Verification, Documentation, Review, Audit, and Branch Finish

**Files:**
- Modify: `README.md` only where the public runtime/CLI behavior changed
- Modify: `docs/GOAL.md`
- Modify: `CHANGELOG.md`
- Modify: this plan by checking completed boxes and recording final evidence paths/hashes
- Test: all Python and Rust tests

- [ ] **Step 1: Verify declared dependencies and clean tracked state**

```powershell
.venv\Scripts\python.exe -c "import importlib.metadata as m; print(m.version('lancedb'))"
git status --short
git diff --check
```

Expected: LanceDB >= 0.34.0; only intentional tracked changes plus preserved untracked runtime artifacts. An older-API fallback is a failed baseline.

- [ ] **Step 2: Run focused corrective shards**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_ordinary_memory_mutation.py tests/test_memory_operations.py tests/test_synthesis_store.py tests/test_synthesis_maintenance.py tests/test_synthesis_mcp_routing.py tests/test_memory_proposals.py -q --no-cov
.venv\Scripts\python.exe -m pytest tests/test_maintenance_scheduler.py tests/test_safety_net_daemon.py tests/test_launcher.py tests/test_smoke_restart_recovery.py -q --no-cov
.venv\Scripts\python.exe -m pytest tests/test_fusion_policy.py tests/test_retrieval_planner.py tests/test_recall_quality_benchmark.py tests/test_recall_experiment.py tests/test_rust_python_parity.py tests/test_rust_integration.py -q --no-cov
cargo test --manifest-path rust/context-engine-core/Cargo.toml
```

- [ ] **Step 3: Run the full repository suites and format/static checks**

```powershell
.venv\Scripts\python.exe -m pytest -q --no-cov
.venv\Scripts\python.exe -m ruff check plastic_promise daemons scripts tests
.venv\Scripts\python.exe -m ruff format --check plastic_promise daemons scripts tests
cargo fmt --manifest-path rust/context-engine-core/Cargo.toml -- --check
cargo test --manifest-path rust/context-engine-core/Cargo.toml
```

Expected: at least the 1331-pass baseline remains green, skips are explained, and Rust remains 52+ green. Record exact counts.

- [ ] **Step 4: Re-run non-mock acceptance proofs from a clean process state**

```powershell
.venv\Scripts\python.exe scripts/smoke_restart_recovery.py --artifact-dir .artifacts/recovery-smoke-final --json
.venv\Scripts\python.exe scripts/benchmark_recall_quality.py --gate --baseline .artifacts/recall-quality-heldout-max-v1.json --candidate-report .artifacts/recall-quality-heldout-wrrf-v1.json --candidate-manifest .artifacts/wrrf-v1-candidate-manifest.json --tolerance 0 --max-p95-ratio 1.20
```

Validate artifact/report SHA-256 hashes and public MCP PIDs. If either proof fails, return to the first failing task; do not relabel mock or direct-engine evidence as acceptance.

- [ ] **Step 5: Update operations and rollback documentation**

Document default `legacy-auto`, explicit `max-v1`, frozen candidate syntax, invalid config behavior, Python fallback reasons, one-shot daemon, recovery smoke command, heartbeat schema, outbox V2 compatibility/V3 writes, and rollback (unset candidate env, retain SQLite/outbox, replay with default). State whether the candidate passed; do not claim adoption if only eligibility was proven.

- [ ] **Step 6: Request code review and apply every accepted finding**

Run the SuperPowers `requesting-code-review` stage and Plastic Promise `review_run` prepare/evaluate/apply pipeline. Review by commit/task boundaries, with special attention to transaction ownership, stale worker races, process cleanup, held-out leakage, and policy metadata truthfulness. Re-run each affected focused test after fixes.

- [ ] **Step 7: Run the mandatory high-risk audit**

This implementation necessarily exceeds 10 code files and 500 changed lines, so use the repository `audit` skill and MCP `sp-stage(stage="audit")`. Require a 10-item structured PASS covering security, transaction atomicity, cross-module contracts, migrations, backwards compatibility, subprocess safety, test evidence, performance, observability, and rollback. A BLOCK finding returns to implementation.

- [ ] **Step 8: Perform the final acceptance 1-16 audit**

For each design acceptance item, record the exact test name, commit, runtime artifact/report field, and pass/fail. Items 1-9 are regression-protected existing behavior; 10-16 require new corrective evidence. Explicitly prove:

- tombstones/current correction and same-transaction dependent removal;
- byte-identical non-target fields;
- independent reachable schedules plus parent/stage spans;
- real pending-to-done cross-process replay and revision-2 current-only results;
- frozen candidate Python/Rust parity and public held-out baseline/best-constituent pass;
- full suites under declared dependency versions.

- [ ] **Step 9: Commit documentation/evidence references and finish the branch**

```powershell
git add README.md docs/GOAL.md CHANGELOG.md docs/superpowers/plans/2026-07-12-corrective-governed-retrieval-plan.md
git commit -m "docs: record governed retrieval corrective acceptance"
git status --short --branch
git log --oneline --decorate -15
```

Use `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`. Do not merge, push, or delete the worktree unless the user separately requests that external Git action.

## Plan Self-Review Gate

Before committing this plan, verify all of the following:

- [ ] Every corrective design requirement at lines 905-937 and acceptance item 1-16 has an owning task and executable evidence.
- [ ] The caller inventory precedes existing-ID rejection; no partial-record caller is silently treated as idempotent creation.
- [x] Checked ordinary delete/upsert exists before the coordinator depends on it.
- [ ] Public and internal content/availability routes cannot bypass immediate invalidation.
- [ ] Patch/outbox/synthesis invalidation share transaction ownership and roll back together.
- [ ] One-shot reuses the production registry/cycle; recovery and quality claims use real HTTP MCP processes.
- [ ] Fusion policy names, formula, channel taxonomy, capability routing, and audit fields are unambiguous.
- [ ] Complete channel rankings are captured before fusion and remain separately admitted.
- [ ] The finite grid and deterministic selection objective are fixed; V1 is calibration-only.
- [ ] The held-out corpus is only queried after the immutable manifest is persisted.
- [ ] Failure outcomes preserve evidence and keep `legacy-auto`; no result-dependent retuning is permitted.
- [ ] Every code-changing task 1-13 has RED, expected failure, GREEN implementation scope, exact verification commands, and a separate commit boundary; Task 0 is a read-only inventory gate and Task 14 is final verification.
