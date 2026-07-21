"""Plastic Promise Embedder — text-to-vector with provider abstraction.

Default: local sentence-transformers (BAAI/bge-large-zh-v1.5, 1024 dim).
Falls back to Ollama if local model unavailable.
Set EMBEDDER_PROVIDER=openai to use OpenAI text-embedding-3-small (1536 dim).

Provider chain (auto-detected):
  1. local   — sentence-transformers, in-process, zero HTTP (default)
  2. ollama  — local HTTP server, mxbai-embed-large (fallback)
  3. openai  — cloud API, text-embedding-3-small
  4. fallback — zero vectors, text-only retrieval (last resort)

Environment variables:
  EMBEDDER_PROVIDER=local|ollama|openai  (default: local)
  EMBEDDER_MODEL=mxbai-embed-large  (ollama model name)
  EMBEDDER_LOCAL_MODEL=BAAI/bge-large-zh-v1.5  (sentence-transformers model)
  OLLAMA_HOST=http://localhost:11434
  EMBEDDER_CACHE_SIZE=256          (default: 256, set to 0 to disable)
  EMBEDDER_CACHE_TTL=300           (TTL in seconds, default: 300)
  EMBEDDER_TIMEOUT=5               (HTTP timeout for Ollama/OpenAI, default: 5)
  PP_MEMORY_CHUNKING=off|shadow|structure-v1  (default: off)
  PP_MEMORY_CHUNK_ENRICHMENT=off|shadow|on  (default: off; structure-v1 only)
  PP_MEMORY_CHUNK_ENRICHMENT_MODEL=qwen3:8b
  PP_MEMORY_CHUNK_ENRICHMENT_TIMEOUT=45
  EMBEDDER_CHUNK_CHARS=512         (legacy size / structure-v1 soft target)
  EMBEDDER_MAX_CHUNKS=8            (legacy cap only)
  EMBEDDER_STRUCTURE_HARD_CHARS=1024  (structure-v1 oversized-block limit)
  EMBEDDER_STRUCTURE_MAX_CHUNKS=64    (structure-v1 request cap)
  EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS=2000000  (structure-v1 input guard)
"""

import asyncio
import hashlib
import logging
import math
import os
import threading
import time
from abc import ABC, abstractmethod

import requests

from plastic_promise.core.chunking import (
    STRUCTURE_CHUNK_PARITY_PROBE,
    ChunkMaterial,
    has_uncovered_content,
    legacy_character_chunks,
    limit_chunk_materials,
    shadow_chunking_diagnostics,
    structure_aware_chunks,
)
from plastic_promise.core.semantic_chunk_enrichment import (
    SemanticChunkEnricher,
    decode_embedding_plan,
    is_embedding_plan,
)


class Embedder(ABC):
    """Abstract text-to-vector embedder."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Convert text to an embedding vector."""

    async def aembed(self, text: str) -> list[float]:
        """Async wrapper: runs embed() in thread pool to avoid event-loop blocking."""
        return await asyncio.to_thread(self.embed, text)

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed multiple texts."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimension."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier."""

    @property
    def index_model_name(self) -> str:
        """Versioned identity for derived index material."""
        return self.model_name

    def prepare_index_text(self, text: str) -> str:
        """Prepare exact persisted document material; queries should call embed directly."""
        return text

    def close(self) -> None:
        """Release optional provider resources."""
        return None


class CachedEmbedder(Embedder):
    """LRU embedding cache wrapper — eliminates redundant Ollama/API calls.

    Caches embeddings by content hash. Thread-safe with TTL-based expiry.
    Configurable via EMBEDDER_CACHE_SIZE (default 256) and EMBEDDER_CACHE_TTL (default 300s).
    Set EMBEDDER_CACHE_SIZE=0 to disable.

    Provides both sync ``embed()`` and async ``embed_async()`` — the async variant
    runs the delegate's HTTP call in ``asyncio.to_thread()`` to avoid blocking the
    event loop (critical for SSE/MCP request handlers).
    """

    def __init__(self, delegate: Embedder, max_size: int = None, ttl_seconds: int = None) -> None:
        self._delegate = delegate
        self._max_size = (
            max_size if max_size is not None else int(os.environ.get("EMBEDDER_CACHE_SIZE", "256"))
        )
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else int(os.environ.get("EMBEDDER_CACHE_TTL", "300"))
        )
        self._cache: dict[str, tuple[list[float], float]] = {}  # hash -> (vector, timestamp)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def embed(self, text: str) -> list[float]:
        if self._max_size <= 0:
            return self._delegate.embed(text)
        key = self._key(text)
        now = time.time()
        with self._lock:
            if key in self._cache:
                vec, ts = self._cache[key]
                if now - ts < self._ttl:
                    self._hits += 1
                    return vec
                del self._cache[key]
        self._misses += 1
        vec = self._delegate.embed(text)

        # Runtime fallback: if delegate returns zero vectors and is not
        # already FallbackEmbedder, try Ollama as live recovery path.
        # This detects lazy-init failures (e.g., LocalSentenceEmbedder
        # constructor succeeded but _lazy_load() failed at embed time).
        if (
            vec
            and not any(v != 0.0 for v in vec)
            and not isinstance(self._delegate, FallbackEmbedder)
        ):
            import logging

            _log = logging.getLogger("plastic-promise.embedder")
            _log.warning(
                "CachedEmbedder: delegate %s returned zero vector, "
                "attempting runtime fallback to Ollama",
                type(self._delegate).__name__,
            )
            try:
                replacement: Embedder = OllamaEmbedder()
                if isinstance(self._delegate, StructureAwareEmbedder):
                    replacement = StructureAwareEmbedder(replacement)
                ollama_vec = replacement.embed(text)
                if ollama_vec and any(v != 0.0 for v in ollama_vec):
                    _log.info(
                        "CachedEmbedder: Ollama runtime fallback succeeded, "
                        "switching delegate permanently"
                    )
                    previous = self._delegate
                    self._delegate = replacement
                    previous.close()
                    vec = ollama_vec
                else:
                    replacement.close()
            except Exception as e:
                _log.warning("CachedEmbedder: Ollama runtime fallback also failed: %s", e)

        with self._lock:
            if len(self._cache) >= self._max_size:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                del self._cache[oldest_key]
            self._cache[key] = (vec, now)
        return vec

    async def embed_async(self, text: str) -> list[float]:
        """Async variant: runs the delegate HTTP call in a thread to avoid
        blocking the asyncio event loop.  Cache hit returns immediately;
        cache miss offloads the blocking ``requests.post()`` to a thread.

        Only valid when called from inside a running event loop.
        """
        import asyncio as _asyncio

        # Fast path: cache hit (no I/O)
        if self._max_size > 0:
            key = self._key(text)
            now = time.time()
            with self._lock:
                if key in self._cache:
                    vec, ts = self._cache[key]
                    if now - ts < self._ttl:
                        self._hits += 1
                        return vec
        # Slow path: delegate embed in thread so event loop stays responsive
        return await _asyncio.to_thread(self.embed, text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed with cache check per text."""
        if self._max_size <= 0:
            return self._delegate.embed_batch(texts)
        results = []
        uncached_texts = []
        uncached_indices = []
        now = time.time()
        for i, text in enumerate(texts):
            key = self._key(text)
            with self._lock:
                if key in self._cache:
                    vec, ts = self._cache[key]
                    if now - ts < self._ttl:
                        self._hits += 1
                        results.append((i, vec))
                        continue
                    del self._cache[key]
            uncached_texts.append(text)
            uncached_indices.append(i)
            self._misses += 1

        if uncached_texts:
            new_vecs = self._delegate.embed_batch(uncached_texts)
            with self._lock:
                for j, vec in zip(uncached_indices, new_vecs, strict=True):
                    key = self._key(texts[j])
                    if len(self._cache) >= self._max_size:
                        oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                        del self._cache[oldest_key]
                    self._cache[key] = (vec, now)
                    results.append((j, vec))

        results.sort(key=lambda x: x[0])
        return [v for _, v in results]

    @property
    def dim(self) -> int:
        return self._delegate.dim

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def index_model_name(self) -> str:
        return self._delegate.index_model_name

    def prepare_index_text(self, text: str) -> str:
        return self._delegate.prepare_index_text(text)

    def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()

    @property
    def last_chunking_diagnostics(self) -> dict[str, object]:
        diagnostics = getattr(self._delegate, "last_chunking_diagnostics", None)
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

    @property
    def last_index_preparation_diagnostics(self) -> dict[str, object]:
        diagnostics = getattr(self._delegate, "last_index_preparation_diagnostics", None)
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

    @property
    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(total, 1), 3),
                "cache_size": len(self._cache),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl,
            }


class OllamaEmbedder(Embedder):
    """Local Ollama embedding provider.

    Default model: mxbai-embed-large (1024 dim, MTEB top-tier, multilingual).
    Requires Ollama running at OLLAMA_HOST.
    """

    def __init__(self, host: str | None = None, model: str | None = None) -> None:
        raw = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        # 0.0.0.0 is a server bind address — client must connect to localhost
        raw = raw.replace("0.0.0.0", "127.0.0.1")
        if "://" in raw:
            self._host = raw
        else:
            # Plain host[:port] — add scheme and default port if missing
            if ":" in raw:
                self._host = f"http://{raw}"
            else:
                self._host = f"http://{raw}:11434"
        self._model = model or os.getenv("EMBEDDER_MODEL", "mxbai-embed-large")
        self._chunk_chars = _int_env("EMBEDDER_CHUNK_CHARS", 512, minimum=1)
        self._max_chunks = _int_env("EMBEDDER_MAX_CHUNKS", 8, minimum=1)
        self._chunking_mode = os.getenv("PP_MEMORY_CHUNKING", "off").strip().lower()
        if self._chunking_mode not in {"off", "shadow", "structure-v1"}:
            logging.warning("Unknown PP_MEMORY_CHUNKING=%r; using off", self._chunking_mode)
            self._chunking_mode = "off"
        self._structure_hard_chars = _int_env(
            "EMBEDDER_STRUCTURE_HARD_CHARS",
            self._chunk_chars,
            minimum=self._chunk_chars,
        )
        self._structure_max_chunks = _int_env("EMBEDDER_STRUCTURE_MAX_CHUNKS", 64, minimum=1)
        self._structure_max_source_chars = _int_env(
            "EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", 2_000_000, minimum=1
        )
        self._last_chunking_diagnostics: dict[str, object] = {}
        self._last_index_preparation_diagnostics: dict[str, object] = {}
        self._chunk_enricher = SemanticChunkEnricher(host=self._host)
        if self._chunk_enricher.mode != "off" and self._chunking_mode != "structure-v1":
            logging.warning(
                "PP_MEMORY_CHUNK_ENRICHMENT=%s requires PP_MEMORY_CHUNKING=structure-v1; "
                "enrichment is inactive",
                self._chunk_enricher.mode,
            )

    def embed(self, text: str) -> list[float]:
        if is_embedding_plan(text):
            plan = decode_embedding_plan(text)
            if self._chunk_enricher.mode != "on":
                raise ValueError("embedding_plan_mode_mismatch")
            if plan.get("model_identity") != self._chunk_enricher.model_identity:
                raise ValueError("embedding_plan_model_mismatch")
            plan_chunks = plan["chunks"]
            assert isinstance(plan_chunks, list)
            chunks = [str(chunk["embedding_text"]) for chunk in plan_chunks]
            self._last_chunking_diagnostics = {
                "mode": "embedding-plan-v1",
                "chunk_count": len(chunks),
                "source_text_hash": plan.get("source_text_hash", ""),
                "model_identity": plan.get("model_identity", ""),
                "enriched": sum(1 for chunk in plan_chunks if chunk.get("status") == "enriched"),
                "fallbacks": sum(1 for chunk in plan_chunks if chunk.get("status") == "fallback"),
            }
        elif self._chunking_mode == "structure-v1":
            if len(text or "") > self._structure_max_source_chars:
                self._last_chunking_diagnostics = {
                    "mode": "structure-v1",
                    "source_chars": len(text or ""),
                    "resource_limited": True,
                    "error": "structure_chunking_source_too_large",
                }
                raise ValueError("structure_chunking_source_too_large")
            all_materials = structure_aware_chunks(
                text,
                target_chars=self._chunk_chars,
                hard_chars=self._structure_hard_chars,
            )
            resource_limited = len(all_materials) > self._structure_max_chunks
            materials = limit_chunk_materials(all_materials, self._structure_max_chunks)
            chunks = [material.text for material in materials]
            last_source_end = max((material.source_end for material in materials), default=0)
            meaningful_source_end = len((text or "").rstrip())
            self._last_chunking_diagnostics = {
                "mode": "structure-v1",
                "source_chars": len(text or ""),
                "chunk_count": len(materials),
                "covered_source_chars": sum(
                    max(material.source_end - material.source_start, 0) for material in materials
                ),
                "last_source_end": last_source_end,
                "budget_unit": "characters-fallback",
                "truncated": last_source_end < meaningful_source_end
                or resource_limited
                or has_uncovered_content(text or "", materials)
                or any(material.context_truncated for material in materials),
                "max_chunks": self._structure_max_chunks,
                "resource_limited": resource_limited,
                "context_truncated": any(material.context_truncated for material in materials),
            }
        else:
            chunks = legacy_character_chunks(text, self._chunk_chars, self._max_chunks)
            if self._chunking_mode == "shadow":
                self._last_chunking_diagnostics = shadow_chunking_diagnostics(
                    text,
                    target_chars=self._chunk_chars,
                    hard_chars=self._structure_hard_chars,
                    max_chunks=self._max_chunks,
                    legacy_chunks=chunks,
                    max_source_chars=self._structure_max_source_chars,
                )
            else:
                self._last_chunking_diagnostics = {
                    "mode": "legacy",
                    "source_chars": len(text or ""),
                    "chunk_count": len(chunks),
                    "covered_source_chars": sum(len(chunk) for chunk in chunks),
                    "budget_unit": "characters",
                    "truncated": len(text or "") > sum(len(chunk) for chunk in chunks),
                }
        if len(chunks) == 1:
            return self._embed_chunk(chunks[0])
        return _mean_pool_vectors([self._embed_chunk(chunk) for chunk in chunks])

    def prepare_index_text(self, text: str) -> str:
        """Prepare exact document-only material before SQLite hashing/persistence."""

        if self._chunking_mode != "structure-v1" or self._chunk_enricher.mode == "off":
            self._last_index_preparation_diagnostics = {
                "mode": self._chunk_enricher.mode,
                "active": False,
            }
            return text
        if len(text or "") > self._structure_max_source_chars:
            raise ValueError("structure_chunking_source_too_large")
        all_materials = structure_aware_chunks(
            text,
            target_chars=self._chunk_chars,
            hard_chars=self._structure_hard_chars,
        )
        materials = limit_chunk_materials(all_materials, self._structure_max_chunks)
        batch = self._chunk_enricher.prepare_chunks(materials, source_text=text or "")
        self._last_index_preparation_diagnostics = dict(batch.diagnostics)
        if self._chunk_enricher.mode == "shadow":
            return text
        return self._chunk_enricher.build_embedding_plan(text or "", materials, batch)

    def close(self) -> None:
        self._chunk_enricher.close()

    @property
    def last_chunking_diagnostics(self) -> dict[str, object]:
        return dict(self._last_chunking_diagnostics)

    @property
    def last_index_preparation_diagnostics(self) -> dict[str, object]:
        return dict(self._last_index_preparation_diagnostics)

    def _embed_chunk(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self._host}/api/embeddings",
            json={"model": self._model, "prompt": text},
            timeout=float(os.getenv("EMBEDDER_TIMEOUT", "5")),
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dim(self) -> int:
        return 1024

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def index_model_name(self) -> str:
        if self._chunking_mode != "structure-v1":
            return self._model
        identity = (
            f"{self._model}|chunking=structure-v1"
            f"|target_chars={self._chunk_chars}"
            f"|hard_chars={self._structure_hard_chars}"
            f"|max_chunks={self._structure_max_chunks}"
            f"|max_source_chars={self._structure_max_source_chars}"
            "|budget=characters-fallback"
        )
        if self._chunk_enricher.mode == "on":
            identity = f"{identity}|{self._chunk_enricher.index_identity}"
        return identity


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


def _embedding_chunks(text: str, chunk_chars: int, max_chunks: int) -> list[str]:
    """Compatibility wrapper for callers that used the old private helper."""

    return legacy_character_chunks(text, chunk_chars, max_chunks)


def _mean_pool_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    if dim == 0:
        return []
    totals = [0.0] * dim
    count = 0
    for vec in vectors:
        if len(vec) != dim:
            continue
        count += 1
        for i, value in enumerate(vec):
            totals[i] += float(value)
    if count == 0:
        return []
    pooled = [value / count for value in totals]
    norm = math.sqrt(sum(value * value for value in pooled))
    if norm <= 0.0:
        return pooled
    return [value / norm for value in pooled]


def _structure_chunking_settings() -> dict[str, int | str]:
    target = _int_env("EMBEDDER_CHUNK_CHARS", 512, minimum=1)
    return {
        "mode": os.environ.get("PP_MEMORY_CHUNKING", "off").strip().casefold(),
        "engine": os.environ.get("PP_MEMORY_CHUNK_ENGINE", "python").strip().casefold(),
        "target_chars": target,
        "hard_chars": _int_env(
            "EMBEDDER_STRUCTURE_HARD_CHARS",
            target,
            minimum=target,
        ),
        "max_chunks": _int_env("EMBEDDER_STRUCTURE_MAX_CHUNKS", 64, minimum=1),
        "max_source_chars": _int_env(
            "EMBEDDER_STRUCTURE_MAX_SOURCE_CHARS", 2_000_000, minimum=1
        ),
    }


_RUST_CHUNK_PARITY_GATE_LOCK = threading.Lock()
_RUST_CHUNK_PARITY_GATES: dict[
    tuple[object, ...],
    tuple[str, str],
] = {}


def _reset_rust_chunk_parity_gate() -> None:
    """Clear process-local Rust parity decisions (primarily for isolated tests)."""

    with _RUST_CHUNK_PARITY_GATE_LOCK:
        _RUST_CHUNK_PARITY_GATES.clear()


def _rust_chunk_extension_identity() -> tuple[object, ...]:
    """Identify the loaded projection without making the binary path the sole key."""

    from plastic_promise.core.rust_extension import load_context_engine_core

    try:
        rust_core = load_context_engine_core()
    except Exception as exc:
        # A stable unavailable identity avoids repeating either parser while the
        # same loader failure persists. If the extension later loads, its module
        # identity produces a new gate key and parity is checked again.
        return (
            "unavailable",
            id(load_context_engine_core),
            type(exc).__module__,
            type(exc).__qualname__,
            str(exc),
        )

    spec = getattr(rust_core, "__spec__", None)
    origin = getattr(rust_core, "__file__", None) or getattr(spec, "origin", None) or ""
    projection = getattr(rust_core, "structure_chunk_projection", None)
    return (
        "loaded",
        str(getattr(rust_core, "__name__", type(rust_core).__qualname__)),
        str(origin),
        str(getattr(rust_core, "__version__", "")),
        id(rust_core),
        id(projection),
    )


def _rust_chunk_parity_gate_key(
    *,
    target_chars: int,
    hard_chars: int,
    max_chunks: int,
) -> tuple[object, ...]:
    return (
        _rust_chunk_extension_identity(),
        target_chars,
        hard_chars,
        max_chunks,
        id(structure_aware_chunks),
        id(limit_chunk_materials),
        id(_rust_chunk_materials),
    )


def _python_structure_materials(
    text: str,
    *,
    target_chars: int,
    hard_chars: int,
    max_chunks: int,
) -> list[ChunkMaterial]:
    return limit_chunk_materials(
        structure_aware_chunks(text, target_chars=target_chars, hard_chars=hard_chars),
        max_chunks,
    )


def _rust_chunk_materials(
    text: str,
    *,
    target_chars: int,
    hard_chars: int,
    max_chunks: int,
) -> list[ChunkMaterial]:
    """Load Rust's canonical projection and validate its public shape."""

    from plastic_promise.core.rust_extension import load_context_engine_core

    projection = getattr(load_context_engine_core(), "structure_chunk_projection", None)
    if not callable(projection):
        raise RuntimeError("rust_chunking_api_unavailable")
    rows = projection(text, target_chars, hard_chars, max_chunks)
    if not isinstance(rows, (list, tuple)):
        raise RuntimeError("rust_chunking_result_invalid")
    materials: list[ChunkMaterial] = []
    for row in rows:
        try:
            item = dict(row)
            heading_path = tuple(str(value) for value in item.get("heading_path", []))
            material = ChunkMaterial(
                text=str(item["text"]),
                kind=str(item["kind"]),
                heading_path=heading_path,
                source_start=int(item["source_start"]),
                source_end=int(item["source_end"]),
                context_truncated=bool(item.get("context_truncated", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("rust_chunking_result_invalid") from exc
        if material.source_start < 0 or material.source_end < material.source_start:
            raise RuntimeError("rust_chunking_span_invalid")
        materials.append(material)
    return materials or [ChunkMaterial("", "empty", (), 0, 0)]


def _effective_structure_materials(
    text: str,
    settings: dict[str, int | str],
) -> tuple[list[ChunkMaterial], str, str]:
    """Return chunks, effective engine, and a stable fallback reason."""

    target = int(settings["target_chars"])
    hard = int(settings["hard_chars"])
    max_chunks = int(settings["max_chunks"])
    requested = str(settings["engine"])
    if requested != "rust":
        return (
            _python_structure_materials(
                text,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            ),
            "python",
            "",
        )

    gate_key = _rust_chunk_parity_gate_key(
        target_chars=target,
        hard_chars=hard,
        max_chunks=max_chunks,
    )
    with _RUST_CHUNK_PARITY_GATE_LOCK:
        gate = _RUST_CHUNK_PARITY_GATES.get(gate_key)
        if gate is None:
            python_materials = _python_structure_materials(
                STRUCTURE_CHUNK_PARITY_PROBE,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            )
            try:
                rust_materials = _rust_chunk_materials(
                    STRUCTURE_CHUNK_PARITY_PROBE,
                    target_chars=target,
                    hard_chars=hard,
                    max_chunks=max_chunks,
                )
                if rust_materials != python_materials:
                    raise RuntimeError("rust_python_chunking_mismatch")
            except Exception as exc:
                fallback_reason = str(exc) or "rust_chunking_failed"
                _RUST_CHUNK_PARITY_GATES[gate_key] = ("fallback", fallback_reason)
                gate = ("fallback", fallback_reason)
            else:
                _RUST_CHUNK_PARITY_GATES[gate_key] = ("matched", "")
                gate = ("matched", "")

    gate_status, fallback_reason = gate
    if gate_status == "fallback":
        return (
            _python_structure_materials(
                text,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            ),
            "python",
            fallback_reason,
        )

    try:
        rust_materials = _rust_chunk_materials(
            text,
            target_chars=target,
            hard_chars=hard,
            max_chunks=max_chunks,
        )
    except Exception as exc:
        fallback_reason = str(exc) or "rust_chunking_failed"
        with _RUST_CHUNK_PARITY_GATE_LOCK:
            current_gate = _RUST_CHUNK_PARITY_GATES.get(gate_key)
            if current_gate is None or current_gate[0] == "matched":
                _RUST_CHUNK_PARITY_GATES[gate_key] = ("fallback", fallback_reason)
            else:
                fallback_reason = current_gate[1]
        return (
            _python_structure_materials(
                text,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            ),
            "python",
            fallback_reason,
        )

    # Another request can discover a Rust runtime failure while this call is
    # executing. Honor that process-level fallback before returning its result.
    with _RUST_CHUNK_PARITY_GATE_LOCK:
        current_gate = _RUST_CHUNK_PARITY_GATES.get(gate_key)
    if current_gate is not None and current_gate[0] == "fallback":
        return (
            _python_structure_materials(
                text,
                target_chars=target,
                hard_chars=hard,
                max_chunks=max_chunks,
            ),
            "python",
            current_gate[1],
        )
    return rust_materials, "rust", ""


class StructureAwareEmbedder(Embedder):
    """Provider-neutral structured chunking wrapper.

    Ollama historically owned the chunking implementation itself.  Keeping the
    wrapper at the provider boundary makes ``full`` mode behave the same for
    Ollama, OpenAI, local sentence-transformers, and the zero-vector fallback.
    """

    def __init__(self, delegate: Embedder) -> None:
        self._delegate = delegate
        self._settings = _structure_chunking_settings()
        self._last_chunking_diagnostics: dict[str, object] = {}

    def embed(self, text: str) -> list[float]:
        # Semantic enrichment plans are already provider-specific embedding
        # material; do not parse them as Markdown a second time.
        if is_embedding_plan(text):
            result = self._delegate.embed(text)
            self._last_chunking_diagnostics = dict(
                getattr(self._delegate, "last_chunking_diagnostics", {}) or {}
            )
            return result
        if len(text or "") > int(self._settings["max_source_chars"]):
            self._last_chunking_diagnostics = {
                "mode": "structure-v1",
                "source_chars": len(text or ""),
                "resource_limited": True,
                "error": "structure_chunking_source_too_large",
            }
            raise ValueError("structure_chunking_source_too_large")

        materials, effective_engine, fallback_reason = _effective_structure_materials(
            text or "", self._settings
        )
        source = text or ""
        last_source_end = max((material.source_end for material in materials), default=0)
        coverage_gap = has_uncovered_content(source, materials)
        self._last_chunking_diagnostics = {
            "mode": "structure-v1",
            "requested_engine": str(self._settings["engine"]),
            "effective_engine": effective_engine,
            "engine_fallback_reason": fallback_reason,
            "source_chars": len(source),
            "chunk_count": len(materials),
            "covered_source_chars": sum(
                max(material.source_end - material.source_start, 0) for material in materials
            ),
            "last_source_end": last_source_end,
            "budget_unit": "characters-fallback",
            "truncated": last_source_end < len(source.rstrip())
            or coverage_gap
            or any(material.context_truncated for material in materials),
            "max_chunks": int(self._settings["max_chunks"]),
            "resource_limited": coverage_gap,
            "context_truncated": any(material.context_truncated for material in materials),
        }

        # Ollama exposes a low-level request method; using it avoids re-entering
        # its legacy chunking branch. Other providers use their normal embed().
        low_level = getattr(self._delegate, "_embed_chunk", None)
        embed_one = low_level if callable(low_level) else self._delegate.embed
        vectors = [embed_one(material.text) for material in materials]
        return vectors[0] if len(vectors) == 1 else _mean_pool_vectors(vectors)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    def prepare_index_text(self, text: str) -> str:
        prepare = getattr(self._delegate, "prepare_index_text", None)
        return prepare(text) if callable(prepare) else text

    @property
    def dim(self) -> int:
        return self._delegate.dim

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def index_model_name(self) -> str:
        base = str(self._delegate.index_model_name)
        if "|chunking=structure-v1" in base:
            return base
        return (
            f"{base}|chunking=structure-v1"
            f"|target_chars={self._settings['target_chars']}"
            f"|hard_chars={self._settings['hard_chars']}"
            f"|max_chunks={self._settings['max_chunks']}"
            f"|max_source_chars={self._settings['max_source_chars']}"
            "|budget=characters-fallback"
        )

    @property
    def last_chunking_diagnostics(self) -> dict[str, object]:
        return dict(self._last_chunking_diagnostics)

    @property
    def last_index_preparation_diagnostics(self) -> dict[str, object]:
        diagnostics = getattr(self._delegate, "last_index_preparation_diagnostics", {})
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

    def close(self) -> None:
        self._delegate.close()


class OpenAIEmbedder(Embedder):
    """OpenAI embedding fallback.

    Default model: text-embedding-3-small (1536 dim).
    Requires: pip install openai and OPENAI_API_KEY set.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._model = model or os.getenv("EMBEDDER_MODEL", "text-embedding-3-small")

    def embed(self, text: str) -> list[float]:
        from openai import OpenAI

        client = OpenAI(api_key=self._key)
        resp = client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self._key)
        resp = client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in resp.data]

    @property
    def dim(self) -> int:
        return 1536

    @property
    def model_name(self) -> str:
        return self._model


class LocalSentenceEmbedder(Embedder):
    """Local sentence-transformers embedder — in-process, zero HTTP.

    Runs the embedding model directly in the Python process via
    sentence-transformers (ONNX-optimized).  No external service needed,
    no network round-trips, no API keys.

    Default model: BAAI/bge-large-zh-v1.5 (1024 dim, Chinese+English).
    Set EMBEDDER_LOCAL_MODEL to override.

    First invocation downloads the model from HuggingFace (~1.3 GB),
    subsequent calls hit the disk cache and return in <5 ms.
    """

    _DEFAULT_MODEL = "BAAI/bge-large-zh-v1.5"
    _DIM = 1024

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or os.getenv("EMBEDDER_LOCAL_MODEL", self._DEFAULT_MODEL)
        self._model = None  # lazy-init

    def _lazy_load(self):
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        # Use HF mirror in China if set.
        self._model = SentenceTransformer(
            self._model_name,
            trust_remote_code=True,
            local_files_only=False,
        )

    def embed(self, text: str) -> list[float]:
        self._lazy_load()
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._lazy_load()
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return vecs.tolist()

    @property
    def dim(self) -> int:
        return self._DIM

    @property
    def model_name(self) -> str:
        return self._model_name


class FallbackEmbedder(Embedder):
    """Local zero-vector fallback when no embedding service is available.

    Returns a zero vector of configurable dimension. Downstream systems
    (ContextEngine._text_retrieval) use pure text matching (CJK bigrams /
    word split) which does not depend on vector similarity, so retrieval
    still works — just without semantic ranking.
    """

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim
        self._model = "fallback-zero"

    def embed(self, text: str) -> list[float]:
        return [0.0] * self._dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model


# Singleton embedder instance — shared across all callers to enable cache reuse
_embedder_singleton: Embedder | None = None
_embedder_lock = threading.Lock()


def reset_embedder() -> Embedder | None:
    """Clear the embedder singleton so the next call to get_embedder() re-probes.

    Use when: Ollama becomes available after a FallbackEmbedder lock-in,
    or after deploying a new embedding model.
    """
    global _embedder_singleton
    with _embedder_lock:
        previous = _embedder_singleton
        _embedder_singleton = None
    if previous is not None:
        previous.close()
    logging.getLogger("plastic-promise.embedder").info(
        "Embedder singleton reset — will re-probe on next get_embedder()"
    )
    return previous


def get_embedder(fallback_on_error: bool = True) -> Embedder:
    """Factory: returns embedder based on EMBEDDER_PROVIDER env var.

    Provider chain (auto-detected):
      1. ollama — local HTTP server (default, lightweight, 0.7GB mxbai-embed-large)
      2. local  — sentence-transformers, in-process (fallback if ollama fails)
      3. openai — cloud API
      4. fallback — zero vectors, text-only retrieval

    When fallback_on_error=True and all providers are unreachable,
    returns a FallbackEmbedder (zero vectors) so retrieval degrades to
    pure text matching instead of crashing.

    All embedders are wrapped in CachedEmbedder for performance (unless
    EMBEDDER_CACHE_SIZE=0). The embedder is a singleton shared across all
    callers, enabling cross-request embedding cache reuse.
    """
    global _embedder_singleton
    if _embedder_singleton is not None:
        return _embedder_singleton

    with _embedder_lock:
        if _embedder_singleton is not None:
            return _embedder_singleton

        provider = os.getenv("EMBEDDER_PROVIDER", "ollama").lower()
        delegate: Embedder | None = None

        if provider == "openai":
            try:
                delegate = OpenAIEmbedder()
            except Exception:
                if not fallback_on_error:
                    raise
        elif provider == "ollama":
            try:
                delegate = OllamaEmbedder()
            except Exception:
                if not fallback_on_error:
                    raise
        elif provider == "fallback":
            delegate = FallbackEmbedder(dim=1024)
        else:
            # "local" (default): try Ollama first (lightweight, 0.7GB mxbai-embed-large),
            # fall back to sentence-transformers BAAI/bge-large-zh-v1.5 (3.7GB) if Ollama unavailable
            try:
                delegate = OllamaEmbedder()
            except Exception as e:
                logging.info("OllamaEmbedder unavailable (%s), trying local model...", e)
                try:
                    delegate = LocalSentenceEmbedder()
                except Exception:
                    if not fallback_on_error:
                        raise

        if delegate is None:
            _embedder_singleton = FallbackEmbedder(dim=1024)
            delegate = _embedder_singleton

        if os.environ.get("PP_MEMORY_CHUNKING", "off").strip().casefold() == "structure-v1":
            delegate = StructureAwareEmbedder(delegate)

        # Wrap in cache unless explicitly disabled
        cache_size = int(os.environ.get("EMBEDDER_CACHE_SIZE", "256"))
        _embedder_singleton = CachedEmbedder(delegate) if cache_size > 0 else delegate
        return _embedder_singleton
