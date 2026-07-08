"""Plastic Promise — 顶层入口模块。

运行方式：
    # 本地 stdio 模式 (Claude Code / IDE)
    python -m plastic_promise

    # Streamable HTTP 多 Agent 共享模式
    python -m plastic_promise --streamable-http 9020
    python -m plastic_promise --sse 9020  # legacy alias
"""

import asyncio
import logging
import sys


_STREAMABLE_HTTP_FLAGS = {"--streamable-http", "--http", "--sse"}


def _extract_streamable_http_port(argv: list[str]) -> tuple[bool, int]:
    for flag in _STREAMABLE_HTTP_FLAGS:
        if flag not in argv:
            continue
        try:
            idx = argv.index(flag)
            return True, int(argv[idx + 1]) if idx + 1 < len(argv) else 9020
        except (ValueError, IndexError):
            return True, 9020
    return False, 9020


def main():
    # 转发到 MCP Server
    use_http, port = _extract_streamable_http_port(sys.argv)
    if use_http:
        # 注入 canonical 参数到 server 模块；--sse 在 server 层仍保留兼容。
        sys.argv = [sys.argv[0], "--streamable-http", str(port)]
    else:
        sys.argv = [sys.argv[0]]

    from plastic_promise.mcp.server import main as server_main

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    asyncio.run(server_main())


if __name__ == "__main__":
    main()
