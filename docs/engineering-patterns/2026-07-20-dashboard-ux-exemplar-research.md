---
title: Dashboard V2 Memory UX Exemplar Research
date: 2026-07-20
status: reviewed
topic: lineage-synthesis-retrieval-explain-ux
references:
  - mem0ai/mem0/openmemory/ui@main
  - langfuse/langfuse/web@main
  - MemTensor/MemOS/apps/memos-local-plugin@554bb98ee7c28307dbaeac569a0dea49ff0062fd
---

# Dashboard V2 Memory UX Exemplar Research

## Scope and method

This is a focused follow-up to the reviewed MemOS/OpenMemory research from 2026-07-19. The
question is not which visual style to copy; it is which information structures make a memory
control plane easier to inspect when data is empty, partial, or slow. Source code was checked via
the upstream GitHub trees and files, then compared with the current static Dashboard V2.

## Reference 1: OpenMemory

### Q1: What exactly does it do?

OpenMemory's `MemoryDetails` uses a two-column detail surface: the memory itself occupies the main
column, while `AccessLog` and `RelatedMemories` occupy a secondary column. The detail header keeps
the identifier, copy action, and state actions together. `AccessLog` renders a vertical event rail
with an icon, actor, and timestamp for each entry. `RelatedMemories` keeps each related item as a
short, clickable block with category and lifecycle metadata. `MemoryTable` uses a dense table with
selection, source/category/date columns, and an overflow action menu rather than turning every row
into a large card.

Sources:

- [MemoryDetails.tsx](https://github.com/mem0ai/mem0/blob/main/openmemory/ui/app/memory/%5Bid%5D/components/MemoryDetails.tsx)
- [AccessLog.tsx](https://github.com/mem0ai/mem0/blob/main/openmemory/ui/app/memory/%5Bid%5D/components/AccessLog.tsx)
- [RelatedMemories.tsx](https://github.com/mem0ai/mem0/blob/main/openmemory/ui/app/memory/%5Bid%5D/components/RelatedMemories.tsx)
- [MemoryTable.tsx](https://github.com/mem0ai/mem0/blob/main/openmemory/ui/app/memories/components/MemoryTable.tsx)

### Q2: How does our context differ?

Plastic Promise is read-only at this dashboard boundary and has no access-log table for a memory.
The available evidence is canonical memory metadata, `memory_lineage`, `call_spans`, and runtime
events. A related-memory panel must therefore link to lineage or canonical IDs and must never imply
semantic similarity that was not recorded. Global memories are visible across projects, but their
lineage may carry a different project owner and needs a conservative projection.

### Q3: What should we adapt vs skip?

- **Adapt:** a compact memory detail layout with one primary content panel and evidence panels for
  lineage, source call, lifecycle, and scope. Reuse the existing dialog and timeline primitives;
  no second API is needed.
- **Adapt:** a vertical lineage rail with relation, source ID, call ID, and timestamp. Make the
  scope and evidence availability explicit in the rail header.
- **Skip:** write actions, bulk selection, and a made-up related-memory similarity list. The current
  dashboard contract is read-only and should not manufacture relationships.

## Reference 2: Langfuse trace view

### Q1: What exactly does it do?

Langfuse separates identity from time. `TraceTimeline` keeps a fixed, resizable name gutter beside
one scrollable chart pane so a hierarchy remains readable while the time bars move. It uses a dense
26px row height and virtualized rows. `TimelineBar` renders a minimum 4px marker for zero or nearly
zero duration spans, then places the duration label after the bar. The trace header exposes latency as
a dedicated metadata badge instead of hiding it in a generic JSON blob. The layout code computes a
comfortable information-panel width and persists the split, so detail data is not squeezed by an
overly wide tree.

Sources:

- [TraceTimeline/index.tsx](https://github.com/langfuse/langfuse/blob/main/web/src/components/trace/components/TraceTimeline/index.tsx)
- [TimelineBar.tsx](https://github.com/langfuse/langfuse/blob/main/web/src/components/trace/components/TraceTimeline/TimelineBar.tsx)
- [timeline-calculations.ts](https://github.com/langfuse/langfuse/blob/main/web/src/components/trace/components/TraceTimeline/timeline-calculations.ts)
- [TraceDetailViewHeader.tsx](https://github.com/langfuse/langfuse/blob/main/web/src/components/trace/components/TraceDetailView/TraceDetailViewHeader.tsx)

### Q2: How does our context differ?

Our explain snapshot has channel state and pipeline counters, but not per-channel start/end spans.
Pretending it is a Gantt chart would be misleading. We can still use the same information hierarchy:
stable channel names on the left, a compact state rail in the middle, and counts/score evidence on
the right. Missing latency must remain `not captured`; a zero-length timestamp is not proof of a
zero-cost retrieval.

### Q3: What should we adapt vs skip?

- **Adapt:** a dense channel matrix with one row per planned channel, a state badge, result count,
  and a short reason. Keep the header metrics visible while the evidence list scrolls.
- **Adapt:** a minimum visual marker for a measured sub-millisecond duration and a distinct “not
  captured” label for absent timing. Add a fixed-width latency column in Requests and Explain.
- **Skip:** a full virtualized Gantt/playhead implementation until the trace schema records child
  spans and channel timings. It would add visual complexity without evidence.

## Reference 3: MemOS local-plugin evolution pipeline

### Q1: What exactly does it do?

The local plugin keeps execution evidence in stages: episode -> trace -> candidate bucket -> policy
or world-model abstraction. Candidate promotion requires repeated, distinct evidence and rechecks
before induction. Its public event envelope carries a timestamp, sequence, and correlation ID, which
makes the stage transitions inspectable even when the final artifact is absent.

Source examples are documented in the pinned research study:
[2026-07-19 MemOS/OpenMemory exemplar research](2026-07-19-memos-openmemory-exemplar-research.md).

### Q2: How does our context differ?

Plastic Promise already has stronger canonical controls: `call_spans`, `memory_lineage`, governed
`synthesis_artifacts`, source hashes, visibility, and fail-closed retrieval gates. The current
database legitimately has no synthesis artifacts, so the UI must explain the lifecycle gate rather
than display a dead-end empty table.

### Q3: What should we adapt vs skip?

- **Adapt:** make the synthesis empty state a lifecycle panel: current artifact gate, current
  retrieval gate, source-of-truth counts, and the exact reason no artifact is visible. Link to
  Configuration and Memories rather than inventing rows.
- **Adapt:** show lineage as evidence stages (source, correction/forget event, call, verification)
  with stable relation labels and a bounded “evidence unavailable” state.
- **Skip:** permissive auto-promotion, a second candidate database, and any status inferred from a
  missing row. Unknown remains unknown.

## Applied design decisions

1. **Evidence first:** every detail panel names its source table or says that evidence was not
   captured.
2. **Unknown is not zero:** duration, missing snapshots, and absent synthesis artifacts use explicit
   labels and never silently render as `0 ms`, `draft`, or `related`.
3. **Dense but calm:** use one strong page title, compact metric strip, restrained status colors,
   and tables/timelines for repeated evidence. Avoid nested decorative cards and large gradients.
4. **Stable identity:** IDs remain copyable and are shown with a full-value tooltip; short labels are
   only presentation.
5. **Responsive evidence:** on narrow screens, tables become horizontally scrollable evidence regions
   and detail panels stack in the order content -> status -> provenance.

## Quality review

- All three references answer Q1, Q2, and Q3 with source paths and concrete component behavior.
- Recommendations are constrained by the current SQLite/read-only contract; no new storage or
  retrieval semantics are proposed.
- The latency rule explicitly distinguishes measured sub-millisecond spans from missing timing.
- The synthesis recommendation preserves the existing fail-closed gates.
- The source links resolve to upstream code paths observed during this review; no benchmark or
  production-quality claim is inferred from a visual pattern.
