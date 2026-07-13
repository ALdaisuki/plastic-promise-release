#!/usr/bin/env python3
"""Release sync: filter and squash dev changes into the release repository.

Usage:
  python scripts/release-sync.py \
    --from HEAD~5..HEAD \
    --version v0.2.0 \
    --release-repo F:/Agent/plastic-promise-release \
    --dry-run

  python scripts/release-sync.py \
    --from HEAD~5..HEAD \
    --version v0.2.0 \
    --release-repo F:/Agent/plastic-promise-release \
    --push
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Project root detection ──────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Default release repo path ───────────────────────────
DEFAULT_RELEASE_REPO = Path("F:/Agent/plastic-promise-release")
DEFAULT_EXPECTED_ORIGIN = "https://github.com/ALdaisuki/plastic-promise-release.git"
DEFAULT_EXPECTED_SOURCE_ORIGIN = "https://github.com/ALdaisuki/plastic-promise.git"

# ── Four-tier filter rules (per spec §1) ────────────────

INCLUDE: list[str] = [
    "plastic_promise/",
    "daemons/",
    "scripts/",
    "rust/",
    "tests/",
    "plugins/",
    "skills/",
    ".github/",
    "docs/BUILD_PLAN.md",
    "docs/GOAL.md",
    "docs/SYSTEM_FULL_CHAIN.md",
    "docs/DEVELOPER.md",
    "docs/README.zh-CN.md",
    "docs/architecture/",
    "docs/TODO List/",
    "data/db/.gitkeep",
    "data/lancedb/.gitkeep",
    "var/log/.gitkeep",
    "var/run/.gitkeep",
    "experience_packs/operations.json",
    "pyproject.toml",
    "requirements.txt",
    "Makefile",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "AGENTS.md",
    "CLAUDE.md",
    "LICENSE",
    ".editorconfig",
    ".pre-commit-config.yaml",
    ".codex/config.toml",
    ".gitignore",
    ".mcp.json",
    ".env.example",
    "market-index.yml",
    "package.json",
    "skills-lock.json",
]

EXCLUDE_DEV: list[str] = [
    "docs/superpowers/",
    ".pi/",
    ".claude/",
    ".superpowers/",
    ".trae/",
    "var/memory_files/",
    "docs/disk-space-investigation.md",
    "rust/context-engine-core/target/",
    ".interop/",
]

EXCLUDE_RUNTIME_GLOB: list[str] = [
    "*.dll",
    "*.pyd",
    "*.so",
    "*.dylib",
    "plastic_memory.db",
    "plastic_memory.db-shm",
    "plastic_memory.db-wal",
    "plastic_memory.lancedb/",
    "step_audit_log.jsonl",
    "*.pid",
    "var/log/*.log",
    "var/run/*.heartbeat",
    "var/test-export.json.gz",
    "experience_packs/test_ops.json",
    "nul",
    ".coverage",
    ".pytest_cache/",
    ".ruff_cache/",
    "__pycache__/",
    "*.pyc",
]

# ── Files needing version/date transformation ───────────

TRANSFORM: dict[str, str] = {
    "pyproject.toml": "update_version",
    "CHANGELOG.md": "prepend_entry",
    "docs/GOAL.md": "promote_release_status",
    "docs/SYSTEM_FULL_CHAIN.md": "update_header",
}


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sync filtered changes from dev to release repository")
    p.add_argument(
        "--from",
        dest="from_range",
        required=True,
        help="Git revision range to sync (e.g. HEAD~5..HEAD or main)",
    )
    p.add_argument("--version", required=True, help="SemVer tag (e.g. v0.2.0)")
    p.add_argument(
        "--release-repo",
        default=str(DEFAULT_RELEASE_REPO),
        help=f"Path to release repository (default: {DEFAULT_RELEASE_REPO})",
    )
    p.add_argument(
        "--expected-origin",
        default=DEFAULT_EXPECTED_ORIGIN,
        help="Expected origin URL for the release repository.",
    )
    p.add_argument(
        "--expected-source-origin",
        default=DEFAULT_EXPECTED_SOURCE_ORIGIN,
        help="Expected origin URL for the development repository.",
    )
    p.add_argument(
        "--expected-source-branch",
        default="main",
        help="Expected development branch for a live release (default: main).",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview changes without committing")
    p.add_argument(
        "--push",
        action="store_true",
        help=(
            "After final in-process attestation, atomically push the audited commit and "
            "annotated tag object to --expected-origin. Omitted by default."
        ),
    )
    p.add_argument(
        "--message", "-m", default=None, help="Custom commit message (default: auto-generated)"
    )
    p.add_argument(
        "--audit-range",
        default=None,
        help=(
            "Additional git revision range to verify against the release tree after copy. "
            "Defaults to --from. Use a wider range to catch omitted predecessor commits."
        ),
    )
    p.add_argument(
        "--validation-profile",
        choices=("full", "targeted", "compile", "none"),
        default="full",
        help="Validation mode: full pytest, targeted pytest, compileall only, or none.",
    )
    p.add_argument(
        "--targeted-test",
        action="append",
        default=[],
        help="Test path for --validation-profile targeted. Can be provided multiple times.",
    )
    return p


def run(
    cmd: list[str], cwd: Path | None = None, env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run a command and return the result. Raise on non-zero exit."""
    kwargs = {"cwd": cwd, "capture_output": True, "text": True}
    if env is not None:
        import copy

        full_env = copy.copy(os.environ)
        full_env.update(env)
        kwargs["env"] = full_env
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"ERROR: {' '.join(cmd)}")
        print(result.stderr)
        sys.exit(2)
    return result


def _git_probe(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_NO_INTERACTIVE},
    )


def _normalized_origin(value: str) -> str:
    normalized = value.strip().replace("\\", "/").rstrip("/")
    normalized = re.sub(r"^(?:https?|ssh)://", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^git@", "", normalized, flags=re.IGNORECASE)
    if normalized.lower().startswith("github.com:"):
        normalized = "github.com/" + normalized[len("github.com:") :]
    if normalized.lower().endswith(".git"):
        normalized = normalized[:-4]
    return normalized.lower()


def _bind_remote_branch_head(
    repo: Path,
    *,
    branch: str,
    local_head: str,
    error_prefix: str,
) -> str:
    branch_check = _git_probe(["check-ref-format", "--branch", branch], repo)
    if branch_check.returncode != 0:
        raise ValueError(f"{error_prefix}_branch_invalid")
    fetch = _git_probe(
        [
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        repo,
    )
    remote = _git_probe(
        ["ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
        repo,
    )
    fetched = _git_probe(
        ["rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}"],
        repo,
    )
    remote_lines = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    remote_head = remote_lines[0][0].lower() if len(remote_lines) == 1 else ""
    fetched_head = fetched.stdout.strip().lower()
    if (
        fetch.returncode != 0
        or remote.returncode != 0
        or fetched.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40,64}", remote_head) is None
        or re.fullmatch(r"[0-9a-f]{40,64}", fetched_head) is None
    ):
        raise ValueError(f"{error_prefix}_remote_head_unavailable")
    if remote_head != fetched_head or local_head != remote_head:
        raise ValueError(f"{error_prefix}_remote_head_mismatch")
    return remote_head


def _range_endpoint(revision_range: str) -> str:
    value = str(revision_range or "").strip()
    if not value or value.startswith("-") or any(character.isspace() for character in value):
        raise ValueError("source_range_invalid")
    if "..." in value:
        if value.count("...") != 1:
            raise ValueError("source_range_invalid")
        _left, right = value.split("...", 1)
    elif ".." in value:
        if value.count("..") != 1:
            raise ValueError("source_range_invalid")
        _left, right = value.split("..", 1)
    else:
        right = value
    if not right:
        raise ValueError("source_range_invalid")
    return right


def validate_source_ranges(
    source_root: Path,
    revision_ranges: list[str],
    *,
    expected_head: str | None,
) -> dict[str, str]:
    """Validate diff syntax and optionally bind every range's right endpoint to HEAD."""
    resolved: dict[str, str] = {}
    for revision_range in revision_ranges:
        endpoint = _range_endpoint(revision_range)
        diff = _git_probe(["diff", "--name-only", revision_range, "--"], source_root)
        revision = _git_probe(["rev-parse", "--verify", f"{endpoint}^{{commit}}"], source_root)
        commit = revision.stdout.strip().lower()
        if (
            diff.returncode != 0
            or revision.returncode != 0
            or re.fullmatch(r"[0-9a-f]{40,64}", commit) is None
        ):
            raise ValueError(f"source_range_invalid:{revision_range}")
        if expected_head is not None and commit != expected_head:
            raise ValueError(f"source_range_not_head:{revision_range}")
        resolved[revision_range] = commit
    return resolved


def validate_source_preflight(
    source_root: Path,
    *,
    expected_branch: str,
    expected_origin: str,
    revision_ranges: list[str],
) -> dict[str, object]:
    """Bind a live release to an exact clean development checkout and committed HEAD."""
    repo = source_root.resolve()
    if not repo.is_dir():
        raise ValueError("source_repo_not_directory")
    inside = _git_probe(["rev-parse", "--is-inside-work-tree"], repo)
    top = _git_probe(["rev-parse", "--show-toplevel"], repo)
    if inside.returncode != 0 or inside.stdout.strip() != "true" or top.returncode != 0:
        raise ValueError("source_repo_not_git")
    if Path(top.stdout.strip()).resolve() != repo:
        raise ValueError("source_repo_root_mismatch")

    status = _git_probe(["status", "--porcelain", "--untracked-files=all"], repo)
    if status.returncode != 0 or status.stdout.strip():
        raise ValueError("source_repo_not_clean")

    branch = _git_probe(["branch", "--show-current"], repo)
    if branch.returncode != 0 or not expected_branch or branch.stdout.strip() != expected_branch:
        raise ValueError("source_branch_mismatch")

    origin = _git_probe(["remote", "get-url", "origin"], repo)
    if origin.returncode != 0 or not origin.stdout.strip():
        raise ValueError("source_origin_missing")
    if _normalized_origin(origin.stdout) != _normalized_origin(expected_origin):
        raise ValueError("source_origin_mismatch")

    head_result = _git_probe(["rev-parse", "--verify", "HEAD^{commit}"], repo)
    head = head_result.stdout.strip().lower()
    if head_result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40,64}", head) is None:
        raise ValueError("source_head_invalid")
    remote_head = _bind_remote_branch_head(
        repo,
        branch=expected_branch,
        local_head=head,
        error_prefix="source",
    )
    ranges = validate_source_ranges(repo, revision_ranges, expected_head=head)
    return {
        "branch": branch.stdout.strip(),
        "origin": origin.stdout.strip(),
        "head": head,
        "remote_head": remote_head,
        "ranges": ranges,
    }


def validate_release_preflight(
    release_repo: Path,
    version: str,
    expected_origin: str,
) -> dict[str, str]:
    """Reject live release sync unless repository identity and state are exact."""
    repo = release_repo.resolve()
    if not repo.is_dir():
        raise ValueError("release_repo_not_directory")
    if re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version) is None:
        raise ValueError("release_version_invalid")
    inside = _git_probe(["rev-parse", "--is-inside-work-tree"], repo)
    top = _git_probe(["rev-parse", "--show-toplevel"], repo)
    if inside.returncode != 0 or inside.stdout.strip() != "true" or top.returncode != 0:
        raise ValueError("release_repo_not_git")
    if Path(top.stdout.strip()).resolve() != repo:
        raise ValueError("release_repo_root_mismatch")

    status = _git_probe(["status", "--porcelain", "--untracked-files=all"], repo)
    if status.returncode != 0 or status.stdout.strip():
        raise ValueError("release_repo_not_clean")

    branch = _git_probe(["branch", "--show-current"], repo)
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        raise ValueError("release_repo_not_main")

    origin = _git_probe(["remote", "get-url", "origin"], repo)
    if origin.returncode != 0 or not origin.stdout.strip():
        raise ValueError("release_origin_missing")
    if _normalized_origin(origin.stdout) != _normalized_origin(expected_origin):
        raise ValueError("release_origin_mismatch")

    head_result = _git_probe(["rev-parse", "--verify", "HEAD^{commit}"], repo)
    head = head_result.stdout.strip().lower()
    if head_result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40,64}", head) is None:
        raise ValueError("release_head_invalid")
    remote_head = _bind_remote_branch_head(
        repo,
        branch="main",
        local_head=head,
        error_prefix="release",
    )

    local_tag = _git_probe(["show-ref", "--verify", "--quiet", f"refs/tags/{version}"], repo)
    if local_tag.returncode == 0:
        raise ValueError(f"release_tag_exists_local:{version}")
    if local_tag.returncode != 1:
        raise ValueError("release_local_tag_check_failed")

    remote_tag = _git_probe(
        ["ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{version}"],
        repo,
    )
    if remote_tag.returncode == 0:
        raise ValueError(f"release_tag_exists_remote:{version}")
    if remote_tag.returncode != 2:
        raise ValueError("release_remote_tag_check_failed")
    return {
        "branch": "main",
        "origin": origin.stdout.strip(),
        "version": version,
        "head": head,
        "remote_head": remote_head,
    }


_GIT_NO_INTERACTIVE = {
    "GIT_COMMITTER_NAME": "Plastic Promise Release Bot",
    "GIT_COMMITTER_EMAIL": "release@plastic-promise.local",
    "GIT_AUTHOR_NAME": "Plastic Promise Release Bot",
    "GIT_AUTHOR_EMAIL": "release@plastic-promise.local",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "Never",
}


def is_included(filepath: str) -> bool:
    """Check if a filepath matches any INCLUDE rule and no EXCLUDE rules."""
    # Check explicit dev excludes first
    for pattern in EXCLUDE_DEV:
        if filepath.startswith(pattern) or filepath == pattern:
            return False

    # Check runtime glob excludes
    for pattern in EXCLUDE_RUNTIME_GLOB:
        if pattern.endswith("/"):
            if filepath.startswith(pattern):
                return False
        elif pattern.startswith("*."):
            if filepath.endswith(pattern[1:]):
                return False
        elif filepath == pattern or filepath.startswith(pattern):
            return False

    # Check include list
    for pattern in INCLUDE:
        if pattern.endswith("/"):
            if filepath.startswith(pattern):
                return True
        elif filepath == pattern:
            return True

    return False


def get_changed_files(from_range: str) -> list[str]:
    """Return list of files changed in the given range, relative to repo root."""
    result = run(
        ["git", "diff", "--name-only", from_range],
        cwd=PROJECT_ROOT,
    )
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def warn_left_endpoint_excluded(from_range: str) -> None:
    """Warn when a simple A..B range excludes release files changed by A."""
    if "..." in from_range or from_range.count("..") != 1:
        return

    left, right = from_range.split("..", 1)
    if not left:
        return

    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", left],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return

    included = [
        f.strip() for f in result.stdout.splitlines() if f.strip() and is_included(f.strip())
    ]
    if not included:
        return

    print(
        f"  [WARN] Range '{from_range}' excludes left endpoint {left}; "
        f"that commit changes {len(included)} release-included file(s). "
        f"Use '{left}^..{right}' or a wider --audit-range if it should be released."
    )


def filter_files(files: list[str]) -> tuple[list[str], list[str]]:
    """Split files into included and excluded lists."""
    included = [f for f in files if is_included(f)]
    excluded = [f for f in files if not is_included(f)]
    return included, excluded


def _release_section(content: str, heading_start: int) -> tuple[int, str]:
    next_heading = re.search(r"^## \[", content[heading_start + 1 :], re.MULTILINE)
    section_end = heading_start + 1 + next_heading.start() if next_heading else len(content)
    return section_end, content[heading_start:section_end]


def _require_promoted_status(filepath: str, content: str, version: str) -> None:
    if filepath == "CHANGELOG.md":
        heading = re.search(rf"^## \[v?{re.escape(version)}\](?:\s|$)", content, flags=re.MULTILINE)
        if heading is None:
            raise ValueError(f"release_heading_missing:{version}")
        _section_end, section = _release_section(content, heading.start())
        if "Draft/BLOCK" in content or "Draft (unreleased)" in content or "Draft/BLOCK" in section:
            raise ValueError(f"release_status_not_promoted:{version}")
    elif filepath == "docs/GOAL.md":
        marker = f"- Release version `{version}`"
        marker_start = content.find(marker)
        if marker_start < 0:
            raise ValueError(f"goal_release_marker_missing:{version}")
        next_heading = re.search(r"^## ", content[marker_start:], re.MULTILINE)
        section_end = marker_start + next_heading.start() if next_heading else len(content)
        if "Draft/BLOCK" in content or "Draft/BLOCK" in content[marker_start:section_end]:
            raise ValueError(f"goal_release_status_not_promoted:{version}")
    elif filepath == "docs/SYSTEM_FULL_CHAIN.md":
        expected = re.compile(rf"^> 版本: {re.escape(version)} \| 日期: \S+$", flags=re.MULTILINE)
        if expected.search(content) is None or "Draft/BLOCK" in content:
            raise ValueError(f"system_release_header_not_promoted:{version}")


def _source_file_bytes(
    filepath: str,
    dev_root: Path,
    source_commit: str | None = None,
) -> bytes | None:
    if source_commit is None:
        source = dev_root / filepath
        return source.read_bytes() if source.exists() else None
    result = subprocess.run(
        ["git", "show", f"{source_commit}:{filepath}"],
        cwd=dev_root,
        capture_output=True,
        env={**os.environ, **_GIT_NO_INTERACTIVE},
    )
    return result.stdout if result.returncode == 0 else None


def apply_transform(
    filepath: str,
    version: str,
    dev_root: Path,
    source_commit: str | None = None,
) -> str | None:
    """Apply version transformation to file content. Returns new content or None if no change."""
    if filepath not in TRANSFORM:
        return None

    source_bytes = _source_file_bytes(filepath, dev_root, source_commit)
    if source_bytes is None:
        return None

    content = source_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    transform_type = TRANSFORM[filepath]

    if transform_type == "update_version":
        # Replace version = "X.Y.Z" in pyproject.toml
        new_ver = version.lstrip("v")
        content, version_count = re.subn(
            r'^version\s*=\s*"[^"]*"',
            f'version = "{new_ver}"',
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if version_count != 1 or f'version = "{new_ver}"' not in content:
            raise ValueError(f"release_package_version_not_promoted:{new_ver}")
    elif transform_type == "prepend_entry":
        new_ver = version.lstrip("v")
        today = datetime.now().strftime("%Y-%m-%d")
        draft_heading = re.compile(
            rf"^## \[v?{re.escape(new_ver)}\] - Draft \(unreleased\)$",
            flags=re.MULTILINE,
        )
        if draft_heading.search(content):
            content = draft_heading.sub(f"## [{new_ver}] - {today}", content, count=1)
            content, banner_count = re.subn(
                rf"^Prepared release target: `{re.escape(new_ver)}` \(Draft/BLOCK\)\.$",
                f"Released version: `{new_ver}`.",
                content,
                count=1,
                flags=re.MULTILINE,
            )
            if banner_count != 1:
                raise ValueError(f"release_banner_not_promoted:{new_ver}")

            section_start = content.index(f"## [{new_ver}] - {today}")
            section_end, section = _release_section(content, section_start)
            section, status_count = re.subn(
                r"^- Overall release status is \*\*Draft/BLOCK\*\*\.[^\n]*(?:\n  [^\n]*)*",
                (
                    f"- Release status for `{new_ver}` is **audited and approved**. "
                    "Final whole-repository verification and mandatory high-risk review "
                    "completed before release synchronization. The one-shot public calibration "
                    "produced no eligible WRRF candidate, so held-out queries remained unopened "
                    "and legacy-auto is the released policy."
                ),
                section,
                count=1,
                flags=re.MULTILINE,
            )
            if status_count != 1:
                raise ValueError(f"release_verification_not_promoted:{new_ver}")
            content = content[:section_start] + section + content[section_end:]
            _require_promoted_status(filepath, content, new_ver)
            return content
        if re.search(rf"^## \[v?{re.escape(new_ver)}\](?:\s|$)", content, flags=re.MULTILINE):
            _require_promoted_status(filepath, content, new_ver)
            return content
        version = new_ver
        entry = f"\n## [{version}] — {today}\n\n### Added\n- \n\n### Changed\n- \n\n### Fixed\n- \n"
        content = entry + content
        _require_promoted_status(filepath, content, new_ver)
    elif transform_type == "promote_release_status":
        new_ver = version.lstrip("v")
        release_marker = f"- Release version `{new_ver}`"
        marker_start = content.find(release_marker)
        if marker_start < 0:
            raise ValueError(f"goal_release_marker_missing:{new_ver}")
        next_heading = re.search(r"^## ", content[marker_start:], re.MULTILINE)
        section_end = marker_start + next_heading.start() if next_heading else len(content)
        section = content[marker_start:section_end]
        section, status_count = re.subn(
            r"^- Verification status is \*\*Draft/BLOCK\*\*\..*$",
            (
                f"- Release verification for `{new_ver}` is **audited and approved**. "
                "Final whole-repository verification and mandatory high-risk review completed "
                "before release synchronization. The one-shot public calibration produced no "
                "eligible WRRF candidate, so held-out queries remained unopened and legacy-auto "
                "is the released policy."
            ),
            section,
            count=1,
            flags=re.MULTILINE,
        )
        if status_count != 1:
            raise ValueError(f"goal_release_verification_missing:{new_ver}")
        content = content[:marker_start] + section + content[section_end:]
        _require_promoted_status(filepath, content, new_ver)
    elif transform_type == "update_header":
        new_ver = version.lstrip("v")
        today = datetime.now().strftime("%Y-%m-%d")
        content, header_count = re.subn(
            rf"^> (?:版本: \S+ \| 日期: \S+|Prepared release target: {re.escape(new_ver)} \| Draft/BLOCK \| \S+)$",
            f"> 版本: {new_ver} | 日期: {today}",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if header_count != 1:
            raise ValueError(f"system_release_header_missing:{new_ver}")
        _require_promoted_status(filepath, content, new_ver)

    return content


def expected_release_bytes(
    filepath: str,
    version: str,
    source_commit: str | None = None,
) -> bytes | None:
    """Return expected release bytes for a dev file after release transforms."""
    source_bytes = _source_file_bytes(filepath, PROJECT_ROOT, source_commit)
    if source_bytes is None:
        return None

    transformed = apply_transform(filepath, version, PROJECT_ROOT, source_commit)
    if transformed is not None:
        return transformed.encode("utf-8")
    return source_bytes


def release_file_matches(
    filepath: str,
    version: str,
    dst: Path,
    source_commit: str | None = None,
) -> bool:
    """Compare a release file with expected dev content."""
    transformed = apply_transform(filepath, version, PROJECT_ROOT, source_commit)
    if transformed is not None:
        return dst.read_text(encoding="utf-8") == transformed

    expected = expected_release_bytes(filepath, version, source_commit)
    return expected is not None and dst.read_bytes() == expected


def audit_release_tree(
    filepaths: list[str],
    version: str,
    release_repo: Path,
    source_commit: str | None = None,
) -> bool:
    """Verify release files match dev content for included paths."""
    print("  Auditing release tree against dev content...")
    failures: list[tuple[str, str]] = []

    for filepath in sorted(set(filepaths)):
        expected = expected_release_bytes(filepath, version, source_commit)
        dst = release_repo / filepath

        if expected is None:
            if dst.exists():
                failures.append(("stale", filepath))
            continue

        if not dst.exists():
            failures.append(("missing", filepath))
            continue

        if not release_file_matches(filepath, version, dst, source_commit):
            failures.append(("diff", filepath))

    if failures:
        print("  FAIL: release audit found mismatches")
        for kind, filepath in failures[:50]:
            print(f"    {kind}: {filepath}")
        if len(failures) > 50:
            print(f"    ... and {len(failures) - 50} more")
        return False

    print("  release audit: OK")
    return True


def apply_to_release(
    included_files: list[str],
    version: str,
    release_repo: Path,
    dry_run: bool = False,
    source_commit: str | None = None,
) -> list[str]:
    """Copy included files from dev to release repo. Apply transforms. Return list of copied paths."""
    copied: list[str] = []

    for filepath in included_files:
        dst = release_repo / filepath
        source_bytes = _source_file_bytes(filepath, PROJECT_ROOT, source_commit)

        if source_bytes is None:
            # File was deleted in dev — remove from release
            if dst.exists():
                if dry_run:
                    print(f"  [DRY] REMOVE (deleted in dev): {filepath}")
                else:
                    dst.unlink()
                    print(f"  REMOVE (deleted in dev): {filepath}")
                copied.append(filepath)
            continue

        transformed = apply_transform(filepath, version, PROJECT_ROOT, source_commit)
        content = transformed if transformed is not None else source_bytes

        if dry_run:
            copied.append(filepath)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                dst.write_bytes(content)
            else:
                dst.write_text(content, encoding="utf-8")
            copied.append(filepath)

    return copied


def validate_release(
    release_repo: Path,
    profile: str = "full",
    targeted_tests: list[str] | None = None,
) -> bool:
    """Run compileall and pytest in the release repo. Return True if both pass."""
    if profile == "none":
        print("  validation: SKIP (--validation-profile none)")
        return True

    print("  Running compileall...")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            str(release_repo / "plastic_promise"),
            str(release_repo / "scripts"),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  FAIL: compileall\n{result.stderr}")
        return False
    print("  compileall: OK")

    if profile == "compile":
        return True

    if profile == "targeted":
        tests = targeted_tests or []
        if not tests:
            print("  FAIL: --validation-profile targeted requires --targeted-test")
            return False
        pytest_args = [sys.executable, "-m", "pytest", *tests, "-q", "--tb=short"]
        print(f"  Running targeted pytest: {' '.join(tests)}")
    else:
        pytest_args = [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"]
        print("  Running pytest...")

    result = subprocess.run(
        pytest_args,
        cwd=release_repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  FAIL: pytest\n{result.stderr}")
        return False
    print("  pytest: OK")

    return True


def _cleanup_runtime(release_repo: Path) -> None:
    """Remove runtime artifacts generated during validation to keep the release repo clean."""
    patterns = [
        ".coverage",
        ".pytest_cache",
        "__pycache__",
        ".ruff_cache",
        "*.pyc",
        "plastic_memory.db",
        "plastic_memory.db-shm",
        "plastic_memory.db-wal",
    ]
    import glob as _glob

    for pattern in patterns:
        for p in _glob.glob(str(release_repo / "**" / pattern), recursive=True):
            path = Path(p)
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except (OSError, PermissionError):
                pass


def _index_file_bytes(release_repo: Path, filepath: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f":{filepath}"],
        cwd=release_repo,
        capture_output=True,
        env={**os.environ, **_GIT_NO_INTERACTIVE},
    )
    return result.stdout if result.returncode == 0 else None


def _commit_file_bytes(release_repo: Path, commit: str, filepath: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{commit}:{filepath}"],
        cwd=release_repo,
        capture_output=True,
        env={**os.environ, **_GIT_NO_INTERACTIVE},
    )
    return result.stdout if result.returncode == 0 else None


def _remote_ref_oid(repo: Path, ref: str, *, remote: str = "origin") -> str:
    result = _git_probe(["ls-remote", "--exit-code", remote, ref], repo)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(rows) != 1:
        return ""
    oid = rows[0][0].lower()
    return oid if re.fullmatch(r"[0-9a-f]{40,64}", oid) else ""


def _require_release_tag_absent(repo: Path, version: str) -> None:
    local_tag = _git_probe(["show-ref", "--verify", "--quiet", f"refs/tags/{version}"], repo)
    if local_tag.returncode == 0:
        raise ValueError(f"release_tag_exists_local:{version}")
    if local_tag.returncode != 1:
        raise ValueError("release_local_tag_check_failed")
    _require_remote_release_tag_absent(repo, version)


def _require_remote_release_tag_absent(
    repo: Path,
    version: str,
    *,
    remote: str = "origin",
) -> None:
    remote_tag = _git_probe(
        ["ls-remote", "--exit-code", "--tags", remote, f"refs/tags/{version}"],
        repo,
    )
    if remote_tag.returncode == 0:
        raise ValueError(f"release_tag_exists_remote:{version}")
    if remote_tag.returncode != 2:
        raise ValueError("release_remote_tag_check_failed")


def _validate_staged_release_paths(
    release_repo: Path,
    allowed: list[str],
    *,
    expected_index_bytes: dict[str, bytes | None] | None = None,
) -> list[str]:
    unstaged = _git_probe(["diff", "--name-only"], release_repo)
    untracked = _git_probe(["ls-files", "--others", "--exclude-standard"], release_repo)
    staged = _git_probe(["diff", "--cached", "--name-only"], release_repo)
    if any(result.returncode != 0 for result in (unstaged, untracked, staged)):
        raise ValueError("release_stage_verification_failed")
    if unstaged.stdout.strip() or untracked.stdout.strip():
        raise ValueError("release_unexpected_worktree_changes")

    staged_paths = [path for path in staged.stdout.splitlines() if path.strip()]
    unexpected = sorted(set(staged_paths) - set(allowed))
    if unexpected:
        raise ValueError(f"release_unexpected_staged_paths:{','.join(unexpected)}")
    if set(staged_paths) != set(allowed):
        raise ValueError("release_staged_path_scope_mismatch")
    if not staged_paths:
        raise ValueError("release_no_staged_changes")
    if expected_index_bytes is not None:
        if set(expected_index_bytes) != set(allowed):
            raise ValueError("release_index_scope_mismatch")
        for filepath in allowed:
            if _index_file_bytes(release_repo, filepath) != expected_index_bytes[filepath]:
                raise ValueError(f"release_index_content_mismatch:{filepath}")
    return staged_paths


def stage_release_paths(
    release_repo: Path,
    filepaths: list[str],
    *,
    expected_index_bytes: dict[str, bytes | None] | None = None,
) -> list[str]:
    """Stage only computed release paths and reject validation side effects."""
    allowed = sorted(set(filepaths))
    if not allowed:
        raise ValueError("release_stage_scope_empty")
    run(["git", "add", "-A", "--", *allowed], cwd=release_repo)
    return _validate_staged_release_paths(
        release_repo,
        allowed,
        expected_index_bytes=expected_index_bytes,
    )


def _validate_release_repository_binding(
    release_repo: Path,
    *,
    version: str,
    expected_origin: str,
    expected_remote_head: str,
    allow_local_tag: bool = False,
) -> str:
    repo = release_repo.resolve()
    top = _git_probe(["rev-parse", "--show-toplevel"], repo)
    branch = _git_probe(["branch", "--show-current"], repo)
    origin = _git_probe(["remote", "get-url", "origin"], repo)
    head = _git_probe(["rev-parse", "--verify", "HEAD^{commit}"], repo)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repo:
        raise ValueError("release_repo_root_mismatch")
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        raise ValueError("release_repo_not_main")
    if origin.returncode != 0 or _normalized_origin(origin.stdout) != _normalized_origin(
        expected_origin
    ):
        raise ValueError("release_origin_mismatch")
    local_head = head.stdout.strip().lower()
    if head.returncode != 0 or re.fullmatch(r"[0-9a-f]{40,64}", local_head) is None:
        raise ValueError("release_head_invalid")
    remote_head = _remote_ref_oid(repo, "refs/heads/main")
    if remote_head != expected_remote_head.lower():
        raise ValueError("release_remote_head_mismatch")
    if allow_local_tag:
        _require_remote_release_tag_absent(repo, version)
    else:
        _require_release_tag_absent(repo, version)
    return local_head


def validate_release_commit_precondition(
    release_repo: Path,
    *,
    version: str,
    expected_origin: str,
    base_head: str,
    staged_paths: list[str],
    expected_index_bytes: dict[str, bytes | None],
) -> str:
    """Rebind repository identity and exact index immediately before commit."""
    head = _validate_release_repository_binding(
        release_repo,
        version=version,
        expected_origin=expected_origin,
        expected_remote_head=base_head,
    )
    if head != base_head.lower():
        raise ValueError("release_precommit_head_mismatch")
    _validate_staged_release_paths(
        release_repo,
        sorted(set(staged_paths)),
        expected_index_bytes=expected_index_bytes,
    )
    tree = _git_probe(["write-tree"], release_repo)
    tree_oid = tree.stdout.strip().lower()
    if tree.returncode != 0 or re.fullmatch(r"[0-9a-f]{40,64}", tree_oid) is None:
        raise ValueError("release_index_tree_invalid")
    return tree_oid


def _require_commit_semantics(filepath: str, content: bytes, version: str) -> None:
    if filepath not in TRANSFORM:
        return
    text = content.decode("utf-8")
    release_version = version.lstrip("v")
    if filepath == "pyproject.toml":
        if (
            re.search(rf'^version\s*=\s*"{re.escape(release_version)}"$', text, re.MULTILINE)
            is None
        ):
            raise ValueError(f"release_package_version_not_promoted:{release_version}")
        return
    _require_promoted_status(filepath, text, release_version)


def validate_release_commit_attestation(
    release_repo: Path,
    *,
    version: str,
    expected_origin: str,
    base_head: str,
    committed_paths: list[str],
    expected_tree_bytes: dict[str, bytes | None],
    expected_tree_oid: str,
    allow_local_tag: bool = False,
) -> str:
    """Bind the created commit tree and semantics before an annotated tag is allowed."""
    commit = _validate_release_repository_binding(
        release_repo,
        version=version,
        expected_origin=expected_origin,
        expected_remote_head=base_head,
        allow_local_tag=allow_local_tag,
    )
    status = _git_probe(["status", "--porcelain", "--untracked-files=all"], release_repo)
    parents = _git_probe(["rev-list", "--parents", "-n", "1", commit], release_repo)
    changed = _git_probe(["diff", "--name-only", f"{base_head}..{commit}", "--"], release_repo)
    tree = _git_probe(["rev-parse", "--verify", f"{commit}^{{tree}}"], release_repo)
    parent_fields = parents.stdout.split()
    expected_paths = sorted(set(committed_paths))
    changed_paths = sorted(path for path in changed.stdout.splitlines() if path.strip())
    if status.returncode != 0 or status.stdout.strip():
        raise ValueError("release_postcommit_repo_not_clean")
    if parents.returncode != 0 or len(parent_fields) != 2 or parent_fields[1] != base_head:
        raise ValueError("release_commit_parent_mismatch")
    if changed.returncode != 0 or changed_paths != expected_paths:
        raise ValueError("release_commit_path_scope_mismatch")
    if set(expected_tree_bytes) != set(expected_paths):
        raise ValueError("release_tree_scope_mismatch")
    for filepath in expected_paths:
        actual = _commit_file_bytes(release_repo, commit, filepath)
        expected = expected_tree_bytes[filepath]
        if actual != expected:
            raise ValueError(f"release_commit_tree_mismatch:{filepath}")
        if actual is not None:
            _require_commit_semantics(filepath, actual, version)
    if tree.returncode != 0 or tree.stdout.strip().lower() != expected_tree_oid.lower():
        raise ValueError("release_commit_tree_oid_mismatch")
    return commit


def validate_release_tag_precondition(
    release_repo: Path,
    *,
    version: str,
    expected_origin: str,
    base_head: str,
    release_commit: str,
    committed_paths: list[str],
    expected_tree_bytes: dict[str, bytes | None],
    expected_tree_oid: str,
) -> None:
    """Repeat the full commit attestation immediately before creating the tag."""
    attested = validate_release_commit_attestation(
        release_repo,
        version=version,
        expected_origin=expected_origin,
        base_head=base_head,
        committed_paths=committed_paths,
        expected_tree_bytes=expected_tree_bytes,
        expected_tree_oid=expected_tree_oid,
    )
    if attested != release_commit:
        raise ValueError("release_pretag_commit_mismatch")


def verify_tag_target(
    release_repo: Path,
    version: str,
    *,
    expected_commit: str | None = None,
    expected_origin: str | None = None,
    base_head: str | None = None,
) -> str:
    """Return HEAD only when the new annotated tag resolves to that commit."""
    head = _git_probe(["rev-parse", "HEAD^{commit}"], release_repo)
    tag = _git_probe(["rev-parse", f"refs/tags/{version}^{{commit}}"], release_repo)
    tag_type = _git_probe(["cat-file", "-t", f"refs/tags/{version}"], release_repo)
    if head.returncode != 0 or tag.returncode != 0 or tag_type.returncode != 0:
        raise ValueError("release_tag_target_unreadable")
    head_commit = head.stdout.strip()
    if (
        not head_commit
        or tag.stdout.strip() != head_commit
        or tag_type.stdout.strip() != "tag"
        or (expected_commit is not None and head_commit != expected_commit)
    ):
        raise ValueError(f"release_tag_target_mismatch:{version}")
    if expected_origin is not None or base_head is not None:
        if expected_origin is None or base_head is None:
            raise ValueError("release_tag_binding_incomplete")
        repo = release_repo.resolve()
        top = _git_probe(["rev-parse", "--show-toplevel"], repo)
        branch = _git_probe(["branch", "--show-current"], repo)
        origin = _git_probe(["remote", "get-url", "origin"], repo)
        status = _git_probe(["status", "--porcelain", "--untracked-files=all"], repo)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repo:
            raise ValueError("release_repo_root_mismatch")
        if branch.returncode != 0 or branch.stdout.strip() != "main":
            raise ValueError("release_repo_not_main")
        if origin.returncode != 0 or _normalized_origin(origin.stdout) != _normalized_origin(
            expected_origin
        ):
            raise ValueError("release_origin_mismatch")
        if status.returncode != 0 or status.stdout.strip():
            raise ValueError("release_posttag_repo_not_clean")
        if _remote_ref_oid(repo, "refs/heads/main") != base_head.lower():
            raise ValueError("release_remote_head_mismatch")
        _require_remote_release_tag_absent(repo, version)
    return head_commit


def release_tag_object_oid(release_repo: Path, version: str) -> str:
    """Return the immutable annotated-tag object ID, rejecting lightweight tags."""
    tag = _git_probe(["rev-parse", "--verify", f"refs/tags/{version}"], release_repo)
    tag_type = _git_probe(["cat-file", "-t", f"refs/tags/{version}"], release_repo)
    tag_oid = tag.stdout.strip().lower()
    if (
        tag.returncode != 0
        or tag_type.returncode != 0
        or tag_type.stdout.strip() != "tag"
        or re.fullmatch(r"[0-9a-f]{40,64}", tag_oid) is None
    ):
        raise ValueError("release_tag_object_invalid")
    return tag_oid


def push_attested_release(
    release_repo: Path,
    *,
    version: str,
    expected_origin: str,
    base_head: str,
    release_commit: str,
    expected_tag_object_oid: str,
    committed_paths: list[str],
    expected_tree_bytes: dict[str, bytes | None],
    expected_tree_oid: str,
) -> None:
    """Re-attest immutable release objects, then atomically publish those exact OIDs."""
    repo = release_repo.resolve()
    attested_commit = validate_release_commit_attestation(
        repo,
        version=version,
        expected_origin=expected_origin,
        base_head=base_head,
        committed_paths=committed_paths,
        expected_tree_bytes=expected_tree_bytes,
        expected_tree_oid=expected_tree_oid,
        allow_local_tag=True,
    )
    tagged_commit = verify_tag_target(
        repo,
        version,
        expected_commit=release_commit,
        expected_origin=expected_origin,
        base_head=base_head,
    )
    tag_object_oid = release_tag_object_oid(repo, version)
    if attested_commit != release_commit or tagged_commit != release_commit:
        raise ValueError("release_prepush_commit_mismatch")
    if tag_object_oid != expected_tag_object_oid.lower():
        raise ValueError("release_prepush_tag_object_mismatch")

    # Use the expected URL and immutable object IDs as push sources. The configured
    # remote name and mutable local refs are deliberately not part of this command.
    if _remote_ref_oid(repo, "refs/heads/main", remote=expected_origin) != base_head.lower():
        raise ValueError("release_remote_head_mismatch")
    _require_remote_release_tag_absent(repo, version, remote=expected_origin)
    pushed = _git_probe(
        [
            "push",
            "--atomic",
            f"--force-with-lease=refs/heads/main:{base_head}",
            expected_origin,
            f"{release_commit}:refs/heads/main",
            f"{tag_object_oid}:refs/tags/{version}",
        ],
        repo,
    )
    if pushed.returncode != 0:
        raise ValueError("release_atomic_push_failed")
    if _remote_ref_oid(repo, "refs/heads/main", remote=expected_origin) != release_commit.lower():
        raise ValueError("release_pushed_main_mismatch")
    if _remote_ref_oid(repo, f"refs/tags/{version}", remote=expected_origin) != tag_object_oid:
        raise ValueError("release_pushed_tag_mismatch")


def main() -> None:
    parser = build_argparser()
    args = parser.parse_args()
    if args.dry_run and args.push:
        parser.error("--push cannot be combined with --dry-run")
    release_repo = Path(args.release_repo).resolve()

    if not release_repo.exists():
        print(f"ERROR: Release repo not found at {release_repo}")
        print(
            f"  Clone it first: git clone git@github.com:ALdaisuki/plastic-promise-release.git {release_repo}"
        )
        sys.exit(1)

    print(f"=== Release Sync: {args.version} ===")
    print(f"  Dev:     {PROJECT_ROOT}")
    print(f"  Release: {release_repo}")
    print(f"  Range:   {args.from_range}")
    print(f"  Mode:    {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    audit_range = args.audit_range or args.from_range
    revision_ranges = list(dict.fromkeys([args.from_range, audit_range]))
    source_commit: str | None = None
    if not args.dry_run:
        try:
            source_preflight = validate_source_preflight(
                PROJECT_ROOT,
                expected_branch=args.expected_source_branch,
                expected_origin=args.expected_source_origin,
                revision_ranges=revision_ranges,
            )
            preflight = validate_release_preflight(
                release_repo,
                args.version,
                args.expected_origin,
            )
        except ValueError as exc:
            print(f"ERROR: Release preflight failed: {exc}")
            sys.exit(1)
        source_commit = str(source_preflight["head"])
        print(
            "  Source: clean "
            f"{source_preflight['branch']} at {source_preflight['origin']} "
            f"(HEAD=origin/{source_preflight['branch']}={source_commit})"
        )
        print(
            "  Preflight: clean "
            f"{preflight['branch']} at {preflight['origin']} "
            f"(HEAD=origin/{preflight['branch']}={preflight['head']}; tag absent locally/remotely)"
        )
    else:
        try:
            preview_ranges = validate_source_ranges(
                PROJECT_ROOT,
                revision_ranges,
                expected_head=None,
            )
        except ValueError as exc:
            print(f"ERROR: Dry-run source range invalid: {exc}")
            sys.exit(1)
        print(
            "  [DRY] Source provenance is not enforced; preview uses current worktree bytes. "
            f"Resolved ranges: {preview_ranges}"
        )

    # 1. Get changed files
    print("[1/8] Computing diff...")
    warn_left_endpoint_excluded(args.from_range)
    files = get_changed_files(args.from_range)
    included, excluded = filter_files(files)
    print(f"  Total changed: {len(files)}")
    print(f"  Included:      {len(included)}")
    print(f"  Excluded:      {len(excluded)}")
    if excluded:
        for f in excluded[:10]:
            print(f"    - {f}")
        if len(excluded) > 10:
            print(f"    ... and {len(excluded) - 10} more")

    if not included:
        print("\n  No files to sync. Nothing to do.")
        return

    # 2. Compute audit scope
    print("\n[2/8] Computing audit scope...")
    audit_files, _audit_excluded = filter_files(get_changed_files(audit_range))
    audit_scope = sorted(set(included) | set(audit_files))
    print(f"  Audit range:   {audit_range}")
    print(f"  Audit files:   {len(audit_scope)}")

    # 2. Copy files
    print(f"\n[3/8] Copying {len(included)} files...")
    copied = apply_to_release(
        included,
        args.version,
        release_repo,
        args.dry_run,
        source_commit,
    )
    for f in copied:
        tag = " [TRANSFORMED]" if f in TRANSFORM else ""
        print(f"  {'[DRY] ' if args.dry_run else ''}{f}{tag}")

    # 4. Audit release tree
    print("\n[4/8] Auditing synced content...")
    if not args.dry_run:
        if not audit_release_tree(
            audit_scope,
            args.version,
            release_repo,
            source_commit,
        ):
            print("ERROR: Release audit failed. Refusing to continue.")
            sys.exit(1)
    else:
        print("  DRY RUN - skipping content audit")

    # 5. Validate
    print("\n[5/8] Validating...")
    if not args.dry_run:
        if not validate_release(
            release_repo,
            profile=args.validation_profile,
            targeted_tests=args.targeted_test,
        ):
            print("ERROR: Validation failed. Release repo may be in dirty state.")
            sys.exit(1)
    else:
        print("  DRY RUN — skipping validation")

    # 6. Git add + cleanup
    print("\n[6/8] Staging changes...")
    if not args.dry_run:
        # Clean runtime artifacts that may have been generated during validation
        _cleanup_runtime(release_repo)
        if not audit_release_tree(
            audit_scope,
            args.version,
            release_repo,
            source_commit,
        ):
            print("ERROR: Post-validation release audit failed. Refusing to stage.")
            sys.exit(1)
        expected_index_bytes = {
            filepath: expected_release_bytes(filepath, args.version, source_commit)
            for filepath in copied
        }
        try:
            staged_paths = stage_release_paths(
                release_repo,
                copied,
                expected_index_bytes=expected_index_bytes,
            )
        except ValueError as exc:
            print(f"ERROR: Release staging failed: {exc}")
            sys.exit(1)
        print(f"  Staged: {len(staged_paths)} computed release path(s)")
        try:
            expected_tree_oid = validate_release_commit_precondition(
                release_repo,
                version=args.version,
                expected_origin=args.expected_origin,
                base_head=str(preflight["head"]),
                staged_paths=staged_paths,
                expected_index_bytes=expected_index_bytes,
            )
        except ValueError as exc:
            print(f"ERROR: Release pre-commit binding failed: {exc}")
            sys.exit(1)

    # 7. Commit
    message = args.message or f"chore(release): sync {args.version}"
    print(f"\n[7/8] Committing: {message}")
    if not args.dry_run:
        run(
            ["git", "commit", "-m", message, "--no-gpg-sign"],
            cwd=release_repo,
            env=_GIT_NO_INTERACTIVE,
        )
        try:
            release_commit = validate_release_commit_attestation(
                release_repo,
                version=args.version,
                expected_origin=args.expected_origin,
                base_head=str(preflight["head"]),
                committed_paths=staged_paths,
                expected_tree_bytes=expected_index_bytes,
                expected_tree_oid=expected_tree_oid,
            )
        except ValueError as exc:
            print(f"ERROR: Release commit attestation failed: {exc}")
            sys.exit(1)
        print(f"  Commit tree attested: {release_commit}")

    # 8. Tag
    print(f"\n[8/8] Tagging: {args.version}")
    if not args.dry_run:
        try:
            validate_release_tag_precondition(
                release_repo,
                version=args.version,
                expected_origin=args.expected_origin,
                base_head=str(preflight["head"]),
                release_commit=release_commit,
                committed_paths=staged_paths,
                expected_tree_bytes=expected_index_bytes,
                expected_tree_oid=expected_tree_oid,
            )
        except ValueError as exc:
            print(f"ERROR: Release pre-tag binding failed: {exc}")
            sys.exit(1)
        run(
            [
                "git",
                "tag",
                "-a",
                args.version,
                release_commit,
                "-m",
                f"Release {args.version}",
            ],
            cwd=release_repo,
            env=_GIT_NO_INTERACTIVE,
        )
        try:
            release_commit = verify_tag_target(
                release_repo,
                args.version,
                expected_commit=release_commit,
                expected_origin=args.expected_origin,
                base_head=str(preflight["head"]),
            )
        except ValueError as exc:
            print(f"ERROR: Release tag verification failed: {exc}")
            sys.exit(1)
        try:
            tag_object_oid = release_tag_object_oid(release_repo, args.version)
        except ValueError as exc:
            print(f"ERROR: Release tag object verification failed: {exc}")
            sys.exit(1)
        print(f"  Tag target: {release_commit} (tag object {tag_object_oid})")

        if args.push:
            try:
                push_attested_release(
                    release_repo,
                    version=args.version,
                    expected_origin=args.expected_origin,
                    base_head=str(preflight["head"]),
                    release_commit=release_commit,
                    expected_tag_object_oid=tag_object_oid,
                    committed_paths=staged_paths,
                    expected_tree_bytes=expected_index_bytes,
                    expected_tree_oid=expected_tree_oid,
                )
            except ValueError as exc:
                print(f"ERROR: Attested atomic push failed: {exc}")
                sys.exit(1)
            print("  Atomic push verified against exact commit and annotated tag object OIDs")

    print(f"\n=== {'DRY RUN complete' if args.dry_run else 'Sync complete'} ===")
    if not args.dry_run and not args.push:
        print(
            "  Push skipped (default); rerun the release from a clean baseline with --push to publish"
        )


if __name__ == "__main__":
    main()
