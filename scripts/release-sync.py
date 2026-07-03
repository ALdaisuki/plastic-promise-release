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
    return p


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a command and return the result. Raise on non-zero exit."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: {' '.join(cmd)}")
        print(result.stderr)
        sys.exit(2)
    return result


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
            print(f"  SKIP (deleted in dev): {filepath}")
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


def validate_release(release_repo: Path) -> bool:
    """Run compileall and pytest in the release repo. Return True if both pass."""
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

    print("  Running pytest...")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"],
        cwd=release_repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  FAIL: pytest\n{result.stderr}")
        return False
    print("  pytest: OK")

    return True


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
    print("[1/6] Computing diff...")
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

    # 2. Copy files
    print(f"\n[2/6] Copying {len(included)} files...")
    copied = apply_to_release(included, args.version, release_repo, args.dry_run)
    for f in copied:
        tag = " [TRANSFORMED]" if f in TRANSFORM else ""
        print(f"  {'[DRY] ' if args.dry_run else ''}{f}{tag}")

    # 3. Validate
    print("\n[3/6] Validating...")
    if not args.dry_run:
        if not validate_release(release_repo):
            print("ERROR: Validation failed. Release repo may be in dirty state.")
            sys.exit(1)
    else:
        print("  DRY RUN — skipping validation")

    # 4. Git add
    print("\n[4/6] Staging changes...")
    if not args.dry_run:
        run(["git", "add", "-A"], cwd=release_repo)

    # 5. Commit
    message = args.message or f"chore(release): sync {args.version}"
    print(f"\n[5/6] Committing: {message}")
    if not args.dry_run:
        run(["git", "commit", "-m", message, "--allow-empty"], cwd=release_repo)

    # 6. Tag
    print(f"\n[6/6] Tagging: {args.version}")
    if not args.dry_run:
        run(["git", "tag", "-a", args.version, "-m", f"Release {args.version}"], cwd=release_repo)

    print(f"\n=== {'DRY RUN complete' if args.dry_run else 'Sync complete'} ===")
    if not args.dry_run:
        print(f"  Next: cd {release_repo} && git push origin main --tags")


if __name__ == "__main__":
    main()
