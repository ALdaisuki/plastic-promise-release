#!/usr/bin/env python3
"""Deploy one reviewed Plastic Promise revision to the canonical server.

The operator runs this script from a trusted source checkout.  The server does
not need GitHub credentials: a prerequisite-bound Git bundle carries only the
fast-forward delta from the expected production revision to the target SHA.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SSH_TARGET = re.compile(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]+\Z")
_ABSOLUTE_REMOTE_PATH = re.compile(r"/[A-Za-z0-9._/-]+\Z")
_LOOPBACK_HEALTH_URL = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}/[A-Za-z0-9._/?=&%-]*\Z")
_DEFAULT_RUNTIME = "/srv/plastic-promise/runtime"
_DEFAULT_STATE = "/srv/plastic-promise/state"
_DEFAULT_DB = "/srv/plastic-promise/state/db/plastic_memory.db"
_DEFAULT_HEALTH = "http://127.0.0.1:9020/health"


_REMOTE_SCRIPT = r"""
set -euo pipefail

target="$1"
expected="$2"
runtime="$3"
state_root="$4"
canonical_db="$5"
bundle="$6"
bundle_ref="$7"
health_url="$8"
health_timeout="$9"
trap 'rm -f -- "$bundle"' EXIT

if test "$(id -u)" -ne 0; then
  echo "server_cutover_requires_root" >&2
  exit 2
fi

cd "$runtime"
test -z "$(git status --porcelain)" || {
  echo "server_runtime_worktree_dirty" >&2
  exit 3
}

previous="$(git rev-parse HEAD)"
test "$previous" = "$expected" || {
  echo "server_current_revision_mismatch:$previous" >&2
  exit 4
}

git bundle verify "$bundle" >/dev/null
git fetch --quiet "$bundle" "$bundle_ref"
test "$(git rev-parse FETCH_HEAD)" = "$target" || {
  echo "server_bundle_target_mismatch" >&2
  exit 5
}

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
short="${target:0:12}"
backup="$state_root/backups/deploy-${short}-${timestamp}"
mkdir "$backup"
mkdir "$backup/units"

python3 - "$canonical_db" "$backup/canonical-predeploy.sqlite3" "$target" "$previous" <<'PY'
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
backup_path = Path(sys.argv[2])
target = sys.argv[3]
previous = sys.argv[4]
if not source_path.is_file():
    raise SystemExit("canonical_sqlite_missing")

with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source:
    with sqlite3.connect(backup_path) as destination:
        source.backup(destination)

with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as verified:
    integrity = verified.execute("PRAGMA integrity_check").fetchone()[0]
if integrity != "ok":
    raise SystemExit("canonical_backup_integrity_failed")

hasher = hashlib.sha256()
with backup_path.open("rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        hasher.update(block)
digest = hasher.hexdigest()
evidence = {
    "schema": "plastic-promise/server-cutover-backup/v1",
    "target_revision": target,
    "previous_revision": previous,
    "backup_file": backup_path.name,
    "bytes": backup_path.stat().st_size,
    "integrity_check": integrity,
    "sha256": f"sha256:{digest}",
}
(backup_path.parent / "backup-evidence.json").write_text(
    json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
PY

unit_manifest="$backup/unit-manifest.tsv"
: > "$unit_manifest"
for path in \
  /etc/systemd/system/plastic-promise-control-plane.service \
  /etc/systemd/system/plastic-promise-inference-gateway.service \
  /etc/systemd/system/plastic-promise-knowledge-ingest.service \
  /etc/systemd/system/plastic-promise-maintenance.service \
  /etc/systemd/system/plastic-promise-mcp.service.d/10-managed-env.conf
do
  name="$(printf '%s' "$path" | tr / _)"
  if test -f "$path"; then
    cp -a "$path" "$backup/units/$name"
    printf 'present\t%s\t%s\n' "$path" "$backup/units/$name" >> "$unit_manifest"
  else
    printf 'absent\t%s\t-\n' "$path" >> "$unit_manifest"
  fi
done

active_units="$backup/active-units.txt"
: > "$active_units"
for unit in \
  plastic-promise-control-plane.service \
  plastic-promise-inference-gateway.service \
  plastic-promise-mcp.service \
  plastic-promise-knowledge-ingest.service \
  plastic-promise-maintenance.service
do
  if systemctl is-active --quiet "$unit"; then
    printf '%s\n' "$unit" >> "$active_units"
  fi
done
grep -qx 'plastic-promise-mcp.service' "$active_units" || {
  echo "canonical_mcp_service_not_active" >&2
  exit 6
}

cutover_started=0
rollback() {
  rc=$?
  trap - ERR
  if test "$cutover_started" -eq 1; then
    git checkout --quiet --detach "$previous" || true
    while IFS=$'\t' read -r state path saved; do
      if test "$state" = present; then
        cp -a "$saved" "$path" || true
      else
        rm -f -- "$path" || true
      fi
    done < "$unit_manifest"
    systemctl daemon-reload || true
    while IFS= read -r unit; do
      systemctl restart "$unit" || true
    done < "$active_units"
  fi
  echo "server_cutover_rolled_back:$previous" >&2
  exit "$rc"
}
trap rollback ERR

cutover_started=1
git checkout --quiet --detach "$target"
install -m 0644 deploy/systemd/plastic-promise-control-plane.service /etc/systemd/system/plastic-promise-control-plane.service
install -m 0644 deploy/systemd/plastic-promise-inference-gateway.service /etc/systemd/system/plastic-promise-inference-gateway.service
install -m 0644 deploy/systemd/plastic-promise-knowledge-ingest.service /etc/systemd/system/plastic-promise-knowledge-ingest.service
install -m 0644 deploy/systemd/plastic-promise-maintenance.service /etc/systemd/system/plastic-promise-maintenance.service
install -d -m 0755 /etc/systemd/system/plastic-promise-mcp.service.d
install -m 0644 deploy/systemd/plastic-promise-mcp.service.d/10-managed-env.conf /etc/systemd/system/plastic-promise-mcp.service.d/10-managed-env.conf
systemctl daemon-reload
while IFS= read -r unit; do
  systemctl restart "$unit"
done < "$active_units"

health_file="$backup/postdeploy-health.json"
deadline=$((SECONDS + health_timeout))
while :; do
  all_active=1
  while IFS= read -r unit; do
    systemctl is-active --quiet "$unit" || all_active=0
  done < "$active_units"
  if test "$all_active" -eq 1 && curl -fsS --max-time 2 "$health_url" > "$health_file"; then
    break
  fi
  test "$SECONDS" -lt "$deadline" || {
    echo "server_cutover_health_timeout" >&2
    false
  }
  sleep 1
done

python3 - "$health_file" "$target" <<'PY'
import json
import sys
from pathlib import Path

health = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
target = sys.argv[2]
if health.get("status") != "ok":
    raise SystemExit("server_health_not_ok")
if health.get("source_revision") != target:
    raise SystemExit("server_health_revision_mismatch")
safe = {
    "status": health.get("status"),
    "source_revision": health.get("source_revision"),
    "retrieval_status": health.get("retrieval_status"),
    "vector_ready": health.get("vector_ready"),
    "lancedb_ready": health.get("lancedb_ready"),
    "bm25_ready": health.get("bm25_ready"),
    "graph_ready": health.get("graph_ready"),
    "vector_reason": health.get("vector_reason"),
}
print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
PY

while IFS= read -r unit; do
  pid="$(systemctl show -p MainPID --value "$unit")"
  test -n "$pid" && test "$pid" != 0
  tr '\000' '\n' < "/proc/$pid/environ" | grep -qx 'TZ=UTC' || {
    echo "server_runtime_timezone_not_utc:$unit" >&2
    false
  }
  printf '%s=%s\n' "$unit" active
  printf '%s_tz=%s\n' "$unit" UTC
done < "$active_units"

trap - ERR
printf 'server_cutover_revision=%s\n' "$target"
printf 'server_cutover_backup=%s\n' "$backup"
"""


class CutoverError(RuntimeError):
    """Raised when local cutover validation or execution fails."""


def _sha(value: str, *, name: str) -> str:
    normalized = value.strip().lower()
    if _SHA40.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError(f"{name} must be a full lowercase 40-hex Git SHA")
    return normalized


def _remote_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        value == "/"
        or _ABSOLUTE_REMOTE_PATH.fullmatch(value) is None
        or "//" in value
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise argparse.ArgumentTypeError("remote paths must be explicit safe absolute paths")
    return value.rstrip("/")


def _ssh_target(value: str) -> str:
    if _SSH_TARGET.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("invalid SSH target")
    return value


def _health_url(value: str) -> str:
    if _LOOPBACK_HEALTH_URL.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("health URL must use loopback HTTP")
    return value


def _health_timeout(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 10:
        raise argparse.ArgumentTypeError("health timeout must be between 1 and 10 seconds")
    return parsed


def _run(
    command: Sequence[str], *, cwd: Path, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        input=input_text,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CutoverError(f"command_failed:{command[0]}:{completed.returncode}")
    return completed


def _git_output(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise CutoverError(f"git_command_failed:{arguments[0]}")
    return completed.stdout.strip()


def build_plan(args: argparse.Namespace) -> dict[str, object]:
    return {
        "schema": "plastic-promise/server-revision-cutover-plan/v1",
        "apply": bool(args.apply),
        "source_repo": str(args.source_repo.resolve()),
        "ssh_target": args.ssh_target,
        "target_revision": args.revision,
        "expected_current_revision": args.expected_current_revision,
        "remote_runtime": args.remote_runtime,
        "remote_state": args.remote_state,
        "canonical_db": args.canonical_db,
        "health_url": args.health_url,
        "health_timeout_seconds": args.health_timeout_seconds,
        "transport": "prerequisite-bound-offline-git-bundle",
        "database_backup": "sqlite-online-backup+integrity-check+sha256",
        "rollback": "checkout+systemd-units+active-services",
        "host_timezone_mutation": False,
    }


def execute(args: argparse.Namespace) -> None:
    repo = args.source_repo.resolve()
    if not (repo / ".git").exists():
        raise CutoverError("source_repo_is_not_a_git_worktree")
    if _git_output(repo, "status", "--porcelain"):
        raise CutoverError("source_repo_worktree_dirty")
    resolved_target = _git_output(repo, "rev-parse", f"{args.revision}^{{commit}}")
    if resolved_target != args.revision:
        raise CutoverError("target_revision_not_resolved_exactly")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", args.expected_current_revision, args.revision],
        cwd=repo,
        check=False,
    )
    if ancestor.returncode != 0:
        raise CutoverError("target_is_not_fast_forward_from_expected_current")

    bundle_ref = f"refs/plastic-promise/cutover/{args.revision}"
    remote_bundle = f"/tmp/plastic-promise-cutover-{args.revision[:12]}.bundle"
    with tempfile.TemporaryDirectory(prefix="plastic-promise-cutover-") as temporary:
        bundle_path = Path(temporary) / "revision.bundle"
        _run(["git", "update-ref", bundle_ref, args.revision], cwd=repo)
        try:
            _run(
                [
                    "git",
                    "bundle",
                    "create",
                    str(bundle_path),
                    bundle_ref,
                    f"^{args.expected_current_revision}",
                ],
                cwd=repo,
            )
            _run(["git", "bundle", "verify", str(bundle_path)], cwd=repo)
            _run(
                [
                    "scp",
                    "-P",
                    str(args.ssh_port),
                    str(bundle_path),
                    f"{args.ssh_target}:{remote_bundle}",
                ],
                cwd=repo,
            )
            _run(
                [
                    "ssh",
                    "-p",
                    str(args.ssh_port),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=8",
                    args.ssh_target,
                    "bash",
                    "-s",
                    "--",
                    args.revision,
                    args.expected_current_revision,
                    args.remote_runtime,
                    args.remote_state,
                    args.canonical_db,
                    remote_bundle,
                    bundle_ref,
                    args.health_url,
                    str(args.health_timeout_seconds),
                ],
                cwd=repo,
                input_text=_REMOTE_SCRIPT,
            )
        finally:
            subprocess.run(
                ["git", "update-ref", "-d", bundle_ref],
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
            )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, default=Path.cwd())
    parser.add_argument("--ssh-target", type=_ssh_target, required=True)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument(
        "--revision", type=lambda value: _sha(value, name="revision"), required=True
    )
    parser.add_argument(
        "--expected-current-revision",
        type=lambda value: _sha(value, name="expected current revision"),
        required=True,
    )
    parser.add_argument("--remote-runtime", type=_remote_path, default=_DEFAULT_RUNTIME)
    parser.add_argument("--remote-state", type=_remote_path, default=_DEFAULT_STATE)
    parser.add_argument("--canonical-db", type=_remote_path, default=_DEFAULT_DB)
    parser.add_argument("--health-url", type=_health_url, default=_DEFAULT_HEALTH)
    parser.add_argument("--health-timeout-seconds", type=_health_timeout, default=10)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the cutover; without this flag only a no-secret plan is printed",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.ssh_port <= 65535:
        parser.error("SSH port must be between 1 and 65535")
    print(json.dumps(build_plan(args), ensure_ascii=False, indent=2, sort_keys=True))
    if not args.apply:
        return 0
    try:
        execute(args)
    except CutoverError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
