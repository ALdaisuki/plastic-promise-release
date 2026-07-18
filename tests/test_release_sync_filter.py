import importlib.util
import subprocess
from pathlib import Path

import pytest


def _load_release_sync():
    path = Path("scripts/release-sync.py")
    spec = importlib.util.spec_from_file_location("release_sync", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _release_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo = tmp_path / "release"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


def _source_repo(tmp_path: Path, *, branch: str = "main") -> tuple[Path, Path]:
    remote = tmp_path / "source-remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Source Test")
    _git(repo, "config", "user.email", "source-test@example.invalid")
    (repo / "README.md").write_text("committed source\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "source seed")
    _git(repo, "branch", "-M", branch)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", branch)
    return repo, remote


def _commit_readme(repo: Path, content: str, message: str) -> None:
    (repo / "README.md").write_text(content, encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", message)


def _advance_remote(remote: Path, destination: Path, content: str) -> None:
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(destination, "config", "user.name", "Remote Test")
    _git(destination, "config", "user.email", "remote-test@example.invalid")
    _commit_readme(destination, content, "remote advance")
    _git(destination, "push", "origin", "main")


def test_release_sync_includes_project_codex_config():
    release_sync = _load_release_sync()

    assert release_sync.is_included(".codex/config.toml")


def test_release_push_is_explicit_opt_in_and_dry_run_default_is_unchanged():
    release_sync = _load_release_sync()

    args = release_sync.build_argparser().parse_args(
        ["--from", "HEAD", "--version", "v0.1.15", "--dry-run"]
    )

    assert args.dry_run is True
    assert args.push is False


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


def test_release_sync_includes_authoritative_retrieval_plan_exception():
    release_sync = _load_release_sync()

    included, excluded = release_sync.filter_files(
        [
            "docs/superpowers/plans/2026-07-12-corrective-governed-retrieval-plan.md",
            "docs/superpowers/specs/2026-07-18-rag-shadow-chunking-benchmark-design.md",
        ]
    )

    assert included == ["docs/superpowers/plans/2026-07-12-corrective-governed-retrieval-plan.md"]
    assert excluded == ["docs/superpowers/specs/2026-07-18-rag-shadow-chunking-benchmark-design.md"]


def test_release_sync_includes_engineering_pattern_records():
    release_sync = _load_release_sync()

    included, excluded = release_sync.filter_files(
        ["docs/engineering-patterns/2026-07-12-ordinary-memory-caller-inventory.md"]
    )

    assert included == [
        "docs/engineering-patterns/2026-07-12-ordinary-memory-caller-inventory.md"
    ]
    assert excluded == []


def test_release_sync_normalizes_https_and_ssh_github_origins():
    release_sync = _load_release_sync()

    expected = "github.com/aldaisuki/plastic-promise-release"
    assert (
        release_sync._normalized_origin("https://github.com/ALdaisuki/plastic-promise-release.git")
        == expected
    )
    assert (
        release_sync._normalized_origin("git@github.com:ALdaisuki/plastic-promise-release.git")
        == expected
    )


def test_release_sync_rejects_pyproject_without_version_assignment(tmp_path):
    release_sync = _load_release_sync()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="release_package_version_not_promoted"):
        release_sync.apply_transform("pyproject.toml", "v0.1.15", tmp_path)


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


def test_release_sync_promotes_prepared_draft_without_replacing_notes(tmp_path):
    release_sync = _load_release_sync()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "Prepared release target: `0.1.15` (Draft/BLOCK).\n\n"
        "## [0.1.15] - Draft (unreleased)\n\n"
        "### Fixed\n"
        "- Prepared release note.\n\n"
        "### Verification\n\n"
        "- Overall release status is **Draft/BLOCK**. Final verification remains pending.\n"
        "  Earlier slice results are not a whole-branch release approval.\n",
        encoding="utf-8",
    )

    transformed = release_sync.apply_transform("CHANGELOG.md", "v0.1.15", tmp_path)

    assert transformed is not None
    assert transformed.count("## [0.1.15]") == 1
    assert "Draft (unreleased)" not in transformed
    assert "Draft/BLOCK" not in transformed
    assert "Released version: `0.1.15`." in transformed
    assert "**audited and approved**" in transformed
    assert "Release-specific benchmark and runtime evidence" in transformed
    assert "Prepared release note." in transformed


def test_release_sync_ignores_draft_status_in_older_changelog_sections(tmp_path):
    release_sync = _load_release_sync()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "Prepared release target: `0.1.17` (Draft/BLOCK).\n\n"
        "## [0.1.17] - Draft (unreleased)\n\n"
        "### Verification\n\n"
        "- Overall release status is **Draft/BLOCK**. Final verification remains pending.\n\n"
        "## [0.1.16] - Draft (unreleased)\n\n"
        "### Verification\n\n"
        "- Overall release status is **Draft/BLOCK**. Older release remains pending.\n",
        encoding="utf-8",
    )

    transformed = release_sync.apply_transform("CHANGELOG.md", "v0.1.17", tmp_path)

    assert transformed is not None
    assert "## [0.1.17] - Draft (unreleased)" not in transformed
    assert "## [0.1.16] - Draft (unreleased)" in transformed
    assert "Older release remains pending." in transformed


def test_release_sync_promotes_prepared_system_header(tmp_path):
    release_sync = _load_release_sync()
    system = tmp_path / "docs" / "SYSTEM_FULL_CHAIN.md"
    system.parent.mkdir(parents=True)
    system.write_text(
        "# System\n\n> Prepared release target: 0.1.15 | Draft/BLOCK | 2026-07-13\n",
        encoding="utf-8",
    )

    transformed = release_sync.apply_transform("docs/SYSTEM_FULL_CHAIN.md", "v0.1.15", tmp_path)

    assert transformed is not None
    assert "> 版本: 0.1.15 | 日期:" in transformed
    assert "Draft/BLOCK" not in transformed


def test_release_sync_promotes_goal_release_status_without_changing_dev_file(tmp_path):
    release_sync = _load_release_sync()
    goal = tmp_path / "docs" / "GOAL.md"
    goal.parent.mkdir(parents=True)
    original = (
        "# Goal\n\n"
        "## 2026-07-12 Canonical Mutation and Release Note\n\n"
        "- Release version `0.1.15` follows the active package line.\n"
        "- Verification status is **Draft/BLOCK**. Final verification remains pending.\n"
    )
    goal.write_text(original, encoding="utf-8")

    transformed = release_sync.apply_transform("docs/GOAL.md", "v0.1.15", tmp_path)

    assert transformed is not None
    assert "Draft/BLOCK" not in transformed
    assert "Release verification for `0.1.15` is **audited and approved**" in transformed
    assert "Release-specific benchmark and runtime evidence" in transformed
    assert goal.read_text(encoding="utf-8") == original


def test_release_sync_rejects_formal_changelog_heading_with_draft_status(tmp_path):
    release_sync = _load_release_sync()
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.1.15] - 2026-07-13\n\n- Overall release status is **Draft/BLOCK**.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="release_status_not_promoted"):
        release_sync.apply_transform("CHANGELOG.md", "v0.1.15", tmp_path)


def test_release_sync_rejects_goal_without_version_marker(tmp_path):
    release_sync = _load_release_sync()
    goal = tmp_path / "docs" / "GOAL.md"
    goal.parent.mkdir(parents=True)
    goal.write_text(
        "# Goal\n\n- Verification status is **Draft/BLOCK**.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="goal_release_marker_missing"):
        release_sync.apply_transform("docs/GOAL.md", "v0.1.15", tmp_path)


def test_release_sync_rejects_unrecognized_system_header(tmp_path):
    release_sync = _load_release_sync()
    system = tmp_path / "docs" / "SYSTEM_FULL_CHAIN.md"
    system.parent.mkdir(parents=True)
    system.write_text("# System\n\n> Draft/BLOCK\n", encoding="utf-8")

    with pytest.raises(ValueError, match="system_release_header_missing"):
        release_sync.apply_transform("docs/SYSTEM_FULL_CHAIN.md", "v0.1.15", tmp_path)


def test_release_preflight_accepts_clean_main_with_absent_tag(tmp_path):
    release_sync = _load_release_sync()
    repo, remote = _release_repo(tmp_path)

    result = release_sync.validate_release_preflight(repo, "v0.1.15", str(remote))

    assert result["branch"] == "main"
    assert result["version"] == "v0.1.15"


def test_source_preflight_binds_clean_exact_repo_branch_origin_head_and_ranges(tmp_path):
    release_sync = _load_release_sync()
    repo, remote = _source_repo(tmp_path, branch="codex/governed-synthesis-retrieval")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = release_sync.validate_source_preflight(
        repo,
        expected_branch="codex/governed-synthesis-retrieval",
        expected_origin=str(remote),
        revision_ranges=["HEAD", f"{head}..HEAD"],
    )

    assert result["head"] == head
    assert result["remote_head"] == head
    assert result["branch"] == "codex/governed-synthesis-retrieval"
    assert result["ranges"] == {"HEAD": head, f"{head}..HEAD": head}


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("dirty", "source_repo_not_clean"),
        ("branch", "source_branch_mismatch"),
        ("origin", "source_origin_mismatch"),
        ("root", "source_repo_root_mismatch"),
        ("range", "source_range_not_head"),
    ],
)
def test_source_preflight_rejects_unbound_development_state(tmp_path, mutation, error):
    release_sync = _load_release_sync()
    repo, remote = _source_repo(tmp_path)
    expected_origin = str(remote)
    source_root = repo
    ranges = ["HEAD"]
    if mutation == "dirty":
        (repo / "README.md").write_text("uncommitted source\n", encoding="utf-8")
    elif mutation == "branch":
        _git(repo, "switch", "-c", "foreign")
    elif mutation == "origin":
        expected_origin = str(tmp_path / "foreign.git")
    elif mutation == "root":
        source_root = repo / "nested"
        source_root.mkdir()
    elif mutation == "range":
        (repo / "README.md").write_text("second commit\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "second")
        _git(repo, "push", "origin", "main")
        ranges = ["HEAD~1"]

    with pytest.raises(ValueError, match=error):
        release_sync.validate_source_preflight(
            source_root,
            expected_branch="main",
            expected_origin=expected_origin,
            revision_ranges=ranges,
        )


def test_live_copy_reads_bound_commit_instead_of_uncommitted_worktree(monkeypatch, tmp_path):
    release_sync = _load_release_sync()
    source, _remote = _source_repo(tmp_path)
    head = _git(source, "rev-parse", "HEAD").stdout.strip()
    (source / "README.md").write_text("uncommitted source\n", encoding="utf-8")
    destination = tmp_path / "release-output"
    destination.mkdir()
    monkeypatch.setattr(release_sync, "PROJECT_ROOT", source)

    copied = release_sync.apply_to_release(
        ["README.md"],
        "v0.1.15",
        destination,
        source_commit=head,
    )

    assert copied == ["README.md"]
    assert (destination / "README.md").read_text(encoding="utf-8") == "committed source\n"


@pytest.mark.parametrize("repo_kind", ["source", "release"])
@pytest.mark.parametrize("drift", ["ahead", "behind", "diverged"])
def test_preflight_rejects_local_remote_branch_drift(tmp_path, repo_kind, drift):
    release_sync = _load_release_sync()
    if repo_kind == "source":
        repo, remote = _source_repo(tmp_path)
    else:
        repo, remote = _release_repo(tmp_path)

    if drift in {"ahead", "diverged"}:
        _commit_readme(repo, f"local {drift}\n", "local advance")
    if drift in {"behind", "diverged"}:
        _advance_remote(remote, tmp_path / f"peer-{repo_kind}-{drift}", f"remote {drift}\n")

    error = f"{repo_kind}_remote_head_mismatch"
    with pytest.raises(ValueError, match=error):
        if repo_kind == "source":
            release_sync.validate_source_preflight(
                repo,
                expected_branch="main",
                expected_origin=str(remote),
                revision_ranges=["HEAD"],
            )
        else:
            release_sync.validate_release_preflight(repo, "v0.1.15", str(remote))


def test_post_validation_audit_rejects_rewritten_copied_path(monkeypatch, tmp_path):
    release_sync = _load_release_sync()
    source, _remote = _source_repo(tmp_path)
    head = _git(source, "rev-parse", "HEAD").stdout.strip()
    destination = tmp_path / "release-output"
    destination.mkdir()
    (destination / "README.md").write_bytes(b"committed source\n")
    monkeypatch.setattr(release_sync, "PROJECT_ROOT", source)

    assert release_sync.audit_release_tree(
        ["README.md"], "v0.1.15", destination, source_commit=head
    )
    (destination / "README.md").write_text("validator rewrite\n", encoding="utf-8")
    assert not release_sync.audit_release_tree(
        ["README.md"], "v0.1.15", destination, source_commit=head
    )


def test_release_staging_rejects_index_bytes_different_from_bound_source(tmp_path):
    release_sync = _load_release_sync()
    repo, _remote = _release_repo(tmp_path)
    (repo / "README.md").write_text("validator rewrite\n", encoding="utf-8")

    with pytest.raises(ValueError, match="release_index_content_mismatch:README.md"):
        release_sync.stage_release_paths(
            repo,
            ["README.md"],
            expected_index_bytes={"README.md": b"committed source\n"},
        )


def test_release_staging_accepts_index_bytes_equal_to_bound_source(tmp_path):
    release_sync = _load_release_sync()
    repo, _remote = _release_repo(tmp_path)
    (repo / "README.md").write_bytes(b"release source\n")

    staged = release_sync.stage_release_paths(
        repo,
        ["README.md"],
        expected_index_bytes={"README.md": b"release source\n"},
    )

    assert staged == ["README.md"]


def test_release_dry_run_never_creates_target_directories(monkeypatch, tmp_path):
    release_sync = _load_release_sync()
    source = tmp_path / "preview-source"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts" / "preview.py").write_text("print('preview')\n", encoding="utf-8")
    destination = tmp_path / "missing-release"
    monkeypatch.setattr(release_sync, "PROJECT_ROOT", source)

    copied = release_sync.apply_to_release(
        ["scripts/preview.py"],
        "v0.1.15",
        destination,
        dry_run=True,
    )

    assert copied == ["scripts/preview.py"]
    assert not destination.exists()


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("dirty", "release_repo_not_clean"),
        ("branch", "release_repo_not_main"),
        ("origin", "release_origin_mismatch"),
        ("local_tag", "release_tag_exists_local"),
        ("remote_tag", "release_tag_exists_remote"),
    ],
)
def test_release_preflight_rejects_unsafe_repository_state(tmp_path, mutation, error):
    release_sync = _load_release_sync()
    repo, remote = _release_repo(tmp_path)
    expected_origin = str(remote)
    if mutation == "dirty":
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    elif mutation == "branch":
        _git(repo, "switch", "-c", "release-candidate")
    elif mutation == "origin":
        expected_origin = str(tmp_path / "wrong.git")
    elif mutation == "local_tag":
        _git(repo, "tag", "v0.1.15")
    elif mutation == "remote_tag":
        _git(repo, "tag", "v0.1.15")
        _git(repo, "push", "origin", "refs/tags/v0.1.15")
        _git(repo, "tag", "-d", "v0.1.15")

    with pytest.raises(ValueError, match=error):
        release_sync.validate_release_preflight(repo, "v0.1.15", expected_origin)


def test_release_staging_rejects_noncomputed_validation_side_effect(tmp_path):
    release_sync = _load_release_sync()
    repo, _remote = _release_repo(tmp_path)
    (repo / "README.md").write_text("release\n", encoding="utf-8")
    (repo / "unexpected.txt").write_text("side effect\n", encoding="utf-8")

    with pytest.raises(ValueError, match="release_unexpected_worktree_changes"):
        release_sync.stage_release_paths(repo, ["README.md"])


def test_release_tag_must_resolve_to_current_head(tmp_path):
    release_sync = _load_release_sync()
    repo, _remote = _release_repo(tmp_path)
    _git(repo, "tag", "-a", "v0.1.15", "-m", "release")
    assert (
        release_sync.verify_tag_target(repo, "v0.1.15")
        == _git(repo, "rev-parse", "HEAD").stdout.strip()
    )

    (repo / "README.md").write_text("next\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "next")
    with pytest.raises(ValueError, match="release_tag_target_mismatch"):
        release_sync.verify_tag_target(repo, "v0.1.15")


def test_release_commit_precondition_rejects_index_drift_after_staging(tmp_path):
    release_sync = _load_release_sync()
    repo, remote = _release_repo(tmp_path)
    base_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected = {"README.md": b"release source\n"}
    (repo / "README.md").write_bytes(expected["README.md"])
    staged = release_sync.stage_release_paths(
        repo,
        ["README.md"],
        expected_index_bytes=expected,
    )

    (repo / "README.md").write_text("hook rewrite\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    with pytest.raises(ValueError, match="release_index_content_mismatch:README.md"):
        release_sync.validate_release_commit_precondition(
            repo,
            version="v0.1.15",
            expected_origin=str(remote),
            base_head=base_head,
            staged_paths=staged,
            expected_index_bytes=expected,
        )


def test_release_commit_attestation_rejects_hook_rewritten_commit_tree(tmp_path):
    release_sync = _load_release_sync()
    repo, remote = _release_repo(tmp_path)
    base_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected = {"README.md": b"release source\n"}
    (repo / "README.md").write_bytes(expected["README.md"])
    staged = release_sync.stage_release_paths(
        repo,
        ["README.md"],
        expected_index_bytes=expected,
    )
    expected_tree_oid = release_sync.validate_release_commit_precondition(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        staged_paths=staged,
        expected_index_bytes=expected,
    )

    (repo / "README.md").write_text("hook rewrite\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "hook-rewritten release")

    with pytest.raises(ValueError, match="release_commit_tree_mismatch:README.md"):
        release_sync.validate_release_commit_attestation(
            repo,
            version="v0.1.15",
            expected_origin=str(remote),
            base_head=base_head,
            committed_paths=staged,
            expected_tree_bytes=expected,
            expected_tree_oid=expected_tree_oid,
        )


def test_release_commit_attestation_accepts_exact_tree_and_semantics(tmp_path):
    release_sync = _load_release_sync()
    repo, remote = _release_repo(tmp_path)
    base_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected = {"README.md": b"release source\n"}
    (repo / "README.md").write_bytes(expected["README.md"])
    staged = release_sync.stage_release_paths(
        repo,
        ["README.md"],
        expected_index_bytes=expected,
    )
    expected_tree_oid = release_sync.validate_release_commit_precondition(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        staged_paths=staged,
        expected_index_bytes=expected,
    )
    _git(repo, "commit", "-m", "exact release")

    commit = release_sync.validate_release_commit_attestation(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        committed_paths=staged,
        expected_tree_bytes=expected,
        expected_tree_oid=expected_tree_oid,
    )

    assert commit == _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_release_post_tag_verification_rejects_remote_tag_race(tmp_path):
    release_sync = _load_release_sync()
    repo, remote = _release_repo(tmp_path)
    base_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected = {"README.md": b"release source\n"}
    (repo / "README.md").write_bytes(expected["README.md"])
    staged = release_sync.stage_release_paths(repo, ["README.md"], expected_index_bytes=expected)
    expected_tree_oid = release_sync.validate_release_commit_precondition(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        staged_paths=staged,
        expected_index_bytes=expected,
    )
    _git(repo, "commit", "-m", "release")
    release_commit = release_sync.validate_release_commit_attestation(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        committed_paths=staged,
        expected_tree_bytes=expected,
        expected_tree_oid=expected_tree_oid,
    )
    _git(repo, "tag", "-a", "v0.1.15", release_commit, "-m", "release")
    _git(remote, "tag", "v0.1.15", base_head)

    with pytest.raises(ValueError, match="release_tag_exists_remote"):
        release_sync.verify_tag_target(
            repo,
            "v0.1.15",
            expected_commit=release_commit,
            expected_origin=str(remote),
            base_head=base_head,
        )


def test_release_atomic_push_uses_expected_url_and_attested_object_ids(monkeypatch, tmp_path):
    release_sync = _load_release_sync()
    repo, remote = _release_repo(tmp_path)
    base_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected = {"README.md": b"release source\n"}
    (repo / "README.md").write_bytes(expected["README.md"])
    staged = release_sync.stage_release_paths(repo, ["README.md"], expected_index_bytes=expected)
    expected_tree_oid = release_sync.validate_release_commit_precondition(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        staged_paths=staged,
        expected_index_bytes=expected,
    )
    _git(repo, "commit", "-m", "release")
    release_commit = release_sync.validate_release_commit_attestation(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        committed_paths=staged,
        expected_tree_bytes=expected,
        expected_tree_oid=expected_tree_oid,
    )
    _git(repo, "tag", "-a", "v0.1.15", release_commit, "-m", "release")
    tag_object_oid = release_sync.release_tag_object_oid(repo, "v0.1.15")

    push_calls = []
    original_probe = release_sync._git_probe

    def recording_probe(args, cwd):
        if args and args[0] == "push":
            push_calls.append(list(args))
        return original_probe(args, cwd)

    monkeypatch.setattr(release_sync, "_git_probe", recording_probe)
    release_sync.push_attested_release(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        release_commit=release_commit,
        expected_tag_object_oid=tag_object_oid,
        committed_paths=staged,
        expected_tree_bytes=expected,
        expected_tree_oid=expected_tree_oid,
    )

    assert push_calls == [
        [
            "push",
            "--atomic",
            f"--force-with-lease=refs/heads/main:{base_head}",
            str(remote),
            f"{release_commit}:refs/heads/main",
            f"{tag_object_oid}:refs/tags/v0.1.15",
        ]
    ]
    assert (
        release_sync._remote_ref_oid(repo, "refs/heads/main", remote=str(remote)) == release_commit
    )
    assert (
        release_sync._remote_ref_oid(repo, "refs/tags/v0.1.15", remote=str(remote))
        == tag_object_oid
    )


def test_release_atomic_push_rejects_replaced_annotated_tag_object(tmp_path):
    release_sync = _load_release_sync()
    repo, remote = _release_repo(tmp_path)
    base_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected = {"README.md": b"release source\n"}
    (repo / "README.md").write_bytes(expected["README.md"])
    staged = release_sync.stage_release_paths(repo, ["README.md"], expected_index_bytes=expected)
    expected_tree_oid = release_sync.validate_release_commit_precondition(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        staged_paths=staged,
        expected_index_bytes=expected,
    )
    _git(repo, "commit", "-m", "release")
    release_commit = release_sync.validate_release_commit_attestation(
        repo,
        version="v0.1.15",
        expected_origin=str(remote),
        base_head=base_head,
        committed_paths=staged,
        expected_tree_bytes=expected,
        expected_tree_oid=expected_tree_oid,
    )
    _git(repo, "tag", "-a", "v0.1.15", release_commit, "-m", "release")
    original_tag_object = release_sync.release_tag_object_oid(repo, "v0.1.15")
    _git(repo, "tag", "-f", "-a", "v0.1.15", release_commit, "-m", "replaced release")

    with pytest.raises(ValueError, match="release_prepush_tag_object_mismatch"):
        release_sync.push_attested_release(
            repo,
            version="v0.1.15",
            expected_origin=str(remote),
            base_head=base_head,
            release_commit=release_commit,
            expected_tag_object_oid=original_tag_object,
            committed_paths=staged,
            expected_tree_bytes=expected,
            expected_tree_oid=expected_tree_oid,
        )
    assert release_sync._remote_ref_oid(repo, "refs/heads/main") == base_head
