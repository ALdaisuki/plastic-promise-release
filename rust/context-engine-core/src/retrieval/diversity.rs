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
        let is_dup = kept
            .iter()
            .any(|(_, _, existing)| jaccard_similarity(&content, existing) > threshold);
        if !is_dup {
            kept.push((id, score, content));
        }
    }
    kept
}

/// Result of soft MMR demotion.
#[derive(Clone, Debug, PartialEq)]
pub struct SoftMmrResult {
    pub items: Vec<(String, f64, String)>,
    pub demoted_count: usize,
}

/// Soft MMR-style diversity pass.
///
/// Unlike [`mmr_dedup`], this preserves every item. Near duplicates are demoted
/// and deferred, matching Python `_apply_mmr()` semantics used by recall:
/// keep the strongest item visible, lower duplicate scores, and let lower
/// layers still see the deferred evidence.
pub fn soft_mmr_demote(
    items: Vec<(String, f64, String)>,
    threshold: f64,
    duplicate_penalty: f64,
) -> SoftMmrResult {
    if items.len() <= 1 {
        return SoftMmrResult {
            items,
            demoted_count: 0,
        };
    }

    let mut sorted = items;
    sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    let mut selected: Vec<(String, f64, String)> = Vec::new();
    let mut deferred: Vec<(String, f64, String)> = Vec::new();
    let mut seen_contents: HashSet<String> = HashSet::new();
    let mut seen_prefixes: HashSet<String> = HashSet::new();
    let mut demoted_count = 0;

    for (id, score, content) in sorted {
        if id.starts_with("principle:") {
            selected.push((id, score, content));
            continue;
        }

        let content_key = normalized_prefix(&content, 80);
        let prefix_key = normalized_prefix(&content, 20);
        let exact_or_template_dup = (!content_key.is_empty()
            && seen_contents.contains(&content_key))
            || (!prefix_key.is_empty() && seen_prefixes.contains(&prefix_key));
        let semantic_dup = selected
            .iter()
            .rev()
            .take(5)
            .any(|(_, _, existing)| jaccard_similarity(&content, existing) > threshold);

        if exact_or_template_dup || semantic_dup {
            demoted_count += 1;
            deferred.push((id, score * duplicate_penalty, content));
            continue;
        }

        if !content_key.is_empty() {
            seen_contents.insert(content_key);
        }
        if !prefix_key.is_empty() {
            seen_prefixes.insert(prefix_key);
        }
        selected.push((id, score, content));
    }

    deferred.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    selected.extend(deferred);

    SoftMmrResult {
        items: selected,
        demoted_count,
    }
}

fn normalized_prefix(content: &str, max_chars: usize) -> String {
    content
        .chars()
        .take(max_chars)
        .collect::<String>()
        .trim()
        .to_lowercase()
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

    #[test]
    fn test_soft_mmr_demotes_duplicates_without_removing() {
        let items = vec![
            ("a".into(), 0.9, "hello world rust".into()),
            ("b".into(), 0.7, "hello world python".into()),
            ("c".into(), 0.5, "completely different topic".into()),
        ];

        let result = soft_mmr_demote(items, 0.49, 0.70);

        assert_eq!(result.items.len(), 3);
        assert_eq!(result.demoted_count, 1);
        assert_eq!(result.items[0].0, "a");
        let demoted = result.items.iter().find(|(id, _, _)| id == "b").unwrap();
        assert!((demoted.1 - 0.49).abs() < 1e-9);
    }

    #[test]
    fn test_soft_mmr_preserves_principles() {
        let items = vec![
            ("a".into(), 0.9, "same content body".into()),
            ("principle:1".into(), 0.8, "same content body".into()),
        ];

        let result = soft_mmr_demote(items, 0.20, 0.70);

        assert_eq!(result.demoted_count, 0);
        assert_eq!(result.items.len(), 2);
        assert!(result
            .items
            .iter()
            .any(|(id, score, _)| id == "principle:1" && *score == 0.8));
    }
}
