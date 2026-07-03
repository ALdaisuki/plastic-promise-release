"""Extension Points — Protocol definitions for Plastic Promise plugins.

Each extension point is a Python Protocol. Plugins implement one or more
protocols and declare which in their pack.yml manifest. PluginLoader
discovers, validates, and activates plugins at startup.

Usage:
    from plastic_promise.extensions import HookProvider, ToolProvider

    class MyPlugin:
        slots = ["on_before_dispatch"]
        def execute(self, slot: str, context: dict) -> dict:
            return {"result": "ok"}
"""

from typing import Any, Protocol


class HookProvider(Protocol):
    """Workflow hooks — execute at named slots in the SuperPowers pipeline."""

    slots: list[str]

    def execute(self, slot: str, context: dict) -> dict:
        """Execute the hook at the given slot. Returns dict with results."""
        ...


class ToolProvider(Protocol):
    """Register new MCP tools via MCP stdio subprocess."""

    tools: list[dict]

    def handle(self, tool_name: str, args: dict) -> Any:
        """Handle an MCP tool invocation."""
        ...


class EmbedderProvider(Protocol):
    """Replace the embedding backend."""

    def embed(self, text: str) -> list[float]: ...
    def batch_embed(self, texts: list[str]) -> list[list[float]]: ...


class StorageProvider(Protocol):
    """Replace the vector/record storage backend."""

    def store(self, record: dict) -> str: ...
    def query(self, vec: list[float], top_k: int) -> list[dict]: ...


class NotifierProvider(Protocol):
    """Event notifications (slack, email, webhook, etc.)."""

    channels: list[str]

    def send(self, channel: str, message: str) -> None: ...


class DispatchProvider(Protocol):
    """Subagent orchestration at workflow nodes."""

    dispatch_points: list[str]

    def spawn(self, task: dict) -> dict: ...


KNOWN_EXTENSION_POINTS = {
    "hook": HookProvider,
    "tool": ToolProvider,
    "embedder": EmbedderProvider,
    "storage": StorageProvider,
    "notifier": NotifierProvider,
    "dispatch": DispatchProvider,
}
