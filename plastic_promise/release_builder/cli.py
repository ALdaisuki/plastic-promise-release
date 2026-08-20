"""Local CLI fallback for a maintainer's persistent Release Builder state."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from .contracts import ReleaseBuilderError, confirm_request
from .persistence import load_request, write_confirmation, write_request


def default_state_root() -> Path:
    """Return a per-user Builder state location without creating it."""

    if os.name == "nt":
        preferred = Path(r"D:\PlasticPromise\release-builder")
        return preferred
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured) / "plastic-promise" / "release-builder"
    return Path.home() / ".local" / "state" / "plastic-promise" / "release-builder"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plastic-promise-release-builder",
        description=(
            "Persist and confirm immutable Plastic Promise release requests. "
            "This command does not build, publish, deploy, or read credentials."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate a secret-free request JSON file")
    validate.add_argument("--request", required=True, type=Path)

    submit = subparsers.add_parser("submit", help="Persist an immutable local release request")
    submit.add_argument("--request", required=True, type=Path)
    submit.add_argument("--state-root", type=Path, default=default_state_root())

    confirm = subparsers.add_parser(
        "confirm", help="Create a 30-minute desktop confirmation for an exact request hash"
    )
    confirm.add_argument("--request-hash", required=True)
    confirm.add_argument("--confirm-request-hash", required=True)
    confirm.add_argument("--state-root", type=Path, default=default_state_root())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            request = load_request(args.request)
            _print({"status": "valid", "request_hash": request.request_hash})
            return 0
        if args.command == "submit":
            request = load_request(args.request)
            output = write_request(args.state_root, request)
            _print(
                {
                    "status": "submitted",
                    "request_hash": request.request_hash,
                    "request_path": str(output),
                }
            )
            return 0
        if args.command == "confirm":
            if args.request_hash != args.confirm_request_hash:
                raise ReleaseBuilderError("release_confirmation_hash_mismatch")
            request_path = args.state_root / "requests" / f"{args.request_hash}.json"
            request = load_request(request_path)
            confirmation = confirm_request(request, now=datetime.now(UTC))
            output = write_confirmation(args.state_root, confirmation)
            _print(
                {
                    "status": "confirmed",
                    "request_hash": confirmation.request_hash,
                    "expires_at": confirmation.expires_at.isoformat().replace("+00:00", "Z"),
                    "confirmation_path": str(output),
                }
            )
            return 0
        raise ReleaseBuilderError("release_builder_command_invalid")
    except ReleaseBuilderError as exc:
        _print({"status": "rejected", "reason": str(exc)})
        return 2


def _print(payload: dict[str, str]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main())
