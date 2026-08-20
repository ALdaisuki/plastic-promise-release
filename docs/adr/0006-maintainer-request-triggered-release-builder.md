# Use a request-triggered maintainer Release Builder with desktop confirmation

Plastic Promise stable releases are performed by a locally installed, request-triggered Release Builder rather than a network-facing build service or a permanent polling daemon.  The default `desktop-interactive` mode may use the maintainer's existing desktop release identity only after a 30-minute confirmation bound to the immutable request hash; `headless-builder` is deliberately limited to no-secret local build and smoke work.  This preserves a usable one-machine release path without making arbitrary remote execution or standing publication authority part of the product.

## Consequences

- A Release Request is an explicit, reproducible authority boundary; memories and prior receipts can inform it but cannot grant actions.
- Server deployment may use the independently provisioned Windows Builder root key only after the release evidence chain has passed.
- The module builds Plastic Promise itself.  User-project build adapters remain a future Project Steward capability, not an implicit feature of this release system.
