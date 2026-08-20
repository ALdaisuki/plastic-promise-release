"""The pp-server-backend production composition seam.

The MCP server process is the canonical SQLite owner, but the migration
orchestrator deliberately has no knowledge of that process' concrete runtime
adapters.  This module is the small, discoverable bridge used by the server
composition root: callers must provide every typed observation/mutation
adapter, while the durable journal is opened by
``compose_production_migration_operations``.

Constructing the operations object is intentionally side-effect free.  It
does not issue a grant, acquire a migration lease, or execute any phase.  A
caller must invoke ``MigrationOperations.issue_grant`` and then ``apply``
explicitly when an operator-authorized migration is requested.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from plastic_promise.deployment.migration_operations import (
    DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
    DEFAULT_PLAN_TTL_SECONDS,
    CanonicalStateMigrationAdapter,
    CollaborationSchemaMigrationAdapter,
    DerivedIndexMigrationAdapter,
    EdgeComputeMigrationAdapter,
    MaintenanceMigrationAdapter,
    MigrationObservationAdapter,
    MigrationOperations,
    MigrationOperationsError,
    RetentionCacheMigrationAdapter,
    RuntimeMigrationAdapter,
)
from plastic_promise.deployment.production_migration import (
    DEFAULT_MIGRATION_LEASE_SECONDS,
    compose_production_migration_operations,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime
    from pathlib import Path


def compose_pp_server_backend_migration_operations(
    *,
    observation_adapter: MigrationObservationAdapter,
    edge_compute_adapter: EdgeComputeMigrationAdapter,
    canonical_state_adapter: CanonicalStateMigrationAdapter,
    collaboration_schema_adapter: CollaborationSchemaMigrationAdapter,
    runtime_adapter: RuntimeMigrationAdapter,
    derived_index_adapter: DerivedIndexMigrationAdapter,
    maintenance_adapter: MaintenanceMigrationAdapter,
    retention_cache_adapter: RetentionCacheMigrationAdapter,
    canonical_database_path: str | Path | None = None,
    journal_owner_ref: str = "pp-server-backend",
    environment: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
    plan_ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS,
    observation_max_age_seconds: int = DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
    migration_lease_seconds: int = DEFAULT_MIGRATION_LEASE_SECONDS,
) -> MigrationOperations:
    """Compose the server-owned migration authority without executing it.

    The server role and canonical database path are explicit availability
    gates.  Missing or mismatched configuration fails closed before the
    durable journal is opened; no in-memory journal or placeholder adapter is
    substituted.
    """

    values = os.environ if environment is None else environment
    endpoint_role = str(values.get("PP_ENDPOINT_ROLE") or "").strip()
    if endpoint_role != "pp-server-backend":
        raise MigrationOperationsError(
            "migration_server_backend_role_required",
            category="unavailable",
        )

    if canonical_database_path is None:
        configured_path = str(values.get("PLASTIC_DB_PATH") or "").strip()
        if not configured_path:
            raise MigrationOperationsError(
                "migration_canonical_database_path_required",
                category="unavailable",
            )
        canonical_database_path = configured_path

    return compose_production_migration_operations(
        canonical_database_path=canonical_database_path,
        journal_owner_ref=journal_owner_ref,
        observation_adapter=observation_adapter,
        edge_compute_adapter=edge_compute_adapter,
        canonical_state_adapter=canonical_state_adapter,
        collaboration_schema_adapter=collaboration_schema_adapter,
        runtime_adapter=runtime_adapter,
        derived_index_adapter=derived_index_adapter,
        maintenance_adapter=maintenance_adapter,
        retention_cache_adapter=retention_cache_adapter,
        clock=clock,
        plan_ttl_seconds=plan_ttl_seconds,
        observation_max_age_seconds=observation_max_age_seconds,
        migration_lease_seconds=migration_lease_seconds,
    )


# A concise alias for callers that already name the process ``server``.
compose_server_backend_migration_operations = compose_pp_server_backend_migration_operations


__all__ = [
    "compose_pp_server_backend_migration_operations",
    "compose_server_backend_migration_operations",
]
