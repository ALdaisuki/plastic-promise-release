#!/usr/bin/env python3
"""Smoke test a live Plastic Promise Streamable HTTP MCP server.

This script verifies the actual HTTP MCP process at /mcp, not the Codex tool
surface. It is intended for release validation after the server is already
running.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:9020/mcp"
DEFAULT_HEALTH_URL = "http://127.0.0.1:9020/health"
DEFAULT_PROJECT_ID = "project:plastic-promise"
DEFAULT_DB_PATH = "data/db/plastic_memory.db"
DEFAULT_LANCEDB_PATH = "data/lancedb"


class SmokeFailure(RuntimeError):
    """Raised when a smoke assertion fails."""


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke test a live Plastic Promise Streamable HTTP MCP server."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"MCP URL (default: {DEFAULT_URL})")
    parser.add_argument(
        "--health-url",
        default=DEFAULT_HEALTH_URL,
        help=f"Health URL (default: {DEFAULT_HEALTH_URL})",
    )
    parser.add_argument("--expected-version", default=None, help="Expected /health version")
    parser.add_argument("--expected-mode", default=None, help="Expected runtime_mode.mode")
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--project-policy", default="balanced")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--sse-read-timeout", type=float, default=300.0)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--lancedb-path", default=DEFAULT_LANCEDB_PATH)
    parser.add_argument(
        "--check-summary-index",
        action="store_true",
        help="Verify SQLite raw canary and LanceDB compact text boundaries.",
    )
    parser.add_argument(
        "--summary-index-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for LanceDB smoke rows during --check-summary-index.",
    )
    parser.add_argument(
        "--summary-index-interval",
        type=float,
        default=0.5,
        help="Seconds between LanceDB row visibility checks.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON only.",
    )
    return parser


def now_marker() -> tuple[str, str]:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    marker = f"http_mcp_smoke_{timestamp}_{os.getpid()}"
    canary = f"RAW_SQL_ONLY_CANARY_{marker}"
    return marker, canary


def parse_mcp_json_content(content: list[Any], tool_name: str) -> dict[str, Any]:
    for item in content:
        item_type = getattr(item, "type", None)
        text = getattr(item, "text", None)
        if item_type is None and isinstance(item, dict):
            item_type = item.get("type")
            text = item.get("text")
        if item_type != "text" or text is None:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SmokeFailure(f"{tool_name} returned non-JSON text: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SmokeFailure(
                f"{tool_name} returned JSON {type(parsed).__name__}, expected object"
            )
        return parsed
    raise SmokeFailure(f"{tool_name} returned no text JSON content")


def pipeline_count(pipeline: dict[str, Any], left: str, right: str) -> int:
    expected = f"{left}->{right}"
    for key, value in pipeline.items():
        normalized = str(key)
        left_index = normalized.find(left)
        right_index = normalized.find(right)
        if normalized == expected or (left_index >= 0 and right_index > left_index):
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise SmokeFailure(
                    f"pipeline count for {key!r} is not an integer: {value!r}"
                ) from exc
    return 0


def validate_health(health: dict[str, Any], expected_version: str | None = None) -> dict[str, Any]:
    if health.get("status") != "ok":
        raise SmokeFailure(f"health status is not ok: {health!r}")
    if expected_version and health.get("version") != expected_version:
        raise SmokeFailure(
            f"health version mismatch: expected {expected_version}, got {health.get('version')}"
        )
    if not health.get("pid"):
        raise SmokeFailure("health response did not include pid")
    return {
        "status": health.get("status"),
        "version": health.get("version"),
        "pid": health.get("pid"),
        "uptime": health.get("uptime"),
    }


def validate_runtime(runtime: dict[str, Any], expected_mode: str | None = None) -> dict[str, Any]:
    mode = runtime.get("mode")
    if expected_mode and mode != expected_mode:
        raise SmokeFailure(f"runtime mode mismatch: expected {expected_mode}, got {mode}")
    return {
        "mode": mode,
        "label": runtime.get("label"),
        "rust_accelerated": runtime.get("rust_accelerated"),
    }


def validate_store(store: dict[str, Any]) -> dict[str, Any]:
    if store.get("stored") is not True:
        raise SmokeFailure(f"memory_store did not report stored=true: {store!r}")
    canonical_memory_id = str(store.get("memory_id") or "")
    submitted_memory_id = str(store.get("submitted_memory_id") or "")
    if not canonical_memory_id:
        raise SmokeFailure("memory_store did not return memory_id")
    if not submitted_memory_id:
        raise SmokeFailure("memory_store identity is missing submitted_memory_id")

    deduplicated = store.get("deduplicated")
    created = store.get("created")
    if not isinstance(deduplicated, bool) or not isinstance(created, bool):
        raise SmokeFailure("memory_store flags must be explicit booleans")
    if deduplicated == created:
        raise SmokeFailure(
            "memory_store flags are incoherent: exactly one of deduplicated/created must be true"
        )
    if created and canonical_memory_id != submitted_memory_id:
        raise SmokeFailure(
            "memory_store identity is incoherent: a created submission must be canonical"
        )
    if deduplicated and canonical_memory_id == submitted_memory_id:
        raise SmokeFailure(
            "memory_store identity is incoherent: a deduplicated submission must map to an "
            "existing canonical memory"
        )

    pipeline = store.get("pipeline", {})
    if not isinstance(pipeline, dict):
        raise SmokeFailure("memory_store pipeline is not an object")
    migrated = pipeline_count(pipeline, "embedded", "migrated")
    if migrated < 0:
        raise SmokeFailure(f"memory_store embedded->migrated count is {migrated}")
    if created and migrated <= 0:
        raise SmokeFailure(f"memory_store embedded->migrated count is {migrated}")
    return {
        "memory_id": canonical_memory_id,
        "canonical_memory_id": canonical_memory_id,
        "submitted_memory_id": submitted_memory_id,
        "project_id": store.get("project_id"),
        "pipeline": pipeline,
        "migrated": migrated,
        "deduplicated": deduplicated,
        "created": created,
    }


def _validate_retrieval_evidence(
    payload: dict[str, Any],
    expected_memory_ids: list[str],
) -> dict[str, Any]:
    surfaces: list[tuple[str, Any]] = [
        ("core", payload.get("core")),
        ("related", payload.get("related")),
        ("divergent", payload.get("divergent")),
    ]
    expected = list(dict.fromkeys(str(memory_id) for memory_id in expected_memory_ids if memory_id))
    if not expected:
        raise SmokeFailure("retrieval validation requires at least one stored memory id")

    evidence_locations: dict[str, list[str]] = {memory_id: [] for memory_id in expected}
    for location, rows in surfaces:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            memory_id = str(row.get("id") or "")
            if memory_id not in evidence_locations or not str(row.get("content") or "").strip():
                continue
            if location not in evidence_locations[memory_id]:
                evidence_locations[memory_id].append(location)

    for memory_id, locations in evidence_locations.items():
        if not locations:
            raise SmokeFailure(
                f"retrieval did not expose stored memory {memory_id} as context evidence"
            )
    return {
        "observed_memory_ids": expected,
        "evidence_locations": evidence_locations,
    }


def validate_recall(
    recall: dict[str, Any], expected_memory_ids: list[str]
) -> dict[str, Any]:
    if recall.get("success") is False:
        raise SmokeFailure("memory_recall reported success=false")
    if recall.get("degraded") is True:
        raise SmokeFailure(f"memory_recall degraded: {recall.get('warnings')}")
    audit = recall.get("audit") or {}
    evidence = _validate_retrieval_evidence(recall, expected_memory_ids)
    return {
        "success": recall.get("success", True),
        "degraded": recall.get("degraded", False),
        "engine_mode": audit.get("engine_mode"),
        "engine_version": audit.get("engine_version"),
        "related_count": len(recall.get("related", [])),
        **evidence,
    }


def validate_context(
    context: dict[str, Any], expected_memory_ids: list[str]
) -> dict[str, Any]:
    project_context = context.get("project_context") or {}
    if context.get("degraded") is True:
        raise SmokeFailure(f"context_supply degraded: {context.get('warnings')}")
    if project_context.get("degraded") is True:
        raise SmokeFailure(f"context project degraded: {project_context.get('warnings')}")
    audit = context.get("audit_metadata") or {}
    evidence = _validate_retrieval_evidence(context, expected_memory_ids)
    return {
        "degraded": context.get("degraded", False),
        "project_degraded": project_context.get("degraded", False),
        "engine_mode": audit.get("engine_mode"),
        "engine_version": audit.get("engine_version"),
        "related_count": len(context.get("related", [])),
        **evidence,
    }


def resolve_existing_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.exists() or candidate.is_absolute():
        return candidate
    for parent in [Path.cwd(), *Path.cwd().parents]:
        resolved = parent / candidate
        if resolved.exists():
            return resolved
    return candidate


def resolve_lancedb_path(path: str | Path) -> Path:
    candidate = Path(path)
    if (candidate / "memory_vectors.lance").exists() or candidate.is_absolute():
        return candidate
    for parent in [Path.cwd(), *Path.cwd().parents]:
        resolved = parent / candidate
        if (resolved / "memory_vectors.lance").exists():
            return resolved
    return candidate


def fetch_sqlite_smoke_rows(
    db_path: str | Path,
    marker: str,
    canonical_memory_id: str,
    submitted_memory_id: str,
) -> list[dict[str, Any]]:
    path = resolve_existing_path(db_path)
    if not path.exists():
        raise SmokeFailure(f"SQLite database not found: {path}")
    sqlite_uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(sqlite_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT id, content, raw_content, embedding_text, search_text, origin_ref "
            "FROM memories WHERE origin_ref = ? OR id IN (?, ?) "
            "ORDER BY created_at ASC, id ASC",
            (marker, canonical_memory_id, submitted_memory_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def validate_sqlite_summary_rows(
    rows: list[dict[str, Any]],
    marker: str,
    canary: str,
    store: dict[str, Any],
) -> dict[str, Any]:
    canonical_memory_id = str(store.get("canonical_memory_id") or "")
    submitted_memory_id = str(store.get("submitted_memory_id") or "")
    deduplicated = store.get("deduplicated")
    created = store.get("created")
    if not canonical_memory_id or not submitted_memory_id:
        raise SmokeFailure("SQLite summary-index check received incomplete store identity")
    if not isinstance(deduplicated, bool) or not isinstance(created, bool) or deduplicated == created:
        raise SmokeFailure("SQLite summary-index check received incoherent store flags")

    rows_by_id = {str(row.get("id") or ""): row for row in rows if row.get("id")}
    canonical_row = rows_by_id.get(canonical_memory_id)
    if canonical_row is None:
        raise SmokeFailure(f"SQLite missing canonical memory {canonical_memory_id}")

    marker_rows = [row for row in rows if str(row.get("origin_ref") or "") == marker]
    marker_memory_ids = [str(row.get("id") or "") for row in marker_rows]
    if created:
        if canonical_memory_id != submitted_memory_id:
            raise SmokeFailure("SQLite store identity is incoherent for a created submission")
        if canonical_memory_id not in marker_memory_ids:
            raise SmokeFailure("SQLite created canonical memory is not mapped to the smoke marker")
    else:
        if canonical_memory_id == submitted_memory_id:
            raise SmokeFailure("SQLite deduplicated identity did not map to an older canonical")
        if submitted_memory_id in rows_by_id:
            raise SmokeFailure(
                f"SQLite retained discarded submitted memory {submitted_memory_id}"
            )
        if canonical_memory_id in marker_memory_ids:
            raise SmokeFailure("SQLite deduplication overwrote canonical provenance")

    migrated = int(store.get("migrated", 0) or 0)
    if len(marker_rows) > migrated:
        raise SmokeFailure(
            "SQLite persisted more marker rows than memory_store reported as migrated"
        )

    bad_compact = []
    for row in rows:
        compact_text = "\n".join(
            (str(row.get("embedding_text") or ""), str(row.get("search_text") or ""))
        )
        if marker in compact_text or canary in compact_text:
            bad_compact.append(str(row.get("id") or ""))
    if bad_compact:
        raise SmokeFailure(
            f"SQLite compact index text contains raw identity for {bad_compact}"
        )

    raw_hits = []
    for row in marker_rows:
        raw_content = str(row.get("raw_content") or "")
        if marker not in raw_content or canary not in raw_content:
            raise SmokeFailure(
                f"SQLite raw_content did not retain raw identity for {row.get('id', '')}"
            )
        raw_hits.append(str(row.get("id") or ""))

    split_memory_ids = [
        memory_id for memory_id in marker_memory_ids if memory_id != canonical_memory_id
    ]
    retrieval_memory_ids = list(
        dict.fromkeys([canonical_memory_id, *marker_memory_ids])
    )
    return {
        "sqlite_row_count": len(rows),
        "sqlite_memory_ids": [str(row.get("id") or "") for row in rows],
        "sqlite_marker_memory_ids": marker_memory_ids,
        "sqlite_raw_canary_rows": raw_hits,
        "canonical_memory_id": canonical_memory_id,
        "submitted_memory_id": submitted_memory_id,
        "canonical_reused": deduplicated,
        "split_memory_ids": split_memory_ids,
        "retrieval_memory_ids": retrieval_memory_ids,
        "lancedb_memory_ids": retrieval_memory_ids,
    }


def fetch_lancedb_smoke_rows(
    lancedb_path: str | Path, memory_ids: list[str]
) -> list[dict[str, Any]]:
    try:
        import lancedb
    except Exception as exc:  # pragma: no cover - environment dependent
        raise SmokeFailure(f"lancedb import failed: {exc}") from exc

    path = resolve_lancedb_path(lancedb_path)
    if not path.exists():
        raise SmokeFailure(f"LanceDB path not found: {path}")
    db = lancedb.connect(str(path.resolve()))
    table = db.open_table("memory_vectors")
    arrow = table.to_arrow()
    if "memory_id" not in arrow.column_names or "text" not in arrow.column_names:
        raise SmokeFailure("LanceDB memory_vectors table lacks memory_id/text columns")
    id_set = set(memory_ids)
    rows: list[dict[str, Any]] = []
    memory_ids_col = arrow.column("memory_id").to_pylist()
    texts_col = arrow.column("text").to_pylist()
    for memory_id, text in zip(memory_ids_col, texts_col, strict=True):
        if str(memory_id) in id_set:
            rows.append({"memory_id": str(memory_id), "text": str(text or "")})
    return rows


def validate_lancedb_summary_rows(
    rows: list[dict[str, Any]], memory_ids: list[str], marker: str, canary: str
) -> dict[str, Any]:
    found = {row.get("memory_id") for row in rows}
    missing = sorted(set(memory_ids) - found)
    if missing:
        raise SmokeFailure(f"LanceDB missing smoke rows: {missing}")
    bad_rows = [row.get("memory_id", "") for row in rows if canary in str(row.get("text") or "")]
    if bad_rows:
        raise SmokeFailure(f"LanceDB text contains raw canary for {bad_rows}")
    marker_rows = [
        row.get("memory_id", "") for row in rows if marker in str(row.get("text") or "")
    ]
    if marker_rows:
        raise SmokeFailure(f"LanceDB compact text contains raw identity for {marker_rows}")
    return {
        "lancedb_row_count": len(rows),
        "lancedb_memory_ids": [memory_id for memory_id in memory_ids if memory_id in found],
    }


async def wait_for_lancedb_summary_rows(
    lancedb_path: str | Path,
    memory_ids: list[str],
    marker: str,
    canary: str,
    timeout_s: float,
    interval_s: float,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + max(timeout_s, 0.0)
    interval = max(interval_s, 0.1)
    attempts = 0

    while True:
        attempts += 1
        rows = fetch_lancedb_smoke_rows(lancedb_path, memory_ids)
        try:
            result = validate_lancedb_summary_rows(rows, memory_ids, marker, canary)
        except SmokeFailure as exc:
            if "LanceDB missing smoke rows" not in str(exc):
                raise
            if loop.time() >= deadline:
                raise
            await asyncio.sleep(interval)
            continue

        result["lancedb_attempts"] = attempts
        result["lancedb_wait_seconds"] = round(loop.time() - start, 3)
        return result


def build_smoke_content(marker: str, canary: str) -> str:
    return (
        "HTTP MCP release smoke verifies canonical storage and compact summary retrieval; "
        "L0 topic: HTTP MCP release smoke canonical storage; "
        "L1 summary: canonical storage stays searchable through the compact memory index; "
        f"raw provenance identity {marker} carries SQL-only canary {canary}"
    )


def build_retrieval_query() -> str:
    return "HTTP MCP release smoke canonical storage compact summary retrieval"


def _leaf_error(exc: BaseException) -> BaseException:
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple) and nested:
        return _leaf_error(nested[0])
    return exc


async def call_tool_json(session: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(name, arguments)
    return parse_mcp_json_content(list(result.content), name)


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    marker, canary = now_marker()
    report: dict[str, Any] = {
        "ok": False,
        "url": args.url,
        "health_url": args.health_url,
        "marker": marker,
        "canary": canary,
        "checks": {},
    }

    timeout = httpx.Timeout(args.timeout, read=args.sse_read_timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        health_response = await client.get(args.health_url)
        health_response.raise_for_status()
        health = validate_health(health_response.json(), args.expected_version)
        report["checks"]["health"] = health

        async with streamable_http_client(args.url, http_client=client) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                runtime = await call_tool_json(session, "runtime_mode", {"action": "get"})
                report["checks"]["runtime_mode"] = validate_runtime(runtime, args.expected_mode)

                content = build_smoke_content(marker, canary)
                store = await call_tool_json(
                    session,
                    "memory_store",
                    {
                        "content": content,
                        "memory_type": "experience",
                        "source": "codex_http_smoke_script",
                        "source_class": "experience",
                        "project_id": args.project_id,
                        "project_policy": args.project_policy,
                        "visibility": "project",
                        "tags": [
                            "release-smoke:http-mcp",
                            "release-smoke:summary-index",
                            f"marker:{marker}",
                        ],
                        "origin_kind": "http_mcp_smoke_script",
                        "origin_uri": args.url,
                        "origin_ref": marker,
                        "metadata_json": {
                            "marker": marker,
                            "canary": canary,
                            "summary_index_expected": bool(args.check_summary_index),
                        },
                    },
                )
                store_check = validate_store(store)
                report["checks"]["memory_store"] = store_check

                sqlite_check: dict[str, Any] | None = None
                retrieval_memory_ids = [store_check["canonical_memory_id"]]
                if args.check_summary_index:
                    sqlite_rows = fetch_sqlite_smoke_rows(
                        args.db_path,
                        marker,
                        store_check["canonical_memory_id"],
                        store_check["submitted_memory_id"],
                    )
                    sqlite_check = validate_sqlite_summary_rows(
                        sqlite_rows,
                        marker,
                        canary,
                        store_check,
                    )
                    retrieval_memory_ids = sqlite_check["retrieval_memory_ids"]

                recall = await call_tool_json(
                    session,
                    "memory_recall",
                    {
                        "query": build_retrieval_query(),
                        "task_type": "code_review",
                        "max_results": 20,
                        "debug": True,
                        "project_id": args.project_id,
                        "project_policy": args.project_policy,
                        "retrieval_mode": "hybrid",
                        "request_id": f"{marker}:recall",
                    },
                )
                report["checks"]["memory_recall"] = validate_recall(
                    recall,
                    retrieval_memory_ids,
                )

                context = await call_tool_json(
                    session,
                    "context_supply",
                    {
                        "task_description": build_retrieval_query(),
                        "task_type": "code_review",
                        "debug": True,
                        "project_id": args.project_id,
                        "project_policy": args.project_policy,
                        "retrieval_mode": "hybrid",
                        "request_id": f"{marker}:context",
                    },
                )
                report["checks"]["context_supply"] = validate_context(
                    context,
                    retrieval_memory_ids,
                )

    if args.check_summary_index:
        if sqlite_check is None:
            raise SmokeFailure("summary-index SQLite validation did not run")
        memory_ids = list(sqlite_check["lancedb_memory_ids"])
        lancedb_check = await wait_for_lancedb_summary_rows(
            args.lancedb_path,
            memory_ids,
            marker,
            canary,
            args.summary_index_timeout,
            args.summary_index_interval,
        )
        report["checks"]["summary_index"] = {**sqlite_check, **lancedb_check}

    report["ok"] = True
    return report


def print_human(report: dict[str, Any]) -> None:
    status = "PASS" if report.get("ok") else "FAIL"
    print(f"HTTP MCP smoke: {status}")
    print(f"  url: {report.get('url')}")
    print(f"  marker: {report.get('marker')}")
    for name, payload in report.get("checks", {}).items():
        print(f"  {name}: {json.dumps(payload, ensure_ascii=False)}")


async def async_main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    try:
        report = await run_smoke(args)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_human(report)
        return 0
    except Exception as exc:
        cause = _leaf_error(exc)
        report = {
            "ok": False,
            "error_class": cause.__class__.__name__,
            "error": str(cause),
            "url": getattr(args, "url", DEFAULT_URL),
            "health_url": getattr(args, "health_url", DEFAULT_HEALTH_URL),
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_human(report)
            print(f"  error: {cause.__class__.__name__}: {cause}", file=sys.stderr)
        return 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
