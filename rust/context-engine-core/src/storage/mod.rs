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
    fn search(
        &self,
        vector: &[f32],
        k: usize,
        filter: &SearchFilter,
    ) -> Result<Vec<(String, f64)>, String>;
    fn insert(
        &mut self,
        id: &str,
        vector: &[f32],
        metadata: &IndexMetadata,
    ) -> Result<(), String>;
    fn update(
        &mut self,
        id: &str,
        vector: &[f32],
        metadata: &IndexMetadata,
    ) -> Result<(), String>;
    fn delete(&mut self, id: &str) -> Result<(), String>;
}

pub trait FtsIndex {
    fn search(
        &self,
        query: &str,
        k: usize,
        filter: &SearchFilter,
    ) -> Result<Vec<(String, f64)>, String>;
    fn index(&mut self, id: &str, text: &str, metadata: &IndexMetadata) -> Result<(), String>;
    fn update(
        &mut self,
        id: &str,
        text: &str,
        metadata: &IndexMetadata,
    ) -> Result<(), String>;
    fn delete(&mut self, id: &str) -> Result<(), String>;
}

/// Embedder trait — trait defined here, implementation in Python.
/// Rust never calls this directly; vectors arrive via supply().
pub trait Embedder {
    fn embed(&self, text: &str) -> Result<Vec<f32>, String>;
    fn embed_batch(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, String>;
}
