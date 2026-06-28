# 基础架构设计 — 记忆库 + 向量引擎 + 多层领域

> Date: 2026-06-28
> Status: draft
> Scope: Rust 核心存储/检索/领域模型完整实现，替代骨架中的 HashMap 内存方案

## 1. Goal

将 Plastic Promise 的记忆系统从内存 HashMap 升级为持久化、可检索、带衰减分层的生产级实现。基于 memory-lancedb-pro 的已验证模式，结合 Plastic Promise 独有的 EntityGraph + Memory Worth + 原则注入。

## 2. Architecture Overview

### 2.1 Three-Layer Separation

```
┌─────────────────────────────────────────────┐
│ Storage Layer (4 traits)                    │
│ StorageBackend │ VectorIndex │ FtsIndex     │
│     ↓              ↓            ↓           │
│   SQLite       LanceDB.ANN  LanceDB.FTS    │
├─────────────────────────────────────────────┤
│ Domain Layer (4 traits)                     │
│ DecayModel │ WorthCalculator │ TierManager  │
│     ↓            ↓               ↓          │
│  Weibull      双计数器        4-tier升降级   │
│                                             │
│ MemoryConsolidator ←────────── N.E.K.O 参考 │
│     ↓                                       │
│  记忆合成 (EvolveR 的 trait 化)              │
├─────────────────────────────────────────────┤
│ Orchestration Layer (0 traits)              │
│ HybridRetriever struct                      │
│   Box<dyn VectorIndex>                      │
│   Box<dyn FtsIndex>                         │
│   Box<dyn DecayModel>                       │
│   Box<dyn WorthCalculator>                  │
│   Box<dyn TierManager>                      │
│   Box<dyn MemoryConsolidator>               │
│                                             │
│   retrieve(vec, text, filters) → Vec<ScoredItem> │
└─────────────────────────────────────────────┘
```

### 2.2 Data Flow

```
Python                              Rust
──────                              ────
embedder.embed(query_text)
        │
        ▼  Vec<f32> + text + scope
┌──────────────┐    supply()    ┌──────────────────┐
│ ContextEngine│ ───────────────►│ HybridRetriever   │
│   .supply()  │                │                   │
└──────────────┘                │ VectorIndex ◄─────┤ LDB ANN
        │                       │ FtsIndex    ◄─────┤ LDB BM25
        │                       │ DecayModel  ◄─────┤ Weibull
        ▼                       │ WorthCalc   ◄─────┤ 双计数器
   ContextPack                  │ TierManager ◄─────┤ 升降级
   (returned to Python)         └──────┬───────────┘
                                       │
                                       ▼
                                Vec<ScoredItem>
                                       │
                                       ▼
                              ┌──────────────────┐
                              │ StorageBackend    │
                              │ (SQLite)          │
                              │ EntityGraph       │
                              └──────────────────┘
```

**Pipeline**: Vector Search → BM25 Search → RRF Fusion → Decay Weight → Worth Boost → Length Norm → MMR Diversity → Hard Min Score → Tier Classification

## 3. Trait Definitions

### 3.1 Storage Layer (4 traits)

```rust
// storage/mod.rs

pub trait StorageBackend {
    fn store(&mut self, record: &MemoryRecord) -> Result<String>;   // returns id
    fn get(&self, id: &str) -> Result<Option<MemoryRecord>>;
    fn update(&mut self, id: &str, updates: &UpdateFields) -> Result<bool>;
    fn delete(&mut self, id: &str) -> Result<bool>;
    fn list(&self, filter: &ListFilter) -> Result<Vec<MemoryRecord>>;
    fn stats(&self, scope: Option<&str>) -> Result<MemoryStats>;
}

pub trait VectorIndex {
    fn search(&self, vector: &[f32], k: usize, filter: &SearchFilter) 
        -> Result<Vec<(String, f64)>>;  // (memory_id, cosine_score)
    fn insert(&mut self, id: &str, vector: &[f32], metadata: &IndexMetadata) -> Result<()>;
    fn update(&mut self, id: &str, vector: &[f32], metadata: &IndexMetadata) -> Result<()>;
    fn delete(&mut self, id: &str) -> Result<()>;
}

pub trait FtsIndex {
    fn search(&self, query: &str, k: usize, filter: &SearchFilter)
        -> Result<Vec<(String, f64)>>;  // (memory_id, bm25_score_normalized)
    fn index(&mut self, id: &str, text: &str, metadata: &IndexMetadata) -> Result<()>;
    fn update(&mut self, id: &str, text: &str, metadata: &IndexMetadata) -> Result<()>;
    fn delete(&mut self, id: &str) -> Result<()>;
}

pub trait Embedder {
    // Defined in Python, injected into Rust via PyO3 function pointer / trait object
    fn embed(&self, text: &str) -> Result<Vec<f32>>;
    fn embed_batch(&self, texts: &[String]) -> Result<Vec<Vec<f32>>>;
}
```

Note: `Embedder` is implemented in Python (using `openai` SDK). Rust receives vectors directly via `supply(task_text, task_vector, task_type, scope)`. The trait exists for documentation but the concrete implementation lives in Python.

### 3.2 Domain Layer (4 traits)

Inspired by N.E.K.O's 5-tier memory hierarchy and memory-lancedb-pro's Weibull decay per tier.

```rust
// domain/mod.rs

// ============================================================
// Tier Enum — 4 layers, N.E.K.O-inspired gradient
// ============================================================

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Tier {
    Working,    // 会话窗口内, ttl=1h, max=50, β=1.5
    Recent,     // 跨会话短期, ttl=7d, max=200, β=1.0
    Core,       // 长期核心记忆, ttl=90d, max=2000, β=0.6
    Principle,  // 身份/原则, 永久, 由 EntityGraph 管理, β=0.3
}

impl Tier {
    pub fn base_half_life_days(&self) -> f64 {
        match self {
            Tier::Working => 0.04,    // ~1 hour
            Tier::Recent => 7.0,      // 7 days
            Tier::Core => 90.0,       // 90 days
            Tier::Principle => 365.0, // effectively permanent
        }
    }

    pub fn decay_beta(&self) -> f64 {
        match self {
            Tier::Working => 1.5,     // fast decay
            Tier::Recent => 1.0,      // standard decay
            Tier::Core => 0.6,        // slow decay
            Tier::Principle => 0.3,   // very slow decay
        }
    }

    pub fn max_capacity(&self) -> usize {
        match self {
            Tier::Working => 50,
            Tier::Recent => 200,
            Tier::Core => 2000,
            Tier::Principle => 11,    // exactly 11 core principles
        }
    }
}

// ============================================================
// Traits
// ============================================================

pub trait DecayModel {
    /// Compute decay multiplier for a memory given its tier, age and access history.
    /// Returns a multiplier in [0.0, 1.0] where 1.0 = no decay.
    /// Formula: exp(-age_days / effective_half_life) * (1 - β/3)
    fn compute(&self, tier: Tier, created_at: &DateTime<Utc>,
               last_accessed: &DateTime<Utc>,
               access_count: u32, importance: f64) -> f64;

    /// Compute effective half-life after access reinforcement.
    /// effective = base_half_life * min(1 + reinforcement_factor * access_count, max_multiplier)
    fn effective_half_life(&self, tier: Tier, access_count: u32,
                           reinforcement_factor: f64, max_multiplier: f64) -> f64;
}

pub trait WorthCalculator {
    /// Calculate worth_score from success/failure counters.
    /// Uses modified Wilson lower bound for small-N stability.
    /// ρ ≈ 0.89 correlation with human judgment (academically validated).
    fn calculate(&self, success: u32, failure: u32, min_obs: u32) -> f64;

    /// Update counters for a given feedback type.
    fn record_feedback(&self, record: &mut MemoryRecord, feedback_type: FeedbackType);
}

pub trait TierManager {
    /// Classify a memory into a tier — pure function, no side effects.
    /// Rules:
    /// - access_count >= 10 AND worth_score >= 0.80 AND tier == Recent → promote to Core
    /// - access_count == 0 for 7 days AND tier == Working → demote to Recent
    /// - worth_score < 0.15 for 30 days → demote to Recent (or delete if Working)
    /// - Principles are permanent, never demoted
    fn classify(&self, record: &MemoryRecord) -> Tier;
}

/// Memory Consolidation trait — N.E.K.O's Reflective Memory, EvolveR formalized.
///
/// Periodically synthesizes multiple raw memories into higher-level insights.
/// This is the consolidation step that mirrors N.E.K.O's
/// "raw experiences → summarized reflections → integrated into persona" loop.
pub trait MemoryConsolidator {
    /// Attempt consolidation on a batch of memories.
    /// Returns a synthesized insight if consolidation criteria are met,
    /// or None if no consolidation is currently warranted.
    ///
    /// Consolidation criteria (Plastic Promise):
    /// - At least 5 memories in the same category within a 7-day window
    /// - Average worth_score of source memories >= 0.40
    fn consolidate(&self, memories: &[MemoryRecord]) -> Option<ConsolidatedInsight>;

    /// Get the consolidation interval (how often to check).
    fn interval_hours(&self) -> u32;
}

/// Result of a successful consolidation.
pub struct ConsolidatedInsight {
    pub id: String,
    pub content: String,
    pub source_ids: Vec<String>,    // IDs of memories that contributed
    pub category: String,           // preference / pattern / lesson / rule
    pub confidence: f64,
    pub created_at: DateTime<Utc>,
}
```

### 3.3 Orchestration Layer

```rust
// retrieval/mod.rs — no trait, concrete struct

pub struct HybridRetriever {
    pub vector: Box<dyn VectorIndex>,
    pub fts: Box<dyn FtsIndex>,
    pub decay: Box<dyn DecayModel>,
    pub worth: Box<dyn WorthCalculator>,
    pub tier_mgr: Box<dyn TierManager>,
    pub consolidator: Box<dyn MemoryConsolidator>,

    // Configurable weights
    pub vector_weight: f64,      // default 0.7
    pub bm25_weight: f64,        // default 0.3
    pub hard_min_score: f64,     // default 0.35
    pub length_norm_anchor: usize, // default 500
    pub mmr_threshold: f64,      // default 0.85
    pub candidate_pool_size: usize, // default 20
}

impl HybridRetriever {
    pub fn retrieve(
        &self,
        query_vector: &[f32],
        query_text: &str,
        scope: &str,
        task_type: Option<&str>,
        max_results: usize,
    ) -> Result<Vec<ScoredItem>>;
}
```

## 4. Storage Schema

### 4.1 SQLite (structured primary store)

```sql
CREATE TABLE memories (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    memory_type     TEXT DEFAULT 'experience',
    source          TEXT DEFAULT 'user',
    category        TEXT DEFAULT 'other',
    tier            TEXT DEFAULT 'working',  -- working/recent/core/principle (4-tier)
    importance      REAL DEFAULT 0.7,
    worth_success   INTEGER DEFAULT 0,
    worth_failure   INTEGER DEFAULT 0,
    access_count    INTEGER DEFAULT 0,
    last_accessed_at TEXT,
    created_at      TEXT NOT NULL,
    scope           TEXT DEFAULT 'global',
    metadata        TEXT DEFAULT '{}'
);

CREATE INDEX idx_memories_filter
    ON memories(tier, scope, last_accessed_at, memory_type, category);

CREATE TABLE entities (
    id              TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    activation_weight REAL DEFAULT 0.5,
    attributes      TEXT DEFAULT '{}'
);

CREATE TABLE entity_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node       TEXT NOT NULL REFERENCES entities(id),
    to_node         TEXT NOT NULL REFERENCES entities(id),
    relation_type   TEXT NOT NULL,
    weight          REAL DEFAULT 0.5,
    co_activation_count INTEGER DEFAULT 0
);
```

### 4.2 LanceDB (semantic index)

```
table: memory_vectors
├── memory_id: String          -- FK → SQLite memories.id
├── vector: [f32; EMB_DIM]     -- embedding, dimension configurable
├── text: String               -- FTS indexed copy
├── tier: String               -- scalar filter
├── category: String           -- scalar filter
└── scope: String              -- scalar filter
```

`EMB_DIM` default 1536 (OpenAI text-embedding-3-small), configurable via env `PP_EMBEDDING_DIM`.

## 5. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Embedder stays in Python | Avoid async runtime conflicts with PyO3. Python `openai` SDK is mature. |
| No Retriever trait | Only one strategy (hybrid) exists. Abstract when multiple strategies needed. |
| `TierManager::classify()` is pure | Returns Tier enum. `promote()`/`demote()` are orchestration-layer concerns that call `StorageBackend::update()`. |
| `RankFuser` merged into `fusion.rs` | RRF + dual-channel symbol rules are retrieval pipeline stages, not standalone components. |
| SQLite sync-first dual-write | Write to SQLite synchronously, then LanceDB asynchronously. Cron repair process reconciles inconsistencies. |
| Scope filtering pushed down | Both `VectorIndex::search()` and `FtsIndex::search()` accept `SearchFilter { scope, tier?, category? }`. |
| `WorthCalculator` domain layer | worth_score computed from counters, not stored as column. Keeps calculation logic in one place. |

## 6. File Changes

### New files
```
rust/context-engine-core/src/storage/
├── mod.rs            # 4 storage traits + SearchFilter + IndexMetadata
├── schema.rs         # SQLite DDL + LanceDB table config + EMB_DIM
├── sqlite_impl.rs    # rusqlite StorageBackend implementation
└── lancedb_impl.rs   # lancedb VectorIndex + FtsIndex implementation

rust/context-engine-core/src/retrieval/
├── mod.rs            # HybridRetriever struct
├── embedder.rs       # Embedder trait (Python-side impl)
├── fusion.rs         # RRF fuse + symbol_rules_boost (from rank_fuser.rs)
└── diversity.rs      # mmr_dedup + length_norm + hard_min_score

rust/context-engine-core/src/domain/
├── mod.rs            # 4 domain traits + Tier enum + ConsolidatedInsight
├── tier.rs           # TierManager trait + promotion/demotion rules
├── decay.rs          # DecayModel trait + Weibull impl (per-tier β)
├── worth.rs          # WorthCalculator trait + dual-counter impl
└── consolidator.rs   # MemoryConsolidator trait + EvolveR impl
```

### Modified files
```
rust/context-engine-core/src/lib.rs         # register new modules + EMB_DIM constant
rust/context-engine-core/src/context_engine.rs  # supply() uses HybridRetriever instead of inline HashMap
rust/context-engine-core/src/memory_worth.rs    # enhanced → domain/worth.rs
```

### Removed files (merged elsewhere)
```
rust/context-engine-core/src/rank_fuser.rs  # → retrieval/fusion.rs
```

### Unchanged files
```
rust/context-engine-core/src/entity_graph.rs
rust/context-engine-core/src/source_tracker.rs
rust/context-engine-core/src/association_feedback.rs
rust/context-engine-core/src/principles.rs
```

## 7. Acceptance Criteria

1. `StorageBackend` (SQLite) fully functional — CRUD passes with 1000 records
2. `VectorIndex` (LanceDB) returns correct ANN results, cosine distance accurate to 1e-4
3. `FtsIndex` (LanceDB) returns correct BM25 results for CJK and English queries
4. `HybridRetriever.retrieve()` completes in <200ms with 1000 memories
5. Dual-write: SQLite + LanceDB stay consistent through insert/update/delete cycle
6. `DecayModel` (Weibull) produces expected decay curves: 14-day half-life for medium importance, 30-day for high
7. `WorthCalculator` output matches manual calculation for 20 test cases
8. `TierManager.classify()` correctly assigns tier based on access_count + worth_score: Working(<1h)/Recent(<7d)/Core(<90d)/Principle(permanent)
9. `MemoryConsolidator.consolidate()` triggers when >=5 same-category memories in 7-day window with avg worth >=0.40
10. `decay.compute()` produces differentiated decay per tier: β_working=1.5, β_recent=1.0, β_core=0.6, β_principle=0.3
11. `fusion::rrf_fuse()` produces deterministic merged results from vector + BM25 channels
12. `diversity::mmr_dedup()` removes >=85% cosine-similar duplicates
13. `supply()` signature: `(task_description, task_vector, task_type, scope)` — all four params required
14. `EMB_DIM` configurable via env `PP_EMBEDDING_DIM` with default 1536
15. All 4 tiers have corresponding LanceDB scalar filter values and SQLite tier column values

## 8. Out of Scope

- Python `Embedder` implementation (uses existing `openai` patterns)
- LanceDB repair cron (separate Task in Phase 4 cron restoration)
- Multi-scope authorization logic (scope parameter accepted, `isAccessible()` check stays in Python)
- Cross-encoder reranking (Phase 2, requires HTTP calls from Rust — adds complexity)
- Adaptive retrieval (calls Embedder, evaluates query — stays in Python for now)
- Noise filter (Python-side, before calling supply)

## 9. Inspirations & References

- **memory-lancedb-pro** (CortexReach): Hybrid retrieval pipeline (vector+BM25+RRF+decay+MMR), LanceDB schema, Weibull decay + access reinforcement, noise filtering, adaptive retrieval, multi-scope isolation. 4.4k stars, production-grade OpenClaw plugin.
- **N.E.K.O.** (Project-N-E-K-O): Five-tier memory hierarchy (Working/Recent/Factual/Reflective/Persona), memory consolidation via periodic reflection, multi-process memory server isolation, proactive agent behavior engine. Inspired our 4-tier system and MemoryConsolidator trait. 1.8k stars.
