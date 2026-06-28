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

    # Pipeline backlog check — auto-process if items pending
    pipeline_processed = 0
    try:
        if engine is not None and hasattr(engine, '_fuzzy_buffer') and engine._fuzzy_buffer is not None:
            pl_stats = engine._fuzzy_buffer.stats()
            if pl_stats["total"] > 0:
                result = engine._fuzzy_buffer.process_pipeline()
                pipeline_processed = result.get("total_processed", 0)
            checks["memory_pipeline"] = {
                "status": "ok",
                "backlog": pl_stats["total"],
                "processed": pipeline_processed,
            }
    except Exception as e:
        checks["memory_pipeline"] = {"status": "error", "message": str(e)}

    all_ok = all(c.get("status") == "ok" for c in checks.values())

    return {
        "timestamp": now,
        "healthy": all_ok,
        "checks": checks,
        "pipeline_processed": pipeline_processed,
    }
