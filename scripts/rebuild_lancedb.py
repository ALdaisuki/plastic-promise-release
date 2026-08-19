"""Rebuild LanceDB from SQLite, optionally as an inactive generation.

Without generation arguments this command preserves the legacy in-place
maintenance behavior.  Generation mode takes a consistent SQLite backup into
a private temporary directory and builds only below the inactive ``index/``
directory supplied by :class:`GenerationManager`; it never promotes a build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plastic_promise.core.lancedb_generation import (  # noqa: E402
    RECALL_QUALITY_REPORT_SCHEMA,
    ArtifactVerifier,
    BuildResult,
    GenerationError,
    GenerationManager,
    GenerationSpec,
    index_material_sha256,
    quality_report_generation_identity,
)
from plastic_promise.core.recall_experiment import (  # noqa: E402
    MAX_V1_CONTROL_RETRIEVAL_CONFIGURATION,
)
from plastic_promise.core.recall_quality_environment import (  # noqa: E402
    canonical_embedding_provider,
    loaded_rust_extension_identity,
    recall_quality_code_fingerprint,
    recall_quality_comparison_environment_fingerprint,
    recall_quality_environment_fingerprint,
    recall_quality_source_commit,
    recall_quality_source_fingerprint,
    recall_quality_source_label,
    recall_quality_source_paths,
    retrieval_dependency_versions,
)

INDEX_SCHEMA = "memory-vectors/v1"
_MAX_QUALITY_REPORT_BYTES = 4 * 1024 * 1024
_INDEX_OUTBOX_TOOLS = ("memory_index", "synthesis_index")
_SHADOW_REPAIR_PASSES = 3
_OUTBOX_IMMUTABLE_COLUMNS = (
    "outbox_id",
    "tool_name",
    "project_id",
    "call_id",
    "payload_json",
    "metadata_json",
    "created_at",
)
_UNSET = object()


class ShadowBuildError(RuntimeError):
    """The offline generation could not be built without ambiguity."""


@dataclass(frozen=True)
class SidecarFingerprint:
    """Stable identity and content digest for a SQLite WAL sidecar."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class SourceFingerprint:
    """Observable source state used to prove this command did not mutate it."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str
    memory_row_count: int
    wal: SidecarFingerprint | None


@dataclass(frozen=True)
class SourceSnapshot:
    """Immutable identity of the SQLite backup before clone-only migrations."""

    path: Path
    sha256: str
    memory_row_count: int
    eligible_memory_ids: frozenset[str]
    index_text_policy: str
    index_material_sha256: str
    index_outbox: dict[str, Any]
    # Legacy fixture databases did not carry project metadata.  Keep their
    # snapshot constructor readable while all new generation manifests remain
    # explicitly project-bound.
    project_id: str = "project:legacy-global"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild LanceDB in place or build an inactive shadow generation"
    )
    parser.add_argument(
        "--generation-root",
        type=Path,
        help="Private root containing generations/ and the current pointer",
    )
    parser.add_argument("--generation-id", help="Immutable identifier for the inactive build")
    parser.add_argument(
        "--project-id",
        default=os.environ.get("PLASTIC_PROJECT_ID"),
        help="Canonical project scope bound into the generation manifest",
    )
    parser.add_argument(
        "--source-db",
        type=Path,
        help="Canonical SQLite database opened read-only and copied with the Backup API",
    )
    parser.add_argument(
        "--quality-report",
        type=Path,
        help="Publishable live report produced by scripts/benchmark_recall_quality.py",
    )
    parser.add_argument(
        "--candidate-manifest",
        type=Path,
        help="Frozen candidate manifest used to authenticate held-out v2 evidence",
    )
    return parser


def _legacy_rebuild() -> int:
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=True)
    engine._ensure_heavy_init()
    ldb = engine._ldb
    if ldb is None:
        print("LanceDB is not available; nothing rebuilt.")
        return 1

    rebuilt = ldb.rebuild_all(engine)
    print(f"Re-indexed canonical eligible memories: {rebuilt}")
    print(f"LanceDB rows: {ldb.count_rows()}")
    return 0


def _generation_arguments(
    arguments: argparse.Namespace,
) -> tuple[Path, str, str, Path, Path, Path | None] | None:
    values = (
        arguments.generation_root,
        arguments.generation_id,
        arguments.source_db,
        arguments.quality_report,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ShadowBuildError("generation_mode_requires_root_id_source_db_and_quality_report")
    if not isinstance(arguments.project_id, str) or not arguments.project_id.strip():
        raise ShadowBuildError("generation_mode_requires_project_id")
    return (
        values[0],
        values[1],
        arguments.project_id.strip(),
        values[2],
        values[3],
        arguments.candidate_manifest,
    )


def _load_json_object(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    entry = resolved.stat()
    if not stat.S_ISREG(entry.st_mode):
        raise ShadowBuildError("quality_report_not_regular_file")
    if entry.st_size > _MAX_QUALITY_REPORT_BYTES:
        raise ShadowBuildError("quality_report_too_large")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ShadowBuildError("quality_report_unreadable") from exc
    if not isinstance(payload, dict):
        raise ShadowBuildError("quality_report_not_object")
    return payload


def _normalize_quality_report(
    raw_report: Mapping[str, Any],
    *,
    candidate_manifest: object | None = None,
) -> dict[str, Any]:
    """Use the generation module's adapter for the real benchmark report."""

    try:
        from plastic_promise.core.lancedb_generation import adapt_recall_quality_report
    except ImportError as exc:  # pragma: no cover - deployment/package skew.
        raise ShadowBuildError("quality_report_adapter_unavailable") from exc
    try:
        normalized = adapt_recall_quality_report(
            raw_report,
            candidate_manifest=candidate_manifest,
        )
    except (GenerationError, TypeError, ValueError) as exc:
        raise ShadowBuildError(str(exc)) from exc
    if not isinstance(normalized, Mapping):
        raise ShadowBuildError("quality_report_adapter_result_invalid")
    return json.loads(json.dumps(dict(normalized), allow_nan=False))


def _quality_identity(report: Mapping[str, Any]) -> dict[str, Any]:
    try:
        identity = quality_report_generation_identity(report)
    except (GenerationError, TypeError, ValueError) as exc:
        raise ShadowBuildError(str(exc)) from exc
    return {
        "embedding_model": identity.embedding_model,
        "model_revision": identity.model_revision,
        "embedding_dimension": identity.embedding_dimension,
        "index_text_policy": identity.index_text_policy,
        "benchmark_corpus_sha256": identity.benchmark_corpus_sha256,
        "benchmark_corpus_count": identity.benchmark_corpus_count,
        "benchmark_cases_sha256": identity.benchmark_cases_sha256,
        "benchmark_case_count": identity.benchmark_case_count,
    }


def _assert_quality_report_runtime_environment(report: Mapping[str, Any]) -> None:
    """Bind v2 quality evidence to this exact checkout and native runtime."""

    benchmark = report.get("benchmark")
    if not isinstance(benchmark, Mapping) or "candidate_dimension" not in benchmark:
        return
    environment = report.get("environment")
    if not isinstance(environment, Mapping):
        raise ShadowBuildError("quality_report_runtime_environment_missing")
    dataset_source = environment.get("dataset_source")
    if not isinstance(dataset_source, str) or not dataset_source:
        raise ShadowBuildError("quality_report_dataset_source_invalid")
    dataset_label = Path(dataset_source)
    if dataset_label.is_absolute() or ".." in dataset_label.parts or "\\" in dataset_source:
        raise ShadowBuildError("quality_report_dataset_source_invalid")
    root = ROOT.expanduser().resolve(strict=True)
    try:
        fingerprint_environment = _quality_report_fingerprint_environment(report)
        dataset_path = (root / dataset_label).resolve(strict=True)
        dataset_path.relative_to(root)
        source_paths = recall_quality_source_paths(root, dataset_path)
        source_files = [recall_quality_source_label(root, path) for path in source_paths]
        source_fingerprint = recall_quality_source_fingerprint(root, dataset_path)
        source_commit = recall_quality_source_commit(root)
        dirty_fingerprint = recall_quality_code_fingerprint(root)
        environment_fingerprint = recall_quality_environment_fingerprint(fingerprint_environment)
        comparison_environment_fingerprint = recall_quality_comparison_environment_fingerprint(
            fingerprint_environment
        )
        dependencies = retrieval_dependency_versions()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ShadowBuildError("quality_report_runtime_environment_unavailable") from exc
    if environment.get("source_files") != source_files:
        raise ShadowBuildError("quality_report_source_files_not_current")
    if environment.get("source_fingerprint") != source_fingerprint:
        raise ShadowBuildError("quality_report_source_fingerprint_not_current")
    if (
        environment.get("source_commit") != source_commit
        or environment.get("code_revision") != source_commit
    ):
        raise ShadowBuildError("quality_report_source_revision_not_current")
    if environment.get("dirty_fingerprint") != dirty_fingerprint:
        raise ShadowBuildError("quality_report_dirty_fingerprint_not_current")
    if environment.get("dependencies") != dependencies:
        raise ShadowBuildError("quality_report_dependencies_not_current")
    _assert_quality_report_runtime_configuration(report)
    if (
        environment.get("environment_fingerprint") != environment_fingerprint
        or environment.get("comparison_environment_fingerprint")
        != comparison_environment_fingerprint
    ):
        raise ShadowBuildError("quality_report_environment_fingerprint_not_current")


def _quality_report_fingerprint_environment(
    report: Mapping[str, Any],
) -> Mapping[str, str] | None:
    """Recreate the canonical process environment used by live evidence.

    Live quality runs intentionally override stateful runtime features while
    they exercise an isolated corpus. Promotion must normalize those same
    fields before comparing fingerprints, while retaining every ambient
    provider and transport setting that the benchmark did not override.
    """

    backend = report.get("backend")
    if not isinstance(backend, Mapping) or backend.get("mode") not in {
        "live",
        "engine-diagnostic",
    }:
        return None
    index_text_policy = backend.get("index_text_policy")
    fusion_policy = backend.get("effective_policy")
    if not isinstance(index_text_policy, str) or not isinstance(fusion_policy, str):
        raise ShadowBuildError("quality_report_runtime_configuration_missing")
    try:
        from scripts.benchmark_recall_quality import _benchmark_evidence_environment

        return _benchmark_evidence_environment(
            index_text_policy,
            fusion_policy,
            environ=os.environ,
        )
    except (ImportError, RuntimeError, TypeError, ValueError) as exc:
        raise ShadowBuildError("quality_report_runtime_environment_unavailable") from exc


def _assert_quality_report_runtime_configuration(report: Mapping[str, Any]) -> None:
    """Require promotion tooling to run with the target MCP configuration."""

    backend = report.get("backend")
    environment = report.get("environment")
    if not isinstance(backend, Mapping) or not isinstance(environment, Mapping):
        raise ShadowBuildError("quality_report_runtime_configuration_missing")

    expected_embedding = environment.get("embedding_configuration")
    if not isinstance(expected_embedding, Mapping):
        raise ShadowBuildError("quality_report_runtime_configuration_missing")
    governed_embedding = expected_embedding.get("provider") == "governed-node"
    if governed_embedding:
        # A governed server deliberately does not carry the compute model
        # name/revision in EMBEDDER_* environment variables.  The active
        # private node identity is revalidated against the report immediately
        # after the generation embedder is bootstrapped below; requiring the
        # legacy provider variables here would reject a valid governed build.
        if os.environ.get("PP_CONTROL_PLANE", "0").strip() != "1":
            raise ShadowBuildError("quality_report_embedding_configuration_not_current")
    else:
        model = os.environ.get("EMBEDDER_MODEL", "mxbai-embed-large").strip()
        revision = os.environ.get("EMBEDDER_MODEL_REVISION", model).strip()
        raw_dimension = os.environ.get(
            "PP_EMBEDDING_DIM",
            os.environ.get("EMBEDDER_DIMENSION", "1024"),
        )
        try:
            dimension = int(raw_dimension)
        except (TypeError, ValueError) as exc:
            raise ShadowBuildError("quality_report_embedding_configuration_not_current") from exc
        current_embedding = {
            "provider": canonical_embedding_provider(os.environ.get("EMBEDDER_PROVIDER", "ollama")),
            "model": model,
            "model_revision": revision,
            "dimension": dimension,
        }
        if dict(expected_embedding) != current_embedding:
            raise ShadowBuildError("quality_report_embedding_configuration_not_current")

    # Live evidence deliberately replaces stateful retrieval controls with an
    # isolated benchmark profile.  Keep the embedding identity bound to the
    # real staged runtime, but compare the benchmark-controlled settings to
    # that same normalized profile.  Comparing them to the raw staged
    # environment would reject valid structure-aware production candidates.
    benchmark_environment = _quality_report_fingerprint_environment(report)
    configuration_environment = (
        os.environ if benchmark_environment is None else benchmark_environment
    )

    expected_retrieval = environment.get("retrieval_configuration")
    if not isinstance(expected_retrieval, Mapping):
        raise ShadowBuildError("quality_report_runtime_configuration_missing")
    current_retrieval = {
        "index_text_policy": configuration_environment.get(
            "PP_MEMORY_INDEX_TEXT_POLICY", "legacy"
        ).strip(),
        **{
            name: configuration_environment.get(name, default)
            for name, default in MAX_V1_CONTROL_RETRIEVAL_CONFIGURATION.items()
            if name != "index_text_policy"
        },
    }
    if dict(expected_retrieval) != current_retrieval:
        raise ShadowBuildError("quality_report_retrieval_configuration_not_current")

    force_python = configuration_environment.get("PP_FORCE_PYTHON_SUPPLY", "0") == "1"
    prefer_rust = configuration_environment.get("PP_PREFER_RUST_SUPPLY", "1") == "1"
    current_supply_runtime = "python" if force_python else "auto"
    current_requested_runtime = "python" if force_python or not prefer_rust else "rust"
    if (
        environment.get("supply_runtime") != current_supply_runtime
        or backend.get("requested_runtime") != current_requested_runtime
        or backend.get("requested_policy")
        != configuration_environment.get("PP_RETRIEVAL_FUSION_POLICY", "legacy-auto").strip()
    ):
        raise ShadowBuildError("quality_report_runtime_configuration_not_current")

    current_runtime = {
        "os": platform.system(),
        "os_release": platform.release(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
    }
    if environment.get("runtime") != current_runtime:
        raise ShadowBuildError("quality_report_runtime_metadata_not_current")

    effective_runtime = backend.get("effective_runtime")
    expected_rust_runtime = backend.get("rust_runtime")
    if effective_runtime == "python":
        if expected_rust_runtime is not None:
            raise ShadowBuildError("quality_report_rust_runtime_unexpected")
    elif effective_runtime == "rust":
        try:
            current_rust_runtime = loaded_rust_extension_identity(ROOT)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise ShadowBuildError("quality_report_rust_runtime_identity_unavailable") from exc
        if expected_rust_runtime != current_rust_runtime:
            raise ShadowBuildError("quality_report_rust_runtime_identity_not_current")
    else:
        raise ShadowBuildError("quality_report_effective_runtime_invalid")


def _sqlite_uri(path: Path, *, mode: str) -> str:
    return f"{path.resolve(strict=True).as_uri()}?mode={mode}"


def _memory_row_count(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT COUNT(*) FROM memories").fetchone()
    except sqlite3.Error as exc:
        raise ShadowBuildError("source_memories_unreadable") from exc
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
        raise ShadowBuildError("source_memory_count_invalid")
    return row[0]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_fingerprint(path: Path) -> SidecarFingerprint | None:
    """Hash a WAL sidecar while rejecting replacement or partial reads."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ShadowBuildError("source_database_wal_unreadable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ShadowBuildError("source_database_wal_not_regular_file")
    try:
        sha256 = _sha256_file(path)
        after = path.lstat()
    except FileNotFoundError as exc:
        raise ShadowBuildError("source_database_wal_changed_while_hashing") from exc
    except OSError as exc:
        raise ShadowBuildError("source_database_wal_unreadable") from exc
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ShadowBuildError("source_database_wal_changed_while_hashing")
    # A read-only open of a checkpointed WAL-mode database may create and then
    # remove an empty sidecar.  It contains no committed state, so binding its
    # transient inode and mtime would make an otherwise quiescent source
    # impossible to fingerprint.  Non-empty WAL files remain fully bound.
    if after.st_size == 0:
        return None
    return SidecarFingerprint(
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        sha256=sha256,
    )


def _source_fingerprint(path: Path) -> SourceFingerprint:
    resolved = path.expanduser().resolve(strict=True)
    before = resolved.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ShadowBuildError("source_database_not_regular_file")
    sha256 = _sha256_file(resolved)
    after_hash = resolved.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after_hash.st_dev,
        after_hash.st_ino,
        after_hash.st_size,
        after_hash.st_mtime_ns,
    ):
        raise ShadowBuildError("source_database_changed_while_hashing")
    wal_path = Path(f"{resolved}-wal")
    wal_before = _sidecar_fingerprint(wal_path)
    try:
        with closing(sqlite3.connect(_sqlite_uri(resolved, mode="ro"), uri=True)) as connection:
            connection.execute("PRAGMA query_only = ON")
            row_count = _memory_row_count(connection)
    except sqlite3.Error as exc:
        raise ShadowBuildError("source_database_read_only_open_failed") from exc
    wal_after = _sidecar_fingerprint(wal_path)
    if wal_before != wal_after:
        raise ShadowBuildError("source_database_wal_changed_during_fingerprint")
    final = resolved.stat()
    return SourceFingerprint(
        device=final.st_dev,
        inode=final.st_ino,
        size=final.st_size,
        mtime_ns=final.st_mtime_ns,
        sha256=sha256,
        memory_row_count=row_count,
        wal=wal_after,
    )


@contextmanager
def _private_sqlite_backup(source_path: Path) -> Iterator[Path]:
    """Yield a mode-600 online backup below a mode-700 temporary directory."""

    source = source_path.expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="plastic-promise-shadow-") as raw_directory:
        directory = Path(raw_directory)
        directory.chmod(0o700)
        clone = directory / "source-snapshot.db"
        descriptor = os.open(clone, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        source_connection: sqlite3.Connection | None = None
        clone_connection: sqlite3.Connection | None = None
        try:
            source_connection = sqlite3.connect(_sqlite_uri(source, mode="ro"), uri=True)
            source_connection.execute("PRAGMA query_only = ON")
            clone_connection = sqlite3.connect(clone)
            source_connection.backup(clone_connection)
            clone_connection.commit()
        except sqlite3.Error as exc:
            raise ShadowBuildError("sqlite_backup_failed") from exc
        finally:
            if clone_connection is not None:
                clone_connection.close()
            if source_connection is not None:
                source_connection.close()
        clone.chmod(0o600)
        with closing(sqlite3.connect(clone)) as verification:
            quick_check = verification.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise ShadowBuildError("sqlite_backup_quick_check_failed")
        yield clone


@contextmanager
def _temporary_environment(updates: Mapping[str, str | None]) -> Iterator[None]:
    previous: dict[str, str | object] = {key: os.environ.get(key, _UNSET) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is _UNSET:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)


def _create_context_engine():
    from plastic_promise.core.context_engine import ContextEngine

    return ContextEngine(use_sqlite=True)


def _get_embedder():
    from plastic_promise.core.embedder import get_embedder

    return get_embedder(fallback_on_error=False)


def _generation_embedder(engine: object, *, project_id: str):
    """Select the governed node embedder for a generation build.

    The production server may keep only the server-side managed projection,
    so a generation build must bootstrap the same private node runtime used by
    MCP before it can create vectors.  A blocked governed route fails closed;
    it never silently falls back to the legacy process-wide provider.
    """

    runtime_reader = getattr(engine, "memory_index_node_runtime", None)
    runtime = runtime_reader() if callable(runtime_reader) else None
    if runtime is None and os.environ.get("PP_CONTROL_PLANE", "0").strip() == "1":
        from plastic_promise.core.node_runtime_bootstrap import (
            bootstrap_memory_index_node_runtime,
        )

        report = bootstrap_memory_index_node_runtime(engine)
        if getattr(report, "state", None) == "blocked":
            raise ShadowBuildError(str(getattr(report, "reason", "node_routing_blocked")))
        runtime = runtime_reader() if callable(runtime_reader) else None
    if runtime is not None:
        try:
            from plastic_promise.core.memory_index_node_runtime import GovernedRetrievalEmbedder

            return GovernedRetrievalEmbedder(runtime, default_project_id=project_id)
        except Exception as exc:
            raise ShadowBuildError("governed_generation_embedder_unavailable") from exc
    return _get_embedder()


def _create_lancedb_store(
    index_path: Path,
    embedder: object,
    *,
    persist_index_material: bool = True,
):
    from plastic_promise.core.lancedb_store import LanceDBStore

    return LanceDBStore(
        str(index_path),
        embedder,
        read_only=False,
        persist_index_material=persist_index_material,
    )


def _close_shadow_resources(engine: object | None, embedder: object | None) -> None:
    sqlite_store = getattr(engine, "_sqlite", None)
    connection = getattr(sqlite_store, "_conn", None)
    if connection is not None:
        with suppress(Exception):
            connection.close()
    close = getattr(embedder, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


def _assert_shadow_source_fingerprint(snapshot: SourceSnapshot, engine: object) -> None:
    """Reject clone-only canonical mutations that would detach the artifact.

    Generation builds run migrations and index-material repair against a
    private SQLite clone.  Those writes are safe for the clone, but an index
    built from them cannot be paired with the unchanged source database.  A
    logical fingerprint check before and after the rebuild keeps that case
    fail-closed while retaining compatibility with legacy fixtures that have
    no fingerprint evidence.
    """

    expected = snapshot.index_outbox.get("source_fingerprint")
    if expected is None:
        return
    sqlite_store = getattr(engine, "_sqlite", None)
    connection = getattr(sqlite_store, "_conn", None)
    if not isinstance(connection, sqlite3.Connection):
        raise ShadowBuildError("shadow_source_database_unavailable")
    from plastic_promise.core.index_outbox_reconciliation import (
        IndexOutboxReconciliationError,
        canonical_source_fingerprint,
    )

    try:
        observed = canonical_source_fingerprint(connection)
    except IndexOutboxReconciliationError as exc:
        raise ShadowBuildError(str(exc)) from exc
    if observed != expected:
        raise ShadowBuildError("shadow_source_fingerprint_mismatch")


def _runtime_embedder_identity(embedder: object) -> tuple[str, str, int]:
    model = str(getattr(embedder, "model_name", "") or "").strip()
    dimension = getattr(embedder, "dim", 0)
    candidate = embedder
    revision = ""
    visited: set[int] = set()
    while candidate is not None and id(candidate) not in visited:
        visited.add(id(candidate))
        value = getattr(candidate, "_model_revision", "")
        if isinstance(value, str) and value.strip():
            revision = value.strip()
            break
        candidate = getattr(candidate, "_delegate", None)
    if not revision:
        revision = os.environ.get("EMBEDDER_MODEL_REVISION", model).strip()
    if (
        not model
        or not revision
        or isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or dimension <= 0
    ):
        raise ShadowBuildError("runtime_embedding_identity_invalid")

    # ``get_embedder`` is a process-wide singleton.  A long-lived worker can
    # therefore retain a client for endpoint A after its environment has been
    # reloaded with endpoint B.  Model/revision/dimension alone cannot detect
    # that split, while the derived index identity already includes the
    # provider endpoint (and chunking configuration).  When the provider was
    # explicitly configured, bind the object to the current environment
    # before any vectors are written.
    configured_provider = os.environ.get("EMBEDDER_PROVIDER")
    if configured_provider is not None:
        provider = configured_provider.strip().casefold()
        if provider in {"openai", "openai-compatible", "cloud"}:
            runtime_index = getattr(embedder, "index_model_name", None)
            if not isinstance(runtime_index, str) or not runtime_index.strip():
                raise ShadowBuildError("runtime_embedding_identity_invalid")
            try:
                from plastic_promise.core.memory_index import effective_embedding_model_name

                configured_index = effective_embedding_model_name()
            except (TypeError, ValueError) as exc:
                raise ShadowBuildError(str(exc)) from exc
            if configured_index != runtime_index.strip():
                raise ShadowBuildError("runtime_embedding_identity_environment_mismatch")
    return model, revision, dimension


def _configured_embedding_index_identity() -> str | None:
    """Return the environment-bound derived-index identity when configured."""

    from plastic_promise.core.embedding_index_identity import (
        EmbeddingIndexIdentityError,
        configured_embedding_index_identity,
    )

    try:
        return configured_embedding_index_identity()
    except EmbeddingIndexIdentityError as exc:
        raise ShadowBuildError(str(exc)) from exc


def _eligible_memories(
    engine: object, *, project_id: str | None = None
) -> dict[str, dict[str, Any]]:
    from plastic_promise.core.lancedb_store import LanceDBStore

    store = object.__new__(LanceDBStore)
    canonical_loader = getattr(store, "_canonical_engine_memories", None)
    eligibility_filter = getattr(store, "_eligible_engine_memories", None)
    if not callable(canonical_loader) or not callable(eligibility_filter):
        raise ShadowBuildError("canonical_eligibility_api_unavailable")
    canonical = canonical_loader(engine)
    if not isinstance(canonical, dict):
        raise ShadowBuildError("canonical_memories_unavailable")
    eligible = eligibility_filter(engine, canonical)
    if not isinstance(eligible, dict):
        raise ShadowBuildError("eligible_memories_unavailable")
    if project_id is None:
        return eligible
    scoped: dict[str, dict[str, Any]] = {}
    for memory_id, memory in eligible.items():
        owner = str(memory.get("project_id") or "").strip()
        if owner != project_id:
            continue
        scoped[memory_id] = memory
    return scoped


def _project_eligible_memories(engine: object, project_id: str) -> dict[str, dict[str, Any]]:
    """Load only records with an explicit, matching project binding."""
    try:
        return _eligible_memories(engine, project_id=project_id)
    except TypeError as exc:
        if "project_id" not in str(exc):
            raise
        legacy = _eligible_memories(engine)
        if not isinstance(legacy, dict):
            raise ShadowBuildError("eligible_memories_unavailable") from exc
        return {
            key: value
            for key, value in legacy.items()
            if isinstance(value, Mapping)
            and str(value.get("project_id") or "").strip() == project_id
        }


def _source_index_material_sha256(
    memories: Mapping[str, Mapping[str, Any]],
    *,
    expected_policy: str,
) -> str:
    """Bind the exact persisted text and filter material used by LanceDB."""

    from plastic_promise.core.memory_index import read_persisted_index_material

    rows: dict[str, dict[str, object]] = {}
    for memory_id, memory in memories.items():
        if not isinstance(memory_id, str) or not isinstance(memory, Mapping):
            raise ShadowBuildError("source_index_material_invalid")
        material = read_persisted_index_material(memory)
        if material is None:
            raise ShadowBuildError("source_index_material_unverifiable")
        if material.policy != expected_policy:
            raise ShadowBuildError("source_index_text_policy_mismatch")
        rows[memory_id] = {
            "text": material.search_text,
            "tier": memory.get("tier", "L1"),
            "category": memory.get("category", "other"),
            "scope": memory.get("scope", "global"),
        }
    try:
        return index_material_sha256(expected_policy, rows)
    except (TypeError, ValueError) as exc:
        raise ShadowBuildError("source_index_material_invalid") from exc


def _assert_source_index_material(snapshot: SourceSnapshot, engine: object) -> None:
    eligible = _project_eligible_memories(engine, snapshot.project_id)
    if frozenset(eligible) != snapshot.eligible_memory_ids:
        raise ShadowBuildError("shadow_source_eligibility_changed")
    observed = _source_index_material_sha256(
        eligible,
        expected_policy=snapshot.index_text_policy,
    )
    if observed != snapshot.index_material_sha256:
        raise ShadowBuildError("shadow_index_material_mismatch")


def _index_outbox_evidence(
    connection: sqlite3.Connection, *, project_id: str | None = None
) -> dict[str, Any]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_outbox'"
    ).fetchone()
    if table is None:
        return {
            "status": "unresolved",
            "reason": "store_outbox_table_absent",
            "snapshot_jobs": 0,
            "active_snapshot_jobs": 0,
            "reconciled": False,
        }
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(store_outbox)").fetchall()
    }
    # Keep old/minimal fixture databases readable, but never claim a durable
    # watermark when the immutable payload columns are unavailable.
    if not set(_OUTBOX_IMMUTABLE_COLUMNS).issubset(columns):
        if not {"tool_name", "status"}.issubset(columns):
            raise ShadowBuildError("generation_mode_requires_outbox_watermark")
        placeholders = ",".join("?" for _ in _INDEX_OUTBOX_TOOLS)
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM store_outbox "
            f"WHERE tool_name IN ({placeholders}) GROUP BY status ORDER BY status",
            _INDEX_OUTBOX_TOOLS,
        ).fetchall()
        counts = {str(status): int(count) for status, count in rows}
        return {
            "status": "unresolved",
            "reason": "generation_manifest_has_no_outbox_snapshot_watermark",
            "snapshot_jobs": sum(counts.values()),
            "active_snapshot_jobs": sum(
                count for status, count in counts.items() if status not in {"done", "failed"}
            ),
            "status_counts": counts,
            "reconciled": False,
            "required_action": (
                "after promotion, inspect and explicitly requeue only durable index jobs "
                "newer than a separately recorded cutover watermark"
            ),
        }
    from plastic_promise.core.index_outbox_reconciliation import snapshot_index_outbox

    return snapshot_index_outbox(connection, project_id=project_id)


def _prepare_source_snapshot(
    clone_path: Path,
    *,
    project_id: str,
    expected_index_text_policy: str,
) -> SourceSnapshot:
    snapshot_sha256 = _sha256_file(clone_path)
    with closing(sqlite3.connect(clone_path)) as connection:
        total_rows = _memory_row_count(connection)
        outbox = _index_outbox_evidence(connection, project_id=project_id)

    engine: object | None = None
    with _temporary_environment(
        {
            "PLASTIC_DB_PATH": str(clone_path),
            "LDB_INIT_ON_HEAVY_INIT": "0",
            "LDB_BACKFILL_ON_INIT": "0",
            "LDB_REBUILD_ON_INIT": "0",
            "PLASTIC_LANCEDB_GENERATION_ROOT": None,
        }
    ):
        try:
            engine = _create_context_engine()
            eligible = _project_eligible_memories(engine, project_id)
            eligible_ids = frozenset(eligible)
            material_sha256 = _source_index_material_sha256(
                eligible,
                expected_policy=expected_index_text_policy,
            )
        finally:
            _close_shadow_resources(engine, None)
    return SourceSnapshot(
        path=clone_path,
        project_id=project_id,
        sha256=snapshot_sha256,
        memory_row_count=total_rows,
        eligible_memory_ids=eligible_ids,
        index_text_policy=expected_index_text_policy,
        index_material_sha256=material_sha256,
        index_outbox=outbox,
    )


def _build_callback(
    *,
    snapshot: SourceSnapshot,
    quality_report: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
    project_id: str,
    expected_index_identity: str | None = None,
    require_index_identity_binding: bool = False,
    runtime_environment_validator: Callable[[], None] | None = None,
):
    def repair_missing_rows(store: object, engine: object, expected_ids: frozenset[str]) -> int:
        """Retry only transiently missing rows after the full rebuild pass."""

        backfill = getattr(store, "backfill", None)
        list_memory_ids = getattr(store, "list_memory_ids", None)
        if not callable(backfill) or not callable(list_memory_ids):
            return 0
        repaired = 0
        for _attempt in range(_SHADOW_REPAIR_PASSES):
            actual_ids = set(list_memory_ids())
            if actual_ids == set(expected_ids):
                break
            if actual_ids - expected_ids:
                break
            with _temporary_environment(
                {"LDB_BACKFILL_MAX_PER_CALL": str(max(len(expected_ids), 1))}
            ):
                added = backfill(engine)
            if isinstance(added, bool) or not isinstance(added, int) or added < 0:
                raise ShadowBuildError("shadow_rebuild_repair_result_invalid")
            repaired += added
            _assert_shadow_source_fingerprint(snapshot, engine)
            _assert_source_index_material(snapshot, engine)
            if runtime_environment_validator is not None:
                runtime_environment_validator()
        return repaired

    def build(index_path: Path) -> BuildResult:
        engine: object | None = None
        embedder: object | None = None
        expected_ids = snapshot.eligible_memory_ids
        with _temporary_environment(
            {
                "PLASTIC_DB_PATH": str(snapshot.path),
                "PLASTIC_LANCEDB_PATH": str(index_path),
                "LDB_INIT_ON_HEAVY_INIT": "0",
                "LDB_BACKFILL_ON_INIT": "0",
                "LDB_REBUILD_ON_INIT": "0",
                "LDB_REBUILD_MAX_PER_CALL": str(max(len(expected_ids), 1)),
                "PLASTIC_LANCEDB_GENERATION_ROOT": None,
            }
        ):
            try:
                if runtime_environment_validator is not None:
                    runtime_environment_validator()
                engine = _create_context_engine()
                _assert_shadow_source_fingerprint(snapshot, engine)
                _assert_source_index_material(snapshot, engine)
                embedder = _generation_embedder(engine, project_id=project_id)
                observed_identity = _runtime_embedder_identity(embedder)
                required_identity = (
                    expected_identity["embedding_model"],
                    expected_identity["model_revision"],
                    expected_identity["embedding_dimension"],
                )
                if observed_identity != required_identity:
                    raise ShadowBuildError("runtime_embedding_identity_mismatch")
                if require_index_identity_binding:
                    runtime_index_identity = getattr(embedder, "index_model_name", None)
                    if runtime_index_identity is None:
                        runtime_index_identity = observed_identity[0]
                    elif (
                        not isinstance(runtime_index_identity, str)
                        or not runtime_index_identity.strip()
                    ):
                        raise ShadowBuildError("runtime_embedding_identity_invalid")
                    if runtime_index_identity.strip() != observed_identity[0]:
                        raise ShadowBuildError(
                            "generation_mode_requires_embedding_index_identity_binding"
                        )
                if expected_index_identity is not None:
                    observed_index_identity = getattr(embedder, "index_model_name", None)
                    if (
                        not isinstance(observed_index_identity, str)
                        or observed_index_identity.strip() != expected_index_identity
                    ):
                        raise ShadowBuildError("runtime_embedding_index_identity_mismatch")
                # A shadow build may derive a new chunk/model material but
                # must never write it back to canonical SQLite. Promotion
                # binds the derived generation to the original snapshot;
                # canonical material migration belongs to maintenance.
                store = _create_lancedb_store(
                    index_path,
                    embedder,
                    persist_index_material=False,
                )
                rebuilt = store.rebuild_all(engine, memory_ids=expected_ids)
                rebuilt += repair_missing_rows(store, engine, expected_ids)
                _assert_shadow_source_fingerprint(snapshot, engine)
                _assert_source_index_material(snapshot, engine)
                actual_count = store.count_rows()
                actual_ids = store.list_memory_ids()
                failures = tuple(getattr(store, "_index_failures", ()) or ())
                if rebuilt != len(expected_ids):
                    raise ShadowBuildError("shadow_rebuild_partial")
                if actual_count != len(expected_ids) or actual_ids != set(expected_ids):
                    raise ShadowBuildError("shadow_rebuild_backend_mismatch")
                if failures:
                    raise ShadowBuildError("shadow_rebuild_index_failure")
                if runtime_environment_validator is not None:
                    runtime_environment_validator()
                return BuildResult(row_count=actual_count, quality_report=quality_report)
            finally:
                _close_shadow_resources(engine, embedder)

    return build


def _load_default_artifact_verifier() -> ArtifactVerifier:
    try:
        from plastic_promise.core.lancedb_artifact import verify_lancedb_artifact
    except ImportError as exc:  # pragma: no cover - deployment/package skew.
        raise ShadowBuildError("artifact_verifier_unavailable") from exc
    return verify_lancedb_artifact


def _shadow_generation_build(
    generation_root: Path,
    generation_id: str,
    project_id: str | Path,
    source_db: Path,
    quality_report_path: Path | None = None,
    candidate_manifest_path: Path | None = None,
    *,
    artifact_verifier: ArtifactVerifier | None,
) -> dict[str, Any]:
    # Keep the pre-project-binding call shape rejected; generation identity is
    # never safe to infer from a legacy/global default.
    if quality_report_path is None and isinstance(project_id, Path):
        raise ShadowBuildError("generation_mode_requires_project_id")
    if quality_report_path is None:
        raise ShadowBuildError("generation_mode_requires_quality_report")
    project_id = str(project_id)
    raw_report = _load_json_object(quality_report_path)
    candidate_manifest = None
    if raw_report.get("schema_version") == RECALL_QUALITY_REPORT_SCHEMA:
        candidate_id = raw_report.get("candidate_id")
        # max-v1 is the preregistered control and has no calibrated parameters;
        # only a selected WRRF policy needs an external frozen manifest.
        requires_candidate_manifest = isinstance(candidate_id, str) and candidate_id.startswith(
            "wrrf-v1:"
        )
        if requires_candidate_manifest and candidate_manifest_path is None:
            raise ShadowBuildError("generation_mode_requires_candidate_manifest_for_v2")
        if candidate_manifest_path is not None:
            try:
                from plastic_promise.core.recall_experiment import load_frozen_manifest

                candidate_manifest = load_frozen_manifest(candidate_manifest_path)
            except (OSError, TypeError, ValueError) as exc:
                raise ShadowBuildError("candidate_manifest_invalid") from exc
        quality_report = _normalize_quality_report(
            raw_report,
            candidate_manifest=candidate_manifest,
        )
    else:
        quality_report = _normalize_quality_report(raw_report)
    v2_report = raw_report.get("schema_version") == RECALL_QUALITY_REPORT_SCHEMA
    runtime_environment_validator: Callable[[], None] | None = None
    if v2_report:

        def validate_runtime_environment() -> None:
            _assert_quality_report_runtime_environment(quality_report)

        runtime_environment_validator = validate_runtime_environment
        runtime_environment_validator()
    identity = _quality_identity(quality_report)
    source = source_db.expanduser().resolve(strict=True)
    before = _source_fingerprint(source)
    try:
        with _private_sqlite_backup(source) as clone_path:
            snapshot = _prepare_source_snapshot(
                clone_path,
                project_id=project_id,
                expected_index_text_policy=identity["index_text_policy"],
            )
            has_outbox_snapshot = snapshot.index_outbox.get("status") == "snapshot"
            if (
                snapshot.index_outbox.get("status") == "unresolved"
                and int(snapshot.index_outbox.get("snapshot_jobs", 0) or 0) != 0
            ):
                # Without the immutable payload columns there is no safe
                # watermark to reconcile. Refuse to create a generation that
                # could silently strand even a completed/failed index job.
                raise ShadowBuildError("generation_mode_requires_outbox_watermark")
            configured_index_identity = _configured_embedding_index_identity()
            if (
                not has_outbox_snapshot
                and configured_index_identity is not None
                and configured_index_identity != identity["embedding_model"]
            ):
                # Endpoint/provider/chunking decorations must be persisted with
                # a source-bound outbox snapshot. Without that binding the
                # runtime cannot prove which derived index owner produced the
                # vectors, so refuse to publish an unusable generation.
                raise ShadowBuildError("generation_mode_requires_embedding_index_identity_binding")
            spec = GenerationSpec(
                generation_id=generation_id,
                project_id=project_id,
                index_schema=INDEX_SCHEMA,
                source_db_sha256=snapshot.sha256,
                source_row_count=len(snapshot.eligible_memory_ids),
                index_outbox_watermark=(
                    snapshot.index_outbox.get("watermark")
                    if snapshot.index_outbox.get("status") == "snapshot"
                    else None
                ),
                index_outbox_digest=(
                    snapshot.index_outbox.get("immutable_digest")
                    if snapshot.index_outbox.get("status") == "snapshot"
                    else None
                ),
                index_outbox_job_count=(
                    snapshot.index_outbox.get("job_count")
                    if snapshot.index_outbox.get("status") == "snapshot"
                    else None
                ),
                index_outbox_source_fingerprint=(
                    snapshot.index_outbox.get("source_fingerprint") if has_outbox_snapshot else None
                ),
                embedding_index_identity=(
                    configured_index_identity if has_outbox_snapshot else None
                ),
                index_material_sha256=snapshot.index_material_sha256,
                **identity,
            )
            verifier = artifact_verifier or _load_default_artifact_verifier()
            with GenerationManager(
                generation_root,
                artifact_verifier=verifier,
            ) as manager:
                manifest = manager.build_generation(
                    spec,
                    _build_callback(
                        snapshot=snapshot,
                        quality_report=quality_report,
                        expected_identity=identity,
                        expected_index_identity=spec.embedding_index_identity,
                        require_index_identity_binding=not has_outbox_snapshot,
                        project_id=project_id,
                        runtime_environment_validator=runtime_environment_validator,
                    ),
                )
            result = {
                "generation": manifest.to_dict(),
                "promoted": False,
                "source_snapshot": {
                    "sha256": snapshot.sha256,
                    "sqlite_memory_rows": snapshot.memory_row_count,
                    "eligible_index_rows": len(snapshot.eligible_memory_ids),
                },
                "benchmark": {
                    "corpus_sha256": spec.benchmark_corpus_sha256,
                    "corpus_count": spec.benchmark_corpus_count,
                    "cases_sha256": spec.benchmark_cases_sha256,
                    "case_count": spec.benchmark_case_count,
                },
                "outbox_reconciliation": snapshot.index_outbox,
            }
    finally:
        after = _source_fingerprint(source)
        if after != before:
            raise ShadowBuildError("source_database_changed_during_shadow_build")
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    artifact_verifier: ArtifactVerifier | None = None,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        generation = _generation_arguments(arguments)
        if generation is None:
            return _legacy_rebuild()
        payload = _shadow_generation_build(
            *generation,
            artifact_verifier=artifact_verifier,
        )
    except (GenerationError, OSError, ShadowBuildError, ValueError) as exc:
        print(f"LanceDB rebuild failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
