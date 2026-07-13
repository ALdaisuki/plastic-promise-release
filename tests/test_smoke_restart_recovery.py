"""Recovery-smoke artifact and managed-process contracts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest


def test_evidence_sha256_preserves_existing_digest_identity():
    from scripts.smoke_restart_recovery import _evidence_sha256

    digest = "ABCDEF0123456789" * 4

    assert _evidence_sha256(digest) == "sha256:" + digest.lower()
    assert _evidence_sha256("sha256:" + digest) == "sha256:" + digest.lower()


def test_memory_hash_reads_raw_digest_without_rehashing(tmp_path):
    from scripts.smoke_restart_recovery import _memory_hash

    db_path = tmp_path / "memory.db"
    digest = "1234567890abcdef" * 4
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, embedding_hash TEXT)")
        conn.execute("INSERT INTO memories VALUES (?, ?)", ("memory-1", digest))

    assert _memory_hash(db_path, "memory-1") == "sha256:" + digest


@pytest.mark.parametrize("memory_id", ["missing", "empty"])
def test_memory_hash_fails_closed_without_canonical_embedding(tmp_path, memory_id):
    from scripts.smoke_restart_recovery import _memory_hash

    db_path = tmp_path / "memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, embedding_hash TEXT)")
        conn.execute("INSERT INTO memories VALUES ('empty', '')")

    with pytest.raises(RuntimeError, match=f"memory_embedding_hash_missing:{memory_id}"):
        _memory_hash(db_path, memory_id)


def test_recovery_source_calls_persist_two_distinct_ids_without_ollama(
    tmp_path, monkeypatch
):
    from plastic_promise.core.context_engine import ContextEngine
    from plastic_promise.core.embedder import reset_embedder
    from plastic_promise.mcp.tools.memory import handle_memory_store
    from scripts.smoke_restart_recovery import (
        _recovery_source_store_calls,
        _require_distinct_source_ids,
    )

    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    monkeypatch.setenv("EMBEDDER_PROVIDER", "fallback")
    monkeypatch.setenv("LDB_INIT_ON_HEAVY_INIT", "0")
    reset_embedder()
    engine = ContextEngine()
    try:
        payloads = []
        calls = _recovery_source_store_calls("nonce", "project:recovery-smoke:nonce")
        for tool_name, args in calls:
            assert tool_name == "memory_store"
            assert args["max_llm_calls"] == 0
            result = asyncio.run(handle_memory_store(engine, args))
            payloads.append(json.loads(result[0].text))

        memory_ids = [payload["memory_id"] for payload in payloads]
        assert all(payload["stored"] is True for payload in payloads)
        _require_distinct_source_ids(memory_ids)
        assert all(engine._sqlite.get(memory_id) is not None for memory_id in memory_ids)
    finally:
        engine._sqlite._conn.close()
        reset_embedder()


def test_recovery_source_ids_fail_closed_when_dedup_collapses_sources():
    from scripts.smoke_restart_recovery import _require_distinct_source_ids

    with pytest.raises(RuntimeError, match="recovery_sources_collapsed"):
        _require_distinct_source_ids(["canonical", "canonical"])


@pytest.mark.parametrize(
    ("returncodes", "error"),
    [
        ([1], "recovery_source_tree_dirty"),
        ([0, 1], "recovery_source_tree_dirty"),
        ([128], "recovery_source_cleanliness_unavailable"),
    ],
)
def test_recovery_smoke_requires_clean_tracked_source(
    monkeypatch, tmp_path, returncodes, error
):
    from scripts import smoke_restart_recovery

    pending = list(returncodes)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, pending.pop(0))

    monkeypatch.setattr(smoke_restart_recovery.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=error):
        smoke_restart_recovery._require_tracked_source_clean(tmp_path)


def test_recovery_smoke_accepts_clean_tracked_source(monkeypatch, tmp_path):
    from scripts import smoke_restart_recovery

    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(smoke_restart_recovery.subprocess, "run", fake_run)

    smoke_restart_recovery._require_tracked_source_clean(tmp_path)

    assert [entry[0][1:3] for entry in commands] == [
        ["diff", "--quiet"],
        ["diff", "--cached"],
    ]
    assert all(entry[1]["cwd"] == tmp_path for entry in commands)


def complete_recovery_smoke_v1(*, run_nonce="run-a", port=9101):
    from plastic_promise.launcher.service_manager import (
        MCP_FUSION_IDENTITY_SCHEMA,
        canonical_source_root,
        resolve_source_revision,
    )
    from scripts.http_mcp_harness import runtime_python
    from scripts.smoke_restart_recovery import _recovery_evidence_binding

    run_id = f"recovery-run:{run_nonce}"
    mcp_url = f"http://127.0.0.1:{port}/mcp"
    health_url = f"http://127.0.0.1:{port}/health"
    source_root = canonical_source_root(Path(__file__).resolve().parents[1])
    source_revision = resolve_source_revision(source_root)
    assert source_revision is not None
    python = runtime_python()
    daemon_script = canonical_source_root(Path(source_root) / "daemons" / "maintenance_daemon.py")
    project_id = f"project:recovery-smoke:{run_nonce}"
    source_id = f"ordinary-source-{run_nonce}"
    current_id = f"revision-2-{run_nonce}"
    retired_id = f"revision-1-{run_nonce}"
    cycle_call_id = f"cycle-once-{run_nonce}"
    corrected_source_embedding_hash = "3" * 64
    corrected_source_material_hash = "sha256:" + corrected_source_embedding_hash

    def health(pid):
        return {
            "status": "ok",
            "pid": pid,
            "source_root": source_root,
            "source_revision": source_revision,
            "fusion_policy": "max-v1",
            "fusion_attestation": {
                "schema": MCP_FUSION_IDENTITY_SCHEMA,
                "requested_policy": "max-v1",
                "effective_policy": "max-v1",
                "requested_runtime": "python",
                "effective_runtime": "python",
                "capability_reason": "runtime_forced:python",
                "candidate_id": "",
                "config_hash": "",
                "config": None,
            },
        }

    def job(outbox_id, tool_name, action, memory_id, *, done=False):
        payload = {"action": action, "memory_id": memory_id}
        schema = "synthesis-index/v1"
        if tool_name == "memory_index":
            schema = "memory-index/v3"
            payload.update(
                {
                    "expected_embedding_hash": corrected_source_embedding_hash,
                    "material_revision": corrected_source_embedding_hash,
                    "memory_version": 7,
                    "project_id": project_id,
                }
            )
        else:
            payload["revision"] = 1
        return {
            "outbox_id": outbox_id,
            "tool_name": tool_name,
            "project_id": project_id,
            "call_id": f"call:{outbox_id}",
            "status": "done" if done else "pending",
            "attempt_count": 1,
            "error_class": "" if done else "InjectedIndexFailure",
            "payload": payload,
            "metadata": {"job_schema": schema},
        }

    ordinary_id = f"outbox_ordinary_{run_nonce}"
    synthesis_job_id = f"outbox_synthesis_{run_nonce}"
    run_option = f"recovery_smoke_run_id={run_id}"
    mcp_command = [
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
    ]
    artifact = {
        "schema": "recovery-smoke/v1",
        "run_identity": {
            "schema": "recovery-run-identity/v1",
            "run_id": run_id,
            "source_root": source_root,
            "source_revision": source_revision,
            "fusion_policy": "max-v1",
            "port": port,
            "mcp_url": mcp_url,
            "health_url": health_url,
            "project_id": project_id,
        },
        "expected_server_identity": {
            "source_root": source_root,
            "source_revision": source_revision,
            "fusion_policy": "max-v1",
        },
        "processes": {
            "mcp_old": {
                "pid": 101,
                "dead": True,
                "cwd": source_root,
                "command": mcp_command,
            },
            "daemon_old": {
                "pid": 102,
                "dead": True,
                "cwd": source_root,
                "command": [
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
            },
            "mcp_restart": {
                "pid": 201,
                "dead": True,
                "cwd": source_root,
                "command": mcp_command,
            },
            "daemon_once": {
                "pid": 202,
                "dead": True,
                "cwd": source_root,
                "command": [
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
            },
            "mcp_final": {
                "pid": 301,
                "dead": False,
                "cwd": source_root,
                "command": mcp_command,
            },
        },
        "health": {
            "old": health(101),
            "restart": health(201),
            "final": health(301),
        },
        "daemon_once": {
            "schema": "daemon-once/v1",
            "ok": True,
            "pid": 202,
            "mcp_url": mcp_url,
            "cycle": {
                "status": "success",
                "cycle_call_id": cycle_call_id,
                "errors": {},
                "results": {
                    "memory_index_replay": {
                        "selected": 1,
                        "claimed": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "skipped": 0,
                        "done_ids": [ordinary_id],
                        "failed_ids": [],
                    },
                    "synthesis_index_replay": {
                        "selected": 1,
                        "claimed": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "skipped": 0,
                        "done_ids": [synthesis_job_id],
                        "failed_ids": [],
                    },
                },
            },
        },
        "identifiers": {
            "project_id": project_id,
            "canonical_source_ids": [source_id],
            "synthesis_id": current_id,
            "synthesis_key": f"recovery-smoke:{run_nonce}",
        },
        "outbox": {
            "ordinary": {
                "before": [job(ordinary_id, "memory_index", "upsert", source_id)],
                "after": [
                    job(
                        ordinary_id,
                        "memory_index",
                        "upsert",
                        source_id,
                        done=True,
                    )
                ],
            },
            "synthesis": {
                "before": [job(synthesis_job_id, "synthesis_index", "delete", current_id)],
                "after": [
                    job(
                        synthesis_job_id,
                        "synthesis_index",
                        "delete",
                        current_id,
                        done=True,
                    )
                ],
            },
        },
        "revisions": {
            "synthesis_id": current_id,
            "retired_memory_ids": [retired_id],
            "current_memory_id": current_id,
            "retired_revision": 1,
            "current_revision": 2,
            "revision_1_material_hash": "sha256:" + "1" * 64,
            "revision_2_material_hash": "sha256:" + "2" * 64,
            "corrected_source_id": source_id,
            "corrected_source_material_hash": corrected_source_material_hash,
            "ordinary_memory_version": 7,
            "recovery_cycle_call_id": cycle_call_id,
        },
        "final_public_results": {
            "memory_recall": {"memory_ids": [current_id]},
            "context_supply": {"memory_ids": [current_id]},
        },
    }
    artifact["run_identity"]["evidence_binding"] = _recovery_evidence_binding(artifact, run_id)
    return artifact


def test_recovery_smoke_validator_accepts_complete_v1():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    assert validate_recovery_smoke(complete_recovery_smoke_v1()) == {"ok": True}


def test_recovery_smoke_validator_rejects_sections_spliced_from_another_valid_run():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    first = complete_recovery_smoke_v1(run_nonce="run-a", port=9101)
    second = complete_recovery_smoke_v1(run_nonce="run-b", port=9202)
    assert validate_recovery_smoke(first) == {"ok": True}
    assert validate_recovery_smoke(second) == {"ok": True}

    mixed = deepcopy(first)
    for section in (
        "identifiers",
        "outbox",
        "revisions",
        "final_public_results",
        "daemon_once",
    ):
        mixed[section] = deepcopy(second[section])
    mixed["processes"]["daemon_once"] = deepcopy(second["processes"]["daemon_once"])

    assert validate_recovery_smoke(mixed)["error"] == "recovery_run_identity_invalid"


@pytest.mark.parametrize(
    "process_name", ["mcp_old", "daemon_old", "mcp_restart", "daemon_once", "mcp_final"]
)
def test_recovery_smoke_validator_rejects_process_without_shared_run_marker(process_name):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    command = artifact["processes"][process_name]["command"]
    marker_index = command.index("-X")
    del command[marker_index : marker_index + 2]

    assert validate_recovery_smoke(artifact)["error"] == "recovery_run_identity_invalid"


@pytest.mark.parametrize(
    "process_name", ["mcp_old", "daemon_old", "mcp_restart", "daemon_once", "mcp_final"]
)
def test_recovery_smoke_validator_requires_exact_runtime_python(process_name):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["processes"][process_name]["command"] = list(
        artifact["processes"][process_name]["command"]
    )
    artifact["processes"][process_name]["command"][0] = "python"

    assert validate_recovery_smoke(artifact)["error"] in {
        "recovery_run_identity_invalid",
        "recovery_daemon_once_evidence_invalid",
    }


@pytest.mark.parametrize(
    "process_name", ["mcp_old", "daemon_old", "mcp_restart", "daemon_once", "mcp_final"]
)
def test_recovery_smoke_validator_requires_canonical_project_cwd(process_name):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["processes"][process_name]["cwd"] = str(Path.cwd().parent)

    assert validate_recovery_smoke(artifact)["error"] in {
        "recovery_run_identity_invalid",
        "recovery_daemon_once_evidence_invalid",
    }


@pytest.mark.parametrize("process_name", ["daemon_old", "daemon_once"])
def test_recovery_smoke_validator_requires_absolute_canonical_daemon_script(process_name):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["processes"][process_name]["command"][4] = "daemons/maintenance_daemon.py"

    assert validate_recovery_smoke(artifact)["error"] in {
        "recovery_run_identity_invalid",
        "recovery_daemon_once_evidence_invalid",
    }


@pytest.mark.parametrize("field", ["--source-root", "--source-revision"])
@pytest.mark.parametrize(
    "process_name", ["mcp_old", "daemon_old", "mcp_restart", "daemon_once", "mcp_final"]
)
def test_recovery_smoke_validator_requires_exact_source_arguments(process_name, field):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    command = list(artifact["processes"][process_name]["command"])
    artifact["processes"][process_name]["command"] = command
    command[command.index(field) + 1] = "foreign-source-identity"

    assert validate_recovery_smoke(artifact)["error"] in {
        "recovery_run_identity_invalid",
        "recovery_daemon_once_evidence_invalid",
    }


def test_recovery_smoke_validator_binds_mcp_port_and_daemon_url():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    port_drift = complete_recovery_smoke_v1()
    port_drift["processes"]["mcp_restart"]["command"] = list(
        port_drift["processes"]["mcp_restart"]["command"]
    )
    stream_flag = port_drift["processes"]["mcp_restart"]["command"].index("--streamable-http")
    port_drift["processes"]["mcp_restart"]["command"][stream_flag + 1] = "9202"
    assert validate_recovery_smoke(port_drift)["error"] == "recovery_run_identity_invalid"

    url_drift = complete_recovery_smoke_v1()
    mcp_url_flag = url_drift["processes"]["daemon_old"]["command"].index("--mcp-url")
    url_drift["processes"]["daemon_old"]["command"][mcp_url_flag + 1] = "http://127.0.0.1:9202/mcp"
    assert validate_recovery_smoke(url_drift)["error"] == "recovery_run_identity_invalid"


def test_recovery_smoke_validator_rejects_missing_or_mismatched_pid_evidence():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["processes"]["daemon_once"]["pid"] = artifact["processes"]["mcp_restart"]["pid"]
    assert validate_recovery_smoke(artifact)["error"] == "recovery_pid_evidence_invalid"


@pytest.mark.parametrize("health_name", ["old", "restart", "final"])
def test_recovery_smoke_validator_rejects_health_pid_not_bound_to_process(health_name):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["health"][health_name]["pid"] += 1000
    assert validate_recovery_smoke(artifact)["error"] == "recovery_health_identity_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_root", "foreign-root"),
        ("source_revision", "f" * 40),
        ("fusion_policy", "legacy-auto"),
    ],
)
def test_recovery_smoke_validator_rejects_health_source_or_fusion_mismatch(field, value):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["health"]["restart"][field] = value
    assert validate_recovery_smoke(artifact)["error"] == "recovery_health_identity_invalid"


def test_recovery_smoke_validator_rejects_invalid_fusion_attestation():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["health"]["final"]["fusion_attestation"]["effective_policy"] = "legacy-auto"
    assert validate_recovery_smoke(artifact)["error"] == "recovery_health_identity_invalid"


@pytest.mark.parametrize("missing_argument", ["--once", "--json", "--mcp-url"])
def test_recovery_smoke_validator_rejects_missing_daemon_once_argument(missing_argument):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["processes"]["daemon_once"]["command"].remove(missing_argument)
    assert validate_recovery_smoke(artifact)["error"] == ("recovery_daemon_once_evidence_invalid")


def test_recovery_smoke_validator_rejects_daemon_flags_before_script():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    command = artifact["processes"]["daemon_once"]["command"]
    command[:] = command[-2:] + command[:-2]
    assert validate_recovery_smoke(artifact)["error"] == ("recovery_daemon_once_evidence_invalid")


@pytest.mark.parametrize(
    ("receipt_field", "invalid_value"),
    [
        ("schema", "daemon-once/v0"),
        ("ok", False),
        ("pid", 999),
        ("mcp_url", "http://127.0.0.1:9999/mcp"),
    ],
)
def test_recovery_smoke_validator_rejects_forged_daemon_once_receipt(receipt_field, invalid_value):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["daemon_once"][receipt_field] = invalid_value
    assert validate_recovery_smoke(artifact)["error"] == ("recovery_daemon_once_evidence_invalid")


def test_recovery_smoke_validator_rejects_failed_daemon_once_replay_receipt():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["daemon_once"]["cycle"]["results"]["synthesis_index_replay"]["failed"] = 1
    assert validate_recovery_smoke(artifact)["error"] == ("recovery_daemon_once_evidence_invalid")


def test_recovery_smoke_validator_rejects_missing_checked_outbox_transition():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["outbox"]["ordinary"]["before"] = artifact["outbox"]["ordinary"]["after"]
    assert validate_recovery_smoke(artifact)["error"] == ("recovery_outbox_transition_missing")


@pytest.mark.parametrize(
    ("field", "value"),
    [("attempt_count", 0), ("error_class", "")],
)
def test_recovery_smoke_validator_requires_injected_failure_evidence(field, value):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["outbox"]["ordinary"]["before"][0][field] = value

    assert validate_recovery_smoke(artifact)["error"] == ("recovery_outbox_transition_missing")


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("ordinary", "tool_name", "synthesis_index"),
        ("ordinary", "project_id", "project:forged"),
        ("ordinary", "metadata", {"job_schema": "memory-index/v2"}),
        ("synthesis", "payload", {"action": "delete", "memory_id": "revision-2"}),
    ],
)
def test_recovery_smoke_validator_rejects_forged_checked_job_evidence(section, field, value):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    for phase in ("before", "after"):
        artifact["outbox"][section][phase][0][field] = value
    assert validate_recovery_smoke(artifact)["error"] == ("recovery_outbox_transition_missing")


def test_recovery_smoke_validator_rejects_synchronized_outbox_ids_not_in_cycle_receipt():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    for phase in ("before", "after"):
        artifact["outbox"]["ordinary"][phase][0]["outbox_id"] = "outbox_aaaaaaaaaaaaaaaa"
    assert validate_recovery_smoke(artifact)["error"] == ("recovery_daemon_once_evidence_invalid")


def test_recovery_smoke_validator_rejects_missing_current_revision_only_results():
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["final_public_results"]["memory_recall"]["memory_ids"].append(
        artifact["revisions"]["retired_memory_ids"][0]
    )
    assert validate_recovery_smoke(artifact)["error"] == "recovery_current_revision_missing"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("synthesis_id", "unrelated", "recovery_current_revision_missing"),
        ("current_memory_id", "unrelated", "recovery_current_revision_missing"),
        ("current_revision", 3, "recovery_current_revision_missing"),
        ("retired_revision", 2, "recovery_current_revision_missing"),
        ("corrected_source_id", "unrelated", "recovery_current_revision_missing"),
        ("corrected_source_material_hash", "material-hash-2", "recovery_current_revision_missing"),
        (
            "corrected_source_material_hash",
            "sha256:" + "g" * 64,
            "recovery_current_revision_missing",
        ),
        ("ordinary_memory_version", 8, "recovery_outbox_transition_missing"),
        ("recovery_cycle_call_id", "unrelated", "recovery_daemon_once_evidence_invalid"),
    ],
)
def test_recovery_smoke_validator_rejects_unbound_revision_material_and_cycle(field, value, error):
    from scripts.smoke_restart_recovery import validate_recovery_smoke

    artifact = complete_recovery_smoke_v1()
    artifact["revisions"][field] = value

    assert validate_recovery_smoke(artifact)["error"] == error


def test_checked_failure_batch_consumes_each_exact_side_effect(monkeypatch, tmp_path):
    from plastic_promise.core.synthesis_maintenance import (
        InjectedIndexFailure,
        consume_test_index_failure,
    )

    marker = tmp_path / "failures.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "test-index-failure/v1",
                "failures": [
                    {"action": "upsert", "memory_id": "ordinary-a", "remaining": 1},
                    {"action": "delete", "memory_id": "synthesis-a", "remaining": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PP_TEST_MODE", "1")
    monkeypatch.setenv("PP_TEST_INDEX_FAIL_MARKER", str(marker))

    with pytest.raises(InjectedIndexFailure):
        consume_test_index_failure(action="upsert", memory_id="ordinary-a")
    assert marker.exists()
    with pytest.raises(InjectedIndexFailure):
        consume_test_index_failure(action="delete", memory_id="synthesis-a")
    assert not marker.exists()
