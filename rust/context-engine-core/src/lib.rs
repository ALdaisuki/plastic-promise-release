//! Plastic Promise — Context Engine Core (Rust)
//!
//! 上下文供应引擎的核心实现：
//! - EntityGraph: 实体关联图谱 + 原则注入
//! - RankFuser: RRF 融合 + 双通道符号规则
//! - SourceTracker: 来源追溯 + 时间有效性
//! - AssociationFeedback: 自演化反馈权重
//! - MemoryWorth: 双计数器计算 (ρ ≈ 0.89)
//! - ContextEngine: 主编排器

pub mod entity_graph;
pub mod rank_fuser;
pub mod source_tracker;
pub mod association_feedback;
pub mod memory_worth;
pub mod context_engine;
pub mod principles;

use pyo3::prelude::*;

/// Python 模块入口
#[pymodule]
fn context_engine_core(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_class::<context_engine::ContextEngine>()?;
    m.add_class::<entity_graph::EntityGraph>()?;
    m.add_class::<memory_worth::MemoryRecord>()?;
    Ok(())
}
