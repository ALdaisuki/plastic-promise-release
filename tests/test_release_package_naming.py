from plastic_promise.release_package_naming import (
    OCI_PACKAGE_REPOSITORIES,
    canonical_oci_repository,
    is_known_oci_repository,
)


def test_canonical_oci_repositories_use_one_versioned_flat_namespace():
    assert OCI_PACKAGE_REPOSITORIES == {
        "local-edge": "ghcr.io/aldaisuki/plastic-promise-edge",
        "server": "ghcr.io/aldaisuki/plastic-promise-server",
        "inference-cpu": "ghcr.io/aldaisuki/plastic-promise-compute",
        "inference-node": "ghcr.io/aldaisuki/plastic-promise-compute",
    }


def test_historical_repositories_are_read_compatible_but_not_canonical():
    assert canonical_oci_repository("server") == "ghcr.io/aldaisuki/plastic-promise-server"
    assert is_known_oci_repository("ghcr.io/aldaisuki/plastic-promise-local-edge", "local-edge")
    assert is_known_oci_repository(
        "ghcr.io/aldaisuki/plastic-promise-local-inference-node", "inference-node"
    )
    assert not is_known_oci_repository("ghcr.io/aldaisuki/plastic-promise:latest", "server")
