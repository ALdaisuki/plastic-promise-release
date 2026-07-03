# 01 — Baseline Comparison: Plastic Promise vs CortexReach memory-lancedb-pro

> Comparative analysis date: 2026-07-03.
> This file is a dated research baseline, not the current roadmap truth.
> Current unfinished/partial items are tracked in [README.md](README.md).

## Purpose

This comparison was created to identify memory and retrieval subsystem gaps by studying CortexReach `memory-lancedb-pro`. Since the analysis date, several Plastic Promise retrieval features have been implemented or partially implemented. Do not use this file alone to determine whether work remains open.

## Scope Difference

| Dimension | Plastic Promise | CortexReach memory-lancedb-pro |
|---|---|---|
| Scope | AI behavior governance runtime | Memory plugin for OpenClaw agents |
| Paradigm | Commitment Engineering | Plugin memory architecture |
| Language | Python + optional Rust | TypeScript + JavaScript |
| Distribution | MCP server | npm package |
| Database | SQLite + LanceDB | LanceDB |
| Agent model | Multi-agent governance and task dispatch | Single-agent memory capability |

## Key Lessons Kept From the Comparison

1. Retrieval quality benefits from explicit query expansion, reranking, recency/decay ranking, and diversity.
2. Memory lifecycle quality requires category-aware merge behavior, chunking for long records, compaction, and throttled extraction.
3. Observability matters: a trace explaining why a memory surfaced is useful for debugging and trust.
4. Infrastructure polish matters: recovery, benchmarking, export formats, and provider fallback make a memory system operable.
5. Plastic Promise should preserve its advantages: principles, trust, audit, graph context, Hunter Guild, and step closure.

## Current Open Work Derived From This Baseline

See [README.md](README.md) for the authoritative status table. The main remaining buckets are:

- Verify partially implemented retrieval features.
- Finish smart extraction and memory lifecycle features.
- Add infrastructure safety and observability.
- Close Rust context-engine parity gaps.
- Begin causal/event memory foundation.
