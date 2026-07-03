---
name: tools-visual-ascii-arch
description: Express product, service, or data architectures through layered ASCII diagrams suitable for terminals, PRs, and ADRs.
---

# ASCII Architecture Mapping

## Intent
- Provide quick system overviews when graphical tools aren’t available.
- Maintain multiple zoom levels (context, container, component) in text form.

## Inputs
1. List of components/services/contracts.
2. Interaction types (sync, async, queues, blockchain events).
3. Deployment/runtime context (devices, regions, environments).

## Workflow
1. **Choose framing**
   - Context diagram (users ↔ system).
   - Container diagram (services, DBs, queues).
   - Component diagram (modules, classes, contracts).
2. **Apply layout grid**
   - Use `+---+` boxes for nodes, `|`/`-` for edges.
   - Keep consistent spacing (2 spaces between layers).
3. **Annotate flows**
   - Label arrows with protocols (`HTTP`, `gRPC`, `L1 tx`).
   - Use `~>` for async events, `=>` for sync calls.
4. **Layer metadata**
   - Add footnotes for SLAs, owners, repos.
   - Indicate platform-specific components (e.g., `[iOS]`, `[Windows agent]`).
5. **Version and reuse**
   - Store diagrams in `.factory/diagrams/<topic>.txt`.
   - Link from AGENTS.md or skills that need system context.

## Verification
- Diagram fits within 100 characters width; no tab characters.
- All components labeled; directional arrows show data/control flow.
- Latest version referenced in related skills/docs.
