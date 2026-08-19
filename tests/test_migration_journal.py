"""Focused persistence tests for the pp-core migration execution journal."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.deployment.collaboration_schema_migration import (
    COLLABORATION_SCHEMA_MANIFEST,
    COLLABORATION_SCHEMA_MANIFEST_SHA256,
)
from plastic_promise.deployment.migration_journal import (
    MigrationExecutionIdentity,
    MigrationJournalError,
    MigrationPhaseReceipt,
    SQLiteMigrationExecutionJournal,
    migration_journal_schema_present,
)
from plastic_promise.deployment.migration_operations import OPERATION_PHASE_MANIFEST
from plastic_promise.deployment.sqlite_migrations import apply_deployment_migrations

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
PLAN_HASH = "sha256:" + "a" * 64


def _manifest_digest(manifest: tuple[str, ...]) -> str:
    payload = json.dumps(
        list(manifest),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


PHASE_MANIFEST_SHA256 = _manifest_digest(OPERATION_PHASE_MANIFEST)


def _identity(
    *,
    grant_id: str = "grant-one",
    operation_ref: str = "migration-one",
) -> MigrationExecutionIdentity:
    return MigrationExecutionIdentity(
        installation_ref="installation-one",
        operation_ref=operation_ref,
        plan_hash=PLAN_HASH,
        phase_manifest=OPERATION_PHASE_MANIFEST,
        phase_manifest_sha256=PHASE_MANIFEST_SHA256,
        schema_manifest=COLLABORATION_SCHEMA_MANIFEST,
        schema_manifest_sha256=COLLABORATION_SCHEMA_MANIFEST_SHA256,
        grant_id=grant_id,
        grant_issued_at=NOW,
        grant_expires_at=NOW + timedelta(minutes=5),
    )


def _journal(tmp_path, *, owner_ref: str = "pp-core-owner-a", initialize: bool = True):
    return SQLiteMigrationExecutionJournal(
        tmp_path / "canonical.db",
        owner_ref=owner_ref,
        initialize_schema=initialize,
    )


def test_sqlite_journal_requires_explicit_schema_initialization(tmp_path):
    journal = _journal(tmp_path, initialize=False)

    with pytest.raises(MigrationJournalError, match="migration_journal_schema_missing"):
        journal.register_grant(_identity())


def test_sqlite_journal_persists_terminal_receipt_and_rejects_restart_replay(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)
    lease = journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=10),
    )
    journal.assert_current(lease, now=NOW + timedelta(seconds=1))
    receipt = {"outcome": "applied", "plan_hash": PLAN_HASH, "phases": []}
    journal.complete(lease, outcome="applied", receipt=receipt, now=NOW + timedelta(seconds=2))

    restarted = _journal(tmp_path, owner_ref="pp-core-owner-b", initialize=False)
    second = _identity(grant_id="grant-two")
    restarted.register_grant(second)
    with pytest.raises(MigrationJournalError, match="migration_operation_replayed"):
        restarted.begin(
            second,
            now=NOW + timedelta(seconds=3),
            lease_expires_at=NOW + timedelta(minutes=10),
        )

    with sqlite3.connect(tmp_path / "canonical.db") as connection:
        row = connection.execute(
            "SELECT status, receipt_json FROM pp_migration_operations"
        ).fetchone()
    assert row is not None
    assert row[0] == "applied"
    assert json.loads(row[1]) == receipt


def test_sqlite_journal_serializes_installation_and_increments_fence(tmp_path):
    journal = _journal(tmp_path)
    first = _identity()
    second = _identity(grant_id="grant-two", operation_ref="migration-two")
    journal.register_grant(first)
    journal.register_grant(second)
    first_lease = journal.begin(
        first,
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(MigrationJournalError, match="migration_operation_active"):
        journal.begin(
            second,
            now=NOW + timedelta(seconds=1),
            lease_expires_at=NOW + timedelta(minutes=10),
        )

    journal.complete(
        first_lease,
        outcome="applied",
        receipt={"outcome": "applied"},
        now=NOW + timedelta(seconds=2),
    )
    second_lease = journal.begin(
        second,
        now=NOW + timedelta(seconds=3),
        lease_expires_at=NOW + timedelta(minutes=10),
    )
    assert second_lease.fencing_generation == first_lease.fencing_generation + 1


def test_journal_timestamp_ordering_is_stable_within_one_second(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)
    lease = journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(microseconds=500_000),
    )

    journal.assert_current(lease, now=NOW + timedelta(microseconds=250_000))
    with pytest.raises(MigrationJournalError, match="migration_operation_fence_lost"):
        journal.complete(
            lease,
            outcome="applied",
            receipt={"outcome": "applied"},
            now=NOW + timedelta(microseconds=750_000),
        )


def test_sqlite_journal_marks_expired_running_operation_for_recovery(tmp_path):
    journal = _journal(tmp_path)
    first = _identity()
    second = _identity(grant_id="grant-two", operation_ref="migration-two")
    journal.register_grant(first)
    journal.register_grant(second)
    lease = journal.begin(
        first,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(MigrationJournalError, match="migration_operation_recovery_required"):
        journal.begin(
            second,
            now=NOW + timedelta(seconds=2),
            lease_expires_at=NOW + timedelta(minutes=10),
        )
    with pytest.raises(MigrationJournalError, match="migration_operation_fence_lost"):
        journal.assert_current(lease, now=NOW + timedelta(seconds=2))

    with sqlite3.connect(tmp_path / "canonical.db") as connection:
        status = connection.execute(
            "SELECT status FROM pp_migration_operations WHERE operation_ref = 'migration-one'"
        ).fetchone()
    assert status == ("recovery-required",)


def test_sqlite_journal_rejects_unissued_or_mismatched_grant(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    with pytest.raises(MigrationJournalError, match="migration_grant_unissued"):
        journal.begin(
            identity,
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=10),
        )

    journal.register_grant(identity)
    mismatched = replace(identity, plan_hash="sha256:" + "b" * 64)
    with pytest.raises(MigrationJournalError, match="migration_grant_registration_conflict"):
        journal.begin(
            mismatched,
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=10),
        )


def test_sqlite_journal_rejects_expired_lease_acquisition_and_completion(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)

    with pytest.raises(MigrationJournalError, match="migration_lease_expiry_invalid"):
        journal.begin(identity, now=NOW, lease_expires_at=NOW)

    lease = journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(MigrationJournalError, match="migration_operation_fence_lost"):
        journal.complete(
            lease,
            outcome="applied",
            receipt={"outcome": "applied"},
            now=NOW + timedelta(seconds=2),
        )


def test_sqlite_journal_persists_ordered_phase_receipts_and_is_idempotent(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)
    lease = journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=10),
    )

    journal.record_phase(
        lease,
        phase_index=0,
        phase="stage-verify-edge-compute",
        outcome="completed",
        reason_code="migration_phase_completed",
        now=NOW + timedelta(seconds=1),
    )
    # Replaying the exact phase receipt is safe; a different receipt at the
    # same ordered slot is a durable conflict.
    journal.record_phase(
        lease,
        phase_index=0,
        phase="stage-verify-edge-compute",
        outcome="completed",
        reason_code="migration_phase_completed",
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(MigrationJournalError, match="migration_phase_registration_conflict"):
        journal.record_phase(
            lease,
            phase_index=0,
            phase="stage-verify-edge-compute",
            outcome="failed",
            reason_code="migration_phase_failed",
            now=NOW + timedelta(seconds=2),
        )

    assert journal.list_phase_records(lease.operation_id) == (
        MigrationPhaseReceipt(
            operation_id=lease.operation_id,
            phase_index=0,
            phase="stage-verify-edge-compute",
            outcome="completed",
            reason_code="migration_phase_completed",
            phase_manifest_sha256=PHASE_MANIFEST_SHA256,
            schema_manifest_sha256=COLLABORATION_SCHEMA_MANIFEST_SHA256,
            completed_at=NOW + timedelta(seconds=1),
        ),
    )


def test_sqlite_journal_rejects_phase_write_after_fence_expiry(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)
    lease = journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(MigrationJournalError, match="migration_operation_fence_lost"):
        journal.record_phase(
            lease,
            phase_index=0,
            phase="stage-verify-edge-compute",
            outcome="completed",
            reason_code="migration_phase_completed",
            now=NOW + timedelta(seconds=2),
        )


def test_sqlite_journal_rejects_wrong_phase_index_name_and_foreign_manifest(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)
    lease = journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(MigrationJournalError, match="migration_phase_index_manifest_mismatch"):
        journal.record_phase(
            lease,
            phase_index=1000,
            phase="stage-verify-edge-compute",
            outcome="completed",
            reason_code="migration_phase_completed",
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(MigrationJournalError, match="migration_phase_name_manifest_mismatch"):
        journal.record_phase(
            lease,
            phase_index=0,
            phase="canonical-rehearsal",
            outcome="completed",
            reason_code="migration_phase_completed",
            now=NOW + timedelta(seconds=1),
        )

    foreign_schema_manifest = ("schema:foreign",)
    foreign = replace(
        lease,
        schema_manifest=foreign_schema_manifest,
        schema_manifest_sha256=_manifest_digest(foreign_schema_manifest),
    )
    with pytest.raises(MigrationJournalError, match="migration_operation_fence_lost"):
        journal.record_phase(
            foreign,
            phase_index=0,
            phase="stage-verify-edge-compute",
            outcome="completed",
            reason_code="migration_phase_completed",
            now=NOW + timedelta(seconds=1),
        )


def test_sqlite_journal_rejects_secret_bearing_receipts(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)
    lease = journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(MigrationJournalError, match="migration_receipt_secret_detected"):
        journal.complete(
            lease,
            outcome="applied",
            receipt={"provider_token": "ghp_abcdefghijklmnopqrstuvwxyz123456"},
            now=NOW + timedelta(seconds=1),
        )


def test_sqlite_journal_explicitly_marks_expired_operations_for_restart_recovery(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)
    journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=1),
    )

    expected = (
        "migration-operation:"
        + hashlib.sha256(
            "\x1f".join(("installation-one", "migration-one", PLAN_HASH, "grant-one")).encode()
        ).hexdigest()
    )
    assert journal.recover_expired(now=NOW + timedelta(seconds=2)) == (expected,)


def test_recovery_required_blocks_new_operation_until_explicit_recovery(tmp_path):
    journal = _journal(tmp_path)
    first = _identity()
    journal.register_grant(first)
    lease = journal.begin(
        first,
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=10),
    )
    journal.complete(
        lease,
        outcome="recovery-required",
        receipt={"outcome": "recovery-required"},
        now=NOW + timedelta(seconds=1),
    )

    second = _identity(grant_id="grant-two", operation_ref="migration-two")
    journal.register_grant(second)
    with pytest.raises(MigrationJournalError, match="migration_operation_recovery_required"):
        journal.begin(
            second,
            now=NOW + timedelta(seconds=2),
            lease_expires_at=NOW + timedelta(minutes=10),
        )


def test_restart_preserves_exact_operation_manifests(tmp_path):
    journal = _journal(tmp_path)
    identity = _identity()
    journal.register_grant(identity)
    journal.begin(
        identity,
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=10),
    )

    with sqlite3.connect(tmp_path / "canonical.db") as connection:
        row = connection.execute(
            "SELECT phase_manifest_json, phase_manifest_sha256, "
            "schema_manifest_json, schema_manifest_sha256 "
            "FROM pp_migration_operations"
        ).fetchone()
    assert row == (
        json.dumps(list(OPERATION_PHASE_MANIFEST), separators=(",", ":")),
        PHASE_MANIFEST_SHA256,
        json.dumps(list(COLLABORATION_SCHEMA_MANIFEST), separators=(",", ":")),
        COLLABORATION_SCHEMA_MANIFEST_SHA256,
    )


def test_controlled_deployment_migration_installs_journal_once(tmp_path):
    database_path = tmp_path / "canonical.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        first = apply_deployment_migrations(connection)
        connection.commit()
        assert migration_journal_schema_present(connection)

        connection.execute("BEGIN IMMEDIATE")
        second = apply_deployment_migrations(connection)
        connection.commit()

    assert first == (
        "node-governance-v2",
        "migration-execution-journal-v2",
        "production-readiness-schema-v1",
        "memory-proposal-promotion-tasks-v1",
    )
    assert second == ()


def test_controlled_deployment_migration_installs_readiness_and_promotion_tables(tmp_path):
    database_path = tmp_path / "canonical.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        applied = apply_deployment_migrations(connection)
        connection.commit()

        assert "production-readiness-schema-v1" in applied
        assert "memory-proposal-promotion-tasks-v1" in applied
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "production_evidence_attestations",
            "memory_proposal_promotion_tasks",
        }.issubset(tables)
        assert {
            row[1]
            for row in connection.execute("PRAGMA table_info(production_evidence_attestations)")
        } == {
            "attestation_id",
            "subject_path_sha256",
            "evidence_sha256",
            "issuer",
            "signature",
            "status",
            "issued_at",
            "expires_at",
        }
        promotion_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(memory_proposal_promotion_tasks)")
        }
        assert {
            "task_id",
            "proposal_id",
            "project_id",
            "lease_token_hash",
            "next_attempt_at",
            "idempotency_key",
        }.issubset(promotion_columns)
