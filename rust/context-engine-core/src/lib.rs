//! Plastic Promise — Context Engine Core (Rust)
//!
//! 上下文供应引擎的核心实现：
//! - EntityGraph: 实体关联图谱 + 原则注入
//! - HybridRetriever: 向量 ANN + BM25 + RRF 融合 + 符号规则
//! - SourceTracker: 来源追溯 + 时间有效性
//! - AssociationFeedback: 自演化反馈权重
//! - MemoryWorth: 双计数器计算 (ρ ≈ 0.89)
//! - StorageBackend: SQLite 持久化存储
//! - Domain: 衰减模型 + Worth 计算 + Tier 管理 + 内存合并
//! - ContextEngine: 主编排器 → supply() 返回 ContextPack

pub mod association_feedback;
pub mod chunking;
pub mod context_engine;
pub mod domain;
pub mod entity_graph;
pub mod memory_worth;
pub mod principles;
pub mod retrieval;
pub mod source_tracker;
pub mod storage;

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyDict};
use std::collections::HashMap;

fn config_field<'py>(config: &'py PyAny, name: &str) -> PyResult<&'py PyAny> {
    if let Ok(dict) = config.downcast::<PyDict>() {
        return dict
            .get_item(name)?
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("fusion_config_field_missing"));
    }
    config.getattr(name)
}

#[pyfunction(name = "weighted_rrf_fuse")]
fn weighted_rrf_fuse_py(
    rankings: HashMap<String, Vec<(String, f64)>>,
    config: &PyAny,
) -> PyResult<Vec<(String, f64)>> {
    let k_value = config_field(config, "k")?;
    if k_value.is_instance_of::<PyBool>() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid_k:must_be_positive_integer",
        ));
    }
    let k_u64 = k_value.extract::<u64>().map_err(|_| {
        pyo3::exceptions::PyValueError::new_err("invalid_k:must_be_positive_integer")
    })?;
    let k = u32::try_from(k_u64).map_err(|_| {
        pyo3::exceptions::PyValueError::new_err("invalid_k:must_be_positive_integer:overflow")
    })?;
    if k == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid_k:must_be_positive_integer",
        ));
    }

    let channels = config_field(config, "channels")?.extract::<Vec<String>>()?;
    let weights = config_field(config, "weights")?.extract::<HashMap<String, f64>>()?;
    let windows = config_field(config, "windows")?.extract::<HashMap<String, usize>>()?;
    let config = retrieval::fusion::WrrfConfig {
        k,
        channels: channels.clone(),
        weights,
        windows,
    };
    let channel_results = channels
        .iter()
        .map(|channel| {
            (
                channel.clone(),
                rankings.get(channel).cloned().unwrap_or_default(),
            )
        })
        .collect::<Vec<_>>();
    if rankings.len() != channel_results.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid_rankings:channel_mismatch",
        ));
    }
    retrieval::fusion::weighted_rrf_fuse(&channel_results, &config)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

#[pyfunction(name = "structure_chunk_projection", signature = (text, target_chars, hard_chars=None, max_chunks=None))]
fn structure_chunk_projection_py(
    py: Python<'_>,
    text: &str,
    target_chars: usize,
    hard_chars: Option<usize>,
    max_chunks: Option<usize>,
) -> PyResult<Vec<PyObject>> {
    chunking::structure_chunk_projection(text, target_chars, hard_chars, max_chunks)
        .into_iter()
        .map(|row| {
            let item = PyDict::new(py);
            item.set_item("schema_version", row.schema_version)?;
            item.set_item("chunk_id", row.chunk_id)?;
            item.set_item("ordinal", row.ordinal)?;
            item.set_item("text", row.text)?;
            item.set_item("kind", row.kind)?;
            item.set_item("heading_path", row.heading_path)?;
            item.set_item("source_start", row.source_start)?;
            item.set_item("source_end", row.source_end)?;
            item.set_item("source_hash", row.source_hash)?;
            item.set_item("text_hash", row.text_hash)?;
            item.set_item("context_truncated", row.context_truncated)?;
            Ok(item.into())
        })
        .collect()
}

/// Python 模块入口 — `import context_engine_core`
///
/// 暴露的核心类：
/// - ContextEngine: 主编排器
/// - EntityGraph: 实体关联图谱
/// - Entity: 实体节点
/// - MemoryRecord: 含双计数器的记忆记录
/// - SourceTracker: 来源追溯
/// - AssociationFeedback: 反馈权重
/// - ContextPack: 三层上下文包
/// - ContextItem: 上下文条目
#[pymodule]
fn context_engine_core(_py: Python, m: &PyModule) -> PyResult<()> {
    // Core engine & data
    m.add_class::<context_engine::ContextEngine>()?;
    m.add_class::<context_engine::ContextPack>()?;
    m.add_class::<context_engine::ContextItem>()?;

    // Entity graph
    m.add_class::<entity_graph::EntityGraph>()?;
    m.add_class::<entity_graph::Entity>()?;

    // Memory
    m.add_class::<memory_worth::MemoryRecord>()?;

    // Pipeline components
    m.add_class::<source_tracker::SourceTracker>()?;
    m.add_class::<association_feedback::AssociationFeedback>()?;
    m.add_function(wrap_pyfunction!(weighted_rrf_fuse_py, m)?)?;
    m.add_function(wrap_pyfunction!(structure_chunk_projection_py, m)?)?;

    Ok(())
}
