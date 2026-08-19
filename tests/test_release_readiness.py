from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from plastic_promise.release_readiness import (
    RELEASE_DEPLOYMENT_VERIFICATION_SCHEMA_VERSION,
    ReleaseReadinessError,
    verify_source_only_deployment,
)


class _BlockedPreflightController:
    def preflight(self, _plan: object) -> object:
        return type("BlockedPreflight", (), {"ok": False})()


def test_source_only_profile_proof_parses_every_release_profile_without_creating_state(
    tmp_path: Path,
) -> None:
    examples = tuple(sorted(Path("deploy/manifests").glob("*.example.json")))

    for example in examples:
        state_root = tmp_path / example.stem
        result = verify_source_only_deployment(
            manifest_path=example,
            state_root=state_root,
        )

        payload = result.as_dict()
        assert payload["schema_version"] == RELEASE_DEPLOYMENT_VERIFICATION_SCHEMA_VERSION
        assert payload["source_only"] is True
        assert payload["profile"] in {
            "local-all-in-one",
            "local-cloud",
            "split-accelerated",
        }
        assert len(str(payload["plan_hash"])) == 71
        assert not state_root.exists()


def test_source_only_profile_proof_rejects_an_existing_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "existing"
    state_root.mkdir()

    with pytest.raises(ReleaseReadinessError, match="release_verify_state_root_must_be_absent"):
        verify_source_only_deployment(
            manifest_path=Path("deploy/manifests/local-cloud.example.json"),
            state_root=state_root,
        )


def test_source_only_profile_proof_fails_closed_when_preflight_is_blocked(tmp_path: Path) -> None:
    state_root = tmp_path / "blocked"

    with pytest.raises(ReleaseReadinessError, match="release_verify_preflight_failed"):
        verify_source_only_deployment(
            manifest_path=Path("deploy/manifests/local-cloud.example.json"),
            state_root=state_root,
            controller=_BlockedPreflightController(),  # type: ignore[arg-type]
        )

    assert not state_root.exists()


def test_source_only_release_script_has_no_mutating_operation_surface(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_release_deployment.py",
            "--manifest",
            "deploy/manifests/local-cloud.example.json",
            "--state-root",
            str(state_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["source_only"] is True
    assert not state_root.exists()


def test_source_only_release_script_rejects_deployment_operation_arguments(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_release_deployment.py",
            "--manifest",
            "deploy/manifests/local-cloud.example.json",
            "--state-root",
            str(state_root),
            "apply",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert not state_root.exists()


def test_source_only_contract_import_does_not_eagerly_require_starlette() -> None:
    source = """
import builtins

real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'starlette' or name.startswith('starlette.'):
        raise AssertionError('source_only_import_must_not_require_starlette')
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from plastic_promise.release_readiness import verify_source_only_deployment
assert callable(verify_source_only_deployment)
"""
    result = subprocess.run(
        (sys.executable, "-c", source),
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
