# 04 — Infrastructure Gaps and Polish Roadmap

> Current status: active roadmap. This file tracks operational safety, observability, exports, and configuration polish.

## Status Summary

| Area | Status | Evidence | Remaining work |
|---|---|---|---|
| Session recovery | Planned | Launcher cleans stale PID files, but full storage recovery was not verified. | Add SQLite/LanceDB consistency recovery and stale task release. |
| Performance benchmarking | Planned | No benchmark history command verified. | Add opt-in timing and regression baselines. |
| Emoji-only noise detection | Needs verification | `noise_filter.py` exists. | Add explicit emoji-only tests and implementation if missing. |
| Dual-layer iron rules | Planned | Step closure exists; derived principles not verified. | Store technical lesson and decision principle as linked outputs. |
| Obsidian vault sync | Planned | `pack_export` JSON exists; markdown export not verified. | Add markdown/YAML export command. |
| Config-driven tier/decay | Planned | Decay constants appear code-based. | Move thresholds to validated config with env overrides. |
| Multi-provider embedding/key rotation | Planned | Default Ollama/fallback path exists. | Research provider abstraction and vector dimension strategy. |

## 1. Session Recovery

### Goal

Recover consistent state after MCP server restart or process interruption.

### Tasks

- Compare LanceDB rows against SQLite memory records and remove or repair orphans.
- Re-index SQLite memories missing LanceDB vectors.
- Release stale Hunter Guild claims that exceeded heartbeat timeout.
- Run recovery during startup after bootstrap and before normal service operation.
- Report degraded recovery when storage is unavailable.

## 2. Performance Benchmarking

### Goal

Make performance claims measurable rather than anecdotal.

### Metrics

- Retrieval latency p50/p95/p99.
- Candidate counts by source.
- Rerank time.
- Embedding throughput.
- Memory pool and LanceDB index size.

### Tasks

- Add opt-in benchmark instrumentation.
- Store recent benchmark history.
- Add a `system` action or CLI command for reporting.
- Add regression gates for representative queries.

## 3. Emoji-Only Noise Detection

### Goal

Prevent low-information reaction messages from entering durable memory.

### Tasks

- Add tests for pure emoji, emoji plus whitespace, bracketed reaction text, and mixed meaningful text.
- Implement the filter if tests show it is missing.
- Keep the rule conservative so legitimate content containing emoji is not discarded.

## 4. Dual-Layer Iron Rules

### Goal

Every important lesson should optionally produce two linked memory shapes:

1. A concrete technical pitfall.
2. A reusable decision principle.

### Tasks

- Extend step closure or smart remembering to derive a principle candidate from lesson/root cause/optimization.
- Store backlink metadata between the lesson and principle.
- Make the behavior opt-in or quality-gated to avoid noisy principle spam.

## 5. Obsidian Vault Sync

### Goal

Export memories to a markdown vault for human review and external knowledge workflows.

### Proposed command

```bash
python -m plastic_promise export-obsidian --output ./obsidian-vault/
```

### Proposed folders

```text
00-Preferences/
01-Facts/
02-Decisions/
03-Entities/
04-Events/
05-Patterns/
```

## 6. Config-Driven Tier/Decay

### Goal

Move hard-coded tier and decay values into validated configuration.

### Tasks

- Define schema and safe ranges.
- Support environment variable overrides.
- Keep current defaults stable.
- Add runtime reload only after validation.

## 7. Multi-Provider Embedding and Key Rotation

### Goal

Provide resilience beyond local Ollama without breaking existing vector dimensions or local-first expectations.

### Research Questions

- Which providers support compatible dimensions or safe projection?
- Should each provider have its own LanceDB table?
- How should API key arrays rotate on rate limit?
- What privacy warning is required for hosted embeddings?

## Acceptance Criteria

- Recovery can repair a simulated orphan vector and missing vector.
- Benchmark output can compare current run against a baseline.
- Noise filter has explicit low-information reaction tests.
- Config changes preserve existing defaults.
- External provider documentation clearly states data-boundary implications.
