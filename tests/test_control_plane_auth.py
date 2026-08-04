from __future__ import annotations

import pytest

from plastic_promise.control_plane.auth import (
    ControlPlaneAuthenticationError,
    ControlPlaneAuthenticator,
    ControlPlaneCredential,
)


def _token(character: str) -> str:
    return character * 48


def _authenticator() -> ControlPlaneAuthenticator:
    return ControlPlaneAuthenticator(
        [
            ControlPlaneCredential.from_token("viewer", "viewer", _token("v")),
            ControlPlaneCredential.from_token("operator", "operator", _token("o")),
            ControlPlaneCredential.from_token("secret", "secret-admin", _token("s")),
        ]
    )


@pytest.mark.parametrize("values", [[], ["Bearer one", "Bearer two"], ["Basic value"]])
def test_authenticator_rejects_missing_duplicate_or_wrong_scheme(values):
    with pytest.raises(ControlPlaneAuthenticationError, match="control_token_invalid"):
        _authenticator().authenticate(values)


def test_authenticator_returns_role_and_enforces_hierarchy():
    principal = _authenticator().authenticate([f"Bearer {_token('o')}"])

    assert principal.actor == "operator"
    assert principal.role == "operator"
    principal.require("viewer")
    principal.require("operator")
    with pytest.raises(ControlPlaneAuthenticationError, match="control_role_insufficient"):
        principal.require("secret-admin")


def test_credentials_require_distinct_high_entropy_tokens():
    with pytest.raises(ValueError, match="control_token_invalid"):
        ControlPlaneCredential.from_token("viewer", "viewer", "short")

    duplicate = _token("x")
    with pytest.raises(ValueError, match="control_tokens_must_be_distinct"):
        ControlPlaneAuthenticator(
            [
                ControlPlaneCredential.from_token("viewer", "viewer", duplicate),
                ControlPlaneCredential.from_token("operator", "operator", duplicate),
            ]
        )


def test_from_env_requires_every_role_token():
    with pytest.raises(ValueError, match="control_token_invalid"):
        ControlPlaneAuthenticator.from_env({"PP_CONTROL_VIEWER_TOKEN_SHA256": "a" * 64})
