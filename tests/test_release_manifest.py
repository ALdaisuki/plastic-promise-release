import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from plastic_promise.release_manifest import (
    ReleaseManifestError,
    build_release_manifest,
    build_server_deployment_receipt,
    package_version_for_release,
    release_manifest_sha256,
    validate_release_manifest,
    validate_server_deployment_receipt,
    write_release_manifest,
    write_server_deployment_receipt,
)


def _write_python_artifacts(
    dist_directory: Path,
    version: str = "1.2.3",
    *,
    include_egg_info_metadata: bool = False,
) -> None:
    dist_directory.mkdir()
    metadata = (f"Metadata-Version: 2.1\nName: plastic-promise\nVersion: {version}\n\n").encode()
    with zipfile.ZipFile(
        dist_directory / f"plastic_promise-{version}-py3-none-any.whl", mode="w"
    ) as archive:
        archive.writestr(f"plastic_promise-{version}.dist-info/METADATA", metadata)
    with tarfile.open(dist_directory / f"plastic_promise-{version}.tar.gz", mode="w:gz") as archive:
        info = tarfile.TarInfo(f"plastic_promise-{version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
        if include_egg_info_metadata:
            egg_info = tarfile.TarInfo(
                f"plastic_promise-{version}/src/plastic_promise.egg-info/PKG-INFO"
            )
            egg_info.size = len(metadata)
            archive.addfile(egg_info, io.BytesIO(metadata))


def _release_inputs(tmp_path: Path) -> dict[str, object]:
    dist_directory = tmp_path / "dist"
    _write_python_artifacts(dist_directory)
    sbom_path = tmp_path / "sbom.cdx.json"
    sbom_path.write_text(
        json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5"}), encoding="utf-8"
    )
    return {
        "release_version": "v1.2.3",
        "source_repository": "https://github.com/ALdaisuki/plastic-promise",
        "source_commit": "a" * 40,
        "dist_directory": dist_directory,
        "sbom_path": sbom_path,
        "image_references": {
            "server": "ghcr.io/aldaisuki/plastic-promise-server@sha256:" + ("b" * 64),
            "inference-node": "ghcr.io/aldaisuki/plastic-promise-local-inference-node@sha256:"
            + ("c" * 64),
        },
        "workflow_ref": "https://github.com/ALdaisuki/plastic-promise/actions/runs/42",
    }


def test_release_manifest_binds_semver_source_python_artifacts_and_oci_digests(tmp_path: Path):
    payload = build_release_manifest(**_release_inputs(tmp_path))

    validate_release_manifest(payload)

    assert payload["release"] == {
        "semver": "v1.2.3",
        "channel": "stable",
        "package_version": "1.2.3",
    }
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {"wheel", "sdist"}
    image_platforms = {image["name"]: image["platforms"] for image in payload["images"]}
    assert image_platforms == {
        "server": ["linux/amd64", "linux/arm64"],
        "inference-node": ["linux/amd64"],
    }


def test_release_manifest_supports_rc_semver_and_pep440_package_mapping(tmp_path: Path):
    inputs = _release_inputs(tmp_path)
    inputs["release_version"] = "v1.2.3-rc.4"
    inputs["dist_directory"] = tmp_path / "rc-dist"
    _write_python_artifacts(inputs["dist_directory"], version="1.2.3rc4")

    payload = build_release_manifest(**inputs)

    assert package_version_for_release("v1.2.3-rc.4") == "1.2.3rc4"
    assert payload["release"]["channel"] == "rc"
    assert payload["release"]["package_version"] == "1.2.3rc4"


def test_release_manifest_prefers_release_root_sdist_metadata_over_egg_info(tmp_path: Path):
    inputs = _release_inputs(tmp_path)
    inputs["dist_directory"] = tmp_path / "dist-with-egg-info"
    _write_python_artifacts(inputs["dist_directory"], include_egg_info_metadata=True)

    payload = build_release_manifest(**inputs)

    assert {artifact["kind"] for artifact in payload["artifacts"]} == {"wheel", "sdist"}


def test_server_deployment_receipt_binds_stable_manifest_source_and_digest():
    payload = {
        "schema_version": "plastic-promise-server-deployment-receipt/v1",
        "release_version": "v1.2.3",
        "source_commit": "a" * 40,
        "release_manifest_sha256": "b" * 64,
        "server_image": "ghcr.io/aldaisuki/plastic-promise-server@sha256:" + ("c" * 64),
        "checks": {
            "image_pulled_by_digest": True,
            "image_revision_verified": True,
            "manifest_digest_verified": True,
            "memory_recall": True,
            "context_supply": True,
            "mcp_health": True,
            "mcp_initialize": True,
            "mcp_tools_list": True,
        },
    }

    validate_server_deployment_receipt(
        payload,
        release_version="v1.2.3",
        source_commit="a" * 40,
        release_manifest_sha256="b" * 64,
    )

    payload["checks"]["mcp_health"] = False
    with pytest.raises(ReleaseManifestError, match="check_failed"):
        validate_server_deployment_receipt(
            payload,
            release_version="v1.2.3",
            source_commit="a" * 40,
            release_manifest_sha256="b" * 64,
        )


def test_server_deployment_receipt_is_derived_from_a_validated_manifest_and_read_only_smoke(
    tmp_path: Path,
):
    manifest = build_release_manifest(**_release_inputs(tmp_path))
    server_image = next(
        image["reference"] for image in manifest["images"] if image["name"] == "server"
    )
    smoke_report = {
        "ok": True,
        "checks": {
            "health": {},
            "initialize": {"success": True},
            "tools_list": {},
            "session-init": {},
            "memory_recall": {},
            "context_supply": {},
        },
    }

    receipt = build_server_deployment_receipt(
        release_manifest=manifest,
        container_image=server_image,
        image_revision="a" * 40,
        smoke_report=smoke_report,
    )
    output = tmp_path / "server-deployment-receipt.json"
    write_server_deployment_receipt(output, receipt)

    assert json.loads(output.read_text(encoding="utf-8")) == receipt


def test_release_manifest_rejects_missing_or_mutable_image_evidence(tmp_path: Path):
    inputs = _release_inputs(tmp_path)
    inputs["image_references"] = {"server": "ghcr.io/aldaisuki/plastic-promise:latest"}

    with pytest.raises(ReleaseManifestError, match="image_set_invalid"):
        build_release_manifest(**inputs)


def test_release_manifest_rejects_source_commit_outside_immutable_sha_format(tmp_path: Path):
    inputs = _release_inputs(tmp_path)
    inputs["source_commit"] = "main"

    with pytest.raises(ReleaseManifestError, match="source_commit_invalid"):
        build_release_manifest(**inputs)


def test_release_manifest_output_is_validated_and_never_overwritten(tmp_path: Path):
    payload = build_release_manifest(**_release_inputs(tmp_path))
    output = tmp_path / "release-manifest.json"

    write_release_manifest(output, payload)

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    with pytest.raises(ReleaseManifestError, match="output_exists"):
        write_release_manifest(output, payload)
    assert not list(tmp_path.glob(".release-manifest.json.*"))


def test_release_manifest_hash_is_stable_across_equivalent_json_layouts(tmp_path: Path):
    payload = build_release_manifest(**_release_inputs(tmp_path))
    reordered = json.loads(json.dumps(payload, sort_keys=True))

    assert release_manifest_sha256(payload) == release_manifest_sha256(reordered)


def test_release_manifest_rejects_tampered_image_digest(tmp_path: Path):
    payload = build_release_manifest(**_release_inputs(tmp_path))
    payload["images"][0]["digest"] = "sha256:" + ("d" * 64)

    with pytest.raises(ReleaseManifestError, match="image_reference_invalid"):
        validate_release_manifest(payload)
