from __future__ import annotations

import json

import pytest

from scripts import pp_node_smoke


def test_node_env_keeps_authorization_private_and_ignores_cloud_key(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            (
                "PP_LOCAL_NODE_AUTHORIZATION=Bearer private-node-token",
                "PP_LOCAL_NODE_CLOUD_API_KEY=private-cloud-key",
                "PP_LOCAL_NODE_ID=inference-node",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    values, authorization = pp_node_smoke._read_node_env(env_path)

    assert authorization == "Bearer private-node-token"
    assert values == {"PP_LOCAL_NODE_ID": "inference-node"}


def test_node_env_rejects_unknown_sensitive_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PP_LOCAL_NODE_AUTHORIZATION=Bearer private-node-token\n"
        "PP_LOCAL_NODE_UNKNOWN_SECRET=unexpected\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="contains a sensitive key"):
        pp_node_smoke._read_node_env(env_path)


def test_node_request_sends_authorization_without_serializing_it(monkeypatch):
    observed: dict[str, str | None] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"status": "ok"}).encode()

    def _urlopen(request, timeout):
        observed["authorization"] = request.get_header("Authorization")
        observed["timeout"] = str(timeout)
        return _Response()

    monkeypatch.setattr(pp_node_smoke.urllib.request, "urlopen", _urlopen)

    payload, _elapsed = pp_node_smoke._request(
        "http://127.0.0.1:19130",
        "GET",
        "/health",
        authorization="Bearer private-node-token",
    )

    assert payload == {"status": "ok"}
    assert observed == {"authorization": "Bearer private-node-token", "timeout": "120"}
