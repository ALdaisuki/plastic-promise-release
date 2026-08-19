# Deployment and Runtime Guide

> This is the release-facing deployment entry point. It shares its safety
> invariants with the [Chinese guide](README.zh-CN.md); implementation details,
> templates, and error codes remain in the linked operational references.

> **Normative integrated scope:** the
> [Union Six-PR Contract](../standards/union-six-pr-contract.json), revision
> `2026-08-18.1`, governs both deployment delivery and project collaboration.
> A PR is complete only when every `delivery_scope`, `collaboration_scope`, and
> `required_evidence` item passes; one-sided completion is not PR completion.
> This deployment guide is derived guidance and cannot narrow that contract or
> promote source/test evidence into runtime/production evidence.

## Five ownership rules

1. **SQLite WAL belongs only to `pp-server-backend` under a strict single-writer policy.** It holds memory,
   proposals, tasks, audit, and configuration state. No local edge, compute
   node, cloud provider, or second backend may write the same canonical state.
2. **LanceDB is a rebuildable derived retrieval index.** It may be shadow
   rebuilt, checked, and atomically promoted; it cannot recover or overwrite
   SQLite content.
3. **All local, hosted, and raw provider adapters execute only inside
   `pp-compute-node` and return derived inference.** `pp-server-backend` may
   schedule and validate compute work but cannot construct or invoke an
   embedding, rerank, or structured-JSON provider. Compute has no SQLite,
   LanceDB-promotion, collaboration, or canonical-write authority.
4. **The secret-free Deployment Manifest is deployment truth.** Dashboard and
   Deployment Center run in `pp-local-edge`; PR 4 host `ppctl` performs only
   typed `inspect` / `preview` planning. Any PR 5 mutation is a server-owned
   operation through `pp-core`, not a browser or `ppctl` apply command.
5. **Plan and preflight before installing or migrating.** A failed preflight
   must have no side effects, and a verified online SQLite backup precedes a
   database migration.

The [three-endpoint architecture](../architecture/three-endpoint-deployment/architecture.md)
is the target for the staged PR stack. This guide does not claim that a current
production installation has already migrated to those containers.

**PR 2–4 source status:** `plastic-promise-deployment/v2` defines secret-free
endpoint contracts and typed records; `ContainerArtifactCompiler` adds an
inspectable role/platform/variant policy and immutable evidence boundary; the
read-only `DeploymentCenter` and `ppctl` planning seam add only `inspect` and
`preview` projections.
Legacy V1 manifests are compatibility inputs, not V2 records. The source policy
does not prove an image exists or grant Docker/Compose execution. Runtime
activation, SQLite migration, LanceDB promotion, and Maintenance remain target
work for PRs 5–6. The PR 4 host planning seam is inspect/preview only; see
[profiles and endpoint manifest contracts](profiles.md), the
[three-endpoint architecture](../architecture/three-endpoint-deployment/architecture.md),
and [the PR 3 artifact boundary](../architecture/three-endpoint-deployment/container-artifacts.md).

Before a local or CI builder receives Docker arguments, the checked-in
[static recipe-policy validator](../../scripts/validate_container_artifact_policy.py)
reads the Dockerfiles, Compose templates, `.dockerignore`, and versioned
[immutable base-image catalog](../../deploy/oci-base-images.json). The
[identity resolver](../../scripts/resolve_container_artifact_identity.py) then
selects the exact role/platform/variant entry and emits only its pinned base
reference/digest, source revision, package version, build-policy digest,
recipe-policy digest, and expected OCI labels. A caller must use that resolved
identity rather than supply a floating tag or an independent base image.

This is a source/CI verification contract, not an image availability, signing,
publication, deployment, or production-approval proof.

## PR 4 Deployment Center planning boundary

This is a current source interface, not evidence that a host adapter is
configured or a deployment is running. The edge renders a static,
non-authoritative projection and sends only a secret-free `EndpointManifestV2`
candidate to host `ppctl`. It never sends a host path, legacy SSH-host manifest,
credential, private key, Docker request, or shell command.

`ppctl` has a closed operation allowlist:

```text
inspect -> platform/resource/catalog/status/model/enrollment/receipt projection
preview -> V2 diff, profile recommendation or user override, estimates,
           hard resource refusal, conservative PR 4 update class, and
           inspection-only plan hash
```

`manifest_comparison` is deliberately digest-level only. If the controller
also supplies a safe active-topology projection, `manifest_diff` presents a
redacted structural V2 comparison of profile, module IDs, endpoint IDs, and
compute capability kinds; it never includes a path, transport, credential, or
active manifest body. Without that projection the diff explicitly reports that
it is unavailable. Enrollment readiness is likewise a controller-owned,
secret-free projection rather than enrollment material.

PR 4's `update_class` is inspection-only and can emit only `no-change`,
`enrollment-required`, or `manual-review`. The future executable classes stay
with PR 5. Its plan hash binds the safe observed state as well as the candidate
and profile, so it can report drift, but it is not an activation or execution
token.

The optional edge-to-host bridge is disabled by default. When a host enables
it, the configured base is limited to `http://127.0.0.1:<port>/ppctl/v1` and
JSON `POST`; the edge may only form the two fixed operations, exposes fresh
no-store bridge configuration, and never proxies the host interface.

The profile recommendation is advisory; a supported user override cannot bypass
V2 validation, complete model identity, artifact evidence, or preflight.
`max(20%, 10 GiB)` remains a hard free-space floor on every selected volume.
The resulting plan hash is for display and drift reporting only; it is not an
activation token. In PR 4 there is no apply, enrollment consumption, tunnel,
service action, SQLite mutation, LanceDB promotion, Maintenance action, or
receipt persistence.

## PR 5 migration-operations boundary (durable source / target live adapters)

PR 5 now has a **durable source seam**, while live phase-adapter composition and
runtime activation remain **target**. The server-owned typed
`MigrationOperation` orchestrator is the only intended coordinator for a
systemd-to-container transition. It creates a fresh, secret-free Migration
Operation Plan and validates a separate operation-bound Execution Grant before
mutation. `SQLiteMigrationExecutionJournal` persists server-issued grants,
installation-scoped leases, monotonic fencing generations, terminal state, and
secret-free receipts in canonical pp-core SQLite. Its schema is installed only
by the backup-gated versioned deployment migration. The PR 4 Deployment Center
inspection hash is for drift reporting only and can never authorize execution.

The orchestrator calls fixed edge/compute, canonical-state, runtime,
derived-index, Maintenance, and retention/cache adapters in typed phases:
stage/verify, rehearsal, stop legacy, canonical backup/migration, start
backend, shadow rebuild/verify/promote, enable Maintenance, then policy. Its
bounded rollback disables Maintenance, reverts derived selection, stops the new
backend, restores canonical state only after a successful canonical migration,
and restarts legacy. Adapters receive typed phase inputs and stable reason
codes; they do not receive arbitrary shell, Docker, SSH, or SQLite commands. In the
production composition, `pp-core`/`pp-server-backend` remains the sole canonical
SQLite writer and durable migration-lease holder. Tests and explicitly
non-production composition may use the in-memory journal; it must never be
substituted for the SQLite adapter in production. Deployment Center and `ppctl`
remain read-only.

Mutable `apply` rejects digest-only transport projections and requires the
server-memory topology and artifact bindings that were checked when the plan
was created. The journal rejects concurrent/replayed operations, consumes each
grant exactly once at the first mutable boundary, and refuses stale completion
after lease/fence loss. Expired running work becomes `recovery-required` and is
never silently replayed.

The source contract enforces short-lived plan/grant windows (300-second default,
900-second maximum) and rejects observations older than 120 seconds by default.
It targets five-day production-backup retention and daily temporary cache
cleanup, with LanceDB treated as rebuildable derived state rather than a
recovery authority. The journal durably stores the same secret-free phase
result as its terminal receipt. Until live mutable phase adapters are separately
authorised and composed, no live listener, container, tunnel,
production migration, LanceDB promotion, Maintenance transition, or MCP restart
is verified.

### Governed index identity and project-scoped correction (current source/test)

The canonical index-material migration seam accepts the active governed
compute-node identity explicitly. In a governed route, the migration plan and
its apply-time compare-and-swap bind the target model identity and its SHA-256
digest to the control-plane registration; a legacy environment fallback such
as `fallback-zero` is not a valid substitute. A plan may be inspected with
unresolved index-outbox work only when the immutable outbox watermark, digest,
job count, and active-job count are supplied again at apply.

Ordinary-memory correction remains project-scoped end to end. Its embedding
probe and governed retrieval calls use the memory's `project_id`; they cannot
perform a cross-project identity probe or silently fall back to an unmanaged
provider while a governed route is active. The governed embedder exposes only
bounded usage evidence (request count, estimated input tokens, zero local
cost, and pricing revision) for health and quality receipts.

These are **current source implementation / focused tests passed**. They do
not prove a live migration, a rebuilt or promoted LanceDB generation, a running
MCP/Maintenance service, or production acceptance. After a canonical migration,
the required sequence remains: inspect and apply with matching evidence,
rebuild a fresh shadow generation, verify, reconcile the outbox, atomically
promote, then restart and collect runtime/production receipts.

## PR 6 release-readiness authority (target / unverified)

Windows/WSL2 may be used for local build cache and a GPU smoke test for derived
embedding/rerank only; it is never a canonical-state, release, or publishing
authority. The target protected GitHub workflow creates RC artifacts and, after
separate approval, immutable OCI evidence for a Release Bundle. The target
server may consume only a digest after a separately verified selected-evidence
gate, owns MCP/SQLite/LanceDB, and may later create a bounded receipt. The
current `release-publish.yml` stable publisher is **not yet wired** to consume
or verify the PR 6 Release Bundle, Model Catalog, artifact binding, or RC
attestation; it is not proof of that selected-evidence gate. Neither the bundle
nor this document proves that those actions occurred. Model Catalog references
remain opaque and include no weights, paths, credentials, or endpoint details.

See the full [Release Delivery](../release/delivery.md) target contract.

For the protected **verify-only** PR path, Buildx emits an OCI layout with
`--sbom=true` and `--provenance=mode=max`. The local verifier checks descriptor
hashes, the resolved revision/base-image/build-policy/recipe-policy labels, and
that both Buildx SBOM and provenance attestation layers name the selected
platform image digest as their subject. It does not load the image, contact a
registry, validate a signer or certificate, publish an artifact, deploy a
container, or assert a production trust decision.

## Endpoint profiles (source contract; runtime evidence pending)

| Profile | Endpoint placement | Canonical state | Inference responsibility |
| --- | --- | --- | --- |
| `local-all-in-one` | All three endpoint containers on one host | `pp-server-backend` only | Managed `pp-compute-node`, local by default, advertises only its configured exact capabilities |
| `local-cloud` | Local edge and backend on one host, with a managed compute execution plane | `pp-server-backend` only | `pp-compute-node` executes configured hosted embedding, rerank, and structured JSON; provider credentials never enter the server |
| `split-accelerated` | Edge, server, and compute on separate hosts | Server `pp-server-backend` only | Registered `pp-compute-node` may be `local`, `cloud`, or `hybrid` when capability and model identity match exactly |

A profile changes placement, never ownership. Every profile retains project
isolation, durable outbox admission, failure reasons, retries, and reconcile.
Local and cloud embedding can be fallback peers only when model, fixed
revision, dimension, normalization, distance metric, tokenization/pooling
contract, artifact hash, and golden-vector evidence match. Otherwise the
profile selects one identity and switching requires a shadow rebuild and atomic
promotion.

### Current compute provider and structured-JSON settings

The checked-in CPU, CUDA, and compatibility Compose templates expose
`embedding/v1`, `rerank/v1`, and `structured-json/v1` from
`pp-compute-node`. This is a current source capability, not proof that a cloud
provider, live node, or production route has been activated. Structured JSON is
disabled by default until its backend, model, and fixed revision are configured.

| Purpose | Compose environment | Contract |
| --- | --- | --- |
| Execution plane and routing | `PP_ENDPOINT_ROLE=pp-compute-node`; `PP_LOCAL_NODE_PROVIDER_MODE=local|cloud|hybrid` | The provider mode must match the configured local/cloud backend mix; a mismatch fails closed. |
| Compute-only credential | `PP_LOCAL_NODE_CLOUD_API_KEY` | Supplied only to the compute projection; it is write-only from the control plane and must not appear in identity, response, diagnostic, or receipt data. |
| Hosted embedding | `PP_LOCAL_NODE_EMBEDDING_BACKEND=cloud|openai-compatible`; `PP_LOCAL_NODE_EMBEDDING_CLOUD_BASE_URL`; `PP_LOCAL_NODE_EMBEDDING_CLOUD_PATH` (default `/embeddings`) | Model, revision, dimension, and normalization remain mandatory identity fields. |
| Hosted rerank | `PP_LOCAL_NODE_RERANK_BACKEND=cloud|openai-compatible`; `PP_LOCAL_NODE_RERANK_CLOUD_BASE_URL`; `PP_LOCAL_NODE_RERANK_CLOUD_PATH` (default `/rerank`) | The server receives only bounded scores/receipts; unavailable routing preserves original order. |
| Structured JSON | `PP_LOCAL_NODE_STRUCTURED_JSON_BACKEND=off|cloud|openai-compatible`; `PP_LOCAL_NODE_STRUCTURED_JSON_MODEL`; `PP_LOCAL_NODE_STRUCTURED_JSON_REVISION`; `PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_BASE_URL`; `PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_PATH` (default `/chat/completions`); `PP_LOCAL_NODE_MAX_STRUCTURED_TOKENS` (`0` = no additional local ceiling) | When enabled, model/revision identity and bounded prompt, payload, output, timeout, and UTF-8 limits are enforced on the compute node. Token requests remain provider-adaptive. |

For `cloud` or `hybrid`, the active project control revision must atomically
activate the compute profile and record a matching identity-revalidation
receipt before new work is dispatched. Mode changes affect new work only;
in-flight leases and receipts remain bound to their original identity. No
source configuration or focused test establishes runtime or production
evidence.

Structured-JSON intent/schema pairs resolve through a closed registry owned by
the compute node; the server never constructs provider prompts. Embedding and
structured-JSON deferral uses a content-free durable retry marker, and the
caller rehydrates raw input when it retries. Canonical SQLite retains only
bounded intent, identity, digest, failure, and receipt references. Scheduling
consumes an explicit identity-revalidation receipt and never manufactures one
from cached health.

#### Passive semantic capture route

With `PP_PASSIVE_SEMANTIC_CAPTURE=shadow|on`, a Stop Hook first persists
eligible user text as durable work. The server worker submits its bounded batch
through the registered node using
`plastic-promise/structured-json/passive-semantic-v1` and
`plastic-promise/structured-json/passive-semantic-memory-v1`. The node owns
the fixed schema prompt, local/cloud provider selection, URL, and credential;
the user text remains untrusted data and cannot replace that contract.

Passive semantic work defaults to a 32K request budget so a reasoning-capable
structured-JSON model has room to finish its private reasoning before emitting
the final JSON object. There is no local 8192-token ceiling anymore:
`PP_PASSIVE_SEMANTIC_MAX_TOKENS` can be raised as needed without a Plastic
Promise token cap. Prompt/payload/output byte limits, provider policy, request
timeouts, retries, and queue capacity remain the safety boundary. Production
acceptance must prove that `message.content` contains the final strict JSON;
never treat `reasoning_content` as the result, store it, or promote it into
memory.

Use the authenticated Control Dashboard/API transaction to bind the allowed
node and the exact embedding, rerank, and structured-JSON identities before
enabling this route. Never edit `managed.env` or the Control SQLite file by
hand. If identity admission or routing is unavailable, canonical SQLite stays
unchanged: embedding degrades to text-only, while the semantic work retries and
reconciles without a server-local provider or automatic memory adoption. A
validated `on` result creates only a pending proposal; promotion gates remain
separate.

The operator CLI performs the same authenticated ETag/idempotency transaction.
For a node with local embedding/rerank and hosted structured JSON, bind all
three observed identity digests in one hybrid revision:

```bash
.venv/bin/python scripts/activate_compute_node_routing.py \
  --token-file /root/.config/plastic-promise/control.token \
  --node-id <technical-node-id> \
  --embedding-identity <sha256-embedding-identity> \
  --rerank-identity <sha256-rerank-identity> \
  --structured-json-identity <sha256-structured-json-identity> \
  --embedding-model <model> \
  --embedding-revision <fixed-revision> \
  --embedding-dimension 2560 \
  --rerank-model <model> \
  --rerank-revision <fixed-revision> \
  --structured-json-model <model> \
  --structured-json-revision <fixed-revision> \
  --inference-mode hybrid
```

The three structured-JSON arguments are an atomic group. Omitting the complete
group selects the local-only default and clears any stale structured-JSON pin;
requesting `hybrid` without the group fails closed. Identity values must come
from the authenticated node observation/receipt rather than model aliases.

All endpoint containers, generated native runtime assets, repository-provided systemd units and
drop-ins, and the browser timestamp formatter use `TZ=UTC`. Canonical timestamps stay
timezone-aware UTC regardless of placement. This is a logical runtime/display policy only: the
installer never changes the host timezone, mounts `/etc/localtime`, invokes `timedatectl`, or
invokes Windows `Set-TimeZone`. After installing or updating a native unit, run `systemctl
daemon-reload`, restart the affected service, and verify only its `TZ` process variable rather than
dumping the complete environment.

## Standard deployment order

1. Copy a no-secret manifest template and declare profile, optional modules,
   resource locations, and a measured `resource_budget`. Keep keys, private
   endpoints, tunnel credentials, model paths, and provider tokens out of Git.
2. Generate an operation-bound `plastic-promise deploy plan` and retain its
   `sha256:` hash. Planning is read-only and is not an install, upgrade, or
   restore operation.
3. Run `preflight`. It measures backup/WAL/SHM, image, model-cache, LanceDB
   shadow rebuild, rollback coexistence, and migration space on their actual
   filesystems. It rejects any selected volume projected below
   `max(20%, 10 GiB)` free space.
4. For an upgrade, create and verify an online SQLite backup, including
   `integrity_check` and SHA-256, then apply only a reviewed versioned
   migration that is actually required.
5. Start exactly one canonical lock-holder through the platform-supported
   launcher. Native and Compose runtimes must never open the same SQLite state.

6. Verify MCP initialization/tool listing, project-isolated recall, outbox
   reconcile, degraded behavior, and the runtime lock. Rebuild a LanceDB shadow
   generation and pass quality/isolation gates before atomic promotion.

## Fixed-revision native server cutover

For the repository-provided native systemd deployment, use the guarded cutover
runner instead of teaching the production server how to authenticate to GitHub:

```bash
python scripts/deploy_server_revision.py \
  --ssh-target root@server.example \
  --expected-current-revision <current-40-hex-sha> \
  --revision <target-40-hex-sha>
```

The default invocation prints a no-secret plan and makes no changes. Repeat it
with `--apply` only after reviewing that plan. The runner requires a clean local
and remote worktree, proves the target is a fast-forward descendant, transports
only a prerequisite-bound offline Git bundle, creates an online canonical
SQLite backup with `integrity_check` and SHA-256 evidence, backs up installed
units, installs the repository units, restarts only services that were already
active, and accepts health only when the reported source revision equals the
requested SHA. The health observation is bounded to ten seconds. Any failure
after checkout restores the previous revision and unit files and restarts the
previously active services. It never changes the host timezone and it removes
the exact remote temporary bundle after success.

This runner is for a same-dependency source revision. If the reviewed release
changes the Python environment, wheel, database schema, or OCI digest, use the
corresponding release-bundle or migration workflow first; do not smuggle an
unreviewed install command into the cutover.

## `pp-compute-node`

The compute endpoint is an accelerator, not a data replica or another MCP
server. It is the target container form of the existing local heterogeneous
inference-node contract. It may run with Docker/Compose on Windows/WSL2, Linux,
or macOS, subject to these rules:

- Its API listens on `127.0.0.1`; the server-side reverse forward is also
  loopback-only. Never publish node inference, MCP `:9020`, or tunnel endpoints
  to a LAN or the public internet.
- Registration, health evidence, and returned work bind complete identity:
  **model name, fixed revision, output dimension, normalization, metric,
  tokenization, pooling, artifact hash, golden-vector proof, and transport
  evidence**. Equal dimensions alone do not make vectors interchangeable.
- Models mount read-only. The node stores no user memory, SQLite, LanceDB, or
  canonical-write credential.
- A tunnel account permits only required forwarding: no shell, sudo, SFTP,
  agent/X11 forwarding, or public forwarding. Use `ServerAlive`,
  `ExitOnForwardFailure`, and supervised restart recovery.

Changing model, revision, dimension, normalization, metric, or artifact requires
fresh identity evidence and a rebuild of the corresponding derived generation.
When local and cloud identities differ, they cannot both serve the same active
generation: `split-accelerated` keeps local, while `local-cloud` keeps cloud.

If local compute fails, an explicitly configured, enabled, healthy, and
identity-compatible cloud provider may be used with visible fallback evidence.
If cloud is absent or also fails, derived work remains queued, recall uses the
current verified generation plus text/BM25/symbolic retrieval, and the backend
polls configured providers. Routing returns only after consecutive identity and
capability probes pass a stable recovery window.

### One-click compute-node build (generic)

The one-click build scripts auto-detect the source revision (git HEAD), Docker,
and CUDA (`nvidia-smi`), resolve the compute variant, generate a
non-secret compose `.env` with pinned model identity, run the immutable local
build, start Compose, and record performance evidence. No machine-specific
user, path, model, or revision is hard-coded: every value is auto-detected or
must be supplied through the operator profile / `PP_LOCAL_NODE_*` environment
variables.
After the immutable build, the script completes the compose `.env` with the
container identity resolved during the build (base image reference/digest,
source revision, package version, and build/recipe policy digests), aliases the
built image to the compose image name, and starts Compose with `--no-build` so
the verified image is never rebuilt.

External llama.cpp workers require `PP_LLAMA_CPP_IMAGE` as a registry
reference pinned with `@sha256:<64-hex>`; floating tags are rejected. The
Windows environment writer also requires independently computed embedding and
rerank artifact SHA-256 values, stores them beside the expected model identity,
and replaces inherited ACLs with current-user, SYSTEM, and local Administrators
access only. Windows and server smoke compare observed artifact digests to
these expected values rather than accepting a digest-shaped self-assertion.

```bash
# POSIX (macOS / Linux / WSL2)
./scripts/build_compute_node.sh

# Windows (PowerShell)
./scripts/build_compute_node.ps1
```

The build is the same governed path as the historical Windows/WSL2 preflight:
it crosses into the selected WSL2 distribution (or native Docker Desktop),
observes CPU, memory, GPU, BuildKit/model locks, and disk capacity for 10
seconds before creating Buildx, cleaning a cache, or building an image. A busy
result is `deferred_resource_busy`: it does not queue, clean, or create a
builder. It then performs the bounded Plastic Promise cleanup. That cleanup
does **not** prune containers, volumes, models, databases, networks, or
another project's images; it does not push to GHCR. CUDA builds make room for
the selected llama.cpp workers. An Ollama stop/restore stage exists only when
the operator explicitly selects the legacy `ollama` compatibility backend; it
is never part of the default path.

Both platform scripts use the dedicated generic `plastic-promise-local` Buildx
builder, keep recent project-owned cache for 24 hours, resolve the immutable
base/policy identity before Buildx, and verify the image labels for source
revision, base-image reference/digest, build-policy digest, and recipe-policy
digest. This local label check does not replace protected CI's OCI-layout
SBOM/provenance subject validation and is not a signing, publication,
deployment, or production proof. A CUDA container smoke test is required by
default, but `--skip-gpu-smoke` / `-SkipGpuSmoke` is an explicit degraded
operator override: its report is **not** GPU-smoke evidence and cannot support
GPU-node readiness or a release decision. Local reports stay under
`artifacts/local-node-build/`.

The image build resolves Python dependencies through PyPI. Operators behind
restrictive or lossy network paths can point the build at a reachable mirror
with `--pip-index-url` / `-PipIndexUrl` (for example
`https://mirrors.aliyun.com/pypi/simple/`); the value is embedded as an
immutable build argument, and the usual hash-verified install still applies.

Model identity is never hard-coded in the repository. The operator profile
must declare the embedding model, its fixed revision, the output dimension and
normalization,
and the rerank model with its fixed revision; missing values fail closed with
explicit remediation. After Compose starts, the one-click flow runs
[`scripts/pp_node_smoke.py`](../../scripts/pp_node_smoke.py): it verifies
`/health`, `/v1/identity`, embedding dimension and L2 normalization, and a
bounded rerank batch, records median latency evidence, and writes a
doctor-compatible `runtime-status.json` (`plastic-promise/local-inference-runtime-status/v1`).
The smoke reads the private node authorization from the ACL-protected compose
environment only to authenticate these probes; it never writes the value to a
report or log, and ignores the structured-JSON cloud credential entirely.

### Initial-deployment bootstrap integration

During the initial deployment phase, the deployment controller exposes the
same one-click build as an explicit operator command:

```bash
plastic-promise-deploy build-node --dry-run   # print the resolved command
plastic-promise-deploy build-node             # build, start, and smoke
```

It resolves the checked-out source revision (or `--source-revision`), detects
the platform and variant, and executes the matching platform script. Use
`--no-start` for a build-only run, `--skip-gpu-smoke` for the explicit degraded
override, and `--node-config` / `--runtime-status` to pin where the compose
`.env` and doctor evidence are written. The `split-accelerated` profile's
initial deployment therefore has a documented build step after `apply`, before
the node is registered for routing.

### Persistent Windows compute-node bootstrap

[`scripts/setup_windows_compute_node.ps1`](../../scripts/setup_windows_compute_node.ps1)
turns the ad-hoc host recovery and node build into an idempotent, persisted
bootstrap. Given an exact source revision and a node-local operator profile (see
[`windows-compute-node.env.example`](../../deploy/local-inference-node/windows-compute-node.env.example)),
one invocation registers three scheduled tasks:

| Task | Purpose |
| --- | --- |
| `PPOllamaServe` | Serves the local Ollama registry (SYSTEM, restart-on-failure, `OLLAMA_HOST=0.0.0.0:11434`) |
| `PPNodeModelSync` | Pins and downloads the rerank model tree at the exact HF revision into the read-only `/models` source |
| `PPNodeBuild` | Runs the immutable CUDA image build through Docker Desktop or the detected WSL2-native daemon under the interactive user |

```powershell
# Host-only preflight. Add -MigrateVhdxTo D:\WSL only when the move is intended.
./scripts/preflight_windows_node_host.ps1 `
  -ProfilePath D:\PlasticPromise\node.env `
  -OutputPath D:\PlasticPromise\logs\preflight-report.json

# Persist Ollama, model sync, build, and the compose identity.
./scripts/setup_windows_compute_node.ps1 `
  -SourceRevision <exact-40-character-source-sha> `
  -ProfilePath D:\PlasticPromise\node.env

# Start the resolved compose variant and run identity/embedding/rerank smoke.
./scripts/setup_windows_compute_node.ps1 `
  -SourceRevision <exact-40-character-source-sha> `
  -ProfilePath D:\PlasticPromise\node.env `
  -Stage verify
```

`verify` first finalizes the private compose EnvironmentFile through
`configure_windows_compute_env.ps1`: it preserves the immutable image/build
identity, binds the exact embedding and rerank artifact SHA-256 values and
model-file references from the operator profile, and removes inherited ACLs.
For a mixed node, pass `-StructuredJsonBackend openai-compatible` together
with a model, a pinned 40-hex or `sha256:` deployment revision, the real HTTPS
API root, and `-CloudApiKeyFile`. The script writes `hybrid` provider mode,
uses the same `..._STRUCTURED_JSON_CLOUD_BASE_URL/PATH` names as Compose and
Control, applies the private ACL, and removes the one-time key file. A chat
provider must echo the exact model; if it omits a revision, the configured
revision remains a deployment attestation rather than a claim that hosted
model weights are immutable. An echoed revision must match exactly.
The cross-platform smoke then compares the observed node ID, model revisions,
artifact digests, vector shape/normalization, and rerank direction against that
same file. `PP_LOCAL_NODE_ID` is a technical Control identifier and therefore
uses lowercase ASCII such as `inference-node`; **推理节点** remains the
localized Dashboard label, not the protocol ID.

The preflight detects Docker Desktop and WSL2-native Docker. For the latter it
records `PP_DOCKER_COMMAND=wsl.exe -d <distro> -e docker`; build and verification
use that prefix directly, so the loopback `socat` context is opt-in through
`-EnableDockerBridge` and is never a correctness dependency. It checks
system-disk headroom, discovers the selected distribution's VHDX through the
Lxss registry, optionally moves it, merges and updates adaptive
`memory`/`processors`/`swap` values in `.wslconfig`, configures systemd in the
distribution-owned `/etc/wsl.conf`, and enables/starts `docker.service` as root.
Profile values override existing managed resource keys as well as adaptive
defaults. A legacy `[boot]` block previously written to `.wslconfig` is converted
to comments before systemd is moved to the correct file.

Connectivity is tested from the selected runtime, not only from Windows. If
WSL direct access fails, loopback proxy candidates are translated to the WSL
host gateway when required and verified with a real HTTPS request. The selected
proxy is installed in `/etc/profile.d/pp-proxy.sh`, in a Docker systemd drop-in,
and passed explicitly to BuildKit build arguments. A failed VHDX move, an
unready Docker service, an unsafe low-space VHDX placement, or failed required
proxy configuration makes the JSON report `ready=false` and returns non-zero.

The bootstrap writes `deploy/local-inference-node/.env` (compose runtime
identity; gitignored) and derives the Ollama embedding digest from `/api/tags`
when `PP_LOCAL_NODE_EMBEDDING_REVISION` is empty. The build resource gate defers
while model sync is active, so the operator waits for
`PP_NODE_MODEL_SYNC_COMPLETE` in `D:\PlasticPromise\logs\model-sync.log` and
then re-runs the build task (`PPNodeBuild`) or `-Stage build`. Staged re-entry
is supported: `-Stage preflight|ollama|models|build|env|verify`.

The checked-in compute Dockerfile contains `python3-dev`, `gcc`, and `g++` for
Triton JIT. If an older cached image lacks them, the Windows build performs a
small overlay repair instead of rebuilding the full dependency graph, verifies
the repaired image, and then applies the CUDA/CPU compose alias even when
`-NoStart` is selected. CUDA compose mounts
`/tmp` with `exec`; changing it back to `noexec` breaks Triton-generated shared
objects and is rejected by the asset contract tests.

Python, the Ollama executable, the Ollama model directory, and the interactive
user profile are auto-detected and can be overridden through the profile
(`PP_PYTHON_EXECUTABLE`, `PP_OLLAMA_EXECUTABLE`, `PP_OLLAMA_MODELS_DIR`,
`PP_WINDOWS_USER_PROFILE`). Docker selection and host sizing can be overridden
with `PP_DOCKER_COMMAND`, `PP_WSL_DISTRO`, `PP_WSL_VHDX_TARGET`, `PP_PROXY_URL`,
`PP_WSL_MEMORY`, `PP_WSL_PROCESSORS`, and `PP_WSL_SWAP`; the
`D:\PlasticPromise\remote-builds\<SHA>\source` layout remains the repository's
immutable Windows builder contract.
`PP_PROXY_URL` accepts only credential-free absolute HTTP(S) URLs. Every public
Windows build entry rejects URI userinfo before exporting proxy environment
variables or constructing Docker/BuildKit arguments.

This remains operator tooling, not an installer: it never creates a tunnel,
contacts the governed server, promotes a generation, or persists canonical
state.

### Production generation cutover

[`scripts/cutover_lancedb_generation.py`](../../scripts/cutover_lancedb_generation.py)
implements two explicit phases. `prepare` loads the canonical database and
generation root from the runtime EnvironmentFile (unless the operator supplies
explicit paths), and fails closed if either identity is absent. It then builds,
reconciles, and verifies an inactive candidate.
It requires a live quality report produced under the exact managed/revision
environment. `cutover` runs only after a separately authorized host operator
has stopped MCP, inference gateway, Maintenance, and Knowledge Ingest. It may
activate an immutable staged Control revision through Bearer-authenticated,
ETag/Idempotency-Key CAS; it then promotes, retargets Control through the same
authenticated API, bootstraps and verifies the generation-bound live root, and
atomically updates only the generation/live-root EnvironmentFile pointers.

Every generation command drops root privileges to the owner of the control
root, so generation material is not accidentally published as `root:root
0600`. The default is a JSON plan with zero writes. The script never restarts a
service, changes Maintenance policy, creates a quality report, or edits Control
SQLite directly. Stop, restart, post-cutover smoke, and any Maintenance
transition remain separate host-authorized operations.

```bash
.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase prepare \
  --generation-id <new-generation-id> \
  --quality-report /srv/plastic-promise/state/quality/<live-report>.json \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json

.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase prepare \
  --generation-id <new-generation-id> \
  --quality-report /srv/plastic-promise/state/quality/<live-report>.json \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json \
  --apply

# When embedding identity changes, prepare under the staged revision too.
.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase prepare \
  --generation-id <new-generation-id> \
  --quality-report /srv/plastic-promise/state/quality/<live-report>.json \
  --revision-id <revision> \
  --revision-env /srv/plastic-promise/state/control/revisions/<revision>.env \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json \
  --apply

# Separately stop the required runtime units, then review the cutover plan.
.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase cutover \
  --generation-id <new-generation-id> \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json \
  --token-file /root/.config/plastic-promise/control.token

.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase cutover \
  --generation-id <new-generation-id> \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json \
  --token-file /root/.config/plastic-promise/control.token \
  --revision-id <revision> \
  --revision-env /srv/plastic-promise/state/control/revisions/<revision>.env \
  --evidence-file /srv/plastic-promise/state/evidence/<revision>.json \
  --apply
```

The prepare phase writes a private, atomic receipt binding the generation ID,
generation manifest bytes, declared manifest/index-tree digests, quality-report
path and digest, revision ID, and revision EnvironmentFile digest. Cutover
recomputes those identities and refuses a missing, replaced, or mismatched
candidate/receipt. When an embedding revision
changes, add the same `--revision-id` and `--revision-env` to **both** phases so
build/reconcile/verification load the staged environment after the base and
managed EnvironmentFiles; add `--evidence-file` to cutover for activation.
After cutover, the independently authorized host
operator restarts the required units and verifies MCP `/health`, `runtime_mode`,
`context_supply`, `memory_recall`, the private compute transport, and the
Windows/WSL2 node smoke before reviewing or changing Maintenance state. A
failed step leaves later steps unapplied; canonical SQLite remains the sole
truth source.

## Operational references

- [Profiles and endpoint manifest contracts](profiles.md) · [中文](profiles.zh-CN.md)
- [Startup and runtime modes](startup-modes.md)
- [Cross-platform deployment controller](deploy-controller.md)
- [Resource planning and hard gate](resource-planning.md)
- [Configuration baselines](config-baselines.md)
- [Local heterogeneous inference node](local-inference-node.md) · [中文](local-inference-node.zh-CN.md)
- [Troubleshooting](troubleshooting.md)
- [Controlled release and production promotion](../release/delivery.md)

## Resource and cost evidence

All capacity figures use binary `GiB` and describe usable free space before the
controller reserves `max(20%, 10 GiB)` on each touched volume. The authoritative
planning assumptions are in [resource planning](resource-planning.md).

This repository does not freeze provider prices, network egress rates, registry
retention fees, or CI-minute prices. Those are dynamic external evidence. A
cost estimate must record provider/catalog revision, region and currency,
observation time, selected model identity, expected request volume, and cache/
fallback assumptions. A stale copied price is not a deployment fact.

Cache cleanup is separately planned and authorized: retain active and verified
rollback revisions, plan only unreferenced artifacts idle for at least 24 hours,
and run resource preflight again before a later pull, build, or unpack.
