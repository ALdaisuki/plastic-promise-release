"""Plastic Promise — 顶层入口模块。

运行方式：
    # 本地 stdio 模式 (Claude Code / IDE)
    python -m plastic_promise

    # SSE 多 Agent 共享模式
    python -m plastic_promise --sse 9020
"""

import sys
import asyncio
import logging


def main():
    # 转发到 MCP Server
    if "--sse" in sys.argv:
        try:
            idx = sys.argv.index("--sse")
            port = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 9020
        except (ValueError, IndexError):
            port = 9020
        # 注入参数到 server 模块
        sys.argv = [sys.argv[0], "--sse", str(port)]
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
