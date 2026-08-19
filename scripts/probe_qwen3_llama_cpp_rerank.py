#!/usr/bin/env python3
"""Compare llama.cpp native rerank with Qwen3's official yes/no scoring."""

from __future__ import annotations

import argparse
import json
import math
import urllib.error
import urllib.request

DEFAULT_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the "
    'Query and the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n<|im_start|>user\n"
)
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def post_json(base_url: str, path: str, payload: object) -> dict[str, object]:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"llama_cpp_http_{exc.code}:{detail}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("llama_cpp_response_invalid")
    return value


def token_id(base_url: str, value: str) -> int:
    response = post_json(
        base_url,
        "/tokenize",
        {"content": value, "add_special": False, "parse_special": True},
    )
    tokens = response.get("tokens")
    if (
        not isinstance(tokens, list)
        or len(tokens) != 1
        or not isinstance(tokens[0], int)
        or isinstance(tokens[0], bool)
    ):
        raise RuntimeError(f"qwen3_rerank_token_not_single:{value}")
    return tokens[0]


def qwen3_score(
    base_url: str,
    *,
    instruction: str,
    query: str,
    document: str,
    yes_id: int,
    no_id: int,
) -> float:
    user = f"<Instruct>: {instruction}\n\n<Query>: {query}\n\n<Document>: {document}"
    response = post_json(
        base_url,
        "/completion",
        {
            "prompt": PREFIX + user + SUFFIX,
            "n_predict": 1,
            # Preserve the raw yes/no ratio. Greedy sampling collapses the
            # selected token to probability 1.0 and destroys rerank evidence.
            "temperature": 1.0,
            "top_k": 0,
            "top_p": 1.0,
            "min_p": 0.0,
            "n_probs": 2,
            "post_sampling_probs": True,
            # Equal bias preserves the raw yes/no logit difference while
            # excluding the rest of the vocabulary from the normalized pair.
            "logit_bias": [[yes_id, 100.0], [no_id, 100.0]],
            "cache_prompt": True,
        },
    )
    probabilities = response.get("probs", response.get("completion_probabilities"))
    if not isinstance(probabilities, list) or len(probabilities) != 1:
        raise RuntimeError("qwen3_rerank_probabilities_missing:" + ",".join(sorted(response)))
    first = probabilities[0]
    top = first.get("top_probs") if isinstance(first, dict) else None
    if not isinstance(top, list):
        raise RuntimeError("qwen3_rerank_top_probabilities_missing")
    by_id = {
        row.get("id"): row.get("prob")
        for row in top
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    }
    yes = by_id.get(yes_id)
    no = by_id.get(no_id)
    if not isinstance(yes, (int, float)) or not isinstance(no, (int, float)):
        raise RuntimeError(
            "qwen3_rerank_yes_no_probabilities_missing:"
            + json.dumps(probabilities, ensure_ascii=False, separators=(",", ":"))[:2000]
        )
    if not math.isfinite(float(yes)) or not math.isfinite(float(no)) or yes + no <= 0:
        raise RuntimeError("qwen3_rerank_yes_no_probabilities_invalid")
    return float(yes) / (float(yes) + float(no))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:19132")
    parser.add_argument("--query", default="memory governance")
    parser.add_argument(
        "--documents",
        nargs="+",
        default=["canonical memory governance", "unrelated cooking recipe"],
    )
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    args = parser.parse_args()
    yes_id = token_id(args.base_url, "yes")
    no_id = token_id(args.base_url, "no")
    native = post_json(
        args.base_url,
        "/rerank",
        {"query": args.query, "documents": args.documents},
    )
    official = [
        qwen3_score(
            args.base_url,
            instruction=args.instruction,
            query=args.query,
            document=document,
            yes_id=yes_id,
            no_id=no_id,
        )
        for document in args.documents
    ]
    print(
        json.dumps(
            {
                "yes_token_id": yes_id,
                "no_token_id": no_id,
                "native": native.get("results"),
                "official_yes_probability": official,
                "official_top_index": max(range(len(official)), key=official.__getitem__),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
