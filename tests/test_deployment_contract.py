import json
from pathlib import Path

import pytest

from plastic_promise.deployment import DEPLOYMENT_MANIFEST_SCHEMA_VERSION


def _manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema_version": DEPLOYMENT_MANIFEST_SCHEMA_VERSION,
        "deployment_id": "development-laptop",
        "profile": "local-all-in-one",
        "modules": {},
        "nodes": [],
    }
    manifest.update(overrides)
    return manifest


def test_stable_profiles_are_the_three_supported_release_topologies():
    from plastic_promise.deployment import stable_profile_ids

    assert stable_profile_ids() == (
        "local-all-in-one",
        "local-cloud",
        "split-accelerated",
    )


def test_resolve_manifest_adds_profile_modules_without_high_risk_modules():
    from plastic_promise.deployment import resolve_deployment_manifest

    plan = resolve_deployment_manifest(_manifest())

    assert plan.profile_id == "local-all-in-one"
    assert plan.module_ids == (
        "canonical-runtime",
        "durable-outbox",
        "derived-index",
        "operator-dashboard",
    )
    assert "accelerator-max" not in plan.module_ids
    assert "maintenance-daemon" not in plan.module_ids


def test_high_risk_module_requires_an_explicit_acknowledgement():
    from plastic_promise.deployment import DeploymentContractError, resolve_deployment_manifest

    manifest = _manifest(modules={"accelerator-max": {"enabled": True}})

    with pytest.raises(DeploymentContractError, match="high_risk_module_acknowledgement_required"):
        resolve_deployment_manifest(manifest)


def test_high_risk_module_is_resolved_only_after_explicit_acknowledgement():
    from plastic_promise.deployment import resolve_deployment_manifest

    manifest = _manifest(
        modules={
            "accelerator-max": {
                "enabled": True,
                "acknowledge_high_risk": True,
            }
        }
    )

    plan = resolve_deployment_manifest(manifest)

    assert plan.module_ids == (
        "canonical-runtime",
        "durable-outbox",
        "derived-index",
        "operator-dashboard",
        "accelerator-max",
    )


def test_manifest_module_resolution_is_independent_of_json_key_order():
    from plastic_promise.deployment import resolve_deployment_manifest

    first = resolve_deployment_manifest(
        _manifest(
            modules={
                "local-ollama": {"enabled": True},
                "accelerator-max": {
                    "enabled": True,
                    "acknowledge_high_risk": True,
                },
            }
        )
    )
    second = resolve_deployment_manifest(
        _manifest(
            modules={
                "accelerator-max": {
                    "enabled": True,
                    "acknowledge_high_risk": True,
                },
                "local-ollama": {"enabled": True},
            }
        )
    )

    assert first == second
    assert first.module_ids[-2:] == ("local-ollama", "accelerator-max")


def test_manifest_rejects_an_optional_module_that_is_unsafe_for_its_profile():
    from plastic_promise.deployment import DeploymentContractError, resolve_deployment_manifest

    with pytest.raises(DeploymentContractError, match="manifest_module_profile_incompatible"):
        resolve_deployment_manifest(
            _manifest(
                profile="local-cloud",
                modules={"local-ollama": {"enabled": True}},
            )
        )


def test_manifest_rejects_secret_values_and_secret_named_fields():
    from plastic_promise.deployment import DeploymentContractError, resolve_deployment_manifest

    with pytest.raises(DeploymentContractError, match="secret_field_forbidden"):
        resolve_deployment_manifest(_manifest(api_key="not-allowed"))

    with pytest.raises(DeploymentContractError, match="secret_value_detected"):
        resolve_deployment_manifest(_manifest(metadata={"note": "sk-" + ("x" * 32)}))


def test_manifest_loads_from_json_file_at_the_public_file_seam(tmp_path: Path):
    from plastic_promise.deployment import load_deployment_manifest

    path = tmp_path / "deployment.json"
    path.write_text(
        json.dumps(_manifest(profile="local-cloud")),
        encoding="utf-8",
    )

    plan = load_deployment_manifest(path)

    assert plan.profile_id == "local-cloud"
    assert "cloud-inference" in plan.module_ids
    assert "canonical-runtime" in plan.module_ids


def test_profile_documentation_manifest_example_is_a_valid_contract():
    from plastic_promise.deployment import resolve_deployment_manifest

    documentation = Path("docs/deployment/profiles.md").read_text(encoding="utf-8")
    sample = documentation.split("```json\n", 1)[1].split("\n```", 1)[0]

    plan = resolve_deployment_manifest(json.loads(sample))

    assert plan.profile_id == "local-all-in-one"
    assert "local-ollama" in plan.module_ids


def test_profiles_publish_resource_and_scheduling_defaults_for_future_preflight():
    from plastic_promise.deployment import stable_profiles

    profiles = {profile.id: profile for profile in stable_profiles()}

    assert profiles["local-all-in-one"].scheduling_default == "remote-node-first"
    assert profiles["local-cloud"].scheduling_default == "cloud-only"
    assert profiles["split-accelerated"].scheduling_default == "remote-node-first"
    assert profiles["local-all-in-one"].resource_policy.minimum_free_bytes == 10 * 1024**3
    assert profiles["split-accelerated"].resource_policy.minimum_free_fraction == 0.2


def test_split_accelerated_requires_a_declared_local_inference_node():
    from plastic_promise.deployment import DeploymentContractError, resolve_deployment_manifest

    with pytest.raises(DeploymentContractError, match="manifest_inference_node_required"):
        resolve_deployment_manifest(_manifest(profile="split-accelerated"))

    plan = resolve_deployment_manifest(
        _manifest(
            profile="split-accelerated",
            nodes=[
                {
                    "id": "home-accelerator",
                    "role": "local-heterogeneous-inference-node",
                    "ssh_host": "home-accelerator",
                    "capabilities": {"embedding": True, "rerank": True},
                    "max_concurrency": 3,
                }
            ],
        )
    )
    assert plan.node_ids == ("home-accelerator",)
    assert plan.nodes[0].ssh_host == "home-accelerator"
    assert plan.nodes[0].max_concurrency == 3


def test_manifest_defaults_and_validates_bounded_node_capacity():
    from plastic_promise.deployment import DeploymentContractError, resolve_deployment_manifest

    node = {
        "id": "home-accelerator",
        "role": "local-heterogeneous-inference-node",
        "ssh_host": "home-accelerator",
        "capabilities": {"embedding": True, "rerank": True},
        "max_concurrency": 0,
    }
    with pytest.raises(DeploymentContractError, match="manifest_node_max_concurrency_invalid"):
        resolve_deployment_manifest(_manifest(profile="split-accelerated", nodes=[node]))

    node.pop("max_concurrency")
    plan = resolve_deployment_manifest(_manifest(profile="split-accelerated", nodes=[node]))
    assert plan.nodes[0].max_concurrency == 1


def test_manifest_rejects_placeholder_capacity_for_selected_components():
    from plastic_promise.deployment import DeploymentContractError, resolve_deployment_manifest

    with pytest.raises(
        DeploymentContractError,
        match="resource_budget_estimate_required:lancedb_shadow_rebuild_bytes",
    ):
        resolve_deployment_manifest(
            _manifest(
                resource_budget={
                    "image_layers_bytes": 0,
                    "image_unpack_bytes": 0,
                    "model_cache_bytes": 0,
                    "lancedb_shadow_rebuild_bytes": 0,
                    "rollback_coexistence_bytes": 1,
                }
            )
        )


def test_layering_check_rejects_direct_and_literal_deferred_cross_layer_imports(tmp_path: Path):
    from plastic_promise.deployment.layering import check_deployment_layering

    deployment_dir = tmp_path / "plastic_promise" / "deployment"
    deployment_dir.mkdir(parents=True)
    (deployment_dir / "direct.py").write_text(
        "from plastic_promise.mcp.server import create_server\n",
        encoding="utf-8",
    )
    (deployment_dir / "deferred.py").write_text(
        "import importlib\nimportlib.import_module('plastic_promise.memory.repository')\n",
        encoding="utf-8",
    )
    (deployment_dir / "safe_name.py").write_text(
        "from plastic_promise.memoryful import helper\n",
        encoding="utf-8",
    )
    (deployment_dir / "aliased.py").write_text(
        "import importlib as loader\nloader.import_module('plastic_promise.mcp.server')\n",
        encoding="utf-8",
    )
    (deployment_dir / "one.py").write_text(
        "from plastic_promise.deployment import two\n",
        encoding="utf-8",
    )
    (deployment_dir / "two.py").write_text(
        "from plastic_promise.deployment import one\n",
        encoding="utf-8",
    )

    violations = check_deployment_layering(tmp_path)

    assert {(item.kind, item.target) for item in violations} == {
        ("import", "plastic_promise.mcp.server"),
        ("deferred-import", "plastic_promise.memory.repository"),
        ("deferred-import", "plastic_promise.mcp.server"),
        ("cycle", "one -> two -> one"),
    }


def test_layering_check_rejects_relative_alias_import_cycles(tmp_path: Path):
    from plastic_promise.deployment.layering import check_deployment_layering

    deployment_dir = tmp_path / "plastic_promise" / "deployment"
    deployment_dir.mkdir(parents=True)
    (deployment_dir / "one.py").write_text("from . import two\n", encoding="utf-8")
    (deployment_dir / "two.py").write_text("from . import one\n", encoding="utf-8")

    violations = check_deployment_layering(tmp_path)

    assert {(item.kind, item.target) for item in violations} == {
        ("cycle", "one -> two -> one"),
    }


def test_layering_check_rejects_relative_imports_that_escape_deployment(tmp_path: Path):
    from plastic_promise.deployment.layering import check_deployment_layering

    deployment_dir = tmp_path / "plastic_promise" / "deployment" / "nested"
    deployment_dir.mkdir(parents=True)
    (deployment_dir / "check.py").write_text(
        "from ...mcp import server\nfrom ...memory import repository\n",
        encoding="utf-8",
    )

    violations = check_deployment_layering(tmp_path)

    assert {(item.kind, item.target) for item in violations} == {
        ("import", "plastic_promise.mcp"),
        ("import", "plastic_promise.memory"),
    }


def test_deployment_docs_cover_all_stable_profiles_and_lifecycle_boundaries():
    documents = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "docs/deployment/config-baselines.md",
            "docs/deployment/resource-planning.md",
            "docs/deployment/startup-modes.md",
        )
    )

    for profile_id in ("local-all-in-one", "local-cloud", "split-accelerated"):
        assert profile_id in documents
    for term in ("Minimum", "Recommended", "preflight", "doctor", "upgrade", "restore"):
        assert term in documents
    for template in (
        "local-all-in-one.example.json",
        "local-cloud.example.json",
        "split-accelerated.example.json",
    ):
        assert template in documents


def test_real_deployment_package_satisfies_its_layering_contract():
    from plastic_promise.deployment.layering import check_deployment_layering

    assert check_deployment_layering(Path.cwd()) == ()
