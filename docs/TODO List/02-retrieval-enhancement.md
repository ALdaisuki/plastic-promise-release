# 02 — Retrieval Pipeline Roadmap

> Current status: several retrieval gaps are implemented or partially implemented. Use this file for design context and [README.md](README.md) for authoritative roadmap status.

## Status Summary

| Area | Status | Evidence | Remaining work |
|---|---|---|---|
| Query expansion | Done | `plastic_promise/core/query_expander.py` | Keep tests and docs aligned. |
| Reranker upgrade | Partial | `plastic_promise/core/reranker.py`, `tests/test_vertical_slice_units.py` | Local fallback order, default generation model, host normalization, and score parsing are verified; hosted-provider privacy docs remain. |
| Decay-aware ranking | Partial | `plastic_promise/core/context_engine.py`, `plastic_promise/core/decay_engine.py` | Verify additive recency boost and multiplicative time decay both affect retrieval ranking. |
| Vector MMR diversity | Partial | `plastic_promise/core/context_engine.py`, `plastic_promise/core/lancedb_store.py` | Verify real vector lookup path and add tests for near-duplicate demotion. |
| Pipeline trace | Planned | No verified public trace object in this docs pass. | Add optional trace/score history gated by env var. |

## Design Notes

### Query expansion

The preferred pattern is a lightweight local synonym and bilingual expansion layer. It should avoid LLM calls by default and only expand text/BM25 style retrieval, while semantic vector retrieval can still use the original query when appropriate.

### Reranking

Provider chains must degrade safely:

```text
configured hosted reranker -> local Ollama reranker -> original score / cosine fallback
```

External providers can send task text or memory snippets over the network. Documentation and configuration should make this explicit.

Local Ollama reranking must use a generation-capable model. The default is
`qwen2.5:3b`; `mxbai-embed-large` remains the embedding model and must not be
sent to `/api/generate`.

### Decay-aware ranking

The target design combines two effects:

```text
1. Additive recency boost: recent memories can surface even with moderate text/vector score.
2. Multiplicative time decay: stale memories naturally sink but do not vanish abruptly.
```

### Vector MMR

The target behavior is not just content-prefix deduplication. It should compare candidate vectors against selected vectors and demote near-duplicates, while preserving enough related context for coherent reasoning.

### Pipeline trace

Planned trace shape:

```python
@dataclass
class ScoreStep:
    stage: str
    score_before: float
    score_after: float
    delta: float
```

Trace output should be optional because score history can add memory and latency overhead.

## Acceptance Criteria for Closing This Roadmap

- Retrieval tests cover short ambiguous queries, bilingual queries, and already-expanded queries.
- Reranker fallback tests prove local/offline behavior still works.
- Decay ranking tests cover new, old, reinforced, and low-relevance memories.
- MMR tests prove near-duplicates are demoted without removing all useful context.
- Trace mode can explain why a result surfaced without affecting default latency.
