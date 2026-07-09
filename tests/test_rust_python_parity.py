from __future__ import annotations

from copy import deepcopy
import math
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.project_context import ProjectContext
from plastic_promise.mcp.tools.memory import _project_allowed


PROJECT_ID = "project:alpha"
OTHER_PROJECT_ID = "project:beta"
VECTOR_DIM = 1024


def _unit_vector(axis: int, scale: float = 1.0) -> list[float]:
    vector = [0.0] * VECTOR_DIM
    vector[axis] = scale
    return vector


FIXED_VECTORS: dict[str, list[float]] = {
    "project": _unit_vector(0),
    "bm25_en": _unit_vector(1),
    "bm25_zh": _unit_vector(2),
    "source": _unit_vector(3),
    "noise": _unit_vector(4),
    "hard_min": _unit_vector(5),
    "mmr": _unit_vector(6),
}


def _ensure_rust_extension_importable() -> None:
    """Allow local cargo release builds to satisfy `import context_engine_core`."""
    root = Path(__file__).resolve().parents[1]
    release_dir = root / "rust" / "context-engine-core" / "target" / "release"
    if release_dir.exists():
        sys.path.insert(0, str(release_dir))

    if sys.platform.startswith("win"):
        dll_path = release_dir / "context_engine_core.dll"
        pyd_path = release_dir / "context_engine_core.pyd"
        if dll_path.exists() and (
            not pyd_path.exists() or dll_path.stat().st_mtime > pyd_path.stat().st_mtime
        ):
            dll_bytes = dll_path.read_bytes()
            try:
                pyd_path.write_bytes(dll_bytes)
            except PermissionError:
                temp_dir = Path(tempfile.mkdtemp(prefix="context_engine_core_"))
                temp_pyd = temp_dir / "context_engine_core.pyd"
                temp_pyd.write_bytes(dll_bytes)
                sys.path.insert(0, str(temp_dir))
    sys.modules.pop("context_engine_core", None)


def _memory(
    memory_id: str,
    content: str,
    *,
    project_id: str = PROJECT_ID,
    visibility: str = "project",
    source: str = "user",
    source_class: str = "experience",
    vector_key: str | None = None,
    vector_scale: float = 1.0,
) -> dict[str, Any]:
    row = {
        "id": memory_id,
        "content": content,
        "memory_type": "experience",
        "source": source,
        "source_class": source_class,
        "project_id": project_id,
        "visibility": visibility,
        "tier": "L1",
        "scope": "global",
        "category": "fact",
        "domain": "building",
        "worth_success": 9,
        "worth_failure": 0,
        "worth_score": 1.0,
        "access_count": 0,
        "created_at": "",
        "last_accessed": "",
        "tags": [],
        "entity_ids": [],
    }
    if vector_key:
        vector = list(FIXED_VECTORS[vector_key])
        if 0.0 < vector_scale < 1.0:
            axis = next((index for index, value in enumerate(vector) if value), 0)
            vector[axis] = vector_scale
            vector[(axis + 100) % VECTOR_DIM] = math.sqrt(1.0 - vector_scale**2)
        else:
            vector = [value * vector_scale for value in vector]
        row["_vector"] = vector
    return row


FIXED_MEMORY_SNAPSHOT: dict[str, dict[str, Any]] = {
    "project_same": _memory(
        "project_same",
        "project isolation sentinel same project recall fixture",
        vector_key="project",
    ),
    "project_other_private": _memory(
        "project_other_private",
        "project isolation sentinel private cross project recall fixture",
        project_id=OTHER_PROJECT_ID,
        visibility="project",
        vector_key="project",
        vector_scale=0.98,
    ),
    "project_shared": _memory(
        "project_shared",
        "project isolation sentinel shared beta inspiration",
        project_id=OTHER_PROJECT_ID,
        visibility="shared",
        vector_key="project",
        vector_scale=0.96,
    ),
    "project_global": _memory(
        "project_global",
        "project isolation sentinel global fallback memory",
        project_id="project:legacy-global",
        visibility="global",
        vector_key="project",
    ),
    "bm25_english": _memory(
        "bm25_english",
        "english bm25 scanner reviews lexical parity fixture",
        vector_key="bm25_en",
    ),
    "bm25_chinese": _memory(
        "bm25_chinese",
        "中文 召回 检索 项目 隔离 语义 对齐",
        vector_key="bm25_zh",
    ),
    "source_user": _memory(
        "source_user",
        "source penalty sentinel user memory should remain",
        source="user",
        vector_key="source",
    ),
    "source_daemon": _memory(
        "source_daemon",
        "daemon telemetry evidence for source penalty sentinel should be demoted",
        source="maintenance_daemon",
        source_class="telemetry",
        vector_key="source",
        vector_scale=0.90,
    ),
    "source_excluded": _memory(
        "source_excluded",
        "source penalty sentinel skill session should be excluded",
        source="skill_session",
        source_class="telemetry",
        vector_key="source",
        vector_scale=0.96,
    ),
    "noise_audit": _memory(
        "noise_audit",
        "[maintenance_daemon] AUDIT trust=0.60 pipeline=0.80 domain=0.80",
        source="maintenance_daemon",
        source_class="telemetry",
        vector_key="noise",
    ),
    "noise_survivor": _memory(
        "noise_survivor",
        "noise filter sentinel useful recall evidence survives",
        vector_key="noise",
        vector_scale=0.98,
    ),
    "hard_keep": _memory(
        "hard_keep",
        "hard minimum sentinel user memory stays above floor",
        vector_key="hard_min",
    ),
    "hard_drop": _memory(
        "hard_drop",
        "daemon telemetry evidence for hard minimum sentinel falls below floor",
        source="maintenance_daemon",
        source_class="telemetry",
        vector_key="hard_min",
        vector_scale=0.90,
    ),
    "mmr_primary": _memory(
        "mmr_primary",
        "mmr duplicate sentinel same prefix alpha beta gamma",
        vector_key="mmr",
    ),
    "mmr_duplicate": _memory(
        "mmr_duplicate",
        "mmr duplicate sentinel same prefix alpha beta gamma",
        vector_key="mmr",
        vector_scale=0.99,
    ),
}


class _FakeVectorStore:
    def __init__(self, memories: dict[str, dict[str, Any]]) -> None:
        self._memories = memories

    def search(self, vector, k=20, scope=None):
        results = []
        for memory in self._memories.values():
            candidate = memory.get("_vector") or []
            if not candidate:
                continue
            score = _cosine(vector, candidate)
            if score <= 0.0:
                continue
            results.append(
                (
                    memory["id"],
                    score,
                    memory["content"],
                    memory.get("tier", "L1"),
                    memory.get("scope", "global"),
                )
            )
        results.sort(key=lambda row: row[1], reverse=True)
        return results[:k]

    def get_vector(self, memory_id: str):
        return self._memories.get(memory_id, {}).get("_vector") or []

    def count_rows(self):
        return sum(1 for memory in self._memories.values() if memory.get("_vector"))


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm <= 1e-12 or right_norm <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PP_CODE_MEMORY_ENABLED", "0")
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "plastic_memory.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "plastic_memory.lancedb"))
    monkeypatch.setenv("PP_QUERY_EXPANSION", "0")
    monkeypatch.setenv("PP_RERANK_DISABLED", "1")
    monkeypatch.setenv("PP_CANONICAL_HOT_LOOKUP", "0")
    monkeypatch.setenv("PP_CONTEXT_GATE", "0")
    monkeypatch.setenv("PP_CONTEXT_GATE_ENFORCE", "0")
    monkeypatch.setenv("PP_DECAY_IN_RANKING", "0")
    monkeypatch.setenv("PP_FTS_DISABLED", "1")
    monkeypatch.setenv("PP_SOURCE_FILTER", "1")
    monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
    monkeypatch.delenv("PP_SOURCE_EXCLUDE", raising=False)


@pytest.fixture
def rust_core():
    _ensure_rust_extension_importable()
    return pytest.importorskip("context_engine_core")


def _snapshot(*memory_ids: str) -> list[dict[str, Any]]:
    return [deepcopy(FIXED_MEMORY_SNAPSHOT[memory_id]) for memory_id in memory_ids]


def _python_pack(
    memories: list[dict[str, Any]],
    query: str,
    task_vector: list[float],
    *,
    project_id: str = PROJECT_ID,
    project_policy: str = "balanced",
    project_degraded: bool = False,
):
    engine = ContextEngine(use_sqlite=False)
    engine._memories = {memory["id"]: deepcopy(memory) for memory in memories}
    engine._ldb = _FakeVectorStore(engine._memories)
    engine._ensure_heavy_init = lambda: None
    engine._activate_principles = lambda task_type, task_description: []
    engine._inject_activated_to_graph = lambda activated, task_type: 0
    engine._graph_traversal = lambda task_type: []
    engine._fts_retrieval = lambda query, scope="global": []
    engine._apply_edge_feedback = lambda: None
    engine._maybe_adjust_tier = lambda memory_id: None
    engine._calc_freshness = lambda memory_id: "valid"
    engine._calc_decay_status = lambda memory_id, memory: "healthy"
    engine._compute_divergent_quality = lambda items, all_items: items

    pack = engine._supply_python(
        query,
        task_vector,
        task_type="general",
        scope="global",
        debug=True,
        project_id=project_id,
        project_policy=project_policy,
        project_degraded=project_degraded,
    )
    return _filter_python_pack_by_project(pack, engine, project_id, project_policy, project_degraded)


def _filter_python_pack_by_project(pack, engine, project_id, project_policy, project_degraded):
    project_ctx = ProjectContext(
        project_id=project_id,
        project_policy=project_policy,
        visibility="project",
        source_class="experience",
        degraded=project_degraded,
    )
    filtered = deepcopy(pack)
    for layer in ("core", "related", "divergent"):
        setattr(
            filtered,
            layer,
            [
                item
                for item in getattr(filtered, layer)
                if _project_allowed(item, project_ctx, layer, engine)
            ],
        )
    return filtered


def _rust_pack(
    rust_module,
    memories: list[dict[str, Any]],
    query: str,
    task_vector: list[float],
    *,
    project_id: str = PROJECT_ID,
    project_policy: str = "balanced",
    project_degraded: bool = False,
):
    if not hasattr(rust_module.ContextEngine, "new_with_backends"):
        pytest.skip("context_engine_core lacks new_with_backends snapshot constructor")
    engine = rust_module.ContextEngine.new_with_backends(":memory:", ":memory:")
    if hasattr(engine, "set_current_time"):
        engine.set_current_time("2026-07-09T00:00:00")
    if not hasattr(engine, "supply_with_project_context"):
        pytest.skip("context_engine_core lacks project-aware snapshot supply")
    return engine.supply_with_project_context(
        query,
        [float(value) for value in task_vector],
        "general",
        "global",
        [deepcopy(memory) for memory in memories],
        project_id,
        project_policy,
        project_degraded,
    )


def _normalized_items(pack, *, score_digits: int = 2):
    rows = []
    for layer in ("core", "related", "divergent"):
        for item in getattr(pack, layer):
            if getattr(item, "is_principle", False) or item.id.startswith("principle:"):
                continue
            rows.append((item.id, layer, round(float(item.relevance), score_digits)))
    return rows


def _item_ids(pack) -> list[str]:
    return [memory_id for memory_id, _layer, _score in _normalized_items(pack)]


def _counter_value(pack, key: str, default: int = 0) -> int:
    value = getattr(pack, "pipeline_stats", {}).get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _item_score(pack, memory_id: str) -> float:
    for _id, _layer, score in _normalized_items(pack, score_digits=4):
        if _id == memory_id:
            return score
    raise AssertionError(f"{memory_id} not present in normalized pack")


@pytest.mark.parametrize(
    ("name", "memory_ids", "query", "vector_key", "project_policy", "project_degraded", "expected_ids"),
    [
        (
            "strict project isolation",
            ("project_same", "project_other_private", "project_shared"),
            "project isolation sentinel",
            "project",
            "strict",
            False,
            ("project_same",),
        ),
        (
            "balanced project isolation",
            ("project_same", "project_other_private", "project_shared"),
            "project isolation sentinel",
            "project",
            "balanced",
            False,
            ("project_same", "project_shared"),
        ),
        (
            "degraded project isolation",
            ("project_same", "project_global"),
            "project isolation sentinel",
            "project",
            "balanced",
            True,
            ("project_global",),
        ),
        (
            "english bm25",
            ("bm25_english",),
            "english bm25 scanner",
            "bm25_en",
            "balanced",
            False,
            ("bm25_english",),
        ),
        (
            "chinese bm25",
            ("bm25_chinese",),
            "中文 召回 检索",
            "bm25_zh",
            "balanced",
            False,
            ("bm25_chinese",),
        ),
        (
            "noise filter",
            ("noise_audit", "noise_survivor"),
            "noise filter sentinel",
            "noise",
            "balanced",
            False,
            ("noise_survivor",),
        ),
    ],
)
def test_rust_python_semantic_parity_for_fixed_snapshot(
    rust_core,
    name,
    memory_ids,
    query,
    vector_key,
    project_policy,
    project_degraded,
    expected_ids,
):
    memories = _snapshot(*memory_ids)
    task_vector = FIXED_VECTORS[vector_key]

    python_pack = _python_pack(
        memories,
        query,
        task_vector,
        project_policy=project_policy,
        project_degraded=project_degraded,
    )
    rust_pack = _rust_pack(
        rust_core,
        memories,
        query,
        task_vector,
        project_policy=project_policy,
        project_degraded=project_degraded,
    )

    python_items = _normalized_items(python_pack)
    rust_items = _normalized_items(rust_pack)

    assert [item[0] for item in python_items] == list(expected_ids), name
    assert [item[0] for item in rust_items] == [item[0] for item in python_items], name
    assert "after_noise_filter" in rust_pack.pipeline_stats
    assert "after_source_filter" in rust_pack.pipeline_stats
    assert "after_hard_score_filter" in rust_pack.pipeline_stats
    assert "stage_timing_ms" in rust_pack.pipeline_stats
    assert "fallback_reason" in rust_pack.pipeline_stats
    if "bm25" in name:
        assert _counter_value(python_pack, "bm25_count") == 1
        assert _counter_value(rust_pack, "bm25_hits", _counter_value(rust_pack, "bm25_count")) >= 1
    if name == "noise filter":
        assert _counter_value(python_pack, "after_noise_filter") == 1
        rust_noise_rows = [
            row
            for row in rust_pack.per_item_stats
            if row.get("id") == "noise_audit" and row.get("filter_reason") == "noise"
        ]
        assert rust_noise_rows


def test_source_penalty_is_semantically_equivalent(rust_core, monkeypatch):
    monkeypatch.setenv("PP_HARD_MIN_SCORE", "0.31")
    memories = _snapshot("source_user", "source_daemon")
    query = "source penalty sentinel"
    task_vector = FIXED_VECTORS["source"]

    python_pack = _python_pack(memories, query, task_vector)
    rust_pack = _rust_pack(rust_core, memories, query, task_vector)

    assert _item_ids(rust_pack) == _item_ids(python_pack)
    assert "source_user" in _item_ids(python_pack)

    rust_penalties = {
        row.get("id"): row.get("source_penalty")
        for row in getattr(rust_pack, "per_item_stats", [])
        if row.get("id")
    }
    if rust_penalties:
        assert float(rust_penalties["source_daemon"]) == pytest.approx(0.3)
    assert any(
        row.get("id") == "source_daemon"
        and row.get("filter_reason") == "below_hard_min_score"
        for row in rust_pack.per_item_stats
    )


def test_source_exclusion_matches_python_reference(rust_core, monkeypatch):
    monkeypatch.setenv("PP_SOURCE_EXCLUDE", "skill_session")
    memories = _snapshot("source_user", "source_excluded")
    query = "source penalty sentinel"
    task_vector = FIXED_VECTORS["source"]

    python_pack = _python_pack(memories, query, task_vector)
    rust_pack = _rust_pack(rust_core, memories, query, task_vector)

    assert _item_ids(rust_pack) == _item_ids(python_pack)
    assert _item_ids(rust_pack) == ["source_user"]
    assert _counter_value(python_pack, "after_source_filter") == 1
    assert any(
        row.get("id") == "source_excluded" and row.get("filter_reason") == "source_excluded"
        for row in rust_pack.per_item_stats
    )


def test_hard_min_score_filters_after_source_penalty(rust_core, monkeypatch):
    monkeypatch.setenv("PP_HARD_MIN_SCORE", "0.50")
    memories = _snapshot("hard_keep", "hard_drop")
    query = "hard minimum sentinel"
    task_vector = FIXED_VECTORS["hard_min"]

    python_pack = _python_pack(memories, query, task_vector)
    rust_pack = _rust_pack(rust_core, memories, query, task_vector)

    assert _item_ids(rust_pack) == _item_ids(python_pack)
    assert _item_ids(rust_pack) == ["hard_keep"]
    assert _counter_value(python_pack, "after_hard_score_filter") == 1
    assert any(
        row.get("id") == "hard_drop" and row.get("filter_reason") == "below_hard_min_score"
        for row in rust_pack.per_item_stats
    )


def test_mmr_demotes_duplicate_without_leaking_above_primary(rust_core):
    memories = _snapshot("mmr_primary", "mmr_duplicate")
    query = "mmr duplicate sentinel"
    task_vector = FIXED_VECTORS["mmr"]

    python_pack = _python_pack(memories, query, task_vector)
    rust_pack = _rust_pack(rust_core, memories, query, task_vector)

    assert set(_item_ids(rust_pack)) == set(_item_ids(python_pack))
    python_scores = sorted(
        [_item_score(python_pack, "mmr_primary"), _item_score(python_pack, "mmr_duplicate")]
    )
    rust_scores = sorted(
        [_item_score(rust_pack, "mmr_primary"), _item_score(rust_pack, "mmr_duplicate")]
    )
    assert python_scores[0] < python_scores[1]
    assert rust_scores[0] < rust_scores[1]
    assert _counter_value(rust_pack, "mmr_demoted") >= 1
