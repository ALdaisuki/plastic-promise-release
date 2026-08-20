"""Loopback-only process entry point for the local inference node."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

from .adapters import NodeModelUnavailableError
from .app import create_node_app
from .contract import (
    EmbeddingEngine,
    NodeConfigurationError,
    RerankingEngine,
    StructuredJSONEngine,
)
from .runtime import (
    NodeRuntimeConfig,
    bind_model_artifact_identity,
    create_embedding_engine,
    create_reranking_engine,
    create_structured_json_engine,
    validate_loopback_bind_host,
    validate_node_port,
)
from .support import valid_private_node_authorization


def create_runtime_app(
    config: NodeRuntimeConfig,
    *,
    authorization: str | None = None,
    embedder: EmbeddingEngine | None = None,
    reranker: RerankingEngine | None = None,
    structured_json: StructuredJSONEngine | None = None,
):
    """Build a serving app without giving it any canonical-state dependency."""

    _require_compute_node_role()
    resolved_authorization = _node_authorization_from_environment(authorization)
    identity = bind_model_artifact_identity(config)
    return create_node_app(
        identity,
        authorization=resolved_authorization,
        embedder=embedder
        or create_embedding_engine(
            config,
            embedding_artifact_sha256=identity.embedding_artifact_sha256,
        ),
        reranker=reranker or create_reranking_engine(config),
        structured_json=structured_json or create_structured_json_engine(config),
        limits=config.limits,
        max_concurrency=config.max_concurrency,
        resource_guard=config.resource_guard,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="Loopback listener override (127.0.0.1 or ::1 only).")
    parser.add_argument("--port", type=int, help="Listener port override (1024–65535).")
    parser.add_argument(
        "--config-check",
        action="store_true",
        help="Validate non-secret configuration without loading model weights or serving traffic.",
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info"),
        default="info",
        help="Uvicorn log level; request access logs remain disabled to minimize metadata retention.",
    )
    return parser


def _require_compute_node_role() -> None:
    """Reject direct factory use outside the compute execution plane."""

    if os.environ.get("PP_ENDPOINT_ROLE", "").strip() != "pp-compute-node":
        raise NodeConfigurationError("node_endpoint_role_mismatch")


def _node_authorization_from_environment(configured: str | None = None) -> str:
    value = os.environ.get("PP_LOCAL_NODE_AUTHORIZATION") if configured is None else configured
    if value is None or value == "":
        raise NodeConfigurationError("node_authorization_required")
    if not valid_private_node_authorization(value):
        raise NodeConfigurationError("node_authorization_invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _require_compute_node_role()
        config = NodeRuntimeConfig.from_environment()
        authorization = _node_authorization_from_environment()
        if args.host is not None:
            config = replace(config, bind_host=validate_loopback_bind_host(args.host))
        if args.port is not None:
            config = replace(config, port=validate_node_port(args.port))
        if args.config_check:
            return 0
        app = create_runtime_app(config, authorization=authorization)
    except (NodeConfigurationError, NodeModelUnavailableError) as exc:
        print(f"local inference node configuration failed: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    uvicorn.run(
        app,
        host=config.bind_host,
        port=config.port,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
