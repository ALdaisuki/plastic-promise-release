"""Release-facing documentation contracts.

These tests deliberately check invariant coverage rather than requiring a
line-for-line translation. English and Chinese guides serve different reading
styles, but neither may omit a safety boundary that would change deployment or
release behavior.
"""

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _svg_shape_inventory(relative_path: str) -> tuple[str | None, Counter[str]]:
    root = ElementTree.parse(REPOSITORY_ROOT / relative_path).getroot()
    shapes = Counter(
        element.tag.rsplit("}", maxsplit=1)[-1]
        for element in root.iter()
        if element.tag.rsplit("}", maxsplit=1)[-1]
        in {"circle", "ellipse", "line", "path", "polygon", "polyline", "rect"}
    )
    return root.attrib.get("viewBox"), shapes


def _relative_markdown_links(relative_path: str) -> list[str]:
    links = re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", _read(relative_path))
    return [
        unquote(link.split("#", maxsplit=1)[0])
        for link in links
        if link
        and not link.startswith(("#", "http://", "https://", "mailto:"))
        and not link.startswith("<")
    ]


def test_release_facing_chinese_deployment_and_release_guides_are_present_and_linked():
    chinese_index = _read("docs/README.zh-CN.md")
    english_index = _read("README.md")

    for relative_path in (
        "docs/deployment/README.md",
        "docs/deployment/README.zh-CN.md",
        "docs/release/delivery.zh-CN.md",
        "docs/release/six-pr-readiness.zh-CN.md",
    ):
        assert (REPOSITORY_ROOT / relative_path).is_file()
        assert relative_path in english_index

    for relative_path in (
        "deployment/README.zh-CN.md",
        "release/delivery.zh-CN.md",
        "release/six-pr-readiness.zh-CN.md",
    ):
        assert relative_path in chinese_index


def test_deployment_guides_share_canonical_and_derived_state_invariants():
    english = "\n".join(
        _read(relative_path)
        for relative_path in (
            "docs/deployment/README.md",
            "docs/deployment/profiles.md",
            "docs/deployment/local-inference-node.md",
            "docs/deployment/resource-planning.md",
        )
    )
    chinese = "\n".join(
        _read(relative_path)
        for relative_path in (
            "docs/deployment/README.zh-CN.md",
            "docs/deployment/profiles.zh-CN.md",
            "docs/deployment/local-inference-node.zh-CN.md",
            "docs/deployment/resource-planning.zh-CN.md",
        )
    )

    for required in (
        "SQLite",
        "canonical",
        "LanceDB",
        "derived",
        "resource_budget",
        "loopback",
        "model",
        "revision",
        "dimension",
        "normalization",
        "metric",
        "tokenization",
        "pooling",
        "artifact",
        "golden",
        "transport",
    ):
        assert required.lower() in english.lower()

    for required in (
        "SQLite",
        "canonical",
        "LanceDB",
        "派生",
        "resource_budget",
        "loopback",
        "模型名",
        "revision",
        "维度",
        "归一化",
        "metric",
        "tokenization",
        "pooling",
        "工件哈希",
        "golden",
        "传输证据",
    ):
        assert required.lower() in chinese.lower()


def test_release_guides_share_evidence_and_production_promotion_boundaries():
    english = "\n".join(
        _read(relative_path)
        for relative_path in (
            "docs/release/delivery.md",
            "docs/release/six-pr-readiness.md",
        )
    )
    chinese = "\n".join(
        _read(relative_path)
        for relative_path in (
            "docs/release/delivery.zh-CN.md",
            "docs/release/six-pr-readiness.zh-CN.md",
        )
    )

    for required in ("RC", "TestPyPI", "GHCR", "PyPI", "SBOM", "OIDC", "SQLite", "LanceDB"):
        assert required in english
        assert required in chinese

    assert "protected" in english.lower()
    assert "受保护" in chinese
    assert "no SQLite" in english
    assert "不得包含 SQLite" in chinese


def test_release_guides_record_the_formal_build_deploy_publish_authority_split():
    english = _read("docs/release/delivery.md")
    chinese = _read("docs/release/delivery.zh-CN.md")
    diagram = _read("docs/architecture/diagrams/release-deployment-authority.txt")

    for required in (
        "Windows / WSL2",
        "GitHub protected workflows",
        "manifest-pinned",
        "MCP E2E receipt",
        "stable-only release repository",
    ):
        assert required in english
    for required in (
        "Windows / WSL2",
        "GitHub 受保护工作流",
        "只拉取",
        "MCP E2E 回执",
        "stable-only 发行仓库",
    ):
        assert required in chinese

    assert "derived embedding/rerank only" in diagram
    assert "immutable manifest digest" in diagram
    assert "bounded deployment receipt" in diagram
    for line in diagram.splitlines():
        assert "\t" not in line
        assert len(line) <= 100


def test_three_endpoint_docs_share_the_non_mutating_utc_runtime_policy():
    english = "\n".join(
        _read(relative_path)
        for relative_path in (
            "README.md",
            "docs/deployment/README.md",
            "docs/architecture/three-endpoint-deployment/architecture.md",
        )
    )
    chinese = "\n".join(
        _read(relative_path)
        for relative_path in (
            "docs/README.zh-CN.md",
            "docs/deployment/README.zh-CN.md",
            "docs/architecture/three-endpoint-deployment/architecture.zh-CN.md",
        )
    )

    for required in ("TZ=UTC", "host timezone", "canonical timestamps", "browser"):
        assert required.lower() in english.lower()
    for required in ("TZ=UTC", "宿主时区", "canonical timestamp", "浏览器"):
        assert required.lower() in chinese.lower()


def test_architecture_diagrams_describe_current_runtime_not_legacy_pi_roles():
    diagrams = "\n".join(
        _read(relative_path)
        for relative_path in (
            "docs/architecture/diagrams/c4-level1-context.txt",
            "docs/architecture/diagrams/c4-level2-container.txt",
            "docs/architecture/diagrams/c4-level3-component.txt",
            "docs/architecture/diagrams/architecture.mermaid",
            "docs/architecture/diagrams/sequence.mermaid",
            "docs/architecture/diagrams/components.mermaid",
        )
    )

    for forbidden in ("Pi Builder", "Pi Reviewer", "Pi Fixer", "memory_gc (weekly)"):
        assert forbidden not in diagrams
    for required in ("SQLite", "LanceDB", "outbox", "project", "inference", "canonical"):
        assert required.lower() in diagrams.lower()

    for relative_path in (
        "docs/architecture/diagrams/c4-level1-context.txt",
        "docs/architecture/diagrams/c4-level2-container.txt",
        "docs/architecture/diagrams/c4-level3-component.txt",
    ):
        for line in _read(relative_path).splitlines():
            assert "\t" not in line
            assert len(line) <= 100


def test_readme_has_parseable_current_architecture_infographic_and_compact_c4_map():
    readme = _read("README.md")
    chinese_readme = _read("docs/README.zh-CN.md")
    svg_path = REPOSITORY_ROOT / ".github" / "readme-runtime-architecture.svg"
    chinese_svg_path = REPOSITORY_ROOT / ".github" / "readme-runtime-architecture.zh-CN.svg"

    assert ".github/readme-runtime-architecture.svg" in readme
    assert "../.github/readme-runtime-architecture.zh-CN.svg" in chinese_readme
    assert "### C4 deployment view" in readme
    c4_map = readme.split("### C4 deployment view", maxsplit=1)[1].split("```", maxsplit=2)[1]
    assert all("\t" not in line and len(line) <= 100 for line in c4_map.splitlines())
    assert svg_path.stat().st_size < 100_000
    assert chinese_svg_path.stat().st_size < 100_000
    assert ElementTree.parse(svg_path).getroot().tag.endswith("svg")
    assert ElementTree.parse(chinese_svg_path).getroot().tag.endswith("svg")


def test_readme_badges_and_three_endpoint_navigation_have_bilingual_parity():
    english = _read("README.md")
    chinese = _read("docs/README.zh-CN.md")

    badge_pattern = re.compile(r"https://img\.shields\.io/[^)\s]+")
    assert set(badge_pattern.findall(english)) == set(badge_pattern.findall(chinese))
    assert "docs/architecture/three-endpoint-deployment/architecture.md" in english
    assert "architecture/three-endpoint-deployment/architecture.zh-CN.md" in chinese


def test_three_endpoint_readmes_do_not_present_target_profiles_as_current():
    english = _read("README.md")
    chinese = _read("docs/README.zh-CN.md")

    assert "Endpoint Contract V2\nand the PR 3 source-level artifact policy are current" in english
    assert "runtime deployment target" in english
    assert "PRs 4–6" in english
    assert "currently supports three deployment profiles" not in english
    assert "Endpoint Contract V2 现已把 `local-all-in-one`、`local-cloud` 与" in chinese
    assert "ContainerArtifactCompiler" in chinese
    assert "实际 image activation、部署" in chinese
    assert "标准发行版支持 `local-all-in-one`、`local-cloud` 与" not in chinese


def test_resource_planning_tables_and_cost_evidence_have_chinese_parity():
    english = _read("docs/deployment/resource-planning.md")
    chinese = _read("docs/deployment/resource-planning.zh-CN.md")

    assert "resource-planning.zh-CN.md" in english
    assert "resource-planning.md" in chinese
    for profile in (
        "local-all-in-one",
        "local-cloud",
        "split-accelerated",
    ):
        assert profile in english
        assert profile in chinese
    for requirement in (
        "max(20%, 10 GiB)",
        "50 GiB",
        "100 GiB",
        "160 GiB",
        "provider/catalog revision",
        "region",
        "currency",
    ):
        assert requirement in english
    for requirement in (
        "max(20%, 10 GiB)",
        "50 GiB",
        "100 GiB",
        "160 GiB",
        "provider/catalog revision",
        "region",
        "currency",
    ):
        assert requirement in chinese


def test_three_endpoint_architecture_contract_is_bilingual_and_machine_readable():
    english_architecture = _read("docs/architecture/three-endpoint-deployment/architecture.md")
    chinese_architecture = _read(
        "docs/architecture/three-endpoint-deployment/architecture.zh-CN.md"
    )
    english_notes = _read("docs/architecture/three-endpoint-deployment/implementation-notes.md")
    chinese_notes = _read(
        "docs/architecture/three-endpoint-deployment/implementation-notes.zh-CN.md"
    )

    shared_terms = (
        "pp-local-edge",
        "pp-server-backend",
        "pp-compute-node",
        "SQLite",
        "LanceDB",
        "Deployment Manifest",
        "Release Bundle",
        "Model Catalog",
        "local-all-in-one",
        "local-cloud",
        "split-accelerated",
        "routing-core",
        "endpoint-contracts",
        "container-artifacts",
        "deployment-center",
        "migration-operations",
        "release-readiness",
    )
    for term in shared_terms:
        assert term in english_architecture
        assert term in chinese_architecture

    for pr_number in range(1, 7):
        assert f"PR {pr_number}" in english_notes
        assert f"PR {pr_number}" in chinese_notes

    config = json.loads(_read("docs/architecture/three-endpoint-deployment/config/mcp-config.json"))
    assert config["schema_version"] == "plastic-promise/mcp-client-config/v1"
    assert config["status"] == "target-architecture-example"
    assert set(config["endpoints"]) == {"localEdge", "serverBackend", "computeNode"}
    assert config["mcpServers"]["plastic-promise"]["canonicalWriter"] == ("pp-server-backend")


def test_three_endpoint_diagrams_and_existing_svg_pairs_do_not_drift():
    ascii_diagram = _read("docs/architecture/three-endpoint-deployment/diagrams/architecture.txt")
    assert all("\t" not in line and len(line) <= 100 for line in ascii_diagram.splitlines())
    for endpoint in ("pp-local-edge", "pp-server-backend", "pp-compute-node"):
        assert endpoint in ascii_diagram

    for relative_path in (
        "docs/architecture/three-endpoint-deployment/diagrams/sequence.mermaid",
        "docs/architecture/three-endpoint-deployment/diagrams/workflow.mermaid",
    ):
        diagram = _read(relative_path)
        for endpoint in ("pp-local-edge", "pp-server-backend", "pp-compute-node"):
            assert endpoint in diagram

    for english_path, chinese_path in (
        (
            "docs/architecture/plastic-promise-flow.svg",
            "docs/architecture/plastic-promise-flow.zh-CN.svg",
        ),
        (
            "docs/architecture/distribution-profiles.svg",
            "docs/architecture/distribution-profiles.zh-CN.svg",
        ),
    ):
        assert _svg_shape_inventory(english_path) == _svg_shape_inventory(chinese_path)

    english_profiles = _read("docs/architecture/distribution-profiles.svg")
    chinese_profiles = _read("docs/architecture/distribution-profiles.zh-CN.svg")
    assert "UNION CONTRACT 2026-08-18.1 · RUNTIME TARGET PRS 3–6" in english_profiles
    assert "联合合同 2026-08-18.1 · 运行时目标 PR 3–6" in chinese_profiles
    assert "SUPPORTED PROFILES" not in english_profiles
    assert "支持的部署 PROFILE" not in chinese_profiles


def test_endpoint_contract_docs_keep_v2_current_and_runtime_operations_target():
    english = "\n".join(
        _read(relative_path)
        for relative_path in (
            "README.md",
            "docs/architecture/three-endpoint-deployment/architecture.md",
            "docs/deployment/README.md",
            "docs/deployment/profiles.md",
            "docs/deployment/local-inference-node.md",
            "docs/roadmap/composable-deployment.md",
        )
    )
    chinese = "\n".join(
        _read(relative_path)
        for relative_path in (
            "docs/README.zh-CN.md",
            "docs/architecture/three-endpoint-deployment/architecture.zh-CN.md",
            "docs/deployment/README.zh-CN.md",
            "docs/deployment/profiles.zh-CN.md",
            "docs/deployment/local-inference-node.zh-CN.md",
            "docs/roadmap/composable-deployment.zh-CN.md",
        )
    )

    for required in (
        "Endpoint Contract V2",
        "legacy",
        "pp-server-backend",
        "single-writer",
        "manifest",
        "admission",
        "fencing",
        "CapabilityBinding",
        "ContainerArtifactCompiler",
        "target",
        "PR 3",
        "PR 6",
    ):
        assert required.lower() in english.lower()
    assert "ComputeFence" in english
    assert "structured JSON as a first-class `pp-compute-node`" in english
    assert "structured JSON is off by default" in english
    for required in (
        "Endpoint Contract V2",
        "兼容",
        "pp-server-backend",
        "单写者",
        "manifest",
        "准入",
        "fencing",
        "CapabilityBinding",
        "ContainerArtifactCompiler",
        "目标",
        "PR 3",
        "PR 6",
    ):
        assert required.lower() in chinese.lower()
    assert "ComputeFence" in chinese
    assert "structured JSON 与 embedding、rerank 一样作为 `pp-compute-node`" in chinese
    assert "structured JSON 默认关闭" in chinese

    workflow = _read("docs/architecture/three-endpoint-deployment/diagrams/workflow.mermaid")
    for target_label in (
        "C/T: Host ppctl adapter",
        "T: Live apply",
        "T: Backup and migration",
        "C: durable secret-free migration receipt",
        "C: Validate identity, capability binding, lease, current fence<br/>T: runtime apply",
    ):
        assert target_label in workflow

    for relative_path in (
        "docs/deployment/profiles.md",
        "docs/deployment/profiles.zh-CN.md",
        "docs/deployment/local-inference-node.md",
        "docs/deployment/local-inference-node.zh-CN.md",
        "docs/roadmap/composable-deployment.md",
        "docs/roadmap/composable-deployment.zh-CN.md",
    ):
        assert (REPOSITORY_ROOT / relative_path).is_file()


def test_container_artifact_docs_keep_build_evidence_separate_from_runtime_and_release():
    english_path = "docs/architecture/three-endpoint-deployment/container-artifacts.md"
    chinese_path = "docs/architecture/three-endpoint-deployment/container-artifacts.zh-CN.md"
    english = _read(english_path)
    chinese = _read(chinese_path)

    for required in (
        "ContainerArtifactCompiler",
        "ArtifactBuildExecutor",
        "ArtifactBuildPlan",
        "ArtifactBundle",
        "pp-local-edge",
        "pp-server-backend",
        "pp-compute-node",
        "CPU",
        "CUDA",
        "no-push",
        "SQLite",
        "LanceDB",
        "production",
    ):
        assert required.lower() in english.lower()
    for required in (
        "ContainerArtifactCompiler",
        "ArtifactBuildExecutor",
        "ArtifactBuildPlan",
        "ArtifactBundle",
        "pp-local-edge",
        "pp-server-backend",
        "pp-compute-node",
        "CPU",
        "CUDA",
        "不 push",
        "SQLite",
        "LanceDB",
        "production",
    ):
        assert required.lower() in chinese.lower()

    for receipt_path, provenance_rule in (
        (
            "docs/architecture/three-endpoint-deployment/documentation-parity.md",
            "Historical local receipts are provenance only.",
        ),
        (
            "docs/architecture/three-endpoint-deployment/documentation-parity.zh-CN.md",
            "历史本地 receipt 只作为来源记录",
        ),
    ):
        parity_standard = _read(receipt_path)
        assert provenance_rule in parity_standard
        assert "documentation-parity-receipt/v1" in parity_standard
        assert "union-six-pr-contract.json" in parity_standard

    todo = _read("docs/TODO List/README.md")
    assert "ContainerArtifactCompiler" in todo
    assert "protected no-push OCI build-verification" in todo

    assert _svg_shape_inventory(
        "docs/architecture/three-endpoint-deployment/diagrams/container-artifact-matrix.svg"
    ) == _svg_shape_inventory(
        "docs/architecture/three-endpoint-deployment/diagrams/container-artifact-matrix.zh-CN.svg"
    )


def test_current_todo_roadmap_has_a_chinese_peer_and_localized_entry_points():
    english_path = "docs/TODO List/README.md"
    chinese_path = "docs/TODO List/README.zh-CN.md"
    english = _read(english_path)
    chinese = _read(chinese_path)

    assert (REPOSITORY_ROOT / chinese_path).is_file()
    for item_id in ("R1", "R2", "D1", "D2", *(f"R{number}" for number in range(3, 23))):
        assert f"| {item_id} |" in english
        assert f"| {item_id} |" in chinese

    for required in (
        "PR 1",
        "PR 2",
        "PR 3",
        "PR 6",
        "pp-server-backend",
        "SQLite",
        "LanceDB",
        "pp-local-edge",
        "current",
        "target",
        "production migration",
        "stable release",
    ):
        assert required.lower() in english.lower()
    for required in (
        "PR 1",
        "PR 2",
        "PR 3",
        "PR 6",
        "pp-server-backend",
        "SQLite",
        "LanceDB",
        "pp-local-edge",
        "当前",
        "目标态",
        "生产迁移",
        "稳定发行",
    ):
        assert required.lower() in chinese.lower()

    for filename in (
        "01-comparison-analysis.md",
        "02-retrieval-enhancement.md",
        "03-smart-extraction-upgrade.md",
        "04-infrastructure-gaps.md",
        "05-integration-roadmap.md",
        "06-rust-engine-gaps.md",
        "07-causal-world-model-roadmap.md",
    ):
        assert f"]({filename})" in english
        assert f"]({filename})" in chinese

    localized_link = "TODO%20List/README.zh-CN.md"
    for relative_path in ("docs/README.zh-CN.md", "docs/GOAL.md"):
        document = _read(relative_path)
        assert localized_link in document
        assert "TODO%20List/README.md" not in document

    for relative_path in (
        "docs/architecture/three-endpoint-deployment/documentation-parity.md",
        "docs/architecture/three-endpoint-deployment/documentation-parity.zh-CN.md",
    ):
        parity_standard = _read(relative_path)
        assert "documentation-parity-receipt/v1" in parity_standard
        assert '"intentional_differences": []' in parity_standard
        assert "english_files:" not in parity_standard
        assert "chinese_files:" not in parity_standard

    english_receipt = _read("docs/architecture/three-endpoint-deployment/documentation-parity.md")
    chinese_receipt = _read(
        "docs/architecture/three-endpoint-deployment/documentation-parity.zh-CN.md"
    )
    assert "derived-document manifest" in english_receipt
    assert "派生文档 manifest" in chinese_receipt
    assert "does not self-issue a `pass`" in english_receipt
    assert "不会自行签发 `pass`" in chinese_receipt


def test_three_endpoint_document_relative_links_resolve():
    for relative_path in (
        "docs/architecture/three-endpoint-deployment/architecture.md",
        "docs/architecture/three-endpoint-deployment/architecture.zh-CN.md",
        "docs/architecture/three-endpoint-deployment/documentation-parity.md",
        "docs/architecture/three-endpoint-deployment/container-artifacts.md",
        "docs/architecture/three-endpoint-deployment/container-artifacts.zh-CN.md",
        "docs/architecture/three-endpoint-deployment/implementation-notes.md",
        "docs/architecture/three-endpoint-deployment/implementation-notes.zh-CN.md",
        "docs/deployment/profiles.md",
        "docs/deployment/profiles.zh-CN.md",
        "docs/deployment/local-inference-node.md",
        "docs/deployment/local-inference-node.zh-CN.md",
        "docs/roadmap/composable-deployment.md",
        "docs/roadmap/composable-deployment.zh-CN.md",
    ):
        parent = (REPOSITORY_ROOT / relative_path).parent
        for link in _relative_markdown_links(relative_path):
            assert (parent / link).exists(), f"broken link in {relative_path}: {link}"
