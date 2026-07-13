import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_smoke_module():
    path = Path("scripts/smoke_http_mcp.py")
    spec = importlib.util.spec_from_file_location("smoke_http_mcp", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pipeline_count_accepts_arrow_variants():
    smoke = load_smoke_module()
    unicode_arrow_key = f"embedded{chr(0x2192)}migrated"

    assert smoke.pipeline_count({unicode_arrow_key: 3}, "embedded", "migrated") == 3
    assert smoke.pipeline_count({"embedded->migrated": "2"}, "embedded", "migrated") == 2


def test_parse_mcp_json_content_rejects_malformed_json():
    smoke = load_smoke_module()
    content = [SimpleNamespace(type="text", text="not-json")]

    with pytest.raises(smoke.SmokeFailure, match="non-JSON"):
        smoke.parse_mcp_json_content(content, "memory_store")


def test_validate_store_requires_migrated_pipeline_count():
    smoke = load_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="embedded->migrated"):
        smoke.validate_store({"stored": True, "memory_id": "m1", "pipeline": {}})

    result = smoke.validate_store(
        {"stored": True, "memory_id": "m1", "pipeline": {f"embedded{chr(0x2192)}migrated": 1}}
    )

    assert result["memory_id"] == "m1"
    assert result["migrated"] == 1


@pytest.mark.parametrize("validator_name", ["validate_recall", "validate_context"])
def test_retrieval_validators_require_the_stored_memory_as_evidence(validator_name):
    smoke = load_smoke_module()
    validator = getattr(smoke, validator_name)
    payload = {
        "core": [
            {
                "id": "m1",
                "content": "HTTP MCP release smoke marker:marker-one",
            }
        ],
        "related": [],
        "divergent": [],
    }
    if validator_name == "validate_context":
        payload["project_context"] = {"degraded": False}
        payload["audit_metadata"] = {}
    else:
        payload["audit"] = {}

    result = validator(payload, "m1", "marker-one")

    assert result["observed_memory_id"] == "m1"
    assert result["observed_marker"] == "marker-one"
    assert result["evidence_locations"] == ["core"]


@pytest.mark.parametrize("validator_name", ["validate_recall", "validate_context"])
def test_retrieval_validators_reject_acceptance_without_matching_evidence(validator_name):
    smoke = load_smoke_module()
    validator = getattr(smoke, validator_name)
    payload = {
        "core": [{"id": "other", "content": "unrelated context"}],
        "related": [],
        "divergent": [],
    }
    if validator_name == "validate_context":
        payload["project_context"] = {"degraded": False}
        payload["audit_metadata"] = {}
    else:
        payload["audit"] = {}

    with pytest.raises(smoke.SmokeFailure, match="stored memory m1"):
        validator(payload, "m1", "marker-one")


@pytest.mark.parametrize("validator_name", ["validate_recall", "validate_context"])
def test_retrieval_validators_reject_id_only_without_marker_content(validator_name):
    smoke = load_smoke_module()
    validator = getattr(smoke, validator_name)
    payload = {
        "core": [{"id": "m1", "content": "content from another revision"}],
        "related": [],
        "divergent": [],
    }
    if validator_name == "validate_context":
        payload["project_context"] = {"degraded": False}
        payload["audit_metadata"] = {}
    else:
        payload["audit"] = {}

    with pytest.raises(smoke.SmokeFailure, match="marker marker-one"):
        validator(payload, "m1", "marker-one")


def test_validate_sqlite_summary_rows_keeps_raw_but_not_embedding_canary():
    smoke = load_smoke_module()
    canary = "RAW_ONLY_CANARY"
    rows = [
        {
            "id": "m1",
            "content": "summary text",
            "raw_content": f"raw text {canary}",
            "embedding_text": "L0: summary\nL1: compact",
        }
    ]

    result = smoke.validate_sqlite_summary_rows(rows, canary)

    assert result["sqlite_memory_ids"] == ["m1"]
    assert result["sqlite_raw_canary_rows"] == ["m1"]


def test_validate_sqlite_summary_rows_rejects_embedding_canary():
    smoke = load_smoke_module()
    canary = "RAW_ONLY_CANARY"

    with pytest.raises(smoke.SmokeFailure, match="embedding_text contains raw canary"):
        smoke.validate_sqlite_summary_rows(
            [
                {
                    "id": "m1",
                    "content": "summary text",
                    "raw_content": f"raw text {canary}",
                    "embedding_text": f"L0: {canary}",
                }
            ],
            canary,
        )


def test_validate_lancedb_summary_rows_requires_all_ids_and_no_canary():
    smoke = load_smoke_module()
    canary = "RAW_ONLY_CANARY"
    rows = [
        {"memory_id": "m1", "text": "compact summary"},
        {"memory_id": "m2", "text": "other compact summary"},
    ]

    result = smoke.validate_lancedb_summary_rows(rows, ["m1", "m2"], canary)

    assert result["lancedb_memory_ids"] == ["m1", "m2"]


def test_validate_lancedb_summary_rows_rejects_raw_canary():
    smoke = load_smoke_module()
    canary = "RAW_ONLY_CANARY"

    with pytest.raises(smoke.SmokeFailure, match="LanceDB text contains raw canary"):
        smoke.validate_lancedb_summary_rows(
            [{"memory_id": "m1", "text": f"compact {canary}"}],
            ["m1"],
            canary,
        )


def test_wait_for_lancedb_summary_rows_retries_missing_rows(monkeypatch):
    smoke = load_smoke_module()
    calls = {"count": 0}

    def fake_fetch(_path, _memory_ids):
        calls["count"] += 1
        if calls["count"] == 1:
            return []
        return [{"memory_id": "m1", "text": "compact summary"}]

    monkeypatch.setattr(smoke, "fetch_lancedb_smoke_rows", fake_fetch)

    result = asyncio.run(
        smoke.wait_for_lancedb_summary_rows(
            "unused",
            ["m1"],
            "RAW_ONLY_CANARY",
            timeout_s=1.0,
            interval_s=0.1,
        )
    )

    assert calls["count"] == 2
    assert result["lancedb_memory_ids"] == ["m1"]
    assert result["lancedb_attempts"] == 2


def test_resolve_existing_path_searches_parents(tmp_path, monkeypatch):
    smoke = load_smoke_module()
    root = tmp_path / "root"
    child = root / ".worktrees" / "branch"
    data = root / "data" / "db"
    data.mkdir(parents=True)
    child.mkdir(parents=True)
    db = data / "plastic_memory.db"
    db.write_text("", encoding="utf-8")
    monkeypatch.chdir(child)

    resolved = smoke.resolve_existing_path("data/db/plastic_memory.db")

    assert resolved == db


def test_resolve_lancedb_path_requires_memory_vectors_table(tmp_path, monkeypatch):
    smoke = load_smoke_module()
    root = tmp_path / "root"
    child = root / ".worktrees" / "branch"
    empty_lancedb = child / "data" / "lancedb"
    real_lancedb = root / "data" / "lancedb" / "memory_vectors.lance"
    empty_lancedb.mkdir(parents=True)
    real_lancedb.mkdir(parents=True)
    child.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(child)

    resolved = smoke.resolve_lancedb_path("data/lancedb")

    assert resolved == root / "data" / "lancedb"
