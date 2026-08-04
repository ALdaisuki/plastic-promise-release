"""Authentication primitives for the loopback-only control plane."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_TOKEN_RE = re.compile(r"[\x21-\x7e]{32,512}")
_TOKEN_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ACTOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")
_ROLE_RANK = {"viewer": 0, "operator": 1, "secret-admin": 2}


class ControlPlaneAuthenticationError(ValueError):
    """Stable authentication or authorization failure."""

    def __init__(self, code: str, *, status_code: int = 401) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ControlPlanePrincipal:
    actor: str
    role: str

    def __post_init__(self) -> None:
        if not _ACTOR_RE.fullmatch(self.actor):
            raise ValueError("control_actor_invalid")
        if self.role not in _ROLE_RANK:
            raise ValueError("control_role_invalid")

    def require(self, role: str) -> None:
        if role not in _ROLE_RANK:
            raise ValueError("control_role_invalid")
        if _ROLE_RANK[self.role] < _ROLE_RANK[role]:
            raise ControlPlaneAuthenticationError(
                "control_role_insufficient",
                status_code=403,
            )


@dataclass(frozen=True)
class ControlPlaneCredential:
    actor: str
    role: str
    token_sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        ControlPlanePrincipal(self.actor, self.role)
        if not _TOKEN_DIGEST_RE.fullmatch(self.token_sha256):
            # Do not distinguish missing, malformed, or otherwise unusable
            # credentials at the public authentication boundary.
            raise ValueError("control_token_invalid")

    @classmethod
    def from_token(cls, actor: str, role: str, token: str) -> ControlPlaneCredential:
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError("control_token_invalid")
        return cls(actor=actor, role=role, token_sha256=_token_sha256(token))


class ControlPlaneAuthenticator:
    """Authenticate one Bearer token without exposing token metadata."""

    def __init__(self, credentials: Sequence[ControlPlaneCredential]) -> None:
        values = tuple(credentials)
        if not values:
            raise ValueError("control_credentials_missing")
        if len({credential.token_sha256 for credential in values}) != len(values):
            raise ValueError("control_tokens_must_be_distinct")
        if len({(credential.actor, credential.role) for credential in values}) != len(values):
            raise ValueError("control_credentials_duplicate")
        self._credentials = values

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, object],
    ) -> ControlPlaneAuthenticator:
        credentials = []
        for role, env_name in (
            ("viewer", "PP_CONTROL_VIEWER_TOKEN_SHA256"),
            ("operator", "PP_CONTROL_OPERATOR_TOKEN_SHA256"),
            ("secret-admin", "PP_CONTROL_SECRET_ADMIN_TOKEN_SHA256"),
        ):
            token_digest = str(environ.get(env_name) or "").strip().casefold()
            credentials.append(
                ControlPlaneCredential(
                    actor=f"control:{role}",
                    role=role,
                    token_sha256=token_digest,
                )
            )
        return cls(credentials)

    def authenticate(self, authorization_values: Sequence[str]) -> ControlPlanePrincipal:
        if len(authorization_values) != 1:
            raise ControlPlaneAuthenticationError("control_token_invalid")
        authorization = authorization_values[0]
        if not authorization.startswith("Bearer "):
            raise ControlPlaneAuthenticationError("control_token_invalid")
        supplied = authorization.removeprefix("Bearer ")
        if not _TOKEN_RE.fullmatch(supplied):
            raise ControlPlaneAuthenticationError("control_token_invalid")

        matched: ControlPlaneCredential | None = None
        supplied_digest = _token_sha256(supplied)
        for credential in self._credentials:
            if hmac.compare_digest(supplied_digest, credential.token_sha256):
                matched = credential
        if matched is None:
            raise ControlPlaneAuthenticationError("control_token_invalid")
        return ControlPlanePrincipal(actor=matched.actor, role=matched.role)


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()
