"""Serve an isolated loopback-only Dashboard browser-smoke fixture.

This helper is intentionally not an installer, service launcher, or control
plane.  It has no database, no provider settings, and no production endpoint.
It exists so the Dashboard's browser-only behavior can be exercised against
the same static bundle and a strict, deterministic control API fixture:

    python scripts/dashboard_browser_smoke.py

It binds the normal local Dashboard/control test pair (19020/19040) by
default, so the UI follows the same control-origin routing as a real local
session. Stop it with Ctrl-C after manual inspection, or run the automated
regression through ``node scripts/dashboard_browser_regression.mjs``.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

_FIXTURE_TOKEN = "browser-smoke-token"
_STATIC_DIR = (
    Path(__file__).resolve().parents[1] / "plastic_promise" / "mcp" / "dashboard_v2" / "static"
)

if TYPE_CHECKING:
    from starlette.requests import Request


def _require_fixture_token(request: Request) -> JSONResponse | None:
    if request.headers.get("authorization") != f"Bearer {_FIXTURE_TOKEN}":
        return JSONResponse({"error": {"code": "control_unauthorized"}}, status_code=401)
    return None


def _safe_config() -> dict[str, object]:
    return {
        "active_revision_id": "cfg-20260806T000000Z-000000000000",
        "config": {
            "node_routing": {
                "embedding_policy": "remote-node-first",
                "rerank_policy": "fastest-estimated",
                "allowed_node_ids": ["node-a", "node-b"],
                "embedding_pinned_node_id": "node-a",
                "rerank_pinned_node_id": "node-b",
                "accelerator_max_enabled": True,
                "accelerator_max_concurrency": 1,
                "accelerator_max_queue_depth": 32,
                "accelerator_max_daily_tasks": 100,
                "accelerator_min_free_memory_mib": 512,
            }
        },
    }


def _node(index: int) -> dict[str, object]:
    node_id = f"node-{chr(ord('a') + index)}"
    return {
        "node_id": node_id,
        "node_kind": "remote-node",
        "state": "active",
        "health": {"state": "fresh", "last_observed_at": "2026-08-06T00:00:00Z"},
        "capabilities": {"declared": ["embedding", "rerank"], "observed": ["embedding", "rerank"]},
        "embedding": {
            "model": "BAAI/bge-m3",
            "revision": "a" * 40,
            "dimension": 1024,
            "normalization": "l2",
        },
        "rerank": {"model": "BAAI/bge-reranker-v2-m3", "revision": "b" * 40},
        "capacity": {
            "queue_depth": index % 3,
            "available_slots": 4,
            "active_leases": 0,
            "max_concurrency": 4,
        },
        "latency": {
            "embedding": {"sample_count": 24, "median_ms": 10.5 + index},
            "rerank": {"sample_count": 24, "median_ms": 15.5 + index},
        },
        "quarantine_reason": None,
    }


def _nodes_projection() -> dict[str, object]:
    nodes = [_node(index) for index in range(12)]
    return {
        "schema": "plastic-promise/node-governance-dashboard/v1",
        "state": "ready",
        "summary": {
            "nodes": {"registered": len(nodes), "active": len(nodes), "quarantined": 0},
            "active_reservations": 0,
            "audit_event_count": 2,
        },
        "nodes": nodes,
        "recent_routes": [
            {
                "node_id": "node-a",
                "outcome": "completed",
                "selection_reason": "remote-node-first",
                "degradation_reason": None,
                "failure_code": None,
                "occurred_at": "2026-08-06T00:00:00Z",
            }
        ],
        "derived_work": {
            "node_inference": {
                "pending": 0,
                "retry_wait": 0,
                "leased": 0,
                "completed": 1,
                "dead": 0,
                "cancelled": 0,
            },
            "accelerator_max": {
                "pending": 2,
                "retry_wait": 1,
                "leased": 0,
                "completed": 4,
                "dead": 0,
                "cancelled": 0,
            },
        },
        "accelerator_audit": {
            "daily_admissions": 3,
            "recent_events": [
                {
                    "event": "job_lifecycle",
                    "task_kind": "semantic-dedupe",
                    "decision": "completed",
                    "reason": None,
                    "occurred_at": "2026-08-06T00:00:00Z",
                },
                {
                    "event": "attempt",
                    "task_kind": "conflict-risk",
                    "decision": "retry_wait",
                    "reason": "accelerator_provider_unavailable",
                    "occurred_at": "2026-08-06T00:00:00Z",
                },
            ],
        },
    }


async def _dashboard(request: Request) -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


async def _favicon(request: Request) -> Response:
    """Avoid a fixture-only browser 404 unrelated to Dashboard behavior."""

    return Response(status_code=204)


async def _scopes(request: Request) -> JSONResponse:
    """Return the minimum server-owned project scope envelope the shell needs."""

    return JSONResponse(
        {
            "data": {
                "default_project_id": "project:browser-smoke",
                "recommended_project_id": "project:browser-smoke",
                "scopes": [
                    {
                        "project_id": "project:browser-smoke",
                        "label": "Browser smoke fixture",
                    }
                ],
            }
        }
    )


async def _configuration(request: Request) -> JSONResponse:
    """Return feature gates without enabling unrelated Dashboard fixtures."""

    return JSONResponse(
        {
            "data": {
                "dashboard": {
                    "retrieval_explain_enabled": False,
                    "proposal_review_enabled": False,
                    "knowledge_enabled": False,
                }
            }
        }
    )


async def _session(request: Request) -> JSONResponse:
    if denied := _require_fixture_token(request):
        return denied
    return JSONResponse({"actor": "browser-smoke", "role": "operator"})


async def _safe_config_endpoint(request: Request) -> JSONResponse:
    if denied := _require_fixture_token(request):
        return denied
    return JSONResponse(_safe_config(), headers={"ETag": '"browser-smoke-v1"'})


async def _nodes(request: Request) -> JSONResponse:
    if denied := _require_fixture_token(request):
        return denied
    return JSONResponse(_nodes_projection())


async def _status(request: Request) -> JSONResponse:
    if denied := _require_fixture_token(request):
        return denied
    return JSONResponse(
        {
            "schema": "plastic-promise/control-status/v1",
            "state": "ready",
            "sqlite": {"state": "ready"},
            "inference_jobs": {"state": "ready"},
            "lancedb": {"state": "derived"},
            "maintenance": {"state": "disabled"},
        }
    )


async def _diagnostic_bundle(request: Request) -> JSONResponse:
    if denied := _require_fixture_token(request):
        return denied
    return JSONResponse(
        {
            "schema": "plastic-promise/diagnostic-bundle/v1",
            "telemetry": {
                "network_egress": "disabled",
                "export_mode": "operator_initiated",
                "redaction": "strict_allowlist_v1",
            },
            "node_governance": {"state": "ready", "node_count": 12},
        }
    )


def build_apps() -> tuple[Starlette, Starlette]:
    """Build isolated static/control apps without production dependencies."""

    dashboard = Starlette(
        routes=[
            Route("/dashboard", _dashboard),
            Route("/favicon.ico", _favicon),
            Route("/api/dashboard/v2/scopes", _scopes),
            Route("/api/dashboard/v2/configuration", _configuration),
            Mount("/dashboard/assets/v2", StaticFiles(directory=_STATIC_DIR)),
        ]
    )
    control = Starlette(
        routes=[
            Route("/api/control/v1/session", _session),
            Route("/api/control/v1/config/safe", _safe_config_endpoint),
            Route("/api/control/v1/status", _status),
            Route("/api/control/v1/nodes", _nodes),
            Route("/api/control/v1/diagnostics/bundle", _diagnostic_bundle, methods=["POST"]),
        ]
    )
    control.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:19020"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    return dashboard, control


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-port", type=int, default=19020)
    parser.add_argument("--control-port", type=int, default=19040)
    return parser


async def _serve(*, dashboard_port: int, control_port: int) -> None:
    dashboard, control = build_apps()
    dashboard_server = uvicorn.Server(
        uvicorn.Config(dashboard, host="127.0.0.1", port=dashboard_port, log_level="warning")
    )
    control_server = uvicorn.Server(
        uvicorn.Config(control, host="127.0.0.1", port=control_port, log_level="warning")
    )
    await asyncio.gather(dashboard_server.serve(), control_server.serve())


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    print(
        "Dashboard smoke fixture: "
        f"http://127.0.0.1:{args.dashboard_port}/dashboard#/control-nodes\n"
        f"Control token: {_FIXTURE_TOKEN}\n"
        "This is an isolated loopback fixture, not a Plastic Promise service."
    )
    with suppress(KeyboardInterrupt):
        asyncio.run(_serve(dashboard_port=args.dashboard_port, control_port=args.control_port))


if __name__ == "__main__":
    main()
