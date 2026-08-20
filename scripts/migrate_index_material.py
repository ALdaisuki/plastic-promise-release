"""Inspect or explicitly normalize canonical SQLite index material."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plastic_promise.core.index_material_migration import (  # noqa: E402
    IndexMaterialMigrationError,
    apply_migration,
    configured_environment,
    inspect_database,
)


def _configured_target_model_identity() -> str | None:
    """Resolve the active derived-index identity, including governed nodes.

    A governed server intentionally keeps the compute model out of the
    legacy ``EMBEDDER_*`` environment variables.  The shared resolver binds
    migration and generation rebuilds to one active control-plane identity
    instead of silently falling back to ``fallback-zero``.
    """

    from plastic_promise.core.embedding_index_identity import (
        configured_embedding_index_identity,
    )

    return configured_embedding_index_identity()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing canonical SQLite database")
    parser.add_argument(
        "--environment-file",
        action="append",
        default=[],
        help="Generated EnvironmentFile; later files override earlier files",
    )
    parser.add_argument(
        "--target-policy",
        choices=("legacy", "compact-v2"),
        required=True,
        help="Single persisted policy required by the new generation",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create a verified online backup and apply the migration",
    )
    parser.add_argument(
        "--allow-unresolved-index-outbox",
        action="store_true",
        help=(
            "Explicit recovery mode: allow pending, blocked, or failed index jobs; "
            "processing jobs remain forbidden"
        ),
    )
    parser.add_argument("--backup-dir", help="Existing backup directory required with --apply")
    parser.add_argument("--expect-row-count", type=int)
    parser.add_argument("--expect-source-fingerprint")
    parser.add_argument("--expect-target-model-sha256")
    parser.add_argument("--expect-index-outbox-watermark", type=int)
    parser.add_argument("--expect-index-outbox-immutable-digest")
    parser.add_argument("--expect-index-outbox-job-count", type=int)
    parser.add_argument("--expect-index-outbox-active-count", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        with configured_environment(arguments.environment_file):
            target_model_name = _configured_target_model_identity()
            if arguments.apply:
                if (
                    not arguments.backup_dir
                    or arguments.expect_row_count is None
                    or not arguments.expect_source_fingerprint
                    or not arguments.expect_target_model_sha256
                ):
                    raise IndexMaterialMigrationError("apply_expectations_required")
                report = apply_migration(
                    arguments.db,
                    backup_directory=arguments.backup_dir,
                    target_policy=arguments.target_policy,
                    expected_row_count=arguments.expect_row_count,
                    expected_source_fingerprint=arguments.expect_source_fingerprint,
                    expected_target_model_sha256=arguments.expect_target_model_sha256,
                    allow_unresolved_index_outbox=arguments.allow_unresolved_index_outbox,
                    expected_index_outbox_watermark=arguments.expect_index_outbox_watermark,
                    expected_index_outbox_immutable_digest=(
                        arguments.expect_index_outbox_immutable_digest
                    ),
                    expected_index_outbox_job_count=arguments.expect_index_outbox_job_count,
                    expected_index_outbox_active_count=arguments.expect_index_outbox_active_count,
                    target_model_name=target_model_name,
                )
            else:
                plan = inspect_database(
                    arguments.db,
                    target_policy=arguments.target_policy,
                    allow_unresolved_index_outbox=arguments.allow_unresolved_index_outbox,
                    target_model_name=target_model_name,
                )
                report = {"applied": False, **plan.public_report()}
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
        return 0
    except (IndexMaterialMigrationError, OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"index material migration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
