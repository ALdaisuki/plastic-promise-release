from __future__ import annotations

import contextlib
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from plastic_promise.collaboration.passive_runtime_adapter import (
    get_server_passive_collaboration_runtime,
)


def _engine(connection: sqlite3.Connection) -> SimpleNamespace:
    @contextlib.contextmanager
    def batch():
        try:
            yield
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    storage = SimpleNamespace(_conn=connection, batch=batch)
    return SimpleNamespace(_sqlite=storage, _write_lock=threading.RLock())


def test_adapter_owns_server_storage_lookup_and_reuses_process_local_runtime() -> None:
    engine = _engine(sqlite3.connect(":memory:"))

    runtime = get_server_passive_collaboration_runtime(engine)

    assert runtime is not None
    assert get_server_passive_collaboration_runtime(engine) is runtime
    assert engine._server_passive_collaboration_runtime is runtime


def test_adapter_fails_open_when_engine_has_no_canonical_writer() -> None:
    assert get_server_passive_collaboration_runtime(SimpleNamespace()) is None


def test_adapter_rejects_invalid_preexisting_runtime_binding() -> None:
    engine = SimpleNamespace(_server_passive_collaboration_runtime=object())

    with pytest.raises(RuntimeError, match="passive_collaboration_runtime_binding_invalid"):
        get_server_passive_collaboration_runtime(engine)
