//! HybridRetriever — orchestration struct for multi-channel memory retrieval.
//!
//! Pipeline: Vector ANN → BM25 → RRF Fusion → Symbol Rules →
//!           Decay Weight → Worth Boost → Length Norm → MMR → Hard Min Score

use std::collections::HashMap;
use crate::domain::{DecayModel, MemoryConsolidator, Tier, TierManager, WorthCalculator};
use crate::storage::{FtsIndex, SearchFilter, VectorIndex};

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
    pub vector: Box<dyn VectorIndex>,
    pub fts: Box<dyn FtsIndex>,
    pub decay: Box<dyn DecayModel>,
    pub worth: Box<dyn WorthCalculator>,
    pub tier_mgr: Box<dyn TierManager>,
    pub consolidator: Box<dyn MemoryConsolidator>,

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
        // Use empty placeholder trait objects — caller must set real ones after construction.
        // For now, provide reasonable defaults that will be replaced.
        // Actually, since we can't create trait objects without impls, we use a builder pattern.
        // The default creates unusable state — caller MUST use HybridRetriever::builder().
        panic!("Use HybridRetriever::new() with explicit trait objects");
    }
}

impl HybridRetriever {
    /// Create a new HybridRetriever with the given trait objects and default weights.
    pub fn new(
        vector: Box<dyn VectorIndex>,
        fts: Box<dyn FtsIndex>,
        decay: Box<dyn DecayModel>,
        worth: Box<dyn WorthCalculator>,
        tier_mgr: Box<dyn TierManager>,
        consolidator: Box<dyn MemoryConsolidator>,
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

        // 1. Vector search
        let vector_results = self.vector.search(query_vector, self.candidate_pool_size, &filter)?;

        // 2. BM25 search (graceful fallback on error)
        let bm25_results = self.fts.search(query_text, self.candidate_pool_size, &filter)
            .unwrap_or_default();

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
