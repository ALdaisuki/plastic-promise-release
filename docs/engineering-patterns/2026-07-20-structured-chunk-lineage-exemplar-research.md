---
title: Structured Chunking and Memory Lineage Exemplar Research
date: 2026-07-20
status: reviewed
topic: full-runtime-structured-chunking-and-lineage-ui
plastic_promise_revision: dffee898788d20c80fef92f1bf975ac96b98272c
references:
  - MemTensor/MemOS@554bb98ee7c28307dbaeac569a0dea49ff0062fd
  - mem0ai/mem0@ddaa655edf41e3ed375b263fb227da0bcd42ccb9
  - run-llama/llama_index@dbdaf89dc66a6469081c9f8fddc9c1bf6c43d8a2
  - langchain-ai/langchain@dc26ca50354268d248914d4f2b81f45d99725cc7
---

# Structured Chunking and Memory Lineage Exemplar Research

## Decision

The complete runtime needs one inspectable chunk contract shared by Python and Rust. A chunk is
an index/evidence projection of a canonical memory, not a new canonical memory. The minimum
contract is:

```text
chunk_id, parent_memory_id, ordinal, kind, header_path,
source_start, source_end, source_hash, text_hash, text
```

The parent remains the authorization, lifecycle, worth, and audit subject. Search may rank a
chunk, but the API must return its parent and source span; lineage and UI must never imply that a
chunk is an independently writable memory. Python and Rust must produce the same boundaries and
hashes for the same UTF-8 input and configuration. Provider-specific vectors are allowed only
after the contract check passes.

This document applies the required three-question method to four mature references. Claims below
are based on pinned source snapshots, not README-only summaries.

## Reference 1: MemOS OpenClaw Chunker

Source: [`apps/memos-local-openclaw/src/ingest/chunker.ts`](https://github.com/MemTensor/MemOS/blob/554bb98ee7c28307dbaeac569a0dea49ff0062fd/apps/memos-local-openclaw/src/ingest/chunker.ts)

### Q1: What exactly does it do?

`chunkText` first extracts fenced code, brace-delimited function/class blocks, error stacks,
lists, and command lines into placeholders (lines 23-52). Remaining prose is split at blank
paragraph boundaries (lines 54-74), short adjacent chunks are merged up to `MAX_CHUNK_CHARS=3000`
and an `IDEAL_CHUNK_CHARS=1500` target (lines 162-185), and oversized prose is split at sentence
boundaries including Chinese punctuation (lines 187-214). It labels chunks as `paragraph`,
`code_block`, `error_stack`, `list`, or `command` (lines 1-10), and keeps fenced/code regions
intact before fallback splitting.

### Q2: How does our context differ?

Plastic Promise already has canonical SQLite memories, a rebuildable LanceDB index, project and
visibility filters, outbox jobs, and `memory_lineage`. We therefore cannot copy this function's
flat `RawChunk` output: it lacks parent IDs, offsets, hashes, authorization scope, and a stable
cross-language identity. Its regex/brace heuristics are still useful for classifying structural
blocks and for a bounded fallback when a Markdown parser cannot classify a document.

### Q3: What should we adapt vs skip?

- **Adapt:** structural kinds, protected fenced/code blocks, paragraph and sentence fallback,
  and explicit caps. Integrate in `plastic_promise/core/chunking.py` and mirror the same state
  machine in Rust. Add `kind` and `header_path` to `ChunkMaterial`.
- **Redesign:** every output gets UTF-8 byte/character spans, parent ID, ordinal, and hashes;
  emit diagnostics instead of silently dropping text.
- **Skip:** a flat, non-addressable chunk list and provider-specific limits as the canonical
  contract.

## Reference 2: LlamaIndex MarkdownNodeParser

Source: [`markdown.py`](https://github.com/run-llama/llama_index/blob/dbdaf89dc66a6469081c9f8fddc9c1bf6c43d8a2/llama-index-core/llama_index/core/node_parser/file/markdown.py)

### Q1: What exactly does it do?

`MarkdownNodeParser` scans line by line, toggles a code-block guard, and only interprets `#`-style
headers outside fenced code (lines 48-68). It maintains a header stack, pops equal or higher
levels, and stores the path of ancestor headers as `header_path` metadata (lines 81-105 and
118-123). Each section becomes a node while preserving previous/next relationships through the
common node builder.

### Q2: How does our context differ?

Our chunks must remain useful for non-Markdown memories and Chinese text, and they must preserve
exact source spans for lineage. A header path alone is insufficient for audit or UI highlighting;
we need offsets and a deterministic source hash. We also must keep headers in the embedding context
even if the display text is compacted, otherwise a short section loses its topic.

### Q3: What should we adapt vs skip?

- **Adapt:** a header stack with equal/higher-level popping, code-fence protection, and a stable
  serialized `header_path`.
- **Redesign:** attach offsets/hashes and preserve headers as `context_prefix` rather than relying
  only on metadata.
- **Skip:** framework-specific node IDs and callback machinery.

## Reference 3: LangChain MarkdownHeaderTextSplitter

Source: [`markdown.py`](https://github.com/langchain-ai/langchain/blob/dc26ca50354268d248914d4f2b81f45d99725cc7/libs/text-splitters/langchain_text_splitters/markdown.py)

### Q1: What exactly does it do?

`MarkdownHeaderTextSplitter` sorts configured header tokens by length (lines 23-55), tracks a
header stack while scanning lines, and associates each content line with a copy of the current
metadata (lines 180-263). Consecutive lines with equal metadata are aggregated (lines 88-107 and
273-280). Its experimental splitter additionally treats fenced code and horizontal rules as
structural boundaries and records code language metadata (lines 298-315 and 392-480).

### Q2: How does our context differ?

Line-level metadata aggregation is a good model for preserving hierarchy, but the implementation
does not promise stable source offsets or cross-runtime hashes. It also makes "strip headers" a
caller choice, while our index must always retain enough header context for retrieval explanation.

### Q3: What should we adapt vs skip?

- **Adapt:** configurable header levels, metadata aggregation, code/horizontal-rule boundaries,
  and an explicit `structure-v1` schema version.
- **Redesign:** make configuration part of `chunking_identity`, and include both display text and
  embedding text so the UI can show exact source while retrieval uses header context.
- **Skip:** line-by-line output as the default; it creates too many low-value candidates.

## Reference 4: mem0 OpenMemory Detail UX

Sources: [`MemoryDetails.tsx`](https://github.com/mem0ai/mem0/blob/ddaa655edf41e3ed375b263fb227da0bcd42ccb9/openmemory/ui/app/memory/%5Bid%5D/components/MemoryDetails.tsx),
[`AccessLog.tsx`](https://github.com/mem0ai/mem0/blob/ddaa655edf41e3ed375b263fb227da0bcd42ccb9/openmemory/ui/app/memory/%5Bid%5D/components/AccessLog.tsx),
and [`RelatedMemories.tsx`](https://github.com/mem0ai/mem0/blob/ddaa655edf41e3ed375b263fb227da0bcd42ccb9/openmemory/ui/app/memory/%5Bid%5D/components/RelatedMemories.tsx).

### Q1: What exactly does it do?

The detail route separates the memory body from actions, access history, and related-memory
navigation. This keeps the primary fact readable while making operational evidence available on
demand. The list/table and detail routes use stable IDs and pagination rather than embedding all
history in the card.

### Q2: How does our context differ?

Our lineage is richer than a flat related list: it has parent/child revisions, correction and
synthesis relations, call IDs, project visibility, retrieval evidence, and now chunk spans. A
single "related memories" list would hide relation direction and provenance, so the UI needs a
typed graph plus a selected-edge evidence panel.

### Q3: What should we adapt vs skip?

- **Adapt:** a readable primary detail surface, progressive disclosure for history, stable deep
  links, and separate access/evidence sections.
- **Redesign:** show typed lineage edges and chunk evidence with direction, timestamps, call IDs,
  score contributions, and visibility status.
- **Skip:** treating related items as peers without relation semantics or authorization checks.

## Plastic Promise Integration Contract

1. `ChunkMaterial` is the single source of truth for Python chunk boundaries and metadata.
2. A Rust `structure-v1` implementation must consume the same normalized UTF-8 text and config,
   return the same ordered spans/kinds/header paths, and expose a parity diagnostic in health and
   retrieval explain output.
3. `prepare_index_material` persists a versioned chunk manifest (or a compact manifest hash plus
   spans) alongside the canonical memory/index material. LanceDB rows carry `parent_memory_id`,
   `chunk_id`, `ordinal`, `kind`, and `header_path`.
4. Retrieval results expose `matched_chunk`, `chunk_count`, `chunking_mode`, and `lineage_anchor`;
   scores remain attached to the parent result for worth/lifecycle decisions.
5. Dashboard APIs return a parent-centered detail object with `chunks`, `lineage_edges`, and
   `evidence_events`. Project/visibility filtering applies before both chunk and edge expansion.
6. Full mode enables `structure-v1` only when parity and index readiness are proven; otherwise it
   reports an explicit degraded capability instead of claiming full mode.

## Review Checklist

- [x] Four references are pinned to immutable commits.
- [x] Each reference has concrete source paths, algorithms, defaults, and fallback behavior.
- [x] Adapt/redesign/skip decisions name the Plastic Promise integration point.
- [x] The canonical-parent rule prevents chunks from becoming a second memory store.
- [x] The cross-language parity and UI evidence requirements are explicit.

This research is reviewed against the current local implementation at revision
`dffee898788d20c80fef92f1bf975ac96b98272c`; implementation work may proceed only after the
contract above is used by tests and runtime diagnostics.
