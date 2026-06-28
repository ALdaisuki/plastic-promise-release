//! 原则实体定义与注入逻辑
//!
//! 定义了 11 条核心原则的静态数据。
//! 与 constants.py 中的 CORE_PRINCIPLES 保持同步。

use pyo3::prelude::*;
use crate::entity_graph::Entity;
use std::collections::HashMap;

/// 11 条核心原则定义
pub fn core_principles() -> Vec<Entity> {
    let principles = vec![
        (1, "诚实优先于完美",
         "如果某个指标下降了，不要遮掩。数字身体的成长不是线性的，有起伏才是真实的。",
         "all",
         "诚实,透明,指标,下降,真实,报告"),
        (2, "约定优于约束",
         "Agent 遵守规则不是因为「被禁止」，而是因为「不想让在乎的人失望」。用内部动机替代外部强制。",
         "work",
         "约定,动机,规则,禁止,信任,自觉"),
        (3, "记忆主动供应而非被动查询",
         "记忆系统不是「被查询的档案库」，而是「主动供应上下文的引擎」。查找记忆的过程同时也是提示词注入的过程。",
         "all",
         "记忆,上下文,供应,查询,档案,引擎"),
        (4, "原则随记忆自然浮现",
         "原则不是靠防火墙强制执行的，而是在 Agent 检索历史决策时自然浮现的。联想不是「检索」，是「涌现」。",
         "work",
         "原则,浮现,检索,联想,涌现,自然"),
        (5, "存在性不等于有效性",
         "检查了「机制是否存在」不等于检查了「机制是否真的改变了行为」。要验证实际效果而非仅确认存在。",
         "work",
         "验证,效果,存在,检查,机制,行为改变"),
        (6, "连通性不等于协同性",
         "画了系统间的连通矩阵，但没有追踪数据是否真的在这些链路中流转。要追踪实际数据流。",
         "work",
         "连通,协同,数据流,链路,矩阵,追踪"),
        (7, "器官互相守护",
         "不增加新器官，让已有器官学会互相守护。每个系统的健康检查可以委托给相邻系统。",
         "all",
         "守护,协作,器官,委托,冗余,互相"),
        (8, "工具是 LLM 的唯一感官",
         "LLM 本质上是一个聋哑人，但不是一个智力残疾的聋哑人。工具是它唯一的感官和双手。",
         "all",
         "工具,感官,LLM,限制,能力,扩展"),
        (9, "信任换自主——动态约束",
         "信任分驱动的 L1↔L0 切换：高分放宽约束，低分收紧约束。信任是挣来的，不是默认给予的。",
         "work",
         "信任,自主,约束,动态,切换,挣取"),
        (10, "自演化闭环不可断裂",
         "行为→评价→信任变化→自主权调整 这四个环节缺一不可。任何一环断裂都会导致系统退化。",
         "all",
         "闭环,演化,评价,反馈,退化,连续性"),
        (11, "原则继承——单向扩散同步衰减",
         "work→all、life→all 单向扩散，核心约定跨 Agent 代际传递，但权重随传播距离同步衰减。",
         "all",
         "继承,扩散,衰减,传递,代际,同步"),
    ];

    principles
        .into_iter()
        .map(|(id, name, content, domain, keywords)| {
            let mut attrs = HashMap::new();
            attrs.insert("domain".to_string(), domain.to_string());
            attrs.insert("keywords".to_string(), keywords.to_string());

            Entity {
                id: format!("principle:{}", id),
                entity_type: "principle".to_string(),
                name: name.to_string(),
                description: content.to_string(),
                activation_weight: 0.5,
                attributes: attrs,
            }
        })
        .collect()
}

/// 根据任务类型获取推荐原则 ID 列表
pub fn recommended_principles(task_type: &str) -> Vec<&'static str> {
    match task_type {
        "code_generation" => vec!["principle:3", "principle:4", "principle:8", "principle:10"],
        "code_review" => vec!["principle:1", "principle:5", "principle:6", "principle:9"],
        "debugging" => vec!["principle:1", "principle:5", "principle:10"],
        "architecture" => vec!["principle:2", "principle:7", "principle:8"],
        "refactoring" => vec!["principle:5", "principle:6", "principle:7"],
        "learning" => vec!["principle:1", "principle:10", "principle:11"],
        "collaboration" => vec!["principle:2", "principle:7", "principle:9"],
        _ => vec!["principle:1", "principle:2", "principle:3", "principle:4"],
    }
}
