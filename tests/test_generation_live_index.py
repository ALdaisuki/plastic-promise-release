"""Safety contract for a mutable live view derived from an immutable generation."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from plastic_promise.core import generation_live_index as live_index_module
from plastic_promise.core.generation_live_index import (
    GenerationLiveIndexError,
    inspect_generation_live_index,
    summarize_generation_live_index_lag,
)
from plastic_promise.core.generation_live_index import (
    bootstrap_generation_live_index as _bootstrap_generation_live_index,
)
from plastic_promise.core.generation_live_index import (
    resolve_generation_live_index as _resolve_generation_live_index,
)

_SELECTION_A = "d" * 64


def bootstrap_generation_live_index(**kwargs):
    kwargs.setdefault("base_selection_identity", _SELECTION_A)
    return _bootstrap_generation_live_index(**kwargs)


def resolve_generation_live_index(live_root, base_manifest, **kwargs):
    kwargs.setdefault("base_selection_identity", _SELECTION_A)
    return _resolve_generation_live_index(live_root, base_manifest, **kwargs)


def _manifest(
    generation_id: str = "generation-a",
    manifest_sha256: str = "a" * 64,
) -> dict[str, object]:
    immutable_digest = "b" * 64
    return {
        "generation_id": generation_id,
        "manifest_sha256": manifest_sha256,
        "index_outbox": {
            "watermark": 41,
            "immutable_digest": immutable_digest,
            "job_count": 0,
            "reconciled": True,
            "embedding_index_identity": "embedder-a|dim=3",
            "receipt": {
                "generation_id": generation_id,
                "manifest_hash": "c" * 64,
                "watermark": 41,
                "immutable_digest": immutable_digest,
                "job_count": 0,
                "marked_done_count": 0,
                "reconciled_at": "2026-07-27T00:00:00Z",
            },
        },
    }


def _base_index(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "artifact.bin").write_bytes(b"immutable-base")
    nested = path / "nested"
    nested.mkdir()
    (nested / "part.bin").write_bytes(b"part")
    return path


def test_bootstrap_copies_base_without_mutating_it_and_binds_manifest(tmp_path):
    base = _base_index(tmp_path / "generation-a" / "index")
    live_root = tmp_path / "live"
    before = {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file()
    }

    live_index = bootstrap_generation_live_index(
        base_index_path=base,
        base_manifest=_manifest(),
        live_root=live_root,
    )

    assert live_index == live_root / "index"
    assert (live_index / "artifact.bin").read_bytes() == b"immutable-base"
    assert (live_index / "nested" / "part.bin").read_bytes() == b"part"
    assert {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file()
    } == before
    assert os.stat(live_root).st_mode & 0o077 == 0
    assert inspect_generation_live_index(live_root) == {
        "schema": "plastic-promise/generation-live-index/v1",
        "base_generation_id": "generation-a",
        "base_manifest_sha256": "a" * 64,
        "base_outbox_watermark": 41,
        "base_selection_identity": _SELECTION_A,
        "embedding_index_identity": "embedder-a|dim=3",
    }
    assert resolve_generation_live_index(live_root, _manifest()) == live_index


def test_bootstrap_fsyncs_every_copied_file_before_publication(tmp_path, monkeypatch):
    base = _base_index(tmp_path / "generation-a" / "index")
    live_root = tmp_path / "live"
    fsynced_inodes: set[int] = set()
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        fsynced_inodes.add(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(live_index_module.os, "fsync", record_fsync)

    live_index = bootstrap_generation_live_index(
        base_index_path=base,
        base_manifest=_manifest(),
        live_root=live_root,
    )

    copied_file_inodes = {path.stat().st_ino for path in live_index.rglob("*") if path.is_file()}
    assert copied_file_inodes <= fsynced_inodes


def test_bootstrap_refuses_to_overwrite_existing_live_root(tmp_path):
    base = _base_index(tmp_path / "generation-a" / "index")
    live_root = tmp_path / "live"
    live_root.mkdir()
    sentinel = live_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(GenerationLiveIndexError, match="live_index_root_exists"):
        bootstrap_generation_live_index(
            base_index_path=base,
            base_manifest=_manifest(),
            live_root=live_root,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_bootstrap_rejects_base_without_reconciled_outbox_evidence(tmp_path):
    base = _base_index(tmp_path / "generation-a" / "index")
    manifest = _manifest()
    manifest["index_outbox"] = None

    with pytest.raises(GenerationLiveIndexError, match="live_index_base_outbox_unavailable"):
        bootstrap_generation_live_index(
            base_index_path=base,
            base_manifest=manifest,
            live_root=tmp_path / "live",
        )


def test_bootstrap_refuses_target_created_during_final_publication(tmp_path, monkeypatch):
    base = _base_index(tmp_path / "generation-a" / "index")
    live_root = tmp_path / "live"
    real_mkdir = os.mkdir

    def competing_mkdir(path, mode=0o777, *, dir_fd=None):
        if Path(path) == live_root and dir_fd is None:
            real_mkdir(path, mode)
            raise FileExistsError(path)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(live_index_module.os, "mkdir", competing_mkdir)

    with pytest.raises(GenerationLiveIndexError, match="live_index_root_exists"):
        bootstrap_generation_live_index(
            base_index_path=base,
            base_manifest=_manifest(),
            live_root=live_root,
        )

    assert live_root.is_dir()
    assert list(live_root.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("generation_id", "generation-b", "live_index_base_generation_mismatch"),
        ("manifest_sha256", "b" * 64, "live_index_base_manifest_mismatch"),
    ],
)
def test_resolve_fails_closed_when_current_generation_binding_changes(
    tmp_path,
    field,
    value,
    reason,
):
    base = _base_index(tmp_path / "generation-a" / "index")
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=base,
        base_manifest=_manifest(),
        live_root=live_root,
    )
    changed = _manifest()
    changed[field] = value

    with pytest.raises(GenerationLiveIndexError, match=reason):
        resolve_generation_live_index(live_root, changed)


def test_resolve_rejects_old_live_root_after_a_to_b_to_a_selection_cycle(tmp_path):
    base = _base_index(tmp_path / "generation-a" / "index")
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=base,
        base_manifest=_manifest(),
        live_root=live_root,
    )

    with pytest.raises(GenerationLiveIndexError, match="live_index_base_generation_mismatch"):
        resolve_generation_live_index(
            live_root,
            _manifest(generation_id="generation-b", manifest_sha256="b" * 64),
            base_selection_identity="e" * 64,
        )
    with pytest.raises(GenerationLiveIndexError, match="live_index_base_selection_mismatch"):
        resolve_generation_live_index(
            live_root,
            _manifest(),
            base_selection_identity="f" * 64,
        )


def test_resolve_rejects_tampered_or_symlinked_live_material(tmp_path):
    base = _base_index(tmp_path / "generation-a" / "index")
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=base,
        base_manifest=_manifest(),
        live_root=live_root,
    )

    manifest_path = live_root / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["base_manifest_sha256"] = "not-a-hash"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(GenerationLiveIndexError, match="live_index_manifest_invalid"):
        resolve_generation_live_index(live_root, _manifest())

    manifest_path.unlink()
    manifest_path.symlink_to(base / "artifact.bin")
    with pytest.raises(GenerationLiveIndexError, match="live_index_manifest_unsafe"):
        inspect_generation_live_index(live_root)


def test_operator_cli_inspect_returns_bounded_binding_json(tmp_path, capsys):
    from scripts.manage_generation_live_index import main

    base = _base_index(tmp_path / "generation-a" / "index")
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=base,
        base_manifest=_manifest(),
        live_root=live_root,
    )

    assert main(["--live-root", str(live_root), "inspect"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "binding": {
            "schema": "plastic-promise/generation-live-index/v1",
            "base_generation_id": "generation-a",
            "base_manifest_sha256": "a" * 64,
            "base_outbox_watermark": 41,
            "base_selection_identity": _SELECTION_A,
            "embedding_index_identity": "embedder-a|dim=3",
        },
        "status": "inspected",
    }


def test_operator_cli_bootstrap_uses_current_verified_generation(tmp_path, capsys):
    from scripts.manage_generation_live_index import main

    base = _base_index(tmp_path / "generation-a" / "index")
    generation_root = tmp_path / "generations"
    live_root = tmp_path / "live"
    observed_roots: list[Path] = []

    def resolve_current(root: Path):
        observed_roots.append(root)
        return _manifest(), base, _SELECTION_A

    result = main(
        [
            "--live-root",
            str(live_root),
            "bootstrap",
            "--generation-root",
            str(generation_root),
        ],
        generation_resolver=resolve_current,
    )

    assert result == 0
    assert observed_roots == [generation_root]
    assert json.loads(capsys.readouterr().out) == {
        "binding": inspect_generation_live_index(live_root),
        "status": "bootstrapped",
    }


def test_operator_cli_bootstrap_refuses_existing_live_root(tmp_path, capsys):
    from scripts.manage_generation_live_index import main

    base = _base_index(tmp_path / "generation-a" / "index")
    generation_root = tmp_path / "generations"
    live_root = tmp_path / "live"
    live_root.mkdir()
    sentinel = live_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = main(
        [
            "--live-root",
            str(live_root),
            "bootstrap",
            "--generation-root",
            str(generation_root),
        ],
        generation_resolver=lambda root: (_manifest(), base, _SELECTION_A),
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "live_index_root_exists" in captured.err
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_operator_cli_verify_binds_live_root_to_current_generation(tmp_path, capsys):
    from scripts.manage_generation_live_index import main

    base = _base_index(tmp_path / "generation-a" / "index")
    generation_root = tmp_path / "generations"
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=base,
        base_manifest=_manifest(),
        live_root=live_root,
    )

    result = main(
        [
            "--live-root",
            str(live_root),
            "verify",
            "--generation-root",
            str(generation_root),
        ],
        generation_resolver=lambda root: (_manifest(), base, _SELECTION_A),
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "binding": inspect_generation_live_index(live_root),
        "status": "verified",
    }


def test_operator_cli_verify_rejects_generation_mismatch(tmp_path, capsys):
    from scripts.manage_generation_live_index import main

    base = _base_index(tmp_path / "generation-a" / "index")
    generation_root = tmp_path / "generations"
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=base,
        base_manifest=_manifest(),
        live_root=live_root,
    )

    changed = _manifest(generation_id="generation-b")
    result = main(
        [
            "--live-root",
            str(live_root),
            "verify",
            "--generation-root",
            str(generation_root),
        ],
        generation_resolver=lambda root: (changed, base, _SELECTION_A),
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "live_index_base_generation_mismatch" in captured.err


def test_live_lag_summary_distinguishes_completed_active_and_blocked_jobs():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE store_outbox (tool_name TEXT NOT NULL, status TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO store_outbox (tool_name, status) VALUES (?, ?)",
            [
                ("memory_index", "done"),
                ("synthesis_index", "done"),
                ("audit", "pending"),
                ("memory_index", "done"),
                ("memory_index", "pending"),
                ("synthesis_index", "processing"),
            ],
        )

        manifest = _manifest()
        manifest["index_outbox"]["watermark"] = 2
        lag = summarize_generation_live_index_lag(connection, manifest)
    finally:
        connection.close()

    assert lag == {
        "schema": "plastic-promise/generation-live-index-lag/v1",
        "state": "lagged",
        "base_generation_id": "generation-a",
        "base_outbox_watermark": 2,
        "newer_job_count": 3,
        "active_job_count": 2,
        "completed_job_count": 1,
        "blocked_job_count": 0,
        "status_counts": {"done": 1, "pending": 1, "processing": 1},
        "newest_rowid": 6,
    }
