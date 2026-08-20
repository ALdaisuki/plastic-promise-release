from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plastic_promise.deployment import (
    DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION,
    PP_COMPUTE_NODE,
    PP_SERVER_BACKEND,
    CapabilityBinding,
    CapabilityLeaseBinding,
    CapabilityResourceBinding,
    ComputeFence,
    ComputeLease,
    ComputeResult,
    EmbeddingIdentity,
    EndpointAuthority,
    EndpointCapability,
    EndpointContractError,
    EndpointContractRegistry,
    EndpointHeartbeat,
    EndpointHello,
    EndpointIdentityEvidence,
    EndpointObservation,
    EndpointProtocol,
    EndpointRequirement,
    EndpointResourceReport,
    GoldenProbeBinding,
    ManifestRevisionRecord,
    admit_endpoint,
    parse_deployment_manifest_v2,
    resolve_deployment_manifest_v2,
    validate_compute_exchange,
)
from plastic_promise.endpoint_roles import (
    COMPUTE_PACKAGE_MANIFEST_SCHEMA_VERSION,
    compute_package_manifest,
    endpoint_role_contract,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
_REVISION = "a" * 40
_RERANK_REVISION = "b" * 40


def test_compute_package_manifest_is_the_closed_capability_and_server_exclusion_authority():
    manifest = compute_package_manifest()

    assert manifest.schema_version == COMPUTE_PACKAGE_MANIFEST_SCHEMA_VERSION
    assert manifest.capability_contracts == (
        "embedding/v1",
        "rerank/v1",
        "structured-json/v1",
    )
    assert manifest.capability_for("structured-json").to_dict() == {
        "kind": "structured-json",
        "contract_version": "structured-json/v1",
        "input_schema": "structured-json-input/v1",
        "result_schema": "structured-json-result/v1",
    }
    assert EndpointCapability("structured-json", "structured-json/v1").to_dict() == {
        "kind": "structured-json",
        "contract_version": "structured-json/v1",
    }
    server_contract = endpoint_role_contract(PP_SERVER_BACKEND)
    assert server_contract.source_exclusions == manifest.server_source_exclusions
    assert not server_contract.includes_source_path("plastic_promise/core/provider_http.py")
    assert server_contract.includes_source_path("plastic_promise/core/node_governance.py")


def test_server_runtime_source_uses_only_the_provider_neutral_embedding_seam():
    repository_root = Path(__file__).resolve().parents[1]
    server_contract = endpoint_role_contract(PP_SERVER_BACKEND)
    assert server_contract.includes_source_path("plastic_promise/core/embedder.py") is False

    excluded = server_contract.source_exclusions
    forbidden_imports = (
        "from plastic_promise.core.embedder import",
        "from plastic_promise.core.reranker import",
        "from plastic_promise.core.inference_provider import",
        "from plastic_promise.core.provider_http import",
        "from plastic_promise.core.backend_inference import",
    )
    for source_path in (repository_root / "plastic_promise").rglob("*.py"):
        relative = source_path.relative_to(repository_root).as_posix()
        if any(relative == path or relative.startswith(f"{path}/") for path in excluded):
            continue
        source = source_path.read_text(encoding="utf-8")
        for forbidden_import in forbidden_imports:
            assert forbidden_import not in source


def test_server_context_engine_starts_without_compute_provider_modules():
    repository_root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import importlib.abc
        import os
        import sys

        blocked = {
            "plastic_promise.core.backend_inference",
            "plastic_promise.core.embedder",
            "plastic_promise.core.inference_provider",
            "plastic_promise.core.provider_http",
            "plastic_promise.core.reranker",
        }

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname in blocked:
                    raise ModuleNotFoundError(fullname)
                return None

        os.environ["PP_ENDPOINT_ROLE"] = "pp-server-backend"
        os.environ["AGENT_USE_SQLITE"] = "0"
        sys.meta_path.insert(0, Blocker())
        from plastic_promise.core.context_engine import ContextEngine
        from plastic_promise.skills.semantic_tool_routing import create_chunk_json_provider

        engine = ContextEngine(use_sqlite=False)
        assert engine is not None
        provider = create_chunk_json_provider(deterministic=True)
        assert provider.identity == "structured-json:unavailable"
        try:
            provider.complete_json(system_prompt="x", user_payload={})
        except RuntimeError as exc:
            assert str(exc) == "structured_json_provider_unavailable"
        else:
            raise AssertionError("server provider seam did not fail closed")
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_compute_compose_templates_require_private_node_authorization():
    repository_root = Path(__file__).resolve().parents[1]
    compose_root = repository_root / "deploy" / "local-inference-node"
    for name in ("compose.yaml", "compose.cpu.yaml", "compose.cuda.yaml"):
        source = (compose_root / name).read_text(encoding="utf-8")
        assert 'PP_LOCAL_NODE_AUTHORIZATION: "${PP_LOCAL_NODE_AUTHORIZATION:?' in source


def test_compute_healthcheck_reuses_private_node_authorization():
    repository_root = Path(__file__).resolve().parents[1]
    source = (repository_root / "deploy" / "local-inference-node" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "Request('http://127.0.0.1:19130/health'" in source
    assert "os.environ['PP_LOCAL_NODE_AUTHORIZATION']" in source
    assert "'Authorization': os.environ['PP_LOCAL_NODE_AUTHORIZATION']" in source


def _digest(character: str) -> str:
    return f"sha256:{character * 64}"


def _identity(*, metric: str = "cosine", golden: str = "c") -> EndpointIdentityEvidence:
    return EndpointIdentityEvidence(
        embedding=EmbeddingIdentity(
            model="BAAI/bge-m3",
            revision=_REVISION,
            dimension=1024,
            normalization="l2",
            metric=metric,
            tokenization="wordpiece",
            pooling="cls",
            artifact_sha256=_digest("a"),
            golden_vector_sha256=_digest(golden),
        )
    )


def _capability_binding(
    *,
    identity: EndpointIdentityEvidence | None = None,
    golden: str = "e",
    max_concurrency: int = 4,
    cancel_supported: bool = True,
    terminal_reasons: tuple[str, ...] = ("completed", "cancelled"),
) -> CapabilityBinding:
    resolved_identity = identity or _identity()
    fingerprint = resolved_identity.fingerprint_for("embedding")
    assert fingerprint is not None
    return CapabilityBinding(
        model_identity_fingerprint=fingerprint,
        input_schema="embedding-input/v1",
        result_schema="embedding-result/v1",
        resources=CapabilityResourceBinding(
            minimum_memory_mib=4_096,
            minimum_model_cache_bytes=4 * 1024**3,
        ),
        max_concurrency=max_concurrency,
        lease=CapabilityLeaseBinding(
            timeout_seconds=30,
            idempotency_key_schema="sha256/v1",
            cancel_supported=cancel_supported,
            terminal_reasons=terminal_reasons,
        ),
        golden_probe=GoldenProbeBinding(
            input_schema="embedding-input/v1",
            result_schema="embedding-result/v1",
            probe_input_sha256=_digest("b"),
            expected_result_sha256=_digest(golden),
        ),
    )


def _manifest(*, profile: str = "local-all-in-one") -> dict[str, object]:
    endpoints: list[dict[str, object]] = [
        {
            "id": "local-edge",
            "role": "pp-local-edge",
            "protocol": {"family": "edge", "major": 1, "minor": 0},
            "capabilities": [],
            "transport_ref": "loopback",
            "resource_policy_ref": "edge-default",
        },
        {
            "id": "server-backend",
            "role": "pp-server-backend",
            "protocol": {"family": "backend", "major": 1, "minor": 0},
            "capabilities": [],
            "transport_ref": "backend-private",
            "resource_policy_ref": "backend-default",
        },
        {
            "id": "compute-node",
            "role": "pp-compute-node",
            "protocol": {"family": "compute", "major": 1, "minor": 2},
            "capabilities": [
                {"kind": "embedding", "contract_version": "embedding/v1"},
                {"kind": "rerank", "contract_version": "rerank/v1"},
            ],
            "max_concurrency": 4,
            "transport_ref": "compute-registry",
            "resource_policy_ref": "compute-default",
        },
    ]
    return {
        "schema_version": DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION,
        "deployment_id": "developer-laptop",
        "profile": profile,
        "modules": {},
        "endpoints": endpoints,
    }


def _observation(
    identity: EndpointIdentityEvidence | None = None,
    *,
    observed_at: datetime = NOW,
    ttl_seconds: int = 30,
    available_slots: int = 2,
    protocol: EndpointProtocol | None = None,
    capability_binding: CapabilityBinding | None = None,
) -> EndpointObservation:
    return EndpointObservation(
        hello=EndpointHello(
            endpoint_id="compute-node",
            role="pp-compute-node",
            protocol=protocol or EndpointProtocol("compute", 1, 2),
            capabilities=(
                EndpointCapability("embedding", "embedding/v1", capability_binding),
                EndpointCapability("rerank", "rerank/v1"),
            ),
            identity=identity or _identity(),
        ),
        heartbeat=EndpointHeartbeat(
            endpoint_id="compute-node",
            boot_id="boot-1",
            sequence=7,
            server_observed_at=observed_at,
            ttl_seconds=ttl_seconds,
        ),
        resources=EndpointResourceReport(
            report_generation=3,
            queue_depth=1,
            active_lease_count=2,
            available_slots=available_slots,
            max_concurrency=4,
            memory_total_mib=16_384,
            memory_free_mib=8_192,
            model_cache_free_bytes=8 * 1024**3,
        ),
    )


def _requirement(
    identity: EndpointIdentityEvidence | None = None,
    *,
    protocol: EndpointProtocol | None = None,
    capability_binding: CapabilityBinding | None = None,
) -> EndpointRequirement:
    return EndpointRequirement(
        capability="embedding",
        contract_version="embedding/v1",
        protocol=protocol or EndpointProtocol("compute", 1, 1),
        required_identity=identity or _identity(),
        allowed_endpoint_ids=("compute-node",),
        capability_binding=capability_binding,
    )


def test_v2_resolution_enforces_server_single_writer_and_sanitises_browser_projection():
    plan = resolve_deployment_manifest_v2(_manifest())

    assert plan.canonical_sqlite_owner == "server-backend"
    assert plan.lancedb_promotion_owner == "server-backend"
    assert plan.receipt_persistence_owner == "server-backend"
    assert plan.authorities_for("server-backend") == (
        "canonical-sqlite-single-writer",
        "lancedb-promotion-decision",
        "deployment-receipt-persistence",
    )
    assert plan.authority_profile_for("server-backend").actions == (
        "canonical-sqlite-write",
        "inference-job-administer",
        "inference-result-accept",
        "task-queue-administer",
        "collaboration-agent-register",
        "collaboration-event-write",
        "collaboration-work-board-write",
        "collaboration-awareness-publish",
        "memory-proposal-promote",
        "knowledge-proposal-promote",
        "lancedb-promotion-decide",
        "merge-govern",
        "deployment-govern",
        "maintenance-govern",
        "deployment-receipt-persist",
    )
    assert plan.authority_profile_for("local-edge").actions == (
        "project-intent-submit",
        "bounded-state-projection-read",
    )
    assert plan.authority_profile_for("compute-node").actions == (
        "bounded-inference-lease",
        "derived-inference-return",
        "node-health-report",
        "node-resource-report",
        "model-identity-report",
        "timing-evidence-report",
    )
    assert plan.endpoint_for_role(PP_SERVER_BACKEND).endpoint_id == "server-backend"
    projection = plan.browser_projection()
    assert projection["manifest_digest"].startswith("sha256:")
    assert "transport_ref" not in str(projection)
    assert "resource_policy_ref" not in str(projection)
    server_projection = next(
        endpoint for endpoint in projection["endpoints"] if endpoint["id"] == "server-backend"
    )
    assert server_projection["authorities"] == [
        "canonical-sqlite-single-writer",
        "lancedb-promotion-decision",
        "deployment-receipt-persistence",
    ]
    assert server_projection["actions"] == list(
        plan.authority_profile_for("server-backend").actions
    )


def test_endpoint_authority_has_exactly_three_lifecycle_operations_and_registry_alias():
    public_operations = {
        name
        for name, member in vars(EndpointAuthority).items()
        if not name.startswith("_") and callable(member)
    }

    assert public_operations == {"resolve", "assess", "verify_completion"}
    assert EndpointContractRegistry is EndpointAuthority


def test_compute_authority_fails_closed_for_developer_and_governance_actions():
    compute = resolve_deployment_manifest_v2(_manifest()).authority_profile_for("compute-node")
    forbidden_actions = (
        "task-queue-administer",
        "collaboration-agent-register",
        "collaboration-event-write",
        "collaboration-work-board-write",
        "collaboration-awareness-publish",
        "memory-proposal-promote",
        "knowledge-proposal-promote",
        "canonical-sqlite-write",
        "merge-govern",
        "deployment-govern",
        "maintenance-govern",
        "lancedb-promotion-decide",
    )

    for action in forbidden_actions:
        assert compute.allows(action) is False
        with pytest.raises(EndpointContractError) as captured:
            compute.require(action)
        assert (captured.value.code, captured.value.category) == (
            "endpoint_authority_denied",
            "forbidden",
        )

    assert compute.allows("unregistered-action") is False
    assert compute.allows(None) is False


def test_endpoint_claims_cannot_expand_the_compute_authority_profile():
    baseline = resolve_deployment_manifest_v2(_manifest()).authority_profile_for("compute-node")
    embedding_only_manifest = _manifest()
    embedding_only_manifest["endpoints"][2]["capabilities"] = [  # type: ignore[index]
        {"kind": "embedding", "contract_version": "embedding/v1"}
    ]
    embedding_only = resolve_deployment_manifest_v2(embedding_only_manifest).authority_profile_for(
        "compute-node"
    )

    assert baseline.role == embedding_only.role == PP_COMPUTE_NODE
    assert baseline.actions == embedding_only.actions
    assert baseline.authorities == embedding_only.authorities == ("typed-derived-inference",)
    assert baseline.authorities != baseline.actions

    injected_project = _manifest()
    injected_project["endpoints"][2]["project_id"] = "project:untrusted"  # type: ignore[index]
    with pytest.raises(EndpointContractError, match="endpoint_unknown_field:project_id"):
        resolve_deployment_manifest_v2(injected_project)


def test_cross_endpoint_authority_flow_accepts_derived_result_without_collaboration_power():
    authority = EndpointAuthority()
    plan = authority.resolve(parse_deployment_manifest_v2(_manifest()))
    admission = authority.assess(plan, _observation(), _requirement(), observed_at=NOW)
    assert admission.accepted is True
    assert admission.binding is not None

    lease = ComputeLease(
        lease_id="lease-flow",
        job_id="job-flow",
        project_id="project:plastic-promise",
        endpoint_id="compute-node",
        manifest_digest=plan.manifest_digest,
        fencing_generation=7,
        capability="embedding",
        contract_version="embedding/v1",
        required_identity_fingerprint=admission.binding.identity_fingerprint,
        result_schema="embedding-result/v1",
        idempotency_key=_digest("d"),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    result = ComputeResult(
        lease_id="lease-flow",
        endpoint_id="compute-node",
        fencing_generation=7,
        capability="embedding",
        contract_version="embedding/v1",
        identity=_identity(),
        result_schema="embedding-result/v1",
        result_digest=_digest("e"),
        result_item_count=2,
        vector_dimension=1024,
    )
    completion = authority.verify_completion(
        admission.binding,
        lease,
        result,
        ComputeFence(job_id="job-flow", fencing_generation=7),
        observed_at=NOW,
    )
    compute = plan.authority_profile_for("compute-node")

    assert (completion.accepted, completion.reason_code) == (
        True,
        "endpoint_compute_completed",
    )
    assert compute.allows("derived-inference-return") is True
    assert compute.allows("collaboration-agent-register") is False


def test_v2_manifest_rejects_private_transport_fields_and_requires_split_compute_endpoint():
    with pytest.raises(EndpointContractError, match="endpoint_unknown_field:ssh_host"):
        payload = _manifest()
        payload["endpoints"][2]["ssh_host"] = "192.168.5.14"  # type: ignore[index]
        resolve_deployment_manifest_v2(payload)

    with pytest.raises(EndpointContractError, match="endpoint_compute_endpoint_required"):
        payload = _manifest(profile="split-accelerated")
        payload["endpoints"] = payload["endpoints"][:2]  # type: ignore[index]
        resolve_deployment_manifest_v2(payload)


def test_v2_manifest_rejects_secret_fields_and_duplicate_endpoint_roles():
    with pytest.raises(EndpointContractError, match="endpoint_manifest_secret_forbidden"):
        payload = _manifest()
        payload["token"] = "not-permitted"
        resolve_deployment_manifest_v2(payload)


def test_v2_manifest_round_trips_canonical_payload_and_rejects_capability_version_drift():
    payload = _manifest()
    payload["resource_budget"] = {
        "image_layers_bytes": 10,
        "image_unpack_bytes": 20,
        "model_cache_bytes": 30,
        "lancedb_shadow_rebuild_bytes": 40,
        "rollback_coexistence_bytes": 50,
    }
    payload["resource_locations"] = {
        "container_store": "container-store",
        "model_cache": "model-cache",
    }
    parsed = parse_deployment_manifest_v2(payload)
    reparsed = parse_deployment_manifest_v2(parsed.canonical_payload())

    assert reparsed.canonical_payload() == parsed.canonical_payload()
    assert reparsed.manifest_digest == parsed.manifest_digest

    with pytest.raises(
        EndpointContractError, match="endpoint_capability_contract_version_mismatch"
    ):
        payload = _manifest()
        payload["endpoints"][2]["capabilities"][0]["contract_version"] = "rerank/v1"  # type: ignore[index]
        resolve_deployment_manifest_v2(payload)

    with pytest.raises(EndpointContractError, match="endpoint_role_assignment_invalid"):
        payload = _manifest()
        payload["endpoints"][1]["role"] = "pp-local-edge"  # type: ignore[index]
        resolve_deployment_manifest_v2(payload)


def test_capability_binding_round_trips_and_rejects_secret_or_schema_drift():
    payload = _manifest()
    binding = _capability_binding()
    payload["endpoints"][2]["capabilities"][0]["binding"] = binding.to_dict()  # type: ignore[index]

    parsed = parse_deployment_manifest_v2(payload)
    reparsed = parse_deployment_manifest_v2(parsed.canonical_payload())

    parsed_binding = parsed.endpoints[2].capability_for("embedding", "embedding/v1")
    assert parsed_binding is not None
    assert parsed_binding.binding is not None
    assert parsed_binding.binding.fingerprint == binding.fingerprint
    assert reparsed.canonical_payload() == parsed.canonical_payload()
    assert reparsed.manifest_digest == parsed.manifest_digest

    with pytest.raises(EndpointContractError, match="endpoint_manifest_secret_forbidden"):
        secret_payload = _manifest()
        secret_payload["endpoints"][2]["capabilities"][0]["binding"] = {
            **binding.to_dict(),
            "token": "not-permitted",
        }  # type: ignore[index]
        parse_deployment_manifest_v2(secret_payload)

    with pytest.raises(EndpointContractError, match="endpoint_input_schema_capability_mismatch"):
        drifted_payload = _manifest()
        drifted_payload["endpoints"][2]["capabilities"][0]["binding"] = {
            **binding.to_dict(),
            "input_schema": "rerank-input/v1",
        }  # type: ignore[index]
        parse_deployment_manifest_v2(drifted_payload)


def test_admission_requires_complete_vector_space_identity_not_only_dimension_match():
    plan = resolve_deployment_manifest_v2(_manifest())
    admission = admit_endpoint(
        plan,
        _observation(_identity(metric="dot")),
        _requirement(_identity(metric="cosine")),
        observed_at=NOW,
    )

    assert admission.accepted is False
    assert admission.reason_code == "endpoint_embedding_identity_incompatible"
    assert admission.quarantine_recommended is True
    assert admission.receipt.outcome == "rejected"


def test_admission_exposes_retryable_stale_and_capacity_outcomes_without_quarantine():
    plan = resolve_deployment_manifest_v2(_manifest())

    stale = admit_endpoint(
        plan,
        _observation(observed_at=NOW - timedelta(seconds=31)),
        _requirement(),
        observed_at=NOW,
    )
    exhausted = admit_endpoint(
        plan,
        _observation(available_slots=0),
        _requirement(),
        observed_at=NOW,
    )

    assert (stale.accepted, stale.retryable, stale.quarantine_recommended) == (False, True, False)
    assert (exhausted.accepted, exhausted.retryable, exhausted.quarantine_recommended) == (
        False,
        True,
        False,
    )


def test_lower_minor_protocol_rejection_quarantines_manifest_and_requirement_mismatch():
    plan = resolve_deployment_manifest_v2(_manifest())

    below_manifest = admit_endpoint(
        plan,
        _observation(protocol=EndpointProtocol("compute", 1, 1)),
        _requirement(protocol=EndpointProtocol("compute", 1, 1)),
        observed_at=NOW,
    )
    below_requirement = admit_endpoint(
        plan,
        _observation(protocol=EndpointProtocol("compute", 1, 2)),
        _requirement(protocol=EndpointProtocol("compute", 1, 3)),
        observed_at=NOW,
    )

    assert (
        below_manifest.reason_code,
        below_manifest.quarantine_recommended,
    ) == ("endpoint_protocol_minor_unsupported", True)
    assert (
        below_requirement.reason_code,
        below_requirement.quarantine_recommended,
    ) == ("endpoint_protocol_minor_unsupported", True)


def test_bound_capability_admission_uses_manifest_as_authority_and_compares_hello_and_requirement():
    binding = _capability_binding()
    payload = _manifest()
    payload["endpoints"][2]["capabilities"][0]["binding"] = binding.to_dict()  # type: ignore[index]
    plan = resolve_deployment_manifest_v2(payload)

    admitted = admit_endpoint(
        plan,
        _observation(capability_binding=binding),
        _requirement(capability_binding=binding),
        observed_at=NOW,
    )
    hello_drifted = admit_endpoint(
        plan,
        _observation(capability_binding=_capability_binding(golden="f")),
        _requirement(capability_binding=binding),
        observed_at=NOW,
    )
    requirement_drifted = admit_endpoint(
        plan,
        _observation(capability_binding=binding),
        _requirement(capability_binding=_capability_binding(golden="f")),
        observed_at=NOW,
    )

    assert admitted.accepted is True
    assert admitted.binding is not None
    assert admitted.binding.capability_binding == binding
    assert (hello_drifted.reason_code, hello_drifted.quarantine_recommended) == (
        "endpoint_capability_binding_incompatible",
        True,
    )
    assert (requirement_drifted.reason_code, requirement_drifted.quarantine_recommended) == (
        "endpoint_capability_binding_incompatible",
        True,
    )


def test_completion_contract_accepts_only_the_authoritative_fence_and_rejects_late_or_drifting_result():
    plan = resolve_deployment_manifest_v2(_manifest())
    admission = admit_endpoint(plan, _observation(), _requirement(), observed_at=NOW)
    assert admission.binding is not None
    lease = ComputeLease(
        lease_id="lease-1",
        job_id="job-1",
        project_id="project:plastic-promise",
        endpoint_id="compute-node",
        manifest_digest=plan.manifest_digest,
        fencing_generation=5,
        capability="embedding",
        contract_version="embedding/v1",
        required_identity_fingerprint=admission.binding.identity_fingerprint,
        result_schema="embedding-result/v1",
        idempotency_key=_digest("d"),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    result = ComputeResult(
        lease_id="lease-1",
        endpoint_id="compute-node",
        fencing_generation=5,
        capability="embedding",
        contract_version="embedding/v1",
        identity=_identity(),
        result_schema="embedding-result/v1",
        result_digest=_digest("e"),
        result_item_count=2,
        vector_dimension=1024,
    )

    current_fence = ComputeFence(job_id="job-1", fencing_generation=5)
    accepted = validate_compute_exchange(
        admission.binding,
        lease,
        result,
        current_fence,
        observed_at=NOW,
    )
    expired = validate_compute_exchange(
        admission.binding,
        lease,
        result,
        current_fence,
        observed_at=NOW + timedelta(seconds=31),
    )
    drifted = validate_compute_exchange(
        admission.binding,
        lease,
        ComputeResult(
            lease_id="lease-1",
            endpoint_id="compute-node",
            fencing_generation=5,
            capability="embedding",
            contract_version="embedding/v1",
            identity=_identity(golden="f"),
            result_schema="embedding-result/v1",
            result_digest=_digest("f"),
            result_item_count=2,
            vector_dimension=1024,
        ),
        current_fence,
        observed_at=NOW,
    )
    stale = validate_compute_exchange(
        admission.binding,
        lease,
        result,
        ComputeFence(job_id="job-1", fencing_generation=6),
        observed_at=NOW,
    )
    mismatched = validate_compute_exchange(
        admission.binding,
        lease,
        ComputeResult(
            lease_id="lease-1",
            endpoint_id="compute-node",
            fencing_generation=4,
            capability="embedding",
            contract_version="embedding/v1",
            identity=_identity(),
            result_schema="embedding-result/v1",
            result_digest=_digest("f"),
            result_item_count=2,
            vector_dimension=1024,
        ),
        current_fence,
        observed_at=NOW,
    )

    assert (accepted.accepted, accepted.reason_code) == (True, "endpoint_compute_completed")
    assert (expired.accepted, expired.retryable, expired.reason_code) == (
        False,
        True,
        "endpoint_compute_lease_expired",
    )
    assert (drifted.accepted, drifted.quarantine_recommended, drifted.reason_code) == (
        False,
        True,
        "endpoint_result_identity_drift",
    )
    assert (stale.accepted, stale.retryable, stale.reason_code) == (
        False,
        True,
        "endpoint_compute_fencing_stale",
    )
    assert (mismatched.accepted, mismatched.retryable, mismatched.reason_code) == (
        False,
        True,
        "endpoint_compute_fencing_stale",
    )


def test_bound_capability_completion_validates_lease_schema_timeout_and_terminal_reason():
    capability_binding = _capability_binding()
    payload = _manifest()
    payload["endpoints"][2]["capabilities"][0]["binding"] = capability_binding.to_dict()  # type: ignore[index]
    plan = resolve_deployment_manifest_v2(payload)
    admission = admit_endpoint(
        plan,
        _observation(capability_binding=capability_binding),
        _requirement(capability_binding=capability_binding),
        observed_at=NOW,
    )
    assert admission.binding is not None
    lease = ComputeLease(
        lease_id="lease-1",
        job_id="job-1",
        project_id="project:plastic-promise",
        endpoint_id="compute-node",
        manifest_digest=plan.manifest_digest,
        fencing_generation=5,
        capability="embedding",
        contract_version="embedding/v1",
        required_identity_fingerprint=admission.binding.identity_fingerprint,
        result_schema="embedding-result/v1",
        idempotency_key=_digest("d"),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        input_schema="embedding-input/v1",
        capability_binding_fingerprint=capability_binding.fingerprint,
    )
    result = ComputeResult(
        lease_id="lease-1",
        endpoint_id="compute-node",
        fencing_generation=5,
        capability="embedding",
        contract_version="embedding/v1",
        identity=_identity(),
        result_schema="embedding-result/v1",
        result_digest=_digest("e"),
        result_item_count=2,
        vector_dimension=1024,
        capability_binding_fingerprint=capability_binding.fingerprint,
    )
    fence = ComputeFence(job_id="job-1", fencing_generation=5)

    accepted = validate_compute_exchange(admission.binding, lease, result, fence, observed_at=NOW)
    invalid_terminal = validate_compute_exchange(
        admission.binding,
        lease,
        ComputeResult(
            lease_id="lease-1",
            endpoint_id="compute-node",
            fencing_generation=5,
            capability="embedding",
            contract_version="embedding/v1",
            identity=_identity(),
            result_schema="embedding-result/v1",
            result_digest=_digest("f"),
            result_item_count=2,
            vector_dimension=1024,
            capability_binding_fingerprint=capability_binding.fingerprint,
            terminal_reason="failed",
        ),
        fence,
        observed_at=NOW,
    )
    timeout_exceeded = validate_compute_exchange(
        admission.binding,
        ComputeLease(
            lease_id="lease-1",
            job_id="job-1",
            project_id="project:plastic-promise",
            endpoint_id="compute-node",
            manifest_digest=plan.manifest_digest,
            fencing_generation=5,
            capability="embedding",
            contract_version="embedding/v1",
            required_identity_fingerprint=admission.binding.identity_fingerprint,
            result_schema="embedding-result/v1",
            idempotency_key=_digest("d"),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=31),
            input_schema="embedding-input/v1",
            capability_binding_fingerprint=capability_binding.fingerprint,
        ),
        result,
        fence,
        observed_at=NOW,
    )

    assert accepted.accepted is True
    assert (invalid_terminal.reason_code, invalid_terminal.quarantine_recommended) == (
        "endpoint_result_terminal_reason_invalid",
        True,
    )
    assert timeout_exceeded.reason_code == "endpoint_capability_lease_timeout_exceeded"


def test_manifest_revision_record_is_server_owned_schema_without_claiming_persistence():
    plan = resolve_deployment_manifest_v2(_manifest())
    record = ManifestRevisionRecord(
        deployment_id=plan.deployment_id,
        revision=1,
        manifest_digest=plan.manifest_digest,
        parent_manifest_digest=None,
        created_at=NOW,
        status="staged",
    )

    assert record.to_dict()["owner"] == PP_SERVER_BACKEND
    assert record.to_dict()["status"] == "staged"
