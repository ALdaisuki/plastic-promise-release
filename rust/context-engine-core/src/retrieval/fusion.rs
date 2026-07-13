//! Fusion layer — RRF reciprocal rank fusion + symbol-rule boosting.
//!
//! Absorbed from the original rank_fuser.rs with the dual-channel logic
//! simplified to free functions that operate on (id, score) tuples.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

/// RRF smoothing constant — proven stable across IR benchmarks.
pub const RRF_K: f64 = 60.0;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WrrfConfig {
    pub k: u32,
    pub channels: Vec<String>,
    pub weights: HashMap<String, f64>,
    pub windows: HashMap<String, usize>,
}

fn validate_wrrf_config(config: &WrrfConfig) -> Result<(), String> {
    if config.k == 0 {
        return Err("invalid_k:must_be_positive_integer".into());
    }
    if config.channels.is_empty() {
        return Err("invalid_channels:duplicate_or_empty".into());
    }
    let canonical = ["vector", "bm25", "fts"];
    let expected: Vec<String> = canonical
        .iter()
        .filter(|channel| config.channels.iter().any(|item| item == **channel))
        .map(|channel| (*channel).to_string())
        .collect();
    let unique_count = config
        .channels
        .iter()
        .collect::<std::collections::HashSet<_>>()
        .len();
    if unique_count != config.channels.len() {
        return Err("invalid_channels:duplicate_or_empty".into());
    }
    if config.channels != expected {
        if config
            .channels
            .iter()
            .any(|channel| !canonical.contains(&channel.as_str()))
        {
            return Err("invalid_channels:unknown_channel".into());
        }
        return Err("invalid_channels:noncanonical_order".into());
    }

    let channel_keys: std::collections::HashSet<&String> = config.channels.iter().collect();
    if config
        .weights
        .keys()
        .collect::<std::collections::HashSet<_>>()
        != channel_keys
    {
        return Err("invalid_weights:channel_mismatch".into());
    }
    if config
        .windows
        .keys()
        .collect::<std::collections::HashSet<_>>()
        != channel_keys
    {
        return Err("invalid_windows:channel_mismatch".into());
    }
    if config
        .weights
        .values()
        .any(|weight| !weight.is_finite() || *weight < 0.0)
    {
        return Err("invalid_weights:must_be_finite_non_negative".into());
    }
    if !config.weights.values().any(|weight| *weight > 0.0) {
        return Err("invalid_weights:all_zero".into());
    }
    if config.windows.values().any(|window| *window == 0) {
        return Err("invalid_windows:must_be_positive_integer".into());
    }
    Ok(())
}

pub fn weighted_rrf_fuse(
    channel_results: &[(String, Vec<(String, f64)>)],
    config: &WrrfConfig,
) -> Result<Vec<(String, f64)>, String> {
    validate_wrrf_config(config)?;

    let result_channels: std::collections::HashSet<&String> =
        channel_results.iter().map(|(channel, _)| channel).collect();
    let config_channels: std::collections::HashSet<&String> = config.channels.iter().collect();
    if channel_results.len() != config.channels.len() || result_channels != config_channels {
        return Err("invalid_rankings:channel_mismatch".into());
    }

    let mut scores: HashMap<String, f64> = HashMap::new();
    for channel in &config.channels {
        let rankings = channel_results
            .iter()
            .find(|(name, _)| name == channel)
            .map(|(_, rows)| rows)
            .ok_or_else(|| "invalid_rankings:channel_mismatch".to_string())?;
        let mut canonical = rankings.clone();
        let mut seen = std::collections::HashSet::new();
        for (id, score) in &canonical {
            if id.is_empty() {
                return Err(format!("invalid_rankings:empty_id:{channel}"));
            }
            if !seen.insert(id.as_str()) {
                return Err(format!("invalid_rankings:duplicate_id:{channel}"));
            }
            if !score.is_finite() {
                return Err(format!("invalid_rankings:score:{channel}"));
            }
        }
        canonical.sort_by(|left, right| {
            right
                .1
                .partial_cmp(&left.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| left.0.cmp(&right.0))
        });
        let window = config.windows[channel];
        let weight = config.weights[channel];
        for (rank, (id, _raw_score)) in canonical.iter().take(window).enumerate() {
            let contribution = weight / (f64::from(config.k) + rank as f64 + 1.0);
            *scores.entry(id.clone()).or_insert(0.0) += contribution;
        }
    }

    let mut fused: Vec<(String, f64)> = scores.into_iter().collect();
    fused.sort_by(|left, right| {
        right
            .1
            .partial_cmp(&left.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.0.cmp(&right.0))
    });
    Ok(fused)
}

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
                keywords: vec![
                    "安全", "漏洞", "权限", "密钥", "认证", "授权", "加密", "注入",
                ]
                .into_iter()
                .map(String::from)
                .collect(),
                boost_factor: 1.5,
            },
            SymbolRule {
                category: "quality".into(),
                keywords: vec!["质量", "测试", "覆盖率", "性能", "优化", "重构", "代码审查"]
                    .into_iter()
                    .map(String::from)
                    .collect(),
                boost_factor: 1.2,
            },
            SymbolRule {
                category: "commitment".into(),
                keywords: vec!["约定", "原则", "信任", "承诺", "边界", "伦理", "责任"]
                    .into_iter()
                    .map(String::from)
                    .collect(),
                boost_factor: 1.4,
            },
            SymbolRule {
                category: "learning".into(),
                keywords: vec!["学习", "反思", "技能", "演化", "适应", "成长", "进步"]
                    .into_iter()
                    .map(String::from)
                    .collect(),
                boost_factor: 1.0,
            },
            SymbolRule {
                category: "collaboration".into(),
                keywords: vec!["协作", "沟通", "共享", "同步", "对齐", "透明"]
                    .into_iter()
                    .map(String::from)
                    .collect(),
                boost_factor: 1.1,
            },
            SymbolRule {
                category: "innovation".into(),
                keywords: vec!["创新", "探索", "实验", "尝试", "假设", "新思路"]
                    .into_iter()
                    .map(String::from)
                    .collect(),
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
                let task_match = rule.keywords.iter().any(|kw| task_description.contains(kw));
                let content_match = rule.keywords.iter().any(|kw| content.contains(kw));

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

    fn wrrf_config() -> WrrfConfig {
        WrrfConfig {
            k: 2,
            channels: vec!["vector".into(), "bm25".into()],
            weights: HashMap::from([("vector".into(), 0.6), ("bm25".into(), 0.4)]),
            windows: HashMap::from([("vector".into(), 3), ("bm25".into(), 3)]),
        }
    }

    #[test]
    fn test_weighted_rrf_uses_one_based_rank_and_id_tie_break() {
        let result = weighted_rrf_fuse(
            &[
                ("vector".into(), vec![("b".into(), 99.0), ("a".into(), 0.1)]),
                ("bm25".into(), vec![("a".into(), 500.0), ("b".into(), 1.0)]),
            ],
            &wrrf_config(),
        )
        .unwrap();

        assert_eq!(result[0].0, "b");
        assert_eq!(result[1].0, "a");
        let scores: HashMap<String, f64> = result.into_iter().collect();
        assert!((scores["a"] - (0.6 / 4.0 + 0.4 / 3.0)).abs() < 1e-15);
        assert!((scores["b"] - (0.6 / 3.0 + 0.4 / 4.0)).abs() < 1e-15);
    }

    #[test]
    fn test_weighted_rrf_rejects_duplicate_ids() {
        let error = weighted_rrf_fuse(
            &[
                ("vector".into(), vec![("a".into(), 1.0), ("a".into(), 0.5)]),
                ("bm25".into(), vec![]),
            ],
            &wrrf_config(),
        )
        .unwrap_err();

        assert_eq!(error, "invalid_rankings:duplicate_id:vector");
    }

    #[test]
    fn test_weighted_rrf_rejects_invalid_k() {
        let mut config = wrrf_config();
        config.k = 0;
        let error = weighted_rrf_fuse(
            &[("vector".into(), vec![]), ("bm25".into(), vec![])],
            &config,
        )
        .unwrap_err();

        assert_eq!(error, "invalid_k:must_be_positive_integer");
    }
}
