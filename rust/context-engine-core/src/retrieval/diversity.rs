//! Post-retrieval diversity and quality filters.

use std::collections::HashSet;

/// Length normalization — prevents long entries from dominating.
/// anchor: character count for normalization (default 500).
/// Returns multiplier in [0.5, 2.0].
pub fn length_norm(content: &str, anchor: usize) -> f64 {
    let len = content.chars().count().max(1) as f64;
    let ratio = len / anchor as f64;
    if ratio <= 1.0 {
        (1.0 + (1.0 - ratio) * 0.2).clamp(1.0, 1.2)
    } else {
        (1.0 / (1.0 + ratio.log2())).clamp(0.5, 1.0)
    }
}

/// Hard minimum score filter — removes items below threshold.
pub fn hard_min_score(items: Vec<(String, f64)>, min_score: f64) -> Vec<(String, f64)> {
    items.into_iter().filter(|(_, s)| *s >= min_score).collect()
}

/// MMR deduplication — removes near-duplicate items using Jaccard similarity.
/// Items: (id, score, content). threshold: Jaccard similarity above which items are deduped.
pub fn mmr_dedup(items: Vec<(String, f64, String)>, threshold: f64) -> Vec<(String, f64, String)> {
    let mut kept: Vec<(String, f64, String)> = Vec::new();
    for (id, score, content) in items {
        let is_dup = kept.iter().any(|(_, _, existing)| {
            jaccard_similarity(&content, existing) > threshold
        });
        if !is_dup {
            kept.push((id, score, content));
        }
    }
    kept
}

/// Jaccard similarity on word sets.
fn jaccard_similarity(a: &str, b: &str) -> f64 {
    let set_a: HashSet<&str> = a.split_whitespace().collect();
    let set_b: HashSet<&str> = b.split_whitespace().collect();
    let intersection = set_a.intersection(&set_b).count();
    let union = set_a.union(&set_b).count();
    if union == 0 {
        return 0.0;
    }
    intersection as f64 / union as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_length_norm_short_boost() {
        let s = length_norm("hello", 500);
        let l = length_norm(&"x".repeat(2000), 500);
        assert!(s > l);
    }

    #[test]
    fn test_hard_min_score() {
        let items = vec![("a".into(), 0.5), ("b".into(), 0.1), ("c".into(), 0.35)];
        let f = hard_min_score(items, 0.35);
        assert_eq!(f.len(), 2);
        assert_eq!(f[0].0, "a");
        assert_eq!(f[1].0, "c");
    }

    #[test]
    fn test_mmr_dedup_removes_duplicates() {
        let items = vec![
            ("a".into(), 0.9, "hello world rust".into()),
            ("b".into(), 0.7, "hello world python".into()),
            ("c".into(), 0.5, "completely different topic".into()),
        ];
        let result = mmr_dedup(items, 0.49);
        assert_eq!(result.len(), 2); // a and b are similar, b removed
    }
}
