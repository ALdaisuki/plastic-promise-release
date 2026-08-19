from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from plastic_promise.release_builder import (
    BuilderMode,
    ReleaseActions,
    ReleaseBuilderError,
    ReleaseLedger,
    ReleasePhase,
    ReleaseRequest,
    ResourceSample,
    ResourceSnapshot,
    RetentionEntry,
    aggregate_resource_samples,
    confirm_request,
    evaluate_resource_gate,
    load_confirmation,
    load_request,
    observe_resource_window,
    plan_retention_cleanup,
    remaining_phases,
    validate_confirmation,
    validate_windows_source_root,
    write_confirmation,
    write_receipt,
    write_request,
)
from plastic_promise.release_builder.cli import main as release_builder_main

SOURCE_COMMIT = "a" * 40
NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _request(**changes: object) -> ReleaseRequest:
    request = ReleaseRequest(
        release_version="v0.2.15",
        source_commit=SOURCE_COMMIT,
        source_channel="github-commit",
        actions=ReleaseActions(deploy_server=True),
    )
    return replace(request, **changes)


def _snapshot(**changes: object) -> ResourceSnapshot:
    snapshot = ResourceSnapshot(
        observed_at=NOW,
        sample_seconds=10,
        cpu_average_percent=12.0,
        available_ram_bytes=16 * 1024**3,
        gpu_average_utilization_percent=0.0,
        gpu_vram_used_bytes=0,
        gpu_temperature_celsius=42.0,
        active_buildkit_build=False,
        inference_or_model_lock=False,
        d_drive_free_bytes=160 * 1024**3,
        d_drive_total_bytes=500 * 1024**3,
    )
    return replace(snapshot, **changes)


def test_release_request_is_hash_stable_and_maintenance_defaults_on():
    first = ReleaseRequest(
        release_version="v0.2.15",
        source_commit=SOURCE_COMMIT,
        source_channel="github-commit",
    )
    second = ReleaseRequest.from_mapping(first.to_mapping())

    assert first.actions.start_maintenance is True
    assert first.request_hash == second.request_hash
    assert first.to_mapping()["actions"]["sqlite_migration"] is False
    assert first.to_mapping()["actions"]["lancedb_promotion"] is False


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"source_commit": "main"}, "release_source_commit_invalid"),
        ({"source_commit": "A" * 40}, "release_source_commit_invalid"),
        (
            {"source_channel": "https://example.invalid/source.tar.gz"},
            "release_source_channel_invalid",
        ),
        ({"unknown": True}, "release_request_field_not_allowed"),
        ({"api_token": "never"}, "release_request_field_not_allowed"),
        (
            {
                "source_channel": "verified-archive",
                "source_archive_sha256": None,
            },
            "release_source_archive_sha256_required",
        ),
    ],
)
def test_release_request_rejects_floating_or_secret_bearing_inputs(
    payload: dict[str, object], reason: str
):
    base = _request().to_mapping()
    base.update(payload)

    with pytest.raises(ReleaseBuilderError, match=reason):
        ReleaseRequest.from_mapping(base)


def test_verified_archive_requires_full_digest_and_stable_version_is_canonical():
    payload = _request(
        source_channel="verified-archive",
        source_archive_sha256="b" * 64,
    ).to_mapping()
    request = ReleaseRequest.from_mapping(payload)

    assert request.source_archive_sha256 == "b" * 64
    assert request.release_version == "v0.2.15"

    payload["release_version"] = "0.2.15"
    with pytest.raises(ReleaseBuilderError, match="release_version_invalid"):
        ReleaseRequest.from_mapping(payload)


def test_windows_source_root_is_exact_sha_scoped_d_drive_path():
    expected = rf"D:\PlasticPromise\remote-builds\{SOURCE_COMMIT}\source"

    assert validate_windows_source_root(expected, SOURCE_COMMIT) == expected
    for invalid in (
        rf"C:\PlasticPromise\remote-builds\{SOURCE_COMMIT}\source",
        r"D:\PlasticPromise\source",
        rf"D:\PlasticPromise\remote-builds\{'b' * 40}\source",
        rf"D:\PlasticPromise\remote-builds\{SOURCE_COMMIT}\source\subdir",
    ):
        with pytest.raises(ReleaseBuilderError, match="release_source_workspace_invalid"):
            validate_windows_source_root(invalid, SOURCE_COMMIT)


def test_headless_builder_cannot_validate_stable_confirmation():
    request = _request()
    confirmation = confirm_request(request, now=NOW)

    with pytest.raises(ReleaseBuilderError, match="release_credentials_interactive_required"):
        validate_confirmation(
            request,
            confirmation,
            mode=BuilderMode.HEADLESS_BUILDER,
            now=NOW + timedelta(minutes=1),
        )


def test_confirmation_is_bound_to_request_hash_and_expires_after_30_minutes():
    request = _request()
    confirmation = confirm_request(request, now=NOW)

    validate_confirmation(
        request,
        confirmation,
        mode=BuilderMode.DESKTOP_INTERACTIVE,
        now=NOW + timedelta(minutes=30),
    )

    with pytest.raises(ReleaseBuilderError, match="release_confirmation_expired"):
        validate_confirmation(
            request,
            confirmation,
            mode=BuilderMode.DESKTOP_INTERACTIVE,
            now=NOW + timedelta(minutes=30, seconds=1),
        )

    changed = _request(actions=ReleaseActions(deploy_server=True, publish_pypi=True))
    with pytest.raises(ReleaseBuilderError, match="release_confirmation_hash_mismatch"):
        validate_confirmation(
            changed,
            confirmation,
            mode=BuilderMode.DESKTOP_INTERACTIVE,
            now=NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_snapshot(cpu_average_percent=75.0), "cpu_busy"),
        (_snapshot(available_ram_bytes=8 * 1024**3 - 1), "memory_low"),
        (_snapshot(gpu_average_utilization_percent=20.0), "gpu_busy"),
        (_snapshot(gpu_vram_used_bytes=4 * 1024**3), "gpu_vram_busy"),
        (_snapshot(gpu_temperature_celsius=75.0), "gpu_temperature_high"),
        (_snapshot(active_buildkit_build=True), "buildkit_active"),
        (_snapshot(inference_or_model_lock=True), "plastic_promise_accelerator_busy"),
        (_snapshot(d_drive_free_bytes=79 * 1024**3), "d_drive_space_low"),
    ],
)
def test_resource_gate_defers_without_queuing_when_machine_is_busy(
    snapshot: ResourceSnapshot, reason: str
):
    decision = evaluate_resource_gate(snapshot)

    assert decision.status == "deferred_resource_busy"
    assert decision.reason == reason
    assert decision.retry_queued is False


def test_resource_gate_accepts_full_10_second_idle_sample():
    decision = evaluate_resource_gate(_snapshot())

    assert decision.status == "ready"
    assert decision.reason is None


def test_resource_gate_rejects_under_sampled_observation():
    decision = evaluate_resource_gate(_snapshot(sample_seconds=9))

    assert decision.status == "deferred_resource_busy"
    assert decision.reason == "resource_sample_incomplete"


def test_resource_probe_aggregates_the_full_window_conservatively():
    snapshot = aggregate_resource_samples(
        (
            ResourceSample(10.0, 16 * 1024**3, 0.0, 0, 40.0, False, False, 100, 200),
            ResourceSample(30.0, 12 * 1024**3, 20.0, 4 * 1024**3, 60.0, True, True, 90, 200),
        ),
        observed_at=NOW,
        sample_seconds=10,
    )

    assert snapshot.cpu_average_percent == 20.0
    assert snapshot.available_ram_bytes == 12 * 1024**3
    assert snapshot.gpu_average_utilization_percent == 10.0
    assert snapshot.gpu_vram_used_bytes == 4 * 1024**3
    assert snapshot.active_buildkit_build is True
    assert snapshot.inference_or_model_lock is True
    assert snapshot.d_drive_free_bytes == 90


def test_resource_probe_observes_the_required_window_without_real_sleep(tmp_path):
    elapsed = {"seconds": 0.0}

    def sample(_disk_path: Path) -> ResourceSample:
        return ResourceSample(10.0, 16 * 1024**3, 0.0, 0, 40.0, False, False, 100, 200)

    snapshot = observe_resource_window(
        tmp_path,
        interval_seconds=5.0,
        sampler=sample,
        sleeper=lambda seconds: elapsed.__setitem__("seconds", elapsed["seconds"] + seconds),
        monotonic=lambda: elapsed["seconds"],
        now=lambda: NOW,
    )

    assert snapshot.sample_seconds == 10
    assert snapshot.cpu_average_percent == 10.0


def test_phase_order_uses_only_explicit_actions_and_stops_after_terminal_failure():
    request = _request(
        actions=ReleaseActions(
            push_ghcr_stable=True,
            publish_pypi=True,
            deploy_server=True,
            lancedb_promotion=True,
        )
    )
    ledger = ReleaseLedger()

    assert remaining_phases(request, ledger) == (
        ReleasePhase.RESOURCE_GATE,
        ReleasePhase.LOCAL_BUILD,
        ReleasePhase.GHCR_EVIDENCE,
        ReleasePhase.SERVER_DEPLOYMENT,
        ReleasePhase.MCP_E2E,
        ReleasePhase.LANCEDB_PROMOTION,
        ReleasePhase.MAINTENANCE_START,
        ReleasePhase.PYPI_PUBLICATION,
        ReleasePhase.RELEASE_SYNC,
        ReleasePhase.RELEASE_LEARNING,
    )

    ledger.record(ReleasePhase.RESOURCE_GATE, "passed")
    ledger.record(ReleasePhase.LOCAL_BUILD, "failed")
    assert remaining_phases(request, ledger) == ()


def test_phase_order_does_not_infer_production_work_from_a_local_build():
    request = _request(actions=ReleaseActions(start_maintenance=False))

    assert remaining_phases(request, ReleaseLedger()) == (
        ReleasePhase.RESOURCE_GATE,
        ReleasePhase.LOCAL_BUILD,
    )


def test_phase_order_does_not_start_maintenance_without_server_deployment():
    request = _request(actions=ReleaseActions(lancedb_promotion=True, start_maintenance=True))

    phases = remaining_phases(request, ReleaseLedger())
    assert ReleasePhase.MAINTENANCE_START not in phases
    assert ReleasePhase.LANCEDB_PROMOTION in phases


def test_retention_cleanup_never_selects_current_or_incomplete_and_caps_backups_at_five_days():
    old = NOW - timedelta(days=6)
    entries = (
        RetentionEntry("backup-old", "server-backup", old),
        RetentionEntry("backup-recent", "server-backup", NOW - timedelta(days=4, hours=23)),
        RetentionEntry("receipt-old", "receipt", NOW - timedelta(days=91)),
        RetentionEntry("incomplete", "receipt", NOW - timedelta(days=120), incomplete_request=True),
        RetentionEntry(
            "rollback", "source-archive", NOW - timedelta(days=20), rollback_protected=True
        ),
    )

    plan = plan_retention_cleanup(entries, now=NOW)

    assert plan.eligible_ids == ("backup-old", "receipt-old")
    assert plan.server_backup_retention_days == 5


def test_request_confirmation_and_receipt_are_secret_free_immutable_files(tmp_path):
    request = _request()
    request_path = write_request(tmp_path, request)
    confirmation = confirm_request(request, now=NOW)
    confirmation_path = write_confirmation(tmp_path, confirmation)
    receipt_path = write_receipt(
        tmp_path,
        request_hash=request.request_hash,
        phase=ReleasePhase.RESOURCE_GATE,
        attempt=1,
        outcome="passed",
        created_at=NOW,
        evidence_hashes={"resource_snapshot": "c" * 64},
    )

    assert load_request(request_path) == request
    assert load_confirmation(confirmation_path) == confirmation
    assert "api_token" not in receipt_path.read_text(encoding="utf-8")
    assert write_request(tmp_path, request) == request_path

    with pytest.raises(ReleaseBuilderError, match="release_receipt_immutable_conflict"):
        write_receipt(
            tmp_path,
            request_hash=request.request_hash,
            phase=ReleasePhase.RESOURCE_GATE,
            attempt=1,
            outcome="failed",
            created_at=NOW,
            evidence_hashes={"resource_snapshot": "d" * 64},
        )

    retry_path = write_receipt(
        tmp_path,
        request_hash=request.request_hash,
        phase=ReleasePhase.RESOURCE_GATE,
        attempt=2,
        outcome="passed",
        created_at=NOW,
        evidence_hashes={"resource_snapshot": "e" * 64},
    )
    assert retry_path != receipt_path


def test_release_builder_cli_submits_then_confirms_the_exact_hash(tmp_path, capsys):
    request = _request()
    request_input = tmp_path / "request.json"
    request_input.write_text(__import__("json").dumps(request.to_mapping()), encoding="utf-8")

    assert (
        release_builder_main(
            [
                "submit",
                "--state-root",
                str(tmp_path / "state"),
                "--request",
                str(request_input),
            ]
        )
        == 0
    )
    assert request.request_hash in capsys.readouterr().out

    assert (
        release_builder_main(
            [
                "confirm",
                "--state-root",
                str(tmp_path / "state"),
                "--request-hash",
                request.request_hash,
                "--confirm-request-hash",
                request.request_hash,
            ]
        )
        == 0
    )
    assert "confirmed" in capsys.readouterr().out

    assert (
        release_builder_main(
            [
                "confirm",
                "--state-root",
                str(tmp_path / "state"),
                "--request-hash",
                request.request_hash,
                "--confirm-request-hash",
                "b" * 64,
            ]
        )
        == 2
    )


def test_windows_release_builder_install_and_local_build_scripts_keep_credentials_bounded():
    install = (REPO_ROOT / "deploy" / "release-builder" / "windows-install.ps1").read_text(
        encoding="utf-8"
    )
    build = (REPO_ROOT / "scripts" / "run_windows_local_inference_build.ps1").read_text(
        encoding="utf-8"
    )
    rendered_build = build.replace("`", "")

    assert "desktop-interactive" in install
    assert "headless-builder" in install
    assert "D:\\PlasticPromise\\release-builder" in install
    assert "Register-ScheduledTask" not in install
    assert "TOKEN" not in install
    assert "PRIVATE KEY" not in install
    assert "CredentialMode" in build
    assert "desktop-interactive" in build
    assert "headless-builder" in build
    assert "docker-config" in build
    assert "credential_mode" in build
    assert "validate-windows-source" in build
    assert "resource-gate" in build
    assert "$dockerConfigArguments = @('--config', $dockerConfigDirectory)" in build
    assert "[Guid]::NewGuid().ToString('N')" in build
    assert "[System.IO.Path]::GetTempPath()" in build
    assert '"https://index.docker.io/v1/":{}' in rendered_build
    assert '"registry-1.docker.io":{}' in rendered_build
    assert '"credsStore":""' in rendered_build
    assert '"credHelpers":{}' in rendered_build
    assert "function Invoke-PpDocker" in build
    assert "[string]$DockerCommand = 'docker.exe'" in build
    assert "$proxyUri.UserInfo" in build
    assert "windows_local_build_proxy_url_invalid" in build
    assert build.index("$proxyUri.UserInfo") < build.index("$env:HTTP_PROXY = $ProxyUrl")
    assert build.index("$proxyUri.UserInfo") < build.index('"HTTP_PROXY=$ProxyUrl"')
    assert "Invoke-DockerPullWithRetry" in build
    assert "Invoke-DockerBuildWithRetry" in build
    assert "BuildAttempts" in build
    assert "DockerHubMirror" in build
    assert "mirror.gcr.io" in build
    assert "buildkitd.toml" in build
    assert "--buildkitd-config" in build
    assert "Invoke-PpDocker -Arguments @('image', 'inspect', $Image)" in build
    assert build.index("Test-DockerImagePresent -Image $Image") < build.index(
        "Invoke-PpDocker -Arguments @('pull', $Image)"
    )
    assert (
        "moby/buildkit@sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec"
    ) in build
    assert "'--driver-opt', \"image=$BuildkitImage\"" in build
    assert "Join-Path $env:TEMP 'plastic-promise-buildx'" in build
    assert "D:\\PlasticPromise\\buildx" not in build
    assert "$env:BUILDX_CONFIG = $BuildxConfigDirectory" in build
    assert "Remove-Item -LiteralPath $BuildxConfigDirectory" not in build
    assert "[regex]::Match(" in build
    assert "$pyprojectContent," in build
    assert "$versionMatch.Groups[1].Value" in build
    assert "& $pythonExecutable @pythonPrefix '-c'" not in build
    assert "scripts/resolve_container_artifact_identity.py" in build
    assert "--verify-head" in build
    assert "local-builder-catalog" in build
    assert "BASE_IMAGE=$baseImage" in build
    assert "BASE_IMAGE_DIGEST=$baseImageDigest" in build
    assert "COMPUTE_VARIANT=$computeVariant" in build
    assert "BUILD_POLICY_DIGEST=$buildPolicyDigest" in build
    assert "RECIPE_POLICY_DIGEST=$recipePolicyDigest" in build
    assert "'--format', '{{json .Config.Labels}}'" in build
    assert "ConvertFrom-Json" in build
    assert "$expectedLabels = [ordered]@{" in build
    assert "$actualProperty = $imageLabels.PSObject.Properties[$labelName]" in build
    assert "$actualValue = if ($null -eq $actualProperty)" in build
    assert "'org.opencontainers.image.base.name'" in build
    assert "'org.opencontainers.image.base.digest'" in build
    assert "'org.plastic-promise.build.policy-digest'" in build
    assert "'org.plastic-promise.build.recipe-policy-digest'" in build
    assert "base_image_digest = $baseImageDigest" in build
    assert "build_policy_digest = $buildPolicyDigest" in build
    assert "recipe_policy_digest = $recipePolicyDigest" in build
    assert '{{ index .Config.Labels "org.opencontainers.image.revision" }}' not in build
    assert "$cleanupArguments += @('--docker-config', $dockerConfigDirectory)" in build
    assert "Remove-Item -LiteralPath $dockerConfigDirectory -Recurse -Force" in build
    assert "Join-Path $reportPath 'docker-config'" not in build
    assert "if (-not (Test-Path -LiteralPath $dockerConfigPath))" not in build
