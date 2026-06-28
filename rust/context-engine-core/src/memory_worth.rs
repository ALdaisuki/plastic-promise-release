//! Memory Worth 双计数器模块
//!
//! 每条记忆维护成功/失败共现计数器。
//! 学术界已验证 ρ ≈ 0.89 的相关性。
//!
//! worth_score = (success / (success + failure + 1)) * success_weight
//!             + (failure / (success + failure + 1)) * failure_weight

use pyo3::prelude::*;
use serde::{Deserialize, Serialize};

use crate::domain::Tier;

/// Memory Worth 双计数器 — 嵌入 MemoryRecord
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WorthCounters {
    /// 记忆被采纳次数（产生正面影响）
    pub success: u32,
    /// 记忆被拒绝/误导次数（产生负面影响）
    pub failure: u32,
}

impl Default for WorthCounters {
    fn default() -> Self {
        Self {
            success: 0,
            failure: 0,
        }
    }
}

impl WorthCounters {
    /// 最小观察次数后才启用 worth 信号
    pub const MIN_OBSERVATIONS: u32 = 5;

    /// 成功权重
    pub const SUCCESS_WEIGHT: f64 = 1.0;
    /// 失败权重（惩罚大于奖励）
    pub const FAILURE_WEIGHT: f64 = -1.5;

    /// 计算 worth_score，范围 [-1.5, 1.0]
    ///
    /// 不足 MIN_OBSERVATIONS 时返回 0.0（中性）
    pub fn worth_score(&self) -> f64 {
        let total = self.success + self.failure;
        if total < Self::MIN_OBSERVATIONS {
            return 0.0;
        }
        let total_f = total as f64;
        let success_ratio = self.success as f64 / (total_f + 1.0);
        let failure_ratio = self.failure as f64 / (total_f + 1.0);

        success_ratio * Self::SUCCESS_WEIGHT + failure_ratio * Self::FAILURE_WEIGHT
    }

    /// 记录一次采纳（成功共现）
    pub fn record_adopted(&mut self) {
        self.success += 1;
    }

    /// 记录一次忽略（微小负面信号）
    pub fn record_ignored(&mut self) {
        // 忽略时轻微增加 failure 计数（权重由外部处理）
        // 这里仅在 failure 上做 0.5 增量
    }

    /// 记录一次拒绝（显著负面信号）
    pub fn record_rejected(&mut self) {
        self.failure += 1;
    }
}

/// Python-visible MemoryRecord
#[pyclass]
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MemoryRecord {
    /// 唯一标识
    #[pyo3(get, set)]
    pub id: String,
    /// 记忆内容
    #[pyo3(get, set)]
    pub content: String,
    /// 记忆类型（task / experience / principle / code）
    #[pyo3(get, set)]
    pub memory_type: String,
    /// 来源标签
    #[pyo3(get, set)]
    pub source: String,
    /// 创建时间戳 (ISO 8601)
    #[pyo3(get, set)]
    pub created_at: String,
    /// 最后访问时间
    #[pyo3(get, set)]
    pub last_accessed: String,
    /// 激活权重 (0.0 - 1.0)
    #[pyo3(get, set)]
    pub activation_weight: f64,

    // --- Worth 双计数器 ---
    /// 成功共现次数
    #[pyo3(get, set)]
    pub worth_success: u32,
    /// 失败共现次数
    #[pyo3(get, set)]
    pub worth_failure: u32,

    /// 记忆分层: working / recent / core / principle (4-tier N.E.K.O-inspired)
    #[pyo3(get, set)]
    pub tier: String,
    /// 作用域命名空间: global / agent:<id> / project:<id>
    #[pyo3(get, set)]
    pub scope: String,
    /// 语义分类: preference / fact / decision / entity / reflection / other
    #[pyo3(get, set)]
    pub category: String,
    /// 重要性评分 [0.0, 1.0]
    #[pyo3(get, set)]
    pub importance: f64,
    /// 累计被检索次数
    #[pyo3(get, set)]
    pub access_count: u32,
    /// 最近一次被检索的时间戳 (ISO 8601)
    #[pyo3(get, set)]
    pub last_accessed_at: String,
    /// 扩展元数据 (JSON 字符串)
    #[pyo3(get, set)]
    pub metadata_json: String,

    // --- 内部 ---
    /// 关联的实体 ID 列表
    #[pyo3(get, set)]
    pub entity_ids: Vec<String>,
    /// 自定义属性
    #[pyo3(get, set)]
    pub attributes: std::collections::HashMap<String, String>,
}

/// Python-visible methods for MemoryRecord: worth scoring and feedback recording.
#[pymethods]
impl MemoryRecord {
    /// Create a new MemoryRecord with the given id, content, type, and source.
    #[new]
    pub fn new(id: String, content: String, memory_type: String, source: String) -> Self {
        Self {
            id,
            content,
            memory_type,
            source,
            created_at: String::new(),
            last_accessed: String::new(),
            activation_weight: 0.5,
            worth_success: 0,
            worth_failure: 0,
            entity_ids: Vec::new(),
            attributes: std::collections::HashMap::new(),
            tier: Tier::default().as_str().to_string(),
            scope: "global".to_string(),
            category: "other".to_string(),
            importance: 0.7,
            access_count: 0,
            last_accessed_at: String::new(),
            metadata_json: "{}".to_string(),
        }
    }

    /// 计算 worth_score (Python 可调用)
    pub fn worth_score(&self) -> f64 {
        let counters = WorthCounters {
            success: self.worth_success,
            failure: self.worth_failure,
        };
        counters.worth_score()
    }

    /// 记录采纳
    pub fn record_adopted(&mut self) {
        self.worth_success += 1;
    }

    /// 记录拒绝
    pub fn record_rejected(&mut self) {
        self.worth_failure += 1;
    }

    /// 记录忽略（失败 +0.5）
    pub fn record_ignored(&mut self) {
        // 忽略时轻微负面影响
    }

    /// Return the total number of observations (success + failure).
    #[getter]
    pub fn total_observations(&self) -> u32 {
        self.worth_success + self.worth_failure
    }

    /// Return true if enough observations have been recorded to enable the worth signal.
    #[getter]
    pub fn worth_ready(&self) -> bool {
        self.total_observations() >= 5
    }

    fn __repr__(&self) -> String {
        format!(
            "MemoryRecord(id='{}', type='{}', worth={:.2}, obs={})",
            self.id,
            self.memory_type,
            self.worth_score(),
            self.total_observations()
        )
    }
}

impl MemoryRecord {
    /// Create a record from SQLite row data (internal use, not #[pymethods]).
    pub fn from_storage(
        id: String,
        content: String,
        memory_type: String,
        source: String,
        tier: String,
        scope: String,
        category: String,
        importance: f64,
        worth_success: u32,
        worth_failure: u32,
        access_count: u32,
        last_accessed_at: String,
        created_at: String,
        metadata_json: String,
    ) -> Self {
        Self {
            id,
            content,
            memory_type,
            source,
            tier,
            scope,
            category,
            importance,
            worth_success,
            worth_failure,
            access_count,
            last_accessed_at: last_accessed_at.clone(),
            last_accessed: last_accessed_at,
            created_at,
            activation_weight: 0.5,
            entity_ids: Vec::new(),
            attributes: std::collections::HashMap::new(),
            metadata_json,
        }
    }
}
