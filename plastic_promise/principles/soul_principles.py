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
        pass

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
        pass

    def inject_to_graph(self, task_type: str) -> List[str]:
        """将当前激活的原则注入到关联的 ContextEngine 图引擎。

        为每条激活的原则在图引擎中创建或更新对应节点，
        并建立 task_type -> principle_id 的有向边。

        Args:
            task_type: 任务类型，用于创建入边

        Returns:
            成功注入的原则名称列表
        """
        pass

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
                "inherited": int,       # 成功继承的原则数
                "decay_applied": float, # 应用的衰减系数
                "principles": List[Dict[str, Any]],  # 继承后的原则状态
            }
        """
        pass

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
                "before_weights": List[float],
                "after_weights": List[float],
            }
        """
        pass

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
                "score": float,          # 综合评分
                "keyword_match": float,  # 关键词匹配度
                "domain_match": float,   # 域匹配度
                "weight_factor": float,  # 当前权重因子
                "recommendation": str,   # "strong", "moderate", "weak", "none"
            }
        """
        pass

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
