import json
from pathlib import Path
from xml.etree import ElementTree

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_release_dockerfiles_require_immutable_build_identity_and_exclude_runtime_state():
    dockerfiles = {
        "pp-local-edge": (REPOSITORY_ROOT / "deploy" / "local-edge" / "Dockerfile").read_text(
            encoding="utf-8"
        ),
        "pp-server-backend": (REPOSITORY_ROOT / "deploy" / "server" / "Dockerfile").read_text(
            encoding="utf-8"
        ),
        "pp-compute-node": (
            REPOSITORY_ROOT / "deploy" / "local-inference-node" / "Dockerfile"
        ).read_text(encoding="utf-8"),
    }
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
    base_catalog = json.loads(
        (REPOSITORY_ROOT / "deploy" / "oci-base-images.json").read_text(encoding="utf-8")
    )

    assert base_catalog["schema_version"] == "plastic-promise-oci-base-images/v1"
    assert {(item["role"], item["variant"]) for item in base_catalog["images"]} == {
        ("pp-local-edge", "standard"),
        ("pp-server-backend", "standard"),
        ("pp-compute-node", "cpu"),
        ("pp-compute-node", "cuda"),
    }
    assert all("@sha256:" in item["reference"] for item in base_catalog["images"])

    for dockerfile in dockerfiles.values():
        assert "ENV TZ=UTC" in dockerfile
        assert "ARG BASE_IMAGE" in dockerfile
        assert "FROM ${BASE_IMAGE}" in dockerfile
        assert "ARG BASE_IMAGE_DIGEST" in dockerfile
        assert 'test "$BASE_IMAGE_DIGEST" = "${BASE_IMAGE##*@}"' in dockerfile
        assert 'case "$BASE_IMAGE" in *@sha256:*) ;; *) exit 64 ;; esac' in dockerfile
        assert 'org.opencontainers.image.base.name="${BASE_IMAGE}"' in dockerfile
        assert 'org.opencontainers.image.base.digest="${BASE_IMAGE_DIGEST}"' in dockerfile
        assert 'org.plastic-promise.build.policy-digest="${BUILD_POLICY_DIGEST}"' in dockerfile
        assert (
            'org.plastic-promise.build.recipe-policy-digest="${RECIPE_POLICY_DIGEST}"' in dockerfile
        )

    assert 'ENTRYPOINT ["plastic-promise-canonical-runtime"]' in dockerfiles["pp-server-backend"]
    assert "USER ppruntime" in dockerfiles["pp-server-backend"]
    assert "USER ppnode" in dockerfiles["pp-compute-node"]
    for excluded in (
        "*.db",
        "*.db-*",
        "*.sqlite",
        "*.sqlite-*",
        "*.key",
        ".env",
        "state",
        "runtime",
        "logs",
        "lancedb",
        "lancedb/**",
        "*.lance",
        "models",
    ):
        assert excluded in dockerignore
    for forbidden in ("COPY state", "COPY runtime", "COPY models", "COPY .env"):
        assert all(forbidden not in dockerfile for dockerfile in dockerfiles.values())


def test_server_distribution_assets_require_an_immutable_image_and_keep_native_runtime_separate():
    compose = (REPOSITORY_ROOT / "deploy" / "server" / "compose.yaml").read_text(encoding="utf-8")
    systemd = (
        REPOSITORY_ROOT / "deploy" / "server" / "plastic-promise-server.service.example"
    ).read_text(encoding="utf-8")
    environment = (
        REPOSITORY_ROOT / "deploy" / "server" / "plastic-promise-server.env.example"
    ).read_text(encoding="utf-8")

    assert "PLASTIC_PROMISE_SERVER_IMAGE:?set an immutable server image digest" in compose
    assert "pull_policy: never" in compose
    assert "network_mode: host" in compose
    assert (
        'PLASTIC_RUNTIME_LOCK_PATH: "${PLASTIC_RUNTIME_LOCK_PATH:?set the shared canonical runtime lock path}"'
        in compose
    )
    assert "ExecStart=/usr/bin/env plastic-promise-canonical-runtime" in systemd
    assert "Environment=TZ=UTC" in systemd
    assert "ReadWritePaths=/var/lib/plastic-promise" in systemd
    assert "PLASTIC_PROMISE_SERVER_IMAGE=" in environment
    assert "TZ=UTC" in environment
    assert "PLASTIC_DB_PATH=/var/lib/plastic-promise/db/plastic_memory.db" in environment
    assert "PLASTIC_LANCEDB_PATH=/var/lib/plastic-promise/lancedb" in environment
    assert "PLASTIC_RUNTIME_LOCK_PATH=/var/lib/plastic-promise/runtime/mcp.lock" in environment
    assert "OPENAI_API_KEY" not in environment


def test_three_endpoint_runtime_assets_use_utc_without_mutating_host_timezone():
    compose_paths = (
        "deploy/local-edge/compose.yaml",
        "deploy/server/compose.yaml",
        "deploy/local-inference-node/compose.cpu.yaml",
        "deploy/local-inference-node/compose.cuda.yaml",
        "deploy/local-inference-node/compose.yaml",
    )
    combined = "\n".join(
        (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in compose_paths
    )
    dashboard = (
        REPOSITORY_ROOT / "plastic_promise" / "mcp" / "dashboard_v2" / "static" / "app.js"
    ).read_text(encoding="utf-8")

    assert combined.count('TZ: "UTC"') == len(compose_paths)
    assert 'timeZone: "UTC"' in dashboard
    for forbidden in ("timedatectl", "Set-TimeZone", "/etc/localtime", "/etc/timezone"):
        assert forbidden not in combined


def test_readmes_embed_compact_release_c4_and_localized_parseable_svg_infographics():
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    chinese_readme = (REPOSITORY_ROOT / "docs" / "README.zh-CN.md").read_text(encoding="utf-8")
    english_svg_path = REPOSITORY_ROOT / ".github" / "readme-release-delivery.svg"
    chinese_svg_path = REPOSITORY_ROOT / ".github" / "readme-release-delivery.zh-CN.svg"

    assert "### C4 release-delivery context" in readme
    assert "## Controlled release delivery (target / unverified)" in readme
    assert "not yet connected to the PR 6 selected Release Bundle/Model" in readme
    assert ".github/readme-release-delivery.svg" in readme
    assert "[![Release verification]" in readme
    assert "## 受控发行交付（目标/未验证）" in chinese_readme
    assert "../.github/readme-release-delivery.zh-CN.svg" in chinese_readme
    assert "目标/未验证的发行控制设计" in chinese_readme
    release_ascii = readme.split("### C4 release-delivery context", maxsplit=1)[1].split(
        "```", maxsplit=2
    )[1]
    assert all("\t" not in line and len(line) <= 100 for line in release_ascii.splitlines())
    for svg_path in (english_svg_path, chinese_svg_path):
        assert svg_path.stat().st_size < 100_000
        assert ElementTree.parse(svg_path).getroot().tag.endswith("svg")

    assert "NO AUTO-PUBLISH" in english_svg_path.read_text(encoding="utf-8")
    assert "不自动发布" in chinese_svg_path.read_text(encoding="utf-8")


def test_release_delivery_docs_keep_runtime_and_publication_authority_separate():
    architecture = (
        REPOSITORY_ROOT / "docs" / "architecture" / "release-delivery" / "architecture.md"
    ).read_text(encoding="utf-8")
    delivery = (REPOSITORY_ROOT / "docs" / "release" / "delivery.md").read_text(encoding="utf-8")
    workflow = (
        REPOSITORY_ROOT / "docs" / "architecture" / "release-delivery" / "workflow.mermaid"
    ).read_text(encoding="utf-8")
    chinese_architecture = (
        REPOSITORY_ROOT / "docs" / "architecture" / "release-delivery" / "architecture.zh-CN.md"
    ).read_text(encoding="utf-8")
    chinese_notes = (
        REPOSITORY_ROOT
        / "docs"
        / "architecture"
        / "release-delivery"
        / "implementation-notes.zh-CN.md"
    ).read_text(encoding="utf-8")
    chinese_workflow = (
        REPOSITORY_ROOT / "docs" / "architecture" / "release-delivery" / "workflow.zh-CN.mermaid"
    ).read_text(encoding="utf-8")

    assert "has no MCP-memory, task-queue, SQLite, or LanceDB write path" in architecture
    assert "RC artifacts" in delivery
    assert "release-candidate" in delivery
    assert "production-release" in delivery
    assert "TestPyPI" in delivery
    assert "stable-only" in delivery
    assert "plastic-promise-server" in delivery
    assert "plastic-promise-local-inference-node" in delivery
    assert "PR verification" in workflow
    assert "Protected default-branch SHA" in workflow
    assert "Release manifest" in workflow
    assert "not yet wired to current publisher" in workflow
    assert "release-publish.yml" in delivery
    assert "does **not** currently consume or verify" in delivery
    assert "artifact-sbom-receipts.json" in delivery
    assert "artifact-binding/v2" in delivery
    assert "当前 `release-publish.yml`" in chinese_architecture
    assert "当前 `.github/workflows/release-publish.yml`" in chinese_notes
    assert "artifact-sbom-receipts.json" in chinese_architecture
    assert "artifact-binding/v2" in chinese_notes
    assert "当前 publisher 尚未接线" in chinese_workflow
    assert "receipt set" in workflow
    assert "receipt set" in chinese_workflow
