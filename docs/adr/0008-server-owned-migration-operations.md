# Keep coordinated migrations server-owned

Plastic Promise has a single canonical SQLite writer: `pp-server-backend`.
The Deployment Center browser surface and its `ppctl` planning adapter therefore
remain read-only, while the existing cross-platform CLI remains a planner and
operator entry point rather than a general command runner. Coordinated runtime
migration is modeled as one server-owned Migration Operation with a fresh
Migration Operation Plan, an explicit Execution Grant, typed runtime/node/
derived-index adapters, and a secret-free Migration Receipt.

## Considered options

- Extend `ppctl` with browser-callable apply commands.
- Let the CLI directly open and migrate canonical SQLite.
- Use a server-owned orchestration seam with typed adapters.

The first two make a browser or a local operator process a competing canonical
writer and blur the difference between planning evidence and execution
authority. The third keeps the command surface narrow and makes rehearsal,
cutover, rollback, Maintenance, and receipt persistence observable through one
deep module.

## Consequences

- A Deployment Center `plan_hash` is permanently inspection-only and cannot be
  accepted as an Execution Grant.
- Runtime, node, and derived-index adapters may execute only fixed migration
  phases; they never receive arbitrary shell, Docker, SSH, or SQLite commands.
- Live mutable phase-adapter wiring remains separately authorized. Source contracts and
  fake-adapter tests do not claim a live migration, listener, tunnel, container,
  or production cutover.
- Production composition must use `SQLiteMigrationExecutionJournal`, whose
  backup-gated versioned schema persists server-issued grants,
  installation-scoped leases, monotonic fences, one-shot operation state, and
  secret-free receipts. Expired running work becomes `recovery-required`;
  stale owners cannot complete or roll back after fence loss.
- The in-memory journal is limited to tests and explicitly non-production
  composition. Deployment Center and `ppctl` remain inspection-only.
