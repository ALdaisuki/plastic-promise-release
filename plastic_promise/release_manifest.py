"""Pure contracts for attestable Plastic Promise release manifests.

The module intentionally has no network, Docker, GitHub API, or publishing
dependency.  CI and release-repository automation supply their evidence, while
this module verifies that the resulting manifest binds one release version to
the exact source commit, Python artifacts, SBOM, and immutable OCI digests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from email.parser import BytesParser
from email.policy import default
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


RELEASE_MANIFEST_SCHEMA_VERSION = "plastic-promise-release-manifest/v1"
_RELEASE_VERSION = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-rc\.(?P<rc>0|[1-9]\d*))?$"
)
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUIRED_IMAGE_PLATFORMS = {
    "server": ("linux/amd64", "linux/arm64"),
    "inference-node": ("linux/amd64",),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SERVER_IMAGE_REFERENCE = re.compile(
    r"^ghcr\.io/aldaisuki/plastic-promise-server@sha256:[0-9a-f]{64}$"
)
SERVER_DEPLOYMENT_RECEIPT_SCHEMA_VERSION = "plastic-promise-server-deployment-receipt/v1"
_REQUIRED_SERVER_DEPLOYMENT_CHECKS = frozenset(
    {
        "image_pulled_by_digest",
        "image_revision_verified",
        "manifest_digest_verified",
        "memory_recall",
        "context_supply",
        "mcp_health",
        "mcp_initialize",
        "mcp_tools_list",
    }
)
_REQUIRED_SERVER_SMOKE_CHECKS = frozenset(
    {
        "health",
        "initialize",
        "tools_list",
        "session-init",
        "memory_recall",
        "context_supply",
    }
)


class ReleaseManifestError(ValueError):
    """Raised when release evidence is incomplete, ambiguous, or malformed."""


def package_version_for_release(release_version: str) -> str:
    """Map a SemVer release tag to the exact PEP 440 distribution version."""

    match = _RELEASE_VERSION.fullmatch(release_version)
    if match is None:
        raise ReleaseManifestError("release_manifest_version_invalid")
    base = ".".join(match.group(name) for name in ("major", "minor", "patch"))
    rc = match.group("rc")
    return base if rc is None else f"{base}rc{rc}"


def release_channel(release_version: str) -> str:
    """Return the immutable channel implied by a validated SemVer tag."""

    package_version_for_release(release_version)
    return "rc" if "-rc." in release_version else "stable"


def sha256_file(path: Path) -> str:
    """Hash one regular file without following an artifact symlink."""

    if path.is_symlink() or not path.is_file():
        raise ReleaseManifestError("release_manifest_artifact_not_regular_file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_metadata_from_wheel(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ReleaseManifestError("release_manifest_wheel_metadata_invalid")
            metadata = BytesParser(policy=default).parsebytes(archive.read(names[0]))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseManifestError("release_manifest_wheel_invalid") from exc
    return str(metadata.get("Name", "")), str(metadata.get("Version", ""))


def _package_metadata_from_sdist(path: Path) -> tuple[str, str]:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            # Setuptools sdists commonly carry both the release-root PKG-INFO
            # and a package *.egg-info/PKG-INFO.  Only the former is the
            # sdist's distribution metadata; treating both as an ambiguity
            # makes a valid release artifact fail after it has been built.
            members = [
                member
                for member in archive.getmembers()
                if member.isfile()
                and member.name.endswith("/PKG-INFO")
                and member.name.count("/") == 1
            ]
            if len(members) != 1:
                raise ReleaseManifestError("release_manifest_sdist_metadata_invalid")
            member = archive.extractfile(members[0])
            if member is None:
                raise ReleaseManifestError("release_manifest_sdist_metadata_invalid")
            metadata = BytesParser(policy=default).parsebytes(member.read())
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseManifestError("release_manifest_sdist_invalid") from exc
    return str(metadata.get("Name", "")), str(metadata.get("Version", ""))


def python_artifact_records(dist_directory: Path, package_version: str) -> list[dict[str, object]]:
    """Return exact wheel/sdist records after inspecting their embedded metadata."""

    if dist_directory.is_symlink() or not dist_directory.is_dir():
        raise ReleaseManifestError("release_manifest_dist_directory_invalid")
    entries = sorted(dist_directory.iterdir(), key=lambda path: path.name)
    wheels = [path for path in entries if path.name.endswith(".whl")]
    sdists = [path for path in entries if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseManifestError("release_manifest_python_artifact_set_invalid")

    expected = ("plastic-promise", package_version)
    if _package_metadata_from_wheel(wheels[0]) != expected:
        raise ReleaseManifestError("release_manifest_wheel_identity_mismatch")
    if _package_metadata_from_sdist(sdists[0]) != expected:
        raise ReleaseManifestError("release_manifest_sdist_identity_mismatch")

    return [
        {
            "kind": "wheel",
            "filename": wheels[0].name,
            "sha256": sha256_file(wheels[0]),
            "size_bytes": wheels[0].stat().st_size,
        },
        {
            "kind": "sdist",
            "filename": sdists[0].name,
            "sha256": sha256_file(sdists[0]),
            "size_bytes": sdists[0].stat().st_size,
        },
    ]


def _image_record(name: str, reference: str) -> dict[str, object]:
    if name not in _REQUIRED_IMAGE_PLATFORMS:
        raise ReleaseManifestError("release_manifest_image_name_invalid")
    if "@" not in reference:
        raise ReleaseManifestError("release_manifest_image_reference_not_immutable")
    repository, digest = reference.rsplit("@", maxsplit=1)
    if not repository or not _OCI_DIGEST.fullmatch(digest):
        raise ReleaseManifestError("release_manifest_image_reference_invalid")
    return {
        "name": name,
        "reference": reference,
        "digest": digest,
        "platforms": list(_REQUIRED_IMAGE_PLATFORMS[name]),
    }


def validate_server_deployment_receipt(
    payload: Mapping[str, object],
    *,
    release_version: str,
    source_commit: str,
    release_manifest_sha256: str,
) -> None:
    """Validate the bounded receipt required before stable package publication.

    The receipt deliberately contains only immutable references and boolean
    checks.  It never carries a server hostname, endpoint, secret, database
    body, or free-form operator note.  This permits the stable-only release
    repository to prove service acceptance without turning its history into an
    operational inventory.
    """

    if release_channel(release_version) != "stable":
        raise ReleaseManifestError("server_deployment_receipt_stable_release_required")
    if not _COMMIT_SHA.fullmatch(source_commit):
        raise ReleaseManifestError("server_deployment_receipt_source_commit_invalid")
    if not _SHA256.fullmatch(release_manifest_sha256):
        raise ReleaseManifestError("server_deployment_receipt_manifest_sha256_invalid")

    expected_fields = {
        "schema_version",
        "release_version",
        "source_commit",
        "release_manifest_sha256",
        "server_image",
        "checks",
    }
    if set(payload) != expected_fields:
        raise ReleaseManifestError("server_deployment_receipt_fields_invalid")
    if payload["schema_version"] != SERVER_DEPLOYMENT_RECEIPT_SCHEMA_VERSION:
        raise ReleaseManifestError("server_deployment_receipt_schema_invalid")
    if payload["release_version"] != release_version:
        raise ReleaseManifestError("server_deployment_receipt_version_mismatch")
    if payload["source_commit"] != source_commit:
        raise ReleaseManifestError("server_deployment_receipt_source_mismatch")
    if payload["release_manifest_sha256"] != release_manifest_sha256:
        raise ReleaseManifestError("server_deployment_receipt_manifest_mismatch")

    server_image = payload["server_image"]
    if not isinstance(server_image, str) or not _SERVER_IMAGE_REFERENCE.fullmatch(server_image):
        raise ReleaseManifestError("server_deployment_receipt_server_image_invalid")

    checks = payload["checks"]
    if not isinstance(checks, Mapping) or set(checks) != _REQUIRED_SERVER_DEPLOYMENT_CHECKS:
        raise ReleaseManifestError("server_deployment_receipt_checks_invalid")
    if any(value is not True for value in checks.values()):
        raise ReleaseManifestError("server_deployment_receipt_check_failed")


def build_server_deployment_receipt(
    *,
    release_manifest: Mapping[str, Any],
    container_image: str,
    image_revision: str,
    smoke_report: Mapping[str, Any],
) -> dict[str, object]:
    """Build a no-secret receipt from exact container and MCP smoke evidence."""

    validate_release_manifest(release_manifest)
    release = release_manifest["release"]
    source = release_manifest["source"]
    images = release_manifest["images"]
    if not isinstance(release, Mapping) or release.get("channel") != "stable":
        raise ReleaseManifestError("server_deployment_receipt_stable_release_required")
    if not isinstance(source, Mapping) or not isinstance(source.get("commit"), str):
        raise ReleaseManifestError("server_deployment_receipt_source_commit_invalid")
    server_images = [
        image for image in images if isinstance(image, Mapping) and image.get("name") == "server"
    ]
    if len(server_images) != 1 or not isinstance(server_images[0].get("reference"), str):
        raise ReleaseManifestError("server_deployment_receipt_server_image_invalid")
    expected_image = str(server_images[0]["reference"])
    if container_image != expected_image:
        raise ReleaseManifestError("server_deployment_receipt_image_mismatch")
    if image_revision != source["commit"]:
        raise ReleaseManifestError("server_deployment_receipt_revision_mismatch")

    checks = smoke_report.get("checks")
    if smoke_report.get("ok") is not True or not isinstance(checks, Mapping):
        raise ReleaseManifestError("server_deployment_receipt_smoke_invalid")
    if not set(checks) >= _REQUIRED_SERVER_SMOKE_CHECKS:
        raise ReleaseManifestError("server_deployment_receipt_smoke_checks_missing")

    payload: dict[str, object] = {
        "schema_version": SERVER_DEPLOYMENT_RECEIPT_SCHEMA_VERSION,
        "release_version": release["semver"],
        "source_commit": source["commit"],
        "release_manifest_sha256": release_manifest_sha256(release_manifest),
        "server_image": expected_image,
        "checks": {
            "image_pulled_by_digest": True,
            "image_revision_verified": True,
            "manifest_digest_verified": True,
            "mcp_health": True,
            "mcp_initialize": True,
            "mcp_tools_list": True,
            "memory_recall": True,
            "context_supply": True,
        },
    }
    validate_server_deployment_receipt(
        payload,
        release_version=str(release["semver"]),
        source_commit=str(source["commit"]),
        release_manifest_sha256=str(payload["release_manifest_sha256"]),
    )
    return payload


def build_release_manifest(
    *,
    release_version: str,
    source_repository: str,
    source_commit: str,
    dist_directory: Path,
    sbom_path: Path,
    image_references: Mapping[str, str],
    workflow_ref: str,
) -> dict[str, object]:
    """Build a deterministic, non-secret release manifest from verified evidence."""

    package_version = package_version_for_release(release_version)
    if not source_repository.startswith("https://github.com/"):
        raise ReleaseManifestError("release_manifest_source_repository_invalid")
    if not _COMMIT_SHA.fullmatch(source_commit):
        raise ReleaseManifestError("release_manifest_source_commit_invalid")
    if not workflow_ref.startswith("https://github.com/"):
        raise ReleaseManifestError("release_manifest_workflow_ref_invalid")
    if set(image_references) != set(_REQUIRED_IMAGE_PLATFORMS):
        raise ReleaseManifestError("release_manifest_image_set_invalid")
    if sbom_path.suffix != ".json":
        raise ReleaseManifestError("release_manifest_sbom_format_invalid")

    return {
        "schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
        "release": {
            "semver": release_version,
            "channel": release_channel(release_version),
            "package_version": package_version,
        },
        "source": {"repository": source_repository, "commit": source_commit},
        "artifacts": python_artifact_records(dist_directory, package_version),
        "images": [
            _image_record(name, image_references[name]) for name in sorted(image_references)
        ],
        "sbom": {
            "format": "cyclonedx-json",
            "filename": sbom_path.name,
            "sha256": sha256_file(sbom_path),
            "size_bytes": sbom_path.stat().st_size,
        },
        "provenance": {
            "workflow_ref": workflow_ref,
            "artifact_attestation_required": True,
        },
    }


def validate_release_manifest(payload: Mapping[str, Any]) -> None:
    """Fail closed unless a manifest binds every stable-release requirement."""

    expected_fields = {
        "schema_version",
        "release",
        "source",
        "artifacts",
        "images",
        "sbom",
        "provenance",
    }
    if set(payload) != expected_fields:
        raise ReleaseManifestError("release_manifest_fields_invalid")
    if payload["schema_version"] != RELEASE_MANIFEST_SCHEMA_VERSION:
        raise ReleaseManifestError("release_manifest_schema_invalid")

    release = payload["release"]
    source = payload["source"]
    artifacts = payload["artifacts"]
    images = payload["images"]
    sbom = payload["sbom"]
    provenance = payload["provenance"]
    if not all(isinstance(item, Mapping) for item in (release, source, sbom, provenance)):
        raise ReleaseManifestError("release_manifest_section_invalid")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        raise ReleaseManifestError("release_manifest_artifacts_invalid")
    if not isinstance(images, Sequence) or isinstance(images, (str, bytes)):
        raise ReleaseManifestError("release_manifest_images_invalid")

    semver = release.get("semver")
    if not isinstance(semver, str) or release.get("package_version") != package_version_for_release(
        semver
    ):
        raise ReleaseManifestError("release_manifest_release_identity_invalid")
    if release.get("channel") != release_channel(semver):
        raise ReleaseManifestError("release_manifest_channel_invalid")
    if not isinstance(source.get("repository"), str) or not source["repository"].startswith(
        "https://github.com/"
    ):
        raise ReleaseManifestError("release_manifest_source_repository_invalid")
    if not isinstance(source.get("commit"), str) or not _COMMIT_SHA.fullmatch(source["commit"]):
        raise ReleaseManifestError("release_manifest_source_commit_invalid")

    artifact_kinds = {item.get("kind") for item in artifacts if isinstance(item, Mapping)}
    if artifact_kinds != {"wheel", "sdist"} or len(artifacts) != 2:
        raise ReleaseManifestError("release_manifest_artifact_set_invalid")
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or not isinstance(artifact.get("filename"), str):
            raise ReleaseManifestError("release_manifest_artifact_invalid")
        if not isinstance(artifact.get("sha256"), str) or not _SHA256.fullmatch(artifact["sha256"]):
            raise ReleaseManifestError("release_manifest_artifact_hash_invalid")
        if not isinstance(artifact.get("size_bytes"), int) or artifact["size_bytes"] <= 0:
            raise ReleaseManifestError("release_manifest_artifact_size_invalid")

    images_by_name = {
        item.get("name"): item
        for item in images
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    if set(images_by_name) != set(_REQUIRED_IMAGE_PLATFORMS) or len(images) != 2:
        raise ReleaseManifestError("release_manifest_image_set_invalid")
    for name, platforms in _REQUIRED_IMAGE_PLATFORMS.items():
        image = images_by_name[name]
        reference = image.get("reference")
        digest = image.get("digest")
        if (
            not isinstance(reference, str)
            or not isinstance(digest, str)
            or not reference.endswith(f"@{digest}")
        ):
            raise ReleaseManifestError("release_manifest_image_reference_invalid")
        if not _OCI_DIGEST.fullmatch(digest) or image.get("platforms") != list(platforms):
            raise ReleaseManifestError("release_manifest_image_contract_invalid")

    if sbom.get("format") != "cyclonedx-json" or not isinstance(sbom.get("filename"), str):
        raise ReleaseManifestError("release_manifest_sbom_format_invalid")
    if not isinstance(sbom.get("sha256"), str) or not _SHA256.fullmatch(sbom["sha256"]):
        raise ReleaseManifestError("release_manifest_sbom_hash_invalid")
    if not isinstance(sbom.get("size_bytes"), int) or sbom["size_bytes"] <= 0:
        raise ReleaseManifestError("release_manifest_sbom_size_invalid")
    if provenance.get("artifact_attestation_required") is not True:
        raise ReleaseManifestError("release_manifest_provenance_required")
    if not isinstance(provenance.get("workflow_ref"), str) or not provenance[
        "workflow_ref"
    ].startswith("https://github.com/"):
        raise ReleaseManifestError("release_manifest_workflow_ref_invalid")


def canonical_release_manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the sole serialization used for manifest identity and storage."""

    validate_release_manifest(payload)
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def release_manifest_sha256(payload: Mapping[str, Any]) -> str:
    """Hash validated manifest semantics, not a transport-specific JSON layout."""

    return hashlib.sha256(canonical_release_manifest_bytes(payload)).hexdigest()


def write_server_deployment_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    """Write one validated receipt atomically without replacing existing proof."""

    release_version = payload.get("release_version")
    source_commit = payload.get("source_commit")
    manifest_sha256 = payload.get("release_manifest_sha256")
    if not all(
        isinstance(value, str) for value in (release_version, source_commit, manifest_sha256)
    ):
        raise ReleaseManifestError("server_deployment_receipt_identity_invalid")
    validate_server_deployment_receipt(
        payload,
        release_version=release_version,
        source_commit=source_commit,
        release_manifest_sha256=manifest_sha256,
    )
    if path.exists() or path.is_symlink():
        raise ReleaseManifestError("server_deployment_receipt_output_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=False
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary_path)
        raise


def write_release_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a validated manifest once; existing evidence is never overwritten."""

    encoded = canonical_release_manifest_bytes(payload)
    if path.exists() or path.is_symlink():
        raise ReleaseManifestError("release_manifest_output_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=False
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise ReleaseManifestError("release_manifest_output_exists") from exc
        finally:
            os.unlink(temporary_path)
    except OSError as exc:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise ReleaseManifestError("release_manifest_output_write_failed") from exc


__all__ = [
    "RELEASE_MANIFEST_SCHEMA_VERSION",
    "ReleaseManifestError",
    "build_release_manifest",
    "package_version_for_release",
    "python_artifact_records",
    "release_channel",
    "sha256_file",
    "validate_release_manifest",
    "write_release_manifest",
]
