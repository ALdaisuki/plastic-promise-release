"""Private loopback transport verification for local inference nodes.

The controlled configuration records no endpoint or credential.  A server-only
resolver supplies a loopback tunnel endpoint at runtime; this module probes the
node's fixed protocol and returns non-secret identity and capacity evidence for
the registration authority.  It intentionally never exposes the resolver's
address in result objects or error messages.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import requests

from plastic_promise.core.node_governance import (
    NodeExecutionFailure,
    NodeExecutionResult,
    NodeGovernanceError,
    NodeHealthEvidence,
    NodeIdentityEvidence,
    NodeRegistration,
    NodeWorkLease,
)
from plastic_promise.core.structured_intent import structured_intent_digest
from plastic_promise.core.structured_token_budget import (
    UNBOUNDED_STRUCTURED_TOKEN_LIMIT,
    structured_tokens_allowed,
)

_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_EMBEDDING_INPUT_BYTES = 1 * 1024 * 1024
_MAX_RERANK_QUERY_BYTES = 16 * 1024
_MAX_RERANK_DOCUMENT_BYTES = 48 * 1024
_MAX_RERANK_DOCUMENTS = 128
_MAX_STRUCTURED_SYSTEM_PROMPT_BYTES = 32 * 1024
_MAX_STRUCTURED_USER_PAYLOAD_BYTES = 256 * 1024
_MAX_STRUCTURED_OUTPUT_BYTES = 256 * 1024
_MAX_STRUCTURED_TOKENS = UNBOUNDED_STRUCTURED_TOKEN_LIMIT
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_AUTHORIZATION_RE = re.compile(r"\ABearer [A-Za-z0-9._~+/=-]{1,4096}\Z")

# Health/identity probes are cheap, but the first request to a resident
# llama.cpp model can spend several seconds loading CUDA graphs and warming
# the model.  Keep that budget bounded and operation-specific instead of
# forcing every request through the old five-second transport timeout.
_DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 20.0
_DEFAULT_RERANK_TIMEOUT_SECONDS = 15.0
_DEFAULT_STRUCTURED_JSON_TIMEOUT_SECONDS = 45.0


class PrivateNodeEndpointResolver(Protocol):
    """Resolve a registered node ID through server-private runtime material."""

    def resolve(self, node_id: str) -> PrivateNodeEndpoint: ...


class HttpResponse(Protocol):
    status_code: int
    content: bytes

    def json(self) -> object: ...


class HttpClient(Protocol):
    def get(self, url: str, **kwargs: object) -> HttpResponse: ...

    def post(self, url: str, **kwargs: object) -> HttpResponse: ...


@dataclass(frozen=True, repr=False)
class PrivateNodeEndpoint:
    """Runtime-only loopback endpoint.  Never store or return this publicly."""

    node_id: str
    transport_id: str
    base_url: str = field(repr=False)
    authorization: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_private_endpoint_node_invalid")
        _identifier(self.transport_id, "node_private_endpoint_transport_invalid")
        normalized = _loopback_base_url(self.base_url)
        if not valid_private_node_authorization(self.authorization):
            raise NodeGovernanceError("node_private_endpoint_auth_invalid")
        object.__setattr__(self, "base_url", normalized)

    def __repr__(self) -> str:
        return (
            "PrivateNodeEndpoint(node_id="
            f"{self.node_id!r}, transport_id={self.transport_id!r}, base_url='<private>')"
        )


def valid_private_node_authorization(value: object) -> bool:
    """Return whether ``value`` is one bounded canonical Bearer header value."""

    return isinstance(value, str) and _AUTHORIZATION_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class PrivateNodeTransportObservation:
    """Server-probed, non-secret registration and health evidence."""

    registration: NodeRegistration
    health: NodeHealthEvidence


class PrivateNodeTransportProbe:
    """Probe a local-node protocol using only a private endpoint resolver."""

    def __init__(
        self,
        resolver: PrivateNodeEndpointResolver,
        *,
        timeout_seconds: float = 5.0,
        embedding_timeout_seconds: float = _DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
        rerank_timeout_seconds: float = _DEFAULT_RERANK_TIMEOUT_SECONDS,
        structured_json_timeout_seconds: float = _DEFAULT_STRUCTURED_JSON_TIMEOUT_SECONDS,
        http_client: HttpClient | None = None,
    ) -> None:
        if not callable(getattr(resolver, "resolve", None)):
            raise NodeGovernanceError("node_private_resolver_invalid")
        self._timeout_seconds = _validate_timeout(timeout_seconds, "node_private_timeout_invalid")
        self._embedding_timeout_seconds = _validate_timeout(
            embedding_timeout_seconds,
            "node_private_embedding_timeout_invalid",
        )
        self._rerank_timeout_seconds = _validate_timeout(
            rerank_timeout_seconds,
            "node_private_rerank_timeout_invalid",
        )
        self._structured_json_timeout_seconds = _validate_timeout(
            structured_json_timeout_seconds,
            "node_private_structured_json_timeout_invalid",
        )
        self._resolver = resolver
        self._http = http_client or requests.Session()

    def probe(self, registration: NodeRegistration) -> PrivateNodeTransportObservation:
        """Validate `/health` and `/v1/identity` without trusting caller evidence."""

        if not isinstance(registration, NodeRegistration):
            raise NodeGovernanceError("node_registration_invalid")
        try:
            endpoint = self._resolver.resolve(registration.node_id)
        except NodeGovernanceError:
            raise
        except Exception as exc:
            raise NodeGovernanceError("node_private_transport_unavailable") from exc
        if not isinstance(endpoint, PrivateNodeEndpoint):
            raise NodeGovernanceError("node_private_endpoint_invalid")
        if (
            endpoint.node_id != registration.node_id
            or endpoint.transport_id != registration.transport_id
        ):
            raise NodeGovernanceError("node_private_transport_binding_invalid")

        headers = {"Authorization": endpoint.authorization}
        health_payload = self._get_json(endpoint, "/health", headers)
        identity_payload = self._get_json(endpoint, "/v1/identity", headers)
        identity = _identity(identity_payload)
        capabilities = _capabilities(identity_payload)
        if (
            not isinstance(health_payload, Mapping)
            or health_payload.get("status") != "ok"
            or health_payload.get("protocol_version") != identity.protocol_version
        ):
            raise NodeGovernanceError("node_private_health_invalid")
        queue_depth, available_slots = _health_capacity(
            health_payload, max_concurrency=registration.max_concurrency
        )
        transport_evidence = _transport_evidence(
            registration=registration,
            identity=identity,
            capabilities=capabilities,
        )
        verified_registration = replace(registration, transport_evidence=transport_evidence)
        health = NodeHealthEvidence(
            node_id=registration.node_id,
            observed_identity=identity,
            capabilities=capabilities,
            queue_depth=queue_depth,
            available_slots=available_slots,
        )
        return PrivateNodeTransportObservation(verified_registration, health)

    def discover_registration(
        self,
        *,
        node_id: str,
        node_kind: str = "remote-node",
        max_concurrency: int = 1,
    ) -> NodeRegistration:
        """Discover an untrusted declaration through the private loopback path.

        The returned object is only a candidate.  It still must be checked
        against the active controlled revision, deployment manifest, and a
        second private transport probe before the registry accepts it.
        """

        try:
            endpoint = self._resolver.resolve(node_id)
        except NodeGovernanceError:
            raise
        except Exception as exc:
            raise NodeGovernanceError("node_private_transport_unavailable") from exc
        if not isinstance(endpoint, PrivateNodeEndpoint) or endpoint.node_id != node_id:
            raise NodeGovernanceError("node_private_endpoint_invalid")
        headers = {"Authorization": endpoint.authorization}
        identity_payload = self._get_json(endpoint, "/v1/identity", headers)
        identity = _identity_for_node(identity_payload, node_id=node_id)
        capabilities = _capabilities(identity_payload)
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool):
            raise NodeGovernanceError("node_max_concurrency_invalid")
        return NodeRegistration(
            node_id=node_id,
            node_kind=node_kind,
            transport_id=endpoint.transport_id,
            # This declaration is intentionally replaced by ``probe`` with a
            # server-derived digest before it is persisted.
            transport_evidence=(
                "sha256:"
                + hashlib.sha256(
                    ("discovery:" + node_id + ":" + endpoint.transport_id).encode()
                ).hexdigest()
            ),
            expected_identity=identity,
            capabilities=capabilities,
            max_concurrency=max_concurrency,
        )

    def execute_embedding(self, lease: NodeWorkLease, *, input_text: str) -> NodeExecutionResult:
        """Run one bounded embedding request after rechecking full node identity.

        This remains a private server adapter: it accepts a scheduler-issued
        lease plus source text already loaded from canonical SQLite, resolves a
        loopback transport itself, and returns only non-source derived output.
        It never reveals endpoint, authorization, request text, or response
        body in results or error messages.
        """

        if not isinstance(lease, NodeWorkLease) or lease.resolved.operation != "embedding":
            raise NodeExecutionFailure("node_private_embedding_lease_invalid")
        if not isinstance(input_text, str) or not input_text.strip():
            raise NodeExecutionFailure("node_private_embedding_input_invalid")
        if len(input_text.encode("utf-8")) > _MAX_EMBEDDING_INPUT_BYTES:
            raise NodeExecutionFailure("node_private_embedding_input_too_large")
        try:
            endpoint = self._resolve_bound_endpoint(
                node_id=lease.node_id,
                transport_id=lease.transport_id,
            )
            headers = {"Authorization": endpoint.authorization}
            identity_payload = self._get_json(endpoint, "/v1/identity", headers)
            identity = _identity_for_node(identity_payload, node_id=lease.node_id)
            capabilities = _capabilities(identity_payload)
            if "embedding" not in capabilities:
                raise NodeGovernanceError("node_private_embedding_capability_missing")
            if identity.embedding_key != lease.resolved.required_identity:
                raise NodeGovernanceError("node_private_embedding_identity_drift")
            started = time.monotonic()
            payload = self._post_json(
                endpoint,
                "/v1/embeddings",
                headers,
                {"input": [input_text]},
                timeout_seconds=self._embedding_timeout_seconds,
            )
            latency_ms = max(0.001, (time.monotonic() - started) * 1000)
            vector = _embedding_vector(payload, identity)
            return NodeExecutionResult(
                latency_ms=latency_ms,
                evidence={
                    "transport_id": lease.transport_id,
                    "embedding_identity": identity.embedding_key,
                    "embedding_dimension": identity.embedding_dimension,
                },
                result={
                    "embedding": vector,
                    "embedding_identity": identity.embedding_key,
                    "embedding_model": identity.embedding_model,
                    "embedding_revision": identity.embedding_revision,
                    "embedding_dimension": identity.embedding_dimension,
                    "embedding_normalization": identity.embedding_normalization,
                },
            )
        except NodeExecutionFailure:
            raise
        except NodeGovernanceError as exc:
            raise NodeExecutionFailure(exc.code) from exc
        except Exception as exc:
            raise NodeExecutionFailure("node_private_embedding_failed") from exc

    def execute_rerank(
        self,
        lease: NodeWorkLease,
        *,
        query: str,
        documents: list[str],
    ) -> NodeExecutionResult:
        """Run one private rerank request with identity re-probe.

        Query and document text stay in this process and the private loopback
        transport; only score/index pairs and non-secret identity evidence are
        returned to the governed server runtime.
        """

        if not isinstance(lease, NodeWorkLease) or lease.resolved.operation != "rerank":
            raise NodeExecutionFailure("node_private_rerank_lease_invalid")
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode("utf-8")) > _MAX_RERANK_QUERY_BYTES
        ):
            raise NodeExecutionFailure("node_private_rerank_input_invalid")
        if (
            not isinstance(documents, list)
            or not 2 <= len(documents) <= _MAX_RERANK_DOCUMENTS
            or any(
                not isinstance(document, str)
                or not document.strip()
                or len(document.encode("utf-8")) > _MAX_RERANK_DOCUMENT_BYTES
                for document in documents
            )
        ):
            raise NodeExecutionFailure("node_private_rerank_input_invalid")
        try:
            endpoint = self._resolve_bound_endpoint(
                node_id=lease.node_id,
                transport_id=lease.transport_id,
            )
            headers = {"Authorization": endpoint.authorization}
            identity_payload = self._get_json(endpoint, "/v1/identity", headers)
            identity = _identity_for_node(identity_payload, node_id=lease.node_id)
            capabilities = _capabilities(identity_payload)
            if "rerank" not in capabilities:
                raise NodeGovernanceError("node_private_rerank_capability_missing")
            if identity.rerank_key != lease.resolved.required_identity:
                raise NodeGovernanceError("node_private_rerank_identity_drift")
            started = time.monotonic()
            payload = self._post_json(
                endpoint,
                "/v1/rerank",
                headers,
                {"query": query, "documents": documents, "top_k": len(documents)},
                response_error_code="node_private_rerank_response_invalid",
                timeout_seconds=self._rerank_timeout_seconds,
            )
            latency_ms = max(0.001, (time.monotonic() - started) * 1000)
            scores = _rerank_scores(payload, identity, candidate_count=len(documents))
            return NodeExecutionResult(
                latency_ms=latency_ms,
                evidence={
                    "transport_id": lease.transport_id,
                    "rerank_identity": identity.rerank_key,
                },
                result={
                    "rerank_scores": scores,
                    "rerank_identity": identity.rerank_key,
                    "rerank_model": identity.rerank_model,
                    "rerank_revision": identity.rerank_revision,
                },
            )
        except NodeExecutionFailure:
            raise
        except NodeGovernanceError as exc:
            raise NodeExecutionFailure(exc.code) from exc
        except Exception as exc:
            raise NodeExecutionFailure("node_private_rerank_failed") from exc

    def execute_structured_json(
        self,
        lease: NodeWorkLease,
        *,
        user_payload: Mapping[str, object],
        max_tokens: int,
        intent_id: str | None = None,
        schema_id: str | None = None,
        input_digest: str | None = None,
    ) -> NodeExecutionResult:
        """Run bounded structured JSON inference on a registered compute node.

        The server receives only the validated object and immutable model
        identity.  Provider envelopes, credentials, endpoint material and
        unbounded exception details never cross this seam.
        """

        if not isinstance(lease, NodeWorkLease) or lease.resolved.operation != "structured-json":
            raise NodeExecutionFailure("node_private_structured_json_lease_invalid")
        if (
            not isinstance(user_payload, Mapping)
            or not _bounded_json_object(user_payload, _MAX_STRUCTURED_USER_PAYLOAD_BYTES)
            or not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not structured_tokens_allowed(max_tokens, _MAX_STRUCTURED_TOKENS)
        ):
            raise NodeExecutionFailure("node_private_structured_json_input_invalid")
        if (
            not isinstance(intent_id, str)
            or not intent_id.strip()
            or not isinstance(schema_id, str)
            or not schema_id.strip()
            or not isinstance(input_digest, str)
            or _SHA256_RE.fullmatch(input_digest) is None
        ):
            raise NodeExecutionFailure("node_private_structured_json_intent_invalid")
        expected_input_digest = structured_intent_digest(
            project_id=lease.resolved.project_id,
            intent_id=intent_id,
            schema_id=schema_id,
            user_payload=user_payload,
        )
        if not hmac.compare_digest(input_digest, expected_input_digest):
            raise NodeExecutionFailure("node_private_structured_json_intent_digest_mismatch")
        try:
            endpoint = self._resolve_bound_endpoint(
                node_id=lease.node_id,
                transport_id=lease.transport_id,
            )
            headers = {"Authorization": endpoint.authorization}
            identity_payload = self._get_json(endpoint, "/v1/identity", headers)
            identity = _identity_for_node(identity_payload, node_id=lease.node_id)
            capabilities = _capabilities(identity_payload)
            if "structured-json" not in capabilities:
                raise NodeGovernanceError("node_private_structured_json_capability_missing")
            required_identity = lease.resolved.required_identity
            if identity.structured_json_key is None or identity.structured_json_key != required_identity:
                raise NodeGovernanceError("node_private_structured_json_identity_drift")
            started = time.monotonic()
            intent = {
                "intent_id": intent_id,
                "schema_id": schema_id,
                "input_digest": input_digest,
                "project_id": lease.resolved.project_id,
            }
            payload = self._post_json(
                endpoint,
                "/v1/structured-json",
                headers,
                {
                    "intent": intent,
                    "user_payload": dict(user_payload),
                    "max_tokens": max_tokens,
                },
                response_error_code="node_private_structured_json_response_invalid",
                max_response_bytes=_MAX_STRUCTURED_OUTPUT_BYTES,
                timeout_seconds=self._structured_json_timeout_seconds,
            )
            latency_ms = max(0.001, (time.monotonic() - started) * 1000)
            output = _structured_json_output(payload, identity)
            return NodeExecutionResult(
                latency_ms=latency_ms,
                evidence={
                    "transport_id": lease.transport_id,
                    "structured_json_identity": identity.structured_json_key,
                    "provider_class": identity.provider_class,
                    "intent_id": intent_id,
                    "input_digest": input_digest,
                },
                result={
                    "structured_json": output,
                    "structured_json_identity": identity.structured_json_key,
                    "structured_json_model": identity.structured_json_model,
                    "structured_json_revision": identity.structured_json_revision,
                },
            )
        except NodeExecutionFailure:
            raise
        except NodeGovernanceError as exc:
            raise NodeExecutionFailure(exc.code) from exc
        except Exception as exc:
            raise NodeExecutionFailure("node_private_structured_json_failed") from exc

    def _resolve_bound_endpoint(self, *, node_id: str, transport_id: str) -> PrivateNodeEndpoint:
        try:
            endpoint = self._resolver.resolve(node_id)
        except NodeGovernanceError:
            raise
        except Exception as exc:
            raise NodeGovernanceError("node_private_transport_unavailable") from exc
        if not isinstance(endpoint, PrivateNodeEndpoint):
            raise NodeGovernanceError("node_private_endpoint_invalid")
        if endpoint.node_id != node_id or endpoint.transport_id != transport_id:
            raise NodeGovernanceError("node_private_transport_binding_invalid")
        return endpoint

    def _get_json(
        self,
        endpoint: PrivateNodeEndpoint,
        path: str,
        headers: Mapping[str, str],
    ) -> object:
        try:
            response = self._http.get(
                endpoint.base_url + path,
                timeout=self._timeout_seconds,
                headers=dict(headers),
            )
        except Exception as exc:
            raise NodeGovernanceError("node_private_transport_unavailable") from exc
        content = getattr(response, "content", b"")
        if not isinstance(content, bytes) or len(content) > _MAX_RESPONSE_BYTES:
            raise NodeGovernanceError("node_private_transport_response_invalid")
        if getattr(response, "status_code", None) != 200:
            raise NodeGovernanceError("node_private_transport_response_invalid")
        try:
            return response.json()
        except Exception as exc:
            raise NodeGovernanceError("node_private_transport_response_invalid") from exc

    def _post_json(
        self,
        endpoint: PrivateNodeEndpoint,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        *,
        response_error_code: str = "node_private_embedding_response_invalid",
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        timeout_seconds: float | None = None,
    ) -> object:
        try:
            response = self._http.post(
                endpoint.base_url + path,
                timeout=(
                    self._timeout_seconds if timeout_seconds is None else timeout_seconds
                ),
                headers=dict(headers),
                json=cast("Any", dict(payload)),
            )
        except Exception as exc:
            raise NodeGovernanceError("node_private_transport_unavailable") from exc
        content = getattr(response, "content", b"")
        if not isinstance(content, bytes) or len(content) > max_response_bytes:
            raise NodeGovernanceError("node_private_transport_response_invalid")
        if getattr(response, "status_code", None) != 200:
            node_error = _node_error_code(response)
            if node_error == "node_embedding_identity_drift":
                raise NodeGovernanceError("node_private_embedding_identity_drift")
            if node_error == "node_rerank_identity_drift":
                raise NodeGovernanceError("node_private_rerank_identity_drift")
            if node_error == "node_structured_json_identity_drift":
                raise NodeGovernanceError("node_private_structured_json_identity_drift")
            if node_error in {"node_overloaded", "node_resource_busy"}:
                # Accelerator overload is an expected, retryable admission
                # result. Do not turn it into a transport failure.
                raise NodeGovernanceError("node_overloaded")
            raise NodeGovernanceError(response_error_code)
        try:
            return response.json()
        except Exception as exc:
            raise NodeGovernanceError(response_error_code) from exc


def _node_error_code(response: object) -> str | None:
    """Read one allowlisted public node error without retaining response detail."""

    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("error")
    return (
        value
        if value
        in {
            "node_embedding_identity_drift",
            "node_rerank_identity_drift",
            "node_structured_json_identity_drift",
            "node_overloaded",
            # Rolling upgrades may still have an older compute node.
            "node_resource_busy",
        }
        else None
    )


def _validate_timeout(value: object, code: str) -> float:
    """Validate one bounded HTTP timeout without accepting booleans or NaN."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.1 <= float(value) <= 60.0
    ):
        raise NodeGovernanceError(code)
    return float(value)


def _loopback_base_url(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) > 512:
        raise NodeGovernanceError("node_private_endpoint_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise NodeGovernanceError("node_private_endpoint_invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise NodeGovernanceError("node_private_endpoint_invalid")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}"


def _identity(value: object) -> NodeIdentityEvidence:
    if not isinstance(value, Mapping):
        raise NodeGovernanceError("node_private_identity_invalid")
    try:
        return NodeIdentityEvidence.from_dict(value)
    except NodeGovernanceError as exc:
        raise NodeGovernanceError("node_private_identity_invalid") from exc


def _identity_for_node(value: object, *, node_id: str) -> NodeIdentityEvidence:
    if not isinstance(value, Mapping) or value.get("node_id") != node_id:
        raise NodeGovernanceError("node_private_identity_invalid")
    return _identity(value)


def _capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        raise NodeGovernanceError("node_private_identity_invalid")
    raw = value.get("capabilities")
    if not isinstance(raw, list):
        raise NodeGovernanceError("node_private_capabilities_invalid")
    aliases = {
        "embeddings": "embedding",
        "embedding": "embedding",
        "rerank": "rerank",
        "structured-json": "structured-json",
    }
    mapped: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in aliases:
            raise NodeGovernanceError("node_private_capabilities_invalid")
        mapped.append(aliases[item])
    if not mapped or len(set(mapped)) != len(mapped):
        raise NodeGovernanceError("node_private_capabilities_invalid")
    return tuple(sorted(mapped))


def _bounded_json_object(value: Mapping[str, object], maximum: int) -> bool:
    """Accept only finite, JSON-serializable object payloads within a byte cap."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    return len(encoded) <= maximum


def _structured_json_output(
    payload: object,
    identity: NodeIdentityEvidence,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise NodeGovernanceError("node_private_structured_json_response_invalid")
    expected_identity = identity.structured_json_key
    if expected_identity is None or payload.get("structured_json_identity") != (
        f"{identity.structured_json_model}@{identity.structured_json_revision}"
    ):
        raise NodeGovernanceError("node_private_structured_json_result_identity_drift")
    output = payload.get("output")
    if not isinstance(output, Mapping) or not _bounded_json_object(output, _MAX_STRUCTURED_OUTPUT_BYTES):
        raise NodeGovernanceError("node_private_structured_json_response_invalid")
    return dict(output)


def _embedding_vector(payload: object, identity: NodeIdentityEvidence) -> list[float]:
    if not isinstance(payload, Mapping):
        raise NodeGovernanceError("node_private_embedding_response_invalid")
    expected_public_identity = f"{identity.embedding_model}@{identity.embedding_revision}"
    if (
        payload.get("embedding_identity") != expected_public_identity
        or payload.get("dimension") != identity.embedding_dimension
    ):
        raise NodeGovernanceError("node_private_embedding_identity_drift")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
        raise NodeGovernanceError("node_private_embedding_response_invalid")
    row = data[0]
    vector = row.get("embedding")
    if row.get("index") != 0 or not isinstance(vector, list):
        raise NodeGovernanceError("node_private_embedding_response_invalid")
    if len(vector) != identity.embedding_dimension:
        raise NodeGovernanceError("node_private_embedding_dimension_invalid")
    normalized: list[float] = []
    for component in vector:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise NodeGovernanceError("node_private_embedding_vector_invalid")
        numeric = float(component)
        if not math.isfinite(numeric):
            raise NodeGovernanceError("node_private_embedding_vector_invalid")
        normalized.append(numeric)
    if not any(normalized):
        raise NodeGovernanceError("node_private_embedding_vector_invalid")
    return normalized


def _rerank_scores(
    payload: object,
    identity: NodeIdentityEvidence,
    *,
    candidate_count: int,
) -> list[dict[str, float | int]]:
    if not isinstance(payload, Mapping):
        raise NodeGovernanceError("node_private_rerank_response_invalid")
    expected_identity = f"{identity.rerank_model}@{identity.rerank_revision}"
    if payload.get("rerank_identity") != expected_identity:
        raise NodeGovernanceError("node_private_rerank_result_identity_drift")
    values = payload.get("results")
    if not isinstance(values, list) or len(values) != candidate_count:
        raise NodeGovernanceError("node_private_rerank_response_invalid")
    checked: list[dict[str, float | int]] = []
    seen: set[int] = set()
    for item in values:
        if not isinstance(item, Mapping):
            raise NodeGovernanceError("node_private_rerank_response_invalid")
        index = item.get("index")
        score = item.get("score")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < candidate_count
            or index in seen
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            raise NodeGovernanceError("node_private_rerank_response_invalid")
        seen.add(index)
        checked.append({"index": index, "score": float(score)})
    return checked


def _transport_evidence(
    *,
    registration: NodeRegistration,
    identity: NodeIdentityEvidence,
    capabilities: tuple[str, ...],
) -> str:
    material = {
        "node_id": registration.node_id,
        "transport_id": registration.transport_id,
        "identity": identity.to_dict(),
        "capabilities": list(capabilities),
        "max_concurrency": registration.max_concurrency,
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _health_capacity(payload: Mapping[str, object], *, max_concurrency: int) -> tuple[int, int]:
    """Require the node's live load report to match its declared capacity."""

    queue_depth = payload.get("queue_depth")
    available_slots = payload.get("available_slots")
    reported_maximum = payload.get("max_concurrency")
    if (
        not isinstance(queue_depth, int)
        or isinstance(queue_depth, bool)
        or queue_depth < 0
        or not isinstance(available_slots, int)
        or isinstance(available_slots, bool)
        or not 0 <= available_slots <= max_concurrency
        or reported_maximum != max_concurrency
    ):
        raise NodeGovernanceError("node_private_capacity_invalid")
    return queue_depth, available_slots


def _identifier(value: object, code: str) -> str:
    import re

    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_.:-]{1,127}", value) is None:
        raise NodeGovernanceError(code)
    return value


__all__ = [
    "PrivateNodeEndpoint",
    "PrivateNodeEndpointResolver",
    "PrivateNodeTransportObservation",
    "PrivateNodeTransportProbe",
]
