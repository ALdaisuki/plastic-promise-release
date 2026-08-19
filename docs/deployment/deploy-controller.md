# Cross-platform deployment controller

`plastic-promise deploy` is the primary local, explicit deployment controller;
`plastic-promise-deploy` is an equivalent direct entry point. It owns only the
selected local deployment state root, its installer-state SQLite file and local
backup files. It never starts or stops a service, manages Docker or Compose,
creates SSH accounts, opens a tunnel, downloads a model, or sends telemetry.
It is not the production migration authority and must not be used as a second
writer for the server's canonical memory SQLite.

The Bash and PowerShell wrappers are thin argument forwarders:

```bash
scripts/plastic-promise-deploy.sh doctor --json
plastic-promise deploy plan --manifest deployment.json --state-root ./var/deployment --json
```

```powershell
./scripts/plastic-promise-deploy.ps1 doctor --json
plastic-promise deploy plan --manifest deployment.json --state-root .\var\deployment --json
```

Set `PP_PYTHON` only when the wrapper should use a particular already-installed
Python interpreter. Neither wrapper invokes privilege escalation or accepts a
secret value.

## Initialize a manifest

Create a reproducible, non-secret deployment manifest with either interactive
terminal choices or explicit parameters. The default output directory is
`~/.config/plastic-promise/deployments/`; `init` refuses to overwrite a file.
When `--manifest` is omitted, operational commands discover the one JSON file
in that directory and fail closed when none or multiple files exist. `init`
leaves resource budgeting absent on purpose, so preflight cannot approve an
apply until measured component capacity is added.

```bash
plastic-promise-deploy init \
  --profile local-all-in-one \
  --module local-ollama \
  --deployment-id workstation \
  --json
```

## Plan first

Every mutating operation starts with an immutable **operation-bound** plan:

```bash
plastic-promise-deploy plan \
  --manifest deployment.json \
  --state-root ./var/deployment \
  --json
```

The result has a deterministic `sha256:` plan hash. `plan` and `preflight` do
not create the requested state directory, SQLite database, backup, or temporary
SQLite file. The hash binds the selected operation, manifest/profile/module
intent, selected state root, installer-state fingerprint, and non-secret
fingerprints for the canonical SQLite primary, exact `-wal`/`-shm` sidecars and
an optional restore source. A changed action, database, installer record or
restore source is rejected as drift rather than applied.

For anything other than the default `install` action, state the action in the
plan explicitly. `repair` is an `upgrade` alias and `replace-db` is a
`restore` alias:

```bash
# A migration plan is not interchangeable with an install plan.
plastic-promise-deploy plan \
  --operation upgrade \
  --manifest deployment.json \
  --state-root ./var/deployment \
  --json

# Restore accepts only a controller-generated same-profile backup. Cross-profile
# movement needs its own one-way migration contract, not a generic restore.
plastic-promise-deploy plan \
  --operation restore \
  --source ./backups/known.sqlite3 \
  --manifest deployment.json \
  --state-root ./var/deployment \
  --json
```

`preflight` computes the expected remaining space after the write set the
controller can actually perform: an empty-bootstrap allocation where relevant,
an existing SQLite online backup including its observed sidecars, a restore
candidate where relevant, and bounded migration scratch space. It refuses the
operation when the result would be below `max(20% of the volume, 10 GiB)`:

```bash
plastic-promise-deploy preflight \
  --operation upgrade \
  --manifest deployment.json \
  --state-root ./var/deployment \
  --json
```

## SQLite lifecycle

An empty target is created only by a non-dry-run `apply` or `install` with a
matching plan hash. An existing database is attached, checked with
`integrity_check`, backed up online, and only then receives known versioned
migrations inside one transaction. A failed migration rolls back its database
transaction; the pre-migration backup remains available for recovery.

```bash
plastic-promise-deploy apply \
  --manifest deployment.json \
  --state-root ./var/deployment \
  --plan-hash 'sha256:...' \
  --dry-run
```

Use the same command without `--dry-run` only after reviewing the plan. The
controller stores no provider credentials, endpoints, request data or memory
rows in its state record.

`backup` is a separate verified online backup operation. `upgrade` / `repair`
operate only on an installed, existing and healthy canonical database: they
cannot turn a missing primary file into a fresh empty database. A missing
primary must go through the separately confirmed recovery path instead.

`restore` and `replace-db` are separate high-risk operations: both require an
explicit restore confirmation, a reviewed plan bound to the source fingerprint,
and an explicit assertion that the relevant service is already stopped. Restore
accepts only a controller-created backup with a matching `.evidence.json`
sidecar: it contains the source profile and SHA-256/byte-count provenance, all
rechecked before the current canonical SQLite is backed up. A cross-profile
source is rejected and requires a separate explicit one-way migration path.
Restore first creates a new backup of the current canonical database. It stages
only the old target `-wal` / `-shm` sidecars; if the primary replacement fails,
those sidecars are put back. If the restored database fails its versioned
migration, the controller restores the new pre-restore backup before returning
a stable failure reason.

## Coordinated migration authority (PR 5 durable source / target live adapters)

The local controller's plan/apply lifecycle is limited to an explicitly local
deployment state root. A coordinated systemd-to-container migration is a
separate **current source / target runtime** `MigrationOperation` owned by
`pp-core`/`pp-server-backend`. It binds a fresh Migration Operation Plan to current
artifact, runtime, node, canonical-state, backup, and derived-index evidence,
then requires a separate operation-bound Execution Grant. A local controller
plan hash, browser confirmation, or `ppctl` request cannot serve as that grant.

The server-owned orchestrator calls only typed fixed-phase adapters for
preflight/drift fencing, backup and rehearsal, cutover, shadow-generation
validation/promotion, Maintenance, rollback, and secret-free receipt
persistence. The source defaults to a 300-second plan TTL (900-second maximum),
rejects observations older than 120 seconds, and persists its terminal result
through `SQLiteMigrationExecutionJournal`. The controller's backup-gated
versioned migration installs the grant/operation/installation-lease tables;
runtime construction never creates them implicitly. The journal supplies
cross-process lease/fence CAS and marks expired running work
`recovery-required`. `pp-server-backend` retains the single-writer lease and is
the only component allowed to mutate canonical SQLite. Deployment Center and
`ppctl` remain read-only; adapters never receive
arbitrary shell, Docker, SSH, or SQLite commands. Until a separately authorised
set of live mutable phase adapters is available, no live listener, container, tunnel,
production migration, LanceDB promotion, Maintenance transition, or MCP
restart is verified.

`module install|enable|disable|remove` maintains installer-owned module state
and a non-secret component receipt under `runtime-components/`. For selected
modules that have an executable runtime, install also renders real, exact
activation assets under `runtime-assets/<module>/`: a fixed-digest, no-pull
Compose file plus a current-platform systemd unit (Linux/WSL2), launchd plist
(macOS), or PowerShell activation script (Windows). `canonical-runtime` also
receives a non-secret `canonical-runtime.env` with only the selected SQLite
and LanceDB paths; its platform activation asset starts the local MCP runtime
explicitly. The generated component receipt lists only relative asset names,
never an endpoint, secret, SQLite path or user content. Its Compose contract
has `pull_policy: never`, so activating it cannot silently download an image
after preflight.

The controller writes these assets but never registers, enables, starts, stops
or migrates a service. Core modules cannot be disabled or removed, and an
optional module cannot be removed while another installed module requires it.
Module removal physically clears only its exact generated descriptor and assets
while retaining unrelated user files; `remove` / `uninstall` clear all
controller-owned runtime assets while retaining SQLite, knowledge sources,
model caches, backups and audits. `purge` physically removes
only the managed SQLite primary file and
its exact `-wal` / `-shm` sidecars after both a purge confirmation and a
service-stopped confirmation; it first creates a verified backup.

## Platform doctor and service boundary

```bash
plastic-promise-deploy doctor --state-root ./var/deployment --json
plastic-promise-deploy status --state-root ./var/deployment --json
plastic-promise-deploy module list --json
plastic-promise module list --json
plastic-promise-deploy module install \
  --manifest deployment.json \
  --state-root ./var/deployment \
  --module local-ollama \
  --plan-hash 'sha256:...' \
  --dry-run
```

Doctor reports local SSH/Docker/GPU/Ollama command availability, disk facts
when a state root is supplied, the local service-manager family, and a
redacted diagnosis for node identity, model configuration, runtime and reverse
tunnel. It does not connect to a node, invoke Docker/SSH/system services, or
expose an endpoint. Optional evidence is read-only and should be non-secret:

```bash
plastic-promise-deploy doctor \
  --state-root ./var/deployment \
  --node-config ./local-inference-node.env \
  --tunnel-config ./local-inference-tunnel.env \
  --runtime-status ./runtime-status.json \
  --json
```

`--node-config` checks whether a local node has a valid non-secret identity
contract, fixed revisions and readable local model references. It reports
`configured_unverified` rather than guessing when the model backend cannot be
validated from local files. A matching manifest declaration is labelled
`declaration_evidence_accepted`, not live node acceptance: this doctor never
connects to the declared host. `--tunnel-config` checks only the required
restricted-tunnel contract fields, never their values.
`--runtime-status` accepts only this redacted local supervisor evidence:

```json
{
  "schema_version": "plastic-promise/local-inference-runtime-status/v1",
  "running": true,
  "node_healthy": true
}
```

No node ID, revision, model path, host, port, tunnel target or credential is
included in doctor output.

Systemd and Compose assets may coexist. The controller never migrates an
existing production systemd service, invokes `systemctl`, runs Compose, or
changes launchd/Windows service state. Once plan and preflight have passed,
an operator may review the generated asset, supply its separate node-local
environment file, and use the platform's explicit activation command. That
activation stays outside the CLI so interactive sudo and account boundaries
remain intact.

For a local heterogeneous inference node, use a dedicated restricted server
account and a private reverse tunnel as documented in
[`local-inference-node.md`](local-inference-node.md). Account creation,
interactive sudo authentication and tunnel activation stay outside this CLI.
The server still independently verifies node identity and holds all canonical
SQLite write authority.

On Windows, `scripts/plastic-promise-deploy.ps1` invokes the local Python CLI
when available. Set `PP_DEPLOY_TARGET=wsl` to select a non-`docker-*` WSL
distribution (optionally through `PP_WSL_DISTRIBUTION`), map the repository
path into that distribution, change to the mapped directory, and execute the
same Python deployment CLI there. If local Python is unavailable it attempts
that same validated WSL route. The wrapper checks that the Windows and WSL
packages identify as the same Plastic Promise version; it only forwards
validated deployment commands and never starts a container or service.
