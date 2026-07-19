"""Fail-closed local semantic enrichment for canonical structure-aware chunks.

The generated metadata is derived index material only. It never changes canonical source
content or source spans. Active enrichment is serialized into an embedding plan so the exact
chunk inputs used to create a vector can be replayed and hashed from SQLite.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from plastic_promise.core.paths import get_db_path

if TYPE_CHECKING:
    from plastic_promise.core.chunking import ChunkMaterial


DEFAULT_MODEL = "qwen3:8b"
DEFAULT_PROMPT_VERSION = "semantic-chunk-enrichment-v1"
SCHEMA_VERSION = "semantic-chunk-enrichment-schema-v1"
ENRICHMENT_IDENTITY = "semantic-v1"
EMBEDDING_PLAN_VERSION = "embedding-plan-v1"
EMBEDDING_PLAN_PREFIX = "plastic-promise-embedding-plan-v1:"
_CACHE_TABLE = "semantic_chunk_enrichment_cache_v2"

SYSTEM_PROMPT = (
    "Extract compact retrieval metadata from the exact source fragment. Do not infer facts. "
    "The summary must be copied verbatim as one contiguous source span. Every keyword, entity, "
    "and evidence span must be copied verbatim from source_fragment. Copy required_identifiers "
    "exactly and in the same order. Return only the requested JSON object."
)

_EXPECTED_FIELDS = {"summary", "keywords", "entities", "identifiers", "evidence"}
_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:\d+(?:\.\d+)*|[A-Za-z][A-Za-z0-9]*"
    r"(?:[_:/.-][A-Za-z0-9]+)+)(?![A-Za-z0-9_])"
)
_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "keywords", "entities", "identifiers", "evidence"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 360},
        "keywords": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 96},
            "maxItems": 12,
            "uniqueItems": True,
        },
        "entities": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
            "maxItems": 12,
            "uniqueItems": True,
        },
        "identifiers": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
            "maxItems": 32,
            "uniqueItems": True,
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 240},
            "minItems": 1,
            "maxItems": 5,
            "uniqueItems": True,
        },
    },
}
_SCHEMA_HASH = hashlib.sha256(
    json.dumps(_OUTPUT_SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
).hexdigest()


@dataclass(frozen=True)
class SemanticChunkMetadata:
    """Validated metadata returned by the local analysis model."""

    summary: str
    keywords: tuple[str, ...]
    entities: tuple[str, ...]
    identifiers: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class EnrichmentBatch:
    """Prepared per-chunk embedding inputs and operational diagnostics."""

    embedding_texts: tuple[str, ...]
    diagnostics: dict[str, object]
    chunk_records: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class _EnrichmentAttempt:
    metadata: SemanticChunkMetadata | None = None
    cache_hit: bool = False
    error: str | None = None
    cache_key: str = ""


@dataclass(frozen=True)
class _QueuedChunk:
    cache_key: str
    material: ChunkMaterial
    source_fragment: str


class SemanticChunkEnricher:
    """Add grounded semantic metadata after deterministic chunking.

    ``shadow`` performs bounded daemon work and never changes embedding input. ``on`` is
    synchronous for document/index preparation; query embedding never calls this class.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        model: str | None = None,
        model_digest: str | None = None,
        mode: str | None = None,
        cache_path: str | Path | None = None,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
    ) -> None:
        self._host = _normalize_ollama_host(host or os.getenv("OLLAMA_HOST"))
        self._model = model or os.getenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL", DEFAULT_MODEL)
        self._mode = (mode or os.getenv("PP_MEMORY_CHUNK_ENRICHMENT", "off")).strip().lower()
        if self._mode not in {"off", "shadow", "on"}:
            logging.warning("Unknown PP_MEMORY_CHUNK_ENRICHMENT=%r; using off", self._mode)
            self._mode = "off"
        self._prompt_version = prompt_version
        self._prompt_hash = _prompt_hash(prompt_version)
        self._schema_hash = _SCHEMA_HASH
        env_model_digest = (os.getenv("PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST") or "").strip()
        self._pinned_model_digest = _normalize_model_digest(env_model_digest)
        self._pinned_model_digest_invalid = bool(env_model_digest and not self._pinned_model_digest)
        explicit_model_digest = (model_digest or "").strip()
        self._model_digest = _normalize_model_digest(explicit_model_digest)
        if explicit_model_digest and not self._model_digest:
            raise ValueError("semantic_enrichment_model_digest_invalid")
        self._timeout = _float_env("PP_MEMORY_CHUNK_ENRICHMENT_TIMEOUT", 45.0, minimum=0.1)
        self._num_predict = _int_env("PP_MEMORY_CHUNK_ENRICHMENT_NUM_PREDICT", 768, minimum=128)
        self._max_output_chars = _int_env(
            "PP_MEMORY_CHUNK_ENRICHMENT_MAX_OUTPUT_CHARS", 8192, minimum=512
        )
        self._cache_path = Path(cache_path) if cache_path else _default_cache_path()
        self._cache_ready = False
        self._cache_lock = threading.Lock()
        queue_size = _int_env("PP_MEMORY_CHUNK_ENRICHMENT_QUEUE_SIZE", 32, minimum=1)
        self._queue: queue.Queue[_QueuedChunk] = queue.Queue(maxsize=queue_size)
        self._pending_keys: set[str] = set()
        self._pending_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._worker_stop = threading.Event()
        self._worker_idle_timeout = _float_env(
            "PP_MEMORY_CHUNK_ENRICHMENT_WORKER_IDLE_TIMEOUT", 30.0, minimum=0.1
        )
        self._closed = False
        self._owner_id = uuid.uuid4().hex

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def model(self) -> str:
        return self._model

    @property
    def model_digest(self) -> str:
        if self._model_digest:
            return self._model_digest
        if self._pinned_model_digest_invalid:
            return ""
        resolved = self._resolve_model_digest()
        if not resolved:
            return ""
        if self._pinned_model_digest and resolved != self._pinned_model_digest:
            logging.error(
                "Configured semantic enrichment digest does not match Ollama: %s != %s",
                self._pinned_model_digest,
                resolved,
            )
            return ""
        self._model_digest = resolved
        return self._model_digest

    @property
    def model_identity(self) -> str:
        digest = self.model_digest
        if not digest:
            raise RuntimeError("semantic_enrichment_model_digest_unavailable")
        return f"{self._model}@{digest}"

    @property
    def index_identity(self) -> str:
        """Identity bound into derived indexes only when enrichment is on."""

        digest = self.model_digest
        if not digest:
            raise RuntimeError("semantic_enrichment_model_digest_unavailable")
        return (
            f"enrichment={ENRICHMENT_IDENTITY}"
            f"|enrichment_model={self._model}@{digest}"
            f"|enrichment_prompt_hash={self._prompt_hash}"
            f"|enrichment_schema_hash={self._schema_hash}"
        )

    @property
    def prompt_hash(self) -> str:
        return self._prompt_hash

    @property
    def schema_hash(self) -> str:
        return self._schema_hash

    def prepare_chunks(
        self, materials: list[ChunkMaterial], *, source_text: str
    ) -> EnrichmentBatch:
        """Return derived embedding inputs without mutating canonical materials."""

        original = tuple(material.text for material in materials)
        diagnostics: dict[str, object] = {
            "mode": self._mode,
            "model": self._model,
            "model_digest": self._model_digest or "unresolved",
            "prompt_hash": self._prompt_hash,
            "schema_hash": self._schema_hash,
            "chunk_count": len(materials),
            "enriched": 0,
            "fallbacks": 0,
            "cache_hits": 0,
            "queued": 0,
            "queue_dropped": 0,
            "errors": {},
        }
        if self._mode == "off" or not materials:
            return EnrichmentBatch(original, diagnostics)

        resolved_digest = self.model_digest
        diagnostics["model_digest"] = resolved_digest or "unresolved"
        if not resolved_digest:
            if self._mode == "on":
                raise RuntimeError("semantic_enrichment_model_digest_unavailable")
            _increment_error(diagnostics, "model_digest_unavailable")
            diagnostics["queue_dropped"] = len(materials)
            return EnrichmentBatch(original, diagnostics)

        prepared: list[str] = []
        records: list[dict[str, object]] = []
        for material in materials:
            source_fragment = _source_fragment(source_text, material)
            cache_key = self._cache_key(material, source_fragment)
            if not source_fragment.strip() or not material.text.strip():
                prepared.append(material.text)
                records.append(_chunk_record(material, material.text, "fallback", cache_key))
                continue
            if self._mode == "shadow":
                cached = self._cache_get(cache_key, source_fragment)
                if cached is not None:
                    diagnostics["cache_hits"] = int(diagnostics["cache_hits"]) + 1
                elif self._enqueue(cache_key, material, source_fragment):
                    diagnostics["queued"] = int(diagnostics["queued"]) + 1
                else:
                    diagnostics["queue_dropped"] = int(diagnostics["queue_dropped"]) + 1
                prepared.append(material.text)
                continue

            attempt = self._load_or_request(cache_key, material, source_fragment)
            if attempt.cache_hit:
                diagnostics["cache_hits"] = int(diagnostics["cache_hits"]) + 1
            if attempt.metadata is None:
                diagnostics["fallbacks"] = int(diagnostics["fallbacks"]) + 1
                _increment_error(diagnostics, attempt.error or "unknown_error")
                prepared.append(material.text)
                records.append(
                    _chunk_record(material, material.text, "fallback", cache_key, attempt.error)
                )
                continue
            derived = _derived_embedding_text(material.text, attempt.metadata)
            diagnostics["enriched"] = int(diagnostics["enriched"]) + 1
            prepared.append(derived)
            records.append(_chunk_record(material, derived, "enriched", cache_key))

        return EnrichmentBatch(tuple(prepared), diagnostics, tuple(records))

    def build_embedding_plan(
        self,
        source_text: str,
        materials: list[ChunkMaterial],
        batch: EnrichmentBatch,
    ) -> str:
        """Serialize the exact document chunk inputs used by active enrichment."""

        if self._mode != "on":
            return source_text
        payload = {
            "version": EMBEDDING_PLAN_VERSION,
            "source_text_hash": _sha256_text(source_text),
            "model_identity": self.model_identity,
            "prompt_hash": self._prompt_hash,
            "schema_hash": self._schema_hash,
            "chunks": list(batch.chunk_records),
        }
        if len(payload["chunks"]) != len(materials):
            raise RuntimeError("semantic_enrichment_plan_incomplete")
        return EMBEDDING_PLAN_PREFIX + json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait for shadow work in tests and maintenance jobs."""

        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() <= deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def close(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        """Stop the shadow worker after draining queued items when requested."""

        self._closed = True
        self._worker_stop.set()
        worker = self._worker
        if wait and worker is not None and worker.is_alive():
            worker.join(timeout=max(timeout, 0.0))

    def _enqueue(self, cache_key: str, material: ChunkMaterial, source_fragment: str) -> bool:
        if self._closed:
            return False
        with self._pending_lock:
            if cache_key in self._pending_keys:
                return True
            self._pending_keys.add(cache_key)
        self._ensure_worker()
        try:
            self._queue.put_nowait(_QueuedChunk(cache_key, material, source_fragment))
        except queue.Full:
            with self._pending_lock:
                self._pending_keys.discard(cache_key)
            return False
        return True

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        with self._pending_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker_stop.clear()
            self._worker = threading.Thread(
                target=self._shadow_worker,
                name="semantic-chunk-enrichment",
                daemon=True,
            )
            self._worker.start()

    def _shadow_worker(self) -> None:
        while True:
            if self._worker_stop.is_set() and self._queue.empty():
                return
            try:
                item = self._queue.get(timeout=self._worker_idle_timeout)
            except queue.Empty:
                if self._queue.empty():
                    return
                continue
            try:
                self._load_or_request(item.cache_key, item.material, item.source_fragment)
            except Exception:
                logging.debug("Semantic chunk shadow enrichment failed", exc_info=True)
            finally:
                with self._pending_lock:
                    self._pending_keys.discard(item.cache_key)
                self._queue.task_done()

    def _load_or_request(
        self, cache_key: str, material: ChunkMaterial, source_fragment: str
    ) -> _EnrichmentAttempt:
        cached = self._cache_get(cache_key, source_fragment)
        if cached is not None:
            return _EnrichmentAttempt(metadata=cached, cache_hit=True, cache_key=cache_key)
        claim, claimed = self._cache_claim(cache_key, source_fragment)
        if claim == "hit":
            return _EnrichmentAttempt(metadata=claimed, cache_hit=True, cache_key=cache_key)
        if claim != "claimed":
            return _EnrichmentAttempt(error="cache_busy", cache_key=cache_key)
        attempt = self._request_metadata(material, source_fragment)
        if attempt.metadata is not None:
            self._cache_complete(cache_key, attempt.metadata)
        else:
            self._cache_release(cache_key)
        return _EnrichmentAttempt(
            metadata=attempt.metadata,
            error=attempt.error,
            cache_key=cache_key,
        )

    def _request_metadata(
        self, material: ChunkMaterial, source_fragment: str
    ) -> _EnrichmentAttempt:
        identifiers = extract_identifiers(source_fragment)
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "heading_path": list(material.heading_path),
                            "kind": material.kind,
                            "source_fragment": source_fragment,
                            "required_identifiers": list(identifiers),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "stream": False,
            "think": False,
            "format": _OUTPUT_SCHEMA,
            "options": {"temperature": 0, "num_predict": self._num_predict},
        }
        try:
            response = requests.post(f"{self._host}/api/chat", json=payload, timeout=self._timeout)
            response.raise_for_status()
            response_payload = response.json()
            content = response_payload["message"]["content"]
        except Exception:
            logging.debug("Local semantic chunk request failed", exc_info=True)
            return _EnrichmentAttempt(error="request_failed")
        if not isinstance(content, str) or len(content) > self._max_output_chars:
            return _EnrichmentAttempt(error="invalid_output_length")
        try:
            raw_metadata = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return _EnrichmentAttempt(error="invalid_json")
        metadata, error = validate_metadata(raw_metadata, source_fragment)
        return _EnrichmentAttempt(metadata=metadata, error=error)

    def _cache_key(self, material: ChunkMaterial, source_fragment: str) -> str:
        identity = {
            "source": source_fragment,
            "heading_path": list(material.heading_path),
            "kind": material.kind,
            "model": self._model,
            "model_digest": self.model_digest or "unresolved",
            "prompt_hash": self._prompt_hash,
            "schema_hash": self._schema_hash,
        }
        return _sha256_text(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def _resolve_model_digest(self) -> str:
        try:
            response = requests.get(f"{self._host}/api/tags", timeout=min(self._timeout, 5.0))
            response.raise_for_status()
            models = response.json().get("models", [])
            for item in models if isinstance(models, list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("model") or "").strip()
                digest = _normalize_model_digest(item.get("digest"))
                if digest and name.casefold() == self._model.casefold():
                    return digest
        except Exception:
            logging.debug("Could not resolve Ollama model digest", exc_info=True)
        return ""

    def _ensure_cache(self) -> None:
        if self._cache_ready:
            return
        with self._cache_lock:
            if self._cache_ready:
                return
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._cache_path, timeout=10.0)) as connection, connection:
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_CACHE_TABLE} (
                        cache_key TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        metadata_json TEXT,
                        model TEXT NOT NULL,
                        model_digest TEXT NOT NULL,
                        prompt_hash TEXT NOT NULL,
                        schema_hash TEXT NOT NULL,
                        owner_id TEXT,
                        lease_expires REAL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
            self._cache_ready = True

    def _cache_get(self, cache_key: str, source_fragment: str) -> SemanticChunkMetadata | None:
        try:
            self._ensure_cache()
            with closing(sqlite3.connect(self._cache_path, timeout=10.0)) as connection, connection:
                row = connection.execute(
                    f"SELECT status, metadata_json FROM {_CACHE_TABLE} WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
        except sqlite3.Error:
            logging.debug("Semantic chunk cache read failed", exc_info=True)
            return None
        if row is None or row[0] != "ready" or not row[1]:
            return None
        try:
            raw_metadata = json.loads(row[1])
        except (TypeError, json.JSONDecodeError):
            return None
        metadata, _ = validate_metadata(raw_metadata, source_fragment)
        return metadata

    def _cache_claim(
        self, cache_key: str, source_fragment: str
    ) -> tuple[str, SemanticChunkMetadata | None]:
        deadline = time.monotonic() + self._timeout + 5.0
        while time.monotonic() < deadline:
            now = time.time()
            try:
                self._ensure_cache()
                with (
                    closing(sqlite3.connect(self._cache_path, timeout=10.0)) as connection,
                    connection,
                ):
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        f"SELECT status, metadata_json, owner_id, lease_expires FROM {_CACHE_TABLE} "
                        "WHERE cache_key = ?",
                        (cache_key,),
                    ).fetchone()
                    if row and row[0] == "ready" and row[1]:
                        try:
                            metadata, _ = validate_metadata(json.loads(row[1]), source_fragment)
                        except (TypeError, json.JSONDecodeError):
                            metadata = None
                        if metadata is not None:
                            connection.commit()
                            return "hit", metadata
                    if row and row[0] == "inflight" and float(row[3] or 0) > now:
                        connection.commit()
                    else:
                        connection.execute(
                            f"""
                            INSERT INTO {_CACHE_TABLE} (
                                cache_key, status, metadata_json, model, model_digest,
                                prompt_hash, schema_hash, owner_id, lease_expires, updated_at
                            ) VALUES (?, 'inflight', NULL, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(cache_key) DO UPDATE SET
                                status = 'inflight', metadata_json = NULL, model = excluded.model,
                                model_digest = excluded.model_digest, prompt_hash = excluded.prompt_hash,
                                schema_hash = excluded.schema_hash, owner_id = excluded.owner_id,
                                lease_expires = excluded.lease_expires, updated_at = excluded.updated_at
                            """,
                            (
                                cache_key,
                                self._model,
                                self._model_digest or "unresolved",
                                self._prompt_hash,
                                self._schema_hash,
                                self._owner_id,
                                now + self._timeout + 10.0,
                                now,
                            ),
                        )
                        connection.commit()
                        return "claimed", None
            except sqlite3.Error:
                logging.debug("Semantic chunk cache claim failed", exc_info=True)
                return "cache_error", None
            time.sleep(0.05)
        return "cache_busy", None

    def _cache_complete(self, cache_key: str, metadata: SemanticChunkMetadata) -> None:
        try:
            self._ensure_cache()
            encoded = json.dumps(
                asdict(metadata), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            with closing(sqlite3.connect(self._cache_path, timeout=10.0)) as connection, connection:
                connection.execute(
                    f"UPDATE {_CACHE_TABLE} SET status = 'ready', metadata_json = ?, "
                    "owner_id = NULL, lease_expires = NULL, updated_at = ? "
                    "WHERE cache_key = ? AND status = 'inflight' AND owner_id = ?",
                    (encoded, time.time(), cache_key, self._owner_id),
                )
        except sqlite3.Error:
            logging.debug("Semantic chunk cache write failed", exc_info=True)

    def _cache_release(self, cache_key: str) -> None:
        try:
            self._ensure_cache()
            with closing(sqlite3.connect(self._cache_path, timeout=10.0)) as connection, connection:
                connection.execute(
                    f"DELETE FROM {_CACHE_TABLE} WHERE cache_key = ? AND owner_id = ?",
                    (cache_key, self._owner_id),
                )
        except sqlite3.Error:
            logging.debug("Semantic chunk cache release failed", exc_info=True)


def validate_metadata(
    value: object, source_fragment: str
) -> tuple[SemanticChunkMetadata | None, str | None]:
    """Apply strict schema and grounding checks independently of model guarantees."""

    if not isinstance(value, dict) or set(value) != _EXPECTED_FIELDS:
        return None, "schema_fields"
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > 360:
        return None, "invalid_summary"
    summary = summary.strip()
    if summary not in source_fragment:
        return None, "ungrounded_summary"
    normalized: dict[str, tuple[str, ...]] = {}
    limits = {
        "keywords": (12, 96),
        "entities": (12, 128),
        "identifiers": (32, 128),
        "evidence": (5, 240),
    }
    for field, (max_items, max_chars) in limits.items():
        raw_items = value.get(field)
        if not isinstance(raw_items, list) or len(raw_items) > max_items:
            return None, f"invalid_{field}"
        items: list[str] = []
        for item in raw_items:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > max_chars:
                return None, f"invalid_{field}"
            items.append(item.strip())
        if len(set(items)) != len(items):
            return None, f"invalid_{field}"
        normalized[field] = tuple(items)
    if not normalized["evidence"]:
        return None, "invalid_evidence"
    if any(item not in source_fragment for item in normalized["evidence"]):
        return None, "ungrounded_evidence"
    if summary not in normalized["evidence"]:
        return None, "summary_not_in_evidence"
    if any(item not in source_fragment for item in normalized["keywords"]):
        return None, "ungrounded_keywords"
    if any(item not in source_fragment for item in normalized["entities"]):
        return None, "ungrounded_entities"
    expected_identifiers = extract_identifiers(source_fragment)
    if normalized["identifiers"] != expected_identifiers:
        return None, "identifier_mismatch"
    if any(item not in expected_identifiers for item in extract_identifiers(summary)):
        return None, "unsupported_summary_identifier"
    return (
        SemanticChunkMetadata(
            summary=summary,
            keywords=normalized["keywords"],
            entities=normalized["entities"],
            identifiers=normalized["identifiers"],
            evidence=normalized["evidence"],
        ),
        None,
    )


def extract_identifiers(text: str) -> tuple[str, ...]:
    """Return high-signal numbers and code-like identifiers in source order."""

    found: list[str] = []
    seen: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(text or ""):
        value = match.group(0)
        if value not in seen:
            seen.add(value)
            found.append(value)
    return tuple(found)


def is_embedding_plan(text: object) -> bool:
    return isinstance(text, str) and text.startswith(EMBEDDING_PLAN_PREFIX)


def decode_embedding_plan(text: str) -> dict[str, object]:
    """Decode and validate an exact persisted plan before direct embedding."""

    if not is_embedding_plan(text):
        raise ValueError("embedding_plan_prefix_missing")
    try:
        payload = json.loads(text[len(EMBEDDING_PLAN_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise ValueError("embedding_plan_json_invalid") from exc
    required = {
        "version",
        "source_text_hash",
        "model_identity",
        "prompt_hash",
        "schema_hash",
        "chunks",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("version") != EMBEDDING_PLAN_VERSION
    ):
        raise ValueError("embedding_plan_version_invalid")
    for field in ("source_text_hash", "prompt_hash", "schema_hash"):
        if not _is_sha256(payload.get(field)):
            raise ValueError("embedding_plan_hash_invalid")
    if not isinstance(payload.get("model_identity"), str) or not payload["model_identity"]:
        raise ValueError("embedding_plan_model_invalid")
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks or len(chunks) > 1024:
        raise ValueError("embedding_plan_chunks_invalid")
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError("embedding_plan_chunk_invalid")
        expected_fields = {
            "embedding_text",
            "source_text",
            "source_start",
            "source_end",
            "heading_path",
            "kind",
            "status",
            "cache_key",
        }
        if "error" in chunk:
            expected_fields.add("error")
        if set(chunk) != expected_fields:
            raise ValueError("embedding_plan_chunk_invalid")
        embedding_text = chunk.get("embedding_text")
        source_text = chunk.get("source_text")
        if (
            not isinstance(embedding_text, str)
            or not embedding_text
            or not isinstance(source_text, str)
            or not source_text
        ):
            raise ValueError("embedding_plan_chunk_invalid")
        if chunk.get("status") not in {"enriched", "fallback"}:
            raise ValueError("embedding_plan_status_invalid")
        if chunk["status"] == "fallback" and embedding_text != source_text:
            raise ValueError("embedding_plan_fallback_invalid")
        if chunk["status"] == "enriched" and not embedding_text.endswith(
            f"[Source]\n{source_text}"
        ):
            raise ValueError("embedding_plan_enriched_invalid")
        if not _is_sha256(chunk.get("cache_key")):
            raise ValueError("embedding_plan_cache_key_invalid")
        start = chunk.get("source_start")
        end = chunk.get("source_end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            raise ValueError("embedding_plan_span_invalid")
        heading_path = chunk.get("heading_path")
        if not isinstance(heading_path, list) or not all(
            isinstance(item, str) for item in heading_path
        ):
            raise ValueError("embedding_plan_heading_invalid")
        if not isinstance(chunk.get("kind"), str) or not chunk["kind"]:
            raise ValueError("embedding_plan_kind_invalid")
        if "error" in chunk and not isinstance(chunk["error"], str):
            raise ValueError("embedding_plan_error_invalid")
    return payload


def embedding_plan_metadata(text: str) -> dict[str, object] | None:
    if not is_embedding_plan(text):
        return None
    payload = decode_embedding_plan(text)
    chunks = payload["chunks"]
    assert isinstance(chunks, list)
    statuses = [str(chunk.get("status")) for chunk in chunks if isinstance(chunk, dict)]
    return {
        "version": payload["version"],
        "model_identity": payload.get("model_identity", ""),
        "prompt_hash": payload.get("prompt_hash", ""),
        "schema_hash": payload.get("schema_hash", ""),
        "source_text_hash": payload.get("source_text_hash", ""),
        "chunk_count": len(chunks),
        "enriched_count": statuses.count("enriched"),
        "fallback_count": statuses.count("fallback"),
        "cache_keys": [str(chunk.get("cache_key")) for chunk in chunks if isinstance(chunk, dict)],
    }


def _derived_embedding_text(source_text: str, metadata: SemanticChunkMetadata) -> str:
    lines = ["[Semantic context]", f"Summary: {metadata.summary}"]
    if metadata.keywords:
        lines.append(f"Keywords: {', '.join(metadata.keywords)}")
    if metadata.entities:
        lines.append(f"Entities: {', '.join(metadata.entities)}")
    if metadata.identifiers:
        lines.append(f"Identifiers: {', '.join(metadata.identifiers)}")
    return "\n".join([*lines, "[Source]", source_text])


def _chunk_record(
    material: ChunkMaterial,
    embedding_text: str,
    status: str,
    cache_key: str,
    error: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "embedding_text": embedding_text,
        "source_text": material.text,
        "source_start": material.source_start,
        "source_end": material.source_end,
        "heading_path": list(material.heading_path),
        "kind": material.kind,
        "status": status,
        "cache_key": cache_key,
    }
    if error:
        record["error"] = error
    return record


def _source_fragment(source_text: str, material: ChunkMaterial) -> str:
    start = min(max(int(material.source_start), 0), len(source_text))
    end = min(max(int(material.source_end), start), len(source_text))
    return source_text[start:end]


def _default_cache_path() -> Path:
    configured = os.getenv("PP_MEMORY_CHUNK_ENRICHMENT_CACHE_PATH")
    if configured:
        return Path(configured)
    return Path(get_db_path()).with_name("semantic_chunk_enrichment.db")


def _normalize_ollama_host(raw: str | None) -> str:
    host = (raw or "http://localhost:11434").replace("0.0.0.0", "127.0.0.1")
    if "://" not in host:
        host = f"http://{host}"
    if host.rsplit(":", 1)[-1].isdigit() or host.endswith("/"):
        return host.rstrip("/")
    return f"{host.rstrip('/')}:11434"


def _prompt_hash(prompt_version: str) -> str:
    return hashlib.sha256(f"{prompt_version}\n{SYSTEM_PROMPT}\n{_SCHEMA_HASH}".encode()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _normalize_model_digest(value: object) -> str:
    digest = str(value or "").strip().casefold()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return ""
    return f"sha256:{digest}"


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


def _float_env(name: str, default: float, minimum: float) -> float:
    try:
        return max(float(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


def _increment_error(diagnostics: dict[str, object], error: str) -> None:
    errors = diagnostics["errors"]
    if not isinstance(errors, dict):
        errors = {}
        diagnostics["errors"] = errors
    errors[error] = int(errors.get(error, 0)) + 1
