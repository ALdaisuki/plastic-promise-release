//! Vector and FTS index — in-memory implementation.
//!
//! Currently uses in-memory brute-force search (fast for <10K vectors).
//! Upgrade path: swap for lancedb crate when protobuf/tokio deps resolve.
//! API is identical — callers are insulated.

use std::collections::HashMap;
use crate::storage::{FtsIndex, IndexMetadata, SearchFilter, VectorIndex};

/// LanceDbStore provides both VectorIndex and FtsIndex via in-memory data structures.
///
/// Stores:
/// - `vectors`: id → f32 vector (for cosine-similarity ANN search)
/// - `texts`: id → raw text (for word-overlap FTS scoring)
/// - `metadata`: id → IndexMetadata (for scope/tier/category filtering)
///
/// Search is brute-force over all entries, filtered by SearchFilter.
/// Suitable for <10K entries. Beyond that, swap in the lancedb crate.
pub struct LanceDbStore {
    vectors: HashMap<String, Vec<f32>>,
    texts: HashMap<String, String>,
    metadata: HashMap<String, IndexMetadata>,
}

impl LanceDbStore {
    /// Open or create a backing directory at `path`.
    ///
    /// The path is created if missing, but all data lives in memory.
    /// Future persistence (snapshot/restore) can be added without changing callers.
    pub fn open<P: AsRef<std::path::Path>>(path: P) -> Result<Self, String> {
        if let Some(parent) = path.as_ref().parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("Failed to create directory: {}", e))?;
        }
        Ok(Self {
            vectors: HashMap::new(),
            texts: HashMap::new(),
            metadata: HashMap::new(),
        })
    }

    // ------------------------------------------------------------------
    // Internal helpers
    // ------------------------------------------------------------------

    /// Cosine similarity between two equal-length vectors.
    /// Returns a value in [-1.0, 1.0]. Higher = more similar.
    fn cosine(a: &[f32], b: &[f32]) -> f64 {
        if a.len() != b.len() {
            return 0.0;
        }
        let dot: f64 = a.iter().zip(b).map(|(&x, &y)| x as f64 * y as f64).sum();
        let na: f64 = a.iter().map(|&x| (x as f64).powi(2)).sum::<f64>().sqrt();
        let nb: f64 = b.iter().map(|&x| (x as f64).powi(2)).sum::<f64>().sqrt();
        if na < 1e-12 || nb < 1e-12 {
            return 0.0;
        }
        (dot / (na * nb)).clamp(-1.0, 1.0)
    }

    /// BM25-like word-overlap score between a query and a text.
    ///
    /// Tokenizes both sides, counts how many query words appear in the text.
    /// Returns a fraction in [0.0, 1.0].
    fn bm25_like(query: &str, text: &str) -> f64 {
        let q_words: Vec<&str> = query
            .split_whitespace()
            .map(|w| w.trim_matches(|c: char| !c.is_alphanumeric()))
            .filter(|w| w.len() >= 2)
            .collect();
        if q_words.is_empty() {
            return 0.0;
        }
        let t_lower = text.to_lowercase();
        let hits = q_words
            .iter()
            .filter(|w| t_lower.contains(&w.to_lowercase()))
            .count();
        hits as f64 / q_words.len() as f64
    }

    /// Check whether `meta` satisfies every non-None field in `filter`.
    fn matches_filter(meta: &IndexMetadata, filter: &SearchFilter) -> bool {
        if let Some(ref s) = filter.scope {
            if meta.scope != *s {
                return false;
            }
        }
        if let Some(ref t) = filter.tier {
            if meta.tier != t.as_str() {
                return false;
            }
        }
        if let Some(ref c) = filter.category {
            if !c.is_empty() && meta.category != *c {
                return false;
            }
        }
        true
    }
}

// ============================================================
// VectorIndex impl
// ============================================================

impl VectorIndex for LanceDbStore {
    /// Return the top-`k` vectors by cosine similarity, filtered by `filter`.
    ///
    /// Brute-force scan over all stored vectors. Every entry whose metadata
    /// passes `matches_filter` is scored and ranked.
    fn search(
        &self,
        vector: &[f32],
        k: usize,
        filter: &SearchFilter,
    ) -> Result<Vec<(String, f64)>, String> {
        let mut results: Vec<(String, f64)> = self
            .vectors
            .iter()
            .filter(|(id, _)| {
                self.metadata
                    .get(*id)
                    .map(|m| Self::matches_filter(m, filter))
                    .unwrap_or(true)
            })
            .map(|(id, v)| (id.clone(), Self::cosine(vector, v)))
            .collect();
        results.sort_by(|a, b| {
            b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal)
        });
        Ok(results.into_iter().take(k).collect())
    }

    /// Insert a new vector and its metadata.
    fn insert(
        &mut self,
        id: &str,
        vector: &[f32],
        metadata: &IndexMetadata,
    ) -> Result<(), String> {
        self.vectors.insert(id.to_string(), vector.to_vec());
        self.metadata.insert(id.to_string(), metadata.clone());
        Ok(())
    }

    /// Update an existing vector and its metadata (upsert semantics).
    fn update(
        &mut self,
        id: &str,
        vector: &[f32],
        metadata: &IndexMetadata,
    ) -> Result<(), String> {
        self.vectors.insert(id.to_string(), vector.to_vec());
        self.metadata.insert(id.to_string(), metadata.clone());
        Ok(())
    }

    /// Delete a vector entry by id.
    fn delete(&mut self, id: &str) -> Result<(), String> {
        self.vectors.remove(id);
        self.texts.remove(id);
        self.metadata.remove(id);
        Ok(())
    }
}

// ============================================================
// FtsIndex impl
// ============================================================

impl FtsIndex for LanceDbStore {
    /// Full-text search: token-overlap scoring, filtered by `filter`.
    ///
    /// Only entries with a positive BM25-like score are returned.
    fn search(
        &self,
        query: &str,
        k: usize,
        filter: &SearchFilter,
    ) -> Result<Vec<(String, f64)>, String> {
        let mut results: Vec<(String, f64)> = self
            .texts
            .iter()
            .filter(|(id, _)| {
                self.metadata
                    .get(*id)
                    .map(|m| Self::matches_filter(m, filter))
                    .unwrap_or(true)
            })
            .map(|(id, text)| (id.clone(), Self::bm25_like(query, text)))
            .filter(|(_, s)| *s > 0.0)
            .collect();
        results.sort_by(|a, b| {
            b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal)
        });
        Ok(results.into_iter().take(k).collect())
    }

    /// Index a text entry with metadata (upsert semantics for new ids).
    fn index(
        &mut self,
        id: &str,
        text: &str,
        metadata: &IndexMetadata,
    ) -> Result<(), String> {
        self.texts.insert(id.to_string(), text.to_string());
        self.metadata
            .entry(id.to_string())
            .or_insert_with(|| metadata.clone());
        Ok(())
    }

    /// Update an existing text entry and its metadata.
    fn update(
        &mut self,
        id: &str,
        text: &str,
        metadata: &IndexMetadata,
    ) -> Result<(), String> {
        self.texts.insert(id.to_string(), text.to_string());
        self.metadata.insert(id.to_string(), metadata.clone());
        Ok(())
    }

    /// Delete a text entry by id.
    fn delete(&mut self, id: &str) -> Result<(), String> {
        self.texts.remove(id);
        self.metadata.remove(id);
        Ok(())
    }
}

// ============================================================
// Tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::SearchFilter;

    fn make_meta(id: &str) -> IndexMetadata {
        IndexMetadata {
            memory_id: id.into(),
            tier: "working".into(),
            category: "fact".into(),
            scope: "global".into(),
        }
    }

    #[test]
    fn test_vector_search_self_similarity() {
        let tmp = std::env::temp_dir().join("pp_test_lv1");
        let _ = std::fs::remove_dir_all(&tmp);
        let mut store = LanceDbStore::open(&tmp).unwrap();
        let v = vec![1.0f32; 1024];
        store.insert("m1", &v, &make_meta("m1")).unwrap();
        let results = <LanceDbStore as VectorIndex>::search(
            &store,
            &v,
            5,
            &SearchFilter::default(),
        )
        .unwrap();
        assert!(!results.is_empty());
        assert!(results[0].1 > 0.99);
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn test_vector_search_orthogonal_is_low() {
        let tmp = std::env::temp_dir().join("pp_test_lv2");
        let _ = std::fs::remove_dir_all(&tmp);
        let mut store = LanceDbStore::open(&tmp).unwrap();
        let mut v1 = vec![0.0f32; 1024];
        v1[0] = 1.0;
        let mut v2 = vec![0.0f32; 1024];
        v2[1] = 1.0;
        store.insert("m1", &v1, &make_meta("m1")).unwrap();
        store.insert("m2", &v2, &make_meta("m2")).unwrap();
        let results = <LanceDbStore as VectorIndex>::search(&store, &v1, 5, &SearchFilter::default())
            .unwrap();
        assert!(results.len() >= 2);
        assert!(results[0].1 > 0.9); // v1 matches itself
        assert!(results[1].1 < 0.1); // v2 is orthogonal
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn test_fts_search() {
        let tmp = std::env::temp_dir().join("pp_test_lf1");
        let _ = std::fs::remove_dir_all(&tmp);
        let mut store = LanceDbStore::open(&tmp).unwrap();
        <LanceDbStore as FtsIndex>::index(
            &mut store,
            "m1",
            "Rust context engine with PyO3 bindings",
            &make_meta("m1"),
        )
        .unwrap();
        <LanceDbStore as FtsIndex>::index(
            &mut store,
            "m2",
            "Python MCP server for memory recall",
            &make_meta("m2"),
        )
        .unwrap();
        let results =
            <LanceDbStore as FtsIndex>::search(&store, "Rust engine", 5, &SearchFilter::default())
                .unwrap();
        assert!(!results.is_empty());
        assert!(results[0].0 == "m1");
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn test_filter_by_scope() {
        let tmp = std::env::temp_dir().join("pp_test_lf2");
        let _ = std::fs::remove_dir_all(&tmp);
        let mut store = LanceDbStore::open(&tmp).unwrap();
        let meta_g = IndexMetadata {
            memory_id: "mg".into(),
            tier: "working".into(),
            category: "fact".into(),
            scope: "global".into(),
        };
        let meta_a = IndexMetadata {
            memory_id: "ma".into(),
            tier: "working".into(),
            category: "fact".into(),
            scope: "agent:test".into(),
        };
        <LanceDbStore as FtsIndex>::index(&mut store, "mg", "global memory", &meta_g).unwrap();
        <LanceDbStore as FtsIndex>::index(&mut store, "ma", "agent memory", &meta_a).unwrap();
        let filter = SearchFilter {
            scope: Some("agent:test".into()),
            tier: None,
            category: None,
        };
        let results =
            <LanceDbStore as FtsIndex>::search(&store, "memory", 5, &filter).unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].0, "ma");
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn test_delete_removes_from_all() {
        let tmp = std::env::temp_dir().join("pp_test_ld1");
        let _ = std::fs::remove_dir_all(&tmp);
        let mut store = LanceDbStore::open(&tmp).unwrap();
        <LanceDbStore as FtsIndex>::index(&mut store, "m1", "test text", &make_meta("m1"))
            .unwrap();
        <LanceDbStore as VectorIndex>::insert(
            &mut store,
            "m1",
            &vec![1.0f32; 1024],
            &make_meta("m1"),
        )
        .unwrap();
        <LanceDbStore as VectorIndex>::delete(&mut store, "m1").unwrap();
        assert!(<LanceDbStore as VectorIndex>::search(
            &store,
            &vec![1.0f32; 1024],
            5,
            &SearchFilter::default()
        )
        .unwrap()
        .is_empty());
        assert!(<LanceDbStore as FtsIndex>::search(
            &store,
            "test",
            5,
            &SearchFilter::default()
        )
        .unwrap()
        .is_empty());
        std::fs::remove_dir_all(&tmp).ok();
    }
}
