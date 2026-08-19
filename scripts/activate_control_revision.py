#!/usr/bin/env python3
"""Activate an existing staged revision through the authenticated Control API."""

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9040/api/control/v1")
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    token = read_bearer_token(args.token_file)
    _config, etag = safe_config(args.base_url, token)
    evidence: dict[str, object] = {}
    if args.evidence_file is not None:
        if not args.evidence_file.is_file() or args.evidence_file.is_symlink():
            raise ControlApiError("control_evidence_file_missing_or_unsafe")
        evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise ControlApiError("control_evidence_file_invalid")
    idem = args.idempotency_key or (
        "activate-"
        + hashlib.sha256(
            json.dumps(
                {"revision": args.revision, "evidence": evidence},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:24]
    )
    body: dict[str, object] = {"evidence": evidence} if args.evidence_file is not None else {}
    result = request_json(
        args.base_url,
        f"/config/revisions/{args.revision}/activate",
        token,
        method="POST",
        body=body,
        etag=etag,
        idempotency_key=idem,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ControlApiError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
