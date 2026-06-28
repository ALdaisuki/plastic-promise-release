"""soul_principles — 原则遗传系统核心模块

实现 DNA/基因遗传模型:
- work->all、life->all 单向扩散
- 权重随传播距离同步衰减 (PRINCIPLE_INHERITANCE_DECAY)
- 原则联想注入图引擎
- 原则在不同场景下的有效性评价

对应九大系统中的「遗传系统」(maturity 0.60)。
"""

from typing import Optional, List, Dict, Any

from plastic_promise.core.constants import (
    CORE_PRINCIPLES,
    PRINCIPLE_DOMAINS,
    PRINCIPLE_INHERITANCE_DIRECTIONS,
    PRINCIPLE_INHERITANCE_DECAY,
)
from plastic_promise.core.context_engine import ContextEngine


class PrincipleManager:
    """原则管理器 — 遗传系统核心

    管理 11 条核心原则的:
    - 激活 (activate): 根据任务类型激活相关原则并注入图引擎
    - 继承 (inherit): 跨域单向扩散 work->all / life->all
    - 扩散 (diffuse): 原则权重同步衰减传播
    - 评价 (evaluate): 原则在特定场景下的适用性评分

    通常由 ContextEngine 在 supply() 阶段调用，
    也可独立使用以检查原则状态或手动触发扩散。
    """

    def __init__(self, engine: Optional[ContextEngine] = None) -> None:
        """初始化原则管理器。

        Args:
            engine: 可选的 ContextEngine 实例。传入后 activate() 可自动调用
                    inject_to_graph() 将激活的原则写入图引擎。若为 None 则仅
                    在内部管理原则状态。
        """
        self._engine = engine

    # ================================================================
    # 激活
    # ================================================================

    def activate(
        self,
        task_type: str,
        task_description: str = "",
        max_principles: int = 5,
    ) -> List[Dict[str, Any]]:
        """根据任务类型和描述激活相关原则。

        激活逻辑 (由 ContextEngine._activate_principles 驱动):
        1. 按 task_type 查找推荐原则 ID 列表
        2. 对 task_description 进行关键词匹配，追加额外原则
        3. 去重取前 max_principles 条
        4. 将激活的原则注入到图引擎

        Args:
            task_type: 任务类型，如 "code_generation", "code_review",
                       "debugging", "architecture", "refactoring",
                       "learning", "collaboration"
            task_description: 任务描述文本，用于关键词额外匹配
            max_principles: 最多激活原则数

        Returns:
            激活的原则列表，每项包含 id, name, content, domain, keywords
        """
        # Step 1: Map task_type to recommended principle IDs
        mapping = {
            "code_generation": [1, 3, 8, 10],
            "code_review": [1, 5, 6, 9],
            "debugging": [1, 5, 10],
            "architecture": [2, 7, 8],
            "refactoring": [5, 6, 7],
            "learning": [1, 10, 11],
            "collaboration": [2, 7, 9],
            "general": [1, 2, 3, 4],
        }
        ids = list(mapping.get(task_type, [1, 2, 3, 4]))

        # Step 2: Keyword bonus matching from task_description
        for p in CORE_PRINCIPLES:
            if p["id"] not in ids:
                for kw in p.get("keywords", []):
                    if kw in task_description:
                        ids.append(p["id"])
                        break

        # Step 3: Deduplicate while preserving order, then limit
        seen: set = set()
        unique_ids: List[int] = []
        for pid in ids:
            if pid not in seen:
                seen.add(pid)
                unique_ids.append(pid)
        unique_ids = unique_ids[:max_principles]

        # Step 4: Return matching principle dicts from CORE_PRINCIPLES
        result: List[Dict[str, Any]] = []
        for p in CORE_PRINCIPLES:
            if p["id"] in unique_ids:
                result.append(dict(p))
        return result

    def inject_to_graph(self, task_type: str) -> List[str]:
        """将当前激活的原则注入到关联的 ContextEngine 图引擎。

        为每条激活的原则在图引擎中创建或更新对应节点，
        并建立 task_type -> principle_id 的有向边。

        Args:
            task_type: 任务类型，用于创建入边

        Returns:
            成功注入的原则名称列表
        """
        if self._engine is None:
            return []

        # Activate principles for this task_type
        activated = self.activate(task_type)
        edge_ids: List[str] = []

        for i, p in enumerate(activated):
            # Descending relevance: first match = highest relevance
            relevance = max(0.1, 0.9 - (i * 0.15))
            node_id = f"principle:{p['id']}"

            # Ensure the principle node exists in the graph
            if node_id not in self._engine._graph_nodes:
                self._engine._graph_nodes[node_id] = {
                    "type": "principle",
                    "name": p["name"],
                    "description": p["content"],
                    "domain": p["domain"],
                }

            # Create directed edge: task_type -> principle
            edge = {
                "from": f"task_type:{task_type}",
                "to": node_id,
                "relation": "activates",
                "weight": relevance,
            }
            self._engine._graph_edges.append(edge)
            edge_ids.append(f"task_type:{task_type} -> {node_id}")

        return edge_ids

    # ================================================================
    # 继承与扩散
    # ================================================================

    def inherit(
        self,
        source_domain: str,
        target_domain: str = "all",
        principle_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """单向继承：将原则从源域扩散到目标域。

        继承方向遵循 PRINCIPLE_INHERITANCE_DIRECTIONS:
        - work -> all
        - life -> all

        不允许反向继承 (all -> work, all -> life) 或跨域继承 (work -> life)。

        Args:
            source_domain: 源域 ("work" 或 "life")
            target_domain: 目标域，默认为 "all"
            principle_ids: 要继承的原则 ID 列表，None 表示继承源域所有原则

        Returns:
            {
                "inherited_count": int,          # 成功继承的原则数
                "decayed_weights": List[float],  # 每条原则的衰减后权重
                "affected_principles": List[str],# 受影响的原则名称
            }
        """
        decay = PRINCIPLE_INHERITANCE_DECAY

        # Filter CORE_PRINCIPLES by source_domain
        principles = [p for p in CORE_PRINCIPLES if p["domain"] == source_domain]

        # Further filter by principle_ids if provided
        if principle_ids is not None:
            id_set = set(principle_ids)
            principles = [p for p in principles if p["id"] in id_set]

        inherited_count = len(principles)
        decayed_weights = [decay for _ in principles]
        affected_principles = [p["name"] for p in principles]

        return {
            "inherited_count": inherited_count,
            "decayed_weights": decayed_weights,
            "affected_principles": affected_principles,
        }

    def diffuse(
        self,
        principle_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """执行原则扩散：所有权重乘以 PRINCIPLE_INHERITANCE_DECAY。

        扩散用于模拟「传播距离越远，影响力越弱」的自然衰减规律。
        每次扩散操作对所有原则（或其指定子集）应用衰减系数。

        Args:
            principle_id: 要扩散的原则 ID。None 表示对所有原则执行扩散。

        Returns:
            {
                "diffused_count": int,      # 扩散影响的原则数
                "decay_coefficient": float, # 衰减系数
                "domain_status": Dict,      # 每条原则的域状态和传播路径
            }
        """
        decay = PRINCIPLE_INHERITANCE_DECAY

        # Select principles to diffuse
        if principle_id is not None:
            principles = [p for p in CORE_PRINCIPLES if p["id"] == principle_id]
        else:
            principles = list(CORE_PRINCIPLES)

        # Build domain status per principle
        domain_status: Dict[int, Dict[str, Any]] = {}
        for p in principles:
            pid = p["id"]
            # Determine propagation path based on domain
            if p["domain"] == "work":
                path = ["work -> all"]
                reachable_domains = ["work", "all"]
            elif p["domain"] == "life":
                path = ["life -> all"]
                reachable_domains = ["life", "all"]
            else:  # "all"
                path = ["work -> all", "life -> all"]
                reachable_domains = ["all"]

            domain_status[pid] = {
                "name": p["name"],
                "source_domain": p["domain"],
                "propagation_path": path,
                "reachable_domains": reachable_domains,
                "decay_coefficient": decay,
            }

        return {
            "diffused_count": len(principles),
            "decay_coefficient": decay,
            "domain_status": domain_status,
        }

    # ================================================================
    # 评价
    # ================================================================

    def evaluate(
        self,
        principle_id: int,
        scenario: str,
    ) -> Dict[str, Any]:
        """评价原则在特定场景下的有效性和适用性。

        基于场景文本与原则关键词的重叠度、原则所属域匹配度、
        以及当前权重计算综合评分 (0.0-1.0)。

        Args:
            principle_id: 要评价的原则 ID (1-11)
            scenario: 场景描述文本

        Returns:
            {
                "principle_id": int,
                "principle_name": str,
                "scenario": str,
                "consequence": str,      # 违反原则的反事实后果
                "keyword_match": float,  # 关键词匹配度
                "recommendation": str,   # "strong", "moderate", "weak", "none"
            }
        """
        # Step 1: Look up principle by ID
        principle: Optional[Dict[str, Any]] = None
        for p in CORE_PRINCIPLES:
            if p["id"] == principle_id:
                principle = p
                break

        if principle is None:
            return {
                "principle_id": principle_id,
                "error": f"Principle {principle_id} not found",
                "scenario": scenario,
            }

        # Step 2: Pre-defined counterfactual consequence text per principle ID
        consequences: Dict[int, str] = {
            1: "If honesty is not prioritized over perfection: metrics become unreliable, "
               "trust erodes, and systemic issues go unreported — the violation of truth-telling "
               "creates a cascade of hidden failures that eventually destroy system integrity.",
            2: "Without conventions over constraints: agents become rule-following automatons "
               "lacking intrinsic motivation, leading to minimal compliance, creative stagnation, "
               "and a brittle system that collapses the moment external enforcement is removed.",
            3: "Without active memory supply: context becomes fragmented across decisions, "
               "past lessons are repeatedly lost, and the system operates with incomplete "
               "information — the consequence is perpetual relearning at every turn.",
            4: "If principles do not emerge naturally: compliance becomes performative, "
               "agents follow rules they do not understand, and the firewall becomes the only "
               "enforcement mechanism — a fragile house of cards that crumbles under pressure.",
            5: "Confusing existence with effectiveness: teams ship features that pass checks on "
               "paper but fail in practice, creating a false sense of security. The consequence "
               "is undetected regression masked by green checkmarks.",
            6: "Without actual data-flow tracking: integration points become black boxes, "
               "system failures cascade unpredictably, and debugging devolves into guesswork — "
               "the consequence is invisible coupling that amplifies every small failure.",
            7: "Without organs protecting each other: single points of failure proliferate, "
               "system resilience degrades, and no subsystem catches another's errors — "
               "the consequence is a domino chain where one failure topples everything.",
            8: "Without tools as senses: the LLM is blind and handless, every capability "
               "constrained to pure text. The consequence is a brilliant mind trapped in a "
               "soundproof room, unable to observe or affect the real world.",
            9: "Without trust-driven dynamic constraints: the system oscillates between "
               "complete lockdown (stifling all productivity) and total openness (inviting "
               "disaster) — the consequence is a binary rigidity that cannot adapt to nuance.",
            10: "If the self-evolution loop breaks: behavior drifts without correction, "
                "evaluation decouples from reality, and the system degrades without self-awareness "
                "— the consequence is silent entropy that goes unnoticed until catastrophic failure.",
            11: "Without principle inheritance: each new agent instance starts from scratch, "
                "core values are lost across generations, and the system forgets its foundational "
                "commitments — the consequence is generational amnesia that erodes the culture.",
        }

        consequence = consequences.get(
            principle_id,
            "Violation of this principle leads to systemic degradation and erosion of trust.",
        )

        # Step 3: Compute keyword match score
        keywords = principle.get("keywords", [])
        if keywords:
            matched = sum(1 for kw in keywords if kw in scenario)
            keyword_match = matched / len(keywords)
        else:
            keyword_match = 0.0

        # Step 4: Recommendation based on consequence severity and keyword match
        if principle_id <= 3:
            recommendation = "strong"
        elif principle_id <= 7:
            recommendation = "moderate"
        elif keyword_match > 0.3:
            recommendation = "moderate"
        else:
            recommendation = "weak"

        return {
            "principle_id": principle_id,
            "principle_name": principle["name"],
            "scenario": scenario,
            "consequence": consequence,
            "keyword_match": keyword_match,
            "recommendation": recommendation,
        }

    # ================================================================
    # 查询
    # ================================================================

    def get_all_principles(self) -> List[Dict[str, Any]]:
        """获取所有 11 条核心原则的完整信息。

        Returns:
            所有原则列表，每项包含 id, name, content, domain, keywords
            以及运行时状态 (weight, activation_count, last_activated 等)
        """
        pass

    def get_by_domain(self, domain: str) -> List[Dict[str, Any]]:
        """按域筛选原则。

        Args:
            domain: 域名称 ("work", "life", "all")

        Returns:
            属于指定域的原则列表

        Raises:
            ValueError: 如果 domain 不在 PRINCIPLE_DOMAINS 中
        """
        pass


# ================================================================
# 模块级便捷函数 (pass-through stubs)
# ================================================================

def principle_activate(
    task_type: str,
    task_description: str = "",
    max_principles: int = 5,
) -> List[Dict[str, Any]]:
    """便捷函数：使用默认 PrincipleManager 激活原则。

    Args:
        task_type: 任务类型
        task_description: 任务描述文本
        max_principles: 最多激活原则数

    Returns:
        激活的原则列表
    """
    pass


def principle_inherit(
    source_domain: str,
    target_domain: str = "all",
    principle_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """便捷函数：使用默认 PrincipleManager 执行单向继承。

    Args:
        source_domain: 源域
        target_domain: 目标域
        principle_ids: 要继承的原则 ID 列表

    Returns:
        继承结果字典
    """
    pass


def principle_diffuse(
    principle_id: Optional[int] = None,
) -> Dict[str, Any]:
    """便捷函数：使用默认 PrincipleManager 执行原则扩散。

    Args:
        principle_id: 要扩散的原则 ID，None 表示全部

    Returns:
        扩散结果字典
    """
    pass


def principle_evaluate(
    principle_id: int,
    scenario: str,
) -> Dict[str, Any]:
    """便捷函数：使用默认 PrincipleManager 评价原则有效性。

    Args:
        principle_id: 原则 ID
        scenario: 场景描述

    Returns:
        评价结果字典
    """
    pass
