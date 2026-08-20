# Local heterogeneous inference node

> **PR 5 source status — runtime contract implemented; production activation is
> separately evidenced.** This page defines the compute-node identity,
> admission, lease, and result-record boundaries. A source build may expose
> the governed listener and adapters, but deployment, migration, tunnel
> registration, and production acceptance still require their own receipts.

`split-accelerated` keeps SQLite, LanceDB, outbox, leases, audit, promotion,
and project isolation on the governed server. A local heterogeneous inference
node returns bounded derived inference only. The following are logical contract
operations, not PR 2 listener routes:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness and protocol version |
| `GET /v1/identity` | Complete fixed embedding/rerank identity evidence, including a declared dimension rather than a fixed model-specific dimension |
| `POST /v1/embeddings` | Up to 64 bounded text inputs |
| `POST /v1/rerank` | Scores every bounded candidate exactly once |
| `POST /v1/structured-json` | Optional bounded structured result, only after model/revision activation and identity revalidation |

The node has no SQLite, LanceDB, MCP, dashboard, task-queue, lease, outbox, or
canonical-memory dependency. The server scheduler validates its identity and
chooses whether derived output is usable; a node can never promote or write a
memory directly.

The V2 endpoint manifest does not contain `ssh_host`, a private address or URL,
filesystem path, tunnel material, credential, or authorization value. It uses
opaque `transport_ref` and `resource_policy_ref` labels only. Legacy V1 inputs
remain distinct compatibility material; see [profiles and endpoint manifest
contracts](profiles.md) for the exact V1/V2 boundary.

## Target server-side admission and scheduling (PRs 4–5)

The governed-server adapter will own `NodeGovernanceStore`. It may accept a local node only
after the `split-accelerated` deployment manifest has declared its node ID and
the server has independently verified a transport evidence digest **and**
recorded a server-produced verification receipt. The server-owned canonical record
binds that receipt to either the resolved deployment ID or a controlled
configuration revision. The store keeps an opaque transport label and digest,
never an operator-entered endpoint or credential. A private tunnel may rotate
while the pinned model identity is unchanged; an observed model, revision,
dimension, normalization, metric, tokenization, pooling, artifact,
golden-vector, or reranker identity drift automatically quarantines the node.
A later matching health proof records recovery and is required before
the scheduler can use it again.

The server-owned SQLite `DerivedWorkStore` remains the sole durable task outbox:
it persists project-scoped canonical references, idempotency, lease-token
hashes, failure reasons, retry windows, and reconciliation state. Node
governance adds only registry evidence and a short-lived reservation bound to a
derived-work fencing generation; it does **not** create a second task queue.
The server is the only process that can bind a durable lease to a node.
Effective capacity is conservative: it combines the node's reported free slots
with unexpired server reservations, so a health refresh cannot oversubscribe a
node.

The registry schema is additive but is never initialized by MCP or the control
plane. Until the explicit backup-and-migration flow is available in PR 5, a
runtime with no node-governance schema reports `schema_missing` and fails closed
for node registration and scheduling. This protects canonical SQLite from a
status request becoming an implicit migration.

Scheduling never treats matching dimension alone as compatibility:

- embedding accepts a local, Ollama, or cloud route only when the complete
  embedding model/revision/dimension/normalization identity matches the active
  generation;
- rerank uses its own model identity and has an explicit `original-order`
  terminal fallback;
- structured JSON is a first-class `node_routing` capability. The authenticated
  compute-node transport receives only the bounded, intent-bound payload and
  must return the exact configured model/revision identity. It remains disabled
  until the structured-json profile is activated and revalidated. If no healthy
  matching node is available, the operation defers into retry/reconcile; the
  server does not call a cloud provider or a deterministic local fallback and
  the node has no direct canonical-memory write capability;
- `fastest-estimated` uses median successful latency plus queue/capacity only
  after at least 20 successes for the same node, operation, and identity;
  before that it deterministically falls back to `remote-node-first`;
- `pinned-node` never silently falls through to another node.

`accelerator-max` remains server-governed and off by default. Its admission
interface accepts only bounded non-generative derived work with hard
concurrency, queue, daily-work, and memory budgets. Its queue and UTC-day
admission counter are checked in the same SQLite write transaction as job
creation; leases enforce concurrency across process restarts. Before claiming
any background task, the scheduler yields globally to foreground embedding and
rerank work and requires fresh capacity evidence. The only accepted task kinds
are index compensation, vector-relation candidates, semantic-deduplication
candidates, conflict-risk candidates, preclassification, and scoring evidence.
Results are bounded proposal/outbox/evidence/derived artifacts. They cannot
write canonical memory or promote a LanceDB generation.

The later control-plane status response exposes a bounded non-secret registry
projection and derived-work counters. The authenticated Dashboard's **推理节点**
view additionally shows per-node health freshness, declared and observed
capabilities, expected embedding/rerank identity, dimensions, available
capacity, bounded latency aggregates, quarantine reason, stable recent routing
and degradation codes, node/accelerator derived-work queue counts, and the
accelerator UTC-day admission count plus bounded durable lifecycle audit. Job
lifecycle records are projected from the same outbox/lease ledger; denied or
deferred policy gates come from a separate daily-deduplicated decision ledger.
That ledger is not a task queue and exposes only task kind, audit/lifecycle
code, stable reason code, and time. The explicit node-governance migration
creates it; neither Dashboard nor MCP startup repairs or creates it. Its
configuration summary is read from the existing safe configuration projection;
it cannot register an address, alter a private endpoint document, or bypass a
controlled configuration revision.

Neither control-plane surface discloses endpoints, credentials, transport
evidence, raw health payloads, user content, task input references, result
payloads, project identifiers, subjects, providers, lease tokens, or provider
responses.

When an operator explicitly chooses **脱敏诊断** in a later authenticated Dashboard,
the control plane generates a browser-local JSON download from a strict
allowlist. It contains only stable component states, bounded counters and
configuration-presence booleans. It does not send telemetry, and it cannot
contain node IDs, model or revision values, endpoint/host/port data,
filesystem paths, configuration values, credentials, request/task payloads or
SQLite rows. Generating this bundle neither creates a revision nor changes any
runtime, database or deployment state.

## Target server-private bootstrap boundary (PRs 4–5)

When a later active `node_routing` revision is enabled, MCP startup will perform a
fail-closed bootstrap before routing either a memory-index outbox item or a
foreground rerank to a node. The Maintenance daemon applies the same bootstrap
to its independently created engine, so canonical index replay cannot bypass
the controlled route:

1. it opens the existing control-plane store read-only and requires the active
   revision plus a matching `split-accelerated` deployment manifest;
2. it loads the server-only endpoint document named by
   `PP_NODE_PRIVATE_ENDPOINTS_FILE`; the file must be absolute, regular,
   non-symlinked, and `0600` on POSIX;
3. the document contains only an opaque node ID, opaque transport ID, and a
   `127.0.0.1` / `::1` tunnel URL. Authorization is required and referenced by
   a `PP_NODE_AUTH_*` environment variable rather than written in the document;
4. the server discovers identity through the tunnel, checks the complete
   embedding and rerank identity against the active revision, then probes it
   again while issuing the controlled registration receipt and observing
   health.

The endpoint document is never a configuration revision, Dashboard value,
receipt payload, public status field, or log value. If any bootstrap check
fails, canonical SQLite writes continue and the corresponding index outbox
remains durable/retryable, but the process installs a blocked derived-index
route rather than silently invoking the legacy ungoverned embedder.

Foreground rerank uses the same verified registration, capacity reservation,
full rerank-identity re-probe, and latency evidence as durable index work. Its
live query and candidate text remain process-local and are not copied into
SQLite merely to schedule it. If no eligible node is available or the private
call fails, the operation records a bounded defer/retry reason and uses the
contracted terminal ordering policy; the server does not invoke a cloud or
server-local provider. An execution-time identity or response-identity drift
immediately quarantines the node; a subsequent healthy matching probe is
required before it can rejoin selection.

The private document schema is intentionally small:

```json
{
  "schema": "private-node-endpoints/v1",
  "nodes": [
    {
      "node_id": "opaque-node-id",
      "transport_id": "opaque-transport-id",
      "base_url": "http://127.0.0.1:port",
      "authorization_env": "PP_NODE_AUTH_EXAMPLE"
    }
  ]
}
```

The value shown for `authorization_env` is a name, not a credential. This
runtime asset is prepared by a future deployment apply step; it must not be
committed to a repository or copied into a control revision.

## Target node-local configuration (PR 3)

PR 3 makes this a source-level artifact-policy input, not an installation or
activation instruction. If a later runtime adapter prepares a node-local
environment file, both model revisions must be fixed identifiers, not
`latest`, `main`, or `stable`.

```text
PP_LOCAL_NODE_ID=workstation-inference
PP_LOCAL_NODE_AUTHORIZATION=Bearer <random-private-node-token>
PP_LOCAL_NODE_EMBEDDING_BACKEND=llama.cpp
PP_LOCAL_NODE_EMBEDDING_MODEL=Qwen3-Embedding-4B-GGUF
PP_LOCAL_NODE_EMBEDDING_REVISION=<fixed-40-hex-revision>
PP_LOCAL_NODE_EMBEDDING_DIMENSION=2560
PP_LOCAL_NODE_EMBEDDING_NORMALIZATION=l2
PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL=http://127.0.0.1:19131
PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH=/v1/embeddings
PP_LOCAL_NODE_RERANK_BACKEND=llama.cpp
PP_LOCAL_NODE_RERANK_MODEL=Qwen3-Reranker-0.6B-GGUF
PP_LOCAL_NODE_RERANK_REVISION=<fixed-40-hex-revision>
PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL=http://127.0.0.1:19132
PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH=/rerank
PP_LOCAL_NODE_MODEL_CACHE_DIR=/models
PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=/models/embedding/<exact-model>.gguf
PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=/models/rerank/<exact-model>.gguf
```

The node ID is a lowercase ASCII protocol identifier accepted by Control; the
localized Dashboard label is independent. Runtime artifact identity hashes the
exact referenced GGUF bytes (or an explicitly referenced model tree).

The intended config check validates the contract without loading model weights,
downloading anything, or opening a listener. The optional `local-inference`
package extra supplies governed adapters behind the same fixed contract:

- `llama.cpp` embedding and rerank: separate loopback llama-server processes
  return structured vectors and scores; model/revision/dimension/digest remain
  bound to the Plastic Promise node identity, and generation text is rejected;
- `bge-local` embeddings and `bge-local` rerank (sentence-transformers /
  `AutoModelForSequenceClassification`, `local_files_only=True`);
- `ollama` embeddings (e.g. `qwen3-embedding:4b`, 2560 dim, L2): the node binds
  the Ollama model digest from the configured local `/api/tags` (loopback or the
  explicit `host.docker.internal` Docker Desktop gateway) before and after every
  batch and refuses the request on digest drift. Ollama has no request-level
  artifact binding, so a mid-batch A->B->A replacement cannot be fully
  excluded; exposing these vectors to a LanceDB generation still requires a
  separately verified shadow rebuild and promotion gate. Ollama never receives
  canonical-state access;
- `qwen3-cross-encoder` rerank (`Qwen/Qwen3-Reranker-4B`): an optional,
  separately deployed worker may load it with `local_files_only=True` and the
  official raw-logit CrossEncoder algorithm. The `pp-compute-node` control
  container never embeds PyTorch, Triton, CUDA libraries, or model weights.

Model weights must already exist in an operator-selected local cache or be
downloaded by the installation wizard. Plastic Promise does not silently pull
or replace a model at node startup.

### One-click build and readiness evidence

[`scripts/build_compute_node.sh`](../../scripts/build_compute_node.sh) (POSIX)
and [`scripts/build_compute_node.ps1`](../../scripts/build_compute_node.ps1)
(Windows) generate this `.env` with the operator-pinned manifest, run the
immutable local build, start Compose, and then execute
[`scripts/pp_node_smoke.py`](../../scripts/pp_node_smoke.py). After the immutable
build, the script completes the `.env` with the container
identity resolved during the build (base image reference/digest, source
revision, package version, and build/recipe policy digests), aliases the built
image to the compose image name, and starts Compose with `--no-build` so the
verified image is never rebuilt. The smoke verifies `/health`, `/v1/identity`,
embedding dimension and L2 normalization, and a
bounded rerank batch, and records median latency per endpoint. It writes a
smoke report (`plastic-promise/local-inference-node-smoke/v1`) plus a
doctor-compatible `runtime-status.json`
(`plastic-promise/local-inference-runtime-status/v1`, keys `schema_version`,
`running`, `node_healthy`), so the deployment `doctor --runtime-status` can
consume the readiness evidence. The deployment controller exposes the same
flow during initial deployment as `plastic-promise-deploy build-node`.
The smoke uses the private authorization in the ACL-protected compose
environment only as an HTTP header for these probes. It never serializes that
authorization, and it ignores the structured-JSON cloud credential rather
than retaining it in the smoke configuration.

On Windows, [`preflight_windows_node_host.ps1`](../../scripts/preflight_windows_node_host.ps1)
is the current source-level recovery seam for either Docker Desktop or a
WSL2-native daemon. It discovers and can explicitly relocate the selected
distribution VHDX, updates managed `.wslconfig` resource keys, configures
systemd in `/etc/wsl.conf`, enables the Docker service, verifies WSL-side
connectivity, and persists a verified proxy for the WSL shell, Docker daemon,
and BuildKit when direct access is unavailable. The normal native path invokes
`wsl.exe -d <distro> -e docker`; the host-global `socat` context is opt-in only.
Its JSON report is fail-closed: `ready=true` is required before the persisted
bootstrap proceeds.

The Windows build checks the Python package toolchain before composing the
control node. Model-worker dependencies are provisioned and verified by their
own worker setup rather than repaired into this image. The selected CPU/CUDA
control image is aliased before `-NoStart` may return. WSL-native compose
environment files use `/mnt/*` model paths, while Windows host operations
retain native drive paths.

## Source recipe / target Docker and WSL2 boundary (PR 3)

The checked-in [`Dockerfile`](../../deploy/local-inference-node/Dockerfile),
compatibility [`compose.yaml`](../../deploy/local-inference-node/compose.yaml),
[`compose.cpu.yaml`](../../deploy/local-inference-node/compose.cpu.yaml), and
[`compose.cuda.yaml`](../../deploy/local-inference-node/compose.cuda.yaml) are
recipe inputs for the PR 3 artifact matrix. Their presence proves neither a
completed build nor a usable local runtime.

The build policy requires a non-root, read-only-rootfs descriptor with a
loopback listener scope. It permits only logical `model-catalog` (read-only),
bounded `node-runtime` (read-write), and `node-tmp` (tmpfs) mounts. It forbids
SQLite, LanceDB, Docker sockets, credentials, private keys, model weights in
layers, arbitrary shell/tool access, and canonical authority. CPU and CUDA
remain variants behind the same `embedding/v1` / `rerank/v1` contract; CUDA is
limited to its supported platform policy.

The control node uses non-executable `node-tmp` and `node-runtime` tmpfs
mounts. Any worker that genuinely requires executable JIT scratch space owns
that exception in its separate runtime boundary.

Before Buildx receives any arguments, the builder uses
[`validate_container_artifact_policy.py`](../../scripts/validate_container_artifact_policy.py)
and [`resolve_container_artifact_identity.py`](../../scripts/resolve_container_artifact_identity.py)
to derive the CPU or CUDA identity from the versioned
[`oci-base-images.json`](../../deploy/oci-base-images.json) catalog. The
resolver supplies the immutable base reference/digest, source revision, package
version, build-policy digest, recipe-policy digest, and expected labels; it is
not valid to substitute a local tag or independently selected base image.
Both control-node variants intentionally use the slim Python base. The CUDA
variant differs through GPU visibility, resource telemetry, and routing
policy; operator-managed llama.cpp workers own CUDA and model execution. This
keeps the published control image small and prevents CUDA/cuDNN layers from
being duplicated in a container that never executes kernels.

The Windows/WSL2 local builder checks the built image labels for source
revision, base-image reference/digest, build-policy digest, and recipe-policy
digest. That check confirms only the local image's plan-bound metadata. It is
not a check of SBOM/provenance attestations, a signature verification, an image
publication, a deployment authorization, or proof that the node is running.

PR 3 does not activate Docker or Compose locally, allocate a runtime GPU, bind
a listener, write `runtime-assets/`, contact a node, or create a tunnel. Its
protected **verify-only** PR workflow may build a no-push OCI layout with
Buildx SBOM and provenance attestation layers. Its
[`verify_oci_artifact_evidence.py`](../../scripts/verify_oci_artifact_evidence.py)
step validates OCI descriptor hashes, the resolved revision/base/policy/
recipe-policy labels, and that both attestation layers name the selected
platform image digest as their subject. The verifier does not load an image,
contact a registry, validate a signer or certificate, publish, deploy, or
assert a production trust decision; this page also does not claim that a
verification job has run. PR 4 may inspect resources and produce a reviewed,
non-mutating activation plan, but it cannot activate or enroll a node. Actual
measured preflight enforcement, no-pull activation, enrollment, restricted
tunnel configuration, and runtime service ownership belong to PR 5; PR 6 owns
the cross-platform installer and release evidence. When runtime work is
authorized, a tunnel account must have no shell, sudo, SFTP, agent forwarding,
or public forwarding, and the server side must bind only loopback.

## Target cache and capacity contract (PR 5)

The cache manifest tracks only Plastic Promise-managed model-cache artifacts.
At local time **04:30**, the supplied host-systemd timer runs the read-only
`plastic-promise-local-inference-cache-plan` command against the manifest and a
supervisor status file. It plans cleanup only if the node is healthy and no
model download or index rebuild is active. It always preserves the active and
verified rollback revisions; only unreferenced artifacts idle for at least 24
hours become candidates. The planner emits JSON and never deletes anything;
PR 5's installer owns the explicit, separately authorized apply path.

Before any future pull, build, or image unpack, run the deployment controller's
plan-bound preflight with the measured resource budget and the actual local
container/model-store paths. It groups those paths with canonical state by
physical filesystem and rejects the whole install when any selected volume
would fall below `max(20%, 10 GiB)`. Node activation is no-pull and read-only
for model weights, so it cannot circumvent that resource gate.
