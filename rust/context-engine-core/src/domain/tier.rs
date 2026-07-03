//! Memory tier enum — aligned with Python L1/L2/L3 tiers.
//!
//! L1 → L2 → L3 → Principle. Each tier carries its own decay beta
//! and capacity limit; aliases for the older working/recent/core names
//! are accepted on read for backward compatibility.

use serde::{Deserialize, Serialize};

/// 4-tier memory classification with per-tier decay and capacity parameters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Tier {
    /// L1 working memory, ttl ~3 days, fast decay (β=1.5)
    Working,
    /// L2 recent memory, ttl 7 days, standard decay (β=1.2)
    Recent,
    /// L3 core memory, ttl 90 days, slow decay (β=0.7)
    Core,
    /// Identity/principle memory, permanent, very slow decay
    Principle,
}

impl Tier {
    /// Base half-life in days for this tier.
    pub fn base_half_life_days(&self) -> f64 {
        match self {
            Tier::Working => 3.0,
            Tier::Recent => 7.0,
            Tier::Core => 90.0,
            Tier::Principle => 3650.0, // effectively permanent
        }
    }

    /// Weibull decay shape parameter — higher = faster decay.
    pub fn decay_beta(&self) -> f64 {
        match self {
            Tier::Working => 1.5,
            Tier::Recent => 1.2,
            Tier::Core => 0.7,
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
            "L1" | "working" => Some(Tier::Working),
            "L2" | "recent" => Some(Tier::Recent),
            "L3" | "core" => Some(Tier::Core),
            "principle" | "Principle" => Some(Tier::Principle),
            _ => None,
        }
    }

    /// Convert to SQLite-compatible string.
    pub fn as_str(&self) -> &'static str {
        match self {
            Tier::Working => "L1",
            Tier::Recent => "L2",
            Tier::Core => "L3",
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

use crate::domain::TierManager;
use crate::memory_worth::MemoryRecord;

/// Default tier classification based on access_count + worth_score thresholds.
pub struct DefaultTierManager;

impl TierManager for DefaultTierManager {
    fn classify(&self, record: &MemoryRecord) -> Tier {
        if record.tier == Tier::Principle.as_str() {
            return Tier::Principle;
        }
        let worth = record.worth_score();
        let tier_normalized = Tier::from_str(&record.tier).unwrap_or_default();
        if tier_normalized == Tier::Recent && record.access_count >= 20 && worth >= 0.80 {
            return Tier::Core;
        }
        if tier_normalized == Tier::Working && record.access_count >= 5 && worth >= 0.50 {
            return Tier::Recent;
        }
        if tier_normalized == Tier::Core && worth < 0.15 {
            return Tier::Recent;
        }
        tier_normalized
    }
}
