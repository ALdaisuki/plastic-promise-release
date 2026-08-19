from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from plastic_promise.deployment import DEPLOYMENT_MANIFEST_SCHEMA_VERSION


def _manifest() -> dict[str, object]:
    return {
        "schema_version": DEPLOYMENT_MANIFEST_SCHEMA_VERSION,
        "deployment_id": "test-laptop",
        "profile": "local-all-in-one",
        "modules": {},
        "nodes": [],
        "resource_budget": {
            "image_layers_bytes": 0,
            "image_unpack_bytes": 0,
            "model_cache_bytes": 0,
            "lancedb_shadow_rebuild_bytes": 1,
            "rollback_coexistence_bytes": 1,
        },
    }


def _deployment_plan(
    manifest: dict[str, object],
    *,
    state_root: Path,
    operation: str = "install",
    module_id: str | None = None,
    source: Path | None = None,
):
    from plastic_promise.deployment import create_deployment_plan, resolve_deployment_manifest

    resolved = resolve_deployment_manifest(manifest)
    return create_deployment_plan(
        resolved,
        state_root=state_root,
        operation=operation,
        module_id=module_id,
        source=source,
    )


def _deploy_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "plastic_promise.deployment", *args],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )


def _primary_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from plastic_promise.cli import main; main()",
            "deploy",
            *args,
        ],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )


def test_package_exposes_the_deployment_cli_entry_point():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'plastic-promise-deploy = "plastic_promise.deployment.cli:main"' in pyproject


def test_primary_cli_forwards_deploy_commands_without_loading_runtime(tmp_path: Path):
    manifest_path = tmp_path / "deployment.json"
    state_root = tmp_path / "never-created"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    result = _primary_cli(
        "plan",
        "--manifest",
        str(manifest_path),
        "--state-root",
        str(state_root),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["operation"] == "install"
    assert not state_root.exists()


def test_primary_cli_exposes_the_root_module_management_alias():
    result = _primary_cli("module", "list", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema"] == "plastic-promise/deployment-modules/v1"


def test_checked_in_deployment_manifest_examples_are_strict_and_secret_free():
    from plastic_promise.deployment import load_deployment_manifest

    examples = tuple(sorted(Path("deploy/manifests").glob("*.example.json")))

    assert [path.name for path in examples] == [
        "local-all-in-one.example.json",
        "local-cloud.example.json",
        "split-accelerated.example.json",
    ]
    plans = [load_deployment_manifest(path) for path in examples]
    assert [plan.profile_id for plan in plans] == [
        "local-all-in-one",
        "local-cloud",
        "split-accelerated",
    ]
    for path in examples:
        text = path.read_text(encoding="utf-8").casefold()
        assert not any(
            marker in text for marker in ("api_key", "password", "secret", "token", "endpoint")
        )


def test_shell_wrappers_only_forward_to_the_python_deploy_cli():
    bash_wrapper = Path("scripts/plastic-promise-deploy.sh")
    powershell_wrapper = Path("scripts/plastic-promise-deploy.ps1")

    bash = bash_wrapper.read_text(encoding="utf-8")
    powershell = powershell_wrapper.read_text(encoding="utf-8")

    assert 'exec "$python_bin" -m plastic_promise.deployment "$@"' in bash
    assert "& $pythonBin -m plastic_promise.deployment @Arguments" in powershell
    assert '$env:PP_DEPLOY_TARGET -eq "wsl"' in powershell
    assert "$env:PP_WSL_DISTRIBUTION" in powershell
    assert "--list --quiet" in powershell
    assert "--distribution $wslDistribution --cd $wslRoot --exec $wslPython" in powershell
    assert "wsl_repository_mapping_failed" in powershell
    assert "wsl_package_identity_check_failed" in powershell
    assert "plastic_promise_version_mismatch" in powershell
    assert '$requestedDistribution -like "docker-*"' in powershell
    assert "wsl_distribution_not_allowed" in powershell
    for forbidden in ("sudo", "ssh", "systemctl", "password"):
        assert forbidden not in bash.lower()
        assert forbidden not in powershell.lower()
    assert '$_ -notlike "docker-*"' in powershell
    assert "docker " not in powershell.lower()
    result = subprocess.run(
        ["bash", str(bash_wrapper), "--help"],
        cwd=Path.cwd(),
        env={"PP_PYTHON": sys.executable},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "plastic-promise-deploy" in result.stdout


def test_init_creates_a_strict_manifest_without_overwriting_an_existing_file(tmp_path: Path):
    manifest_path = tmp_path / "deployment.json"

    result = _deploy_cli(
        "init",
        "--profile",
        "local-all-in-one",
        "--module",
        "local-ollama",
        "--deployment-id",
        "test-laptop",
        "--output",
        str(manifest_path),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["created"] is True
    assert payload["profile"] == "local-all-in-one"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["modules"] == {"local-ollama": {"enabled": True}}
    assert "resource_budget" not in manifest

    repeat = _deploy_cli(
        "init",
        "--profile",
        "local-all-in-one",
        "--output",
        str(manifest_path),
    )
    assert repeat.returncode == 2
    assert "init_manifest_output_exists" in repeat.stderr


def test_init_rejects_unacknowledged_high_risk_module(tmp_path: Path):
    result = _deploy_cli(
        "init",
        "--profile",
        "local-all-in-one",
        "--module",
        "accelerator-max",
        "--output",
        str(tmp_path / "deployment.json"),
    )

    assert result.returncode == 2
    assert "init_high_risk_acknowledgement_required" in result.stderr


def test_init_split_profile_writes_the_declared_node_capacity(tmp_path: Path):
    manifest_path = tmp_path / "split.json"

    result = _deploy_cli(
        "init",
        "--profile",
        "split-accelerated",
        "--node-ssh-host",
        "home-accelerator",
        "--node-max-concurrency",
        "3",
        "--output",
        str(manifest_path),
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["nodes"][0]["max_concurrency"] == 3


def test_default_manifest_discovery_requires_exactly_one_file(tmp_path: Path, monkeypatch):
    from argparse import Namespace

    import pytest

    import plastic_promise.deployment.cli as cli
    from plastic_promise.deployment import DeploymentContractError

    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    directory = tmp_path / ".config" / "plastic-promise" / "deployments"
    directory.mkdir(parents=True)
    only_manifest = directory / "one.json"
    only_manifest.write_text(json.dumps(_manifest()), encoding="utf-8")

    assert cli._manifest_path_from_args(Namespace(manifest=None)) == only_manifest
    (directory / "two.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    with pytest.raises(DeploymentContractError, match="default_manifest_ambiguous"):
        cli._manifest_path_from_args(Namespace(manifest=None))


def test_declared_node_evidence_requires_identity_and_model_acceptance(tmp_path: Path):
    from plastic_promise.deployment.doctor import observe_node_evidence

    model_cache = tmp_path / "models"
    embedding = model_cache / "embedding"
    rerank = model_cache / "rerank"
    embedding.mkdir(parents=True)
    rerank.mkdir()
    evidence = observe_node_evidence(
        node_config=None,
        tunnel_config=None,
        runtime_status=None,
        expected_node_id="home-accelerator",
        expected_capabilities=("embedding", "rerank"),
        environment={
            "PP_LOCAL_NODE_ID": "home-accelerator",
            "PP_LOCAL_NODE_EMBEDDING_REVISION": "a" * 40,
            "PP_LOCAL_NODE_RERANK_REVISION": "b" * 40,
            "PP_LOCAL_NODE_MODEL_CACHE_DIR": str(model_cache),
            "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE": "embedding",
            "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE": "rerank",
        },
    )

    assert evidence["declaration"] == {
        "status": "declaration_evidence_accepted",
        "capabilities": ["embedding", "rerank"],
    }


def test_platform_doctor_reports_all_required_capability_categories_without_endpoints(
    tmp_path: Path,
):
    state_root = tmp_path / "absent-state"
    result = _deploy_cli("doctor", "--state-root", str(state_root), "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "plastic-promise/deployment-doctor/v1"
    assert {"ssh", "docker", "docker-compose", "nvidia-smi", "ollama"} <= set(
        payload["capabilities"]
    )
    assert {"identity", "models", "tunnel", "runtime"} <= set(payload["node"])
    assert payload["node"]["identity"]["status"] == "not_configured"
    assert payload["node"]["models"]["status"] == "not_configured"
    assert payload["node"]["tunnel"]["status"] == "not_configured"
    assert payload["node"]["runtime"]["status"] == "not_observed"
    assert {"system", "release", "wsl2"} <= set(payload["platform"])
    assert {"detected", "managed_by_deploy_cli"} <= set(payload["service_manager"])
    assert {"total_bytes", "free_bytes"} <= set(payload["disk"])
    assert payload["deployment"]["installed"] is False
    assert "endpoint" not in json.dumps(payload, ensure_ascii=False).lower()
    assert not state_root.exists()


def test_platform_doctor_uses_explicit_redacted_node_evidence_without_connecting(
    tmp_path: Path,
):
    state_root = tmp_path / "absent-state"
    model_cache = tmp_path / "models"
    model_cache.mkdir()
    (model_cache / "embedding").mkdir()
    (model_cache / "rerank").mkdir()
    node_config = tmp_path / "node.env"
    node_config.write_text(
        "\n".join(
            (
                "PP_LOCAL_NODE_ID=doctor-node",
                "PP_LOCAL_NODE_EMBEDDING_REVISION=" + "a" * 40,
                "PP_LOCAL_NODE_RERANK_REVISION=" + "b" * 40,
                "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=embedding",
                "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=rerank",
                f"PP_LOCAL_NODE_MODEL_CACHE_DIR={model_cache}",
            )
        ),
        encoding="utf-8",
    )
    tunnel_config = tmp_path / "tunnel.env"
    tunnel_config.write_text(
        "\n".join(
            (
                "PP_TUNNEL_TARGET=restricted-user@private-host",
                "PP_TUNNEL_IDENTITY_FILE=/non-secret/key-reference",
                "PP_TUNNEL_SERVER_PORT=19130",
                "PP_LOCAL_NODE_PORT=19130",
            )
        ),
        encoding="utf-8",
    )
    runtime_status = tmp_path / "runtime-status.json"
    runtime_status.write_text(
        json.dumps(
            {
                "schema_version": "plastic-promise/local-inference-runtime-status/v1",
                "running": True,
                "node_healthy": True,
            }
        ),
        encoding="utf-8",
    )

    result = _deploy_cli(
        "doctor",
        "--state-root",
        str(state_root),
        "--node-config",
        str(node_config),
        "--tunnel-config",
        str(tunnel_config),
        "--runtime-status",
        str(runtime_status),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["node"]["identity"] == {
        "status": "configured",
        "evidence": "pinned_revisions",
        "source": "file",
    }
    assert payload["node"]["models"] == {
        "status": "available",
        "evidence": "readable_model_references",
        "source": "file",
    }
    assert payload["node"]["tunnel"] == {
        "status": "configured",
        "evidence": "restricted_tunnel_contract",
        "source": "file",
    }
    assert payload["node"]["runtime"] == {
        "status": "running",
        "evidence": "runtime_status_file",
    }
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "private-host" not in rendered
    assert "doctor-node" not in rendered
    assert str(model_cache) not in rendered
    assert not state_root.exists()


def test_platform_doctor_rejects_sensitive_node_evidence_without_echoing_it(tmp_path: Path):
    node_config = tmp_path / "node.env"
    node_config.write_text(
        "PP_LOCAL_NODE_EMBEDDING_REVISION=" + "a" * 40 + "\nPP_LOCAL_NODE_TOKEN=do-not-print",
        encoding="utf-8",
    )

    result = _deploy_cli("doctor", "--node-config", str(node_config), "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["node"]["identity"]["status"] == "sensitive_key_rejected"
    assert payload["node"]["models"]["status"] == "sensitive_key_rejected"
    assert "do-not-print" not in result.stdout


def test_platform_doctor_simulates_windows_and_wsl2_platform_classification(monkeypatch):
    import plastic_promise.deployment.cli as cli
    from plastic_promise.deployment import DeploymentController, HostDiskUsage

    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.platform, "release", lambda: "10.0.22631")
    windows = cli._doctor_payload(
        controller,
        state_root=None,
        node_config=None,
        tunnel_config=None,
        runtime_status=None,
    )
    assert windows["platform"] == {"system": "Windows", "release": "10.0.22631", "wsl2": False}
    assert windows["service_manager"] == {
        "detected": "windows-service-manager",
        "managed_by_deploy_cli": False,
    }

    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.platform, "release", lambda: "5.15.153.1-microsoft-standard-WSL2")
    wsl = cli._doctor_payload(
        controller,
        state_root=None,
        node_config=None,
        tunnel_config=None,
        runtime_status=None,
    )
    assert wsl["platform"]["wsl2"] is True


def test_platform_doctor_simulates_macos_service_boundary(monkeypatch):
    import plastic_promise.deployment.cli as cli
    from plastic_promise.deployment import DeploymentController

    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.platform, "release", lambda: "24.0.0")
    payload = cli._doctor_payload(
        DeploymentController(),
        state_root=None,
        node_config=None,
        tunnel_config=None,
        runtime_status=None,
    )

    assert payload["platform"] == {"system": "Darwin", "release": "24.0.0", "wsl2": False}
    assert payload["service_manager"] == {
        "detected": "launchd",
        "managed_by_deploy_cli": False,
    }


def test_doctor_marks_ollama_node_backend_as_unusable_without_request_artifact_proof():
    from plastic_promise.deployment.doctor import _model_cache_status

    assert _model_cache_status(
        {"PP_LOCAL_NODE_EMBEDDING_BACKEND": "ollama"},
        source_status="configured",
    ) == {
        "status": "identity_proof_unavailable",
        "evidence": "ollama_mutable_tag_not_governed",
    }


def test_plan_is_deterministic_and_never_creates_target_state(tmp_path: Path):
    manifest_path = tmp_path / "deployment.json"
    state_root = tmp_path / "never-created-by-plan"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    first = _deploy_cli(
        "plan",
        "--manifest",
        str(manifest_path),
        "--state-root",
        str(state_root),
        "--json",
    )
    second = _deploy_cli(
        "plan",
        "--manifest",
        str(manifest_path),
        "--state-root",
        str(state_root),
        "--json",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["schema"] == "plastic-promise/deployment-plan/v2"
    assert first_payload["operation"] == "install"
    assert first_payload["plan_id"].startswith("plan-")
    assert first_payload["target"]["state_root"] == str(state_root)
    assert first_payload["plan_hash"].startswith("sha256:")
    assert first_payload["preconditions"]["database"]["primary_state"] == "missing"
    assert first_payload["plan_hash"] == second_payload["plan_hash"]
    assert first_payload["plan_id"] == second_payload["plan_id"]
    assert first_payload["high_risk_steps"] == [
        "canonical_sqlite_bootstrap",
        "versioned_sqlite_migration",
    ]
    assert not state_root.exists()


def test_plan_binds_operation_database_and_restore_source_fingerprints(tmp_path: Path):
    import sqlite3

    import pytest

    from plastic_promise.deployment import create_deployment_plan, resolve_deployment_manifest

    state_root = tmp_path / "state"
    database_path = state_root / "data" / "plastic-promise.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE source_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO source_rows (value) VALUES ('before')")
    source_path = tmp_path / "restore-source.sqlite3"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE source_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO source_rows (value) VALUES ('source')")

    resolved = resolve_deployment_manifest(_manifest())
    install = create_deployment_plan(resolved, state_root=state_root, operation="install")
    upgrade = create_deployment_plan(resolved, state_root=state_root, operation="upgrade")
    restore = create_deployment_plan(
        resolved,
        state_root=state_root,
        operation="restore",
        source=source_path,
    )

    assert install.plan_hash != upgrade.plan_hash
    assert upgrade.target_snapshot.fingerprint == restore.target_snapshot.fingerprint
    assert restore.source_snapshot is not None
    assert restore.source_snapshot.fingerprint.startswith("sha256:")
    with pytest.raises(ValueError, match="deployment_plan_source_not_allowed"):
        create_deployment_plan(
            resolved,
            state_root=state_root,
            source=source_path,
        )


def test_preflight_reserves_existing_sqlite_backup_and_migration_scratch(tmp_path: Path):
    import plastic_promise.deployment.plan as plan_module
    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
        SQLiteAssetSnapshot,
        create_deployment_plan,
        resolve_deployment_manifest,
    )

    state_root = tmp_path / "state"
    observed = SQLiteAssetSnapshot(
        primary_state="file",
        primary_bytes=6 * 1024**3,
        wal_bytes=512 * 1024**2,
        shm_bytes=0,
        fingerprint="sha256:target",
    )
    original_snapshot = plan_module.sqlite_asset_snapshot
    plan_module.sqlite_asset_snapshot = lambda _path: observed
    try:
        plan = create_deployment_plan(
            resolve_deployment_manifest(_manifest()), state_root=state_root, operation="upgrade"
        )
    finally:
        plan_module.sqlite_asset_snapshot = original_snapshot

    expected_write_bytes = observed.online_backup_bytes + max(
        64 * 1024**2, observed.primary_bytes // 10
    )
    assert plan.estimated_write_bytes == expected_write_bytes + 2
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=20 * 1024**3 + expected_write_bytes - 1,
        )
    )

    report = controller.preflight(plan)

    assert report.ok is False
    assert report.estimated_write_bytes == expected_write_bytes + 2
    assert report.failure_codes == ("post_install_disk_reserve_unmet",)
    assert not state_root.exists()


def test_preflight_fails_closed_without_complete_external_resource_budget(tmp_path: Path):
    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
        create_deployment_plan,
        resolve_deployment_manifest,
    )

    manifest = _manifest()
    manifest.pop("resource_budget")
    plan = create_deployment_plan(
        resolve_deployment_manifest(manifest), state_root=tmp_path / "state"
    )
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(total_bytes=100 * 1024**3, free_bytes=90 * 1024**3)
    )

    report = controller.preflight(plan)

    assert report.ok is False
    assert report.failure_codes == ("resource_budget_required",)
    assert report.resource_evidence_complete is False
    assert report.external_write_bytes == 0
    assert not plan.target.state_root.exists()


def test_preflight_reserves_declared_external_artifact_write_set(tmp_path: Path):
    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
        create_deployment_plan,
        resolve_deployment_manifest,
    )

    manifest = _manifest()
    manifest["resource_budget"] = {
        "image_layers_bytes": 2 * 1024**3,
        "image_unpack_bytes": 3 * 1024**3,
        "model_cache_bytes": 4 * 1024**3,
        "lancedb_shadow_rebuild_bytes": 5 * 1024**3,
        "rollback_coexistence_bytes": 6 * 1024**3,
    }
    manifest["resource_locations"] = {
        "container_store": str(tmp_path / "container-store"),
        "model_cache": str(tmp_path / "model-cache"),
    }
    plan = create_deployment_plan(
        resolve_deployment_manifest(manifest), state_root=tmp_path / "state"
    )
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=35 * 1024**3,
        )
    )

    report = controller.preflight(plan)

    assert plan.estimated_write_bytes == 20 * 1024**3 + 512 * 1024**2 + 64 * 1024**2
    assert report.ok is False
    assert report.external_write_bytes == 9 * 1024**3
    assert report.existing_artifact_bytes == 0
    assert report.resource_evidence_complete is True
    assert report.failure_codes == ("post_install_disk_reserve_unmet",)


def test_preflight_observes_actual_container_and_model_occupancy_by_filesystem(tmp_path: Path):
    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
        create_deployment_plan,
        resolve_deployment_manifest,
    )

    container_store = tmp_path / "container-store"
    model_cache = tmp_path / "model-cache"
    container_store.mkdir()
    model_cache.mkdir()
    (container_store / "layer").write_bytes(b"a" * 7)
    (model_cache / "model").write_bytes(b"b" * 11)
    manifest = _manifest()
    manifest["resource_budget"] = {
        "image_layers_bytes": 10,
        "image_unpack_bytes": 20,
        "model_cache_bytes": 30,
        "lancedb_shadow_rebuild_bytes": 40,
        "rollback_coexistence_bytes": 50,
    }
    manifest["resource_locations"] = {
        "container_store": str(container_store),
        "model_cache": str(model_cache),
    }
    plan = create_deployment_plan(
        resolve_deployment_manifest(manifest), state_root=tmp_path / "state"
    )
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )

    report = controller.preflight(plan)

    assert report.ok is True
    assert report.existing_artifact_bytes == 18
    assert report.external_write_bytes == 60
    assert len(report.volumes) == 1
    assert report.volumes[0].purposes == (
        "canonical-state",
        "container-store",
        "model-cache",
    )
    assert report.volumes[0].planned_write_bytes == plan.estimated_write_bytes


def test_backup_restore_and_purge_apply_the_same_disk_hard_gate(tmp_path: Path):
    import pytest

    from plastic_promise.deployment import DeploymentApplyError, DeploymentController, HostDiskUsage

    state_root = tmp_path / "state"
    installing_controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    install_plan = _deployment_plan(_manifest(), state_root=state_root)
    installing_controller.apply(install_plan, plan_hash=install_plan.plan_hash)
    constrained_controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=20 * 1024**3 - 1,
        )
    )
    backup_plan = _deployment_plan(_manifest(), state_root=state_root, operation="backup")

    with pytest.raises(DeploymentApplyError, match="post_install_disk_reserve_unmet"):
        constrained_controller.backup(backup_plan, plan_hash=backup_plan.plan_hash)


def test_preflight_rejects_post_install_disk_shortfall_without_creating_state(tmp_path: Path):
    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=15 * 1024**3,
        )
    )

    report = controller.preflight(plan)

    assert report.ok is False
    assert report.failure_codes == ("post_install_disk_reserve_unmet",)
    assert report.required_free_bytes == 20 * 1024**3
    assert not state_root.exists()


def test_apply_rejects_a_plan_hash_that_drifted_from_the_current_manifest(tmp_path: Path):
    manifest_path = tmp_path / "deployment.json"
    state_root = tmp_path / "state"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    planned = _deploy_cli(
        "plan",
        "--manifest",
        str(manifest_path),
        "--state-root",
        str(state_root),
        "--json",
    )
    expected_hash = json.loads(planned.stdout)["plan_hash"]
    manifest_path.write_text(
        json.dumps({**_manifest(), "profile": "local-cloud"}),
        encoding="utf-8",
    )

    applied = _deploy_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--state-root",
        str(state_root),
        "--plan-hash",
        expected_hash,
        "--dry-run",
        "--json",
    )

    assert applied.returncode == 2
    assert json.loads(applied.stderr) == {"error": "plan_hash_mismatch"}
    assert not state_root.exists()


def test_apply_dry_run_executes_preflight_without_creating_target_state(tmp_path: Path):
    manifest_path = tmp_path / "deployment.json"
    state_root = tmp_path / "state"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    planned = _deploy_cli(
        "plan",
        "--manifest",
        str(manifest_path),
        "--state-root",
        str(state_root),
        "--json",
    )
    plan_hash = json.loads(planned.stdout)["plan_hash"]

    result = _deploy_cli(
        "apply",
        "--manifest",
        str(manifest_path),
        "--state-root",
        str(state_root),
        "--plan-hash",
        plan_hash,
        "--dry-run",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "dry-run"
    assert payload["changed"] is False
    assert payload["preflight"]["checked"] is True
    assert not state_root.exists()


def test_status_and_module_catalog_commands_are_read_only(tmp_path: Path):
    state_root = tmp_path / "missing-state"
    manifest_path = tmp_path / "deployment.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    status = _deploy_cli("status", "--state-root", str(state_root), "--json")
    modules = _deploy_cli("module", "list", "--json")
    resolved = _deploy_cli("module", "resolve", "--manifest", str(manifest_path), "--json")

    assert status.returncode == 0, status.stderr
    assert modules.returncode == 0, modules.stderr
    assert resolved.returncode == 0, resolved.stderr
    assert json.loads(status.stdout)["installed"] is False
    assert "canonical-runtime" in {item["id"] for item in json.loads(modules.stdout)["modules"]}
    assert json.loads(resolved.stdout)["profile"] == "local-all-in-one"
    assert not state_root.exists()


def test_apply_builds_an_empty_canonical_sqlite_only_after_preflight_passes(tmp_path: Path):
    import sqlite3

    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )

    outcome = controller.apply(plan, plan_hash=plan.plan_hash)

    assert outcome.changed is True
    assert outcome.database_action == "created"
    assert plan.target.database_path.is_file()
    assert (state_root / "deployment-state.json").is_file()
    with sqlite3.connect(plan.target.database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_apply_attaches_existing_sqlite_after_online_backup_and_integrity_check(tmp_path: Path):
    import sqlite3

    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    database_path = state_root / "data" / "plastic-promise.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE preserved_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved_rows (value) VALUES ('keep-me')")
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )

    outcome = controller.apply(plan, plan_hash=plan.plan_hash)

    assert outcome.changed is True
    assert outcome.database_action == "attached"
    assert outcome.backup_path is not None
    assert outcome.backup_path.is_file()
    with sqlite3.connect(plan.target.database_path) as connection:
        assert connection.execute("SELECT value FROM preserved_rows").fetchone()[0] == "keep-me"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(outcome.backup_path) as backup:
        assert backup.execute("SELECT value FROM preserved_rows").fetchone()[0] == "keep-me"


def test_apply_runs_node_governance_v2_once_after_backup(tmp_path: Path):
    import sqlite3

    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    database_path = state_root / "data" / "plastic-promise.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE preserved_rows (value TEXT NOT NULL)")
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )

    first = controller.apply(plan, plan_hash=plan.plan_hash)
    second_plan = _deployment_plan(_manifest(), state_root=state_root)
    second = controller.apply(second_plan, plan_hash=second_plan.plan_hash)

    assert first.migrations_applied == (
        "node-governance-v2",
        "migration-execution-journal-v2",
    )
    assert second.migrations_applied == ()
    assert first.backup_path is not None
    with sqlite3.connect(plan.target.database_path) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "inference_nodes" in names
        assert "derived_work_accelerator_audit_events" in names
        assert "pp_migration_operations" in names
        assert "deployment_migration_journal" in names
    with sqlite3.connect(first.backup_path) as backup:
        backup_names = {row[0] for row in backup.execute("SELECT name FROM sqlite_master")}
        assert "inference_nodes" not in backup_names
        assert "pp_migration_operations" not in backup_names
        assert "deployment_migration_journal" not in backup_names


def test_incompatible_existing_schema_rolls_back_migration_and_keeps_backup(tmp_path: Path):
    import sqlite3

    import pytest

    from plastic_promise.deployment import (
        DeploymentApplyError,
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    database_path = state_root / "data" / "plastic-promise.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE inference_nodes (node_id TEXT PRIMARY KEY)")
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )

    with pytest.raises(DeploymentApplyError, match="node_governance_schema_validation_failed"):
        controller.apply(plan, plan_hash=plan.plan_hash)

    with sqlite3.connect(plan.target.database_path) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "inference_nodes" in names
        assert "deployment_migration_journal" not in names
        assert "derived_work_accelerator_audit_events" not in names
    backups = tuple((state_root / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        names = {row[0] for row in backup.execute("SELECT name FROM sqlite_master")}
        assert "inference_nodes" in names
        assert "deployment_migration_journal" not in names
        assert "derived_work_accelerator_audit_events" not in names


def test_restore_requires_plan_bound_confirmation_before_touching_existing_database(tmp_path: Path):
    import sqlite3

    import pytest

    from plastic_promise.deployment import (
        DeploymentApplyError,
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    database_path = state_root / "data" / "plastic-promise.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE preserved_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved_rows (value) VALUES ('before')")
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    applied = controller.apply(plan, plan_hash=plan.plan_hash)
    assert applied.backup_path is not None
    with sqlite3.connect(plan.target.database_path) as connection:
        connection.execute("UPDATE preserved_rows SET value = 'after'")

    assert applied.backup_path is not None
    restore_plan = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="restore",
        source=applied.backup_path,
    )
    with pytest.raises(DeploymentApplyError, match="restore_confirmation_required"):
        controller.restore(
            restore_plan,
            plan_hash=restore_plan.plan_hash,
            source=applied.backup_path,
            confirmed=False,
            service_stopped=False,
        )

    with sqlite3.connect(plan.target.database_path) as connection:
        assert connection.execute("SELECT value FROM preserved_rows").fetchone()[0] == "after"


def test_upgrade_and_repair_refuse_to_create_a_missing_installed_database(tmp_path: Path):
    import pytest

    from plastic_promise.deployment import (
        DeploymentApplyError,
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    controller.apply(plan, plan_hash=plan.plan_hash)
    plan.target.database_path.unlink()
    upgrade_plan = _deployment_plan(_manifest(), state_root=state_root, operation="upgrade")

    with pytest.raises(DeploymentApplyError, match="database_missing_restore_required"):
        controller.upgrade(upgrade_plan, plan_hash=upgrade_plan.plan_hash)

    assert not plan.target.database_path.exists()
    assert not (state_root / "backups").exists()


def test_restore_takes_a_pre_restore_backup_before_replacing_canonical_sqlite(tmp_path: Path):
    import sqlite3

    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    database_path = state_root / "data" / "plastic-promise.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE preserved_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved_rows (value) VALUES ('before')")
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    applied = controller.apply(plan, plan_hash=plan.plan_hash)
    assert applied.backup_path is not None
    assert applied.backup_path.with_name(f"{applied.backup_path.name}.evidence.json").is_file()
    with sqlite3.connect(plan.target.database_path) as connection:
        connection.execute("UPDATE preserved_rows SET value = 'after'")

    assert applied.backup_path is not None
    restore_plan = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="restore",
        source=applied.backup_path,
    )
    restored = controller.restore(
        restore_plan,
        plan_hash=restore_plan.plan_hash,
        source=applied.backup_path,
        confirmed=True,
        service_stopped=True,
    )

    assert restored.database_action == "restored"
    assert restored.backup_path is not None
    with sqlite3.connect(plan.target.database_path) as connection:
        assert connection.execute("SELECT value FROM preserved_rows").fetchone()[0] == "before"
    with sqlite3.connect(restored.backup_path) as backup:
        assert backup.execute("SELECT value FROM preserved_rows").fetchone()[0] == "after"


def test_restore_rejects_sources_without_controller_evidence_before_backup(tmp_path: Path):
    import pytest

    from plastic_promise.deployment import DeploymentApplyError, DeploymentController, HostDiskUsage
    from plastic_promise.deployment.backup_evidence import backup_evidence_path

    state_root = tmp_path / "state"
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    install_plan = _deployment_plan(_manifest(), state_root=state_root)
    controller.apply(install_plan, plan_hash=install_plan.plan_hash)
    assert (state_root / "runtime-components" / "canonical-runtime.json").is_file()
    backup_plan = _deployment_plan(_manifest(), state_root=state_root, operation="backup")
    backup = controller.backup(backup_plan, plan_hash=backup_plan.plan_hash)
    assert backup.backup_path is not None
    backup_evidence_path(backup.backup_path).unlink()
    restore_plan = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="restore",
        source=backup.backup_path,
    )

    with pytest.raises(DeploymentApplyError, match="restore_source_evidence_missing"):
        controller.restore(
            restore_plan,
            plan_hash=restore_plan.plan_hash,
            source=backup.backup_path,
            confirmed=True,
            service_stopped=True,
        )

    assert list((state_root / "backups").glob("pre-restore-*.sqlite3")) == []


def test_restore_rejects_tampered_or_cross_profile_backup_evidence(tmp_path: Path):
    import pytest

    from plastic_promise.deployment import DeploymentApplyError, DeploymentController, HostDiskUsage
    from plastic_promise.deployment.backup_evidence import (
        backup_evidence_path,
        write_backup_evidence,
    )

    state_root = tmp_path / "state"
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    install_plan = _deployment_plan(_manifest(), state_root=state_root)
    controller.apply(install_plan, plan_hash=install_plan.plan_hash)
    backup_plan = _deployment_plan(_manifest(), state_root=state_root, operation="backup")
    backup = controller.backup(backup_plan, plan_hash=backup_plan.plan_hash)
    assert backup.backup_path is not None
    restore_plan = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="restore",
        source=backup.backup_path,
    )

    evidence_path = backup_evidence_path(backup.backup_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["sha256"] = "sha256:" + "0" * 64
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(DeploymentApplyError, match="restore_source_evidence_hash_mismatch"):
        controller.restore(
            restore_plan,
            plan_hash=restore_plan.plan_hash,
            source=backup.backup_path,
            confirmed=True,
            service_stopped=True,
        )

    write_backup_evidence(backup.backup_path, profile_id="local-cloud")
    with pytest.raises(DeploymentApplyError, match="cross_profile_restore_requires_migration"):
        controller.restore(
            restore_plan,
            plan_hash=restore_plan.plan_hash,
            source=backup.backup_path,
            confirmed=True,
            service_stopped=True,
        )


def test_restore_discards_only_stale_wal_and_shm_for_the_replaced_database(
    tmp_path: Path, monkeypatch
):
    import sqlite3

    import plastic_promise.deployment.controller as controller_module
    from plastic_promise.deployment import (
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    database_path = state_root / "data" / "plastic-promise.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE preserved_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved_rows (value) VALUES ('before')")
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    applied = controller.apply(plan, plan_hash=plan.plan_hash)
    assert applied.backup_path is not None
    with sqlite3.connect(plan.target.database_path) as connection:
        connection.execute("UPDATE preserved_rows SET value = 'after'")

    original_backup = controller_module._verified_online_backup

    def backup_then_leave_stale_sidecars(*args, **kwargs):
        backup_path = original_backup(*args, **kwargs)
        if kwargs.get("prefix") == "pre-restore":
            plan.target.database_path.with_name(
                f"{plan.target.database_path.name}-wal"
            ).write_bytes(b"stale-wal")
            plan.target.database_path.with_name(
                f"{plan.target.database_path.name}-shm"
            ).write_bytes(b"stale-shm")
        return backup_path

    monkeypatch.setattr(
        controller_module, "_verified_online_backup", backup_then_leave_stale_sidecars
    )

    assert applied.backup_path is not None
    restore_plan = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="restore",
        source=applied.backup_path,
    )
    restored = controller.restore(
        restore_plan,
        plan_hash=restore_plan.plan_hash,
        source=applied.backup_path,
        confirmed=True,
        service_stopped=True,
    )

    assert restored.database_action == "restored"
    assert not plan.target.database_path.with_name(f"{plan.target.database_path.name}-wal").exists()
    assert not plan.target.database_path.with_name(f"{plan.target.database_path.name}-shm").exists()
    with sqlite3.connect(plan.target.database_path) as connection:
        assert connection.execute("SELECT value FROM preserved_rows").fetchone()[0] == "before"


def test_restore_reinstates_staged_sidecars_when_primary_replace_fails(tmp_path: Path, monkeypatch):
    import sqlite3

    import pytest

    import plastic_promise.deployment.controller as controller_module
    from plastic_promise.deployment import DeploymentController, HostDiskUsage

    state_root = tmp_path / "state"
    database_path = state_root / "data" / "plastic-promise.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE preserved_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved_rows (value) VALUES ('before')")
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    install_plan = _deployment_plan(_manifest(), state_root=state_root)
    applied = controller.apply(install_plan, plan_hash=install_plan.plan_hash)
    assert applied.backup_path is not None
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE preserved_rows SET value = 'after'")
    restore_plan = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="restore",
        source=applied.backup_path,
    )

    original_backup = controller_module._verified_online_backup

    def backup_then_leave_stale_sidecars(*args, **kwargs):
        backup_path = original_backup(*args, **kwargs)
        if kwargs.get("prefix") == "pre-restore":
            database_path.with_name(f"{database_path.name}-wal").write_bytes(b"old-wal")
            database_path.with_name(f"{database_path.name}-shm").write_bytes(b"old-shm")
        return backup_path

    original_replace = controller_module.os.replace

    def fail_primary_replace(source, target):
        if Path(source).name.startswith(".restore-") and Path(target) == database_path:
            raise OSError("simulated_primary_replace_failure")
        return original_replace(source, target)

    monkeypatch.setattr(
        controller_module, "_verified_online_backup", backup_then_leave_stale_sidecars
    )
    monkeypatch.setattr(controller_module.os, "replace", fail_primary_replace)

    with pytest.raises(OSError, match="simulated_primary_replace_failure"):
        controller.restore(
            restore_plan,
            plan_hash=restore_plan.plan_hash,
            source=applied.backup_path,
            confirmed=True,
            service_stopped=True,
        )

    assert database_path.is_file()
    assert database_path.with_name(f"{database_path.name}-wal").read_bytes() == b"old-wal"
    assert database_path.with_name(f"{database_path.name}-shm").read_bytes() == b"old-shm"


def test_operation_and_source_drift_are_refused_before_mutating_sqlite(tmp_path: Path):
    import sqlite3

    import pytest

    from plastic_promise.deployment import DeploymentApplyError, DeploymentController, HostDiskUsage

    state_root = tmp_path / "state"
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    install_plan = _deployment_plan(_manifest(), state_root=state_root)
    controller.apply(install_plan, plan_hash=install_plan.plan_hash)
    upgrade_plan = _deployment_plan(_manifest(), state_root=state_root, operation="upgrade")
    with sqlite3.connect(upgrade_plan.target.database_path) as connection:
        connection.execute("CREATE TABLE plan_drift_rows (value TEXT NOT NULL)")

    with pytest.raises(DeploymentApplyError, match="database_state_drift"):
        controller.upgrade(upgrade_plan, plan_hash=upgrade_plan.plan_hash)

    current_install_plan = _deployment_plan(_manifest(), state_root=state_root)
    with pytest.raises(DeploymentApplyError, match="plan_operation_mismatch"):
        controller.backup(current_install_plan, plan_hash=current_install_plan.plan_hash)

    source_path = tmp_path / "restore-source.sqlite3"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE source_rows (value TEXT NOT NULL)")
    restore_plan = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="restore",
        source=source_path,
    )
    with sqlite3.connect(source_path) as connection:
        connection.execute("INSERT INTO source_rows (value) VALUES ('drift')")

    with pytest.raises(DeploymentApplyError, match="restore_source_drift"):
        controller.restore(
            restore_plan,
            plan_hash=restore_plan.plan_hash,
            source=source_path,
            confirmed=True,
            service_stopped=True,
        )


def test_module_enable_install_and_remove_are_state_bound(tmp_path: Path):
    import pytest

    from plastic_promise.deployment import DeploymentApplyError, DeploymentController, HostDiskUsage

    state_root = tmp_path / "state"
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    install_plan = _deployment_plan(_manifest(), state_root=state_root)
    controller.apply(install_plan, plan_hash=install_plan.plan_hash)

    module_install = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="module-install",
        module_id="local-ollama",
    )
    assert controller.install_module(
        module_install,
        plan_hash=module_install.plan_hash,
        module_id="local-ollama",
    )
    assert "local-ollama" in controller.status(state_root=state_root).module_ids
    assert (state_root / "runtime-components" / "local-ollama.json").is_file()

    module_disable = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="module-disable",
        module_id="local-ollama",
    )
    assert controller.disable_module(
        module_disable,
        plan_hash=module_disable.plan_hash,
        module_id="local-ollama",
    )
    module_enable = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="module-enable",
        module_id="local-ollama",
    )
    assert controller.enable_module(
        module_enable,
        plan_hash=module_enable.plan_hash,
        module_id="local-ollama",
    )
    assert controller.status(state_root=state_root).disabled_modules == ()

    module_remove = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="module-remove",
        module_id="local-ollama",
    )
    assert controller.remove_module(
        module_remove,
        plan_hash=module_remove.plan_hash,
        module_id="local-ollama",
    )
    assert "local-ollama" not in controller.status(state_root=state_root).module_ids
    assert not (state_root / "runtime-components" / "local-ollama.json").exists()
    core_remove = _deployment_plan(
        _manifest(),
        state_root=state_root,
        operation="module-remove",
        module_id="canonical-runtime",
    )
    with pytest.raises(DeploymentApplyError, match="core_module_remove_forbidden"):
        controller.remove_module(
            core_remove,
            plan_hash=core_remove.plan_hash,
            module_id="canonical-runtime",
        )


def test_optional_module_disable_is_persisted_but_core_modules_are_immutable(tmp_path: Path):
    import pytest

    from plastic_promise.deployment import (
        DeploymentApplyError,
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    manifest = {
        **_manifest(),
        "modules": {"local-ollama": {"enabled": True}},
        "resource_locations": {
            "container_store": None,
            "model_cache": str(tmp_path / "model-cache"),
        },
        "resource_budget": {
            **_manifest()["resource_budget"],
            "model_cache_bytes": 1,
        },
    }
    plan = _deployment_plan(manifest, state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    controller.apply(plan, plan_hash=plan.plan_hash)

    disabled_plan = _deployment_plan(
        manifest,
        state_root=state_root,
        operation="module-disable",
        module_id="local-ollama",
    )
    assert controller.disable_module(
        disabled_plan,
        plan_hash=disabled_plan.plan_hash,
        module_id="local-ollama",
    )
    assert controller.status(state_root=state_root).disabled_modules == ("local-ollama",)
    core_plan = _deployment_plan(
        manifest,
        state_root=state_root,
        operation="module-disable",
        module_id="canonical-runtime",
    )
    with pytest.raises(DeploymentApplyError, match="core_module_disable_forbidden"):
        controller.disable_module(
            core_plan,
            plan_hash=core_plan.plan_hash,
            module_id="canonical-runtime",
        )


def test_remove_and_purge_keep_separate_confirmation_boundaries(tmp_path: Path):
    import sqlite3

    import pytest

    from plastic_promise.deployment import (
        DeploymentApplyError,
        DeploymentController,
        HostDiskUsage,
    )

    state_root = tmp_path / "state"
    plan = _deployment_plan(_manifest(), state_root=state_root)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )
    controller.apply(plan, plan_hash=plan.plan_hash)
    with sqlite3.connect(plan.target.database_path) as connection:
        connection.execute("CREATE TABLE retained_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO retained_rows (value) VALUES ('retained')")

    remove_plan = _deployment_plan(_manifest(), state_root=state_root, operation="remove")
    with pytest.raises(DeploymentApplyError, match="remove_confirmation_required"):
        controller.remove(remove_plan, plan_hash=remove_plan.plan_hash, confirmed=False)
    assert controller.remove(remove_plan, plan_hash=remove_plan.plan_hash, confirmed=True)
    assert not (state_root / "runtime-components").exists()
    assert plan.target.database_path.is_file()
    with sqlite3.connect(plan.target.database_path) as connection:
        assert connection.execute("SELECT value FROM retained_rows").fetchone()[0] == "retained"

    reapply_plan = _deployment_plan(_manifest(), state_root=state_root)
    controller.apply(reapply_plan, plan_hash=reapply_plan.plan_hash)
    purge_plan = _deployment_plan(_manifest(), state_root=state_root, operation="purge")
    with pytest.raises(DeploymentApplyError, match="service_stopped_confirmation_required"):
        controller.purge(
            purge_plan,
            plan_hash=purge_plan.plan_hash,
            confirmed=True,
            service_stopped=False,
        )
    purged = controller.purge(
        purge_plan,
        plan_hash=purge_plan.plan_hash,
        confirmed=True,
        service_stopped=True,
    )
    assert purged.database_action == "purged"
    assert purged.backup_path is not None and purged.backup_path.is_file()
    assert not plan.target.database_path.exists()
