"""Focused fail-closed tests for the RC release-bundle command seam."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from plastic_promise.deployment import ModelCatalog, canonical_model_catalog_bytes


def _load_script():
    path = Path("scripts/create_release_bundle.py")
    spec = importlib.util.spec_from_file_location("create_release_bundle_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _catalog_payload() -> dict[str, object]:
    return {
        "schema_version": "plastic-promise-model-catalog/v1",
        "catalog_id": "rc-models-v1",
        "profile": "split-accelerated",
        "runtime": "remote-inference",
        "resource_estimate": {
            "model_cache_bytes": 5 * 1024**3,
            "minimum_system_memory_bytes": 8 * 1024**3,
            "minimum_gpu_memory_bytes": 0,
        },
        "capabilities": [
            {"kind": "embedding", "contract_version": "embedding/v1"},
            {"kind": "rerank", "contract_version": "rerank/v1"},
        ],
        "embedding": {
            "model": "vendor/embedding-v1",
            "revision": "a" * 40,
            "dimension": 1024,
            "normalization": "l2",
            "metric": "cosine",
            "tokenization": "wordpiece",
            "pooling": "mean",
            "artifact_sha256": "sha256:" + ("b" * 64),
            "golden_vector_sha256": "sha256:" + ("c" * 64),
        },
        "rerank": {
            "model": "vendor/rerank-v1",
            "revision": "d" * 40,
            "artifact_sha256": "sha256:" + ("e" * 64),
            "scoring_schema": "rerank-score/v1",
        },
    }


def _write_tar_member(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _oci_digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _write_single_image_oci_layout(path: Path, *, tamper: str | None = None) -> None:
    layer = b"synthetic-oci-layer"
    layer_descriptor = {
        "digest": _oci_digest(layer),
        "mediaType": "application/vnd.oci.image.layer.v1.tar",
        "size": len(layer),
    }
    config = _canonical_json(
        {
            "architecture": "amd64",
            "config": {"Labels": {"org.example.test": "valid"}},
            "os": "linux",
        }
    )
    config_descriptor = {
        "digest": _oci_digest(config),
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "size": len(config),
    }
    manifest = _canonical_json(
        {
            "config": config_descriptor,
            "layers": [layer_descriptor],
            "schemaVersion": 2,
        }
    )
    manifest_descriptor = {
        "digest": _oci_digest(manifest),
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "platform": {"architecture": "amd64", "os": "linux"},
        "size": len(manifest),
    }
    index = _canonical_json({"manifests": [manifest_descriptor], "schemaVersion": 2})
    written_manifest = b"x" * len(manifest) if tamper == "manifest" else manifest
    written_config = b"x" * len(config) if tamper == "config" else config
    written_layer = b"x" * len(layer) if tamper == "layer" else layer

    with tarfile.open(path, mode="w") as archive:
        _write_tar_member(archive, "index.json", index)
        _write_tar_member(
            archive,
            f"blobs/sha256/{manifest_descriptor['digest'].removeprefix('sha256:')}",
            written_manifest,
        )
        _write_tar_member(
            archive,
            f"blobs/sha256/{config_descriptor['digest'].removeprefix('sha256:')}",
            written_config,
        )
        _write_tar_member(
            archive,
            f"blobs/sha256/{layer_descriptor['digest'].removeprefix('sha256:')}",
            written_layer,
        )


def _write_multi_platform_oci_layout(path: Path) -> str:
    manifests: list[dict[str, object]] = []
    blobs: list[tuple[str, bytes]] = []
    for architecture in ("amd64", "arm64"):
        layer = f"synthetic-{architecture}-layer".encode("ascii")
        layer_descriptor = {
            "digest": _oci_digest(layer),
            "mediaType": "application/vnd.oci.image.layer.v1.tar",
            "size": len(layer),
        }
        config = _canonical_json(
            {
                "architecture": architecture,
                "config": {"Labels": {"org.example.platform": architecture}},
                "os": "linux",
            }
        )
        config_descriptor = {
            "digest": _oci_digest(config),
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": len(config),
        }
        manifest = _canonical_json(
            {
                "config": config_descriptor,
                "layers": [layer_descriptor],
                "schemaVersion": 2,
            }
        )
        manifest_descriptor = {
            "digest": _oci_digest(manifest),
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "platform": {"architecture": architecture, "os": "linux"},
            "size": len(manifest),
        }
        manifests.append(manifest_descriptor)
        blobs.extend(
            (
                (
                    f"blobs/sha256/{manifest_descriptor['digest'].removeprefix('sha256:')}",
                    manifest,
                ),
                (
                    f"blobs/sha256/{config_descriptor['digest'].removeprefix('sha256:')}",
                    config,
                ),
                (
                    f"blobs/sha256/{layer_descriptor['digest'].removeprefix('sha256:')}",
                    layer,
                ),
            )
        )
    index = _canonical_json({"manifests": manifests, "schemaVersion": 2})
    with tarfile.open(path, mode="w") as archive:
        _write_tar_member(archive, "index.json", index)
        for name, payload in blobs:
            _write_tar_member(archive, name, payload)
    return _oci_digest(index)


def test_model_catalog_parser_rejects_missing_and_unsupported_source_catalog(tmp_path: Path):
    script = _load_script()

    with pytest.raises(script.ReleaseBundleCreateError, match="model_catalog_missing"):
        script._load_supported_catalog(tmp_path, "release/model-catalog.json")

    catalog_path = tmp_path / "release" / "model-catalog.json"
    catalog_path.parent.mkdir()
    payload = _catalog_payload()
    payload["profile"] = "local-cloud"
    payload["runtime"] = "cloud-inference"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(script.ReleaseBundleCreateError, match="profile_matrix_unsupported"):
        script._load_supported_catalog(tmp_path, "release/model-catalog.json")


def test_oci_bundle_parser_rejects_an_incomplete_layout(tmp_path: Path):
    script = _load_script()
    layout = tmp_path / "incomplete.oci.tar"
    with tarfile.open(layout, mode="w") as archive:
        _write_tar_member(archive, "index.json", b'{"schemaVersion":2,"manifests":[]}')

    with pytest.raises(script.ReleaseBundleCreateError, match="oci_layout_root_invalid"):
        script._read_oci_layout(layout)


@pytest.mark.parametrize("tamper", ("manifest", "config", "layer"))
def test_oci_bundle_parser_rejects_descriptor_blob_digest_tampering(tmp_path: Path, tamper: str):
    script = _load_script()
    layout = tmp_path / f"tampered-{tamper}.oci.tar"
    _write_single_image_oci_layout(layout, tamper=tamper)

    with pytest.raises(script.ReleaseBundleCreateError, match="oci_layout_digest_mismatch"):
        script._read_oci_layout(layout)


def test_oci_bundle_parser_accepts_a_descriptor_verified_layout(tmp_path: Path):
    script = _load_script()
    layout_path = tmp_path / "valid.oci.tar"
    _write_single_image_oci_layout(layout_path)

    layout = script._read_oci_layout(layout_path)

    assert set(layout.images_by_platform) == {"linux/amd64"}
    assert layout.images_by_platform["linux/amd64"].labels == {"org.example.test": "valid"}


def test_oci_bundle_parser_accepts_multi_platform_root_index(tmp_path: Path):
    script = _load_script()
    layout_path = tmp_path / "multi-platform.oci.tar"
    expected_root_digest = _write_multi_platform_oci_layout(layout_path)

    layout = script._read_oci_layout(layout_path)

    assert layout.root_digest == expected_root_digest
    assert {
        platform: image.labels["org.example.platform"]
        for platform, image in layout.images_by_platform.items()
    } == {"linux/amd64": "amd64", "linux/arm64": "arm64"}


def test_external_evidence_parser_rejects_unverified_or_incomplete_evidence(tmp_path: Path):
    script = _load_script()
    evidence = tmp_path / "verified-evidence.json"
    evidence.write_text(
        json.dumps(
            [
                {
                    "schema_version": "plastic-promise-release-evidence/v1",
                    "subject": "release-manifest",
                    "subject_digest": "sha256:" + ("a" * 64),
                    "attestation_digest": "sha256:" + ("b" * 64),
                    "predicate_type": "slsa-provenance/v1",
                    "verifier": "github-attestation-verify",
                    "verified": False,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(script.ReleaseBundleCreateError, match="verified_evidence_invalid"):
        script._load_verified_evidence(evidence)


def test_artifact_sbom_receipts_bind_each_cyclonedx_file_to_inspected_oci_identity(
    tmp_path: Path,
):
    script = _load_script()
    edge = tmp_path / "edge.json"
    compute = tmp_path / "compute.json"
    for path in (edge, compute):
        path.write_text(
            json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6"}),
            encoding="utf-8",
        )
    plan = SimpleNamespace(
        artifacts=(
            SimpleNamespace(
                artifact_id="local-edge-linux-amd64-standard",
                role="pp-local-edge",
                platform="linux/amd64",
                variant="standard",
            ),
            SimpleNamespace(
                artifact_id="compute-node-linux-amd64-cpu",
                role="pp-compute-node",
                platform="linux/amd64",
                variant="cpu",
            ),
        )
    )
    layouts = {
        ("pp-local-edge", "standard"): script.OciLayout(
            root_digest="sha256:" + ("a" * 64),
            images_by_platform={
                "linux/amd64": script.OciImage(digest="sha256:" + ("b" * 64), labels={})
            },
        ),
        ("pp-compute-node", "cpu"): script.OciLayout(
            root_digest="sha256:" + ("c" * 64),
            images_by_platform={
                "linux/amd64": script.OciImage(digest="sha256:" + ("d" * 64), labels={})
            },
        ),
    }

    receipts = script._derive_artifact_sbom_receipts(
        [
            f"local-edge-linux-amd64-standard={edge}",
            f"compute-node-linux-amd64-cpu={compute}",
        ],
        plan=plan,
        layouts=layouts,
    )

    by_id = receipts.by_artifact_id
    assert set(by_id) == {"local-edge-linux-amd64-standard", "compute-node-linux-amd64-cpu"}
    assert by_id["local-edge-linux-amd64-standard"].image_digest == "sha256:" + ("b" * 64)
    assert by_id["compute-node-linux-amd64-cpu"].oci_layout_digest == "sha256:" + ("c" * 64)
    assert all(item.scanner == "syft-oci-archive" for item in receipts.receipts)

    receipt_path = tmp_path / "artifact-sbom-receipts.json"
    receipt_path.write_bytes(script._canonical_json(receipts.canonical_payload()))
    assert script._load_artifact_sbom_receipts(receipt_path) == receipts

    with pytest.raises(script.ReleaseBundleCreateError, match="artifact_sbom_matrix_incomplete"):
        script._derive_artifact_sbom_receipts(
            [f"local-edge-linux-amd64-standard={edge}"],
            plan=plan,
            layouts=layouts,
        )
    edge.write_text(json.dumps({"bomFormat": "SPDX"}), encoding="utf-8")
    with pytest.raises(script.ReleaseBundleCreateError, match="artifact_sbom_invalid"):
        script._derive_artifact_sbom_receipts(
            [
                f"local-edge-linux-amd64-standard={edge}",
                f"compute-node-linux-amd64-cpu={compute}",
            ],
            plan=plan,
            layouts=layouts,
        )


def test_artifact_sbom_receipt_parser_rejects_noncanonical_or_tampered_identity(tmp_path: Path):
    script = _load_script()
    receipt_path = tmp_path / "artifact-sbom-receipts.json"
    valid = {
        "schema_version": "plastic-promise-artifact-sbom-receipts/v1",
        "receipts": [
            {
                "artifact_id": "local-edge-linux-amd64-standard",
                "role": "pp-local-edge",
                "platform": "linux/amd64",
                "variant": "standard",
                "oci_layout_digest": "sha256:" + ("a" * 64),
                "image_digest": "sha256:" + ("b" * 64),
                "sbom_digest": "sha256:" + ("c" * 64),
                "sbom_size_bytes": 123,
                "sbom_format": "CycloneDX",
                "sbom_spec_version": "1.6",
                "scanner": "syft-oci-archive",
            }
        ],
    }
    receipt_path.write_bytes(script._canonical_json(valid))
    assert script._load_artifact_sbom_receipts(receipt_path).digest.startswith("sha256:")

    receipt_path.write_text(json.dumps(valid, indent=2), encoding="utf-8")
    with pytest.raises(
        script.ReleaseBundleCreateError, match="artifact_sbom_receipts_noncanonical"
    ):
        script._load_artifact_sbom_receipts(receipt_path)

    valid["receipts"][0]["image_digest"] = "sha256:" + ("z" * 64)
    receipt_path.write_bytes(script._canonical_json(valid))
    with pytest.raises(script.ReleaseBundleCreateError, match="artifact_sbom_receipts_invalid"):
        script._load_artifact_sbom_receipts(receipt_path)


def test_release_manifest_must_bind_the_exact_package_sbom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    script = _load_script()
    source_revision = "a" * 40
    edge_digest = "sha256:" + ("a" * 64)
    server_digest = "sha256:" + ("b" * 64)
    cpu_digest = "sha256:" + ("d" * 64)
    cuda_digest = "sha256:" + ("c" * 64)
    package_sbom = tmp_path / "sbom.cdx.json"
    package_sbom.write_text('{"bomFormat":"CycloneDX","specVersion":"1.6"}', encoding="utf-8")
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "release": {"semver": "v1.2.3-rc.1"},
                "source": {"commit": source_revision},
                "images": [
                    {
                        "name": "local-edge",
                        "digest": edge_digest,
                        "reference": f"oci@{edge_digest}",
                    },
                    {
                        "name": "server",
                        "digest": server_digest,
                        "reference": f"oci@{server_digest}",
                    },
                    {
                        "name": "inference-cpu",
                        "digest": cpu_digest,
                        "reference": f"oci@{cpu_digest}",
                    },
                    {
                        "name": "inference-node",
                        "digest": cuda_digest,
                        "reference": f"oci@{cuda_digest}",
                    },
                ],
                "sbom": {
                    "filename": package_sbom.name,
                    "sha256": script._file_digest(package_sbom).removeprefix("sha256:"),
                    "size_bytes": package_sbom.stat().st_size,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(script, "validate_release_manifest", lambda _payload: None)
    edge_layout = script.OciLayout(root_digest=edge_digest, images_by_platform={})
    server_layout = script.OciLayout(root_digest=server_digest, images_by_platform={})
    cpu_layout = script.OciLayout(root_digest=cpu_digest, images_by_platform={})
    cuda_layout = script.OciLayout(root_digest=cuda_digest, images_by_platform={})

    script._validate_release_manifest(
        manifest_path,
        release_version="v1.2.3-rc.1",
        source_revision=source_revision,
        package_sbom_path=package_sbom,
        edge_layout=edge_layout,
        server_layout=server_layout,
        cpu_layout=cpu_layout,
        cuda_layout=cuda_layout,
    )

    package_sbom.write_text('{"bomFormat":"CycloneDX","specVersion":"1.5"}', encoding="utf-8")
    with pytest.raises(script.ReleaseBundleCreateError, match="package_sbom_mismatch"):
        script._validate_release_manifest(
            manifest_path,
            release_version="v1.2.3-rc.1",
            source_revision=source_revision,
            package_sbom_path=package_sbom,
            edge_layout=edge_layout,
            server_layout=server_layout,
            cpu_layout=cpu_layout,
            cuda_layout=cuda_layout,
        )


def test_catalog_evidence_bytes_match_the_typed_digest_and_cannot_be_replaced(tmp_path: Path):
    script = _load_script()
    catalog = ModelCatalog.from_dict(_catalog_payload())
    output = tmp_path / "model-catalog.json"
    encoded = canonical_model_catalog_bytes(catalog)

    script._write_bytes(output, encoded)
    script._write_bytes(output, encoded)

    assert script._file_digest(output) == catalog.digest
    with pytest.raises(script.ReleaseBundleCreateError, match="output_exists"):
        script._write_bytes(output, b"different-evidence")


def test_catalog_source_binding_requires_the_clean_claimed_git_checkout(tmp_path: Path):
    script = _load_script()
    repository_root = tmp_path / "source"
    catalog_path = repository_root / "release" / "model-catalog.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(json.dumps(_catalog_payload()), encoding="utf-8")

    for command in (
        ("git", "init"),
        ("git", "config", "user.email", "release-test@example.invalid"),
        ("git", "config", "user.name", "Release Test"),
        ("git", "add", "release/model-catalog.json"),
        ("git", "commit", "-m", "add catalog"),
    ):
        subprocess.run(command, cwd=repository_root, check=True, capture_output=True)
    revision = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    script._verify_catalog_source_binding(
        repository_root,
        source_revision=revision,
        catalog_path="release/model-catalog.json",
    )

    catalog_path.write_text(
        json.dumps({**_catalog_payload(), "catalog_id": "changed-v1"}), encoding="utf-8"
    )
    with pytest.raises(script.ReleaseBundleCreateError, match="source_checkout_dirty"):
        script._verify_catalog_source_binding(
            repository_root,
            source_revision=revision,
            catalog_path="release/model-catalog.json",
        )
