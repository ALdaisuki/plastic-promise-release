#!/usr/bin/env python3
"""Verify Buildx OCI-layout attestations against one prepared artifact plan.

This command is intentionally verification-only.  It reads a local OCI-layout
tar produced by Buildx, checks descriptor hashes, selects one image platform,
and requires both SBOM and provenance attestation layers to name that exact
image digest.  It never loads an image, talks to a registry, or starts Docker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

_IMAGE_INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
_IMAGE_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_IN_TOTO_STATEMENT_TYPES = frozenset(
    {
        "https://in-toto.io/Statement/v0.1",
        "https://in-toto.io/Statement/v1",
    }
)
_SPDX_PREDICATE_TYPES = frozenset(
    {
        "https://spdx.dev/Document",
        "https://spdx.dev/Document/v2.2",
        "https://spdx.dev/Document/v2.3",
    }
)

if TYPE_CHECKING:
    from plastic_promise.deployment import ArtifactCollaborationSurface


class OciEvidenceError(ValueError):
    """A stable and non-secret OCI layout validation failure."""


def _fail(code: str) -> None:
    raise OciEvidenceError(code)


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _mapping(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code)
    return value


def _sequence(value: object, code: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(code)
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=_SOURCE_ROOT)
    parser.add_argument("--profile-id", default="split-accelerated")
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--plan-platform", action="append", required=True)
    parser.add_argument("--compute-variant", action="append")
    parser.add_argument("--model-catalog-reference")
    parser.add_argument("--model-catalog-digest")
    parser.add_argument("--role", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--variant", default="standard")
    parser.add_argument("--oci-layout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _read_layout(path: Path) -> dict[str, bytes]:
    if path.is_symlink() or not path.is_file():
        _fail("container_artifact_oci_layout_missing")
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts or name in members:
                    _fail("container_artifact_oci_layout_invalid")
                extracted = archive.extractfile(member)
                if extracted is None:
                    _fail("container_artifact_oci_layout_invalid")
                members[name] = extracted.read()
    except (OSError, tarfile.TarError) as exc:
        raise OciEvidenceError("container_artifact_oci_layout_invalid") from exc
    if "index.json" not in members:
        _fail("container_artifact_oci_layout_invalid")
    return members


def _json(value: bytes, code: str) -> dict[str, Any]:
    try:
        return _mapping(json.loads(value), code)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OciEvidenceError(code) from exc


def _blob(members: dict[str, bytes], descriptor: dict[str, Any]) -> tuple[str, bytes]:
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not isinstance(digest, str) or not digest.startswith("sha256:") or not isinstance(size, int):
        _fail("container_artifact_oci_descriptor_invalid")
    value = members.get(f"blobs/sha256/{digest.removeprefix('sha256:')}")
    if value is None or len(value) != size or _digest(value) != digest:
        _fail("container_artifact_oci_descriptor_digest_mismatch")
    return digest, value


def _descriptor_payload(members: dict[str, bytes], descriptor: dict[str, Any]) -> dict[str, Any]:
    _, value = _blob(members, descriptor)
    return _json(value, "container_artifact_oci_descriptor_invalid")


def _normalise_layer_path(name: str) -> str | None:
    """Return one safe rootfs-relative POSIX path from an OCI layer member."""

    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        _fail("container_artifact_oci_layer_path_invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        _fail("container_artifact_oci_layer_path_invalid")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts:
        return None
    return "/".join(parts)


def _remove_path(rootfs_paths: set[str], target: str, *, descendants_only: bool = False) -> None:
    """Remove one path subtree from the accumulated non-directory inventory."""

    prefix = f"{target}/" if target else ""
    removals = {
        path
        for path in rootfs_paths
        if (not descendants_only and path == target) or not target or path.startswith(prefix)
    }
    rootfs_paths.difference_update(removals)


def _apply_layer(rootfs_paths: set[str], layer_payload: bytes) -> None:
    """Apply one OCI layer to a non-directory rootfs path inventory.

    Whiteouts affect lower layers before normal members from this layer are
    materialised.  Consequently a whiteout followed by a replacement in the
    same layer leaves the replacement visible, matching OCI layer semantics.
    """

    try:
        with tarfile.open(fileobj=BytesIO(layer_payload), mode="r:*") as archive:
            members = archive.getmembers()
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise OciEvidenceError("container_artifact_oci_layer_invalid") from exc

    opaque_directories: set[str] = set()
    whiteout_targets: set[str] = set()
    normal_members: list[tuple[str, bool]] = []
    for member in members:
        path = _normalise_layer_path(member.name)
        if path is None:
            continue
        parent, _, basename = path.rpartition("/")
        if basename == ".wh..wh..opq":
            opaque_directories.add(parent)
            continue
        if basename.startswith(".wh."):
            target_name = basename.removeprefix(".wh.")
            if not target_name:
                _fail("container_artifact_oci_layer_invalid")
            whiteout_targets.add(f"{parent}/{target_name}" if parent else target_name)
            continue
        normal_members.append((path, member.isdir()))

    for directory in opaque_directories:
        _remove_path(rootfs_paths, directory, descendants_only=True)
    for target in whiteout_targets:
        _remove_path(rootfs_paths, target)

    for path, is_directory in normal_members:
        if is_directory:
            rootfs_paths.discard(path)
            continue
        _remove_path(rootfs_paths, path)
        rootfs_paths.add(path)


def _rootfs_file_inventory(
    members: dict[str, bytes], image_descriptor: dict[str, Any]
) -> tuple[tuple[str, ...], str]:
    """Return the selected image's final sorted file paths and inventory digest."""

    manifest = _descriptor_payload(members, image_descriptor)
    rootfs_paths: set[str] = set()
    for raw_layer in _sequence(manifest.get("layers"), "container_artifact_oci_manifest_invalid"):
        layer = _mapping(raw_layer, "container_artifact_oci_layer_invalid")
        _, layer_payload = _blob(members, layer)
        _apply_layer(rootfs_paths, layer_payload)
    inventory = tuple(sorted(rootfs_paths))
    inventory_payload = json.dumps(
        list(inventory), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return inventory, _digest(inventory_payload)


def _collaboration_relative_paths(
    inventory: tuple[str, ...],
    package_path: str,
) -> tuple[str, ...]:
    """Project physical inventory paths onto one canonical package namespace."""

    package_marker = f"/{package_path}/"
    relative_paths: list[str] = []
    for inventory_path in inventory:
        wrapped_path = f"/{inventory_path}"
        if inventory_path == package_path or inventory_path.endswith(f"/{package_path}"):
            relative_paths.append(package_path)
            continue
        marker_index = wrapped_path.rfind(package_marker)
        if marker_index >= 0:
            relative_paths.append(wrapped_path[marker_index + 1 :])
    return tuple(relative_paths)


def _validate_collaboration_surface(
    surface: ArtifactCollaborationSurface,
    inventory: tuple[str, ...],
) -> None:
    """Validate packaged source only; this does not claim an active writer."""

    actual_paths = _collaboration_relative_paths(inventory, surface.package_path)
    if surface.writer_surface == "source-only-unwired":
        expected_paths = frozenset(surface.source_paths)
        source_paths = tuple(path for path in actual_paths if path in expected_paths)
        source_path_set = frozenset(source_paths)
        missing = expected_paths - source_path_set
        if missing:
            _fail("container_artifact_collaboration_foundation_missing")

        compiled_sources: list[str] = []
        unexpected_paths: list[str] = []
        cache_prefix = f"{surface.package_path}/__pycache__/"
        for path in actual_paths:
            if path in expected_paths:
                continue
            if path.startswith(cache_prefix):
                cache_name = path.removeprefix(cache_prefix)
                match = re.fullmatch(
                    r"(?P<stem>.+)\.[A-Za-z0-9_-]+(?:\.opt-\d+)?\.pyc",
                    cache_name,
                )
                if match is not None:
                    compiled_sources.append(f"{surface.package_path}/{match.group('stem')}.py")
                    continue
            unexpected_paths.append(path)

        if (
            unexpected_paths
            or any(path not in expected_paths for path in compiled_sources)
            or len(source_paths) != len(expected_paths)
        ):
            print(
                "collaboration_surface_diagnostic:"
                f" unexpected={sorted(unexpected_paths)!r}"
                f" compiled={sorted(compiled_sources)!r}"
                f" source_count={len(source_paths)} expected_count={len(expected_paths)}"
                f" physical={sorted(path for path in inventory if '/plastic_promise/collaboration/' in f'/{path}')!r}",
                file=sys.stderr,
            )
            _fail("container_artifact_collaboration_surface_forbidden")
        return
    if surface.writer_surface == "absent" and actual_paths:
        _fail("container_artifact_collaboration_surface_forbidden")


def _inventory_contains_path(inventory: tuple[str, ...], target: str) -> bool:
    """Match one repository-relative path anywhere in an image inventory."""

    target = target.strip("/")
    if not target:
        return False
    prefix = f"{target}/"
    return any(
        path == target
        or path.endswith(f"/{target}")
        or path.endswith(f"/{prefix}")
        or f"/{prefix}" in f"/{path}"
        for path in inventory
    )


def _validate_server_compute_exclusions(
    role: str,
    inventory: tuple[str, ...],
    *,
    sbom: bool = False,
) -> None:
    """Prove the server image inventory contains no compute implementation."""

    if role != "pp-server-backend":
        return
    from plastic_promise.endpoint_roles import endpoint_role_contract

    forbidden = endpoint_role_contract(role).source_exclusions
    for target in forbidden:
        if _inventory_contains_path(inventory, target):
            suffix = "sbom" if sbom else "rootfs"
            _fail(f"container_artifact_server_compute_source_present_{suffix}")


def _collect_manifest_descriptors(
    members: dict[str, bytes], index: dict[str, Any], seen_indexes: set[str]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for raw_descriptor in _sequence(
        index.get("manifests"), "container_artifact_oci_layout_invalid"
    ):
        descriptor = _mapping(raw_descriptor, "container_artifact_oci_descriptor_invalid")
        media_type = descriptor.get("mediaType")
        if media_type in _IMAGE_INDEX_MEDIA_TYPES:
            digest, _ = _blob(members, descriptor)
            if digest in seen_indexes:
                _fail("container_artifact_oci_layout_cycle")
            seen_indexes.add(digest)
            results.extend(
                _collect_manifest_descriptors(
                    members, _descriptor_payload(members, descriptor), seen_indexes
                )
            )
        elif media_type in _IMAGE_MANIFEST_MEDIA_TYPES:
            _blob(members, descriptor)
            results.append(descriptor)
        else:
            _fail("container_artifact_oci_descriptor_invalid")
    return results


def _platform_matches(descriptor: dict[str, Any], wanted: str) -> bool:
    try:
        operating_system, architecture = wanted.split("/", maxsplit=1)
    except ValueError:
        _fail("container_artifact_platform_unsupported")
    platform = descriptor.get("platform")
    return (
        isinstance(platform, dict)
        and platform.get("os") == operating_system
        and platform.get("architecture") == architecture
    )


def _labels_for_image(
    members: dict[str, bytes], descriptor: dict[str, Any]
) -> tuple[str, dict[str, str]]:
    manifest = _descriptor_payload(members, descriptor)
    config = _mapping(manifest.get("config"), "container_artifact_oci_manifest_invalid")
    _, config_blob = _blob(members, config)
    config_payload = _json(config_blob, "container_artifact_oci_manifest_invalid")
    config_section = _mapping(
        config_payload.get("config"), "container_artifact_oci_manifest_invalid"
    )
    labels = _mapping(config_section.get("Labels"), "container_artifact_oci_labels_missing")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
        _fail("container_artifact_oci_labels_invalid")
    image_digest = descriptor.get("digest")
    if not isinstance(image_digest, str):
        _fail("container_artifact_oci_descriptor_invalid")
    return image_digest, dict(labels)


def _attestation_statement(layer_payload: bytes, image_digest: str) -> dict[str, Any]:
    """Return an in-toto statement after binding it to the selected image."""

    statement = _json(layer_payload, "container_artifact_attestation_subject_invalid")
    raw_subjects = _sequence(
        statement.get("subject"), "container_artifact_attestation_subject_invalid"
    )
    subject_digests: set[str] = set()
    for raw_subject in raw_subjects:
        subject = _mapping(raw_subject, "container_artifact_attestation_subject_invalid")
        raw_digests = _mapping(
            subject.get("digest"), "container_artifact_attestation_subject_invalid"
        )
        raw_sha256 = raw_digests.get("sha256")
        if not isinstance(raw_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", raw_sha256) is None:
            _fail("container_artifact_attestation_subject_invalid")
        candidate = f"sha256:{raw_sha256}"
        subject_digests.add(candidate)
    if image_digest not in subject_digests:
        _fail("container_artifact_attestation_subject_mismatch")
    return statement


def _normalise_sbom_file_path(name: str) -> str:
    """Return one safe rootfs-relative SPDX file path."""

    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        _fail("container_artifact_sbom_predicate_invalid")
    path = PurePosixPath(name.lstrip("/"))
    if not path.parts or ".." in path.parts:
        _fail("container_artifact_sbom_predicate_invalid")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts:
        _fail("container_artifact_sbom_predicate_invalid")
    return "/".join(parts)


def _spdx_file_inventory(layer_payload: bytes, image_digest: str) -> tuple[str, ...]:
    """Validate the bounded SPDX predicate needed for application-surface proof."""

    statement = _attestation_statement(layer_payload, image_digest)
    if (
        statement.get("_type") not in _IN_TOTO_STATEMENT_TYPES
        or statement.get("predicateType") not in _SPDX_PREDICATE_TYPES
    ):
        _fail("container_artifact_sbom_predicate_invalid")
    predicate = _mapping(statement.get("predicate"), "container_artifact_sbom_predicate_invalid")
    if (
        predicate.get("spdxVersion") not in {"SPDX-2.2", "SPDX-2.3"}
        or predicate.get("SPDXID") != "SPDXRef-DOCUMENT"
        or predicate.get("dataLicense") != "CC0-1.0"
        or not isinstance(predicate.get("name"), str)
        or not predicate["name"]
        or not isinstance(predicate.get("documentNamespace"), str)
        or not predicate["documentNamespace"]
    ):
        _fail("container_artifact_sbom_predicate_invalid")
    creation_info = _mapping(
        predicate.get("creationInfo"), "container_artifact_sbom_predicate_invalid"
    )
    creators = _sequence(creation_info.get("creators"), "container_artifact_sbom_predicate_invalid")
    if (
        not isinstance(creation_info.get("created"), str)
        or not creation_info["created"]
        or not creators
        or not all(isinstance(creator, str) and creator for creator in creators)
    ):
        _fail("container_artifact_sbom_predicate_invalid")
    _sequence(predicate.get("packages"), "container_artifact_sbom_predicate_invalid")
    raw_files = _sequence(predicate.get("files"), "container_artifact_sbom_predicate_invalid")
    paths: list[str] = []
    identifiers: set[str] = set()
    for raw_file in raw_files:
        file_entry = _mapping(raw_file, "container_artifact_sbom_predicate_invalid")
        identifier = file_entry.get("SPDXID")
        file_name = file_entry.get("fileName")
        if (
            not isinstance(identifier, str)
            or not identifier.startswith("SPDXRef-")
            or identifier in identifiers
            or not isinstance(file_name, str)
        ):
            _fail("container_artifact_sbom_predicate_invalid")
        identifiers.add(identifier)
        paths.append(_normalise_sbom_file_path(file_name))
    if len(paths) != len(set(paths)):
        _fail("container_artifact_sbom_predicate_invalid")
    return tuple(sorted(paths))


def _validate_sbom_collaboration_surface(
    surface: ArtifactCollaborationSurface,
    rootfs_inventory: tuple[str, ...],
    sbom_inventory: tuple[str, ...],
) -> None:
    """Require SPDX and final-rootfs views to agree on collaboration files."""

    rootfs_paths = _collaboration_relative_paths(rootfs_inventory, surface.package_path)
    sbom_paths = _collaboration_relative_paths(sbom_inventory, surface.package_path)
    if rootfs_paths != sbom_paths:
        _fail("container_artifact_sbom_collaboration_surface_mismatch")


def _attestation_layers(
    members: dict[str, bytes],
    descriptors: list[dict[str, Any]],
    image_digest: str,
    surface: ArtifactCollaborationSurface,
    rootfs_inventory: tuple[str, ...],
    role: str,
) -> tuple[str, str]:
    sbom_digest: str | None = None
    provenance_digest: str | None = None
    for descriptor in descriptors:
        annotations = descriptor.get("annotations")
        if not isinstance(annotations, dict):
            continue
        if annotations.get("vnd.docker.reference.type") != "attestation-manifest":
            continue
        if annotations.get("vnd.docker.reference.digest") != image_digest:
            continue
        manifest = _descriptor_payload(members, descriptor)
        for raw_layer in _sequence(
            manifest.get("layers"), "container_artifact_attestation_invalid"
        ):
            layer = _mapping(raw_layer, "container_artifact_attestation_invalid")
            digest, layer_payload = _blob(members, layer)
            layer_annotations = layer.get("annotations")
            predicate_type = ""
            if isinstance(layer_annotations, dict):
                raw_predicate = layer_annotations.get("in-toto.io/predicate-type")
                if isinstance(raw_predicate, str):
                    predicate_type = raw_predicate.lower()
            if not predicate_type:
                payload = _json(layer_payload, "container_artifact_attestation_invalid")
                raw_predicate = payload.get("predicateType")
                if isinstance(raw_predicate, str):
                    predicate_type = raw_predicate.lower()
            if "provenance" in predicate_type:
                _attestation_statement(layer_payload, image_digest)
                provenance_digest = digest
            elif predicate_type == "https://spdx.dev/document":
                sbom_inventory = _spdx_file_inventory(layer_payload, image_digest)
                _validate_sbom_collaboration_surface(
                    surface,
                    rootfs_inventory,
                    sbom_inventory,
                )
                _validate_server_compute_exclusions(role, sbom_inventory, sbom=True)
                sbom_digest = digest
    if sbom_digest is None:
        _fail("container_artifact_sbom_attestation_missing")
    if provenance_digest is None:
        _fail("container_artifact_provenance_attestation_missing")
    return sbom_digest, provenance_digest


def _canonical_labels_digest(labels: dict[str, str]) -> str:
    value = json.dumps(labels, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return _digest(value)


def main() -> int:
    from plastic_promise.deployment import (
        ArtifactEvidenceReceipt,
        ArtifactRequest,
        ContainerArtifactCompiler,
    )

    args = _arguments()
    repository_root = args.repository_root.resolve()
    request = ArtifactRequest(
        profile_id=args.profile_id,
        source_revision=args.source_revision,
        package_version=args.package_version,
        platforms=tuple(args.plan_platform),
        compute_variants=tuple(args.compute_variant or ()),
        model_catalog_reference=args.model_catalog_reference,
        model_catalog_digest=args.model_catalog_digest,
    )
    plan = ContainerArtifactCompiler(repository_root=repository_root).prepare(request)
    artifact = plan.artifact_for(args.role, args.platform, args.variant)
    members = _read_layout(args.oci_layout)
    root_digest = _digest(members["index.json"])
    root_index = _json(members["index.json"], "container_artifact_oci_layout_invalid")
    descriptors = _collect_manifest_descriptors(members, root_index, set())
    images = [item for item in descriptors if _platform_matches(item, args.platform)]
    if len(images) != 1:
        _fail("container_artifact_oci_platform_image_invalid")
    image_descriptor = images[0]
    image_digest, actual_labels = _labels_for_image(members, image_descriptor)
    expected_labels = plan.expected_oci_labels(artifact.artifact_id)
    if {key: actual_labels.get(key) for key in expected_labels} != expected_labels:
        _fail("container_artifact_oci_labels_mismatch")
    application_inventory, application_inventory_digest = _rootfs_file_inventory(
        members, image_descriptor
    )
    _validate_collaboration_surface(artifact.collaboration_surface, application_inventory)
    _validate_server_compute_exclusions(artifact.role, application_inventory)
    sbom_digest, provenance_digest = _attestation_layers(
        members,
        descriptors,
        image_digest,
        artifact.collaboration_surface,
        application_inventory,
        artifact.role,
    )
    receipt = ArtifactEvidenceReceipt(
        artifact_id=artifact.artifact_id,
        role=artifact.role,
        platform=artifact.platform,
        variant=artifact.variant,
        source_revision=plan.request.source_revision,
        package_version=plan.request.package_version,
        base_image_reference=artifact.base_image_reference,
        recipe_policy_digest=plan.recipe_policy_digest,
        policy_digest=plan.policy_digest,
        oci_layout_digest=root_digest,
        image_digest=image_digest,
        oci_labels_digest=_canonical_labels_digest(expected_labels),
        sbom_digest=sbom_digest,
        sbom_subject_digest=image_digest,
        provenance_digest=provenance_digest,
        provenance_subject_digest=image_digest,
        collaboration_surface_digest=artifact.collaboration_surface_digest,
        application_inventory_digest=application_inventory_digest,
    )
    receipt.validate_against(plan, artifact)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - command-line entry point
    try:
        raise SystemExit(main())
    except OciEvidenceError as exc:
        raise SystemExit(str(exc)) from exc
