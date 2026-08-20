#!/usr/bin/env python3
"""Create a bounded stable-release receipt from a digest-pinned server smoke.

The script is intended to run on the production server after an operator has
started the reviewed Compose service by immutable image digest and has run
``smoke_http_mcp.py --read-only --json``.  It never reads SQLite or LanceDB,
contacts a registry, starts a service, or writes outside the requested receipt.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from plastic_promise.release_manifest import (
    ReleaseManifestError,
    build_server_deployment_receipt,
    validate_release_manifest,
    write_server_deployment_receipt,
)

MAX_JSON_BYTES = 65_536


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a no-secret server deployment receipt for a stable release."
    )
    parser.add_argument("--release-manifest", required=True, help="Stable manifest JSON path")
    parser.add_argument("--smoke-report", required=True, help="Read-only MCP smoke JSON path")
    parser.add_argument(
        "--container",
        required=True,
        help="Running Compose container name or ID; it is not recorded in the receipt",
    )
    parser.add_argument(
        "--docker-bin", default="docker", help="Docker executable (default: docker)"
    )
    parser.add_argument("--output", required=True, help="New receipt output path")
    return parser


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file():
        raise ReleaseManifestError(f"{label}_not_regular_file")
    raw = resolved.read_bytes()
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ReleaseManifestError(f"{label}_size_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(f"{label}_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ReleaseManifestError(f"{label}_not_object")
    return payload


def _docker_format(docker_bin: str, *, target: str, template: str, image: bool = False) -> str:
    command = [
        docker_bin,
        "image" if image else "container",
        "inspect",
        "--format",
        template,
        target,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ReleaseManifestError("server_deployment_receipt_docker_inspect_failed")
    value = result.stdout.strip()
    if not value or value == "<no value>":
        raise ReleaseManifestError("server_deployment_receipt_docker_value_missing")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    try:
        manifest = _load_json_object(Path(args.release_manifest), label="release_manifest")
        validate_release_manifest(manifest)
        smoke_report = _load_json_object(Path(args.smoke_report), label="smoke_report")
        running = _docker_format(
            args.docker_bin,
            target=args.container,
            template="{{.State.Running}}",
        )
        if running.lower() != "true":
            raise ReleaseManifestError("server_deployment_receipt_container_not_running")
        container_image = _docker_format(
            args.docker_bin,
            target=args.container,
            template="{{.Config.Image}}",
        )
        image_revision = _docker_format(
            args.docker_bin,
            target=container_image,
            template='{{index .Config.Labels "org.opencontainers.image.revision"}}',
            image=True,
        )
        receipt = build_server_deployment_receipt(
            release_manifest=manifest,
            container_image=container_image,
            image_revision=image_revision,
            smoke_report=smoke_report,
        )
        output = Path(args.output)
        write_server_deployment_receipt(output, receipt)
    except (OSError, ReleaseManifestError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
