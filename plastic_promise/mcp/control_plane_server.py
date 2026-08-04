"""Dedicated loopback listener for the Plastic Promise control plane."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn

from plastic_promise.launcher.default_environment import configure_default_environment
from plastic_promise.mcp.control_plane import ControlPlaneSettings, create_control_plane_app

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9040)
    return parser


async def serve(port: int = 9040) -> None:
    if isinstance(port, bool) or not 1 <= port <= 65_535:
        raise ValueError("control_port_invalid")
    configure_default_environment(str(_PROJECT_ROOT))
    settings = ControlPlaneSettings.from_env(bind_host="127.0.0.1")
    if not settings.enabled:
        raise ValueError("control_plane_disabled")
    app = create_control_plane_app(settings)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
        limit_concurrency=64,
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=5,
    )
    await uvicorn.Server(config).serve()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    asyncio.run(serve(arguments.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
