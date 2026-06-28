//! ContextEngine — 主编排器
//!
//! supply(task, history) → ContextPack
//!
//! 流程：双路检索 → RRF 融合 → 符号规则 → 分层 → 追溯 → 审计
//!
//! 双路检索：
//! - 通道 A: 文本相似度检索 (基于关键词匹配 + 记忆内容)
//! - 通道 B: 图遍历检索 (EntityGraph.traverse)
//!
//! 融合后按 relevance 分为三层：🔵核心 / 🟡关联 / 🟢发散

use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use crate::association_feedback::AssociationFeedback;
use crate::entity_graph::EntityGraph;
use crate::memory_worth::MemoryRecord;
use crate::principles;
use crate::rank_fuser::{RankFuser, SearchResult};
use crate::source_tracker::SourceTracker;

// ============================================================
// Python-visible ContextPack
// ============================================================

/// 三层上下文包
///
/// 🔵 core: 必读——最高优先级，直接关联
/// 🟡 related: 补充——间接关联
/// 🟢 divergent: 灵感——低关联但有创意价值
#[pyclass]
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ContextPack {
    /// 🔵 核心层 (必读)
    #[pyo3(get)]
    pub core: Vec<ContextItem>,
    /// 🟡 关联层 (补充)
    #[pyo3(get)]
    pub related: Vec<ContextItem>,
    /// 🟢 发散层 (灵感)
    #[pyo3(get)]
    pub divergent: Vec<ContextItem>,
    /// 注入的原则列表
    #[pyo3(get)]
    pub activated_principles: Vec<String>,
    /// 审计元数据
    #[pyo3(get)]
    pub audit_metadata: HashMap<String, String>,
}

/// Python-visible methods for ContextPack: construction, prompt rendering, and stats.
#[pymethods]
impl ContextPack {
    /// Create an empty ContextPack with three empty layers (core, related, divergent).
    #[new]
    pub fn new() -> Self {
        Self {
            core: Vec::new(),
            related: Vec::new(),
            divergent: Vec::new(),
            activated_principles: Vec::new(),
            audit_metadata: HashMap::new(),
        }
    }

    /// 转换为可注入 Agent 决策的 prompt 字符串
    pub fn to_prompt(&self) -> String {
        let mut lines = Vec::new();

        if !self.activated_principles.is_empty() {
            lines.push("## 🧬 激活的核心原则".to_string());
            for p in &self.activated_principles {
                lines.push(format!("- {}", p));
            }
            lines.push(String::new());
        }

        if !self.core.is_empty() {
            lines.push("## 🔵 核心上下文（必读）".to_string());
            for item in &self.core {
                lines.push(item.to_prompt_line());
            }
            lines.push(String::new());
        }

        if !self.related.is_empty() {
            lines.push("## 🟡 关联上下文（参考）".to_string());
            for item in &self.related {
                lines.push(item.to_prompt_line());
            }
            lines.push(String::new());
        }

        if !self.divergent.is_empty() {
            lines.push("## 🟢 发散联想（灵感）".to_string());
            for item in &self.divergent {
                lines.push(item.to_prompt_line());
            }
            lines.push(String::new());
        }

        lines.join("\n")
    }

    /// 三层总条目数
    #[getter]
    pub fn total_items(&self) -> usize {
        self.core.len() + self.related.len() + self.divergent.len()
    }

    fn __repr__(&self) -> String {
        format!(
            "ContextPack(core={}, related={}, divergent={}, principles={})",
            self.core.len(),
            self.related.len(),
            self.divergent.len(),
            self.activated_principles.len()
        )
    }
}

/// 上下文包中的单个条目
#[pyclass]
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ContextItem {
    /// 条目 ID
    #[pyo3(get, set)]
    pub id: String,
    /// 内容摘要
    #[pyo3(get, set)]
    pub content: String,
    /// 关联分数
    #[pyo3(get, set)]
    pub relevance: f64,
    /// 来源追溯
    #[pyo3(get, set)]
    pub source: String,
    /// 新鲜度: fresh / valid / stale / expired
    #[pyo3(get, set)]
    pub freshness: String,
    /// 所属层级: core / related / divergent
    #[pyo3(get, set)]
    pub layer: String,
    /// 是否原则实体
    #[pyo3(get, set)]
    pub is_principle: bool,
    /// worth_score (如果有)
    #[pyo3(get, set)]
    pub worth_score: f64,
}

/// Python-visible methods for ContextItem: field access and prompt formatting.
#[pymethods]
impl ContextItem {
    /// Create a new ContextItem with the given id, content, and relevance score.
    #[new]
    pub fn new(id: String, content: String, relevance: f64) -> Self {
        Self {
            id,
            content,
            relevance,
            source: String::new(),
            freshness: "valid".into(),
            layer: "related".into(),
            is_principle: false,
            worth_score: 0.0,
        }
    }

    fn to_prompt_line(&self) -> String {
        let principle_mark = if self.is_principle { " 🧬" } else { "" };
        format!(
            "- [{:.2}]{} [{}] {}",
            self.relevance, principle_mark, self.source, self.content
        )
    }

    fn __repr__(&self) -> String {
        format!(
            "ContextItem(id='{}', relevance={:.2}, layer='{}')",
            self.id, self.relevance, self.layer
        )
    }
}

// ============================================================
// ContextEngine 主编排器
// ============================================================

/// 上下文供应引擎主编排器
///
/// # 流程
/// 1. 文本检索通道：关键词匹配记忆内容
/// 2. 图遍历通道：EntityGraph 多跳遍历 + 原则注入
/// 3. RRF 融合双路结果
/// 4. 符号规则双通道调整
/// 5. 自演化反馈权重应用
/// 6. 分层：core (>0.80) / related (>0.50) / divergent (>0.20)
/// 7. 来源追溯
#[pyclass]
pub struct ContextEngine {
    /// 内部 EntityGraph 实例
    graph: EntityGraph,
    /// RRF 融合器
    rank_fuser: RankFuser,
    /// 来源追溯
    source_tracker: SourceTracker,
    /// 反馈权重
    feedback: AssociationFeedback,
    /// 记忆存储（Python 侧可通过 MemoryRecord 管理，此处为引用缓存）
    memories: HashMap<String, MemoryRecord>,
    /// 是否启用原则注入
    pub enable_principles: bool,
    /// 当前时间戳 (ISO 8601)
    current_time: String,
}

/// Python-visible methods for ContextEngine: memory registration, supply, and graph management.
#[pymethods]
impl ContextEngine {
    /// Create a new ContextEngine with default sub-components (empty graph, rank fuser, tracker, feedback).
    #[new]
    pub fn new() -> Self {
        Self {
            graph: EntityGraph::new(),
            rank_fuser: RankFuser::new(),
            source_tracker: SourceTracker::new(),
            feedback: AssociationFeedback::new(),
            memories: HashMap::new(),
            enable_principles: true,
            current_time: String::new(),
        }
    }

    /// 注册一条记忆到引擎（Python 侧 MemoryRecord 的 Rust 副本）
    pub fn register_memory(&mut self, record: MemoryRecord) {
        self.memories.insert(record.id.clone(), record);
    }

    /// 批量注册记忆
    pub fn register_memories(&mut self, records: Vec<MemoryRecord>) {
        for record in records {
            self.register_memory(record);
        }
    }

    /// 获取已注册的记忆数量
    #[getter]
    pub fn memory_count(&self) -> usize {
        self.memories.len()
    }

    /// 设置当前时间（用于新鲜度计算）
    pub fn set_current_time(&mut self, iso_timestamp: String) {
        self.current_time = iso_timestamp;
    }

    /// 加载/更新 EntityGraph（从 Python 侧传入）
    pub fn load_graph(&mut self, graph: EntityGraph) {
        self.graph = graph;
    }

    /// 获取 EntityGraph 引用（用于 Python 侧持久化等）
    pub fn get_graph(&self) -> EntityGraph {
        self.graph.clone()
    }

    // ============================================================
    // 核心方法: supply()
    // ============================================================

    /// 供应上下文：输入任务描述和历史快照，返回结构化 ContextPack
    ///
    /// # Arguments
    /// - `task_description`: 当前任务的自然语言描述
    /// - `task_type`: 任务类型标签 (code_generation / code_review / debugging / ...)
    /// - `pre_context`: 已有的前文上下文（可选，用于增强检索）
    ///
    /// # Returns
    /// 包含三层上下文的 ContextPack
    pub fn supply(
        &mut self,
        task_description: String,
        task_type: String,
        pre_context: Option<String>,
    ) -> ContextPack {
        let pre_context = pre_context.unwrap_or_default();

        // === Phase 0: 原则注入 (P0 任务) ===
        let mut activated_principle_names = Vec::new();
        if self.enable_principles {
            let core_principles = principles::core_principles();

            // 提取任务关键词
            let task_keywords: Vec<String> = task_description
                .split(&[' ', '，', '。', '、', '\n', '\t'][..])
                .filter(|w| w.len() >= 2)
                .map(|w| w.to_string())
                .collect();

            let injected = self.graph.inject_principles(
                core_principles.clone(),
                &task_type,
                task_keywords,
            );

            if injected > 0 {
                activated_principle_names = self.graph.get_activated_principles(&task_type)
                    .into_iter()
                    .map(|(name, _)| name)
                    .collect();
            }
        }

        // === Phase 1: 双路检索 ===

        // 通道 A: 文本检索（关键词匹配 + 内容相似度）
        let text_channel = self.text_retrieval(&task_description, &pre_context);

        // 通道 B: 图遍历检索
        let graph_channel = self.graph_traversal(&task_type);

        // === Phase 2: RRF 融合 ===
        let fused = self.rank_fuser.fuse(vec![text_channel, graph_channel]);

        // === Phase 3: 符号规则双通道 ===
        let item_contents: HashMap<String, String> = self
            .memories
            .iter()
            .map(|(id, mem)| (id.clone(), mem.content.clone()))
            .collect();

        let boosted = self.rank_fuser.apply_symbol_rules(fused, &task_description, &item_contents);

        // === Phase 4: 自演化反馈权重 ===
        let rankings: Vec<(String, f64)> = boosted
            .into_iter()
            .map(|(id, score, _)| (id, score))
            .collect();
        let adjusted = self.feedback.apply_to_ranking(rankings);

        // === Phase 5: 分层 → ContextPack ===
        let mut pack = ContextPack::new();
        pack.activated_principles = activated_principle_names;

        for (item_id, score) in &adjusted {
            // 构建 ContextItem
            let content = self
                .memories
                .get(item_id)
                .map(|m| m.content.clone())
                .unwrap_or_else(|| format!("Item: {}", item_id));

            let worth = self
                .memories
                .get(item_id)
                .map(|m| m.worth_score())
                .unwrap_or(0.0);

            let is_principle = item_id.starts_with("principle:");

            let freshness = self
                .memories
                .get(item_id)
                .map(|m| {
                    crate::source_tracker::Freshness::from_timestamps(
                        &m.created_at,
                        &self.current_time,
                    )
                    .as_str()
                    .to_string()
                })
                .unwrap_or_else(|| "valid".into());

            let source = self
                .memories
                .get(item_id)
                .map(|m| m.source.clone())
                .unwrap_or_else(|| "unknown".into());

            let mut item = ContextItem::new(item_id.clone(), content, *score);
            item.source = source;
            item.freshness = freshness;
            item.is_principle = is_principle;
            item.worth_score = worth;

            // 按 relevance 分层
            if *score >= 0.80 {
                item.layer = "core".into();
                pack.core.push(item);
            } else if *score >= 0.50 {
                item.layer = "related".into();
                pack.related.push(item);
            } else if *score >= 0.20 {
                item.layer = "divergent".into();
                pack.divergent.push(item);
            }
            // score < 0.20 丢弃
        }

        // === Phase 6: 审计元数据 ===
        let mut audit = HashMap::new();
        audit.insert("engine_version".into(), "0.1.0".into());
        audit.insert("task_type".into(), task_type);
        audit.insert("principle_injection_count".into(),
            pack.activated_principles.len().to_string());
        audit.insert("graph_nodes".into(), self.graph.node_count().to_string());
        audit.insert("graph_edges".into(), self.graph.edge_count().to_string());
        audit.insert("memory_pool_size".into(), self.memories.len().to_string());
        audit.insert("timestamp".into(), self.current_time.clone());
        pack.audit_metadata = audit;

        pack
    }

    // ============================================================
    // 内部检索方法
    // ============================================================

    /// 通道 A: 文本相似度检索
    fn text_retrieval(&self, task: &str, pre_context: &str) -> Vec<SearchResult> {
        let mut results = Vec::new();

        for (id, memory) in &self.memories {
            // 简化版：关键词重叠度作为相似度
            let mut score = 0.0_f64;

            // 拆分任务为关键词
            let task_words: Vec<&str> = task
                .split(&[' ', '，', '。', '、', '\n', '\t'][..])
                .filter(|w| w.len() >= 2)
                .collect();

            for word in &task_words {
                if memory.content.contains(word) {
                    score += 1.0 / (task_words.len() as f64);
                }
                if pre_context.contains(word) {
                    score += 0.5 / (task_words.len() as f64);
                }
            }

            // 结合 worth_score 作为基础信号
            let worth = memory.worth_score();
            score = score * 0.7 + worth.abs().min(1.0) * 0.3;

            if score > 0.0 {
                results.push((id.clone(), score.clamp(0.0, 1.0)));
            }
        }

        RankFuser::as_channel_results(results, "text")
    }

    /// 通道 B: 图遍历检索
    fn graph_traversal(&self, task_type: &str) -> Vec<SearchResult> {
        let start_id = format!("task_type:{}", task_type);
        let traversed = self.graph.traverse(&start_id, 3);

        let results: Vec<(String, f64)> = traversed
            .into_iter()
            .map(|(id, weight, _hops)| {
                // 距离越远权重越低，已在 traverse 中体现
                (id, weight.clamp(0.0, 1.0))
            })
            .collect();

        RankFuser::as_channel_results(results, "graph")
    }
}
