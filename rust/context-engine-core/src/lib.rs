//! Plastic Promise — Context Engine Core (Rust)
//!
//! 上下文供应引擎的核心实现：
//! - EntityGraph: 实体关联图谱 + 原则注入
//! - RankFuser: RRF 融合 + 双通道符号规则
//! - SourceTracker: 来源追溯 + 时间有效性
//! - AssociationFeedback: 自演化反馈权重
//! - MemoryWorth: 双计数器计算 (ρ ≈ 0.89)
//! - ContextEngine: 主编排器 → supply() 返回 ContextPack

pub mod domain;
pub mod entity_graph;
pub mod rank_fuser;
pub mod source_tracker;
pub mod association_feedback;
pub mod memory_worth;
pub mod context_engine;
pub mod principles;
pub mod storage;
pub mod retrieval;

use pyo3::prelude::*;

/// Python 模块入口 — `import context_engine_core`
///
/// 暴露的核心类：
/// - ContextEngine: 主编排器
/// - EntityGraph: 实体关联图谱
/// - Entity: 实体节点
/// - MemoryRecord: 含双计数器的记忆记录
/// - RankFuser: RRF 融合器
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
    m.add_class::<rank_fuser::RankFuser>()?;
    m.add_class::<source_tracker::SourceTracker>()?;
    m.add_class::<association_feedback::AssociationFeedback>()?;

    Ok(())
}
