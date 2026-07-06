import importlib.util
from pathlib import Path


def _load_release_sync():
    path = Path("scripts/release-sync.py")
    spec = importlib.util.spec_from_file_location("release_sync", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_sync_includes_project_codex_config():
    release_sync = _load_release_sync()

    assert release_sync.is_included(".codex/config.toml")


def test_release_sync_keeps_internal_superpowers_docs_excluded():
    release_sync = _load_release_sync()

    included, excluded = release_sync.filter_files(
        [
            ".codex/config.toml",
            "docs/superpowers/plans/2026-07-05-sp-stage-guidance.md",
        ]
    )

    assert included == [".codex/config.toml"]
    assert excluded == ["docs/superpowers/plans/2026-07-05-sp-stage-guidance.md"]


def test_release_sync_does_not_duplicate_existing_changelog_version(tmp_path):
    release_sync = _load_release_sync()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "## [0.1.1] - 2026-07-06\n\n"
        "### Fixed\n"
        "- Existing release note.\n",
        encoding="utf-8",
    )

    transformed = release_sync.apply_transform("CHANGELOG.md", "v0.1.1", tmp_path)

    assert transformed is not None
    assert transformed.count("## [0.1.1]") == 1
    assert "## [v0.1.1]" not in transformed
    assert "Existing release note." in transformed
