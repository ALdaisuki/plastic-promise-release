//! 实体关联图谱 — EntityGraph
//!
//! 核心数据结构：存储实体节点和有向加权边。
//! 支持：
//! - 节点/边 CRUD + 持久化 (JSON)
//! - 多跳遍历 (BFS/DFS)
//! - 原则图谱注入：inject_principles(task_type)
//! - 共激活权重更新
//!
//! P0 任务：原则图谱注入在此模块实现

use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// 实体节点
#[pyclass]
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Entity {
    /// 唯一标识
    #[pyo3(get, set)]
    pub id: String,
    /// 实体类型: task / principle / code_module / memory / skill / experience
    #[pyo3(get, set)]
    pub entity_type: String,
    /// 显示名称
    #[pyo3(get, set)]
    pub name: String,
    /// 描述
    #[pyo3(get, set)]
    pub description: String,
    /// 激活权重 (0.0 - 1.0)
    #[pyo3(get, set)]
    pub activation_weight: f64,
    /// 自定义属性
    #[pyo3(get, set)]
    pub attributes: HashMap<String, String>,
}

#[pymethods]
impl Entity {
    #[new]
    pub fn new(id: String, entity_type: String, name: String, description: String) -> Self {
        Self {
            id,
            entity_type,
            name,
            description,
            activation_weight: 0.5,
            attributes: HashMap::new(),
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "Entity(id='{}', type='{}', name='{}', weight={:.2})",
            self.id, self.entity_type, self.name, self.activation_weight
        )
    }
}

/// 有向关联边
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EntityEdge {
    /// 源节点 ID
    pub from: String,
    /// 目标节点 ID
    pub to: String,
    /// 关系类型: activates / related_to / inherits_from / constrains / uses
    pub relation_type: String,
    /// 关联权重 (0.0 - 1.0)
    pub weight: f64,
    /// 共激活次数
    pub co_activation_count: u32,
}

impl EntityEdge {
    pub fn new(from: String, to: String, relation_type: String, weight: f64) -> Self {
        Self {
            from,
            to,
            relation_type,
            weight,
            co_activation_count: 0,
        }
    }
}

/// 实体关联图谱
///
/// 图遍历通道是上下文供应引擎的核心检索路径之一。
/// 原则注入在此实现：P0 任务
#[pyclass]
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct EntityGraph {
    /// 节点表
    nodes: HashMap<String, Entity>,
    /// 邻接表: node_id -> [(target_id, edge)]
    adjacency: HashMap<String, Vec<(String, EntityEdge)>>,
}

#[pymethods]
impl EntityGraph {
    #[new]
    pub fn new() -> Self {
        Self {
            nodes: HashMap::new(),
            adjacency: HashMap::new(),
        }
    }

    // ========== 节点操作 ==========

    /// 添加节点
    pub fn add_node(&mut self, entity: Entity) {
        let id = entity.id.clone();
        self.nodes.insert(id.clone(), entity);
        self.adjacency.entry(id).or_default();
    }

    /// 获取节点
    pub fn get_node(&self, id: &str) -> Option<Entity> {
        self.nodes.get(id).cloned()
    }

    /// 移除节点
    pub fn remove_node(&mut self, id: &str) -> bool {
        self.nodes.remove(id);
        self.adjacency.remove(id);
        // 清理所有指向该节点的边
        for edges in self.adjacency.values_mut() {
            edges.retain(|(target, _)| target != id);
        }
        true
    }

    /// 节点数量
    #[getter]
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    /// 所有节点 ID 和名称
    pub fn list_nodes(&self) -> Vec<(String, String, String)> {
        self.nodes
            .iter()
            .map(|(id, e)| (id.clone(), e.entity_type.clone(), e.name.clone()))
            .collect()
    }

    // ========== 边操作 ==========

    /// 添加边
    pub fn add_edge(
        &mut self,
        from: String,
        to: String,
        relation_type: String,
        weight: f64,
    ) {
        // 确保两端节点存在
        self.adjacency.entry(from.clone()).or_default();
        // 添加边
        let edge = EntityEdge::new(from.clone(), to.clone(), relation_type, weight);
        if let Some(edges) = self.adjacency.get_mut(&from) {
            // 去重：如果同类型边已存在，更新权重
            if let Some(existing) = edges.iter_mut().find(|(t, e)| t == &to && e.relation_type == edge.relation_type) {
                existing.1.weight = (existing.1.weight + weight) / 2.0; // 平滑更新
                existing.1.co_activation_count += 1;
            } else {
                edges.push((to, edge));
            }
        }
    }

    /// 边数量
    #[getter]
    pub fn edge_count(&self) -> usize {
        self.adjacency.values().map(|v| v.len()).sum()
    }

    /// 从指定节点出发的所有边
    pub fn edges_from(&self, node_id: &str) -> Vec<(String, String, String, f64)> {
        self.adjacency
            .get(node_id)
            .map(|edges| {
                edges
                    .iter()
                    .map(|(target, edge)| {
                        (
                            edge.from.clone(),
                            target.clone(),
                            edge.relation_type.clone(),
                            edge.weight,
                        )
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    // ========== 图遍历 ==========

    /// 多跳遍历（BFS）：从起始节点出发，返回 N 跳内可达的所有实体
    pub fn traverse(&self, start_id: &str, max_hops: usize) -> Vec<(String, f64, usize)> {
        let mut visited: HashMap<String, (f64, usize)> = HashMap::new();
        let mut frontier: Vec<(String, f64, usize)> = vec![(start_id.to_string(), 1.0, 0)];

        while let Some((current, accumulated_weight, hops)) = frontier.pop() {
            if hops > max_hops {
                continue;
            }

            let effective_weight = accumulated_weight * (0.8_f64.powi(hops as i32));
            let entry = visited.entry(current.clone()).or_insert((0.0, hops));
            entry.0 = f64::max(entry.0, effective_weight);

            if hops < max_hops {
                if let Some(edges) = self.adjacency.get(&current) {
                    for (target, edge) in edges {
                        if !visited.contains_key(target) {
                            frontier.push((
                                target.clone(),
                                accumulated_weight * edge.weight,
                                hops + 1,
                            ));
                        }
                    }
                }
            }
        }

        visited.remove(start_id); // 排除起始节点自身
        visited
            .into_iter()
            .map(|(id, (weight, hops))| (id, weight, hops))
            .collect()
    }

    // ========== 原则图谱注入 (P0) ==========

    /// 原则图谱注入：将原则实体注入图中，并建立任务类型→原则的关联边
    ///
    /// 这是 P0 任务的核心实现：
    /// 1. 确保所有原则实体已注册为节点 (entity_type = "principle")
    /// 2. 根据任务类型关键词匹配，建立 activates 边
    /// 3. 返回注入的边数量
    pub fn inject_principles(
        &mut self,
        principles: Vec<Entity>,
        task_type: &str,
        task_keywords: Vec<String>,
    ) -> usize {
        let mut injected = 0;

        // Step 1: 注册/更新所有原则节点
        for principle in &principles {
            self.add_node(principle.clone());
        }

        // Step 2: 为每个原则检查关键词匹配
        for principle in &principles {
            let principle_keywords: Vec<&str> = principle
                .attributes
                .get("keywords")
                .map(|s| s.split(',').map(|k| k.trim()).collect())
                .unwrap_or_default();

            // 匹配度 = 任务关键词与原则关键词的交集比例
            let match_count = task_keywords
                .iter()
                .filter(|kw| principle_keywords.iter().any(|pk| pk.contains(kw.as_str())))
                .count();

            if match_count > 0 {
                let relevance = (match_count as f64)
                    / (task_keywords.len().max(principle_keywords.len().max(1)) as f64);

                // 建立 activates 边：任务类型 → 原则实体
                self.add_edge(
                    format!("task_type:{}", task_type),
                    principle.id.clone(),
                    "activates".to_string(),
                    relevance.clamp(0.1, 1.0),
                );
                injected += 1;
            }
        }

        injected
    }

    /// 获取从指定任务类型激活的所有原则
    pub fn get_activated_principles(&self, task_type: &str) -> Vec<(String, f64)> {
        let task_node_id = format!("task_type:{}", task_type);
        self.adjacency
            .get(&task_node_id)
            .map(|edges| {
                edges
                    .iter()
                    .filter(|(_, e)| e.relation_type == "activates")
                    .map(|(target, e)| {
                        let name = self
                            .nodes
                            .get(target)
                            .map(|n| n.name.clone())
                            .unwrap_or_default();
                        (name, e.weight)
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    // ========== 持久化 ==========

    /// 序列化为 JSON 字符串
    pub fn to_json(&self) -> PyResult<String> {
        serde_json::to_string(self).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Serialization error: {}", e))
        })
    }

    /// 从 JSON 字符串反序列化
    #[staticmethod]
    pub fn from_json(json: &str) -> PyResult<Self> {
        serde_json::from_str(json).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Deserialization error: {}", e))
        })
    }
}
