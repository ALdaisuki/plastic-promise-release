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
        if record.tier == Tier::Recent.as_str() && record.access_count >= 10 && worth >= 0.80 {
            return Tier::Core;
        }
        if record.tier == Tier::Working.as_str() && record.access_count >= 2 && worth >= 0.50 {
            return Tier::Recent;
        }
        if record.tier == Tier::Core.as_str() && worth < 0.15 {
            return Tier::Recent;
        }
        Tier::from_str(&record.tier).unwrap_or_default()
    }
}
