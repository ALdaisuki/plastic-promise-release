"""Focused verification tests for the OCI-layout evidence CLI."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from plastic_promise.deployment import (
    COMPUTE_VARIANT_CPU,
    COMPUTE_VARIANT_CUDA,
    ArtifactRequest,
    ContainerArtifactCompiler,
)
from plastic_promise.endpoint_roles import PP_SERVER_BACKEND, endpoint_role_contract

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPOSITORY_ROOT / "scripts" / "verify_oci_artifact_evidence.py"
_VERIFY_SPEC = importlib.util.spec_from_file_location("verify_oci_artifact_evidence", VERIFY_SCRIPT)
assert _VERIFY_SPEC is not None and _VERIFY_SPEC.loader is not None
_VERIFY_MODULE = importlib.util.module_from_spec(_VERIFY_SPEC)
_VERIFY_SPEC.loader.exec_module(_VERIFY_MODULE)
_apply_layer = _VERIFY_MODULE._apply_layer
SOURCE_REVISION = "a" * 40
CATALOG_DIGEST = "sha256:" + ("b" * 64)
COLLABORATION_FILES = tuple(
    f"app/{path}" for path in endpoint_role_contract(PP_SERVER_BACKEND).collaboration_source_paths
)


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _descriptor(value: bytes, media_type: str, **extra: object) -> dict[str, object]:
    return {"mediaType": media_type, "digest": _digest(value), "size": len(value), **extra}


def _tar_layer(paths: tuple[str, ...] | dict[str, bytes]) -> bytes:
    payload = BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        entries = paths.items() if isinstance(paths, dict) else ((path, None) for path in paths)
        for path, explicit_value in entries:
            value = (
                explicit_value
                if explicit_value is not None
                else (b"" if "/.wh." in f"/{path}" else path.encode("utf-8"))
            )
            info = tarfile.TarInfo(path)
            info.size = len(value)
            archive.addfile(info, BytesIO(value))
    return payload.getvalue()


def _tar_layer_with_symlink() -> bytes:
    payload = BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        target = tarfile.TarInfo("bin/busybox")
        target.size = len(b"busybox")
        archive.addfile(target, BytesIO(b"busybox"))
        link = tarfile.TarInfo("bin/sh")
        link.type = tarfile.SYMTYPE
        link.linkname = "bin//bin/busybox"
        archive.addfile(link)
    return payload.getvalue()


def test_oci_layer_inventory_does_not_dereference_symlink_targets(tmp_path: Path):
    """Base-image symlinks must not make evidence parsing fail closed."""

    payload = _tar_layer_with_symlink()
    rootfs_paths: set[str] = set()
    _apply_layer(rootfs_paths, payload)
    assert rootfs_paths == {"bin/busybox", "bin/sh"}


def test_role_receipt_inside_package_is_not_source_inventory():
    package_root = "usr/local/lib/python3.12/site-packages/plastic_promise"
    inventory = (
        f"{package_root}/__init__.py",
        f"{package_root}/role-package.receipt.json",
    )
    projected = _VERIFY_MODULE._package_relative_paths(inventory)
    assert "plastic_promise/role-package.receipt.json" not in projected
    assert projected == {"plastic_promise/__init__.py": (f"{package_root}/__init__.py",)}


def _write_oci_layout(
    path: Path,
    *,
    labels: dict[str, str],
    include_sbom: bool,
    sbom_subject_digest: str | None = None,
    provenance_subject_digest: str | None = None,
    empty_statement_subjects: bool = False,
    rootfs_layers: tuple[tuple[str, ...] | dict[str, bytes], ...] = (),
    sbom_files: tuple[str, ...] = (),
    sbom_predicate: dict[str, object] | None = None,
    statement_type: str = "https://in-toto.io/Statement/v0.1",
) -> None:
    blobs: dict[str, bytes] = {}

    config_blob = _canonical({"config": {"Labels": labels}})
    config = _descriptor(config_blob, "application/vnd.oci.image.config.v1+json")
    blobs[config["digest"]] = config_blob  # type: ignore[index]

    image_layers: list[dict[str, object]] = []
    for paths in rootfs_layers:
        layer_blob = _tar_layer(paths)
        layer = _descriptor(layer_blob, "application/vnd.oci.image.layer.v1.tar")
        blobs[layer["digest"]] = layer_blob  # type: ignore[index]
        image_layers.append(layer)

    image_manifest_blob = _canonical({"schemaVersion": 2, "config": config, "layers": image_layers})
    image = _descriptor(
        image_manifest_blob,
        "application/vnd.oci.image.manifest.v1+json",
        platform={"os": "linux", "architecture": "amd64"},
    )
    blobs[image["digest"]] = image_manifest_blob  # type: ignore[index]

    attestation_layers: list[dict[str, object]] = []

    def statement(
        predicate_type: str,
        subject_digest: str | None,
        predicate: dict[str, object],
    ) -> bytes:
        digest = subject_digest or image["digest"]
        assert isinstance(digest, str)
        subjects: list[dict[str, object]] = []
        if not empty_statement_subjects:
            subjects.append(
                {
                    "name": "_",
                    "digest": {"sha256": digest.removeprefix("sha256:")},
                }
            )
        return _canonical(
            {
                "_type": statement_type,
                "predicateType": predicate_type,
                "predicate": predicate,
                "subject": subjects,
            }
        )

    if include_sbom:
        resolved_sbom_predicate = sbom_predicate
        if resolved_sbom_predicate is None:
            resolved_sbom_predicate = {
                "spdxVersion": "SPDX-2.3",
                "SPDXID": "SPDXRef-DOCUMENT",
                "dataLicense": "CC0-1.0",
                "name": "plastic-promise-test-image",
                "documentNamespace": "https://plastic-promise.test/spdx/test-image",
                "creationInfo": {
                    "created": "2026-08-10T00:00:00Z",
                    "creators": ["Tool: plastic-promise-test-fixture"],
                },
                "packages": [],
                "files": [
                    {
                        "SPDXID": f"SPDXRef-File-{index}",
                        "fileName": f"/{file_name}",
                    }
                    for index, file_name in enumerate(sbom_files, start=1)
                ],
            }
        sbom_blob = statement(
            "https://spdx.dev/Document",
            sbom_subject_digest,
            resolved_sbom_predicate,
        )
        sbom = _descriptor(
            sbom_blob,
            "application/vnd.in-toto+json",
            annotations={"in-toto.io/predicate-type": "https://spdx.dev/Document"},
        )
        blobs[sbom["digest"]] = sbom_blob  # type: ignore[index]
        attestation_layers.append(sbom)
    provenance_blob = statement(
        "https://slsa.dev/provenance/v1",
        provenance_subject_digest,
        {"buildType": "https://plastic-promise.test/build"},
    )
    provenance = _descriptor(
        provenance_blob,
        "application/vnd.in-toto+json",
        annotations={"in-toto.io/predicate-type": "https://slsa.dev/provenance/v1"},
    )
    blobs[provenance["digest"]] = provenance_blob  # type: ignore[index]
    attestation_layers.append(provenance)
    attestation_blob = _canonical({"schemaVersion": 2, "layers": attestation_layers})
    attestation = _descriptor(
        attestation_blob,
        "application/vnd.oci.image.manifest.v1+json",
        annotations={
            "vnd.docker.reference.type": "attestation-manifest",
            "vnd.docker.reference.digest": image["digest"],
        },
    )
    blobs[attestation["digest"]] = attestation_blob  # type: ignore[index]
    index_blob = _canonical({"schemaVersion": 2, "manifests": [image, attestation]})

    with tarfile.open(path, "w") as archive:
        members = {
            "index.json": index_blob,
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            **{
                f"blobs/sha256/{digest.removeprefix('sha256:')}": value
                for digest, value in blobs.items()
            },
        }
        for name, value in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, BytesIO(value))


def _request() -> ArtifactRequest:
    return ArtifactRequest(
        profile_id="split-accelerated",
        source_revision=SOURCE_REVISION,
        package_version="0.8.0rc1",
        platforms=("linux/amd64", "linux/arm64"),
        compute_variants=(COMPUTE_VARIANT_CPU, COMPUTE_VARIANT_CUDA),
        model_catalog_reference="pr-verify-catalog",
        model_catalog_digest=CATALOG_DIGEST,
    )


def _command(
    layout: Path,
    output: Path,
    *,
    role: str = "pp-local-edge",
    variant: str = "standard",
) -> list[str]:
    return [
        sys.executable,
        str(VERIFY_SCRIPT),
        "--repository-root",
        str(REPOSITORY_ROOT),
        "--profile-id",
        "split-accelerated",
        "--source-revision",
        SOURCE_REVISION,
        "--package-version",
        "0.8.0rc1",
        "--plan-platform",
        "linux/amd64",
        "--plan-platform",
        "linux/arm64",
        "--compute-variant",
        "cpu",
        "--compute-variant",
        "cuda",
        "--model-catalog-reference",
        "pr-verify-catalog",
        "--model-catalog-digest",
        CATALOG_DIGEST,
        "--role",
        role,
        "--platform",
        "linux/amd64",
        "--variant",
        variant,
        "--oci-layout",
        str(layout),
        "--output",
        str(output),
    ]


def test_oci_evidence_cli_binds_an_attested_layout_to_the_prepared_artifact(tmp_path: Path):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-local-edge", "linux/amd64")
    layout = tmp_path / "local-edge.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout, labels=plan.expected_oci_labels(artifact.artifact_id), include_sbom=True
    )

    result = subprocess.run(
        _command(layout, output), cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["artifact_id"] == artifact.artifact_id
    assert receipt["policy_digest"] == plan.policy_digest
    assert receipt["recipe_policy_digest"] == plan.recipe_policy_digest
    assert receipt["sbom_subject_digest"] == receipt["image_digest"]
    assert receipt["provenance_subject_digest"] == receipt["image_digest"]
    assert receipt["collaboration_surface_digest"] == artifact.collaboration_surface_digest
    assert receipt["application_inventory_digest"] == _digest(_canonical([]))


def test_oci_evidence_cli_requires_and_binds_a_complete_compute_role_package(tmp_path: Path):
    from plastic_promise.role_package import RolePackageCompiler

    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-compute-node", "linux/amd64", COMPUTE_VARIANT_CPU)
    materialized = RolePackageCompiler(REPOSITORY_ROOT).materialize(
        "pp-compute-node", tmp_path / "role-package", "0.8.0rc1"
    )
    receipt_bytes = (tmp_path / "role-package" / "role-package.receipt.json").read_bytes()
    package_paths = tuple(
        f"usr/local/lib/python3/site-packages/{path}" for path in materialized.source_paths
    )
    rootfs_files = {path: path.encode("utf-8") for path in package_paths}
    receipt_path = (
        "usr/local/lib/python3.12/site-packages/plastic_promise/role-package.receipt.json"
    )
    rootfs_files[receipt_path] = receipt_bytes
    sbom_files = package_paths + (receipt_path,)
    layout = tmp_path / "compute-role-package.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        rootfs_layers=(rootfs_files,),
        sbom_files=sbom_files,
    )

    result = subprocess.run(
        _command(layout, output, role="pp-compute-node", variant=COMPUTE_VARIANT_CPU),
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.exists()


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        ("missing-receipt", "container_artifact_role_package_receipt_missing"),
        ("extra-core", "container_artifact_role_package_inventory_mismatch"),
        ("duplicate-package", "container_artifact_role_package_duplicate_path"),
        ("receipt-digest", "container_artifact_role_package_receipt_digest_mismatch"),
        ("sbom-extra-core", "container_artifact_sbom_role_package_inventory_mismatch"),
    ],
)
def test_oci_evidence_cli_rejects_role_package_tampering(
    tmp_path: Path, mutate: str, expected_error: str
):
    from plastic_promise.role_package import RolePackageCompiler

    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-compute-node", "linux/amd64", COMPUTE_VARIANT_CPU)
    materialized = RolePackageCompiler(REPOSITORY_ROOT).materialize(
        "pp-compute-node", tmp_path / "role-package", "0.8.0rc1"
    )
    receipt_path = tmp_path / "role-package" / "role-package.receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    package_paths = tuple(
        f"usr/local/lib/python3/site-packages/{path}" for path in materialized.source_paths
    )
    rootfs_files = {path: path.encode("utf-8") for path in package_paths}
    sbom_files = list(package_paths)
    if mutate == "extra-core":
        rootfs_files["usr/local/lib/python3/site-packages/plastic_promise/core/embedder.py"] = b"x"
    elif mutate == "duplicate-package":
        duplicate = "app/plastic_promise/local_inference_node/app.py"
        rootfs_files[duplicate] = b"x"
    elif mutate == "receipt-digest":
        receipt = json.loads(receipt_bytes)
        receipt["package_digest"] = "sha256:" + ("0" * 64)
        receipt_bytes = _canonical(receipt)
    elif mutate == "sbom-extra-core":
        sbom_files.append("usr/local/lib/python3/site-packages/plastic_promise/core/embedder.py")
    if mutate != "missing-receipt":
        rootfs_files["app/role-package.receipt.json"] = receipt_bytes
        sbom_files.append("app/role-package.receipt.json")
    layout = tmp_path / f"compute-tampered-{mutate}.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        rootfs_layers=(rootfs_files,),
        sbom_files=tuple(sbom_files),
    )

    result = subprocess.run(
        _command(layout, output, role="pp-compute-node", variant=COMPUTE_VARIANT_CPU),
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    (
        "role",
        "variant",
        "rootfs_layers",
        "sbom_files",
        "expected_error",
        "expected_inventory",
    ),
    [
        (
            "pp-server-backend",
            "standard",
            (COLLABORATION_FILES,),
            COLLABORATION_FILES,
            "container_artifact_role_package_receipt_missing",
            COLLABORATION_FILES,
        ),
        (
            "pp-server-backend",
            "standard",
            (
                COLLABORATION_FILES
                + tuple(
                    path.replace(
                        "app/plastic_promise/collaboration/",
                        "usr/lib/python3/site-packages/plastic_promise/collaboration/__pycache__/",
                    ).replace(".py", ".cpython-312.pyc")
                    for path in COLLABORATION_FILES
                ),
            ),
            COLLABORATION_FILES
            + tuple(
                path.replace(
                    "app/plastic_promise/collaboration/",
                    "usr/lib/python3/site-packages/plastic_promise/collaboration/__pycache__/",
                ).replace(".py", ".cpython-312.pyc")
                for path in COLLABORATION_FILES
            ),
            "container_artifact_role_package_receipt_missing",
            COLLABORATION_FILES
            + tuple(
                path.replace(
                    "app/plastic_promise/collaboration/",
                    "usr/lib/python3/site-packages/plastic_promise/collaboration/__pycache__/",
                ).replace(".py", ".cpython-312.pyc")
                for path in COLLABORATION_FILES
            ),
        ),
        (
            "pp-server-backend",
            "standard",
            (COLLABORATION_FILES[:-1],),
            COLLABORATION_FILES[:-1],
            "container_artifact_collaboration_foundation_missing",
            (),
        ),
        (
            "pp-server-backend",
            "standard",
            (COLLABORATION_FILES + ("app/plastic_promise/collaboration/agent_registry.py",),),
            COLLABORATION_FILES + ("app/plastic_promise/collaboration/agent_registry.py",),
            "container_artifact_collaboration_surface_forbidden",
            (),
        ),
        (
            "pp-server-backend",
            "standard",
            (
                COLLABORATION_FILES
                + (
                    "usr/lib/python3/site-packages/plastic_promise/"
                    "collaboration/__pycache__/agent_registry.cpython-312.pyc",
                ),
            ),
            COLLABORATION_FILES
            + (
                "usr/lib/python3/site-packages/plastic_promise/"
                "collaboration/__pycache__/agent_registry.cpython-312.pyc",
            ),
            "container_artifact_collaboration_surface_forbidden",
            (),
        ),
        (
            "pp-server-backend",
            "standard",
            (
                COLLABORATION_FILES
                + tuple(
                    path.replace(
                        "app/plastic_promise",
                        "usr/lib/python3/site-packages/plastic_promise",
                    )
                    for path in COLLABORATION_FILES
                ),
            ),
            COLLABORATION_FILES
            + tuple(
                path.replace(
                    "app/plastic_promise",
                    "usr/lib/python3/site-packages/plastic_promise",
                )
                for path in COLLABORATION_FILES
            ),
            "container_artifact_collaboration_surface_forbidden",
            (),
        ),
        (
            "pp-local-edge",
            "standard",
            (COLLABORATION_FILES, ("app/plastic_promise/.wh.collaboration",)),
            (),
            None,
            (),
        ),
        (
            "pp-compute-node",
            "cpu",
            (
                COLLABORATION_FILES,
                ("app/plastic_promise/collaboration/.wh..wh..opq",),
            ),
            (),
            "container_artifact_role_package_receipt_missing",
            (),
        ),
        (
            "pp-local-edge",
            "standard",
            (COLLABORATION_FILES,),
            COLLABORATION_FILES,
            "container_artifact_collaboration_surface_forbidden",
            (),
        ),
        (
            "pp-compute-node",
            "cpu",
            (COLLABORATION_FILES,),
            COLLABORATION_FILES,
            "container_artifact_collaboration_surface_forbidden",
            (),
        ),
    ],
)
def test_oci_evidence_cli_enforces_final_rootfs_collaboration_surface_with_whiteouts(
    tmp_path: Path,
    role: str,
    variant: str,
    rootfs_layers: tuple[tuple[str, ...], ...],
    sbom_files: tuple[str, ...],
    expected_error: str | None,
    expected_inventory: tuple[str, ...],
):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for(role, "linux/amd64", variant)
    layout = tmp_path / f"{artifact.artifact_id}.oci.tar"
    output = tmp_path / f"{artifact.artifact_id}.receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        rootfs_layers=rootfs_layers,
        sbom_files=sbom_files,
    )

    result = subprocess.run(
        _command(layout, output, role=role, variant=variant),
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    if expected_error is not None:
        assert result.returncode != 0
        assert expected_error in result.stderr
        assert not output.exists()
        return

    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["collaboration_surface_digest"] == artifact.collaboration_surface_digest
    assert receipt["application_inventory_digest"] == _digest(
        _canonical(sorted(expected_inventory))
    )


def test_oci_evidence_cli_rejects_an_layout_missing_the_sbom_attestation(tmp_path: Path):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-local-edge", "linux/amd64")
    layout = tmp_path / "missing-sbom.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout, labels=plan.expected_oci_labels(artifact.artifact_id), include_sbom=False
    )

    result = subprocess.run(
        _command(layout, output), cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode != 0
    assert "container_artifact_sbom_attestation_missing" in result.stderr
    assert not output.exists()


def test_oci_evidence_cli_rejects_an_empty_sbom_predicate(tmp_path: Path):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-local-edge", "linux/amd64")
    layout = tmp_path / "empty-sbom-predicate.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        sbom_predicate={},
    )

    result = subprocess.run(
        _command(layout, output), cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode != 0
    assert "container_artifact_sbom_predicate_invalid" in result.stderr
    assert not output.exists()


def test_oci_evidence_cli_accepts_current_in_toto_v1_sbom_statement(tmp_path: Path):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-local-edge", "linux/amd64")
    layout = tmp_path / "in-toto-v1.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        statement_type="https://in-toto.io/Statement/v1",
    )

    result = subprocess.run(
        _command(layout, output), cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert output.exists()


def test_oci_evidence_cli_rejects_sbom_that_omits_a_server_collaboration_file(
    tmp_path: Path,
):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-server-backend", "linux/amd64")
    layout = tmp_path / "server-sbom-mismatch.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        rootfs_layers=(COLLABORATION_FILES,),
        sbom_files=COLLABORATION_FILES[:-1],
    )

    result = subprocess.run(
        _command(layout, output, role="pp-server-backend"),
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "container_artifact_role_package_receipt_missing" in result.stderr
    assert not output.exists()


def test_oci_evidence_cli_rejects_package_level_server_sbom_without_file_entries(
    tmp_path: Path,
):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-server-backend", "linux/amd64")
    layout = tmp_path / "server-package-level-sbom.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        rootfs_layers=(COLLABORATION_FILES,),
        sbom_files=(),
    )

    result = subprocess.run(
        _command(layout, output, role="pp-server-backend"),
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "container_artifact_role_package_receipt_missing" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("rootfs_layers", "sbom_files", "expected_error"),
    [
        (
            (COLLABORATION_FILES + ("app/plastic_promise/core/embedder.py",),),
            COLLABORATION_FILES,
            "container_artifact_server_compute_source_present_rootfs",
        ),
        (
            (COLLABORATION_FILES,),
            COLLABORATION_FILES
            + ("usr/lib/python3/site-packages/plastic_promise/core/provider_http.py",),
            "container_artifact_role_package_receipt_missing",
        ),
    ],
)
def test_oci_evidence_cli_rejects_server_compute_provider_inventory(
    tmp_path: Path,
    rootfs_layers: tuple[tuple[str, ...], ...],
    sbom_files: tuple[str, ...],
    expected_error: str,
):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-server-backend", "linux/amd64")
    layout = tmp_path / f"server-provider-{expected_error}.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        rootfs_layers=rootfs_layers,
        sbom_files=sbom_files,
    )

    result = subprocess.run(
        _command(layout, output, role="pp-server-backend"),
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not output.exists()


def test_oci_evidence_cli_rejects_an_attestation_with_a_different_subject(tmp_path: Path):
    plan = ContainerArtifactCompiler().prepare(_request())
    artifact = plan.artifact_for("pp-local-edge", "linux/amd64")
    layout = tmp_path / "wrong-subject.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels(artifact.artifact_id),
        include_sbom=True,
        provenance_subject_digest="sha256:" + ("c" * 64),
    )

    result = subprocess.run(
        _command(layout, output), cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode != 0
    assert "container_artifact_attestation_subject_mismatch" in result.stderr
    assert not output.exists()


def test_oci_evidence_cli_rejects_an_attestation_without_statement_subjects(
    tmp_path: Path,
):
    plan = ContainerArtifactCompiler().prepare(_request())
    layout = tmp_path / "empty-subjects.oci.tar"
    output = tmp_path / "receipt.json"
    _write_oci_layout(
        layout,
        labels=plan.expected_oci_labels("local-edge-linux-amd64-standard"),
        include_sbom=True,
        empty_statement_subjects=True,
    )

    result = subprocess.run(
        _command(layout, output), cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode != 0
    assert "container_artifact_attestation_subject_mismatch" in result.stderr
    assert not output.exists()
