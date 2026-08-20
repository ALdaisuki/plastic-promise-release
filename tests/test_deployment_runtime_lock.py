import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import plastic_promise.deployment.runtime_lock as runtime_lock
from plastic_promise.deployment.runtime_lock import (
    LOCK_HELD_EXIT_STATUS,
    acquire_canonical_runtime_lock,
    runtime_lock_path,
)


def test_runtime_lock_path_uses_explicit_path_or_the_canonical_state_root():
    assert runtime_lock_path({"PLASTIC_RUNTIME_LOCK_PATH": "/state/runtime/mcp.lock"}) == Path(
        "/state/runtime/mcp.lock"
    )
    assert runtime_lock_path({"PLASTIC_DB_PATH": "/state/db/plastic_memory.db"}) == Path(
        "/state/runtime/mcp.lock"
    )


def test_runtime_lock_prevents_another_process_from_opening_the_same_canonical_state(tmp_path):
    lock_path = tmp_path / "runtime" / "mcp.lock"
    descriptor = acquire_canonical_runtime_lock(lock_path)
    try:
        script = """
from pathlib import Path
from plastic_promise.deployment.runtime_lock import (
    CanonicalRuntimeLockHeldError,
    acquire_canonical_runtime_lock,
)
try:
    descriptor = acquire_canonical_runtime_lock(Path(__import__('sys').argv[1]))
except CanonicalRuntimeLockHeldError:
    raise SystemExit(75)
else:
    __import__('os').close(descriptor)
    raise SystemExit(0)
"""
        environment = os.environ | {"PYTHONPATH": str(Path.cwd())}
        result = subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert result.returncode == LOCK_HELD_EXIT_STATUS
    finally:
        os.close(descriptor)


def test_runtime_lock_source_has_a_windows_backend_and_no_unconditional_posix_import():
    source = Path("plastic_promise/deployment/runtime_lock.py").read_text(encoding="utf-8")

    assert 'if os.name == "nt"' in source
    assert "import msvcrt" in source
    assert "import fcntl" in source
    assert "os.O_RDWR | os.O_CREAT" in source


def test_windows_lock_backend_is_exercised_with_a_fake_crt(monkeypatch):
    calls: list[tuple[int, int, int]] = []

    class FakeMsvcrt:
        LK_NBLCK = 42

        @staticmethod
        def locking(descriptor: int, mode: int, size: int) -> None:
            calls.append((descriptor, mode, size))

    monkeypatch.setattr(runtime_lock, "msvcrt", FakeMsvcrt, raising=False)
    monkeypatch.setattr(runtime_lock.os, "fstat", lambda _descriptor: SimpleNamespace(st_size=1))
    monkeypatch.setattr(runtime_lock.os, "lseek", lambda *_args: 0)

    runtime_lock._acquire_windows_lock(17)

    assert calls == [(17, FakeMsvcrt.LK_NBLCK, 1)]
