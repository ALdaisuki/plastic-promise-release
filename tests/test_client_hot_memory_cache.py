from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from plastic_promise.client.hot_memory_cache import HotMemoryCacheSelection, ReadOnlyHotMemoryCache


class _Clock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value


class _ObservableLock:
    def __init__(self) -> None:
        self.raw = threading.Lock()
        self.entered = threading.Event()

    def __enter__(self):
        self.entered.set()
        self.raw.acquire()
        return self

    def __exit__(self, *_exc_info):
        self.raw.release()


def _digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _store(
    cache: ReadOnlyHotMemoryCache,
    memory_id: str,
    content: str,
    *,
    version: int = 7,
    selection: HotMemoryCacheSelection | None = None,
):
    return cache.store_verified(
        project_id="project:alpha",
        memory_id=memory_id,
        memory_version=version,
        content_hash=_digest(content),
        content=content,
        request_context=cache.capture_request_context("project:alpha"),
        selection=selection or HotMemoryCacheSelection(retain=True),
    )


def test_cache_requires_project_and_matching_server_response_hash():
    cache = ReadOnlyHotMemoryCache()

    with pytest.raises(RuntimeError, match="client_cache_project_not_selected"):
        _store(cache, "memory:one", "server text")

    cache.switch_project("project:alpha")
    with pytest.raises(ValueError, match="client_cache_content_hash_mismatch"):
        cache.store_verified(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=7,
            content_hash=_digest("different text"),
            content="server text",
            request_context=cache.capture_request_context("project:alpha"),
            selection=HotMemoryCacheSelection(retain=True),
        )

    entry = _store(cache, "memory:one", "server text")
    assert entry.content == "server text"
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=7,
            content_hash=_digest("server text"),
        )
        == "server text"
    )
    assert "server text" not in repr(entry)
    assert "content=" not in repr(entry)


def test_cache_is_ttl_and_lru_bounded():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        max_entries=2,
        max_total_bytes=32,
        max_entry_bytes=16,
        ttl_seconds=5,
        clock=clock,
    )
    cache.switch_project("project:alpha")
    _store(cache, "memory:a", "alpha")
    _store(cache, "memory:b", "bravo")
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:a",
            memory_version=7,
            content_hash=_digest("alpha"),
        )
        == "alpha"
    )

    _store(cache, "memory:c", "charlie")
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:b",
            memory_version=7,
            content_hash=_digest("bravo"),
        )
        is None
    )

    clock.value += 6
    assert cache.stats()["entries"] == 0
    assert cache.stats()["total_bytes"] == 0


def test_agent_selection_and_system_cadence_both_control_retention():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        ttl_seconds=300,
        selection_interval_seconds=10,
        clock=clock,
    )
    cache.switch_project("project:alpha")

    assert (
        _store(
            cache,
            "memory:skip",
            "not selected",
            selection=HotMemoryCacheSelection(retain=False),
        )
        is None
    )
    entry = _store(
        cache,
        "memory:selected",
        "selected text",
        selection=HotMemoryCacheSelection(retain=True, priority=0.0, requested_ttl_seconds=90),
    )
    assert entry is not None
    assert entry.expires_at == pytest.approx(100.0)
    assert entry.selection_due_at == pytest.approx(20.0)

    clock.value += 11
    assert cache.stats()["entries"] == 0
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:selected",
            memory_version=7,
            content_hash=_digest("selected text"),
        )
        is None
    )


def test_trusted_server_response_keeps_agent_selection_separate():
    cache = ReadOnlyHotMemoryCache()
    cache.switch_project("project:alpha")
    response = {
        "project_id": "project:alpha",
        "memory_id": "memory:one",
        "memory_version": 7,
        "content_hash": _digest("server text"),
        "content": "server text",
        # A transport response cannot nominate itself for local retention.
        "cache_hint": {"retain": True, "priority": 1.0},
    }

    assert (
        cache.store_server_response(
            response,
            request_context=cache.capture_request_context("project:alpha"),
            selection=HotMemoryCacheSelection(retain=False),
        )
        is None
    )
    assert cache.stats()["entries"] == 0

    selected_response = {
        **response,
        "memory_id": "memory:two",
    }
    entry = cache.store_server_response(
        selected_response,
        request_context=cache.capture_request_context("project:alpha"),
        selection=HotMemoryCacheSelection(retain=True, priority=0.7),
    )
    assert entry is not None
    assert entry.content == "server text"
    assert entry.priority == pytest.approx(0.7)


def test_trusted_server_response_requires_canonical_identity_fields():
    cache = ReadOnlyHotMemoryCache()
    cache.switch_project("project:alpha")

    with pytest.raises(ValueError, match="client_cache_server_response_incomplete"):
        cache.store_server_response(
            {"project_id": "project:alpha", "memory_id": "memory:one"},
            request_context=cache.capture_request_context("project:alpha"),
            selection=HotMemoryCacheSelection(retain=True),
        )

    with pytest.raises(ValueError, match="client_cache_content_hash_mismatch"):
        cache.store_server_response(
            {
                "project_id": "project:alpha",
                "memory_id": "memory:one",
                "memory_version": 7,
                "content_hash": _digest("different text"),
                "content": "server text",
            },
            request_context=cache.capture_request_context("project:alpha"),
            selection=HotMemoryCacheSelection(retain=True),
        )


def test_agent_preference_and_system_frequency_jointly_select_at_cadence():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        max_entries=4,
        max_total_bytes=64,
        max_entry_bytes=16,
        ttl_seconds=120,
        selection_interval_seconds=10,
        system_accesses_per_interval=4,
        clock=clock,
    )
    cache.switch_project("project:alpha")
    _store(
        cache,
        "memory:agent-high-one-hit",
        "high-one-hit-001",
        selection=HotMemoryCacheSelection(retain=True, priority=1.0),
    )
    _store(
        cache,
        "memory:agent-low-one-hit",
        "low-one-hit--001",
        selection=HotMemoryCacheSelection(retain=True, priority=0.02),
    )
    _store(
        cache,
        "memory:agent-low-two-hits",
        "low-two-hits-001",
        selection=HotMemoryCacheSelection(retain=True, priority=0.02),
    )
    _store(
        cache,
        "memory:agent-high-zero-hits",
        "high-zero-hit001",
        selection=HotMemoryCacheSelection(retain=True, priority=1.0),
    )

    clock.value = 15.0
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:agent-high-one-hit",
            memory_version=7,
            content_hash=_digest("high-one-hit-001"),
        )
        == "high-one-hit-001"
    )
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:agent-low-one-hit",
            memory_version=7,
            content_hash=_digest("low-one-hit--001"),
        )
        == "low-one-hit--001"
    )
    for _ in range(2):
        assert (
            cache.get(
                project_id="project:alpha",
                memory_id="memory:agent-low-two-hits",
                memory_version=7,
                content_hash=_digest("low-two-hits-001"),
            )
            == "low-two-hits-001"
        )

    clock.value = 21.0
    assert cache.stats()["entries"] == 2
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:agent-high-one-hit",
            memory_version=7,
            content_hash=_digest("high-one-hit-001"),
        )
        == "high-one-hit-001"
    )
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:agent-low-one-hit",
            memory_version=7,
            content_hash=_digest("low-one-hit--001"),
        )
        is None
    )
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:agent-low-two-hits",
            memory_version=7,
            content_hash=_digest("low-two-hits-001"),
        )
        == "low-two-hits-001"
    )
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:agent-high-zero-hits",
            memory_version=7,
            content_hash=_digest("high-zero-hit001"),
        )
        is None
    )


def test_initial_admission_uses_joint_system_and_agent_score():
    cache = ReadOnlyHotMemoryCache(max_entry_bytes=16)
    cache.switch_project("project:alpha")

    assert (
        _store(
            cache,
            "memory:joint-admission",
            "sixteen-byte-001",
            selection=HotMemoryCacheSelection(retain=True, priority=0.0),
        )
        is None
    )
    assert (
        _store(
            cache,
            "memory:joint-admission",
            "sixteen-byte-001",
            selection=HotMemoryCacheSelection(retain=True, priority=1.0),
        )
        is not None
    )


def test_agent_preference_cannot_bypass_full_system_signal_floor():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        max_entry_bytes=16,
        ttl_seconds=120,
        selection_interval_seconds=10,
        clock=clock,
    )
    cache.switch_project("project:alpha")
    assert (
        _store(
            cache,
            "memory:agent-high-cold",
            "tiny",
            selection=HotMemoryCacheSelection(retain=True, priority=1.0),
        )
        is not None
    )

    clock.value = 21.0
    assert cache.stats()["entries"] == 0


def test_retain_false_invalidates_every_cached_version_without_content_body():
    cache = ReadOnlyHotMemoryCache()
    cache.switch_project("project:alpha")
    _store(cache, "memory:one", "old text", version=7)

    assert (
        cache.store_verified(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=8,
            content_hash=_digest("new text"),
            content=None,
            request_context=cache.capture_request_context("project:alpha"),
            selection=HotMemoryCacheSelection(retain=False),
        )
        is None
    )
    assert cache.stats()["entries"] == 0
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=7,
            content_hash=_digest("old text"),
        )
        is None
    )


def test_positive_refresh_cannot_postpone_system_selection():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        ttl_seconds=100,
        selection_interval_seconds=10,
        clock=clock,
    )
    cache.switch_project("project:alpha")
    first = _store(cache, "memory:one", "server text")
    assert first is not None

    clock.value = 15.0
    repeated = _store(
        cache,
        "memory:one",
        "server text",
        selection=HotMemoryCacheSelection(
            retain=True,
            priority=1.0,
            requested_ttl_seconds=20,
        ),
    )
    assert repeated is not first
    assert repeated.priority == pytest.approx(1.0)
    assert repeated.expires_at == pytest.approx(35.0)
    assert repeated.access_count == first.access_count
    assert repeated.last_accessed_at == first.last_accessed_at
    assert repeated.system_score == first.system_score
    assert repeated.selection_due_at == pytest.approx(20.0)

    clock.value = 21.0
    assert cache.stats()["entries"] == 0


def test_system_eviction_cannot_be_reinserted_until_next_cadence():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        ttl_seconds=100,
        selection_interval_seconds=10,
        clock=clock,
    )
    cache.switch_project("project:alpha")
    _store(
        cache,
        "memory:one",
        "server text",
        selection=HotMemoryCacheSelection(retain=True, priority=0.0),
    )

    clock.value = 21.0
    assert cache.stats()["entries"] == 0
    assert _store(cache, "memory:one", "server text") is None

    clock.value = 31.0
    replacement = _store(cache, "memory:one", "server text")
    assert replacement is not None


def test_version_high_watermark_and_agent_eviction_block_late_responses():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        ttl_seconds=100,
        selection_interval_seconds=10,
        clock=clock,
    )
    cache.switch_project("project:alpha")
    _store(cache, "memory:one", "new text", version=8)

    clock.value = 11.0
    assert _store(cache, "memory:one", "old text", version=7) is None
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=8,
            content_hash=_digest("new text"),
        )
        == "new text"
    )

    clock.value = 12.0
    cache.store_verified(
        project_id="project:alpha",
        memory_id="memory:one",
        memory_version=8,
        content_hash=_digest("new text"),
        content=None,
        request_context=cache.capture_request_context("project:alpha"),
        selection=HotMemoryCacheSelection(retain=False),
    )
    assert _store(cache, "memory:one", "old text", version=7) is None
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=8,
            content_hash=_digest("new text"),
        )
        is None
    )

    clock.value = 20.1
    assert _store(cache, "memory:one", "latest text", version=9) is not None
    clock.value = 21.0
    cache.store_verified(
        project_id="project:alpha",
        memory_id="memory:one",
        memory_version=8,
        content_hash=_digest("new text"),
        content=None,
        request_context=cache.capture_request_context("project:alpha"),
        selection=HotMemoryCacheSelection(retain=False),
    )
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=9,
            content_hash=_digest("latest text"),
        )
        is None
    )


def test_response_from_logged_out_session_cannot_fill_same_project_after_relogin():
    cache = ReadOnlyHotMemoryCache()
    cache.switch_project("project:alpha")
    old_context = cache.capture_request_context("project:alpha")

    cache.logout()
    cache.switch_project("project:alpha")

    with pytest.raises(ValueError, match="client_cache_request_context_stale"):
        cache.store_verified(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=7,
            content_hash=_digest("old session text"),
            content="old session text",
            request_context=old_context,
            selection=HotMemoryCacheSelection(retain=True),
        )
    assert cache.stats()["entries"] == 0


def test_same_project_session_rotation_clears_preferences_and_rejects_late_response():
    cache = ReadOnlyHotMemoryCache()
    cache.switch_project("project:alpha")
    old_context = cache.capture_request_context("project:alpha")
    assert (
        _store(
            cache,
            "memory:one",
            "old session text",
            selection=HotMemoryCacheSelection(retain=True, priority=1.0),
        )
        is not None
    )

    cache.switch_project("project:alpha")

    assert cache.stats()["entries"] == 0
    with pytest.raises(ValueError, match="client_cache_request_context_stale"):
        cache.store_verified(
            project_id="project:alpha",
            memory_id="memory:two",
            memory_version=7,
            content_hash=_digest("late response"),
            content="late response",
            request_context=old_context,
            selection=HotMemoryCacheSelection(retain=True, priority=1.0),
        )


def test_request_context_cannot_cross_cache_instances_or_use_nonfinite_time():
    first = ReadOnlyHotMemoryCache()
    second = ReadOnlyHotMemoryCache()
    first.switch_project("project:alpha")
    second.switch_project("project:alpha")
    first_context = first.capture_request_context("project:alpha")

    for invalid_context in (
        first_context,
        replace(
            second.capture_request_context("project:alpha"),
            issued_at=float("nan"),
        ),
    ):
        with pytest.raises(ValueError, match="client_cache_request_context_stale"):
            second.store_verified(
                project_id="project:alpha",
                memory_id="memory:one",
                memory_version=7,
                content_hash=_digest("server text"),
                content="server text",
                request_context=invalid_context,
                selection=HotMemoryCacheSelection(retain=True),
            )


def test_response_age_is_checked_after_waiting_for_cache_lock():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(response_max_age_seconds=5, clock=clock)
    cache.switch_project("project:alpha")
    context = cache.capture_request_context("project:alpha")
    observable_lock = _ObservableLock()
    cache._lock = observable_lock
    observable_lock.raw.acquire()

    def store_delayed_response():
        return cache.store_verified(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=7,
            content_hash=_digest("server text"),
            content="server text",
            request_context=context,
            selection=HotMemoryCacheSelection(retain=True),
        )

    clock.value = 14.0
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store_delayed_response)
        assert observable_lock.entered.wait(timeout=1)
        clock.value = 15.0
        observable_lock.raw.release()
        with pytest.raises(ValueError, match="client_cache_request_context_stale"):
            future.result(timeout=1)


def test_request_context_expires_and_cold_identity_states_are_safely_reclaimed():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        max_entries=1,
        ttl_seconds=5,
        selection_interval_seconds=1,
        response_max_age_seconds=5,
        clock=clock,
    )
    cache.switch_project("project:alpha")
    stale_context = cache.capture_request_context("project:alpha")
    _store(cache, "memory:one", "server text")

    # The validity interval is half-open: age == max age is already stale.
    clock.value += 5
    with pytest.raises(ValueError, match="client_cache_request_context_stale"):
        cache.store_verified(
            project_id="project:alpha",
            memory_id="memory:late",
            memory_version=7,
            content_hash=_digest("late text"),
            content="late text",
            request_context=stale_context,
            selection=HotMemoryCacheSelection(retain=True),
        )
    assert cache.stats()["identity_states"] == 0
    assert _store(cache, "memory:fresh", "fresh text") is not None


def test_high_watermark_for_live_entry_survives_identity_state_churn():
    """A bounded metadata pass must not make a live entry accept stale text."""

    cache = ReadOnlyHotMemoryCache(
        max_entries=1,
        selection_interval_seconds=1_000,
    )
    cache.switch_project("project:alpha")
    _store(cache, "memory:target", "new text", version=7)

    # These candidates are rejected by capacity, but each still exercises the
    # bounded identity high-watermark map.
    for index in range(8_192):
        _store(
            cache,
            f"memory:filler:{index}",
            "filler",
            selection=HotMemoryCacheSelection(retain=True, priority=0.0),
        )

    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:target",
            memory_version=7,
            content_hash=_digest("new text"),
        )
        == "new text"
    )
    assert _store(cache, "memory:target", "old text", version=6) is None


def test_changed_version_invalidates_stale_text_and_waits_for_cadence():
    clock = _Clock()
    cache = ReadOnlyHotMemoryCache(
        ttl_seconds=100,
        selection_interval_seconds=10,
        clock=clock,
    )
    cache.switch_project("project:alpha")
    _store(cache, "memory:one", "old text", version=7)

    clock.value = 15.0
    assert _store(cache, "memory:one", "new text", version=8) is None
    assert cache.stats()["entries"] == 0
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=7,
            content_hash=_digest("old text"),
        )
        is None
    )

    clock.value = 20.1
    replacement = _store(cache, "memory:one", "new text", version=8)
    assert replacement is not None
    assert replacement.key.memory_version == 8


def test_system_capacity_keeps_higher_priority_agent_choices():
    cache = ReadOnlyHotMemoryCache(max_entries=2, max_total_bytes=32, max_entry_bytes=16)
    cache.switch_project("project:alpha")
    _store(
        cache,
        "memory:high",
        "high",
        selection=HotMemoryCacheSelection(retain=True, priority=0.9),
    )
    _store(
        cache,
        "memory:medium",
        "medium",
        selection=HotMemoryCacheSelection(retain=True, priority=0.5),
    )

    rejected = _store(
        cache,
        "memory:low",
        "low",
        selection=HotMemoryCacheSelection(retain=True, priority=0.1),
    )
    assert rejected is None
    selected = _store(
        cache,
        "memory:top",
        "top",
        selection=HotMemoryCacheSelection(retain=True, priority=1.0),
    )
    assert selected is not None
    assert _store(cache, "memory:medium", "medium") is None
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:high",
            memory_version=7,
            content_hash=_digest("high"),
        )
        == "high"
    )
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:medium",
            memory_version=7,
            content_hash=_digest("medium"),
        )
        is None
    )


def test_cache_key_includes_version_and_hash():
    cache = ReadOnlyHotMemoryCache()
    cache.switch_project("project:alpha")
    _store(cache, "memory:one", "version seven", version=7)

    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=8,
            content_hash=_digest("version seven"),
        )
        is None
    )
    assert (
        cache.get(
            project_id="project:alpha",
            memory_id="memory:one",
            memory_version=7,
            content_hash=_digest("changed"),
        )
        is None
    )


def test_project_switch_and_logout_clear_all_content():
    cache = ReadOnlyHotMemoryCache()
    cache.switch_project("project:alpha")
    _store(cache, "memory:one", "server text")

    cache.switch_project("project:beta")
    assert cache.stats()["entries"] == 0
    assert cache.active_project == "project:beta"

    cache.logout()
    assert cache.stats()["entries"] == 0
    assert cache.active_project is None


def test_policy_exposes_system_selection_without_cached_content():
    cache = ReadOnlyHotMemoryCache(
        selection_interval_seconds=15,
        system_min_retention_score=0.4,
        system_accesses_per_interval=3,
    )
    policy = cache.policy()

    assert policy["persistence"] is False
    assert policy["eviction"] == "ttl+joint-retention-score+lru"
    assert policy["selection"]["cadence"] == "lazy-on-cache-operation"
    assert (
        policy["selection"]["access_frequency_scope"]
        == "successful-local-hits-since-last-selection"
    )
    assert policy["selection"]["system_signal_weight"] == pytest.approx(0.7)
    assert policy["selection"]["agent_preference_weight"] == pytest.approx(0.3)
    assert policy["selection"]["agent_preference_scope"] == "active-project-session"
    assert policy["selection"]["agent_preference_pin"] is False
    assert policy["session_model"] == "one-active-project-session-per-cache-instance"
    assert policy["policy"] == {
        "max_entries": 256,
        "max_total_bytes": 4 * 1024 * 1024,
        "max_entry_bytes": 64 * 1024,
        "ttl_seconds": 300.0,
        "selection_interval_seconds": 15.0,
        "response_max_age_seconds": 120.0,
        "system_min_retention_score": 0.4,
        "system_accesses_per_interval": 3,
        "system_signal_weight": 0.7,
        "agent_preference_weight": 0.3,
        "system_retention_floor": "system_min_retention_score",
        "positive_refresh": "same-key-updates-priority-only-shortens-ttl; changed-key-waits-for-cadence",
    }


def test_cache_has_hard_capacity_and_no_oversized_entry():
    with pytest.raises(ValueError, match="client_cache_max_entries_invalid"):
        ReadOnlyHotMemoryCache(max_entries=2_049)
    with pytest.raises(ValueError, match="client_cache_max_total_bytes_invalid"):
        ReadOnlyHotMemoryCache(max_total_bytes=64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="client_cache_response_max_age_invalid"):
        ReadOnlyHotMemoryCache(response_max_age_seconds=0)

    cache = ReadOnlyHotMemoryCache(max_total_bytes=8, max_entry_bytes=4)
    cache.switch_project("project:alpha")
    with pytest.raises(ValueError, match="client_cache_content_size_invalid"):
        _store(cache, "memory:large", "12345")
