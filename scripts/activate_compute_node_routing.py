#!/usr/bin/env python3
"""Activate the server-owned routing identity for the live private node.

The script is intentionally operator-side: it reads a one-time control token
from a mode-0600 file and never prints it.  It stages the non-secret routing
policy together with the exact embedding/rerank model identity that a promoted
generation must prove through the control-plane CAS API.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.control_api_client import (  # noqa: E402
    ControlApiError,
    read_bearer_token,
    request_json,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9040/api/control/v1")
    parser.add_argument(
        "--node-id",
        default="inference-node",
        help="Stable technical node id; the Dashboard may render a localized display name",
    )
    parser.add_argument("--embedding-identity", required=True)
    parser.add_argument("--rerank-identity", required=True)
    parser.add_argument("--structured-json-identity")
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--embedding-revision", required=True)
    parser.add_argument("--embedding-dimension", type=int, required=True)
    parser.add_argument("--rerank-model", required=True)
    parser.add_argument("--rerank-revision", required=True)
    parser.add_argument("--structured-json-model")
    parser.add_argument("--structured-json-revision")
    parser.add_argument("--inference-mode", choices=("local", "cloud", "hybrid"))
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--evidence-file", type=Path)
    args = parser.parse_args()
    structured_values = (
        args.structured_json_identity,
        args.structured_json_model,
        args.structured_json_revision,
    )
    structured_enabled = all(structured_values)
    if any(structured_values) and not structured_enabled:
        raise SystemExit("control_structured_json_identity_incomplete")
    inference_mode = args.inference_mode or ("hybrid" if structured_enabled else "local")
    if inference_mode == "hybrid" and not structured_enabled:
        raise SystemExit("control_hybrid_structured_json_identity_required")
    token = read_bearer_token(args.token_file)
    safe = request_json(args.base_url, "/config/safe", token)
    if not isinstance(safe, dict) or not isinstance(safe.get("config"), dict):
        raise SystemExit("control_safe_config_invalid")
    etag = safe.get("etag")
    if not isinstance(etag, str) or not etag:
        raise SystemExit("control_safe_etag_missing")
    routing = copy.deepcopy(safe["config"].get("node_routing"))
    if not isinstance(routing, dict):
        raise SystemExit("control_node_routing_missing")
    routing["enabled"] = True
    routing["inference_mode"] = inference_mode
    node_id = args.node_id.strip()
    if not node_id or len(node_id) > 128 or any(ch in node_id for ch in "\r\n\x00"):
        raise SystemExit("control_node_id_invalid")
    routing["allowed_node_ids"] = [node_id]
    routing["embedding_policy"] = "pinned-node"
    routing["rerank_policy"] = "pinned-node"
    routing["embedding_pinned_node_id"] = node_id
    routing["rerank_pinned_node_id"] = node_id
    routing["embedding_required_identity"] = args.embedding_identity
    routing["rerank_required_identity"] = args.rerank_identity
    if structured_enabled:
        routing["structured_json_policy"] = "pinned-node"
        routing["structured_json_pinned_node_id"] = node_id
        routing["structured_json_required_identity"] = args.structured_json_identity
    else:
        routing["structured_json_policy"] = "remote-node-first"
        routing["structured_json_pinned_node_id"] = ""
        routing["structured_json_required_identity"] = ""
    if not 1 <= args.embedding_dimension <= 16_384:
        raise SystemExit("control_embedding_dimension_invalid")
    # A local governed route owns provider execution on pp-compute-node.  The
    # server-side provider sections therefore remain disabled; enabling them
    # would incorrectly trigger HTTPS provider validation and blur the
    # server/compute responsibility boundary.  Cloud/hybrid keeps the legacy
    # provider projection available for the explicitly configured operations.
    local_route = inference_mode == "local"
    config: dict[str, object] = {
        # Keep the real compute identity in the revision even when the
        # server-side provider adapters are disabled. Generation evidence
        # binds these fields to the candidate manifest; execution remains on
        # pp-compute-node through node_routing.
        "embedding": {
            "enabled": local_route is False,
            "model": args.embedding_model,
            "model_revision": args.embedding_revision,
            "dimension": args.embedding_dimension,
        },
        "rerank": {
            "enabled": local_route is False,
            "model": args.rerank_model,
            "model_revision": args.rerank_revision,
        },
        "node_routing": routing,
    }
    if structured_enabled and not local_route:
        config["chunk_inference"] = {
            "model": args.structured_json_model,
            "model_revision": args.structured_json_revision,
        }
    patch = {"config": config}
    request_json(args.base_url, "/config/validate", token, method="POST", body=patch, etag=etag)
    request_fingerprint = hashlib.sha256(
        json.dumps(patch, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    stage_key = f"compute-node-routing-{request_fingerprint}"
    stage = request_json(
        args.base_url,
        "/config/stage",
        token,
        method="POST",
        body=patch,
        etag=etag,
        idempotency_key=stage_key,
    )
    if not isinstance(stage, dict):
        raise SystemExit("control_stage_response_invalid")
    revision = stage.get("revision_id")
    if not isinstance(revision, str):
        revision_obj = stage.get("revision")
        revision = revision_obj.get("revision_id") if isinstance(revision_obj, dict) else None
    if not isinstance(revision, str) or not revision:
        raise SystemExit("control_stage_revision_missing")
    output: dict[str, object] = {
        "staged_revision": revision,
        "base_etag": etag,
        "node_id": node_id,
        "embedding_identity": args.embedding_identity,
        "rerank_identity": args.rerank_identity,
        "embedding_model": args.embedding_model,
        "embedding_revision": args.embedding_revision,
        "embedding_dimension": args.embedding_dimension,
        "rerank_model": args.rerank_model,
        "rerank_revision": args.rerank_revision,
        "inference_mode": inference_mode,
    }
    if structured_enabled:
        output.update(
            {
                "structured_json_identity": args.structured_json_identity,
                "structured_json_model": args.structured_json_model,
                "structured_json_revision": args.structured_json_revision,
            }
        )
    if args.stage_only:
        output["activation"] = "deferred"
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0

    evidence: object = {}
    if args.evidence_file is not None:
        if not args.evidence_file.is_file() or args.evidence_file.is_symlink():
            raise SystemExit("control_evidence_file_missing_or_unsafe")
        evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise SystemExit("control_evidence_file_invalid")
    activated = request_json(
        args.base_url,
        f"/config/revisions/{revision}/activate",
        token,
        method="POST",
        body={"evidence": evidence} if evidence else {},
        # Activation is a CAS against the *current* snapshot.  The staged
        # revision ETag is the target state and must never be used here.
        etag=etag,
        idempotency_key=f"{stage_key}-activate",
    )
    output["activation"] = (
        activated if isinstance(activated, dict) else {"type": type(activated).__name__}
    )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ControlApiError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
