"""Emit the daily local-node cache cleanup plan without deleting anything."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .cache_policy import load_cache_manifest, load_cleanup_conditions, plan_cache_cleanup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument(
        "--now",
        help="RFC 3339 timestamp for a deterministic dry-run; defaults to the current UTC time.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        now = _parse_now(args.now)
        manifest = load_cache_manifest(args.manifest)
        conditions = load_cleanup_conditions(args.status, now=now)
        plan = plan_cache_cleanup(
            manifest.entries,
            active_revision=manifest.active_revision,
            fallback_revision=manifest.fallback_revision,
            conditions=conditions,
        )
    except ValueError as exc:
        print(f"local inference cache plan failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "run_at_local_time": plan.run_at_local_time,
                "eligible_paths": list(plan.eligible_paths),
                "skipped_reason": plan.skipped_reason,
            },
            sort_keys=True,
        )
    )
    return 0


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("node_cache_planner_now_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("node_cache_planner_now_timezone_required")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
