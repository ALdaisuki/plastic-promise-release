"""Focused contracts for the one-click compute-node build planning module."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from plastic_promise.deployment.node_build import (
    DeploymentContractError,
    plan_node_build,
    run_node_build,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_plan_resolves_the_platform_one_click_script(monkeypatch):
    for platform_name, expected_suffix in (
        ("Windows", "build_compute_node.ps1"),
        ("Darwin", "build_compute_node.sh"),
        ("Linux", "build_compute_node.sh"),
    ):
        monkeypatch.setattr(platform, "system", lambda name=platform_name: name)
        plan = plan_node_build(source_revision=_git_head())
        assert plan.script.name == expected_suffix
        assert plan.script.is_file()
        assert plan.dry_run is False


def test_plan_validates_source_revision(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    with pytest.raises(DeploymentContractError, match="build_node_source_revision_invalid"):
        plan_node_build(source_revision="not-a-sha")
    with pytest.raises(DeploymentContractError, match="build_node_source_revision_invalid"):
        plan_node_build(source_revision="abc")


def test_plan_auto_detects_variant_from_nvidia_smi(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("plastic_promise.deployment.node_build.shutil.which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    plan = plan_node_build(source_revision=_git_head())
    assert plan.variant == "cuda"

    monkeypatch.setattr("plastic_promise.deployment.node_build.shutil.which", lambda name: None)
    plan = plan_node_build(source_revision=_git_head())
    assert plan.variant == "cpu"


def test_plan_forwards_operator_overrides(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    plan = plan_node_build(
        source_revision=_git_head(),
        variant="cuda",
        builder="plastic-promise-local",
        image_tag="plastic-promise-local-inference-node:test",
        retention_hours=12,
        skip_gpu_smoke=True,
        no_start=True,
        dry_run=True,
    )
    assert "--variant" in plan.arguments and "cuda" in plan.arguments
    assert "--builder" in plan.arguments
    assert "--image-tag" in plan.arguments
    assert "--retention-hours" in plan.arguments and "12" in plan.arguments
    assert "--skip-gpu-smoke" in plan.arguments
    assert "--no-start" in plan.arguments
    assert plan.dry_run is True


def test_cli_build_node_dry_run_prints_a_plan_without_executing():
    result = subprocess.run(
        [sys.executable, "-m", "plastic_promise.deployment", "build-node", "--dry-run", "--json"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "plastic-promise/deployment-node-build/v1"
    assert payload["executed"] is False
    assert payload["script"].endswith(("build_compute_node.sh", "build_compute_node.ps1"))
    assert len(payload["source_revision"]) == 40
    assert payload["variant"] in {"cpu", "cuda"}


def test_run_node_build_executes_the_resolved_script(monkeypatch):
    calls: list[list[str]] = []

    def fake_call(command, cwd):
        calls.append(list(command))
        return 0

    monkeypatch.setattr("plastic_promise.deployment.node_build.subprocess.call", fake_call)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    plan = plan_node_build(source_revision=_git_head(), dry_run=False)
    exit_code = run_node_build(plan)
    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] == "bash"
    assert calls[0][1] == str(plan.script)
    assert "--source-revision" in calls[0]
    assert "--variant" in calls[0]


def test_run_node_build_uses_powershell_for_windows(monkeypatch):
    calls: list[list[str]] = []

    def fake_call(command, cwd):
        calls.append(list(command))
        return 0

    monkeypatch.setattr("plastic_promise.deployment.node_build.subprocess.call", fake_call)
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    plan = plan_node_build(source_revision=_git_head(), dry_run=False)
    exit_code = run_node_build(plan)
    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] == "powershell.exe"
    assert "-File" in calls[0]
    assert "--variant" in calls[0]
