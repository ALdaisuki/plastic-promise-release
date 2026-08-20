# Plastic Promise Release Delivery Architecture

> **Status (2026-08-11): target / unverified.** This document describes the
> PR 6 selected-evidence release gate; it does not claim that an RC, signature,
> attestation, PyPI/GHCR publication, or production deployment has occurred.

## 1. System overview

The release-delivery subsystem turns a reviewed source revision into verifiable
distribution evidence without granting release permissions to the MCP runtime.
It supports the standard distribution's wheel, source distribution, local-edge
OCI image, server OCI image, and CPU/CUDA inference-node images. The PR 6
selected-evidence rule is **target/unverified**: a release may be considered
valid only when its manifest and Release Bundle bind a SemVer version to an
immutable source commit, SHA-256 Python artifacts, an SBOM, OCI digests, and a
fixed Model Catalog identity.

The intended scale is a small, self-hosted agent platform with public artifacts
and a separate stable release repository. Release workflows are deliberately
more conservative than normal development: PRs verify only; RCs produce
candidate artifacts; TestPyPI and stable publication are separately approved.

## 2. Architecture diagram

The corresponding workflow diagrams are [`workflow.mermaid`](workflow.mermaid)
and [its Chinese equivalent](workflow.zh-CN.mermaid). The README contains the
compact C4 context diagram intended for everyday contributors.

## 3. Module inventory

| Module | Boundary | Responsibility | Does not do |
|---|---|---|---|
| `plastic_promise.release_manifest` | Pure package contract | Validate SemVer/PEP 440 mapping, hashes, embedded Python metadata, SBOM, source commit, and OCI digest bindings. | Build, publish, invoke GitHub, Docker, or MCP. |
| `plastic_promise.deployment.release_bundle` | Pure release contract | Bind Model Catalog resource/identity metadata, complete OCI role matrix, protocol/capability compatibility, and verified evidence projections. | Download models, query a registry, verify a signature itself, or gain deployment authority. |
| `scripts/create_release_manifest.py` | Script adapter | Convert completed CI evidence into a new immutable JSON manifest. | Overwrite evidence or infer missing image digests. |
| `scripts/create_release_bundle.py` | Script adapter | Parse generated OCI layouts, emit canonical catalog/binding evidence, and build a Release Bundle only from externally verified evidence. | Build or publish OCI images, self-certify evidence, or rewrite attested bytes. |
| `artifact-sbom-receipts.json` | Independently attested evidence | Canonically bind every expanded artifact's OCI-layout root, image digest, role/platform/variant, opaque CycloneDX SBOM digest, and SBOM byte size. | Carry an SBOM path, SBOM payload, a model path, or release authority. |
| `deploy/local-edge/Dockerfile` | Static edge container | Build the non-authoritative dashboard-edge image. | Proxy canonical state, own SQLite, or retain model/runtime state. |
| `deploy/server/Dockerfile` | Runtime container | Build the portable Streamable HTTP MCP runtime image. | Include runtime state, models, logs, secrets, or credentials. |
| `deploy/local-inference-node/Dockerfile` | Accelerator container | Build the Linux NVIDIA embedding/rerank node image. | Own canonical SQLite or fetch model weights. |
| Release workflows | CI authority | Build, attest, and upload RC candidates; the current stable publisher is separately protected but not yet wired to PR 6 selected evidence. | Auto-publish from a PR or ordinary `main` push, or treat stable publication as Release Bundle verification. |
| `release/variants/standard.json` | Public contract | Declare artifacts, profiles, exclusions, provenance, and gates. | Store a concrete release's hashes or secrets. |
| `scripts/release-sync.py` | Release-repository handoff | Copy an audited stable release tree and enforce source/release evidence. | Be invoked by normal PR CI. |

## 4. Communication patterns

- PR verification is synchronous CI: source checkout, contract validation,
  artifact build, exact local wheel installation, and non-pushing OCI builds.
- RC processing is manual and asynchronous from the developer's perspective.
  The first implemented RC candidate accepts only a tracked,
  fixed-revision `split-accelerated` / `remote-inference` Model Catalog with
  `embedding/v1` and `rerank/v1`; it refuses all other catalog matrices before
  OCI work. The dispatch must run from the default branch at the exact input
  SHA and GitHub must report that ref protected; the build then uses the
  repository-configured `release-candidate` environment. It uploads short-lived
  candidate artifacts for review but makes no registry or release-repository
  mutation.
- TestPyPI uses a separate protected environment and its own trusted publisher;
  the following job installs exactly the requested package version with no
  dependency resolution.
- Stable OCI publication runs only from `workflow_dispatch` in the
  `production-release` environment. The current `release-publish.yml` produces
  digest evidence and convenience tags for manifest construction, but accepts
  only `source_ref` and `release_version`; it does not yet consume or verify
  the PR 6 Release Bundle, Model Catalog, artifact binding, or RC attestation.
  The selected-evidence stable gate is therefore future work, not an achieved
  property of the current stable publisher.
- Stable repository synchronization and PyPI publication are a separate
  release-repository responsibility. They consume an attested stable manifest
  and cannot accept an RC channel.

## 5. Data flow

1. A default-branch workflow run proves that its own SHA equals the fixed
   source ref, then checks out and resolves that Git commit SHA.
2. The checked-in package version is compared with the requested RC version
   before OCI work; then `python -m build` emits one wheel and one sdist whose
   embedded package metadata must match the release version.
3. A fresh environment installs the exact built wheel with its runtime
   dependencies, produces the package SBOM, and binds it through the release
   manifest.
4. The initial RC matrix builds local edge, server, and CPU compute for
   `linux/amd64` + `linux/arm64`, plus CUDA compute for `linux/amd64`.
   A later selected-evidence stable gate must consume the verified RC evidence;
   the current stable publisher has not yet been wired to do so.
5. Buildx emits embedded SBOM and provenance attestations for every platform
   image. The verifier checks each attestation subject against the exact image
   digest and records the OCI-layout, labels, embedded-SBOM, provenance, and
   verifier-receipt digests. Independently, Syft scans every
   role/platform/variant entry in the exact OCI archives and
   emits one opaque per-artifact CycloneDX SBOM. The canonical,
   independently attested `artifact-sbom-receipts.json` binds its digest/size
   to the OCI-layout root, exact image digest, and role/platform/variant.
6. The pure manifest module hashes Python files and the package SBOM, verifies
   artifact metadata, source, and digest formats, then writes a new manifest.
7. The bundle script proves its checkout and tracked Model Catalog blob match
   the claimed source revision, verifies raw OCI descriptor blob SHA-256 and
   size, then writes the receipt set. For every artifact, `artifact-binding/v2`
   carries the OCI-layout, image, labels, embedded-SBOM, provenance,
   verifier-receipt, and independent Syft-SBOM digests; it also carries the
   receipt-set digest and rejects a receipt whose OCI/image/platform/SBOM
   association differs. It writes canonical Model Catalog and artifact-binding
   files whose bytes equal the typed semantic digests.
8. GitHub attests and verifies the manifest, catalog, **v2** binding, and
   receipt set with the source commit and signer workflow policy. Only then can
   the script construct the Release Bundle, which is attested as a final
   candidate artifact.
9. A future selected-evidence stable gate must verify the RC bundle before
   stable publication. Separately, the release repository accepts only a stable
   manifest whose version and source commit match the requested release.

## 6. Memory and state management

Release delivery has no MCP-memory, task-queue, SQLite, or LanceDB write path.
The only durable release evidence is the artifact, opaque SBOM receipt set,
GitHub attestation, release manifest, Model Catalog, **v2** artifact binding,
and Release Bundle. Runtime
containers are stateless; operators mount their own SQLite/LanceDB and node
configuration only after deploy preflight.

## 7. Error handling strategy

- A missing, duplicate, corrupt, or metadata-mismatched Python artifact fails
  manifest construction.
- A mutable OCI reference, missing required image, invalid SHA, absent SBOM,
  missing receipt set, or receipt whose layout/image/platform/SBOM association
  differs from `artifact-binding/v2` fails closed.
- A missing, untracked, path-escaping, incompatible, or placeholder Model
  Catalog fails before the RC OCI build begins. No synthetic catalog is stored
  in this repository.
- A Release Bundle cannot be emitted until the three pre-bundle subjects have
  passed GitHub attestation verification; canonical catalog/binding bytes may
  be read again but cannot be rewritten after attestation.
- Writing an existing manifest fails rather than replacing prior evidence.
- PR and RC workflows stop before any registry publication.
- A stable release requires protected-environment approval before either image
  publishing or release-repository synchronization can start. Approval alone
  does not prove the PR 6 selected-evidence gate: current `release-publish.yml`
  still lacks that bundle/catalog/attestation verification.
- A failed stable sync leaves the source evidence unchanged; no MCP runtime or
  canonical data path is modified.

## 8. Security model

The source tree stores no package token, registry password, private key, model,
database, runtime state, or log bundle. TestPyPI/PyPI use configured OIDC
Trusted Publishing; workflows request `id-token: write` only where needed.
The production workflow has package write permission only behind
`production-release`. Release manifests contain public references and hashes,
never secret values. The existing `.dockerignore` excludes state and
credential-like files from image contexts.

## 9. Monitoring and observability

Release observability is evidence-oriented rather than telemetry-oriented:
workflow run URLs, digest output, attestations, exact artifact hashes, and
manifest validation errors are the primary signals. The stable manifest is the
operator-facing index of a release's source and artifact identity. It does not
include user content, infrastructure addresses, or runtime logs.

## 10. Scalability plan

The design keeps expensive operations isolated. PR jobs validate an OCI layout
without registry writes. RC jobs retain artifacts for 14 days. Stable OCI builds
can use Buildx cache in a future operational change without changing the
manifest contract. The server image is multi-architecture; the accelerator
image remains Linux/amd64 because its CUDA/NVIDIA runtime is platform-specific.

## 11. Technology stack

| Concern | Choice |
|---|---|
| Python packaging | `python -m build`, `twine check`, isolated wheel install |
| OCI build | Docker Buildx and OCI labels |
| SBOM | CycloneDX JSON |
| Provenance | GitHub artifact attestations |
| Registry | GHCR immutable digest references |
| Package registries | TestPyPI rehearsal, PyPI stable publishing via OIDC |
| Release truth | Versioned manifest plus release-repository audit |

## 12. Cost model

No fixed dollar estimate is embedded because GitHub Actions, GHCR retention,
and model-image build time depend on the repository plan and source revision.
The architecture instead bounds cost by avoiding container pushes on PRs,
retaining RC artifacts for only 14 days, and requiring an explicit approval for
the expensive stable multi-architecture build.

## 13. Implementation phases

1. **Contract and build verification:** release-manifest validation, wheel/sdist
   build, exact installation, OCI Dockerfiles, and no-push PR checks.
2. **Candidate rehearsal:** manual RC artifacts and separately approved
   TestPyPI exact-version installation.
3. **Stable publication (future PR 6 gate):** verify the selected RC Release
   Bundle/catalog/binding/attestation, then perform protected GHCR evidence
   build, manifest construction, and stable-only release-repository sync/PyPI
   OIDC. Current `release-publish.yml` is not yet this gate.

No new MCP server configuration is generated for this subsystem. Keeping
release authority outside the MCP tool surface is an intentional least-
privilege boundary.
