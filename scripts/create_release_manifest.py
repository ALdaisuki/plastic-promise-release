#!/usr/bin/env python3
"""Create one non-secret, immutable-release evidence manifest from CI artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Keep the script runnable from a source checkout before the project is installed.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from plastic_promise.release_manifest import (  # noqa: E402
    ReleaseManifestError,
    build_release_manifest,
    write_release_manifest,
)


def _image_reference(value: str) -> tuple[str, str]:
    name, separator, reference = value.partition("=")
    if separator != "=" or not name or not reference:
        raise argparse.ArgumentTypeError("image must use NAME=repository@sha256:<digest>")
    return name, reference


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-version", required=True, help="SemVer tag such as v1.2.3 or v1.2.3-rc.1"
    )
    parser.add_argument("--source-commit", required=True, help="Immutable source Git commit SHA")
    parser.add_argument(
        "--source-repository",
        default="https://github.com/ALdaisuki/plastic-promise",
        help="Public source repository URL",
    )
    parser.add_argument(
        "--dist-dir", type=Path, required=True, help="Directory containing one wheel and one sdist"
    )
    parser.add_argument("--sbom", type=Path, required=True, help="CycloneDX JSON SBOM")
    parser.add_argument(
        "--image",
        action="append",
        type=_image_reference,
        default=[],
        metavar="NAME=REFERENCE",
        help="Immutable OCI reference; provide server and inference-node exactly once",
    )
    parser.add_argument("--workflow-ref", required=True, help="Public GitHub Actions run URL")
    parser.add_argument("--output", type=Path, required=True, help="New output manifest path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    image_references = dict(args.image)
    if len(image_references) != len(args.image):
        raise SystemExit("release_manifest_image_duplicate")
    try:
        payload = build_release_manifest(
            release_version=args.release_version,
            source_repository=args.source_repository,
            source_commit=args.source_commit,
            dist_directory=args.dist_dir,
            sbom_path=args.sbom,
            image_references=image_references,
            workflow_ref=args.workflow_ref,
        )
        write_release_manifest(args.output, payload)
    except ReleaseManifestError as exc:
        raise SystemExit(str(exc)) from exc
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
