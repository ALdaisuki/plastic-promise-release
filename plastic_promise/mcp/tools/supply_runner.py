"""Bounded execution for synchronous ContextEngine supply calls."""

import asyncio
import os
from concurrent.futures import Executor
from functools import partial
from typing import Any


def float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def call_engine_supply(engine: Any, supply_args: dict[str, Any]):
    """Call current or legacy ``ContextEngine.supply`` signatures."""
    try:
        return engine.supply(**supply_args)
    except TypeError:
        legacy_args = {
            "task_description": supply_args["task_description"],
            "task_vector": supply_args["task_vector"],
            "task_type": supply_args["task_type"],
            "scope": supply_args["scope"],
            "debug": supply_args["debug"],
        }
        try:
            return engine.supply(**legacy_args)
        except TypeError:
            return engine.supply(
                supply_args["task_description"],
                supply_args["task_vector"],
                supply_args["task_type"],
                supply_args["scope"],
            )


def call_governed_retrieval_embedding(
    engine: Any,
    *,
    text: str,
    project_id: str,
) -> list[float]:
    """Call ContextEngine's project-scoped retrieval embedding seam.

    Production ContextEngine exposes ``retrieval_embedding_probe``.  An
    engine without that seam is treated as unavailable: callers select the
    text-only degraded path rather than silently rediscovering an arbitrary
    provider through an older engine implementation.
    """

    operation = getattr(engine, "retrieval_embedding_probe", None)
    if not callable(operation):
        raise RuntimeError("retrieval_embedding_route_unavailable")
    return operation(text, project_id=project_id)


async def run_bounded_governed_retrieval_embedding(
    engine: Any,
    *,
    text: str,
    project_id: str,
    executor: Executor,
    timeout: float,
) -> list[float]:
    """Run the governed foreground embedding route outside the event loop."""

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            executor,
            partial(
                call_governed_retrieval_embedding,
                engine,
                text=text,
                project_id=project_id,
            ),
        ),
        timeout=timeout,
    )


async def run_bounded_engine_supply(
    engine: Any,
    supply_args: dict[str, Any],
    *,
    executor: Executor,
    timeout: float,
):
    """Run synchronous supply outside the event loop with a response deadline."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(executor, call_engine_supply, engine, supply_args),
        timeout=timeout,
    )
