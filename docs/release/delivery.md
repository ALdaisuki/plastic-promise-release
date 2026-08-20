# Release Delivery and Installation Profiles

> **Status on August 20, 2026:** the `v0.2.15` stable workflow built and pushed
> all four required GHCR OCI roles at immutable digests. The Python wheel and
> sdist were built, checked, and attested, but PyPI Trusted Publishing was
> rejected by PyPI with `invalid-publisher`; no PyPI publication or complete
> release manifest is claimed until the external publisher is configured.

The verified OCI subjects from run
[`32376104075`](https://github.com/ALdaisuki/plastic-promise-release/actions/runs/32376104075)
are:

| Role | Immutable digest |
| --- | --- |
| `local-edge` | `sha256:65c6d54a5c1cbbf96f837bef05f26e4756283b1060c98223dc108682aaf433ea` |
| `server` | `sha256:40e0d61d1efb59899ab4160b9dd34001a2198781966564cae95aca47534340dd` |
| `inference-cpu` | `sha256:947ffcfe0c43b9fc228ecb99755bc7113cdc49a881ce1baf9b95b68e5a20d189` |
| `inference-node` (CUDA control image) | `sha256:fb48386f34bcd6693ea6f1de9ad829c64ad3e772f3ca87a3859d7b023dd41d2a` |

These digests are build evidence, not a claim that the Mac canonical runtime or
the Windows node has been cut over. Those deployments require a successful
pull, health check, and runtime receipt.

Plastic Promise has one source distribution contract and three supported target
runtime profiles. A profile changes the placement of runtime and inference work;
it never changes ownership: `pp-server-backend` is the only SQLite writer and
LanceDB is rebuildable derived state.

## Installation choices

| Need | Basic target choice | Recommended target choice |
| --- | --- | --- |
| MCP runtime with hosted inference | Base package plus `local-cloud` profile | Exact reviewed wheel and controlled configuration revision |
| All-local development | `local-all-in-one` profile | Optional Rust core only after Python behavior is verified |
| Server plus local accelerator | `split-accelerated` profile | Bounded compute-node identity and restricted private transport |
| OCI server runtime | Server image selected by digest | Verified digest referenced by a reviewed Release Bundle |
| Linux NVIDIA compute node | Compute-node image selected by digest | Read-only, node-local model mount and matching identity evidence |

For manifest fields, resource preflight, and profile boundaries, see
[deployment profiles](../deployment/profiles.md) and the
[three-endpoint architecture](../architecture/three-endpoint-deployment/architecture.md).

## Delivery channels and authority

| Channel | Target output | External authority | Non-claim |
| --- | --- | --- | --- |
| Pull-request verification | Test result and no-push artifacts | None | Does not create a tag, image publication, or deployment. |
| RC artifacts / candidate | Exact wheel/sdist hashes, OCI-layout evidence, SBOM/provenance references; an optional **target** TestPyPI candidate | Protected candidate workflow | Does not imply stable publication or that TestPyPI received a package. |
| Stable immutable evidence | Digest-pinned OCI evidence plus a selected Release Bundle | **Target** protected selected-evidence gate; not yet the current stable workflow | Does not imply a live server. |
| Stable-only repository handoff | Selected stable manifest and release receipt projection | Separate protected release-repository workflow | Does not imply PyPI/GHCR publication unless that workflow succeeds. |

PRs do not automatically create a stable tag, upload a package, push an image,
or synchronize `plastic-promise-release`.

The target `production-release` selected-evidence gate is one of the **GitHub protected workflows**,
not an inference from a merged PR. It must independently
verify the selected Release Bundle, Model Catalog, artifact binding, and RC
attestation, using GitHub OIDC identities rather than stored registry or PyPI
tokens where the target supports trusted publishing, before it may publish the `plastic-promise-server` and
`plastic-promise-local-inference-node` OCI roles or advance a package from a
TestPyPI candidate to a stable channel.

**Current implementation boundary:** `.github/workflows/release-publish.yml`
is a separately protected, transition stable publisher. It accepts only
`source_ref` and `release_version`; it does **not** currently consume or verify
a PR 6 Release Bundle, Model Catalog, artifact binding, or RC attestation.
It is therefore not the selected-evidence gate described above and must not be
used as evidence that the PR 6 stable handoff is complete. Those role names and
the selected-evidence gate remain target-only until the workflow is wired and
independently verified.

## PR 6 target: Model Catalog and Release Bundle

The source-level `ArtifactBundle` proves build-policy inspection only. PR 6
introduces a separate **target** Release Bundle contract for selecting a
candidate and carrying immutable evidence across protected release boundaries.

| Item | Required target fields | Explicit exclusion |
| --- | --- | --- |
| Model Catalog | Opaque catalog ref/digest; fixed model revision; identity/capability; compatibility and resource metadata | Weights, local path, provider token, node address, and deployment authority |
| Artifact Bundle | **v2** role/platform/variant descriptors; immutable image evidence; the digest of independently attested `artifact-sbom-receipts.json` | Container start, registry push, service control, migration, and promotion authority |
| Release Bundle | Source revision; package version; protocol compatibility; supported profile/variant matrix; image/per-artifact SBOM-receipt evidence plus verified protected-build provenance; Model Catalog ref/digest | Mutable tags, runtime config, canonical state, credentials, and a claim that a candidate is live |
| Release manifest | Selected, reviewable Release Bundle projection for a protected workflow | An execution grant, Migration Operation evidence, or health receipt |

All digests and signatures mentioned here are target evidence requirements. A
documented field is not evidence until the corresponding protected workflow has
produced and independently verified it.

The current RC workflow deliberately implements one complete candidate matrix:
`split-accelerated` with `remote-inference`, `embedding/v1` and `rerank/v1`;
local edge, server, and CPU compute cover `linux/amd64` and `linux/arm64`, and
CUDA compute covers `linux/amd64`. It fails closed for every other
profile/runtime/capability combination before an OCI build begins. No active
model catalog is checked into this target contract: an operator must supply a
real, tracked, fixed-revision catalog when starting an RC, and a successful
workflow run is still required before any attestation exists.

The RC workflow must be dispatched from the repository's default branch at the
same immutable SHA supplied as `source_ref`; it fails closed unless GitHub
reports that ref as protected by a branch protection rule or ruleset. Its
expensive build job uses the `release-candidate` environment, which repository
settings must protect with the required reviewers/restrictions. Before OCI work
it also checks that the exact source package version matches the requested RC
version. Bundle creation refuses a checkout, tracked catalog, or catalog blob
whose Git identity does not exactly match the claimed source revision.

The package SBOM (`package.sbom.cdx.json`) is generated from a fresh environment
that installs the exact built wheel with its runtime dependencies, then is
separately bound by the release manifest.
Buildx also emits embedded SBOM and provenance attestations for every OCI
platform image. The verifier binds their subjects to the exact image digest and
records the OCI-layout, image, labels, embedded-SBOM, provenance, and verifier-
receipt digests. Separately, for the candidate OCI matrix, the workflow scans each exact platform entry in
each OCI archive and derives one opaque CycloneDX SBOM receipt for every
expanded artifact. The independently attested canonical
`artifact-sbom-receipts.json` binds the OCI-layout root digest, image digest,
role/platform/variant, SBOM digest, and SBOM byte size; it contains no SBOM path
or payload. `artifact-binding/v2` carries both the embedded BuildKit evidence
digests and the independent Syft SBOM digest for each artifact, plus the
receipt-set digest, and rejects a mismatched receipt matrix. The bundle parser also verifies raw OCI descriptor
bytes against both their declared SHA-256 and size before it accepts labels or
an image digest.

## Source-only verification boundary

The source distribution intentionally includes `scripts/init_and_start.py` as
an **optional operator runtime launcher**. It is not an installation default
and is never invoked by PR or source-only release verification. Those checks
extract the exact sdist and run only the side-effect-free
`scripts/verify_release_deployment.py` proof against example manifests. They
must not start MCP or Maintenance, invoke Docker, create backups, migrate
SQLite, promote LanceDB, or change runtime state.

The extracted-sdist proof installs with `--no-deps` and imports only the pure
deployment planning/controller leaves needed for parse, plan, preflight, and
in-memory asset rendering. A server runtime dependency is therefore not an
implicit requirement of this source-only boundary.

## Build authority and runtime split

All Docker/OCI image construction is now a GitHub Actions responsibility. The
protected `release-verify.yml`, `release-rc.yml`, and `release-publish.yml`
workflows own Buildx, multi-architecture output, SBOM/provenance attestations,
and registry publication. A Mac, Windows, or WSL2 checkout must not build a
release image locally and must not be treated as equivalent evidence.

Local machines may still run source-only checks, recipe validation, resource
preflight, and an already-published compute-node image for derived-inference
smoke. They consume a verified image digest; they do not create the digest.

```text
source SHA -> GitHub protected Buildx -> SBOM/provenance -> immutable digest
           -> selected release evidence -> server/edge/compute deployment
```

The checked-in local build helpers are retained for compatibility with older
operator environments, but they are not part of the release authority and
their output must never be promoted to RC or stable evidence. New release
instructions should dispatch the GitHub workflow instead of invoking a local
`docker build` or `docker buildx build` command.

## Target build and runtime split

```text
+-------------------- Windows / WSL2 --------------------+
| source checks + digest-pinned runtime/GPU smoke          |
| no image build; no SQLite writer; no release authority   |
+------------------------+--------------------------------+
                         | candidate inputs only
                         v
+---------------- GitHub protected workflow --------------+
| RC/stable builds -> SBOM/provenance -> immutable digest |
| -> target Release Bundle and release-manifest evidence  |
+------------------------+--------------------------------+
                         | verified selected digest only
                         v
+-------------------- Server runtime ---------------------+
| pp-server-backend: canonical SQLite single writer       |
| LanceDB: derived/rebuildable; target MCP/Maintenance    |
+------------------------+--------------------------------+
                         | bounded secret-free receipt
                         v
+--------------- Stable-only release repository ----------+
| target: explicit sync + separately approved publication |
+----------------------------------------------------------+
```

Windows/WSL2 is a source-check and digest-pinned runtime/GPU-smoke location
only. It may produce **derived inference** smoke evidence; it may not build a
release image, become a canonical writer, or substitute for protected release
evidence. GitHub protected automation is the producer of immutable release
evidence. The server is a consumer of a verified digest, never the build authority: it performs a
**manifest-pinned** pull only after verification. It returns a bounded **MCP E2E receipt**
before any **stable-only release repository** handoff is considered.

## Controlled server consumption

Before a target server accepts a selected digest, its composition must keep one
canonical runtime lock and one SQLite writer. The server may then perform the
separately authorized Migration Operation, derived shadow rebuild/promotion,
MCP verification, and Maintenance transition. Each is independent from the
bundle itself and must return a bounded, secret-free result before a stable-only
handoff can be considered.

Neither an image digest nor an RC artifact can recover data. If a later gate
fails, preserve SQLite and audit/source evidence, select a prior immutable
bundle/digest, and rebuild LanceDB from canonical state. Do not restore memory
text from a derived index.

See the matching [six-PR readiness plan](six-pr-readiness.md) and
[deployment guide](../deployment/README.md).
