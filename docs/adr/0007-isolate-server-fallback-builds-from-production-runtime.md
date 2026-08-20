# Isolate CPU-only server fallback builds from the production runtime

Plastic Promise normally releases through three distinct roles:

```text
Windows/WSL2 Builder -> local cache and RTX GPU smoke
GitHub protected workflow -> immutable OCI digest, SBOM, and attestation
Production server -> verified-digest runtime, canonical SQLite, LanceDB, and Maintenance
```

The Windows Builder can be unavailable or correctly deferred by its resource
gate.  A network failure or a busy local GPU does not make the production
runtime a safe arbitrary-build host: it has canonical state, service
availability duties, and must not inherit release credentials.

The build-dispatch order is explicit:

```text
1. Windows/WSL2 local node: resource gate, build, and RTX GPU smoke
2. GitHub protected workflow: exact-SHA CPU OCI build, SBOM, and attestation
3. Separately provisioned Server Fallback Builder: explicit CPU-only last resort
4. Production MCP runtime: never a build target
```

The dispatcher evaluates the local node first.  A `ready` resource result uses
the Windows/WSL2 Builder.  A node transport failure or
`deferred_resource_busy` result selects GitHub, which already executes the
protected release evidence chain outside the production host.  Neither remote
path has the Windows RTX hardware; Windows GPU smoke remains a separate
required gate before stable promotion of an accelerated inference image.

## Decision

A future **Server Fallback Builder** is an opt-in, CPU-only, request-triggered
module.  It is a separate role, not a mode of the MCP runtime or deployment
Compose stack.  Its one public interface is:

```text
execute(FallbackBuildRequest) -> FallbackBuildReceipt
```

`FallbackBuildRequest` contains only an exact source commit, verified archive
SHA-256, allowed image target, and operator-selected fallback reason.  It does
not accept shell commands, workspace paths, registry destinations, action
flags, or credentials.  The module must:

- run as a non-login builder identity with an isolated source, report, and
  BuildKit cache root on a dedicated mount;
- use an empty request-local Docker configuration and have no GitHub, PyPI,
  registry-push, SSH deployment, MCP, SQLite, LanceDB, or Maintenance access;
- perform the same bounded cleanup and resource admission before a build, with
  at least 80 GiB free on its dedicated mount and no active fallback build;
- build only the reviewed exact SHA, verify the OCI revision label, and run the
  package smoke test;
- record `gpu_smoke=not_applicable_server_cpu` and a redacted receipt.

The receipt is **CPU preflight evidence only**.  It cannot publish an image,
modify a release request, authorize deployment, promote LanceDB, start
Maintenance, or replace either the Windows GPU smoke or GitHub protected OCI
evidence.  A Server Fallback Builder is never selected from the local resource
gate: it requires an additional explicit request after GitHub is unavailable or
unsuitable for the selected diagnostic work.

## Consequences

- The current production MCP host remains a verified-digest consumer and must
  not execute fallback builds through its runtime service or Compose project.
- When a local node is busy or unavailable, GitHub receives the exact source
  SHA as the first remote fallback.  For a pull request its normal CI job runs
  on push; for an RC the immutable request and its confirmation still govern
  the no-publish `release-rc` dispatch.
- A server CPU receipt is useful only as additional preflight evidence, never
  as a replacement for protected OCI evidence.
- A physical server may host the fallback role only after it has a qualified
  dedicated mount and the isolated builder identity.  A root filesystem shared
  with canonical state does not qualify.
- The fallback runner is a future implementation slice.  Its tests must prove
  source/archive binding, absent runtime mounts and credentials, capacity
  rejection, immutable receipt behavior, and that a successful CPU receipt
  does not satisfy GPU or publication gates.
