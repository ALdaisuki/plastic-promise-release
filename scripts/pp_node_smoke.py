#!/usr/bin/env python3
"""Local compute-node identity, correctness, and performance smoke.

Reads the non-secret node compose .env for the expected model identity, probes
/health and /v1/identity, verifies a small embedding batch (dimension and L2
normalization) and a bounded rerank batch (all candidates scored), records
median latency evidence, and writes a doctor-compatible runtime-status file.
Never sends canonical data, never opens state, and never writes secrets.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SMOKE_SCHEMA = "plastic-promise/local-inference-node-smoke/v1"
RUNTIME_STATUS_SCHEMA = "plastic-promise/local-inference-runtime-status/v1"
_SENSITIVE_TOKENS = frozenset(
    {"APIKEY", "AUTHORIZATION", "CREDENTIAL", "PASSWORD", "PRIVATEKEY", "SECRET", "TOKEN"}
)
_NODE_AUTHORIZATION_KEY = "PP_LOCAL_NODE_AUTHORIZATION"
_IGNORED_PRIVATE_KEYS = frozenset({"PP_LOCAL_NODE_CLOUD_API_KEY"})


def _read_node_env(path: Path) -> tuple[dict[str, str], str]:
    values: dict[str, str] = {}
    authorization = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"node config unreadable: {exc}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        if key == _NODE_AUTHORIZATION_KEY:
            authorization = value.strip()
            continue
        if key in _IGNORED_PRIVATE_KEYS:
            continue
        if any(token in key.upper() for token in _SENSITIVE_TOKENS):
            raise SystemExit("node config contains a sensitive key; refusing to read it")
        values[key] = value.strip()
    if not authorization:
        raise SystemExit("node authorization missing from node config")
    return values, authorization


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    authorization: str | None = None,
) -> tuple[dict, float]:
    url = f"{base_url}{path}"
    body = None
    headers = {}
    if authorization:
        headers["Authorization"] = authorization
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    started = time.monotonic()
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"HTTP {exc.code} on {method} {path}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f"node unreachable on {method} {path}: {exc}") from exc
    elapsed_ms = (time.monotonic() - started) * 1000.0
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON from {method} {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"unexpected payload shape from {method} {path}")
    return parsed, elapsed_ms


def _l2_norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:19130")
    parser.add_argument("--node-config", type=Path, help="non-secret compose .env")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/local-node-build"))
    parser.add_argument("--runtime-status", type=Path, help="doctor-compatible runtime-status.json")
    parser.add_argument("--expected-dimension", type=int)
    parser.add_argument("--expected-normalization", choices=("l2", "none"))
    args = parser.parse_args(argv)

    expected = {
        "PP_LOCAL_NODE_ID": None,
        "PP_LOCAL_NODE_EMBEDDING_MODEL": None,
        "PP_LOCAL_NODE_EMBEDDING_REVISION": None,
        "PP_LOCAL_NODE_EMBEDDING_DIMENSION": None,
        "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION": None,
        "PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256": None,
        "PP_LOCAL_NODE_RERANK_MODEL": None,
        "PP_LOCAL_NODE_RERANK_REVISION": None,
        "PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256": None,
    }
    authorization = None
    if args.node_config is not None:
        env, authorization = _read_node_env(args.node_config)
        for key in expected:
            expected[key] = env.get(key)

    health, health_ms = _request(
        args.base_url,
        "GET",
        "/health",
        authorization=authorization,
    )
    if health.get("status") != "ok":
        raise SystemExit(f"node health not ok: {health}")
    identity, identity_ms = _request(
        args.base_url,
        "GET",
        "/v1/identity",
        authorization=authorization,
    )
    embedding = identity.get("embedding")
    rerank = identity.get("rerank")
    if not isinstance(embedding, dict) or not isinstance(rerank, dict):
        raise SystemExit("node identity payload invalid")

    embedding_model = embedding.get("model")
    embedding_revision = embedding.get("revision")
    embedding_artifact = embedding.get("artifact_sha256")
    dimension = embedding.get("dimension")
    normalization = embedding.get("normalization")
    if expected["PP_LOCAL_NODE_ID"] and identity.get("node_id") != expected["PP_LOCAL_NODE_ID"]:
        raise SystemExit(f"node id mismatch: {identity.get('node_id')}")
    if args.expected_dimension is not None and dimension != args.expected_dimension:
        raise SystemExit(f"embedding dimension mismatch: {dimension} != {args.expected_dimension}")
    if (
        expected["PP_LOCAL_NODE_EMBEDDING_MODEL"]
        and embedding_model != expected["PP_LOCAL_NODE_EMBEDDING_MODEL"]
    ):
        raise SystemExit(f"embedding model mismatch: {embedding_model}")
    if (
        expected["PP_LOCAL_NODE_EMBEDDING_REVISION"]
        and embedding_revision != expected["PP_LOCAL_NODE_EMBEDDING_REVISION"]
    ):
        raise SystemExit(f"embedding revision mismatch: {embedding_revision}")
    if (
        expected["PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256"]
        and embedding_artifact != expected["PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256"]
    ):
        raise SystemExit(f"embedding artifact mismatch: {embedding_artifact}")
    if (
        expected["PP_LOCAL_NODE_RERANK_MODEL"]
        and rerank.get("model") != expected["PP_LOCAL_NODE_RERANK_MODEL"]
    ):
        raise SystemExit(f"rerank model mismatch: {rerank.get('model')}")
    if (
        expected["PP_LOCAL_NODE_RERANK_REVISION"]
        and rerank.get("revision") != expected["PP_LOCAL_NODE_RERANK_REVISION"]
    ):
        raise SystemExit(f"rerank revision mismatch: {rerank.get('revision')}")
    if (
        expected["PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256"]
        and rerank.get("artifact_sha256") != expected["PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256"]
    ):
        raise SystemExit(f"rerank artifact mismatch: {rerank.get('artifact_sha256')}")

    embed_payload = {
        "input": [
            "Plastic Promise governed memory",
            "local heterogeneous inference node",
        ]
    }
    embedded, embed_ms = _request(
        args.base_url,
        "POST",
        "/v1/embeddings",
        embed_payload,
        authorization=authorization,
    )
    data = embedded.get("data")
    if not isinstance(data, list) or len(data) != 2:
        raise SystemExit("embedding result count mismatch")
    norms = []
    for item in data:
        vector = item.get("embedding")
        if not isinstance(vector, list) or not all(isinstance(v, (int, float)) for v in vector):
            raise SystemExit("embedding vector invalid")
        if len(vector) != dimension:
            raise SystemExit(f"embedding vector dimension mismatch: {len(vector)} != {dimension}")
        if normalization == "l2":
            norm = _l2_norm(vector)
            if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-2):
                raise SystemExit(f"embedding vector not L2-normalized: {norm}")
            norms.append(norm)

    rerank_payload = {
        "query": "local inference node performance smoke",
        "documents": [
            "The node runs embedding and rerank behind a loopback listener.",
            "Model identity is pinned by revision before every request.",
            "Latency evidence is recorded for the routing controller.",
        ],
        "top_k": 3,
    }
    reranked, rerank_ms = _request(
        args.base_url,
        "POST",
        "/v1/rerank",
        rerank_payload,
        authorization=authorization,
    )
    results = reranked.get("results")
    if not isinstance(results, list) or len(results) != 3:
        raise SystemExit("rerank result count mismatch")

    latencies_ms = [health_ms, identity_ms, embed_ms, rerank_ms]
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    smoke_report = {
        "schema_version": SMOKE_SCHEMA,
        "node_id": identity.get("node_id") or expected["PP_LOCAL_NODE_ID"],
        "checks": {
            "health": "passed",
            "identity": "passed",
            "embedding": {
                "model": embedding_model,
                "revision": embedding_revision,
                "artifact_sha256": embedding_artifact,
                "dimension": dimension,
                "normalization": normalization,
                "l2_norms": [round(value, 6) for value in norms],
                "status": "passed",
            },
            "rerank": {
                "model": rerank.get("model"),
                "revision": rerank.get("revision"),
                "artifact_sha256": rerank.get("artifact_sha256"),
                "candidate_count": len(results),
                "status": "passed",
            },
        },
        "performance": {
            "median_latency_ms": round(statistics.median(latencies_ms), 3),
            "sample_count": len(latencies_ms),
            "endpoint_latency_ms": {
                "health": round(health_ms, 3),
                "identity": round(identity_ms, 3),
                "embeddings": round(embed_ms, 3),
                "rerank": round(rerank_ms, 3),
            },
        },
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    smoke_path = output_dir / f"node-smoke-{timestamp}.json"
    smoke_path.write_text(
        json.dumps(smoke_report, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )

    runtime = {
        "schema_version": RUNTIME_STATUS_SCHEMA,
        "running": True,
        "node_healthy": True,
    }
    runtime_path = args.runtime_status or (output_dir / "runtime-status.json")
    runtime_path = Path(runtime_path)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(json.dumps(runtime, sort_keys=True) + "\n", encoding="utf-8")

    print(f"node smoke passed: {smoke_path}")
    print(f"runtime status: {runtime_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
