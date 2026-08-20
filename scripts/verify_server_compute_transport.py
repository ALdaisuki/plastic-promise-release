#!/usr/bin/env python3
"""Fail-closed verification of the private server-to-compute transport."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.private_http_client import (  # noqa: E402
    PrivateHttpError,
    open_no_redirect,
    validate_loopback_base_url,
)
from scripts.run_with_env_files import parse_env_file  # noqa: E402

AUTH_NAME = re.compile(r"PP_NODE_AUTH_[A-Z0-9_]{1,96}$")
AUTH_VALUE = re.compile(r"Bearer [A-Za-z0-9._~+/=-]{1,4096}$")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}$")


def request_json(
    base_url: str,
    path: str,
    authorization: str,
    *,
    body: object | None = None,
) -> dict[str, object]:
    base_url = validate_loopback_base_url(base_url)
    payload = None
    headers = {"Authorization": authorization, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=payload,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    with open_no_redirect(request, timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("compute_transport_response_invalid")
    return value


def identity_digest(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _require_equal(actual: object, expected: object, code: str) -> None:
    if actual != expected:
        raise RuntimeError(code)


def _require_identity(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(code)
    artifact = value.get("artifact_sha256")
    if not isinstance(artifact, str) or SHA256.fullmatch(artifact) is None:
        raise RuntimeError(f"{code}_artifact_invalid")
    return value


def _embedding_probe(
    base_url: str,
    authorization: str,
    *,
    model: str,
    dimension: int,
    normalization: str,
) -> float:
    payload = request_json(
        base_url,
        "/v1/embeddings",
        authorization,
        body={"model": model, "input": ["Plastic Promise compute transport verification"]},
    )
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError("compute_transport_embedding_response_invalid")
    vector = data[0].get("embedding")
    if not isinstance(vector, list) or len(vector) != dimension:
        raise RuntimeError("compute_transport_embedding_dimension_mismatch")
    try:
        norm = math.sqrt(sum(float(item) ** 2 for item in vector))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("compute_transport_embedding_vector_invalid") from exc
    if not math.isfinite(norm):
        raise RuntimeError("compute_transport_embedding_vector_invalid")
    if normalization == "l2" and not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
        raise RuntimeError("compute_transport_embedding_normalization_mismatch")
    return norm


def _rerank_probe(base_url: str, authorization: str) -> bool:
    payload = request_json(
        base_url,
        "/v1/rerank",
        authorization,
        body={
            "query": "memory governance",
            "documents": ["canonical memory governance", "unrelated cooking recipe"],
            "top_k": 2,
        },
    )
    results = payload.get("results")
    if not isinstance(results, list) or len(results) < 2:
        raise RuntimeError("compute_transport_rerank_response_invalid")
    ranked = sorted(
        (item for item in results if isinstance(item, dict)),
        key=lambda item: float(item.get("score", item.get("relevance_score", "-inf"))),
        reverse=True,
    )
    if len(ranked) < 2 or ranked[0].get("index") != 0:
        raise RuntimeError("compute_transport_rerank_directional_probe_failed")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--auth-name", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-node-id", required=True)
    parser.add_argument("--expected-embedding-model", required=True)
    parser.add_argument("--expected-embedding-revision", required=True)
    parser.add_argument("--expected-embedding-dimension", type=int, required=True)
    parser.add_argument("--expected-embedding-normalization", choices=("l2", "none"), required=True)
    parser.add_argument("--expected-embedding-identity", required=True)
    parser.add_argument("--expected-rerank-model", required=True)
    parser.add_argument("--expected-rerank-revision", required=True)
    parser.add_argument("--expected-rerank-identity", required=True)
    args = parser.parse_args()
    if not AUTH_NAME.fullmatch(args.auth_name):
        raise SystemExit("compute_transport_auth_name_invalid")
    if not args.env_file.is_file() or args.env_file.is_symlink():
        raise SystemExit("compute_transport_env_missing_or_unsafe")
    if os.name == "posix" and args.env_file.stat().st_mode & 0o077:
        raise SystemExit("compute_transport_env_permissions_invalid")
    if SHA256.fullmatch(args.expected_embedding_identity) is None:
        raise SystemExit("compute_transport_expected_embedding_identity_invalid")
    if SHA256.fullmatch(args.expected_rerank_identity) is None:
        raise SystemExit("compute_transport_expected_rerank_identity_invalid")
    if not 1 <= args.expected_embedding_dimension <= 65_536:
        raise SystemExit("compute_transport_expected_embedding_dimension_invalid")
    authorization = parse_env_file(args.env_file).get(args.auth_name, "")
    if not AUTH_VALUE.fullmatch(authorization):
        raise SystemExit("compute_transport_authorization_invalid")

    health = request_json(args.base_url, "/health", authorization)
    identity = request_json(args.base_url, "/v1/identity", authorization)
    _require_equal(health.get("status"), "ok", "compute_transport_health_not_ok")
    _require_equal(
        identity.get("node_id"), args.expected_node_id, "compute_transport_node_mismatch"
    )
    capabilities = identity.get("capabilities")
    if not isinstance(capabilities, list) or not {"embeddings", "rerank"}.issubset(
        {str(item) for item in capabilities}
    ):
        raise RuntimeError("compute_transport_capabilities_invalid")

    embedding = _require_identity(identity.get("embedding"), "compute_transport_embedding_identity")
    rerank = _require_identity(identity.get("rerank"), "compute_transport_rerank_identity")
    _require_equal(
        embedding.get("model"),
        args.expected_embedding_model,
        "compute_transport_embedding_model_mismatch",
    )
    _require_equal(
        embedding.get("revision"),
        args.expected_embedding_revision,
        "compute_transport_embedding_revision_mismatch",
    )
    _require_equal(
        embedding.get("dimension"),
        args.expected_embedding_dimension,
        "compute_transport_embedding_dimension_mismatch",
    )
    _require_equal(
        embedding.get("normalization"),
        args.expected_embedding_normalization,
        "compute_transport_embedding_normalization_mismatch",
    )
    _require_equal(
        rerank.get("model"), args.expected_rerank_model, "compute_transport_rerank_model_mismatch"
    )
    _require_equal(
        rerank.get("revision"),
        args.expected_rerank_revision,
        "compute_transport_rerank_revision_mismatch",
    )

    embedding_key = identity_digest(
        {
            "model": embedding.get("model"),
            "revision": embedding.get("revision"),
            "dimension": embedding.get("dimension"),
            "normalization": embedding.get("normalization"),
            "artifact_sha256": embedding.get("artifact_sha256"),
        }
    )
    rerank_key = identity_digest(
        {
            "model": rerank.get("model"),
            "revision": rerank.get("revision"),
            "artifact_sha256": rerank.get("artifact_sha256"),
        }
    )
    _require_equal(
        embedding_key,
        args.expected_embedding_identity,
        "compute_transport_embedding_identity_mismatch",
    )
    _require_equal(
        rerank_key, args.expected_rerank_identity, "compute_transport_rerank_identity_mismatch"
    )
    norm = _embedding_probe(
        args.base_url,
        authorization,
        model=args.expected_embedding_model,
        dimension=args.expected_embedding_dimension,
        normalization=args.expected_embedding_normalization,
    )
    directional = _rerank_probe(args.base_url, authorization)
    print(
        json.dumps(
            {
                "health_status": "ok",
                "node_id": args.expected_node_id,
                "provider_class": identity.get("provider_class"),
                "capabilities": capabilities,
                "embedding_identity": embedding_key,
                "embedding_dimension": args.expected_embedding_dimension,
                "embedding_l2": round(norm, 6),
                "rerank_identity": rerank_key,
                "rerank_directional_probe": directional,
                "structured_json": identity.get("structured_json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PrivateHttpError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
