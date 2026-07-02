# 8-Unit Vertical Slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate CortexReach proven retrieval patterns into Plastic Promise governance architecture — 8 independent units, each env-var gated, zero new MCP tools.

**Architecture:** 8 units across 3 layers: retrieval pipeline (query expansion, reranker, decay ranking), storage lifecycle (L2 tier, periodic cron, vector MMR), and infrastructure (emoji detection, Rust merge). Each unit is independently testable and rollback-safe via env var gate.

**Tech Stack:** Python 3.13, LanceDB, Ollama mxbai-embed-large, SQLite

## Global Constraints

- Every unit gated behind env var (default ON for P0/P1, OFF for P2)
- No new MCP tools, no LanceDB schema migrations, no API changes
- All external calls (Jina, SiliconFlow) wrapped in try/except — never block retrieval
- Trust-score modulation uses existing `trust_boost` variable (context_engine.py:1282)
- All governance atoms (defense, step_closure) run automatically via sp-stage injection (PR #11)
- Commit per unit with Conventional Commits format: `feat(<unit>): <description>`
- Existing tests must pass after each unit

---

## File Structure

| File | Unit | Change Type | Responsibility |
|------|------|-------------|---------------|
| `plastic_promise/core/query_expander.py` | 1 | **NEW** | Local synonym dict, domain-aware expansion |
| `plastic_promise/core/context_engine.py` | 1,3,4,6 | **MODIFY** | Expansion hook, decay ranking, tier promotion, MMR fix |
| `plastic_promise/core/reranker.py` | 2 | **REWRITE** | MultiProviderReranker class, 4-provider chain |
| `plastic_promise/core/constants.py` | 4 | **MODIFY** | L2 tier config in MEMORY_TIERS |
| `plastic_promise/memory/soul_memory.py` | 4,5 | **MODIFY** | L2 classify_tier, promote/demote stepwise |
| `plastic_promise/cron/scan_memory_decay.py` | 5 | **MODIFY** | Routine maintenance + anomaly detection |
| `plastic_promise/core/lancedb_store.py` | 6 | **MODIFY** | get_vector(memory_id) method |
| `plastic_promise/core/noise_filter.py` | 7 | **MODIFY** | Emoji regex + detection check |
| `rust/context-engine-core/src/` | 0 | **MERGE** | Rebase worktree, fix formula divergence |
| `.agents/skills/exemplar-research/SKILL.md` | — | **DONE** | Skill registration (PR #12) |
| `plastic_promise/skills/superpowers_stages.py` | — | **DONE** | Governance injection (PR #11) |
| `plastic_promise/skills/engine.py` | — | **DONE** | AtomRegistry (PR #11) |

---

## Unit Dependency Graph

```
Unit 0 (Rust merge) ──┐
                       ├── no dependencies, can start immediately
Unit 1 (Query Exp)  ──┤
Unit 2 (Reranker)   ──┤
Unit 3 (Decay Rank) ──┤  all independent of each other
Unit 6 (MMR Fix)    ──┤
Unit 7 (Emoji)      ──┤
                       │
Unit 4 (L2 Tier)    ──┤  independent but shares soul_memory.py with Unit 5
Unit 5 (Periodic)   ──┘  independent but shares scan_memory_decay.py with nothing

Recommended: units 0,1,2,3,6,7 in parallel (6 subagents) → units 4,5 sequentially
```

---

## Unit 0: Rust Engine Worktree Merge + Graph Injection

**Priority:** P0/P1 | **Effort:** 1h | **Env:** `PP_FORCE_PYTHON_SUPPLY=0` to test

- [ ] **Step 1: Rebase worktree onto current main**
  ```bash
  git checkout worktree-rust-engine-phase2
  git rebase main
  # Resolve conflicts if any (context_engine.py most likely)
  ```
- [ ] **Step 2: Fix formula divergences found in exemplar research**
  - `rust/context-engine-core/src/retrieval/fusion.rs`: RRF K 60 → 20
  - `rust/context-engine-core/src/domain/decay.rs`: linear cap → log1p formula
- [ ] **Step 3: Inject graph traversal into _supply_rust()**
  - File: `plastic_promise/core/context_engine.py` (~line 1820)
  - Serialize `self._graph_nodes` + `self._graph_edges` to JSON
  - Call `rust.load_graph(graph_json)` before `rust.supply()`
  - ~10 lines Python
- [ ] **Step 4: Cherry-pick to feature branch and verify**
  ```bash
  PP_FORCE_PYTHON_SUPPLY=0 python -c "
  from plastic_promise.core.context_engine import ContextEngine
  e = ContextEngine(); e._ensure_heavy_init()
  pack = e.supply('test', [0.0]*1024, 'general', 'global')
  print(f'principles: {len(pack.activated_principles)}')  # >= 2
  print(f'graph_nodes: {pack.audit_metadata.get(\"graph_nodes\")}')  # >= 50
  "
  ```
- [ ] **Step 5: Commit and PR**
  ```bash
  git add -A && git commit -m "feat(rust): merge worktree + fix formula divergence + graph injection"
  gh pr create --title "feat(rust): merge worktree-rust-engine-phase2 with formula fixes"
  ```

---

## Unit 1: Query Expansion (Local Synonym Dictionary)

**Priority:** P0 | **Effort:** 1.5h | **Env:** `PP_QUERY_EXPANSION=1` default on

- [ ] **Step 1: Create query_expander.py**
  - File: `plastic_promise/core/query_expander.py` (NEW, ~100 lines)
  - `SYNONYM_MAP`: dict with `cn`, `en`, `expansions`, `domains` keys
  - Initial 15-20 entries covering Plastic Promise domains: governance, audit, memory, retrieval, embedding, decay, trust, graph, tier, principles, building, fixing, designing
  - `expand_query(query, domain_hint=None) -> str`
  - CJK: exact substring match | English: `\b` word boundary regex
  - Max 3 expansion terms, already-present terms skipped
  - Short queries (<2 chars) pass through
- [ ] **Step 2: Wire into _text_retrieval()**
  - File: `plastic_promise/core/context_engine.py` line 1290
  - Before `text_results = self._text_retrieval(task_description, trust_boost)`:
    ```python
    from plastic_promise.core.query_expander import expand_query
    expanded = expand_query(task_description, self._domain_hint)
    text_results = self._text_retrieval(expanded, trust_boost)
    ```
- [ ] **Step 3: Verify**
  ```bash
  python -c "from plastic_promise.core.query_expander import expand_query; assert 'crash' in expand_query('挂了')"
  ```
- [ ] **Step 4: Commit**

---

## Unit 2: Unified Multi-Provider Reranker

**Priority:** P0 | **Effort:** 2h | **Env:** `PP_RERANK_DISABLED=1` for off

- [ ] **Step 1: Rewrite reranker.py**
  - File: `plastic_promise/core/reranker.py` (REWRITE ~110→~200 lines)
  - `MultiProviderReranker` class with `rerank(query, candidates) -> list`
  - Provider chain: `_rerank_jina()` → `_rerank_siliconflow()` → `_rerank_ollama()` → `_rerank_cosine()`
  - Blend: `final = 0.6*ce_score + 0.4*original`, floor `original*0.5`
  - 5s per provider timeout, 10s total
  - Cache: SHA-256(query+candidate_ids), LRU 64, TTL 60s
  - Jina: POST `api.jina.ai/v1/rerank` (free tier, no key)
  - SiliconFlow: POST `api.siliconflow.cn/v1/rerank` (free tier)
  - Ollama: existing `/api/generate` with structured prompt
  - Cosine: pure computation, always available
- [ ] **Step 2: Delete inline _apply_rerank()**
  - File: `plastic_promise/core/context_engine.py` lines 1184-1246
  - Remove `_apply_rerank()` method and `_last_rerank_status` attribute
- [ ] **Step 3: Wire unified reranker into both callers**
  - Caller 1: `context_engine.py:1380` (was `_apply_rerank` call)
  - Caller 2: `context.py:47` (was `cross_encode_rerank` call)
  - Both → `MultiProviderReranker().rerank(task_description, items)`
- [ ] **Step 4: Env vars**
  - Remove `PP_RECALL_RERANK` (old opt-in gate)
  - Add `PP_RERANK_DISABLED=1` (emergency off)
  - Add `PP_RERANK_PROVIDERS=jina,siliconflow,ollama,cosine` (default chain)
- [ ] **Step 5: Test with Ollama down**
  ```bash
  # Stop Ollama, verify Jina fallback works
  PP_RERANK_PROVIDERS=jina,cosine python -c "from plastic_promise.core.reranker import MultiProviderReranker; ..."
  ```
- [ ] **Step 6: Commit**

---

## Unit 3: Decay-Aware Ranking + Trust Modulation

**Priority:** P0 | **Effort:** 0.5h | **Env:** `PP_DECAY_IN_RANKING=1` default on

- [ ] **Step 1: Add _apply_decay_awareness() method**
  - File: `plastic_promise/core/context_engine.py` (NEW method, ~30 lines)
  - Formula A: `score = min(1.0, score + exp(-age/recency_hl)*0.1)` — additive recency
  - Formula B: `score = max(score*0.5, score * (0.5 + 0.5*exp(-age/effectiveHL)))` — multiplicative decay
  - Trust modulation: `trust_mod = 1.0 + (trust_boost - 1.0)*0.5`
  - `recency_hl = 14.0 * trust_mod`
  - Reads `mem.get("created_at")`, `mem.get("effective_half_life")` — both already in dict
- [ ] **Step 2: Wire into supply loop**
  - File: `plastic_promise/core/context_engine.py` line 1337
  - After `score = score * multiplier`, insert: `score = self._apply_decay_awareness(score, mem, current_time_str, trust_boost)`
- [ ] **Step 3: Verify**
  ```bash
  # Two memories with same content but 30 days apart → newer ranks higher
  ```
- [ ] **Step 4: Commit**

---

## Unit 4: L2 Tier Completion

**Priority:** P1 | **Effort:** 1.5h | **Env:** `PP_TIER_AUTO_PROMOTE=1` default on

- [ ] **Step 1: Add L2 to MEMORY_TIERS**
  - File: `plastic_promise/core/constants.py` line 319
  - Add: `"L2": {"max_items": 500, "ttl_hours": 168, "promote_threshold": 20}`
- [ ] **Step 2: Insert L2 branch in classify_tier()**
  - File: `plastic_promise/memory/soul_memory.py` line 315
  - Current: `composite >= 0.5 AND access >= 3 → L3, else L1`
  - New: `composite >= 0.7 AND access >= 20 → L3 | composite >= 0.4 AND access >= 5 → L2 | else L1`
- [ ] **Step 3: Stepwise promote/demote**
  - File: `plastic_promise/memory/soul_memory.py` lines 348-376
  - `promote`: L1→L2→L3 (no direct L1→L3 jumps)
  - `demote`: L3→L2→L1 (no direct L3→L1 jumps)
  - Update `MemoryTierManager.__init__` to load L2 config
- [ ] **Step 4: Real-time promotion during retrieval**
  - File: `plastic_promise/core/context_engine.py` line 2098
  - Add `self._maybe_adjust_tier(mid)` after `access_count` increment
  - `_maybe_adjust_tier(mid)`: check access_count against promote_threshold, call MemoryTierManager
- [ ] **Step 5: Test**
  ```bash
  python -c "from plastic_promise.memory.soul_memory import MemoryTierManager; ..."
  # Access a memory 6 times → should promote L1→L2
  ```
- [ ] **Step 6: Commit**

---

## Unit 5: Periodic Activation of Dead Code

**Priority:** P1 | **Effort:** 0.5h | **Env:** `PP_PERIODIC_MAINTENANCE=1` default on

- [ ] **Step 1: Add routine maintenance to scan_memory_decay()**
  - File: `plastic_promise/cron/scan_memory_decay.py` (add after existing 3 checks, before `conn.close()`)
  - Call `RecMem().update_all_decay()` — activates dead code (zero callers before)
  - Call `EvolveR(rm).evolve_cycle()` — activates non-periodic code
  - Wrap in try/except — maintenance failures never block scan
- [ ] **Step 2: Add decay anomaly detection (4th dimension)**
  - SQL: `SELECT id FROM memories WHERE decay_multiplier < 0.2 AND access_count > 10 AND tier != 'L1' LIMIT 20`
  - Findings dispatched as `fix_memory` tasks → Hunter Guild (pi_fixer)
  - L1 excluded (L1 is fast-decay layer, low decay is normal)
- [ ] **Step 3: Verify execution order**
  - Routine maintenance BEFORE anomaly detection (use fresh decay values)
- [ ] **Step 4: Commit**

---

## Unit 6: Vector MMR Fix

**Priority:** P0 | **Effort:** 1h | **Env:** `PP_MMR_VECTOR=1` default on

- [ ] **Step 1: Add get_vector() to LanceDBStore**
  - File: `plastic_promise/core/lancedb_store.py` (NEW method, ~15 lines)
  - `get_vector(memory_id) -> Optional[list[float]]`
  - Single row lookup via `self._table.search().where(...)`
  - Returns None on any failure
- [ ] **Step 2: Fix _apply_mmr() Stage 2**
  - File: `plastic_promise/core/context_engine.py` lines 1164-1173
  - Replace zero-vector dummy with real vector lookup
  - Pre-build `vec_cache = {}` per supply() call
  - Compare against last 5 selected items (not all, for perf)
  - Threshold 0.85, penalty 0.70 (CortexReach defaults)
  - Fallback: if vector unavailable → content-only dedup (Stage 1)
- [ ] **Step 3: Test**
  ```bash
  # Retrieve "memory system" → should not return 3 near-identical results
  ```
- [ ] **Step 4: Commit**

---

## Unit 7: Emoji Detection in Noise Filter

**Priority:** P2 | **Effort:** 0.25h | **Env:** none (always on, negligible overhead)

- [ ] **Step 1: Add emoji regex**
  - File: `plastic_promise/core/noise_filter.py`
  - Pattern: Unicode emoji ranges (emoticons, symbols, transport, flags, dingbats, enclosed)
  - `_EMOJI_PATTERN = re.compile(r"^[...]+$")`
- [ ] **Step 2: Add check in is_noise()**
  - Before length check: `if _EMOJI_PATTERN.match(t.strip()): return True`
  - ~5 lines
- [ ] **Step 3: Test**
  ```python
  assert is_noise("👍") == True
  assert is_noise("fix the bug") == False
  ```
- [ ] **Step 4: Commit**

---

## Unit X: Code Memory Plugin (Deferred)

**Priority:** P2 | **Effort:** 2h | **Env:** `PP_ENABLE_CODE_MEMORY=1` default OFF

Deferred to next phase. Plan already exists at `docs/superpowers/plans/2026-07-02-code-memory-plugin.md`.

---

## Execution Strategy

**Recommended: subagent-driven-development** — 6 units are independent (0,1,2,3,6,7) and can be dispatched in parallel to subagents. Units 4 and 5 share `soul_memory.py` and should run sequentially after the parallel wave.

```
Wave 1 (parallel, 6 subagents):
  Agent A → Unit 0 (Rust merge)
  Agent B → Unit 1 (Query expansion)
  Agent C → Unit 2 (Reranker)
  Agent D → Unit 3 (Decay ranking)
  Agent E → Unit 6 (MMR fix)
  Agent F → Unit 7 (Emoji)

Wave 2 (sequential):
  Unit 4 (L2 tier) → Unit 5 (Periodic cron)
```

## Verification

```bash
# After all units:
python -m pytest tests/ -x -q --tb=short
PP_FORCE_PYTHON_SUPPLY=0 python -c "from plastic_promise.core.context_engine import ContextEngine; ..."
```
