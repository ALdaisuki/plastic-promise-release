"""Deterministic structure-aware text chunking.

This module deliberately does not call an LLM.  It preserves verbatim source spans and
provides a small, dependency-free baseline that can be compared with semantic and local-model
variants later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")


@dataclass(frozen=True)
class StructuralBlock:
    """A verbatim source region with the minimum structure needed for packing."""

    kind: str
    text: str
    heading_path: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True)
class ChunkMaterial:
    """A chunk ready for embedding, plus its canonical source span."""

    text: str
    kind: str
    heading_path: tuple[str, ...]
    source_start: int
    source_end: int
    context_truncated: bool = False


def legacy_character_chunks(text: str, chunk_chars: int, max_chunks: int) -> list[str]:
    """Return the bounded character chunks used by the legacy embedder path."""

    source = text or ""
    size = max(int(chunk_chars), 1)
    limit = max(int(max_chunks), 1)
    if len(source) <= size:
        return [source]
    chunks = [source[start : start + size] for start in range(0, len(source), size)]
    chunks = [chunk for chunk in chunks[:limit] if chunk]
    return chunks or [""]


def shadow_chunking_diagnostics(
    text: str,
    *,
    target_chars: int,
    hard_chars: int,
    max_chunks: int,
    legacy_chunks: list[str] | None = None,
    max_source_chars: int | None = None,
) -> dict[str, object]:
    """Compare legacy coverage with a complete structure-aware candidate plan."""

    source = text or ""
    legacy = legacy_chunks or legacy_character_chunks(source, target_chars, max_chunks)
    if max_source_chars is not None and len(source) > max(int(max_source_chars), 1):
        legacy_diag = {
            "mode": "legacy",
            "source_chars": len(source),
            "chunk_count": len(legacy),
            "covered_source_chars": sum(len(chunk) for chunk in legacy),
            "budget_unit": "characters",
            "truncated": len(source) > sum(len(chunk) for chunk in legacy),
        }
        candidate_diag = {
            "mode": "structure-v1",
            "source_chars": len(source),
            "chunk_count": 0,
            "covered_source_chars": 0,
            "last_source_end": 0,
            "budget_unit": "characters-fallback",
            "truncated": True,
            "context_truncated": False,
            "kinds": [],
            "resource_limited": True,
            "error": "structure_chunking_source_too_large",
        }
        return {
            "mode": "shadow",
            "active_mode": "legacy",
            "source_chars": len(source),
            "legacy": legacy_diag,
            "candidate": candidate_diag,
            "bounded_candidate": {
                **candidate_diag,
                "mode": "structure-v1-bounded",
                "max_chunks": max(int(max_chunks), 1),
            },
        }
    materials = structure_aware_chunks(
        source,
        target_chars=target_chars,
        hard_chars=hard_chars,
    )
    bounded_materials = limit_chunk_materials(materials, max_chunks)
    last_source_end = max((material.source_end for material in materials), default=0)
    meaningful_source_end = len(source.rstrip())
    candidate_coverage_gap = has_uncovered_content(source, materials)
    candidate_context_truncated = any(material.context_truncated for material in materials)
    bounded_last_source_end = max(
        (material.source_end for material in bounded_materials), default=0
    )
    bounded_coverage_gap = has_uncovered_content(source, bounded_materials)
    bounded_context_truncated = any(
        material.context_truncated for material in bounded_materials
    )
    return {
        "mode": "shadow",
        "active_mode": "legacy",
        "source_chars": len(source),
        "legacy": {
            "mode": "legacy",
            "source_chars": len(source),
            "chunk_count": len(legacy),
            "covered_source_chars": sum(len(chunk) for chunk in legacy),
            "budget_unit": "characters",
            "truncated": len(source) > sum(len(chunk) for chunk in legacy),
        },
        "candidate": {
            "mode": "structure-v1",
            "chunk_count": len(materials),
            "covered_source_chars": sum(
                max(material.source_end - material.source_start, 0) for material in materials
            ),
            "last_source_end": last_source_end,
            "budget_unit": "characters-fallback",
            "truncated": last_source_end < meaningful_source_end
            or candidate_coverage_gap
            or candidate_context_truncated,
            "context_truncated": candidate_context_truncated,
            "kinds": [material.kind for material in materials],
        },
        "bounded_candidate": {
            "mode": "structure-v1-bounded",
            "chunk_count": len(bounded_materials),
            "last_source_end": bounded_last_source_end,
            "max_chunks": max(int(max_chunks), 1),
            "resource_limited": len(bounded_materials) < len(materials),
            "truncated": bounded_last_source_end < meaningful_source_end
            or bounded_coverage_gap
            or bounded_context_truncated,
            "context_truncated": bounded_context_truncated,
        },
    }


def structure_aware_chunks(
    text: str,
    *,
    target_chars: int,
    hard_chars: int | None = None,
    max_chunks: int | None = None,
) -> list[ChunkMaterial]:
    """Parse structural blocks and pack them without silently dropping the tail.

    Character limits are an intentional first-stage fallback because the Ollama embeddings API
    does not expose tokenizer counts.  The API is shaped so a model-matched token counter can be
    added later without changing source-span or packing semantics.
    """

    source = text or ""
    if not source:
        return [ChunkMaterial("", "empty", (), 0, 0)]
    target = max(int(target_chars), 1)
    hard = max(int(hard_chars or target), target)
    blocks = _parse_structural_blocks(source)
    materials = _pack_blocks(blocks, target_chars=target, hard_chars=hard)
    return limit_chunk_materials(materials, max_chunks)


def limit_chunk_materials(
    materials: list[ChunkMaterial], max_chunks: int | None
) -> list[ChunkMaterial]:
    """Apply a bounded request budget while retaining the beginning and tail."""

    if max_chunks is None:
        return materials
    limit = max(int(max_chunks), 1)
    if len(materials) <= limit:
        return materials
    if limit == 1:
        return [materials[-1]]
    return [*materials[: limit - 1], materials[-1]]


def has_uncovered_content(source: str, materials: list[ChunkMaterial]) -> bool:
    """Return whether non-heading, non-whitespace source falls outside the plan."""

    cursor = 0
    for material in sorted(materials, key=lambda item: (item.source_start, item.source_end)):
        gap = source[cursor : material.source_start]
        if any(line.strip() and not _HEADING_RE.match(line) for line in gap.splitlines()):
            return True
        cursor = max(cursor, material.source_end)
    tail = source[cursor:]
    return any(line.strip() and not _HEADING_RE.match(line) for line in tail.splitlines())


def _parse_structural_blocks(text: str) -> list[StructuralBlock]:
    lines = text.splitlines(keepends=True)
    blocks: list[StructuralBlock] = []
    heading_stack: list[str] = []
    pending: list[tuple[int, str]] = []
    pending_heading: tuple[int, str, tuple[str, ...]] | None = None
    offset = 0
    in_fence = False
    fence_marker = ""

    def flush(kind: str | None = None) -> None:
        nonlocal pending
        if not pending:
            return
        raw = "".join(value for _, value in pending)
        leading = len(raw) - len(raw.lstrip())
        body = raw.strip()
        start = pending[0][0] + leading
        end = start + len(body)
        if body:
            blocks.append(
                StructuralBlock(
                    kind=kind or _classify_block(body),
                    text=body,
                    heading_path=tuple(heading_stack),
                    start=start,
                    end=end,
                )
            )
        pending = []

    for line in lines:
        raw = line.rstrip("\r\n")
        stripped = raw.strip()
        fence = _FENCE_RE.match(raw)

        if fence:
            if not in_fence:
                pending_heading = None
                flush()
                in_fence = True
                fence_marker = fence.group(1)[0]
                pending.append((offset, line))
            elif fence_marker == fence.group(1)[0]:
                pending.append((offset, line))
                flush("code")
                in_fence = False
                fence_marker = ""
            else:
                pending.append((offset, line))
            offset += len(line)
            continue

        if in_fence:
            pending.append((offset, line))
            offset += len(line)
            continue

        heading = _HEADING_RE.match(raw)
        if heading:
            flush()
            if pending_heading is not None:
                start, heading_text, parent_path = pending_heading
                blocks.append(
                    StructuralBlock(
                        kind="heading",
                        text=heading_text,
                        heading_path=parent_path,
                        start=start,
                        end=start + len(heading_text),
                    )
                )
            level = len(heading.group(1))
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(heading.group(2).strip())
            pending_heading = (offset, raw.strip(), tuple(heading_stack[:-1]))
            offset += len(line)
            continue

        if not stripped:
            flush()
            offset += len(line)
            continue

        pending_heading = None
        if pending and _starts_new_atomic_block(pending, raw):
            flush()
        pending.append((offset, line))
        offset += len(line)

    flush("code" if in_fence else None)
    if pending_heading is not None:
        start, heading_text, parent_path = pending_heading
        blocks.append(
            StructuralBlock(
                kind="heading",
                text=heading_text,
                heading_path=parent_path,
                start=start,
                end=start + len(heading_text),
            )
        )
    return blocks


def _starts_new_atomic_block(pending: list[tuple[int, str]], raw: str) -> bool:
    current = "".join(value for _, value in pending).strip()
    if not current:
        return False
    current_kind = _classify_block(current)
    next_kind = _classify_block(raw.strip())
    atomic_kinds = {"table", "list"}
    return current_kind != next_kind and bool({current_kind, next_kind} & atomic_kinds)


def _classify_block(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "empty"
    if any(_FENCE_RE.match(line) for line in lines[:1]):
        return "code"
    if len(lines) >= 2 and all("|" in line for line in lines[:2]):
        return "table"
    if _LIST_RE.match(lines[0]):
        return "list"
    return "paragraph"


def _pack_blocks(
    blocks: list[StructuralBlock],
    *,
    target_chars: int,
    hard_chars: int,
) -> list[ChunkMaterial]:
    chunks: list[ChunkMaterial] = []
    current: ChunkMaterial | None = None

    for block in blocks:
        pieces = _split_oversized_block(block, hard_chars)
        for piece in pieces:
            contextual = _contextual_text(piece, max_chars=hard_chars)
            candidate = ChunkMaterial(
                text=contextual,
                kind=piece.kind,
                heading_path=piece.heading_path,
                source_start=piece.start,
                source_end=piece.end,
                context_truncated=_contextual_text_truncated(piece, hard_chars),
            )
            if current is None:
                current = candidate
                continue
            same_context = current.heading_path == candidate.heading_path
            compatible_kind = current.kind == candidate.kind == "paragraph"
            candidate_body = _without_heading_context(candidate.text, candidate.heading_path)
            combined_len = len(current.text) + 2 + len(candidate_body)
            if same_context and compatible_kind and combined_len <= target_chars:
                current = ChunkMaterial(
                    text=f"{current.text}\n\n{candidate_body}",
                    kind="paragraph",
                    heading_path=current.heading_path,
                    source_start=current.source_start,
                    source_end=candidate.source_end,
                    context_truncated=current.context_truncated or candidate.context_truncated,
                )
            else:
                chunks.append(current)
                current = candidate
    if current is not None:
        chunks.append(current)
    return chunks or [ChunkMaterial("", "empty", (), 0, 0)]


def _split_oversized_block(block: StructuralBlock, hard_chars: int) -> list[StructuralBlock]:
    if len(_contextual_text(block)) <= hard_chars:
        return [block]
    text = block.text
    pieces: list[StructuralBlock] = []
    cursor = 0
    while cursor < len(text):
        remaining = text[cursor:]
        limit = max(hard_chars - len(_heading_prefix(block.heading_path)) - 1, 1)
        end = len(remaining) if len(remaining) <= limit else _preferred_break(remaining, limit)
        piece_text = remaining[:end].strip()
        if not piece_text:
            end = min(len(remaining), max(limit, 1))
            piece_text = remaining[:end]
        start = block.start + cursor + len(remaining[:end]) - len(remaining[:end].lstrip())
        pieces.append(
            StructuralBlock(
                kind=block.kind,
                text=piece_text,
                heading_path=block.heading_path,
                start=start,
                end=start + len(piece_text),
            )
        )
        cursor += end
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    return pieces


def _preferred_break(text: str, limit: int) -> int:
    window = text[:limit]
    for marker in ("\n", "。", "！", "？", ". ", "! ", "? ", " "):
        position = window.rfind(marker)
        if position >= max(1, limit // 3):
            return position + len(marker)
    return limit


def _heading_prefix(heading_path: tuple[str, ...]) -> str:
    return " > ".join(heading_path)


def _contextual_text(block: StructuralBlock, max_chars: int | None = None) -> str:
    prefix = _heading_prefix(block.heading_path)
    if max_chars is not None and prefix:
        available = max(int(max_chars) - len(block.text) - 1, 0)
        if available < len(prefix):
            prefix = prefix[-available:] if available else ""
    return f"{prefix}\n{block.text}" if prefix else block.text


def _contextual_text_truncated(block: StructuralBlock, max_chars: int) -> bool:
    return bool(_heading_prefix(block.heading_path)) and len(_contextual_text(block)) > max_chars


def _without_heading_context(text: str, heading_path: tuple[str, ...]) -> str:
    if not heading_path:
        return text
    _, separator, body = text.partition("\n")
    return body if separator else text
