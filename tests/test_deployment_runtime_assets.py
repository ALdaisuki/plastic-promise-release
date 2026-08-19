from __future__ import annotations

from typing import TYPE_CHECKING

from plastic_promise.deployment import (
    DEPLOYMENT_MANIFEST_SCHEMA_VERSION,
    DeploymentController,
    HostDiskUsage,
    create_deployment_plan,
    resolve_deployment_manifest,
)
from plastic_promise.deployment.runtime_assets import (
    materialize_runtime_assets,
    remove_runtime_assets,
    runtime_asset_directory,
    runtime_assets_for_module,
)

if TYPE_CHECKING:
    from pathlib import Path


def _split_manifest(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": DEPLOYMENT_MANIFEST_SCHEMA_VERSION,
        "deployment_id": "accelerated-test",
        "profile": "split-accelerated",
        "modules": {},
        "nodes": [
            {
                "id": "home-accelerator",
                "role": "local-heterogeneous-inference-node",
                "ssh_host": "home-accelerator",
                "capabilities": {"embedding": True, "rerank": True},
                "max_concurrency": 1,
            }
        ],
        "resource_locations": {
            "container_store": str(tmp_path / "container-store"),
            "model_cache": str(tmp_path / "model-cache"),
        },
        "resource_budget": {
            "image_layers_bytes": 1,
            "image_unpack_bytes": 1,
            "model_cache_bytes": 1,
            "lancedb_shadow_rebuild_bytes": 1,
            "rollback_coexistence_bytes": 1,
        },
    }


def _plan(tmp_path: Path):
    return create_deployment_plan(
        resolve_deployment_manifest(_split_manifest(tmp_path)),
        state_root=tmp_path / "state",
    )


def test_split_install_materializes_real_no_pull_platform_assets(tmp_path: Path):
    plan = _plan(tmp_path)
    controller = DeploymentController(
        disk_usage=lambda _path: HostDiskUsage(
            total_bytes=100 * 1024**3,
            free_bytes=90 * 1024**3,
        )
    )

    outcome = controller.apply(plan, plan_hash=plan.plan_hash)

    assert outcome.database_action == "created"
    canonical_directory = runtime_asset_directory(plan, "canonical-runtime")
    canonical_environment = (canonical_directory / "canonical-runtime.env").read_text(
        encoding="utf-8"
    )
    assert 'TZ="UTC"' in canonical_environment
    assert f'PLASTIC_DB_PATH="{plan.target.database_path}"' in canonical_environment
    assert f'PLASTIC_LANCEDB_PATH="{plan.target.state_root / "lancedb"}"' in canonical_environment
    canonical_assets = runtime_assets_for_module(plan, "canonical-runtime")
    assert len(canonical_assets) == 2
    assert (canonical_directory / canonical_assets[1].relative_path).is_file()

    asset_directory = runtime_asset_directory(plan, "heterogeneous-inference-node")
    compose = (asset_directory / "compose.yaml").read_text(encoding="utf-8")
    assert "pull_policy: never" in compose
    assert "build:" not in compose
    assert "PP_LOCAL_NODE_IMAGE" in compose
    assert (
        "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE: "
        '"${PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE:-/models/embedding}"'
    ) in compose
    assert (
        "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE: "
        '"${PP_LOCAL_NODE_RERANK_MODEL_REFERENCE:-/models/rerank}"'
    ) in compose
    assert 'TZ: "UTC"' in compose
    assert (
        "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION: "
        '"${PP_LOCAL_NODE_EMBEDDING_NORMALIZATION:?set normalization}"'
    ) in compose
    receipt = (
        plan.target.state_root / "runtime-components" / "heterogeneous-inference-node.json"
    ).read_text(encoding="utf-8")
    assert '"activation": "explicit-platform-asset"' in receipt
    assert '"assets": ["compose.yaml",' in receipt


def test_runtime_asset_selection_covers_linux_macos_and_windows(tmp_path: Path):
    plan = _plan(tmp_path)

    linux = runtime_assets_for_module(plan, "heterogeneous-inference-node", system_name="Linux")
    macos = runtime_assets_for_module(plan, "heterogeneous-inference-node", system_name="Darwin")
    windows = runtime_assets_for_module(plan, "heterogeneous-inference-node", system_name="Windows")

    assert [asset.relative_path.name for asset in linux] == [
        "compose.yaml",
        "plastic-promise-local-inference-node-compose.service",
    ]
    assert [asset.relative_path.name for asset in macos] == [
        "compose.yaml",
        "org.plastic-promise.local-inference-node.plist",
    ]
    assert [asset.relative_path.name for asset in windows] == [
        "compose.yaml",
        "start-local-inference-node.ps1",
    ]
    assert all("pull_policy: never" in asset.content for asset in (linux[0], macos[0], windows[0]))
    assert all(
        "/tmp:rw,exec,nosuid,size=512m" in asset.content
        for asset in (linux[0], macos[0], windows[0])
    )
    assert all(
        "/tmp:rw,noexec,nosuid,size=512m" not in asset.content
        for asset in (linux[0], macos[0], windows[0])
    )
    assert "/bin/sh" not in macos[1].content
    assert "-lc" not in macos[1].content
    assert "<string>docker</string>" in macos[1].content

    for system_name, activation_asset_name in (
        ("Linux", "plastic-promise-canonical-runtime.service"),
        ("Darwin", "org.plastic-promise.canonical-runtime.plist"),
        ("Windows", "start-plastic-promise-runtime.ps1"),
    ):
        assets = runtime_assets_for_module(plan, "canonical-runtime", system_name=system_name)
        assert [asset.relative_path.name for asset in assets] == [
            "canonical-runtime.env",
            activation_asset_name,
        ]
        assert "PLASTIC_DB_PATH" in assets[0].content
        assert "PLASTIC_LANCEDB_PATH" in assets[0].content
        assert "TZ" in assets[0].content
        assert "UTC" in assets[0].content
        assert "plastic-promise-canonical-runtime" in assets[1].content
        assert "plastic-promise-streamable-http" not in assets[1].content
        if system_name == "Darwin":
            assert "/bin/sh" not in assets[1].content
            assert "-lc" not in assets[1].content
            assert "<key>TZ</key>" in assets[1].content
        if system_name == "Windows":
            assert "$env:TZ = 'UTC'" in assets[1].content

    rendered = "\n".join(asset.content for asset in (*linux, *macos, *windows))
    for forbidden in ("timedatectl", "Set-TimeZone", "/etc/localtime", "/etc/timezone"):
        assert forbidden not in rendered


def test_runtime_asset_removal_keeps_unrelated_user_files(tmp_path: Path):
    plan = _plan(tmp_path)
    asset_directory = runtime_asset_directory(plan, "heterogeneous-inference-node")
    materialize_runtime_assets(plan, "heterogeneous-inference-node")
    user_file = asset_directory / "operator-notes.txt"
    user_file.write_text("retain", encoding="utf-8")

    remove_runtime_assets(plan, "heterogeneous-inference-node")

    assert user_file.read_text(encoding="utf-8") == "retain"
    assert not (asset_directory / "compose.yaml").exists()
