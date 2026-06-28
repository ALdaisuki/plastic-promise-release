//! LanceDB implementation of VectorIndex + FtsIndex traits.
//!
//! Uses LanceDB Rust SDK for ANN vector search and BM25 full-text search.
//! LanceDB auto-manages both indices.
//!
//! Current status: trait implementations return Err pending Windows SDK installation
//! for the LanceDB Rust crate linking.

use std::path::Path;
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
    /// Whether FTS indexing will be available (runtime detection).
    fts_available: bool,
    /// Path to the LanceDB data directory.
    data_path: String,
}

impl LanceDbStore {
    /// Open or create a LanceDB database at the given path.
    ///
    /// Creates the memory_vectors table if it doesn't exist.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, String> {
        let data_path = path.as_ref().to_string_lossy().to_string();
        // Create directory if it doesn't exist
        if let Some(parent) = path.as_ref().parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("Failed to create LanceDB directory: {}", e))?;
        }
        Ok(Self {
            fts_available: false, // will probe when LanceDB linked
            data_path,
        })
    }

    /// Check if FTS is available on this LanceDB build.
    pub fn has_fts(&self) -> bool {
        self.fts_available
    }

    /// Return the data directory path.
    pub fn data_path(&self) -> &str {
        &self.data_path
    }
}

impl VectorIndex for LanceDbStore {
    fn search(
        &self,
        _vector: &[f32],
        _k: usize,
        _filter: &SearchFilter,
    ) -> Result<Vec<(String, f64)>, String> {
        Err("LanceDB not linked — install Windows SDK and rebuild with lancedb crate".into())
    }

    fn insert(
        &mut self,
        _id: &str,
        _vector: &[f32],
        _metadata: &IndexMetadata,
    ) -> Result<(), String> {
        Err("LanceDB not linked — install Windows SDK and rebuild with lancedb crate".into())
    }

    fn update(
        &mut self,
        _id: &str,
        _vector: &[f32],
        _metadata: &IndexMetadata,
    ) -> Result<(), String> {
        Err("LanceDB not linked — install Windows SDK and rebuild with lancedb crate".into())
    }

    fn delete(&mut self, _id: &str) -> Result<(), String> {
        Err("LanceDB not linked — install Windows SDK and rebuild with lancedb crate".into())
    }
}

impl FtsIndex for LanceDbStore {
    fn search(
        &self,
        _query: &str,
        _k: usize,
        _filter: &SearchFilter,
    ) -> Result<Vec<(String, f64)>, String> {
        Err("LanceDB not linked — install Windows SDK and rebuild with lancedb crate".into())
    }

    fn index(
        &mut self,
        _id: &str,
        _text: &str,
        _metadata: &IndexMetadata,
    ) -> Result<(), String> {
        Err("LanceDB not linked — install Windows SDK and rebuild with lancedb crate".into())
    }

    fn update(
        &mut self,
        _id: &str,
        _text: &str,
        _metadata: &IndexMetadata,
    ) -> Result<(), String> {
        Err("LanceDB not linked — install Windows SDK and rebuild with lancedb crate".into())
    }

    fn delete(&mut self, _id: &str) -> Result<(), String> {
        Err("LanceDB not linked — install Windows SDK and rebuild with lancedb crate".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lancedb_store_creation() {
        let tmp = std::env::temp_dir().join("pp_test_lancedb_task4");
        let _ = std::fs::remove_dir_all(&tmp);
        let store = LanceDbStore::open(&tmp).unwrap();
        assert_eq!(store.data_path(), tmp.to_string_lossy());
        assert!(!store.has_fts());
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn test_vector_search_returns_err() {
        let tmp = std::env::temp_dir().join("pp_test_lancedb_vs");
        let store = LanceDbStore::open(&tmp).unwrap();
        let result = store.search(&[0.1], 5, &SearchFilter::default());
        assert!(result.is_err());
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn test_fts_search_returns_err() {
        let tmp = std::env::temp_dir().join("pp_test_lancedb_fts");
        let store = LanceDbStore::open(&tmp).unwrap();
        let result = store.search("hello", 5, &SearchFilter::default());
        assert!(result.is_err());
        std::fs::remove_dir_all(&tmp).ok();
    }
}
