"""MCP Subprocess Plugin — spawn and communicate with MCP servers via stdio.

Used by PluginLoader for capability plugins declared with method: mcp.
Follows standard MCP JSON-RPC protocol over stdin/stdout.
"""

import json
import logging
import subprocess
import threading
from typing import Any

logger = logging.getLogger("plastic-promise.extensions.mcp-subprocess")

JSONRPC_VERSION = "2.0"

_QUEUE_SENTINEL = object()


class McpSubprocessPlugin:
    """Manages a plugin that exposes its own MCP server via stdio.

    Spawns the plugin binary as a subprocess, communicates via
    JSON-RPC over stdin/stdout, and auto-discovers available tools.

    Usage:
        plugin = McpSubprocessPlugin(["codebase-memory-mcp"])
        tools = plugin.discover_tools()
        result = plugin.call_tool("trace_path", {"from_name": "foo"})
        plugin.shutdown()
    """

    def __init__(self, command: list[str], timeout: int = 30):
        self._command = command
        self._timeout = timeout
        self._process: subprocess.Popen | None = None
        self._tools: dict[str, dict] = {}
        self._next_id: int = 0

    # ── Lifecycle ──

    def start(self) -> bool:
        """Spawn the MCP server subprocess. Returns True on success."""
        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return self._initialize()
        except FileNotFoundError:
            logger.debug("MCP binary not found: %s", self._command[0])
            return False
        except Exception as e:
            logger.warning("MCP subprocess start failed: %s", e)
            return False

    def shutdown(self) -> None:
        """Graceful shutdown of MCP subprocess."""
        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                if self._process.stdout:
                    self._process.stdout.close()
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass

    # ── MCP Protocol ──

    def _initialize(self) -> bool:
        """Send initialize request, receive capabilities."""
        result = self._send_request(
            "initialize",
            {
                "protocolVersion": "0.1.0",
                "clientInfo": {"name": "plastic-promise"},
            },
        )
        return result is not None

    def discover_tools(self) -> list[dict]:
        """Send tools/list via JSON-RPC, cache and return tool schemas."""
        result = self._send_request("tools/list", {})
        if not result:
            return []
        tools = result.get("tools", [])
        for tool in tools:
            self._tools[tool["name"]] = tool
        return tools

    def call_tool(self, name: str, args: dict) -> Any:
        """Send tools/call via JSON-RPC, return result content."""
        result = self._send_request(
            "tools/call",
            {
                "name": name,
                "arguments": args,
            },
        )
        if not result:
            return None
        # MCP returns content array; extract text
        content = result.get("content", [])
        if content and isinstance(content, list):
            return [c.get("text", "") for c in content if c.get("type") == "text"]
        return result

    # ── JSON-RPC Transport ──

    def _send_request(self, method: str, params: dict) -> dict | None:
        """Send a JSON-RPC request and return the result, with timeout."""
        if not self._process or self._process.poll() is not None:
            return None

        self._next_id += 1
        request = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        try:
            payload = json.dumps(request) + "\n"
            if self._process.stdin is None:
                return None
            self._process.stdin.write(payload)
            self._process.stdin.flush()
            if self._process.stdout is None:
                return None

            # Read with timeout using a daemon thread + queue
            from queue import Queue

            q: Queue[str | None] = Queue()

            def _read() -> None:
                try:
                    line = self._process.stdout.readline()  # type: ignore[union-attr]
                    q.put(line if line else None)
                except Exception:
                    q.put(None)

            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout=self._timeout)
            if t.is_alive():
                # Timeout: kill the subprocess so we don't leak it
                logger.warning(
                    "MCP request %s timed out after %ds, killing subprocess",
                    method,
                    self._timeout,
                )
                try:
                    self._process.kill()
                except Exception:
                    pass
                t.join(timeout=2)
                return None

            response_line = q.get_nowait() if not q.empty() else None
            if not response_line:
                return None
            response = json.loads(response_line)
            if "error" in response:
                logger.debug(
                    "MCP error: %s",
                    response["error"].get("message", ""),
                )
                return None
            return response.get("result", {})
        except (json.JSONDecodeError, BrokenPipeError, OSError) as e:
            logger.debug("MCP JSON-RPC transport error: %s", e)
            return None
