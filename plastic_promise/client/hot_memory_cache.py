"""Bounded, process-local cache for trusted server-response memory text.

This cache is deliberately unable to persist or synchronize data.  It holds
only immutable text returned by the server and never owns canonical memory,
vectors, credentials, or an offline write queue.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_IDENTIFIER_BYTES = 512
_HARD_MAX_ENTRIES = 2_048
_HARD_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_HARD_MAX_ENTRY_BYTES = 1024 * 1024
_HARD_MAX_TTL_SECONDS = 24 * 60 * 60
_HARD_MAX_RESPONSE_AGE_SECONDS = 60 * 60
_HARD_MAX_SYSTEM_ACCESSES_PER_INTERVAL = 10_000
_HARD_MAX_IDENTITY_STATES = 8_192

_DEFAULT_SYSTEM_MIN_RETENTION_SCORE = 0.25
_DEFAULT_SYSTEM_ACCESSES_PER_INTERVAL = 2
_SYSTEM_SCORE_BASE = 0.10
_SYSTEM_SCORE_FREQUENCY_WEIGHT = 0.55
_SYSTEM_SCORE_RECENCY_WEIGHT = 0.25
_SYSTEM_SCORE_SIZE_EFFICIENCY_WEIGHT = 0.10
_SYSTEM_SIGNAL_WEIGHT = 0.70
_AGENT_PREFERENCE_WEIGHT = 0.30
_CAPACITY_SCORE_TIE_EPSILON = 0.05


@dataclass(frozen=True)
class HotMemoryCacheKey:
    """Versioned identity of one immutable server memory response."""

    project_id: str
    memory_id: str
    memory_version: int
    content_hash: str


@dataclass(frozen=True)
class HotMemoryCacheEntry:
    """One display-only memory value held in process memory."""

    key: HotMemoryCacheKey
    content: str = field(repr=False)
    priority: float
    size_bytes: int
    expires_at: float
    system_score: float = 0.0
    access_count: int = 0
    last_accessed_at: float = 0.0
    selection_due_at: float = 0.0


@dataclass(frozen=True)
class _HotMemoryIdentityState:
    """Bounded high-watermark state that prevents stale response re-entry."""

    highest_version: int
    content_hash: str
    not_before: float = 0.0
    protected_until: float = 0.0


@dataclass(frozen=True)
class HotMemoryCacheRequestContext:
    """Project-session generation captured before starting a server request."""

    project_id: str
    session_epoch: int
    issued_at: float
    owner_token: object = field(repr=False)


@dataclass(frozen=True)
class HotMemoryCacheSelection:
    """An agent's non-authoritative recommendation for one hot-memory entry.

    The recommendation can only make a local entry eligible.  The cache still
    applies its own cadence and capacity rules before retaining any content.
    """

    retain: bool
    priority: float = 0.5
    requested_ttl_seconds: float | None = None


def hot_memory_cache_contract() -> dict[str, object]:
    """Return the non-authoritative client-cache contract for API capabilities.

    The contract deliberately describes a process-memory helper, not a
    synchronization protocol.  A frontend must obtain text from the server,
    and it must never use this cache as a write queue or source of truth.
    """

    return {
        "allowed": True,
        "mode": "bounded-read-only-hot",
        "authoritative": False,
        "implementation": "process-memory-only",
        "persistence": False,
        "full_database": False,
        "lancedb_replica": False,
        "offline_write_queue": False,
        "key_scope": ["project_id", "memory_id", "memory_version", "content_hash"],
        "eviction": "ttl+joint-retention-score+lru",
        "selection": {
            "agent_controls": ["retain", "priority", "requested_ttl_seconds"],
            "system_controls": [
                "selection_interval_seconds",
                "ttl_seconds",
                "max_entries",
                "max_total_bytes",
                "max_entry_bytes",
                "system_min_retention_score",
                "system_accesses_per_interval",
                "system_score_signals",
                "weighted_system_score_and_agent_priority",
            ],
            "cadence": "lazy-on-cache-operation",
            "system_score_signals": ["access_frequency", "recency", "entry_size"],
            "access_frequency_scope": "successful-local-hits-since-last-selection",
            "system_signal_weight": _SYSTEM_SIGNAL_WEIGHT,
            "agent_preference_weight": _AGENT_PREFERENCE_WEIGHT,
            "system_retention_floor": "system_min_retention_score",
            "agent_preference_scope": "active-project-session",
            "agent_preference_pin": False,
            "effective_ttl": "min(agent_request, cache_ttl)",
            "positive_refresh": "same-key-updates-priority-only-shortens-ttl; changed-key-invalidates-and-waits",
            "retain_false": "invalidate-project-memory-identity",
        },
        "response_adapter": {
            "trusted_server_fields": [
                "project_id",
                "memory_id",
                "memory_version",
                "content_hash",
                "content",
            ],
            "agent_selection": "separate-client-input",
            "server_cache_hint_authority": False,
            "request_context": "capture-before-transport",
        },
        "clear_on": ["session_start", "logout", "project_switch", "explicit_clear"],
        "session_model": "one-active-project-session-per-cache-instance",
        "identity_state_overflow": "fail-closed-until-safe-window-prune",
    }


class ReadOnlyHotMemoryCache:
    """TTL/LRU cache that cannot become a second memory database.

    ``HotMemoryCacheSelection`` lets an agent nominate an entry, but it is not
    final authority: a lazy system pass independently scores access frequency,
    recency and size at the configured cadence.  The system score must clear
    its full minimum threshold, then controls 70% of the joint retention
    decision; Agent priority is a 30% soft signal scoped to the active project
    session.  There is intentionally no serialization, vector, mutation,
    database import, or offline synchronization API.

    ``store_verified`` is a trusted-client adapter API.  Its SHA-256 comparison
    protects response-content consistency, but a bare digest does not prove
    that an arbitrary caller obtained the text from the server.
    """

    def __init__(
        self,
        *,
        max_entries: int = 256,
        max_total_bytes: int = 4 * 1024 * 1024,
        max_entry_bytes: int = 64 * 1024,
        ttl_seconds: float = 300.0,
        selection_interval_seconds: float = 60.0,
        response_max_age_seconds: float = 120.0,
        system_min_retention_score: float = _DEFAULT_SYSTEM_MIN_RETENTION_SCORE,
        system_accesses_per_interval: int = _DEFAULT_SYSTEM_ACCESSES_PER_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = _bounded_int(
            max_entries,
            minimum=1,
            maximum=_HARD_MAX_ENTRIES,
            reason="client_cache_max_entries_invalid",
        )
        self._max_total_bytes = _bounded_int(
            max_total_bytes,
            minimum=1,
            maximum=_HARD_MAX_TOTAL_BYTES,
            reason="client_cache_max_total_bytes_invalid",
        )
        self._max_entry_bytes = _bounded_int(
            max_entry_bytes,
            minimum=1,
            maximum=min(_HARD_MAX_ENTRY_BYTES, self._max_total_bytes),
            reason="client_cache_max_entry_bytes_invalid",
        )
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise ValueError("client_cache_ttl_invalid")
        self._ttl_seconds = float(ttl_seconds)
        if not 0 < self._ttl_seconds <= _HARD_MAX_TTL_SECONDS:
            raise ValueError("client_cache_ttl_invalid")
        if isinstance(selection_interval_seconds, bool) or not isinstance(
            selection_interval_seconds, (int, float)
        ):
            raise ValueError("client_cache_selection_interval_invalid")
        self._selection_interval_seconds = float(selection_interval_seconds)
        if not 0 < self._selection_interval_seconds <= _HARD_MAX_TTL_SECONDS:
            raise ValueError("client_cache_selection_interval_invalid")
        if isinstance(response_max_age_seconds, bool) or not isinstance(
            response_max_age_seconds, (int, float)
        ):
            raise ValueError("client_cache_response_max_age_invalid")
        self._response_max_age_seconds = float(response_max_age_seconds)
        if not 0 < self._response_max_age_seconds <= _HARD_MAX_RESPONSE_AGE_SECONDS:
            raise ValueError("client_cache_response_max_age_invalid")
        if isinstance(system_min_retention_score, bool) or not isinstance(
            system_min_retention_score, (int, float)
        ):
            raise ValueError("client_cache_system_min_retention_score_invalid")
        self._system_min_retention_score = float(system_min_retention_score)
        if not 0.0 <= self._system_min_retention_score <= 1.0:
            raise ValueError("client_cache_system_min_retention_score_invalid")
        self._system_accesses_per_interval = _bounded_int(
            system_accesses_per_interval,
            minimum=1,
            maximum=_HARD_MAX_SYSTEM_ACCESSES_PER_INTERVAL,
            reason="client_cache_system_accesses_per_interval_invalid",
        )
        if not callable(clock):
            raise TypeError("client_cache_clock_invalid")
        self._clock = clock
        self._active_project: str | None = None
        self._session_epoch = 0
        self._request_context_owner = object()
        self._entries: OrderedDict[HotMemoryCacheKey, HotMemoryCacheEntry] = OrderedDict()
        self._identity_states: OrderedDict[tuple[str, str], _HotMemoryIdentityState] = OrderedDict()
        self._total_bytes = 0
        self._identity_overflow_rejections = 0
        self._lock = threading.Lock()

    @property
    def active_project(self) -> str | None:
        with self._lock:
            return self._active_project

    def switch_project(self, project_id: str) -> None:
        """Start a fresh cache session for one project.

        Calling this method always rotates the session, even when ``project_id``
        is unchanged.  A cache instance therefore cannot silently carry Agent
        preferences or late responses between two sessions of the same project.
        """

        project_id = _identifier(project_id, "client_cache_project_id_invalid")
        with self._lock:
            self._clear_locked()
            self._active_project = project_id
            self._session_epoch += 1

    def logout(self) -> None:
        """Remove every cached value and forget the active project."""

        with self._lock:
            self._clear_locked()
            self._active_project = None
            self._session_epoch += 1

    def clear(self) -> None:
        """Remove every cached value while retaining the active project."""

        with self._lock:
            self._clear_locked()
            self._session_epoch += 1

    def capture_request_context(self, project_id: str) -> HotMemoryCacheRequestContext:
        """Bind a future response to the current login/project generation."""

        project_id = _identifier(project_id, "client_cache_project_id_invalid")
        issued_at = self._now()
        with self._lock:
            self._require_active_project(project_id)
            return HotMemoryCacheRequestContext(
                project_id=project_id,
                session_epoch=self._session_epoch,
                issued_at=issued_at,
                owner_token=self._request_context_owner,
            )

    def policy(self) -> dict[str, object]:
        """Return the effective local policy without exposing cached text."""

        contract = hot_memory_cache_contract()
        contract["policy"] = {
            "max_entries": self._max_entries,
            "max_total_bytes": self._max_total_bytes,
            "max_entry_bytes": self._max_entry_bytes,
            "ttl_seconds": self._ttl_seconds,
            "selection_interval_seconds": self._selection_interval_seconds,
            "response_max_age_seconds": self._response_max_age_seconds,
            "system_min_retention_score": self._system_min_retention_score,
            "system_accesses_per_interval": self._system_accesses_per_interval,
            "system_signal_weight": _SYSTEM_SIGNAL_WEIGHT,
            "agent_preference_weight": _AGENT_PREFERENCE_WEIGHT,
            "system_retention_floor": "system_min_retention_score",
            "positive_refresh": "same-key-updates-priority-only-shortens-ttl; changed-key-waits-for-cadence",
        }
        return contract

    def store_verified(
        self,
        *,
        project_id: str,
        memory_id: str,
        memory_version: int,
        content_hash: str,
        content: str | None,
        request_context: HotMemoryCacheRequestContext,
        selection: HotMemoryCacheSelection,
    ) -> HotMemoryCacheEntry | None:
        """Consider one server-returned text value for the local hot cache.

        ``retain=False`` removes every cached version of this memory identity;
        it intentionally needs no content body for an Agent-local eviction.
        For ``retain=True``, agent selection only makes
        a value eligible.  The system must also admit it and periodically
        retain it based on access frequency, recency and size.
        """

        key = _cache_key(project_id, memory_id, memory_version, content_hash)
        selection = _selection(selection)
        if not selection.retain:
            with self._lock:
                now = self._now()
                self._require_request_context_locked(request_context, key.project_id, now)
                self._expire_locked(now)
                self._run_system_selection_locked(now)
                self._remove_identity_locked(key)
                self._observe_identity_locked(key, now=now)
            return None
        if not isinstance(content, str):
            raise TypeError("client_cache_content_invalid")
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > self._max_entry_bytes:
            raise ValueError("client_cache_content_size_invalid")
        observed_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if not _constant_time_equal(observed_hash, key.content_hash):
            raise ValueError("client_cache_content_hash_mismatch")
        effective_ttl = min(
            self._ttl_seconds,
            selection.requested_ttl_seconds or self._ttl_seconds,
        )
        with self._lock:
            now = self._now()
            self._require_request_context_locked(request_context, key.project_id, now)
            self._expire_locked(now)
            self._run_system_selection_locked(now)
            entry = HotMemoryCacheEntry(
                key=key,
                content=content,
                priority=selection.priority,
                size_bytes=len(encoded),
                expires_at=now + effective_ttl,
                system_score=0.0,
                access_count=0,
                last_accessed_at=now,
                selection_due_at=now + self._selection_interval_seconds,
            )
            entry = replace(entry, system_score=self._system_score(entry, now))
            identity = _memory_identity(key)
            state_before = self._identity_states.get(identity)
            existing = self._entries.get(key)
            if existing is not None:
                if not self._observe_identity_locked(key, now=now):
                    return None
                # An Agent may revise priority or shorten TTL for the same
                # immutable response, but cannot reset system hotness or
                # extend a deadline/cadence that the system already chose.
                updated = replace(
                    existing,
                    priority=selection.priority,
                    expires_at=min(existing.expires_at, now + effective_ttl),
                )
                self._entries[key] = updated
                return updated
            if not self._observe_identity_locked(key, now=now):
                return None
            state = self._identity_states[identity]
            # A newer server response supersedes all cached variants of the
            # same memory.  Older or conflicting same-version responses were
            # rejected by the identity high-watermark above.
            if state_before is None or key.memory_version > state_before.highest_version:
                self._remove_identity_locked(key)
            if state.not_before > now:
                return None
            if not self._joint_retention_admits(entry, system_score=entry.system_score):
                return None
            if not self._make_room_locked(entry, now):
                return None
            self._entries[key] = entry
            self._total_bytes += entry.size_bytes
            self._set_identity_not_before_locked(identity, entry.selection_due_at)
        return entry

    def store_server_response(
        self,
        response: Mapping[str, object],
        *,
        request_context: HotMemoryCacheRequestContext,
        selection: HotMemoryCacheSelection,
    ) -> HotMemoryCacheEntry | None:
        """Apply a trusted server response and a separate Agent recommendation.

        Transport authentication belongs to the caller.  This adapter only
        separates immutable server-owned memory material from the local
        Agent's non-authoritative cache choice, then delegates all version,
        hash, cadence and capacity checks to :meth:`store_verified`.
        """

        if not isinstance(response, Mapping):
            raise TypeError("client_cache_server_response_mapping_required")
        required = {
            "project_id",
            "memory_id",
            "memory_version",
            "content_hash",
        }
        if not required.issubset(response):
            raise ValueError("client_cache_server_response_incomplete")
        return self.store_verified(
            project_id=response["project_id"],  # type: ignore[arg-type]
            memory_id=response["memory_id"],  # type: ignore[arg-type]
            memory_version=response["memory_version"],  # type: ignore[arg-type]
            content_hash=response["content_hash"],  # type: ignore[arg-type]
            content=response.get("content"),  # type: ignore[arg-type]
            request_context=request_context,
            selection=selection,
        )

    def get(
        self,
        *,
        project_id: str,
        memory_id: str,
        memory_version: int,
        content_hash: str,
    ) -> str | None:
        """Return matching text, or ``None`` for stale/evicted/project-mismatched data."""

        key = _cache_key(project_id, memory_id, memory_version, content_hash)
        with self._lock:
            now = self._now()
            if key.project_id != self._active_project:
                return None
            state = self._identity_states.get(_memory_identity(key))
            if (
                state is None
                or state.highest_version != key.memory_version
                or state.content_hash != key.content_hash
            ):
                return None
            self._identity_states.move_to_end(_memory_identity(key))
            self._expire_locked(now)
            self._run_system_selection_locked(now, preserve_key=key)
            entry = self._entries.pop(key, None)
            if entry is None:
                return None
            touched = replace(
                entry,
                access_count=min(
                    entry.access_count + 1,
                    _HARD_MAX_SYSTEM_ACCESSES_PER_INTERVAL,
                ),
                last_accessed_at=now,
            )
            touched = replace(touched, system_score=self._system_score(touched, now))
            self._entries[key] = touched
            # Treat the current read as a genuine hotness signal if a lazy
            # cadence pass is due at exactly this operation.
            self._run_system_selection_locked(now)
            retained = self._entries.get(key)
            return retained.content if retained is not None else None

    def stats(self) -> dict[str, int | float | str | None]:
        """Expose capacity metadata without exposing cached memory text."""

        with self._lock:
            now = self._now()
            self._expire_locked(now)
            self._run_system_selection_locked(now)
            return {
                "active_project": self._active_project,
                "entries": len(self._entries),
                "total_bytes": self._total_bytes,
                "max_entries": self._max_entries,
                "max_total_bytes": self._max_total_bytes,
                "max_entry_bytes": self._max_entry_bytes,
                "ttl_seconds": self._ttl_seconds,
                "selection_interval_seconds": self._selection_interval_seconds,
                "response_max_age_seconds": self._response_max_age_seconds,
                "system_min_retention_score": self._system_min_retention_score,
                "system_accesses_per_interval": self._system_accesses_per_interval,
                "selection_cadence": "lazy-on-cache-operation",
                "identity_states": len(self._identity_states),
                "max_identity_states": _HARD_MAX_IDENTITY_STATES,
                "identity_overflow_rejections": self._identity_overflow_rejections,
            }

    def _require_active_project(self, project_id: str) -> None:
        if self._active_project is None:
            raise RuntimeError("client_cache_project_not_selected")
        if project_id != self._active_project:
            raise ValueError("client_cache_project_mismatch")

    def _require_request_context_locked(
        self,
        context: object,
        project_id: str,
        now: float,
    ) -> None:
        if not isinstance(context, HotMemoryCacheRequestContext):
            raise TypeError("client_cache_request_context_required")
        self._require_active_project(project_id)
        if (
            context.owner_token is not self._request_context_owner
            or context.project_id != project_id
            or context.session_epoch != self._session_epoch
            or not math.isfinite(context.issued_at)
        ):
            raise ValueError("client_cache_request_context_stale")
        age = now - context.issued_at
        if not 0 <= age < self._response_max_age_seconds:
            raise ValueError("client_cache_request_context_stale")

    def _expire_locked(self, now: float) -> None:
        expired = [key for key, value in self._entries.items() if value.expires_at <= now]
        for key in expired:
            entry = self._entries.pop(key)
            self._total_bytes -= entry.size_bytes
        self._prune_identity_states_locked(now)

    def _prune_identity_states_locked(self, now: float) -> None:
        active = {_memory_identity(key) for key in self._entries}
        for identity, state in tuple(self._identity_states.items()):
            if identity not in active and state.protected_until <= now:
                del self._identity_states[identity]

    def _run_system_selection_locked(
        self,
        now: float,
        *,
        preserve_key: HotMemoryCacheKey | None = None,
    ) -> None:
        """Lazily apply system retention at the configured cadence.

        No timer or durable worker is created for a client cache.  The next
        cache operation performs a bounded selection pass, which keeps the
        helper process-local while still preventing an idle agent choice from
        controlling retention indefinitely.
        """

        for key, entry in tuple(self._entries.items()):
            if key == preserve_key:
                continue
            if entry.selection_due_at > now:
                continue
            system_score = self._system_score(entry, now)
            if not self._joint_retention_admits(entry, system_score=system_score):
                self._remove_locked(key)
                self._set_identity_not_before_locked(
                    _memory_identity(key), now + self._selection_interval_seconds
                )
                continue
            selection_due_at = now + self._selection_interval_seconds
            self._entries[key] = replace(
                entry,
                system_score=system_score,
                access_count=0,
                selection_due_at=selection_due_at,
            )
            self._set_identity_not_before_locked(_memory_identity(key), selection_due_at)

    def _remove_locked(self, key: HotMemoryCacheKey) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._total_bytes -= entry.size_bytes

    def _remove_identity_locked(self, key: HotMemoryCacheKey) -> None:
        """Drop every cached version/hash for one project-scoped memory."""

        for candidate in tuple(self._entries):
            if candidate.project_id == key.project_id and candidate.memory_id == key.memory_id:
                self._remove_locked(candidate)

    def _observe_identity_locked(
        self,
        key: HotMemoryCacheKey,
        *,
        now: float,
    ) -> bool:
        """Apply an ordered response/tombstone and reject stale material."""

        identity = _memory_identity(key)
        state = self._identity_states.get(identity)
        self._prune_identity_states_locked(now)
        if state is None and len(self._identity_states) >= _HARD_MAX_IDENTITY_STATES:
            # Never forget an observed version or revocation just to admit a
            # new identity.  This cache is optional, so refusing new entries
            # until logout/project switch is safer than allowing stale text to
            # re-enter after metadata pressure.
            self._identity_overflow_rejections += 1
            return False
        if state is not None:
            if key.memory_version < state.highest_version:
                return False
            if key.memory_version == state.highest_version:
                if state.content_hash != key.content_hash:
                    return False
            else:
                state = _HotMemoryIdentityState(
                    highest_version=key.memory_version,
                    content_hash=key.content_hash,
                    not_before=state.not_before,
                    protected_until=now + self._response_max_age_seconds,
                )
        else:
            state = _HotMemoryIdentityState(
                highest_version=key.memory_version,
                content_hash=key.content_hash,
                protected_until=now + self._response_max_age_seconds,
            )
        state = replace(
            state,
            protected_until=max(
                state.protected_until,
                now + self._response_max_age_seconds,
            ),
        )
        self._identity_states[identity] = state
        self._identity_states.move_to_end(identity)
        return True

    def _set_identity_not_before_locked(self, identity: tuple[str, str], not_before: float) -> None:
        state = self._identity_states.get(identity)
        if state is None:
            return
        if not_before > state.not_before:
            self._identity_states[identity] = replace(
                state,
                not_before=not_before,
                protected_until=max(state.protected_until, not_before),
            )
        self._identity_states.move_to_end(identity)

    def _joint_retention_admits(
        self,
        entry: HotMemoryCacheEntry,
        *,
        system_score: float,
    ) -> bool:
        """Combine bounded Agent preference with mandatory system evidence."""

        if system_score < self._system_min_retention_score:
            return False
        return self._joint_retention_score(entry, system_score=system_score) >= (
            self._system_min_retention_score
        )

    def _system_score(self, entry: HotMemoryCacheEntry, now: float) -> float:
        """Compute an independent hotness score from local, non-secret signals."""

        age = max(0.0, now - entry.last_accessed_at)
        recency = max(0.0, 1.0 - age / self._selection_interval_seconds)
        frequency = min(1.0, entry.access_count / self._system_accesses_per_interval)
        size_efficiency = 1.0 - min(1.0, entry.size_bytes / self._max_entry_bytes)
        return (
            _SYSTEM_SCORE_BASE
            + _SYSTEM_SCORE_FREQUENCY_WEIGHT * frequency
            + _SYSTEM_SCORE_RECENCY_WEIGHT * recency
            + _SYSTEM_SCORE_SIZE_EFFICIENCY_WEIGHT * size_efficiency
        )

    def _joint_retention_score(
        self,
        entry: HotMemoryCacheEntry,
        *,
        system_score: float,
    ) -> float:
        return _SYSTEM_SIGNAL_WEIGHT * system_score + _AGENT_PREFERENCE_WEIGHT * entry.priority

    def _capacity_score(self, entry: HotMemoryCacheEntry, now: float) -> float:
        return self._joint_retention_score(
            entry,
            system_score=self._system_score(entry, now),
        )

    def _make_room_locked(self, entry: HotMemoryCacheEntry, now: float) -> bool:
        """Evict lower combined-score entries, then break ties by LRU."""

        positions = {key: position for position, key in enumerate(self._entries)}
        candidates = sorted(
            self._entries.items(),
            key=lambda item: (self._capacity_score(item[1], now), positions[item[0]]),
        )
        entry_score = self._capacity_score(entry, now)
        evicted: list[HotMemoryCacheKey] = []
        simulated_entries = len(self._entries)
        simulated_bytes = self._total_bytes
        for candidate_key, candidate in candidates:
            if (
                simulated_entries < self._max_entries
                and simulated_bytes + entry.size_bytes <= self._max_total_bytes
            ):
                break
            if self._capacity_score(candidate, now) > entry_score + _CAPACITY_SCORE_TIE_EPSILON:
                return False
            simulated_entries -= 1
            simulated_bytes -= candidate.size_bytes
            evicted.append(candidate_key)
        if (
            simulated_entries >= self._max_entries
            or simulated_bytes + entry.size_bytes > self._max_total_bytes
        ):
            return False
        for candidate_key in evicted:
            self._remove_locked(candidate_key)
            self._set_identity_not_before_locked(
                _memory_identity(candidate_key), now + self._selection_interval_seconds
            )
        return True

    def _clear_locked(self) -> None:
        self._entries.clear()
        self._identity_states.clear()
        self._total_bytes = 0
        self._identity_overflow_rejections = 0

    def _now(self) -> float:
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError):
            raise RuntimeError("client_cache_clock_invalid") from None
        if not value >= 0 or value == float("inf"):
            raise RuntimeError("client_cache_clock_invalid")
        return value


def _cache_key(
    project_id: object,
    memory_id: object,
    memory_version: object,
    content_hash: object,
) -> HotMemoryCacheKey:
    project = _identifier(project_id, "client_cache_project_id_invalid")
    memory = _identifier(memory_id, "client_cache_memory_id_invalid")
    if (
        isinstance(memory_version, bool)
        or not isinstance(memory_version, int)
        or memory_version < 0
    ):
        raise ValueError("client_cache_memory_version_invalid")
    if not isinstance(content_hash, str) or not _SHA256_RE.fullmatch(content_hash):
        raise ValueError("client_cache_content_hash_invalid")
    return HotMemoryCacheKey(project, memory, memory_version, content_hash)


def _memory_identity(key: HotMemoryCacheKey) -> tuple[str, str]:
    return key.project_id, key.memory_id


def _selection(value: object) -> HotMemoryCacheSelection:
    if not isinstance(value, HotMemoryCacheSelection):
        raise TypeError("client_cache_selection_required")
    if not isinstance(value.retain, bool):
        raise ValueError("client_cache_selection_invalid")
    if isinstance(value.priority, bool) or not isinstance(value.priority, (int, float)):
        raise ValueError("client_cache_selection_invalid")
    priority = float(value.priority)
    if not 0.0 <= priority <= 1.0:
        raise ValueError("client_cache_selection_invalid")
    requested_ttl = value.requested_ttl_seconds
    if requested_ttl is not None:
        if isinstance(requested_ttl, bool) or not isinstance(requested_ttl, (int, float)):
            raise ValueError("client_cache_selection_invalid")
        requested_ttl = float(requested_ttl)
        if not 0 < requested_ttl <= _HARD_MAX_TTL_SECONDS:
            raise ValueError("client_cache_selection_invalid")
    return HotMemoryCacheSelection(
        retain=value.retain,
        priority=priority,
        requested_ttl_seconds=requested_ttl,
    )


def _identifier(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError(reason)
    if len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise ValueError(reason)
    return value


def _bounded_int(value: object, *, minimum: int, maximum: int, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(reason)
    return value


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
