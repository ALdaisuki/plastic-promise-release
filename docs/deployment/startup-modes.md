# Startup and runtime modes

Startup placement and runtime execution mode are separate choices. A profile
does not silently enable a heavier engine or a provider.

## Current runtime modes

The existing runtime mode contract is `light`, `normal`, `rust-normal`, `full`,
and `rust-full`. Rust modes remain optional acceleration modes; Python remains
the reference path until parity evidence exists.

## Startup placement

- `local-all-in-one`: start the local runtime and loopback Dashboard together.
- `local-cloud`: start the local runtime; hosted inference is configured
  separately and is not bundled into the manifest.
- `split-accelerated`: start the server-owned services and register the remote
  node before scheduling work to it.

The PR 1 contract does not start services, create tunnels, migrate SQLite, or
download models. Those operations belong to later, explicitly gated PRs.

## Profile lifecycle runbook

Choose the matching no-secret manifest and an operator-owned state directory.
The commands below prepare and validate local controller state; they do not
start a service, pull an image, create a tunnel, download a model or activate a
provider. Review a plan before replacing the shown placeholder hash.

| Profile | Plan and preflight | Explicit runtime boundary |
| --- | --- | --- |
| `local-all-in-one` | `plastic-promise-deploy plan --manifest deploy/manifests/local-all-in-one.example.json --state-root ./var/local-all-in-one --json` then the matching `preflight` command. | Activate only the generated local platform asset after preflight; MCP and Dashboard remain loopback-only. |
| `local-cloud` | `plastic-promise-deploy plan --manifest deploy/manifests/local-cloud.example.json --state-root ./var/local-cloud --json` then the matching `preflight` command. | Configure hosted-provider credentials separately and activate a provider/model revision through the control plane, never through a manifest. |
| `split-accelerated` | `plastic-promise-deploy plan --manifest deploy/manifests/split-accelerated.example.json --state-root ./var/split-server --json` then the matching `preflight` command. | First verify the restricted node tunnel and identity; activate server and node assets separately. Do not bind inference to a LAN/public address. |

After a reviewed plan and successful preflight, rehearse the exact apply without
side effects:

```bash
plastic-promise-deploy apply \
  --manifest deployment.json \
  --state-root ./var/deployment \
  --plan-hash 'sha256:reviewed-plan-hash' \
  --dry-run
```

Only remove `--dry-run` after the operator explicitly accepts the plan. The
controller does not start or stop systemd, launchd, Windows services or Compose.
Use the generated asset's platform-native command to stop a selected runtime;
never run native systemd and server Compose against the same SQLite directory.

For recovery, take a verified backup before changes, then create a fresh
operation-bound plan for `upgrade` or the separately confirmed `restore` path.
For example, `plastic-promise-deploy backup` is a verified online backup
operation; `plastic-promise-deploy plan --operation upgrade ...` is not
interchangeable with a restore plan. Run `plastic-promise-deploy doctor --json`
before activation and after any failure. See
[`deploy-controller.md`](deploy-controller.md) for exact upgrade, rollback and
restore acknowledgements, and [`troubleshooting.md`](troubleshooting.md) for
failure diagnosis.
