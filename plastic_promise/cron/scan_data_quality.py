"""scan_data_quality -- 6-dimension data quality health check.

Dimensions:
  1. embedder_health  -- FallbackEmbedder active? real embedder working?
  2. zero_vector_ratio -- what % of LanceDB rows are all zeros?
  3. principle_injection -- are principles carrying full content?
  4. rust_engine_health -- is Rust engine importable and healthy?
  5. pipeline_buffer_health -- MemoryPipeline backlog + embed:deferred count
  6. mcp_server_alive -- can we reach the MCP health endpoint?
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger("plastic-promise.scan_data_quality")


def scan_data_quality(engine: Any) -> List[Dict[str, Any]]:
    """Run all 6 data quality checks and return actionable findings."""
    findings: List[Dict[str, Any]] = []

    # --- 1. Embedder health ---
    _check_embedder(engine, findings)

    # --- 2. Zero-vector ratio ---
    _check_zero_vectors(engine, findings)

    # --- 3. Principle injection ---
    _check_principle_injection(engine, findings)

    # --- 4. Rust engine health ---
    _check_rust_health(engine, findings)

    # --- 5. Pipeline buffer health ---
    _check_pipeline_buffer(engine, findings)

    # --- 6. MCP server alive ---
    _check_mcp_alive(findings)

    if findings:
        logger.warning("scan_data_quality: %d issues found", len(findings))
        for f in findings:
            logger.warning("  [%s] %s: %s", f["severity"], f["dimension"], f["summary"])
    else:
        logger.info("scan_data_quality: all checks passed")

    return findings


def _check_embedder(engine: Any, findings: List[Dict]):
    """Check if embedder is healthy (not FallbackEmbedder, produces non-zero vectors)."""
    try:
        from plastic_promise.core.embedder import FallbackEmbedder, get_embedder
        embedder = get_embedder(fallback_on_error=False)
        if isinstance(embedder, FallbackEmbedder):
            findings.append({
                "dimension": "embedder_health",
                "severity": "critical",
                "summary": "FallbackEmbedder active -- all vectors are zeros",
                "fix": "Ensure Ollama is running: ollama serve; then call reset_embedder()",
            })
            return
        # Actually test embedding
        vec = embedder.embed("health check probe")
        if not vec or not any(v != 0.0 for v in vec):
            findings.append({
                "dimension": "embedder_health",
                "severity": "critical",
                "summary": "Embedder returns zero vectors despite not being FallbackEmbedder",
                "fix": "Check embedder logs, try reset_embedder() to re-probe",
            })
    except Exception as e:
        findings.append({
            "dimension": "embedder_health",
            "severity": "critical",
            "summary": f"Embedder probe failed: {e}",
            "fix": "Check EMBEDDER_PROVIDER env var and Ollama connectivity",
        })


def _check_zero_vectors(engine: Any, findings: List[Dict]):
    """Check LanceDB for zero-vector entries."""
    ldb = getattr(engine, '_ldb', None)
    if ldb is None:
        findings.append({
            "dimension": "zero_vector_ratio",
            "severity": "high",
            "summary": "LanceDB store not initialized",
            "fix": "Restart MCP server to trigger _ensure_heavy_init()",
        })
        return
    try:
        table = getattr(ldb, '_table', None)
        if table is None:
            return
        total = table.count_rows()
        if total == 0:
            return
        # Sample first 100 rows for zero-vector check
        sample = table.search().limit(min(total, 100)).to_list()
        zero_count = 0
        for row in sample:
            vec = row.get("vector", [])
            if vec and not any(v != 0.0 for v in vec):
                zero_count += 1
        if sample:
            ratio = zero_count / len(sample)
            if ratio > 0.1:  # >10% zero vectors
                findings.append({
                    "dimension": "zero_vector_ratio",
                    "severity": "critical" if ratio > 0.5 else "high",
                    "summary": f"{ratio:.0%} of sampled LanceDB rows are zero vectors "
                               f"({zero_count}/{len(sample)} sampled, {total} total)",
                    "fix": "Run scripts/repair_zero_vectors.py to re-embed corrupted rows",
                })
    except Exception as e:
        logger.warning("zero_vector check failed: %s", e)


def _check_principle_injection(engine: Any, findings: List[Dict]):
    """Check that principle activation returns full content."""
    try:
        principles = engine._activate_principles("code_generation", "test probe")
        if not principles:
            findings.append({
                "dimension": "principle_injection",
                "severity": "medium",
                "summary": "No principles activated for code_generation task type",
                "fix": "Check CORE_PRINCIPLES and TASK_TYPE_PRINCIPLE_MAP in constants.py",
            })
            return
        for p in principles:
            if isinstance(p, str):
                findings.append({
                    "dimension": "principle_injection",
                    "severity": "high",
                    "summary": "Principles are strings, not dicts -- content not injected",
                    "fix": "Update _activate_principles() to return dicts with name+content",
                })
                return
            if not p.get("content"):
                findings.append({
                    "dimension": "principle_injection",
                    "severity": "medium",
                    "summary": f"Principle '{p.get('name')}' has empty content",
                    "fix": "Check CORE_PRINCIPLES entry for missing content field",
                })
                return
    except Exception as e:
        findings.append({
            "dimension": "principle_injection",
            "severity": "medium",
            "summary": f"Principle injection check failed: {e}",
        })


def _check_rust_health(engine: Any, findings: List[Dict]):
    """Check Rust engine availability."""
    try:
        healthy = engine._check_rust_health()
        if not healthy:
            findings.append({
                "dimension": "rust_engine_health",
                "severity": "low",
                "summary": "Rust engine not available -- using Python fallback",
                "fix": "Build Rust: cd rust/context-engine-core && cargo build --release",
            })
    except Exception as e:
        findings.append({
            "dimension": "rust_engine_health",
            "severity": "low",
            "summary": f"Rust health check failed: {e}",
        })


def _check_pipeline_buffer(engine: Any, findings: List[Dict]):
    """Check MemoryPipeline buffer for stuck/deferred items."""
    try:
        from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer
        fb = _get_fuzzy_buffer(engine)
        stats = fb.stats()
        total = stats.get("total", 0)
        if total > 10:
            # Count embed:deferred tags
            deferred = sum(
                1 for r in getattr(fb, '_buffer', {}).values()
                if "embed:deferred" in r.get("tags", [])
            )
            findings.append({
                "dimension": "pipeline_buffer_health",
                "severity": "high" if deferred > 5 else "medium",
                "summary": f"Pipeline buffer has {total} items ({deferred} with embed:deferred)",
                "fix": "Check embedder health; if recovered, run fuzzy_process MCP tool",
            })
    except Exception as e:
        logger.warning("pipeline buffer check failed: %s", e)


def _check_mcp_alive(findings: List[Dict]):
    """Check MCP server health endpoint."""
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://127.0.0.1:9020/health", timeout=5)
        if resp.status != 200:
            raise Exception(f"HTTP {resp.status}")
    except Exception as e:
        findings.append({
            "dimension": "mcp_server_alive",
            "severity": "critical",
            "summary": f"MCP server unreachable: {e}",
            "fix": "Start MCP: python -m plastic_promise.mcp.server --sse 9020",
        })
