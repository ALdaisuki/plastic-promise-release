import copy
import json

import pytest

from plastic_promise.core.context_engine import OrdinaryMemoryConflict
from plastic_promise.core.pack_index import pack_import_with_strategy
from plastic_promise.pack import import_pack


class _PackEngine:
    def __init__(self, memory=None):
        self.memory = copy.deepcopy(memory)
        self.mutations = []
        self.patches = []
        self.created = []

    def get_memory_dict_for_review(self, memory_id):
        if self.memory is None or self.memory["id"] != memory_id:
            return None
        return copy.deepcopy(self.memory)

    def mutate_ordinary_source(self, memory_id, **kwargs):
        self.mutations.append((memory_id, copy.deepcopy(kwargs)))
        self.memory["content"] = kwargs["content"]
        self.memory.update(copy.deepcopy(kwargs["policy_replacements"]))
        return object()

    def patch_ordinary_memory(self, memory_id, **kwargs):
        self.patches.append((memory_id, copy.deepcopy(kwargs)))
        self.memory.update(copy.deepcopy(kwargs["replacements"]))
        return copy.deepcopy(self.memory)

    def create_ordinary_if_absent(self, record):
        if self.memory is not None and self.memory["id"] == record["id"]:
            raise OrdinaryMemoryConflict("ordinary_memory_already_exists")
        self.created.append(copy.deepcopy(record))
        self.memory = copy.deepcopy(record)
        return record["id"]


def _canonical_memory():
    return {
        "id": "pack-memory",
        "content": "canonical before",
        "memory_type": "experience",
        "source": "canonical-source",
        "owner": "private-owner",
        "tier": "L3",
        "scope": "agent:private",
        "category": "decision",
        "tags": ["private", "old"],
        "domain": "governing",
        "importance": 0.93,
        "entity_ids": ["entity:private"],
        "created_at": "2026-07-01T00:00:00Z",
        "access_count": 5,
        "worth_success": 4,
        "worth_failure": 1,
        "activation_weight": 0.8,
        "decay_multiplier": 0.75,
        "effective_half_life": 90.0,
        "last_accessed": "2026-07-02T00:00:00Z",
        "project_id": "project:private",
        "visibility": "private",
        "source_class": "experience",
        "created_by_call_id": "call:private",
        "origin_kind": "pack-test",
        "origin_uri": "memory://private",
        "origin_ref": "ref:private",
        "origin_hash": "sha256:private",
        "parent_memory_ids": ["parent:private"],
        "metadata_json": {"private": True},
        "raw_content": "raw before",
        "l0_abstract": "abstract before",
        "l1_summary": "summary before",
        "l2_content": "canonical before",
        "embedding_text": "embedding before",
        "embedding_hash": "sha256:embedding-before",
        "search_text": "search before",
    }


def _write_pack(tmp_path, memory, *, version="2.0"):
    path = tmp_path / "pack.json"
    path.write_text(
        json.dumps({"version": version, "memories": [memory]}),
        encoding="utf-8",
    )
    return str(path)


def test_pack_replace_uses_source_mutation_and_preserves_unowned_fields(tmp_path):
    before = _canonical_memory()
    engine = _PackEngine(before)
    path = _write_pack(
        tmp_path,
        {
            "id": before["id"],
            "content": "replacement content",
            "tags": ["pack", "replacement"],
            "domain": "building",
        },
    )

    result = pack_import_with_strategy(path, engine, strategy="replace")

    assert result["imported"] == 1
    assert len(engine.mutations) == 1
    _, mutation = engine.mutations[0]
    assert mutation["operation"] == "replace_content"
    assert mutation["expected_project_id"] == before["project_id"]
    assert mutation["require_source_available"] is True
    assert mutation["policy_replacements"] == {
        "category": before["category"],
        "domain": "building",
        "tags": ["pack", "replacement"],
        "tier": before["tier"],
    }
    assert engine.memory["content"] == "replacement content"
    assert engine.memory["domain"] == "building"
    assert engine.memory["tags"] == ["pack", "replacement"]
    for field in (
        "project_id",
        "visibility",
        "source_class",
        "owner",
        "origin_hash",
        "parent_memory_ids",
        "importance",
    ):
        assert engine.memory[field] == before[field]


def test_pack_merge_uses_field_scoped_metadata_patch(tmp_path):
    before = _canonical_memory()
    engine = _PackEngine(before)
    path = _write_pack(
        tmp_path,
        {
            "id": before["id"],
            "content": "ignored for merge",
            "tags": ["pack", "old"],
            "domain": "building",
        },
    )

    result = pack_import_with_strategy(path, engine, strategy="merge")

    assert result["merged"] == 1
    assert not engine.mutations
    _, patch = engine.patches[0]
    assert patch["replacements"] == {
        "tags": ["old", "pack", "private"],
        "domain": "building",
    }
    assert patch["expected_project_id"] == before["project_id"]
    assert patch["expected_tags"] == before["tags"]
    assert patch["require_source_available"] is True
    assert engine.memory["content"] == before["content"]
    assert engine.memory["project_id"] == before["project_id"]


@pytest.mark.parametrize(
    ("strategy", "content"),
    [("replace", "changed content must not reach the coordinator"), ("merge", "ignored")],
)
def test_pack_existing_memory_rejects_availability_changing_tags(tmp_path, strategy, content):
    before = _canonical_memory()
    engine = _PackEngine(before)
    path = _write_pack(
        tmp_path,
        {
            "id": before["id"],
            "content": content,
            "tags": ["pack", "status:wrong"],
            "domain": "building",
        },
    )

    with pytest.raises(
        OrdinaryMemoryConflict,
        match="ordinary_patch_availability_change_requires_coordinator",
    ):
        pack_import_with_strategy(path, engine, strategy=strategy)

    assert engine.memory == before
    assert not engine.mutations
    assert not engine.patches


def test_legacy_pack_import_does_not_replace_existing_id(tmp_path):
    before = _canonical_memory()
    engine = _PackEngine(before)
    path = tmp_path / "legacy-pack.json"
    path.write_text(
        json.dumps(
            {
                "pack": {"name": "legacy"},
                "memories": [
                    {
                        "id": before["id"],
                        "content": "must not replace",
                        "type": "procedure",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = import_pack(engine, str(path))

    assert result["imported"] == 0
    assert engine.memory == before
    assert not engine.created
