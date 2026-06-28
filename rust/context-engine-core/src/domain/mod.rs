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
    fn record_feedback(
        &self,
        success: &mut u32,
        failure: &mut u32,
        feedback_type: FeedbackType,
    );
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
    fn effective_half_life(
        &self,
        tier: Tier,
        access_count: u32,
        reinforcement_factor: f64,
        max_multiplier: f64,
    ) -> f64;
}

pub trait TierManager {
    fn classify(&self, record: &crate::memory_worth::MemoryRecord) -> Tier;
}

pub trait MemoryConsolidator {
    fn consolidate(
        &self,
        memories: &[crate::memory_worth::MemoryRecord],
    ) -> Option<ConsolidatedInsight>;
    fn interval_hours(&self) -> u32;
}
