"""任务分类器 — 45关键词 + ACP 路由

基于 CLASSIFIER_KEYWORDS 的关键词匹配，将用户指令分类为不同任务类型，
并根据分类得分决定路由目标 (Claude Code 或 ACP)。
"""

from typing import Any, Dict, List

from plastic_promise.core.constants import (
    CLASSIFIER_KEYWORDS,
    CLASSIFIER_THRESHOLD_ACP,
    CLASSIFIER_THRESHOLD_CLAUDE,
)

# ---------------------------------------------------------------------------
# 类别定义 — 与 CLASSIFIER_KEYWORDS 的布局严格对应
# ---------------------------------------------------------------------------
# CLASSIFIER_KEYWORDS 按顺序分为 6 组:
#   0..10  代码生成 (11 个)
#   11..18 修改编辑 (8 个)
#   19..26 查询分析 (8 个)
#   27..32 审查测试 (6 个)
#   33..38 协作管理 (6 个)
#   39..44 学习探索 (6 个)
# ---------------------------------------------------------------------------

CATEGORY_NAMES: List[str] = [
    "code_generation",
    "modify",
    "query",
    "review",
    "collaboration",
    "learning",
]

_KEYWORD_GROUPS: List[List[str]] = [
    CLASSIFIER_KEYWORDS[0:11],  # code_generation
    CLASSIFIER_KEYWORDS[11:19],  # modify
    CLASSIFIER_KEYWORDS[19:27],  # query
    CLASSIFIER_KEYWORDS[27:33],  # review
    CLASSIFIER_KEYWORDS[33:39],  # collaboration
    CLASSIFIER_KEYWORDS[39:45],  # learning
]


class TaskClassifier:
    """基于关键词的任务分类与路由引擎。

    分类结果包含：
    - 匹配到的关键词及类别
    - 分类得分
    - 路由建议 (acpx_claude_exec / claude_print / local)
    """

    def __init__(self) -> None:
        """初始化分类器，预加载关键词索引。"""
        self._keywords: List[str] = CLASSIFIER_KEYWORDS
        self._keyword_groups: List[List[str]] = _KEYWORD_GROUPS
        self._category_names: List[str] = CATEGORY_NAMES
        self._threshold_claude: int = CLASSIFIER_THRESHOLD_CLAUDE
        self._threshold_acp: int = CLASSIFIER_THRESHOLD_ACP

        # 统计追踪
        self._total_classified: int = 0
        self._route_counts: Dict[str, int] = {
            "acpx_claude_exec": 0,
            "claude_print": 0,
            "local": 0,
        }
        self._score_sum: float = 0.0

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def classify(self, instruction: str) -> Dict[str, Any]:
        """对单条指令进行分类。

        Args:
            instruction: 用户指令文本。

        Returns:
            分类结果字典，包含:
            - instruction: str
            - matched_keywords: List[str]  匹配到的关键词
            - score: int                   匹配得分
            - category: str                主类别
            - route: str                   路由建议
            - categories: Dict[str, int]   各类别命中次数
            - confidence: float            置信度 (score/10, 上限 1.0)
        """
        instruction_lower = instruction.lower()

        matched: List[str] = []
        category_hits: Dict[str, int] = {name: 0 for name in self._category_names}

        for cat_idx, keywords in enumerate(self._keyword_groups):
            cat_name = self._category_names[cat_idx]
            for kw in keywords:
                if kw in instruction_lower:
                    matched.append(kw)
                    category_hits[cat_name] += 1

        score = len(matched)

        # 主类别 — 命中数最多的类别；平局取先出现的
        best_category = "code_generation"
        best_hits = 0
        for cat_name in self._category_names:
            if category_hits[cat_name] > best_hits:
                best_hits = category_hits[cat_name]
                best_category = cat_name

        route = self._compute_route(score)
        confidence = min(score / 10.0, 1.0)

        # 更新统计
        self._total_classified += 1
        self._route_counts[route] = self._route_counts.get(route, 0) + 1
        self._score_sum += score

        return {
            "instruction": instruction,
            "matched_keywords": matched,
            "score": score,
            "category": best_category,
            "route": route,
            "categories": category_hits,
            "confidence": confidence,
        }

    def route(self, instruction: str) -> str:
        """对单条指令执行路由决策。

        Args:
            instruction: 用户指令文本。

        Returns:
            路由目标字符串:
            - "acpx_claude_exec"  score >= CLASSIFIER_THRESHOLD_ACP
            - "claude_print"      score >= CLASSIFIER_THRESHOLD_CLAUDE
            - "local"             否则
        """
        score = len([kw for kw in self._keywords if kw in instruction.lower()])
        return self._compute_route(score)

    def batch_classify(self, instructions: List[str]) -> List[Dict[str, Any]]:
        """批量分类多条指令。

        Args:
            instructions: 用户指令文本列表。

        Returns:
            与每条指令对应的分类结果列表，元素结构与 classify() 相同。
        """
        return [self.classify(inst) for inst in instructions]

    @property
    def accuracy_stats(self) -> Dict[str, Any]:
        """分类准确率统计。

        Returns:
            统计字典，包含:
            - total_classified: int
            - routes: Dict[str, int]  各路由目标计数
            - avg_score: float
        """
        return {
            "total_classified": self._total_classified,
            "routes": dict(self._route_counts),
            "avg_score": (
                self._score_sum / self._total_classified if self._total_classified > 0 else 0.0
            ),
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _compute_route(self, score: int) -> str:
        """根据得分计算路由目标。"""
        if score >= self._threshold_acp:
            return "acpx_claude_exec"
        if score >= self._threshold_claude:
            return "claude_print"
        return "local"


# ---------------------------------------------------------------------------
# 模块级便捷函数
# ---------------------------------------------------------------------------

_DEFAULT_CLASSIFIER: TaskClassifier = TaskClassifier()


def classify_task(instruction: str) -> Dict[str, Any]:
    """模块级便捷函数 — 使用默认分类器实例对单条指令分类。

    Args:
        instruction: 用户指令文本。

    Returns:
        分类结果字典，结构与 TaskClassifier.classify() 相同。
    """
    return _DEFAULT_CLASSIFIER.classify(instruction)
