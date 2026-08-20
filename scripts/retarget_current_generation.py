#!/usr/bin/env python3
"""Retarget Control desired state through the authenticated Control API."""

from __future__ import annotations

import argparse
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
    safe_config,
)


def _current_generation_payload(generation_root: Path) -> dict[str, str]:
    from plastic_promise.core.lancedb_artifact import verify_lancedb_artifact
    from plastic_promise.core.lancedb_generation import GenerationManager

    with GenerationManager(
        generation_root,
        create=False,
        artifact_verifier=verify_lancedb_artifact,
    ) as manager:
        manifest, _index_path, _selection = manager.resolve_verified_current_selection()
    if manifest is None:
        raise ControlApiError("current_generation_unavailable")
    return {
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest.manifest_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--generation-root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9040/api/control/v1")
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    token = read_bearer_token(args.token_file)
    _config, etag = safe_config(args.base_url, token)
    payload = _current_generation_payload(args.generation_root)
    idem = args.idempotency_key or (
        "retarget-"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    result = request_json(
        args.base_url,
        "/generation/retarget-current",
        token,
        method="POST",
        body=payload,
        etag=etag,
        idempotency_key=idem,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ControlApiError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
