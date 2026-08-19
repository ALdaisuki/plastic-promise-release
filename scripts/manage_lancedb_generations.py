"""Inspect, verify, and switch prebuilt generations without opening LanceDB.

Candidate verification, promotion, and rollback lazily import the repository's
open-only LanceDB artifact verifier. Inspection does not import LanceDB. Tests
and embedding applications may inject an equivalent verifier directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plastic_promise.core.lancedb_generation import (  # noqa: E402
    ArtifactVerifier,
    GenerationError,
    GenerationManager,
    GenerationSpec,
)


def _load_default_artifact_verifier() -> ArtifactVerifier:
    try:
        from plastic_promise.core.lancedb_artifact import verify_lancedb_artifact
    except ImportError as exc:
        raise GenerationError("artifact_verifier_unavailable") from exc
    return verify_lancedb_artifact


def _validate_runtime_environment(spec: GenerationSpec, report: Mapping[str, object]) -> None:
    """Recheck evidence under the same environment intended for the MCP unit."""

    from scripts.rebuild_lancedb import _assert_quality_report_runtime_environment

    _assert_quality_report_runtime_environment(report)
    if spec.embedding_index_identity is None:
        return
    backend = report.get("backend")
    provider = backend.get("provider") if isinstance(backend, Mapping) else None
    if provider == "governed-node":
        # Governed nodes own the stable index identity.  The process-level
        # legacy model helper may decorate EMB_MODEL with local chunking
        # settings and is not authoritative for a routed node.
        from plastic_promise.core.embedding_index_identity import (
            configured_embedding_index_identity,
        )

        observed_identity = configured_embedding_index_identity()
    else:
        from plastic_promise.core.memory_index import effective_embedding_model_name

        observed_identity = effective_embedding_model_name()
    if observed_identity != spec.embedding_index_identity:
        raise ValueError("runtime_embedding_index_identity_not_current")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or verify prebuilt generations, or switch them when an "
            "embedding application injects an offline artifact verifier"
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Generation root containing generations/ and the current symlink",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser(
        "inspect", help="Inspect manifests without opening LanceDB"
    )
    inspect_parser.add_argument("generation_id", nargs="?")

    verify_parser = commands.add_parser(
        "verify-candidate",
        help="verify an inactive generation without switching the current pointer",
    )
    verify_parser.add_argument("generation_id")
    verify_parser.add_argument(
        "--db",
        type=Path,
        help="canonical SQLite database (required for source-bound generations)",
    )
    verify_parser.add_argument(
        "--embedding-index-identity",
        help="exact staged embedding index identity expected in the manifest",
    )

    promote_parser = commands.add_parser(
        "promote",
        help="verify and switch a generation (requires an injected artifact verifier)",
    )
    promote_parser.add_argument("generation_id")
    promote_parser.add_argument(
        "--db",
        type=Path,
        help="canonical SQLite database (required for source-bound generations)",
    )

    rollback_parser = commands.add_parser(
        "rollback",
        help="switch to a verified generation (requires an injected artifact verifier)",
    )
    rollback_parser.add_argument("generation_id")
    rollback_parser.add_argument(
        "--db",
        type=Path,
        help="canonical SQLite database (required for source-bound generations)",
    )
    reconcile_parser = commands.add_parser(
        "reconcile",
        help="Atomically reconcile a generation's SQLite index outbox watermark",
    )
    reconcile_parser.add_argument("generation_id")
    reconcile_parser.add_argument("--db", type=Path, required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    artifact_verifier: ArtifactVerifier | None = None,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    connection = None
    try:
        if arguments.command not in {"inspect", "reconcile"} and artifact_verifier is None:
            artifact_verifier = _load_default_artifact_verifier()
        with GenerationManager(
            arguments.root,
            create=False,
            artifact_verifier=artifact_verifier,
            runtime_environment_validator=_validate_runtime_environment,
        ) as manager:
            if arguments.command == "inspect":
                if arguments.generation_id:
                    payload = manager.load_manifest(arguments.generation_id).to_dict()
                else:
                    current = manager.current_manifest()
                    payload = {
                        "root": str(manager.root),
                        "current": current.to_dict() if current is not None else None,
                        "generations": [
                            manifest.to_dict() for manifest in manager.list_manifests()
                        ],
                    }
            elif arguments.command == "verify-candidate":
                if arguments.db is not None:
                    connection = _open_database(arguments.db)
                payload = manager.verify_candidate(
                    arguments.generation_id,
                    connection=connection,
                    expected_embedding_index_identity=arguments.embedding_index_identity,
                ).to_dict()
            elif arguments.command == "promote":
                if arguments.db is not None:
                    connection = _open_database(arguments.db)
                payload = manager.promote(
                    arguments.generation_id,
                    connection=connection,
                ).to_dict()
            elif arguments.command == "rollback":
                if arguments.db is not None:
                    connection = _open_database(arguments.db)
                payload = manager.rollback(
                    arguments.generation_id,
                    connection=connection,
                ).to_dict()
            elif arguments.command == "reconcile":
                manifest = manager.load_manifest(arguments.generation_id)
                if manifest.index_outbox is None:
                    raise GenerationError("generation_has_no_outbox_evidence")
                connection = _open_database(arguments.db)
                from plastic_promise.core.index_outbox_reconciliation import (
                    reconcile_index_outbox,
                )

                receipt = reconcile_index_outbox(
                    connection,
                    generation_id=manifest.generation_id,
                    manifest_hash=manifest.manifest_sha256,
                    evidence=manifest.index_outbox,
                )
                payload = manager.mark_reconciled(
                    arguments.generation_id,
                    receipt,
                    connection=connection,
                ).to_dict()
            else:  # pragma: no cover - argparse owns the command domain.
                parser.error("unsupported command")
    except (GenerationError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"generation management failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def _open_database(db_path: Path) -> sqlite3.Connection:
    db_path = db_path.expanduser()
    if db_path.is_symlink() or not db_path.is_file():
        raise GenerationError("database_must_be_existing_regular_file")
    resolved_db = db_path.resolve(strict=True)
    configured_db = os.environ.get("PLASTIC_DB_PATH", "").strip()
    if configured_db:
        configured_path = Path(configured_db).expanduser().resolve(strict=True)
        if configured_path != resolved_db:
            raise GenerationError("database_must_match_plastic_db_path")
    connection = sqlite3.connect(
        f"{resolved_db.as_uri()}?mode=rw",
        uri=True,
        timeout=5,
    )
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


if __name__ == "__main__":
    raise SystemExit(main())
