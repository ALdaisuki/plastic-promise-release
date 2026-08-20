# Container Artifact Boundary

> **PR 3/PR 6 source boundary — build and verification only (2026-08-11).**
> `ContainerArtifactCompiler` is a source-level planning/materialization seam.
> Neither this document nor an `ArtifactBundle` proves that an image was built,
> loaded, pushed, started, deployed, or used in production.
> The current collaboration contract/projection/lease/policy/bridge modules and
> `CollaborationEventLog` foundation are packaged only in
> `pp-server-backend`; that source remains unwired and opens no listener or
> SQLite connection in this source-level evidence layer.

Chinese parity document:
[`container-artifacts.zh-CN.md`](container-artifacts.zh-CN.md).

Related maps:

- [`diagrams/artifact-build.txt`](diagrams/artifact-build.txt) — compact ASCII
  component map;
- [`diagrams/artifact-build.mermaid`](diagrams/artifact-build.mermaid) —
  build-only sequence and authority boundary;
- [`diagrams/container-artifact-matrix.svg`](diagrams/container-artifact-matrix.svg)
  — visual matrix, with a Chinese-equivalent SVG beside it.

## 1. Scope and authority

PR 3 gives the three-endpoint design a **build-time** deep module. Its public
surface is intentionally small:

```text
ContainerArtifactCompiler.prepare(request) -> ArtifactBuildPlan
ContainerArtifactCompiler.materialize(plan, executor) -> ArtifactBundle
static recipe-policy preflight(repository root) -> RecipePolicyReceipt
RolePackageCompiler.materialize(role, output root, version) -> RolePackageMaterialization
```

The implementation lives in `plastic_promise/role_package.py`. It is the
single source-to-package seam consumed by the server and compute Docker
recipes. The complete development tree is available only in the discarded
compiler stage; the final image receives the materialized allowlist output.
This is physical allowlist materialization, not a full-copy-then-delete
simulation.

`prepare` resolves a deterministic, secret-free artifact policy. `materialize`
hands that policy to a build-time `ArtifactBuildExecutor` and returns an
inspectable descriptor bundle. The executor receives the prepared plan and a
descriptor, so it has the pinned revision/package metadata and expected-label
policy without a second out-of-band configuration. A local Docker/Buildx
adapter, a protected CI adapter, and a test fake may differ internally without
changing that policy contract. The P1 correction adds a source-only static
recipe-policy preflight plus a structured, plan-bound, body-free evidence
receipt for every planned descriptor. It binds immutable OCI layout/image,
SBOM, provenance, expected labels, source/package identity, recipe-policy
digest, base-image digest, `collaboration_surface_digest`, and
`application_inventory_digest`. The inventory is the normalized file-path view
of the final OCI root filesystem after layer and whiteout semantics are
applied. These mandatory surface and inventory bindings make the receipt
`plastic-promise-container-evidence/v2`; a v1 receipt is rejected rather than
silently interpreted with the new shape. The typed role surface is also the
single source for module names, source paths, and OCI inventory expectations.
The receipt is a local/CI verification contract. In
protected CI it is generated only after checking Buildx-provided SBOM and
provenance attestation layers bound to the exact OCI image; it is not a release
signature, publication, runtime activation, or deployment proof.

Plastic Promise remains one monorepo and one version line. The root
`pyproject.toml` is the complete development installation; production recipes
do not install it. They compile three role packages from the same source
revision: static `pp-local-edge`, Python `pp-server-backend`, and Python
`pp-compute-node`. CPU and CUDA are two image variants of the one compute role
package, not separate repositories or independently versioned Python packages.

This module is not a deployment controller. It does **not** own a Deployment
Manifest, Deployment Receipt, canonical SQLite, LanceDB promotion decision,
release credential, server SSH authority, or user-facing `ppctl` action.
Those runtime and release authorities remain assigned to later PRs.

| Concern | PR 3 owner / outcome | Explicitly not granted in PR 3 |
|---|---|---|
| Artifact policy | `ContainerArtifactCompiler` produces a plan, policy digest, and planned base-image identity. The static preflight returns a canonical `RecipePolicyReceipt`. | A deployment decision or an active configuration revision. |
| Image materialization | `ArtifactBuildExecutor` returns plan-bound OCI/SBOM/provenance evidence; `ContainerArtifactCompiler` rejects an identity, label, recipe-policy, or receipt mismatch. | `docker run`, `docker compose up`, image push, release signing, or credential use. |
| Collaboration application surface | The closed role policy and final-rootfs inventory prove that the current collaboration package exists only in `pp-server-backend`; edge contains no Python collaboration runtime and compute contains no collaboration package or writer configuration. | Event-log construction, listener binding, SQLite schema creation/opening, or durable collaboration wiring. |
| Role package surface | `RolePackageCompiler` copies only the repository-owned source, dependency, and console-script allowlist for one role. Final-rootfs and SPDX inventories reject package paths outside that allowlist and require each role's import foundation. | Installing the complete root development package, treating CPU/CUDA as separately governed packages, or granting authority through package presence. |
| Artifact inspection | `ArtifactBundle` projects immutable/local descriptors, role/variant metadata, and body-free verification receipts. The opaque Model Catalog reference remains in the prepared plan, not the read-only bundle projection. | A proof that any descriptor is present on a host or is production-approved. |
| Runtime application | Deliberately absent. | Listener binding, service activation, endpoint enrollment, scheduling, tunnel setup, migration, promotion, Maintenance, or MCP restart. |

An opaque Model Catalog reference/digest is input evidence only. PR 3 neither
downloads model weights nor interprets a catalog as permission to select a
provider, publish an image, or run a model.

## 2. Secret-free request and inspectable outputs

`ArtifactRequest` contains only build selection data:

- immutable `source_revision` and `package_version`;
- selected target platforms;
- selected compute variants;
- an opaque Model Catalog reference/digest; and
- the selected deployment profile.

It must not carry Docker socket paths, host paths, URLs, SSH material, private
keys, API tokens, registries' credentials, model weights, or arbitrary
metadata. A rejected request returns a stable, sanitized reason rather than
echoing the unsafe input.

`ArtifactBuildPlan` records the endpoint × platform × variant matrix, expected
OCI labels, entrypoints, listener/mount policy, a versioned immutable
base-image catalog entry, recipe-policy digest, collaboration-surface digest,
and policy digest. `ArtifactBundle` keeps only descriptors and inspection
evidence needed by a later verified release/deployment flow, including the
digest of the inspected final-rootfs application inventory. It is not a
`Release Bundle`, an active manifest, or a persisted deployment receipt.

The currently defined selection set is `linux/amd64` and `linux/arm64`; CUDA is
valid only for `linux/amd64`. That is matrix validation, not a statement that
an image for either platform has been built or is available.

Expected OCI labels bind the immutable source revision, package version,
endpoint role, endpoint variant, endpoint-contract revision, the exact
role-authority matrix, selected base-image digest, build-policy digest, and
recipe-policy digest. The
descriptor and its policy digest bind `collaboration_surface_digest`; the OCI
inspector then derives `application_inventory_digest` from the selected image's
final root filesystem and records both values in the image/SBOM/provenance-
bound evidence receipt. Neither digest is presented as a separate OCI label.
The server inventory must equal the closed current collaboration allowlist
published by `endpoint_role_contract(PP_SERVER_BACKEND)`. The container
compiler consumes that same manifest instead of maintaining a second module
list. Merely containing a subset is insufficient, and an extra module fails
closed.
The SBOM attestation must carry a valid SPDX 2.2/2.3 document predicate. The
native BuildKit attestation may be package-level and omit file entries; the
verifier accepts that bounded omission, while any collaboration file entries
it carries must exactly match the final rootfs. RC/stable publication adds the
per-artifact Syft file inventory and binds that complete inventory separately.
The same comparison covers the complete observed `plastic_promise` namespace:
every installed source path must belong to the selected role allowlist, every
required import foundation must be present, and the SPDX package view must
equal the final-rootfs package view. The materializer separately proves that
its staged source tree equals the full allowlist, so a partial synthetic OCI
inventory cannot redefine the package policy.
Compute recipes additionally declare their CPU/CUDA variant, typed
capabilities, and operator-mounted read-only model source. The compiler verifies
the returned label digest against this policy; an adapter cannot replace it
with a label from an arbitrary configuration source.

## 3. Artifact matrix

The compiler keeps public roles distinct even where a later local profile puts
them on the same host. “Allowed runtime data” below is a policy declaration;
it is not evidence of a live bind mount.

The exact `org.plastic-promise.authority` value is part of the descriptor,
Dockerfile, Compose, OCI-label, SBOM/provenance-bound evidence chain:

| Artifact role | Exact authority label |
|---|---|
| `pp-server-backend` | `agent-registry-authority,work-board-authority,canonical-memory-authority,collaboration-event-writer` |
| `pp-local-edge` | `local-edge,bounded-awareness-display,bounded-event-submission` |
| `pp-compute-node` | `compute-execution` |

These labels declare the reviewed role surface; they do not prove that a
registry, work board, event listener, or production runtime is active.

| Artifact role | Variant | Typed boundary | Allowed runtime-data policy | Must be absent from image and role |
|---|---|---|---|---|
| `pp-local-edge` | `standard` | Static Nginx/browser assets, bounded awareness display, and typed bounded-event submission only; it is not a raw-history proxy or MCP authority. | Logical `edge-session-cache` is read-write only for a bounded ephemeral edge cache; no canonical-state mount is eligible. | Python runtime/package, raw collaboration history, Docker socket, SQLite, LanceDB, model weights, private key, arbitrary host command channel. |
| `pp-server-backend` | `standard` | Sole registry/work-board/canonical-memory/event-writer role. It installs the server role allowlist and the current collaboration package as `source-only-unwired`. | The **only** role with logical `canonical-state` read-write eligibility, plus bounded `backend-tmp` tmpfs. This source layer does not construct `CollaborationEventLog`, create its schema, open SQLite for it, or bind a listener. | `plastic_promise.local_inference_node`, local model-worker dependencies, `release_builder`, Docker socket, model weights, user source text baked into layers, compute-node credential, arbitrary host command channel. |
| `pp-compute-node` | `cpu` | One compute role package containing only the root package identity and `plastic_promise.local_inference_node`; it exposes `embedding/v1`, `rerank/v1`, and the optional `structured-json/v1` capability. Structured JSON remains off until its model/revision and bounded provider configuration are activated. | Read-only `model-catalog`, bounded read-write `node-runtime`, and `node-tmp` tmpfs only when a later runtime applies it. | MCP/server, canonical SQLite, memory, knowledge, collaboration, deployment/migration/Maintenance, release builder, LanceDB, Docker socket, private key, credential file, shell/tool administration. |
| `pp-compute-node` | `cuda` | The same compute role package and typed contract as CPU; CUDA changes only the image/runtime variant, including the optional `structured-json/v1` capability when explicitly configured. | The same `model-catalog` / `node-runtime` / `node-tmp` policy as CPU; accelerator details remain internal to the variant. | The same forbidden server/canonical surface as CPU; no model weights are embedded in layers. |

`structured-json/v1` is a first-class compute-node capability in this matrix,
but the label alone does not activate it. The node must advertise a matching
model and immutable revision, and the authenticated server route must pass the
same identity revalidation. The server never constructs or invokes the
provider; when no eligible node is available, structured JSON defers into
retry/reconcile rather than falling back to a server-local provider.

The matrix does not lock a model family. Any future compatible provider must
still satisfy the complete identity tuple established by Endpoint Contract V2:
model name, immutable revision, dimension, normalization, metric,
tokenization, pooling, artifact SHA-256, and golden-vector SHA-256.

## 4. Image and mount safety policy

The compiler makes the following checks inspectable before a later runtime
adapter is allowed to consume a descriptor:

- images exclude canonical databases, LanceDB generations, model weights,
  credentials, private keys, API tokens, runtime state, logs, and build caches;
- all endpoint descriptors require a non-root runtime identity and read-only
  root filesystem in their target recipe;
- `pp-local-edge` cannot receive Docker or canonical-state authority;
- only `pp-server-backend` may declare the *eligibility* for a canonical SQLite
  runtime mount;
- only `pp-server-backend` packages the closed current collaboration source
  surface and `CollaborationEventLog`; this source layer does not call its constructor, create
  `collaboration_events`, open SQLite for it, bind a listener, or connect it to
  MCP/Hook paths;
- compute variants may declare read-only model-cache eligibility and bounded
  scratch space; their role package contains no MCP, SQLite, memory, knowledge,
  collaboration, deployment, migration, Maintenance, or release-builder
  implementation; and
- listener policy is private-container/loopback by default. A build plan does
  not publish a port or establish connectivity.

Host Ollama remains an explicit compatibility adapter outside the image policy.
It cannot receive canonical-state authority and is not a production image
default.

## 5. Source recipe map

The following checked-in source recipes are inputs to the policy; their
presence is not proof of a build or a usable runtime:

| Endpoint artifact | Source recipe / companion file | Declared entrypoint |
|---|---|---|
| `pp-local-edge` | `deploy/local-edge/Dockerfile`, `entrypoint.sh`, `nginx.conf`, and `compose.yaml` | `plastic-promise-local-edge` |
| `pp-server-backend` | `deploy/server/Dockerfile` and `compose.yaml`; a discarded `server-package` stage feeds the monorepo to `RolePackageCompiler`, and the fresh final stage installs only the materialized server allowlist, then removes the installed staging tree and pip's `/app/build` tree | `plastic-promise-canonical-runtime` |
| `pp-compute-node` CPU/CUDA | `deploy/local-inference-node/Dockerfile`, compatibility `compose.yaml`, `compose.cpu.yaml`, and `compose.cuda.yaml`; discarded `compute-package` compiles the one compute allowlist, and the fresh final stage installs it for either variant | `plastic-promise-local-inference-node` |

The Compose files are recipe inputs for later reviewed activation. Their
presence is not an authorization to activate them, bind a listener, create a
tunnel, or generate a runtime asset.

## 6. Static recipe preflight and immutable base-image catalog

The P1 correction introduces a **pure static preflight** over the checked-in
Dockerfiles, Compose files, and `.dockerignore`. It only reads those source
files and emits a canonical `RecipePolicyReceipt`; it does not invoke Docker,
contact a registry, read credentials, start a container, or inspect a host.

The receipt covers the complete three-role recipe matrix and fails closed when
any of the following is false:

- every `FROM` resolves through the versioned base-image catalog to a pinned
  `@sha256` digest; the catalog is source evidence, not a registry lookup;
- each Dockerfile accepts only the checked instruction vocabulary. Edge has one
  static final stage. Server has one discarded `server-package` stage and one
  fresh final stage. Compute has a reusable dependency stage, one discarded
  `compute-package` source stage, and a fresh final stage derived from the
  dependency stage. Both Python recipes invoke the same repository-owned
  `RolePackageCompiler`; they never copy the complete source tree into the
  final stage and do not rely on deletion/whiteout pruning. Any other stage,
  role selection, copy, BuildKit flag, or mount type is rejected; the sole
  allowed mount is the compute recipe's named build-cache mount;
- Dockerfiles reject `SOURCE_REVISION=unknown` and `PACKAGE_VERSION=unknown`,
  retain a final non-root `USER`, and do not use a floating base image;
- CPU/CUDA Compose build arguments carry the concrete source revision, package
  version, selected catalog base-image digest, and selected variant;
- Compose services keep `read_only: true`, expose only loopback/private
  listeners, never mount a Docker socket, and only declare bounded,
  role-appropriate runtime state; and
- every recipe carries exactly its reviewed role-authority label; edge or
  compute claiming any server authority fails closed, and every role rejects
  collaboration writer environment configuration, while the
  each Python recipe must remove its already-allowlisted `/app/plastic_promise`
  staging copy strictly after installation, and the final OCI rootfs inventory
  rejects duplicate or unexpected package paths outside the exact role surface;
  and
- `.dockerignore` excludes canonical SQLite, derived LanceDB, model weights,
  credentials, runtime state, logs, and caches so none can enter an image.

The base-image catalog identity and `RecipePolicyReceipt` digest are included
in the prepared policy. Thus changing an image base, Dockerfile, Compose file,
or relevant ignore boundary invalidates a prior plan rather than silently
reusing its labels or evidence.

## 7. Strict build-versus-runtime split

The following boundary is intentionally mechanical, so a future deployment UI
cannot accidentally treat a build descriptor as authorization:

```text
source + selection
        -> static recipe-policy preflight
        -> RecipePolicyReceipt + pinned base-image catalog entry
        -> RolePackageCompiler -> static/server/compute allowlist package
        -> ContainerArtifactCompiler
        -> ArtifactBuildPlan
        -> ArtifactBuildExecutor
        -> final OCI rootfs application inventory (layer + whiteout semantics)
        -> application_inventory_digest + collaboration_surface_digest
        -> ArtifactEvidenceReceipt set
        -> ArtifactBundle + inspection receipt

ArtifactBundle is input evidence for later work; it is not a runtime command.
```

PR 3 does not grant its local/runtime or production surfaces any of the
following authority:

- start a container, invoke Compose for activation, allocate a runtime GPU,
  bind a listener, or load a model;
- push or publish an OCI image, publish to GHCR/PyPI, sign a release, or use
  release credentials;
- create an SSH or reverse-SSH tunnel, register/enroll a node, or contact a
  remote endpoint;
- mount SQLite, migrate SQLite, promote LanceDB, start Maintenance, restart
  MCP, or write a Deployment Receipt; or
- change production, stable release state, or external runtime configuration.

PR 3 does not add or invoke local runtime activation. It hardens the
pre-existing, operator-invoked Release Builder with a resolved immutable build
identity and post-build label checks. That builder remains outside this
compiler's authority boundary; it is never run by the PR 3 source policy or
protected verification workflow.

A protected CI verification adapter may perform **verify-only**, no-push OCI
work. It runs the same static preflight, requests Buildx SBOM/provenance
attestation layers, and derives/validates a receipt only after their subjects
match the inspected OCI image. It does not publish, apply release signing,
deploy, contact a production host, or create a runtime receipt. No statement
here claims that such a workflow run completed.

PR 4 consumes verified descriptors through the restricted Deployment Center /
host-adapter path. PR 5 owns backup-bound migration, actual runtime mounts,
tunnel activation, LanceDB promotion, and operational restart evidence. PR 6
owns release-readiness packaging and any separately authorized publication.

## 8. Evidence and rollback interpretation

PR 3 evidence is limited to deterministic source/build-policy checks: matrix
coverage, stable rejection reasons, recipe-policy receipt, OCI
label/entrypoint/mount/listener inspection, final-rootfs application inventory,
and fake-executor inspection receipts. One body-free
`ArtifactEvidenceReceipt` is bound to each planned artifact and records its
target, source revision, package version, base-image digest, plan and
recipe-policy digests, `collaboration_surface_digest`,
`application_inventory_digest`, OCI layout/image digest, label digest, and
SBOM/provenance digests. The inventory is computed only after applying the OCI
layers and ordinary/opaque whiteouts. Verification requires the exact,
role-contract-bound collaboration surface in server, rejects any collaboration package
path in edge or compute, and rejects every observed Python package path outside
the selected role allowlist. Server cannot carry the compute-node or
release-builder packages; compute cannot carry MCP, SQLite/memory/knowledge,
collaboration, deployment/Maintenance, or release-builder packages. The SBOM
package namespace must equal the final-rootfs namespace. A receipt set rejects
a missing target, duplicate target, or mismatch with its plan/labels/identity.

`provenance_digest` identifies the Buildx provenance-attestation layer checked
inside the local OCI layout. PR 3 does not claim that this layer was
release-signed, uploaded, GitHub-attested, or accepted by a registry. Likewise,
a CI build smoke, if run, is evidence about that build invocation only; it is
not deployment evidence.

Because PR 3 has no authorized runtime mutation, reverting its source changes
does not require deleting existing images, volumes, model caches, database
files, or host state. Production rollback remains a PR 5 operation with a
verified backup and explicit authorization.

No MCP health, endpoint reachability, local-node registration, container
status, or production state was used as PR 3 evidence; those remain separate
runtime checks.
