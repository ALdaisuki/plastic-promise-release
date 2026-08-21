# OCI package naming

Plastic Promise uses one predictable GHCR namespace for published OCI
artifacts. The Python distribution keeps the PyPI name `plastic-promise`; its
version is carried by the wheel/sdist metadata and the release tag.

## Canonical names

```text
ghcr.io/aldaisuki/plastic-promise-edge:vX.Y.Z
ghcr.io/aldaisuki/plastic-promise-server:vX.Y.Z
ghcr.io/aldaisuki/plastic-promise-compute:vX.Y.Z-cpu
ghcr.io/aldaisuki/plastic-promise-compute:vX.Y.Z-cuda
```

Every published image also receives an immutable source tag:

```text
sha-<full-source-commit>
sha-<full-source-commit>-cpu
sha-<full-source-commit>-cuda
```

Deployments use the digest returned by the build, never a mutable version or
source tag. The canonical mapping is maintained in
`plastic_promise/release_package_naming.py` and consumed by the stable
workflow, so a package rename cannot silently drift from release manifests.

## Compatibility

The former flat repositories remain valid inputs when reading historical
release manifests and deployment receipts:

```text
ghcr.io/aldaisuki/plastic-promise-local-edge
ghcr.io/aldaisuki/plastic-promise-server
ghcr.io/aldaisuki/plastic-promise-local-inference-node
```

New releases do not publish new tags to those legacy repositories. Existing
digest-pinned deployments continue to work; migrate them by replacing the
repository with the canonical package and verifying the new release manifest.
