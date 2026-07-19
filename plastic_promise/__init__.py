"""Plastic Promise — AI 行为治理系统"""

__version__ = "0.1.18"


def main_streamable_http():
    """Console entry point for the shared Streamable HTTP MCP server."""
    import sys

    from plastic_promise.__main__ import main

    sys.argv = [sys.argv[0], "--streamable-http", "9020"]
    main()


def main_http():
    """Short alias for main_streamable_http."""
    main_streamable_http()


def main_sse():
    """Legacy console entry point; prefer main_streamable_http."""
    import sys

    from plastic_promise.__main__ import main

    sys.argv = [sys.argv[0], "--sse", "9020"]
    main()
