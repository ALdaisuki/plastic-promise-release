#!/usr/bin/env python3
"""Install or remove the per-user macOS timer for Codex hook state cleanup."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "org.plastic-promise.codex-hook-cleanup"
DEFAULT_INTERVAL_SECONDS = 900


def build_launch_agent(
    *,
    project_root: Path,
    python_executable: Path,
    state_dir: Path,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> dict[str, object]:
    root = project_root.resolve()
    python = python_executable.resolve()
    states = state_dir.resolve()
    if not 60 <= interval_seconds <= 86400:
        raise ValueError("interval_seconds must be between 60 and 86400")
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "plastic_promise.passive_memory.codex_hook",
            "--cleanup-states",
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PYTHONPATH": str(root),
            "PP_CODEX_HOOK_STATE_DIR": str(states),
        },
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "ProcessType": "Background",
        "ThrottleInterval": 60,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def _project_python(project_root: Path, explicit: str = "") -> Path:
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.extend(
        [
            project_root / ".venv" / "bin" / "python",
            project_root / ".venv" / "Scripts" / "python.exe",
        ]
    )
    for candidate in candidates:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        resolved = candidate.resolve()
        try:
            check = subprocess.run(
                [
                    str(resolved),
                    "-c",
                    "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if check.returncode == 0:
            return resolved
    raise RuntimeError("project Python not found; create .venv with Python 3.10+ first")


def _write_plist(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            plistlib.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        raise RuntimeError("launchctl is unavailable; this command requires macOS")
    return subprocess.run(
        [launchctl, *arguments],
        check=check,
        text=True,
        capture_output=True,
    )


def _launch_agent_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent installation requires macOS")
    project_root = Path(args.project_root).expanduser().resolve()
    python_executable = _project_python(project_root, args.python)
    state_dir = (
        Path(args.state_dir).expanduser().resolve()
        if args.state_dir
        else project_root / "var" / "codex-hooks"
    )
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise RuntimeError("hook state directory is unsafe")
    os.chmod(state_dir, 0o700)
    payload = build_launch_agent(
        project_root=project_root,
        python_executable=python_executable,
        state_dir=state_dir,
        interval_seconds=args.interval,
    )
    path = _launch_agent_path(Path.home())
    _write_plist(path, payload)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}", check=False)
    _launchctl("bootstrap", domain, str(path))
    _launchctl("kickstart", "-k", f"{domain}/{LABEL}")
    print(path)
    return 0


def uninstall(_args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent removal requires macOS")
    path = _launch_agent_path(Path.home())
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}", check=False)
    path.unlink(missing_ok=True)
    print(path)
    return 0


def print_plist(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).expanduser().resolve()
    python_executable = _project_python(project_root, args.python)
    state_dir = (
        Path(args.state_dir).expanduser().resolve()
        if args.state_dir
        else project_root / "var" / "codex-hooks"
    )
    payload = build_launch_agent(
        project_root=project_root,
        python_executable=python_executable,
        state_dir=state_dir,
        interval_seconds=args.interval,
    )
    sys.stdout.buffer.write(plistlib.dumps(payload, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for name, handler in (("install", install), ("print-plist", print_plist)):
        command = subparsers.add_parser(name)
        command.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
        command.add_argument("--python", default="")
        command.add_argument("--state-dir", default="")
        command.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
        command.set_defaults(handler=handler)
    remove = subparsers.add_parser("uninstall")
    remove.set_defaults(handler=uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
