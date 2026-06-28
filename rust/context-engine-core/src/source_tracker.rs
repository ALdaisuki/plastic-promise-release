//! SourceTracker — 来源追溯 + 时间有效性
//!
//! 每个上下文条目标注来源和新鲜度标记：
//! - 来源类型: user / system / previous_output / inherited / injected
//! - 时间有效性: fresh (< 6h) / valid (< 7d) / stale (< 30d) / expired (> 30d)

use pyo3::prelude::*;
use serde::{Deserialize, Serialize};

/// 来源类型
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum SourceType {
    User,
    System,
    PreviousOutput,
    Inherited,
    Injected,
}

impl SourceType {
    pub fn as_str(&self) -> &'static str {
        match self {
            SourceType::User => "user",
            SourceType::System => "system",
            SourceType::PreviousOutput => "previous_output",
            SourceType::Inherited => "inherited",
            SourceType::Injected => "injected",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s {
            "user" => SourceType::User,
            "system" => SourceType::System,
            "previous_output" => SourceType::PreviousOutput,
            "inherited" => SourceType::Inherited,
            "injected" => SourceType::Injected,
            _ => SourceType::System,
        }
    }
}

/// 时间有效性
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Freshness {
    /// < 6 小时
    Fresh,
    /// < 7 天
    Valid,
    /// < 30 天
    Stale,
    /// > 30 天
    Expired,
}

impl Freshness {
    pub fn as_str(&self) -> &'static str {
        match self {
            Freshness::Fresh => "fresh",
            Freshness::Valid => "valid",
            Freshness::Stale => "stale",
            Freshness::Expired => "expired",
        }
    }

    /// 根据创建时间（ISO 8601）和当前时间计算新鲜度
    pub fn from_timestamps(created_at: &str, now: &str) -> Self {
        // 简化实现：解析 ISO 8601 时间戳的天数差
        // 完整实现需 chrono crate；此处使用字符串前缀对比
        let created_date = &created_at[..10.min(created_at.len())]; // YYYY-MM-DD
        let now_date = &now[..10.min(now.len())];

        if created_date == now_date {
            return Freshness::Fresh;
        }

        // 粗略计算天数差（简化版，不处理月份边界）
        if let (Ok(created_days), Ok(now_days)) = (
            approximate_days(created_date),
            approximate_days(now_date),
        ) {
            let diff = now_days - created_days;
            if diff <= 1 {
                Freshness::Fresh
            } else if diff <= 7 {
                Freshness::Valid
            } else if diff <= 30 {
                Freshness::Stale
            } else {
                Freshness::Expired
            }
        } else {
            Freshness::Valid // 保守默认
        }
    }
}

/// 粗略估算从 Year 0 到指定日期的天数（仅用于计算差值）
fn approximate_days(date_str: &str) -> Result<i64, ()> {
    let parts: Vec<&str> = date_str.split('-').collect();
    if parts.len() != 3 {
        return Err(());
    }
    let year: i64 = parts[0].parse().map_err(|_| ())?;
    let month: i64 = parts[1].parse().map_err(|_| ())?;
    let day: i64 = parts[2].parse().map_err(|_| ())?;
    Ok(year * 365 + month * 30 + day)
}

/// 来源追溯条目
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SourceTrace {
    /// 条目 ID
    pub item_id: String,
    /// 来源类型
    pub source_type: SourceType,
    /// 来源详情（谁/什么产生了它）
    pub source_detail: String,
    /// 创建时间 (ISO 8601)
    pub created_at: String,
    /// 新鲜度
    pub freshness: Freshness,
    /// 是否可验证（关键决策需第三方独立验证）
    pub verifiable: bool,
}

/// SourceTracker: 批量追溯和标记
#[pyclass]
#[derive(Default)]
pub struct SourceTracker {
    traces: Vec<SourceTrace>,
}

#[pymethods]
impl SourceTracker {
    #[new]
    pub fn new() -> Self {
        Self { traces: Vec::new() }
    }

    /// 添加追溯条目
    pub fn trace(
        &mut self,
        item_id: String,
        source_type: String,
        source_detail: String,
        created_at: String,
        now: String,
        verifiable: bool,
    ) {
        let freshness = Freshness::from_timestamps(&created_at, &now);
        self.traces.push(SourceTrace {
            item_id,
            source_type: SourceType::from_str(&source_type),
            source_detail,
            created_at,
            freshness,
            verifiable,
        });
    }

    /// 获取某条目的追溯信息
    pub fn get_trace(&self, item_id: &str) -> Option<String> {
        self.traces.iter().find(|t| t.item_id == item_id).map(|t| {
            format!(
                "source={} detail={} freshness={} verifiable={}",
                t.source_type.as_str(),
                t.source_detail,
                t.freshness.as_str(),
                t.verifiable
            )
        })
    }

    /// 获取所有过期条目
    pub fn expired_items(&self) -> Vec<String> {
        self.traces
            .iter()
            .filter(|t| t.freshness == Freshness::Expired)
            .map(|t| t.item_id.clone())
            .collect()
    }

    /// 获取所有可验证的关键决策条目
    pub fn verifiable_items(&self) -> Vec<String> {
        self.traces
            .iter()
            .filter(|t| t.verifiable)
            .map(|t| t.item_id.clone())
            .collect()
    }

    /// 追溯条目数量
    #[getter]
    pub fn trace_count(&self) -> usize {
        self.traces.len()
    }
}
