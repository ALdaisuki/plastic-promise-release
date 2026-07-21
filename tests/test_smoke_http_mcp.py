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


def test_smoke_content_keeps_run_identity_out_of_the_stable_retrieval_query():
    smoke = load_smoke_module()
    marker = "http_mcp_smoke_atomic_marker"
    canary = f"RAW_SQL_ONLY_CANARY_{marker}"

    content = smoke.build_smoke_content(marker, canary)
    query = smoke.build_retrieval_query()

    assert ". " not in content
    assert not content.endswith(".")
    assert "\n" not in content
    assert marker in content
    assert canary in content
    assert marker not in query
    assert canary not in query
    assert "canonical storage" in query


def test_leaf_error_unwraps_nested_exception_groups():
    smoke = load_smoke_module()

    class NestedError(Exception):
        def __init__(self, *exceptions):
            super().__init__("nested")
            self.exceptions = exceptions

    cause = smoke.SmokeFailure("stored memory missing from recall")
    nested = NestedError(NestedError(cause))

    assert smoke._leaf_error(nested) is cause


def test_parse_mcp_json_content_rejects_malformed_json():
    smoke = load_smoke_module()
    content = [SimpleNamespace(type="text", text="not-json")]

    with pytest.raises(smoke.SmokeFailure, match="non-JSON"):
        smoke.parse_mcp_json_content(content, "memory_store")


def test_validate_store_requires_migrated_pipeline_count():
    smoke = load_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="embedded->migrated"):
        smoke.validate_store(
            {
                "stored": True,
                "memory_id": "m1",
                "submitted_memory_id": "m1",
                "deduplicated": False,
                "created": True,
                "pipeline": {},
            }
        )

    result = smoke.validate_store(
        {
            "stored": True,
            "memory_id": "m1",
            "submitted_memory_id": "m1",
            "deduplicated": False,
            "created": True,
            "pipeline": {f"embedded{chr(0x2192)}migrated": 1},
        }
    )

    assert result["memory_id"] == "m1"
    assert result["canonical_memory_id"] == "m1"
    assert result["submitted_memory_id"] == "m1"
    assert result["migrated"] == 1


def test_validate_store_accepts_zero_migration_for_a_deduplicated_submission():
    smoke = load_smoke_module()

    result = smoke.validate_store(
        {
            "stored": True,
            "memory_id": "canonical-old",
            "submitted_memory_id": "fuzzy-new",
            "deduplicated": True,
            "created": False,
            "pipeline": {f"embedded{chr(0x2192)}migrated": 0},
        }
    )

    assert result == {
        "memory_id": "canonical-old",
        "canonical_memory_id": "canonical-old",
        "submitted_memory_id": "fuzzy-new",
        "project_id": None,
        "pipeline": {f"embedded{chr(0x2192)}migrated": 0},
        "migrated": 0,
        "deduplicated": True,
        "created": False,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"submitted_memory_id": ""},
        {"deduplicated": True, "created": True},
        {"deduplicated": False, "created": False},
        {"memory_id": "canonical-other"},
    ],
)
def test_validate_store_rejects_incoherent_identity_mapping(overrides):
    smoke = load_smoke_module()
    payload = {
        "stored": True,
        "memory_id": "fuzzy-new",
        "submitted_memory_id": "fuzzy-new",
        "deduplicated": False,
        "created": True,
        "pipeline": {f"embedded{chr(0x2192)}migrated": 1},
    }
    payload.update(overrides)

    with pytest.raises(smoke.SmokeFailure, match="identity|flags"):
        smoke.validate_store(payload)


@pytest.mark.parametrize("validator_name", ["validate_recall", "validate_context"])
def test_retrieval_validators_require_every_persisted_memory_as_evidence(validator_name):
    smoke = load_smoke_module()
    validator = getattr(smoke, validator_name)
    payload = {
        "core": [
            {
                "id": "m1",
                "content": "stable canonical storage evidence",
            },
            {
                "id": "m2",
                "content": "stable compact index evidence",
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

    result = validator(payload, ["m1", "m2"])

    assert result["observed_memory_ids"] == ["m1", "m2"]
    assert result["evidence_locations"] == {"m1": ["core"], "m2": ["core"]}


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
        validator(payload, ["m1"])


@pytest.mark.parametrize("validator_name", ["validate_recall", "validate_context"])
def test_retrieval_validators_reject_audit_id_without_context_content(validator_name):
    smoke = load_smoke_module()
    validator = getattr(smoke, validator_name)
    payload = {
        "core": [],
        "related": [],
        "divergent": [],
        "raw_evidence": [{"id": "m1"}],
    }
    if validator_name == "validate_context":
        payload["project_context"] = {"degraded": False}
        payload["audit_metadata"] = {}
    else:
        payload["audit"] = {}

    with pytest.raises(smoke.SmokeFailure, match="context evidence"):
        validator(payload, ["m1"])


def test_validate_sqlite_summary_rows_maps_created_and_split_rows():
    smoke = load_smoke_module()
    marker = "marker-one"
    canary = "RAW_ONLY_CANARY"
    rows = [
        {
            "id": "m1",
            "content": "summary text",
            "raw_content": f"raw text {marker} {canary}",
            "embedding_text": "L0: summary\nL1: compact",
            "search_text": "compact summary",
            "origin_ref": marker,
        },
        {
            "id": "m2",
            "content": "second extracted fact",
            "raw_content": f"raw text {marker} {canary}",
            "embedding_text": "second compact summary",
            "search_text": "second compact summary",
            "origin_ref": marker,
        },
    ]
    store = {
        "canonical_memory_id": "m1",
        "submitted_memory_id": "m1",
        "deduplicated": False,
        "created": True,
        "migrated": 2,
    }

    result = smoke.validate_sqlite_summary_rows(rows, marker, canary, store)

    assert result["sqlite_marker_memory_ids"] == ["m1", "m2"]
    assert result["sqlite_raw_canary_rows"] == ["m1", "m2"]
    assert result["split_memory_ids"] == ["m2"]
    assert result["retrieval_memory_ids"] == ["m1", "m2"]
    assert result["lancedb_memory_ids"] == ["m1", "m2"]


def test_validate_sqlite_summary_rows_accepts_reused_canonical_and_split_sibling():
    smoke = load_smoke_module()
    marker = "marker-two"
    canary = "RAW_ONLY_CANARY"
    rows = [
        {
            "id": "canonical-old",
            "content": "stable old canonical",
            "raw_content": "old provenance",
            "embedding_text": "stable compact summary",
            "search_text": "stable compact summary",
            "origin_ref": "older-run",
        },
        {
            "id": "split-new",
            "content": "new sibling",
            "raw_content": f"raw {marker} {canary}",
            "embedding_text": "new compact sibling",
            "search_text": "new compact sibling",
            "origin_ref": marker,
        },
    ]
    store = {
        "canonical_memory_id": "canonical-old",
        "submitted_memory_id": "fuzzy-discarded",
        "deduplicated": True,
        "created": False,
        "migrated": 1,
    }

    result = smoke.validate_sqlite_summary_rows(rows, marker, canary, store)

    assert result["canonical_reused"] is True
    assert result["sqlite_marker_memory_ids"] == ["split-new"]
    assert result["split_memory_ids"] == ["split-new"]
    assert result["retrieval_memory_ids"] == ["canonical-old", "split-new"]


def test_validate_sqlite_summary_rows_accepts_fully_deduplicated_submission():
    smoke = load_smoke_module()
    rows = [
        {
            "id": "canonical-old",
            "content": "stable old canonical",
            "raw_content": "old provenance",
            "embedding_text": "stable compact summary",
            "search_text": "stable compact summary",
            "origin_ref": "older-run",
        }
    ]
    store = {
        "canonical_memory_id": "canonical-old",
        "submitted_memory_id": "fuzzy-discarded",
        "deduplicated": True,
        "created": False,
        "migrated": 0,
    }

    result = smoke.validate_sqlite_summary_rows(
        rows, "marker-three", "RAW_ONLY_CANARY", store
    )

    assert result["sqlite_marker_memory_ids"] == []
    assert result["retrieval_memory_ids"] == ["canonical-old"]
    assert result["lancedb_memory_ids"] == ["canonical-old"]


def test_validate_sqlite_summary_rows_rejects_embedding_canary():
    smoke = load_smoke_module()
    marker = "marker-one"
    canary = "RAW_ONLY_CANARY"

    with pytest.raises(smoke.SmokeFailure, match="compact index text contains raw identity"):
        smoke.validate_sqlite_summary_rows(
            [
                {
                    "id": "m1",
                    "content": "summary text",
                    "raw_content": f"raw text {marker} {canary}",
                    "embedding_text": f"L0: {canary}",
                    "search_text": "summary text",
                    "origin_ref": marker,
                }
            ],
            marker,
            canary,
            {
                "canonical_memory_id": "m1",
                "submitted_memory_id": "m1",
                "deduplicated": False,
                "created": True,
                "migrated": 1,
            },
        )


def test_validate_lancedb_summary_rows_requires_all_ids_and_no_canary():
    smoke = load_smoke_module()
    canary = "RAW_ONLY_CANARY"
    rows = [
        {"memory_id": "m1", "text": "compact summary"},
        {"memory_id": "m2", "text": "other compact summary"},
    ]

    result = smoke.validate_lancedb_summary_rows(
        rows, ["m1", "m2"], "marker-one", canary
    )

    assert result["lancedb_memory_ids"] == ["m1", "m2"]


def test_validate_lancedb_summary_rows_rejects_raw_canary():
    smoke = load_smoke_module()
    canary = "RAW_ONLY_CANARY"

    with pytest.raises(smoke.SmokeFailure, match="LanceDB text contains raw canary"):
        smoke.validate_lancedb_summary_rows(
            [{"memory_id": "m1", "text": f"compact {canary}"}],
            ["m1"],
            "marker-one",
            canary,
        )


def test_validate_lancedb_summary_rows_rejects_run_marker():
    smoke = load_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="raw identity"):
        smoke.validate_lancedb_summary_rows(
            [{"memory_id": "m1", "text": "compact marker-one"}],
            ["m1"],
            "marker-one",
            "RAW_ONLY_CANARY",
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
            "marker-one",
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
