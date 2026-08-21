"""Canonical names for published Plastic Promise OCI packages.

The Python distribution keeps the stable PyPI name ``plastic-promise``.  OCI
artifacts use one GHCR namespace with component names below it so that the
package list is predictable and version tags carry the release identity:

``ghcr.io/aldaisuki/plastic-promise-<component>:vX.Y.Z``

The legacy flat repositories remain accepted when reading old release
manifests and deployment receipts.  New workflows must publish only the
canonical repositories defined here.
"""

from __future__ import annotations

from typing import Final

GHCR_NAMESPACE: Final = "ghcr.io/aldaisuki"

OCI_PACKAGE_REPOSITORIES: Final[dict[str, str]] = {
    "local-edge": f"{GHCR_NAMESPACE}/plastic-promise-edge",
    "server": f"{GHCR_NAMESPACE}/plastic-promise-server",
    "inference-cpu": f"{GHCR_NAMESPACE}/plastic-promise-compute",
    "inference-node": f"{GHCR_NAMESPACE}/plastic-promise-compute",
}

LEGACY_OCI_PACKAGE_REPOSITORIES: Final[dict[str, str]] = {
    "local-edge": "ghcr.io/aldaisuki/plastic-promise-local-edge",
    "server": "ghcr.io/aldaisuki/plastic-promise-server",
    "inference-cpu": "ghcr.io/aldaisuki/plastic-promise-local-inference-node",
    "inference-node": "ghcr.io/aldaisuki/plastic-promise-local-inference-node",
}


def canonical_oci_repository(image_name: str) -> str:
    """Return the published GHCR repository for a manifest image name."""

    try:
        return OCI_PACKAGE_REPOSITORIES[image_name]
    except KeyError as exc:
        raise ValueError("oci_package_image_name_invalid") from exc


def legacy_oci_repository(image_name: str) -> str:
    """Return the pre-v1 flat GHCR repository for compatibility checks."""

    try:
        return LEGACY_OCI_PACKAGE_REPOSITORIES[image_name]
    except KeyError as exc:
        raise ValueError("oci_package_image_name_invalid") from exc


def is_known_oci_repository(repository: str, image_name: str) -> bool:
    """Accept canonical and legacy repositories when reading evidence."""

    return repository in {
        canonical_oci_repository(image_name),
        legacy_oci_repository(image_name),
    }
