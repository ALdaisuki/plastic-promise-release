# Resource planning

Chinese parity document:
[`resource-planning.zh-CN.md`](resource-planning.zh-CN.md).

This document describes the controller-owned resource boundary. Every manifest
must contain a complete non-secret `resource_budget`; preflight fails closed
with `resource_budget_required` when it is missing. The declared budget covers
the full selected deployment write set, while the controller adds its own
SQLite write reservation:

- SQLite online backup space;
- exact WAL/SHM sidecars observed with the SQLite primary;
- image layers and unpacked image space;
- model cache;
- LanceDB shadow rebuild space;
- rollback-version coexistence;
- existing Docker and model-cache occupancy, observed directly from the
  configured non-secret local paths rather than copied from a manifest;
- restore candidate staging space;
- bounded versioned-migration scratch space; and
- an empty-database bootstrap allocation.

The manifest's `resource_budget` gives measured planned writes. Its
`resource_locations.container_store` and `resource_locations.model_cache`
identify the local filesystems on which those selected writes would land.
Preflight measures current occupancy recursively without following symlinks,
groups state/container/model paths by physical filesystem, and applies the
post-install free-space reserve to each grouped volume. A missing/unreadable
selected location blocks the whole operation; an absent, writable cache
directory is reported as zero current occupancy and is not created by planning.
Existing Docker/model occupancy is reported, not subtracted from free space a
second time. The profile catalog exposes the common policy as machine-readable
metadata (`minimum_free_bytes`, `minimum_free_fraction`, state hosts, and a
`model_artifacts_bundled=false` guarantee) so later adapters do not invent a
second set of defaults.

Every controller operation refuses to proceed when its projected free space is
below `max(20%, 10 GiB)`. Planning and preflight are read-only: they do not
create the requested state root, a SQLite file, a backup, or temporary SQLite
state.

The source-level `pp-local-edge` Deployment Center is a static,
non-authoritative planning projection. Its host-only `ppctl` interface accepts only typed
`inspect` and `preview` operations: the latter may render estimates, a manifest
diff, update class, and an inspection-only plan hash, but cannot apply it. Until
an endpoint is explicitly configured and separately authorized to listen, the
current Dashboard remains a transitional projection rather than evidence of
production migration. Neither view may reveal a path, download artifacts,
create state, migrate SQLite, contact/enroll a node, or claim that an installer
plan was accepted. A profile override cannot bypass a resource refusal. The host
controller preflight is the only hard gate for operations it owns; PR 5 owns any
separately authorized mutation.

For clarity, `manifest_comparison` is digest-level. A structural V2
`manifest_diff` is available only when the controller can safely project the
active topology; it contains only profile/module/endpoint/capability identifiers
and otherwise reports unavailable. `update_class` is conservative PR 4
inspection output (`no-change`, `enrollment-required`, or `manual-review`), not
an action decision. The displayed plan hash binds this safe observed projection
for drift reporting and cannot authorize execution.

## Hardware planning baselines

These are conservative starting points for a single user deployment, not a
promise that the controller can bypass its measured preflight. Disk figures are
usable space before the controller reserves `max(20%, 10 GiB)` on every touched
volume. Fixed model revisions, image layers, backups and a shadow rebuild can
raise the required space substantially; record those measured values in the
non-secret `resource_budget` instead of treating this table as an estimate.

| Profile and role | Minimum CPU / RAM / VRAM / free disk | Recommended CPU / RAM / VRAM / free disk | Docker, GPU, network and model prerequisites |
| --- | --- | --- | --- |
| `local-all-in-one` three endpoints | 4 logical CPU, 16 GiB RAM, 0 GiB VRAM, 50 GiB | 8 logical CPU, 32 GiB RAM, 8 GiB VRAM when a local GPU model is selected, 100 GiB | The target container topology requires Docker/Compose; GPU is optional. `pp-local-edge` stays loopback-only and only `pp-server-backend` mounts SQLite. |
| `local-cloud` edge + backend | 2 logical CPU, 8 GiB RAM, 0 GiB VRAM, 30 GiB | 4 logical CPU, 16 GiB RAM, 0 GiB VRAM, 60 GiB | The target container topology requires Docker/Compose and outbound access to the selected provider. Cloud identity is authoritative; credentials stay outside the manifest. |
| `split-accelerated` `pp-server-backend` | 4 logical CPU, 16 GiB RAM, 0 GiB VRAM, 80 GiB | 8 logical CPU, 32 GiB RAM, 0 GiB VRAM, 160 GiB | Requires a private tunnel. It is the sole SQLite writer and owns LanceDB generation verification/promotion; no node inference port is public. |
| `split-accelerated` `pp-compute-node` | 4 logical CPU, 16 GiB RAM, 8 GiB VRAM, 50 GiB | 8 logical CPU, 32 GiB RAM, 16 GiB VRAM, 100 GiB | Requires Docker/Compose with compatible runtime, fixed embedding/rerank revisions, and controlled model mounts. It stores no canonical SQLite or LanceDB generation. |

## Runtime resource avoidance

The compute node also has a runtime admission guard; installation preflight is
not the only resource boundary. The guard is enabled by default through
`PP_LOCAL_NODE_RESOURCE_GUARD=on` and samples aggregate GPU utilization before
starting a new inference request. If another device, game, renderer, or
unrelated accelerator workload crosses the configured limit (70% by default),
the node does not compete: embedding and structured JSON return HTTP 429
`node_overloaded` with `Retry-After`, while the server's rerank contract
preserves original order. The health projection exposes a bounded `resource_guard` state
without exposing process names, paths, credentials, or model contents.

The external llama.cpp worker launcher applies the same ten-second read-only
resource gate before creating workers. Operators can use `--status` for an
inspection-only view and `--stop` for a reversible stop that keeps model files
intact. An explicit `PP_LLAMA_CPP_RESOURCE_GATE=off` override is available for
controlled maintenance only; normal installation and release profiles leave it
enabled.

## Cost evidence

This table is a capacity baseline, not a price list. Provider prices, egress,
CI minutes, registry retention, electricity, and hardware amortization change
outside this repository. Any estimate used for a deployment decision must be
captured as dated dynamic evidence with provider/catalog revision, region,
currency, model identity, expected volume, cache hit assumptions, and fallback
policy. Documentation must not copy a current vendor price into a timeless
default.
