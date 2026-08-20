#!/usr/bin/env python3
"""Run the side-effect-free container recipe preflight used by CI and builders."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=_SOURCE_ROOT,
        help="repository containing deploy/ and .dockerignore",
    )
    parser.add_argument("--output", type=Path, help="optional JSON receipt destination")
    return parser.parse_args()


def main() -> int:
    from plastic_promise.deployment import StaticRecipePolicyValidator

    args = _arguments()
    receipt = StaticRecipePolicyValidator(args.repository_root.resolve()).validate()
    payload = json.dumps(receipt.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - command-line entry point
    raise SystemExit(main())
