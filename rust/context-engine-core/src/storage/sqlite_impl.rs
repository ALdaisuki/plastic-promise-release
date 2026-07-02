//! SQLite implementation of the StorageBackend trait.
//!
//! Uses `rusqlite` with the `bundled` feature so no system SQLite is required.
//! WAL mode is enabled on open for concurrent-read safety.

use rusqlite::{params, Connection, OpenFlags};
use std::collections::HashMap;

use crate::domain::Tier;
use crate::memory_worth::MemoryRecord;
use crate::storage::schema::{
    SQL_COUNT_ALL, SQL_CREATE_ENTITIES, SQL_CREATE_ENTITY_EDGES, SQL_CREATE_INDEX,
    SQL_CREATE_MEMORIES, SQL_DELETE_BY_ID, SQL_GET_BY_ID, SQL_UPSERT_MEMORY,
};
use crate::storage::{ListFilter, MemoryStats, StorageBackend, UpdateFields};

/// SQLite-backed storage engine.
///
/// Holds a single `rusqlite::Connection` and implements [`StorageBackend`]
/// for full CRUD + statistics queries.
pub struct SqliteStorage {
    conn: Connection,
}

impl SqliteStorage {
    /// Open (or create) a SQLite database at `path` and run DDL.
    ///
    /// Pass `":memory:"` for an in-memory database (useful for tests).
    /// Enables WAL journal mode for better concurrent-read performance.
    pub fn open(path: &str) -> Result<Self, String> {
        let conn = Connection::open(path).map_err(|e| format!("Failed to open SQLite: {}", e))?;
        conn.execute_batch("PRAGMA journal_mode=WAL;")
            .map_err(|e| format!("Failed to enable WAL: {}", e))?;
        let mut storage = Self { conn };
        storage.create_tables()?;
        Ok(storage)
    }

    /// Open a SQLite database in read-only mode without running DDL.
    ///
    /// Used by the Rust compute engine to read from `plastic_memory.db`
    /// while Python writes concurrently. WAL mode on the writer side
    /// ensures concurrent reads are safe and unblocked.
    pub fn open_readonly(path: &str) -> Result<Self, String> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        )
        .map_err(|e| format!("Failed to open SQLite read-only: {}", e))?;
        Ok(Self { conn })
    }

    /// Execute all DDL statements to create tables and indexes if they do not exist.
    fn create_tables(&mut self) -> Result<(), String> {
        self.conn
            .execute_batch(SQL_CREATE_MEMORIES)
            .map_err(|e| format!("create memories: {}", e))?;
        self.conn
            .execute_batch(SQL_CREATE_INDEX)
            .map_err(|e| format!("create index: {}", e))?;
        self.conn
            .execute_batch(SQL_CREATE_ENTITIES)
            .map_err(|e| format!("create entities: {}", e))?;
        self.conn
            .execute_batch(SQL_CREATE_ENTITY_EDGES)
            .map_err(|e| format!("create entity edges: {}", e))?;
        Ok(())
    }

    /// Parse a JSON-encoded tags string from SQLite into Vec<String>.
    /// Handles NULL, empty string, "[]", and "null" as empty vec.
    fn parse_tags(raw: &str) -> Vec<String> {
        if raw.is_empty() || raw == "[]" || raw == "null" {
            return vec![];
        }
        serde_json::from_str(raw).unwrap_or_default()
    }

    /// Map a single SQLite row (18 columns, in CREATE TABLE order) to a [`MemoryRecord`].
    ///
    /// Column indices: id(0), content(1), memory_type(2), source(3), category(4),
    /// tier(5), importance(6), worth_success(7), worth_failure(8), access_count(9),
    /// last_accessed_at(10), created_at(11), scope(12), metadata_json(13),
    /// tags(14), domain(15), decay_multiplier(16), effective_half_life(17).
    fn row_to_record(row: &rusqlite::Row) -> std::result::Result<MemoryRecord, rusqlite::Error> {
        let tags_raw: String = row.get::<_, String>(14).unwrap_or_default();
        Ok(MemoryRecord::from_storage(
            row.get::<_, String>(0)?,   // id
            row.get::<_, String>(1)?,   // content
            row.get::<_, String>(2)?,   // memory_type
            row.get::<_, String>(3)?,   // source
            row.get::<_, String>(5)?,   // tier
            row.get::<_, String>(12)?,  // scope
            row.get::<_, String>(4)?,   // category
            row.get::<_, f64>(6)?,      // importance
            row.get::<_, u32>(7)?,      // worth_success
            row.get::<_, u32>(8)?,      // worth_failure
            row.get::<_, u32>(9)?,      // access_count
            row.get::<_, String>(10)?,  // last_accessed_at
            row.get::<_, String>(11)?,  // created_at
            row.get::<_, String>(13)?,  // metadata_json
            Self::parse_tags(&tags_raw),
            row.get::<_, String>(15).unwrap_or_else(|_| "uncategorized".to_string()),
            row.get::<_, f64>(16).unwrap_or(1.0),
            row.get::<_, f64>(17).unwrap_or(3.0),
        ))
    }
}

impl StorageBackend for SqliteStorage {
    /// Insert or replace a memory record.
    ///
    /// If `record.created_at` is empty it is set to the current UTC time in RFC 3339 format.
    /// Returns the record's id on success.
    fn store(&mut self, record: &MemoryRecord) -> Result<String, String> {
        let created_at = if record.created_at.is_empty() {
            chrono::Utc::now().to_rfc3339()
        } else {
            record.created_at.clone()
        };

        self.conn
            .execute(
                SQL_UPSERT_MEMORY,
                params![
                    record.id,
                    record.content,
                    record.memory_type,
                    record.source,
                    record.category,
                    record.tier,
                    record.importance,
                    record.worth_success,
                    record.worth_failure,
                    record.access_count,
                    record.last_accessed_at,
                    created_at,
                    record.scope,
                    record.metadata_json,
                    serde_json::to_string(&record.tags).unwrap_or_else(|_| "[]".to_string()),
                    record.domain,
                    record.decay_multiplier,
                    record.effective_half_life,
                ],
            )
            .map_err(|e| format!("store: {}", e))?;

        Ok(record.id.clone())
    }

    /// Retrieve a single memory by id.
    ///
    /// Returns `None` if no row matches.
    fn get(&self, id: &str) -> Result<Option<MemoryRecord>, String> {
        let mut stmt = self
            .conn
            .prepare(SQL_GET_BY_ID)
            .map_err(|e| format!("get prepare: {}", e))?;
        let mut rows = stmt
            .query_map(params![id], Self::row_to_record)
            .map_err(|e| format!("get query: {}", e))?;
        match rows.next() {
            Some(Ok(record)) => Ok(Some(record)),
            Some(Err(e)) => Err(format!("get row: {}", e)),
            None => Ok(None),
        }
    }

    /// Update specific fields of a memory record.
    ///
    /// Only the fields present in [`UpdateFields`] are modified; others are left unchanged.
    /// Returns `true` if at least one row was updated.
    fn update(&mut self, id: &str, updates: &UpdateFields) -> Result<bool, String> {
        let mut sets: Vec<String> = Vec::new();
        let mut param_vals: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

        if let Some(ref v) = updates.content {
            sets.push("content = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(ref v) = updates.memory_type {
            sets.push("memory_type = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(ref v) = updates.category {
            sets.push("category = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(ref v) = updates.tier {
            sets.push("tier = ?".into());
            param_vals.push(Box::new(v.as_str().to_string()));
        }
        if let Some(v) = updates.importance {
            sets.push("importance = ?".into());
            param_vals.push(Box::new(v));
        }
        if let Some(v) = updates.worth_success {
            sets.push("worth_success = ?".into());
            param_vals.push(Box::new(v as i64));
        }
        if let Some(v) = updates.worth_failure {
            sets.push("worth_failure = ?".into());
            param_vals.push(Box::new(v as i64));
        }
        if let Some(v) = updates.access_count {
            sets.push("access_count = ?".into());
            param_vals.push(Box::new(v as i64));
        }
        if let Some(ref v) = updates.last_accessed_at {
            sets.push("last_accessed_at = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(ref v) = updates.scope {
            sets.push("scope = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(ref v) = updates.metadata {
            sets.push("metadata_json = ?".into());
            param_vals.push(Box::new(v.clone()));
        }

        if sets.is_empty() {
            return Ok(false);
        }

        let sql = format!("UPDATE memories SET {} WHERE id = ?", sets.join(", "));
        param_vals.push(Box::new(id.to_string()));

        let param_refs: Vec<&dyn rusqlite::types::ToSql> =
            param_vals.iter().map(|p| p.as_ref()).collect();
        let affected = self
            .conn
            .execute(&sql, param_refs.as_slice())
            .map_err(|e| format!("update: {}", e))?;
        Ok(affected > 0)
    }

    /// List memory records matching the given filter.
    ///
    /// Supports filtering by scope, tier, category, memory_type, source,
    /// minimum importance, and minimum worth score. Results are ordered by
    /// `last_accessed_at DESC` with pagination via limit/offset.
    fn list(&self, filter: &ListFilter) -> Result<Vec<MemoryRecord>, String> {
        let mut conditions: Vec<String> = Vec::new();
        let mut param_vals: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

        if let Some(ref v) = filter.scope {
            conditions.push("scope = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(ref v) = filter.tier {
            conditions.push("tier = ?".into());
            param_vals.push(Box::new(v.as_str().to_string()));
        }
        if let Some(ref v) = filter.category {
            conditions.push("category = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(ref v) = filter.memory_type {
            conditions.push("memory_type = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(ref v) = filter.source {
            conditions.push("source = ?".into());
            param_vals.push(Box::new(v.clone()));
        }
        if let Some(v) = filter.min_importance {
            conditions.push("importance >= ?".into());
            param_vals.push(Box::new(v));
        }
        if let Some(v) = filter.min_worth {
            conditions.push(
                "CASE WHEN (worth_success + worth_failure) >= 5 THEN (CAST(worth_success AS REAL) * 1.0 - CAST(worth_failure AS REAL) * 1.5) / CAST(worth_success + worth_failure + 1 AS REAL) ELSE 0.0 END >= ?"
                    .into(),
            );
            param_vals.push(Box::new(v));
        }

        let where_clause = if conditions.is_empty() {
            "1=1".to_string()
        } else {
            conditions.join(" AND ")
        };

        let sql = format!(
            "SELECT * FROM memories WHERE {} ORDER BY last_accessed_at DESC LIMIT ? OFFSET ?",
            where_clause
        );
        param_vals.push(Box::new(filter.limit as i64));
        param_vals.push(Box::new(filter.offset as i64));

        let param_refs: Vec<&dyn rusqlite::types::ToSql> =
            param_vals.iter().map(|p| p.as_ref()).collect();

        let mut stmt = self
            .conn
            .prepare(&sql)
            .map_err(|e| format!("list prepare: {}", e))?;
        let rows = stmt
            .query_map(param_refs.as_slice(), Self::row_to_record)
            .map_err(|e| format!("list query: {}", e))?;

        let mut results = Vec::new();
        for row in rows {
            results.push(row.map_err(|e| format!("list row: {}", e))?);
        }
        Ok(results)
    }

    /// Delete a memory record by id.
    ///
    /// Returns `true` if a row was deleted, `false` if no matching id was found.
    fn delete(&mut self, id: &str) -> Result<bool, String> {
        let affected = self
            .conn
            .execute(SQL_DELETE_BY_ID, params![id])
            .map_err(|e| format!("delete: {}", e))?;
        Ok(affected > 0)
    }

    /// Compute aggregate memory statistics, optionally scoped to a single namespace.
    ///
    /// Returns total count, healthy/decaying counts (based on worth_success vs worth_failure),
    /// breakdowns by tier/type/category, and the average worth score.
    fn stats(&self, scope: Option<&str>) -> Result<MemoryStats, String> {
        let scope_filter = scope.map(|_| "AND scope = ?").unwrap_or("");
        let scope_param: Option<String> = scope.map(|s| s.to_string());

        // -- totals + average worth --
        let totals_sql = format!(
            "SELECT
                COUNT(*) as total,
                SUM(CASE WHEN worth_success > worth_failure THEN 1 ELSE 0 END) as healthy,
                SUM(CASE WHEN worth_success < worth_failure THEN 1 ELSE 0 END) as decaying,
                AVG(CASE WHEN (worth_success + worth_failure) >= 5
                    THEN (CAST(worth_success AS REAL) * 1.0 - CAST(worth_failure AS REAL) * 1.5)
                         / CAST(worth_success + worth_failure + 1 AS REAL)
                    ELSE 0.0 END) as avg_worth
            FROM memories WHERE 1=1 {}",
            scope_filter
        );

        // Use self.conn to run the totals query
        let (total, healthy, decaying, avg_worth): (usize, usize, usize, f64) = {
            let mut stmt = self
                .conn
                .prepare(&totals_sql)
                .map_err(|e| format!("stats totals prepare: {}", e))?;
            let result: Result<_, _> = if let Some(ref s) = scope_param {
                stmt.query_row(params![s], |row| {
                    Ok((
                        row.get::<_, i64>(0)? as usize,
                        row.get::<_, i64>(1)? as usize,
                        row.get::<_, i64>(2)? as usize,
                        row.get::<_, f64>(3)?,
                    ))
                })
            } else {
                stmt.query_row([], |row| {
                    Ok((
                        row.get::<_, i64>(0)? as usize,
                        row.get::<_, i64>(1)? as usize,
                        row.get::<_, i64>(2)? as usize,
                        row.get::<_, f64>(3)?,
                    ))
                })
            };
            result.map_err(|e| format!("stats totals: {}", e))?
        };

        // -- by_tier --
        let by_tier_sql = format!(
            "SELECT tier, COUNT(*) FROM memories WHERE 1=1 {} GROUP BY tier",
            scope_filter
        );
        let by_tier = self.group_count_query(&by_tier_sql, &scope_param)?;

        // -- by_type --
        let by_type_sql = format!(
            "SELECT memory_type, COUNT(*) FROM memories WHERE 1=1 {} GROUP BY memory_type",
            scope_filter
        );
        let by_type = self.group_count_query(&by_type_sql, &scope_param)?;

        // -- by_category --
        let by_category_sql = format!(
            "SELECT category, COUNT(*) FROM memories WHERE 1=1 {} GROUP BY category",
            scope_filter
        );
        let by_category = self.group_count_query(&by_category_sql, &scope_param)?;

        Ok(MemoryStats {
            total,
            healthy,
            decaying,
            by_tier,
            by_type,
            by_category,
            average_worth: avg_worth,
        })
    }

    /// Return the total number of rows in the memories table.
    fn total_count(&self) -> Result<usize, String> {
        self.conn
            .query_row(SQL_COUNT_ALL, [], |row| row.get::<_, i64>(0))
            .map(|c| c as usize)
            .map_err(|e| format!("total_count: {}", e))
    }

    /// Execute a scalar SQL query returning a single u64.
    fn query_scalar(&self, sql: &str) -> Result<u64, String> {
        self.conn
            .query_row(sql, [], |row| row.get::<_, i64>(0))
            .map(|v| v as u64)
            .map_err(|e| format!("query_scalar '{}': {}", sql, e))
    }
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

impl SqliteStorage {
    /// Run a `SELECT key, COUNT(*) ... GROUP BY key` query and return a HashMap.
    fn group_count_query(
        &self,
        sql: &str,
        scope_param: &Option<String>,
    ) -> Result<HashMap<String, usize>, String> {
        let mut stmt = self
            .conn
            .prepare(sql)
            .map_err(|e| format!("group query prepare: {}", e))?;
        let rows: Vec<(String, i64)> = if let Some(ref s) = scope_param {
            stmt.query_map(params![s], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(|e| format!("group query map: {}", e))?
            .filter_map(|r| r.ok())
            .collect()
        } else {
            stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(|e| format!("group query map: {}", e))?
            .filter_map(|r| r.ok())
            .collect()
        };

        let mut map = HashMap::new();
        for (key, count) in rows {
            map.insert(key, count as usize);
        }
        Ok(map)
    }
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Tier;

    fn make_record(id: &str, content: &str) -> crate::memory_worth::MemoryRecord {
        crate::memory_worth::MemoryRecord::new(
            id.into(),
            content.into(),
            "experience".into(),
            "user".into(),
        )
    }

    #[test]
    fn test_store_and_retrieve() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        let record = make_record("test-1", "hello world");
        let id = db.store(&record).unwrap();
        assert_eq!(id, "test-1");
        let retrieved = db.get("test-1").unwrap().unwrap();
        assert_eq!(retrieved.content, "hello world");
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
    }

    #[test]
    fn test_delete() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        db.store(&make_record("r1", "x")).unwrap();
        assert!(db.delete("r1").unwrap());
        assert!(db.get("r1").unwrap().is_none());
    }

    #[test]
    fn test_list_filter_by_tier() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        let mut r1 = make_record("r1", "core mem");
        r1.tier = Tier::Core.as_str().into();
        db.store(&r1).unwrap();
        db.store(&make_record("r2", "working mem")).unwrap();
        let filter = ListFilter {
            tier: Some(Tier::Core),
            limit: 10,
            ..Default::default()
        };
        let results = db.list(&filter).unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, "r1");
    }

    #[test]
    fn test_stats() {
        let mut db = SqliteStorage::open(":memory:").unwrap();
        db.store(&make_record("r1", "a")).unwrap();
        db.store(&make_record("r2", "b")).unwrap();
        let stats = db.stats(None).unwrap();
        assert_eq!(stats.total, 2);
    }
}
