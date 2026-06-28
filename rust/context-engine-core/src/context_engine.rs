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
use crate::principles;
use crate::retrieval::HybridRetriever;
use crate::source_tracker::SourceTracker;
use crate::storage::{ListFilter, StorageBackend};
use chrono::{DateTime, Utc};

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
/// 1. 原则注入：EntityGraph 按任务类型注入对应原则
/// 2. 混合检索：HybridRetriever (向量 ANN + BM25 + RRF 融合 + 符号规则)
/// 3. 图遍历补充：EntityGraph 遍历获取额外原则实体
/// 4. 自演化反馈权重应用
/// 5. 分层：core (>0.80) / related (>0.50) / divergent (>0.20)
/// 6. 来源追溯 + 审计
/// 7. 定期内存合并 (MemoryConsolidator)
#[pyclass]
pub struct ContextEngine {
    /// 内部 EntityGraph 实例
    graph: EntityGraph,
    /// 来源追溯
    source_tracker: SourceTracker,
    /// 反馈权重
    feedback: AssociationFeedback,
    /// 混合检索器 (向量 + BM25 + RRF + 符号规则)
    retriever: HybridRetriever,
    /// 持久化存储后端
    storage: Box<dyn StorageBackend + Send>,
    /// 是否启用原则注入
    pub enable_principles: bool,
    /// 当前时间戳 (ISO 8601)
    current_time: String,
    /// 上次内存合并时间
    last_consolidation: DateTime<Utc>,
}

/// Python-visible methods for ContextEngine: configuration, supply, and graph management.
#[pymethods]
impl ContextEngine {
    /// Create a new ContextEngine with default sub-components.
    ///
    /// For a fully configured engine with storage and retriever backends,
    /// construct via a Rust factory function or call `configure()` after construction.
    #[new]
    pub fn new() -> Self {
        // Placeholder retriever and storage — must be configured before use.
        // In practice, a Rust factory function will construct the full engine
        // with real SQLite and LanceDB backends, then return it to Python.
        Self {
            graph: EntityGraph::new(),
            source_tracker: SourceTracker::new(),
            feedback: AssociationFeedback::new(),
            retriever: HybridRetriever::placeholder(),
            storage: Box::new(
                crate::storage::sqlite_impl::SqliteStorage::open(":memory:")
                    .expect("Failed to create in-memory SQLite storage"),
            ),
            enable_principles: true,
            current_time: String::new(),
            last_consolidation: Utc::now(),
        }
    }

    /// 设置当前时间（用于新鲜度计算）
    pub fn set_current_time(&mut self, iso_timestamp: String) {
        self.current_time = iso_timestamp;
    }

    /// 加载/更新 EntityGraph（从 Python 侧传入 JSON 字符串）
    ///
    /// The JSON string must deserialize to a valid EntityGraph.
    pub fn load_graph(&mut self, graph_json: String) -> PyResult<()> {
        self.graph = serde_json::from_str(&graph_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid graph JSON: {}", e)))?;
        Ok(())
    }

    /// 获取 EntityGraph 引用（用于 Python 侧持久化等）
    pub fn get_graph(&self) -> EntityGraph {
        self.graph.clone()
    }

    // ============================================================
    // 核心方法: supply()
    // ============================================================

    /// 供应上下文：输入任务描述、向量、类型和范围，返回结构化 ContextPack
    ///
    /// # Arguments
    /// - `task_description`: 当前任务的自然语言描述
    /// - `task_vector`: 任务描述的嵌入向量 (dim = EMB_DIM)
    /// - `task_type`: 任务类型标签 (code_generation / code_review / debugging / ...)
    /// - `scope`: 命名空间/范围过滤
    ///
    /// # Returns
    /// 包含三层上下文的 ContextPack
    pub fn supply(
        &mut self,
        task_description: String,
        task_vector: Vec<f32>,
        task_type: String,
        scope: String,
    ) -> ContextPack {
        // ============================================================
        // Phase 0: 原则注入 (P0 任务)
        // ============================================================
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

        // ============================================================
        // Phase 1: 构建 item_lookup (从 StorageBackend)
        // ============================================================
        let filter = ListFilter {
            scope: Some(scope.clone()),
            ..Default::default()
        };
        let memories = self.storage.list(&filter).unwrap_or_default();

        let mut item_lookup: HashMap<String, (String, String)> = memories
            .iter()
            .map(|m| (m.id.clone(), (m.content.clone(), m.source.clone())))
            .collect();

        // 构建用于内容回溯的完整记忆索引
        let memory_index: HashMap<String, crate::memory_worth::MemoryRecord> = memories
            .into_iter()
            .map(|m| (m.id.clone(), m))
            .collect();

        // ============================================================
        // Phase 2: 混合检索 (向量 + BM25 + RRF + 符号规则)
        // ============================================================
        let max_results = 30;
        let scored_items = self
            .retriever
            .retrieve(
                &task_vector,
                &task_description,
                &scope,
                Some(&task_type),
                &item_lookup,
                max_results,
            )
            .unwrap_or_default();

        // Convert to (id, score) for feedback pipeline
        let mut all_rankings: Vec<(String, f64)> = scored_items
            .iter()
            .map(|item| (item.id.clone(), item.score))
            .collect();

        // ============================================================
        // Phase 3: 图遍历补充 (原则相关实体)
        // ============================================================
        let graph_items = self.graph_traversal(&task_type);
        for (gid, gscore, gcontent) in &graph_items {
            // Add graph principle items to lookup if not already present
            if !item_lookup.contains_key(gid) {
                item_lookup.insert(gid.clone(), ("graph".to_string(), gcontent.clone()));
            }
            // Add to rankings if not already present (with capped relevance)
            if !all_rankings.iter().any(|(i, _)| i == gid) {
                all_rankings.push((gid.clone(), gscore.min(0.85)));
            }
        }

        // ============================================================
        // Phase 4: 自演化反馈权重
        // ============================================================
        let adjusted = self.feedback.apply_to_ranking(all_rankings);

        // ============================================================
        // Phase 5: 分层 → ContextPack
        // ============================================================
        let mut pack = ContextPack::new();
        pack.activated_principles = activated_principle_names;

        for (item_id, score) in &adjusted {
            // 构建 ContextItem
            let content = memory_index
                .get(item_id)
                .map(|m| m.content.clone())
                .or_else(|| item_lookup.get(item_id).map(|(c, _)| c.clone()))
                .unwrap_or_else(|| format!("Item: {}", item_id));

            let worth = memory_index
                .get(item_id)
                .map(|m| m.worth_score())
                .unwrap_or(0.0);

            let is_principle = item_id.starts_with("principle:");

            let freshness = memory_index
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

            let source = memory_index
                .get(item_id)
                .map(|m| m.source.clone())
                .or_else(|| item_lookup.get(item_id).map(|(_, s)| s.clone()))
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

        // ============================================================
        // Phase 6: 定期内存合并检查
        // ============================================================
        let now = Utc::now();
        let interval_hours = self.retriever.consolidator.interval_hours();
        if interval_hours > 0 {
            let elapsed = now.signed_duration_since(self.last_consolidation);
            if elapsed.num_hours() >= interval_hours as i64 {
                // Collect all memories for consolidation
                let all_memories = self.storage.list(&ListFilter::default()).unwrap_or_default();
                if let Some(insight) = self.retriever.consolidator.consolidate(&all_memories) {
                    // Log consolidation for audit (the insight is not auto-stored here;
                    // the caller can decide whether to persist it via StorageBackend::store)
                    let _ = insight; // insight available for callers that chain consolidation
                }
                self.last_consolidation = now;
            }
        }

        // ============================================================
        // Phase 7: 审计元数据
        // ============================================================
        let mut audit = HashMap::new();
        audit.insert("engine_version".into(), "0.2.0".into());
        audit.insert("task_type".into(), task_type);
        audit.insert("scope".into(), scope);
        audit.insert("principle_injection_count".into(),
            pack.activated_principles.len().to_string());
        audit.insert("graph_nodes".into(), self.graph.node_count().to_string());
        audit.insert("graph_edges".into(), self.graph.edge_count().to_string());
        if let Ok(count) = self.storage.total_count() {
            audit.insert("memory_pool_size".into(), count.to_string());
        }
        audit.insert("timestamp".into(), self.current_time.clone());
        pack.audit_metadata = audit;

        pack
    }
}

// ============================================================
// Rust-internal impl block (non-Python)
// ============================================================

impl ContextEngine {
    /// Create a fully configured ContextEngine with real storage and retriever backends.
    ///
    /// This is the primary constructor for Rust-side factory functions.
    /// Python users receive a pre-configured engine via a PyO3 factory.
    pub fn new_configured(
        storage: Box<dyn StorageBackend + Send>,
        retriever: HybridRetriever,
    ) -> Self {
        Self {
            graph: EntityGraph::new(),
            source_tracker: SourceTracker::new(),
            feedback: AssociationFeedback::new(),
            retriever,
            storage,
            enable_principles: true,
            current_time: String::new(),
            last_consolidation: Utc::now(),
        }
    }

    // ============================================================
    // 内部检索方法
    // ============================================================

    /// 图遍历检索 — 从 EntityGraph 获取原则相关的补充条目
    ///
    /// Returns `Vec<(id, score, source_description)>` for principle entities
    /// reachable from the task_type node.
    fn graph_traversal(&self, task_type: &str) -> Vec<(String, f64, String)> {
        let start_id = format!("task_type:{}", task_type);
        let traversed = self.graph.traverse(&start_id, 3);

        traversed
            .into_iter()
            .map(|(id, weight, _hops)| {
                // Attempt to fetch entity name/description from graph for context
                let description = format!("Entity from graph: {}", id);
                (id, weight.clamp(0.0, 1.0), description)
            })
            .collect()
    }
}
