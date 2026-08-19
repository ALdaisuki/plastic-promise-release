"""Focused contracts for PR6 release bundle binding and evidence projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from plastic_promise.deployment import (
    COMPUTE_VARIANT_CPU,
    MODEL_RUNTIME_CLOUD,
    MODEL_RUNTIME_REMOTE,
    ArtifactBundleBinding,
    ArtifactEvidenceReceipt,
    ArtifactMaterialization,
    ArtifactRequest,
    ContainerArtifactCompiler,
    EmbeddingIdentity,
    EndpointCapability,
    ModelCatalog,
    ModelResourceEstimate,
    ReleaseBundle,
    ReleaseBundleError,
    ReleaseBundleIdentity,
    RerankIdentity,
    VerifiedEvidenceProjection,
    canonical_release_bundle_bytes,
)
from plastic_promise.deployment.release_bundle import ArtifactSbomReceipt, ArtifactSbomReceiptSet

SOURCE_REVISION = "a" * 40
MANIFEST_SHA256 = "b" * 64


def _digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _mapping_digest(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest(encoded)


def _catalog(
    *, profile_id: str = "split-accelerated", runtime: str = MODEL_RUNTIME_REMOTE
) -> ModelCatalog:
    return ModelCatalog(
        catalog_id="rc-models-v1",
        profile_id=profile_id,
        runtime=runtime,
        capabilities=(
            EndpointCapability("embedding", "embedding/v1"),
            EndpointCapability("rerank", "rerank/v1"),
            EndpointCapability("structured-json", "structured-json/v1"),
        ),
        resource_estimate=ModelResourceEstimate(
            model_cache_bytes=5 * 1024**3,
            minimum_system_memory_bytes=8 * 1024**3,
            minimum_gpu_memory_bytes=0,
        ),
        embedding=EmbeddingIdentity(
            model="acme/embedding-v1",
            revision="c" * 40,
            dimension=1024,
            normalization="l2",
            metric="cosine",
            tokenization="wordpiece",
            pooling="mean",
            artifact_sha256=_digest("embedding"),
            golden_vector_sha256=_digest("golden-embedding"),
        ),
        rerank=RerankIdentity(
            model="acme/rerank-v1",
            revision="d" * 40,
            artifact_sha256=_digest("rerank"),
            scoring_schema="rerank-score/v1",
        ),
    )


class _RecordingExecutor:
    def materialize(self, plan, artifact):  # type: ignore[no-untyped-def]
        image_digest = _digest(f"image:{artifact.artifact_id}")
        layout_digest = _digest(f"layout:{artifact.role}:{artifact.variant}")
        labels_digest = _mapping_digest(plan.expected_oci_labels(artifact.artifact_id))
        embedded_sbom_digest = _digest(f"embedded-sbom:{artifact.artifact_id}")
        provenance_digest = _digest(f"provenance:{artifact.artifact_id}")
        evidence = ArtifactEvidenceReceipt(
            artifact_id=artifact.artifact_id,
            role=artifact.role,
            platform=artifact.platform,
            variant=artifact.variant,
            source_revision=plan.request.source_revision,
            package_version=plan.request.package_version,
            base_image_reference=artifact.base_image_reference,
            recipe_policy_digest=plan.recipe_policy_digest,
            policy_digest=plan.policy_digest,
            collaboration_surface_digest=artifact.collaboration_surface_digest,
            application_inventory_digest=_digest(f"inventory:{artifact.artifact_id}"),
            oci_layout_digest=layout_digest,
            image_digest=image_digest,
            oci_labels_digest=labels_digest,
            sbom_digest=embedded_sbom_digest,
            sbom_subject_digest=image_digest,
            provenance_digest=provenance_digest,
            provenance_subject_digest=image_digest,
        )
        return ArtifactMaterialization(
            artifact_id=artifact.artifact_id,
            role=artifact.role,
            platform=artifact.platform,
            variant=artifact.variant,
            immutable_reference=f"oci@{image_digest}",
            image_digest=image_digest,
            oci_layout_digest=layout_digest,
            oci_labels_digest=labels_digest,
            sbom_digest=embedded_sbom_digest,
            provenance_digest=provenance_digest,
            evidence_receipt=evidence,
        )


def _artifact_binding_with_receipts(
    catalog: ModelCatalog, *, release_version: str = "v1.2.3-rc.1"
) -> tuple[ArtifactBundleBinding, ArtifactSbomReceiptSet]:
    compute_variants = () if catalog.runtime == MODEL_RUNTIME_CLOUD else (COMPUTE_VARIANT_CPU,)
    bundle = ContainerArtifactCompiler().materialize(
        ContainerArtifactCompiler().prepare(
            ArtifactRequest(
                profile_id=catalog.profile_id,
                source_revision=SOURCE_REVISION,
                package_version="1.2.3rc1" if release_version == "v1.2.3-rc.1" else "1.2.4rc1",
                platforms=("linux/amd64",),
                compute_variants=compute_variants,
                model_catalog_reference=catalog.catalog_id,
                model_catalog_digest=catalog.digest,
            )
        ),
        _RecordingExecutor(),
    )
    receipts = ArtifactSbomReceiptSet(
        receipts=tuple(
            ArtifactSbomReceipt(
                artifact_id=item.artifact_id,
                role=item.role,
                platform=item.platform,
                variant=item.variant,
                oci_layout_digest=item.oci_layout_digest,
                image_digest=item.image_digest,
                sbom_digest=_digest(f"external-sbom:{item.artifact_id}"),
                sbom_size_bytes=1024,
                sbom_format="CycloneDX",
                sbom_spec_version="1.6",
            )
            for item in bundle.materializations
        )
    )
    return (
        ArtifactBundleBinding.from_artifact_bundle(bundle, artifact_sbom_receipts=receipts),
        receipts,
    )


def _artifact_binding(
    catalog: ModelCatalog, *, release_version: str = "v1.2.3-rc.1"
) -> ArtifactBundleBinding:
    return _artifact_binding_with_receipts(catalog, release_version=release_version)[0]


def _evidence(
    binding: ArtifactBundleBinding, catalog: ModelCatalog
) -> tuple[VerifiedEvidenceProjection, ...]:
    return (
        VerifiedEvidenceProjection(
            subject="release-manifest",
            subject_digest=f"sha256:{MANIFEST_SHA256}",
            attestation_digest=_digest("manifest-attestation"),
            predicate_type="slsa-provenance/v1",
            verifier="github-actions",
        ),
        VerifiedEvidenceProjection(
            subject="model-catalog",
            subject_digest=catalog.digest,
            attestation_digest=_digest("catalog-attestation"),
            predicate_type="model-catalog-attestation/v1",
            verifier="github-actions",
        ),
        VerifiedEvidenceProjection(
            subject="artifact-bundle",
            subject_digest=binding.digest,
            attestation_digest=_digest("artifact-attestation"),
            predicate_type="slsa-provenance/v1",
            verifier="github-actions",
        ),
        VerifiedEvidenceProjection(
            subject="artifact-sbom-receipts",
            subject_digest=binding.artifact_sbom_receipts_digest,
            attestation_digest=_digest("artifact-sbom-receipts-attestation"),
            predicate_type="slsa-provenance/v1",
            verifier="github-actions",
        ),
    )


def _release_bundle(
    catalog: ModelCatalog,
    binding: ArtifactBundleBinding,
    *,
    evidence: tuple[VerifiedEvidenceProjection, ...] | None = None,
    previous_bundle: ReleaseBundleIdentity | None = None,
) -> ReleaseBundle:
    return ReleaseBundle(
        release_version="v1.2.3-rc.1",
        source_revision=SOURCE_REVISION,
        release_manifest_sha256=MANIFEST_SHA256,
        artifact_binding=binding,
        model_catalog=catalog,
        attestation_evidence=_evidence(binding, catalog) if evidence is None else evidence,
        previous_bundle=previous_bundle,
    )


def test_release_bundle_binds_source_manifest_catalog_artifacts_evidence_and_rollback_identity():
    catalog = _catalog()
    binding = _artifact_binding(catalog)
    previous = ReleaseBundleIdentity(
        release_version="v1.2.2",
        source_revision="e" * 40,
        bundle_digest=_digest("previous-release"),
    )

    bundle = _release_bundle(catalog, binding, previous_bundle=previous)
    reordered_evidence = _release_bundle(
        catalog,
        binding,
        evidence=tuple(reversed(_evidence(binding, catalog))),
        previous_bundle=previous,
    )

    assert binding.compute_capabilities == (
        "embedding/v1",
        "rerank/v1",
        "structured-json/v1",
    )
    assert bundle.release_manifest_digest == f"sha256:{MANIFEST_SHA256}"
    assert bundle.digest == reordered_evidence.digest
    assert canonical_release_bundle_bytes(bundle) == canonical_release_bundle_bytes(
        reordered_evidence
    )
    assert bundle.to_dict()["previous_bundle"] == previous.to_dict()
    assert bundle.identity.bundle_digest == bundle.digest


def test_release_bundle_rejects_catalog_and_compute_contract_mismatches():
    catalog = _catalog()
    binding = _artifact_binding(catalog)
    changed_catalog = replace(
        catalog,
        embedding=replace(catalog.embedding, artifact_sha256=_digest("different")),
    )

    with pytest.raises(ReleaseBundleError, match="catalog_digest_mismatch"):
        _release_bundle(changed_catalog, binding)

    with pytest.raises(ReleaseBundleError, match="capability_matrix_mismatch"):
        _release_bundle(catalog, replace(binding, compute_capabilities=("embedding/v1",)))


def test_release_bundle_allows_cloud_catalog_only_without_local_compute_artifacts():
    catalog = _catalog(profile_id="local-cloud", runtime=MODEL_RUNTIME_CLOUD)
    binding = _artifact_binding(catalog)

    bundle = _release_bundle(catalog, binding)

    assert binding.compute_capabilities == ()
    assert bundle.model_catalog.runtime == MODEL_RUNTIME_CLOUD


def test_release_bundle_requires_exact_already_verified_evidence_projection_set():
    catalog = _catalog()
    binding = _artifact_binding(catalog)
    evidence = _evidence(binding, catalog)

    with pytest.raises(ReleaseBundleError, match="evidence_set_invalid"):
        _release_bundle(catalog, binding, evidence=evidence[:-1])

    with pytest.raises(ReleaseBundleError, match="evidence_subject_mismatch"):
        _release_bundle(
            catalog,
            binding,
            evidence=(
                replace(evidence[0], subject_digest=_digest("wrong-manifest")),
                *evidence[1:],
            ),
        )

    with pytest.raises(ReleaseBundleError, match="not_verified"):
        replace(evidence[0], verified=False)


def test_artifact_binding_rejects_non_bundle_input_with_a_stable_error_code():
    with pytest.raises(ReleaseBundleError, match="binding_input_invalid"):
        ArtifactBundleBinding.from_artifact_bundle(
            object(),  # type: ignore[arg-type]
            artifact_sbom_receipts=object(),  # type: ignore[arg-type]
        )


def test_artifact_binding_expands_each_immutable_image_sbom_and_provenance_record():
    catalog = _catalog()
    binding, receipts = _artifact_binding_with_receipts(catalog)

    assert binding.artifact_ids == tuple(item.artifact_id for item in binding.artifacts)
    assert {
        (item.role, item.platform, item.variant, item.immutable_reference)
        for item in binding.artifacts
    } == {
        (
            "pp-local-edge",
            "linux/amd64",
            "standard",
            f"oci@{_digest('image:local-edge-linux-amd64-standard')}",
        ),
        (
            "pp-server-backend",
            "linux/amd64",
            "standard",
            f"oci@{_digest('image:server-backend-linux-amd64-standard')}",
        ),
        (
            "pp-compute-node",
            "linux/amd64",
            "cpu",
            f"oci@{_digest('image:compute-node-linux-amd64-cpu')}",
        ),
    }
    assert all(item.sbom_digest.startswith("sha256:") for item in binding.artifacts)
    assert binding.artifact_sbom_receipts_digest == receipts.digest
    parsed_receipts = ArtifactSbomReceiptSet.from_dict(receipts.to_dict())
    ArtifactBundleBinding.from_dict(binding.to_dict()).validate_artifact_sbom_receipts(
        parsed_receipts
    )
    assert ArtifactBundleBinding.from_dict(binding.to_dict()) == binding


def test_artifact_binding_rejects_sbom_and_identity_tampering_and_secret_like_fields():
    catalog = _catalog()
    binding, receipts = _artifact_binding_with_receipts(catalog)
    payload = json.loads(json.dumps(binding.to_dict()))
    payload["artifacts"][0]["sbom_digest"] = _digest("tampered-sbom")

    with pytest.raises(ReleaseBundleError, match="binding_digest_mismatch"):
        ArtifactBundleBinding.from_dict(payload)

    with pytest.raises(ReleaseBundleError, match="reference_digest_mismatch"):
        replace(binding.artifacts[0], image_digest=_digest("tampered-image"))

    mismatched_receipts = ArtifactSbomReceiptSet(
        receipts=(
            replace(receipts.receipts[0], image_digest=_digest("receipt-image-mismatch")),
            *receipts.receipts[1:],
        )
    )
    with pytest.raises(ReleaseBundleError, match="sbom_receipts_digest_mismatch"):
        binding.validate_artifact_sbom_receipts(mismatched_receipts)

    with pytest.raises(ReleaseBundleError, match="sbom_receipts_artifact_mismatch"):
        replace(
            binding,
            artifact_sbom_receipts_digest=mismatched_receipts.digest,
        ).validate_artifact_sbom_receipts(mismatched_receipts)

    mismatched_layout_receipts = ArtifactSbomReceiptSet(
        receipts=(
            replace(receipts.receipts[0], oci_layout_digest=_digest("receipt-layout-mismatch")),
            *receipts.receipts[1:],
        )
    )
    with pytest.raises(ReleaseBundleError, match="sbom_receipts_artifact_mismatch"):
        replace(
            binding,
            artifact_sbom_receipts_digest=mismatched_layout_receipts.digest,
        ).validate_artifact_sbom_receipts(mismatched_layout_receipts)

    with pytest.raises(ReleaseBundleError, match="secret_forbidden"):
        replace(binding, package_version="sk-0123456789abcdef0123456789abcdef")

    with pytest.raises(ReleaseBundleError, match="secret_forbidden"):
        replace(binding.artifacts[0], artifact_id="sk-0123456789abcdef0123456789abcdef")

    evidence = _evidence(binding, catalog)[0]
    assert VerifiedEvidenceProjection.from_dict(evidence.to_dict()) == evidence
