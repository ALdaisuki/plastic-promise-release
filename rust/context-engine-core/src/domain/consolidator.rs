//! EvolveR memory consolidator — synthesizes higher-level insights
//! from clusters of related memories.
//!
//! When enough related memories accumulate within a time window
//! (default: >=5 same-category within 7 days, average worth >= 0.40),
//! the consolidator synthesizes them into a ConsolidatedInsight.

use std::collections::HashMap;

use chrono::{DateTime, Duration, Utc};
use std::sync::atomic::{AtomicU64, Ordering};

static CONSOLIDATION_COUNTER: AtomicU64 = AtomicU64::new(0);

use crate::domain::{ConsolidatedInsight, MemoryConsolidator};
use crate::memory_worth::MemoryRecord;

/// EvolveR memory consolidator with configurable thresholds.
pub struct EvolveRConsolidator {
    /// Minimum number of memories in a cluster to trigger consolidation.
    pub min_cluster_size: usize,
    /// Time window in hours for grouping memories.
    pub window_hours: i64,
    /// Minimum average worth score for a cluster to be consolidated.
    pub min_avg_worth: f64,
    /// How often consolidation should run, in hours.
    pub interval_hours: u32,
}

impl Default for EvolveRConsolidator {
    fn default() -> Self {
        Self {
            min_cluster_size: 5,
            window_hours: 168, // 7 days
            min_avg_worth: 0.40,
            interval_hours: 24, // daily
        }
    }
}

impl EvolveRConsolidator {
    fn parse_time(&self, s: &str) -> Option<DateTime<Utc>> {
        // Try RFC 3339 first, then fall back to naive parsing.
        if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
            return Some(dt.with_timezone(&Utc));
        }
        // Fallback: try NaiveDateTime with common formats
        let cleaned = s.trim();
        for fmt in &[
            "%Y-%m-%dT%H:%M:%S%.fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ] {
            if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(cleaned, fmt) {
                return Some(DateTime::<Utc>::from_naive_utc_and_offset(naive, Utc));
            }
        }
        // Try date-only
        if let Ok(naive) = chrono::NaiveDate::parse_from_str(cleaned, "%Y-%m-%d") {
            let dt = naive
                .and_hms_opt(0, 0, 0)
                .map(|ndt| DateTime::<Utc>::from_naive_utc_and_offset(ndt, Utc));
            return dt;
        }
        None
    }
}

impl MemoryConsolidator for EvolveRConsolidator {
    fn consolidate(&self, memories: &[MemoryRecord]) -> Option<ConsolidatedInsight> {
        let now = Utc::now();
        let cutoff = now - Duration::hours(self.window_hours);

        // Group memories by category, filtering to those within the time window.
        let mut groups: HashMap<String, Vec<&MemoryRecord>> = HashMap::new();
        for mem in memories {
            if let Some(created) = self.parse_time(&mem.created_at) {
                if created >= cutoff {
                    groups.entry(mem.category.clone()).or_default().push(mem);
                }
            }
        }

        // Find the best cluster: largest group meeting thresholds.
        let mut best: Option<(String, Vec<&MemoryRecord>, f64)> = None;
        for (cat, group) in &groups {
            if group.len() < self.min_cluster_size {
                continue;
            }
            let avg_worth: f64 =
                group.iter().map(|m| m.worth_score()).sum::<f64>() / group.len() as f64;
            if avg_worth < self.min_avg_worth {
                continue;
            }
            // Keep the group with the highest average worth (tie-break by size).
            match &best {
                None => {
                    best = Some((cat.clone(), group.clone(), avg_worth));
                }
                Some((_, _, best_worth)) => {
                    if avg_worth > *best_worth
                        || (avg_worth == *best_worth
                            && group.len() > best.as_ref().unwrap().1.len())
                    {
                        best = Some((cat.clone(), group.clone(), avg_worth));
                    }
                }
            }
        }

        let (category, cluster, confidence) = best?;

        // Synthesize insight content from the cluster's memory contents.
        let content_excerpts: Vec<&str> = cluster
            .iter()
            .map(|m| m.content.as_str())
            .filter(|c| !c.is_empty())
            .collect();
        let content = if content_excerpts.is_empty() {
            format!(
                "Consolidated insight from {} {} memories (confidence: {:.2})",
                cluster.len(),
                category,
                confidence
            )
        } else {
            let joined = content_excerpts.join(" | ");
            format!(
                "[EvolveR consolidation: {}] {}",
                category,
                if joined.len() > 512 {
                    format!("{}...", &joined[..509])
                } else {
                    joined
                }
            )
        };

        let source_ids: Vec<String> = cluster.iter().map(|m| m.id.clone()).collect();
        let id = format!(
            "cons-{}-{}",
            now.timestamp_nanos_opt().unwrap_or(0),
            CONSOLIDATION_COUNTER.fetch_add(1, Ordering::Relaxed)
        );

        Some(ConsolidatedInsight {
            id,
            content,
            source_ids,
            category,
            confidence,
            created_at: now,
        })
    }

    fn interval_hours(&self) -> u32 {
        self.interval_hours
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_mem(
        id: &str,
        category: &str,
        created_at: &str,
        worth_success: u32,
        worth_failure: u32,
    ) -> MemoryRecord {
        MemoryRecord::from_storage(
            id.to_string(),
            format!("content of {}", id),
            "experience".to_string(),
            "test".to_string(),
            "working".to_string(),
            "global".to_string(),
            category.to_string(),
            0.7,
            worth_success,
            worth_failure,
            0,
            String::new(),
            created_at.to_string(),
            "{}".to_string(),
        )
    }

    #[test]
    fn test_insufficient_memories_no_consolidation() {
        let c = EvolveRConsolidator::default();
        let memories = vec![make_mem("a", "fact", "2026-06-28T00:00:00Z", 20, 0)];
        assert!(c.consolidate(&memories).is_none());
    }

    #[test]
    fn test_enough_memories_triggers_consolidation() {
        let c = EvolveRConsolidator::default();
        let now = Utc::now();
        let ts = now.format("%Y-%m-%dT%H:%M:%SZ").to_string();
        let mut memories = Vec::new();
        for i in 0..6 {
            memories.push(make_mem(&format!("m{}", i), "fact", &ts, 20, 0));
        }
        let result = c.consolidate(&memories);
        assert!(result.is_some());
        let insight = result.unwrap();
        assert_eq!(insight.category, "fact");
        assert_eq!(insight.source_ids.len(), 6);
        assert!(insight.confidence > 0.8);
    }

    #[test]
    fn test_low_worth_no_consolidation() {
        let c = EvolveRConsolidator::default();
        let now = Utc::now();
        let ts = now.format("%Y-%m-%dT%H:%M:%SZ").to_string();
        let mut memories = Vec::new();
        for i in 0..6 {
            // All failures, worth will be low
            memories.push(make_mem(&format!("m{}", i), "fact", &ts, 0, 20));
        }
        assert!(c.consolidate(&memories).is_none());
    }
}
