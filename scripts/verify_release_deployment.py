#!/usr/bin/env python3
"""Run the source-only deployment proof used by RC release verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# A source distribution executes this script directly from ``scripts/``.  Keep
# the project root ahead of an unrelated globally installed package so the
# proof validates the exact unpacked release material.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plastic_promise.release_readiness import (  # noqa: E402
    ReleaseReadinessError,
    verify_source_only_deployment,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verify-release-deployment")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_source_only_deployment(
            manifest_path=args.manifest,
            state_root=args.state_root,
        )
    except (OSError, ValueError, ReleaseReadinessError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the script entry point.
    raise SystemExit(main())
