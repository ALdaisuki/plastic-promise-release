"""CLI entry points for plastic-promise commands."""

import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(prog="plastic-promise")
    sub = parser.add_subparsers(dest="command")

    # market subcommands
    market = sub.add_parser("market", help="Plugin market operations")
    market_sub = market.add_subparsers(dest="market_command")

    market_sub.add_parser("list", help="List available packs")
    install = market_sub.add_parser("install", help="Install a pack")
    install.add_argument("name", help="Pack name or GitHub URL")
    upgrade = market_sub.add_parser("upgrade", help="Upgrade an installed pack")
    upgrade.add_argument("name", help="Pack name")
    remove = market_sub.add_parser("remove", help="Remove an installed pack")
    remove.add_argument("name", help="Pack name")
    market_sub.add_parser("status", help="Show plugin status")
    enable = market_sub.add_parser("enable", help="Enable a disabled plugin")
    enable.add_argument("name", help="Pack name")
    disable = market_sub.add_parser("disable", help="Disable a plugin")
    disable.add_argument("name", help="Pack name")

    # start subcommand
    start = sub.add_parser("start", help="Start Plastic Promise services")
    start.add_argument("--skip-ollama-check", action="store_true", help="Skip Ollama check")

    # deploy subcommand: a thin forwarder to the isolated deployment controller.
    deploy = sub.add_parser("deploy", help="Plan or administer a local deployment state root")
    deploy.add_argument("deployment_args", nargs=argparse.REMAINDER)
    module = sub.add_parser(
        "module", help="Manage deployment modules through the deploy controller"
    )
    module.add_argument("deployment_args", nargs=argparse.REMAINDER)

    # knowledge subcommands
    knowledge = sub.add_parser("knowledge", help="Knowledge truth store operations")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command")
    schema = knowledge_sub.add_parser("schema", help="Inspect or repair the knowledge schema")
    schema.add_argument(
        "--check", action="store_true", help="Report schema health without mutation"
    )
    schema.add_argument(
        "--db", default=None, help="Knowledge database path (defaults to PP_KNOWLEDGE_DB_PATH)"
    )
    migrate = knowledge_sub.add_parser("migrate", help="Show the idempotent migration plan")
    migrate.add_argument(
        "--dry-run", action="store_true", help="Print DDL statements without executing"
    )
    migrate.add_argument("--db", default=None, help="Knowledge database path")
    ingest = knowledge_sub.add_parser(
        "ingest-smoke", help="Deterministic smoke ingestion of Markdown/text"
    )
    ingest.add_argument("--project", required=True, help="Canonical project id")
    ingest.add_argument("--source", required=True, help="Source name")
    ingest.add_argument("--space", default="default", help="Knowledge space name")
    ingest.add_argument("--file", default=None, help="Path to a Markdown/text file")
    ingest.add_argument("--text", default=None, help="Inline Markdown/text content")
    ingest.add_argument("--db", default=None, help="Knowledge database path")
    ingest.add_argument("--blob-root", default=None, help="Blob root directory")
    query = knowledge_sub.add_parser("query", help="Lexical knowledge search with citations")
    query.add_argument("--project", required=True, help="Canonical project id")
    query.add_argument("--query", required=True, help="Search query text")
    query.add_argument("--space", default=None, help="Restrict to a knowledge space id")
    query.add_argument("--limit", type=int, default=10, help="Max hits")
    query.add_argument("--include-stale", action="store_true", help="Include stale lifecycle rows")
    query.add_argument("--db", default=None, help="Knowledge database path")
    jobs = knowledge_sub.add_parser("jobs", help="List ingestion jobs")
    jobs.add_argument("--project", required=True, help="Canonical project id")
    jobs.add_argument("--status", default=None, help="Filter by job status")
    jobs.add_argument("--limit", type=int, default=50, help="Max rows")
    jobs.add_argument("--db", default=None, help="Knowledge database path")
    backup = knowledge_sub.add_parser(
        "backup", help="Create backup evidence for the knowledge database"
    )
    backup.add_argument("--target", required=True, help="Backup target path")
    backup.add_argument("--db", default=None, help="Knowledge database path")
    restore_smoke = knowledge_sub.add_parser(
        "restore-smoke",
        help="Isolated restore drill: integrity + blob hashes + lexical smoke",
    )
    restore_smoke.add_argument(
        "--backup",
        required=True,
        help="Knowledge backup database path to restore",
    )
    restore_smoke.add_argument(
        "--blob-root", default=None, help="Blob root used for hash verification"
    )
    restore_smoke.add_argument(
        "--project",
        default="project:plastic-promise",
        help="Project scope for the lexical smoke query",
    )
    restore_smoke.add_argument(
        "--query", default="", help="Probe query (defaults to the first chunk text)"
    )
    restore_smoke.add_argument(
        "--keep", action="store_true", help="Keep the isolated restored directory"
    )
    semantic_status = knowledge_sub.add_parser(
        "semantic-status",
        help="Knowledge semantic compilation status (read-only)",
    )
    semantic_status.add_argument("--project", required=True, help="Canonical project id")
    semantic_status.add_argument("--db", default=None, help="Knowledge database path")
    domains = knowledge_sub.add_parser(
        "domains",
        help="List knowledge domains (read-only)",
    )
    domains.add_argument("--project", required=True, help="Canonical project id")
    domains.add_argument("--db", default=None, help="Knowledge database path")
    artifacts = knowledge_sub.add_parser(
        "artifacts",
        help="List Wiki artifacts (read-only)",
    )
    artifacts.add_argument("--project", required=True, help="Canonical project id")
    artifacts.add_argument("--db", default=None, help="Knowledge database path")

    args = parser.parse_args()

    if args.command == "market":
        _handle_market(args)
    elif args.command == "start":
        _handle_start(args)
    elif args.command == "knowledge":
        _handle_knowledge(args)
    elif args.command in {"deploy", "module"}:
        from plastic_promise.deployment.cli import main as deployment_main

        deployment_args = (
            args.deployment_args if args.command == "deploy" else ["module", *args.deployment_args]
        )
        raise SystemExit(deployment_main(deployment_args))
    else:
        parser.print_help()
        sys.exit(1)


def _handle_market(args):
    """Route market subcommands to MCP tool handlers."""
    from plastic_promise.mcp.tools.market import (
        handle_market_disable,
        handle_market_enable,
        handle_market_install,
        handle_market_list,
        handle_market_remove,
        handle_market_status,
        handle_market_upgrade,
    )

    async def _run():
        cmd = args.market_command
        if cmd == "list":
            return await handle_market_list(None, {"upgradable": False})
        elif cmd == "install":
            return await handle_market_install(None, {"name": args.name})
        elif cmd == "upgrade":
            return await handle_market_upgrade(None, {"name": args.name})
        elif cmd == "remove":
            return await handle_market_remove(None, {"name": args.name})
        elif cmd == "status":
            return await handle_market_status(None, {})
        elif cmd == "enable":
            return await handle_market_enable(None, {"name": args.name})
        elif cmd == "disable":
            return await handle_market_disable(None, {"name": args.name})
        else:
            print(
                "Unknown market command. Try: list, install, upgrade, remove, status, enable, disable"
            )
            return []

    results = asyncio.run(_run())
    for r in results:
        print(r.text)


def _handle_start(args):
    """Start Plastic Promise services."""
    skip_ollama = getattr(args, "skip_ollama_check", False)
    print(f"Starting Plastic Promise... (skip_ollama_check={skip_ollama})")
    # Delegate to existing init_and_start script
    import subprocess

    cmd = [sys.executable, "scripts/init_and_start.py"]
    if skip_ollama:
        cmd.append("--skip-ollama-check")
    subprocess.run(cmd)


if __name__ == "__main__":
    main()


def _handle_knowledge(args):
    """Route knowledge subcommands to the knowledge truth store."""
    import json as _json
    from pathlib import Path

    from plastic_promise.knowledge.blobs import FilesystemBlobStore
    from plastic_promise.knowledge.contracts import knowledge_blob_root, knowledge_db_path
    from plastic_promise.knowledge.ingestion import IngestCoordinator
    from plastic_promise.knowledge.migrations import backup_evidence, migrate_dry_run, schema_check
    from plastic_promise.knowledge.query import LexicalKnowledgeQuery
    from plastic_promise.knowledge.repository import KnowledgeRepository

    command = args.knowledge_command

    if command == "restore-smoke":
        # The restore drill only touches the backup file; it must not read or
        # initialize the live knowledge store.
        from plastic_promise.knowledge.restore import restore_smoke_evidence

        evidence = restore_smoke_evidence(
            args.backup,
            blob_root=args.blob_root,
            project_id=args.project,
            probe=args.query,
            keep=args.keep,
        )
        print(_json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        if not evidence.get("ok"):
            raise SystemExit(1)
        return

    db = args.db or str(knowledge_db_path())
    repository = KnowledgeRepository(db)
    repository.init_schema()

    if command == "semantic-status":
        from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

        coordinator = KnowledgeSemanticCoordinator(repository)
        print(_json.dumps(coordinator.status(args.project), ensure_ascii=False, indent=2))
        return

    if command == "domains":
        print(_json.dumps(repository.list_domains(args.project), ensure_ascii=False, indent=2))
        return

    if command == "artifacts":
        print(_json.dumps(repository.list_artifacts(args.project), ensure_ascii=False, indent=2))
        return

    if command == "schema":
        if not args.check:
            print("schema: use --check (mutation requires explicit authorization)")
            return
        print(_json.dumps(schema_check(db), ensure_ascii=False, indent=2, default=str))
        return

    if command == "migrate":
        if not args.dry_run:
            print("migrate: use --dry-run (real migration requires explicit authorization)")
            return
        plan = migrate_dry_run(db)
        print(
            _json.dumps(
                {
                    "database": plan["database"],
                    "statement_count": plan["statement_count"],
                    "note": plan["note"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        for statement in plan["statements"]:
            print(statement.strip())
        return

    if command == "ingest-smoke":
        if bool(args.file) == bool(args.text):
            print("ingest-smoke: provide exactly one of --file or --text")
            raise SystemExit(2)
        content = Path(args.file).read_bytes() if args.file else args.text.encode("utf-8")
        blob_root = args.blob_root or str(knowledge_blob_root())
        blobs = FilesystemBlobStore(blob_root)
        coordinator = IngestCoordinator(repository, blobs, actor="cli")
        submission = coordinator.submit_source(
            args.project,
            content,
            source_name=args.source,
            space_name=args.space,
            actor="cli",
        )
        print(
            _json.dumps(
                {
                    "submission": {
                        "job_id": submission.job_id,
                        "source_id": submission.source_id,
                        "reused_version_id": submission.reused_version_id,
                        "status": submission.status,
                    },
                    "jobs": [
                        _job_json(job) for job in coordinator.list_jobs(args.project, limit=5)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if command == "query":
        engine = LexicalKnowledgeQuery(repository)
        result = engine.search(
            args.project,
            args.query,
            space_id=args.space,
            limit=args.limit,
            include_stale=args.include_stale,
        )
        print(
            _json.dumps(
                {
                    "query": result.query,
                    "project_id": result.project_id,
                    "total_hits": result.total_hits,
                    "elapsed_ms": result.elapsed_ms,
                    "gates": list(result.gates),
                    "hits": [
                        {
                            "chunk_id": hit.chunk_id,
                            "source_id": hit.source_id,
                            "source_name": hit.source_name,
                            "version_no": hit.version_no,
                            "header_path": list(hit.header_path),
                            "score": round(hit.score, 3),
                            "snippet": hit.snippet,
                        }
                        for hit in result.hits
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if command == "jobs":
        jobs = repository.list_jobs(args.project, status=args.status, limit=args.limit)
        print(_json.dumps([_job_json(job) for job in jobs], ensure_ascii=False, indent=2))
        return

    if command == "backup":
        evidence = backup_evidence(db, args.target)
        print(_json.dumps(evidence, ensure_ascii=False, indent=2))
        return


def _job_json(job) -> dict:
    return {
        "id": job.id,
        "source_id": job.source_id,
        "project_id": job.project_id,
        "stage": job.stage,
        "status": job.status,
        "attempts": job.attempts,
        "error": job.error,
        "result": job.result_json,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
