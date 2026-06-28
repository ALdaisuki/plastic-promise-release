//! AssociationFeedback — 自演化反馈权重
//!
//! 反馈权重:
//! - adopted: +0.10 (加强关联)
//! - ignored:  -0.05 (轻微衰减)
//! - rejected: -0.20 (显著衰减)
//!
//! 积累后应用到 RRF 融合排序和实体边权重更新。

use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// 反馈类型
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub enum FeedbackType {
    Adopted,
    Ignored,
    Rejected,
}

impl FeedbackType {
    pub fn weight_delta(&self) -> f64 {
        match self {
            FeedbackType::Adopted => 0.10,
            FeedbackType::Ignored => -0.05,
            FeedbackType::Rejected => -0.20,
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "adopted" => FeedbackType::Adopted,
            "ignored" => FeedbackType::Ignored,
            "rejected" => FeedbackType::Rejected,
            _ => FeedbackType::Ignored,
        }
    }
}

/// 单条反馈记录
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FeedbackRecord {
    pub item_id: String,
    pub feedback_type: FeedbackType,
    pub task_context: String,
    pub timestamp: String,
}

/// 自演化反馈管理器
///
/// 跟踪每条记忆/实体的反馈历史，计算累积权重调整。
#[pyclass]
#[derive(Default)]
pub struct AssociationFeedback {
    /// item_id -> feedback history
    history: HashMap<String, Vec<FeedbackRecord>>,
    /// item_id -> accumulated weight delta
    accumulated: HashMap<String, f64>,
}

#[pymethods]
impl AssociationFeedback {
    #[new]
    pub fn new() -> Self {
        Self {
            history: HashMap::new(),
            accumulated: HashMap::new(),
        }
    }

    /// 记录一条反馈
    pub fn record(
        &mut self,
        item_id: String,
        feedback_type: String,
        task_context: String,
        timestamp: String,
    ) {
        let fb_type = FeedbackType::from_str(&feedback_type);
        let delta = fb_type.weight_delta();

        let record = FeedbackRecord {
            item_id: item_id.clone(),
            feedback_type: fb_type,
            task_context,
            timestamp,
        };

        self.history.entry(item_id.clone()).or_default().push(record);

        let accumulated = self.accumulated.entry(item_id).or_insert(0.0);
        *accumulated = (*accumulated + delta).clamp(-1.0, 1.0);
    }

    /// 获取某条目的累积权重调整
    pub fn get_delta(&self, item_id: &str) -> f64 {
        self.accumulated.get(item_id).copied().unwrap_or(0.0)
    }

    /// 获取某条目的反馈历史摘要
    pub fn get_summary(&self, item_id: &str) -> String {
        match self.history.get(item_id) {
            None => "No feedback recorded".into(),
            Some(records) => {
                let adopted = records
                    .iter()
                    .filter(|r| r.feedback_type == FeedbackType::Adopted)
                    .count();
                let ignored = records
                    .iter()
                    .filter(|r| r.feedback_type == FeedbackType::Ignored)
                    .count();
                let rejected = records
                    .iter()
                    .filter(|r| r.feedback_type == FeedbackType::Rejected)
                    .count();
                format!(
                    "adopted={} ignored={} rejected={} delta={:.2}",
                    adopted,
                    ignored,
                    rejected,
                    self.get_delta(item_id)
                )
            }
        }
    }

    /// 获取所有有反馈记录的条目 ID
    pub fn tracked_items(&self) -> Vec<String> {
        self.history.keys().cloned().collect()
    }

    /// 反馈记录总数
    #[getter]
    pub fn record_count(&self) -> usize {
        self.history.values().map(|v| v.len()).sum()
    }

    /// 批量应用反馈权重到 RRF 排序结果
    ///
    /// Python 友好接口：接收 (id, score) 列表，返回调整后的列表
    pub fn apply_to_ranking(
        &self,
        ranked: Vec<(String, f64)>,
    ) -> Vec<(String, f64)> {
        ranked
            .into_iter()
            .map(|(id, score)| {
                let delta = self.get_delta(&id);
                // delta 直接加到 score 上（范围已在 [-1, 1]）
                (id, (score + delta).clamp(0.0, 2.0))
            })
            .collect()
    }
}
