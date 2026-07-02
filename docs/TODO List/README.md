# TODO List — Plastic Promise Enhancement Roadmap

> Based on deep comparative analysis of [CortexReach/memory-lancedb-pro](https://github.com/CortexReach/memory-lancedb-pro) (v1.1.0-beta.10, 4.4k stars).
> Analysis date: 2026-07-03.

## Priority Legend

| Mark | Meaning |
|------|---------|
| 🔴 P0 | Critical — directly impacts retrieval quality or system correctness |
| 🟡 P1 | High — significant improvement, should be in next iteration |
| 🟢 P2 | Medium — nice to have, clear benefit |
| ⚪ P3 | Low — polish, research, or long-term |

## Quick Summary

Plastic Promise and CortexReach solve different problems at different scales:
- **CortexReach**: A focused, polished memory plugin for OpenClaw agents (npm package, TypeScript). Excellent retrieval pipeline, multi-provider reranker, community ecosystem.
- **Plastic Promise**: A full AI behavior governance system (MCP-native, Python+Rust). 12 principles, SCARF, trust scores, Hunter Guild, domain federation. Memory is one domain among many.

**Plastic Promise has 48 MCP tools across 11 domains; CortexReach has 18 contracted agent tools. The comparison is about the memory/retrieval subsystem specifically.**

> **Agent 2 深挖更正**: `decay-engine.ts` 和 `tier-manager.ts` 并非独立文件——衰减逻辑分布在 `retriever.ts`（三种指数公式）、`access-tracker.ts`（间隔重复）和配置 schema（43 个配置对象）中。`session-recovery.ts` 是目录解析工具，并非崩溃恢复。

---

## Gap Inventory (17 items)

| # | Gap | Priority | Effort | File |
|---|-----|----------|--------|------|
| 1 | Query Expansion (local synonym dict) | 🔴 P0 | S | [02-retrieval-enhancement.md](02-retrieval-enhancement.md) |
| 2 | Cross-Encoder Reranker — multi-provider + always-on | 🔴 P0 | M | [02-retrieval-enhancement.md](02-retrieval-enhancement.md) |
| 3 | Decay in Ranking (additive recency + multiplicative time) | 🔴 P0 | S | [02-retrieval-enhancement.md](02-retrieval-enhancement.md) |
| 4 | Real-time Tier Promotion/Demotion (config-driven) | 🟡 P1 | M | [03-smart-extraction-upgrade.md](03-smart-extraction-upgrade.md) |
| 5 | Category-Aware Merge Rules (7 LLM decisions) | 🟡 P1 | M | [03-smart-extraction-upgrade.md](03-smart-extraction-upgrade.md) |
| 6 | Content Chunking for Long Memories | 🟡 P1 | M | [03-smart-extraction-upgrade.md](03-smart-extraction-upgrade.md) |
| 7 | Vector-Based MMR Diversity | 🟡 P1 | M | [02-retrieval-enhancement.md](02-retrieval-enhancement.md) |
| 8 | Memory Compaction (Progressive Summarization) | 🟡 P1 | M | [03-smart-extraction-upgrade.md](03-smart-extraction-upgrade.md) |
| 9 | Extraction Throttling (sliding window rate limiter) | 🟡 P1 | S | [03-smart-extraction-upgrade.md](03-smart-extraction-upgrade.md) |
| 10 | Pipeline Trace (ScoreHistory + RetrievalTrace) | 🟢 P2 | M | [02-retrieval-enhancement.md](02-retrieval-enhancement.md) |
| 11 | Debounced Access Tracking with Write-Back | 🟢 P2 | M | [04-infrastructure-gaps.md](04-infrastructure-gaps.md) |
| 12 | Performance Benchmarking (smoke/baseline/gate) | 🟢 P2 | S | [04-infrastructure-gaps.md](04-infrastructure-gaps.md) |
| 13 | Emoji-Only Detection in Noise Filter | 🟢 P2 | XS | [04-infrastructure-gaps.md](04-infrastructure-gaps.md) |
| 14 | Dual-Layer Iron Rules (LRN/ERR markdown entries) | 🟢 P2 | M | [04-infrastructure-gaps.md](04-infrastructure-gaps.md) |
| 15 | Obsidian Vault Sync (markdown + YAML export) | 🟢 P2 | M | [04-infrastructure-gaps.md](04-infrastructure-gaps.md) |
| 16 | Config-Driven Tier/Decay (43 config objects) | ⚪ P3 | L | [04-infrastructure-gaps.md](04-infrastructure-gaps.md) |
| 17 | Multi-Provider Embedding with Key Rotation | ⚪ P3 | L | [04-infrastructure-gaps.md](04-infrastructure-gaps.md) |
| 18 | Rust Engine — Principle Injection | 🟡 P1 | S | [06-rust-engine-gaps.md](06-rust-engine-gaps.md) |
| 19 | Rust Engine — Graph Traversal | 🟢 P2 | M | [06-rust-engine-gaps.md](06-rust-engine-gaps.md) |

---

## What Plastic Promise Already Does Better

These are areas where Plastic Promise leads and should NOT be changed:

| Area | Plastic Promise Advantage |
|------|--------------------------|
| **Governance Integration** | Memory retrieval is trust-score-aware, principle-aligned, and SCARF-linked |
| **Entity Graph** | Bidirectional principle↔memory edges with deep-grammar traversal |
| **Domain Federation** | 7 domains with auto-merge, federated signals, and domain-weighted retrieval |
| **Audit Trail** | 11-dimension audit, pre-check, full history |
| **Hunter Guild** | Task delegation, trust-based access, daemon-driven maintenance |
| **Step Closure** | Six-chain loop (principles→SCARF→hormones→trust→reflection→CEI) |
| **Experience Packs** | Streaming export/import with version mapping |
| **Git Governance** | Conventional Commits, Squash Merge, CI/CD integration |
| **Daemon System** | 11 scanners, automatic fix task generation, scheduler health meta-audit |
| **SuperPowers Pipeline** | 12-stage workflow with chain validation |

---

## Recommended Implementation Order

```
Week 1: P0 items (3 items)
  → Query Expansion: local synonym dict, zero API cost, ~150 lines
  → Multi-Provider Reranker: Jina+SiliconFlow free tiers + Ollama fallback
  → Decay-in-Ranking: additive recency boost + multiplicative time decay
  → All independent, immediate retrieval quality improvement

Week 2: P1 items (6 items)
  → Tier Promotion: composite score thresholds during retrieval
  → Merge Rules: 7 LLM decisions (SUPERSEDE/SUPPORT/CONTEXTUALIZE)
  → Chunking: LanceDB schema v2 migration required
  → Vector MMR: real cosine diversity, not content-only
  → Memory Compaction: LLM clustering + cooldown + archive
  → Extraction Throttling: sliding window, 30/hr default

Week 3: P2 items (5 items)
  → Pipeline Trace: ScoreHistory per stage
  → Debounced Access Tracking: 5s write-back buffer
  → Benchmarking: smoke/baseline/gate fixtures
  → Emoji Detection + Iron Rules + Obsidian Sync
  → Low risk, incremental delivery

Backlog: P3 (2 items)
  → Config-Driven Tier/Decay + Multi-Provider Embedding
  → Research phase first — validate cost/benefit
```

---

## Files in This Folder

| File | Contents |
|------|----------|
| [01-comparison-analysis.md](01-comparison-analysis.md) | Full architectural comparison: Plastic Promise vs CortexReach |
| [02-retrieval-enhancement.md](02-retrieval-enhancement.md) | P0/P1 retrieval pipeline improvements |
| [03-smart-extraction-upgrade.md](03-smart-extraction-upgrade.md) | P1 smart extractor and memory lifecycle upgrades |
| [04-infrastructure-gaps.md](04-infrastructure-gaps.md) | P2/P3 infrastructure and polish items |
| [05-integration-roadmap.md](05-integration-roadmap.md) | Integration plan with existing systems |
| [06-rust-engine-gaps.md](06-rust-engine-gaps.md) | Rust engine missing features (principle injection, graph traversal) |
