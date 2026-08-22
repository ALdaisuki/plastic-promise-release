#!/usr/bin/env python3
"""Install the PR5 collaboration schema into the canonical SQLite database.

Deployment-owned one-shot migration (union-six-pr PR5). Takes a full online
backup of the target database into <db>.pre-collab-schema.bak before
installing, installs every collaboration schema participant in one caller
transaction with a typed backup receipt, then verifies schema presence.

Usage:
    PYTHONPATH=<repo-root> .venv/bin/python scripts/collab_schema_migrate.py <db-path>

Idempotent: re-running against an installed database validates the existing
install receipt and returns it.
"""

import hashlib, json, sqlite3, sys
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from plastic_promise.deployment.collaboration_schema_migration import (
    COLLABORATION_SCHEMA_MANIFEST,
    COLLABORATION_SCHEMA_MANIFEST_SHA256,
    CollaborationSchemaMigration,
    bind_canonical_backup_receipt,
    collaboration_schema_present,
)
from plastic_promise.deployment.migration_operations import OPERATION_PHASE_MANIFEST

DB_PATH = sys.argv[1]
NOW = datetime.now(timezone.utc).replace(microsecond=0)

def digest(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

def manifest_digest(manifest):
    payload = json.dumps(list(manifest), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()

@dataclass(frozen=True)
class Plan:
    installation_ref: str = "installation-mac-server-001"
    operation_ref: str = "migration-collab-schema-001"
    plan_hash: str = digest("plan-collab-schema-001")
    phase_manifest: tuple = OPERATION_PHASE_MANIFEST
    phase_manifest_sha256: str = manifest_digest(OPERATION_PHASE_MANIFEST)
    schema_manifest: tuple = COLLABORATION_SCHEMA_MANIFEST
    schema_manifest_sha256: str = COLLABORATION_SCHEMA_MANIFEST_SHA256

@dataclass(frozen=True)
class Lease:
    operation_id: str = "migration-operation:collab-schema-001"
    plan_hash: str = digest("plan-collab-schema-001")
    grant_id: str = "grant-collab-schema-001"
    fencing_generation: int = 7
    phase_manifest_sha256: str = manifest_digest(OPERATION_PHASE_MANIFEST)
    schema_manifest_sha256: str = COLLABORATION_SCHEMA_MANIFEST_SHA256

@dataclass(frozen=True)
class Context:
    plan: Plan = Plan()
    lease: Lease = Lease()

class TxFactory:
    def __init__(self, conn): self.conn = conn
    def __call__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __enter__(self): return None
    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        return False

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = ON")
migration = CollaborationSchemaMigration(conn, transaction_factory=TxFactory(conn))
context = Context()

backup_file = DB_PATH + ".pre-collab-schema.bak"
src = sqlite3.connect(DB_PATH)
dst = sqlite3.connect(backup_file)
src.backup(dst)
dst.close()
src.close()
backup_digest = "sha256:" + hashlib.sha256(open(backup_file, "rb").read()).hexdigest()
receipt = bind_canonical_backup_receipt(
    context,
    backup_receipt_sha256=backup_digest,
    completed_at=NOW,
)
r = migration.install(context, receipt)
print("INSTALL-OK revision:", getattr(r, "schema_revision", "?"))
print("schema_present:", collaboration_schema_present(conn))
conn.close()
