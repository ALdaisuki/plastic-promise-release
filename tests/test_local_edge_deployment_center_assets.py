from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EDGE_ROOT = REPO_ROOT / "deploy" / "local-edge"


def _asset(name: str) -> str:
    return (EDGE_ROOT / name).read_text(encoding="utf-8")


def test_local_edge_ppctl_bridge_is_explicit_loopback_only_and_preview_limited():
    compose = _asset("compose.yaml")
    entrypoint = _asset("entrypoint.sh")

    # Compose supplies an empty default. An operator must deliberately opt in
    # to a browser-visible adapter and the edge never receives a host mount.
    assert (
        'PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT: "${PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT:-}"' in compose
    )
    assert '"127.0.0.1:${PP_LOCAL_EDGE_PORT:-19021}:8080"' in compose
    assert "volumes:" not in compose
    assert "network_mode: host" not in compose
    assert "privileged:" not in compose
    assert "docker.sock" not in compose

    # The entrypoint accepts a single fixed ppctl v1 endpoint grammar, then
    # constrains both the port and the browser-visible operation set.
    assert "http://127.0.0.1:*/ppctl/v1)" in entrypoint
    assert "''|0*|*[!0-9]*|??????*)" in entrypoint
    assert '"$bridge_port" -gt 65535' in entrypoint
    assert '"operations": ["inspect", "preview"]' in entrypoint
    assert '"method": "POST"' in entrypoint
    assert '"content_type": "application/json"' in entrypoint
    assert '"endpoint": null' in entrypoint
    assert "proxy_pass" not in entrypoint
    assert "docker.sock" not in entrypoint
    assert "ssh" not in entrypoint.casefold()


def test_local_edge_bridge_configuration_is_same_origin_and_csp_constrained():
    nginx = _asset("nginx.conf")
    entrypoint = _asset("entrypoint.sh")

    assert "include /tmp/pp-local-edge-bridge-csp.conf;" in nginx
    assert "location = /pp-local-edge/v1/bridge-config.json" in nginx
    assert "alias /tmp/pp-local-edge-bridge-config.json;" in nginx
    assert 'Cache-Control "no-store, max-age=0"' in nginx
    assert "connect-src 'self'$pp_local_edge_bridge_connect_src" in nginx
    for forbidden_proxy in ("proxy_pass", "fastcgi_pass", "uwsgi_pass", "scgi_pass", "grpc_pass"):
        assert not re.search(rf"^\s*{forbidden_proxy}\s", nginx, flags=re.MULTILINE)

    # The configured origin can only be copied into CSP after the loopback
    # grammar passes; an absent setting leaves the external connect source blank.
    assert 'bridge_connect_src=" http://127.0.0.1:$bridge_port"' in entrypoint
    assert r"set \$pp_local_edge_bridge_connect_src" in entrypoint
    assert "bridge_status=disabled" in entrypoint
    assert "bridge_status=configured" in entrypoint
    assert '> "$bridge_config_path"' in entrypoint
    assert '>> "$bridge_config_path"' in entrypoint


def test_local_edge_image_remains_static_and_non_authoritative_with_bridge_metadata():
    dockerfile = _asset("Dockerfile")
    nginx = _asset("nginx.conf")

    # The exact base image comes from the immutable catalog rather than a
    # floating Dockerfile reference. The static recipe must only consume the
    # catalog-provided build argument.
    assert "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}" in dockerfile
    assert "FROM nginx" not in dockerfile
    assert "USER 101" in dockerfile
    assert "COPY plastic_promise/mcp/dashboard_v2/static/" in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert "canonical state" in dockerfile
    assert "backend proxy configuration" in dockerfile
    assert "server_tokens off;" in nginx
    assert "read_only" not in dockerfile.casefold()
