# Release Delivery Implementation Notes

> **Status (2026-08-11): target / unverified.** The current stable publisher is
> not wired to the PR 6 selected-evidence gate; no stable workflow run is Bundle
> verification evidence by itself.

## Runtime boundaries

The release subsystem is intentionally independent of the MCP process and its
SQLite/LanceDB state. Do not add publishing credentials, release side effects,
or container-build code to `plastic_promise.mcp`, the memory pipeline, or the
deployment controller.

## Required GitHub environments

Create these GitHub environments before enabling a workflow:

| Environment | Used by | Required protection |
|---|---|---|
| `release-candidate` | `release-rc.yml` | Explicit reviewer/restriction policy in repository settings; dispatch only from the default branch with `source_ref` equal to the workflow SHA and `github.ref_protected=true`. |
| `testpypi` | `release-testpypi.yml` | Explicit reviewer; only RC refs may invoke it. |
| `production-release` | `release-publish.yml`, release-repository `release-sync.yml` | Explicit reviewer and restricted stable tag/ref policy. This protects the current transition publisher; it does not yet wire the PR 6 bundle/catalog/attestation gate. |

Configure TestPyPI and PyPI Trusted Publishers to match their specific
repository, workflow filename, and environment. Do not add API tokens to
repository secrets for these workflows. Configure GHCR package visibility and
retention independently; manifests use image digests even when a convenience
tag exists.

## Verification commands

```bash
python scripts/validate_release_variant.py --repo-root . release/variants/standard.json
python -m pytest -q --no-cov \
  tests/test_release_variant.py tests/test_release_workflow.py tests/test_release_manifest.py \
  tests/test_model_catalog.py tests/test_release_bundle.py tests/test_create_release_bundle.py
python -m build --outdir dist
python -m twine check dist/*
python -m venv .release-wheel-venv
.release-wheel-venv/bin/python -m pip install --no-deps --only-binary=:all: dist/*.whl
```

The local build confirms Python artifacts but does not publish to TestPyPI,
PyPI, GHCR, create a tag, or touch the release repository.

The RC workflow needs a real, source-controlled, fixed-revision Model Catalog;
the repository intentionally contains no example that could accidentally be
released as an active identity. Its first complete matrix is
`split-accelerated` / `remote-inference` with `embedding/v1` and `rerank/v1`.
It builds edge/server/CPU artifacts for `linux/amd64` and `linux/arm64`, CUDA
compute for `linux/amd64`, derives an opaque per-artifact CycloneDX receipt
set, then attests and verifies catalog, **v2** artifact binding, receipt set,
and manifest before creating the final Release Bundle. The independent,
canonical `artifact-sbom-receipts.json` binds each OCI-layout root, image
digest, role/platform/variant, SBOM digest, and SBOM byte size; it carries no
SBOM path or payload. Separately, Buildx embedded SBOM/provenance subjects are
verified against each exact image digest. The **v2** artifact binding records
the layout, image, labels, embedded SBOM, provenance, verifier receipt, and
independent Syft SBOM digests instead of conflating the two SBOM evidence
classes. Do not run the bundle
script with locally invented `verified-evidence.json` outside this workflow.
The candidate job also verifies that the checkout, clean tracked catalog blob,
and requested source revision are identical, and compares `pyproject.toml`
package version before Buildx starts. It generates a separate CycloneDX SBOM
for every image role/platform/variant from the OCI archives; the package SBOM
is generated from the exact wheel in a fresh dependency-resolved environment
and remains a distinct release-manifest input.

## Stable release handoff

The following is the **target** handoff. It must not be inferred from the
current `.github/workflows/release-publish.yml`: that workflow accepts only
`source_ref` and `release_version` and does not yet consume or verify the PR 6
Release Bundle, Model Catalog, artifact binding, or RC attestation.

1. Add a selected-evidence gate that verifies the exact RC Release Bundle,
   catalog, binding, and attestation against the requested stable source.
2. Only after that gate passes, run the protected stable publisher and preserve
   the generated release-manifest artifact and GitHub attestation.
3. Supply the manifest, exact source commit, release evidence, and reviewed
   commit range to the release-repository workflow.
4. The release-repository workflow rejects RC channels and mismatched commit or
   version evidence before it runs the existing audited sync script.

An operator must explicitly approve each protected workflow. A merged `main`
commit is never sufficient authority to publish.
