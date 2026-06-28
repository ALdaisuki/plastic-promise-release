//! RankFuser — RRF 倒数排名融合 + 双通道符号规则
//!
//! 核心功能：
//! 1. RRF (Reciprocal Rank Fusion): 多路检索结果融合排序
//! 2. 双通道符号规则: 同时匹配任务描述和记忆内容，提升规则触发率

use pyo3::prelude::*;
use std::collections::HashMap;

/// RRF 平滑常数
pub const RRF_K: f64 = 60.0;

/// 符号规则关键词分类（6 类）
#[derive(Clone, Debug)]
pub struct SymbolRule {
    pub category: String,
    pub keywords: Vec<String>,
    /// 如果匹配，融合排序权重乘以此系数
    pub boost_factor: f64,
}

impl SymbolRule {
    /// Return the default set of 6 symbol rule categories with associated keywords and boost factors.
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

/// 搜索结果条目
#[derive(Clone, Debug)]
pub struct SearchResult {
    pub item_id: String,
    pub score: f64,
    pub source_channel: String,  // "graph" | "text" | "vector" | "symbol"
    pub rank: usize,              // 在该通道内的排名 (1-based)
}

/// RankFuser: RRF 融合 + 符号规则提升/降权
#[pyclass]
pub struct RankFuser {
    rules: Vec<SymbolRule>,
    /// 双通道模式：是否同时匹配任务描述和记忆内容
    pub dual_channel: bool,
}

/// Python-visible methods for RankFuser: fusion, symbol rules, and channel helpers.
#[pymethods]
impl RankFuser {
    /// Create a new RankFuser with default symbol rules and dual-channel mode enabled.
    #[new]
    pub fn new() -> Self {
        Self {
            rules: SymbolRule::default_rules(),
            dual_channel: true,
        }
    }

    /// RRF 融合多路检索结果
    ///
    /// score(item) = Σ (1 / (k + rank_i(item)))  对所有通道 i
    /// 其中 rank_i(item) 是 item 在通道 i 内的排名
    pub fn fuse(&self, channel_results: Vec<Vec<SearchResult>>) -> Vec<(String, f64)> {
        let mut scores: HashMap<String, f64> = HashMap::new();

        for channel in &channel_results {
            for result in channel {
                if result.rank == 0 {
                    continue; // 跳过未排名的
                }
                let rrf_score = 1.0 / (RRF_K + result.rank as f64);
                *scores.entry(result.item_id.clone()).or_insert(0.0) += rrf_score;
            }
        }

        // 按分数降序排列
        let mut fused: Vec<(String, f64)> = scores.into_iter().collect();
        fused.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        fused
    }

    /// 符号规则双通道匹配
    ///
    /// 同时匹配任务描述 (channel_task) 和记忆内容 (channel_content)，
    /// 提升规则触发率从 1/5 到 4/5+
    pub fn apply_symbol_rules(
        &self,
        ranked_items: Vec<(String, f64)>,
        task_description: &str,
        item_contents: &HashMap<String, String>, // item_id -> content
    ) -> Vec<(String, f64, Vec<String>)> {
        // (id, boosted_score, matched_categories)

        ranked_items
            .into_iter()
            .map(|(id, base_score)| {
                let content = item_contents.get(&id).map(|s| s.as_str()).unwrap_or("");
                let mut boost = 1.0;
                let mut matched = Vec::new();

                for rule in &self.rules {
                    let mut task_match = false;
                    let mut content_match = false;

                    if self.dual_channel {
                        // 双通道：同时检查任务描述和记忆内容
                        task_match = rule
                            .keywords
                            .iter()
                            .any(|kw| task_description.contains(kw));
                        content_match = rule
                            .keywords
                            .iter()
                            .any(|kw| content.contains(kw));
                    } else {
                        // 单通道：仅检查任务描述
                        task_match = rule
                            .keywords
                            .iter()
                            .any(|kw| task_description.contains(kw));
                    }

                    // 任一路径匹配即触发
                    if task_match || content_match {
                        boost *= rule.boost_factor;
                        matched.push(rule.category.clone());
                    }
                }

                // 限制 boost 在 [0.3, 3.0] 范围内
                let clamped_boost = boost.clamp(0.3, 3.0);

                (id, base_score * clamped_boost, matched)
            })
            .collect()
    }

    /// 获取所有符号规则类别
    pub fn rule_categories(&self) -> Vec<String> {
        self.rules.iter().map(|r| r.category.clone()).collect()
    }
}

// 为 Rust 内部使用提供的 impl
impl RankFuser {
    /// 将 (id, score) 列表转换为 SearchResult 格式
    pub fn as_channel_results(
        ids_and_scores: Vec<(String, f64)>,
        channel_name: &str,
    ) -> Vec<SearchResult> {
        // 按 score 降序排列以确定 rank
        let mut sorted = ids_and_scores;
        sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

        sorted
            .into_iter()
            .enumerate()
            .map(|(idx, (id, score))| SearchResult {
                item_id: id,
                score,
                source_channel: channel_name.to_string(),
                rank: idx + 1, // 1-based
            })
            .collect()
    }
}
