"""Bootstrap, inspect, or verify a generation-bound writable LanceDB live view."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plastic_promise.core.generation_live_index import (  # noqa: E402
    GenerationLiveIndexError,
    bootstrap_generation_live_index,
    inspect_generation_live_index,
    resolve_generation_live_index,
)
from plastic_promise.core.lancedb_generation import GenerationError  # noqa: E402

GenerationResolver = Callable[[Path], tuple[object, Path, str]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="inspect bounded live-view binding metadata")
    bootstrap = commands.add_parser(
        "bootstrap",
        help="copy the verified current generation into a new live root",
    )
    bootstrap.add_argument("--generation-root", type=Path, required=True)
    verify = commands.add_parser(
        "verify",
        help="verify that the live root is bound to the current generation",
    )
    verify.add_argument("--generation-root", type=Path, required=True)
    return parser


def _resolve_current_generation(generation_root: Path) -> tuple[object, Path, str]:
    from plastic_promise.core.lancedb_artifact import verify_lancedb_artifact
    from plastic_promise.core.lancedb_generation import GenerationManager

    with GenerationManager(
        generation_root,
        create=False,
        artifact_verifier=verify_lancedb_artifact,
    ) as manager:
        manifest, index_path, selection_identity = manager.resolve_verified_current_selection()
    if manifest is None:
        raise GenerationError("current_generation_unavailable")
    return manifest, index_path, selection_identity


def main(
    argv: list[str] | None = None,
    *,
    generation_resolver: GenerationResolver | None = None,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "inspect":
            payload = {
                "status": "inspected",
                "binding": inspect_generation_live_index(arguments.live_root),
            }
        elif arguments.command == "bootstrap":
            resolver = generation_resolver or _resolve_current_generation
            manifest, index_path, selection_identity = resolver(arguments.generation_root)
            bootstrap_generation_live_index(
                base_index_path=index_path,
                base_manifest=manifest,
                base_selection_identity=selection_identity,
                live_root=arguments.live_root,
            )
            payload = {
                "status": "bootstrapped",
                "binding": inspect_generation_live_index(arguments.live_root),
            }
        elif arguments.command == "verify":
            resolver = generation_resolver or _resolve_current_generation
            manifest, _index_path, selection_identity = resolver(arguments.generation_root)
            resolve_generation_live_index(
                arguments.live_root,
                manifest,
                base_selection_identity=selection_identity,
            )
            payload = {
                "status": "verified",
                "binding": inspect_generation_live_index(arguments.live_root),
            }
        else:  # pragma: no cover - argparse owns the command domain.
            parser.error("unsupported command")
    except (GenerationError, GenerationLiveIndexError, OSError, ValueError) as exc:
        print(f"generation live index management failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
