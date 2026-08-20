#!/usr/bin/env python3
"""Fail when the deployment-contract package imports runtime layers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from plastic_promise.deployment.layering import check_deployment_layering

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    violations = check_deployment_layering(args.repo_root)
    print(
        json.dumps(
            [
                {
                    "path": str(item.path),
                    "line": item.line,
                    "kind": item.kind,
                    "target": item.target,
                }
                for item in violations
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
