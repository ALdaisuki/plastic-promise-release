//! Fusion layer — RRF reciprocal rank fusion + symbol-rule boosting.
//!
//! Absorbed from the original rank_fuser.rs with the dual-channel logic
//! simplified to free functions that operate on (id, score) tuples.

use std::collections::HashMap;

/// RRF smoothing constant — proven stable across IR benchmarks.
pub const RRF_K: f64 = 60.0;

/// Symbol rule keyword category with associated boost factor.
#[derive(Clone, Debug)]
pub struct SymbolRule {
    pub category: String,
    pub keywords: Vec<String>,
    /// When matched, the fusion score is multiplied by this factor.
    pub boost_factor: f64,
}

impl SymbolRule {
    /// Return the default set of 6 symbol rule categories.
    pub fn default_rules() -> Vec<Self> {
        vec![
            SymbolRule {
                category: "security".into(),
                keywords: vec!["安全", "漏洞", "权限", "密钥", "认证", "授权", "加密", "注入"]
                    .into_iter().map(String::from).collect(),
                boost_factor: 1.5,
            },
            SymbolRule {
                category: "quality".into(),
                keywords: vec!["质量", "测试", "覆盖率", "性能", "优化", "重构", "代码审查"]
                    .into_iter().map(String::from).collect(),
                boost_factor: 1.2,
            },
            SymbolRule {
                category: "commitment".into(),
                keywords: vec!["约定", "原则", "信任", "承诺", "边界", "伦理", "责任"]
                    .into_iter().map(String::from).collect(),
                boost_factor: 1.4,
            },
            SymbolRule {
                category: "learning".into(),
                keywords: vec!["学习", "反思", "技能", "演化", "适应", "成长", "进步"]
                    .into_iter().map(String::from).collect(),
                boost_factor: 1.0,
            },
            SymbolRule {
                category: "collaboration".into(),
                keywords: vec!["协作", "沟通", "共享", "同步", "对齐", "透明"]
                    .into_iter().map(String::from).collect(),
                boost_factor: 1.1,
            },
            SymbolRule {
                category: "innovation".into(),
                keywords: vec!["创新", "探索", "实验", "尝试", "假设", "新思路"]
                    .into_iter().map(String::from).collect(),
                boost_factor: 0.9,
            },
        ]
    }
}

/// RRF (Reciprocal Rank Fusion) — fuse multiple ranked result channels.
///
/// Each inner `Vec<(String, f64)>` is assumed to be sorted by score descending
/// (best result first). The position index (0-based) is used as rank.
///
/// score(item) = Σ (1 / (K + rank_i(item))) across all channels i.
pub fn rrf_fuse(channel_results: &[Vec<(String, f64)>]) -> Vec<(String, f64)> {
    let mut scores: HashMap<String, f64> = HashMap::new();

    for channel in channel_results {
        for (rank, (id, _score)) in channel.iter().enumerate() {
            let rrf_score = 1.0 / (RRF_K + (rank as f64 + 1.0));
            *scores.entry(id.clone()).or_insert(0.0) += rrf_score;
        }
    }

    let mut fused: Vec<(String, f64)> = scores.into_iter().collect();
    fused.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    fused
}

/// Apply symbol rules with dual-channel matching.
///
/// Checks keywords against both `task_description` AND `item_contents`,
/// multiplying the base score by each matched rule's boost factor.
/// Boost is clamped to [0.3, 3.0] per original RankFuser behaviour.
pub fn apply_symbol_rules(
    items: Vec<(String, f64)>,
    task_description: &str,
    item_contents: &HashMap<String, String>,
) -> Vec<(String, f64)> {
    let rules = SymbolRule::default_rules();

    items
        .into_iter()
        .map(|(id, base_score)| {
            let content = item_contents.get(&id).map(|s| s.as_str()).unwrap_or("");
            let mut boost = 1.0;

            for rule in &rules {
                let task_match = rule
                    .keywords
                    .iter()
                    .any(|kw| task_description.contains(kw));
                let content_match = rule
                    .keywords
                    .iter()
                    .any(|kw| content.contains(kw));

                // Either channel triggers the rule (dual-channel)
                if task_match || content_match {
                    boost *= rule.boost_factor;
                }
            }

            let clamped_boost = boost.clamp(0.3, 3.0);
            (id, base_score * clamped_boost)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rrf_fuse_deterministic() {
        let ch1 = vec![("a".into(), 0.9), ("b".into(), 0.7)];
        let ch2 = vec![("b".into(), 0.8), ("c".into(), 0.6)];
        let result = rrf_fuse(&[ch1, ch2]);
        assert_eq!(result[0].0, "b"); // b appears in both channels, should rank highest
    }

    #[test]
    fn test_rrf_single_channel() {
        let ch1 = vec![("x".into(), 0.9), ("y".into(), 0.5)];
        let result = rrf_fuse(&[ch1]);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].0, "x");
    }
}
