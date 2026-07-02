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
use std::cell::{Cell, RefCell};
use std::collections::HashMap;

use crate::association_feedback::AssociationFeedback;
use crate::entity_graph::EntityGraph;
use crate::principles;
use crate::retrieval::HybridRetriever;
use crate::source_tracker::SourceTracker;
use crate::storage::{ListFilter, StorageBackend, UpdateFields};
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
    /// 内部 EntityGraph 实例 (RefCell for interior mutability — supply() is &self)
    graph: RefCell<EntityGraph>,
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
    /// 上次内存合并时间 (Cell allows mutation through &self)
    last_consolidation: Cell<DateTime<Utc>>,
    /// BM25 text retrieval index (version-checked lazy refresh)
    bm25_index: RefCell<crate::retrieval::bm25::Bm25Index>,
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
        // Use the real plastic_memory.db read-only when available,
        // fall back to :memory: for tests and isolated environments.
        let db_path = std::env::var("PLASTIC_DB_PATH")
            .unwrap_or_else(|_| "plastic_memory.db".to_string());
        let storage = if std::path::Path::new(&db_path).exists() {
            crate::storage::sqlite_impl::SqliteStorage::open_readonly(&db_path)
                .unwrap_or_else(|_| {
                    crate::storage::sqlite_impl::SqliteStorage::open(":memory:")
                        .expect("Failed to create in-memory SQLite storage")
                })
        } else {
            crate::storage::sqlite_impl::SqliteStorage::open(":memory:")
                .expect("Failed to create in-memory SQLite storage")
        };

        Self {
            graph: RefCell::new(EntityGraph::new()),
            source_tracker: SourceTracker::new(),
            feedback: AssociationFeedback::new(),
            retriever: HybridRetriever::placeholder(),
            storage: Box::new(storage),
            enable_principles: true,
            current_time: String::new(),
            last_consolidation: Cell::new(Utc::now()),
            bm25_index: RefCell::new(crate::retrieval::bm25::Bm25Index::new()),
        }
    }

    /// 设置当前时间（用于新鲜度计算）
    pub fn set_current_time(&mut self, iso_timestamp: String) {
        self.current_time = iso_timestamp;
    }

    /// Create a ContextEngine with real domain models (not placeholders).
    ///
    /// Vector + FTS channels use Noop stubs per architecture contract
    /// (Python owns LanceDB). Domain models use real implementations:
    /// WeibullDecay for tier-aware decay, WilsonWorthCalculator for
    /// statistically-sound worth scoring, DefaultTierManager for
    /// access-count-based tier promotion.
    ///
    /// The keyword-overlap BM25 fallback in HybridRetriever.retrieve()
    /// (retrieval/mod.rs:131-147) provides text-based retrieval when
    /// vector indices are unavailable.
    #[staticmethod]
    pub fn new_with_backends(_sqlite_path: String, _lancedb_path: String) -> PyResult<Self> {
        use crate::domain::decay::WeibullDecay;
        use crate::domain::worth::WilsonWorthCalculator;
        use crate::domain::tier::DefaultTierManager;
        // NoopVectorIndex / NoopFtsIndex are defined in retrieval/mod.rs:194,210
        use crate::retrieval::NoopVectorIndex;
        use crate::retrieval::NoopFtsIndex;
        use crate::retrieval::NoopConsolidator;

        let storage = crate::storage::sqlite_impl::SqliteStorage::open(":memory:")
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))?;

        let retriever = HybridRetriever::new(
            Box::new(NoopVectorIndex),                  // vector search: Python-side
            Box::new(NoopFtsIndex),                     // FTS: Python-side
            Box::new(WeibullDecay::default()),          // REAL decay model
            Box::new(WilsonWorthCalculator::default()), // REAL worth model
            Box::new(DefaultTierManager),               // REAL tier manager
            Box::new(NoopConsolidator),
        );

        Ok(Self {
            graph: RefCell::new(EntityGraph::new()),
            source_tracker: SourceTracker::new(),
            feedback: AssociationFeedback::new(),
            retriever,
            storage: Box::new(storage),
            enable_principles: true,
            current_time: String::new(),
            last_consolidation: Cell::new(Utc::now()),
            bm25_index: RefCell::new(crate::retrieval::bm25::Bm25Index::new()),
        })
    }

    /// 加载/更新 EntityGraph（从 Python 侧传入 JSON 字符串）
    ///
    /// The JSON string must deserialize to a valid EntityGraph.
    pub fn load_graph(&self, graph_json: String) -> PyResult<()> {
        *self.graph.borrow_mut() = serde_json::from_str(&graph_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid graph JSON: {}", e)))?;
        Ok(())
    }

    /// 获取 EntityGraph 引用（用于 Python 侧持久化等）
    pub fn get_graph(&self) -> EntityGraph {
        self.graph.borrow().clone()
    }

    // ============================================================
    // Storage delegation methods (exposed to Python)
    // ============================================================

    /// Store a memory record into the persistent backend.
    /// Returns the record's id on success.
    pub fn store_memory(&mut self, record: crate::memory_worth::MemoryRecord) -> PyResult<String> {
        self.storage.store(&record)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
    }

    /// Retrieve a memory record by id. Returns None if not found.
    pub fn get_memory(&self, id: String) -> PyResult<Option<crate::memory_worth::MemoryRecord>> {
        self.storage.get(&id)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
    }

    /// Update specific fields of a memory record.
    /// Only provided fields are modified; others are left unchanged.
    #[pyo3(signature = (id, *, content=None, importance=None, category=None))]
    pub fn update_memory(
        &mut self,
        id: String,
        content: Option<String>,
        importance: Option<f64>,
        category: Option<String>,
    ) -> PyResult<bool> {
        let updates = UpdateFields {
            content,
            importance,
            category,
            ..Default::default()
        };
        self.storage.update(&id, &updates)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
    }

    /// Delete a memory record by id (hard delete).
    /// Returns true if a row was deleted.
    pub fn delete_memory(&mut self, id: String) -> PyResult<bool> {
        self.storage.delete(&id)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
    }

    /// List memory records with optional filters.
    /// Results are ordered by last_accessed_at DESC.
    #[pyo3(signature = (memory_type=None, source=None, min_worth=None, limit=50, scope=None))]
    pub fn list_memories(
        &self,
        memory_type: Option<String>,
        source: Option<String>,
        min_worth: Option<f64>,
        limit: usize,
        scope: Option<String>,
    ) -> PyResult<Vec<crate::memory_worth::MemoryRecord>> {
        let filter = ListFilter {
            memory_type,
            source,
            min_worth,
            limit,
            scope,
            ..Default::default()
        };
        self.storage.list(&filter)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))
    }

    /// Return aggregate memory pool statistics as a JSON string.
    /// Optionally scoped to a namespace.
    pub fn memory_stats_json(&self, scope: Option<String>) -> PyResult<String> {
        let stats = self.storage.stats(scope.as_deref())
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))?;
        serde_json::to_string(&stats)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
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
    #[pyo3(signature = (task_description, task_vector, task_type, scope, memories))]
    pub fn supply(
        &self,
        task_description: String,
        task_vector: Vec<f32>,
        task_type: String,
        scope: String,
        memories: Vec<PyObject>,
    ) -> PyResult<ContextPack> {
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

            let injected = self.graph.borrow_mut().inject_principles(
                core_principles.clone(),
                &task_type,
                task_keywords,
            );

            if injected > 0 {
                activated_principle_names = self.graph.borrow().get_activated_principles(&task_type)
                    .into_iter()
                    .map(|(name, _)| name)
                    .collect();
            }
        }

        // ============================================================
        // Phase 1: 构建 item_lookup + 填充真实检索后端 (from Python objects)
        // ============================================================
        let (mut item_lookup, memory_index, mut real_vector, mut real_fts) = Python::with_gil(|py| -> PyResult<_> {
            let mut item_lookup: HashMap<String, (String, String)> = HashMap::new();
            let mut memory_index: HashMap<String, crate::memory_worth::MemoryRecord> = HashMap::new();
            // Real backends — replace Noop stubs for actual retrieval
            let mut vectors: Vec<(String, Vec<f32>, String, String, String)> = Vec::new();
            let mut texts: Vec<(String, String, String, String, String)> = Vec::new();

            for py_mem in &memories {
                let obj = py_mem.as_ref(py);
                let id: String = obj.get_item("id")
                    .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("missing id: {}", e)))?
                    .extract()
                    .map_err(|e| pyo3::exceptions::PyTypeError::new_err(format!("id not a string: {}", e)))?;
                let content: String = obj.get_item("content")
                    .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("missing content: {}", e)))?
                    .extract()
                    .map_err(|e| pyo3::exceptions::PyTypeError::new_err(format!("content not a string: {}", e)))?;
                let source: String = obj.get_item("source")
                    .ok()
                    .and_then(|v| v.extract().ok())
                    .unwrap_or_default();
                let memory_type: String = obj.get_item("memory_type")
                    .ok()
                    .and_then(|v| v.extract().ok())
                    .unwrap_or_else(|| "experience".to_string());
                let worth_success: u32 = obj.get_item("worth_success")
                    .ok()
                    .and_then(|v| v.extract().ok())
                    .unwrap_or(0);
                let worth_failure: u32 = obj.get_item("worth_failure")
                    .ok()
                    .and_then(|v| v.extract().ok())
                    .unwrap_or(0);
                let created_at: String = obj.get_item("created_at")
                    .ok()
                    .and_then(|v| v.extract().ok())
                    .unwrap_or_default();
                let last_accessed_val: String = obj.get_item("last_accessed")
                    .ok()
                    .and_then(|v| v.extract().ok())
                    .unwrap_or_default();

                // Extract vector if available (Python: mem.get("_vector"))
                let vec_opt: Option<Vec<f32>> = obj.get_item("_vector")
                    .ok()
                    .and_then(|v| v.extract().ok());
                let tier_val: String = obj.get_item("tier").ok()
                    .and_then(|v| v.extract().ok()).unwrap_or_else(|| "L1".to_string());
                let scope_val: String = obj.get_item("scope").ok()
                    .and_then(|v| v.extract().ok()).unwrap_or_else(|| "global".to_string());
                let category_val: String = obj.get_item("category").ok()
                    .and_then(|v| v.extract().ok()).unwrap_or_else(|| "other".to_string());

                if let Some(vec) = vec_opt {
                    if vec.len() == 1024 && vec.iter().any(|&v| v != 0.0) {
                        vectors.push((id.clone(), vec, content.clone(), tier_val.clone(), scope_val.clone()));
                    }
                }
                texts.push((id.clone(), content.clone(), tier_val.clone(), category_val.clone(), scope_val.clone()));

                item_lookup.insert(id.clone(), (content.clone(), source.clone()));
                memory_index.insert(id.clone(), crate::memory_worth::MemoryRecord {
                    id,
                    content,
                    source,
                    memory_type,
                    worth_success,
                    worth_failure,
                    created_at,
                    last_accessed: last_accessed_val,
                    last_accessed_at: String::new(),
                    activation_weight: 0.5,
                    tier: "working".to_string(),
                    scope: "global".to_string(),
                    category: "other".to_string(),
                    importance: 0.7,
                    access_count: 0,
                    metadata_json: String::new(),
                    entity_ids: Vec::new(),
                    attributes: HashMap::new(),
                    tags: Vec::new(),
                    domain: "uncategorized".to_string(),
                    decay_multiplier: 1.0,
                    effective_half_life: 3.0,
                });
            }
            Ok((item_lookup, memory_index, vectors, texts))
        })?;

        // Build a real retriever with actual data from Python (replaces Noop placeholders).
        // LanceDbStore implements both VectorIndex and FtsIndex — use a single instance.
        let ldb_tmp = std::env::temp_dir().join("pp_rust_retrieval");
        let mut ldb_store = crate::storage::lancedb_impl::LanceDbStore::open(&ldb_tmp)
            .unwrap_or_else(|_| panic!("LanceDbStore open failed"));
        for (id, vec, _content, tier, scope) in &real_vector {
            let meta = crate::storage::IndexMetadata {
                memory_id: id.clone(), tier: tier.clone(),
                category: "other".to_string(), scope: scope.clone(),
            };
            <crate::storage::lancedb_impl::LanceDbStore as crate::storage::VectorIndex>::insert(
                &mut ldb_store, id, vec, &meta).ok();
        }
        for (id, text, tier, category, scope) in &real_fts {
            let meta = crate::storage::IndexMetadata {
                memory_id: id.clone(), tier: tier.clone(),
                category: category.clone(), scope: scope.clone(),
            };
            <crate::storage::lancedb_impl::LanceDbStore as crate::storage::FtsIndex>::index(
                &mut ldb_store, id, text, &meta).ok();
        }

        // Note: LanceDbStore cannot be used as both VectorIndex AND FtsIndex in
        // HybridRetriever (ownership). Use the vector path for real retrieval,
        // BM25 falls back to keyword-overlap in retrieve() method naturally.

        // ============================================================
        // BM25 version check: rebuild index if memory_version changed
        // ============================================================
        let current_version: u64 = self.storage
            .query_scalar("SELECT version FROM memory_version")
            .unwrap_or(0);
        if self.bm25_index.borrow().version() != current_version {
            let all_docs: Vec<(String, String)> = item_lookup
                .iter()
                .map(|(id, (content, _))| (id.clone(), content.clone()))
                .collect();
            self.bm25_index.borrow_mut().rebuild(&all_docs, current_version);
        }

        // ============================================================
        // Phase 2: Real retrieval (vector from populated LanceDbStore + BM25 keyword fallback)
        // ============================================================
        let candidate_pool_size = 20;

        // Vector search: use real data from Python objects (replaces NoopVectorIndex)
        let filter = crate::storage::SearchFilter {
            scope: Some(scope.clone()), tier: None, category: None,
        };
        let vector_hits: Vec<(String, f64)> = <crate::storage::lancedb_impl::LanceDbStore as crate::storage::VectorIndex>::search(
            &ldb_store, &task_vector, candidate_pool_size, &filter,
        ).unwrap_or_default();

        // BM25: keyword-overlap fallback (same logic as old HybridRetriever.retrieve())
        let bm25_hits: Vec<(String, f64)> = {
            let mut hits: Vec<(String, f64)> = Vec::new();
            let q_lower = task_description.to_lowercase();
            let q_words: Vec<&str> = q_lower.split_whitespace().collect();
            if !q_words.is_empty() {
                for (id, (content, _source)) in item_lookup.iter() {
                    let c_lower = content.to_lowercase();
                    let matched = q_words.iter().filter(|w| c_lower.contains(*w)).count();
                    let score = matched as f64 / q_words.len() as f64;
                    if score > 0.0 {
                        hits.push((id.clone(), score));
                    }
                }
                hits.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            }
            hits
        };

        // RRF fusion
        let fused: Vec<(String, f64)> = crate::retrieval::fusion::rrf_fuse(
            &[vector_hits, bm25_hits],
        );

        // Build ScoredItems
        let max_results = 30;
        let scored_items: Vec<crate::retrieval::ScoredItem> = fused.into_iter()
            .take(max_results)
            .map(|(id, score)| {
                let (content, source) = item_lookup.get(&id)
                    .cloned()
                    .unwrap_or_else(|| (String::new(), "unknown".to_string()));
                crate::retrieval::ScoredItem {
                    id, content, score, source,
                    tier: crate::domain::Tier::default(),
                    worth_score: 0.0, decay_multiplier: 1.0, is_principle: false,
                }
            })
            .collect();

        // FALLBACK: if retriever is a placeholder and returns nothing,
        // return memories at relevance 0.50 in "related" tier.
        // Capped at 200 items to prevent O(n) blowup with large pools.
        // This guarantees Rust never returns emptier than Python would.
        if scored_items.is_empty() && !memory_index.is_empty() {
            let mut pack = ContextPack::new();
            pack.activated_principles = activated_principle_names;

            let max_return: usize = 200;
            for (idx, (id, mem)) in memory_index.iter().enumerate() {
                if idx >= max_return {
                    break;
                }
                let worth = mem.worth_score();
                let is_principle = id.starts_with("principle:");
                let freshness = crate::source_tracker::Freshness::from_timestamps(
                    &mem.created_at,
                    &self.current_time,
                )
                .as_str()
                .to_string();

                let mut item = ContextItem::new(id.clone(), mem.content.clone(), 0.50);
                item.source = mem.source.clone();
                item.freshness = freshness;
                item.layer = "related".into();
                item.is_principle = is_principle;
                item.worth_score = worth;
                pack.related.push(item);
            }

            // Audit metadata (lightweight — no graph stats in fallback path)
            let mut audit = HashMap::new();
            audit.insert("engine_version".into(), "0.2.0-rs-fallback".into());
            audit.insert("task_type".into(), task_type);
            audit.insert("scope".into(), scope);
            audit.insert(
                "principle_injection_count".into(),
                pack.activated_principles.len().to_string(),
            );
            audit.insert("memory_pool_size".into(), memory_index.len().to_string());
            audit.insert("timestamp".into(), self.current_time.clone());
            audit.insert("fallback".into(), "true".into());
            pack.audit_metadata = audit;

            return Ok(pack);
        }

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
            let elapsed = now.signed_duration_since(self.last_consolidation.get());
            if elapsed.num_hours() >= interval_hours as i64 {
                // Collect all memories for consolidation
                let all_memories = self.storage.list(&ListFilter::default()).unwrap_or_default();
                if let Some(insight) = self.retriever.consolidator.consolidate(&all_memories) {
                    // Log consolidation for audit (the insight is not auto-stored here;
                    // the caller can decide whether to persist it via StorageBackend::store)
                    let _ = insight; // insight available for callers that chain consolidation
                }
                self.last_consolidation.set(now);
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
        audit.insert("graph_nodes".into(), self.graph.borrow().node_count().to_string());
        audit.insert("graph_edges".into(), self.graph.borrow().edge_count().to_string());
        if let Ok(count) = self.storage.total_count() {
            audit.insert("memory_pool_size".into(), count.to_string());
        }
        audit.insert("timestamp".into(), self.current_time.clone());
        pack.audit_metadata = audit;

        Ok(pack)
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
            graph: RefCell::new(EntityGraph::new()),
            source_tracker: SourceTracker::new(),
            feedback: AssociationFeedback::new(),
            retriever,
            storage,
            enable_principles: true,
            current_time: String::new(),
            last_consolidation: Cell::new(Utc::now()),
            bm25_index: RefCell::new(crate::retrieval::bm25::Bm25Index::new()),
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
        let traversed = self.graph.borrow().traverse(&start_id, 3);

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
