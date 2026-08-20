"""Tests for server-private loopback endpoint resolution."""

from __future__ import annotations

import json
import os

import pytest

from plastic_promise.core.node_governance import NodeGovernanceError
from plastic_promise.core.node_private_runtime import RuntimePrivateNodeEndpointResolver
from plastic_promise.core.node_private_transport import PrivateNodeEndpoint


def _write_runtime_file(tmp_path, document, *, mode: int = 0o600):  # type: ignore[no-untyped-def]
    path = tmp_path / "private-node-endpoints.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_resolver_reads_loopback_endpoint_and_keeps_auth_out_of_repr(tmp_path):
    path = _write_runtime_file(
        tmp_path,
        {
            "schema": "private-node-endpoints/v1",
            "nodes": [
                {
                    "node_id": "remote-a",
                    "transport_id": "transport:remote-a",
                    "base_url": "http://127.0.0.1:18443",
                    "authorization_env": "PP_NODE_AUTH_REMOTE_A",
                }
            ],
        },
    )
    resolver = RuntimePrivateNodeEndpointResolver.from_environment(
        {
            "PP_NODE_PRIVATE_ENDPOINTS_FILE": str(path),
            "PP_NODE_AUTH_REMOTE_A": "Bearer test-secret-not-to-log",
        }
    )

    endpoint = resolver.resolve("remote-a")
    assert endpoint.node_id == "remote-a"
    assert endpoint.base_url == "http://127.0.0.1:18443"
    assert "test-secret-not-to-log" not in repr(endpoint)
    assert "127.0.0.1:18443" not in repr(endpoint)


def test_resolver_refuses_missing_auth_nonloopback_and_unsafe_permissions(tmp_path):
    document = {
        "schema": "private-node-endpoints/v1",
        "nodes": [
            {
                "node_id": "remote-a",
                "transport_id": "transport:remote-a",
                "base_url": "http://192.168.5.14:8000",
                "authorization_env": "PP_NODE_AUTH_REMOTE_A",
            }
        ],
    }
    path = _write_runtime_file(tmp_path, document)
    with pytest.raises(NodeGovernanceError, match="node_private_runtime_auth_unavailable"):
        RuntimePrivateNodeEndpointResolver.from_environment(
            {"PP_NODE_PRIVATE_ENDPOINTS_FILE": str(path)}
        )
    with pytest.raises(NodeGovernanceError, match="node_private_runtime_config_invalid"):
        RuntimePrivateNodeEndpointResolver.from_environment(
            {
                "PP_NODE_PRIVATE_ENDPOINTS_FILE": str(path),
                "PP_NODE_AUTH_REMOTE_A": "Bearer test-secret",
            }
        )

    loopback = dict(document)
    loopback["nodes"] = [
        {
            "node_id": "remote-a",
            "transport_id": "transport:remote-a",
            "base_url": "http://127.0.0.1:8000",
        }
    ]
    unsafe = _write_runtime_file(tmp_path, loopback, mode=0o644)
    if os.name != "nt":
        with pytest.raises(NodeGovernanceError, match="node_private_runtime_config_permissions"):
            RuntimePrivateNodeEndpointResolver.from_environment(
                {"PP_NODE_PRIVATE_ENDPOINTS_FILE": str(unsafe)}
            )


@pytest.mark.parametrize(
    "authorization",
    ["", "   ", "Basic private-only", "Bearer ", "Bearer private-only\r\nInjected: yes"],
)
def test_resolver_requires_one_well_formed_bearer_authorization(tmp_path, authorization):
    path = _write_runtime_file(
        tmp_path,
        {
            "schema": "private-node-endpoints/v1",
            "nodes": [
                {
                    "node_id": "remote-a",
                    "transport_id": "transport:remote-a",
                    "base_url": "http://127.0.0.1:8000",
                    "authorization_env": "PP_NODE_AUTH_REMOTE_A",
                }
            ],
        },
    )

    with pytest.raises(NodeGovernanceError, match="node_private_runtime_auth_unavailable"):
        RuntimePrivateNodeEndpointResolver.from_environment(
            {
                "PP_NODE_PRIVATE_ENDPOINTS_FILE": str(path),
                "PP_NODE_AUTH_REMOTE_A": authorization,
            }
        )


def test_resolver_refuses_an_endpoint_without_an_authorization_binding(tmp_path):
    path = _write_runtime_file(
        tmp_path,
        {
            "schema": "private-node-endpoints/v1",
            "nodes": [
                {
                    "node_id": "remote-a",
                    "transport_id": "transport:remote-a",
                    "base_url": "http://127.0.0.1:8000",
                }
            ],
        },
    )

    with pytest.raises(NodeGovernanceError, match="node_private_runtime_config_invalid"):
        RuntimePrivateNodeEndpointResolver.from_environment(
            {"PP_NODE_PRIVATE_ENDPOINTS_FILE": str(path)}
        )


@pytest.mark.parametrize(
    "authorization",
    ["", "Basic private-only", "Bearer ", "Bearer private-only\nInjected: yes"],
)
def test_private_endpoint_fails_closed_for_invalid_authorization(authorization):
    with pytest.raises(NodeGovernanceError, match="node_private_endpoint_auth_invalid"):
        PrivateNodeEndpoint(
            node_id="remote-a",
            transport_id="transport:remote-a",
            base_url="http://127.0.0.1:8000",
            authorization=authorization,
        )


def test_resolver_uses_exact_opaque_node_binding_and_refuses_unknown_node(tmp_path):
    path = _write_runtime_file(
        tmp_path,
        {
            "schema": "private-node-endpoints/v1",
            "nodes": [
                {
                    "node_id": "remote-a",
                    "transport_id": "transport:remote-a",
                    "base_url": "http://[::1]:8000",
                    "authorization_env": "PP_NODE_AUTH_REMOTE_A",
                }
            ],
        },
    )
    resolver = RuntimePrivateNodeEndpointResolver.from_environment(
        {
            "PP_NODE_PRIVATE_ENDPOINTS_FILE": str(path),
            "PP_NODE_AUTH_REMOTE_A": "Bearer private-only",
        }
    )

    assert resolver.resolve("remote-a").transport_id == "transport:remote-a"
    with pytest.raises(NodeGovernanceError, match="node_private_endpoint_unconfigured"):
        resolver.resolve("remote-b")
