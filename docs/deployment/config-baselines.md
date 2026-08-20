# Configuration baselines

Configuration is intentionally split into three layers:

1. **Profile defaults** — safe, versioned defaults shipped by the distribution.
2. **Deployment manifest** — non-secret profile and optional-module choices.
3. **Node-local environment** — API keys, private endpoints, credentials, and
   machine-specific paths; never commit this layer.

The manifest validator rejects secret-named fields and known token/key
patterns. It is not a secret manager and must not be used to transport
credentials.

## Baseline recommendations

| Profile | Baseline | Recommended when |
| --- | --- | --- |
| `local-all-in-one` | SQLite/LanceDB/MCP/Dashboard on loopback | A single machine is sufficient |
| `local-cloud` | Local state with bounded hosted inference | Local storage is preferred but hosted models are available |
| `split-accelerated` | Server state with a registered local node | A separate local node can accelerate embedding/rerank |

`accelerator-max` and `maintenance-daemon` are not default modules. Enabling
them requires explicit acknowledgement and later server-side governance.

## Per-profile baseline and recommended configuration

The following files are parseable, non-secret starting points. Copy one to an
operator-owned deployment directory, replace only non-secret identifiers and
measured resource budgets, then run `plan` and `preflight` before any apply.
They are not environment files and must never carry provider keys, tunnel
credentials or model-download tokens.

| Profile | Basic usable configuration | Recommended production configuration |
| --- | --- | --- |
| `local-all-in-one` | [`local-all-in-one.example.json`](../../deploy/manifests/local-all-in-one.example.json) with local state and an optional registered `local-ollama` module. | Keep SQLite, LanceDB and the Dashboard on one loopback-only host; pin the selected local model revision outside the manifest and take verified controller backups before upgrades. |
| `local-cloud` | [`local-cloud.example.json`](../../deploy/manifests/local-cloud.example.json) with local SQLite/LanceDB and no bundled model cache. | Keep all canonical and derived state local; place hosted-provider credentials only in node-local environment/process configuration and activate provider/model changes through a controlled revision. |
| `split-accelerated` | [`split-accelerated.example.json`](../../deploy/manifests/split-accelerated.example.json) with a declared node role, non-secret SSH alias, embedding/rerank capabilities and optional bounded `max_concurrency` (default `1`). | Keep SQLite, Outbox, LanceDB and governance on the server; use a restricted private tunnel, fixed node model revisions and read-only model mounts. The node returns derived inference results only and never receives canonical-write authority. |

`accelerator-max` remains off in every baseline. `maintenance-daemon` is also
off and cannot become enabled merely because another module depends on it.
