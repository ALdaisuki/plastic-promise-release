"""Plastic Promise 冷启动脚本 — 原则注册 + 种子记忆 + 初始化验证

一次性完成：
1. 将 11 条核心原则注入 EntityGraph
2. 为 7 种任务类型建立 task_type → principle 激活边
3. 注册种子记忆（覆盖 6 个类别）
4. 验证所有数据可检索
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plastic_promise.core.constants import CORE_PRINCIPLES
from plastic_promise.core.context_engine import ContextEngine, ContextPack


def bootstrap():
    print("=" * 60)
    print("Plastic Promise — Cold Start Bootstrap")
    print("=" * 60)

    # ============================================================
    # Phase 1: 初始化引擎
    # ============================================================
    print("\n[1/4] 初始化 ContextEngine...")
    engine = ContextEngine()
    engine.set_current_time("2026-06-28T20:00:00")
    print(f"  引擎版本: Python 回退版 (Rust 不可用时自动切换)")

    # ============================================================
    # Phase 2: 注册 11 条核心原则为 EntityGraph 节点
    # ============================================================
    print("\n[2/4] 注册 11 条核心原则到 EntityGraph...")
    principle_nodes = []
    for p in CORE_PRINCIPLES:
        node = {
            "id": f"principle:{p['id']}",
            "entity_type": "principle",
            "name": p["name"],
            "description": p["content"],
            "domain": p["domain"],
            "keywords": p.get("keywords", []),
        }
        engine._graph_nodes[node["id"]] = node
        engine._graph_edges.append({
            "from": f"domain:{p['domain']}",
            "to": node["id"],
            "relation_type": "contains",
            "weight": 1.0,
        })
        principle_nodes.append(node)
    print(f"  已注册 {len(principle_nodes)} 条原则节点")
    for n in principle_nodes:
        print(f"    [{n['id']}] {n['name']}: {n['description'][:50]}...")

    # ============================================================
    # Phase 3: 建立 task_type → principle 激活边
    # ============================================================
    print("\n[3/4] 建立任务类型 → 原则激活边...")

    task_type_mapping = {
        "code_generation": [1, 2, 3],
        "code_review": [1, 2, 3],
        "debugging": [1, 2, 3],
        "architecture": [1, 2, 3],
        "refactoring": [1, 2, 3],
        "learning": [1, 2, 3],
        "collaboration": [1, 2, 3],
    }

    edge_count = 0
    for task_type, principle_ids in task_type_mapping.items():
        # Register task_type as a node
        task_node_id = f"task_type:{task_type}"
        engine._graph_nodes[task_node_id] = {
            "id": task_node_id,
            "entity_type": "task_type",
            "name": task_type,
            "description": f"预定义的任务类型: {task_type}",
        }
        # Create activation edges
        for pid in principle_ids:
            engine._graph_edges.append({
                "from": task_node_id,
                "to": f"principle:{pid}",
                "relation_type": "activates",
                "weight": 0.9,
            })
            edge_count += 1

    print(f"  已创建 {edge_count} 条激活边，覆盖 {len(task_type_mapping)} 种任务类型")
    print(f"  EntityGraph: {len(engine._graph_nodes)} 节点 / {len(engine._graph_edges)} 边")

    # ============================================================
    # Phase 4: 注册种子记忆
    # ============================================================
    print("\n[4/4] 注册种子记忆 (6 类别 × 3 = 18 条)...")

    seed_memories = [
        # preference (偏好)
        ("用户偏好使用 Rust 编写后端服务，认为其性能和类型安全优于 Python",
         "experience", "user"),
        ("用户不喜欢未经过测试的代码直接合并到主分支",
         "experience", "user"),
        ("用户倾向于使用简洁的函数式编程风格，避免深层嵌套",
         "experience", "user"),

        # fact (事实)
        ("Plastic Promise 项目包含 9 大数字身体系统，覆盖感官到认知的完整链路",
         "experience", "system"),
        ("当前 Rust 引擎使用 SQLite 做主存储，LanceDB 待环境就绪后接入",
         "experience", "system"),
        ("Ollama mxbai-embed-large 提供 1024 维嵌入向量，本地运行零延迟",
         "experience", "system"),

        # decision (决策)
        ("决定采用约定工程而非约束工程作为 AI 行为治理范式",
         "experience", "system"),
        ("决定优先实现核心方法的完整逻辑，辅助方法保持骨架",
         "experience", "system"),
        ("决定使用 PyO3 桥接 Rust 引擎，上层 Python 调用无感切换",
         "experience", "system"),

        # entity (实体)
        ("plastic_promise/core/context_engine.py 是上下文供应引擎的 Python 包装",
         "code", "system"),
        ("rust/context-engine-core 包含 HybridRetriever、SQLite、Weibull 衰减等核心组件",
         "code", "system"),
        ("plastic_promise/mcp/server.py 暴露 25 个 MCP 工具，7 个工具组",
         "code", "system"),

        # event (事件)
        ("2026-06-28: 完成了从 P0 到 P2 的全部优先级任务实现",
         "task", "system"),
        ("2026-06-28: Ollama mxbai-embed-large 接入成功，dim=1024",
         "task", "system"),
        ("2026-06-28: 11 条核心原则成功注入 EntityGraph",
         "task", "system"),

        # pattern (模式)
        ("每次重大架构决策都会触发原则评估，形成决策→原则→反馈的闭环",
         "experience", "system"),
        ("记忆检索总是先尝试向量搜索，失败后回退文本匹配",
         "experience", "system"),
        ("系统遵循 '约定优于约束' 的核心哲学，内部动机驱动而非外部规则强制",
         "experience", "system"),
    ]

    stored_ids = []
    for content, mem_type, source in seed_memories:
        mid = engine.register_memory({
            "content": content,
            "memory_type": mem_type,
            "source": source,
            "worth_success": 5,
            "worth_failure": 0,
            "activation_weight": 0.8,
            "created_at": "2026-06-28T20:00:00",
        })
        stored_ids.append(mid)

    print(f"  已注册 {len(stored_ids)} 条种子记忆")
    for mid, (content, _, _) in zip(stored_ids, seed_memories):
        print(f"    [{mid}] {content[:60]}...")

    # ============================================================
    # Phase 5: 验证检索
    # ============================================================
    print("\n" + "=" * 60)
    print("验证检索链路")
    print("=" * 60)

    test_queries = [
        ("用户喜欢什么编程语言", "code_generation"),
        ("项目的架构决策是什么", "architecture"),
        ("记忆系统的存储方案", "general"),
    ]

    for query, task_type in test_queries:
        pack = engine.supply(query, task_type)
        print(f"\n  查询: '{query}' (task_type={task_type})")
        print(f"  核心层: {len(pack.core)} 条")
        print(f"  关联层: {len(pack.related)} 条")
        print(f"  发散层: {len(pack.divergent)} 条")
        print(f"  激活原则: {pack.activated_principles}")
        if pack.core:
            for item in pack.core[:3]:
                print(f"    - [{item.relevance:.2f}] {item.content[:80]}...")

    print("\n" + "=" * 60)
    print(f"Bootstrap 完成")
    print(f"  原则节点: {sum(1 for n in engine._graph_nodes.values() if n.get('entity_type') == 'principle')}")
    print(f"  Task Type 节点: {sum(1 for n in engine._graph_nodes.values() if n.get('entity_type') == 'task_type')}")
    print(f"  总节点: {len(engine._graph_nodes)} / 总边: {len(engine._graph_edges)}")
    print(f"  记忆池: {engine.memory_count} 条")
    print("=" * 60)

    return engine


if __name__ == "__main__":
    engine = bootstrap()
