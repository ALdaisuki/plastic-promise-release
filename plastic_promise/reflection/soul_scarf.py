"""SCARF 五维度自省引擎

基于神经科学 SCARF 模型（David Rock, 2008）的五维度自省框架：
- Status（状态感知）—— 系统当前运行状态是否正常？
- Certainty（确定性）—— 当前决策是否有充分依据？
- Autonomy（自主权）—— 当前行为是否在授权范围内？
- Relatedness（关联性）—— 当前行为是否与核心约定对齐？
- Fairness（公平性）—— 当前决策是否公平、一致？

评分策略（混合）：
1. 关键词匹配 — 显式命中时产生强信号（±0.06~0.08/词）
2. 语义相似度 — 无关键词命中时，用 embedding 余弦相似度
   比较上下文与各维度正/负锚点，产生 ±0.15 以内的微调信号
3. 默认回退 — 既无关键词又无 embedding 时退回 0.65

提供模块级便捷函数 `scarf_reflect` 以及可实例化的 `SCARFReflector` 类。
"""

import datetime
import math
from typing import Any, Dict, List, Optional

from plastic_promise.core.constants import SCARF_DIMENSIONS

# ============================================================
# 五维度关键词语义映射
# ============================================================

_DIMENSION_KEYWORDS: Dict[str, Dict[str, List[str]]] = {
    "Status": {
        "positive": [
            "成功",
            "完成",
            "正常",
            "通过",
            "良好",
            "稳定",
            "健康",
            "success",
            "pass",
            "ok",
            "done",
            "fine",
            "good",
            "stable",
            "healthy",
            "working",
            "correct",
            "valid",
            "ready",
        ],
        "negative": [
            "错误",
            "失败",
            "异常",
            "崩溃",
            "故障",
            "损坏",
            "中断",
            "error",
            "fail",
            "crash",
            "bug",
            "broken",
            "issue",
            "problem",
            "invalid",
            "wrong",
            "down",
            "dead",
            "halt",
        ],
    },
    "Certainty": {
        "positive": [
            "确定",
            "明确",
            "确认",
            "依据",
            "证据",
            "数据",
            "可靠",
            "验证",
            "证实",
            "事实",
            "certain",
            "sure",
            "confirmed",
            "evidence",
            "data",
            "verified",
            "reliable",
            "proven",
            "fact",
            "known",
            "clear",
        ],
        "negative": [
            "可能",
            "猜测",
            "也许",
            "不确定",
            "大概",
            "假设",
            "推测",
            "模糊",
            "未知",
            "怀疑",
            "疑惑",
            "maybe",
            "guess",
            "uncertain",
            "possibly",
            "assumption",
            "hypothetical",
            "vague",
            "unknown",
            "doubt",
            "unclear",
        ],
    },
    "Autonomy": {
        "positive": [
            "选择",
            "决定",
            "自主",
            "自由",
            "授权",
            "主动权",
            "自愿",
            "自行",
            "主动",
            "choose",
            "decide",
            "autonomous",
            "free",
            "authorized",
            "empowered",
            "voluntary",
            "opt",
            "initiative",
        ],
        "negative": [
            "强制",
            "必须",
            "被迫",
            "限制",
            "禁止",
            "命令",
            "服从",
            "不得不",
            "强迫",
            "must",
            "forced",
            "restricted",
            "banned",
            "prohibited",
            "mandatory",
            "compelled",
            "obligated",
            "constrained",
        ],
    },
    "Relatedness": {
        "positive": [
            "约定",
            "原则",
            "信任",
            "协作",
            "对齐",
            "一致",
            "共享",
            "关联",
            "连接",
            "同步",
            "承诺",
            "守护",
            "principle",
            "trust",
            "align",
            "collaborate",
            "shared",
            "commitment",
            "connect",
            "synchronize",
            "together",
            "joint",
        ],
        "negative": [
            "偏离",
            "孤立",
            "矛盾",
            "冲突",
            "违背",
            "断裂",
            "脱节",
            "分离",
            "对立",
            "deviate",
            "isolated",
            "contradict",
            "conflict",
            "violate",
            "broken",
            "disconnect",
            "separated",
            "opposed",
            "drift",
        ],
    },
    "Fairness": {
        "positive": [
            "公平",
            "一致",
            "平衡",
            "公正",
            "平等",
            "均衡",
            "均匀",
            "客观",
            "中立",
            "fair",
            "consistent",
            "balanced",
            "just",
            "equal",
            "impartial",
            "objective",
            "neutral",
            "even",
        ],
        "negative": [
            "偏差",
            "偏袒",
            "不公",
            "倾斜",
            "不一致",
            "歧视",
            "片面",
            "失衡",
            "主观",
            "bias",
            "unfair",
            "inconsistent",
            "skewed",
            "unequal",
            "partial",
            "imbalanced",
            "discriminatory",
            "subjective",
        ],
    },
}

_DEFAULT_SCORE = 0.65
_STRONG_SIGNAL_THRESHOLD = 3  # 匹配 keyword 数达到此值视为强信号
_SEMANTIC_SIGNAL_MAX = 0.15  # 语义信号最大偏移量
_SEMANTIC_SIGNAL_FLOOR = 0.03  # 语义相似度低于此值视为噪声，归零

# Pre-built dimension anchor texts (lazily embedded on first use)
_ANCHOR_TEXTS: Dict[str, Dict[str, str]] = {}
for _dk, _kw in _DIMENSION_KEYWORDS.items():
    _ANCHOR_TEXTS[_dk] = {
        "positive": " ".join(_kw.get("positive", [])),
        "negative": " ".join(_kw.get("negative", [])),
    }


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors (manual, no numpy)."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0  # zero-vector fallback → no signal
    return dot / (norm_a * norm_b)


def _compute_semantic_signal(context: str, dim_key: str) -> float:
    """Compute embedding-based semantic signal for a SCARF dimension.

    Embeds the context text and compares it against pre-built positive/
    negative anchor embeddings for the dimension. Returns a value in
    [-_SEMANTIC_SIGNAL_MAX, +_SEMANTIC_SIGNAL_MAX] that nudges the
    default score when no keywords match.

    Gracefully degrades to 0.0 when the embedder is unavailable or
    returns zero vectors (FallbackEmbedder).
    """
    anchors = _ANCHOR_TEXTS.get(dim_key)
    if not anchors:
        return 0.0
    try:
        from plastic_promise.core.embedder import get_embedder

        embedder = get_embedder()
        ctx_vec = embedder.embed(context)
        pos_vec = embedder.embed(anchors["positive"])
        neg_vec = embedder.embed(anchors["negative"])
        pos_sim = _cosine_similarity(ctx_vec, pos_vec)
        neg_sim = _cosine_similarity(ctx_vec, neg_vec)
        raw = pos_sim - neg_sim
        if abs(raw) < _SEMANTIC_SIGNAL_FLOOR:
            return 0.0
        return max(-_SEMANTIC_SIGNAL_MAX, min(_SEMANTIC_SIGNAL_MAX, raw))
    except Exception:
        return 0.0


def _compute_dimension_score(
    dim_key: str, context_lower: str, context_original: str = ""
) -> Dict[str, Any]:
    """对单个维度计算评分和评估文本。

    Args:
        dim_key: 维度键名（Status/Certainty/Autonomy/Relatedness/Fairness）。
        context_lower: 已转为小写的上下文字符串。

    Returns:
        Dict with 'score', 'assessment', 'suggestion' keys.
    """
    keywords = _DIMENSION_KEYWORDS.get(dim_key, {})
    positive_kw = keywords.get("positive", [])
    negative_kw = keywords.get("negative", [])

    pos_count = sum(1 for kw in positive_kw if kw in context_lower)
    neg_count = sum(1 for kw in negative_kw if kw in context_lower)

    dim_def = SCARF_DIMENSIONS.get(dim_key, {})
    dim_label = dim_def.get("name", dim_key)
    dim_question = dim_def.get("question", "")

    # Compute score from signal strength
    if pos_count == 0 and neg_count == 0:
        # No keyword hits — try semantic embedding signal
        semantic_signal = _compute_semantic_signal(context_original, dim_key)
        if semantic_signal != 0.0:
            score = _DEFAULT_SCORE + semantic_signal
            direction = "正面" if semantic_signal > 0 else "负面"
            assessment = (
                f"{dim_label}：无显式关键词，语义信号{direction}倾向"
                f"（偏移 {semantic_signal:+.3f}）。"
            )
            suggestion = f"建议主动检查{dim_label}状态：{dim_question}"
        else:
            score = _DEFAULT_SCORE
            assessment = f"{dim_label}：无明显信号，维持默认评估。"
            suggestion = f"建议主动检查{dim_label}状态：{dim_question}"
    elif pos_count >= _STRONG_SIGNAL_THRESHOLD and neg_count == 0:
        score = min(1.0, 0.65 + 0.07 * pos_count)
        assessment = f"{dim_label}：强正面信号（+{pos_count}个积极指标）。"
        suggestion = f"{dim_label}状态良好，继续保持。"
    elif neg_count >= _STRONG_SIGNAL_THRESHOLD and pos_count == 0:
        score = max(0.0, 0.65 - 0.08 * neg_count)
        assessment = f"{dim_label}：强负面信号（+{neg_count}个消极指标），需要关注。"
        suggestion = f"建议优先排查{dim_label}问题：{dim_question}"
    elif pos_count > neg_count:
        net = pos_count - neg_count
        score = min(1.0, 0.65 + 0.06 * net)
        assessment = f"{dim_label}：正面信号占优（+{pos_count}/-{neg_count}），总体倾向积极。"
        suggestion = f"维持{dim_label}正面趋势，留意残余负面指标。"
    elif neg_count > pos_count:
        net = neg_count - pos_count
        score = max(0.0, 0.65 - 0.07 * net)
        assessment = f"{dim_label}：负面信号占优（+{pos_count}/-{neg_count}），存在改进空间。"
        suggestion = f"建议针对性改善{dim_label}：{dim_question}"
    else:
        # equal non-zero counts
        score = _DEFAULT_SCORE
        assessment = f"{dim_label}：正负信号持平（+{pos_count}/-{neg_count}），信号矛盾。"
        suggestion = f"信号矛盾，建议进一步收集信息澄清{dim_label}。"
        # Clamp small negative
        score = max(0.0, min(1.0, score))

    return {
        "score": round(score, 4),
        "assessment": assessment,
        "suggestion": suggestion,
    }


class SCARFReflector:
    """SCARF 五维度自省器。

    对给定上下文在 Status / Certainty / Autonomy / Relatedness / Fairness
    五个维度上进行自省评估，并支持历史对比分析。

    Attributes:
        dimensions: 当前启用的自省维度配置（继承自 SCARF_DIMENSIONS）。
        history: 历次自省结果的内部记录列表。
    """

    def __init__(self) -> None:
        """初始化 SCARFReflector。

        从核心常量中加载 SCARF_DIMENSIONS 作为评估维度，
        并初始化空的历史记录列表。
        """
        self.dimensions = dict(SCARF_DIMENSIONS)
        self.history: List[Dict[str, Any]] = []

    def reflect(
        self,
        context: str,
        dimensions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """在指定维度上对给定上下文进行自省。

        Args:
            context: 需要自省的上下文描述（任务描述、决策场景等）。
            dimensions: 需要评估的维度名称列表（如 ["Status", "Certainty"]）。
                        若为 None，则评估 SCARF_DIMENSIONS 中全部五个维度。

        Returns:
            Dict[str, Any]: 自省结果，包含：
                - dimensions: 各维度的评估详情（评分、理由等）
                - summary: 整体自省摘要
                - timestamp: 自省时间戳
        """
        if dimensions is None:
            dimensions = list(SCARF_DIMENSIONS.keys())

        context_lower = context.lower()
        dim_results: Dict[str, Any] = {}

        for dim_key in dimensions:
            if dim_key not in SCARF_DIMENSIONS:
                continue
            dim_results[dim_key] = _compute_dimension_score(
                dim_key, context_lower, context_original=context
            )

        # Build result as flat {dimension: {...}, summary: ..., timestamp: ...}
        scores = [d["score"] for d in dim_results.values()]
        overall = round(sum(scores) / len(scores), 4) if scores else _DEFAULT_SCORE

        result: Dict[str, Any] = dict(dim_results)
        result["summary"] = {
            "overall_score": overall,
            "dimensions_evaluated": list(dim_results.keys()),
            "score_range": (
                round(min(scores), 4) if scores else _DEFAULT_SCORE,
                round(max(scores), 4) if scores else _DEFAULT_SCORE,
            ),
        }
        result["timestamp"] = (
            datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat() + "Z"
        )

        self.history.append(result)
        return result

    def get_status_summary(self) -> Dict[str, Any]:
        """获取当前 SCARF 状态的摘要视图。

        返回各维度的最新评分和整体状态快照，用于快速监控。

        Returns:
            Dict[str, Any]: 状态摘要，包含：
                - scores: 各维度最近一次的评分映射
                - overall_score: 整体加权评分
                - strongest_dimension: 最高分维度
                - weakest_dimension: 最低分维度
                - alerts: 评分低于阈值的维度告警列表
        """
        if not self.history:
            return {
                "scores": {},
                "overall_score": None,
                "strongest_dimension": None,
                "weakest_dimension": None,
                "alerts": [],
                "note": "尚无自省记录，请先调用 reflect()。",
            }

        latest: Dict[str, Any] = self.history[-1]
        # Extract dimension entries from flat record (exclude meta keys)
        _META_KEYS = {"summary", "timestamp"}
        dims: Dict[str, Any] = {
            k: v
            for k, v in latest.items()
            if k not in _META_KEYS and isinstance(v, dict) and "score" in v
        }

        if not dims:
            return {
                "scores": {},
                "overall_score": None,
                "strongest_dimension": None,
                "weakest_dimension": None,
                "alerts": [],
                "note": "最近一次自省无维度数据。",
            }

        scores: Dict[str, float] = {k: v["score"] for k, v in dims.items()}
        overall = round(sum(scores.values()) / len(scores), 4)
        strongest = max(scores, key=scores.get)
        weakest = min(scores, key=scores.get)

        # Dimensions below 0.50 threshold raise alerts
        alert_threshold = 0.50
        alerts = [
            {"dimension": k, "score": v, "assessment": dims[k]["assessment"]}
            for k, v in scores.items()
            if v < alert_threshold
        ]

        return {
            "scores": scores,
            "overall_score": overall,
            "strongest_dimension": strongest,
            "weakest_dimension": weakest,
            "alerts": alerts,
        }

    def compare_with_history(self, window: int = 10) -> Dict[str, Any]:
        """将最近一次自省结果与历史窗口内的记录进行对比。

        Args:
            window: 对比的历史窗口大小（取最近 N 次记录）。

        Returns:
            Dict[str, Any]: 对比分析结果，包含：
                - current: 当前自省结果
                - trend: 各维度的变化趋势（上升/下降/稳定）
                - anomalies: 异常波动维度列表
                - window_size: 实际参与对比的历史记录数
        """
        if len(self.history) < 2:
            return {
                "current": self.history[-1] if self.history else None,
                "trend": {},
                "anomalies": [],
                "window_size": 0,
                "note": "历史记录不足，至少需要2次自省才能对比。",
            }

        # Window of recent records excluding the latest
        recent = self.history[-window - 1 : -1]
        latest_record = self.history[-1]

        # Extract dimension entries from flat record (exclude meta keys)
        _META_KEYS = {"summary", "timestamp"}
        latest_dims: Dict[str, Any] = {
            k: v
            for k, v in latest_record.items()
            if k not in _META_KEYS and isinstance(v, dict) and "score" in v
        }

        if not recent or not latest_dims:
            return {
                "current": latest_record,
                "trend": {},
                "anomalies": [],
                "window_size": len(recent),
                "note": "历史窗口或当前记录无有效维度数据。",
            }

        # Compute average historical scores per dimension
        hist_avgs: Dict[str, float] = {}
        dim_keys = list(latest_dims.keys())

        for dk in dim_keys:
            hist_scores: List[float] = []
            for rec in recent:
                dim_val = rec.get(dk)
                if isinstance(dim_val, dict) and "score" in dim_val:
                    hist_scores.append(dim_val["score"])
                else:
                    hist_scores.append(_DEFAULT_SCORE)
            hist_avgs[dk] = sum(hist_scores) / len(hist_scores) if hist_scores else _DEFAULT_SCORE

        # Determine trend per dimension
        trend: Dict[str, str] = {}
        change_threshold = 0.05  # minimum absolute change to count as rise/fall

        for dk in dim_keys:
            current_score = latest_dims[dk]["score"]
            hist_avg = hist_avgs.get(dk, _DEFAULT_SCORE)
            diff = current_score - hist_avg
            if diff > change_threshold:
                trend[dk] = "上升"
            elif diff < -change_threshold:
                trend[dk] = "下降"
            else:
                trend[dk] = "稳定"

        # Detect anomalies: dimensions where change exceeds 2x the typical
        anomaly_threshold = 0.15
        anomalies = [
            {
                "dimension": dk,
                "current": latest_dims[dk]["score"],
                "historical_avg": round(hist_avgs[dk], 4),
                "delta": round(latest_dims[dk]["score"] - hist_avgs[dk], 4),
            }
            for dk in dim_keys
            if abs(latest_dims[dk]["score"] - hist_avgs[dk]) > anomaly_threshold
        ]

        return {
            "current": latest_record,
            "trend": trend,
            "anomalies": anomalies,
            "window_size": len(recent),
        }


def scarf_reflect(
    context: str,
    dimensions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """模块级别的便捷自省函数。

    创建临时 SCARFReflector 实例对当前上下文进行一次性自省，
    适用于不需要维护历史状态的轻量使用场景。

    Args:
        context: 需要自省的上下文描述。
        dimensions: 需要评估的维度名称列表。若为 None 则评估全部维度。

    Returns:
        Dict[str, Any]: 自省结果，格式与 SCARFReflector.reflect() 返回值一致。
    """
    reflector = SCARFReflector()
    return reflector.reflect(context, dimensions=dimensions)
