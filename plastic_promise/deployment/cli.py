"""Cross-platform command surface for bounded deployment administration.

This CLI is intentionally a local control plane.  It can build plans, inspect
the host and manage a state root, and its ``build-node`` command is the
explicit operator action that runs the one-click local compute-node build,
start, and performance smoke.  The CLI itself never starts/stops systemd,
Compose, Windows services, SSH tunnels or inference processes outside that
explicit command; those other lifecycle steps remain operator actions
documented alongside the generated plan.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .catalog import deployment_modules, module_by_id, profile_by_id, stable_profile_ids
from .controller import DeploymentApplyError, DeploymentApplyResult, DeploymentController
from .doctor import observe_node_evidence
from .manifest import (
    DEPLOYMENT_MANIFEST_SCHEMA_VERSION,
    DeploymentContractError,
    DeploymentNode,
    load_deployment_manifest,
    resolve_deployment_manifest,
)
from .plan import DEPLOYMENT_OPERATIONS, DeploymentPlan, create_deployment_plan

if TYPE_CHECKING:
    from collections.abc import Sequence


def _add_plan_input(parser: argparse.ArgumentParser, *, plan_hash: bool = False) -> None:
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--state-root", type=Path, required=True)
    if plan_hash:
        parser.add_argument("--plan-hash", required=True)


def _add_mutation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")


def _add_planning_operation_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operation", choices=sorted(DEPLOYMENT_OPERATIONS), default="install")
    parser.add_argument("--module", dest="module_id")
    parser.add_argument("--source", type=Path)


def _add_module_mutation_inputs(parser: argparse.ArgumentParser) -> None:
    _add_plan_input(parser, plan_hash=True)
    parser.add_argument("--module", required=True, dest="module_id")
    _add_mutation_flags(parser)


def _add_init_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=stable_profile_ids())
    parser.add_argument("--module", action="append", dest="module_ids", default=[])
    parser.add_argument("--deployment-id", default="plastic-promise")
    parser.add_argument("--node-ssh-host")
    parser.add_argument("--node-max-concurrency", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--acknowledge-high-risk", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plastic-promise-deploy")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser(
        "init", help="interactively or declaratively create a non-secret deployment manifest"
    )
    _add_init_inputs(init)

    plan = subcommands.add_parser("plan", help="render a zero-side-effect deployment plan")
    _add_plan_input(plan)
    _add_planning_operation_inputs(plan)
    plan.add_argument("--json", action="store_true", dest="json_output")

    preflight = subcommands.add_parser("preflight", help="run read-only deployment safety checks")
    _add_plan_input(preflight)
    _add_planning_operation_inputs(preflight)
    preflight.add_argument("--json", action="store_true", dest="json_output")

    for command, help_text in (
        ("apply", "apply a reviewed deployment plan"),
        ("install", "install a reviewed deployment plan"),
        ("upgrade", "backup then apply versioned local migrations"),
        ("repair", "rerun the backup-gated local migration repair path"),
        ("backup", "create a verified online SQLite backup"),
    ):
        operation = subcommands.add_parser(command, help=help_text)
        _add_plan_input(operation, plan_hash=True)
        _add_mutation_flags(operation)

    status = subcommands.add_parser("status", help="inspect installer state without changing it")
    status.add_argument("--state-root", type=Path, required=True)
    status.add_argument("--json", action="store_true", dest="json_output")

    doctor = subcommands.add_parser(
        "doctor", help="inspect platform prerequisites without starting them"
    )
    doctor.add_argument("--manifest", type=Path)
    doctor.add_argument("--state-root", type=Path)
    doctor.add_argument(
        "--node-config",
        type=Path,
        help="optional non-secret local inference-node .env evidence",
    )
    doctor.add_argument(
        "--tunnel-config",
        type=Path,
        help="optional non-secret reverse-tunnel .env evidence",
    )
    doctor.add_argument(
        "--runtime-status",
        type=Path,
        help="optional redacted local inference runtime-status JSON evidence",
    )
    doctor.add_argument("--json", action="store_true", dest="json_output")

    build_node = subcommands.add_parser(
        "build-node",
        help="one-click local compute-node image build, start, and performance smoke",
    )
    build_node.add_argument(
        "--source-revision",
        help="40-hex source SHA; defaults to the checked-out repository HEAD",
    )
    build_node.add_argument(
        "--variant",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="compute variant; auto detects CUDA via nvidia-smi",
    )
    build_node.add_argument("--builder", help="dedicated Buildx builder name")
    build_node.add_argument("--image-tag", help="local-only image tag")
    build_node.add_argument("--retention-hours", type=int, help="retain recent project cache hours")
    build_node.add_argument("--report-directory", type=Path)
    build_node.add_argument(
        "--node-config",
        type=Path,
        help="compose .env to generate/update (non-secret node identity)",
    )
    build_node.add_argument(
        "--runtime-status",
        type=Path,
        help="runtime-status.json written after the performance smoke",
    )
    build_node.add_argument(
        "--skip-gpu-smoke",
        action="store_true",
        help="explicit degraded override; report is not GPU-smoke evidence",
    )
    build_node.add_argument(
        "--no-start",
        action="store_true",
        help="build and verify image labels only; do not start Compose or smoke",
    )
    build_node.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved platform command without executing it",
    )
    build_node.add_argument("--json", action="store_true", dest="json_output")

    module = subcommands.add_parser("module", help="inspect the fixed deployment module catalog")
    module_subcommands = module.add_subparsers(dest="module_command", required=True)
    module_list = module_subcommands.add_parser("list", help="list stable modules and risk tiers")
    module_list.add_argument("--json", action="store_true", dest="json_output")
    module_resolve = module_subcommands.add_parser(
        "resolve", help="resolve a manifest into its effective module set"
    )
    module_resolve.add_argument("--manifest", type=Path, required=True)
    module_resolve.add_argument("--json", action="store_true", dest="json_output")
    for module_command, help_text in (
        ("enable", "record one installed optional module as enabled"),
        ("disable", "record one installed optional module as disabled"),
        ("install", "record one eligible optional module as installed"),
        ("remove", "remove one optional module while retaining canonical data"),
    ):
        module_mutation = module_subcommands.add_parser(module_command, help=help_text)
        _add_module_mutation_inputs(module_mutation)

    disable = subcommands.add_parser(
        "disable", help="record one selected optional module as disabled"
    )
    _add_plan_input(disable, plan_hash=True)
    disable.add_argument("--module", required=True, dest="module_id")
    _add_mutation_flags(disable)

    for command, help_text in (
        ("remove", "remove installer ownership but preserve SQLite and backups"),
        ("uninstall", "alias for remove; canonical data stays intact"),
    ):
        remove = subcommands.add_parser(command, help=help_text)
        _add_plan_input(remove, plan_hash=True)
        remove.add_argument("--confirm-remove", action="store_true")
        _add_mutation_flags(remove)

    for command, help_text in (
        ("restore", "replace SQLite from a verified backup"),
        ("replace-db", "alias for restore; requires offline-service acknowledgement"),
    ):
        restore = subcommands.add_parser(command, help=help_text)
        _add_plan_input(restore, plan_hash=True)
        restore.add_argument("--source", type=Path, required=True)
        restore.add_argument("--confirm-restore", action="store_true")
        restore.add_argument("--service-stopped", action="store_true")
        _add_mutation_flags(restore)

    purge = subcommands.add_parser(
        "purge", help="physically remove SQLite after an automatic verified backup"
    )
    _add_plan_input(purge, plan_hash=True)
    purge.add_argument("--confirm-purge", action="store_true")
    purge.add_argument("--service-stopped", action="store_true")
    _add_mutation_flags(purge)
    return parser


def _json_print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _error(error: Exception, *, exit_code: int = 2) -> int:
    print(json.dumps({"error": str(error)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return exit_code


def _plan_from_args(args: argparse.Namespace) -> DeploymentPlan:
    resolved = load_deployment_manifest(_manifest_path_from_args(args))
    operation = _planning_operation(args)
    return create_deployment_plan(
        resolved,
        state_root=_required_path_argument(args, "state_root"),
        operation=operation,
        module_id=_optional_string_argument(args, "module_id"),
        source=_optional_path_argument(args, "source"),
    )


def _required_path_argument(args: argparse.Namespace, name: str) -> Path:
    value = getattr(args, name, None)
    if not isinstance(value, Path):
        raise ValueError(f"deployment_cli_path_required:{name}")
    return value


def _optional_path_argument(args: argparse.Namespace, name: str) -> Path | None:
    value = getattr(args, name, None)
    if value is None:
        return None
    if not isinstance(value, Path):
        raise ValueError(f"deployment_cli_path_invalid:{name}")
    return value


def _required_string_argument(args: argparse.Namespace, name: str) -> str:
    value = getattr(args, name, None)
    if not isinstance(value, str) or not value:
        raise ValueError(f"deployment_cli_string_required:{name}")
    return value


def _optional_string_argument(args: argparse.Namespace, name: str) -> str | None:
    value = getattr(args, name, None)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"deployment_cli_string_invalid:{name}")
    return value


def _manifest_path_from_args(args: argparse.Namespace) -> Path:
    manifest = _optional_path_argument(args, "manifest")
    if manifest is not None:
        return manifest
    directory = Path.home() / ".config" / "plastic-promise" / "deployments"
    try:
        candidates = tuple(sorted(directory.glob("*.json")))
    except OSError as exc:
        raise DeploymentContractError("default_manifest_unreadable") from exc
    if len(candidates) != 1:
        raise DeploymentContractError(
            "default_manifest_missing" if not candidates else "default_manifest_ambiguous"
        )
    return candidates[0]


def _planning_operation(args: argparse.Namespace) -> str:
    """Normalize user-facing aliases to one action-bound plan operation."""

    command = _required_string_argument(args, "command")
    if command in {"plan", "preflight"}:
        return _required_string_argument(args, "operation")
    if command in {"apply", "install"}:
        return "install"
    if command in {"upgrade", "repair"}:
        return "upgrade"
    if command in {"backup", "purge"}:
        return command
    if command in {"remove", "uninstall"}:
        return "remove"
    if command in {"restore", "replace-db"}:
        return "restore"
    if command == "disable":
        return "module-disable"
    if command == "module":
        return f"module-{_required_string_argument(args, 'module_command')}"
    raise ValueError("deployment_plan_operation_unsupported")


def _doctor_payload(
    controller: DeploymentController,
    *,
    state_root: Path | None,
    node_config: Path | None,
    tunnel_config: Path | None,
    runtime_status: Path | None,
    declared_node: DeploymentNode | None = None,
) -> dict[str, object]:
    """Produce a non-invasive platform report without invoking external programs."""

    system = platform.system()
    release = platform.release()
    is_wsl = system == "Linux" and "microsoft" in release.casefold()
    service_manager = (
        "windows-service-manager"
        if system == "Windows"
        else "launchd"
        if system == "Darwin"
        else "systemd"
        if Path("/run/systemd/system").exists()
        else "compose-or-manual"
    )
    executables = {
        name: "available" if shutil.which(name) else "missing"
        for name in ("ssh", "docker", "docker-compose", "nvidia-smi", "ollama")
    }
    disk: dict[str, object] = {"status": "not_requested"}
    deployment: dict[str, object] = {"status": "not_requested"}
    if state_root is not None:
        # Both observations avoid creating the requested root.
        status = controller.status(state_root=state_root)
        observed = controller.observe_disk_usage(state_root)
        disk = {"total_bytes": observed.total_bytes, "free_bytes": observed.free_bytes}
        deployment = status.as_dict()
    return {
        "schema": "plastic-promise/deployment-doctor/v1",
        "platform": {"system": system, "release": release, "wsl2": is_wsl},
        "service_manager": {"detected": service_manager, "managed_by_deploy_cli": False},
        "capabilities": executables,
        "node": observe_node_evidence(
            node_config=node_config,
            tunnel_config=tunnel_config,
            runtime_status=runtime_status,
            expected_node_id=declared_node.id if declared_node is not None else None,
            expected_capabilities=declared_node.capabilities if declared_node is not None else (),
        ),
        "disk": disk,
        "deployment": deployment,
        "guidance": {
            "ssh": "Use a dedicated restricted account for a reverse tunnel; do not pass passwords to this CLI.",
            "services": "Start or stop systemd, Compose, launchd, or Windows services explicitly outside this CLI.",
        },
    }


def _operation_payload(
    *,
    operation: str,
    outcome: DeploymentApplyResult,
    preflight: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "plastic-promise/deployment-operation/v1",
        "operation": operation,
        "mode": "dry-run" if outcome.database_action.endswith("dry_run") else "apply",
        **outcome.as_dict(),
    }
    if preflight is not None:
        payload["preflight"] = preflight
    return payload


def _prompt_profile() -> str:
    """Select a release profile only when an operator has an interactive terminal."""

    choices = stable_profile_ids()
    print("Available profiles:")
    for profile_id in choices:
        print(f"- {profile_id}")
    selected = input("Profile: ").strip()
    if selected not in choices:
        raise DeploymentContractError("init_profile_required")
    return selected


def _prompt_modules(profile_id: str) -> list[str]:
    """Collect optional module IDs without inventing an implicit default."""

    supported = [
        module.id
        for module in deployment_modules()
        if module.risk_tier != "core" and profile_id in module.supported_profiles
    ]
    if not supported:
        return []
    print("Optional modules: " + ", ".join(supported))
    selected = input("Optional modules (comma-separated, blank for none): ").strip()
    return [module_id.strip() for module_id in selected.split(",") if module_id.strip()]


def _manifest_for_init(
    *,
    deployment_id: str,
    profile_id: str,
    requested_module_ids: list[str],
    acknowledge_high_risk: bool,
    node_ssh_host: str | None,
    node_max_concurrency: int = 1,
) -> dict[str, object]:
    """Produce the smallest strict manifest without inferring runtime settings."""

    if profile_by_id(profile_id) is None:
        raise DeploymentContractError("init_profile_required")
    if len(requested_module_ids) != len(set(requested_module_ids)):
        raise DeploymentContractError("init_module_duplicate")
    modules: dict[str, dict[str, bool]] = {}
    for module_id in requested_module_ids:
        module = module_by_id(module_id)
        if module is None:
            raise DeploymentContractError("init_module_unsupported")
        if module.risk_tier == "core":
            raise DeploymentContractError("init_core_module_implicit")
        if profile_id not in module.supported_profiles:
            raise DeploymentContractError("init_module_profile_incompatible")
        if module.risk_tier == "high-risk" and not acknowledge_high_risk:
            raise DeploymentContractError("init_high_risk_acknowledgement_required")
        selection: dict[str, bool] = {"enabled": True}
        if module.risk_tier == "high-risk":
            selection["acknowledge_high_risk"] = True
        modules[module_id] = selection
    payload: dict[str, object] = {
        "schema_version": DEPLOYMENT_MANIFEST_SCHEMA_VERSION,
        "deployment_id": deployment_id,
        "profile": profile_id,
        "modules": modules,
        "nodes": [],
    }
    if profile_id == "split-accelerated":
        if not node_ssh_host:
            raise DeploymentContractError("init_node_ssh_host_required")
        payload["nodes"] = [
            {
                "id": "local-inference-node",
                "role": "local-heterogeneous-inference-node",
                "ssh_host": node_ssh_host,
                "capabilities": {"embedding": True, "rerank": True},
                "max_concurrency": node_max_concurrency,
            }
        ]
    resolve_deployment_manifest(payload)
    return payload


def _write_initial_manifest(args: argparse.Namespace) -> dict[str, object]:
    profile_id = args.profile
    if profile_id is None:
        if not sys.stdin.isatty():
            raise DeploymentContractError("init_profile_required")
        profile_id = _prompt_profile()
    requested_module_ids = list(args.module_ids)
    if not requested_module_ids and sys.stdin.isatty():
        requested_module_ids = _prompt_modules(profile_id)
    acknowledge_high_risk = args.acknowledge_high_risk
    if (
        not acknowledge_high_risk
        and sys.stdin.isatty()
        and any(
            (module := module_by_id(module_id)) is not None and module.risk_tier == "high-risk"
            for module_id in requested_module_ids
        )
    ):
        acknowledge_high_risk = input("Acknowledge high-risk modules (yes/no): ").strip() == "yes"
    payload = _manifest_for_init(
        deployment_id=args.deployment_id,
        profile_id=profile_id,
        requested_module_ids=requested_module_ids,
        acknowledge_high_risk=acknowledge_high_risk,
        node_ssh_host=args.node_ssh_host,
        node_max_concurrency=args.node_max_concurrency,
    )
    output = args.output or (
        Path.home() / ".config" / "plastic-promise" / "deployments" / f"{args.deployment_id}.json"
    )
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise DeploymentContractError("init_manifest_output_exists") from exc
    return {
        "schema": "plastic-promise/deployment-manifest-init/v1",
        "created": True,
        "manifest": str(output),
        "profile": profile_id,
        "modules": list(resolve_deployment_manifest(payload).module_ids),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run deployment administration without initialising Plastic Promise runtime."""

    args = _parser().parse_args(argv)
    controller = DeploymentController()
    try:
        if args.command == "init":
            _json_print(_write_initial_manifest(args))
            return 0
        if args.command == "module" and args.module_command == "list":
            _json_print(
                {
                    "schema": "plastic-promise/deployment-modules/v1",
                    "modules": [
                        {
                            "id": module.id,
                            "risk_tier": module.risk_tier,
                            "supported_profiles": list(module.supported_profiles),
                            "requires": list(module.requires),
                        }
                        for module in deployment_modules()
                    ],
                }
            )
            return 0
        if args.command == "module" and args.module_command == "resolve":
            resolved = load_deployment_manifest(args.manifest)
            _json_print(
                {
                    "schema": "plastic-promise/deployment-module-resolution/v1",
                    "deployment_id": resolved.deployment_id,
                    "profile": resolved.profile_id,
                    "modules": list(resolved.module_ids),
                    "nodes": list(resolved.node_ids),
                }
            )
            return 0

        if args.command == "status":
            _json_print(controller.status(state_root=args.state_root).as_dict())
            return 0

        if args.command == "build-node":
            from .node_build import plan_node_build, run_node_build

            plan = plan_node_build(
                source_revision=args.source_revision,
                variant=args.variant,
                builder=args.builder,
                image_tag=args.image_tag,
                retention_hours=args.retention_hours,
                report_directory=(
                    str(args.report_directory) if args.report_directory is not None else None
                ),
                node_config=args.node_config,
                runtime_status=args.runtime_status,
                skip_gpu_smoke=args.skip_gpu_smoke,
                no_start=args.no_start,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                payload = {**plan.as_dict(), "executed": False, "exit_code": None}
                _json_print(payload)
                return 0
            exit_code = run_node_build(plan)
            payload = {
                **plan.as_dict(),
                "executed": True,
                "exit_code": exit_code,
            }
            if args.json_output:
                _json_print(payload)
            else:
                print(f"build-node={'ok' if exit_code == 0 else 'failed'} exit_code={exit_code}")
            return exit_code

        if args.command == "doctor":
            declared_node = None
            if args.manifest is not None:
                resolved = load_deployment_manifest(args.manifest)
                declared_node = resolved.nodes[0] if resolved.nodes else None
            _json_print(
                _doctor_payload(
                    controller,
                    state_root=args.state_root,
                    node_config=args.node_config,
                    tunnel_config=args.tunnel_config,
                    runtime_status=args.runtime_status,
                    declared_node=declared_node,
                )
            )
            return 0

        plan = _plan_from_args(args)
        if args.command == "plan":
            if args.json_output:
                _json_print(plan.as_dict())
            else:
                print(f"plan_hash={plan.plan_hash}")
            return 0

        report = controller.preflight(plan)
        if args.command == "preflight":
            payload: dict[str, object] = {
                "schema": "plastic-promise/deployment-preflight/v1",
                "operation": "preflight",
                "planned_operation": plan.operation,
                "plan_hash": plan.plan_hash,
                "preflight": report.as_dict(),
            }
            if args.json_output:
                _json_print(payload)
            else:
                print(f"preflight={'ok' if report.ok else 'blocked'} plan_hash={plan.plan_hash}")
            return 0 if report.ok else 3

        if args.command in {"apply", "install"}:
            outcome = controller.apply(plan, plan_hash=args.plan_hash, dry_run=args.dry_run)
            _json_print(
                _operation_payload(
                    operation=args.command,
                    outcome=outcome,
                    preflight=report.as_dict(),
                )
            )
            return 0
        if args.command in {"upgrade", "repair"}:
            outcome = controller.upgrade(plan, plan_hash=args.plan_hash, dry_run=args.dry_run)
            _json_print(
                _operation_payload(
                    operation=args.command,
                    outcome=outcome,
                    preflight=report.as_dict(),
                )
            )
            return 0
        if args.command == "backup":
            outcome = controller.backup(plan, plan_hash=args.plan_hash, dry_run=args.dry_run)
            _json_print(_operation_payload(operation="backup", outcome=outcome))
            return 0
        if args.command == "disable":
            changed = controller.disable_module(
                plan,
                plan_hash=args.plan_hash,
                module_id=args.module_id,
                dry_run=args.dry_run,
            )
            _json_print(
                {
                    "schema": "plastic-promise/deployment-operation/v1",
                    "operation": "disable",
                    "mode": "dry-run" if args.dry_run else "apply",
                    "changed": changed,
                    "module": args.module_id,
                    "plan_hash": plan.plan_hash,
                }
            )
            return 0
        if args.command == "module":
            handlers = {
                "enable": controller.enable_module,
                "disable": controller.disable_module,
                "install": controller.install_module,
                "remove": controller.remove_module,
            }
            changed = handlers[args.module_command](
                plan,
                plan_hash=args.plan_hash,
                module_id=args.module_id,
                dry_run=args.dry_run,
            )
            _json_print(
                {
                    "schema": "plastic-promise/deployment-operation/v1",
                    "operation": plan.operation,
                    "mode": "dry-run" if args.dry_run else "apply",
                    "changed": changed,
                    "module": args.module_id,
                    "plan_hash": plan.plan_hash,
                }
            )
            return 0
        if args.command in {"remove", "uninstall"}:
            changed = controller.remove(
                plan,
                plan_hash=args.plan_hash,
                confirmed=args.confirm_remove,
                dry_run=args.dry_run,
            )
            _json_print(
                {
                    "schema": "plastic-promise/deployment-operation/v1",
                    "operation": args.command,
                    "mode": "dry-run" if args.dry_run else "apply",
                    "changed": changed,
                    "database_preserved": True,
                    "plan_hash": plan.plan_hash,
                }
            )
            return 0
        if args.command == "purge":
            outcome = controller.purge(
                plan,
                plan_hash=args.plan_hash,
                confirmed=args.confirm_purge,
                service_stopped=args.service_stopped,
                dry_run=args.dry_run,
            )
            _json_print(_operation_payload(operation="purge", outcome=outcome))
            return 0
        if args.command in {"restore", "replace-db"}:
            outcome = controller.restore(
                plan,
                plan_hash=args.plan_hash,
                source=args.source,
                confirmed=args.confirm_restore,
                service_stopped=args.service_stopped,
                dry_run=args.dry_run,
            )
            _json_print(_operation_payload(operation=args.command, outcome=outcome))
            return 0
    except (DeploymentApplyError, DeploymentContractError, OSError, ValueError) as exc:
        return _error(exc)
    raise AssertionError(f"unhandled deployment command: {args.command}")


__all__ = ["main"]
