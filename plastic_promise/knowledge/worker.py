"""Background worker for durable knowledge semantic compilation."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from typing import TYPE_CHECKING, Any

from plastic_promise.knowledge.contracts import (
    ActiveProjectCursor,
    SemanticChunkCursor,
    knowledge_feature_gate,
    utc_now_iso,
)
from plastic_promise.knowledge.semantic import (
    SEMANTIC_GATE,
    KnowledgeSemanticCoordinator,
    KnowledgeSemanticProvider,
)

if TYPE_CHECKING:
    from plastic_promise.knowledge.repository import KnowledgeRepository

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_PROJECT_LIMIT = 100
DEFAULT_PLAN_CHUNK_LIMIT = 500
DEFAULT_BATCH_LIMIT = 5
DEFAULT_PARTIAL_FLUSH_SECONDS = 30.0


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _bounded_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


class KnowledgeSemanticWorker:
    """Plan and process project-scoped semantic jobs outside the HTTP loop."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        provider: KnowledgeSemanticProvider | None = None,
        interval_seconds: float | None = None,
        project_limit: int | None = None,
        plan_chunk_limit: int | None = None,
        batch_limit: int | None = None,
        partial_flush_seconds: float | None = None,
    ) -> None:
        self._repository = repository
        self._coordinator = KnowledgeSemanticCoordinator(
            repository,
            provider=provider,
            owner=f"knowledge-semantic-worker:{uuid.uuid4().hex}",
        )
        self._interval_seconds = interval_seconds or _bounded_float(
            "PP_KNOWLEDGE_SEMANTIC_INTERVAL_SECONDS",
            DEFAULT_INTERVAL_SECONDS,
            minimum=1.0,
            maximum=3600.0,
        )
        self._project_limit = project_limit or _bounded_int(
            "PP_KNOWLEDGE_SEMANTIC_PROJECT_LIMIT",
            DEFAULT_PROJECT_LIMIT,
            minimum=1,
            maximum=1000,
        )
        self._plan_chunk_limit = plan_chunk_limit or _bounded_int(
            "PP_KNOWLEDGE_SEMANTIC_PLAN_CHUNK_LIMIT",
            DEFAULT_PLAN_CHUNK_LIMIT,
            minimum=1,
            maximum=10_000,
        )
        self._batch_limit = batch_limit or _bounded_int(
            "PP_KNOWLEDGE_SEMANTIC_BATCH_LIMIT",
            DEFAULT_BATCH_LIMIT,
            minimum=1,
            maximum=100,
        )
        self._partial_flush_seconds = (
            partial_flush_seconds
            if partial_flush_seconds is not None
            else _bounded_float(
                "PP_KNOWLEDGE_SEMANTIC_PARTIAL_FLUSH_SECONDS",
                DEFAULT_PARTIAL_FLUSH_SECONDS,
                minimum=0.0,
                maximum=300.0,
            )
        )
        self._project_cursor: ActiveProjectCursor | None = None
        self._project_sweep_seen: set[str] = set()
        self._plan_cursors: dict[str, SemanticChunkCursor] = {}
        self._state_lock = threading.Lock()
        self._wake = asyncio.Event()
        self._state: dict[str, Any] = {
            "gate": knowledge_feature_gate(SEMANTIC_GATE),
            "running": False,
            "last_cycle_at": None,
            "last_result": None,
            "last_error": None,
        }

    @property
    def enabled(self) -> bool:
        return knowledge_feature_gate(SEMANTIC_GATE) in {"shadow", "on"}

    def notify(self) -> None:
        """Wake the coalescing loop after a new source is admitted."""
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def run_cycle(self) -> dict[str, Any]:
        """Run one bounded plan/process cycle; safe for ``asyncio.to_thread``."""
        gate = knowledge_feature_gate(SEMANTIC_GATE)
        if gate not in {"shadow", "on"}:
            return {
                "gate": gate,
                "project_count": 0,
                "planned": 0,
                "processed": 0,
                "failed": 0,
                "reason": "semantic_disabled",
            }
        self._repository.init_schema()
        project_page = self._repository.list_active_project_page(
            limit=self._project_limit,
            after_cursor=self._project_cursor,
        )
        project_ids = list(project_page.project_ids)
        self._project_sweep_seen.update(project_ids)
        if project_page.has_more and project_page.next_cursor is not None:
            self._project_cursor = project_page.next_cursor
        else:
            active_projects = set(self._project_sweep_seen)
            self._plan_cursors = {
                project_id: cursor
                for project_id, cursor in self._plan_cursors.items()
                if project_id in active_projects
            }
            self._project_cursor = None
            self._project_sweep_seen.clear()
        planned = 0
        for project_id in project_ids:
            result = self._coordinator.plan(
                project_id,
                limit_chunks=self._plan_chunk_limit,
                partial_flush_seconds=self._partial_flush_seconds,
                after_cursor=self._plan_cursors.get(project_id),
            )
            planned += int(result["created"])
            if result["has_more"] and result["next_cursor"] is not None:
                self._plan_cursors[project_id] = result["next_cursor"]
            else:
                self._plan_cursors.pop(project_id, None)
        processed = self._coordinator.process_next(limit=self._batch_limit)
        return {
            "gate": gate,
            "project_count": len(project_ids),
            "planned": planned,
            **processed,
        }

    async def serve(self, stop: asyncio.Event) -> None:
        """Run until stopped; cloud calls execute in a worker thread."""
        with self._state_lock:
            self._state["running"] = True
            self._state["gate"] = knowledge_feature_gate(SEMANTIC_GATE)
        try:
            while not stop.is_set():
                try:
                    result = await asyncio.to_thread(self.run_cycle)
                except Exception as exc:  # pragma: no cover - defensive runtime boundary
                    logger.exception("knowledge semantic cycle failed")
                    with self._state_lock:
                        self._state["last_cycle_at"] = utc_now_iso()
                        self._state["last_error"] = type(exc).__name__
                else:
                    with self._state_lock:
                        self._state["gate"] = str(result.get("gate") or "off")
                        self._state["last_cycle_at"] = utc_now_iso()
                        self._state["last_result"] = result
                        self._state["last_error"] = None
                if stop.is_set():
                    break
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self._interval_seconds,
                    )
                except TimeoutError:
                    pass
                finally:
                    self._wake.clear()
        finally:
            with self._state_lock:
                self._state["running"] = False
