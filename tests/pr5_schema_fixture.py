"""Shared PR5 test installer using the production migration authority path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from plastic_promise.deployment.collaboration_schema_migration import (
    COLLABORATION_SCHEMA_MANIFEST,
    COLLABORATION_SCHEMA_MANIFEST_SHA256,
    CollaborationSchemaInstallReceipt,
    CollaborationSchemaMigration,
    bind_canonical_backup_receipt,
)
from plastic_promise.deployment.migration_journal import (
    InMemoryMigrationExecutionJournal,
    MigrationExecutionIdentity,
)
from plastic_promise.deployment.migration_operations import OPERATION_PHASE_MANIFEST

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from contextlib import AbstractContextManager


def _manifest_digest(manifest: tuple[str, ...]) -> str:
    payload = json.dumps(
        list(manifest),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


PHASE_MANIFEST_SHA256 = _manifest_digest(OPERATION_PHASE_MANIFEST)


@dataclass(frozen=True, slots=True)
class _Plan:
    installation_ref: str
    operation_ref: str
    plan_hash: str
    phase_manifest: tuple[str, ...] = OPERATION_PHASE_MANIFEST
    phase_manifest_sha256: str = PHASE_MANIFEST_SHA256
    schema_manifest: tuple[str, ...] = COLLABORATION_SCHEMA_MANIFEST
    schema_manifest_sha256: str = COLLABORATION_SCHEMA_MANIFEST_SHA256


@dataclass(frozen=True, slots=True)
class _Context:
    plan: _Plan
    lease: object


def install_pr5_collaboration_schema(
    connection: sqlite3.Connection,
    *,
    transaction_factory: Callable[[], AbstractContextManager[None]],
    clock: Callable[[], datetime],
    suffix: str,
) -> CollaborationSchemaInstallReceipt:
    """Install the complete PR5 schema through the deployment-owned seam."""

    connection.execute("PRAGMA foreign_keys = ON")

    now = clock()
    plan = _Plan(
        installation_ref=f"installation:{suffix}",
        operation_ref=f"operation:{suffix}",
        plan_hash=f"sha256:{hashlib.sha256(f'plan:{suffix}'.encode()).hexdigest()}",
    )
    identity = MigrationExecutionIdentity(
        installation_ref=plan.installation_ref,
        operation_ref=plan.operation_ref,
        plan_hash=plan.plan_hash,
        phase_manifest=plan.phase_manifest,
        phase_manifest_sha256=plan.phase_manifest_sha256,
        schema_manifest=plan.schema_manifest,
        schema_manifest_sha256=plan.schema_manifest_sha256,
        grant_id=f"grant:{suffix}",
        grant_issued_at=now - timedelta(minutes=1),
        grant_expires_at=now + timedelta(hours=1),
    )
    journal = InMemoryMigrationExecutionJournal(owner_ref="pp-core-test")
    journal.register_grant(identity)
    lease = journal.begin(
        identity,
        now=now,
        lease_expires_at=now + timedelta(minutes=30),
    )
    context = _Context(plan=plan, lease=lease)
    backup_receipt = bind_canonical_backup_receipt(
        context,
        backup_receipt_sha256=(f"sha256:{hashlib.sha256(f'backup:{suffix}'.encode()).hexdigest()}"),
        completed_at=now,
    )
    return CollaborationSchemaMigration(
        connection,
        transaction_factory=transaction_factory,
        clock=clock,
    ).install(context, backup_receipt)


__all__ = ["install_pr5_collaboration_schema"]
