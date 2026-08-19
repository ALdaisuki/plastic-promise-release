"""Server-side proxy between Dashboard V2 and the knowledge ingest service.

The browser never sees ``PP_KNOWLEDGE_API_TOKEN``.  The control plane reads the
token from the knowledge env file and forwards writes to the isolated ingest
service on 127.0.0.1:9050, matching ADR-0004 (Dashboard talks to the isolated
backend with project-scoped authorization; ingestion cannot stall the MCP).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from plastic_promise.knowledge.contracts import (
    knowledge_feature_gate,
    knowledge_state_root,
)

_INGEST_BASE = "http://127.0.0.1:9050"
_INGEST_TIMEOUT_SECONDS = 20.0
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_TOKEN_KEY = "PP_KNOWLEDGE_API_TOKEN"


class KnowledgeProxyError(RuntimeError):
    """Structured proxy failure that never includes the ingest token."""

    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def knowledge_ingest_enabled() -> bool:
    """Return whether the knowledge truth store gate admits ingest writes."""
    return knowledge_feature_gate("PP_KNOWLEDGE_SYSTEM") in {"shadow", "on"}


def read_ingest_token() -> str:
    """Read the ingest Bearer token from the knowledge env file (never logged)."""
    env_file = knowledge_state_root() / "knowledge.env"
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeProxyError(
            "knowledge_ingest_token_unavailable",
            f"cannot read {env_file.name}",
            503,
        ) from exc
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == _TOKEN_KEY:
            token = value.strip()
            if token:
                return token
    raise KnowledgeProxyError(
        "knowledge_ingest_token_missing",
        f"{_TOKEN_KEY} is not configured",
        503,
    )


def forward_upload(data: bytes, content_type: str) -> dict[str, Any]:
    """Quarantine an upload through the ingest service."""
    if not data:
        raise KnowledgeProxyError("knowledge_upload_empty", "Upload body is empty", 400)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise KnowledgeProxyError(
            "knowledge_upload_too_large",
            "Upload exceeds the 8 MiB limit",
            413,
        )
    status, raw = _request(
        "POST",
        "/v1/uploads",
        body=data,
        headers={"Content-Type": content_type},
    )
    return _decode(status, raw)


def forward_submit(payload: dict[str, Any]) -> dict[str, Any]:
    """Register a quarantined upload as a project source."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, raw = _request(
        "POST",
        "/v1/sources",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    return _decode(status, raw)


def forward_job_detail(job_id: str, project_id: str) -> dict[str, Any]:
    """Fetch a project-scoped job from the ingest service."""
    path = (
        "/v1/jobs/"
        + urllib.parse.quote(job_id, safe="")
        + "?project_id="
        + urllib.parse.quote(project_id, safe="")
    )
    status, raw = _request("GET", path)
    return _decode(status, raw)


def _request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    token = read_ingest_token()
    request = urllib.request.Request(_INGEST_BASE + path, method=method, data=body)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Cache-Control", "no-store")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=_INGEST_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else "knowledge_ingest_upstream_error"
        message = (
            error.get("message") if isinstance(error, dict) else "ingest service rejected request"
        )
        raise KnowledgeProxyError(
            str(code),
            str(message)[:300],
            int(exc.code),
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise KnowledgeProxyError(
            "knowledge_ingest_unreachable",
            "ingest service unreachable on 127.0.0.1:9050",
            503,
        ) from exc


def _decode(status: int, raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KnowledgeProxyError(
            "knowledge_ingest_invalid_response",
            "ingest service returned a non-JSON response",
            502,
        ) from exc
    if not isinstance(payload, dict):
        raise KnowledgeProxyError(
            "knowledge_ingest_invalid_response",
            "ingest service returned an unexpected payload shape",
            502,
        )
    return payload
