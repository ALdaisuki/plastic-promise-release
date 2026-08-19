"""Shared fail-closed HTTP boundary for loopback operator transports."""

from __future__ import annotations

import ipaddress
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class PrivateHttpError(RuntimeError):
    """A stable loopback transport failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def validate_loopback_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        raise PrivateHttpError("private_http_base_url_invalid")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise PrivateHttpError("private_http_loopback_required") from exc
    if not address.is_loopback:
        raise PrivateHttpError("private_http_loopback_required")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise PrivateHttpError("private_http_base_url_invalid") from exc
    return value.rstrip("/")


def open_no_redirect(request: urllib.request.Request, *, timeout: float):
    try:
        return _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as exc:
        raise PrivateHttpError("private_http_transport_unavailable") from exc


__all__ = ["PrivateHttpError", "open_no_redirect", "validate_loopback_base_url"]
