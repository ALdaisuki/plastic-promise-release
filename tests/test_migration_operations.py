"""Focused tests for the server-owned migration operation seam."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.deployment.collaboration_schema_migration import (
    COLLABORATION_SCHEMA_MANIFEST,
    COLLABORATION_SCHEMA_MANIFEST_SHA256,
    CollaborationSchemaMigration,
    bind_canonical_backup_receipt,
)
from plastic_promise.deployment.container_artifacts import (
    COMPUTE_VARIANT_CPU,
    COMPUTE_VARIANT_CUDA,
    ArtifactEvidenceReceipt,
    ArtifactMaterialization,
    ArtifactRequest,
    ContainerArtifactCompiler,
)
from plastic_promise.deployment.endpoint_contract import (
    DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION,
    resolve_deployment_manifest_v2,
)
from plastic_promise.deployment.migration_journal import SQLiteMigrationExecutionJournal
from plastic_promise.deployment.migration_operations import (
    OPERATION_PHASE_MANIFEST,
    CanonicalStateObservation,
    DerivedGenerationObservation,
    MigrationAdapters,
    MigrationExecutionGrant,
    MigrationIntent,
    MigrationObservations,
    MigrationOperationPlan,
    MigrationOperations,
    MigrationOperationsError,
    NodeReadinessObservation,
    RuntimeObservation,
)
from plastic_promise.deployment.production_migration import (
    compose_production_migration_operations,
)
from plastic_promise.mcp.server_composition import (
    compose_pp_server_backend_migration_operations,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _manifest() -> dict[str, object]:
    gib = 1024**3
    return {
        "schema_version": DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION,
        "deployment_id": "developer-laptop",
        "profile": "split-accelerated",
        "modules": {},
        "endpoints": [
            {
                "id": "local-edge",
                "role": "pp-local-edge",
                "protocol": {"family": "edge", "major": 1, "minor": 0},
                "capabilities": [],
                "transport_ref": "loopback",
                "resource_policy_ref": "edge-default",
            },
            {
                "id": "server-backend",
                "role": "pp-server-backend",
                "protocol": {"family": "backend", "major": 1, "minor": 0},
                "capabilities": [],
                "transport_ref": "backend-private",
                "resource_policy_ref": "backend-default",
            },
            {
                "id": "compute-node",
                "role": "pp-compute-node",
                "protocol": {"family": "compute", "major": 1, "minor": 0},
                "capabilities": [
                    {"kind": "embedding", "contract_version": "embedding/v1"},
                    {"kind": "rerank", "contract_version": "rerank/v1"},
                ],
                "max_concurrency": 4,
                "transport_ref": "compute-registry",
                "resource_policy_ref": "compute-default",
            },
        ],
        "resource_budget": {
            "image_layers_bytes": gib,
            "image_unpack_bytes": gib,
            "model_cache_bytes": gib,
            "lancedb_shadow_rebuild_bytes": gib,
            "rollback_coexistence_bytes": gib,
        },
        "resource_locations": {
            "container_store": "container-store",
            "model_cache": "model-cache",
        },
    }


def _bundle(*, profile_id: str = "split-accelerated"):
    compiler = ContainerArtifactCompiler()
    accelerated = profile_id == "split-accelerated"
    request = ArtifactRequest(
        profile_id=profile_id,
        source_revision="a" * 40,
        package_version="0.8.0rc1",
        platforms=("linux/amd64", "linux/arm64") if accelerated else ("linux/amd64",),
        compute_variants=(COMPUTE_VARIANT_CPU, COMPUTE_VARIANT_CUDA) if accelerated else (),
        model_catalog_reference="catalog-v1" if accelerated else None,
        model_catalog_digest=_digest("catalog") if accelerated else None,
    )
    plan = compiler.prepare(request)

    class Executor:
        def materialize(self, artifact_plan, artifact):  # type: ignore[no-untyped-def]
            image = _digest(f"image:{artifact.artifact_id}")
            labels = json.dumps(
                artifact_plan.expected_oci_labels(artifact.artifact_id),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            labels_digest = _digest(labels)
            oci_layout_digest = _digest(f"layout:{artifact.artifact_id}")
            sbom_digest = _digest(f"sbom:{artifact.artifact_id}")
            provenance_digest = _digest(f"provenance:{artifact.artifact_id}")
            evidence = ArtifactEvidenceReceipt(
                artifact_id=artifact.artifact_id,
                role=artifact.role,
                platform=artifact.platform,
                variant=artifact.variant,
                source_revision=artifact_plan.request.source_revision,
                package_version=artifact_plan.request.package_version,
                base_image_reference=artifact.base_image_reference,
                recipe_policy_digest=artifact_plan.recipe_policy_digest,
                policy_digest=artifact_plan.policy_digest,
                collaboration_surface_digest=artifact.collaboration_surface_digest,
                application_inventory_digest=_digest(f"inventory:{artifact.artifact_id}"),
                oci_layout_digest=oci_layout_digest,
                image_digest=image,
                oci_labels_digest=labels_digest,
                sbom_digest=sbom_digest,
                sbom_subject_digest=image,
                provenance_digest=provenance_digest,
                provenance_subject_digest=image,
            )
            return ArtifactMaterialization(
                artifact_id=artifact.artifact_id,
                role=artifact.role,
                platform=artifact.platform,
                variant=artifact.variant,
                immutable_reference=f"oci@{image}",
                image_digest=image,
                oci_layout_digest=oci_layout_digest,
                oci_labels_digest=labels_digest,
                sbom_digest=sbom_digest,
                provenance_digest=provenance_digest,
                evidence_receipt=evidence,
            )

    return compiler.materialize(plan, Executor())


class _Observer:
    def __init__(self, observations: MigrationObservations) -> None:
        self.observations = observations
        self.calls = 0

    def observe(self, topology, installation_ref):  # type: ignore[no-untyped-def]
        self.calls += 1
        assert installation_ref == "test-installation"
        return self.observations


class _Adapters:
    def __init__(
        self,
        *,
        fail: str | tuple[str, ...] | None = None,
        on_event=None,
    ) -> None:  # type: ignore[no-untyped-def]
        self.events: list[str] = []
        self.failures = {fail} if isinstance(fail, str) else set(fail or ())
        self.on_event = on_event
        self.schema_connection = sqlite3.connect(":memory:")
        self.schema_connection.execute("PRAGMA foreign_keys = ON")
        self.schema_migration = CollaborationSchemaMigration(
            self.schema_connection,
            transaction_factory=self._schema_transaction,
            clock=lambda: NOW,
        )

    @contextmanager
    def _schema_transaction(self):  # type: ignore[no-untyped-def]
        self.schema_connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.schema_connection.rollback()
            raise
        else:
            self.schema_connection.commit()

    def _run(self, name: str) -> None:
        self.events.append(name)
        if self.on_event is not None:
            self.on_event(name)
        if name in self.failures:
            raise RuntimeError("adapter failure must not leak")

    def stage_and_verify(self, plan):  # type: ignore[no-untyped-def]
        self._run("stage_and_verify")

    def rehearse(self, plan):  # type: ignore[no-untyped-def]
        self._run("rehearse")

    def backup_and_migrate(self, plan):  # type: ignore[no-untyped-def]
        self._run("backup_and_migrate")
        return bind_canonical_backup_receipt(
            plan,
            backup_receipt_sha256=_digest("canonical-backup"),
            completed_at=NOW,
        )

    def install(self, plan, backup_receipt):  # type: ignore[no-untyped-def]
        self._run("install_collaboration_schema")
        return self.schema_migration.install(plan, backup_receipt)

    def restore(self, plan):  # type: ignore[no-untyped-def]
        self._run("restore")

    def stop_legacy(self, plan):  # type: ignore[no-untyped-def]
        self._run("stop_legacy")

    def start_backend(self, plan):  # type: ignore[no-untyped-def]
        self._run("start_backend")

    def stop_backend(self, plan):  # type: ignore[no-untyped-def]
        self._run("stop_backend")

    def restart_legacy(self, plan):  # type: ignore[no-untyped-def]
        self._run("restart_legacy")

    def shadow_rebuild_verify_promote(self, plan):  # type: ignore[no-untyped-def]
        self._run("shadow_rebuild_verify_promote")

    def revert_selection(self, plan):  # type: ignore[no-untyped-def]
        self._run("revert_selection")

    def enable(self, plan):  # type: ignore[no-untyped-def]
        self._run("enable")

    def disable(self, plan):  # type: ignore[no-untyped-def]
        self._run("disable")

    def apply(self, plan):  # type: ignore[no-untyped-def]
        self._run("retention_cache")


def _observations(*, ready: bool = True) -> MigrationObservations:
    return MigrationObservations(
        canonical=CanonicalStateObservation(1, _digest("canonical"), True, observed_at=NOW),
        runtime=RuntimeObservation(1, True, False, _digest("runtime"), observed_at=NOW),
        nodes=NodeReadinessObservation(1, ready, _digest("nodes"), observed_at=NOW),
        derived=DerivedGenerationObservation(1, _digest("derived"), True, observed_at=NOW),
    )


def _operation(
    *,
    observations: MigrationObservations | None = None,
    fail: str | tuple[str, ...] | None = None,
    clock=None,  # type: ignore[no-untyped-def]
    on_event=None,  # type: ignore[no-untyped-def]
    execution_journal=None,  # type: ignore[no-untyped-def]
):
    observer = _Observer(observations or _observations())
    adapters = _Adapters(fail=fail, on_event=on_event)
    operations = MigrationOperations(
        observer,
        MigrationAdapters(
            edge_compute=adapters,
            canonical_state=adapters,
            collaboration_schema=adapters,
            runtime=adapters,
            derived_index=adapters,
            maintenance=adapters,
            retention_cache=adapters,
        ),
        execution_journal=execution_journal,
        clock=clock or (lambda: NOW),
    )
    intent = MigrationIntent(
        topology=resolve_deployment_manifest_v2(_manifest()),
        artifact_bundle=_bundle(),
        installation_ref="test-installation",
        operation_ref="test-migration",
    )
    return operations, observer, adapters, operations.plan(intent)


def _production_operation(database_path, *, initialize_schema: bool = True):  # type: ignore[no-untyped-def]
    if initialize_schema:
        SQLiteMigrationExecutionJournal(
            database_path,
            owner_ref="schema-installer",
            initialize_schema=True,
        )
    observer = _Observer(_observations())
    adapters = _Adapters()
    operations = compose_production_migration_operations(
        canonical_database_path=database_path,
        journal_owner_ref="pp-server-backend",
        observation_adapter=observer,
        edge_compute_adapter=adapters,
        canonical_state_adapter=adapters,
        collaboration_schema_adapter=adapters,
        runtime_adapter=adapters,
        derived_index_adapter=adapters,
        maintenance_adapter=adapters,
        retention_cache_adapter=adapters,
        clock=lambda: NOW,
    )
    return operations, observer, adapters


def _digest_only_plan() -> MigrationOperationPlan:
    observations = _observations()
    expires_at = NOW + timedelta(seconds=300)
    topology_digest = _digest("topology")
    artifact_bundle_digest = _digest("artifact-bundle")
    phase_manifest_sha256 = _canonical_digest(list(OPERATION_PHASE_MANIFEST))
    plan_hash = _canonical_digest(
        {
            "schema_version": "plastic-promise-migration-operations/v2",
            "operation_ref": "test-migration",
            "installation_ref": "test-installation",
            "topology_digest": topology_digest,
            "artifact_bundle_digest": artifact_bundle_digest,
            "observation_digest": observations.digest,
            "created_at": "2026-08-08T12:00:00Z",
            "expires_at": "2026-08-08T12:05:00Z",
            "phase_manifest": list(OPERATION_PHASE_MANIFEST),
            "phase_manifest_sha256": phase_manifest_sha256,
            "schema_manifest": list(COLLABORATION_SCHEMA_MANIFEST),
            "schema_manifest_sha256": COLLABORATION_SCHEMA_MANIFEST_SHA256,
        }
    )
    return MigrationOperationPlan(
        operation_ref="test-migration",
        installation_ref="test-installation",
        topology_digest=topology_digest,
        artifact_bundle_digest=artifact_bundle_digest,
        observations=observations,
        created_at=NOW,
        expires_at=expires_at,
        phase_manifest=OPERATION_PHASE_MANIFEST,
        phase_manifest_sha256=phase_manifest_sha256,
        schema_manifest=COLLABORATION_SCHEMA_MANIFEST,
        schema_manifest_sha256=COLLABORATION_SCHEMA_MANIFEST_SHA256,
        plan_hash=plan_hash,
    )


def _grant(
    plan,
    *,
    plan_hash: str | None = None,
    grant_id: str = "grant-test",
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
):  # type: ignore[no-untyped-def]
    return MigrationExecutionGrant(
        plan_hash=plan_hash or plan.plan_hash,
        grant_id=grant_id,
        issued_at=issued_at or plan.created_at,
        expires_at=expires_at or NOW + timedelta(seconds=60),
    )


def _issued_grant(
    operations: MigrationOperations,
    plan,
    *,
    grant_id: str = "grant-test",
):  # type: ignore[no-untyped-def]
    return operations.register_grant(plan, _grant(plan, grant_id=grant_id))


def test_dry_run_invokes_zero_mutable_methods():
    operations, _observer, adapters, plan = _operation()

    result = operations.apply(plan, _grant(plan), dry_run=True)

    assert result.accepted is True
    assert result.outcome == "dry-run"
    assert adapters.events == []


def test_preflight_rejects_without_mutations():
    operations, _observer, adapters, plan = _operation(observations=_observations(ready=False))

    result = operations.apply(plan, _grant(plan), dry_run=True)

    assert result.accepted is False
    assert result.reason_code == "migration_nodes_not_ready"
    assert adapters.events == []


def test_observation_drift_is_rejected_before_first_mutation():
    operations, observer, adapters, plan = _operation()
    observer.observations = replace(
        observer.observations,
        runtime=replace(observer.observations.runtime, runtime_digest=_digest("changed")),
    )

    result = operations.apply(plan, _grant(plan))

    assert result.accepted is False
    assert result.reason_code == "migration_observation_drift"
    assert adapters.events == []


def test_happy_path_uses_the_fixed_phase_order():
    operations, _observer, adapters, plan = _operation()

    result = operations.apply(plan, _issued_grant(operations, plan))

    assert result.accepted is True
    assert adapters.events == [
        "stage_and_verify",
        "rehearse",
        "stop_legacy",
        "backup_and_migrate",
        "install_collaboration_schema",
        "start_backend",
        "shadow_rebuild_verify_promote",
        "enable",
        "retention_cache",
    ]
    assert plan.schema_manifest == COLLABORATION_SCHEMA_MANIFEST
    assert plan.schema_manifest_sha256 == COLLABORATION_SCHEMA_MANIFEST_SHA256
    assert result.canonical_backup_receipt_sha256.startswith("sha256:")
    assert result.collaboration_schema_receipt_sha256.startswith("sha256:")


def test_sqlite_journal_receives_ordered_apply_phase_records(tmp_path):
    journal = SQLiteMigrationExecutionJournal(
        tmp_path / "canonical.db",
        owner_ref="pp-core-owner",
        initialize_schema=True,
    )
    operations, _observer, adapters, plan = _operation(execution_journal=journal)

    result = operations.apply(plan, _issued_grant(operations, plan))

    assert result.outcome == "applied"
    with sqlite3.connect(tmp_path / "canonical.db") as connection:
        operation_id = str(
            connection.execute("SELECT operation_id FROM pp_migration_operations").fetchone()[0]
        )
    records = journal.list_phase_records(operation_id)
    assert len(records) == len(result.phases)
    assert [record.phase_index for record in records] == list(range(len(result.phases)))
    assert all(record.outcome == "completed" for record in records)


def test_production_composition_requires_existing_migrated_canonical_database(tmp_path):
    database_path = (tmp_path / "canonical.db").resolve()

    with pytest.raises(MigrationOperationsError, match="migration_journal_database_missing"):
        _production_operation(database_path, initialize_schema=False)

    database_path.touch()
    with pytest.raises(MigrationOperationsError, match="migration_journal_schema_missing"):
        _production_operation(database_path, initialize_schema=False)


def test_production_composition_persists_authority_across_reconstruction(tmp_path):
    database_path = (tmp_path / "canonical.db").resolve()
    first, _observer, _adapters = _production_operation(database_path)
    plan = _digest_only_plan()
    grant = first.issue_grant(plan, grant_id="grant-production")

    reconstructed, _observer, _adapters = _production_operation(
        database_path,
        initialize_schema=False,
    )
    reconstructed.register_grant(plan, grant)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT grant_id, status FROM pp_migration_grants"
        ).fetchall() == [("grant-production", "available")]


def test_server_backend_composition_fails_closed_without_server_role(tmp_path):
    adapters = _Adapters()
    observer = _Observer(_observations())

    with pytest.raises(
        MigrationOperationsError,
        match="migration_server_backend_role_required",
    ):
        compose_pp_server_backend_migration_operations(
            observation_adapter=observer,
            edge_compute_adapter=adapters,
            canonical_state_adapter=adapters,
            collaboration_schema_adapter=adapters,
            runtime_adapter=adapters,
            derived_index_adapter=adapters,
            maintenance_adapter=adapters,
            retention_cache_adapter=adapters,
            environment={"PLASTIC_DB_PATH": str(tmp_path / "canonical.db")},
        )
    assert adapters.events == []


def test_server_backend_composition_only_constructs_operations(tmp_path):
    database_path = (tmp_path / "canonical.db").resolve()
    SQLiteMigrationExecutionJournal(
        database_path,
        owner_ref="schema-installer",
        initialize_schema=True,
    )
    adapters = _Adapters()
    observer = _Observer(_observations())

    operations = compose_pp_server_backend_migration_operations(
        observation_adapter=observer,
        edge_compute_adapter=adapters,
        canonical_state_adapter=adapters,
        collaboration_schema_adapter=adapters,
        runtime_adapter=adapters,
        derived_index_adapter=adapters,
        maintenance_adapter=adapters,
        retention_cache_adapter=adapters,
        environment={"PP_ENDPOINT_ROLE": "pp-server-backend"},
        canonical_database_path=database_path,
        clock=lambda: NOW,
    )

    assert isinstance(operations, MigrationOperations)
    assert observer.calls == 0
    assert adapters.events == []


def test_digest_only_transport_projection_cannot_mutate():
    operations, _observer, adapters, plan = _operation()
    transport_plan = replace(plan, resolved_topology=None, artifact_bundle=None)

    result = operations.apply(transport_plan, _grant(transport_plan))

    assert result.accepted is False
    assert result.reason_code == "migration_plan_bindings_unavailable"
    assert adapters.events == []


def test_terminal_operation_replay_is_rejected_even_with_a_new_grant():
    operations, _observer, adapters, plan = _operation()

    first = operations.apply(plan, _issued_grant(operations, plan))
    second = operations.apply(plan, _issued_grant(operations, plan, grant_id="grant-second"))

    assert first.outcome == "applied"
    assert second.reason_code == "migration_operation_replayed"
    assert adapters.events.count("stage_and_verify") == 1


def test_post_cutover_failure_runs_bounded_rollback_in_order():
    operations, _observer, adapters, plan = _operation(fail="shadow_rebuild_verify_promote")

    result = operations.apply(plan, _issued_grant(operations, plan))

    assert result.outcome == "rolled-back"
    assert result.rollback_attempted is True
    assert result.rollback_completed is True
    assert adapters.events == [
        "stage_and_verify",
        "rehearse",
        "stop_legacy",
        "backup_and_migrate",
        "install_collaboration_schema",
        "start_backend",
        "shadow_rebuild_verify_promote",
        "disable",
        "revert_selection",
        "stop_backend",
        "restore",
        "restart_legacy",
    ]


def test_incomplete_rollback_is_persisted_as_recovery_required(tmp_path):
    journal = SQLiteMigrationExecutionJournal(
        tmp_path / "canonical.db",
        owner_ref="pp-core-owner",
        initialize_schema=True,
    )
    operations, _observer, _adapters, plan = _operation(
        fail=("shadow_rebuild_verify_promote", "restart_legacy"),
        execution_journal=journal,
    )

    result = operations.apply(plan, _issued_grant(operations, plan))

    assert result.outcome == "recovery-required"
    assert result.rollback_attempted is True
    assert result.rollback_completed is False
    with sqlite3.connect(tmp_path / "canonical.db") as connection:
        status = connection.execute("SELECT status FROM pp_migration_operations").fetchone()
    assert status == ("recovery-required",)


def test_plan_identity_rejects_phase_or_schema_manifest_drift():
    _operations, _observer, _adapters, plan = _operation()

    with pytest.raises(MigrationOperationsError, match="migration_phase_manifest_digest_mismatch"):
        replace(plan, phase_manifest_sha256=_digest("foreign-phase-manifest"))
    with pytest.raises(MigrationOperationsError, match="migration_schema_manifest_mismatch"):
        replace(plan, schema_manifest=("schema:foreign",))


def test_failure_before_canonical_migration_skips_restore():
    operations, _observer, adapters, plan = _operation(fail="stop_legacy")

    result = operations.apply(plan, _issued_grant(operations, plan))

    assert result.outcome == "rolled-back"
    assert "restore" not in adapters.events
    assert any(
        phase.phase == "canonical-restore" and phase.outcome == "skipped" for phase in result.phases
    )


def test_deadline_is_checked_before_each_phase_and_rolls_back_after_cutover():
    class Clock:
        expired = False

        def __call__(self) -> datetime:
            return NOW + timedelta(seconds=301) if self.expired else NOW

    clock = Clock()
    operations, _observer, adapters, plan = _operation(
        clock=clock,
        on_event=lambda name: setattr(clock, "expired", name == "backup_and_migrate"),
    )

    result = operations.apply(plan, _issued_grant(operations, plan))

    assert result.outcome == "rolled-back"
    assert result.reason_code == "migration_plan_expired"
    assert "restore" in adapters.events


def test_observation_age_is_bounded_to_the_short_lived_window():
    observer = _Observer(_observations())
    adapters = _Adapters()
    with pytest.raises(MigrationOperationsError, match="migration_observation_max_age_excessive"):
        MigrationOperations(
            observer,
            MigrationAdapters(
                edge_compute=adapters,
                canonical_state=adapters,
                collaboration_schema=adapters,
                runtime=adapters,
                derived_index=adapters,
                maintenance=adapters,
                retention_cache=adapters,
            ),
            clock=lambda: NOW,
            observation_max_age_seconds=901,
        )


def test_incomplete_mutable_adapter_is_rejected_before_planning():
    observer = _Observer(_observations())
    adapters = _Adapters()

    with pytest.raises(MigrationOperationsError, match="migration_runtime_adapter_required"):
        MigrationAdapters(
            edge_compute=adapters,
            canonical_state=adapters,
            collaboration_schema=adapters,
            runtime=object(),
            derived_index=adapters,
            maintenance=adapters,
            retention_cache=adapters,
        )

    assert observer.calls == 0
    assert adapters.events == []


def test_artifact_bundle_profile_must_match_resolved_topology():
    with pytest.raises(MigrationOperationsError, match="migration_artifact_profile_mismatch"):
        MigrationIntent(
            topology=resolve_deployment_manifest_v2(_manifest()),
            artifact_bundle=_bundle(profile_id="local-cloud"),
            installation_ref="test-installation",
            operation_ref="test-migration",
        )


def test_grant_validation_happens_before_mutation():
    operations, _observer, adapters, plan = _operation()

    wrong_plan = operations.apply(plan, _grant(plan, plan_hash=_digest("wrong")))
    issued_before_plan = operations.apply(
        plan,
        _grant(
            plan,
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=30),
        ),
    )
    expires_after_plan = operations.apply(
        plan,
        _grant(plan, expires_at=plan.expires_at + timedelta(seconds=1)),
    )
    expired = operations.apply(
        plan,
        _grant(
            plan,
            issued_at=NOW - timedelta(seconds=2),
            expires_at=NOW - timedelta(microseconds=1),
        ),
    )

    assert wrong_plan.reason_code == "migration_grant_invalid"
    assert issued_before_plan.reason_code == "migration_grant_invalid"
    assert expires_after_plan.reason_code == "migration_grant_invalid"
    assert expired.reason_code == "migration_grant_invalid"
    assert adapters.events == []
