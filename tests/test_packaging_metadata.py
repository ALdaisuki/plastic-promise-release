from __future__ import annotations

import importlib
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10.
    import tomli as tomllib


def test_project_uses_modern_spdx_license_metadata():
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = payload["project"]

    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert "License :: OSI Approved :: MIT License" not in project["classifiers"]


def test_source_distribution_includes_public_runtime_templates_and_release_docs():
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8")

    assert "include scripts/init_and_start.py" in manifest
    assert "include scripts/plastic-promise-deploy.sh" in manifest
    assert "include scripts/plastic-promise-deploy.ps1" in manifest
    assert "include scripts/verify_release_deployment.py" in manifest
    assert "include scripts/create_release_bundle.py" in manifest
    assert "include deploy/release-builder/windows-install.ps1" in manifest
    assert "recursive-include deploy/manifests *.json" in manifest
    assert "include deploy/oci-base-images.json" in manifest
    assert "include deploy/server/Dockerfile" in manifest
    assert "recursive-include deploy/server *.yaml *.example" in manifest
    assert "include deploy/local-edge/Dockerfile" in manifest
    assert "recursive-include deploy/local-edge *.yaml *.conf *.sh" in manifest
    assert "include deploy/local-inference-node/Dockerfile" in manifest
    assert "include scripts/resolve_container_artifact_identity.py" in manifest
    assert "recursive-include deploy/local-inference-node *.yaml *.example" in manifest
    assert "recursive-include docs/release *.md" in manifest
    assert "recursive-include docs/deployment *.md" in manifest
    assert "recursive-include docs/architecture/release-delivery *.md *.mermaid" in manifest
    assert (
        "recursive-include docs/architecture/three-endpoint-deployment *.md *.mermaid *.txt *.svg"
        in manifest
    )


def test_runtime_modules_keep_python_310_import_compatibility():
    """Guard Python 3.10 against newer stdlib-only imports in runtime modules."""

    modules = (
        "plastic_promise.core.knowledge_base",
        "plastic_promise.core.production_readiness",
        "plastic_promise.core.proposal_promotion",
        "plastic_promise.core.proposal_promotion_tasks",
        "plastic_promise.core.workflow_state",
        "plastic_promise.mcp.tools.skill_tracking",
        "plastic_promise.passive_memory.coordinator",
        "plastic_promise.reflection.soul_scarf",
    )

    for module_name in modules:
        assert importlib.import_module(module_name) is not None
