#!/usr/bin/env python3
"""Release sync: filter and squash dev changes into the release repository.

Usage:
  python scripts/release-sync.py \
    --from HEAD~5..HEAD \
    --version v0.2.0 \
    --release-repo F:/Agent/plastic-promise-release

  python scripts/release-sync.py \
    --from main \
    --version v0.3.0 \
    --dry-run
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

# ── Project root detection ──────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Default release repo path ───────────────────────────
DEFAULT_RELEASE_REPO = Path("F:/Agent/plastic-promise-release")

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
    "docs/SYSTEM_FULL_CHAIN.md": "update_header",
}


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sync filtered changes from dev to release repository"
    )
    p.add_argument(
        "--from", dest="from_range", required=True,
        help="Git revision range to sync (e.g. HEAD~5..HEAD or main)"
    )
    p.add_argument(
        "--version", required=True,
        help="SemVer tag (e.g. v0.2.0)"
    )
    p.add_argument(
        "--release-repo", default=str(DEFAULT_RELEASE_REPO),
        help=f"Path to release repository (default: {DEFAULT_RELEASE_REPO})"
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without committing"
    )
    p.add_argument(
        "--message", "-m", default=None,
        help="Custom commit message (default: auto-generated)"
    )
    p.add_argument(
        "--audit-range", default=None,
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


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
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


def apply_transform(filepath: str, version: str, dev_root: Path) -> str | None:
    """Apply version transformation to file content. Returns new content or None if no change."""
    if filepath not in TRANSFORM:
        return None

    src = dev_root / filepath
    if not src.exists():
        return None

    content = src.read_text(encoding="utf-8")
    transform_type = TRANSFORM[filepath]

    if transform_type == "update_version":
        # Replace version = "X.Y.Z" in pyproject.toml
        new_ver = version.lstrip("v")
        content = re.sub(
            r'^version\s*=\s*"[^"]*"',
            f'version = "{new_ver}"',
            content,
            flags=re.MULTILINE,
        )
    elif transform_type == "prepend_entry":
        today = datetime.now().strftime("%Y-%m-%d")
        entry = f"\n## [{version}] — {today}\n\n### Added\n- \n\n### Changed\n- \n\n### Fixed\n- \n"
        content = entry + content
    elif transform_type == "update_header":
        new_ver = version.lstrip("v")
        today = datetime.now().strftime("%Y-%m-%d")
        content = re.sub(
            r"> 版本: \S+ \| 日期: \S+",
            f"> 版本: {new_ver} | 日期: {today}",
            content,
        )

    return content


def expected_release_bytes(filepath: str, version: str) -> bytes | None:
    """Return expected release bytes for a dev file after release transforms."""
    src = PROJECT_ROOT / filepath
    if not src.exists():
        return None

    transformed = apply_transform(filepath, version, PROJECT_ROOT)
    if transformed is not None:
        return transformed.encode("utf-8")
    return src.read_bytes()


def release_file_matches(filepath: str, version: str, dst: Path) -> bool:
    """Compare a release file with expected dev content."""
    transformed = apply_transform(filepath, version, PROJECT_ROOT)
    if transformed is not None:
        return dst.read_text(encoding="utf-8") == transformed

    expected = expected_release_bytes(filepath, version)
    return expected is not None and dst.read_bytes() == expected


def audit_release_tree(filepaths: list[str], version: str, release_repo: Path) -> bool:
    """Verify release files match dev content for included paths."""
    print("  Auditing release tree against dev content...")
    failures: list[tuple[str, str]] = []

    for filepath in sorted(set(filepaths)):
        expected = expected_release_bytes(filepath, version)
        dst = release_repo / filepath

        if expected is None:
            if dst.exists():
                failures.append(("stale", filepath))
            continue

        if not dst.exists():
            failures.append(("missing", filepath))
            continue

        if not release_file_matches(filepath, version, dst):
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
) -> list[str]:
    """Copy included files from dev to release repo. Apply transforms. Return list of copied paths."""
    copied: list[str] = []

    for filepath in included_files:
        src = PROJECT_ROOT / filepath
        dst = release_repo / filepath

        if not src.exists():
            # File was deleted in dev — remove from release
            if dst.exists():
                if dry_run:
                    print(f"  [DRY] REMOVE (deleted in dev): {filepath}")
                else:
                    dst.unlink()
                    print(f"  REMOVE (deleted in dev): {filepath}")
                copied.append(filepath)
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)

        transformed = apply_transform(filepath, version, PROJECT_ROOT)
        content = transformed if transformed is not None else src.read_bytes()

        if dry_run:
            copied.append(filepath)
        else:
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
        [sys.executable, "-m", "compileall", "-q",
         str(release_repo / "plastic_promise"),
         str(release_repo / "scripts")],
        capture_output=True, text=True,
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
        cwd=release_repo, capture_output=True, text=True,
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


def main() -> None:
    args = build_argparser().parse_args()
    release_repo = Path(args.release_repo).resolve()

    if not release_repo.exists():
        print(f"ERROR: Release repo not found at {release_repo}")
        print(f"  Clone it first: git clone git@github.com:ALdaisuki/plastic-promise-release.git {release_repo}")
        sys.exit(1)

    print(f"=== Release Sync: {args.version} ===")
    print(f"  Dev:     {PROJECT_ROOT}")
    print(f"  Release: {release_repo}")
    print(f"  Range:   {args.from_range}")
    print(f"  Mode:    {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

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
    audit_range = args.audit_range or args.from_range
    audit_files, _audit_excluded = filter_files(get_changed_files(audit_range))
    audit_scope = sorted(set(included) | set(audit_files))
    print(f"  Audit range:   {audit_range}")
    print(f"  Audit files:   {len(audit_scope)}")

    # 2. Copy files
    print(f"\n[3/8] Copying {len(included)} files...")
    copied = apply_to_release(included, args.version, release_repo, args.dry_run)
    for f in copied:
        tag = " [TRANSFORMED]" if f in TRANSFORM else ""
        print(f"  {'[DRY] ' if args.dry_run else ''}{f}{tag}")

    # 4. Audit release tree
    print("\n[4/8] Auditing synced content...")
    if not args.dry_run:
        if not audit_release_tree(audit_scope, args.version, release_repo):
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
        run(["git", "add", "-A"], cwd=release_repo)

    # 7. Commit
    message = args.message or f"chore(release): sync {args.version}"
    print(f"\n[7/8] Committing: {message}")
    if not args.dry_run:
        run(
            ["git", "commit", "-m", message, "--allow-empty", "--no-gpg-sign"],
            cwd=release_repo,
            env=_GIT_NO_INTERACTIVE,
        )

    # 8. Tag
    print(f"\n[8/8] Tagging: {args.version}")
    if not args.dry_run:
        run(
            ["git", "tag", "-a", args.version, "-m", f"Release {args.version}"],
            cwd=release_repo,
            env=_GIT_NO_INTERACTIVE,
        )

    print(f"\n=== {'DRY RUN complete' if args.dry_run else 'Sync complete'} ===")
    if not args.dry_run:
        print(f"  Next: cd {release_repo} && git push origin main --tags")


if __name__ == "__main__":
    main()
