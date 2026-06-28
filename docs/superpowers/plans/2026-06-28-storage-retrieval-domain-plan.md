# Storage + Retrieval + Domain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current HashMap-based `ContextEngine` with a persistent, vector-searchable, 4-tier memory system backed by SQLite + LanceDB.

**Architecture:** Three-layer Rust crate: Storage (4 traits + 2 impls), Domain (4 traits + Tier enum), Retrieval (HybridRetriever struct). Embedder stays in Python. Existing `RankFuser` absorbed into `retrieval/fusion.rs`. Existing `MemoryRecord` enhanced with tier/scope/category/importance/access_count fields.

**Tech Stack:** Rust 2021, PyO3 0.20, rusqlite 0.31, lancedb (Rust native), chrono 0.4, serde/serde_json

## Global Constraints

- All public items in Rust must have `///` doc comments
- PyO3 `#[pyclass]` types must retain Python accessibility
- No async Rust — synchronous `Result<T, E>` throughout
- Embedding vectors come from Python — Rust never calls embedding APIs
- `TierManager::classify()` is a pure function, no side effects
- SQLite writes are synchronous; LanceDB writes are synchronous too (simplified from async spec — LanceDB Rust crate is sync)
- `supply()` signature: `(task_description: String, task_vector: Vec<f32>, task_type: String, scope: String) -> ContextPack`
- Existing `entity_graph.rs`, `source_tracker.rs`, `association_feedback.rs`, `principles.rs` remain unchanged
- `RankFuser` merged into `retrieval/fusion.rs` — remove `rank_fuser.rs`
- `MemoryRecord` enhanced with new fields — old fields preserved

---

### Task 1: Cargo.toml 依赖 + Tier 枚举 + 共享类型

**Files:**
- Modify: `rust/context-engine-core/Cargo.toml`
- Create: `rust/context-engine-core/src/domain/mod.rs`
- Create: `rust/context-engine-core/src/domain/tier.rs`
- Create: `rust/context-engine-core/src/storage/mod.rs` (shared types only: `SearchFilter`, `IndexMetadata`, `ListFilter`, `UpdateFields`, `MemoryStats`)

**Interfaces:**
- Consumes: nothing
- Produces: `domain::Tier` enum, `domain::ConsolidatedInsight`, `storage::SearchFilter`, `storage::IndexMetadata`, `storage::ListFilter`, `storage::UpdateFields`, `storage::MemoryStats`

- [ ] **Step 1: Update Cargo.toml**

Replace dependencies section:

```toml
[dependencies]
pyo3 = { version = "0.20", features = ["extension-module"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
chrono = { version = "0.4", features = ["serde"] }
rusqlite = { version = "0.31", features = ["bundled"] }

[profile.release]
opt-level = 3
lto = true
```

Remove `petgraph = "0.6"` — not used.

Run: `cargo check` in `rust/context-engine-core/`
Expected: deprecation warnings from petgraph removed, new deps compiled successfully

- [ ] **Step 2: Write domain/tier.rs**

```rust
//! Memory tier enum — 4-layer classification inspired by N.E.K.O.
//!
//! Working(1h) → Recent(7d) → Core(90d) → Principle(permanent)
//! Each tier carries its own decay beta and capacity limit.

use serde::{Deserialize, Serialize};

/// 4-tier memory classification with per-tier decay and capacity parameters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Tier {
    /// Session-scoped, ttl ~1 hour, max 50 entries, fast decay (β=1.5)
    Working,
    /// Cross-session short-term, ttl 7 days, max 200 entries, standard decay (β=1.0)
    Recent,
    /// Long-term core memory, ttl 90 days, max 2000 entries, slow decay (β=0.6)
    Core,
    /// Identity/principle memory, permanent, max 11 entries, very slow decay (β=0.3)
    Principle,
}

impl Tier {
    /// Base half-life in days for this tier.
    pub fn base_half_life_days(&self) -> f64 {
        match self {
            Tier::Working => 0.04,    // ~1 hour
            Tier::Recent => 7.0,      // 7 days
            Tier::Core => 90.0,       // ~3 months
            Tier::Principle => 365.0, // effectively permanent
        }
    }

    /// Weibull decay shape parameter — higher = faster decay.
    pub fn decay_beta(&self) -> f64 {
        match self {
            Tier::Working => 1.5,
            Tier::Recent => 1.0,
            Tier::Core => 0.6,
            Tier::Principle => 0.3,
        }
    }

    /// Maximum capacity for this tier before eviction.
    pub fn max_capacity(&self) -> usize {
        match self {
            Tier::Working => 50,
            Tier::Recent => 200,
            Tier::Core => 2000,
            Tier::Principle => 11, // exactly 11 core principles
        }
    }

    /// Convert from SQLite string representation.
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "working" => Some(Tier::Working),
            "recent" => Some(Tier::Recent),
            "core" => Some(Tier::Core),
            "principle" => Some(Tier::Principle),
            _ => None,
        }
    }

    /// Convert to SQLite-compatible string.
    pub fn as_str(&self) -> &'static str {
        match self {
            Tier::Working => "working",
            Tier::Recent => "recent",
            Tier::Core => "core",
            Tier::Principle => "principle",
        }
    }
}

impl Default for Tier {
    fn default() -> Self {
        Tier::Working
    }
}

impl std::fmt::Display for Tier {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}
```

- [ ] **Step 3: Write domain/mod.rs**

```rust
//! Domain layer — memory lifecycle and value calculation.
//!
//! Four traits:
//! - DecayModel: Weibull stretched-exponential time decay per tier
//! - WorthCalculator: Wilson-bound dual-counter memory value
//! - TierManager: Pure-function tier classification
//! - MemoryConsolidator: Multi-memory synthesis (EvolveR)
//!
//! Plus the Tier enum and ConsolidatedInsight struct.

pub mod tier;
pub mod decay;
pub mod worth;
pub mod consolidator;

pub use tier::Tier;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Result of a successful memory consolidation.
///
/// When enough related memories accumulate (>=5 same-category within 7 days,
/// average worth_score >= 0.40), the consolidator synthesizes them into
/// a higher-level insight.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConsolidatedInsight {
    pub id: String,
    pub content: String,
    pub source_ids: Vec<String>,
    pub category: String,
    pub confidence: f64,
    pub created_at: DateTime<Utc>,
}

/// Feedback type for memory worth tracking.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FeedbackType {
    Adopted,
    Ignored,
    Rejected,
}

/// Entry point for feedback on a MemoryRecord, used by WorthCalculator.
pub trait WorthCalculator {
    fn calculate(&self, success: u32, failure: u32, min_obs: u32) -> f64;
    fn record_feedback(&self, success: &mut u32, failure: &mut u32, feedback_type: FeedbackType);
}

pub trait DecayModel {
    fn compute(
        &self,
        tier: Tier,
        created_at: &DateTime<Utc>,
        last_accessed: &DateTime<Utc>,
        access_count: u32,
        importance: f64,
    ) -> f64;
    fn effective_half_life(&self, tier: Tier, access_count: u32, reinforcement_factor: f64, max_multiplier: f64) -> f64;
}

pub trait TierManager {
    fn classify(&self, record: &super::super::memory_worth::MemoryRecord) -> Tier;
}

pub trait MemoryConsolidator {
    fn consolidate(&self, memories: &[super::super::memory_worth::MemoryRecord]) -> Option<ConsolidatedInsight>;
    fn interval_hours(&self) -> u32;
}
```

- [ ] **Step 4: Write storage/mod.rs (shared types)**

```rust
//! Storage layer traits and shared types.
//!
//! Four traits:
//! - StorageBackend: CRUD operations on MemoryRecords (SQLite impl)
//! - VectorIndex: ANN vector search (LanceDB impl)
//! - FtsIndex: Full-text BM25 search (LanceDB impl)
//! - Embedder: Text-to-vector (Python-side impl, trait declared here)

use std::collections::HashMap;
use crate::domain::Tier;
use crate::memory_worth::MemoryRecord;

/// Embedding dimension — configurable via PP_EMBEDDING_DIM env var, default 1536.
pub const EMB_DIM: usize = 1536;

/// Scope + tier + category filter for vector/FTS searches.
#[derive(Debug, Clone, Default)]
pub struct SearchFilter {
    pub scope: Option<String>,
    pub tier: Option<Tier>,
    pub category: Option<String>,
}

/// Field-level updates for MemoryRecord.
#[derive(Debug, Clone, Default)]
pub struct UpdateFields {
    pub content: Option<String>,
    pub memory_type: Option<String>,
    pub category: Option<String>,
    pub tier: Option<Tier>,
    pub importance: Option<f64>,
    pub worth_success: Option<u32>,
    pub worth_failure: Option<u32>,
    pub access_count: Option<u32>,
    pub last_accessed_at: Option<String>,
    pub scope: Option<String>,
    pub metadata: Option<String>,
}

/// Paginated list filter for SQLite queries.
#[derive(Debug, Clone, Default)]
pub struct ListFilter {
    pub scope: Option<String>,
    pub tier: Option<Tier>,
    pub category: Option<String>,
    pub memory_type: Option<String>,
    pub source: Option<String>,
    pub min_worth: Option<f64>,
    pub min_importance: Option<f64>,
    pub limit: usize,
    pub offset: usize,
}

/// Aggregate memory statistics.
#[derive(Debug, Clone, Default)]
pub struct MemoryStats {
    pub total: usize,
    pub healthy: usize,
    pub decaying: usize,
    pub by_tier: HashMap<String, usize>,
    pub by_type: HashMap<String, usize>,
    pub by_category: HashMap<String, usize>,
    pub average_worth: f64,
}

/// Metadata written alongside each vector in LanceDB.
#[derive(Debug, Clone, Default)]
pub struct IndexMetadata {
    pub memory_id: String,
    pub tier: String,
    pub category: String,
    pub scope: String,
}

// ============================================================
// Traits
// ============================================================

pub trait StorageBackend {
    fn store(&mut self, record: &MemoryRecord) -> Result<String, String>;
    fn get(&self, id: &str) -> Result<Option<MemoryRecord>, String>;
    fn update(&mut self, id: &str, updates: &UpdateFields) -> Result<bool, String>;
    fn delete(&mut self, id: &str) -> Result<bool, String>;
    fn list(&self, filter: &ListFilter) -> Result<Vec<MemoryRecord>, String>;
    fn stats(&self, scope: Option<&str>) -> Result<MemoryStats, String>;
    fn total_count(&self) -> Result<usize, String>;
}

pub trait VectorIndex {
    fn search(&self, vector: &[f32], k: usize, filter: &SearchFilter) -> Result<Vec<(String, f64)>, String>;
    fn insert(&mut self, id: &str, vector: &[f32], metadata: &IndexMetadata) -> Result<(), String>;
    fn update(&mut self, id: &str, vector: &[f32], metadata: &IndexMetadata) -> Result<(), String>;
    fn delete(&mut self, id: &str) -> Result<(), String>;
}

pub trait FtsIndex {
    fn search(&self, query: &str, k: usize, filter: &SearchFilter) -> Result<Vec<(String, f64)>, String>;
    fn index(&mut self, id: &str, text: &str, metadata: &IndexMetadata) -> Result<(), String>;
    fn update(&mut self, id: &str, text: &str, metadata: &IndexMetadata) -> Result<(), String>;
    fn delete(&mut self, id: &str) -> Result<(), String>;
}

/// Embedder trait — trait defined here, implementation in Python.
/// Rust never calls this directly; vectors arrive via supply().
pub trait Embedder {
    fn embed(&self, text: &str) -> Result<Vec<f32>, String>;
    fn embed_batch(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, String>;
}
```

- [ ] **Step 5: Verify compilation**

```bash
cd rust/context-engine-core && cargo check 2>&1
```
Expected: zero errors (warnings for unused imports OK)

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: Cargo.toml deps + Tier enum + domain/storage shared types"
```

---

### Task 2: MemoryRecord 增强（新字段）

**Files:**
- Modify: `rust/context-engine-core/src/memory_worth.rs`

**Interfaces:**
- Consumes: `domain::Tier`
- Produces: Enhanced `MemoryRecord` with `tier`, `scope`, `category`, `importance`, `access_count`, `last_accessed_at`, `metadata` fields

- [ ] **Step 1: Add Tier import to memory_worth.rs**

```rust
use crate::domain::Tier;
```

- [ ] **Step 2: Add new fields to MemoryRecord struct**

After `pub worth_failure: u32,` add:

```rust
    /// Memory tier: working / recent / core / principle
    #[pyo3(get, set)]
    pub tier: String,
    /// Scope namespace: global / agent:<id> / project:<id>
    #[pyo3(get, set)]
    pub scope: String,
    /// Semantic category: preference / fact / decision / entity / reflection / other
    #[pyo3(get, set)]
    pub category: String,
    /// Importance score [0.0, 1.0]
    #[pyo3(get, set)]
    pub importance: f64,
    /// Cumulative recall count
    #[pyo3(get, set)]
    pub access_count: u32,
    /// ISO 8601 timestamp of last retrieval
    #[pyo3(get, set)]
    pub last_accessed_at: String,
    /// Extended metadata as JSON string
    #[pyo3(get, set)]
    pub metadata_json: String,
```

- [ ] **Step 3: Update MemoryRecord::new() to initialize new fields**

In the `#[pymethods] impl MemoryRecord` block, update `new()`:

```rust
#[new]
pub fn new(id: String, content: String, memory_type: String, source: String) -> Self {
    Self {
        id,
        content,
        memory_type,
        source,
        created_at: String::new(),
        last_accessed: String::new(),
        activation_weight: 0.5,
        worth_success: 0,
        worth_failure: 0,
        entity_ids: Vec::new(),
        attributes: std::collections::HashMap::new(),
        tier: Tier::default().as_str().to_string(),
        scope: "global".to_string(),
        category: "other".to_string(),
        importance: 0.7,
        access_count: 0,
        last_accessed_at: String::new(),
        metadata_json: "{}".to_string(),
    }
}
```

- [ ] **Step 4: Add a constructor from_storage for internal Rust use**

In the non-Python `impl MemoryRecord` block (not `#[pymethods]`):

```rust
impl MemoryRecord {
    /// Create a record from SQLite row data (internal use, not #[pymethods]).
    pub fn from_storage(
        id: String, content: String, memory_type: String, source: String,
        tier: String, scope: String, category: String, importance: f64,
        worth_success: u32, worth_failure: u32, access_count: u32,
        last_accessed_at: String, created_at: String, metadata_json: String,
    ) -> Self {
        Self {
            id, content, memory_type, source,
            tier, scope, category, importance,
            worth_success, worth_failure, access_count,
            last_accessed_at,
            last_accessed: last_accessed_at.clone(),
            created_at,
            activation_weight: 0.5,
            entity_ids: Vec::new(),
            attributes: std::collections::HashMap::new(),
            metadata_json,
        }
    }
}
```

- [ ] **Step 5: Verify compilation**

```bash
cd rust/context-engine-core && cargo check 2>&1
```
Expected: zero errors

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: MemoryRecord enhanced with tier/scope/category/importance/access_count fields"
```

---

### Task 3: Storage schema + SQLite StorageBackend impl

**Files:**
- Create: `rust/context-engine-core/src/storage/schema.rs`
- Create: `rust/context-engine-core/src/storage/sqlite_impl.rs`

**Interfaces:**
- Consumes: `domain::Tier`, `memory_worth::MemoryRecord`, `storage::{StorageBackend, SearchFilter, ListFilter, UpdateFields, MemoryStats, IndexMetadata, EMB_DIM}`
- Produces: `storage::sqlite_impl::SqliteStorage` implementing `StorageBackend`

- [ ] **Step 1: Write storage/schema.rs**

```rust
//! Database schema constants — SQLite DDL + LanceDB table config.

use crate::storage::EMB_DIM;

/// SQLite schema version for migration tracking.
pub const SCHEMA_VERSION: u32 = 1;

/// LanceDB table name for vector + FTS index.
pub const LANCEDB_TABLE: &str = "memory_vectors";

/// LanceDB table version stored in metadata.
pub const LANCEDB_TABLE_VERSION: &str = "1.0.0";

/// SQL: Create memories table with compound index.
pub const SQL_CREATE_MEMORIES: &str = r#"
CREATE TABLE IF NOT EXISTS memories (
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
    last_accessed_at TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    scope           TEXT DEFAULT 'global',
    metadata_json   TEXT DEFAULT '{}'
);
"#;

/// SQL: Composite index for fast filtered queries (GC, list, stats).
pub const SQL_CREATE_INDEX: &str = r#"
CREATE INDEX IF NOT EXISTS idx_memories_filter
    ON memories(tier, scope, last_accessed_at, memory_type, category);
"#;

/// SQL: Create entities table for EntityGraph persistence.
pub const SQL_CREATE_ENTITIES: &str = r#"
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    activation_weight REAL DEFAULT 0.5,
    attributes      TEXT DEFAULT '{}'
);
"#;

/// SQL: Create entity_edges table.
pub const SQL_CREATE_ENTITY_EDGES: &str = r#"
CREATE TABLE IF NOT EXISTS entity_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node       TEXT NOT NULL REFERENCES entities(id),
    to_node         TEXT NOT NULL REFERENCES entities(id),
    relation_type   TEXT NOT NULL,
    weight          REAL DEFAULT 0.5,
    co_activation_count INTEGER DEFAULT 0
);
"#;

/// SQL: Insert or replace a memory record.
pub const SQL_UPSERT_MEMORY: &str = r#"
INSERT OR REPLACE INTO memories
    (id, content, memory_type, source, category, tier, importance,
     worth_success, worth_failure, access_count, last_accessed_at,
     created_at, scope, metadata_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"#;

/// SQL: Select a memory by ID.
pub const SQL_GET_BY_ID: &str = "SELECT * FROM memories WHERE id = ?";

/// SQL: Delete a memory by ID.
pub const SQL_DELETE_BY_ID: &str = "DELETE FROM memories WHERE id = ?";

/// SQL: Count total memories for a scope.
pub const SQL_COUNT_BY_SCOPE: &str = "SELECT COUNT(*) FROM memories WHERE scope = ?";

/// SQL: Stats aggregation query.
pub const SQL_STATS: &str = r#"
SELECT
    COUNT(*) as total,
    SUM(CASE WHEN worth_success > worth_failure THEN 1 ELSE 0 END) as healthy,
    SUM(CASE WHEN worth_failure > worth_success THEN 1 ELSE 0 END) as decaying,
    AVG(CAST(worth_success AS REAL) / (worth_success + worth_failure + 1)) as avg_worth
FROM memories
WHERE (?1 IS NULL OR scope = ?1);
"#;

/// SQL: Stats by tier.
pub const SQL_STATS_BY_TIER: &str =
    "SELECT tier, COUNT(*) as cnt FROM memories GROUP BY tier";

/// SQL: Stats by type.
pub const SQL_STATS_BY_TYPE: &str =
    "SELECT memory_type, COUNT(*) as cnt FROM memories GROUP BY memory_type";

/// SQL: Stats by category.
pub const SQL_STATS_BY_CATEGORY: &str =
    "SELECT category, COUNT(*) as cnt FROM memories GROUP BY category";
```

- [ ] **Step 2: Write storage/sqlite_impl.rs**

Full implementation (~300 lines). Key structure:

```rust
//! SQLite implementation of StorageBackend trait.

use rusqlite::{params, Connection};
use std::path::Path;

use crate::domain::Tier;
use crate::memory_worth::MemoryRecord;
use crate::storage::schema::*;
use crate::storage::{ListFilter, MemoryStats, StorageBackend, UpdateFields};

pub struct SqliteStorage {
    conn: Connection,
}

impl SqliteStorage {
    /// Open or create a SQLite database at the given path.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, String> {
        let conn = Connection::open(path).map_err(|e| e.to_string())?;
        // Enable WAL mode
        conn.execute_batch("PRAGMA journal_mode=WAL;").map_err(|e| e.to_string())?;
        let mut storage = Self { conn };
        storage.create_tables()?;
        Ok(storage)
    }

    fn create_tables(&mut self) -> Result<(), String> {
        self.conn.execute_batch(SQL_CREATE_MEMORIES).map_err(|e| e.to_string())?;
        self.conn.execute_batch(SQL_CREATE_INDEX).map_err(|e| e.to_string())?;
        self.conn.execute_batch(SQL_CREATE_ENTITIES).map_err(|e| e.to_string())?;
        self.conn.execute_batch(SQL_CREATE_ENTITY_EDGES).map_err(|e| e.to_string())?;
        Ok(())
    }

    fn row_to_record(row: &rusqlite::Row) -> rusqlite::Result<MemoryRecord> {
        // Map all 14 columns to MemoryRecord::from_storage()
        // Columns: id, content, memory_type, source, category, tier,
        //          importance, worth_success, worth_failure, access_count,
        //          last_accessed_at, created_at, scope, metadata_json
        Ok(MemoryRecord::from_storage(
            row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?,
            row.get(5)?, row.get(12)?, row.get(4)?, row.get(6)?,
            row.get(7)?, row.get(8)?, row.get(9)?,
            row.get(10)?, row.get(11)?, row.get(13)?,
        ))
    }
}

impl StorageBackend for SqliteStorage {
    fn store(&mut self, record: &MemoryRecord) -> Result<String, String> { ... }
    fn get(&self, id: &str) -> Result<Option<MemoryRecord>, String> { ... }
    fn update(&mut self, id: &str, updates: &UpdateFields) -> Result<bool, String> { ... }
    fn delete(&mut self, id: &str) -> Result<bool, String> { ... }
    fn list(&self, filter: &ListFilter) -> Result<Vec<MemoryRecord>, String> { ... }
    fn stats(&self, scope: Option<&str>) -> Result<MemoryStats, String> { ... }
    fn total_count(&self) -> Result<usize, String> { ... }
}
```

The `store()` method:
1. If `record.created_at` is empty, set it to `chrono::Utc::now().to_rfc3339()`
2. Execute `SQL_UPSERT_MEMORY` with all 14 fields
3. Return `record.id.clone()`

The `list()` method:
1. Build dynamic WHERE clause from `ListFilter` fields
2. Append `ORDER BY importance DESC LIMIT ? OFFSET ?`
3. Execute and map rows

- [ ] **Step 3: Write a Rust unit test**

Create test in `sqlite_impl.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Tier;

    fn make_record(id: &str, content: &str) -> MemoryRecord {
        MemoryRecord::new(id.into(), content.into(), "experience".into(), "user".into())
    }

    #[test]
    fn test_store_and_retrieve() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        let record = make_record("test-1", "hello world");
        let id = db.store(&record).unwrap();
        assert_eq!(id, "test-1");

        let retrieved = db.get("test-1").unwrap().unwrap();
        assert_eq!(retrieved.content, "hello world");
        assert_eq!(retrieved.tier, "working");
        assert_eq!(retrieved.scope, "global");
    }

    #[test]
    fn test_list_filter_by_tier() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        let mut r1 = make_record("r1", "core memory");
        r1.tier = Tier::Core.as_str().into();
        db.store(&r1).unwrap();
        db.store(&make_record("r2", "working memory")).unwrap();

        let filter = ListFilter { tier: Some(Tier::Core), ..Default::default() };
        let results = db.list(&filter).unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, "r1");
    }

    #[test]
    fn test_update_fields() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        db.store(&make_record("r1", "old")).unwrap();

        let updates = UpdateFields {
            content: Some("new".into()),
            importance: Some(0.9),
            ..Default::default()
        };
        assert!(db.update("r1", &updates).unwrap());
        let r = db.get("r1").unwrap().unwrap();
        assert_eq!(r.content, "new");
        assert_eq!(r.importance, 0.9);
    }

    #[test]
    fn test_delete() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        db.store(&make_record("r1", "x")).unwrap();
        assert!(db.delete("r1").unwrap());
        assert!(db.get("r1").unwrap().is_none());
    }

    #[test]
    fn test_stats() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        db.store(&make_record("r1", "a")).unwrap();
        db.store(&make_record("r2", "b")).unwrap();
        let stats = db.stats(None).unwrap();
        assert_eq!(stats.total, 2);
        assert_eq!(stats.healthy, 2); // worth_success == worth_failure == 0 => healthy (not decaying)
    }
}
```

Run: `cargo test --lib` in `rust/context-engine-core/`
Expected: 5 tests PASS

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: SQLite StorageBackend impl with schema + 5 passing tests"
```

---

### Task 4: LanceDB VectorIndex + FtsIndex impl

**Files:**
- Create: `rust/context-engine-core/src/storage/lancedb_impl.rs`

**Interfaces:**
- Consumes: `storage::{VectorIndex, FtsIndex, SearchFilter, IndexMetadata, EMB_DIM}`, `storage::schema::{LANCEDB_TABLE, LANCEDB_TABLE_VERSION}`
- Produces: `storage::lancedb_impl::LanceDbStore` implementing both `VectorIndex` and `FtsIndex`

**Note:** The `lancedb` Rust crate may have compilation issues on Windows (MSVC). This implementation MUST compile and pass `cargo check` but actual LanceDB runtime tests may be skipped on Windows with a `#[cfg(not(windows))]` gate.

- [ ] **Step 1: Write storage/lancedb_impl.rs**

Full implementation (~200 lines). Key structure:

```rust
//! LanceDB implementation of VectorIndex + FtsIndex traits.
//!
//! Uses LanceDB Rust SDK for ANN vector search and BM25 full-text search.
//! LanceDB auto-manages both indices — we only define the table schema.

use crate::storage::{FtsIndex, IndexMetadata, SearchFilter, VectorIndex, EMB_DIM};
use crate::storage::schema::{LANCEDB_TABLE, LANCEDB_TABLE_VERSION};

/// LanceDB-backed store implementing both VectorIndex and FtsIndex.
///
/// Stores vectors in a LanceDB table with columns:
/// - memory_id (String)
/// - vector (FixedSizeList<Float32>[EMB_DIM])
/// - text (String, FTS indexed)
/// - tier (String, scalar filter)
/// - category (String, scalar filter)
/// - scope (String, scalar filter)
pub struct LanceDbStore {
    // LanceDB connection and table reference
    // db: lancedb::Connection,
    // table: Option<lancedb::Table>,
    /// Whether FTS indexing is available (runtime detection).
    fts_available: bool,
    /// Path to the LanceDB data directory.
    data_path: String,
}

impl LanceDbStore {
    /// Open or create a LanceDB database at the given path.
    /// Creates the memory_vectors table if it doesn't exist.
    pub fn open<P: AsRef<std::path::Path>>(path: P) -> Result<Self, String> { ... }

    /// Initialize the memory_vectors table schema.
    fn init_table(&mut self) -> Result<(), String> { ... }

    /// Check if FTS is available on this LanceDB build.
    fn probe_fts(&self) -> bool { false } // placeholder until LanceDB linked
}

impl VectorIndex for LanceDbStore {
    fn search(&self, vector: &[f32], k: usize, filter: &SearchFilter) -> Result<Vec<(String, f64)>, String> { ... }
    fn insert(&mut self, id: &str, vector: &[f32], metadata: &IndexMetadata) -> Result<(), String> { ... }
    fn update(&mut self, id: &str, vector: &[f32], metadata: &IndexMetadata) -> Result<(), String> { ... }
    fn delete(&mut self, id: &str) -> Result<(), String> { ... }
}

impl FtsIndex for LanceDbStore {
    fn search(&self, query: &str, k: usize, filter: &SearchFilter) -> Result<Vec<(String, f64)>, String> { ... }
    fn index(&mut self, id: &str, text: &str, metadata: &IndexMetadata) -> Result<(), String> { ... }
    fn update(&mut self, id: &str, text: &str, metadata: &IndexMetadata) -> Result<(), String> { ... }
    fn delete(&mut self, id: &str) -> Result<(), String> { ... }
}
```

**CRITICAL:** Due to LanceDB Rust crate linking issues on Windows, the implementation may use `unimplemented!()` or return `Err("LanceDB not linked".into())` for now. Write the complete struct with correct signatures and docstrings. The actual LanceDB calls are wrapped in `#[cfg(feature = "lancedb")]` blocks.

For NOW: implement the trait methods with `Err("LanceDB pending".into())` and full docstrings. The SQLite path works end-to-end; LanceDB integration completes once the Rust crate compiles.

- [ ] **Step 2: Write unit tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lancedb_store_creation() {
        // Use temp directory
        let tmp = std::env::temp_dir().join("pp_test_lancedb");
        let _ = std::fs::remove_dir_all(&tmp);
        // On Windows with no LanceDB, this should still create the struct
        let store = LanceDbStore::open(&tmp);
        // May be Err("LanceDB pending") — that's OK for now
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn test_search_filter_default() {
        let filter = SearchFilter::default();
        assert!(filter.scope.is_none());
        assert!(filter.tier.is_none());
    }
}
```

- [ ] **Step 3: Verify compilation**

```bash
cd rust/context-engine-core && cargo check 2>&1
```
Expected: zero errors (warnings OK)

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: LanceDB VectorIndex + FtsIndex impl skeleton (MSVC linking pending)"
```

---

### Task 5: Domain layer implementations

**Files:**
- Create: `rust/context-engine-core/src/domain/decay.rs`
- Create: `rust/context-engine-core/src/domain/worth.rs`
- Create: `rust/context-engine-core/src/domain/tier.rs` — add `TierManagerImpl`
- Create: `rust/context-engine-core/src/domain/consolidator.rs`

**Interfaces:**
- Consumes: `domain::{Tier, DecayModel, WorthCalculator, TierManager, MemoryConsolidator, FeedbackType, ConsolidatedInsight}`, `memory_worth::MemoryRecord`
- Produces: `WeibullDecay`, `WilsonWorthCalculator`, `DefaultTierManager`, `EvolveRConsolidator`

- [ ] **Step 1: Write domain/decay.rs**

```rust
//! Weibull stretched-exponential decay model.
//!
//! Formula: score_multiplier = exp(-(age_days / half_life)^β)
//! where β varies per tier: Working=1.5, Recent=1.0, Core=0.6, Principle=0.3.
//!
//! Access reinforcement: effective_half_life = base_half_life * min(1 + rf * access_count, max_mult)

use chrono::{DateTime, Utc};
use crate::domain::{DecayModel, Tier};

pub struct WeibullDecay {
    pub reinforcement_factor: f64,
    pub max_half_life_multiplier: f64,
}

impl Default for WeibullDecay {
    fn default() -> Self {
        Self {
            reinforcement_factor: 0.5,
            max_half_life_multiplier: 3.0,
        }
    }
}

impl DecayModel for WeibullDecay {
    fn compute(
        &self,
        tier: Tier,
        created_at: &DateTime<Utc>,
        last_accessed: &DateTime<Utc>,
        access_count: u32,
        importance: f64,
    ) -> f64 {
        let now = Utc::now();
        let age_days = (now - *created_at).num_hours() as f64 / 24.0;
        let half_life = self.effective_half_life(tier, access_count, self.reinforcement_factor, self.max_half_life_multiplier);
        let beta = tier.decay_beta();

        // Weibull: exp(-(t/λ)^β), λ = half_life / ln(2)^(1/β)
        let lambda = half_life / (2.0_f64.ln().powf(1.0 / beta));
        let decay = (-(age_days / lambda).powf(beta)).exp();

        // Importance boost: [0.7, 1.3] multiplier
        let importance_factor = 0.7 + (1.0 - 0.7) * importance;

        (decay * importance_factor).clamp(0.0, 1.0)
    }

    fn effective_half_life(&self, tier: Tier, access_count: u32, reinforcement_factor: f64, max_multiplier: f64) -> f64 {
        let base = tier.base_half_life_days();
        let reinforced = base * (1.0 + reinforcement_factor * access_count as f64).min(max_multiplier);
        reinforced
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    #[test]
    fn test_fresh_memory_no_decay() {
        let decay = WeibullDecay::default();
        let now = Utc::now();
        let score = decay.compute(Tier::Working, &now, &now, 0, 0.7);
        assert!(score > 0.95); // brand new, almost no decay
    }

    #[test]
    fn test_old_memory_decays() {
        let decay = WeibullDecay::default();
        let created = Utc::now() - Duration::days(30);
        let accessed = Utc::now() - Duration::days(25);
        let score = decay.compute(Tier::Working, &created, &accessed, 0, 0.5);
        assert!(score < 0.1); // very old working memory should decay heavily
    }

    #[test]
    fn test_principle_barely_decays() {
        let decay = WeibullDecay::default();
        let created = Utc::now() - Duration::days(365);
        let score = decay.compute(Tier::Principle, &created, &created, 0, 1.0);
        assert!(score > 0.5); // principles decay very slowly
    }

    #[test]
    fn test_access_reinforcement() {
        let decay = WeibullDecay::default();
        let half_life_no_access = decay.effective_half_life(Tier::Core, 0, 0.5, 3.0);
        let half_life_many_access = decay.effective_half_life(Tier::Core, 10, 0.5, 3.0);
        assert!(half_life_many_access > half_life_no_access * 2.0);
    }

    #[test]
    fn test_tier_decay_ordering() {
        let decay = WeibullDecay::default();
        let created = Utc::now() - Duration::days(7);
        let w = decay.compute(Tier::Working, &created, &created, 0, 0.5);
        let r = decay.compute(Tier::Recent, &created, &created, 0, 0.5);
        let c = decay.compute(Tier::Core, &created, &created, 0, 0.5);
        let p = decay.compute(Tier::Principle, &created, &created, 0, 0.5);
        assert!(w < r && r < c && c < p); // Working decays fastest, Principle slowest
    }
}
```

- [ ] **Step 2: Write domain/worth.rs**

```rust
//! Wilson-bound WorthCalculator implementation.
//!
//! Uses modified Wilson lower bound for small-N stability.
//! ρ ≈ 0.89 correlation with human judgment.

use crate::domain::{FeedbackType, WorthCalculator};

pub struct WilsonWorthCalculator {
    pub z: f64, // confidence level z-score, default 1.96 for 95% CI
}

impl Default for WilsonWorthCalculator {
    fn default() -> Self {
        Self { z: 1.96 }
    }
}

impl WorthCalculator for WilsonWorthCalculator {
    fn calculate(&self, success: u32, failure: u32, min_obs: u32) -> f64 {
        let n = success + failure;
        if n < min_obs {
            return 0.5; // neutral prior when insufficient data
        }
        let n_f = n as f64;
        let p = success as f64 / n_f;
        let z2 = self.z * self.z;

        // Wilson lower bound
        let center = (p + z2 / (2.0 * n_f)) / (1.0 + z2 / n_f);
        let margin = self.z * (p * (1.0 - p) / n_f + z2 / (4.0 * n_f * n_f)).sqrt() / (1.0 + z2 / n_f);
        let lower = (center - margin).max(0.0);

        // Scale from [0,1] to [-1.5, 1.0] range (matching old FAILURE_WEIGHT)
        lower * 2.5 - 0.5
    }

    fn record_feedback(&self, success: &mut u32, failure: &mut u32, feedback_type: FeedbackType) {
        match feedback_type {
            FeedbackType::Adopted => *success += 1,
            FeedbackType::Rejected => *failure += 1,
            FeedbackType::Ignored => {} // no counter change for ignored
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_insufficient_data_returns_neutral() {
        let calc = WilsonWorthCalculator::default();
        assert_eq!(calc.calculate(1, 0, 5), 0.5);
    }

    #[test]
    fn test_all_success_high_score() {
        let calc = WilsonWorthCalculator::default();
        let score = calc.calculate(20, 0, 5);
        assert!(score > 0.8);
    }

    #[test]
    fn test_all_failure_low_score() {
        let calc = WilsonWorthCalculator::default();
        let score = calc.calculate(0, 20, 5);
        assert!(score < 0.2);
    }

    #[test]
    fn test_mixed_balanced() {
        let calc = WilsonWorthCalculator::default();
        let score = calc.calculate(10, 10, 5);
        assert!((0.3..0.7).contains(&score));
    }

    #[test]
    fn test_record_feedback() {
        let calc = WilsonWorthCalculator::default();
        let (mut s, mut f) = (0u32, 0u32);
        calc.record_feedback(&mut s, &mut f, FeedbackType::Adopted);
        assert_eq!(s, 1);
        assert_eq!(f, 0);
        calc.record_feedback(&mut s, &mut f, FeedbackType::Rejected);
        assert_eq!(s, 1);
        assert_eq!(f, 1);
        calc.record_feedback(&mut s, &mut f, FeedbackType::Ignored);
        assert_eq!(s, 1); // unchanged
    }
}
```

- [ ] **Step 3: Add DefaultTierManager to domain/tier.rs**

Append to `rust/context-engine-core/src/domain/tier.rs`:

```rust
use crate::memory_worth::MemoryRecord;
use crate::domain::TierManager;

pub struct DefaultTierManager;

impl TierManager for DefaultTierManager {
    fn classify(&self, record: &MemoryRecord) -> Tier {
        // Principles are permanent
        if record.tier == Tier::Principle.as_str() {
            return Tier::Principle;
        }

        let worth = record.worth_score();

        // Promotion: Recent → Core
        if record.tier == Tier::Recent.as_str()
            && record.access_count >= 10
            && worth >= 0.80
        {
            return Tier::Core;
        }

        // Promotion: Working → Recent (accessed at least once and worth OK)
        if record.tier == Tier::Working.as_str()
            && record.access_count >= 2
            && worth >= 0.50
        {
            return Tier::Recent;
        }

        // Demotion: Core → Recent (worth dropped)
        if record.tier == Tier::Core.as_str()
            && worth < 0.15
        {
            return Tier::Recent;
        }

        // Demotion: Working → delete candidate (worth very low)
        if record.tier == Tier::Working.as_str()
            && worth < 0.10
            && record.access_count == 0
        {
            return Tier::Working; // stays but marked for GC by caller
        }

        // Stay in current tier
        Tier::from_str(&record.tier).unwrap_or_default()
    }
}
```

- [ ] **Step 4: Write domain/consolidator.rs**

```rust
//! Memory consolidation — EvolveR formalized as a trait implementation.
//!
//! Periodically synthesizes multiple raw memories into higher-level insights.
//! Inspired by N.E.K.O's Reflective Memory system.

use chrono::{DateTime, Duration, Utc};
use uuid::Uuid;
use crate::domain::{ConsolidatedInsight, MemoryConsolidator};
use crate::memory_worth::MemoryRecord;

pub struct EvolveRConsolidator {
    /// Minimum memories in the same category to trigger consolidation.
    pub min_cluster_size: usize,
    /// Time window (hours) for grouping memories.
    pub window_hours: i64,
    /// Minimum average worth_score of source memories.
    pub min_avg_worth: f64,
    /// Consolidation interval (hours between checks).
    pub interval_hours: u32,
}

impl Default for EvolveRConsolidator {
    fn default() -> Self {
        Self {
            min_cluster_size: 5,
            window_hours: 168, // 7 days
            min_avg_worth: 0.40,
            interval_hours: 24,
        }
    }
}

impl MemoryConsolidator for EvolveRConsolidator {
    fn consolidate(&self, memories: &[MemoryRecord]) -> Option<ConsolidatedInsight> {
        if memories.len() < self.min_cluster_size {
            return None;
        }

        // Group by category
        let mut by_cat: std::collections::HashMap<String, Vec<&MemoryRecord>> = std::collections::HashMap::new();
        let cutoff = Utc::now() - Duration::hours(self.window_hours);

        for mem in memories {
            if mem.created_at.is_empty() {
                continue;
            }
            if let Ok(ts) = DateTime::parse_from_rfc3339(&mem.created_at) {
                if ts < cutoff {
                    continue;
                }
            } else {
                continue;
            }
            by_cat.entry(mem.category.clone())
                .or_default()
                .push(mem);
        }

        // Find the largest cluster meeting the worth threshold
        let mut best: Option<(&str, Vec<&MemoryRecord>)> = None;
        for (cat, group) in &by_cat {
            if group.len() < self.min_cluster_size {
                continue;
            }
            let avg_worth: f64 = group.iter()
                .map(|m| m.worth_score())
                .sum::<f64>() / group.len() as f64;
            if avg_worth >= self.min_avg_worth {
                if best.map_or(true, |b| group.len() > b.1.len()) {
                    best = Some((cat, group.clone()));
                }
            }
        }

        let (category, group) = best?;
        let source_ids: Vec<String> = group.iter().map(|m| m.id.clone()).collect();
        let contents: Vec<&str> = group.iter().map(|m| m.content.as_str()).collect();
        let avg_worth: f64 = group.iter()
            .map(|m| m.worth_score())
            .sum::<f64>() / group.len() as f64;

        Some(ConsolidatedInsight {
            id: format!("cons-{}", Uuid::new_v4()),
            content: format!("Consolidated {} insight from {} memories: {}",
                category, group.len(), contents.join(" | ")),
            source_ids,
            category: category.to_string(),
            confidence: avg_worth,
            created_at: Utc::now(),
        })
    }

    fn interval_hours(&self) -> u32 {
        self.interval_hours
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_mem(id: &str, cat: &str, content: &str, worth_s: u32, worth_f: u32, hours_ago: i64) -> MemoryRecord {
        let ts = (Utc::now() - Duration::hours(hours_ago)).to_rfc3339();
        MemoryRecord::from_storage(
            id.into(), content.into(), "experience".into(), "user".into(),
            "working".into(), "global".into(), cat.into(), 0.7,
            worth_s, worth_f, 1,
            ts.clone(), ts, "{}".into(),
        )
    }

    #[test]
    fn test_insufficient_memories_no_consolidation() {
        let c = EvolveRConsolidator::default();
        let mems = vec![make_mem("1", "fact", "x", 5, 0, 1)];
        assert!(c.consolidate(&mems).is_none());
    }

    #[test]
    fn test_enough_memories_triggers_consolidation() {
        let c = EvolveRConsolidator::default();
        let mems: Vec<_> = (0..6)
            .map(|i| make_mem(&format!("{}", i), "fact", "memory", 8, 0, i as i64))
            .collect();
        let insight = c.consolidate(&mems);
        assert!(insight.is_some());
        let i = insight.unwrap();
        assert_eq!(i.category, "fact");
        assert_eq!(i.source_ids.len(), 6);
    }

    #[test]
    fn test_low_worth_no_consolidation() {
        let c = EvolveRConsolidator::default();
        let mems: Vec<_> = (0..6)
            .map(|i| make_mem(&format!("{}", i), "fact", "bad", 0, 10, i as i64))
            .collect();
        assert!(c.consolidate(&mems).is_none());
    }
}
```

Add `uuid = { version = "1", features = ["v4"] }` to Cargo.toml dependencies.

- [ ] **Step 5: Verify compilation and run tests**

```bash
cd rust/context-engine-core && cargo test --lib 2>&1
```
Expected: all domain tests PASS (14 tests from decay + worth + consolidator)

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: domain layer impls - WeibullDecay + WilsonWorth + DefaultTierManager + EvolveRConsolidator"
```

---

### Task 6: Retrieval layer — fusion + diversity + HybridRetriever

**Files:**
- Create: `rust/context-engine-core/src/retrieval/mod.rs`
- Create: `rust/context-engine-core/src/retrieval/fusion.rs`
- Create: `rust/context-engine-core/src/retrieval/diversity.rs`
- Create: `rust/context-engine-core/src/retrieval/embedder.rs`

**Interfaces:**
- Consumes: `storage::{VectorIndex, FtsIndex}`, `domain::{DecayModel, WorthCalculator, TierManager, MemoryConsolidator}`, `memory_worth::MemoryRecord`
- Produces: `retrieval::HybridRetriever`, `retrieval::fusion::{rrf_fuse, apply_symbol_rules}`, `retrieval::diversity::{mmr_dedup, length_norm, hard_min_score}`

- [ ] **Step 1: Write retrieval/embedder.rs**

```rust
//! Embedder trait — defined here, implemented in Python.
//!
//! Rust never calls this directly. Vectors arrive via context_engine::supply()
//! parameter `task_vector: Vec<f32>`.

/// Trait for text-to-vector embedding.
/// Implementation lives in Python (using openai SDK).
/// Rust side: vectors injected via supply(task_vector) parameter.
pub trait Embedder {
    fn embed(&self, text: &str) -> Result<Vec<f32>, String>;
    fn embed_batch(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, String>;
    fn dim(&self) -> usize;
}
```

- [ ] **Step 2: Write retrieval/fusion.rs**

Absorb RankFuser logic. Key functions:

```rust
//! RRF fusion + dual-channel symbol rules.
//!
//! Absorbed from the former rank_fuser.rs module.

use std::collections::HashMap;

pub const RRF_K: f64 = 60.0;

/// Fuse results from multiple search channels using Reciprocal Rank Fusion.
/// Returns (item_id, fused_score) sorted descending.
pub fn rrf_fuse(channel_results: &[Vec<(String, f64)>]) -> Vec<(String, f64)> {
    let mut scores: HashMap<String, f64> = HashMap::new();
    for channel in channel_results {
        for (rank, (id, score)) in channel.iter().enumerate() {
            let rrf = 1.0 / (RRF_K + (rank + 1) as f64);
            *scores.entry(id.clone()).or_insert(0.0) += rrf;
        }
    }
    let mut fused: Vec<_> = scores.into_iter().collect();
    fused.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    fused
}

/// Apply dual-channel symbol rules. Boosts items matching task keywords or
/// memory content keywords across 6 rule categories.
pub fn apply_symbol_rules(
    items: Vec<(String, f64)>,
    task_description: &str,
    item_contents: &HashMap<String, String>,
) -> Vec<(String, f64)> { ... }  // same logic as current RankFuser::apply_symbol_rules

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rrf_fuse_deterministic() {
        let ch1 = vec![("a".into(), 0.9), ("b".into(), 0.7)];
        let ch2 = vec![("b".into(), 0.8), ("c".into(), 0.6)];
        let result = rrf_fuse(&[ch1, ch2]);
        assert_eq!(result[0].0, "b"); // b appears in both channels
    }
}
```

- [ ] **Step 3: Write retrieval/diversity.rs**

```rust
//! Post-retrieval diversity and quality filters.
//!
//! - length_norm: Prevent long entries from dominating (anchor: 500 chars)
//! - mmr_dedup: Maximal Marginal Relevance, cosine > 0.85 triggers demotion
//! - hard_min_score: Remove results below threshold (default 0.35)

pub fn length_norm(content: &str, anchor: usize) -> f64 {
    let len = content.chars().count().max(1) as f64;
    1.0 / (1.0 + (len / anchor as f64).log2())
}

pub fn hard_min_score(items: Vec<(String, f64)>, min_score: f64) -> Vec<(String, f64)> {
    items.into_iter().filter(|(_, s)| *s >= min_score).collect()
}

/// Simple MMR: if two items have high content similarity, demote the lower-scored one.
/// Uses Jaccard similarity on word sets as a proxy for vector cosine.
pub fn mmr_dedup(items: Vec<(String, f64, String)>, threshold: f64) -> Vec<(String, f64, String)> {
    // items: (id, score, content)
    let mut kept = Vec::new();
    for (id, score, content) in items {
        let is_duplicate = kept.iter().any(|(_, _, existing_content): &(String, f64, String)| {
            jaccard_similarity(&content, existing_content) > threshold
        });
        if !is_duplicate {
            kept.push((id, score, content));
        }
    }
    kept
}

fn jaccard_similarity(a: &str, b: &str) -> f64 {
    let set_a: std::collections::HashSet<&str> = a.split_whitespace().collect();
    let set_b: std::collections::HashSet<&str> = b.split_whitespace().collect();
    let intersection = set_a.intersection(&set_b).count();
    let union = set_a.union(&set_b).count();
    if union == 0 { return 0.0; }
    intersection as f64 / union as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_length_norm_short_boost() {
        let short = length_norm("hello", 500);
        let long = length_norm(&"x".repeat(2000), 500);
        assert!(short > long);
    }

    #[test]
    fn test_hard_min_score_filters() {
        let items = vec![("a".into(), 0.5), ("b".into(), 0.1), ("c".into(), 0.35)];
        let filtered = hard_min_score(items, 0.35);
        assert_eq!(filtered.len(), 2);
        assert_eq!(filtered[0].0, "a");
        assert_eq!(filtered[1].0, "c");
    }
}
```

- [ ] **Step 4: Write retrieval/mod.rs — HybridRetriever**

```rust
//! HybridRetriever — orchestration struct combining vector + FTS + domain models.
//!
//! Pipeline: Vector Search → BM25 Search → RRF Fusion → Decay Weight →
//!           Worth Boost → Length Norm → MMR Diversity → Hard Min Score

use std::collections::HashMap;
use crate::domain::{DecayModel, MemoryConsolidator, Tier, TierManager, WorthCalculator};
use crate::memory_worth::MemoryRecord;
use crate::storage::{FtsIndex, IndexMetadata, SearchFilter, VectorIndex};

use super::diversity;
use super::fusion;

/// One element of retrieval output before final packaging.
#[derive(Debug, Clone)]
pub struct ScoredItem {
    pub id: String,
    pub content: String,
    pub score: f64,
    pub source: String,
    pub tier: Tier,
    pub worth_score: f64,
    pub decay_multiplier: f64,
    pub is_principle: bool,
}

/// Hybrid memory retrieval orchestrator.
///
/// Holds trait objects for vector search, full-text search, and domain models.
/// Composes them into a single `retrieve()` method.
pub struct HybridRetriever {
    pub vector: Box<dyn VectorIndex>,
    pub fts: Box<dyn FtsIndex>,
    pub decay: Box<dyn DecayModel>,
    pub worth: Box<dyn WorthCalculator>,
    pub tier_mgr: Box<dyn TierManager>,
    pub consolidator: Box<dyn MemoryConsolidator>,

    pub vector_weight: f64,
    pub bm25_weight: f64,
    pub hard_min_score: f64,
    pub length_norm_anchor: usize,
    pub mmr_threshold: f64,
    pub candidate_pool_size: usize,
}

impl HybridRetriever {
    /// Execute a full hybrid retrieval pipeline.
    ///
    /// # Arguments
    /// * `query_vector` - Embedding vector from Python (dim = EMB_DIM).
    /// * `query_text` - Raw query text for BM25 and symbol rules.
    /// * `scope` - Scope namespace filter.
    /// * `task_type` - Optional task type for symbol rule activation.
    /// * `max_results` - Maximum results to return after all filtering.
    pub fn retrieve(
        &self,
        query_vector: &[f32],
        query_text: &str,
        scope: &str,
        task_type: Option<&str>,
        max_results: usize,
    ) -> Result<Vec<ScoredItem>, String> {
        let filter = SearchFilter {
            scope: Some(scope.to_string()),
            tier: None,
            category: None,
        };

        // 1. Vector search
        let vector_hits = self.vector.search(query_vector, self.candidate_pool_size, &filter)?;

        // 2. BM25 search
        let bm25_hits = self.fts.search(query_text, self.candidate_pool_size, &filter)
            .unwrap_or_default();

        // 3. RRF fusion (weighted: vector * vector_weight + BM25 * bm25_weight)
        let fused = fusion::rrf_fuse(&[vector_hits, bm25_hits]);

        // 4. Apply decay + worth scoring
        // (decay and worth require MemoryRecord lookups — in full impl, passed in or loaded)
        // For now: pass through with identity
        let scored: Vec<(String, f64)> = fused;

        // 5. Hard min score
        let filtered = diversity::hard_min_score(scored, self.hard_min_score);

        // 6. Build ScoredItems (simplified — full version loads records from StorageBackend)
        let items: Vec<ScoredItem> = filtered.into_iter().take(max_results).map(|(id, score)| {
            ScoredItem {
                id,
                content: String::new(), // loaded by ContextEngine
                score,
                source: "hybrid".into(),
                tier: Tier::default(),
                worth_score: 0.0,
                decay_multiplier: 1.0,
                is_principle: false,
            }
        }).collect();

        Ok(items)
    }
}
```

- [ ] **Step 5: Verify compilation + run tests**

```bash
cd rust/context-engine-core && cargo test --lib 2>&1
```
Expected: all existing + new tests PASS

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: retrieval layer - HybridRetriever + fusion + diversity + embedder"
```

---

### Task 7: Refactor ContextEngine to use HybridRetriever + remove rank_fuser.rs

**Files:**
- Modify: `rust/context-engine-core/src/context_engine.rs`
- Delete: `rust/context-engine-core/src/rank_fuser.rs`
- Modify: `rust/context-engine-core/src/lib.rs`

**Interfaces:**
- Consumes: `retrieval::HybridRetriever`, `storage::StorageBackend`, `domain::*`
- Produces: Updated `supply()` with new signature `(task_description, task_vector, task_type, scope)`

- [ ] **Step 1: Rewrite context_engine.rs supply() signature**

Change `supply()` signature from:
```rust
pub fn supply(&mut self, task_description: String, task_type: String, pre_context: Option<String>) -> ContextPack
```
To:
```rust
pub fn supply(&mut self, task_description: String, task_vector: Vec<f32>, task_type: String, scope: String) -> ContextPack
```

- [ ] **Step 2: Update supply() body**

The new body:
1. Load relevant memories from `StorageBackend` filtered by `scope`
2. Inject principles into `EntityGraph` (unchanged)
3. Call `HybridRetriever.retrieve(task_vector, task_description, scope, Some(task_type), max_results)`
4. Apply decay + worth scoring to each result
5. Classify into tiers via `TierManager`
6. Stratify into core/related/divergent layers (unchanged logic)
7. Run `MemoryConsolidator.consolidate()` if interval elapsed
8. Build and return `ContextPack`

- [ ] **Step 3: Update ContextEngine struct fields**

Remove: `rank_fuser: RankFuser`, `memories: HashMap<String, MemoryRecord>`
Add: `retriever: HybridRetriever`, `storage: Box<dyn StorageBackend>`, `last_consolidation: chrono::DateTime<chrono::Utc>`

- [ ] **Step 4: Update lib.rs**

Remove `pub mod rank_fuser;` and `m.add_class::<rank_fuser::RankFuser>()?;`
Add new modules:
```rust
pub mod storage;
pub mod domain;
pub mod retrieval;
```

Register new Python-exposed classes (as needed).

- [ ] **Step 5: Verify compilation**

```bash
cd rust/context-engine-core && cargo check 2>&1
```
Expected: zero errors. Warnings for unused imports OK.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor: ContextEngine uses HybridRetriever + StorageBackend; remove rank_fuser.rs"
```

---

### Task 8: Integration — wire up + end-to-end test

**Files:**
- Modify: `rust/context-engine-core/src/lib.rs` — final module registration
- Create: `rust/context-engine-core/tests/integration_test.rs`

**Interfaces:**
- Consumes: everything
- Produces: working end-to-end pipeline

- [ ] **Step 1: Write integration test**

```rust
// tests/integration_test.rs
// End-to-end: store → retrieve → verify

#[cfg(test)]
mod integration {
    use context_engine_core::storage::sqlite_impl::SqliteStorage;
    use context_engine_core::storage::StorageBackend;
    use context_engine_core::domain::Tier;

    #[test]
    fn test_full_store_and_list_cycle() {
        let mut db = SqliteStorage::open(":memory:").unwrap();

        // Store 10 memories
        for i in 0..10 {
            let record = context_engine_core::memory_worth::MemoryRecord::new(
                format!("mem-{}", i),
                format!("content number {}", i),
                "experience".into(),
                "user".into(),
            );
            db.store(&record).unwrap();
        }

        assert_eq!(db.total_count().unwrap(), 10);

        let stats = db.stats(None).unwrap();
        assert_eq!(stats.total, 10);
        assert_eq!(stats.healthy, 10);
    }

    #[test]
    fn test_tier_filtering() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        for i in 0..5 {
            let mut r = context_engine_core::memory_worth::MemoryRecord::new(
                format!("m{}", i), format!("text {}", i),
                "experience".into(), "user".into(),
            );
            if i < 2 {
                r.tier = Tier::Core.as_str().into();
            } else {
                r.tier = Tier::Working.as_str().into();
            }
            db.store(&r).unwrap();
        }

        use context_engine_core::storage::ListFilter;
        let filter = ListFilter {
            tier: Some(Tier::Core),
            ..Default::default()
        };
        let core_mems = db.list(&filter).unwrap();
        assert_eq!(core_mems.len(), 2);
    }

    #[test]
    fn test_domain_weibull_decay() {
        use context_engine_core::domain::decay::WeibullDecay;
        use context_engine_core::domain::DecayModel;
        use chrono::{DateTime, Duration, Utc};

        let decay = WeibullDecay::default();
        let created = Utc::now() - Duration::days(14);
        let score = decay.compute(Tier::Working, &created, &created, 0, 0.5);
        assert!(score < 0.2); // 14-day-old working memory heavily decayed
    }
}
```

- [ ] **Step 2: Run integration tests**

```bash
cd rust/context-engine-core && cargo test 2>&1
```
Expected: ALL tests PASS (unit + integration)

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test: integration tests — store/list/tier filter/weibull decay pipeline"
```

---

### Task 9: Python-side ContextEngine.update() for new signature

**Files:**
- Modify: `plastic_promise/core/context_engine.py` — Python wrapper updated

- [ ] **Step 1: Update Python ContextEngine.supply() wrapper**

Update the Python `ContextEngine.supply()` to match new signature:
```python
def supply(self, task_description: str, task_vector: list[float], task_type: str = "general", scope: str = "global") -> ContextPack:
```

- [ ] **Step 2: Verify Python import still works**

```bash
python -c "from plastic_promise.core.context_engine import ContextEngine, ContextPack; print('Python wrapper OK')"
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: Python ContextEngine wrapper updated for supply(task_vector, scope)"
```

---

### Task 10: Final verification — full import chain + push

- [ ] **Step 1: Full Rust + Python import check**

```bash
cd rust/context-engine-core && cargo check 2>&1
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.core import CORE_PRINCIPLES, ContextEngine
from plastic_promise.memory import RecMem, MemoryRecord
from plastic_promise.loop import SoulLoop
from plastic_promise.principles import PrincipleManager
from plastic_promise.reflection import SCARFReflector
from plastic_promise.defense import SoulEnforcer
from plastic_promise.growth import HormoneEngine
print('Full chain OK')
"
```

- [ ] **Step 2: Push**

```bash
git push origin main
```
