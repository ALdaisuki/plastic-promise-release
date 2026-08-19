from __future__ import annotations

from datetime import datetime, timezone

import pytest

from plastic_promise.deployment.deployment_center import (
    ControllerState,
    DeploymentCenter,
    DeploymentCenterError,
    DeploymentPreviewRequest,
    EndpointContractGate,
    EnrollmentReadiness,
    HostInspection,
    HostStorage,
    InstallationResolution,
    ManifestTopologyProjection,
    ModelIdentityGate,
)
from plastic_promise.deployment.endpoint_contract import (
    DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION,
    resolve_deployment_manifest_v2,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
GIB = 1024**3


def _digest(character: str) -> str:
    return f"sha256:{character * 64}"


def _manifest(*, image_layers_bytes: int = GIB) -> dict[str, object]:
    return {
        "schema_version": DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION,
        "deployment_id": "developer-laptop",
        "profile": "split-accelerated",
        "modules": {},
        "endpoints": [
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
                "protocol": {"family": "compute", "major": 1, "minor": 0},
                "capabilities": [
                    {"kind": "embedding", "contract_version": "embedding/v1"},
                    {"kind": "rerank", "contract_version": "rerank/v1"},
                ],
                "max_concurrency": 4,
                "transport_ref": "compute-registry",
                "resource_policy_ref": "compute-default",
            },
        ],
        "resource_budget": {
            "image_layers_bytes": image_layers_bytes,
            "image_unpack_bytes": GIB,
            "model_cache_bytes": 2 * GIB,
            "lancedb_shadow_rebuild_bytes": GIB,
            "rollback_coexistence_bytes": GIB,
        },
        "resource_locations": {
            "container_store": "container-store",
            "model_cache": "model-cache",
        },
    }


class _Resolver:
    def resolve(self, installation_ref: str) -> InstallationResolution:
        return InstallationResolution(
            installation_ref=installation_ref, canonical_state_ref="state"
        )


class _Host:
    def __init__(
        self,
        *,
        free_bytes: int = 100 * GIB,
        container_runtime_ready: bool = True,
        fresh: bool = True,
    ) -> None:
        self.free_bytes = free_bytes
        self.container_runtime_ready = container_runtime_ready
        self.fresh = fresh
        self.calls = 0

    def inspect(self, installation: InstallationResolution) -> HostInspection:
        self.calls += 1
        assert installation.canonical_state_ref == "state"
        return HostInspection(
            platform="wsl2",
            observed_at=NOW,
            freshness_seconds=30,
            fresh=self.fresh,
            container_runtime_ready=self.container_runtime_ready,
            accelerator_available=True,
            storage=(
                HostStorage("state", "work-volume", 100 * GIB, self.free_bytes),
                HostStorage("container-store", "work-volume", 100 * GIB, self.free_bytes),
                HostStorage("model-cache", "work-volume", 100 * GIB, self.free_bytes),
            ),
        )


class _Controller:
    def __init__(
        self,
        *,
        identity_matches: bool = True,
        contract_accepted: bool = True,
        fresh: bool = True,
        active_manifest_digest: str | None = None,
        active_manifest_projection: ManifestTopologyProjection | None = None,
        enrollment_status: str | None = None,
    ) -> None:
        self.identity_matches = identity_matches
        self.contract_accepted = contract_accepted
        self.fresh = fresh
        self.active_manifest_digest = active_manifest_digest
        self.active_manifest_projection = active_manifest_projection
        resolved_enrollment_status = enrollment_status or (
            "required" if active_manifest_digest is None else "not_required"
        )
        self.enrollment_readiness = EnrollmentReadiness(
            resolved_enrollment_status,
            f"enrollment_{resolved_enrollment_status.replace('-', '_')}",
        )
        self.calls = 0

    def inspect(self, installation: InstallationResolution) -> ControllerState:
        self.calls += 1
        expected_embedding = _digest("a")
        expected_rerank = _digest("b")
        return ControllerState(
            observed_at=NOW,
            freshness_seconds=30,
            fresh=self.fresh,
            active_manifest_digest=self.active_manifest_digest,
            endpoint_contract_gates=tuple(
                EndpointContractGate(
                    endpoint_id=endpoint,
                    accepted=self.contract_accepted,
                    reason_code=(
                        "endpoint_contract_accepted"
                        if self.contract_accepted
                        else "endpoint_contract_rejected"
                    ),
                )
                for endpoint in ("local-edge", "server-backend", "compute-node")
            ),
            model_identity_gates=(
                ModelIdentityGate(
                    endpoint_id="compute-node",
                    capability="embedding",
                    contract_version="embedding/v1",
                    expected_identity_fingerprint=expected_embedding,
                    observed_identity_fingerprint=(
                        expected_embedding if self.identity_matches else _digest("c")
                    ),
                    accepted=True,
                ),
                ModelIdentityGate(
                    endpoint_id="compute-node",
                    capability="rerank",
                    contract_version="rerank/v1",
                    expected_identity_fingerprint=expected_rerank,
                    observed_identity_fingerprint=expected_rerank,
                    accepted=True,
                ),
            ),
            enrollment_readiness=self.enrollment_readiness,
            active_manifest_projection=self.active_manifest_projection,
        )


def _center(
    *,
    host: _Host | None = None,
    controller: _Controller | None = None,
) -> tuple[DeploymentCenter, _Host, _Controller]:
    resolved_host = host or _Host()
    resolved_controller = controller or _Controller()
    return (
        DeploymentCenter(
            installation_resolver=_Resolver(),
            host_inspector=resolved_host,
            controller_state=resolved_controller,
        ),
        resolved_host,
        resolved_controller,
    )


def test_preview_strictly_resolves_v2_and_returns_fresh_safe_non_authoritative_projection():
    center, host, controller = _center()

    preview = center.preview(DeploymentPreviewRequest(_manifest(), "local-installation"))
    payload = preview.to_dict()

    assert preview.admissible is True
    assert preview.failure_codes == ()
    assert host.calls == controller.calls == 1
    assert payload["plan_hash"].startswith("sha256:")
    assert payload["plan_hash_scope"] == "inspection_only"
    assert payload["plan_authorization"] == "deferred_to_pr5"
    assert payload["update_class"] == {
        "kind": "enrollment-required",
        "reason_code": "enrollment_required",
        "authority": "inspection_only",
        "execution_status": "deferred_to_pr5",
    }
    assert payload["inspection"]["observed_at"] == "2026-08-07T12:00:00Z"
    assert payload["inspection"]["freshness_seconds"] == 30
    assert payload["inspection"]["deployment_receipt"] == {
        "availability": "unavailable",
        "persistence": "not_persisted",
        "state": "contract_unpersisted",
    }
    rendered = str(payload)
    assert "transport_ref" not in rendered
    assert "resource_policy_ref" not in rendered
    assert "/Users/" not in rendered
    assert "private-key" not in rendered


def test_preview_hash_binds_candidate_and_installation_but_is_not_authorization():
    center, _, _ = _center()

    first = center.preview(DeploymentPreviewRequest(_manifest(), "local-installation"))
    changed_manifest = _manifest(image_layers_bytes=2 * GIB)
    changed_candidate = center.preview(
        DeploymentPreviewRequest(changed_manifest, "local-installation")
    )
    changed_installation = center.preview(
        DeploymentPreviewRequest(_manifest(), "other-installation")
    )
    changed_observation, _, _ = _center(host=_Host(free_bytes=90 * GIB))

    assert first.plan_hash != changed_candidate.plan_hash
    assert first.plan_hash != changed_installation.plan_hash
    assert (
        first.plan_hash
        != changed_observation.preview(
            DeploymentPreviewRequest(_manifest(), "local-installation")
        ).plan_hash
    )
    assert first.to_dict()["plan_authorization"] == "deferred_to_pr5"


def test_preview_update_classification_stays_conservative_without_active_manifest_body():
    candidate = _manifest()
    candidate_digest = resolve_deployment_manifest_v2(candidate).manifest_digest
    no_change, _, _ = _center(controller=_Controller(active_manifest_digest=candidate_digest))
    changed, _, _ = _center(controller=_Controller(active_manifest_digest=_digest("c")))
    rejected, _, _ = _center(
        host=_Host(free_bytes=15 * GIB),
        controller=_Controller(active_manifest_digest=candidate_digest),
    )
    request = DeploymentPreviewRequest(candidate, "local-installation")
    no_change_class = no_change.preview(request).update_class.to_dict()
    changed_class = changed.preview(request).update_class.to_dict()
    rejected_class = rejected.preview(request).update_class.to_dict()

    assert no_change_class == {
        "kind": "no-change",
        "reason_code": "candidate_matches_active",
        "authority": "inspection_only",
        "execution_status": "deferred_to_pr5",
    }
    assert changed_class == {
        "kind": "manual-review",
        "reason_code": "active_manifest_projection_unavailable",
        "authority": "inspection_only",
        "execution_status": "deferred_to_pr5",
    }
    assert rejected_class == {
        "kind": "manual-review",
        "reason_code": "preflight_not_admissible",
        "authority": "inspection_only",
        "execution_status": "deferred_to_pr5",
    }


def test_preview_projects_safe_v2_manifest_diff_when_controller_supplies_it():
    active_digest = _digest("c")
    active_projection = ManifestTopologyProjection(
        manifest_digest=active_digest,
        profile_id="local-all-in-one",
        module_ids=(
            "canonical-runtime",
            "durable-outbox",
            "derived-index",
            "operator-dashboard",
        ),
        endpoint_ids=("local-edge", "server-backend"),
        compute_capability_kinds=(),
    )
    center, _, _ = _center(
        controller=_Controller(
            active_manifest_digest=active_digest,
            active_manifest_projection=active_projection,
        )
    )

    preview = center.preview(DeploymentPreviewRequest(_manifest(), "local-installation"))

    assert preview.manifest_diff.to_dict() == {
        "availability": "available",
        "reason_code": "manifest_diff_available",
        "candidate_manifest_digest": resolve_deployment_manifest_v2(_manifest()).manifest_digest,
        "active_manifest_digest": active_digest,
        "profile_changed": True,
        "added_module_ids": ["heterogeneous-inference-node"],
        "removed_module_ids": [],
        "added_endpoint_ids": ["compute-node"],
        "removed_endpoint_ids": [],
        "added_compute_capabilities": ["embedding", "rerank"],
        "removed_compute_capabilities": [],
    }
    assert preview.update_class.to_dict()["reason_code"] == "active_manifest_diff_requires_pr5"


def test_preview_fails_closed_for_unsafe_storage_and_model_or_contract_evidence():
    center, _, _ = _center(
        host=_Host(free_bytes=15 * GIB),
        controller=_Controller(identity_matches=False, contract_accepted=False),
    )

    preview = center.preview(DeploymentPreviewRequest(_manifest(), "local-installation"))

    assert preview.admissible is False
    assert set(preview.failure_codes) == {
        "post_install_disk_reserve_unmet",
        "endpoint_contract_rejected",
        "model_identity_incompatible",
    }
    assert preview.storage_preflight[0].ok is False
    assert preview.endpoint_contract_gates[0]["accepted"] is False
    assert preview.model_identity_gates[0]["accepted"] is False


def test_inspect_does_not_cache_adapter_state_and_rejects_legacy_or_path_input():
    host = _Host(fresh=True)
    center, _, _ = _center(host=host)
    assert center.inspect("local-installation").host.fresh is True
    host.fresh = False
    assert center.inspect("local-installation").host.fresh is False
    assert host.calls == 2

    with pytest.raises(DeploymentCenterError, match="deployment_center_candidate_invalid"):
        center.preview(
            DeploymentPreviewRequest(
                {
                    "schema_version": "plastic-promise-deployment/v1",
                    "state_root": "/Users/unsafe",
                },
                "local-installation",
            )
        )
    with pytest.raises(DeploymentCenterError, match="installation_reference_invalid"):
        DeploymentPreviewRequest(_manifest(), "/Users/unsafe")


def test_profile_recommendation_is_advisory_and_does_not_override_candidate_profile():
    manifest = _manifest()
    manifest["profile"] = "local-all-in-one"
    manifest["modules"] = {"heterogeneous-inference-node": {"enabled": True}}
    # A V2 candidate with a compute endpoint cannot be local-all-in-one, so use
    # an optional cloud module to exercise the recommendation without legacy input.
    manifest["endpoints"] = manifest["endpoints"][:2]
    manifest["modules"] = {"cloud-inference": {"enabled": True}}
    manifest["resource_budget"] = {
        "image_layers_bytes": 0,
        "image_unpack_bytes": 0,
        "model_cache_bytes": 0,
        "lancedb_shadow_rebuild_bytes": GIB,
        "rollback_coexistence_bytes": GIB,
    }
    manifest["resource_locations"] = {"container_store": None, "model_cache": None}
    center, _, _ = _center()

    inspection = center.preview(DeploymentPreviewRequest(manifest, "local-installation")).inspection

    recommendation = inspection.profile_recommendation
    assert recommendation.selected_profile_id == "local-all-in-one"
    assert recommendation.recommended_profile_id == "local-cloud"
    assert recommendation.advisory is True
