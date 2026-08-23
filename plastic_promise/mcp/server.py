"""Plastic Promise MCP Server — 全过程 MCP 化入口

启动方式:
    python -m plastic_promise.mcp.server              # stdio 模式 (Claude Code 直接调用)
    python -m plastic_promise.mcp.server --streamable-http 9020
    python -m plastic_promise.mcp.server --sse 9020   # legacy alias

架构:
    MCP Server
    ├── 7 个工具组 (tools/)
    ├── Resources (resources.py)
    └── Prompts (prompts.py)

所有工具共享 ContextEngine 单例，通过依赖注入传递给各工具模块。
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import sys
import threading
import weakref
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

# 确保项目根在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# ---------------------------------------------------------------------------
# 全局 ContextEngine 代理 (Rust 不可用时回退到 Python mock)
# ---------------------------------------------------------------------------
from collections import deque

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)

from plastic_promise import __version__
from plastic_promise.core.constants import (
    CORE_PRINCIPLES,
)
from plastic_promise.core.cost_telemetry import SUPPORTED_COST_CURRENCIES
from plastic_promise.core.fusion_policy import (
    FusionConfigurationError,
    canonical_fusion_config_hash,
    load_fusion_config,
)
from plastic_promise.core.project_identity import canonical_project_id
from plastic_promise.core.recall_quality_environment import loaded_rust_extension_identity
from plastic_promise.core.retrieval_planner import plan_retrieval
from plastic_promise.launcher.default_environment import configure_default_environment
from plastic_promise.launcher.runtime_mode import RUNTIME_MODE_KEYS
from plastic_promise.launcher.service_manager import (
    MCP_FUSION_IDENTITY_SCHEMA,
    canonical_source_root,
    resolve_source_revision,
)
from plastic_promise.mcp import server_composition as _server_composition

compose_pp_server_backend_migration_operations = (
    _server_composition.compose_pp_server_backend_migration_operations
)

PLASTIC_PROMISE_VERSION = __version__
SERVER_INSTRUCTIONS = (
    "Plastic Promise MCP provides shared memory, principles, context_supply, "
    "memory_recall, defense, runtime_mode, session-init, sp-stage, and "
    "step-closure for Codex. Start tasks with session-init(context_mode='light'), "
    "then follow the injected pinned Matt Pocock workflow. Auto-enter only "
    "model-invoked stages; user-only stages require explicit user invocation. "
    "Call memory_recall/context_supply as needed. Use debug=true only for diagnostics."
)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SOURCE_ROOT = canonical_source_root(_PROJECT_ROOT)
_SOURCE_REVISION = resolve_source_revision(_SOURCE_ROOT)
_engine = None  # 延迟初始化
_skill_engine = None  # 延迟初始化 — SkillEngine 单例
_closure_history: deque = deque(maxlen=5)  # 滑动窗口: 最近5次闭环 {scarf, trust, cei}
_workflow_flow_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_workflow_flow_locks_guard = threading.Lock()
_task_session_authorities: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)
_task_session_authorities_guard = threading.Lock()
_durable_collaboration_bindings: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_durable_collaboration_bindings_guard = threading.Lock()
_mcp_transport_instances: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_mcp_transport_instances_guard = threading.Lock()
_durable_collaboration_continuation_authority_instance: Any | None = None
_durable_collaboration_continuation_authority_guard = threading.Lock()
_known_mcp_tool_names_cache: frozenset[str] = frozenset()

_COLLABORATION_CONTINUATION_TOKEN_ARGUMENT = "collaboration_continuation_token"
_COLLABORATION_TOOL_CALL_RECONCILE_EXCLUSIONS = frozenset(
    {
        "session-init",
        "session_init",
        "auto_context_inject",
        "collaboration_lease_heartbeat",
    }
)

_TASK_QUEUE_TOOL_NAMES = frozenset(
    {
        "task_enqueue",
        "task_claim",
        "task_complete",
        "task_verify",
        "task_inbox",
        "task_heartbeat",
        "task_abandon",
    }
)


def _workflow_flow_lock(engine: Any, arguments: dict[str, Any]) -> asyncio.Lock:
    """Return the process-local serialisation lock for one official flow lane."""
    from plastic_promise.core.workflow_state import compose_flow_scope

    scope_id = compose_flow_scope(
        arguments.get("stage_session_id") or arguments.get("stage_id"),
        arguments.get("flow_line_id") or arguments.get("flow_id"),
        arguments.get("project_id"),
    )
    key = f"{id(engine)}:{scope_id}"
    with _workflow_flow_locks_guard:
        lock = _workflow_flow_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _workflow_flow_locks[key] = lock
        return lock


def _health_identity_config_key() -> str:
    """Return a secret-free key for inputs that affect health identity.

    Health probes are cached, but deployment configuration can change while a
    process remains alive (for example, an embedding endpoint rotation).  A
    digest of the relevant environment namespaces plus the active engine and
    embedder objects prevents a prior result from crossing that boundary.
    """

    prefixes = ("PP_", "EMBEDDER_", "OLLAMA_", "PLASTIC_", "LDB_")
    environment = sorted(
        (key, value) for key, value in os.environ.items() if key.startswith(prefixes)
    )
    engine = _engine
    embedder = getattr(engine, "_embedder", None) if engine is not None else None
    try:
        embedder_index_identity = getattr(embedder, "index_model_name", None)
    except Exception:
        # A provider property failure must invalidate the cache; the probe will
        # report the actual stable error on the next line.
        embedder_index_identity = "<unavailable>"
    payload = {
        "environment": environment,
        "engine_id": id(engine) if engine is not None else None,
        "embedder_id": id(embedder) if embedder is not None else None,
        "embedder_index_identity": embedder_index_identity,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _server_process_identity(engine=None, environ=None) -> dict[str, Any]:
    """Return validated deployment and effective retrieval-runtime identity."""
    if _SOURCE_REVISION is None:
        raise RuntimeError("source_revision_unavailable")
    env = environ if environ is not None else os.environ
    allow_text_only = env.get("PP_HEALTH_ALLOW_TEXT_ONLY", "0") == "1"
    fusion_policy = str(env.get("PP_RETRIEVAL_FUSION_POLICY", "legacy-auto")).strip()
    runtime_engine = engine if engine is not None else get_engine()
    runtime_engine._ensure_heavy_init()

    vector_reason = None
    embedding_identity = None
    embedder = getattr(runtime_engine, "_embedder", None)
    route_probe = getattr(runtime_engine, "retrieval_embedding_probe", None)
    node_runtime_reader = getattr(runtime_engine, "memory_index_node_runtime", None)
    retrieval_route_active = callable(route_probe)
    node_route_active = retrieval_route_active and (
        not callable(node_runtime_reader) or node_runtime_reader() is not None
    )
    probe_source = (
        "governed_route"
        if node_route_active
        else "retrieval_route"
        if retrieval_route_active
        else "legacy_embedder"
    )
    if embedder is None and not retrieval_route_active:
        vector_reason = "retrieval_embedder_unavailable"
    else:
        try:
            if retrieval_route_active:
                probe_vector = route_probe("plastic promise retrieval health probe")
            else:
                probe_vector = embedder.embed("plastic promise retrieval health probe")
        except Exception:
            vector_reason = "retrieval_embedding_probe_failed"
            probe_vector = None
        if vector_reason is None and (
            not isinstance(probe_vector, list)
            or not probe_vector
            or any(
                not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in probe_vector
            )
            or not any(float(value) != 0.0 for value in probe_vector)
        ):
            vector_reason = "retrieval_embedding_zero_or_invalid"
        if vector_reason is None:
            try:
                if node_route_active:
                    embedding_identity = _governed_embedding_process_identity(
                        runtime_engine,
                        probe_vector,
                    )
                else:
                    embedding_identity = _embedding_process_identity(embedder, env)
            except RuntimeError:
                raise
    lancedb = getattr(runtime_engine, "_ldb", None)
    lancedb_required = env.get("LDB_INIT_ON_HEAVY_INIT", "1") == "1"
    lancedb_ready = lancedb is not None
    sync_status_reader = getattr(runtime_engine, "generation_live_index_status", None)
    if callable(sync_status_reader):
        lancedb_sync = sync_status_reader()
    else:
        raw_sync = getattr(runtime_engine, "_lancedb_sync_status", None)
        lancedb_sync = dict(raw_sync) if isinstance(raw_sync, dict) else None
    live_lag = lancedb_sync.get("lag") if isinstance(lancedb_sync, dict) else None
    live_lag_state = live_lag.get("state") if isinstance(live_lag, dict) else None
    if vector_reason is None and lancedb_required and not lancedb_ready:
        vector_reason = "retrieval_lancedb_unavailable"
    if vector_reason is None and live_lag_state in {"blocked", "unknown", "unavailable"}:
        vector_reason = f"retrieval_lancedb_live_index_{live_lag_state}"
    vector_ready = vector_reason is None
    bm25_ready = callable(getattr(runtime_engine, "_text_retrieval", None))
    if not bm25_ready:
        raise RuntimeError("retrieval_bm25_unavailable")
    if not vector_ready and not allow_text_only:
        raise RuntimeError(vector_reason) from None

    graph_ready = bool(getattr(runtime_engine, "_graph_edges", None))
    has_fts = (
        lancedb is not None
        and env.get("PP_FTS_DISABLED", "") != "1"
        and env.get("PP_FTS_FUSION", "1") == "1"
    )
    retrieval_plan = plan_retrieval(
        has_vector=vector_ready,
        has_graph=graph_ready,
        has_fts=has_fts,
    )
    fusion_config = load_fusion_config(fusion_policy, retrieval_plan, env)

    force_python = env.get("PP_FORCE_PYTHON_SUPPLY", "0") == "1"
    prefer_rust = env.get("PP_PREFER_RUST_SUPPLY", "1") == "1"
    requested_runtime = "python" if force_python or not prefer_rust else "rust"
    if force_python or not prefer_rust:
        effective_runtime = "python"
        capability_reason = "runtime_forced:python" if force_python else "runtime_preferred:python"
    elif fusion_config is not None and "fts" in retrieval_plan.fusion_channels:
        effective_runtime = "python"
        capability_reason = "rust_capability_missing:fts"
    else:
        if runtime_engine._check_rust_health() is True:
            supports_policy = getattr(runtime_engine, "_rust_supports_fusion_policy", None)
            if fusion_policy == "max-v1" and (
                not callable(supports_policy) or not supports_policy(fusion_policy)
            ):
                effective_runtime = "python"
                capability_reason = "rust_capability_missing:max-v1"
            else:
                effective_runtime = "rust"
                capability_reason = "rust_capability_satisfied"
        else:
            effective_runtime = "python"
            capability_reason = "rust_unavailable_or_failed"

    rust_runtime = None
    if effective_runtime == "rust":
        try:
            rust_runtime = loaded_rust_extension_identity(Path(_SOURCE_ROOT))
        except (ImportError, OSError, ValueError) as exc:
            raise RuntimeError("rust_runtime_identity_unavailable") from exc

    candidate_id = fusion_policy if fusion_policy.startswith("wrrf-v1:") else ""
    config_payload = None
    if fusion_config is not None:
        config_payload = {
            "k": fusion_config.k,
            "channels": list(fusion_config.channels),
            "weights": dict(fusion_config.weights),
            "windows": dict(fusion_config.windows),
        }
        recomputed_hash = canonical_fusion_config_hash(config_payload)
        if recomputed_hash != fusion_config.config_hash:
            raise FusionConfigurationError("fusion_health_config_hash_mismatch")
        config_payload["config_hash"] = recomputed_hash
    live_lagged = vector_ready and live_lag_state == "lagged"
    optional_capabilities = _optional_capability_health(runtime_engine, env)
    optional_capabilities_degraded = any(
        capability.get("state") == "degraded" for capability in optional_capabilities.values()
    )
    return {
        "identity_valid": True,
        "version": PLASTIC_PROMISE_VERSION,
        "pid": os.getpid(),
        "source_root": _SOURCE_ROOT,
        "source_revision": _SOURCE_REVISION,
        "embedding": embedding_identity,
        "embedding_probe": {
            "source": probe_source,
            "status": "ready" if vector_reason is None else "failed",
        },
        "health_policy": "text-only" if allow_text_only else "strict",
        "degraded": not vector_ready or live_lagged,
        "retrieval_status": (
            "ready_index_lagged"
            if live_lagged
            else "ready"
            if vector_ready
            else "degraded_text_only"
        ),
        "vector_ready": vector_ready,
        "vector_reason": vector_reason,
        "lancedb_ready": lancedb_ready,
        "lancedb_required": lancedb_required,
        "lancedb_sync": lancedb_sync,
        "bm25_ready": bm25_ready,
        "graph_ready": graph_ready,
        "optional_capabilities_degraded": optional_capabilities_degraded,
        "optional_capabilities": optional_capabilities,
        "fusion_policy": fusion_policy,
        "rust_runtime": rust_runtime,
        "fusion_attestation": {
            "schema": MCP_FUSION_IDENTITY_SCHEMA,
            "requested_policy": fusion_policy,
            "effective_policy": fusion_policy,
            "requested_runtime": requested_runtime,
            "effective_runtime": effective_runtime,
            "capability_reason": capability_reason,
            "candidate_id": candidate_id,
            "config_hash": candidate_id.partition(":")[2] if candidate_id else "",
            "config": config_payload,
        },
    }


def _governed_embedding_process_identity(
    runtime_engine: object,
    probe_vector: object,
) -> dict[str, Any]:
    """Return the bounded identity of the route that produced the health vector."""

    identity_reader = getattr(runtime_engine, "retrieval_embedding_identity", None)
    if not callable(identity_reader):
        raise RuntimeError("retrieval_embedding_identity_unavailable")
    try:
        raw_identity = identity_reader()
    except Exception:
        raise RuntimeError("retrieval_embedding_identity_unavailable") from None
    if not isinstance(raw_identity, dict):
        raise RuntimeError("retrieval_embedding_identity_invalid")
    try:
        provider = _bounded_identity_value(
            raw_identity.get("provider"),
            "retrieval_embedding_provider",
        )
        model = _bounded_identity_value(
            raw_identity.get("model"),
            "retrieval_embedding_model",
        )
        revision = _bounded_identity_value(
            raw_identity.get("revision") or raw_identity.get("model_revision"),
            "retrieval_embedding_model_revision",
        )
        normalization = _bounded_identity_value(
            raw_identity.get("normalization"),
            "retrieval_embedding_normalization",
        )
        index_identity = _bounded_identity_value(
            raw_identity.get("index_identity"),
            "retrieval_embedding_index_identity",
        )
    except RuntimeError:
        raise RuntimeError("retrieval_embedding_identity_invalid") from None
    dimension = raw_identity.get("dimension")
    if (
        type(dimension) is not int
        or dimension <= 0
        or not isinstance(probe_vector, list)
        or len(probe_vector) != dimension
    ):
        raise RuntimeError("retrieval_embedding_identity_invalid")
    usage_reader = getattr(runtime_engine, "retrieval_embedding_usage", None)
    if not callable(usage_reader):
        raise RuntimeError("retrieval_embedding_usage_unavailable")
    try:
        raw_usage = usage_reader()
    except Exception:
        raise RuntimeError("retrieval_embedding_usage_unavailable") from None
    if not isinstance(raw_usage, Mapping):
        raise RuntimeError("retrieval_embedding_usage_invalid")
    usage = dict(raw_usage)
    required_usage = {
        "embedding_requests",
        "embedding_input_tokens",
        "cost",
        "cost_currency",
        "cost_usd",
        "pricing_revision",
    }
    if set(usage) != required_usage:
        raise RuntimeError("retrieval_embedding_usage_invalid")
    if (
        type(usage["embedding_requests"]) is not int
        or usage["embedding_requests"] < 0
        or type(usage["embedding_input_tokens"]) is not int
        or usage["embedding_input_tokens"] < 0
        or usage["cost_currency"] not in SUPPORTED_COST_CURRENCIES
        or not isinstance(usage["pricing_revision"], str)
        or not usage["pricing_revision"].strip()
    ):
        raise RuntimeError("retrieval_embedding_usage_invalid")

    def optional_cost(value: object) -> float | None:
        if value is None:
            return None
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise RuntimeError("retrieval_embedding_usage_invalid")
        return float(value)

    cost = optional_cost(usage["cost"])
    cost_usd = optional_cost(usage["cost_usd"])
    if cost_usd != (cost if usage["cost_currency"] == "USD" else None):
        raise RuntimeError("retrieval_embedding_usage_invalid")
    usage["cost"] = cost
    usage["cost_usd"] = cost_usd
    return {
        "provider": provider,
        "model": model,
        "model_revision": revision,
        "dimension": dimension,
        "normalization": normalization,
        "index_identity": index_identity,
        "usage": usage,
    }


def _optional_capability_health(
    runtime_engine: object,
    env: Any,
) -> dict[str, dict[str, Any]]:
    """Report async semantic capability state without probing remote providers."""

    from plastic_promise.core import structured_memory_fusion
    from plastic_promise.passive_memory import semantic_pipeline

    structured_mode = str(env.get("PP_STRUCTURED_MEMORY_FUSION", "off")).strip().casefold()
    synthesis_mode = str(env.get("PP_SYNTHESIS_ARTIFACTS", "off")).strip().casefold()
    structured_enabled = structured_mode in {"shadow", "on"} and synthesis_mode in {
        "shadow",
        "on",
    }
    if not structured_enabled:
        structured_state = "disabled"
        structured_reason = ""
    else:
        structured_snapshot = structured_memory_fusion.durable_fusion_runtime_snapshot(
            runtime_engine
        )
        structured_state = structured_snapshot["state"]
        structured_reason = structured_snapshot["reason"]

    passive_mode = str(env.get("PP_PASSIVE_SEMANTIC_CAPTURE", "off")).strip().casefold()
    passive_enabled = passive_mode in {"shadow", "on"}
    if not passive_enabled:
        passive_state = "disabled"
        passive_reason = ""
    else:
        passive_snapshot = semantic_pipeline.semantic_memory_runtime_snapshot(runtime_engine)
        passive_state = passive_snapshot["state"]
        passive_reason = passive_snapshot["reason"]

    return {
        "passive_semantic_capture": {
            "enabled": passive_enabled,
            "state": passive_state,
            "reason": passive_reason,
        },
        "structured_memory_fusion": {
            "enabled": structured_enabled,
            "state": structured_state,
            "reason": structured_reason,
        },
    }


def _embedding_process_identity(embedder: object, env: Any) -> dict[str, Any]:
    """Return bounded provider identity and usage without endpoint or credential data."""

    model = _bounded_identity_value(getattr(embedder, "model_name", ""), "embedding_model")
    raw_stats = getattr(embedder, "stats", {})
    stats = raw_stats if isinstance(raw_stats, dict) else {}
    provider = _bounded_identity_value(
        stats.get("provider") or env.get("EMBEDDER_PROVIDER", type(embedder).__name__),
        "embedding_provider",
    )
    revision = _bounded_identity_value(
        stats.get("revision") or env.get("EMBEDDER_MODEL_REVISION", model),
        "embedding_model_revision",
    )
    pricing_revision = str(
        stats.get("pricing_revision") or env.get("EMBEDDER_PRICING_REVISION", "")
    ).strip()
    if pricing_revision:
        pricing_revision = _bounded_identity_value(
            pricing_revision,
            "embedding_pricing_revision",
        )
    dimension = getattr(embedder, "dim", 0)
    if type(dimension) is not int or dimension <= 0:
        raise RuntimeError("retrieval_embedding_dimension_invalid")

    def nonnegative_integer(name: str) -> int:
        value = stats.get(name, 0)
        return value if type(value) is int and value >= 0 else 0

    currency = (
        str(stats.get("cost_currency") or env.get("EMBEDDER_COST_CURRENCY", "USD")).strip().upper()
    )
    if currency not in SUPPORTED_COST_CURRENCIES:
        raise RuntimeError("embedding_cost_currency_invalid")

    def optional_cost(name: str) -> float | None:
        value = stats.get(name)
        if value is None:
            return None
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise RuntimeError("embedding_cost_invalid")
        return float(value)

    cost = optional_cost("estimated_cost")
    legacy_cost_usd = optional_cost("estimated_cost_usd")
    if cost is None and legacy_cost_usd is not None:
        if currency != "USD":
            raise RuntimeError("embedding_cost_currency_mismatch")
        cost = legacy_cost_usd
    if currency == "USD":
        if legacy_cost_usd is not None and legacy_cost_usd != cost:
            raise RuntimeError("embedding_cost_currency_mismatch")
        cost_usd = cost
    else:
        if legacy_cost_usd is not None:
            raise RuntimeError("embedding_cost_currency_mismatch")
        cost_usd = None
    return {
        "provider": provider,
        "model": model,
        "model_revision": revision,
        "dimension": dimension,
        "usage": {
            "embedding_requests": nonnegative_integer("requests"),
            "embedding_input_tokens": nonnegative_integer("input_tokens"),
            "cost": cost,
            "cost_currency": currency,
            "cost_usd": cost_usd,
            "pricing_revision": pricing_revision,
        },
    }


def _bounded_identity_value(value: object, reason: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise RuntimeError(f"{reason}_invalid")
    return normalized


def _dashboard_process_identity(environ=None) -> dict[str, Any]:
    """Return runtime identity without initializing retrieval dependencies."""
    from plastic_promise.launcher.runtime_mode import runtime_mode_status

    env = environ if environ is not None else os.environ
    identity = {
        "status": "ok",
        "version": PLASTIC_PROMISE_VERSION,
        "pid": os.getpid(),
        "source_root": _SOURCE_ROOT,
        "source_revision": _SOURCE_REVISION or "",
        "transport": str(env.get("PLASTIC_MCP_TRANSPORT") or "streamable_http"),
        "runtime": runtime_mode_status(env),
        "retrieval_initialized": _engine is not None,
    }
    refresh = getattr(_engine, "_runtime_refresh_status", None) if _engine is not None else None
    if isinstance(refresh, dict):
        identity["runtime_refresh"] = refresh
    return identity


def _is_windows_client_disconnect(context: dict[str, Any]) -> bool:
    """Identify benign Windows Proactor disconnect noise from closed HTTP clients."""
    if sys.platform != "win32":
        return False
    exc = context.get("exception")
    if not isinstance(exc, ConnectionResetError):
        return False
    handle = repr(context.get("handle") or "")
    message = str(context.get("message") or "")
    return "_call_connection_lost" in handle or "_call_connection_lost" in message


def _install_windows_client_disconnect_filter(logger: logging.Logger) -> None:
    """Suppress noisy client-close tracebacks while preserving real loop errors."""
    if sys.platform != "win32":
        return

    import asyncio

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def handle_exception(loop, context):
        if _is_windows_client_disconnect(context):
            logger.debug("Suppressed Windows client disconnect: %s", context.get("exception"))
            return
        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)


def get_engine():
    """获取 ContextEngine 单例（Python 主引擎，Rust 加速器就绪后切换）

    Python ContextEngine 拥有完整的数据管道：
    - SQLite 持久化 (plastic_memory.db)
    - LanceDB 向量检索
    - BM25 + RRF 混合检索
    - 原则注入 + 图谱遍历

    Rust context_engine_core 目前是占位实现（:memory: 存储、Noop 检索器），
    待 retriever backends 实现后通过 supply() 中的 _supply_rust 路径切换。
    """
    global _engine
    if _engine is not None:
        return _engine

    # Python 主引擎 — 完整数据管道
    from plastic_promise.core.context_engine import ContextEngine as PyEngine

    _engine = PyEngine()
    logging.info("ContextEngine: Python 核心已加载 (SQLite + LanceDB)")

    # Private-node routing is opt-in through the active controlled revision.
    # The bootstrap is fail-closed for derived indexing only: a missing local
    # tunnel, manifest, schema, or identity proof never blocks canonical
    # SQLite memory writes and never silently falls back to an ungoverned
    # remote embedding call.
    try:
        from plastic_promise.core.node_runtime_bootstrap import (
            bootstrap_memory_index_node_runtime,
        )

        node_runtime = bootstrap_memory_index_node_runtime(_engine)
        logging.info(
            "ContextEngine: node routing bootstrap state=%s reason=%s nodes=%d",
            node_runtime.state,
            node_runtime.reason,
            node_runtime.registered_nodes,
        )
    except Exception:
        # Bootstrap must not prevent the MCP process from serving text and
        # canonical-memory operations.  Do not log raw exception values here:
        # resolver failures may originate from private runtime material.
        _engine.set_memory_index_node_runtime_status(
            {
                "state": "blocked",
                "reason": "node_routing_bootstrap_unavailable",
                "registered_nodes": 0,
                "config_revision": None,
            }
        )
        logging.warning("ContextEngine: node routing bootstrap unavailable")

    # 预导入 Rust 加速器（如果可用），供 _supply_rust 路径使用
    try:
        from plastic_promise.core.rust_extension import try_load_context_engine_core

        if try_load_context_engine_core() is None:
            raise ImportError("context_engine_core")

        logging.info("ContextEngine: Rust 加速器可用（待 supply 路径启用）")
    except ImportError:
        logging.info("ContextEngine: Rust 加速器不可用（需编译 context_engine_core）")

    return _engine


def get_skill_engine():
    """获取 SkillEngine 单例，自动注册所有 Phase 1 技能。"""
    global _skill_engine
    if _skill_engine is not None:
        return _skill_engine

    from plastic_promise.skills.engine import SkillEngine
    from plastic_promise.skills.memory_operations import skill_smart_remember
    from plastic_promise.skills.official_workflow_stages import SKILL_DEFS as _OFFICIAL_DEFS
    from plastic_promise.skills.session_lifecycle import skill_session_init

    _skill_engine = SkillEngine(get_engine())
    _skill_engine.register(skill_session_init)
    _skill_engine.register(skill_smart_remember)
    # Register the pinned official engineering workflow adapters.
    for _name, _def in _OFFICIAL_DEFS.items():
        _skill_engine.register(_def)
    official_names = ", ".join(_OFFICIAL_DEFS.keys())
    logging.info(
        "SkillEngine: official workflow skills registered "
        f"(session-init, smart-remember, {official_names})"
    )
    return _skill_engine


# ---------------------------------------------------------------------------
# MCP Server 实例
# ---------------------------------------------------------------------------

server = Server(
    "plastic-promise",
    version=PLASTIC_PROMISE_VERSION,
    instructions=SERVER_INSTRUCTIONS,
)

_SYSTEM_NOTIFICATION_PROJECT_ID = "project:system-governance"


def _notification_event_with_project(
    event: dict[str, Any],
    runtime_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a project-scoped event without treating the scope as authentication."""

    if not isinstance(event, dict):
        raise ValueError("notification event must be an object")
    if "project_id" in event:
        project_id = canonical_project_id(event.get("project_id"))
        if not project_id:
            raise ValueError("canonical project_id is required")
    else:
        runtime_project_id = canonical_project_id((runtime_authority or {}).get("project_id"))
        project_id = runtime_project_id or _SYSTEM_NOTIFICATION_PROJECT_ID
    return {**event, "project_id": project_id}


class _ProjectNotificationHub:
    """Fan out general notifications to every subscriber in one project."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[Any]]] = {}

    def register(self, project_id: str, queue: asyncio.Queue[Any]) -> None:
        scoped = _notification_event_with_project(
            {"project_id": project_id, "type": "subscription"}
        )["project_id"]
        self._subscribers.setdefault(scoped, set()).add(queue)

    def unregister(self, project_id: str, queue: asyncio.Queue[Any]) -> None:
        subscribers = self._subscribers.get(project_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(project_id, None)

    def put_nowait(self, event: dict[str, Any]) -> None:
        scoped_event = _notification_event_with_project(event)
        project_id = scoped_event["project_id"]
        for queue in tuple(self._subscribers.get(project_id, ())):
            with suppress(Exception):
                queue.put_nowait(dict(scoped_event))

    async def put(self, event: dict[str, Any]) -> None:
        scoped_event = _notification_event_with_project(event)
        project_id = scoped_event["project_id"]
        for queue in tuple(self._subscribers.get(project_id, ())):
            await queue.put(dict(scoped_event))


_notify_queue: Any | None = None


def notify_issue_change(data: dict[str, Any]) -> None:
    """Push a state-change event when the HTTP event queue is active."""
    queue = _notify_queue
    if queue is not None:
        with suppress(Exception):
            queue.put_nowait(data)


def _task_event_subscription_scope(query_params: Any) -> tuple[str, str] | None:
    """Parse one required project-scoped local SSE subscription.

    The query scope isolates trusted loopback clients; it is not internet-grade
    multi-tenant authentication.
    """

    project_id = str(query_params.get("project_id") or "").strip()
    agent_name = str(query_params.get("agent_name") or "").strip()
    scoped_project_id = canonical_project_id(project_id)
    if not scoped_project_id or not agent_name:
        raise ValueError("canonical project_id and agent_name are required")
    return scoped_project_id, agent_name


_CODEX_DISCOVERY_HINTS = {
    "session-init": (
        "Plastic Promise MCP; Codex tool_search discovery; bootstrap; session init; "
        "startup; principles; SCARF; trust; chain_state."
    ),
    "sp-stage": (
        "Plastic Promise MCP; Codex tool_search discovery; Matt Pocock skills; "
        "official workflow stage; diagnosing-bugs; tdd; code-review; governed chain."
    ),
    "memory_recall": (
        "Plastic Promise MCP; Codex tool_search discovery; memory recall; memory_recall; "
        "retrieve memories; context; agent memory."
    ),
    "context_supply": (
        "Plastic Promise MCP; Codex tool_search discovery; context supply; context_supply; "
        "three-layer context pack; task context."
    ),
    "defense": (
        "Plastic Promise MCP; Codex tool_search discovery; trust; defense; permissions; "
        "trust score; autonomy."
    ),
    "step-closure": (
        "Plastic Promise MCP; Codex tool_search discovery; step closure; step_closure; "
        "SCARF reflection; trust feedback; CEI."
    ),
    "runtime_mode": (
        "Plastic Promise MCP; Codex tool_search discovery; runtime mode; hot update; "
        "launcher mode; Rust acceleration; light normal full."
    ),
    "commercial_audit_export": (
        "Plastic Promise MCP; Codex tool_search discovery; commercial audit export; "
        "call spans; degradation events; store outbox; traceability bundle."
    ),
    "knowledge_search": (
        "Plastic Promise MCP; Codex tool_search discovery; knowledge search; "
        "knowledge_search; lexical retrieval; citations; knowledge system."
    ),
}

_REQUEST_SCOPE_PROPERTIES = {
    "stage_session_id": {
        "type": "string",
        "description": "Workflow stage/session scope id for isolating concurrent heavy calls",
    },
    "flow_line_id": {
        "type": "string",
        "description": "Flow line id within stage_session_id; pairs with stage-style workflow isolation",
    },
    "request_id": {
        "type": "string",
        "description": "Caller supplied per-call request id; omitted values are generated server-side",
    },
}

_PROJECT_CONTEXT_PROPERTIES = {
    "project_id": {
        "type": "string",
        "description": "Canonical project identity, e.g. project:plastic-promise",
    },
    "project_policy": {
        "type": "string",
        "enum": ["strict", "balanced", "open"],
        "description": "Project isolation policy for recall/context layers",
    },
}

_RETRIEVAL_MODE_PROPERTY = {
    "retrieval_mode": {
        "type": "string",
        "enum": [
            "local",
            "global",
            "hybrid",
            "mix",
            "project",
            "code",
            "audit",
            "principle",
        ],
        "description": "Optional explicit retrieval strategy mode",
    }
}

_BEHAVIOR_GRAPH_NODE_TYPES = [
    "memory",
    "principle",
    "tool",
    "task",
    "audit_span",
    "code_symbol",
    "file",
    "class",
    "function",
    "method",
    "test",
    "doc",
    "mcp_tool",
    "evidence",
    "document_chunk",
    "skill_session",
    "code_module",
]

_PROVENANCE_PROPERTIES = {
    "visibility": {
        "type": "string",
        "enum": ["project", "global", "shared", "private"],
        "description": "Memory visibility boundary",
    },
    "source_class": {
        "type": "string",
        "description": "Memory source class such as user_fact, code_fact, experience, prompt, telemetry",
    },
    "origin_kind": {"type": "string", "description": "Origin kind for provenance"},
    "origin_uri": {"type": "string", "description": "Origin URI for provenance"},
    "origin_ref": {"type": "string", "description": "Origin reference for provenance"},
    "origin_hash": {"type": "string", "description": "Origin content hash for provenance"},
    "parent_memory_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Parent memory ids used to derive this memory",
    },
    "metadata_json": {"type": "object", "description": "Structured provenance metadata"},
    "call_id": {"type": "string", "description": "Trace call id"},
    "parent_call_id": {"type": "string", "description": "Parent trace call id"},
    "commit_mode": {
        "type": "string",
        "enum": ["direct", "propose"],
        "description": "Advisory write intent; never bypasses server proposal policy",
    },
    "origin_role": {
        "type": "string",
        "description": "Originating conversation role; server runtime provenance remains authoritative",
    },
    "origin_turn_hash": {
        "type": "string",
        "description": "Stable hash of the originating user turn for proposal deduplication",
    },
    "origin_visibility": {
        "type": "string",
        "enum": ["project", "global", "shared", "private"],
        "description": "Visibility boundary of the originating turn",
    },
}

_GROUND_TRUTH_PROPERTY = {
    "ground_truth": {
        "type": "object",
        "description": "Optional versioned relevance labels; computes hit@k/MRR in trace only",
        "properties": {
            "case_id": {"type": "string"},
            "dataset_revision": {"type": "string", "minLength": 1},
            "corpus_hash": {"type": "string", "minLength": 1},
            "relevant_memory_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "uniqueItems": True,
            },
            "forbidden_memory_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "ks": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 100},
                "minItems": 1,
                "uniqueItems": True,
            },
        },
        "required": ["dataset_revision", "corpus_hash", "relevant_memory_ids"],
        "additionalProperties": False,
    }
}


_FUSION_POLICY_PROPERTY = {
    "fusion_policy": {
        "type": "string",
        "pattern": "^(legacy-auto|max-v1|wrrf-v1:[0-9a-f]{64})$",
        "description": "Normalized retrieval fusion policy identifier",
    }
}


def _with_codex_discovery_hints(tools: list[Tool]) -> list[Tool]:
    """Append English discovery terms for clients that search deferred MCP metadata."""
    by_name = {tool.name: tool for tool in tools}
    for name, hint in _CODEX_DISCOVERY_HINTS.items():
        tool = by_name.get(name)
        if tool is None:
            continue
        marker = "Codex/tool_search discovery:"
        if marker not in (tool.description or ""):
            tool.description = f"{tool.description} {marker} {hint}"
    return tools


# ---------------------------------------------------------------------------
# 能力声明
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    """声明所有 MCP 工具"""
    from plastic_promise.skills.official_workflow_stages import STAGE_ATOMS, STAGE_ROUTE_MAP

    tools: list[Tool] = []

    # === 记忆域 ===
    tools.extend(
        [
            Tool(
                name="memory_recall",
                description="混合检索记忆（文本+图遍历双通道），返回三层上下文包。strict=True 时无匹配返回空。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索查询 / 任务描述"},
                        "task_type": {
                            "type": "string",
                            "description": "任务类型: code_generation/code_review/debugging/architecture/refactoring/learning/collaboration",
                        },
                        "max_results": {"type": "integer", "description": "最大返回数 (默认 20)"},
                        "min_relevance": {
                            "type": "number",
                            "description": "最低关联分数 (默认 0.2)",
                        },
                        "include_principles": {
                            "type": "boolean",
                            "description": "是否注入原则 (默认 true)",
                        },
                        "strict": {
                            "type": "boolean",
                            "description": "严格模式: 无匹配时返回空 (默认 false)",
                        },
                        "debug": {
                            "type": "boolean",
                            "description": "兼容开关；等价于 response_mode=debug，诊断默认仅返回有界摘要",
                        },
                        "response_mode": {
                            "type": "string",
                            "enum": ["standard", "compact", "debug"],
                            "description": "响应投影：standard=常规、compact=最小上下文、debug=带有界诊断",
                        },
                        "diagnostics_level": {
                            "type": "string",
                            "enum": ["summary", "full"],
                            "description": "debug 诊断粒度；full 仍受数量与字段预算限制",
                        },
                        "scope": {
                            "type": "string",
                            "description": "检索范围: global (默认) 或 domain 限定",
                        },
                        "domain_hint": {
                            "type": "string",
                            "description": "域联邦提示域；用于生成跨域信号",
                        },
                        "federation": {
                            "type": "boolean",
                            "description": "是否生成跨域联邦信号 (默认 true)",
                        },
                        "pack": {
                            "type": "string",
                            "description": "兼容字段；预留给经验包限定检索",
                        },
                        "retrieval_mode": {
                            "type": "string",
                            "description": "Optional explicit retrieval strategy mode",
                            "enum": [
                                "local",
                                "global",
                                "hybrid",
                                "mix",
                                "project",
                                "code",
                                "audit",
                                "principle",
                            ],
                        },
                        **_PROJECT_CONTEXT_PROPERTIES,
                        **_REQUEST_SCOPE_PROPERTIES,
                        **_GROUND_TRUTH_PROPERTY,
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="memory_store",
                description="存储一条记忆到 Plastic Promise 记忆池。自动分类 (task/experience/principle/code) 并建立实体关联。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "记忆内容"},
                        "memory_type": {
                            "type": "string",
                            "description": "类型: task/experience/principle/code",
                        },
                        "source": {
                            "type": "string",
                            "description": "来源: user/system/previous_output",
                        },
                        "entity_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关联实体 ID 列表",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "自定义标签 (task:pending, assignee:pi_builder 等)",
                        },
                        "source_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Canonical evidence memory ids for governed synthesis",
                        },
                        "synthesis_key": {
                            "type": "string",
                            "description": "Stable unique key for a governed synthesis artifact",
                        },
                        "validity_scope": {
                            "type": "string",
                            "description": "Declared validity scope for the synthesis",
                        },
                        "automatic": {
                            "type": "boolean",
                            "description": "Whether synthesis creation was automatic",
                        },
                        "reuse_signal": {
                            "type": "boolean",
                            "description": "Whether reuse evidence justifies automatic synthesis",
                        },
                        "expected_revision": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "CAS revision for refreshing an existing synthesis key",
                        },
                        "actor": {
                            "type": "string",
                            "description": "Actor responsible for the lifecycle mutation",
                        },
                        **_PROJECT_CONTEXT_PROPERTIES,
                        **_PROVENANCE_PROPERTIES,
                    },
                    "required": ["content", "memory_type"],
                },
            ),
            Tool(
                name="memory_update",
                description="更新已有记忆的内容或元数据。更新后重置 worth 计数以重新评估。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "记忆 ID"},
                        "content": {"type": "string", "description": "新内容"},
                        "importance": {"type": "number", "description": "更新后的重要性"},
                        "category": {"type": "string", "description": "更新后的分类"},
                        "reset_worth": {"type": "boolean", "description": "是否重置 worth 计数器"},
                        "reason": {
                            "type": "string",
                            "description": "内容替换的审计原因",
                        },
                    },
                    "required": ["memory_id"],
                },
            ),
            Tool(
                name="memory_forget",
                description="软删除记忆（标记为衰退，7天后 GC 清理）。不会立即删除，可恢复。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "记忆 ID"},
                        "reason": {"type": "string", "description": "删除原因"},
                    },
                    "required": ["memory_id"],
                },
            ),
            Tool(
                name="memory_list",
                description="按条件列出记忆：类型、来源、时间范围、worth 范围。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "memory_type": {"type": "string", "description": "筛选类型"},
                        "source": {"type": "string", "description": "筛选来源"},
                        "min_worth": {"type": "number", "description": "最低 worth_score"},
                        "limit": {"type": "integer", "description": "返回数量上限"},
                    },
                },
            ),
            Tool(
                name="memory_gc",
                description="手动触发垃圾回收：清除 worth_score 低于阈值且超过 7 天未访问的衰退记忆。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dry_run": {
                            "type": "boolean",
                            "description": "仅预览，不实际删除 (默认 true)",
                        },
                        "force": {"type": "boolean", "description": "强制删除所有标记记忆"},
                    },
                },
            ),
            Tool(
                name="memory_correct",
                description="人类纠正记忆：编辑内容、标记为错误/已废弃/已纠正。服务于原则 2（可查可透明）和原则 3（审计闭环）。",
                inputSchema={
                    "type": "object",
                    "required": ["memory_id"],
                    "properties": {
                        "memory_id": {"type": "string", "description": "目标记忆 ID"},
                        "content": {"type": "string", "description": "纠正后的新内容 (可选)"},
                        "mark_as": {
                            "type": "string",
                            "description": "质量标记: corrected / deprecated / wrong",
                        },
                        "reason": {"type": "string", "description": "纠正原因说明"},
                    },
                },
            ),
            Tool(
                name="memory_reclassify",
                description="强制已有记忆重跑分类管线（tier/domain/category）。批量处理，支持断点续传。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "batch_size": {"type": "integer", "description": "每批处理数量 (默认 50)"},
                        "resume_from": {
                            "type": "integer",
                            "description": "断点续传游标 (从第几条开始)",
                        },
                        "dry_run": {"type": "boolean", "description": "仅预览不执行 (默认 false)"},
                    },
                },
            ),
        ]
    )

    # === 原则域 ===
    tools.extend(
        [
            Tool(
                name="principle_activate",
                description="根据任务类型自动激活相关核心原则。返回原则列表及其关联权重。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_type": {"type": "string", "description": "任务类型"},
                        "task_description": {
                            "type": "string",
                            "description": "任务描述（用于关键词匹配）",
                        },
                        "max_principles": {"type": "integer", "description": "最多返回原则数"},
                        "domain_hint": {
                            "type": "string",
                            "description": "可选，限定域: building|fixing|designing|reflecting|governing|connecting|all",
                            "enum": [
                                "building",
                                "fixing",
                                "designing",
                                "reflecting",
                                "governing",
                                "connecting",
                                "all",
                            ],
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Optional project id for project-level principle overlays",
                        },
                    },
                    "required": ["task_type"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="principle_evaluate",
                description="反事实评估：对指定原则进行「如果违反会怎样」的预演，为 Agent 提供非强制但充分的决策依据。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "principle_id": {
                            "oneOf": [
                                {"type": "integer", "minimum": 1},
                                {"type": "string", "pattern": "^[0-9]+$"},
                            ],
                            "description": "原则 ID（整数或数字字符串）",
                        },
                        "scenario": {"type": "string", "description": "当前决策场景描述"},
                    },
                    "required": ["principle_id", "scenario"],
                },
            ),
        ]
    )

    # === 上下文域 ===
    tools.extend(
        [
            Tool(
                name="context_supply",
                description="【核心工具】调用 ContextEngine.supply()，返回三层结构化上下文包：核心层/关联层/发散层。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_description": {
                            "type": "string",
                            "description": "当前任务的完整自然语言描述（含前文上下文）",
                        },
                        "task_type": {"type": "string", "description": "任务类型标签"},
                        "scope": {
                            "type": "string",
                            "description": "检索范围: global (默认) 或 domain 限定",
                        },
                        **_RETRIEVAL_MODE_PROPERTY,
                        "debug": {
                            "type": "boolean",
                            "description": "兼容开关；等价于 response_mode=debug，诊断默认仅返回有界摘要",
                        },
                        "response_mode": {
                            "type": "string",
                            "enum": ["standard", "compact", "debug"],
                            "description": "响应投影：standard=原 prompt、compact=临时结构包、debug=临时结构包+有界诊断",
                        },
                        "diagnostics_level": {
                            "type": "string",
                            "enum": ["summary", "full"],
                            "description": "debug 诊断粒度；full 仍受数量与字段预算限制",
                        },
                        **_PROJECT_CONTEXT_PROPERTIES,
                        **_REQUEST_SCOPE_PROPERTIES,
                        **_GROUND_TRUTH_PROPERTY,
                    },
                    "required": ["task_description"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="context_inject",
                description="手动向 EntityGraph 注入原则关联边，或注册新实体节点。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_type": {
                            "type": "string",
                            "enum": _BEHAVIOR_GRAPH_NODE_TYPES,
                            "description": "实体类型: task/principle/code_module/memory",
                        },
                        "entity_id": {"type": "string"},
                        "entity_name": {"type": "string"},
                        "entity_description": {"type": "string"},
                        "related_entities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关联实体 ID",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Optional typed behavior graph metadata",
                        },
                    },
                    "required": ["entity_type", "entity_id", "entity_name"],
                },
            ),
            Tool(
                name="context_graph",
                description="查询实体关联图谱：节点列表、边关系、多跳遍历、激活路径可视化数据。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "start_node": {"type": "string", "description": "起始节点 ID"},
                        "max_hops": {"type": "integer", "description": "最大跳数 (默认 3)"},
                        "query_type": {
                            "type": "string",
                            "description": "查询类型: node_info/traverse/full_graph/neighbors",
                            "enum": ["node_info", "traverse", "full_graph", "neighbors"],
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="auto_context_inject",
                description="Provider-neutral passive memory adapter: before_invoke preloads bounded ephemeral context; after_invoke asynchronously audits explicit user facts into the governed proposal outbox. Injected context is never stored as long-term memory.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "event": {
                            "type": "string",
                            "enum": ["before_invoke", "after_invoke", "session_end"],
                            "description": "Lifecycle event; defaults to before_invoke for compatibility",
                        },
                        "task_description": {
                            "type": "string",
                            "description": "Current task description used for passive context retrieval",
                        },
                        "task_type": {
                            "type": "string",
                            "description": "Task type label (default general)",
                        },
                        "user_text": {
                            "type": "string",
                            "description": "Original user-authored text; after_invoke only reads this field",
                        },
                        "assistant_text": {
                            "type": "string",
                            "description": "Optional assistant outcome for trace only; never treated as a user fact",
                        },
                        "source": {
                            "type": "string",
                            "description": "Adapter identity such as pi_agent, claude_code, langgraph, or manual",
                        },
                        "call_id": {"type": "string"},
                        "parent_call_id": {"type": "string"},
                        "request_id": {"type": "string"},
                        "hook_session_id": {
                            "type": "string",
                            "description": "Hook session identity bound to the server-issued collaboration continuation",
                        },
                        _COLLABORATION_CONTINUATION_TOKEN_ARGUMENT: {
                            "type": "string",
                            "description": "Opaque short-lived server-issued bearer assertion; do not log, persist, inspect, or modify",
                        },
                        "stage_session_id": {"type": "string"},
                        "flow_line_id": {"type": "string"},
                        "project_id": {"type": "string"},
                        "project_policy": {"type": "string"},
                        "visibility": {
                            "type": "string",
                            "enum": ["private", "project", "shared", "global"],
                        },
                        "metadata": {"type": "object"},
                        "scope": {
                            "type": "string",
                            "description": "Deprecated compatibility hint; project fields define isolation",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="mgp_shadow_bridge",
                description="MGP-compatible memory governance shadow bridge: status/set_mode/evaluate; P1 is audit-only and does not mutate memory.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "status|set_mode|evaluate",
                            "enum": ["status", "set_mode", "evaluate"],
                        },
                        "mode": {
                            "type": "string",
                            "description": "Bridge rollout mode",
                            "enum": ["off", "shadow", "inject"],
                        },
                        "operation": {
                            "type": "string",
                            "description": "MGP operation: write/search/get/update/expire/delete/revoke/purge/list",
                        },
                        "subject": {"type": "string", "description": "MGP subject or scope"},
                        "content": {"type": "string", "description": "Candidate memory content"},
                        "metadata": {"type": "object", "description": "MGP operation metadata"},
                        "policy_context": {
                            "type": "object",
                            "description": "Policy context carrying project_id, trust tier, request scope, source agent, and domain",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            ),
        ]
    )

    # === 审计与防线 ===
    tools.extend(
        [
            Tool(
                name="audit_run",
                description="执行七维审计: action=full(默认)|report",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "full|report",
                            "enum": ["full", "report"],
                        },
                        "scope": {
                            "type": "string",
                            "description": "审计范围: full/quick/principles_only/memory_only",
                        },
                        "time_range_hours": {
                            "type": "integer",
                            "description": "审计时间范围（小时）",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="audit_pre_check",
                description="实时合规检查：对即将执行的操作进行 L0 硬边界和 L1 约束衰减检查。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action_description": {"type": "string", "description": "操作描述"},
                        "action_type": {
                            "type": "string",
                            "description": "操作类型: exec/write/edit/delete/read",
                            "enum": ["exec", "write", "edit", "delete", "read"],
                        },
                    },
                    "required": ["action_description"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="defense",
                description="防线管理: action=get|history|adjust|status|evaluate_tool",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "get|history|adjust|status|evaluate_tool",
                            "enum": ["get", "history", "adjust", "status", "evaluate_tool"],
                        },
                        "delta": {"type": "number", "description": "调整量 (±0.01 ~ ±0.10)"},
                        "reason": {"type": "string", "description": "调整原因"},
                        "target": {"type": "string", "description": "信任分目标 (空串=当前 Agent)"},
                        "tool_name": {
                            "type": "string",
                            "description": "Tool name for action=evaluate_tool",
                        },
                        "trust_score": {
                            "type": "number",
                            "description": "Optional trust score override for tool semantic evaluation",
                        },
                        "trust_tier": {
                            "type": "string",
                            "description": "Optional trust tier label for tool semantic evaluation",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            ),
        ]
    )

    # === 自省与演化 ===
    tools.extend(
        [
            Tool(
                name="scarf_reflect",
                description="SCARF 五维自省: mode=standard|inertia",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "context": {"type": "string", "description": "当前上下文/最近行为描述"},
                        "dimensions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "指定维度 (空=全部)",
                        },
                        "mode": {"type": "string", "description": "standard|inertia"},
                    },
                    "required": ["context"],
                },
            ),
            Tool(
                name="feedback_apply",
                description="向记忆或上下文条目手动应用反馈：adopted/ignored/rejected，更新 worth 计数器和自演化权重。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string", "description": "条目 ID"},
                        "feedback_type": {
                            "type": "string",
                            "description": "反馈类型: adopted/ignored/rejected",
                        },
                        "task_context": {"type": "string", "description": "触发反馈的任务上下文"},
                        "actor": {
                            "type": "string",
                            "description": (
                                "Caller-declared reviewer identity for audit only; "
                                "runtime authority is server-owned"
                            ),
                        },
                        "call_id": {
                            "type": "string",
                            "description": (
                                "Caller-declared call id for audit only; "
                                "approval evidence uses a server call id"
                            ),
                        },
                        "expected_revision": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Expected synthesis revision for CAS feedback",
                        },
                        "rejection_reason": {
                            "type": "string",
                            "description": "Reason for contesting a rejected synthesis",
                        },
                        **_REQUEST_SCOPE_PROPERTIES,
                    },
                    "required": ["item_id", "feedback_type"],
                },
            ),
        ]
    )

    # === 管理域 ===
    tools.extend(
        [
            Tool(
                name="system",
                description=(
                    "系统工具: action=stats|backup|migrate|benchmark。stats 含模糊缓存"
                    "积压计数；benchmark 提供检索性能历史/显式运行。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["stats", "backup", "migrate", "benchmark"],
                            "description": "stats|backup|migrate|benchmark",
                        },
                        "format": {"type": "string", "description": "导出格式: json/sqlite"},
                        "source_path": {"type": "string", "description": "源数据路径"},
                        "source_type": {
                            "type": "string",
                            "description": "源类型: lancedb/json/csv",
                        },
                        "include_audit_history": {"type": "boolean"},
                        "dry_run": {"type": "boolean", "description": "仅预览，不实际导入"},
                        "run": {
                            "type": "boolean",
                            "description": "benchmark: true 执行检索探针，false 仅读历史",
                        },
                        "queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "benchmark: 检索探针查询列表",
                        },
                        "repeat": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "benchmark: 每条查询重复次数",
                        },
                        "benchmark_name": {
                            "type": "string",
                            "description": "benchmark: 历史分组名称，默认 retrieval",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "benchmark: 历史汇总最多读取的近期样本数",
                        },
                        "baseline_name": {
                            "type": "string",
                            "description": "benchmark: baseline 名称，默认 default",
                        },
                        "set_baseline": {
                            "type": "boolean",
                            "description": "benchmark: 将当前摘要保存为 baseline",
                        },
                        "gate": {
                            "type": "boolean",
                            "description": "benchmark: 对当前摘要执行回归门禁",
                        },
                        "tolerance_ratio": {
                            "type": "number",
                            "minimum": 0,
                            "description": "benchmark: baseline 允许退化比例，默认 0.20",
                        },
                        "max_p50_ms": {
                            "type": "number",
                            "minimum": 0,
                            "description": "benchmark: p50 绝对上限",
                        },
                        "max_p95_ms": {
                            "type": "number",
                            "minimum": 0,
                            "description": "benchmark: p95 绝对上限",
                        },
                        "max_p99_ms": {
                            "type": "number",
                            "minimum": 0,
                            "description": "benchmark: p99 绝对上限",
                        },
                    },
                    "required": ["action"],
                },
            ),
            Tool(
                name="runtime_mode",
                description=(
                    "Get or hot-update the current MCP runtime mode. Modes: light, "
                    "normal, rust-normal, full, rust-full."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["get", "set"],
                            "description": "get|set",
                        },
                        "mode": {
                            "type": "string",
                            "enum": list(RUNTIME_MODE_KEYS),
                            "description": "Required when action=set.",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="issue_create",
                description="创建新 Issue，关联原则和依赖关系。服务实践层：约定→任务→追踪。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Issue 标题"},
                        "description": {"type": "string", "description": "详细描述"},
                        "principle_id": {"type": "integer", "description": "关联原则 ID (1-12)"},
                        "memory_ids": {"type": "array", "items": {"type": "string"}},
                        "blocks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "此 Issue 阻塞的 Issue ID 列表",
                        },
                        "blocked_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "阻塞此 Issue 的 Issue ID 列表",
                        },
                        "owner": {"type": "string", "description": "Agent owner"},
                    },
                    "required": ["title"],
                },
            ),
            Tool(
                name="issue_transition",
                description="推进 Issue 状态: open→in_progress→resolved→closed。自动检查依赖。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "issue_id": {"type": "string"},
                        "state": {
                            "type": "string",
                            "description": "目标状态: in_progress/resolved/closed",
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["issue_id", "state"],
                },
            ),
            Tool(
                name="issue_list",
                description="列出 Issue，支持按状态和 owner 筛选。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": "筛选状态: open/in_progress/resolved/closed",
                        },
                        "owner": {"type": "string", "description": "筛选 owner"},
                    },
                },
            ),
            Tool(
                name="pack_export",
                description="Export memories as a shareable JSON experience pack. Filter by tags or memory IDs.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Pack name (used as filename)"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags to filter memories by",
                        },
                        "memory_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Specific memory IDs to include",
                        },
                        "author": {
                            "type": "string",
                            "description": "Author identifier (default: claude)",
                        },
                        "description": {"type": "string", "description": "Pack description"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="pack_import",
                description="导入经验包。strategy: skip(默认)|replace|merge。merge 时 domain 以包内为准。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the JSON pack file"},
                        "owner": {
                            "type": "string",
                            "description": "Owner to assign to imported memories",
                        },
                        "strategy": {"type": "string", "description": "skip|replace|merge"},
                    },
                    "required": ["path"],
                },
            ),
        ]
    )

    # === 域联邦域 ===
    tools.extend(
        [
            Tool(
                name="domain",
                description="域联邦统一入口: action=stats|merge|unmerge|rename|rebuild",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "stats|merge|unmerge|rename|rebuild",
                        },
                        "source": {"type": "string", "description": "源域 (merge/unmerge)"},
                        "target": {"type": "string", "description": "目标域 (merge)"},
                        "old_name": {"type": "string", "description": "旧域名 (rename)"},
                        "new_name": {"type": "string", "description": "新域名 (rename)"},
                    },
                    "required": ["action"],
                },
            ),
        ]
    )

    # === 任务队列域 ===
    tools.extend(
        [
            Tool(
                name="task_enqueue",
                description="Hunter Guild 委托上架 — 将任务挂到公会板上。自动验证提交者等级权限，C级猎人挂A/B级委托需Claude审批。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project identity, e.g. project:plastic-promise",
                        },
                        "task_type": {
                            "type": "string",
                            "description": "任务类型: fix_memory/gc_*/build_*/refactor_*/review_*/investigate_*",
                        },
                        "title": {"type": "string", "description": "任务标题"},
                        "to_agent": {"type": "string", "description": "目标 Agent"},
                        "priority": {
                            "type": "integer",
                            "description": "优先级: 1=S级 2=A级 3=B级 4=C级 (默认 3)",
                        },
                        "from_agent": {"type": "string", "description": "提交者 (默认 daemon)"},
                        "from_trust_score": {
                            "type": "number",
                            "description": "提交者信任分 (非 daemon/claude 时需提供)",
                        },
                        "description": {"type": "string", "description": "任务描述"},
                        "domain": {"type": "string", "description": "域"},
                        "memory_id": {"type": "string", "description": "关联记忆 ID"},
                        "principle_id": {"type": "string", "description": "关联原则 ID"},
                        "source_scan": {"type": "string", "description": "来源扫描器"},
                        "parent_task_id": {"type": "string", "description": "父任务 ID"},
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "超时秒数 (默认 300)",
                        },
                        "max_escalations": {
                            "type": "integer",
                            "description": "最大升级次数 (默认 3)",
                        },
                        "payload": {"type": "object", "description": "附加数据"},
                    },
                    "required": ["project_id", "task_type", "title", "to_agent"],
                },
            ),
            Tool(
                name="task_claim",
                description="Hunter Guild 委托揭榜 — 猎人认领公会板上的委托。原子操作，先到先得。自动检查等级匹配，force=True 可越级揭榜(会记录)。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project identity owning the task",
                        },
                        "agent_name": {"type": "string", "description": "揭榜猎人名称"},
                        "task_id": {"type": "string", "description": "要认领的委托 ID"},
                        "trust_score": {"type": "number", "description": "猎人当前信任分"},
                        "force": {"type": "boolean", "description": "强制越级揭榜 (默认 false)"},
                    },
                    "required": ["project_id", "agent_name", "task_id", "trust_score"],
                },
            ),
            Tool(
                name="task_complete",
                description="Hunter Guild 委托完成 — 猎人提交已完成委托，自动创建验收子任务给 Claude。只有揭榜猎人才能提交完成。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project identity owning the task",
                        },
                        "task_id": {"type": "string", "description": "委托 ID"},
                        "agent_name": {"type": "string", "description": "提交完成的猎人名称"},
                        "result": {"type": "string", "description": "完成结果描述"},
                        "artifacts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "产物路径列表",
                        },
                    },
                    "required": ["project_id", "task_id", "agent_name", "result"],
                },
            ),
            Tool(
                name="task_verify",
                description="Hunter Guild 委托验收 — 长老验收已完成委托。accepted 信任分+0.02，rejected/reassigned 信任分-0.03 并自动重派。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project identity owning the task",
                        },
                        "task_id": {"type": "string", "description": "待验收的委托 ID"},
                        "verdict": {
                            "type": "string",
                            "description": "验收结论: accepted | rejected | reassigned",
                        },
                        "verified_by": {"type": "string", "description": "验收者 (默认 claude)"},
                        "comment": {"type": "string", "description": "验收评语"},
                        "reassign_to_agent": {
                            "type": "string",
                            "description": "重派目标 Agent (默认原 to_agent)",
                        },
                    },
                    "required": ["project_id", "task_id", "verdict"],
                },
            ),
            Tool(
                name="task_inbox",
                description="Hunter Guild 委托板查看 — 显示可接委托、我的进行中任务和等级匹配度。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project identity whose task board is visible",
                        },
                        "agent_name": {"type": "string", "description": "查看委托板的猎人名称"},
                        "trust_score": {"type": "number", "description": "猎人当前信任分"},
                        "filter_status": {
                            "type": "string",
                            "description": "pending | my_active | pending_review | all (默认 pending)",
                        },
                        "limit": {"type": "integer", "description": "返回数量上限 (默认 20)"},
                    },
                    "required": ["project_id", "agent_name", "trust_score"],
                },
            ),
            Tool(
                name="task_heartbeat",
                description="Hunter Guild 委托心跳 — 猎人汇报任务仍在执行，避免超时释放。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project identity owning the task",
                        },
                        "task_id": {"type": "string", "description": "委托 ID"},
                        "agent_name": {"type": "string", "description": "揭榜猎人名称"},
                    },
                    "required": ["project_id", "task_id", "agent_name"],
                },
            ),
            Tool(
                name="task_abandon",
                description="Hunter Guild 主动弃单 — 放弃已揭榜委托并记录信任分惩罚。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project identity owning the task",
                        },
                        "task_id": {"type": "string", "description": "委托 ID"},
                        "agent_name": {"type": "string", "description": "揭榜猎人名称"},
                        "reason": {"type": "string", "description": "弃单原因"},
                    },
                    "required": ["project_id", "task_id", "agent_name"],
                },
            ),
        ]
    )

    # === PR5 durable ProjectWorkBoard ===
    # These tools never accept AgentSession, lease, role, policy, capability,
    # or storage identifiers.  The server resolves every authority-bearing
    # value from the exact authenticated transport binding created by
    # session-init.
    tools.extend(
        [
            Tool(
                name="collaboration_work_list",
                description=(
                    "列出当前认证 MCP transport 在其精确 project/workflow scope 中的"
                    "有界 ProjectWorkBoard inbox；只读且不授予 authority。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": _PROJECT_CONTEXT_PROPERTIES["project_id"],
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 64,
                            "default": 32,
                        },
                    },
                },
            ),
            Tool(
                name="collaboration_work_register",
                description=(
                    "由服务器为当前认证 Agent session 签发并持久化 WorkReceipt。调用方只能描述"
                    "objective、依赖和幂等提示；project/scope/assignee/time/fence 均由服务器绑定。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": _PROJECT_CONTEXT_PROPERTIES["project_id"],
                        "objective": {"type": "string", "maxLength": 4096},
                        "dependency_work_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 32,
                        },
                        "max_attempts": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 1,
                        },
                        "request_id": {
                            "type": "string",
                            "description": "Bounded idempotency hint; grants no authority.",
                        },
                    },
                    "required": ["objective"],
                },
            ),
            Tool(
                name="collaboration_work_claim",
                description=(
                    "认领已分配给当前认证 transport 的 work；lease、owner、fence、attempt 与"
                    "session authority 均由服务器解析和签发。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": _PROJECT_CONTEXT_PROPERTIES["project_id"],
                        "work_item_id": {"type": "string"},
                    },
                    "required": ["work_item_id"],
                },
            ),
            Tool(
                name="collaboration_lease_heartbeat",
                description=(
                    "为当前认证 transport 精确拥有的 active collaboration lease 发送服务器心跳。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": _PROJECT_CONTEXT_PROPERTIES["project_id"],
                        "work_item_id": {"type": "string"},
                    },
                    "required": ["work_item_id"],
                },
            ),
            Tool(
                name="collaboration_work_review",
                description=(
                    "由当前认证 peer session 将 submitted work 推进到 reviewing；"
                    "reviewer identity/session 由服务器绑定。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": _PROJECT_CONTEXT_PROPERTIES["project_id"],
                        "work_item_id": {"type": "string"},
                    },
                    "required": ["work_item_id"],
                },
            ),
            Tool(
                name="collaboration_work_accept",
                description=(
                    "使用服务器已持久签发的 AcceptanceReceipt 推进 reviewed work；"
                    "receipt id 只是查找键，不是 authority。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": _PROJECT_CONTEXT_PROPERTIES["project_id"],
                        "work_item_id": {"type": "string"},
                        "acceptance_receipt_id": {"type": "string"},
                    },
                    "required": ["work_item_id", "acceptance_receipt_id"],
                },
            ),
        ]
    )

    # === 技能追踪域 ===
    tools.extend(
        [
            Tool(
                name="skill_session_start",
                description="创建技能执行实例实体，自动激活关联原则并建立父→子链追踪。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "技能名称"},
                        "task_description": {"type": "string", "description": "本次执行的任务描述"},
                        "project_id": {
                            "type": "string",
                            "description": "持久化技能记忆所属项目；必须是稳定的 project:* 标识",
                        },
                        "parent_entity_id": {
                            "type": "string",
                            "description": "父技能会话的 entity_id",
                        },
                        "estimated_duration_minutes": {
                            "type": "integer",
                            "description": "预估耗时（分钟）",
                        },
                    },
                    "required": ["skill_name", "task_description", "project_id"],
                },
            ),
            Tool(
                name="skill_session_complete",
                description="标记技能执行完成，自动处理标签状态转换和 worth 更新，支持 still_in_progress/abandoned/normal 三种结果。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_id": {"type": "string", "description": "技能会话 entity_id"},
                        "outcome": {
                            "type": "string",
                            "description": "结果: still_in_progress / abandoned: <原因> / 留空=正常完成",
                        },
                        "artifacts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "产物路径列表",
                        },
                    },
                    "required": ["entity_id", "outcome"],
                },
            ),
            Tool(
                name="skill_session_trace",
                description="追踪技能执行链：查询、完整性检测、违反警告。支持当前/分支/全部范围。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_scope": {
                            "type": "string",
                            "description": "查询范围: current|branch|all (默认 all)",
                        },
                        "skill_name": {"type": "string", "description": "按技能名称筛选"},
                        "status": {
                            "type": "string",
                            "description": "按状态筛选: active|done|abandoned",
                        },
                    },
                },
            ),
            Tool(
                name="skill_session_audit",
                description="事后间隙扫描：检测技能记忆中提到但缺少 session 实体的技能，支持自动补录修复。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "time_range_hours": {
                            "type": "integer",
                            "description": "审计时间范围（小时）",
                        },
                        "auto_fix": {
                            "type": "boolean",
                            "description": "自动补录缺失的 session (默认 false)",
                        },
                    },
                },
            ),
            Tool(
                name="skill_auto_track",
                description="兼容外部客户端 Hook 的 Skill 生命周期追踪；仅记录实体，不推进官方工作流游标。",
                inputSchema={
                    "type": "object",
                    "required": ["phase", "skill_name"],
                    "properties": {
                        "phase": {"type": "string", "description": "'start' | 'complete'"},
                        "skill_name": {"type": "string", "description": "Skill 名称"},
                        "stage_session_id": {
                            "type": "string",
                            "description": "调用方稳定会话 ID；并发会话必须区分。",
                        },
                        "flow_line_id": {
                            "type": "string",
                            "description": "会话内并行 workflow lane ID。",
                        },
                        "project_id": _PROJECT_CONTEXT_PROPERTIES["project_id"],
                    },
                },
            ),
            Tool(
                name="memory_sync_files",
                description="同步文件系统 .md 记忆到 MCP 管道。扫描目录、解析 frontmatter、去重、标记已同步。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_dir": {"type": "string", "description": ".md 记忆文件目录路径"},
                        "dry_run": {"type": "boolean", "description": "仅扫描不写入 (默认 false)"},
                    },
                    "required": ["source_dir"],
                },
            ),
            # === Skills 域 (程序化技能 — Phase 1) ===
            Tool(
                name="session-init",
                description="会话启动 — 轻量引导：原则激活 + SCARF 基线 + 域/系统健康快照 + 信任分 + GC 预览 + chain_state。任务上下文需显式调用 context_supply。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_description": {"type": "string", "description": "当前任务描述"},
                        "task_type": {
                            "type": "string",
                            "description": "任务类型: general/code_generation/debugging/architecture",
                        },
                        "context_mode": {
                            "type": "string",
                            "enum": ["none", "light", "full"],
                            "description": "启动上下文模式：none=只提示延迟；light=1-2条轻量记忆预览；full=显式运行完整 context_supply",
                        },
                        "response_mode": {
                            "type": "string",
                            "enum": ["compact", "standard"],
                            "description": "启动响应投影；默认 compact，standard 按需展开健康与审计结构",
                        },
                        "context_timeout_s": {
                            "type": "number",
                            "description": "context_mode light/full 的超时秒数上限",
                        },
                        "scope": {
                            "type": "string",
                            "description": "context_mode=full 时传给 context_supply 的检索范围",
                        },
                        "stage_session_id": {
                            "type": "string",
                            "description": "Governed stage-chain scope id; omitted means session-init allocates one",
                        },
                        "flow_line_id": {
                            "type": "string",
                            "description": "Governed flow-line id paired with stage_session_id; omitted defaults to the selected route",
                        },
                        "route": {
                            "type": "string",
                            "enum": sorted(STAGE_ROUTE_MAP),
                            "description": "Default governed route profile for workflow_contract",
                        },
                        "agent_name": {
                            "type": "string",
                            "description": "Agent identity used when allocating a stage_session_id",
                        },
                        "hook_session_id": {
                            "type": "string",
                            "description": "Hook session identity to bind to the server-issued collaboration continuation",
                        },
                        _COLLABORATION_CONTINUATION_TOKEN_ARGUMENT: {
                            "type": "string",
                            "description": "Opaque short-lived server-issued bearer assertion; do not log, persist, inspect, or modify",
                        },
                        **_PROJECT_CONTEXT_PROPERTIES,
                    },
                    "required": ["task_description"],
                },
            ),
            Tool(
                name="smart-remember",
                description="智能记忆存储 — 自动去重检查（相似度 ≥ 0.85 则更新已有记忆），通过完整质量管道（分类+向量+门控）。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "记忆内容"},
                        "memory_type": {
                            "type": "string",
                            "description": "类型: task/experience/principle/code",
                        },
                        "source": {
                            "type": "string",
                            "description": "来源: user/system/claude_code",
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project identity for recall and storage",
                        },
                        "project_policy": {
                            "type": "string",
                            "enum": ["strict", "balanced", "open"],
                            "description": "Project isolation policy for duplicate recall",
                        },
                    },
                    "required": ["content", "memory_type"],
                },
            ),
            Tool(
                name="step-closure",
                description="每步完成后的六联闭环：原则对齐检查 → SCARF 五维自省 → 激素更新 → 信任分联动 → LLM反思生成(经验/优化/根因) → CEI 复合指数。mode=light 仅做对齐+注入，mode=full 走完整六联。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_description": {"type": "string", "description": "本步操作描述"},
                        "git_commit": {
                            "type": "string",
                            "description": "关联的 git commit hash (可选)",
                        },
                        "mode": {
                            "type": "string",
                            "description": "light (仅对齐+注入) | full (完整六联闭环+LLM反思，默认)",
                        },
                        "lesson": {
                            "type": "string",
                            "description": "经验教训 — 执行者自己反思：本次学到了什么？",
                        },
                        "improvement": {
                            "type": "string",
                            "description": "优化建议 — 下次如何做得更好？",
                        },
                        "root_cause": {
                            "type": "string",
                            "description": "根因分析 — 如果存在问题，根本原因是什么？",
                        },
                        "optimization": {
                            "type": "string",
                            "description": "优化动作 — 立即可执行的一个具体改进",
                        },
                        "trick": {"type": "string", "description": "窍门/技巧 (可选)"},
                        "target": {
                            "type": "string",
                            "default": "claude",
                            "description": "信任分追踪目标 (claude/pi_builder/pi_reviewer 等)",
                        },
                        "work_item_id": {
                            "type": "string",
                            "description": (
                                "可选的正式协作闭环 WorkItem；提供后由服务器从当前认证传输解析 active lease"
                            ),
                        },
                        "outcome": {
                            "type": "string",
                            "enum": ["completed", "blocked", "failed", "cancelled"],
                            "description": "正式协作结果状态；只有提供 work_item_id 时生效",
                        },
                        "summary": {
                            "type": "string",
                            "description": "正式协作结果的有界公开摘要，不使用原始 prompt 或私有推理",
                        },
                        "artifact_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 32,
                            "description": "正式协作结果关联的 artifact 引用",
                        },
                        "evidence_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 32,
                            "description": "正式协作结果关联的 evidence 引用",
                        },
                        "result": {
                            "type": "object",
                            "description": "正式协作结果的有界 JSON 投影；不得包含 secret 或私有推理",
                        },
                    },
                    "required": ["task_description"],
                },
            ),
            # === 审查域 ===
            Tool(
                name="review_run",
                description="执行结构化代码审查 — 三阶段管线 (prepare→evaluate→apply)。获取 git diff + 12原则检查 + 安全审查 + 信任分联动 + 发现入池 + fix任务创建。支持 action=prepare|evaluate|apply|full。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "审查阶段: prepare(获取diff+生成prompt) | evaluate(解析审查输出) | apply(信任分+记忆+fix任务) | full(完整管线)",
                            "enum": ["prepare", "evaluate", "apply", "full"],
                        },
                        "commit_range": {
                            "type": "string",
                            "description": "审查的 git commit 范围, 如 HEAD~3..HEAD",
                        },
                        "review_output": {
                            "type": "string",
                            "description": "LLM 审查输出文本 (JSON 格式, evaluate/apply/full 时需要)",
                        },
                        "author_target": {
                            "type": "string",
                            "description": "被审查的 agent trust target (默认 pi_builder)",
                        },
                        "reviewer_target": {
                            "type": "string",
                            "description": "审查者 agent trust target (默认 pi_reviewer)",
                        },
                        "spec_path": {
                            "type": "string",
                            "description": "spec 文件路径 (可选, 用于 spec 合规检查)",
                        },
                    },
                    "required": ["action"],
                },
            ),
            # === 知识域 ===
            Tool(
                name="knowledge_search",
                description="项目作用域词法知识检索，返回证据块引用与摘要。受 PP_KNOWLEDGE_SYSTEM / PP_KNOWLEDGE_RETRIEVAL 门控，默认 off 时返回降级结果。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "检索查询文本（中英文均可）",
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Canonical project id, e.g. project:plastic-promise",
                        },
                        "space_id": {
                            "type": "string",
                            "description": "可选：限定知识空间 id",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最大命中数 (1-50, 默认 10)",
                        },
                        "include_stale": {
                            "type": "boolean",
                            "description": "是否包含已过期生命周期行 (默认 false)",
                        },
                    },
                    "required": ["query", "project_id"],
                },
            ),
            # === 商业审计域 ===
            Tool(
                name="commercial_audit_export",
                description="Export a project-filterable commercial audit bundle from persisted call spans, degradation events, and optional store outbox records.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Optional canonical project id filter, e.g. project:plastic-promise",
                        },
                        "since": {
                            "type": "string",
                            "description": "Optional inclusive ISO-8601 lower time bound",
                        },
                        "until": {
                            "type": "string",
                            "description": "Optional inclusive ISO-8601 upper time bound",
                        },
                        "include_outbox": {
                            "type": "boolean",
                            "description": "Include durable memory_store outbox records in the export",
                        },
                        "export_otlp": {
                            "type": "boolean",
                            "description": "Best-effort export of matching trace rows to an OTLP/HTTP JSON endpoint",
                        },
                        "otlp_endpoint": {
                            "type": "string",
                            "description": "Optional OTLP HTTP base URL or /v1/traces endpoint",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            # === 插件市场域 (市场管理) ===
            Tool(
                name="market_list",
                description="列出市场中的插件包。支持按类型和可升级状态筛选。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "筛选类型: knowledge/workflow/capability/adapter",
                        },
                        "upgradable": {
                            "type": "boolean",
                            "description": "仅显示可升级的已安装包",
                        },
                    },
                },
            ),
            Tool(
                name="market_install",
                description="从市场安装一个插件包。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_upgrade",
                description="检查或升级插件到远程最新版本。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_remove",
                description="卸载已安装的插件包。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_enable",
                description="启用一个已禁用的插件。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_disable",
                description="禁用一个已启用的插件。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "包名"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="market_status",
                description="显示所有已安装插件的状态。",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            # === Pinned Matt Pocock engineering workflow (compatibility entry) ===
            Tool(
                name="sp-stage",
                description=(
                    "Compatibility entry for the pinned Matt Pocock engineering workflows. "
                    "Validates official route transitions and caller attestation, isolates session "
                    "and flow state, and returns stage evidence and closure guidance."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "stage": {
                            "type": "string",
                            "description": "Governed workflow stage name",
                            "enum": list(STAGE_ATOMS),
                        },
                        "task_description": {"type": "string", "description": "当前阶段任务描述"},
                        "invocation_source": {
                            "type": "string",
                            "enum": ["user", "model"],
                            "default": "model",
                            "description": (
                                "Caller-supplied workflow attestation; this value is not authenticated "
                                "by the MCP transport. User-only stages require explicit user intent "
                                "at the trusted client boundary."
                            ),
                        },
                        "stage_session_id": {
                            "type": "string",
                            "description": "Governed stage-chain scope id returned by session-init",
                        },
                        "flow_line_id": {
                            "type": "string",
                            "description": "Optional flow-line id for isolating concurrent routes within a stage_session_id",
                        },
                        "route": {
                            "type": "string",
                            "enum": sorted(STAGE_ROUTE_MAP),
                            "description": "Pinned official route used for chain validation and stage summaries",
                        },
                        "agent_name": {
                            "type": "string",
                            "description": "Agent identity for diagnostics when no stage_session_id is supplied",
                        },
                        "guidance_level": {
                            "type": "string",
                            "enum": ["summary", "full"],
                            "description": "阶段指导粒度；默认 summary，full 才展开搜索与派发模板",
                        },
                        "execution_receipt": {
                            "type": "object",
                            "description": (
                                "Bounded completion attestation submitted only after the pinned "
                                "Codex Skill has run; evidence must not contain secrets."
                            ),
                            "properties": {
                                "skill": {"type": "string"},
                                "upstream_revision": {"type": "string"},
                                "content_sha256": {"type": "string"},
                                "status": {"type": "string", "enum": ["completed"]},
                                "evidence": {"type": "object", "minProperties": 1},
                            },
                            "required": [
                                "skill",
                                "upstream_revision",
                                "content_sha256",
                                "status",
                                "evidence",
                            ],
                        },
                        **_PROJECT_CONTEXT_PROPERTIES,
                    },
                    "required": ["stage", "task_description"],
                },
            ),
        ]
    )

    _with_codex_discovery_hints(tools)

    # Compatibility aliases for clients that normalize tool names into identifiers.
    alias_targets = {
        "session_init": "session-init",
        "smart_remember": "smart-remember",
        "step_closure": "step-closure",
        "sp_stage": "sp-stage",
    }
    by_name = {tool.name: tool for tool in tools}
    for alias, target in alias_targets.items():
        original = by_name.get(target)
        if original is not None and alias not in by_name:
            tools.append(
                Tool(
                    name=alias,
                    description=f"Compatibility alias for {target}. {original.description}",
                    inputSchema=original.inputSchema,
                )
            )

    # Project/provenance schema fields are added by name to avoid widening
    # unrelated action schemas that share the same local shape.
    by_name = {tool.name: tool for tool in tools}
    for tool_name in (
        "memory_recall",
        "context_supply",
        "memory_store",
        "memory_update",
        "memory_forget",
        "memory_correct",
        "memory_reclassify",
        "memory_sync_files",
        "smart-remember",
        "smart_remember",
        "review_run",
        "knowledge_search",
    ):
        schema = by_name[tool_name].inputSchema
        schema.setdefault("properties", {}).update(_PROJECT_CONTEXT_PROPERTIES)
    by_name["memory_reclassify"].inputSchema["properties"]["memory_id"] = {
        "type": "string",
        "description": "Optional single memory id to reclassify",
    }
    for tool_name in ("memory_recall", "context_supply"):
        by_name[tool_name].inputSchema.setdefault("properties", {}).update(_RETRIEVAL_MODE_PROPERTY)
        by_name[tool_name].inputSchema["properties"].update(_FUSION_POLICY_PROPERTY)
    by_name["memory_store"].inputSchema["properties"].update(_PROVENANCE_PROPERTIES)
    by_name["review_run"].inputSchema["properties"]["allow_project_unknown"] = {
        "type": "boolean",
        "description": "Allow prepare/full without project_id and accept degraded review guard behavior",
    }

    return tools


# ---------------------------------------------------------------------------
# 闭环仪表盘摘要格式化
# ---------------------------------------------------------------------------


def _format_closure_dashboard(result: dict, history: deque) -> str:
    """Build a human-readable step-closure dashboard from post_task result.

    Features:
    - Trend arrows (↗↘→) comparing current vs previous closure
    - Sigma marker (!) for values beyond ±2σ of sliding window
    - First-closure graceful degradation
    - Reflection fields: lesson, improvement, root_cause, optimization
    """
    scarf = result.get("scarf", {})
    scarf_overall = scarf.get("summary", {}).get("overall_score", 0)
    trust_data = result.get("trust", {})
    trust_score = trust_data.get("score", 0)
    cei = result.get("cei", {})
    cei_score = cei.get("score", 0)
    cei_tier = cei.get("tier", "?")
    reflection = result.get("reflection", {})
    lesson = reflection.get("lesson", "")
    improvement = reflection.get("improvement", "")
    root_cause = reflection.get("root_cause", "")
    optimization = reflection.get("optimization", "")
    source = reflection.get("source", "")

    step_n = len(history) + 1  # history hasn't been updated yet
    is_first = len(history) == 0

    def bar(v):
        filled = int(max(0, min(v, 1)) * 10)
        return "█" * filled + "░" * (10 - filled)

    def trend(current, key):
        """Compare current value against previous closure history."""
        if is_first:
            return "-- baseline"
        prev = history[-1].get(key, 0)
        delta = current - prev
        arrow = "↗" if delta > 0.01 else "↘" if delta < -0.01 else "→"
        tag = f"{arrow} {delta:+.3f}"
        # Sigma check: is current beyond ±2σ of window?
        if len(history) >= 3:
            vals = [h.get(key, 0) for h in history]
            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            std = variance**0.5
            if std > 0 and abs(current - mean) > 2 * std:
                tag += " !!!"
        return tag

    # Extract actual trust/hormone deltas from this closure (not trend vs history)
    hormone = result.get("hormone", {})
    trust_delta = hormone.get("trust_delta", 0)
    scarf_trend = trend(scarf_overall, "scarf")
    trust_trend = trend(trust_score, "trust")
    cei_trend = trend(cei_score, "cei")

    source_tag = " [LLM]" if source == "llm" else " [执行者]" if source == "executor" else ""

    lines = []
    lines.append("")
    lines.append(f"╔══ Step #{step_n} {'(baseline)' if is_first else ''} ═══════════════════╗")
    lines.append(f"║  SCARF {scarf_overall:.2f}  {bar(scarf_overall)}  ({scarf_trend})")
    lines.append(
        f"║  Trust {trust_score:.3f}  {bar(trust_score)}  (adjust: {trust_delta:+.3f}; trend: {trust_trend})"
    )
    lines.append(f"║  CEI   {cei_score:.2f}  {bar(cei_score)}  ({cei_tier} · {cei_trend})")
    lines.append("║  ──────────────────────────────────────────────")

    # Show SCARF dimension bars if available
    dims_shown = 0
    for dim_name in ["Status", "Certainty", "Autonomy", "Relatedness", "Fairness"]:
        if dim_name in scarf and isinstance(scarf[dim_name], dict):
            s = scarf[dim_name].get("score", 0)
            lines.append(f"║  {dim_name[:4]:4s} {s:.2f} {bar(s)}")
            dims_shown += 1

    lines.append("║  ──────────────────────────────────────────────")

    # Show reflection fields (LLM or template generated)
    if lesson:
        label = "[经验]" if source == "llm" else "[教训]"
        lines.append(f"║  {label}: {lesson[:80]}{'…' if len(lesson) > 80 else ''}{source_tag}")
        source_tag = ""  # only show tag once
    if improvement:
        lines.append(f"║  [优化]: {improvement[:80]}{'…' if len(improvement) > 80 else ''}")
    if root_cause:
        lines.append(f"║  [根因]: {root_cause[:80]}{'…' if len(root_cause) > 80 else ''}")
    if optimization:
        lines.append(f"║  [动作]: {optimization[:80]}{'…' if len(optimization) > 80 else ''}")

    # Show repair suggestions if any
    repairs = result.get("repairs", [])
    if repairs:
        lines.append("║  ──────────────────────────────────────────────")
        for r in repairs[:3]:
            dim = r.get("dimension", "?")
            sug = r.get("suggestion", "")
            lines.append(f"║  !!! {dim}: {sug[:70]}{'…' if len(sug) > 70 else ''}")

    lines.append(f"╚{'═' * 52}╝")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具调用路由
# ---------------------------------------------------------------------------


def _feedback_runtime_actor() -> str:
    """Resolve reviewer identity from server-owned process configuration."""
    configured = str(os.environ.get("PP_MCP_RUNTIME_ACTOR") or "").strip()
    normalized = configured.casefold().replace("-", "_").replace(" ", "_")
    compact = normalized.replace("_", "")
    for actor in ("pi_reviewer", "pi_builder", "pi_fixer", "codex", "claude"):
        if actor.replace("_", "") in compact:
            return actor
    return normalized or "mcp"


def _current_mcp_session() -> Any | None:
    """Return the SDK-owned session for the current request, if one exists."""

    try:
        return server.request_context.session
    except (AttributeError, LookupError):
        return None


def _authenticated_transport_instance() -> str:
    """Return an opaque id for the current SDK-owned MCP transport session.

    This is deliberately a private composition detail rather than an MCP
    argument.  A workflow scope says which collaboration lane an invocation
    belongs to; it does *not* prove that two invocations originate from the
    same live transport.  Keeping the minted instance id against the SDK
    session object prevents same-actor peer connections from sharing a
    durable AgentSession, cursor, heartbeat, or SessionEnd lifecycle.
    """

    session = _current_mcp_session()
    if session is None:
        return ""
    with _mcp_transport_instances_guard:
        token = _mcp_transport_instances.get(session)
        if isinstance(token, str) and token:
            return token
        token = f"transport:mcp:{secrets.token_hex(16)}"
        _mcp_transport_instances[session] = token
        return token


def _configured_task_runtime_actor() -> str:
    """Return an explicitly configured process actor; the default ``mcp`` is not identity."""

    if not str(os.environ.get("PP_MCP_RUNTIME_ACTOR") or "").strip():
        return ""
    actor = _feedback_runtime_actor()
    return "" if actor == "mcp" else actor


def _task_session_authority() -> dict[str, Any] | None:
    session = _current_mcp_session()
    if session is None:
        return None
    with _task_session_authorities_guard:
        binding = _task_session_authorities.get(session)
        return dict(binding) if isinstance(binding, dict) else None


def _current_durable_collaboration_binding() -> Any | None:
    """Return the exact durable binding owned by the current MCP transport."""

    session = _current_mcp_session()
    if session is None:
        return None
    with _durable_collaboration_bindings_guard:
        return _durable_collaboration_bindings.get(session)


def _durable_collaboration_continuation_authority() -> Any:
    """Return the server continuation authority backed by a private key ring."""

    global _durable_collaboration_continuation_authority_instance
    with _durable_collaboration_continuation_authority_guard:
        authority = _durable_collaboration_continuation_authority_instance
        if authority is None:
            from plastic_promise.collaboration.runtime_binding import (
                DurableCollaborationContinuationAuthority,
            )
            from plastic_promise.core.paths import get_db_path

            configured_path = str(
                os.environ.get("PP_COLLABORATION_CONTINUATION_KEYRING_FILE") or ""
            ).strip()
            if configured_path:
                keyring_path = Path(configured_path).expanduser()
            else:
                raw_database_path = str(get_db_path())
                keyring_path = (
                    None
                    if raw_database_path == ":memory:"
                    else Path(raw_database_path).expanduser().parent
                    / ".collaboration-continuation-keyring.json"
                )
            authority = (
                DurableCollaborationContinuationAuthority.from_key_file(keyring_path)
                if keyring_path is not None
                else DurableCollaborationContinuationAuthority()
            )
            _durable_collaboration_continuation_authority_instance = authority
        return authority


def _without_collaboration_continuation_token(
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Copy public arguments while removing the bearer secret before tracing."""

    values = dict(arguments or {})
    values.pop(_COLLABORATION_CONTINUATION_TOKEN_ARGUMENT, None)
    return values


def _bind_task_session_authority_from_continuation(claims: Any) -> tuple[bool, str]:
    """Install only the server-verified scope carried by a valid assertion."""

    session = _current_mcp_session()
    if session is None:
        return False, "durable_collaboration_mcp_session_unavailable"
    actor = _configured_task_runtime_actor()
    if not actor or str(getattr(claims, "server_actor", "")) != actor:
        return False, "durable_collaboration_continuation_actor_conflict"
    binding = {
        "project_id": str(getattr(claims, "project_id", "")),
        "actor": actor,
        "stage_session_id": str(getattr(claims, "stage_session_id", "")),
        "flow_line_id": str(getattr(claims, "flow_line_id", "")),
        "flow_scope_id": str(getattr(claims, "flow_scope_id", "")),
        "route_id": str(getattr(claims, "flow_line_id", "")),
        "continuation_authenticated": True,
    }
    if not binding["project_id"] or not binding["flow_scope_id"]:
        return False, "durable_collaboration_continuation_binding_invalid"
    with _task_session_authorities_guard:
        existing = _task_session_authorities.get(session)
        if isinstance(existing, dict) and (
            existing.get("project_id") != binding["project_id"]
            or existing.get("flow_scope_id") != binding["flow_scope_id"]
            or existing.get("actor") != binding["actor"]
        ):
            return False, "durable_collaboration_transport_binding_conflict"
        _task_session_authorities[session] = binding
    return True, ""


def _resume_durable_collaboration_continuation(
    *,
    engine: Any,
    token: object,
    project_id: object,
    flow_scope_id: object,
    hook_session_id: object,
    stage_session_id: object = "",
    flow_line_id: object = "",
) -> tuple[Any | None, str]:
    actor = _configured_task_runtime_actor()
    if not actor:
        return None, "durable_collaboration_server_actor_unconfigured"
    outcome = _durable_collaboration_continuation_authority().resume(
        token,
        project_id=project_id,
        flow_scope_id=flow_scope_id,
        server_actor=actor,
        hook_session_id=hook_session_id,
        stage_session_id=stage_session_id,
        flow_line_id=flow_line_id,
    )
    if not outcome.valid or outcome.claims is None:
        return None, outcome.reason or "durable_collaboration_continuation_invalid"
    binding = outcome.binding
    if binding is None:
        from plastic_promise.collaboration.runtime_binding import (
            resume_mcp_durable_collaboration_runtime,
        )

        binding = resume_mcp_durable_collaboration_runtime(engine, claims=outcome.claims)
        if not binding.durable:
            return None, binding.reason or "durable_collaboration_continuation_session_inactive"
    host = getattr(binding, "host", None)
    try:
        if host is None or not host.continuation_is_active():
            return None, "durable_collaboration_continuation_session_inactive"
    except Exception:
        return None, "durable_collaboration_continuation_session_inactive"
    bound, reason = _bind_task_session_authority_from_continuation(outcome.claims)
    if not bound:
        return None, reason
    return binding, ""


def _issue_durable_collaboration_continuation(
    *,
    hook_session_id: object,
) -> tuple[str, int, str]:
    binding = _task_session_authority()
    exact = _current_durable_collaboration_binding()
    if not isinstance(binding, dict) or exact is None:
        return "", 0, "durable_collaboration_authenticated_binding_required"
    outcome = _durable_collaboration_continuation_authority().issue(
        exact,
        project_id=binding.get("project_id"),
        flow_scope_id=binding.get("flow_scope_id"),
        server_actor=binding.get("actor"),
        hook_session_id=hook_session_id,
        stage_session_id=binding.get("stage_session_id"),
        flow_line_id=binding.get("flow_line_id"),
    )
    if not outcome.valid or not outcome.token:
        return "", 0, outcome.reason or "durable_collaboration_continuation_unavailable"
    return outcome.token, outcome.expires_at_epoch, ""


def _revoke_durable_collaboration_binding(binding: Any) -> None:
    """Revoke assertions and detach every transport sharing one durable session."""

    _durable_collaboration_continuation_authority().revoke_binding(binding)
    target_session = getattr(binding, "session", None)
    target_session_id = str(getattr(target_session, "session_id", ""))
    detached: list[Any] = []
    with _durable_collaboration_bindings_guard:
        for transport, candidate in list(_durable_collaboration_bindings.items()):
            candidate_session = getattr(candidate, "session", None)
            if candidate is binding or (
                target_session_id
                and str(getattr(candidate_session, "session_id", "")) == target_session_id
            ):
                detached.append(transport)
                _durable_collaboration_bindings.pop(transport, None)
    if not detached:
        return
    with _task_session_authorities_guard:
        for transport in detached:
            _task_session_authorities.pop(transport, None)
    with _mcp_transport_instances_guard:
        for transport in detached:
            _mcp_transport_instances.pop(transport, None)


def _bind_task_session_authority(
    project_id: object,
    *,
    workflow: Mapping[str, Any] | None = None,
    stage_session_id: object = "",
) -> tuple[bool, str]:
    """Bind one canonical project and server-owned actor to the current MCP session.

    This is a trusted local-client/session boundary, not internet-grade user
    authentication.  A session may not silently switch project or workflow
    scope after binding.  Both scope values come from the server-resolved
    ``session-init`` result, never directly from arbitrary tool JSON.
    """

    scoped_project_id = canonical_project_id(project_id)
    if not scoped_project_id:
        return False, "task_session_project_required"
    actor = _configured_task_runtime_actor()
    if not actor:
        return False, "task_runtime_actor_unconfigured"
    session = _current_mcp_session()
    if session is None:
        return False, "task_mcp_session_unavailable"

    workflow_data = workflow if isinstance(workflow, Mapping) else {}
    resolved_stage_session_id = str(
        workflow_data.get("stage_session_id") or stage_session_id or "default"
    ).strip()
    resolved_flow_line_id = str(
        workflow_data.get("flow_line_id") or workflow_data.get("route_id") or "idea-to-ship"
    ).strip()
    resolved_flow_scope_id = str(workflow_data.get("flow_scope_id") or "").strip()
    if not resolved_flow_scope_id:
        from plastic_promise.core.workflow_state import compose_flow_scope

        resolved_flow_scope_id = compose_flow_scope(
            resolved_stage_session_id,
            resolved_flow_line_id,
            scoped_project_id,
        )
    resolved_route_id = str(workflow_data.get("route_id") or resolved_flow_line_id).strip()
    if not resolved_stage_session_id or not resolved_flow_line_id or not resolved_flow_scope_id:
        return False, "task_session_workflow_scope_required"

    with _task_session_authorities_guard:
        existing = _task_session_authorities.get(session)
        if isinstance(existing, dict) and existing.get("project_id") != scoped_project_id:
            return False, "task_session_project_conflict"
        if isinstance(existing, dict):
            existing_scope = str(existing.get("flow_scope_id") or "").strip()
            if existing_scope and existing_scope != resolved_flow_scope_id:
                return False, "task_session_workflow_scope_conflict"
        _task_session_authorities[session] = {
            "project_id": scoped_project_id,
            "actor": actor,
            "stage_session_id": resolved_stage_session_id,
            "flow_line_id": resolved_flow_line_id,
            "flow_scope_id": resolved_flow_scope_id,
            "route_id": resolved_route_id,
        }
    return True, ""


def _task_session_project_conflict(project_id: object) -> bool:
    """Return whether session-init would try to rebind an existing Task scope."""

    scoped_project_id = canonical_project_id(project_id)
    binding = _task_session_authority()
    return bool(scoped_project_id and binding and binding.get("project_id") != scoped_project_id)


def _bind_durable_collaboration_runtime_for_project(
    engine: Any,
    project_id: object,
    *,
    continuation_token: object = "",
    hook_session_id: object = "",
) -> tuple[bool, str]:
    """Bind the current authenticated MCP transport to one durable PR5 session.

    The only public input is a project used as a consistency check.  Project,
    workflow scope, actor, and opaque transport instance all come from
    server-owned state.  In particular, neither a tool payload nor an actor
    name can choose an existing durable session.

    A missing PR5 schema is an optional-plane deferred state: the ordinary
    memory/context plane remains available, but the response never pretends
    that collaboration persistence happened.
    """

    binding = _task_session_authority()
    if not isinstance(binding, dict):
        return False, "durable_collaboration_task_session_authority_required"
    bound_project_id = canonical_project_id(binding.get("project_id"))
    requested_project_id = canonical_project_id(project_id)
    if not bound_project_id:
        return False, "durable_collaboration_task_session_authority_required"
    if requested_project_id and requested_project_id != bound_project_id:
        return False, "durable_collaboration_project_conflict"
    coordination_scope = str(binding.get("flow_scope_id") or "").strip()
    if not coordination_scope:
        return False, "durable_collaboration_workflow_scope_required"
    actor = _configured_task_runtime_actor()
    if not actor:
        return False, "durable_collaboration_server_actor_unconfigured"
    session = _current_mcp_session()
    transport_session_id = _authenticated_transport_instance()
    if session is None or not transport_session_id:
        return False, "durable_collaboration_mcp_session_unavailable"

    existing = _current_durable_collaboration_binding()
    if existing is not None:
        current_session = getattr(existing, "session", None)
        if current_session is None:
            return False, "durable_collaboration_binding_invalid"
        if (
            current_session.project.project_id != bound_project_id
            or current_session.coordination_session_id != coordination_scope
            or current_session.identity.agent_id != f"agent:{actor}"
        ):
            return False, "durable_collaboration_transport_binding_conflict"
        # The cached binding holds the session snapshot from registration
        # time; its state never tracks later canonical changes. Probe
        # canonical liveness with a heartbeat instead: success refreshes the
        # presence window and the cached binding stays authoritative; a stale
        # row falls through to re-registration below, which revives the exact
        # deterministic session row via its verified stored identity.
        runtime = getattr(getattr(existing, "host", None), "runtime", None)
        session_id = str(current_session.session_id or "")
        if runtime is not None and session_id:
            try:
                runtime.heartbeat(session_id)
                return True, ""
            except Exception:
                pass

    if str(continuation_token or "").strip():
        resumed, reason = _resume_durable_collaboration_continuation(
            engine=engine,
            token=continuation_token,
            project_id=bound_project_id,
            flow_scope_id=coordination_scope,
            hook_session_id=hook_session_id,
        )
        if resumed is None:
            return False, reason
        with _durable_collaboration_bindings_guard:
            _durable_collaboration_bindings[session] = resumed
        return True, ""

    try:
        from plastic_promise.collaboration.runtime_binding import (
            open_mcp_durable_collaboration_runtime,
        )

        result = open_mcp_durable_collaboration_runtime(
            engine,
            project_id=bound_project_id,
            server_actor=actor,
            coordination_session_id=coordination_scope,
            transport_session_id=transport_session_id,
        )
        if not result.durable:
            return False, result.reason or "durable_collaboration_runtime_unavailable"
        with _durable_collaboration_bindings_guard:
            _durable_collaboration_bindings[session] = result
        return True, ""
    except Exception:
        return False, "durable_collaboration_runtime_unavailable"


def _durable_collaboration_lifecycle(
    engine: Any,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Advance an exact transport binding or a verified server continuation."""

    values = dict(arguments or {})
    event = str(values.get("event") or "before_invoke").strip().casefold()
    if event not in {"before_invoke", "after_invoke", "session_end"}:
        return {"state": "skipped", "reason": "durable_lifecycle_event_unsupported"}
    binding = _task_session_authority()
    exact = _current_durable_collaboration_binding()
    continuation_token = values.get(_COLLABORATION_CONTINUATION_TOKEN_ARGUMENT)
    if str(continuation_token or "").strip():
        expected_project = (
            binding.get("project_id") if isinstance(binding, dict) else values.get("project_id")
        )
        expected_scope = (
            str(binding.get("flow_scope_id") or "").strip() if isinstance(binding, dict) else ""
        )
        resumed, reason = _resume_durable_collaboration_continuation(
            engine=engine,
            token=continuation_token,
            project_id=expected_project,
            flow_scope_id=expected_scope,
            hook_session_id=values.get("hook_session_id"),
            stage_session_id=values.get("stage_session_id") or values.get("stage_id"),
            flow_line_id=values.get("flow_line_id") or values.get("flow_id"),
        )
        if resumed is None:
            return {"state": "deferred", "reason": reason}
        resumed_session = getattr(resumed, "session", None)
        exact_session = getattr(exact, "session", None)
        if exact is not None and (
            exact_session is None
            or resumed_session is None
            or exact_session.session_id != resumed_session.session_id
        ):
            return {
                "state": "deferred",
                "reason": "durable_collaboration_transport_binding_conflict",
            }
        current = _current_mcp_session()
        if current is None:
            return {
                "state": "deferred",
                "reason": "durable_collaboration_mcp_session_unavailable",
            }
        with _durable_collaboration_bindings_guard:
            _durable_collaboration_bindings[current] = resumed
        exact = resumed
        binding = _task_session_authority()
    if not isinstance(binding, dict):
        return {
            "state": "deferred",
            "reason": "durable_collaboration_task_session_authority_required",
        }
    requested_project = canonical_project_id(values.get("project_id"))
    bound_project = canonical_project_id(binding.get("project_id"))
    if requested_project and requested_project != bound_project:
        return {"state": "deferred", "reason": "durable_collaboration_project_conflict"}
    if exact is None:
        return {
            "state": "deferred",
            "reason": "durable_collaboration_authenticated_binding_required",
        }
    runtime = getattr(exact, "runtime", None)
    session = getattr(exact, "session", None)
    host = getattr(exact, "host", None)
    if runtime is None or session is None or host is None:
        return {"state": "deferred", "reason": "durable_collaboration_binding_invalid"}
    if (
        session.project.project_id != bound_project
        or session.coordination_session_id != str(binding.get("flow_scope_id") or "").strip()
    ):
        return {
            "state": "deferred",
            "reason": "durable_collaboration_transport_binding_conflict",
        }
    try:
        if event == "session_end":
            receipt = host.end_session(reason="mcp_session_end")
            action = "session_end"
        else:
            raw_ack = values.get("collaboration_cursor_ack")
            receipt = host.heartbeat(cursor_ack=raw_ack)
            action = "heartbeat"
            if event == "after_invoke":
                stop_activity = host.publish_stop_activity(
                    idempotency_key=values.get("request_id") or values.get("call_id") or "stop"
                )
                receipt = {**receipt, "stop_activity": stop_activity}
        response = {
            "state": "durable",
            "action": action,
            "persistent": True,
            "receipt": dict(receipt) if isinstance(receipt, dict) else {},
        }
        if event == "session_end":
            _revoke_durable_collaboration_binding(exact)
        return response
    except Exception as exc:
        # Only collaboration contract codes are safe public diagnostics.
        # Arbitrary exception text may contain paths, SQL, or provider data.
        from plastic_promise.collaboration.contracts import CollaborationContractError

        reason = str(exc).strip() if isinstance(exc, CollaborationContractError) else ""
        return {
            "state": "deferred",
            "reason": reason or "durable_collaboration_lifecycle_unavailable",
        }


def _publish_sp_stage_collaboration_events(
    *,
    flow_scope_id: str,
    execution_receipt_id: str,
    route_id: str,
    stage: str,
    step_index: int,
) -> dict[str, Any]:
    """Publish bounded workflow events through the exact MCP transport binding.

    ``sp-stage`` owns the official workflow receipt; the durable collaboration
    runtime owns Agent identity, project/session scope, server time, and event
    persistence.  This adapter only joins those two server-owned facts.  It
    never recreates a binding from caller JSON and never copies receipt
    evidence, prompts, or task descriptions into collaboration events.
    """

    def deferred(reason: str) -> dict[str, Any]:
        return {
            "schema_version": "sp-stage-collaboration-handoff/v1",
            "state": "deferred",
            "persistent": False,
            "reason": reason,
            "canonical_memory_effect": "none",
        }

    receipt_id = str(execution_receipt_id or "").strip()
    if not receipt_id:
        return deferred("workflow_execution_receipt_id_required")
    binding = _task_session_authority()
    if not isinstance(binding, dict):
        return deferred("durable_collaboration_task_session_authority_required")
    bound_scope = str(binding.get("flow_scope_id") or "").strip()
    if not bound_scope or bound_scope != str(flow_scope_id or "").strip():
        return deferred("durable_collaboration_workflow_scope_conflict")
    exact = _current_durable_collaboration_binding()
    if exact is None:
        return deferred("durable_collaboration_authenticated_binding_required")
    runtime = getattr(exact, "runtime", None)
    session = getattr(exact, "session", None)
    if runtime is None or session is None:
        return deferred("durable_collaboration_binding_invalid")
    bound_project = canonical_project_id(binding.get("project_id"))
    bound_actor = str(binding.get("actor") or "").strip()
    if (
        not bound_project
        or not bound_actor
        or session.project.project_id != bound_project
        or session.coordination_session_id != bound_scope
        or session.identity.agent_id != f"agent:{bound_actor}"
    ):
        return deferred("durable_collaboration_transport_binding_conflict")
    try:
        outcome = runtime.publish_workflow_receipt_events(
            agent_session_id=session.session_id,
            execution_receipt_id=receipt_id,
            route_id=route_id,
            stage=stage,
            step_index=step_index,
        )
    except Exception as exc:
        from plastic_promise.collaboration.contracts import CollaborationContractError

        reason = str(exc).strip() if isinstance(exc, CollaborationContractError) else ""
        return deferred(reason or "durable_collaboration_event_handoff_unavailable")
    if not isinstance(outcome, Mapping) or outcome.get("state") != "durable":
        return deferred("durable_collaboration_event_handoff_invalid")
    return {
        "schema_version": "sp-stage-collaboration-handoff/v1",
        "state": "durable",
        "persistent": True,
        "execution_receipt_id": receipt_id,
        "receipt_sha256": str(outcome.get("receipt_sha256") or ""),
        "event_ids": list(outcome.get("event_ids") or ()),
        "event_types": list(outcome.get("event_types") or ()),
        "replayed": bool(outcome.get("replayed")),
        "canonical_memory_effect": "none",
    }


def _durable_collaboration_work_operation(
    operation: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Route a bounded work-board operation through the exact MCP binding."""

    values = dict(arguments or {})
    exact = _current_durable_collaboration_binding()
    if exact is None:
        return {
            "schema_version": "collaboration-work-operation/v1",
            "state": "deferred",
            "persistent": False,
            "operation": operation,
            "reason": "durable_collaboration_authenticated_binding_required",
            "canonical_memory_effect": "none",
        }
    binding = _task_session_authority()
    host = getattr(exact, "host", None)
    session = getattr(exact, "session", None)
    if host is None or session is None or not isinstance(binding, dict):
        return {
            "schema_version": "collaboration-work-operation/v1",
            "state": "deferred",
            "persistent": False,
            "operation": operation,
            "reason": "durable_collaboration_binding_invalid",
            "canonical_memory_effect": "none",
        }
    bound_project = canonical_project_id(binding.get("project_id"))
    requested_project = canonical_project_id(values.get("project_id"))
    bound_scope = str(binding.get("flow_scope_id") or "").strip()
    bound_actor = str(binding.get("actor") or "").strip()
    if requested_project and requested_project != bound_project:
        reason = "durable_collaboration_project_conflict"
    elif (
        not bound_project
        or not bound_scope
        or not bound_actor
        or session.project.project_id != bound_project
        or session.coordination_session_id != bound_scope
        or session.identity.agent_id != f"agent:{bound_actor}"
    ):
        reason = "durable_collaboration_transport_binding_conflict"
    else:
        reason = ""
    if reason:
        return {
            "schema_version": "collaboration-work-operation/v1",
            "state": "deferred",
            "persistent": False,
            "operation": operation,
            "reason": reason,
            "canonical_memory_effect": "none",
        }
    try:
        if operation == "list":
            return dict(host.work_list(limit=values.get("limit", 32)))
        if operation == "register":
            return dict(host.work_register(arguments=values))
        if operation == "claim":
            return dict(host.work_claim(work_item_id=values.get("work_item_id")))
        if operation == "lease_heartbeat":
            return dict(host.lease_heartbeat(work_item_id=values.get("work_item_id")))
        if operation == "review":
            return dict(host.work_review(work_item_id=values.get("work_item_id")))
        if operation == "accept":
            return dict(
                host.work_accept(
                    work_item_id=values.get("work_item_id"),
                    acceptance_receipt_id=values.get("acceptance_receipt_id"),
                    acceptance_repository=getattr(exact, "acceptance_repository", None),
                )
            )
        reason = "collaboration_work_operation_unsupported"
    except Exception as exc:
        from plastic_promise.collaboration.contracts import CollaborationContractError

        reason = str(exc).strip() if isinstance(exc, CollaborationContractError) else ""
        reason = reason or "durable_collaboration_work_operation_unavailable"
    return {
        "schema_version": "collaboration-work-operation/v1",
        "state": "deferred",
        "persistent": False,
        "operation": operation,
        "reason": reason,
        "canonical_memory_effect": "none",
    }


def _publish_sp_stage_lifecycle_event(
    *,
    flow_scope_id: str,
    execution_receipt_id: str,
    route_id: str,
    stage: str,
    step_index: int,
    lifecycle: str,
    reason_code: str = "",
) -> dict[str, Any]:
    """Project a bounded started/blocked event through exact MCP binding.

    The official workflow remains authoritative for execution success.  A
    deferred collaboration projection is reported as such and never turns a
    successful or failed SkillEngine call into a different workflow outcome.
    """

    def deferred(reason: str) -> dict[str, Any]:
        return {
            "schema_version": "sp-stage-lifecycle-handoff/v1",
            "state": "deferred",
            "persistent": False,
            "lifecycle": lifecycle,
            "reason": reason,
            "canonical_memory_effect": "none",
        }

    lifecycle = str(lifecycle or "").strip().casefold()
    if lifecycle not in {"started", "blocked"}:
        return deferred("workflow_stage_lifecycle_invalid")
    receipt_id = str(execution_receipt_id or "").strip()
    if not receipt_id:
        return deferred("workflow_execution_receipt_id_required")
    binding = _task_session_authority()
    if not isinstance(binding, dict):
        return deferred("durable_collaboration_task_session_authority_required")
    bound_scope = str(binding.get("flow_scope_id") or "").strip()
    if not bound_scope or bound_scope != str(flow_scope_id or "").strip():
        return deferred("durable_collaboration_workflow_scope_conflict")
    exact = _current_durable_collaboration_binding()
    if exact is None:
        return deferred("durable_collaboration_authenticated_binding_required")
    runtime = getattr(exact, "runtime", None)
    session = getattr(exact, "session", None)
    if runtime is None or session is None:
        return deferred("durable_collaboration_binding_invalid")
    bound_project = canonical_project_id(binding.get("project_id"))
    bound_actor = str(binding.get("actor") or "").strip()
    if (
        not bound_project
        or not bound_actor
        or session.project.project_id != bound_project
        or session.coordination_session_id != bound_scope
        or session.identity.agent_id != f"agent:{bound_actor}"
    ):
        return deferred("durable_collaboration_transport_binding_conflict")
    try:
        outcome = runtime.publish_workflow_stage_lifecycle_event(
            agent_session_id=session.session_id,
            execution_receipt_id=receipt_id,
            route_id=route_id,
            stage=stage,
            step_index=step_index,
            lifecycle=lifecycle,
            reason_code=reason_code,
        )
    except Exception as exc:
        from plastic_promise.collaboration.contracts import CollaborationContractError

        reason = str(exc).strip() if isinstance(exc, CollaborationContractError) else ""
        return deferred(reason or "durable_collaboration_lifecycle_unavailable")
    if not isinstance(outcome, Mapping) or outcome.get("state") != "durable":
        return deferred("durable_collaboration_lifecycle_invalid")
    return {
        "schema_version": "sp-stage-lifecycle-handoff/v1",
        "state": "durable",
        "persistent": True,
        "lifecycle": lifecycle,
        "workflow_attempt_id": str(outcome.get("workflow_attempt_id") or ""),
        "event_id": str(outcome.get("event_id") or ""),
        "event_type": str(outcome.get("event_type") or ""),
        "reason_code": str(outcome.get("reason_code") or ""),
        "causal_parent_event_id": str(outcome.get("causal_parent_event_id") or ""),
        "cursor": outcome.get("cursor"),
        "replayed": bool(outcome.get("replayed")),
        "canonical_memory_effect": "none",
    }


def _task_runtime_context(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build Task authority from process actor + MCP-session project binding."""

    context = _mutation_runtime_context(tool_name, arguments)
    context["tool_name"] = tool_name
    binding = _task_session_authority()
    configured_actor = _configured_task_runtime_actor()
    if not binding or not configured_actor or binding.get("actor") != configured_actor:
        context.update(
            {
                "project_id": str((binding or {}).get("project_id") or ""),
                "actor": configured_actor or "mcp",
                "defense_decision": "deny",
                "authority_source": "server_runtime_unbound",
                "authorization_reason": "task_runtime_authorization_required",
            }
        )
        return context

    context.update(
        {
            "project_id": str(binding["project_id"]),
            "actor": configured_actor,
            "authority_source": "server_runtime_session",
        }
    )
    return context


def _memory_sync_allowed_roots(environ: dict[str, str] | None = None) -> list[str]:
    """Return canonical server-owned roots permitted for file-memory imports."""
    env = environ if environ is not None else os.environ
    roots = [
        os.path.join(_PROJECT_ROOT, "var", "memory_files"),
        os.path.join(os.path.expanduser("~"), ".claude", "projects"),
    ]
    configured = str(env.get("PP_MEMORY_SYNC_ALLOWED_ROOTS") or "")
    roots.extend(path.strip() for path in configured.split(os.pathsep) if path.strip())
    return list(dict.fromkeys(canonical_source_root(path) for path in roots))


def _mutation_runtime_context(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build server-owned authority for a public mutation handler."""
    from plastic_promise.core.project_context import infer_project_context
    from plastic_promise.core.tool_manifest import (
        evaluate_tool_decision,
        manifest_for_tool,
    )
    from plastic_promise.core.traceability import new_call_id
    from plastic_promise.mcp.tools.audit_defense import _get_trust_manager

    actor = _feedback_runtime_actor()
    # Project authority is process-owned for mutation tools. Caller project
    # declarations remain audit metadata and cannot expand the writable scope.
    project_context = infer_project_context({})
    call_id = new_call_id()
    try:
        tm = _get_trust_manager()
        trust_score = float(tm.get(actor))
        trust_tier = str(tm.tier(actor))
        decision = evaluate_tool_decision(
            manifest_for_tool(tool_name),
            trust_score,
            trust_tier=trust_tier,
        )["decision"]
    except Exception:
        trust_score = 0.0
        trust_tier = ""
        decision = "deny"
    context = {
        "actor": actor,
        "call_id": call_id,
        "project_id": project_context.project_id,
        "project_policy": project_context.project_policy,
        "trust_score": trust_score,
        "trust_tier": trust_tier,
        "defense_decision": decision,
    }
    if tool_name == "memory_sync_files":
        context["allowed_source_roots"] = _memory_sync_allowed_roots()
    return context


def _feedback_runtime_context() -> dict[str, Any]:
    """Compatibility wrapper for existing feedback-focused integrations."""
    return _mutation_runtime_context("feedback_apply")


_NOTIFICATION_RUNTIME_TOOL_BY_EVENT = {
    "audit_report": "audit_rollover",
    "llm_classified": "memory_update",
}


def _smart_remember_runtime_caller(
    runtime_context: dict[str, Any] | None,
) -> str:
    """Map a server-owned runtime actor onto SkillEngine's coarse role taxonomy."""
    if not isinstance(runtime_context, dict):
        return ""
    actor = str(runtime_context.get("actor") or "").strip().casefold()
    if actor == "pi" or actor.startswith("pi_"):
        return "pi"
    if actor in {"claude", "codex", "mcp"}:
        return "claude"
    return actor


def _notification_runtime_authority(
    runtime_authority: dict[str, Any] | None,
    *,
    tool_name: str,
    reason_prefix: str,
) -> tuple[tuple[str, str, str] | None, str]:
    """Validate server-owned notification authority against one tool manifest."""
    from plastic_promise.core.tool_manifest import manifest_for_tool

    if not isinstance(runtime_authority, dict):
        return None, f"{reason_prefix}_runtime_authorization_required"
    actor = str(runtime_authority.get("actor") or "").strip()
    call_id = str(runtime_authority.get("call_id") or "").strip()
    project_id = str(runtime_authority.get("project_id") or "").strip()
    if not actor or not call_id or project_id in {"", "project:unknown"}:
        return None, f"{reason_prefix}_runtime_authorization_required"
    try:
        trust_score = float(runtime_authority.get("trust_score"))
    except (TypeError, ValueError):
        return None, f"{reason_prefix}_runtime_authorization_denied"
    if (
        runtime_authority.get("defense_decision") != "allow"
        or not math.isfinite(trust_score)
        or not 0.0 <= trust_score <= 1.0
        or trust_score < manifest_for_tool(tool_name).trust_requirement
    ):
        return None, f"{reason_prefix}_runtime_authorization_denied"
    return (actor, call_id, project_id), ""


def _persist_audit_report_notification(
    engine: Any,
    event: dict[str, Any],
    runtime_authority: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist one authorized audit report with explicit partial evidence."""
    from plastic_promise.core.memory_index import (
        effective_embedding_model_name,
        metadata_with_index_material,
        prepare_index_material,
    )
    from plastic_promise.core.synthesis import synthesis_content_hash
    from plastic_promise.core.synthesis_retrieval import _source_is_available

    authority, authority_reason = _notification_runtime_authority(
        runtime_authority,
        tool_name="audit_rollover",
        reason_prefix="audit_notification",
    )
    if authority is None:
        return {
            "committed": False,
            "partial": False,
            "reason": authority_reason,
            "tombstoned_ids": [],
            "memory_id": "",
        }
    actor, call_id, project_id = authority

    canonical_review = getattr(engine, "get_memory_dict_for_review", None)
    if not callable(canonical_review):
        return {
            "committed": False,
            "partial": False,
            "reason": "audit_notification_canonical_review_required",
            "tombstoned_ids": [],
            "memory_id": "",
        }

    audit_content = str(event.get("content") or "").strip()
    if not audit_content:
        return {
            "committed": False,
            "partial": False,
            "reason": "audit_notification_content_required",
            "tombstoned_ids": [],
            "memory_id": "",
        }
    try:
        overall = float(event.get("overall", 0) or 0)
    except (TypeError, ValueError):
        overall = 0.0
    try:
        embedder = vars(engine).get("_embedder") if hasattr(engine, "__dict__") else None
        material = prepare_index_material(
            {"content": audit_content},
            embedder=embedder,
            policy="legacy",
            model_name=effective_embedding_model_name(embedder),
        )
        audit_memory = {
            "content": audit_content,
            "memory_type": "reflection",
            "tags": ["audit", "domain:governing", f"score:{overall:.2f}"],
            "source": "maintenance_daemon",
            "project_id": project_id,
            "visibility": "project",
            "source_class": "reflection",
            "created_by_call_id": call_id,
            "raw_content": audit_content,
            "l0_abstract": audit_content,
            "l1_summary": audit_content,
            "l2_content": audit_content,
            "embedding_text": material.vector_text,
            "embedding_hash": material.embedding_hash,
            "search_text": material.search_text,
            "metadata_json": metadata_with_index_material({}, material),
        }
    except Exception:
        return {
            "committed": False,
            "partial": False,
            "reason": "audit_notification_index_material_failed",
            "tombstoned_ids": [],
            "memory_id": "",
        }

    audit_sources: list[tuple[str, str, str, dict[str, Any]]] = []
    for mem in list(engine.iter_memories()):
        if not isinstance(mem, dict):
            continue
        memory_id = str(mem.get("id") or "").strip()
        if not memory_id:
            continue
        try:
            canonical = canonical_review(memory_id)
        except Exception:
            return {
                "committed": False,
                "partial": False,
                "reason": "audit_notification_canonical_review_failed",
                "tombstoned_ids": [],
                "memory_id": "",
            }
        if canonical is None:
            continue
        if not isinstance(canonical, dict):
            return {
                "committed": False,
                "partial": False,
                "reason": "audit_notification_canonical_review_failed",
                "tombstoned_ids": [],
                "memory_id": "",
            }
        tags = canonical.get("tags")
        if not isinstance(tags, (list, tuple)) or "audit" not in tags:
            continue
        try:
            if not _source_is_available(canonical):
                continue
        except Exception:
            return {
                "committed": False,
                "partial": False,
                "reason": "audit_notification_canonical_review_failed",
                "tombstoned_ids": [],
                "memory_id": "",
            }
        source_project_id = str(canonical.get("project_id") or "").strip()
        if source_project_id == project_id:
            audit_sources.append(
                (
                    memory_id,
                    source_project_id,
                    synthesis_content_hash(canonical.get("content")),
                    {"tags": list(tags)},
                )
            )

    tombstoned_ids: list[str] = []
    stale_dependents: list[str] = []
    for index, (
        memory_id,
        source_project_id,
        expected_content_hash,
        expected_source_snapshot,
    ) in enumerate(audit_sources):
        try:
            result = engine.mutate_ordinary_source(
                memory_id,
                operation="forgotten",
                reason="http_notify:audit_replaced",
                actor=actor,
                call_id=f"{call_id}:audit-replaced:{index}",
                expected_project_id=source_project_id,
                expected_content_hash=expected_content_hash,
                expected_source_snapshot=expected_source_snapshot,
                require_source_available=True,
            )
        except Exception:
            return {
                "committed": False,
                "partial": bool(tombstoned_ids),
                "reason": "audit_replacement_failed",
                "tombstoned_ids": tombstoned_ids,
                "stale_dependents": stale_dependents,
                "memory_id": "",
            }
        tombstoned_ids.append(memory_id)
        stale_dependents.extend(str(item) for item in result.stale_synthesis_ids)

    try:
        memory_id = engine.create_ordinary_if_absent(audit_memory)
    except Exception:
        memory_id = ""
    if not memory_id:
        return {
            "committed": False,
            "partial": bool(tombstoned_ids),
            "reason": "audit_report_store_failed",
            "tombstoned_ids": tombstoned_ids,
            "stale_dependents": stale_dependents,
            "memory_id": "",
        }
    return {
        "committed": True,
        "partial": False,
        "reason": "",
        "tombstoned_ids": tombstoned_ids,
        "stale_dependents": stale_dependents,
        "memory_id": str(memory_id),
    }


def _persist_llm_classification_notification(
    engine: Any,
    event: dict[str, Any],
    runtime_authority: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply one authorized classification as a single canonical metadata patch."""
    from plastic_promise.core.synthesis import synthesis_content_hash
    from plastic_promise.smart_extractor import CATEGORY_KEYWORDS

    authority, authority_reason = _notification_runtime_authority(
        runtime_authority,
        tool_name="memory_update",
        reason_prefix="llm_classification",
    )
    memory_id = str(event.get("memory_id") or "").strip()
    if authority is None:
        return {
            "committed": False,
            "partial": False,
            "reason": authority_reason,
            "memory_id": memory_id,
        }
    _actor, _call_id, project_id = authority
    if not memory_id:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_memory_id_required",
            "memory_id": "",
        }
    raw_category = event.get("new_category")
    if raw_category is not None and not isinstance(raw_category, str):
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_category_invalid",
            "memory_id": memory_id,
        }
    new_category = str(raw_category or "").strip().casefold()
    allowed_categories = frozenset(CATEGORY_KEYWORDS)
    if new_category not in allowed_categories:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_category_invalid",
            "memory_id": memory_id,
        }

    canonical_review = getattr(engine, "get_memory_dict_for_review", None)
    if not callable(canonical_review):
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_canonical_review_required",
            "memory_id": memory_id,
        }
    try:
        canonical = canonical_review(memory_id)
    except Exception:
        canonical = None
    if not isinstance(canonical, dict):
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_canonical_source_missing",
            "memory_id": memory_id,
        }
    source_project_id = str(canonical.get("project_id") or "").strip()
    if not source_project_id:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_source_project_required",
            "memory_id": memory_id,
        }
    if source_project_id != project_id:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_project_mismatch",
            "memory_id": memory_id,
        }
    observed_project_id = str(event.get("expected_project_id") or "").strip()
    if not observed_project_id:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_expected_project_required",
            "memory_id": memory_id,
        }
    if observed_project_id != source_project_id:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_source_changed",
            "memory_id": memory_id,
        }

    expected_content_hash = str(event.get("expected_content_hash") or "").strip()
    if not expected_content_hash:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_content_hash_required",
            "memory_id": memory_id,
        }
    if synthesis_content_hash(canonical.get("content")) != expected_content_hash:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_source_changed",
            "memory_id": memory_id,
        }
    expected_category = str(event.get("expected_category") or "").strip()
    if not expected_category:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_expected_category_required",
            "memory_id": memory_id,
        }
    if str(canonical.get("category") or "").strip() != expected_category:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_source_changed",
            "memory_id": memory_id,
        }

    source_tags = canonical.get("tags")
    if not isinstance(source_tags, (list, tuple)):
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_source_tags_invalid",
            "memory_id": memory_id,
        }
    observed_tags = event.get("expected_tags")
    if not isinstance(observed_tags, (list, tuple)) or not all(
        isinstance(tag, str) for tag in observed_tags
    ):
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_expected_tags_required",
            "memory_id": memory_id,
        }
    observed_tags = list(observed_tags)
    if list(source_tags) != observed_tags or "llm_pending:true" not in observed_tags:
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_source_changed",
            "memory_id": memory_id,
        }
    tags = [
        str(tag)
        for tag in observed_tags
        if str(tag) != "llm_pending:true" and not str(tag).casefold().startswith("cat:")
    ]
    if "llm_classified:true" not in tags:
        tags.append("llm_classified:true")
    category_tag = f"cat:{new_category}" if new_category else ""
    if category_tag and category_tag not in tags:
        tags.append(category_tag)
    replacements: dict[str, Any] = {"tags": tags}
    if new_category:
        replacements["category"] = new_category

    patch = getattr(engine, "patch_ordinary_memory", None)
    if not callable(patch):
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_patch_api_required",
            "memory_id": memory_id,
        }
    try:
        updated = patch(
            memory_id,
            replacements=replacements,
            expected_project_id=source_project_id,
            expected_content_hash=expected_content_hash,
            expected_tags=observed_tags,
            expected_category=expected_category,
            require_source_available=True,
        )
    except Exception:
        updated = None
    if not isinstance(updated, dict):
        return {
            "committed": False,
            "partial": False,
            "reason": "llm_classification_patch_failed",
            "memory_id": memory_id,
        }
    return {
        "committed": True,
        "partial": False,
        "reason": "",
        "memory_id": memory_id,
        "category": new_category,
        "tags": tags,
    }


async def _persist_then_publish_notification(
    queue: Any,
    event: dict[str, Any],
    *,
    engine: Any | None = None,
    runtime_authority: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Publish governed notifications only after their canonical write outcome."""
    event_type = event.get("type")
    if event_type == "audit_report":
        persistence = _persist_audit_report_notification(
            engine,
            event,
            runtime_authority,
        )
    elif event_type == "llm_classified":
        persistence = _persist_llm_classification_notification(
            engine,
            event,
            runtime_authority,
        )
    else:
        await queue.put(event)
        return None

    if persistence.get("committed"):
        await queue.put(event)
    elif event_type == "audit_report" and persistence.get("partial"):
        await queue.put(
            {
                "type": "audit_report_persistence",
                **({"project_id": event["project_id"]} if event.get("project_id") else {}),
                "status": "partial",
                "event": event,
                "audit_persistence": persistence,
            }
        )
    return persistence


async def _handle_notification_event(
    queue: Any,
    event: dict[str, Any],
    *,
    engine: Any | None = None,
    runtime_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run /notify business logic and build its explicit response payload."""
    persistence = await _persist_then_publish_notification(
        queue,
        event,
        engine=engine,
        runtime_authority=runtime_authority,
    )
    response: dict[str, Any] = {"ok": True}
    if persistence is not None:
        response["ok"] = bool(persistence.get("committed"))
        persistence_key = (
            "audit_persistence"
            if event.get("type") == "audit_report"
            else "classification_persistence"
        )
        response[persistence_key] = persistence
    return response


def _tool_runtime_event_context(
    name: str,
    arguments: dict[str, Any],
    *,
    mutation_runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        from plastic_promise.core.tool_manifest import (
            evaluate_tool_decision,
            manifest_for_tool,
        )
        from plastic_promise.mcp.tools.audit_defense import _get_trust_manager
        from plastic_promise.mcp.tools.request_scope import build_request_scope

        scope = build_request_scope(arguments, name)
        tm = _get_trust_manager()
        target = str(arguments.get("target") or "")
        trust_score = float(arguments.get("trust_score", tm.get(target)))
        trust_tier = str(arguments.get("trust_tier") or tm.tier(target))
        manifest = manifest_for_tool(name)
        decision = evaluate_tool_decision(manifest, trust_score, trust_tier=trust_tier)
        context = {
            **scope,
            "tool_name": name,
            "actor": str(arguments.get("actor") or arguments.get("agent_name") or "mcp"),
            "trust_tier": trust_tier,
            "defense_decision": decision["decision"],
            "audit_trace": {
                "tool_name": name,
                "risk_level": manifest.risk_level,
                "required_trust": manifest.trust_requirement,
                "trust_score": trust_score,
            },
            "metadata": {
                "side_effects": list(manifest.side_effects),
                "fallbacks": list(manifest.fallbacks),
            },
        }
        if mutation_runtime_context is not None:
            context.update(
                {
                    "project_id": str(mutation_runtime_context.get("project_id") or ""),
                    "actor": str(mutation_runtime_context.get("actor") or "mcp"),
                    "trust_tier": str(mutation_runtime_context.get("trust_tier") or ""),
                    "defense_decision": str(
                        mutation_runtime_context.get("defense_decision") or "deny"
                    ),
                }
            )
            context["audit_trace"].update(
                {
                    "runtime_call_id": str(mutation_runtime_context.get("call_id") or ""),
                    "trust_score": mutation_runtime_context.get("trust_score", 0.0),
                }
            )
            context["metadata"]["caller_declarations"] = {
                key: arguments[key]
                for key in (
                    "actor",
                    "call_id",
                    "project_id",
                    "trust_score",
                    "trust_tier",
                    "defense_decision",
                )
                if key in arguments
            }
        return context
    except Exception:
        return {
            "tool_name": name,
            "request_scope_id": "",
            "stage_session_id": "",
            "flow_line_id": "",
            "actor": "mcp",
            "trust_tier": "",
            "defense_decision": "",
            "audit_trace": {},
            "metadata": {},
        }


def _project_principles(principles: Any) -> list[Any]:
    if not isinstance(principles, list):
        return []
    projected: list[Any] = []
    for principle in principles[:8]:
        if not isinstance(principle, dict):
            projected.append(principle)
            continue
        projected.append(
            {
                key: principle.get(key)
                for key in ("id", "name", "domain")
                if principle.get(key) not in (None, "")
            }
        )
    return projected


def _resolved_session_init_project_id(result: Any) -> str:
    """Read the canonical project resolved by session-init itself."""

    data = result.data if isinstance(getattr(result, "data", None), dict) else {}
    workflow = data.get("workflow_contract")
    workflow = workflow if isinstance(workflow, dict) else {}
    context_status = data.get("context_status")
    context_status = context_status if isinstance(context_status, dict) else {}
    chain_state = data.get("chain_state")
    chain_state = chain_state if isinstance(chain_state, dict) else {}
    for candidate in (
        data.get("project_id"),
        workflow.get("project_id"),
        context_status.get("project_id"),
        chain_state.get("project_id"),
    ):
        project_id = canonical_project_id(candidate)
        if project_id:
            return project_id
    return ""


def _annotate_task_session_binding(
    payload: dict[str, Any],
    *,
    project_id: str,
    success: bool,
    reason: str = "",
) -> None:
    diagnostics = payload.get("diagnostics")
    diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    diagnostics["task_session_binding"] = {
        "success": success,
        "project_id": project_id,
        **({"reason": reason} if reason else {}),
    }
    payload["diagnostics"] = diagnostics
    if project_id:
        payload["project_id"] = project_id
    if success:
        return
    payload["degraded"] = True
    warnings = list(payload.get("warnings") or [])
    warning = f"task_session_binding:{reason or 'task_session_project_required'}"
    if warning not in warnings:
        warnings.append(warning)
    payload["warnings"] = warnings


def _annotate_durable_collaboration_binding(
    payload: dict[str, Any],
    *,
    project_id: str,
    success: bool,
    reason: str = "",
    binding: Any | None = None,
) -> None:
    """Project a bounded PR5 binding receipt into ``session-init`` output.

    The receipt deliberately exposes neither the opaque SDK transport identity
    nor the durable AgentSession id.  Those are server-owned lifecycle
    bindings, not caller-selectable handles.  Until the full PR5 lifecycle is
    independently evidenced, a successful local registration is described
    only as a persistent runtime binding—not as a completed collaboration
    system.
    """

    diagnostics = payload.get("diagnostics")
    diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    diagnostics["durable_collaboration_binding"] = {
        "success": success,
        "project_id": project_id,
        "persistent": success,
        **({"reason": reason} if reason else {}),
    }
    payload["diagnostics"] = diagnostics
    if success:
        host = getattr(binding, "host", None)
        if host is None:
            payload["degraded"] = True
            warnings = list(payload.get("warnings") or [])
            warning = "durable_collaboration_binding:host_unavailable"
            if warning not in warnings:
                warnings.append(warning)
            payload["warnings"] = warnings
            return
        try:
            payload["durable_collaboration"] = host.session_init_projection()
        except Exception:
            payload["degraded"] = True
            warnings = list(payload.get("warnings") or [])
            warning = "durable_collaboration_binding:projection_unavailable"
            if warning not in warnings:
                warnings.append(warning)
            payload["warnings"] = warnings
        return
    payload["degraded"] = True
    warnings = list(payload.get("warnings") or [])
    warning = f"durable_collaboration_binding:{reason or 'unavailable'}"
    if warning not in warnings:
        warnings.append(warning)
    payload["warnings"] = warnings


def _project_session_init_result(result: Any, response_mode: str) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    project_id = _resolved_session_init_project_id(result)
    if response_mode == "standard":
        return {
            "schema_version": "session-init-response-v1",
            "response_mode": response_mode,
            "skill": result.skill_name,
            "success": result.success,
            **({"project_id": project_id} if project_id else {}),
            "data": data,
            "degrade_log": result.degrade_log,
            "errors": result.errors,
            "audit_trail": result.audit_trail,
        }

    context_status = data.get("context_status")
    context_status = dict(context_status) if isinstance(context_status, dict) else {}
    items = context_status.get("items")
    if isinstance(items, list):
        context_status["items"] = [
            {
                **{
                    key: item.get(key)
                    for key in ("id", "relevance", "source", "worth_score")
                    if item.get(key) not in (None, "")
                },
                "content": str(item.get("content") or "")[:240],
            }
            for item in items[:2]
            if isinstance(item, dict)
        ]
    workflow = data.get("workflow_contract")
    workflow = workflow if isinstance(workflow, dict) else {}
    chain = data.get("chain_state")
    chain = chain if isinstance(chain, dict) else {}
    compact_chain = {
        key: chain.get(key)
        for key in (
            "stage_session_id",
            "current_stage",
            "valid_next",
            "route_id",
            "flow_line_id",
            "flow_scope_id",
            "entry_stage",
            "valid_root_entrypoints",
        )
        if chain.get(key) not in (None, "", [], {})
    }
    return {
        "schema_version": "session-init-response-v1",
        "response_mode": response_mode,
        "skill": result.skill_name,
        "success": result.success,
        **({"project_id": project_id} if project_id else {}),
        "principles": _project_principles(data.get("principles")),
        "trust": data.get("trust", {}),
        "context_status": context_status,
        "stage_session_id": data.get("stage_session_id", ""),
        "workflow": {
            key: workflow.get(key)
            for key in (
                "route_id",
                "flow_line_id",
                "flow_scope_id",
                "entry_stage",
                "entry_authority",
                "branches",
            )
            if workflow.get(key) not in (None, "", {})
        },
        "chain_state": compact_chain,
        "next_call": dict(
            workflow.get("next_call")
            or {
                "tool": "sp-stage",
                "stage": workflow.get("entry_stage") or "grill-with-docs",
                "invocation_source": workflow.get("entry_authority") or "user",
                "auto_invoke": workflow.get("entry_authority") == "model",
                "stage_session_id": data.get("stage_session_id", ""),
                "flow_line_id": workflow.get("flow_line_id", ""),
            }
        ),
        "degraded": bool(result.degrade_log or result.errors),
        "warnings": list(result.degrade_log or []),
        "errors": list(result.errors or []),
        "diagnostics": {
            "level": "summary",
            "component_health_count": len(data.get("component_health") or {}),
            "domain_health_available": bool(data.get("domain_health")),
            "system_stats_available": bool(data.get("system_stats")),
            "gc_preview_available": bool(data.get("gc_preview")),
        },
    }


def _project_sp_stage_data(data: dict[str, Any], guidance_level: str) -> dict[str, Any]:
    projected = dict(data)
    projected.pop("stage", None)
    if guidance_level == "full":
        return projected
    exemplar = projected.get("exemplar")
    if isinstance(exemplar, dict):
        summary = {
            key: exemplar.get(key)
            for key in (
                "problem",
                "search_query",
                "search_hints",
                "gap_signal",
                "legacy_instructions",
            )
            if exemplar.get(key) not in (None, "", [], {})
        }
        summary["instructions_available"] = bool(exemplar.get("instructions"))
        summary["full_guidance_hint"] = (
            "Set guidance_level=full to expand search and dispatch templates."
        )
        projected["exemplar"] = summary
    return projected


def _record_tool_runtime_event(engine: Any, ctx: dict[str, Any], status: str) -> None:
    try:
        from plastic_promise.core.event_protocol import safe_record_runtime_event

        safe_record_runtime_event(
            engine,
            event_kind="tool",
            event_name=ctx.get("tool_name", ""),
            status=status,
            request_scope_id=ctx.get("request_scope_id", ""),
            stage_session_id=ctx.get("stage_session_id", ""),
            flow_line_id=ctx.get("flow_line_id", ""),
            project_id=str(ctx.get("project_id", "")),
            actor=ctx.get("actor", "mcp"),
            trust_tier=ctx.get("trust_tier", ""),
            defense_decision=ctx.get("defense_decision", ""),
            audit_trace=ctx.get("audit_trace", {}),
            metadata=ctx.get("metadata", {}),
        )
    except Exception:
        pass


async def _is_known_mcp_tool(name: str) -> bool:
    """Resolve registered tools without treating the unknown fallback as work."""

    global _known_mcp_tool_names_cache
    if name in _known_mcp_tool_names_cache:
        return True
    names = frozenset(tool.name for tool in await list_tools())
    _known_mcp_tool_names_cache = names
    return name in names


def _reconcile_authenticated_tool_call(engine: Any) -> dict[str, Any] | None:
    """Run one bounded collaboration reconcile for the exact live transport."""

    exact = _current_durable_collaboration_binding()
    host = getattr(exact, "host", None) if exact is not None else None
    if host is None:
        return None
    try:
        receipt = host.reconcile_tool_call()
    except Exception as exc:
        from plastic_promise.collaboration.contracts import CollaborationContractError

        reason = str(exc).strip() if isinstance(exc, CollaborationContractError) else ""
        return {
            "schema_version": "durable-collaboration-tool-call-runtime/v1",
            "state": "deferred",
            "reason": reason or "durable_collaboration_tool_call_reconcile_unavailable",
        }
    active_leases = receipt.get("active_leases")
    peer_delta = receipt.get("peer_delta")
    cursor = receipt.get("cursor")
    return {
        "schema_version": "durable-collaboration-tool-call-runtime/v1",
        "state": "durable",
        "presence_state": str(receipt.get("state") or "active"),
        "active_lease_count": len(active_leases) if isinstance(active_leases, list) else 0,
        "peer_event_count": (
            len(peer_delta.get("items") or ()) if isinstance(peer_delta, Mapping) else 0
        ),
        "cursor_stored_sequence": (
            int(cursor.get("stored_sequence") or 0) if isinstance(cursor, Mapping) else 0
        ),
        "cursor_next_sequence": (
            int(cursor.get("next_sequence") or 0) if isinstance(cursor, Mapping) else 0
        ),
        "persistent": True,
        "canonical_memory_effect": "none",
    }


def _delegated_policy_error(
    role: object, tool_name: str, decision: dict[str, Any]
) -> list[TextContent]:
    """Return a stable fail-closed response for delegated-agent calls."""

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": "delegated_agent_policy_denied",
                    "tool": tool_name,
                    "agent_role": str(role or ""),
                    "decision": decision,
                },
                ensure_ascii=False,
            ),
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Route MCP tool calls to handler modules.

    Each tool domain is delegated to its own module under
    plastic_promise.mcp.tools.* for clean separation of concerns.
    Handlers are lazily imported on first call.
    """
    from plastic_promise.core.traceability import bind_call_span_start, reset_call_span_start

    # Delegated agents must explicitly declare their role.  The primary
    # Codex client has no role field and keeps the normal full MCP surface;
    # once a role is declared, every call is checked here before any handler
    # or runtime event can run.  This is the enforcement point, not merely
    # guidance emitted by the workflow stage planner.
    delegated_role = arguments.get("agent_role")
    if delegated_role is not None and str(delegated_role).strip():
        from plastic_promise.core.agent_tool_policy import authorize_agent_mcp_call

        policy_arguments = _without_collaboration_continuation_token(arguments)
        policy_arguments.pop("agent_role", None)
        decision = authorize_agent_mcp_call(delegated_role, name, policy_arguments)
        if not decision.get("allowed"):
            return _delegated_policy_error(delegated_role, name, decision)
        arguments = dict(arguments)
        arguments.pop("agent_role", None)

    engine = get_engine()
    mutation_tool_name = "memory_update" if name in {"smart-remember", "smart_remember"} else name
    mutation_runtime_context = (
        _mutation_runtime_context(mutation_tool_name, arguments)
        if mutation_tool_name
        in {
            "memory_update",
            "memory_forget",
            "memory_correct",
            "memory_reclassify",
            "memory_sync_files",
            "feedback_apply",
        }
        else None
    )
    task_runtime_context = (
        _task_runtime_context(name, arguments) if name in _TASK_QUEUE_TOOL_NAMES else None
    )
    authority_runtime_context = task_runtime_context or mutation_runtime_context
    runtime_ctx = _tool_runtime_event_context(
        name,
        _without_collaboration_continuation_token(arguments),
        mutation_runtime_context=authority_runtime_context,
    )
    if name not in _COLLABORATION_TOOL_CALL_RECONCILE_EXCLUSIONS and await _is_known_mcp_tool(name):
        collaboration_reconcile = _reconcile_authenticated_tool_call(engine)
        if collaboration_reconcile is not None:
            runtime_metadata = dict(runtime_ctx.get("metadata") or {})
            runtime_metadata["durable_collaboration"] = collaboration_reconcile
            runtime_ctx["metadata"] = runtime_metadata
    runtime_status = "completed"
    _record_tool_runtime_event(engine, runtime_ctx, "pending")
    _record_tool_runtime_event(engine, runtime_ctx, "running")
    span_start_token = bind_call_span_start()
    workflow_flow_lock = None
    workflow_flow_lock_acquired = False

    try:
        if name in {"sp-stage", "sp_stage"}:
            workflow_flow_lock = _workflow_flow_lock(engine, arguments)
            await workflow_flow_lock.acquire()
            workflow_flow_lock_acquired = True

        # Memory domain
        if name == "memory_recall":
            from plastic_promise.mcp.tools.memory import handle_memory_recall

            return await handle_memory_recall(engine, arguments)
        elif name == "memory_store":
            from plastic_promise.mcp.tools.memory import handle_memory_store

            return await handle_memory_store(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        elif name == "memory_update":
            from plastic_promise.mcp.tools.memory import handle_memory_update

            return await handle_memory_update(
                engine,
                arguments,
                _runtime_context=mutation_runtime_context,
            )
        elif name == "memory_forget":
            from plastic_promise.mcp.tools.memory import handle_memory_forget

            return await handle_memory_forget(
                engine,
                arguments,
                _runtime_context=mutation_runtime_context,
            )
        elif name == "memory_list":
            from plastic_promise.mcp.tools.memory import handle_memory_list

            return await handle_memory_list(engine, arguments)
        elif name == "memory_gc":
            from plastic_promise.mcp.tools.memory import handle_memory_gc

            return await handle_memory_gc(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        elif name == "memory_correct":
            from plastic_promise.mcp.tools.memory import handle_memory_correct

            return await handle_memory_correct(
                engine,
                arguments,
                _runtime_context=mutation_runtime_context,
            )
        elif name == "memory_reclassify":
            from plastic_promise.mcp.tools.memory import handle_memory_reclassify

            return await handle_memory_reclassify(
                engine,
                arguments,
                _runtime_context=mutation_runtime_context,
            )
        # Principle domain
        elif name == "principle_activate":
            from plastic_promise.mcp.tools.principles import handle_principle_activate

            return await handle_principle_activate(engine, arguments)
        elif name == "principle_evaluate":
            from plastic_promise.mcp.tools.principles import handle_principle_evaluate

            return await handle_principle_evaluate(engine, arguments)

        # Context domain
        elif name == "context_supply":
            from plastic_promise.mcp.tools.context import handle_context_supply

            exact = _current_durable_collaboration_binding()
            collaboration_runtime = getattr(exact, "host", None) if exact is not None else None
            return await handle_context_supply(
                engine,
                arguments,
                _collaboration_runtime=collaboration_runtime,
            )
        elif name == "context_inject":
            from plastic_promise.mcp.tools.context import handle_context_inject

            return await handle_context_inject(engine, arguments)
        elif name == "context_graph":
            from plastic_promise.mcp.tools.context import handle_context_graph

            return await handle_context_graph(engine, arguments)
        elif name == "auto_context_inject":
            from plastic_promise.mcp.tools.context import handle_auto_context_inject

            lifecycle = _durable_collaboration_lifecycle(engine, arguments)
            lifecycle_arguments = _without_collaboration_continuation_token(arguments)
            # This is a server-owned internal projection.  It never appears
            # in the public input schema and is consumed by the handler only
            # to expose bounded diagnostics alongside passive-memory output.
            lifecycle_arguments["_durable_collaboration_lifecycle"] = lifecycle
            return await handle_auto_context_inject(engine, lifecycle_arguments)
        elif name == "mgp_shadow_bridge":
            from plastic_promise.mcp.tools.mgp_shadow import handle_mgp_shadow_bridge

            return await handle_mgp_shadow_bridge(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )

        # Audit and defense
        elif name == "audit_run":
            from plastic_promise.mcp.tools.audit_defense import handle_audit_run

            return await handle_audit_run(engine, arguments)
        elif name == "audit_pre_check":
            from plastic_promise.mcp.tools.audit_defense import handle_audit_pre_check

            return await handle_audit_pre_check(engine, arguments)
        elif name == "defense":
            from plastic_promise.mcp.tools.audit_defense import handle_defense

            return await handle_defense(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )

        # Reflection
        elif name == "scarf_reflect":
            from plastic_promise.mcp.tools.reflection import handle_scarf_reflect

            return await handle_scarf_reflect(engine, arguments)
        elif name == "feedback_apply":
            from plastic_promise.mcp.tools.reflection import handle_feedback_apply

            return await handle_feedback_apply(
                engine,
                arguments,
                _runtime_context=mutation_runtime_context,
            )

        # Management
        elif name == "system":
            from plastic_promise.mcp.tools.management import handle_system

            return await handle_system(engine, arguments)
        elif name == "runtime_mode":
            from plastic_promise.mcp.tools.runtime import handle_runtime_mode

            return await handle_runtime_mode(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        elif name == "issue_create":
            from plastic_promise.mcp.tools.management import handle_issue_create

            return await handle_issue_create(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        elif name == "issue_transition":
            from plastic_promise.mcp.tools.management import handle_issue_transition

            return await handle_issue_transition(engine, arguments)
        elif name == "issue_list":
            from plastic_promise.mcp.tools.management import handle_issue_list

            return await handle_issue_list(engine, arguments)
        elif name == "pack_export":
            from plastic_promise.mcp.tools.management import handle_pack_export

            return await handle_pack_export(engine, arguments)
        elif name == "pack_import":
            from plastic_promise.mcp.tools.management import handle_pack_import

            return await handle_pack_import(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        # Domain federation
        elif name == "domain":
            from plastic_promise.mcp.tools.domain import handle_domain

            return await handle_domain(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )

        # Task queue
        elif name == "task_enqueue":
            from plastic_promise.mcp.tools.task_queue import handle_task_enqueue

            return await handle_task_enqueue(
                engine,
                arguments,
                _runtime_context=task_runtime_context,
            )
        elif name == "task_claim":
            from plastic_promise.mcp.tools.task_queue import handle_task_claim

            return await handle_task_claim(
                engine,
                arguments,
                _runtime_context=task_runtime_context,
            )
        elif name == "task_complete":
            from plastic_promise.mcp.tools.task_queue import handle_task_complete

            return await handle_task_complete(
                engine,
                arguments,
                _runtime_context=task_runtime_context,
            )
        elif name == "task_verify":
            from plastic_promise.mcp.tools.task_queue import handle_task_verify

            return await handle_task_verify(
                engine,
                arguments,
                _runtime_context=task_runtime_context,
            )
        elif name == "task_inbox":
            from plastic_promise.mcp.tools.task_queue import handle_task_inbox

            return await handle_task_inbox(
                engine,
                arguments,
                _runtime_context=task_runtime_context,
            )
        elif name == "task_heartbeat":
            from plastic_promise.mcp.tools.task_queue import handle_task_heartbeat

            return await handle_task_heartbeat(
                engine,
                arguments,
                _runtime_context=task_runtime_context,
            )
        elif name == "task_abandon":
            from plastic_promise.mcp.tools.task_queue import handle_task_abandon

            return await handle_task_abandon(
                engine,
                arguments,
                _runtime_context=task_runtime_context,
            )
        elif name == "collaboration_work_list":
            outcome = _durable_collaboration_work_operation("list", arguments)
            return [TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))]
        elif name == "collaboration_work_register":
            outcome = _durable_collaboration_work_operation("register", arguments)
            return [TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))]
        elif name == "collaboration_work_claim":
            outcome = _durable_collaboration_work_operation("claim", arguments)
            return [TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))]
        elif name == "collaboration_lease_heartbeat":
            outcome = _durable_collaboration_work_operation("lease_heartbeat", arguments)
            return [TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))]
        elif name == "collaboration_work_review":
            outcome = _durable_collaboration_work_operation("review", arguments)
            return [TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))]
        elif name == "collaboration_work_accept":
            outcome = _durable_collaboration_work_operation("accept", arguments)
            return [TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))]

        # Skill tracking
        elif name == "skill_session_start":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

            return await handle_skill_session_start(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        elif name == "skill_session_complete":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete

            return await handle_skill_session_complete(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        elif name == "skill_session_trace":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

            return await handle_skill_session_trace(engine, arguments)
        elif name == "skill_session_audit":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_audit

            return await handle_skill_session_audit(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        elif name == "skill_auto_track":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_auto_track

            return await handle_skill_auto_track(engine, arguments)

        # === Skills 域 (Phase 1) ===
        elif name in ("session-init", "session_init"):
            if _task_session_project_conflict(arguments.get("project_id")):
                binding = _task_session_authority() or {}
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "success": False,
                                "error": "task_session_project_conflict",
                                "project_id": str(binding.get("project_id") or ""),
                                "requested_project_id": str(
                                    arguments.get("project_id") or ""
                                ).strip(),
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            continuation_token = arguments.get(_COLLABORATION_CONTINUATION_TOKEN_ARGUMENT)
            hook_session_id = str(arguments.get("hook_session_id") or "").strip()
            if not str(continuation_token or "").strip() and not hook_session_id:
                hook_session_id = f"hook:mcp:{secrets.token_hex(16)}"
            se = get_skill_engine()
            skill_arguments = _without_collaboration_continuation_token(arguments)
            result = await se.exec("session-init", skill_arguments, caller="claude")
            response_mode = str(arguments.get("response_mode") or "compact").strip().casefold()
            if response_mode not in {"standard", "compact"}:
                response_mode = "compact"
            payload = _project_session_init_result(result, response_mode)
            if result.success:
                resolved_project_id = _resolved_session_init_project_id(result)
                session_data = result.data if isinstance(result.data, dict) else {}
                workflow = session_data.get("workflow_contract")
                workflow = workflow if isinstance(workflow, Mapping) else {}
                binding_succeeded, binding_reason = _bind_task_session_authority(
                    resolved_project_id,
                    workflow=workflow,
                    stage_session_id=session_data.get("stage_session_id") or "",
                )
                _annotate_task_session_binding(
                    payload,
                    project_id=resolved_project_id,
                    success=binding_succeeded,
                    reason=binding_reason,
                )
                if binding_succeeded:
                    durable_succeeded, durable_reason = (
                        _bind_durable_collaboration_runtime_for_project(
                            engine,
                            resolved_project_id,
                            continuation_token=continuation_token,
                            hook_session_id=hook_session_id,
                        )
                    )
                else:
                    durable_succeeded = False
                    durable_reason = "durable_collaboration_task_session_authority_required"
                _annotate_durable_collaboration_binding(
                    payload,
                    project_id=resolved_project_id,
                    success=durable_succeeded,
                    reason=durable_reason,
                    binding=(
                        _current_durable_collaboration_binding() if durable_succeeded else None
                    ),
                )
                if durable_succeeded and hook_session_id:
                    token, expires_at_epoch, continuation_reason = (
                        _issue_durable_collaboration_continuation(hook_session_id=hook_session_id)
                    )
                    if token:
                        payload[_COLLABORATION_CONTINUATION_TOKEN_ARGUMENT] = token
                        payload["collaboration_continuation"] = {
                            "schema_version": "durable-collaboration-continuation/v1",
                            "token": token,
                            "expires_at_epoch": expires_at_epoch,
                            "hook_session_id": hook_session_id,
                            "storage": "client-secret-only",
                        }
                    elif continuation_reason:
                        payload["degraded"] = True
                        warnings = list(payload.get("warnings") or [])
                        warning = f"durable_collaboration_continuation:{continuation_reason}"
                        if warning not in warnings:
                            warnings.append(warning)
                        payload["warnings"] = warnings
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
        elif name in ("smart-remember", "smart_remember"):
            se = get_skill_engine()
            skill_arguments = dict(arguments)
            skill_arguments["_runtime_context"] = mutation_runtime_context
            result = await se.exec(
                "smart-remember",
                skill_arguments,
                caller=_smart_remember_runtime_caller(mutation_runtime_context),
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "skill": result.skill_name,
                            "success": result.success,
                            "data": result.data,
                            "degrade_log": result.degrade_log,
                            "errors": result.errors,
                            "audit_trail": result.audit_trail,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
        elif name in ("step-closure", "step_closure"):
            from plastic_promise.loop.soul_loop import post_task

            formal_fields = (
                "outcome",
                "summary",
                "artifact_refs",
                "evidence_refs",
                "result",
            )
            task_desc = arguments.get("task_description", "")
            git_commit = arguments.get("git_commit", "")
            mode = arguments.get("mode", "full")
            lesson = arguments.get("lesson", "")
            improvement = arguments.get("improvement", "")
            root_cause = arguments.get("root_cause", "")
            optimization = arguments.get("optimization", "")
            trick = arguments.get("trick", "")
            target = arguments.get("target", "claude")
            work_item_id = str(arguments.get("work_item_id") or "").strip()
            formal_outcome = str(arguments.get("outcome") or "").strip().casefold()
            formal_summary = str(arguments.get("summary") or "").strip()
            formal_artifact_refs = tuple(
                str(item).strip()
                for item in (arguments.get("artifact_refs") or ())
                if str(item).strip()
            )
            formal_evidence_refs = tuple(
                str(item).strip()
                for item in (arguments.get("evidence_refs") or ())
                if str(item).strip()
            )
            formal_result = arguments.get("result")
            collaboration_result: dict[str, Any] | None = None
            is_formal = bool(work_item_id)
            if not is_formal and any(field in arguments for field in formal_fields):
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "success": False,
                                "error": "formal_work_item_required",
                                "closure": None,
                                "collaboration_result": None,
                                "memory_proposal": None,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            if work_item_id:
                exact = _current_durable_collaboration_binding()
                runtime = getattr(exact, "runtime", None) if exact is not None else None
                session = getattr(exact, "session", None) if exact is not None else None
                if runtime is None or session is None:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "success": False,
                                    "error": "durable_collaboration_authenticated_binding_required",
                                    "closure": None,
                                    "collaboration_result": None,
                                    "memory_proposal": None,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                if formal_outcome not in {"completed", "blocked", "failed", "cancelled"}:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "success": False,
                                    "error": "result_outcome_required",
                                    "closure": None,
                                    "collaboration_result": None,
                                    "memory_proposal": None,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                if not formal_summary:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "success": False,
                                    "error": "result_summary_required",
                                    "closure": None,
                                    "collaboration_result": None,
                                    "memory_proposal": None,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                if "result" in arguments and not isinstance(formal_result, Mapping):
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "success": False,
                                    "error": "result_projection_invalid",
                                    "closure": None,
                                    "collaboration_result": None,
                                    "memory_proposal": None,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                try:
                    collaboration_result = runtime.record_step_closure_result(
                        work_item_id=work_item_id,
                        outcome=formal_outcome,
                        summary=formal_summary,
                        artifact_refs=formal_artifact_refs,
                        evidence_refs=formal_evidence_refs,
                        result=formal_result if isinstance(formal_result, Mapping) else None,
                        agent_session_id=session.session_id,
                    )
                except Exception as exc:
                    from plastic_promise.collaboration.contracts import CollaborationContractError

                    reason = str(exc).strip() if isinstance(exc, CollaborationContractError) else ""
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "success": False,
                                    "error": reason or "durable_collaboration_result_unavailable",
                                    "closure": None,
                                    "collaboration_result": None,
                                    "memory_proposal": None,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
            collaboration_replayed = bool(
                collaboration_result is not None and collaboration_result.get("replayed") is True
            )
            if collaboration_replayed:
                replay_projection = dict(collaboration_result)
                replay_projection["state"] = "replayed"
                replay_projection["persistent"] = True
                payload = {
                    "schema_version": "step-closure-response/v2",
                    "success": True,
                    "closure": {
                        "state": "replayed",
                        "dashboard": "",
                        "reflection": {},
                        "reason": "durable_result_replayed",
                    },
                    "collaboration_result": replay_projection,
                    "memory_proposal": None,
                    "canonical_memory_effect": "none",
                }
                return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
            try:
                result = await asyncio.to_thread(
                    post_task,
                    task_desc,
                    git_commit,
                    mode,
                    None,  # issue_id
                    lesson,
                    improvement,
                    root_cause,
                    optimization,
                    trick,
                    target,
                )
            except Exception:
                if is_formal and collaboration_result is not None:
                    degraded_projection = dict(collaboration_result)
                    degraded_projection["state"] = "submitted"
                    degraded_projection["persistent"] = True
                    payload = {
                        "schema_version": "step-closure-response/v2",
                        "success": True,
                        "closure": {
                            "state": "degraded",
                            "dashboard": "",
                            "reflection": {},
                            "reason": "reflection_unavailable",
                        },
                        "collaboration_result": degraded_projection,
                        "memory_proposal": None,
                        "canonical_memory_effect": "none",
                    }
                    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
                raise

            def safe_serialize(obj):
                if isinstance(obj, dict):
                    return {k: safe_serialize(v) for k, v in obj.items()}
                elif isinstance(obj, (list, tuple)):
                    return [safe_serialize(i) for i in obj]
                elif hasattr(obj, "__dict__"):
                    return {
                        k: safe_serialize(v)
                        for k, v in obj.__dict__.items()
                        if not k.startswith("_")
                    }
                elif callable(obj) and not isinstance(
                    obj, (str, int, float, bool, list, dict, type(None))
                ):
                    return str(obj)
                else:
                    try:
                        json.dumps(obj)
                        return obj
                    except (TypeError, ValueError):
                        return str(obj)

            safe = safe_serialize(result)
            if not isinstance(safe, dict):
                safe = {}

            # Record closure in sliding window for trend tracking
            _closure_history.append(
                {
                    "scarf": safe.get("scarf", {}).get("summary", {}).get("overall_score", 0),
                    "trust": safe.get("trust", {}).get("score", 0),
                    "cei": safe.get("cei", {}).get("score", 0),
                }
            )

            # Build dashboard summary + JSON body
            dashboard = _format_closure_dashboard(safe, _closure_history)
            reflection = safe.get("reflection")
            reflection_fields = reflection if isinstance(reflection, dict) else {}
            reflection_available = any(
                isinstance(reflection_fields.get(field), str)
                and len(reflection_fields.get(field, "").strip()) > 5
                for field in ("lesson", "improvement", "root_cause", "optimization")
            )
            if reflection_available:
                dashboard += (
                    "\n  [记忆] 反思仅作为临时闭环结果返回；"
                    "正式记忆必须经独立 accepted-work promotion。"
                )
            payload = {
                "schema_version": "step-closure-response/v2",
                "success": True,
                "closure": {
                    "state": "completed",
                    "dashboard": dashboard,
                    "reflection": reflection_fields,
                },
                "collaboration_result": (
                    {
                        **collaboration_result,
                        "state": "submitted",
                        "persistent": True,
                    }
                    if collaboration_result is not None
                    else None
                ),
                "memory_proposal": None,
                "canonical_memory_effect": "none",
            }
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        # === 审查域 ===
        elif name == "commercial_audit_export":
            from plastic_promise.mcp.tools.commercial_audit import handle_commercial_audit_export

            return await handle_commercial_audit_export(engine, arguments)

        elif name == "review_run":
            from plastic_promise.mcp.tools.review import handle_review_run

            return await handle_review_run(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )
        elif name == "knowledge_search":
            from plastic_promise.mcp.tools.knowledge import handle_knowledge_search

            return await handle_knowledge_search(engine, arguments)

        # === Pinned Matt Pocock workflow (public name retained for compatibility) ===
        elif name in ("sp-stage", "sp_stage"):
            stage = arguments.get("stage", "")
            task_desc = arguments.get("task_description", "")
            invocation_source = str(arguments.get("invocation_source") or "model").casefold()
            stage_session_id = arguments.get("stage_session_id") or arguments.get("stage_id")
            flow_line_id = str(
                arguments.get("flow_line_id") or arguments.get("flow_id") or ""
            ).strip()
            flow_line_id = flow_line_id or None
            route_id = str(arguments.get("route") or "").strip() or None
            project_id = str(arguments.get("project_id") or "").strip()
            public_stage_session_id = stage_session_id or "default"
            from plastic_promise.core.workflow_state import compose_flow_scope

            flow_scope_id = compose_flow_scope(
                public_stage_session_id,
                flow_line_id,
                project_id,
            )
            # ── Chain validation: reject invalid non-root stage transitions ──
            from plastic_promise.core.constants import (
                SKILL_CHAIN_MAP as _CHAIN_MAP,
            )
            from plastic_promise.core.constants import (
                normalize_stage_name,
            )
            from plastic_promise.core.official_workflow import (
                COMPOSITE_SKILL_CALLS,
                OFFICIAL_SKILLS,
                UPSTREAM_SKILLS_REVISION,
                declared_branch_transition_step,
                validate_execution_receipt,
            )
            from plastic_promise.core.workflow_state import (
                commit_workflow_transition,
                engine_connection,
                inspect_execution_receipt,
            )
            from plastic_promise.mcp.tools.skill_tracking import (
                get_current_stage,
                get_stage_chain_state,
                record_attested_composite_skills,
                set_current_stage,
            )
            from plastic_promise.skills.official_workflow_stages import (
                STAGE_ROUTE_MAP,
                attach_stage_guidance,
                build_stage_guidance,
                resolve_stage_route,
            )
            from plastic_promise.skills.tool_routing import invocation_policy

            lookup_stage = normalize_stage_name(stage)
            if not lookup_stage or lookup_stage not in _CHAIN_MAP:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "unknown_stage",
                                "message": f"Unknown stage: '{stage}'. Valid stages: {sorted(_CHAIN_MAP.keys())}",
                                "requested_stage": stage,
                                "available_stages": sorted(_CHAIN_MAP),
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            if route_id and route_id not in STAGE_ROUTE_MAP:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "unknown_route",
                                "message": f"Unknown official route: '{route_id}'.",
                                "requested_route": route_id,
                                "available_routes": sorted(STAGE_ROUTE_MAP),
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]

            if invocation_source not in {"user", "model"}:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "invalid_invocation_source",
                                "message": "invocation_source must be 'user' or 'model'",
                                "requested_source": invocation_source,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            stage_invocation_policy = invocation_policy(lookup_stage)
            if stage_invocation_policy == "user" and invocation_source != "user":
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "invocation_not_allowed",
                                "message": (
                                    f"Stage '{lookup_stage}' represents a user-only skill and "
                                    "cannot be selected automatically by the model."
                                ),
                                "stage": lookup_stage,
                                "required_source": "user",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            resolved_route_id = resolve_stage_route(lookup_stage, route_id)
            route_stages = list(STAGE_ROUTE_MAP[resolved_route_id]["stages"])
            if lookup_stage not in route_stages:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "stage_not_in_route",
                                "message": (
                                    f"Stage '{lookup_stage}' is not part of official route "
                                    f"'{resolved_route_id}'."
                                ),
                                "stage": lookup_stage,
                                "route": resolved_route_id,
                                "route_stages": route_stages,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]

            chain_state = get_stage_chain_state(flow_scope_id, engine=engine)
            current = get_current_stage(flow_scope_id, engine=engine)
            lookup_current = normalize_stage_name(current)
            route_entry = route_stages[0]
            current_step_index = int(chain_state.get("current_step_index", -1))
            persisted_route_id = str(chain_state.get("route_id") or "")
            branch_transition_step = declared_branch_transition_step(
                parent_route_id=persisted_route_id,
                parent_step_index=current_step_index,
                current_stage=lookup_current,
                target_route_id=resolved_route_id,
                target_stage=lookup_stage,
            )
            target_is_declared_branch = branch_transition_step is not None
            persisted_position_is_valid = (
                persisted_route_id == resolved_route_id
                and 0 <= current_step_index < len(route_stages)
                and route_stages[current_step_index] == lookup_current
            )
            target_is_current_replay = (
                persisted_position_is_valid and lookup_stage == lookup_current
            )
            scope_has_durable_cursor = bool(persisted_route_id) and current_step_index >= 0
            target_is_root = lookup_stage == route_entry and not scope_has_durable_cursor
            valid_root_entrypoints = sorted(
                {profile["stages"][0] for profile in STAGE_ROUTE_MAP.values()}
            )

            if not lookup_current and not target_is_root:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "chain_violation",
                                "message": (
                                    f"Stage '{stage}' cannot start a new workflow chain. "
                                    f"Valid root entrypoints: {valid_root_entrypoints}"
                                ),
                                "current_stage": None,
                                "valid_next": [route_entry],
                                "valid_root_entrypoints": valid_root_entrypoints,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]

            # A root starts only an unused scope. Restarting a workflow requires a new
            # stage_session_id or flow_line_id so historical receipts cannot rewind a
            # lane that has already advanced.
            if (
                persisted_route_id
                and persisted_route_id != resolved_route_id
                and not target_is_root
                and not target_is_declared_branch
            ):
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "route_mismatch",
                                "message": (
                                    f"Workflow scope is bound to route '{persisted_route_id}', "
                                    f"not '{resolved_route_id}'."
                                ),
                                "current_stage": lookup_current or None,
                                "route": persisted_route_id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            if (
                lookup_current
                and lookup_current != lookup_stage
                and not target_is_root
                and not target_is_declared_branch
            ):
                if (
                    persisted_route_id == resolved_route_id
                    and 0 <= current_step_index < len(route_stages)
                    and route_stages[current_step_index] == lookup_current
                ):
                    valid_next_normalized = route_stages[
                        current_step_index + 1 : current_step_index + 2
                    ]
                elif lookup_current in route_stages:
                    current_step_index = route_stages.index(lookup_current)
                    valid_next_normalized = route_stages[
                        current_step_index + 1 : current_step_index + 2
                    ]
                else:
                    valid_next_normalized = [route_entry]
                if lookup_stage not in valid_next_normalized:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "chain_violation",
                                    "message": f"Stage '{stage}' is not a valid successor of '{lookup_current}'. Valid next stages: {valid_next_normalized}",
                                    "current_stage": lookup_current,
                                    "valid_next": valid_next_normalized,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
            # ── End chain validation ──
            if target_is_current_replay:
                target_step_index = current_step_index
            elif target_is_root:
                target_step_index = 0
            elif target_is_declared_branch:
                target_step_index = int(branch_transition_step)
            else:
                target_step_index = current_step_index + 1
            raw_receipt = arguments.get("execution_receipt")
            if raw_receipt is None:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "stage": lookup_stage,
                                "success": True,
                                "execution_status": "awaiting_receipt",
                                "receipt_required": True,
                                "stage_session_id": public_stage_session_id,
                                "flow_line_id": flow_line_id,
                                "flow_scope_id": flow_scope_id,
                                "invocation_source": invocation_source,
                                "invocation_source_authenticated": False,
                                "data": {
                                    "stage_guidance": build_stage_guidance(
                                        lookup_stage, route_id=resolved_route_id
                                    ),
                                    "execution_contract": {
                                        "instruction": (
                                            f"Run the Codex Skill '/{lookup_stage}', then call "
                                            "sp-stage again with execution_receipt."
                                        ),
                                        "skill": lookup_stage,
                                        "upstream_revision": UPSTREAM_SKILLS_REVISION,
                                        "content_sha256": OFFICIAL_SKILLS[
                                            lookup_stage
                                        ].content_sha256,
                                        **(
                                            {
                                                "composite_invoked_skills": {
                                                    "required": list(
                                                        COMPOSITE_SKILL_CALLS[lookup_stage][
                                                            "required"
                                                        ]
                                                    ),
                                                    "optional": list(
                                                        COMPOSITE_SKILL_CALLS[lookup_stage][
                                                            "optional"
                                                        ]
                                                    ),
                                                    "receipt_field": "evidence.invoked_skills",
                                                }
                                            }
                                            if lookup_stage in COMPOSITE_SKILL_CALLS
                                            else {}
                                        ),
                                    },
                                },
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            receipt, receipt_error = validate_execution_receipt(lookup_stage, raw_receipt)
            if receipt_error:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "invalid_execution_receipt",
                                "reason": receipt_error,
                                "stage": lookup_stage,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            connection = engine_connection(engine)
            expected_receipt_id = ""
            receipt_status = "unavailable"
            if connection is not None:
                receipt_status, expected_receipt_id = inspect_execution_receipt(
                    connection,
                    scope_id=flow_scope_id,
                    route_id=resolved_route_id,
                    step_index=target_step_index,
                    receipt=receipt,
                )
                if receipt_status == "conflict":
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "execution_receipt_conflict",
                                    "reason": "workflow_receipt_conflict",
                                    "stage": lookup_stage,
                                    "execution_receipt_id": expected_receipt_id,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                if receipt_status == "match":
                    replay_data = attach_stage_guidance(
                        {}, lookup_stage, route_id=resolved_route_id
                    )
                    collaboration_handoff = _publish_sp_stage_collaboration_events(
                        flow_scope_id=flow_scope_id,
                        execution_receipt_id=expected_receipt_id,
                        route_id=resolved_route_id,
                        stage=lookup_stage,
                        step_index=target_step_index,
                    )
                    replay_data["collaboration_handoff"] = collaboration_handoff
                    guidance_level = (
                        str(arguments.get("guidance_level") or "summary").strip().casefold()
                    )
                    if guidance_level not in {"summary", "full"}:
                        guidance_level = "summary"
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "stage": stage,
                                    "success": True,
                                    "stage_session_id": public_stage_session_id,
                                    "flow_line_id": flow_line_id,
                                    "flow_scope_id": flow_scope_id,
                                    "invocation_source": invocation_source,
                                    "invocation_source_authenticated": False,
                                    "execution_status": "already_completed",
                                    "execution_receipt_id": expected_receipt_id,
                                    "collaboration_event_status": collaboration_handoff["state"],
                                    "guidance_level": guidance_level,
                                    "data": _project_sp_stage_data(replay_data, guidance_level),
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
            started_lifecycle: dict[str, Any] = {
                "schema_version": "sp-stage-lifecycle-handoff/v1",
                "state": "deferred",
                "persistent": False,
                "lifecycle": "started",
                "reason": "workflow_stage_started_not_attempted",
                "canonical_memory_effect": "none",
            }
            # A started event is meaningful only for a receipt-bearing first
            # attempt.  It is deliberately after all route/chain/receipt
            # admission checks and immediately before SkillEngine execution;
            # guidance and already-completed replay paths do not emit it.
            if connection is not None and receipt_status == "missing":
                started_lifecycle = _publish_sp_stage_lifecycle_event(
                    flow_scope_id=flow_scope_id,
                    execution_receipt_id=expected_receipt_id,
                    route_id=resolved_route_id,
                    stage=lookup_stage,
                    step_index=target_step_index,
                    lifecycle="started",
                )
            skill_name = f"sp-{lookup_stage}"
            stage_params = {
                "task_description": task_desc,
                "stage_session_id": flow_scope_id,
                "invocation_source": invocation_source,
                "task_type": OFFICIAL_SKILLS[lookup_stage].task_type,
                "domain_hint": OFFICIAL_SKILLS[lookup_stage].domain,
                "project_id": project_id,
            }
            if flow_line_id:
                stage_params["flow_line_id"] = flow_line_id
            stage_params["route"] = resolved_route_id
            if expected_receipt_id:
                stage_params["tracking_idempotency_key"] = (
                    f"{expected_receipt_id}:outer:{lookup_stage}"
                )
                stage_params["tracking_basis"] = "execution_receipt"
            try:
                # Engine acquisition is part of the post-start finalization
                # boundary. If construction fails, the durable started event
                # must receive a stable blocked terminal instead of hanging.
                se = get_skill_engine()
                result = await se.exec(skill_name, stage_params, caller="trae")
            except asyncio.CancelledError:
                if started_lifecycle.get("state") == "durable":
                    _publish_sp_stage_lifecycle_event(
                        flow_scope_id=flow_scope_id,
                        execution_receipt_id=expected_receipt_id,
                        route_id=resolved_route_id,
                        stage=lookup_stage,
                        step_index=target_step_index,
                        lifecycle="blocked",
                        reason_code="stage_execution_cancelled",
                    )
                raise
            except Exception:
                if started_lifecycle.get("state") == "durable":
                    _publish_sp_stage_lifecycle_event(
                        flow_scope_id=flow_scope_id,
                        execution_receipt_id=expected_receipt_id,
                        route_id=resolved_route_id,
                        stage=lookup_stage,
                        step_index=target_step_index,
                        lifecycle="blocked",
                        reason_code="stage_finalization_failed",
                    )
                raise
            if not result.success:
                blocked_lifecycle = (
                    _publish_sp_stage_lifecycle_event(
                        flow_scope_id=flow_scope_id,
                        execution_receipt_id=expected_receipt_id,
                        route_id=resolved_route_id,
                        stage=lookup_stage,
                        step_index=target_step_index,
                        lifecycle="blocked",
                        reason_code="skill_execution_failed",
                    )
                    if started_lifecycle.get("state") == "durable"
                    else {
                        "schema_version": "sp-stage-lifecycle-handoff/v1",
                        "state": "deferred",
                        "persistent": False,
                        "lifecycle": "blocked",
                        "reason": "workflow_stage_started_not_durable",
                        "canonical_memory_effect": "none",
                    }
                )
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "stage": stage,
                                "success": False,
                                "errors": result.errors,
                                "collaboration_lifecycle": {
                                    "started": started_lifecycle,
                                    "blocked": blocked_lifecycle,
                                },
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            receipt_id = ""
            parent_entity_id = getattr(result, "audit_trail", {}).get("entity_id") or None
            composite_child_entity_ids: list[str] = []
            composite = COMPOSITE_SKILL_CALLS.get(lookup_stage)
            if composite is not None:
                if not expected_receipt_id:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "composite_tracking_requires_durable_receipt",
                                    "stage": lookup_stage,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                if not parent_entity_id:
                    blocked_lifecycle = (
                        _publish_sp_stage_lifecycle_event(
                            flow_scope_id=flow_scope_id,
                            execution_receipt_id=expected_receipt_id,
                            route_id=resolved_route_id,
                            stage=lookup_stage,
                            step_index=target_step_index,
                            lifecycle="blocked",
                            reason_code="composite_tracking_failed",
                        )
                        if started_lifecycle.get("state") == "durable"
                        else {
                            "schema_version": "sp-stage-lifecycle-handoff/v1",
                            "state": "deferred",
                            "persistent": False,
                            "lifecycle": "blocked",
                            "reason": "workflow_stage_started_not_durable",
                            "canonical_memory_effect": "none",
                        }
                    )
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "composite_parent_tracking_failed",
                                    "stage": lookup_stage,
                                    "collaboration_lifecycle": {
                                        "started": started_lifecycle,
                                        "blocked": blocked_lifecycle,
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                try:
                    composite_child_entity_ids = await record_attested_composite_skills(
                        engine,
                        parent_entity_id=parent_entity_id or "",
                        skill_names=list(receipt["evidence"]["invoked_skills"]),
                        task_description=task_desc,
                        receipt_id=expected_receipt_id,
                        project_id=project_id,
                        stage_session_id=str(public_stage_session_id),
                        flow_line_id=str(flow_line_id or ""),
                    )
                except (KeyError, RuntimeError, json.JSONDecodeError):
                    blocked_lifecycle = (
                        _publish_sp_stage_lifecycle_event(
                            flow_scope_id=flow_scope_id,
                            execution_receipt_id=expected_receipt_id,
                            route_id=resolved_route_id,
                            stage=lookup_stage,
                            step_index=target_step_index,
                            lifecycle="blocked",
                            reason_code="composite_tracking_failed",
                        )
                        if started_lifecycle.get("state") == "durable"
                        else {
                            "schema_version": "sp-stage-lifecycle-handoff/v1",
                            "state": "deferred",
                            "persistent": False,
                            "lifecycle": "blocked",
                            "reason": "workflow_stage_started_not_durable",
                            "canonical_memory_effect": "none",
                        }
                    )
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "composite_skill_tracking_failed",
                                    "reason": "composite_tracking_failed",
                                    "stage": lookup_stage,
                                    "collaboration_lifecycle": {
                                        "started": started_lifecycle,
                                        "blocked": blocked_lifecycle,
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
            if connection is not None:
                try:
                    receipt_id = commit_workflow_transition(
                        connection,
                        scope_id=flow_scope_id,
                        route_id=resolved_route_id,
                        step_index=target_step_index,
                        receipt=receipt,
                        current_stage=lookup_stage,
                        parent_entity_id=parent_entity_id,
                    )
                except ValueError:
                    blocked_lifecycle = (
                        _publish_sp_stage_lifecycle_event(
                            flow_scope_id=flow_scope_id,
                            execution_receipt_id=expected_receipt_id,
                            route_id=resolved_route_id,
                            stage=lookup_stage,
                            step_index=target_step_index,
                            lifecycle="blocked",
                            reason_code="workflow_transition_conflict",
                        )
                        if started_lifecycle.get("state") == "durable"
                        else {
                            "schema_version": "sp-stage-lifecycle-handoff/v1",
                            "state": "deferred",
                            "persistent": False,
                            "lifecycle": "blocked",
                            "reason": "workflow_stage_started_not_durable",
                            "canonical_memory_effect": "none",
                        }
                    )
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "execution_receipt_conflict",
                                    "reason": "workflow_receipt_conflict",
                                    "stage": lookup_stage,
                                    "collaboration_lifecycle": {
                                        "started": started_lifecycle,
                                        "blocked": blocked_lifecycle,
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
            set_current_stage(
                lookup_stage,
                stage_session_id=flow_scope_id,
                parent_entity_id=parent_entity_id,
                engine=None if connection is not None else engine,
                route_id=resolved_route_id,
                current_step_index=target_step_index,
            )
            result_data = attach_stage_guidance(
                result.data if isinstance(result.data, dict) else {},
                lookup_stage,
                closed=result.data.get("closed") if isinstance(result.data, dict) else None,
                route_id=resolved_route_id,
            )
            if composite is not None:
                result_data["attested_composite_skills"] = list(
                    receipt["evidence"]["invoked_skills"]
                )
                result_data["composite_child_entity_ids"] = composite_child_entity_ids
            result_data["collaboration_handoff"] = _publish_sp_stage_collaboration_events(
                flow_scope_id=flow_scope_id,
                execution_receipt_id=receipt_id,
                route_id=resolved_route_id,
                stage=lookup_stage,
                step_index=target_step_index,
            )
            result_data["collaboration_lifecycle"] = {
                "started": started_lifecycle,
                "blocked": {
                    "state": "not_applicable",
                    "lifecycle": "blocked",
                    "canonical_memory_effect": "none",
                },
            }
            guidance_level = str(arguments.get("guidance_level") or "summary").strip().casefold()
            if guidance_level not in {"summary", "full"}:
                guidance_level = "summary"
            result_data = _project_sp_stage_data(result_data, guidance_level)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "stage": stage,
                            "success": True,
                            "stage_session_id": public_stage_session_id,
                            "flow_line_id": flow_line_id,
                            "flow_scope_id": flow_scope_id,
                            "invocation_source": invocation_source,
                            "invocation_source_authenticated": False,
                            "execution_status": "completed",
                            "execution_receipt_id": receipt_id,
                            "collaboration_event_status": result_data["collaboration_handoff"][
                                "state"
                            ],
                            "guidance_level": guidance_level,
                            "data": result_data,
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        elif name == "memory_sync_files":
            from plastic_promise.mcp.tools.memory import handle_memory_sync_files

            return await handle_memory_sync_files(
                engine,
                arguments,
                _runtime_context=mutation_runtime_context,
            )

        # ── Market tools ──
        elif name == "market_list":
            from plastic_promise.mcp.tools.market import handle_market_list

            return await handle_market_list(engine, arguments)

        elif name == "market_install":
            from plastic_promise.mcp.tools.market import handle_market_install

            return await handle_market_install(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )

        elif name == "market_upgrade":
            from plastic_promise.mcp.tools.market import handle_market_upgrade

            return await handle_market_upgrade(engine, arguments)

        elif name == "market_remove":
            from plastic_promise.mcp.tools.market import handle_market_remove

            return await handle_market_remove(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )

        elif name == "market_enable":
            from plastic_promise.mcp.tools.market import handle_market_enable

            return await handle_market_enable(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )

        elif name == "market_disable":
            from plastic_promise.mcp.tools.market import handle_market_disable

            return await handle_market_disable(
                engine,
                arguments,
                _runtime_context={
                    "actor": _feedback_runtime_actor(),
                    "authority_source": "server_dispatch",
                },
            )

        elif name == "market_status":
            from plastic_promise.mcp.tools.market import handle_market_status

            return await handle_market_status(engine, arguments)

        # ── Dynamic plugin tool dispatch ──
        else:
            # Check if a loaded plugin provides this tool
            try:
                from plastic_promise.extensions.loader import PluginLoader as _PluginLoader

                _pl = _PluginLoader()
                _pl.discover()
                _pl.activate_all()
                if name in _pl.get_tools():
                    result = _pl.call_plugin_tool(name, arguments)
                    if result is not None:
                        return [
                            TextContent(
                                type="text",
                                text=json.dumps(result, ensure_ascii=False),
                            )
                        ]
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {"error": f"Plugin tool '{name}' returned no result"},
                                ensure_ascii=False,
                            ),
                        )
                    ]
            except Exception:
                pass

            runtime_status = "error"
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False),
                )
            ]
    except asyncio.CancelledError:
        runtime_status = "error"
        raise
    except Exception as e:
        runtime_status = "error"
        logging.exception(f"Tool {name} failed")
        return [
            TextContent(
                type="text", text=json.dumps({"error": str(e), "tool": name}, ensure_ascii=False)
            )
        ]
    finally:
        if workflow_flow_lock_acquired and workflow_flow_lock is not None:
            workflow_flow_lock.release()
        _record_tool_runtime_event(engine, runtime_ctx, runtime_status)
        reset_call_span_start(span_start_token)


# ===================================================================
# Resources
# ===================================================================


@server.list_resources()
async def list_resources() -> list[Resource]:
    """声明 MCP Resources — 系统数据的只读视图"""
    return [
        Resource(
            uri="plastic-promise://principles",
            name="核心原则列表",
            description="13 条核心原则的完整定义",
            mimeType="application/json",
        ),
        Resource(
            uri="plastic-promise://systems",
            name="九大数字身体系统",
            description="九大系统的名称、类比、成熟度和模块组成",
            mimeType="application/json",
        ),
        Resource(
            uri="plastic-promise://trust-history",
            name="信任分变化历史",
            description="信任分随时间变化的时序数据",
            mimeType="application/json",
        ),
        Resource(
            uri="plastic-promise://audit-latest",
            name="最新审计报告",
            description="最近一次七维度审计的完整报告",
            mimeType="application/json",
        ),
        Resource(
            uri="plastic-promise://memory-stats",
            name="记忆池统计",
            description="记忆总量、健康/衰退分布、类型分布、worth 分布",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    """读取 MCP Resource"""
    if uri == "plastic-promise://principles":
        return json.dumps(CORE_PRINCIPLES, ensure_ascii=False, indent=2)
    elif uri == "plastic-promise://systems":
        from plastic_promise.core.constants import DIGITAL_BODY_SYSTEMS

        return json.dumps(DIGITAL_BODY_SYSTEMS, ensure_ascii=False, indent=2)
    elif uri == "plastic-promise://trust-history":
        return json.dumps({"trust_history": [], "current_trust": 0.60}, ensure_ascii=False)
    elif uri == "plastic-promise://audit-latest":
        return json.dumps({"message": "No audit run yet"}, ensure_ascii=False)
    elif uri == "plastic-promise://memory-stats":
        return json.dumps({"total_memories": 0, "healthy": 0, "decaying": 0}, ensure_ascii=False)
    return json.dumps({"error": f"Unknown resource: {uri}"})


# ===================================================================
# Prompts
# ===================================================================


@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    """声明 MCP Prompts — 标准操作流程模板"""
    return [
        Prompt(
            name="run-full-audit",
            description="执行完整的七维度审计流程",
            arguments=[
                {"name": "scope", "description": "审计范围: full/quick"},
            ],
        ),
        Prompt(
            name="check-principle-alignment",
            description="检查当前决策是否与核心原则对齐",
            arguments=[
                {"name": "decision", "description": "当前决策描述"},
            ],
        ),
        Prompt(
            name="daily-reflection",
            description="每日 SCARF 自省 + 记忆演化检查",
            arguments=[],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    """获取 MCP Prompt 模板"""
    if name == "run-full-audit":
        scope = (arguments or {}).get("scope", "full")
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=f"请执行{scope}范围的七维度审计。\n\n"
                    f"审计维度：原则联想/记忆供应/约束合规/反馈闭环/信任校准/原则继承/安全追溯。\n"
                    f"返回每个维度的评分（0.0-1.0）、发现的问题、建议的修复措施。\n"
                    f"如果评分低于 0.60，标记为 P0 并立即告警。",
                )
            ]
        )
    elif name == "check-principle-alignment":
        decision = (arguments or {}).get("decision", "")
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=f"对于以下决策，逐一检查是否与 13 条核心原则对齐：\n\n"
                    f"决策: {decision}\n\n"
                    f"对每条原则给出：[OK] 对齐 / [WARN] 部分对齐 / [FAIL] 冲突。\n"
                    f"如果冲突，说明「如果违反会怎样」的反事实预演。",
                )
            ]
        )
    elif name == "daily-reflection":
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content="执行每日 SCARF 自省。\n\n"
                    "1. 对过去 24 小时的行为进行五维度评分（Status/Certainty/Autonomy/Relatedness/Fairness）\n"
                    "2. 检查记忆池健康度：新增/衰退/GC 数量\n"
                    "3. 检查信任分变化趋势\n"
                    "4. 如有维度低于 0.50，给出改进建议",
                )
            ]
        )
    return GetPromptResult(messages=[PromptMessage(role="user", content=f"Unknown prompt: {name}")])


# ===================================================================
# 启动入口
# ===================================================================


_STREAMABLE_HTTP_FLAGS = {"--streamable-http", "--http", "--sse"}


def _parse_streamable_http_port(argv: list[str]) -> tuple[str | None, int]:
    for flag in _STREAMABLE_HTTP_FLAGS:
        if flag not in argv:
            continue
        try:
            idx = argv.index(flag)
            return flag, int(argv[idx + 1]) if idx + 1 < len(argv) else 9020
        except (ValueError, IndexError):
            return flag, 9020
    return None, 9020


async def main():
    """MCP Server 启动入口 — 支持 stdio 和 Streamable HTTP 双模式。"""
    import sys

    # The canonical backend is never an inference execution plane.  Set this
    # before any lazy engine/provider construction so cloud provider factories
    # fail closed even when a deployment manifest forgot the same declaration.
    configured_endpoint_role = os.environ.setdefault("PP_ENDPOINT_ROLE", "pp-server-backend")
    if configured_endpoint_role != "pp-server-backend":
        raise RuntimeError("mcp_endpoint_role_mismatch")
    configure_default_environment(_PROJECT_ROOT)
    # A directly launched server does not pass through ``init_and_start.py``.
    # Apply coupled switches for an explicit mode so reported and effective
    # runtime capabilities cannot diverge.
    explicit_mode = os.environ.get("PLASTIC_RUNTIME_MODE")
    applied_mode = None
    if explicit_mode:
        from plastic_promise.launcher.runtime_mode import apply_runtime_mode

        applied_mode = apply_runtime_mode(explicit_mode)

    transport_flag, port = _parse_streamable_http_port(sys.argv)
    if transport_flag:
        # Streamable HTTP mode — Codex and modern MCP clients use /mcp.
        os.environ.setdefault("PLASTIC_MCP_TRANSPORT", "streamable_http")
        if transport_flag == "--sse":
            os.environ.setdefault("PLASTIC_MCP_LEGACY_TRANSPORT_ALIAS", "sse")
        # Full-mode initialization can invoke the configured cloud embedding
        # provider.  It must not happen before Uvicorn owns the loopback port:
        # an unavailable provider used to leave systemd "active" while no
        # health endpoint could answer.  Streamable HTTP schedules it after
        # startup and reports a bounded initializing state until it finishes.
        startup_warmup_mode = (
            applied_mode.key
            if applied_mode is not None and applied_mode.runs_lancedb_warmup
            else None
        )
        if startup_warmup_mode is None:
            await run_streamable_http(port)
        else:
            await run_streamable_http(port, startup_warmup_mode=startup_warmup_mode)
    else:
        if applied_mode is not None and applied_mode.runs_lancedb_warmup:
            refresh = await asyncio.to_thread(
                get_engine().refresh_runtime_mode,
                initialize_heavy=True,
                synchronize_index=True,
            )
            index_sync = refresh.get("index_sync") if isinstance(refresh, dict) else None
            if not isinstance(index_sync, dict) or not index_sync.get("ready"):
                # Stdio has no independent health endpoint to publish an
                # initializing state, so retain its historical eager warmup
                # semantics while keeping Streamable HTTP non-blocking.
                logging.warning(
                    "Runtime mode %s started degraded: derived index requires maintenance (%s)",
                    applied_mode.key,
                    index_sync.get("status") if isinstance(index_sync, dict) else "unknown",
                )
        # stdio 模式 — 供 Claude Code 本地调用
        os.environ.setdefault("PLASTIC_MCP_TRANSPORT", "stdio")
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options()
            await server.run(
                read_stream,
                write_stream,
                init_options,
                raise_exceptions=False,
            )


async def run_streamable_http(
    port: int = 9020,
    *,
    startup_warmup_mode: str | None = None,
):
    """启动 Streamable HTTP MCP 传输 — 多 Agent 共享记忆入口。

    Codex 和现代 MCP 客户端使用 /mcp。旧 /sse 和 /messages 端点保留为
    legacy 兼容入口，供尚未迁移的外部 Agent 继续连接。
    """
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    from plastic_promise.mcp.dashboard_v2 import DashboardSettings, create_dashboard_v2_routes

    logger = logging.getLogger("plastic-promise-streamable-http")
    _install_windows_client_disconnect_filter(logger)
    import time as _time

    start_time = _time.time()

    # Service managers poll /health every few seconds.  Identity validation
    # includes the configured embedding probe, so caching it prevents a cloud
    # provider call (and its cost) on every liveness request.  Cache failures
    # too; an unavailable provider must not turn polling into a retry storm.
    try:
        configured_health_identity_cache_ttl = float(
            os.environ.get("PP_HEALTH_IDENTITY_CACHE_SECONDS", "30")
        )
        if not math.isfinite(configured_health_identity_cache_ttl):
            raise ValueError("health_identity_cache_ttl_not_finite")
        health_identity_cache_ttl = min(max(configured_health_identity_cache_ttl, 1.0), 300.0)
    except (TypeError, ValueError):
        health_identity_cache_ttl = 30.0
    health_identity_cache: dict[str, Any] = {
        "expires_at": 0.0,
        "config_key": "",
        "identity": None,
        "error": None,
    }
    health_identity_cache_lock = asyncio.Lock()
    try:
        configured_health_identity_timeout = float(
            os.environ.get("PP_HEALTH_IDENTITY_TIMEOUT_SECONDS", "10")
        )
        if not math.isfinite(configured_health_identity_timeout):
            raise ValueError("health_identity_timeout_not_finite")
        health_identity_timeout = min(max(configured_health_identity_timeout, 0.05), 60.0)
    except (TypeError, ValueError):
        health_identity_timeout = 10.0
    startup_warmup: dict[str, Any] = {
        "mode": str(startup_warmup_mode or ""),
        "state": "pending" if startup_warmup_mode else "not_requested",
        "error": None,
    }
    startup_warmup_task: asyncio.Task[None] | None = None

    sse = SseServerTransport("/messages")
    streamable_http = StreamableHTTPSessionManager(app=server)

    # Project-aware notification fan-out — issue transitions and /notify publish here.
    import asyncio as _asyncio

    global _notify_queue
    _notify_queue = _ProjectNotificationHub()

    class _NoOpResponse(Response):
        """Sentinel response — the SSE transport already handled the send via request._send."""

        async def __call__(self, scope, receive, send):
            pass  # response already sent by SSE transport — do nothing

    async def handle_sse(request: Request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as (
            read_stream,
            write_stream,
        ):
            init_options = server.create_initialization_options()
            await server.run(read_stream, write_stream, init_options, raise_exceptions=False)
        return _NoOpResponse()

    async def handle_events(request: Request):
        """SSE event stream — push notifications to connected clients.

        Uses raw ASGI send to avoid Starlette StreamingResponse lifecycle conflicts.
        """
        import json as _json

        from starlette.responses import JSONResponse

        try:
            task_scope = _task_event_subscription_scope(request.query_params)
        except ValueError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )

        from plastic_promise.core.task_event_bus import get_event_bus

        assert task_scope is not None
        connection_queue = _asyncio.Queue()
        task_bus = get_event_bus()

        async def task_send(payload: str) -> None:
            connection_queue.put_nowait(payload)

        _notify_queue.register(task_scope[0], connection_queue)
        task_bus.register(task_scope[0], task_scope[1], task_send)

        try:
            # Send SSE headers manually
            await request._send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-cache"),
                        (b"connection", b"keep-alive"),
                    ],
                }
            )

            connected = {
                "type": "connected",
                "project_id": task_scope[0],
                "agent_name": task_scope[1],
            }
            body = f"data: {_json.dumps(connected)}\n\n".encode()
            await request._send({"type": "http.response.body", "body": body, "more_body": True})

            # One private queue combines project-scoped TaskBus and general
            # notifications, so subscribers never compete for a global item.
            while True:
                disconnected = await request.is_disconnected()
                if disconnected:
                    break
                try:
                    payload = await _asyncio.wait_for(connection_queue.get(), timeout=1)
                    if isinstance(payload, str):
                        body = f"data: {payload}\n\n".encode()
                    else:
                        body = f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                    await request._send(
                        {"type": "http.response.body", "body": body, "more_body": True}
                    )
                except _asyncio.TimeoutError:
                    body = b'data: {"type":"heartbeat"}\n\n'
                    try:
                        await request._send(
                            {"type": "http.response.body", "body": body, "more_body": True}
                        )
                    except Exception:
                        break
        finally:
            task_bus.unregister(task_scope[0], task_scope[1], task_send)
            _notify_queue.unregister(task_scope[0], connection_queue)
            with suppress(Exception):
                await request._send({"type": "http.response.body", "body": b"", "more_body": False})
        return _NoOpResponse()

    async def handle_notify(request: Request):
        """接收外部推送并广播到 SSE /events。Daemon/Worker 状态变更入口。"""
        import json as _json

        from starlette.responses import JSONResponse

        try:
            body = await request.body()
            event = _json.loads(body.decode())
            event_type = event.get("type")
            runtime_tool = _NOTIFICATION_RUNTIME_TOOL_BY_EVENT.get(event_type)
            runtime_authority = _mutation_runtime_context(runtime_tool) if runtime_tool else None
            event = _notification_event_with_project(event, runtime_authority)
            response = await _handle_notification_event(
                _notify_queue,
                event,
                engine=get_engine() if runtime_tool else None,
                runtime_authority=runtime_authority,
            )
            return JSONResponse(response)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def health(request):
        from starlette.responses import JSONResponse

        warmup_state = str(startup_warmup["state"])
        if warmup_state in {"pending", "running"}:
            return JSONResponse(
                {
                    "status": "starting",
                    "identity_valid": False,
                    "initializing": True,
                    "initialization": {
                        "mode": startup_warmup["mode"],
                        "state": warmup_state,
                    },
                    "uptime": round(_time.time() - start_time, 1),
                    "pid": os.getpid(),
                    "source_root": _SOURCE_ROOT,
                    "source_revision": _SOURCE_REVISION or "",
                },
                status_code=503,
            )

        now = _time.monotonic()
        config_key = _health_identity_config_key()
        async with health_identity_cache_lock:
            cache_valid = health_identity_cache["config_key"] == config_key and now < float(
                health_identity_cache["expires_at"]
            )
            if cache_valid:
                identity = health_identity_cache["identity"]
                cached_error = health_identity_cache["error"]
            else:
                # Keep the lock through the synchronous probe.  This is a
                # short-lived critical section, and collapses simultaneous
                # cache misses into one provider call.
                try:
                    identity = await asyncio.wait_for(
                        asyncio.to_thread(_server_process_identity),
                        timeout=health_identity_timeout,
                    )
                except asyncio.TimeoutError:
                    cached_error = "health_identity_probe_timeout"
                    identity = None
                except (FusionConfigurationError, RuntimeError, ValueError) as exc:
                    cached_error = str(exc)
                    identity = None
                else:
                    cached_error = None
                health_identity_cache.update(
                    {
                        "expires_at": _time.monotonic() + health_identity_cache_ttl,
                        "config_key": config_key,
                        "identity": identity,
                        "error": cached_error,
                    }
                )

        if cached_error is not None:
            return JSONResponse(
                {
                    "status": "error",
                    "identity_valid": False,
                    "identity_error": cached_error,
                    "uptime": round(_time.time() - start_time, 1),
                    "pid": os.getpid(),
                    "source_root": _SOURCE_ROOT,
                    "source_revision": _SOURCE_REVISION or "",
                },
                status_code=503,
            )
        return JSONResponse(
            {"status": "ok", "uptime": round(_time.time() - start_time, 1), **identity}
        )

    async def api_stats(request):
        """Return memory pool + body system statistics."""
        import json as _json

        from starlette.responses import JSONResponse

        try:
            engine = get_engine()
            stats_raw = engine.memory_stats_json()
            stats = _json.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
            from plastic_promise.core.constants import DIGITAL_BODY_SYSTEMS

            systems = {}
            for k, v in DIGITAL_BODY_SYSTEMS.items():
                systems[k] = {
                    "name": v.get("name", k),
                    "maturity": v.get("maturity", 0.0),
                }
            return JSONResponse(
                {
                    "memory": stats,
                    "body_systems": systems,
                    "uptime": round(_time.time() - start_time, 1),
                    "version": PLASTIC_PROMISE_VERSION,
                }
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_issues(request):
        """Return active issue list."""
        import json as _json

        from starlette.responses import JSONResponse

        try:
            engine = get_engine()
            from plastic_promise.mcp.tools.management import handle_issue_list

            result = await handle_issue_list(engine, {})
            data = _json.loads(result[0].text) if result else {"issues": []}
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_trust(request):
        """Return trust/defense status."""
        import json as _json

        from starlette.responses import JSONResponse

        try:
            engine = get_engine()
            from plastic_promise.mcp.tools.audit_defense import handle_audit_run, handle_defense

            result = await handle_defense(engine, {"action": "get"})
            data = _json.loads(result[0].text) if result else {}
            # Add audit summary
            try:
                audit_result = await handle_audit_run(engine, {"action": "report"})
                audit_data = _json.loads(audit_result[0].text) if audit_result else {}
            except Exception:
                audit_data = {"message": "No audit run yet"}
            data["audit_summary"] = audit_data
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_skill_track(request):
        """Lightweight HTTP endpoint for skill_auto_track (used by hook scripts)."""
        import json as _json

        from starlette.responses import JSONResponse

        try:
            body = await request.json()
            engine = get_engine()
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_auto_track

            result = await handle_skill_auto_track(
                engine,
                {
                    "phase": body.get("phase", "start"),
                    "skill_name": body.get("skill_name", ""),
                    "stage_session_id": body.get("stage_session_id") or body.get("stage_id"),
                    "flow_line_id": body.get("flow_line_id") or body.get("flow_id"),
                    "project_id": body.get("project_id"),
                },
            )
            data = _json.loads(result[0].text) if result else {}
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def dashboard(request):
        """Serve the monitoring dashboard HTML page."""
        from starlette.responses import HTMLResponse

        html = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Plastic Promise Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#c9d1d9;padding:24px}
h1{font-size:20px;margin-bottom:4px}
.status{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}
.status-ok{background:#3fb950}.status-err{background:#f85149}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-top:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.card h3{font-size:13px;color:#8b949e;margin-bottom:8px;text-transform:uppercase}
.card .value{font-size:28px;font-weight:700}
.bar{margin-top:8px;height:6px;border-radius:3px;background:#21262d;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;transition:width .5s}
.bar-high{background:#3fb950}.bar-mid{background:#d29922}.bar-low{background:#f85149}
.section{margin-top:24px}
.section h2{font-size:16px;border-bottom:1px solid #30363d;padding-bottom:8px;margin-bottom:12px}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #21262d;font-size:13px}
th{color:#8b949e}
.tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px}
.tag-ok{background:#1b3823;color:#3fb950}.tag-warn{background:#332b00;color:#d29922}
.footer{color:#484f58;font-size:12px;margin-top:32px}
</style>
</head>
<body>
<h1><span class="status status-ok" id="status-dot"></span>Plastic Promise Dashboard <small style="color:#8b949e">v__PLASTIC_PROMISE_VERSION__</small></h1>

<div class="grid" id="stats-grid">
  <div class="card"><h3>Memories</h3><div class="value" id="mem-total">-</div></div>
  <div class="card"><h3>Decaying</h3><div class="value" id="mem-decaying">-</div></div>
  <div class="card"><h3>Trust Score</h3><div class="value" id="trust-score">-</div></div>
  <div class="card"><h3>Active Issues</h3><div class="value" id="issues-count">-</div></div>
</div>

<div class="section"><h2>Body Systems</h2>
<div id="body-systems"></div>
</div>

<div class="section"><h2>Defense</h2>
<div id="defense-info"></div>
</div>

<div class="section"><h2>Audit</h2>
<div id="audit-info"></div>
</div>

<div class="footer">Auto-refreshes every 5s &middot; Plastic Promise</div>

<script>
async function fetchJSON(url) {
  try { const r = await fetch(url); return r.ok ? r.json() : null; }
  catch { return null; }
}

function barColor(v) { return v>=0.7?'bar-high':v>=0.5?'bar-mid':'bar-low'; }

async function refresh() {
  const [stats, issues, trust] = await Promise.all([
    fetchJSON('/api/stats'), fetchJSON('/api/issues'), fetchJSON('/api/trust')
  ]);

  if (!stats) { document.getElementById('status-dot').className='status status-err'; return; }
  document.getElementById('status-dot').className='status status-ok';

  document.getElementById('mem-total').textContent = stats.memory?.total || 0;
  document.getElementById('mem-decaying').textContent = stats.memory?.decaying || 0;

  // Body systems
  const systems = stats.body_systems || {};
  let sysHTML = '';
  for (const [key, s] of Object.entries(systems)) {
    const pct = Math.round(s.maturity*100);
    sysHTML += `<div style="display:flex;align-items:center;margin-bottom:6px">
      <span style="width:140px;font-size:13px">${s.name}</span>
      <div class="bar" style="flex:1"><div class="bar-fill ${barColor(s.maturity)}" style="width:${pct}%"></div></div>
      <span style="width:40px;text-align:right;font-size:13px">${pct}%</span></div>`;
  }
  document.getElementById('body-systems').innerHTML = sysHTML;

  // Trust
  if (trust) {
    document.getElementById('trust-score').textContent = (trust.trust||0).toFixed(2);
    const tier = trust.tier || 'unknown';
    document.getElementById('defense-info').innerHTML = `
      <span class="tag tag-${tier==='high'?'ok':'warn'}">${tier} tier</span>
      <span style="margin-left:12px">Target: ${trust.target||'default'}</span>`;
  }

  // Issues
  if (issues) {
    const count = issues.count || issues.issues?.length || 0;
    document.getElementById('issues-count').textContent = count;
  }

  // Audit
  if (trust?.audit_summary) {
    document.getElementById('audit-info').innerHTML = '<pre style="font-size:12px;color:#8b949e">' +
      JSON.stringify(trust.audit_summary, null, 2).slice(0, 500) + '</pre>';
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
        return HTMLResponse(html.replace("__PLASTIC_PROMISE_VERSION__", PLASTIC_PROMISE_VERSION))

    async def shutdown():
        logger.info("Shutting down Plastic Promise Streamable HTTP server...")
        if _engine is not None:
            from plastic_promise.core.structured_memory_fusion import (
                close_structured_fusion_batcher,
            )

            close_structured_fusion_batcher(_engine, timeout=5.0)
            from plastic_promise.core.proposal_promotion_jobs import (
                close_proposal_promotion_runtime,
            )
            from plastic_promise.passive_memory.semantic_pipeline import (
                close_semantic_memory_runtime,
            )

            close_semantic_memory_runtime(_engine, timeout=5.0)
            close_proposal_promotion_runtime(_engine, timeout=5.0)

    async def _run_startup_warmup() -> None:
        startup_warmup["state"] = "running"
        try:
            refresh = await asyncio.to_thread(
                get_engine().refresh_runtime_mode,
                initialize_heavy=True,
                synchronize_index=True,
            )
            index_sync = refresh.get("index_sync") if isinstance(refresh, dict) else None
            if not isinstance(index_sync, dict) or not index_sync.get("ready"):
                logging.warning(
                    "Runtime mode %s started degraded: derived index requires maintenance (%s)",
                    startup_warmup["mode"],
                    index_sync.get("status") if isinstance(index_sync, dict) else "unknown",
                )
            startup_warmup["state"] = "complete"
        except asyncio.CancelledError:
            startup_warmup["state"] = "cancelled"
            raise
        except Exception as exc:
            startup_warmup["state"] = "failed"
            startup_warmup["error"] = exc.__class__.__name__
            logging.exception(
                "Runtime mode %s startup warmup failed; MCP remains available for diagnosis",
                startup_warmup["mode"],
            )

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        async with streamable_http.run():
            # Creating the task without awaiting it lets Uvicorn bind the
            # loopback socket before full-mode cloud calls begin.  The health
            # route short-circuits with a fast 503 while this runs.
            nonlocal startup_warmup_task
            if startup_warmup_mode:
                startup_warmup_task = asyncio.create_task(_run_startup_warmup())
            try:
                yield
            finally:
                if startup_warmup_task is not None and not startup_warmup_task.done():
                    startup_warmup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await startup_warmup_task
                await shutdown()

    async def handle_messages(request: Request):
        """Wrap sse.handle_post_message as a Starlette Route endpoint.

        sse.handle_post_message is an ASGI app that sends its own response
        via request._send.  Starlette's request_response wrapper would try
        to call the return value as a Response, so we return a no-op sentinel.
        """
        await sse.handle_post_message(request.scope, request.receive, request._send)
        return _NoOpResponse()

    async def handle_mcp(request: Request):
        """Streamable HTTP MCP endpoint used by Codex and modern MCP clients."""
        await streamable_http.handle_request(request.scope, request.receive, request._send)
        return _NoOpResponse()

    dashboard_settings = DashboardSettings.from_env(bind_host="127.0.0.1")

    def _dashboard_issue_projection() -> list[dict[str, object]]:
        """Expose the existing process-local issue board to Dashboard V2.

        The route labels this as a read-only system projection because
        IssueManager records do not carry project IDs.
        """
        return get_engine().get_issue_manager().list()

    async def _dashboard_proposal_review(
        proposal_id: str,
        feedback_type: str,
        rejection_reason: str,
        project_id: str,
    ) -> dict[str, Any]:
        from plastic_promise.mcp.tools.reflection import handle_feedback_apply

        runtime_context = _mutation_runtime_context("feedback_apply")
        runtime_context["project_id"] = project_id
        response = await handle_feedback_apply(
            get_engine(),
            {
                "item_id": proposal_id,
                "feedback_type": feedback_type,
                "rejection_reason": rejection_reason,
            },
            _runtime_context=runtime_context,
        )
        if not response or not isinstance(response[0], TextContent):
            raise RuntimeError("dashboard_proposal_review_invalid_response")
        payload = json.loads(response[0].text)
        if not isinstance(payload, dict):
            raise RuntimeError("dashboard_proposal_review_invalid_response")
        return payload

    dashboard_v2_routes = create_dashboard_v2_routes(
        dashboard_settings,
        version=PLASTIC_PROMISE_VERSION,
        identity_provider=_dashboard_process_identity,
        issue_provider=_dashboard_issue_projection,
        proposal_review_provider=_dashboard_proposal_review,
    )
    dashboard_routes = dashboard_v2_routes or [Route("/dashboard", endpoint=dashboard)]

    app = Starlette(
        routes=[
            Route("/mcp", endpoint=handle_mcp, methods=["GET", "POST", "DELETE"]),
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Route("/messages", endpoint=handle_messages, methods=["POST"]),
            Route("/events", endpoint=handle_events, methods=["GET"]),
            Route("/notify", endpoint=handle_notify, methods=["POST"]),
            Route("/health", endpoint=health),
            Route("/api/stats", endpoint=api_stats),
            Route("/api/issues", endpoint=api_issues),
            Route("/api/trust", endpoint=api_trust),
            Route("/api/skill-track", endpoint=api_skill_track, methods=["POST"]),
            *dashboard_routes,
        ],
        lifespan=lifespan,
    )

    logger.info("Plastic Promise MCP Server v%s", PLASTIC_PROMISE_VERSION)
    logger.info(f"Streamable HTTP endpoint: http://127.0.0.1:{port}/mcp")
    logger.info(f"SSE endpoint: http://127.0.0.1:{port}/sse")
    logger.info(f"Health:      http://127.0.0.1:{port}/health")
    logger.info(f"PID: {os.getpid()}")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    await uvicorn.Server(config).serve()


async def run_sse(port: int = 9020):
    """Legacy alias for run_streamable_http(); prefer Streamable HTTP naming."""
    logging.getLogger("plastic-promise-streamable-http").warning(
        "run_sse() is deprecated; use run_streamable_http() instead."
    )
    await run_streamable_http(port)


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    asyncio.run(main())
