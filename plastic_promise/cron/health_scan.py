"""Health scan — periodic system health check across all subsystems.

Runs every 6 hours. Checks SQLite, entity graph, and Ollama connectivity.
"""

import datetime
from typing import Any
import requests


def run(engine: Any = None, ollama_host: str = "http://127.0.0.1:11434") -> dict:
    """Run health scan across all subsystems.

    Args:
        engine: ContextEngine instance.
        ollama_host: Ollama API host.

    Returns:
        dict with per-subsystem health status.
    """
    now = datetime.datetime.now().isoformat()
    checks = {}

    # SQLite check
    try:
        if engine:
            _ = engine.memory_count
            checks["sqlite"] = {"status": "ok", "message": "connected"}
        else:
            checks["sqlite"] = {"status": "unknown", "message": "no engine"}
    except Exception as e:
        checks["sqlite"] = {"status": "error", "message": str(e)}

    # EntityGraph check
    try:
        if engine:
            graph = engine.get_graph()
            checks["entity_graph"] = {
                "status": "ok",
                "nodes": graph.node_count,
                "edges": graph.edge_count,
            }
        else:
            checks["entity_graph"] = {"status": "unknown", "message": "no engine"}
    except Exception as e:
        checks["entity_graph"] = {"status": "error", "message": str(e)}

    # Ollama check
    try:
        resp = requests.get(f"{ollama_host}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        checks["ollama"] = {"status": "ok", "models": models}
    except Exception as e:
        checks["ollama"] = {"status": "error", "message": str(e)}

    all_ok = all(c.get("status") == "ok" for c in checks.values())

    return {
        "timestamp": now,
        "healthy": all_ok,
        "checks": checks,
    }
