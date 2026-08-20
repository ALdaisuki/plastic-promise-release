"""Small authenticated client for the loopback Control API operator seam."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from scripts.private_http_client import (
    PrivateHttpError,
    open_no_redirect,
    validate_loopback_base_url,
)

if TYPE_CHECKING:
    from pathlib import Path


class ControlApiError(RuntimeError):
    """A bounded Control API failure without exposing the bearer token."""


def read_bearer_token(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise ControlApiError("control_token_file_missing_or_unsafe")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ControlApiError("control_token_file_permissions_invalid")
    token = path.read_text(encoding="utf-8").strip()
    if not token or any(character.isspace() for character in token):
        raise ControlApiError("control_token_file_invalid")
    return token


def request_json(
    base_url: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    body: object | None = None,
    etag: str | None = None,
    idempotency_key: str | None = None,
) -> object:
    try:
        base_url = validate_loopback_base_url(base_url)
    except PrivateHttpError as exc:
        raise ControlApiError(str(exc)) from exc
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    if etag:
        headers["If-Match"] = etag
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with open_no_redirect(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ControlApiError(f"control_api_{exc.code}") from exc
    except PrivateHttpError as exc:
        raise ControlApiError(str(exc)) from exc


def safe_config(base_url: str, token: str) -> tuple[dict[str, object], str]:
    payload = request_json(base_url, "/config/safe", token)
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        raise ControlApiError("control_safe_config_invalid")
    etag = payload.get("etag")
    if not isinstance(etag, str) or not etag:
        raise ControlApiError("control_safe_etag_missing")
    return payload["config"], etag


__all__ = [
    "ControlApiError",
    "read_bearer_token",
    "request_json",
    "safe_config",
]
