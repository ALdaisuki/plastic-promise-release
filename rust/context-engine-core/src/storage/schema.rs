//! Database schema constants — SQLite DDL + LanceDB table config.

use crate::storage::EMB_DIM;

/// Current schema version for migration checks.
pub const SCHEMA_VERSION: u32 = 1;

/// LanceDB table name for vector storage.
pub const LANCEDB_TABLE: &str = "memory_vectors";

/// LanceDB table version string for compatibility checks.
pub const LANCEDB_TABLE_VERSION: &str = "1.0.0";

/// Vector dimension for the LanceDB embedding column, derived from EMB_DIM.
pub const LANCEDB_VECTOR_DIM: usize = EMB_DIM;

/// Create the `memories` table using the Python-owned canonical column names.
///
/// Rust only derives snapshots from this schema. Keep the read/write subset
/// aligned with `plastic_promise.core.context_engine.SQLiteMemoryStore`; Python
/// may add more columns without changing this contract.
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
    last_accessed   TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    scope           TEXT DEFAULT 'global',
    metadata_json   TEXT DEFAULT '{}',
    tags            TEXT DEFAULT '[]',
    domain          TEXT DEFAULT 'uncategorized',
    decay_multiplier REAL DEFAULT 1.0,
    effective_half_life REAL DEFAULT 3.0
);
CREATE TABLE IF NOT EXISTS memory_version (
    version INTEGER DEFAULT 0
);
INSERT OR IGNORE INTO memory_version (version) VALUES (0);
"#;

/// Composite index covering the most common filter/order columns for list queries.
pub const SQL_CREATE_INDEX: &str = r#"
CREATE INDEX IF NOT EXISTS idx_memories_filter
    ON memories(tier, scope, last_accessed, memory_type, category);
"#;

/// Create the `entities` table for the entity graph backing store.
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

/// Create the `entity_edges` table with foreign keys into `entities`.
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

/// Parameterised upsert: insert a new memory or replace an existing row by id.
pub const SQL_UPSERT_MEMORY: &str = r#"
INSERT OR REPLACE INTO memories
    (id, content, memory_type, source, category, tier, importance,
     worth_success, worth_failure, access_count, last_accessed,
     created_at, scope, metadata_json, tags, domain, decay_multiplier, effective_half_life)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"#;

/// Canonical memory projection. Do not use `SELECT *`: Python owns this
/// schema and has additional columns whose order is not part of the Rust ABI.
/// `COALESCE` preserves compatibility with older nullable Python rows.
pub const SQL_MEMORY_COLUMNS: &str = "id, \
    COALESCE(content, ''), \
    COALESCE(memory_type, 'experience'), \
    COALESCE(source, 'user'), \
    COALESCE(tier, 'L1'), \
    COALESCE(scope, 'global'), \
    COALESCE(category, 'other'), \
    COALESCE(importance, 0.7), \
    COALESCE(worth_success, 0), \
    COALESCE(worth_failure, 0), \
    COALESCE(access_count, 0), \
    COALESCE(last_accessed, ''), \
    COALESCE(created_at, ''), \
    COALESCE(metadata_json, '{}'), \
    COALESCE(tags, '[]'), \
    COALESCE(domain, 'uncategorized'), \
    COALESCE(decay_multiplier, 1.0), \
    COALESCE(effective_half_life, 3.0)";

/// Fetch a single memory by primary key using the explicit canonical projection.
pub const SQL_GET_BY_ID: &str = "SELECT id, \
    COALESCE(content, ''), \
    COALESCE(memory_type, 'experience'), \
    COALESCE(source, 'user'), \
    COALESCE(tier, 'L1'), \
    COALESCE(scope, 'global'), \
    COALESCE(category, 'other'), \
    COALESCE(importance, 0.7), \
    COALESCE(worth_success, 0), \
    COALESCE(worth_failure, 0), \
    COALESCE(access_count, 0), \
    COALESCE(last_accessed, ''), \
    COALESCE(created_at, ''), \
    COALESCE(metadata_json, '{}'), \
    COALESCE(tags, '[]'), \
    COALESCE(domain, 'uncategorized'), \
    COALESCE(decay_multiplier, 1.0), \
    COALESCE(effective_half_life, 3.0) \
    FROM memories WHERE id = ?";

/// Delete a single memory by primary key.
pub const SQL_DELETE_BY_ID: &str = "DELETE FROM memories WHERE id = ?";

/// Count all rows in the memories table.
pub const SQL_COUNT_ALL: &str = "SELECT COUNT(*) FROM memories";
