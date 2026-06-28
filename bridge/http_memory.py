"""
Plastic Promise HTTP Wrapper — REST API for memory operations.

Provides HTTP endpoints so neko_adapter.py and other Python clients
can access shared memory without going through MCP stdio.

Endpoints:
  POST /memory/store   — store a memory
  POST /memory/recall  — recall memories by query
  GET  /memory/stats   — memory pool statistics
  POST /context/supply — get context for a task

Start: python bridge/http_memory.py --port 48920
"""

import json
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse

DEFAULT_PORT = int(os.environ.get("PLASTIC_PROMISE_HTTP_PORT", "48920"))
MCP_SERVER_CMD = ["python", "-m", "plastic_promise.mcp.server"]


class MemoryHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler that forwards to Plastic Promise MCP via subprocess calls."""

    def _call_mcp(self, tool_name: str, args: dict) -> dict:
        """Call an MCP tool via subprocess and return JSON result."""
        request = json.dumps({
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": args,
            },
        })

        try:
            result = subprocess.run(
                MCP_SERVER_CMD,
                input=request,
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "PYTHONPATH": "F:/Agent/plastic-promise"},
            )
            if result.returncode != 0:
                return {"error": result.stderr.strip()}
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"result": result.stdout.strip()}
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
        except Exception as e:
            return {"error": str(e)}

    def _send_json(self, data: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length > 0 else {}

        route_map = {
            "/memory/store": ("memory_store", body),
            "/memory/recall": ("memory_recall", body),
            "/context/supply": ("context_supply", body),
        }

        if path in route_map:
            tool_name, args = route_map[path]
            result = self._call_mcp(tool_name, args)
            self._send_json(result)
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/memory/stats":
            result = self._call_mcp("memory_stats", {})
            self._send_json(result)
        elif path == "/health":
            self._send_json({"status": "ok", "service": "plastic-promise-http"})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        print(f"[http-memory] {args[0]}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Plastic Promise HTTP Wrapper")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), MemoryHTTPHandler)
    print(f"[http-memory] Plastic Promise HTTP wrapper on http://127.0.0.1:{args.port}")
    print(f"[http-memory] Endpoints: /memory/store /memory/recall /memory/stats /context/supply")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[http-memory] Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
