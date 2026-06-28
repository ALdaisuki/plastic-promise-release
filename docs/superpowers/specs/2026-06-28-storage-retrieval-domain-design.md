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
│ Domain Layer (3 traits)                     │
│ DecayModel │ WorthCalculator │ TierManager  │
│     ↓            ↓               ↓          │
│  Weibull      双计数器        升降级规则     │
├─────────────────────────────────────────────┤
│ Orchestration Layer (0 traits)              │
│ HybridRetriever struct                      │
│   Box<dyn VectorIndex>                      │
│   Box<dyn FtsIndex>                         │
│   Box<dyn DecayModel>                       │
│   Box<dyn WorthCalculator>                  │
│   Box<dyn TierManager>                      │
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

### 3.2 Domain Layer (3 traits)

```rust
// domain/mod.rs

pub trait DecayModel {
    /// Compute decay multiplier for a memory given its age and access history.
    /// Returns a multiplier in [0.0, 1.0] where 1.0 = no decay.
    fn compute(&self, created_at: &DateTime<Utc>, last_accessed: &DateTime<Utc>,
               access_count: u32, importance: f64) -> f64;

    /// Compute effective half-life after access reinforcement.
    fn effective_half_life(&self, base_half_life_days: f64, access_count: u32,
                           reinforcement_factor: f64, max_multiplier: f64) -> f64;
}

pub trait WorthCalculator {
    /// Calculate worth_score from success/failure counters.
    /// Uses modified Wilson lower bound for small-N stability.
    fn calculate(&self, success: u32, failure: u32, min_obs: u32) -> f64;

    /// Update counters for a given feedback type.
    fn record_feedback(&self, record: &mut MemoryRecord, feedback_type: FeedbackType);
}

pub trait TierManager {
    /// Classify a memory into a tier — pure function, no side effects.
    fn classify(&self, record: &MemoryRecord) -> Tier;
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
    tier            TEXT DEFAULT 'working',
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
├── mod.rs            # 3 domain traits
├── tier.rs           # Tier enum + TierManager trait
├── decay.rs          # DecayModel trait + Weibull impl
└── worth.rs          # WorthCalculator trait (enhanced from memory_worth.rs)
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
8. `TierManager.classify()` correctly assigns core/working/peripheral based on access_count + worth_score
9. `fusion::rrf_fuse()` produces deterministic merged results
10. `diversity::mmr_dedup()` removes ≥85% cosine-similar duplicates
11. `supply()` signature updated to `(task_description, task_vector, task_type, scope)` — old callers compile-error until updated
12. `EMB_DIM` configurable via env `PP_EMBEDDING_DIM` with default 1536

## 8. Out of Scope

- Python `Embedder` implementation (uses existing `openai` patterns)
- LanceDB repair cron (separate Task in Phase 4 cron restoration)
- Multi-scope authorization logic (scope parameter accepted, `isAccessible()` check stays in Python)
- Cross-encoder reranking (Phase 2, requires HTTP calls from Rust — adds complexity)
- Adaptive retrieval (calls Embedder, evaluates query — stays in Python for now)
- Noise filter (Python-side, before calling supply)
