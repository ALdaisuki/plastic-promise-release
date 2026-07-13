#!/usr/bin/env python3
"""Prove checked index recovery across real process restarts."""

# Script imports intentionally follow project-root bootstrapping.
# ruff: noqa: E402, SIM117

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plastic_promise.launcher.service_manager import (
    canonical_source_root,
    resolve_source_revision,
    validate_mcp_health_identity,
)
from scripts.http_mcp_harness import (
    ManagedProcess,
    file_sha256,
    free_tcp_port,
    process_environment,
    runtime_python,
    wait_for_health,
    wait_for_port_closed,
)
from scripts.smoke_http_mcp import call_tool_json


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def _job_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in ("tool_name", "project_id", "call_id")) + (
        row.get("payload"),
        row.get("metadata"),
    )


def _valid_outbox_transition(
    section: Any,
    *,
    tool_name: str,
    job_schema: str,
    action: str,
    project_id: str,
    memory_ids: set[str],
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(section, dict):
        return None
    before = section.get("before")
    after = section.get("after")
    if not isinstance(before, list) or not isinstance(after, list):
        return None
    pending: dict[str, dict[str, Any]] = {}
    for row in before:
        if not isinstance(row, dict) or row.get("status") != "pending":
            continue
        payload = row.get("payload")
        metadata = row.get("metadata")
        outbox_id = str(row.get("outbox_id") or "")
        if (
            not outbox_id
            or row.get("tool_name") != tool_name
            or row.get("project_id") != project_id
            or not isinstance(row.get("call_id"), str)
            or not row["call_id"]
            or not isinstance(payload, dict)
            or not isinstance(metadata, dict)
            or metadata.get("job_schema") != job_schema
            or payload.get("action") != action
            or str(payload.get("memory_id") or "") not in memory_ids
            or type(row.get("attempt_count")) is not int
            or row["attempt_count"] < 1
            or row.get("error_class") != "InjectedIndexFailure"
        ):
            return None
        if tool_name == "memory_index":
            material = str(payload.get("expected_embedding_hash") or "")
            if (
                set(payload)
                != {
                    "action",
                    "expected_embedding_hash",
                    "material_revision",
                    "memory_id",
                    "memory_version",
                    "project_id",
                }
                or payload.get("project_id") != project_id
                or payload.get("material_revision") != material
                or not material
                or type(payload.get("memory_version")) is not int
                or payload["memory_version"] < 0
            ):
                return None
        elif (
            set(payload) != {"action", "memory_id", "revision"}
            or type(payload.get("revision")) is not int
            or payload["revision"] < 1
        ):
            return None
        pending[outbox_id] = row
    if not pending:
        return None
    completed = {str(row.get("outbox_id") or ""): row for row in after if isinstance(row, dict)}
    for outbox_id, before_row in pending.items():
        after_row = completed.get(outbox_id)
        if (
            not isinstance(after_row, dict)
            or after_row.get("status") != "done"
            or type(after_row.get("attempt_count")) is not int
            or after_row["attempt_count"] < before_row["attempt_count"]
            or after_row.get("error_class") != ""
            or _job_signature(after_row) != _job_signature(before_row)
        ):
            return None
    return pending


_REPLAY_REPORT_RE = re.compile(
    r"^ReplayReport\(selected=(\d+), claimed=(\d+), succeeded=(\d+), "
    r"failed=(\d+), skipped=(\d+), done_ids=(\(.*\)), failed_ids=(\(.*\))\)$"
)


def _structured_replay_report(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    match = _REPLAY_REPORT_RE.fullmatch(str(value or ""))
    if match is None:
        return None
    try:
        done_ids = ast.literal_eval(match.group(6))
        failed_ids = ast.literal_eval(match.group(7))
    except (SyntaxError, ValueError):
        return None
    if not isinstance(done_ids, tuple) or not isinstance(failed_ids, tuple):
        return None
    return {
        "selected": int(match.group(1)),
        "claimed": int(match.group(2)),
        "succeeded": int(match.group(3)),
        "failed": int(match.group(4)),
        "skipped": int(match.group(5)),
        "done_ids": list(done_ids),
        "failed_ids": list(failed_ids),
    }


def _normalize_daemon_once_receipt(receipt: Any) -> Any:
    if not isinstance(receipt, dict):
        return receipt
    normalized = dict(receipt)
    cycle = normalized.get("cycle")
    if not isinstance(cycle, dict):
        return normalized
    cycle = dict(cycle)
    results = cycle.get("results")
    if isinstance(results, dict):
        results = dict(results)
        for stage in ("memory_index_replay", "synthesis_index_replay"):
            results[stage] = _structured_replay_report(results.get(stage))
        cycle["results"] = results
    normalized["cycle"] = cycle
    return normalized


def _process_run_id(section: Any) -> str:
    if not isinstance(section, dict):
        return ""
    command = section.get("command")
    if not isinstance(command, list) or any(
        not isinstance(part, str) or not part for part in command
    ):
        return ""
    options = [
        command[index + 1]
        for index, part in enumerate(command[:-1])
        if part == "-X" and command[index + 1].startswith("recovery_smoke_run_id=")
    ]
    if len(options) != 1:
        return ""
    return options[0].partition("=")[2]


def _mcp_process_port(
    section: Any,
    *,
    run_id: str,
    source_root: str,
    source_revision: str,
) -> int | None:
    if not isinstance(section, dict) or not isinstance(section.get("command"), list):
        return None
    command = section["command"]
    if (
        not command
        or section.get("cwd") != source_root
        or command[0] != runtime_python()
        or command[1:7]
        != [
            "-B",
            "-X",
            f"recovery_smoke_run_id={run_id}",
            "-m",
            "plastic_promise",
            "--streamable-http",
        ]
        or command[8:]
        != [
            "--source-root",
            source_root,
            "--source-revision",
            source_revision,
        ]
        or len(command) != 12
    ):
        return None
    try:
        port = int(command[7])
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _daemon_process_url(
    section: Any,
    *,
    run_id: str,
    once: bool,
    mcp_url: str,
    source_root: str,
    source_revision: str,
) -> str:
    if not isinstance(section, dict) or not isinstance(section.get("command"), list):
        return ""
    command = section["command"]
    daemon_script = canonical_source_root(Path(source_root) / "daemons" / "maintenance_daemon.py")
    expected = [
        runtime_python(),
        "-B",
        "-X",
        f"recovery_smoke_run_id={run_id}",
        daemon_script,
        "--mcp-url",
        mcp_url,
        "--source-root",
        source_root,
        "--source-revision",
        source_revision,
    ]
    if once:
        expected.extend(["--once", "--json"])
    if section.get("cwd") != source_root or command != expected:
        return ""
    url = command[6]
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.path != "/mcp"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        return ""
    return url


def _recovery_evidence_binding(artifact: dict[str, Any], run_id: str) -> str:
    """Return an unkeyed consistency checksum, not an authenticity guarantee."""
    identifiers = artifact.get("identifiers")
    outbox = artifact.get("outbox")
    revisions = artifact.get("revisions")
    daemon_once = artifact.get("daemon_once")
    if not all(isinstance(value, dict) for value in (identifiers, outbox, revisions, daemon_once)):
        return ""
    cycle = daemon_once.get("cycle")
    if not isinstance(cycle, dict):
        return ""

    def outbox_ids(name: str) -> list[str]:
        section = outbox.get(name)
        rows = section.get("after") if isinstance(section, dict) else None
        if not isinstance(rows, list):
            return []
        return sorted(str(row.get("outbox_id") or "") for row in rows if isinstance(row, dict))

    evidence = {
        "run_id": run_id,
        "project_id": identifiers.get("project_id"),
        "synthesis_key": identifiers.get("synthesis_key"),
        "source_ids": sorted(str(value) for value in identifiers.get("canonical_source_ids", [])),
        "synthesis_id": identifiers.get("synthesis_id"),
        "ordinary_outbox_ids": outbox_ids("ordinary"),
        "synthesis_outbox_ids": outbox_ids("synthesis"),
        "corrected_source_id": revisions.get("corrected_source_id"),
        "corrected_source_material_hash": revisions.get("corrected_source_material_hash"),
        "revision_1_material_hash": revisions.get("revision_1_material_hash"),
        "revision_2_material_hash": revisions.get("revision_2_material_hash"),
        "cycle_call_id": cycle.get("cycle_call_id"),
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _valid_daemon_once_receipt(
    section: Any, *, pid: int, mcp_url: str, expected_job_ids: dict[str, set[str]]
) -> bool:
    if not isinstance(section, dict):
        return False
    cycle = section.get("cycle")
    if (
        section.get("schema") != "daemon-once/v1"
        or section.get("ok") is not True
        or type(section.get("pid")) is not int
        or section.get("pid") != pid
        or section.get("mcp_url") != mcp_url
        or not isinstance(cycle, dict)
        or cycle.get("status") != "success"
        or not isinstance(cycle.get("cycle_call_id"), str)
        or not cycle["cycle_call_id"]
        or cycle.get("errors") != {}
    ):
        return False
    results = cycle.get("results")
    if not isinstance(results, dict):
        return False
    for stage in ("memory_index_replay", "synthesis_index_replay"):
        replay = results.get(stage)
        if (
            not isinstance(replay, dict)
            or type(replay.get("failed")) is not int
            or replay.get("failed_ids") != []
            or not isinstance(replay.get("done_ids"), list)
            or not expected_job_ids[stage] <= set(replay["done_ids"])
        ):
            return False
        if replay["failed"] != 0:
            return False
    return True


def validate_recovery_smoke(artifact: Any) -> dict[str, Any]:
    if not isinstance(artifact, dict) or artifact.get("schema") != "recovery-smoke/v1":
        return {"ok": False, "error": "recovery_schema_invalid"}
    processes = artifact.get("processes")
    required = ("mcp_old", "daemon_old", "mcp_restart", "daemon_once", "mcp_final")
    if not isinstance(processes, dict) or any(name not in processes for name in required):
        return {"ok": False, "error": "recovery_pid_evidence_invalid"}
    pids = [processes[name].get("pid") for name in required if isinstance(processes[name], dict)]
    expected_dead = {
        "mcp_old": True,
        "daemon_old": True,
        "mcp_restart": True,
        "daemon_once": True,
        "mcp_final": False,
    }
    if (
        len(pids) != len(required)
        or any(type(pid) is not int or pid <= 0 for pid in pids)
        or len(set(pids)) != len(pids)
        or any(processes[name].get("dead") is not dead for name, dead in expected_dead.items())
    ):
        return {"ok": False, "error": "recovery_pid_evidence_invalid"}

    run_identity = artifact.get("run_identity")
    if not isinstance(run_identity, dict):
        return {"ok": False, "error": "recovery_run_identity_invalid"}
    run_id = str(run_identity.get("run_id") or "")
    port = run_identity.get("port")
    mcp_url = str(run_identity.get("mcp_url") or "")
    health_url = str(run_identity.get("health_url") or "")
    source_root = canonical_source_root(_PROJECT_ROOT)
    source_revision = resolve_source_revision(source_root)
    expected_mcp_url = f"http://127.0.0.1:{port}/mcp"
    expected_health_url = f"http://127.0.0.1:{port}/health"
    if (
        run_identity.get("schema") != "recovery-run-identity/v1"
        or not run_id.startswith("recovery-run:")
        or type(port) is not int
        or not 1 <= port <= 65535
        or mcp_url != expected_mcp_url
        or health_url != expected_health_url
        or source_revision is None
        or run_identity.get("source_root") != source_root
        or run_identity.get("source_revision") != source_revision
        or any(_process_run_id(processes[name]) != run_id for name in required)
        or any(
            _mcp_process_port(
                processes[name],
                run_id=run_id,
                source_root=source_root,
                source_revision=source_revision,
            )
            != port
            for name in ("mcp_old", "mcp_restart", "mcp_final")
        )
        or _daemon_process_url(
            processes["daemon_old"],
            run_id=run_id,
            once=False,
            mcp_url=mcp_url,
            source_root=source_root,
            source_revision=source_revision,
        )
        != mcp_url
    ):
        return {"ok": False, "error": "recovery_run_identity_invalid"}

    health = artifact.get("health")
    expected_identity = artifact.get("expected_server_identity")
    if (
        not isinstance(expected_identity, dict)
        or not isinstance(expected_identity.get("source_root"), str)
        or not expected_identity["source_root"].strip()
        or expected_identity.get("source_root") != source_root
        or expected_identity.get("source_revision") != source_revision
        or not isinstance(expected_identity.get("fusion_policy"), str)
        or run_identity.get("source_root") != expected_identity.get("source_root")
        or run_identity.get("source_revision") != expected_identity.get("source_revision")
        or run_identity.get("fusion_policy") != expected_identity.get("fusion_policy")
    ):
        return {"ok": False, "error": "recovery_health_identity_invalid"}
    health_processes = {
        "old": "mcp_old",
        "restart": "mcp_restart",
        "final": "mcp_final",
    }
    if not isinstance(health, dict):
        return {"ok": False, "error": "recovery_health_identity_invalid"}
    for name, process_name in health_processes.items():
        valid, _reason = validate_mcp_health_identity(
            health.get(name),
            expected_pid=processes[process_name]["pid"],
            expected_source_root=expected_identity["source_root"],
            expected_source_revision=expected_identity["source_revision"],
        )
        if not valid or health[name].get("fusion_policy") != expected_identity["fusion_policy"]:
            return {"ok": False, "error": "recovery_health_identity_invalid"}

    identifiers = artifact.get("identifiers")
    if not isinstance(identifiers, dict):
        return {"ok": False, "error": "recovery_outbox_transition_missing"}
    project_id = str(identifiers.get("project_id") or "")
    source_ids = {str(value) for value in identifiers.get("canonical_source_ids", [])}
    synthesis_id = str(identifiers.get("synthesis_id") or "")
    if not project_id or not source_ids or not synthesis_id or "" in source_ids:
        return {"ok": False, "error": "recovery_outbox_transition_missing"}
    run_nonce = run_id.removeprefix("recovery-run:")
    if (
        run_identity.get("project_id") != project_id
        or project_id != f"project:recovery-smoke:{run_nonce}"
        or identifiers.get("synthesis_key") != f"recovery-smoke:{run_nonce}"
    ):
        return {"ok": False, "error": "recovery_run_identity_invalid"}
    outbox = artifact.get("outbox")
    ordinary_jobs = _valid_outbox_transition(
        outbox.get("ordinary") if isinstance(outbox, dict) else None,
        tool_name="memory_index",
        job_schema="memory-index/v3",
        action="upsert",
        project_id=project_id,
        memory_ids=source_ids,
    )
    synthesis_jobs = _valid_outbox_transition(
        outbox.get("synthesis") if isinstance(outbox, dict) else None,
        tool_name="synthesis_index",
        job_schema="synthesis-index/v1",
        action="delete",
        project_id=project_id,
        memory_ids={synthesis_id},
    )
    if ordinary_jobs is None or synthesis_jobs is None:
        return {"ok": False, "error": "recovery_outbox_transition_missing"}

    revisions = artifact.get("revisions")
    results = artifact.get("final_public_results")
    if not isinstance(revisions, dict) or not isinstance(results, dict):
        return {"ok": False, "error": "recovery_current_revision_missing"}
    current = str(revisions.get("current_memory_id") or "")
    corrected_source_id = str(revisions.get("corrected_source_id") or "")
    corrected_source_material = str(revisions.get("corrected_source_material_hash") or "")
    current_material = str(revisions.get("revision_2_material_hash") or "")
    retired_material = str(revisions.get("revision_1_material_hash") or "")
    current_revision = revisions.get("current_revision")
    retired_revision = revisions.get("retired_revision")
    ordinary_memory_version = revisions.get("ordinary_memory_version")
    cycle_call_id = str(revisions.get("recovery_cycle_call_id") or "")
    if (
        current != synthesis_id
        or revisions.get("synthesis_id") != synthesis_id
        or corrected_source_id not in source_ids
        or current_revision != 2
        or retired_revision != 1
        or type(ordinary_memory_version) is not int
        or ordinary_memory_version < 1
        or not cycle_call_id
        or current_material == retired_material
        or any(
            re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
            for value in (corrected_source_material, retired_material, current_material)
        )
    ):
        return {"ok": False, "error": "recovery_current_revision_missing"}
    corrected_source_embedding_hash = corrected_source_material.removeprefix("sha256:")
    ordinary_payloads = [row["payload"] for row in ordinary_jobs.values()]
    synthesis_payloads = [row["payload"] for row in synthesis_jobs.values()]
    if (
        len(ordinary_payloads) != 1
        or ordinary_payloads[0].get("memory_id") != corrected_source_id
        or ordinary_payloads[0].get("expected_embedding_hash")
        != corrected_source_embedding_hash
        or ordinary_payloads[0].get("material_revision") != corrected_source_embedding_hash
        or ordinary_payloads[0].get("memory_version") != ordinary_memory_version
        or ordinary_payloads[0].get("project_id") != project_id
        or len(synthesis_payloads) != 1
        or synthesis_payloads[0].get("memory_id") != synthesis_id
        or synthesis_payloads[0].get("revision") != retired_revision
    ):
        return {"ok": False, "error": "recovery_outbox_transition_missing"}

    daemon_once_pid = processes["daemon_once"]["pid"]
    daemon_once_url = _daemon_process_url(
        processes["daemon_once"],
        run_id=run_id,
        once=True,
        mcp_url=mcp_url,
        source_root=source_root,
        source_revision=source_revision,
    )
    if not daemon_once_url or not _valid_daemon_once_receipt(
        artifact.get("daemon_once"),
        pid=daemon_once_pid,
        mcp_url=daemon_once_url,
        expected_job_ids={
            "memory_index_replay": set(ordinary_jobs),
            "synthesis_index_replay": set(synthesis_jobs),
        },
    ):
        return {"ok": False, "error": "recovery_daemon_once_evidence_invalid"}
    if daemon_once_url != mcp_url:
        return {"ok": False, "error": "recovery_run_identity_invalid"}
    if artifact["daemon_once"]["cycle"].get("cycle_call_id") != cycle_call_id:
        return {"ok": False, "error": "recovery_daemon_once_evidence_invalid"}
    retired = {str(value) for value in revisions.get("retired_memory_ids", [])}
    if not retired:
        return {"ok": False, "error": "recovery_current_revision_missing"}
    for name in ("memory_recall", "context_supply"):
        payload = results.get(name)
        ids = payload.get("memory_ids") if isinstance(payload, dict) else None
        contents = payload.get("contents") if isinstance(payload, dict) else None
        observed = [str(value) for value in ids or []] + [str(value) for value in contents or []]
        if (
            not isinstance(ids, list)
            or current not in ids
            or any(token in value for token in retired for value in observed)
        ):
            return {"ok": False, "error": "recovery_current_revision_missing"}
    if run_identity.get("evidence_binding") != _recovery_evidence_binding(artifact, run_id):
        return {"ok": False, "error": "recovery_run_identity_invalid"}
    return {"ok": True}


async def _call_tools(url: str, calls: list[tuple[str, dict[str, Any]]]):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    timeout = httpx.Timeout(60.0, read=360.0)
    results = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                for name, arguments in calls:
                    results.append(await call_tool_json(session, name, arguments))
    return results


def _public_evidence(payload: dict[str, Any]) -> dict[str, list[str]]:
    ids: list[str] = []
    contents: list[str] = []
    surfaces = [
        payload.get("core"),
        payload.get("related"),
        payload.get("divergent"),
        payload.get("raw_evidence"),
    ]
    for audit_key in ("audit", "audit_metadata"):
        audit = payload.get(audit_key)
        if isinstance(audit, dict):
            surfaces.append(audit.get("raw_evidence"))
    for rows in surfaces:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            memory_id = str(row.get("id") or "")
            if memory_id and memory_id not in ids:
                ids.append(memory_id)
            content = str(row.get("content") or "")
            if content and content not in contents:
                contents.append(content)
    return {"memory_ids": ids, "contents": contents}


def _outbox_rows(db_path: Path, tool_name: str, memory_ids: set[str]):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT outbox_id, tool_name, project_id, call_id, status, payload_json, "
            "metadata_json, attempt_count, error_class "
            "FROM store_outbox WHERE tool_name = ? ORDER BY created_at, outbox_id",
            (tool_name,),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if str(payload.get("memory_id") or "") not in memory_ids:
            continue
        metadata = json.loads(row["metadata_json"])
        result.append(
            {
                key: row[key]
                for key in (
                    "outbox_id",
                    "tool_name",
                    "project_id",
                    "call_id",
                    "status",
                    "attempt_count",
                    "error_class",
                )
            }
            | {"payload": payload, "metadata": metadata}
        )
    return result


def _evidence_sha256(value: str) -> str:
    digest = value.removeprefix("sha256:")
    if len(digest) == 64 and all(char in "0123456789abcdefABCDEF" for char in digest):
        return "sha256:" + digest.lower()
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _memory_hash(db_path: Path, memory_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT embedding_hash FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    finally:
        conn.close()
    value = str(row[0] or "") if row else ""
    if not value:
        raise RuntimeError(f"memory_embedding_hash_missing:{memory_id}")
    return _evidence_sha256(value)


def _lifecycle_survivor(db_path: Path, memory_id: str) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        source = conn.execute(
            "SELECT content, project_id FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if source is None:
            raise RuntimeError(f"source_not_persisted:{memory_id}")
        rows = conn.execute(
            "SELECT id, worth_success, worth_failure, access_count, created_at "
            "FROM memories WHERE content = ? AND project_id = ?",
            (source["content"], source["project_id"]),
        ).fetchall()
    finally:
        conn.close()

    def rank(row):
        success = float(row["worth_success"] or 0.0)
        failure = float(row["worth_failure"] or 0.0)
        total = success + failure
        worth = (success + 1.0) / (total + 2.0) if total > 0 else 0.5
        return worth, int(row["access_count"]), str(row["created_at"]), str(row["id"])

    return str(max(rows, key=rank)["id"])


def _recovery_source_store_calls(token: str, project_id: str) -> list[tuple[str, dict[str, Any]]]:
    source_content_a = (f"RECOVERY_SOURCE_ALPHA_{token} durable evidence. ") * 5
    source_content_b = (f"RECOVERY_SOURCE_BETA_{token} independent evidence. ") * 5
    common = {
        "memory_type": "experience",
        "source": "recovery-smoke",
        "source_class": "experience",
        "project_id": project_id,
        "project_policy": "strict",
        "visibility": "project",
        # Source seeding is not an embedding test; disable extraction and vector dedup.
        "max_llm_calls": 0,
    }
    return [
        ("memory_store", {**common, "content": source_content_a}),
        ("memory_store", {**common, "content": source_content_b}),
    ]


def _require_distinct_source_ids(memory_ids: list[str]) -> None:
    if len(memory_ids) != 2 or len(set(memory_ids)) != 2:
        raise RuntimeError("recovery_sources_collapsed")


async def _wait_heartbeat(path: Path, pid: int, timeout: float = 240.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("schema") == "maintenance-heartbeat/v1"
                and payload.get("pid") == pid
                and payload.get("startup_replay_cycle_id")
            ):
                return payload
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("maintenance_heartbeat_timeout")


def _seed_trust(db_path: Path):
    from plastic_promise.defense.trust_store import TrustStore

    store = TrustStore(str(db_path))
    store.save("codex", 0.95, "high", "autonomous")
    store._conn.close()


def _write_json(path: Path, payload: Any):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _require_tracked_source_clean(project_root: Path) -> None:
    for command in (
        ["git", "diff", "--quiet", "--ignore-submodules", "--"],
        ["git", "diff", "--cached", "--quiet", "--ignore-submodules", "--"],
    ):
        result = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 1:
            raise RuntimeError("recovery_source_tree_dirty")
        if result.returncode != 0:
            raise RuntimeError("recovery_source_cleanliness_unavailable")


async def run_recovery_smoke(args: argparse.Namespace) -> dict[str, Any]:
    artifact_dir = args.artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parents[1]
    source_revision = resolve_source_revision(project_root)
    if source_revision is None:
        raise RuntimeError("recovery_source_revision_unavailable")
    _require_tracked_source_clean(project_root)
    source_root = canonical_source_root(project_root)
    daemon_script = canonical_source_root(project_root / "daemons" / "maintenance_daemon.py")
    runtime_dir = artifact_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_path = runtime_dir / "plastic_memory.db"
    lancedb_path = runtime_dir / "lancedb"
    run_dir = runtime_dir / "run"
    marker_path = runtime_dir / "checked-index-failures.json"
    port = free_tcp_port()
    mcp_url = f"http://127.0.0.1:{port}/mcp"
    health_url = f"http://127.0.0.1:{port}/health"
    token = uuid.uuid4().hex
    run_id = f"recovery-run:{token}"
    run_option = f"recovery_smoke_run_id={run_id}"
    project_id = f"project:recovery-smoke:{token}"
    revision_1_token = f"RECOVERY_REVISION_1_{token}"
    revision_2_token = f"RECOVERY_REVISION_2_{token}"
    _seed_trust(db_path)
    env_overrides = {
        "PLASTIC_DB_PATH": str(db_path),
        "PLASTIC_LANCEDB_PATH": str(lancedb_path),
        "PLASTIC_PROJECT_ID": project_id,
        "PLASTIC_MCP_TRANSPORT": "streamable_http",
        "PP_MCP_RUNTIME_ACTOR": "codex",
        "PP_SYNTHESIS_ARTIFACTS": "on",
        "PP_SYNTHESIS_RETRIEVAL": "1",
        "PP_MEMORY_PROPOSALS": "off",
        "PP_MEMORY_INDEX_TEXT_POLICY": "legacy",
        "PP_RETRIEVAL_FUSION_POLICY": "max-v1",
        "PP_FORCE_PYTHON_SUPPLY": "1",
        "PP_PREFER_RUST_SUPPLY": "0",
        "PP_CODE_MEMORY_ENABLED": "0",
        "PP_QUERY_EXPANSION": "0",
        "PP_RERANK_DISABLED": "1",
        "PP_TEST_MODE": "1",
        "PP_TEST_INDEX_FAIL_MARKER": str(marker_path),
        "PP_MAINTENANCE_RUN_DIR": str(run_dir),
        "LDB_INIT_ON_HEAVY_INIT": "1",
        "LDB_BACKFILL_ON_INIT": "0",
        "LDB_REBUILD_ON_INIT": "0",
        "EMBEDDER_TIMEOUT": "30",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }
    env = process_environment(env_overrides, project_root=project_root)
    python = runtime_python()
    processes: dict[str, ManagedProcess] = {}
    artifact: dict[str, Any] = {
        "schema": "recovery-smoke/v1",
        "ok": False,
        "run_identity": {
            "schema": "recovery-run-identity/v1",
            "run_id": run_id,
            "source_root": source_root,
            "source_revision": source_revision,
            "fusion_policy": env_overrides["PP_RETRIEVAL_FUSION_POLICY"],
            "port": port,
            "mcp_url": mcp_url,
            "health_url": health_url,
            "project_id": project_id,
        },
        "paths": {
            "artifact_dir": str(artifact_dir),
            "sqlite": str(db_path),
            "lancedb": str(lancedb_path),
            "run_dir": str(run_dir),
        },
        "environment_keys": sorted(env_overrides),
        "expected_server_identity": {
            "source_root": source_root,
            "source_revision": source_revision,
            "fusion_policy": env_overrides["PP_RETRIEVAL_FUSION_POLICY"],
        },
        "processes": {},
        "health": {},
        "outbox": {},
        "revisions": {},
        "final_public_results": {},
        "assertions": [],
        "logs": {},
    }

    def start(name: str, command: list[str]):
        managed = ManagedProcess.start(
            command,
            cwd=source_root,
            env=env,
            stdout_path=artifact_dir / f"{name}.stdout.log",
            stderr_path=artifact_dir / f"{name}.stderr.log",
        )
        processes[name] = managed
        artifact["processes"][name] = {
            "pid": managed.pid,
            "dead": False,
            "cwd": source_root,
            "command": list(managed.command),
        }
        return managed

    try:
        mcp_old = start(
            "mcp_old",
            [
                python,
                "-B",
                "-X",
                run_option,
                "-m",
                "plastic_promise",
                "--streamable-http",
                str(port),
                "--source-root",
                source_root,
                "--source-revision",
                source_revision,
            ],
        )
        artifact["health"]["old"] = await wait_for_health(health_url, mcp_old)
        daemon_old = start(
            "daemon_old",
            [
                python,
                "-B",
                "-X",
                run_option,
                daemon_script,
                "--mcp-url",
                mcp_url,
                "--source-root",
                source_root,
                "--source-revision",
                source_revision,
            ],
        )
        artifact["heartbeat_old"] = await _wait_heartbeat(
            run_dir / "maintenance_daemon.heartbeat", daemon_old.pid
        )

        source_a, source_b = await _call_tools(
            mcp_url,
            _recovery_source_store_calls(token, project_id),
        )
        returned_source_ids = [str(source_a["memory_id"]), str(source_b["memory_id"])]
        _require_distinct_source_ids(returned_source_ids)
        source_ids = [_lifecycle_survivor(db_path, memory_id) for memory_id in returned_source_ids]
        _require_distinct_source_ids(source_ids)
        artifact["identifiers"] = {
            "project_id": project_id,
            "returned_source_ids": returned_source_ids,
            "canonical_source_ids": source_ids,
        }
        synthesis_key = f"recovery-smoke:{token}"
        synthesis_v1 = (
            f"{revision_1_token} governed conclusion supported by two independent sources. "
        ) * 4
        created = (
            await _call_tools(
                mcp_url,
                [
                    (
                        "memory_store",
                        {
                            "content": synthesis_v1,
                            "memory_type": "synthesis",
                            "source": "synthesis",
                            "source_ids": source_ids,
                            "synthesis_key": synthesis_key,
                            "validity_scope": project_id,
                            "project_id": project_id,
                            "project_policy": "strict",
                            "visibility": "project",
                            "actor": "codex",
                            "automatic": False,
                        },
                    )
                ],
            )
        )[0]
        synthesis_id = str(created["memory_id"])
        artifact["identifiers"].update(
            {"synthesis_id": synthesis_id, "synthesis_key": synthesis_key}
        )
        verified = (
            await _call_tools(
                mcp_url,
                [
                    (
                        "feedback_apply",
                        {
                            "item_id": synthesis_id,
                            "feedback_type": "adopted",
                            "expected_revision": 1,
                            "project_id": project_id,
                        },
                    )
                ],
            )
        )[0]
        if verified.get("status") != "verified":
            raise RuntimeError(f"revision_1_verify_failed:{verified}")
        revision_1_hash = _memory_hash(db_path, synthesis_id)

        recall_before, context_before = await _call_tools(
            mcp_url,
            [
                (
                    "memory_recall",
                    {
                        "query": revision_1_token,
                        "task_type": "code_review",
                        "max_results": 10,
                        "debug": True,
                        "project_id": project_id,
                        "project_policy": "strict",
                        "retrieval_mode": "hybrid",
                    },
                ),
                (
                    "context_supply",
                    {
                        "task_description": revision_1_token,
                        "task_type": "code_review",
                        "debug": True,
                        "project_id": project_id,
                        "project_policy": "strict",
                        "retrieval_mode": "hybrid",
                    },
                ),
            ],
        )
        before_evidence = {
            "memory_recall": _public_evidence(recall_before),
            "context_supply": _public_evidence(context_before),
        }
        if any(synthesis_id not in evidence["memory_ids"] for evidence in before_evidence.values()):
            raise RuntimeError("revision_1_public_visibility_missing")

        _write_json(
            marker_path,
            {
                "schema": "test-index-failure/v1",
                "failures": [
                    {"action": "upsert", "memory_id": source_ids[0], "remaining": 1},
                    {"action": "delete", "memory_id": synthesis_id, "remaining": 1},
                ],
            },
        )
        corrected = (
            await _call_tools(
                mcp_url,
                [
                    (
                        "memory_correct",
                        {
                            "memory_id": source_ids[0],
                            "content": (f"RECOVERY_SOURCE_ALPHA_CORRECTED_{token} evidence. ") * 5,
                            "mark_as": "corrected",
                            "reason": "recovery smoke source correction",
                            "project_id": project_id,
                        },
                    )
                ],
            )
        )[0]
        if corrected.get("committed") is not True or synthesis_id not in corrected.get(
            "stale_dependents", []
        ):
            raise RuntimeError(f"public_correction_failed:{corrected}")
        corrected_source_hash = _memory_hash(db_path, source_ids[0])
        ordinary_before = _outbox_rows(db_path, "memory_index", set(source_ids))
        synthesis_before = _outbox_rows(db_path, "synthesis_index", {synthesis_id})
        if not any(row["status"] == "pending" for row in ordinary_before) or not any(
            row["status"] == "pending" for row in synthesis_before
        ):
            raise RuntimeError("recovery_pending_jobs_missing")
        ordinary_pending = [row for row in ordinary_before if row["status"] == "pending"]
        if len(ordinary_pending) != 1:
            raise RuntimeError("recovery_ordinary_job_identity_invalid")
        ordinary_memory_version = ordinary_pending[0]["payload"].get("memory_version")

        blocked_recall = (
            await _call_tools(
                mcp_url,
                [
                    (
                        "memory_recall",
                        {
                            "query": revision_1_token,
                            "max_results": 10,
                            "debug": True,
                            "project_id": project_id,
                            "project_policy": "strict",
                        },
                    )
                ],
            )
        )[0]
        if synthesis_id in _public_evidence(blocked_recall)["memory_ids"]:
            raise RuntimeError("stale_synthesis_not_blocked")

        daemon_old.terminate()
        mcp_old.terminate()
        artifact["processes"]["daemon_old"]["dead"] = daemon_old.dead
        artifact["processes"]["mcp_old"]["dead"] = mcp_old.dead
        await wait_for_port_closed(port)
        artifact["death_checks"] = {
            "old_daemon_dead": daemon_old.dead,
            "old_mcp_dead": mcp_old.dead,
            "old_port_closed": True,
        }

        mcp_restart = start(
            "mcp_restart",
            [
                python,
                "-B",
                "-X",
                run_option,
                "-m",
                "plastic_promise",
                "--streamable-http",
                str(port),
                "--source-root",
                source_root,
                "--source-revision",
                source_revision,
            ],
        )
        artifact["health"]["restart"] = await wait_for_health(health_url, mcp_restart)
        daemon_once = start(
            "daemon_once",
            [
                python,
                "-B",
                "-X",
                run_option,
                daemon_script,
                "--mcp-url",
                mcp_url,
                "--source-root",
                source_root,
                "--source-revision",
                source_revision,
                "--once",
                "--json",
            ],
        )
        once_code = await asyncio.to_thread(daemon_once.wait, 240.0)
        artifact["processes"]["daemon_once"]["dead"] = daemon_once.dead
        if once_code != 0:
            raise RuntimeError(f"daemon_once_failed:{once_code}")
        once_lines = [
            line
            for line in daemon_once.stdout_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        artifact["daemon_once"] = _normalize_daemon_once_receipt(json.loads(once_lines[-1]))
        ordinary_after = _outbox_rows(db_path, "memory_index", set(source_ids))
        synthesis_after = _outbox_rows(db_path, "synthesis_index", {synthesis_id})

        synthesis_v2 = (
            f"{revision_2_token} refreshed governed conclusion from corrected evidence. "
        ) * 4
        refreshed = (
            await _call_tools(
                mcp_url,
                [
                    (
                        "memory_store",
                        {
                            "content": synthesis_v2,
                            "memory_type": "synthesis",
                            "source": "synthesis",
                            "source_ids": source_ids,
                            "synthesis_key": synthesis_key,
                            "expected_revision": 1,
                            "validity_scope": project_id,
                            "project_id": project_id,
                            "project_policy": "strict",
                            "visibility": "project",
                            "actor": "codex",
                        },
                    )
                ],
            )
        )[0]
        if refreshed.get("revision") != 2:
            raise RuntimeError(f"revision_2_refresh_failed:{refreshed}")
        verified_v2 = (
            await _call_tools(
                mcp_url,
                [
                    (
                        "feedback_apply",
                        {
                            "item_id": synthesis_id,
                            "feedback_type": "adopted",
                            "expected_revision": 2,
                            "project_id": project_id,
                        },
                    )
                ],
            )
        )[0]
        if verified_v2.get("status") != "verified":
            raise RuntimeError(f"revision_2_verify_failed:{verified_v2}")
        revision_2_hash = _memory_hash(db_path, synthesis_id)

        mcp_restart.terminate()
        artifact["processes"]["mcp_restart"]["dead"] = mcp_restart.dead
        await wait_for_port_closed(port)
        mcp_final = start(
            "mcp_final",
            [
                python,
                "-B",
                "-X",
                run_option,
                "-m",
                "plastic_promise",
                "--streamable-http",
                str(port),
                "--source-root",
                source_root,
                "--source-revision",
                source_revision,
            ],
        )
        artifact["health"]["final"] = await wait_for_health(health_url, mcp_final)
        final_recall, final_context = await _call_tools(
            mcp_url,
            [
                (
                    "memory_recall",
                    {
                        "query": revision_2_token,
                        "max_results": 10,
                        "debug": True,
                        "project_id": project_id,
                        "project_policy": "strict",
                    },
                ),
                (
                    "context_supply",
                    {
                        "task_description": revision_2_token,
                        "debug": True,
                        "project_id": project_id,
                        "project_policy": "strict",
                    },
                ),
            ],
        )
        artifact["outbox"] = {
            "ordinary": {"before": ordinary_before, "after": ordinary_after},
            "synthesis": {"before": synthesis_before, "after": synthesis_after},
        }
        artifact["revisions"] = {
            "synthesis_id": synthesis_id,
            "retired_memory_ids": [revision_1_token],
            "current_memory_id": synthesis_id,
            "retired_revision": 1,
            "current_revision": 2,
            "revision_1_material_hash": revision_1_hash,
            "revision_2_material_hash": revision_2_hash,
            "corrected_source_id": source_ids[0],
            "corrected_source_material_hash": corrected_source_hash,
            "ordinary_memory_version": ordinary_memory_version,
            "recovery_cycle_call_id": artifact["daemon_once"]["cycle"]["cycle_call_id"],
        }
        artifact["public_before_mutation"] = before_evidence
        artifact["final_public_results"] = {
            "memory_recall": _public_evidence(final_recall),
            "context_supply": _public_evidence(final_context),
        }
        artifact["assertions"] = [
            {"name": "old_pids_dead", "passed": True},
            {"name": "checked_outbox_replayed", "passed": True},
            {"name": "current_revision_only", "passed": True},
        ]
        artifact["run_identity"]["evidence_binding"] = _recovery_evidence_binding(artifact, run_id)
        validation = validate_recovery_smoke(artifact)
        if validation.get("ok") is not True:
            raise RuntimeError(str(validation["error"]))
        artifact["ok"] = True
        return artifact
    finally:
        for name, managed in reversed(list(processes.items())):
            with suppress(Exception):
                managed.terminate()
            artifact["processes"].setdefault(name, {})["cleanup_dead"] = managed.dead
        for name, managed in processes.items():
            for stream, path in (
                ("stdout", managed.stdout_path),
                ("stderr", managed.stderr_path),
            ):
                if path.exists():
                    artifact["logs"][f"{name}_{stream}"] = {
                        "path": str(path),
                        "sha256": file_sha256(path),
                    }
        _write_json(artifact_dir / "recovery-smoke.json", artifact)
        (artifact_dir / "recovery-smoke.sha256").write_text(
            file_sha256(artifact_dir / "recovery-smoke.json") + "\n",
            encoding="ascii",
        )


async def async_main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    try:
        artifact = await run_recovery_smoke(args)
        validation = validate_recovery_smoke(artifact)
        if validation.get("ok") is not True:
            raise RuntimeError(str(validation.get("error")))
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False) if args.json else payload)
        return 1
    print(json.dumps(artifact, ensure_ascii=False) if args.json else artifact)
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
