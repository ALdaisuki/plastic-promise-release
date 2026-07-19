from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

LEGACY_POLICY = "legacy"
LEGACY_FALLBACK_POLICY = "legacy-fallback"
SUMMARY_POLICY = "summary-v1"
COMPACT_V2_POLICY = "compact-v2"
INDEX_HASH_SCHEMA = "policy-model-text-v2"
COMPACT_V2_MAX_CHARS = 1200
COMPACT_V2_MAX_LINE_CHARS = 400

_PERSISTED_POLICIES = frozenset(
    {LEGACY_POLICY, LEGACY_FALLBACK_POLICY, SUMMARY_POLICY, COMPACT_V2_POLICY}
)


class IndexMaterialError(ValueError):
    """Stable fail-closed diagnostic for invalid persisted index material."""


def effective_embedding_model_name(embedder: object | None = None) -> str:
    """Return the model identity that owns newly generated index material."""
    if embedder is not None:
        value = (
            getattr(embedder, "index_model_name", None)
            or getattr(embedder, "model_name", None)
            or getattr(embedder, "model", None)
        )
        if value:
            return _model_name(value)
    legacy = os.environ.get("EMBED_MODEL")
    if legacy:
        return _model_name(legacy)
    provider = os.environ.get("EMBEDDER_PROVIDER", "ollama").strip().casefold()
    if provider == "local":
        return _model_name(os.environ.get("EMBEDDER_LOCAL_MODEL", "BAAI/bge-large-zh-v1.5"))
    if provider == "openai":
        return _model_name(os.environ.get("EMBEDDER_MODEL", "text-embedding-3-small"))
    if provider == "fallback":
        return "fallback-zero"
    return _model_name(os.environ.get("EMBEDDER_MODEL", "mxbai-embed-large"))


def embedding_model_family(model_name: object) -> str:
    """Return the base provider/model identity without chunking configuration."""

    return _model_name(model_name).split("|chunking=", 1)[0]


@dataclass(frozen=True)
class IndexMaterial:
    vector_text: str
    search_text: str
    policy: str
    embedding_hash: str
    model_name: str = "unknown"
    hash_schema: str = INDEX_HASH_SCHEMA


def initial_index_policy(*, summary_index_enabled: bool) -> str:
    configured = os.environ.get("PP_MEMORY_INDEX_TEXT_POLICY")
    if configured is None:
        return SUMMARY_POLICY if summary_index_enabled else LEGACY_POLICY
    policy = configured.strip().casefold()
    if policy not in {LEGACY_POLICY, COMPACT_V2_POLICY}:
        raise ValueError(f"unsupported_index_policy:{policy or '<empty>'}")
    return policy


def build_index_material(
    record: Mapping[str, Any],
    *,
    policy: str | None = None,
    model_name: str = "unknown",
) -> IndexMaterial:
    """Build one deterministic material contract for a new canonical row."""
    if policy is None:
        policy = initial_index_policy(summary_index_enabled=_summary_index_enabled_from_env())
    policy = str(policy or "").strip().casefold()
    if policy == SUMMARY_POLICY:
        vector_text = _summary_vector_text(record)
        search_text = (
            _text(record.get("search_text"))
            or _text(record.get("l0_abstract"))
            or _text(record.get("l1_summary"))
        )
    elif policy in {LEGACY_POLICY, LEGACY_FALLBACK_POLICY}:
        vector_text = _text(record.get("content"))
        search_text = vector_text
    elif policy == COMPACT_V2_POLICY:
        vector_text = _compact_v2_text(record)
        search_text = vector_text
    else:
        raise ValueError(f"unsupported_index_policy:{policy}")

    effective_model = _model_name(model_name)
    return IndexMaterial(
        vector_text=vector_text,
        search_text=search_text,
        policy=policy,
        embedding_hash=_embedding_hash(policy, effective_model, vector_text),
        model_name=effective_model,
    )


def prepare_index_material(
    record: Mapping[str, Any],
    *,
    embedder: object | None,
    policy: str | None = None,
    model_name: str | None = None,
) -> IndexMaterial:
    """Build and prepare exact document material before persistence and embedding."""

    effective_model = model_name or effective_embedding_model_name(embedder)
    material = build_index_material(record, policy=policy, model_name=effective_model)
    prepare = getattr(embedder, "prepare_index_text", None)
    if not callable(prepare):
        return material
    prepared_text = prepare(material.vector_text)
    if not isinstance(prepared_text, str) or not prepared_text.strip():
        raise IndexMaterialError("index_material_preparation_failed")
    if prepared_text == material.vector_text:
        return material
    return IndexMaterial(
        vector_text=prepared_text,
        search_text=material.search_text,
        policy=material.policy,
        embedding_hash=_embedding_hash(material.policy, material.model_name, prepared_text),
        model_name=material.model_name,
    )


def resolve_index_material(
    record: Mapping[str, Any],
    *,
    model_name: str = "unknown",
) -> tuple[IndexMaterial, bool]:
    """Return exact persisted material or an explicit pre-v2 migration.

    Rows carrying the v2 hash schema fail closed on any inconsistency. Older
    rows may be materialized once, with ``needs_persist=True`` so callers can
    durably record the upgraded contract before indexing.
    """
    try:
        return _require_persisted_index_material(record, model_name=model_name), False
    except IndexMaterialError:
        metadata = _metadata_dict(record.get("metadata_json"))
        index = metadata.get("memory_index")
        index = dict(index) if isinstance(index, Mapping) else {}
        hash_schema = str(index.get("hash_schema") or "").strip()
        if hash_schema == INDEX_HASH_SCHEMA:
            raise
        if hash_schema:
            raise IndexMaterialError("index_material_hash_schema_unknown") from None
        return _materialize_pre_v2_record(record, index, model_name=model_name), True


def read_persisted_index_material(
    record: Mapping[str, Any],
    *,
    model_name: str | None = None,
) -> IndexMaterial | None:
    """Read an exact row contract; partial or unknown material is not inferred."""
    try:
        return _require_persisted_index_material(record, model_name=model_name)
    except IndexMaterialError:
        return None


def _require_persisted_index_material(
    record: Mapping[str, Any],
    *,
    model_name: str | None = None,
) -> IndexMaterial:
    metadata = _metadata_dict(record.get("metadata_json"))
    index = metadata.get("memory_index")
    if not isinstance(index, Mapping):
        raise IndexMaterialError("index_material_incomplete")

    policy = index.get("policy")
    vector_text = record.get("embedding_text")
    search_text = record.get("search_text")
    embedding_hash = record.get("embedding_hash")
    if (
        not isinstance(policy, str)
        or not isinstance(vector_text, str)
        or not vector_text.strip()
        or not isinstance(search_text, str)
        or not search_text.strip()
        or not isinstance(embedding_hash, str)
        or not embedding_hash.strip()
    ):
        raise IndexMaterialError("index_material_incomplete")
    if policy not in _PERSISTED_POLICIES:
        raise IndexMaterialError("index_material_policy_unknown")

    metadata_hash = index.get("embedding_hash")
    if metadata_hash != embedding_hash:
        raise IndexMaterialError("index_material_hash_mismatch")
    if index.get("hash_schema") != INDEX_HASH_SCHEMA:
        raise IndexMaterialError("index_material_hash_schema_missing")
    persisted_model = index.get("model_name")
    if not isinstance(persisted_model, str) or not persisted_model.strip():
        raise IndexMaterialError("index_material_model_missing")
    persisted_model = _model_name(persisted_model)
    if model_name is not None and _model_name(model_name) != persisted_model:
        raise IndexMaterialError("index_material_model_mismatch")
    expected_hash = _embedding_hash(policy, persisted_model, vector_text)
    if embedding_hash != expected_hash:
        raise IndexMaterialError("index_material_hash_mismatch")
    if index.get("search_text_hash") != _search_text_hash(search_text):
        raise IndexMaterialError("index_material_search_text_mismatch")
    return IndexMaterial(
        vector_text,
        search_text,
        policy,
        embedding_hash,
        model_name=persisted_model,
    )


def index_metadata(material: IndexMaterial) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "embedding_hash": material.embedding_hash,
        "hash_schema": material.hash_schema,
        "model_name": material.model_name,
        "policy": material.policy,
        "search_text_hash": _search_text_hash(material.search_text),
    }
    from plastic_promise.core.semantic_chunk_enrichment import embedding_plan_metadata

    plan = embedding_plan_metadata(material.vector_text)
    if plan is not None:
        metadata["embedding_plan"] = plan
    return metadata


def metadata_with_index_material(
    metadata: object,
    material: IndexMaterial,
) -> dict[str, Any]:
    result = _metadata_dict(metadata)
    result["memory_index"] = index_metadata(material)
    return result


def _embedding_hash(policy: str, model_name: object, vector_text: str) -> str:
    payload = json.dumps(
        {
            "model": _model_name(model_name),
            "policy": str(policy),
            "vector_text": vector_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _search_text_hash(search_text: str) -> str:
    return hashlib.sha256(search_text.encode("utf-8")).hexdigest()


def _materialize_pre_v2_record(
    record: Mapping[str, Any],
    index: Mapping[str, Any],
    *,
    model_name: object,
) -> IndexMaterial:
    """Upgrade only recognizable pre-v2 shapes without consulting policy env."""
    vector_text = _text(record.get("embedding_text"))
    search_text = _text(record.get("search_text"))
    content = _text(record.get("content"))
    old_policy = str(index.get("policy") or "").strip().casefold()
    if old_policy and old_policy not in {
        LEGACY_POLICY,
        LEGACY_FALLBACK_POLICY,
        SUMMARY_POLICY,
    }:
        raise IndexMaterialError("index_material_policy_unverifiable")

    if index.get("summary_index_enabled") is False:
        policy = LEGACY_FALLBACK_POLICY
        vector_text = content
        search_text = content
    elif vector_text.strip():
        search_text = search_text or _legacy_summary_search_text(record) or vector_text
        if old_policy in _PERSISTED_POLICIES:
            policy = old_policy
        else:
            policy = (
                LEGACY_FALLBACK_POLICY
                if vector_text == content and search_text == content
                else SUMMARY_POLICY
            )
    elif content.strip():
        policy = LEGACY_FALLBACK_POLICY
        vector_text = content
        search_text = content
    else:
        raise IndexMaterialError("index_material_incomplete")

    if not vector_text.strip() or not search_text.strip():
        raise IndexMaterialError("index_material_incomplete")
    effective_model = _model_name(model_name)
    return IndexMaterial(
        vector_text=vector_text,
        search_text=search_text,
        policy=policy,
        embedding_hash=_embedding_hash(policy, effective_model, vector_text),
        model_name=effective_model,
    )


def _legacy_summary_search_text(record: Mapping[str, Any]) -> str:
    metadata = _metadata_dict(record.get("metadata_json"))
    return (
        _text(record.get("l0_abstract"))
        or _text(metadata.get("l0_abstract"))
        or _text(record.get("l1_summary"))
        or _text(metadata.get("l1_summary"))
    )


def _summary_vector_text(record: Mapping[str, Any]) -> str:
    vector_text = _text(record.get("embedding_text"))
    from plastic_promise.core.semantic_chunk_enrichment import is_embedding_plan

    if not is_embedding_plan(vector_text):
        return vector_text

    metadata = _metadata_dict(record.get("metadata_json"))
    l0_abstract = _text(record.get("l0_abstract")) or _text(metadata.get("l0_abstract"))
    l1_summary = _text(record.get("l1_summary")) or _text(metadata.get("l1_summary"))
    rebuilt = "\n".join(
        part
        for part in (
            f"L0: {l0_abstract}" if l0_abstract else "",
            f"L1: {l1_summary}" if l1_summary else "",
        )
        if part
    )
    return rebuilt or _text(record.get("l2_content")) or _text(record.get("content"))


def _compact_v2_text(record: Mapping[str, Any]) -> str:
    domain = _normalize_inline(record.get("domain"))
    category = _normalize_inline(record.get("category"))
    lines: list[str] = []
    if domain or category:
        lines.append(f"domain/category: {domain or '-'} / {category or '-'}")
    seen_content: set[str] = set()
    for prefix, value in (
        ("L0", record.get("l0_abstract")),
        ("L1", record.get("l1_summary")),
    ):
        for raw_line in str(value or "").splitlines():
            normalized = _normalize_inline(raw_line)
            if not normalized or normalized in seen_content:
                continue
            seen_content.add(normalized)
            lines.extend(_prefixed_chunks(prefix, normalized))

    bounded = _bounded_complete_lines(lines, COMPACT_V2_MAX_CHARS)
    if not bounded:
        raise ValueError("index_material_empty:compact-v2")
    return "\n".join(bounded)


def _prefixed_chunks(prefix: str, normalized: str) -> list[str]:
    content_limit = max(1, COMPACT_V2_MAX_LINE_CHARS - len(prefix) - 2)
    chunks: list[str] = []
    remaining = normalized
    while remaining:
        if len(remaining) <= content_limit:
            chunk = remaining
            remaining = ""
        else:
            split_at = remaining.rfind(" ", 0, content_limit + 1)
            if split_at < content_limit // 2:
                split_at = content_limit
            chunk = remaining[:split_at].rstrip()
            remaining = remaining[split_at:].lstrip()
        if chunk:
            chunks.append(f"{prefix}: {chunk}")
    return chunks


def _bounded_complete_lines(lines: list[str], limit: int) -> list[str]:
    selected: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + (1 if selected else 0)
        if length + addition > limit:
            break
        selected.append(line)
        length += addition
    return selected


def _normalize_inline(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _model_name(value: object) -> str:
    return str(value or "").strip() or "unknown"


def _summary_index_enabled_from_env() -> bool:
    value = os.environ.get("PP_MEMORY_SUMMARY_INDEX", "")
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _metadata_dict(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _text(value: object) -> str:
    return str(value or "")
