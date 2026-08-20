"""Focused contracts for the PR3 build-time container artifact compiler."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from plastic_promise.deployment import (
    COMPUTE_VARIANT_CPU,
    COMPUTE_VARIANT_CUDA,
    CONTAINER_ARTIFACT_EVIDENCE_SCHEMA_VERSION,
    ENDPOINT_CONTRACT_SCHEMA_VERSION,
    PP_COMPUTE_NODE,
    PP_LOCAL_EDGE,
    PP_SERVER_BACKEND,
    ArtifactEvidenceReceipt,
    ArtifactMaterialization,
    ArtifactRequest,
    ContainerArtifactCompiler,
    ContainerArtifactError,
)
from plastic_promise.endpoint_roles import endpoint_role_contract


def _digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _mapping_digest(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest(encoded)


def _request(**overrides: object) -> ArtifactRequest:
    values: dict[str, object] = {
        "profile_id": "split-accelerated",
        "source_revision": "a" * 40,
        "package_version": "0.8.0rc1",
        "platforms": ("linux/amd64", "linux/arm64"),
        "compute_variants": (COMPUTE_VARIANT_CPU, COMPUTE_VARIANT_CUDA),
        "model_catalog_reference": "release-catalog-v1",
        "model_catalog_digest": _digest("catalog"),
    }
    values.update(overrides)
    return ArtifactRequest(**values)  # type: ignore[arg-type]


class _RecordingExecutor:
    def __init__(
        self,
        *,
        corrupt_first_role: bool = False,
        corrupt_first_labels: bool = False,
        corrupt_first_policy: bool = False,
        corrupt_first_sbom_subject: bool = False,
        corrupt_first_provenance_subject: bool = False,
    ) -> None:
        self.seen: list[str] = []
        self.corrupt_first_role = corrupt_first_role
        self.corrupt_first_labels = corrupt_first_labels
        self.corrupt_first_policy = corrupt_first_policy
        self.corrupt_first_sbom_subject = corrupt_first_sbom_subject
        self.corrupt_first_provenance_subject = corrupt_first_provenance_subject

    def materialize(self, plan, artifact):  # type: ignore[no-untyped-def]
        self.seen.append(artifact.artifact_id)
        image_digest = _digest(f"image:{artifact.artifact_id}")
        role = artifact.role
        if self.corrupt_first_role and len(self.seen) == 1:
            role = PP_SERVER_BACKEND if artifact.role != PP_SERVER_BACKEND else PP_LOCAL_EDGE
        labels_digest = _mapping_digest(plan.expected_oci_labels(artifact.artifact_id))
        if self.corrupt_first_labels and len(self.seen) == 1:
            labels_digest = _digest("incorrect-labels")
        oci_layout_digest = _digest(f"oci-layout:{artifact.artifact_id}")
        sbom_digest = _digest(f"sbom:{artifact.artifact_id}")
        provenance_digest = _digest(f"provenance:{artifact.artifact_id}")
        evidence = ArtifactEvidenceReceipt(
            artifact_id=artifact.artifact_id,
            role=role,
            platform=artifact.platform,
            variant=artifact.variant,
            source_revision=plan.request.source_revision,
            package_version=plan.request.package_version,
            base_image_reference=artifact.base_image_reference,
            recipe_policy_digest=plan.recipe_policy_digest,
            policy_digest=(
                _digest("incorrect-policy")
                if self.corrupt_first_policy and len(self.seen) == 1
                else plan.policy_digest
            ),
            collaboration_surface_digest=artifact.collaboration_surface_digest,
            application_inventory_digest=_digest(f"inventory:{artifact.artifact_id}"),
            oci_layout_digest=oci_layout_digest,
            image_digest=image_digest,
            oci_labels_digest=labels_digest,
            sbom_digest=sbom_digest,
            sbom_subject_digest=(
                _digest("incorrect-sbom-subject")
                if self.corrupt_first_sbom_subject and len(self.seen) == 1
                else image_digest
            ),
            provenance_digest=provenance_digest,
            provenance_subject_digest=(
                _digest("incorrect-provenance-subject")
                if self.corrupt_first_provenance_subject and len(self.seen) == 1
                else image_digest
            ),
        )
        return ArtifactMaterialization(
            artifact_id=artifact.artifact_id,
            role=role,
            platform=artifact.platform,
            variant=artifact.variant,
            immutable_reference=f"oci@{image_digest}",
            image_digest=image_digest,
            oci_layout_digest=oci_layout_digest,
            oci_labels_digest=labels_digest,
            sbom_digest=sbom_digest,
            provenance_digest=provenance_digest,
            evidence_receipt=evidence,
        )


def test_compiler_prepares_role_platform_and_compute_variant_matrix():
    plan = ContainerArtifactCompiler().prepare(_request())

    assert [
        (artifact.role, artifact.platform, artifact.variant) for artifact in plan.artifacts
    ] == [
        (PP_LOCAL_EDGE, "linux/amd64", "standard"),
        (PP_SERVER_BACKEND, "linux/amd64", "standard"),
        (PP_COMPUTE_NODE, "linux/amd64", COMPUTE_VARIANT_CPU),
        (PP_COMPUTE_NODE, "linux/amd64", COMPUTE_VARIANT_CUDA),
        (PP_LOCAL_EDGE, "linux/arm64", "standard"),
        (PP_SERVER_BACKEND, "linux/arm64", "standard"),
        (PP_COMPUTE_NODE, "linux/arm64", COMPUTE_VARIANT_CPU),
    ]
    assert plan.artifact_for(PP_COMPUTE_NODE, "linux/amd64", COMPUTE_VARIANT_CPU).capabilities == (
        "embedding/v1",
        "rerank/v1",
        "structured-json/v1",
    )
    assert plan.artifact_for(PP_COMPUTE_NODE, "linux/amd64", COMPUTE_VARIANT_CUDA).capabilities == (
        "embedding/v1",
        "rerank/v1",
        "structured-json/v1",
    )
    compute = plan.artifact_for(PP_COMPUTE_NODE, "linux/amd64", COMPUTE_VARIANT_CPU)
    assert (
        plan.expected_oci_labels(compute.artifact_id)["org.plastic-promise.compute.capabilities"]
        == "embedding/v1,rerank/v1,structured-json/v1"
    )


def test_server_recipe_requires_role_package_compiler(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[1]
    shutil.copytree(repository_root / "deploy", tmp_path / "deploy")
    shutil.copytree(repository_root / "plastic_promise", tmp_path / "plastic_promise")
    shutil.copy(repository_root / ".dockerignore", tmp_path / ".dockerignore")
    dockerfile = tmp_path / "deploy/server/Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace(
            "python -m plastic_promise.role_package",
            "python -m plastic_promise.other_package",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ContainerArtifactError,
        match="container_recipe_server_role_package_required",
    ):
        ContainerArtifactCompiler(tmp_path).prepare(_request())


def test_compiler_binds_exact_collaboration_surface_matrix_into_policy():
    plan = ContainerArtifactCompiler().prepare(_request())
    server_modules = endpoint_role_contract(PP_SERVER_BACKEND).collaboration_modules
    expected_by_role = {
        PP_LOCAL_EDGE: ((), "absent"),
        PP_SERVER_BACKEND: (server_modules, "source-only-unwired"),
        PP_COMPUTE_NODE: ((), "absent"),
    }

    for artifact in plan.artifacts:
        modules, writer_surface = expected_by_role[artifact.role]
        expected_payload = {
            "modules": list(modules),
            "writer_surface": writer_surface,
        }
        expected_digest = _mapping_digest(expected_payload)
        assert artifact.collaboration_surface.to_dict() == {
            **expected_payload,
            "digest": expected_digest,
        }
        assert artifact.collaboration_surface_digest == expected_digest
        assert artifact.to_dict()["collaboration_surface_digest"] == expected_digest

    policy_payload = plan.to_dict()
    recorded_policy_digest = policy_payload.pop("policy_digest")
    server_payload = next(
        artifact
        for artifact in policy_payload["artifacts"]
        if artifact["role"] == PP_SERVER_BACKEND
    )
    server_payload["collaboration_surface"]["writer_surface"] = "absent"
    assert _mapping_digest(policy_payload) != recorded_policy_digest


def test_public_deployment_policy_import_does_not_require_http_adapter(tmp_path: Path):
    """Release-build preflight installs PyYAML, not the HTTP runtime stack."""

    fake_site = tmp_path / "fake-site"
    fake_starlette = fake_site / "starlette"
    fake_starlette.mkdir(parents=True)
    (fake_starlette / "__init__.py").write_text(
        "raise RuntimeError('starlette_must_not_be_imported_for_recipe_policy')\n",
        encoding="utf-8",
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(fake_site), str(repository_root), environment.get("PYTHONPATH"))
        if value
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from plastic_promise.deployment import StaticRecipePolicyValidator; "
                "print(StaticRecipePolicyValidator(Path('.')).validate().schema_version)"
            ),
        ],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "plastic-promise-container-recipe-policy/v1" in result.stdout


def test_recipe_tmpfs_policy_allows_compute_jit_but_keeps_server_noexec():
    repository_root = Path(__file__).resolve().parents[1]
    compute_paths = (
        "deploy/local-inference-node/compose.yaml",
        "deploy/local-inference-node/compose.cpu.yaml",
        "deploy/local-inference-node/compose.cuda.yaml",
    )

    for relative_path in compute_paths:
        compose = (repository_root / relative_path).read_text(encoding="utf-8")
        assert "/tmp:rw,exec,nosuid,size=512m" in compose
        assert "/tmp:rw,noexec,nosuid,size=512m" not in compose

    server_compose = (repository_root / "deploy/server/compose.yaml").read_text(encoding="utf-8")
    assert "/tmp:rw,noexec,nosuid,size=256m" in server_compose
    assert "/tmp:rw,exec,nosuid,size=256m" not in server_compose


def test_public_deployment_namespace_lazily_exposes_ppctl_runtime_adapter():
    from plastic_promise.deployment import Ppctl, PpctlHttpAdapter, create_ppctl_app
    from plastic_promise.deployment.ppctl import Ppctl as DirectPpctl

    assert Ppctl is DirectPpctl
    assert PpctlHttpAdapter.__name__ == "PpctlHttpAdapter"
    assert callable(create_ppctl_app)


def test_compiler_keeps_canonical_state_and_sensitive_content_out_of_other_artifacts():
    plan = ContainerArtifactCompiler().prepare(_request())

    backend = plan.artifact_for(PP_SERVER_BACKEND, "linux/amd64")
    assert [(mount.name, mount.access) for mount in backend.mounts] == [
        ("canonical-state", "read-write"),
        ("backend-tmp", "tmpfs"),
    ]
    for artifact in plan.artifacts:
        assert artifact.non_root is True
        assert artifact.read_only_rootfs is True
        assert "credentials" in artifact.layer_exclusions
        assert "model-weights" in artifact.layer_exclusions
        assert "host-docker-socket" in artifact.layer_exclusions
        if artifact.role != PP_SERVER_BACKEND:
            assert all(mount.name != "canonical-state" for mount in artifact.mounts)
        if artifact.role != PP_COMPUTE_NODE:
            assert all(mount.name != "model-catalog" for mount in artifact.mounts)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        (
            {
                "profile_id": "local-cloud",
                "compute_variants": (COMPUTE_VARIANT_CPU,),
            },
            "container_artifact_cloud_compute_forbidden",
        ),
        (
            {"platforms": ("linux/arm64",)},
            "container_artifact_cuda_platform_unsupported",
        ),
        (
            {"source_revision": "main"},
            "container_artifact_source_revision_not_pinned",
        ),
    ],
)
def test_compiler_rejects_unpinned_or_incompatible_build_requests(
    overrides: dict[str, object], code: str
):
    with pytest.raises(ContainerArtifactError, match=code):
        _request(**overrides)


def test_compiler_materializes_only_through_executor_and_returns_safe_projection():
    compiler = ContainerArtifactCompiler()
    plan = compiler.prepare(_request())
    executor = _RecordingExecutor()

    bundle = compiler.materialize(plan, executor)

    assert executor.seen == [artifact.artifact_id for artifact in plan.artifacts]
    projection = bundle.inspection_projection()
    assert projection["policy_digest"] == plan.policy_digest
    assert projection["inspection"] == {
        "schema_version": "plastic-promise-container-bundle/v1",
        "policy_digest": plan.policy_digest,
        "artifact_ids": [artifact.artifact_id for artifact in plan.artifacts],
        "outcome": "pass",
    }
    assert "source_revision" not in str(projection)
    assert "model_catalog" not in str(projection)


def test_artifact_evidence_receipt_round_trips_and_binds_surface_and_inventory():
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for(PP_SERVER_BACKEND, "linux/amd64")
    receipt = _RecordingExecutor().materialize(plan, artifact).evidence_receipt
    payload = receipt.to_dict()

    assert CONTAINER_ARTIFACT_EVIDENCE_SCHEMA_VERSION == "plastic-promise-container-evidence/v2"
    assert payload["schema_version"] == CONTAINER_ARTIFACT_EVIDENCE_SCHEMA_VERSION
    assert (
        artifact.collaboration_surface.source_paths
        == endpoint_role_contract(PP_SERVER_BACKEND).collaboration_source_paths
    )
    assert payload["collaboration_surface_digest"] == artifact.collaboration_surface_digest
    assert payload["application_inventory_digest"] == _digest(f"inventory:{artifact.artifact_id}")
    assert ArtifactEvidenceReceipt.from_dict(payload) == receipt

    with pytest.raises(
        ContainerArtifactError,
        match="container_artifact_evidence_collaboration_surface_mismatch",
    ):
        replace(
            receipt,
            collaboration_surface_digest=_digest("wrong-collaboration-surface"),
        ).validate_against(plan, artifact)

    payload["application_inventory_digest"] = _digest("tampered-application-inventory")
    with pytest.raises(
        ContainerArtifactError,
        match="container_artifact_evidence_receipt_digest_mismatch",
    ):
        ArtifactEvidenceReceipt.from_dict(payload)

    legacy_payload = receipt.to_dict()
    legacy_payload["schema_version"] = "plastic-promise-container-evidence/v1"
    with pytest.raises(
        ContainerArtifactError,
        match="container_artifact_evidence_schema_unsupported",
    ):
        ArtifactEvidenceReceipt.from_dict(legacy_payload)


def test_compiler_rejects_evidence_that_changes_a_planned_role():
    compiler = ContainerArtifactCompiler()
    plan = compiler.prepare(_request())

    with pytest.raises(ContainerArtifactError, match="container_artifact_evidence_target_mismatch"):
        compiler.materialize(plan, _RecordingExecutor(corrupt_first_role=True))


def test_compiler_rejects_evidence_with_the_wrong_oci_label_set():
    compiler = ContainerArtifactCompiler()
    plan = compiler.prepare(_request())

    with pytest.raises(ContainerArtifactError, match="container_artifact_evidence_labels_mismatch"):
        compiler.materialize(plan, _RecordingExecutor(corrupt_first_labels=True))


def test_compiler_rejects_evidence_with_the_wrong_plan_binding():
    compiler = ContainerArtifactCompiler()
    plan = compiler.prepare(_request())

    with pytest.raises(
        ContainerArtifactError, match="container_artifact_evidence_binding_mismatch"
    ):
        compiler.materialize(plan, _RecordingExecutor(corrupt_first_policy=True))


@pytest.mark.parametrize(
    ("corruption", "code"),
    [
        ("corrupt_first_sbom_subject", "container_artifact_sbom_subject_mismatch"),
        ("corrupt_first_provenance_subject", "container_artifact_provenance_subject_mismatch"),
    ],
)
def test_compiler_rejects_evidence_whose_sbom_or_provenance_has_a_different_subject(
    corruption: str, code: str
):
    compiler = ContainerArtifactCompiler()
    plan = compiler.prepare(_request())

    with pytest.raises(ContainerArtifactError, match=code):
        compiler.materialize(plan, _RecordingExecutor(**{corruption: True}))


def _copy_recipe_tree(destination: Path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    for relative_path in (
        ".dockerignore",
        "deploy/oci-base-images.json",
        "deploy/local-edge/Dockerfile",
        "deploy/local-edge/compose.yaml",
        "deploy/local-edge/entrypoint.sh",
        "deploy/local-edge/nginx.conf",
        "deploy/server/Dockerfile",
        "deploy/server/compose.yaml",
        "deploy/local-inference-node/Dockerfile",
        "deploy/local-inference-node/compose.cpu.yaml",
        "deploy/local-inference-node/compose.cuda.yaml",
        "deploy/local-inference-node/compose.yaml",
        *endpoint_role_contract(PP_SERVER_BACKEND).collaboration_source_paths,
    ):
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repository_root / relative_path, target)


@pytest.mark.parametrize(
    ("original", "replacement", "code"),
    [
        (
            "FROM ${BASE_IMAGE} AS compute-package",
            "FROM ${BASE_IMAGE} AS renamed-package",
            "container_recipe_compute_source_stage_invalid",
        ),
        (
            "python3 -m plastic_promise.role_package",
            "RUN true",
            "container_recipe_compute_role_package_required",
        ),
        (
            "COPY --from=compute-package /role-package /app",
            "COPY plastic_promise /app/plastic_promise",
            "container_recipe_compute_final_copy_invalid",
        ),
    ],
)
def test_compute_recipe_fails_closed_when_server_only_pruning_contract_drifts(
    tmp_path: Path, original: str, replacement: str, code: str
):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / "deploy/local-inference-node/Dockerfile"
    text = target.read_text(encoding="utf-8")
    assert original in text
    target.write_text(text.replace(original, replacement, 1), encoding="utf-8")

    with pytest.raises(ContainerArtifactError, match=code):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


@pytest.mark.parametrize(
    ("relative_path", "anchor", "injected", "code"),
    [
        (
            "deploy/server/compose.yaml",
            '      PLASTIC_RUNTIME_MODE: "${PLASTIC_RUNTIME_MODE:-normal}"\n',
            '      PLASTIC_COLLABORATION_EVENT_WRITER: "enabled"\n',
            "container_recipe_backend_canonical_mount_required",
        ),
        (
            "deploy/local-edge/compose.yaml",
            '      PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT: "${PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT:-}"\n',
            '      PP_COLLABORATION_EVENT_WRITER: "enabled"\n',
            "container_recipe_edge_service_contract_invalid",
        ),
        (
            "deploy/local-inference-node/compose.cpu.yaml",
            '      PP_LOCAL_NODE_BIND_HOST: "127.0.0.1"\n',
            '      PP_COLLABORATION_EVENT_WRITER: "enabled"\n',
            "container_recipe_compute_contract_invalid",
        ),
    ],
)
def test_recipes_reject_collaboration_writer_runtime_configuration(
    tmp_path: Path,
    relative_path: str,
    anchor: str,
    injected: str,
    code: str,
):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / relative_path
    text = target.read_text(encoding="utf-8")
    assert anchor in text
    target.write_text(text.replace(anchor, anchor + injected, 1), encoding="utf-8")

    with pytest.raises(ContainerArtifactError, match=code):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


@pytest.mark.parametrize(
    ("relative_path", "original", "replacement", "code"),
    [
        (
            "deploy/local-edge/Dockerfile",
            "USER 101",
            "USER root",
            "container_recipe_final_user_invalid",
        ),
        (
            "deploy/local-inference-node/Dockerfile",
            "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}",
            "ARG BASE_IMAGE=python:3.12-slim-bookworm\nFROM ${BASE_IMAGE}",
            "container_recipe_identity_default_forbidden",
        ),
        (
            "deploy/local-inference-node/Dockerfile",
            "ARG BUILD_POLICY_DIGEST\n",
            "ARG BUILD_POLICY_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000\n",
            "container_recipe_identity_default_forbidden",
        ),
        (
            "deploy/local-edge/Dockerfile",
            'ENTRYPOINT ["plastic-promise-local-edge"]',
            'VOLUME /var/lib/plastic-promise\nENTRYPOINT ["plastic-promise-local-edge"]',
            "container_recipe_dockerfile_opcode_forbidden",
        ),
        (
            "deploy/local-edge/Dockerfile",
            "FROM ${BASE_IMAGE}",
            "FROM ${BASE_IMAGE}\nFROM evil:latest",
            "container_recipe_base_image_arg_required",
        ),
        (
            "deploy/local-inference-node/Dockerfile",
            "RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked",
            "RUN --mount=type=bind,source=/etc,target=/mnt,readonly",
            "container_recipe_run_mount_forbidden",
        ),
        (
            ".dockerignore",
            "models\n",
            "",
            "container_recipe_ignore_pattern_missing",
        ),
        (
            "deploy/local-inference-node/compose.cuda.yaml",
            "    gpus: all\n",
            "",
            "container_recipe_compute_cuda_gpu_required",
        ),
        (
            "deploy/local-edge/compose.yaml",
            "    read_only: true\n",
            "    read_only: true\n    read_only: false\n",
            "container_recipe_compose_duplicate_key",
        ),
        (
            "deploy/local-edge/compose.yaml",
            "    restart: unless-stopped\n",
            "    restart: unless-stopped\n    privileged: true\n",
            "container_recipe_compose_privileged_forbidden",
        ),
        (
            "deploy/local-edge/compose.yaml",
            "    cap_drop:\n      - ALL\n",
            "    cap_drop:\n      - ALL\n    cap_add:\n      - NET_ADMIN\n",
            "container_recipe_compose_capability_forbidden",
        ),
        (
            "deploy/local-edge/compose.yaml",
            '      PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT: "${PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT:-}"\n',
            '      PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT: "${PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT:-}"\n'
            '      PP_LOCAL_EDGE_UNSAFE_OVERRIDE: "enabled"\n',
            "container_recipe_edge_service_contract_invalid",
        ),
    ],
)
def test_static_recipe_policy_rejects_unsafe_source_recipe_mutations(
    tmp_path: Path, relative_path: str, original: str, replacement: str, code: str
):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / relative_path
    text = target.read_text(encoding="utf-8")
    assert original in text
    target.write_text(text.replace(original, replacement, 1), encoding="utf-8")

    with pytest.raises(ContainerArtifactError, match=code):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


@pytest.mark.parametrize(
    ("compose_file", "embedding_backend", "rerank_backend", "ollama_host"),
    [
        (
            "deploy/local-inference-node/compose.cpu.yaml",
            "llama.cpp",
            "llama.cpp",
            "http://127.0.0.1:11434",
        ),
        (
            "deploy/local-inference-node/compose.yaml",
            "llama.cpp",
            "llama.cpp",
            "http://host.docker.internal:11434",
        ),
    ],
)
def test_compute_recipe_environment_contract_has_variant_aware_defaults(
    compose_file: str,
    embedding_backend: str,
    rerank_backend: str,
    ollama_host: str,
):
    """The compute-node recipe contract and compose env defaults stay in lockstep."""

    repository_root = Path(__file__).resolve().parents[1]
    text = (repository_root / compose_file).read_text(encoding="utf-8")
    assert (
        f'PP_LOCAL_NODE_EMBEDDING_BACKEND: "${{PP_LOCAL_NODE_EMBEDDING_BACKEND:-{embedding_backend}}}"'
        in text
    )
    assert (
        f'PP_LOCAL_NODE_RERANK_BACKEND: "${{PP_LOCAL_NODE_RERANK_BACKEND:-{rerank_backend}}}"'
        in text
    )
    assert f'PP_LOCAL_NODE_OLLAMA_HOST: "${{PP_LOCAL_NODE_OLLAMA_HOST:-{ollama_host}}}"' in text


def test_static_recipe_policy_rejects_required_label_forged_inside_run_instruction(
    tmp_path: Path,
):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / "deploy/local-edge/Dockerfile"
    text = target.read_text(encoding="utf-8")
    text = text.replace(
        'org.plastic-promise.endpoint.role="pp-local-edge"',
        'org.plastic-promise.endpoint.role="forged-role"',
        1,
    )
    text = text.replace(
        "RUN chmod 0555 /usr/local/bin/plastic-promise-local-edge",
        "RUN printf '%s' 'org.plastic-promise.endpoint.role=\"pp-local-edge\"' "
        "&& chmod 0555 /usr/local/bin/plastic-promise-local-edge",
        1,
    )
    target.write_text(text, encoding="utf-8")

    with pytest.raises(ContainerArtifactError, match="container_recipe_label_or_identity_mismatch"):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


def test_static_recipe_policy_rejects_missing_required_actual_label(tmp_path: Path):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / "deploy/server/Dockerfile"
    text = target.read_text(encoding="utf-8")
    text = text.replace(
        "org.plastic-promise.endpoint.contract=",
        "org.plastic-promise.endpoint.contract-missing=",
        1,
    )
    target.write_text(text, encoding="utf-8")

    with pytest.raises(ContainerArtifactError, match="container_recipe_label_or_identity_mismatch"):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


def test_server_recipe_requires_staged_source_cleanup_after_install(tmp_path: Path):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / "deploy/server/Dockerfile"
    text = target.read_text(encoding="utf-8")
    text = text.replace(
        "    && rm -rf /app/plastic_promise /app/build \\\n",
        "",
        1,
    )
    target.write_text(text, encoding="utf-8")

    with pytest.raises(
        ContainerArtifactError,
        match="container_recipe_server_source_cleanup_required",
    ):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


def test_server_recipe_requires_build_tree_cleanup_after_install(tmp_path: Path):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / "deploy/server/Dockerfile"
    text = target.read_text(encoding="utf-8")
    text = text.replace(" /app/build \\\n", " \\\n", 1)
    target.write_text(text, encoding="utf-8")

    with pytest.raises(
        ContainerArtifactError,
        match="container_recipe_server_source_cleanup_required",
    ):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


def test_server_recipe_rejects_cleanup_before_package_install(tmp_path: Path):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / "deploy/server/Dockerfile"
    text = target.read_text(encoding="utf-8")
    text = text.replace(
        "RUN python -m pip install --no-cache-dir . \\\n    && rm -rf /app/plastic_promise /app/build \\\n",
        "RUN rm -rf /app/plastic_promise\nRUN python -m pip install --no-cache-dir . \\\n",
        1,
    )
    target.write_text(text, encoding="utf-8")

    with pytest.raises(
        ContainerArtifactError,
        match="container_recipe_server_source_cleanup_required",
    ):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


def test_static_recipe_policy_rejects_dockerfile_heredocs(tmp_path: Path):
    _copy_recipe_tree(tmp_path)
    target = tmp_path / "deploy/local-edge/Dockerfile"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nRUN <<'PP_EOF'\ntrue\nPP_EOF\n",
        encoding="utf-8",
    )

    with pytest.raises(ContainerArtifactError, match="container_recipe_heredoc_forbidden"):
        ContainerArtifactCompiler(repository_root=tmp_path).prepare(_request())


def test_source_recipes_expose_the_compiler_required_endpoint_labels():
    repository_root = Path(__file__).resolve().parents[1]
    recipes = {
        PP_LOCAL_EDGE: repository_root / "deploy" / "local-edge" / "Dockerfile",
        PP_SERVER_BACKEND: repository_root / "deploy" / "server" / "Dockerfile",
        PP_COMPUTE_NODE: repository_root / "deploy" / "local-inference-node" / "Dockerfile",
    }

    for role, recipe in recipes.items():
        text = recipe.read_text(encoding="utf-8")
        assert f'org.plastic-promise.endpoint.role="{role}"' in text
        assert "org.plastic-promise.endpoint.variant" in text
        assert f'org.plastic-promise.endpoint.contract="{ENDPOINT_CONTRACT_SCHEMA_VERSION}"' in text
