# Deployment profiles and endpoint manifest contracts

> **Normative scope:** the machine-readable
> [Union Six-PR Contract](../standards/union-six-pr-contract.json), revision
> `2026-08-18.1`, governs endpoint responsibility and PR completion. This page
> is a derived source-contract projection and does not prove runtime or
> production activation.

> **PR 2 status — contracts and records only.** This document specifies the
> versioned endpoint vocabulary, pure resolution result, and bounded evidence
> records. It does not create containers, open a tunnel, invoke `ppctl`, write
> SQLite, migrate a database, promote LanceDB, or start Maintenance. Those
> operational steps remain target work for PRs 3–6.

Plastic Promise has one codebase and one canonical-data contract. A deployment
profile selects where responsibilities will run; it never creates a second
memory product or another SQLite truth source.

## Boundary: legacy V1 and endpoint V2

| Contract | Status in PR 2 | Intended purpose | Location and secret policy |
| --- | --- | --- | --- |
| `plastic-promise-deployment/v1` | **Legacy compatibility input** | Existing controller-era local profile declaration | It may contain a node `ssh_host` and concrete local resource paths. Keep it node-local; it is not a V2 endpoint contract or a portable receipt. |
| `plastic-promise-deployment/v2` | **Current endpoint contract** | Secret-free endpoint placement and pure resolved-plan input | It carries opaque references only. It rejects `ssh_host`, host/IP/URL fields, filesystem paths, tunnel details, credentials, and secret-shaped values. |

V1 is not silently converted, published as a V2 record, or used to infer a
private transport. An operator or a later deployment adapter must explicitly
choose how a legacy local input is handled. The existence of a V1 parser does
not grant it authority over canonical state.

### Legacy V1 compatibility example

This is deliberately the first JSON example so existing V1 documentation
readers can validate their legacy input. It is **not** a V2 template and must
not be copied into a repository, dashboard revision, public receipt, or
cross-host configuration record when it contains host-local paths.

```json
{
  "schema_version": "plastic-promise-deployment/v1",
  "deployment_id": "local-laptop",
  "profile": "local-all-in-one",
  "modules": {
    "local-ollama": {"enabled": true}
  },
  "nodes": [],
  "resource_locations": {
    "container_store": null,
    "model_cache": "/var/lib/plastic-promise/model-cache"
  },
  "resource_budget": {
    "image_layers_bytes": 0,
    "image_unpack_bytes": 0,
    "model_cache_bytes": 1,
    "lancedb_shadow_rebuild_bytes": 1,
    "rollback_coexistence_bytes": 1
  }
}
```

### Endpoint V2 contract example

V2 declares roles, versioned protocols, capabilities, bounded concurrency, and
opaque policy references. `transport_ref` and `resource_policy_ref` are stable
labels, not hosts, URLs, paths, usernames, or secrets. Complete model identity
is attested separately by a typed endpoint observation; it is not guessed from
the profile or from matching vector dimensions.

```json
{
  "schema_version": "plastic-promise-deployment/v2",
  "deployment_id": "developer-laptop",
  "profile": "local-all-in-one",
  "modules": {},
  "endpoints": [
    {
      "id": "local-edge",
      "role": "pp-local-edge",
      "protocol": {"family": "edge", "major": 1, "minor": 0},
      "capabilities": [],
      "transport_ref": "loopback",
      "resource_policy_ref": "edge-default"
    },
    {
      "id": "server-backend",
      "role": "pp-server-backend",
      "protocol": {"family": "backend", "major": 1, "minor": 0},
      "capabilities": [],
      "transport_ref": "backend-private",
      "resource_policy_ref": "backend-default"
    },
    {
      "id": "compute-node",
      "role": "pp-compute-node",
      "protocol": {"family": "compute", "major": 1, "minor": 2},
      "capabilities": [
        {"kind": "embedding", "contract_version": "embedding/v1"},
        {"kind": "rerank", "contract_version": "rerank/v1"},
        {"kind": "structured-json", "contract_version": "structured-json/v1"}
      ],
      "max_concurrency": 4,
      "transport_ref": "compute-registry",
      "resource_policy_ref": "compute-default"
    }
  ]
}
```

V2 admits exactly one `pp-local-edge` and one `pp-server-backend`; a
`split-accelerated` profile also requires at least one `pp-compute-node`. Its
optional `resource_locations` contain opaque labels such as
`"container_store": "server-containers"`, never an absolute or relative
filesystem path. A later preflight adapter resolves those labels privately on
the owning host.

## Endpoint profile vocabulary (source contract; runtime evidence pending)

| Profile | Placement intent | Canonical owner | Derived-inference intent |
| --- | --- | --- | --- |
| `local-all-in-one` | All endpoint roles may be co-located on one host | `pp-server-backend` only | Managed `pp-compute-node`, local by default, advertises only its configured exact capabilities |
| `local-cloud` | Local edge and backend, with a managed compute execution plane | `pp-server-backend` only | `pp-compute-node` executes configured hosted embedding, rerank, and structured JSON; provider credentials never enter the server |
| `split-accelerated` | Edge, server backend, and compute role may run on different hosts | Server `pp-server-backend` only | Registered `pp-compute-node` may be `local`, `cloud`, or `hybrid` when capability and model identity match exactly |

Profiles change placement, not authority. Each retains project isolation,
durable-outbox admission, bounded failure reasons, retry state, and reconcile.
`pp-local-edge` returns bounded projections only. All local, hosted, and raw
provider adapters execute inside `pp-compute-node`, which returns bounded
derived results and receipts only. Neither endpoint receives canonical SQLite,
LanceDB-promotion, collaboration, or canonical-write authority.

PR 5 source treats structured JSON as a first-class compute capability beside
embedding and rerank. It remains disabled by default until its backend, model,
fixed revision, bounded provider settings, and identity-revalidation receipt
are present. The active project control revision may select `local`, `cloud`,
or `hybrid` for new work without moving provider construction or credentials to
`pp-server-backend`. These source contracts and focused tests do not establish
live provider activation, runtime evidence, production acceptance, or
publication.

Structured-JSON intent/schema pairs resolve through a closed registry owned by
the compute node; the server never constructs provider prompts. Embedding and
structured-JSON deferral uses a content-free durable retry marker, and the
caller rehydrates raw input when it retries. Canonical SQLite retains only
bounded intent, identity, digest, failure, and receipt references. Scheduling
consumes an explicit identity-revalidation receipt and never manufactures one
from cached health.

## Resolved plan and ownership

`resolve(manifest)` is the PR 2 deep-module seam. It returns either a typed,
side-effect-free `ResolvedEndpointDeploymentPlan` or a sanitized
`EndpointContractError`. The resolved plan fixes one server-backend endpoint as
all of the following:

- canonical SQLite owner;
- LanceDB-promotion owner; and
- deployment-receipt persistence owner.

The browser projection of this plan is intentionally narrower than the private
runtime view: it excludes paths, addresses, credentials, transport material,
leases, and raw resource or health payloads.

## PR 4 Deployment Center projection

> This is a current source planning contract. It is not a claim that a local
> edge bridge is configured, an endpoint is listening, or production deployment
> is live.

The browser may submit only a secret-free `EndpointManifestV2` candidate to the
host. `ppctl` accepts a fixed typed allowlist—`inspect` and `preview`—and returns
redacted projections. It does not accept a legacy V1 manifest, an SSH host,
filesystem path, credential, private key, Docker request, shell command, or a
generic operation selector.

`inspect` reports platform/resource/catalog/status, recommendation, model and
enrollment readiness, and receipt state. `preview` resolves the V2 candidate,
records whether a supported selected profile is a user override, and returns a
manifest diff, module/resource estimates, complete identity comparison, hard
preflight result, update class, and inspection-only plan hash. The host keeps
raw paths and legacy compatibility data private; browser cache never proves a
current plan or host state.

The recommendation is advisory, but no override can bypass module/profile
compatibility, high-risk acknowledgement, exact embedding/rerank identity
evidence, immutable artifact evidence, or the resource gate. A failed
`max(20%, 10 GiB)` free-space check is a refusal, not a confirmation dialog.
The update class is one of `no-change`, `live-apply`, `rolling-restart`,
`shadow-rebuild-promotion`, `backup-migration`, `enrollment-required`, or
`manual-review`; PR 4 only reports it. Apply, node contact, enrollment
consumption, tunnel/service management, SQLite mutation, LanceDB promotion,
Maintenance, and receipt persistence are deferred to later operational work.

## Identity and compatibility evidence

An embedding route is compatible with an active generation only when its typed
identity matches **every** field below. Equal dimensions are insufficient.

| Required embedding-identity field | Meaning |
| --- | --- |
| `model` | Declared model name |
| `revision` | Fixed immutable revision, never `latest`, `main`, or `stable` |
| `dimension` | Exact output-vector dimension |
| `normalization` | Normalization strategy |
| `metric` | Retrieval distance/similarity metric |
| `tokenization` | Tokenization contract |
| `pooling` | Pooling contract |
| `artifact_sha256` | Immutable model-artifact evidence |
| `golden_vector_sha256` | Golden-vector proof for the declared vector space |

Rerank evidence is independently versioned and includes model, fixed revision,
artifact SHA-256, and scoring-schema version. A complete identity fingerprints
the capability. Any model, revision, dimension, normalization, metric,
tokenization, pooling, artifact, or golden-vector change requires fresh
identity evidence; a later operational stage decides whether a derived shadow
generation may be rebuilt and promoted.

## PR 6 release-readiness selection boundary

> **Target only.** A profile/manifest resolves endpoint placement; it is not a
> release selection, registry authority, or runtime receipt.

The later PR 6 Release Bundle must bind the selected profile/variant matrix to
immutable image evidence and an opaque Model Catalog reference/digest. A
profile may be selected only when the bundle declares that profile and the
endpoint-role/compute-variant matrix is compatible. The catalog provides fixed
model identity, capability, and resource metadata; it never carries model
weights, paths, endpoints, credentials, or canonical-write authority.

| Profile consequence | Release-readiness requirement |
| --- | --- |
| `local-all-in-one` | Bundle lists the co-located role/variant set; the backend remains the single SQLite writer. |
| `local-cloud` | Bundle does not turn provider configuration into image state; a provider identity remains node-local and separately governed. |
| `split-accelerated` | Bundle lists compatible compute CPU/CUDA variants and a Model Catalog reference/digest; the compute node returns derived results only. |

An `ArtifactBundle` can substantiate its own immutable descriptor evidence, but
cannot substitute for a Release Bundle, an Execution Grant, Migration Operation
evidence, or a verified server receipt. A mutable image tag or same-dimension
model is insufficient for profile selection.

## Typed compute and governance records

PR 2 defines only transport-independent records. Adapters in later stages may
persist or transmit them, but the contract itself performs no I/O.

| Record or decision | Contract boundary |
| --- | --- |
| Endpoint protocol and capability | Versioned protocol family/major/minor plus current `embedding`, `rerank`, or `structured-json` capability declaration; structured JSON is disabled by default and requires complete backend, model, fixed-revision, provider-setting, and revalidated identity information before admission; this source-level boundary does not claim live provider activation or runtime/production evidence |
| Hello, heartbeat, and resource report | Static identity attestation, server-observed freshness, and bounded capacity report; no device serial, host address, or path |
| Admission and binding | Verifies role, protocol compatibility, freshness, capacity, and the complete identity fingerprint before derived work is eligible |
| Compute lease and fencing generation | Binds a derived job to an endpoint, capability, result schema, expiry, idempotency key, and monotonic fence so stale work cannot win |
| Completion decision | Accepts or rejects a body-free result envelope, returns a stable reason and retry/quarantine recommendation |
| Manifest revision and deployment receipt | Server-owned record schemas keyed by manifest digest; they do not claim a persistence engine or trigger a write in PR 2 |

Errors expose only a stable code, category, retryability, and optional retry
delay. They do not echo a configuration body, provider response, host, path,
credential, or user payload.

## PR 2 non-goals and staged handoff

| Stage | Target responsibility — not implemented by the PR 2 contract |
| --- | --- |
| PR 3 — container artifacts | Source-level `ContainerArtifactCompiler`, role/platform/variant policy, static recipes, and immutable inspection evidence; protected CI may do no-push OCI build verification, but no local/runtime Compose activation, tunnel, registry publication, or deployment action |
| PR 4 — deployment center | `ppctl`, Deployment Center inspection/preview, local/private adapter planning, and reviewed activation-plan UX; no apply or runtime mutation |
| PR 5 — migration and runtime operations | Measured preflight enforcement, node activation/enrollment, restricted transport, backups, SQLite migration, LanceDB shadow/promotion, and Maintenance operations |
| PR 6 — release readiness | Cross-platform installer, target RC/stable evidence, Release Bundle/Model Catalog selection, and final controlled-release checks; no publication claim until independently verified |

Until those stages land and are explicitly activated, the V2 contract must be
treated as validation and record vocabulary only. It cannot start a service,
create a tunnel, download a model, mutate a database, promote an index, or
enable Maintenance.

## References

- [Deployment and Runtime Guide](README.md)
- [部署与运行指南（中文）](README.zh-CN.md)
- [Local heterogeneous inference node contract](local-inference-node.md)
- [本地异机推理节点合同（中文）](local-inference-node.zh-CN.md)
- [Resource planning and hard gate](resource-planning.md)
