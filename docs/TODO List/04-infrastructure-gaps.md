# 04 — Infrastructure Gaps & Polish Items

> Covers gaps #8, #9, #10, #11, #12: Session Recovery, Benchmarking, Emoji Detection, Iron Rules, Multi-Provider API

## Gap #8: Session Recovery 🟢 P2

**Current State:** No explicit recovery mechanism. If the MCP server restarts mid-operation:
- In-flight `memory_store` calls are lost
- Half-written LanceDB entries may exist without SQLite counterparts
- Active Hunter Guild tasks remain in `claimed` state indefinitely (until heartbeat timeout)

**CortexReach Approach:** `session-recovery.ts` restores state after interruptions:
- Detects interrupted writes (LanceDB has row but SQLite doesn't)
- Replays pending operations from a write-ahead log
- Reconnects to LanceDB with retry logic

**Proposed Implementation:**

```python
# plastic_promise/core/session_recovery.py

class SessionRecovery:
    """Recover consistent state after MCP server restart.
    
    Recovery steps:
    1. Compare LanceDB rows vs SQLite memories → detect orphans
    2. Replay any pending batch operations
    3. Release stale Hunter Guild task claims
    4. Re-index any memories missing LanceDB vectors
    """
    
    def recover(self, engine: ContextEngine) -> dict:
        """Run full recovery. Returns summary of actions taken."""
```

**Tasks:**
1. Create `plastic_promise/core/session_recovery.py`
2. Call from MCP server startup (after `_ensure_heavy_init()`)
3. Add ghost-vector cleanup (LanceDB has row, SQLite doesn't)
4. Add orphan-memory re-index (SQLite has row, LanceDB doesn't)
5. Release stale Hunter Guild claims (>5 min in `claimed` state)

**Expected Impact:** Zero-intervention recovery from crashes. Prevents accumulated state drift.

---

## Gap #9: Performance Benchmarking 🟢 P2

**Current State:** No benchmarking infrastructure. Performance claims ("<200ms cold start") are manual observations.

**CortexReach Approach:** `benchmark.ts` provides structured performance measurement:
- Retrieval latency (p50/p95/p99)
- Embedding throughput (tokens/second)
- Memory usage (heap, LanceDB index size)
- Regression detection (compare against baseline)

**Proposed Implementation:**

```python
# plastic_promise/core/benchmark.py

class RetrievalBenchmark:
    """Measure and track retrieval performance.
    
    Usage:
        with RetrievalBenchmark() as b:
            results = engine.supply(query, vector, task_type, scope)
        print(b.summary())  # {latency_ms: 45, candidates: 20, layers: {core: 3, ...}}
    """
    
    def __init__(self):
        self.start_time = None
        self.metrics = {}
    
    def __enter__(self):
        self.start_time = time.perf_counter()
        return self
    
    def __exit__(self, *args):
        self.metrics["latency_ms"] = (time.perf_counter() - self.start_time) * 1000
```

**Tasks:**
1. Create `plastic_promise/core/benchmark.py`
2. Add `ContextEngine.supply()` timing instrumentation (opt-in via `PP_BENCHMARK=1`)
3. Track: retrieval latency, candidate count, layer distribution, rerank time
4. Store last 100 measurements in SQLite `benchmark_history` table
5. Add `system(action="benchmark")` MCP command

**Expected Impact:** Data-driven performance optimization. Catch regressions early.

---

## Gap #10: Emoji-Only Detection in Noise Filter 🟢 P2

**Current State:** `noise_filter.py` detects greetings, affirmations, and short boilerplate. Pure emoji messages pass through unfiltered.

**CortexReach Approach:** Regex `^[\p{Emoji}\s]+$` catches emoji-only messages.

**Proposed Implementation:**

```python
# In plastic_promise/core/noise_filter.py

import re

EMOJI_PATTERN = re.compile(
    r"^[\U0001F600-\U0001F64F"     # emoticons
    r"\U0001F300-\U0001F5FF"       # symbols & pictographs
    r"\U0001F680-\U0001F6FF"       # transport & map
    r"\U0001F1E0-\U0001F1FF"       # flags
    r"\U00002702-\U000027B0"       # dingbats
    r"\U000024C2-\U0001F251"       # enclosed characters
    r"\s]+$", flags=re.UNICODE
)

def is_noise(text: str) -> bool:
    # ... existing checks ...
    
    # NEW: Emoji-only detection
    if EMOJI_PATTERN.match(t):
        return True
    
    # ... rest of function ...
```

**Tasks:**
1. Add `EMOJI_PATTERN` to `noise_filter.py`
2. Add check in `is_noise()` before length check
3. Add Chinese emoji equivalents (e.g., `[微笑]`, `[赞]`)

**Expected Impact:** Prevents emoji reactions from polluting the memory pool. ~10 lines of code.

---

## Gap #11: Dual-Layer Iron Rules 🟢 P2

**Current State:** Lessons learned are stored as plain `experience` memories. No structured extraction of decision principles from technical pitfalls.

**CortexReach Approach:** Every lesson learned gets stored twice:
1. **Technical pitfall record**: "Don't use X with Y because Z happens"
2. **Decision principle**: "When choosing between X and Y, prefer X if Z condition holds"

This dual-layer approach is enforced through `AGENTS.md` configuration:
```markdown
## Iron Rules
- Every lesson learned MUST produce:
  1. A technical pitfall record (what went wrong)
  2. A decision principle (what rule to follow going forward)
```

**Proposed Implementation:**

Rather than a code change, this is a **process improvement** to the `step-closure` reflection flow:

```python
# In step-closure handler, after receiving lesson/improvement/root_cause/optimization:

# Extract decision principle from lesson
if lesson and root_cause:
    principle_content = f"When {root_cause}, prefer {optimization} to avoid {lesson}"
    memory_store(
        content=principle_content,
        memory_type="principle",  # NEW: store as derived principle
        tags=["derived_principle", f"source:step_closure"],
    )
```

**Tasks:**
1. Add `derived_principle` extraction logic to step-closure handler
2. Store as `memory_type="principle"` with backlink to source lesson
3. Add to `memory_store` that `principle` type memories bypass standard dedup
4. Update CLAUDE.md section on step-closure to document dual-layer expectation

**Expected Impact:** Decision principles accumulate automatically from experience. Over time, the system develops its own domain-specific principles beyond the 12 core principles.

---

## Gap #12: Multi-Provider Embedding/Reranking API ⚪ P3

**Current State:** All embedding and reranking goes through Ollama (local mxbai-embed-large). Single provider, single point of failure.

**CortexReach Approach:** OpenAI-compatible API abstraction supporting Jina, OpenAI, Voyage, Gemini, Ollama. Each provider independently configured with fallback chains.

**Proposed Implementation:**

```python
# plastic_promise/core/embedder.py — upgrade

class MultiProviderEmbedder:
    """Multi-provider embedding with fallback chain.
    
    Providers (configured via PP_EMBED_PROVIDERS env var):
    - ollama: mxbai-embed-large (local, free, 1024 dim)
    - openai: text-embedding-3-small (1536 dim, paid)
    - jina: jina-embeddings-v3 (1024 dim, free tier)
    - voyage: voyage-3 (1024 dim, paid)
    
    Dimension normalization: all providers normalized to 1024 dim
    (truncate or PCA-reduce as needed).
    """
```

**Tasks (research phase first):**
1. Audit which providers offer free tiers suitable for Plastic Promise's scale
2. Design provider abstraction that doesn't break the 1024-dim assumption
3. Evaluate dimension mismatch handling (truncation vs PCA vs per-provider LanceDB tables)
4. Implement if research confirms clear benefit over Ollama-only

**Expected Impact:** Resilience against Ollama unavailability. Potentially better embedding quality from commercial providers. Higher complexity and cost.

---

## Gap #13: Config-Driven Tier/Decay Engine ⚪ P3

**Current State:** Tier and decay parameters are hard-coded in `constants.py`:
```python
DECAY_CONFIG = {
    "L1": {"beta": 1.5, "half_life_days": 3},
    "L2": {"beta": 1.2, "half_life_days": 7},
    "L3": {"beta": 0.7, "half_life_days": 90},
}
```

**CortexReach Approach:** 43 configuration objects with 117 UI hints, all user-configurable within validated ranges:
```json
{
  "tier": {
    "coreAccessThreshold": 10,
    "coreCompositeThreshold": 0.7,
    "coreImportanceThreshold": 0.8,
    "peripheralCompositeThreshold": 0.15,
    "peripheralAgeDays": 60,
    "workingAccessThreshold": 3,
    "workingCompositeThreshold": 0.4
  },
  "decay": {
    "betaCore": 0.8,
    "betaWorking": 1.0,
    "betaPeripheral": 1.3,
    "coreDecayFloor": 0.9,
    "workingDecayFloor": 0.7,
    "peripheralDecayFloor": 0.5
  }
}
```

**Proposed Implementation:** Move tier/decay parameters from `constants.py` to a JSON config file with schema validation, environment variable overrides, and runtime reload. Low priority because current hard-coded values work well for the project's scale.

---

## Gap #14: Obsidian Vault Sync 🟢 P2

**Current State:** Plastic Promise has `pack_export` (JSON only) and `memory_sync_files` (file → MCP). No markdown export for knowledge management tools.

**CortexReach Approach:** `cli.ts sync obsidian` exports memories as Obsidian-formatted markdown:
- YAML frontmatter with all metadata (category, importance, timestamp, scope)
- Category-to-folder mapping (00-Preferences through 05-Other)
- Slug normalization for filenames
- Full metadata preservation

**Proposed Implementation:**
```bash
python -m plastic_promise export-obsidian --output ./obsidian-vault/
```
Would generate folder structure:
```
obsidian-vault/
  00-Preferences/
  01-Facts/
  02-Decisions/
  03-Entities/
  04-Events/
  05-Patterns/
```

**Expected Impact:** Interop with Obsidian, Notion, and other knowledge management tools.

---

## Gap #15: Memory Compaction (Progressive Summarization) 🟡 P1

**Current State:** `MemoryGC.merge_similar()` exists with cos >= 0.70 threshold. But no LLM-powered merging and no cooldown enforcement.

**CortexReach Approach:** Progressive summarization pipeline:
1. Cluster similar memories (cos >= 0.88)
2. Only memories older than 7 days
3. LLM merges into single coherent entry (abstract + overview + content)
4. Cooldown: 24h between compaction runs for same cluster
5. Old entries archived (not deleted)

This is more aggressive than Plastic Promise's current merge (0.70 threshold vs 0.88 in CortexReach) but with LLM quality control.

**Proposed Implementation:** Upgrade `MemoryGC.merge_similar()`:
- Raise threshold to 0.88 (fewer, higher-quality merges)
- Add cooldown tracking (don't merge same cluster twice in 24h)
- Add LLM merge prompt for high-quality compaction
- Archive old entries instead of deleting

**This is a new P1 gap not in the original 12.**
