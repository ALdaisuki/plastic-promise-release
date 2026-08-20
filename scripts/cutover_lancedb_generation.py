#!/usr/bin/env python3
"""Prepare or cut over one verified, generation-bound production live index.

The safe workflow is deliberately two-phase:

* ``prepare`` builds, reconciles, and verifies an inactive candidate while the
  current runtime remains available.
* ``cutover`` requires an independently stopped runtime, then optionally
  activates a staged Control revision, promotes the verified candidate,
  retargets Control through its authenticated API, bootstraps/verifies a new
  live root, and atomically updates the bootstrap EnvironmentFile.

The default mode prints a JSON plan. ``--apply`` is required for writes. This
tool never restarts services or changes Maintenance policy; those are separate
host-authorized operations after post-cutover health and retrieval smoke.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.run_with_env_files import parse_env_file  # noqa: E402

SERVICES_THAT_MUST_BE_STOPPED = (
    "plastic-promise-mcp.service",
    "plastic-promise-inference-gateway.service",
    "plastic-promise-maintenance.service",
    "plastic-promise-knowledge-ingest.service",
)


@dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...]


PREPARE_RECEIPT_SCHEMA = "plastic-promise-generation-prepare-receipt/v1"
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _file_sha256(path: Path, *, reason: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(reason)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise SystemExit(reason) from exc
    return f"sha256:{digest.hexdigest()}"


def _generation_identity(args: argparse.Namespace) -> dict[str, str]:
    paths = _paths(args)
    generations_root = paths["generation_root"] / "generations"
    manifest_path = generations_root / args.generation_id / "manifest.json"
    try:
        resolved_root = generations_root.resolve(strict=True)
        resolved_manifest = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise SystemExit("prepare_generation_manifest_missing_or_unsafe") from exc
    if (
        manifest_path.is_symlink()
        or not resolved_manifest.is_file()
        or not resolved_manifest.is_relative_to(resolved_root)
    ):
        raise SystemExit("prepare_generation_manifest_missing_or_unsafe")
    try:
        payload = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("prepare_generation_manifest_invalid") from exc
    if not isinstance(payload, dict) or payload.get("generation_id") != args.generation_id:
        raise SystemExit("prepare_generation_manifest_invalid")
    manifest_sha256 = payload.get("manifest_sha256")
    index_tree_sha256 = payload.get("index_tree_sha256")
    if (
        not isinstance(manifest_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
        or not isinstance(index_tree_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", index_tree_sha256) is None
    ):
        raise SystemExit("prepare_generation_manifest_identity_invalid")
    return {
        "generation_manifest_path": str(resolved_manifest),
        "generation_manifest_file_sha256": _file_sha256(
            resolved_manifest,
            reason="prepare_generation_manifest_missing_or_unsafe",
        ),
        "generation_manifest_sha256": f"sha256:{manifest_sha256}",
        "index_tree_sha256": f"sha256:{index_tree_sha256}",
    }


def _prepare_receipt_payload(args: argparse.Namespace) -> dict[str, str]:
    paths = _paths(args)
    payload = {
        "schema_version": PREPARE_RECEIPT_SCHEMA,
        "generation_id": args.generation_id,
        "project_id": args.project_id,
        "generation_root": str(paths["generation_root"]),
        "source_db": str(paths["source_db"]),
        "revision_id": args.revision_id or "",
        "revision_env_sha256": (
            _file_sha256(args.revision_env, reason="revision_env_missing_or_unsafe")
            if args.revision_env is not None
            else ""
        ),
        "quality_report_path": str(args.quality_report.resolve(strict=True)),
        "quality_report_sha256": _file_sha256(
            args.quality_report,
            reason="prepare_quality_report_missing_or_unsafe",
        ),
    }
    payload.update(_generation_identity(args))
    return payload


def _write_prepare_receipt(path: Path, payload: dict[str, str]) -> None:
    if path.is_symlink():
        raise SystemExit("prepare_receipt_symlink_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name == "posix":
            temporary.chmod(0o600)
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SystemExit("prepare_receipt_write_failed") from exc


def _verify_prepare_receipt(path: Path, args: argparse.Namespace) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("prepare_receipt_missing_or_unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("prepare_receipt_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "generation_id",
        "project_id",
        "generation_root",
        "source_db",
        "generation_manifest_path",
        "generation_manifest_file_sha256",
        "generation_manifest_sha256",
        "index_tree_sha256",
        "revision_id",
        "revision_env_sha256",
        "quality_report_path",
        "quality_report_sha256",
    }:
        raise SystemExit("prepare_receipt_invalid")
    expected_revision_digest = (
        _file_sha256(args.revision_env, reason="revision_env_missing_or_unsafe")
        if args.revision_env is not None
        else ""
    )
    paths = _paths(args)
    current_generation = _generation_identity(args)
    quality_report_path = Path(str(payload.get("quality_report_path", "")))
    quality_report_sha256 = _file_sha256(
        quality_report_path,
        reason="prepare_quality_report_missing_or_unsafe",
    )
    if (
        payload.get("schema_version") != PREPARE_RECEIPT_SCHEMA
        or payload.get("generation_id") != args.generation_id
        or payload.get("project_id") != args.project_id
        or payload.get("generation_root") != str(paths["generation_root"])
        or payload.get("source_db") != str(paths["source_db"])
        or payload.get("revision_id") != (args.revision_id or "")
        or payload.get("revision_env_sha256") != expected_revision_digest
        or payload.get("quality_report_sha256") != quality_report_sha256
        or any(payload.get(key) != value for key, value in current_generation.items())
        or any(
            SHA256_DIGEST.fullmatch(str(payload.get(key, ""))) is None
            for key in (
                "quality_report_sha256",
                "generation_manifest_file_sha256",
                "generation_manifest_sha256",
                "index_tree_sha256",
            )
        )
    ):
        raise SystemExit("prepare_receipt_identity_mismatch")
    return {str(key): str(value) for key, value in payload.items()}


def _paths(args: argparse.Namespace) -> dict[str, Path]:
    state_root = args.state_root
    control_root = args.control_root or state_root / "control"
    runtime_env = args.runtime_env or state_root / "plastic-promise.env"
    runtime_values = parse_env_file(runtime_env) if runtime_env.exists() else {}
    configured_generation_root = runtime_values.get("PLASTIC_LANCEDB_GENERATION_ROOT", "").strip()
    configured_source_db = runtime_values.get("PLASTIC_DB_PATH", "").strip()
    if args.generation_root is None and not configured_generation_root:
        raise SystemExit("generation_root_missing_from_runtime_environment")
    if args.source_db is None and not configured_source_db:
        raise SystemExit("source_db_missing_from_runtime_environment")
    generation_root = args.generation_root or Path(configured_generation_root)
    source_db = args.source_db or Path(configured_source_db)
    live_root = args.live_root or state_root / "lancedb-live" / f"live-{args.generation_id}"
    managed_env = args.managed_env or control_root / "managed.env"
    owner_reference = args.owner_reference or control_root
    return {
        "control_root": control_root,
        "generation_root": generation_root,
        "live_root": live_root,
        "runtime_env": runtime_env,
        "managed_env": managed_env,
        "owner_reference": owner_reference,
        "source_db": source_db,
    }


def build_steps(args: argparse.Namespace, repository_root: Path) -> tuple[Step, ...]:
    """Build a deterministic command plan for either phase."""

    scripts = repository_root / "scripts"
    paths = _paths(args)
    python = args.python
    runner = [
        python,
        str(scripts / "run_with_env_files.py"),
        "--env-file",
        str(paths["runtime_env"]),
        "--env-file",
        str(paths["managed_env"]),
    ]
    if args.revision_env is not None:
        runner.extend(("--env-file", str(args.revision_env)))
    runner.extend(("--owner-reference", str(paths["owner_reference"]), "--"))
    idem_digest = hashlib.sha256(
        f"{args.generation_id}\0{paths['live_root']}".encode()
    ).hexdigest()[:24]

    prepare_steps = [
        Step(
            "build",
            tuple(
                runner
                + [
                    python,
                    str(scripts / "rebuild_lancedb.py"),
                    "--generation-root",
                    str(paths["generation_root"]),
                    "--generation-id",
                    args.generation_id,
                    "--project-id",
                    args.project_id,
                    "--source-db",
                    str(paths["source_db"]),
                    "--quality-report",
                    str(args.quality_report),
                ]
            ),
        ),
        Step(
            "reconcile",
            tuple(
                runner
                + [
                    python,
                    str(scripts / "manage_lancedb_generations.py"),
                    "--root",
                    str(paths["generation_root"]),
                    "reconcile",
                    args.generation_id,
                    "--db",
                    str(paths["source_db"]),
                ]
            ),
        ),
        Step(
            "verify-candidate",
            tuple(
                runner
                + [
                    python,
                    str(scripts / "manage_lancedb_generations.py"),
                    "--root",
                    str(paths["generation_root"]),
                    "verify-candidate",
                    args.generation_id,
                    "--db",
                    str(paths["source_db"]),
                ]
            ),
        ),
    ]
    if args.phase == "prepare":
        return tuple(prepare_steps)

    if args.token_file is None:
        raise SystemExit("cutover_token_file_required")
    cutover_steps: list[Step] = []
    if args.revision_id:
        if args.evidence_file is None:
            raise SystemExit("cutover_evidence_file_required_for_revision_activation")
        cutover_steps.append(
            Step(
                "activate-control-revision",
                (
                    python,
                    str(scripts / "activate_control_revision.py"),
                    "--token-file",
                    str(args.token_file),
                    "--revision",
                    args.revision_id,
                    "--evidence-file",
                    str(args.evidence_file),
                ),
            )
        )
    cutover_steps.extend(
        (
            Step(
                "promote",
                tuple(
                    runner
                    + [
                        python,
                        str(scripts / "manage_lancedb_generations.py"),
                        "--root",
                        str(paths["generation_root"]),
                        "promote",
                        args.generation_id,
                        "--db",
                        str(paths["source_db"]),
                    ]
                ),
            ),
            Step(
                "control-retarget",
                (
                    python,
                    str(scripts / "retarget_current_generation.py"),
                    "--token-file",
                    str(args.token_file),
                    "--generation-root",
                    str(paths["generation_root"]),
                    "--idempotency-key",
                    f"generation-cutover-{idem_digest}",
                ),
            ),
            Step(
                "bootstrap-live-root",
                tuple(
                    runner
                    + [
                        python,
                        str(scripts / "manage_generation_live_index.py"),
                        "--live-root",
                        str(paths["live_root"]),
                        "bootstrap",
                        "--generation-root",
                        str(paths["generation_root"]),
                    ]
                ),
            ),
            Step(
                "verify-live-root",
                tuple(
                    runner
                    + [
                        python,
                        str(scripts / "manage_generation_live_index.py"),
                        "--live-root",
                        str(paths["live_root"]),
                        "verify",
                        "--generation-root",
                        str(paths["generation_root"]),
                    ]
                ),
            ),
            Step(
                "activate-runtime-env",
                (
                    python,
                    str(scripts / "update_runtime_env_file.py"),
                    "--env-file",
                    str(paths["runtime_env"]),
                    "--owner-reference",
                    str(paths["owner_reference"]),
                    "--set",
                    f"PLASTIC_LANCEDB_GENERATION_ROOT={paths['generation_root']}",
                    "--set",
                    f"PLASTIC_LANCEDB_LIVE_ROOT={paths['live_root']}",
                ),
            ),
        )
    )
    return tuple(cutover_steps)


def _assert_services_stopped(*, runner=subprocess.run) -> None:
    # The canonical cutover helper is also used on macOS development hosts,
    # where the runtime is supervised by launchd rather than systemd.  A
    # missing ``systemctl`` is therefore not evidence that a Linux service is
    # active; the host-specific launcher must be stopped by the caller before
    # invoking this phase.  Linux keeps the strict per-unit check below.
    if shutil.which("systemctl") is None:
        return
    for service in SERVICES_THAT_MUST_BE_STOPPED:
        result = runner(
            ("systemctl", "is-active", service),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or result.stdout.strip() == "active":
            raise SystemExit(f"cutover_service_still_active:{service}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("prepare", "cutover"), default="prepare")
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--quality-report", type=Path)
    parser.add_argument("--project-id", default="project:plastic-promise")
    parser.add_argument("--state-root", type=Path, default=Path("/srv/plastic-promise/state"))
    parser.add_argument("--control-root", type=Path)
    parser.add_argument("--generation-root", type=Path)
    parser.add_argument("--live-root", type=Path)
    parser.add_argument("--runtime-env", type=Path)
    parser.add_argument("--managed-env", type=Path)
    parser.add_argument("--revision-env", type=Path)
    parser.add_argument("--owner-reference", type=Path)
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--revision-id")
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--prepare-receipt", type=Path, required=True)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.phase == "prepare":
        if (
            args.quality_report is None
            or not args.quality_report.is_file()
            or args.quality_report.is_symlink()
        ):
            raise SystemExit("prepare_quality_report_missing_or_unsafe")
        if bool(args.revision_id) != bool(args.revision_env):
            raise SystemExit("prepare_revision_requires_revision_id_and_env")
    else:
        if args.quality_report is not None:
            raise SystemExit("cutover_quality_report_not_used")
        if args.revision_id and (args.evidence_file is None or args.revision_env is None):
            raise SystemExit("cutover_revision_requires_evidence_and_revision_env")
        if args.revision_id is None and args.revision_env is not None:
            raise SystemExit("cutover_revision_env_requires_revision_id")
        _verify_prepare_receipt(args.prepare_receipt, args)
    steps = build_steps(args, REPOSITORY_ROOT)
    plan = {
        "status": "planned",
        "apply_required": True,
        "phase": args.phase,
        "services_must_be_stopped_for_cutover": list(SERVICES_THAT_MUST_BE_STOPPED)
        if args.phase == "cutover"
        else [],
        "steps": [{"name": step.name, "argv": list(step.command)} for step in steps],
        "prepare_receipt": str(args.prepare_receipt),
    }
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.phase == "cutover":
        _assert_services_stopped()
    for step in steps:
        print(f"cutover_step={step.name}", flush=True)
        subprocess.run(step.command, cwd=REPOSITORY_ROOT, check=True)
    if args.phase == "prepare":
        _write_prepare_receipt(args.prepare_receipt, _prepare_receipt_payload(args))
        print(f"prepare_receipt={args.prepare_receipt}")
    print(f"cutover_generation={args.generation_id}")
    print(f"cutover_phase={args.phase}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
