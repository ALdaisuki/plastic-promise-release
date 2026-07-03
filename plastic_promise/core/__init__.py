"""Plastic Promise 核心基础层

包含:
- constants: 九大系统、三层防线、信任分、审计维度、11条核心原则、SCARF等全部常量
- context_engine: ContextEngine 上下文供应引擎（Python回退版 + Rust PyO3桥接）
- lancedb_store: LanceDB 向量存储（延迟导入，避免重型 lancedb/pyarrow 影响启动时间）
"""

from plastic_promise.core.constants import (
    ASSOCIATION_WEIGHTS,
    AUDIT_DIMENSIONS,
    CEI_TARGET,
    CEI_THRESHOLDS,
    CLASSIFIER_KEYWORDS,
    CLASSIFIER_THRESHOLD_ACP,
    CLASSIFIER_THRESHOLD_CLAUDE,
    CLOSURE_RATE_TARGET,
    CONTEXT_LAYERS,
    CORE_PRINCIPLES,
    CRON_CONFIG,
    CURIOSITY_EXPLORE_RATE,
    DEFENSE_LAYERS,
    DIGITAL_BODY_SYSTEMS,
    INERTIA_SUPPRESSION_THRESHOLD,
    INERTIA_SUPPRESSION_WINDOW,
    MEMORY_DECAY_THRESHOLD,
    MEMORY_GC_INTERVAL_DAYS,
    MEMORY_HEALTH_THRESHOLD,
    MEMORY_TIERS,
    PRE_CHECK_ALERT_THRESHOLD,
    PRINCIPLE_ACTIVATION_TARGET,
    PRINCIPLE_DOMAINS,
    PRINCIPLE_INHERITANCE_DECAY,
    PRINCIPLE_INHERITANCE_DIRECTIONS,
    RRF_K,
    SCARF_DIMENSIONS,
    SYMBOL_RULE_KEYWORDS,
    TRUST_BOOST_RATE,
    TRUST_DECAY_RATE,
    TRUST_INITIAL,
    TRUST_MAX,
    TRUST_MIN,
    WORTH_FAILURE_WEIGHT,
    WORTH_MIN_OBSERVATIONS,
    WORTH_SUCCESS_WEIGHT,
)
from plastic_promise.core.context_engine import (
    ContextEngine,
    ContextItem,
    ContextPack,
)

# Lazy imports for heavy modules (lancedb + pyarrow = ~1446ms)
# Only imported when actually accessed, not at package init time
_lazy_lancedb = None


def __getattr__(name):
    """Lazy-load heavy modules to avoid 1446ms startup penalty from lancedb/pyarrow."""
    global _lazy_lancedb
    if name in ("LanceDBStore", "EMB_DIM", "TABLE_NAME"):
        if _lazy_lancedb is None:
            from plastic_promise.core.lancedb_store import (
                EMB_DIM as _EMB_DIM,
            )
            from plastic_promise.core.lancedb_store import (
                TABLE_NAME as _TABLE_NAME,
            )
            from plastic_promise.core.lancedb_store import (
                LanceDBStore as _LanceDBStore,
            )

            _lazy_lancedb = {
                "LanceDBStore": _LanceDBStore,
                "EMB_DIM": _EMB_DIM,
                "TABLE_NAME": _TABLE_NAME,
            }
        return _lazy_lancedb[name]
    raise AttributeError(f"module 'plastic_promise.core' has no attribute '{name}'")


__all__ = [
    "LanceDBStore",
    "EMB_DIM",
    "TABLE_NAME",
    "DIGITAL_BODY_SYSTEMS",
    "DEFENSE_LAYERS",
    "TRUST_INITIAL",
    "TRUST_DECAY_RATE",
    "TRUST_BOOST_RATE",
    "TRUST_MIN",
    "TRUST_MAX",
    "AUDIT_DIMENSIONS",
    "SCARF_DIMENSIONS",
    "CONTEXT_LAYERS",
    "RRF_K",
    "SYMBOL_RULE_KEYWORDS",
    "ASSOCIATION_WEIGHTS",
    "MEMORY_TIERS",
    "MEMORY_HEALTH_THRESHOLD",
    "MEMORY_DECAY_THRESHOLD",
    "MEMORY_GC_INTERVAL_DAYS",
    "WORTH_SUCCESS_WEIGHT",
    "WORTH_FAILURE_WEIGHT",
    "WORTH_MIN_OBSERVATIONS",
    "CORE_PRINCIPLES",
    "PRINCIPLE_DOMAINS",
    "PRINCIPLE_INHERITANCE_DIRECTIONS",
    "PRINCIPLE_INHERITANCE_DECAY",
    "CRON_CONFIG",
    "CLASSIFIER_KEYWORDS",
    "CLASSIFIER_THRESHOLD_CLAUDE",
    "CLASSIFIER_THRESHOLD_ACP",
    "CEI_THRESHOLDS",
    "CEI_TARGET",
    "PRE_CHECK_ALERT_THRESHOLD",
    "CLOSURE_RATE_TARGET",
    "PRINCIPLE_ACTIVATION_TARGET",
    "INERTIA_SUPPRESSION_WINDOW",
    "INERTIA_SUPPRESSION_THRESHOLD",
    "CURIOSITY_EXPLORE_RATE",
    "ContextEngine",
    "ContextPack",
    "ContextItem",
]
