#!/usr/bin/env python3
"""Run credential-safe cloud inference smoke tests with synthetic material.

Secrets are accepted only through hidden interactive prompts or stdin. They are
never accepted as command-line arguments or environment variables, and output
contains only bounded provider metadata and validation results.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plastic_promise.core.embedder import OpenAICompatibleEmbedder  # noqa: E402
from plastic_promise.core.inference_provider import OpenAICompatibleJSONProvider  # noqa: E402
from plastic_promise.core.provider_http import (  # noqa: E402
    ProviderHTTPClient,
    ProviderHTTPError,
    ProviderHTTPPolicy,
)

_SYUAN_BASE_URL = "https://api.syuan.org"
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

_CREDENTIAL_TEXT_RE = re.compile(
    r"""
    (?:
        (?<![A-Za-z0-9])(?i:(?:sk|key))-[A-Za-z0-9._~+/=-]+
        |
        \b(?i:bearer)[ \t]+[A-Za-z0-9._~+/=-]+
        |
        \b(?i:(?:api[ _-]?key|access[ _-]?token|secret|credential))
        [ \t]*[:=][ \t]*["']?[A-Za-z0-9._~+/=-]+
        |
        (?<![A-Za-z0-9._~+/=-])
        (?=[A-Za-z0-9._~+/=-]{32,}(?![A-Za-z0-9._~+/=-]))
        (?=[A-Za-z0-9._~+/=-]*[A-Za-z])
        (?=[A-Za-z0-9._~+/=-]*[0-9])
        [A-Za-z0-9._~+/=-]+
    )
    """,
    re.VERBOSE,
)

_SAFE_FAILURE_REASONS = frozenset(
    {
        "deepseek_smoke_schema_mismatch",
        "deepseek_base_url_unapproved",
        "embedding_response_count_mismatch",
        "embedding_response_dimension_mismatch",
        "embedding_response_index_invalid",
        "embedding_response_model_invalid",
        "embedding_response_model_mismatch",
        "embedding_response_schema_invalid",
        "embedding_response_value_invalid",
        "embedding_response_zero_vector",
        "embedding_smoke_vector_invalid",
        "inference_finish_reason_not_stop",
        "inference_output_invalid_json",
        "inference_output_object_required",
        "inference_output_too_large",
        "inference_response_schema_invalid",
        "provider_http_api_key_missing",
        "provider_http_circuit_open",
        "provider_http_client_closed",
        "provider_http_deadline_exceeded",
        "provider_http_documentation_base_url",
        "provider_http_forbidden",
        "provider_http_invalid_base_url",
        "provider_http_invalid_config",
        "provider_http_invalid_deadline",
        "provider_http_invalid_endpoint",
        "provider_http_invalid_json",
        "provider_http_invalid_payload",
        "provider_http_invalid_provider",
        "provider_http_invalid_response",
        "provider_http_invalid_utf8",
        "provider_http_json_object_required",
        "provider_http_request_failed",
        "provider_http_request_too_large",
        "provider_http_response_too_large",
        "provider_http_retry_exhausted",
        "provider_http_unauthorized",
        "rerank_response_count_mismatch",
        "rerank_response_index_invalid",
        "rerank_response_model_mismatch",
        "rerank_response_order_invalid",
        "rerank_response_schema_invalid",
        "rerank_response_score_invalid",
        "rerank_smoke_quality_mismatch",
        "syuan_base_url_unapproved",
    }
)
_SAFE_FAILURE_TYPES = {
    ProviderHTTPError: "ProviderHTTPError",
    RuntimeError: "RuntimeError",
    ValueError: "ValueError",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke cloud embedding, rerank, and structured JSON providers.",
    )
    parser.add_argument("--keys-from-stdin", action="store_true")
    parser.add_argument("--skip-syuan", action="store_true")
    parser.add_argument("--skip-deepseek", action="store_true")
    parser.add_argument("--syuan-base-url", default=_SYUAN_BASE_URL)
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument("--embedding-dimension", type=int, default=1024)
    parser.add_argument(
        "--embedding-send-dimensions",
        action="store_true",
        help="Send the requested embedding dimension to providers that support it",
    )
    parser.add_argument("--rerank-model", default="Qwen3-Reranker-8B")
    parser.add_argument("--deepseek-base-url", default=_DEEPSEEK_BASE_URL)
    parser.add_argument("--deepseek-model", default="deepseek-v4-flash")
    return parser


def _read_key(*, prompt: str, from_stdin: bool) -> str:
    raw = sys.stdin.readline() if from_stdin else getpass.getpass(prompt)
    key = raw.rstrip("\r\n")
    if not key or key != key.strip():
        raise ValueError("provider_key_missing")
    return key


def _normalize_approved_base_url(
    value: str,
    *,
    approved: str,
    failure_reason: str,
) -> str:
    if value.rstrip("/") != approved:
        raise ValueError(failure_reason)
    return approved


def _normalize_syuan_base_url(value: str) -> str:
    return _normalize_approved_base_url(
        value,
        approved=_SYUAN_BASE_URL,
        failure_reason="syuan_base_url_unapproved",
    )


def _normalize_deepseek_base_url(value: str) -> str:
    return _normalize_approved_base_url(
        value,
        approved=_DEEPSEEK_BASE_URL,
        failure_reason="deepseek_base_url_unapproved",
    )


def _safe_failure(exc: Exception) -> dict[str, object]:
    exc_type = type(exc)
    error_type = _SAFE_FAILURE_TYPES.get(exc_type, "Exception")
    candidate = ""
    if exc_type in _SAFE_FAILURE_TYPES and len(exc.args) == 1 and isinstance(exc.args[0], str):
        candidate = exc.args[0].strip()
    reason = "provider_smoke_failed"
    if not _CREDENTIAL_TEXT_RE.search(candidate) and candidate in _SAFE_FAILURE_REASONS:
        reason = candidate
    return {
        "ok": False,
        "error_type": error_type,
        "reason": reason,
    }


def _validate_rerank_response(
    payload: object,
    *,
    expected_model: str,
    candidate_count: int,
) -> tuple[int, int]:
    if not isinstance(payload, Mapping):
        raise RuntimeError("rerank_response_schema_invalid")
    response_model = payload.get("model")
    if response_model is not None and response_model != expected_model:
        raise RuntimeError("rerank_response_model_mismatch")
    rows = payload.get("results")
    if not isinstance(rows, list) or len(rows) != candidate_count:
        raise RuntimeError("rerank_response_count_mismatch")

    indices: list[int] = []
    scores: list[float] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("rerank_response_schema_invalid")
        index = row.get("index")
        raw_score = row.get("relevance_score")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < candidate_count
            or index in indices
        ):
            raise RuntimeError("rerank_response_index_invalid")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise RuntimeError("rerank_response_score_invalid")
        score = float(raw_score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise RuntimeError("rerank_response_score_invalid")
        indices.append(index)
        scores.append(score)

    if scores != sorted(scores, reverse=True):
        raise RuntimeError("rerank_response_order_invalid")
    return indices[0], len(indices)


def _smoke_syuan(args: argparse.Namespace, api_key: str) -> dict[str, object]:
    base_url = _normalize_syuan_base_url(args.syuan_base_url)
    previous_dimension = os.environ.get("PP_EMBEDDING_DIM")
    previous_path = os.environ.get("EMBEDDER_PATH")
    os.environ["PP_EMBEDDING_DIM"] = str(args.embedding_dimension)
    os.environ["EMBEDDER_PATH"] = "/v1/embeddings"
    embedder: OpenAICompatibleEmbedder | None = None
    rerank_client: ProviderHTTPClient | None = None
    try:
        embedder = OpenAICompatibleEmbedder(
            api_key=api_key,
            base_url=base_url,
            model=args.embedding_model,
            model_revision=args.embedding_model,
            dim=args.embedding_dimension,
            send_dimensions=args.embedding_send_dimensions,
        )
        vector = embedder.embed("Synthetic provider smoke: canonical records live in SQLite.")
        norm = math.sqrt(sum(component * component for component in vector))
        if len(vector) != args.embedding_dimension or not math.isfinite(norm) or norm <= 0.0:
            raise RuntimeError("embedding_smoke_vector_invalid")

        rerank_client = ProviderHTTPClient(
            provider="syuan-rerank-smoke",
            base_url=base_url,
            api_key=api_key,
            policy=ProviderHTTPPolicy(
                timeout_seconds=30.0,
                total_timeout_seconds=90.0,
                max_retries=2,
                max_response_bytes=2 * 1024 * 1024,
            ),
        )
        documents = [
            "SQLite stores the canonical records.",
            "LanceDB is a rebuildable derived vector index.",
            "A synthetic weather sentence is unrelated.",
        ]
        response = rerank_client.post_json(
            "/v1/rerank",
            {
                "model": args.rerank_model,
                "query": "Which component stores the canonical records?",
                "documents": documents,
                "top_n": len(documents),
                "return_documents": False,
            },
        )
        top_index, result_count = _validate_rerank_response(
            response.payload,
            expected_model=args.rerank_model,
            candidate_count=len(documents),
        )
        if top_index != 0:
            raise RuntimeError("rerank_smoke_quality_mismatch")
        return {
            "ok": True,
            "embedding": {
                "model": args.embedding_model,
                "dimension": len(vector),
                "dimension_parameter_sent": args.embedding_send_dimensions,
                "native_dimension": not args.embedding_send_dimensions,
                "nonzero": True,
            },
            "rerank": {
                "model": args.rerank_model,
                "result_count": result_count,
                "top_index": top_index,
            },
        }
    finally:
        if embedder is not None:
            embedder.close()
        if rerank_client is not None:
            rerank_client.close()
        if previous_dimension is None:
            os.environ.pop("PP_EMBEDDING_DIM", None)
        else:
            os.environ["PP_EMBEDDING_DIM"] = previous_dimension
        if previous_path is None:
            os.environ.pop("EMBEDDER_PATH", None)
        else:
            os.environ["EMBEDDER_PATH"] = previous_path


def _smoke_deepseek(args: argparse.Namespace, api_key: str) -> dict[str, object]:
    base_url = _normalize_deepseek_base_url(args.deepseek_base_url)
    provider = OpenAICompatibleJSONProvider(
        api_key=api_key,
        base_url=base_url,
        model=args.deepseek_model,
        model_revision=args.deepseek_model,
        temperature=0.0,
        top_p=1.0,
        json_mode=True,
    )
    try:
        result = provider.complete_json(
            system_prompt=(
                "Return only a valid JSON object with exactly one field named selected. "
                "Its value must be the candidate that exactly matches required."
            ),
            user_payload={"candidates": ["alpha", "beta"], "required": "alpha"},
            max_tokens=128,
        )
        if result != {"selected": "alpha"}:
            raise RuntimeError("deepseek_smoke_schema_mismatch")
        return {
            "ok": True,
            "model": args.deepseek_model,
            "identity": provider.identity,
            "output_fields": sorted(result),
            "requests": provider.stats["requests"],
        }
    finally:
        provider.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.skip_syuan and args.skip_deepseek:
        raise SystemExit("at_least_one_provider_required")
    if args.embedding_dimension <= 0:
        raise SystemExit("embedding_dimension_invalid")

    report: dict[str, object] = {"synthetic_only": True}
    failed = False
    if not args.skip_syuan:
        try:
            args.syuan_base_url = _normalize_syuan_base_url(args.syuan_base_url)
        except ValueError as exc:
            report["syuan"] = _safe_failure(exc)
            failed = True
    if not args.skip_deepseek:
        try:
            args.deepseek_base_url = _normalize_deepseek_base_url(args.deepseek_base_url)
        except ValueError as exc:
            report["deepseek"] = _safe_failure(exc)
            failed = True
    if failed:
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
        return 1

    if not args.skip_syuan:
        try:
            syuan_key = _read_key(prompt="Syuan API key: ", from_stdin=args.keys_from_stdin)
            report["syuan"] = _smoke_syuan(args, syuan_key)
        except Exception as exc:
            report["syuan"] = _safe_failure(exc)
            failed = True
    if not args.skip_deepseek:
        try:
            deepseek_key = _read_key(prompt="DeepSeek API key: ", from_stdin=args.keys_from_stdin)
            report["deepseek"] = _smoke_deepseek(args, deepseek_key)
        except Exception as exc:
            report["deepseek"] = _safe_failure(exc)
            failed = True

    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
