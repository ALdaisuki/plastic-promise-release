"""Fail-closed production composition for server-owned migrations.

This is the only production construction seam for :class:`MigrationOperations`.
It never creates or migrates the canonical database, never substitutes an
in-memory journal, and requires every observation and mutation adapter from
the pp-server-backend composition root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .migration_journal import (
    DEFAULT_MIGRATION_LEASE_SECONDS,
    MigrationJournalError,
    SQLiteMigrationExecutionJournal,
)
from .migration_operations import (
    DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
    DEFAULT_PLAN_TTL_SECONDS,
    CanonicalStateMigrationAdapter,
    CollaborationSchemaMigrationAdapter,
    DerivedIndexMigrationAdapter,
    EdgeComputeMigrationAdapter,
    MaintenanceMigrationAdapter,
    MigrationAdapters,
    MigrationObservationAdapter,
    MigrationOperations,
    MigrationOperationsError,
    RetentionCacheMigrationAdapter,
    RuntimeMigrationAdapter,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path


def compose_production_migration_operations(
    *,
    canonical_database_path: str | Path,
    journal_owner_ref: str,
    observation_adapter: MigrationObservationAdapter,
    edge_compute_adapter: EdgeComputeMigrationAdapter,
    canonical_state_adapter: CanonicalStateMigrationAdapter,
    collaboration_schema_adapter: CollaborationSchemaMigrationAdapter,
    runtime_adapter: RuntimeMigrationAdapter,
    derived_index_adapter: DerivedIndexMigrationAdapter,
    maintenance_adapter: MaintenanceMigrationAdapter,
    retention_cache_adapter: RetentionCacheMigrationAdapter,
    clock: Callable[[], datetime] | None = None,
    plan_ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS,
    observation_max_age_seconds: int = DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
    migration_lease_seconds: int = DEFAULT_MIGRATION_LEASE_SECONDS,
) -> MigrationOperations:
    """Compose production migration authority from durable, explicit parts.

    The canonical database must already exist at an absolute path and contain
    the migration-journal schema installed by the controlled SQLite migration
    path. Missing state is an availability error, not permission to create a
    second truth source.
    """

    try:
        execution_journal = SQLiteMigrationExecutionJournal.open_existing(
            canonical_database_path,
            owner_ref=journal_owner_ref,
        )
    except MigrationJournalError as exc:
        raise MigrationOperationsError(exc.code, category="unavailable") from None

    adapters = MigrationAdapters(
        edge_compute=edge_compute_adapter,
        canonical_state=canonical_state_adapter,
        collaboration_schema=collaboration_schema_adapter,
        runtime=runtime_adapter,
        derived_index=derived_index_adapter,
        maintenance=maintenance_adapter,
        retention_cache=retention_cache_adapter,
    )
    return MigrationOperations(
        observation_adapter,
        adapters,
        execution_journal=execution_journal,
        clock=clock,
        plan_ttl_seconds=plan_ttl_seconds,
        observation_max_age_seconds=observation_max_age_seconds,
        migration_lease_seconds=migration_lease_seconds,
    )


__all__ = ["compose_production_migration_operations"]
