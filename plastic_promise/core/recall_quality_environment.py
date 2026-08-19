"""Canonical runtime evidence for recall-quality benchmark reports.

The benchmark and shadow rebuild must attest the same code, fixed harness,
dataset, and native retrieval dependency versions.  Keeping these calculations
in one module prevents a report from selecting its own smaller source set.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

CANONICAL_BENCHMARK_SOURCE_RELATIVE_PATHS = (
    "scripts/benchmark_recall_quality.py",
    "scripts/http_mcp_harness.py",
    "scripts/manage_lancedb_generations.py",
    "scripts/rebuild_lancedb.py",
    "pyproject.toml",
)
RETRIEVAL_DEPENDENCY_PACKAGES = ("lancedb", "pyarrow")
RECALL_QUALITY_ENVIRONMENT_KEYS = (
    "EMBEDDER_PROVIDER",
    "EMBEDDER_BASE_URL",
    "EMBEDDER_PATH",
    "EMBEDDER_MODEL",
    "EMBEDDER_MODEL_REVISION",
    "EMBEDDER_DIMENSION",
    "EMBEDDER_SEND_DIMENSIONS",
    "EMBEDDER_BATCH_SIZE",
    "EMBEDDER_MAX_INPUT_BYTES",
    "EMBEDDER_MAX_TOTAL_INPUT_BYTES",
    "EMBEDDER_MAX_REQUEST_BYTES",
    "EMBEDDER_MAX_RESPONSE_BYTES",
    "EMBEDDER_TIMEOUT",
    "EMBEDDER_TOTAL_TIMEOUT",
    "EMBEDDER_MAX_RETRIES",
    "EMBEDDER_RETRY_BACKOFF",
    "EMBEDDER_RETRY_BACKOFF_MAX",
    "EMBEDDER_CIRCUIT_FAILURE_THRESHOLD",
    "EMBEDDER_CIRCUIT_RECOVERY_SECONDS",
    "EMBEDDER_COST_PER_MILLION_TOKENS",
    "EMBEDDER_COST_CURRENCY",
    "EMBEDDER_PRICING_REVISION",
    "EMBEDDER_CACHE_SIZE",
    "EMBEDDER_CACHE_TTL",
    "EMBEDDER_CHUNK_CHARS",
    "EMBEDDER_MAX_CHUNKS",
    "EMBEDDER_LOCAL_MODEL",
    "EMBED_MODEL",
    "PP_EMBEDDING_DIM",
    "PP_RERANK_DISABLED",
    "PP_RERANK_PROVIDERS",
    "PP_RERANK_BASE_URL",
    "PP_RERANK_PATH",
    "PP_RERANK_CLOUD_MODEL",
    "PP_RERANK_CLOUD_MODEL_REVISION",
    "PP_RERANK_MODEL",
    "PP_RERANK_MODEL_REVISION",
    "PP_RERANK_OLLAMA_MODEL",
    "PP_RERANK_TIMEOUT",
    "PP_RERANK_TIMEOUT_SEC",
    "PP_RERANK_TOTAL_TIMEOUT",
    "PP_RERANK_TOTAL_TIMEOUT_SEC",
    "PP_RERANK_MAX_RETRIES",
    "PP_RERANK_MAX_CANDIDATES",
    "PP_RERANK_MAX_DOCUMENT_CHARS",
    "PP_RERANK_MAX_QUERY_CHARS",
    "PP_RERANK_COST_PER_MILLION_TOKENS",
    "PP_RERANK_COST_CURRENCY",
    "PP_RERANK_PRICING_REVISION",
    "OLLAMA_HOST",
    "PP_MEMORY_INDEX_TEXT_POLICY",
    "PP_MEMORY_SUMMARY_INDEX",
    "PP_CODE_MEMORY_ENABLED",
    "PP_MEMORY_CHUNKING",
    "PP_MEMORY_CHUNK_ENRICHMENT",
    "PP_SYNTHESIS_ARTIFACTS",
    "PP_SYNTHESIS_RETRIEVAL",
    "PP_MEMORY_PROPOSALS",
    "PP_SYNTHESIS_OVERFETCH_FACTOR",
    "PP_RETRIEVAL_FUSION_POLICY",
    "PP_RETRIEVAL_RRF_K",
    "PP_RETRIEVAL_RRF_WEIGHTS_JSON",
    "PP_RETRIEVAL_RRF_WINDOWS_JSON",
    "PP_HARD_MIN_SCORE",
    "PP_MMR_VECTOR",
    "PP_BM25_PRESERVATION",
    "PP_BM25_PRESERVATION_THRESHOLD",
    "PP_BM25_PRESERVATION_LIMIT",
    "PP_DECAY_IN_RANKING",
    "PP_CONTEXT_GATE",
    "PP_CONTEXT_GATE_ENFORCE",
    "PP_CANONICAL_HOT_LOOKUP",
    "PP_CANONICAL_HOT_ENFORCE",
    "PP_CANONICAL_HOT_LIMIT",
    "PP_SOURCE_FILTER",
    "PP_SOURCE_EXCLUDE",
    "PP_SOURCE_DAEMON_WEIGHT",
    "PP_SOURCE_SUPERPOWERS_WEIGHT",
    "PP_SOURCE_STEP_CLOSURE_WEIGHT",
    "PP_SOURCE_STEP_AUDITOR_WEIGHT",
    "PP_SOURCE_SKILL_SESSION_WEIGHT",
    "PP_SOURCE_AUTO_INJECT_WEIGHT",
    "PP_CORE_MIN_RELEVANCE",
    "PP_RELATED_MIN_RELEVANCE",
    "PP_DIVERGENT_MIN_RELEVANCE",
    "PP_QUERY_EXPANSION_MAX",
    "PP_TIER_AUTO_PROMOTE",
    "PP_FORCE_PYTHON_SUPPLY",
    "PP_PREFER_RUST_SUPPLY",
    "PP_VECTOR_WEIGHT",
    "PP_QUERY_EXPANSION",
    "PP_FTS_DISABLED",
    "PP_FTS_FUSION",
    "PP_RUST_EXTENSION_DIR",
)
RECALL_QUALITY_ENVIRONMENT_DEFAULTS = {
    "EMBEDDER_PROVIDER": "openai-compatible",
    "EMBEDDER_CACHE_SIZE": "256",
    "EMBEDDER_CACHE_TTL": "300",
    "EMBEDDER_CHUNK_CHARS": "512",
    "EMBEDDER_MAX_CHUNKS": "8",
    "EMBEDDER_LOCAL_MODEL": "BAAI/bge-large-zh-v1.5",
    "PP_RERANK_DISABLED": "0",
    "PP_RERANK_PROVIDERS": "original",
    "PP_MEMORY_INDEX_TEXT_POLICY": "legacy",
    "PP_MEMORY_SUMMARY_INDEX": "0",
    "PP_CODE_MEMORY_ENABLED": "1",
    "PP_MEMORY_CHUNKING": "structure-v1",
    "PP_MEMORY_CHUNK_ENRICHMENT": "shadow",
    "PP_SYNTHESIS_ARTIFACTS": "off",
    "PP_SYNTHESIS_RETRIEVAL": "0",
    "PP_MEMORY_PROPOSALS": "off",
    "PP_SYNTHESIS_OVERFETCH_FACTOR": "2",
    "PP_RETRIEVAL_FUSION_POLICY": "legacy-auto",
    "PP_HARD_MIN_SCORE": "0.30",
    "PP_MMR_VECTOR": "1",
    "PP_BM25_PRESERVATION": "1",
    "PP_BM25_PRESERVATION_THRESHOLD": "0.72",
    "PP_BM25_PRESERVATION_LIMIT": "2",
    "PP_DECAY_IN_RANKING": "1",
    "PP_CONTEXT_GATE": "0",
    "PP_CONTEXT_GATE_ENFORCE": "0",
    "PP_CANONICAL_HOT_LOOKUP": "0",
    "PP_CANONICAL_HOT_ENFORCE": "0",
    "PP_CANONICAL_HOT_LIMIT": "12",
    "PP_SOURCE_FILTER": "1",
    "PP_SOURCE_EXCLUDE": "",
    "PP_SOURCE_DAEMON_WEIGHT": "0.3",
    "PP_SOURCE_SUPERPOWERS_WEIGHT": "0.3",
    "PP_SOURCE_STEP_CLOSURE_WEIGHT": "0.3",
    "PP_SOURCE_STEP_AUDITOR_WEIGHT": "0.3",
    "PP_SOURCE_SKILL_SESSION_WEIGHT": "0.1",
    "PP_SOURCE_AUTO_INJECT_WEIGHT": "0.3",
    "PP_CORE_MIN_RELEVANCE": "0.70",
    "PP_RELATED_MIN_RELEVANCE": "0.40",
    "PP_DIVERGENT_MIN_RELEVANCE": "0.20",
    "PP_QUERY_EXPANSION_MAX": "3",
    "PP_TIER_AUTO_PROMOTE": "1",
    "PP_FORCE_PYTHON_SUPPLY": "0",
    "PP_PREFER_RUST_SUPPLY": "1",
    "PP_VECTOR_WEIGHT": "0.50",
    "PP_QUERY_EXPANSION": "1",
    "PP_FTS_DISABLED": "0",
    "PP_FTS_FUSION": "1",
    "LDB_BACKFILL_ON_INIT": "0",
    "LDB_REBUILD_ON_INIT": "0",
}
RECALL_QUALITY_CANDIDATE_ENVIRONMENT_KEYS = frozenset(
    {
        "PP_RETRIEVAL_FUSION_POLICY",
        "PP_RETRIEVAL_RRF_K",
        "PP_RETRIEVAL_RRF_WEIGHTS_JSON",
        "PP_RETRIEVAL_RRF_WINDOWS_JSON",
    }
)
_SHA256 = frozenset("0123456789abcdef")


def canonical_embedding_provider(value: object) -> str:
    """Return the stable provider identity used by quality evidence."""

    provider = str(value or "").strip().casefold()
    return "openai-compatible" if provider == "cloud" else provider


def recall_quality_environment_fingerprint(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Hash non-secret process settings that can affect benchmark evidence."""

    return _environment_fingerprint(environ, excluded=frozenset())


def recall_quality_comparison_environment_fingerprint(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Hash settings that must stay fixed across fusion-policy candidates."""

    return _environment_fingerprint(
        environ,
        excluded=RECALL_QUALITY_CANDIDATE_ENVIRONMENT_KEYS,
    )


def _environment_fingerprint(
    environ: Mapping[str, str] | None,
    *,
    excluded: frozenset[str],
) -> str:
    source = os.environ if environ is None else environ
    payload = {
        name: (
            canonical_embedding_provider(
                source[name] if name in source else RECALL_QUALITY_ENVIRONMENT_DEFAULTS[name]
            )
            if name == "EMBEDDER_PROVIDER"
            else str(source[name] if name in source else RECALL_QUALITY_ENVIRONMENT_DEFAULTS[name])
        )
        for name in RECALL_QUALITY_ENVIRONMENT_KEYS
        if name not in excluded and (name in source or name in RECALL_QUALITY_ENVIRONMENT_DEFAULTS)
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def rust_source_paths(root: Path) -> tuple[Path, ...]:
    """Return the exact Rust build inputs covered by the embedded build identity."""

    crate = root.expanduser().resolve(strict=True) / "rust" / "context-engine-core"
    candidates = [crate / "Cargo.toml", crate / "Cargo.lock", crate / "build.rs"]
    candidates.extend((crate / "src").rglob("*.rs"))
    resolved: list[Path] = []
    for candidate in candidates:
        path = candidate.resolve(strict=True)
        entry = path.lstat()
        if candidate.is_symlink() or not stat.S_ISREG(entry.st_mode):
            raise ValueError("rust_source_not_regular_file")
        resolved.append(path)
    if len(resolved) < 4:
        raise ValueError("rust_source_incomplete")
    return tuple(sorted(set(resolved), key=lambda path: path.relative_to(crate).as_posix()))


def rust_source_fingerprint(root: Path) -> str:
    """Hash Cargo metadata, the build script, and every Rust source file."""

    resolved_root = root.expanduser().resolve(strict=True)
    crate = resolved_root / "rust" / "context-engine-core"
    digest = hashlib.sha256()
    for path in rust_source_paths(resolved_root):
        label = path.relative_to(crate).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(label).to_bytes(8, "big"))
        digest.update(label)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def loaded_rust_extension_identity(root: Path, module: object | None = None) -> dict[str, str]:
    """Attest the loaded Rust binary and its build-embedded source digest."""

    if module is None:
        from plastic_promise.core.rust_extension import load_context_engine_core

        module = load_context_engine_core()
    module_name = str(getattr(module, "__name__", "") or "").strip()
    version = str(getattr(module, "__version__", "") or "").strip()
    embedded_source = str(getattr(module, "__source_sha256__", "") or "").strip().casefold()
    binary_name = getattr(module, "__file__", None)
    if module_name != "context_engine_core" or not version or len(version) > 128:
        raise ValueError("rust_extension_build_identity_invalid")
    if len(embedded_source) != 64 or any(char not in _SHA256 for char in embedded_source):
        raise ValueError("rust_extension_build_identity_invalid")
    if not isinstance(binary_name, str) or not binary_name:
        raise ValueError("rust_extension_binary_unavailable")
    binary_path = Path(binary_name).expanduser()
    if binary_path.is_symlink():
        raise ValueError("rust_extension_binary_not_regular_file")
    binary_path = binary_path.resolve(strict=True)
    if not stat.S_ISREG(binary_path.lstat().st_mode):
        raise ValueError("rust_extension_binary_not_regular_file")
    current_source = rust_source_fingerprint(root)
    if embedded_source != current_source:
        raise ValueError("rust_extension_source_identity_mismatch")
    return {
        "module": module_name,
        "version": version,
        "binary_sha256": hashlib.sha256(binary_path.read_bytes()).hexdigest(),
        "source_sha256": embedded_source,
    }


def canonical_benchmark_source_paths(root: Path) -> tuple[Path, ...]:
    """Return the benchmark harness files that every quality report covers."""

    resolved_root = root.expanduser().resolve(strict=True)
    return tuple(resolved_root / relative for relative in CANONICAL_BENCHMARK_SOURCE_RELATIVE_PATHS)


def recall_quality_source_paths(
    root: Path,
    dataset_path: Path,
    *,
    benchmark_sources: Iterable[Path] | None = None,
) -> tuple[Path, ...]:
    """Return the sorted, canonical source set for one benchmark dataset."""

    resolved_root = root.expanduser().resolve(strict=True)
    sources = (
        tuple(benchmark_sources)
        if benchmark_sources is not None
        else canonical_benchmark_source_paths(resolved_root)
    )
    package_sources = tuple((resolved_root / "plastic_promise").rglob("*.py"))
    candidates = (*sources, *package_sources, *rust_source_paths(resolved_root), Path(dataset_path))
    resolved: set[Path] = set()
    for candidate in candidates:
        path = candidate.expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"recall_quality_source_not_regular_file:{path}")
        resolved.add(path)
    return tuple(
        sorted(resolved, key=lambda path: recall_quality_source_label(resolved_root, path))
    )


def recall_quality_source_label(root: Path, path: Path) -> str:
    """Return the stable report label for one resolved source path."""

    resolved_root = root.expanduser().resolve(strict=True)
    resolved_path = Path(path).expanduser().resolve()
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError:
        return resolved_path.as_posix()


def recall_quality_source_fingerprint(
    root: Path,
    dataset_path: Path,
    *,
    benchmark_sources: Iterable[Path] | None = None,
) -> str:
    """Hash the canonical report source set with labels and exact bytes."""

    resolved_root = root.expanduser().resolve(strict=True)
    digest = hashlib.sha256()
    for path in recall_quality_source_paths(
        resolved_root,
        dataset_path,
        benchmark_sources=benchmark_sources,
    ):
        content = path.read_bytes()
        label = recall_quality_source_label(resolved_root, path).encode("utf-8")
        digest.update(len(label).to_bytes(8, "big"))
        digest.update(label)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def recall_quality_code_fingerprint(
    root: Path,
    *,
    benchmark_sources: Iterable[Path] | None = None,
) -> str:
    """Hash all code and harness files, excluding the measured dataset."""

    resolved_root = root.expanduser().resolve(strict=True)
    sources = (
        tuple(benchmark_sources)
        if benchmark_sources is not None
        else canonical_benchmark_source_paths(resolved_root)
    )
    paths = sorted(
        {
            path.resolve()
            for path in (
                *sources,
                *(resolved_root / "plastic_promise").rglob("*.py"),
                *rust_source_paths(resolved_root),
            )
            if path.is_file()
        },
        key=lambda path: path.as_posix(),
    )
    digest = hashlib.sha256()
    for path in paths:
        label = path.relative_to(resolved_root).as_posix().encode("utf-8")
        digest.update(label)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def recall_quality_source_commit(root: Path) -> str:
    """Return the exact checkout commit used by a benchmark or rebuild."""

    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root.expanduser().resolve(strict=True),
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    commit = completed.stdout.strip().casefold()
    if (
        completed.returncode != 0
        or len(commit) != 40
        or any(char not in "0123456789abcdef" for char in commit)
    ):
        raise ValueError("recall_quality_source_commit_unavailable")
    return commit


def retrieval_dependency_versions() -> dict[str, str]:
    """Return the exact native retrieval dependencies loaded by the process."""

    versions: dict[str, str] = {}
    for package in RETRIEVAL_DEPENDENCY_PACKAGES:
        try:
            versions[package] = package_version(package)
        except PackageNotFoundError:
            versions[package] = "unavailable"
    return versions
