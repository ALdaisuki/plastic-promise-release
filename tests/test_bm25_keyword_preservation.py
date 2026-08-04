from __future__ import annotations

from plastic_promise.core.context_engine import (
    ContextItem,
    _keyword_preservation_candidates,
    _keyword_preservation_settings,
    _restore_keyword_candidates,
)


def _item(memory_id, *, relevance=0.1, bm25_score=0.0):
    return ContextItem(
        id=memory_id,
        content=memory_id,
        relevance=relevance,
        bm25_score=bm25_score,
    )


def test_keyword_preservation_selects_only_bounded_high_confidence_hits():
    items = [
        _item("exact-name", bm25_score=0.99),
        _item("second-name", bm25_score=0.91),
        _item("below-threshold", bm25_score=0.71),
        _item("third-name", bm25_score=0.85),
    ]

    selected = _keyword_preservation_candidates(
        items,
        enabled=True,
        threshold=0.72,
        limit=2,
    )

    assert list(selected) == ["exact-name", "second-name"]
    assert (
        _keyword_preservation_candidates(
            items,
            enabled=False,
            threshold=0.0,
            limit=5,
        )
        == {}
    )


def test_keyword_restore_reinserts_removed_hit_once_at_relevance_floor():
    survivor = _item("survivor", relevance=0.75, bm25_score=0.4)
    removed = _item("exact-name", relevance=0.1, bm25_score=0.99)

    restored, count = _restore_keyword_candidates(
        [survivor],
        {"exact-name": removed},
        relevance_floor=0.4,
    )
    replayed, replay_count = _restore_keyword_candidates(
        restored,
        {"exact-name": removed},
        relevance_floor=0.4,
    )

    assert count == 1
    assert [(item.id, item.relevance) for item in restored] == [
        ("survivor", 0.75),
        ("exact-name", 0.4),
    ]
    assert removed.keyword_preserved is True
    assert replay_count == 0
    assert [item.id for item in replayed] == ["survivor", "exact-name"]


def test_keyword_preservation_settings_are_bounded_and_fail_safe(monkeypatch):
    monkeypatch.setenv("PP_BM25_PRESERVATION", "1")
    monkeypatch.setenv("PP_BM25_PRESERVATION_THRESHOLD", "not-a-number")
    monkeypatch.setenv("PP_BM25_PRESERVATION_LIMIT", "not-an-integer")

    assert _keyword_preservation_settings() == (True, 0.72, 2)

    monkeypatch.setenv("PP_BM25_PRESERVATION_THRESHOLD", "9")
    monkeypatch.setenv("PP_BM25_PRESERVATION_LIMIT", "99")

    assert _keyword_preservation_settings() == (True, 1.0, 5)
