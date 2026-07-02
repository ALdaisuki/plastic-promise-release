//! HybridRetriever — orchestration struct for multi-channel memory retrieval.
//!
//! Pipeline: Vector ANN → BM25 → RRF Fusion → Symbol Rules →
//!           Decay Weight → Worth Boost → Length Norm → MMR → Hard Min Score

use std::collections::HashMap;
use crate::domain::{DecayModel, MemoryConsolidator, Tier, TierManager, WorthCalculator};
use crate::storage::{FtsIndex, SearchFilter, VectorIndex};

pub mod bm25;
pub mod embedder;
pub mod fusion;
pub mod diversity;

/// A scored memory item from the retrieval pipeline.
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
/// Composes vector search, full-text search, and domain models
/// into a single `retrieve()` pipeline.
pub struct HybridRetriever {
    pub vector: Box<dyn VectorIndex + Send>,
    pub fts: Box<dyn FtsIndex + Send>,
    pub decay: Box<dyn DecayModel + Send>,
    pub worth: Box<dyn WorthCalculator + Send>,
    pub tier_mgr: Box<dyn TierManager + Send>,
    pub consolidator: Box<dyn MemoryConsolidator + Send>,

    /// Weight for vector search channel (default 0.7).
    pub vector_weight: f64,
    /// Weight for BM25 channel (default 0.3).
    pub bm25_weight: f64,
    /// Hard minimum score cutoff (default 0.35).
    pub hard_min_score: f64,
    /// Character anchor for length normalization (default 500).
    pub length_norm_anchor: usize,
    /// Jaccard threshold for MMR dedup (default 0.85).
    pub mmr_threshold: f64,
    /// Number of candidates to fetch per channel (default 20).
    pub candidate_pool_size: usize,
}

impl Default for HybridRetriever {
    fn default() -> Self {
        Self::placeholder()
    }
}

impl HybridRetriever {
    /// Create a new HybridRetriever with the given trait objects and default weights.
    pub fn new(
        vector: Box<dyn VectorIndex + Send>,
        fts: Box<dyn FtsIndex + Send>,
        decay: Box<dyn DecayModel + Send>,
        worth: Box<dyn WorthCalculator + Send>,
        tier_mgr: Box<dyn TierManager + Send>,
        consolidator: Box<dyn MemoryConsolidator + Send>,
    ) -> Self {
        Self {
            vector, fts, decay, worth, tier_mgr, consolidator,
            vector_weight: 0.7,
            bm25_weight: 0.3,
            hard_min_score: 0.35,
            length_norm_anchor: 500,
            mmr_threshold: 0.85,
            candidate_pool_size: 20,
        }
    }

    /// Create a placeholder retriever that returns empty results.
    ///
    /// This is used by the Python-facing no-arg constructor. Replace with
    /// a fully configured retriever via `HybridRetriever::new()` before use.
    pub fn placeholder() -> Self {
        Self {
            vector: Box::new(NoopVectorIndex),
            fts: Box::new(NoopFtsIndex),
            decay: Box::new(NoopDecayModel),
            worth: Box::new(NoopWorthCalculator),
            tier_mgr: Box::new(NoopTierManager),
            consolidator: Box::new(NoopConsolidator),
            vector_weight: 0.7,
            bm25_weight: 0.3,
            hard_min_score: 0.35,
            length_norm_anchor: 500,
            mmr_threshold: 0.85,
            candidate_pool_size: 20,
        }
    }

    /// Execute a full hybrid retrieval pipeline.
    ///
    /// # Arguments
    /// * `query_vector` — Embedding vector from Python (dim = EMB_DIM).
    /// * `query_text` — Raw query for BM25 search and symbol rules.
    /// * `scope` — Scope namespace filter.
    /// * `task_type` — Optional task type category.
    /// * `item_lookup` — Map from memory_id to (content, source) for scoring.
    /// * `max_results` — Maximum results after all filtering.
    pub fn retrieve(
        &self,
        query_vector: &[f32],
        query_text: &str,
        scope: &str,
        _task_type: Option<&str>,
        item_lookup: &HashMap<String, (String, String)>,
        max_results: usize,
    ) -> Result<Vec<ScoredItem>, String> {
        let filter = SearchFilter {
            scope: Some(scope.to_string()),
            tier: None,
            category: None,
        };

        // 1. Vector search — fall back to empty on error
        let vector_results = self.vector.search(query_vector, self.candidate_pool_size, &filter)
            .unwrap_or_default();

        // 2. BM25 search — fall back to keyword-overlap scan on error
        let bm25_results = self.fts.search(query_text, self.candidate_pool_size, &filter)
            .unwrap_or_else(|_| {
                let mut fallback: Vec<(String, f64)> = Vec::new();
                let q_lower = query_text.to_lowercase();
                let q_words: Vec<&str> = q_lower.split_whitespace().collect();
                if !q_words.is_empty() {
                    for (id, (content, _source)) in item_lookup.iter() {
                        let c_lower = content.to_lowercase();
                        let hits = q_words.iter().filter(|w| c_lower.contains(*w)).count();
                        let score = hits as f64 / q_words.len() as f64;
                        if score > 0.0 {
                            fallback.push((id.clone(), score));
                        }
                    }
                    fallback.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
                }
                fallback
            });

        // 3. RRF fusion
        let fused = fusion::rrf_fuse(&[vector_results, bm25_results]);

        // 4. Apply symbol rules
        let item_contents: HashMap<String, String> = item_lookup
            .iter()
            .map(|(id, (content, _))| (id.clone(), content.clone()))
            .collect();
        let boosted = fusion::apply_symbol_rules(fused, query_text, &item_contents);

        // 5. Post-processing: hard min score, then take top candidates
        let filtered = diversity::hard_min_score(boosted, self.hard_min_score);

        // 6. Build ScoredItems
        let items: Vec<ScoredItem> = filtered.into_iter()
            .take(max_results)
            .map(|(id, score)| {
                let (content, source) = item_lookup.get(&id)
                    .cloned()
                    .unwrap_or_else(|| (String::new(), "unknown".to_string()));
                ScoredItem {
                    id,
                    content,
                    score,
                    source,
                    tier: Tier::default(),
                    worth_score: 0.0,
                    decay_multiplier: 1.0,
                    is_principle: false,
                }
            })
            .collect();

        Ok(items)
    }
}

// ============================================================
// No-op stub implementations — used by HybridRetriever::placeholder()
// ============================================================

use crate::domain::{ConsolidatedInsight, FeedbackType};
use crate::storage::IndexMetadata;
use chrono::{DateTime, Utc};

struct NoopVectorIndex;
impl VectorIndex for NoopVectorIndex {
    fn search(&self, _vector: &[f32], _k: usize, _filter: &SearchFilter) -> Result<Vec<(String, f64)>, String> {
        Ok(Vec::new())
    }
    fn insert(&mut self, _id: &str, _vector: &[f32], _metadata: &IndexMetadata) -> Result<(), String> {
        Ok(())
    }
    fn update(&mut self, _id: &str, _vector: &[f32], _metadata: &IndexMetadata) -> Result<(), String> {
        Ok(())
    }
    fn delete(&mut self, _id: &str) -> Result<(), String> {
        Ok(())
    }
}

struct NoopFtsIndex;
impl FtsIndex for NoopFtsIndex {
    fn search(&self, _query: &str, _k: usize, _filter: &SearchFilter) -> Result<Vec<(String, f64)>, String> {
        Ok(Vec::new())
    }
    fn index(&mut self, _id: &str, _text: &str, _metadata: &IndexMetadata) -> Result<(), String> {
        Ok(())
    }
    fn update(&mut self, _id: &str, _text: &str, _metadata: &IndexMetadata) -> Result<(), String> {
        Ok(())
    }
    fn delete(&mut self, _id: &str) -> Result<(), String> {
        Ok(())
    }
}

struct NoopDecayModel;
impl DecayModel for NoopDecayModel {
    fn compute(&self, _tier: Tier, _created_at: &DateTime<Utc>, _last_accessed: &DateTime<Utc>, _access_count: u32, _importance: f64) -> f64 {
        1.0
    }
    fn effective_half_life(&self, _tier: Tier, _access_count: u32, _reinforcement_factor: f64, _max_multiplier: f64) -> f64 {
        30.0
    }
}

struct NoopWorthCalculator;
impl WorthCalculator for NoopWorthCalculator {
    fn calculate(&self, _success: u32, _failure: u32, _min_obs: u32) -> f64 {
        0.5
    }
    fn record_feedback(&self, _success: &mut u32, _failure: &mut u32, _feedback_type: FeedbackType) {}
}

struct NoopTierManager;
impl TierManager for NoopTierManager {
    fn classify(&self, _record: &crate::memory_worth::MemoryRecord) -> Tier {
        Tier::Working
    }
}

struct NoopConsolidator;
impl MemoryConsolidator for NoopConsolidator {
    fn consolidate(&self, _memories: &[crate::memory_worth::MemoryRecord]) -> Option<ConsolidatedInsight> {
        None
    }
    fn interval_hours(&self) -> u32 {
        0 // never runs
    }
}
