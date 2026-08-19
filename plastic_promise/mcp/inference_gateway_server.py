"""Dedicated loopback listener for the authenticated inference gateway."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from plastic_promise.launcher.default_environment import configure_default_environment
from plastic_promise.mcp.inference_gateway import (
    InferenceGatewayConfigurationError,
    InferenceGatewaySettings,
    create_inference_gateway_app,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9030)
    return parser


async def serve(port: int = 9030) -> None:
    if isinstance(port, bool) or not 1 <= port <= 65_535:
        raise InferenceGatewayConfigurationError("inference_gateway_port_invalid")
    configure_default_environment(str(_PROJECT_ROOT))
    os.environ.setdefault("PP_ENDPOINT_ROLE", "pp-compute-node")
    settings = InferenceGatewaySettings.from_env(bind_host="127.0.0.1")
    if not settings.enabled:
        # Local governed-node deployments deliberately do not run the cloud
        # gateway.  A clean no-op lets systemd represent this as inactive /
        # skipped instead of a failed service that retries forever.
        logging.getLogger(__name__).info("inference_gateway_disabled")
        return
    app = create_inference_gateway_app(settings)
    http_limit = min(128, max(16, settings.max_concurrency * 4))
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        limit_concurrency=http_limit,
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
