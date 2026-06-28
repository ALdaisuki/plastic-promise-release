# Sub-Project A: Embedder Wiring + MCP Handler Implementation

> Date: 2026-06-28
> Status: draft
> Scope: 打通 Plastic Promise 端到端记忆检索链路 — Ollama Embedder + MCP 16 个 handler

## 1. Goal

当前 Rust 三层架构 (SQLite + Domain + Retrieval) 已落地，但缺少两项关键链接：
1. Embedding 向量无法传入 Rust（`supply()` 的 `task_vector` 参数一直是空数组 `[]`）
2. MCP 工具 handler 大部分是 `pass` stub，无法被 Claude Code 调用

本子项目完成后，以下链路完全可用：
```
Claude Code → MCP memory_recall("帮我找上次的架构决策")
  → embedder.embed("帮我找上次的架构决策") → mxbai-embed-large [1024维]
  → ContextEngine.supply(text, vector, task_type, scope)
  → SQLite list + HybridRetriever.retrieve()
  → ContextPack JSON → Claude 收到上下文
```

## 2. Embedder Design

### 2.1 Architecture

```python
# plastic_promise/embedder.py

from abc import ABC, abstractmethod
import os
import requests

class Embedder(ABC):
    """Text-to-vector embedding abstract base."""
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...
    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
    @property
    @abstractmethod
    def dim(self) -> int: ...
    @property
    @abstractmethod
    def model_name(self) -> str: ...

class OllamaEmbedder(Embedder):
    """Default: local Ollama with mxbai-embed-large (1024 dim)."""
    def __init__(self, host=None, model=None):
        self.host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = model or os.getenv("EMBEDDER_MODEL", "mxbai-embed-large")
    
    def embed(self, text: str) -> list[float]:
        resp = requests.post(f"{self.host}/api/embeddings", json={
            "model": self.model, "prompt": text,
        }, timeout=30)
        resp.raise_for_status()
        return resp.json()["embedding"]
    
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]
    
    @property
    def dim(self) -> int: return 1024  # mxbai-embed-large
    @property  
    def model_name(self) -> str: return self.model

class OpenAIEmbedder(Embedder):
    """Fallback: OpenAI text-embedding-3-small (1536 dim)."""
    def __init__(self, ...): pass  # same pattern with openai SDK

def get_embedder() -> Embedder:
    """Factory: EMBEDDER_PROVIDER env var -> ollama|openai|jina, default ollama."""
    provider = os.getenv("EMBEDDER_PROVIDER", "ollama")
    if provider == "openai": return OpenAIEmbedder()
    if provider == "jina": return JinaEmbedder()  
    return OllamaEmbedder()
```

### 2.2 Integration Point

`plastic_promise/mcp/tools/context.py` — `handle_context_supply()`:
```python
from plastic_promise.embedder import get_embedder

async def handle_context_supply(engine, args):
    task_text = args["task_description"]
    embedder = get_embedder()
    task_vector = embedder.embed(task_text)
    pack = engine.supply(task_text, task_vector, args.get("task_type", "general"), args.get("scope", "global"))
    return [TextContent(type="text", text=json.dumps({...pack JSON...}, ensure_ascii=False))]
```

## 3. MCP Handler Implementation

### 3.1 Memory Domain (7 handlers)

All handlers in `plastic_promise/mcp/tools/memory.py`. Each:
- Calls SQLite storage via `engine.storage` (Rust StorageBackend)
- For `memory_recall` and `memory_store`: uses `get_embedder()` for embedding
- Returns structured JSON via `TextContent`

| Handler | Logic |
|---------|-------|
| `memory_recall` | embed query → `engine.supply(text, vec, type, scope)` → format ContextPack as JSON |
| `memory_store` | embed content → check duplicates via SQLite list → `engine.storage.store(record)` → return ID |
| `memory_update` | `engine.storage.update(id, UpdateFields{...})` → return success |
| `memory_forget` | `engine.storage.delete(id)` → return success |
| `memory_stats` | `engine.storage.stats(scope)` → format as JSON |
| `memory_list` | `engine.storage.list(filter)` → format as JSON |
| `memory_gc` | `engine.storage.list(decaying) → engine.storage.delete_batch(ids)` → return count |

### 3.2 Principle Domain (4 handlers)

| Handler | Logic |
|---------|-------|
| `principle_activate` | Look up CORE_PRINCIPLES by task_type → return matched principles |
| `principle_inherit` | Call PrincipleManager.inherit() → return diffusion results |
| `principle_diffuse` | Call PrincipleManager.diffuse() → return propagation state |
| `principle_evaluate` | Look up principle + run counterfactual evaluation → return consequences |

### 3.3 Context Domain (3 handlers — already partially implemented)

| Handler | Status | Change |
|---------|--------|--------|
| `context_supply` | Has logic, needs embedder | Add `get_embedder()` call before supply |
| `context_inject` | stub | Call `engine.graph.add_node()` or `add_edge()` |
| `context_graph` | stub | Call `engine.get_graph().list_nodes()` or `traverse()` |

### 3.4 Audit/Defense Domain (2 of 5 implemented)

| Handler | Logic |
|---------|-------|
| `audit_pre_check` | Call `engine.enforcer.pre_check(action, type)` → return result |
| `defense_status` | Call `engine.enforcer.get_defense_status()` → return status |

(audit_run, audit_report, defense_trust deferred to Phase B — need scoring logic)

### 3.5 Reflection Domain (1 of 3 implemented)

| Handler | Logic |
|---------|-------|
| `feedback_apply` | Look up memory → `record.record_adopted/rejected()` → `engine.storage.update()` → return new worth_score |

(scarf_reflect, inertia_check deferred — need SCARF engine)

### 3.6 Management Domain (1 of 3 implemented)

| Handler | Logic |
|---------|-------|
| `system_stats` | Aggregate SQLite stats + EntityGraph stats + memory_stats → return JSON |

(system_backup, system_migrate deferred — need file I/O)

## 4. Files Changed

| File | Change |
|------|--------|
| `plastic_promise/embedder.py` | Create — ~80 lines |
| `plastic_promise/mcp/tools/memory.py` | Rewrite — ~200 lines, all 7 handlers |
| `plastic_promise/mcp/tools/principles.py` | Rewrite — ~80 lines, 4 handlers |
| `plastic_promise/mcp/tools/context.py` | Update — add embedder call + inject/graph impl |
| `plastic_promise/mcp/tools/audit_defense.py` | Update — 2 handlers |
| `plastic_promise/mcp/tools/reflection.py` | Update — 1 handler |
| `plastic_promise/mcp/tools/management.py` | Update — 1 handler |
| `rust/context-engine-core/src/retrieval/mod.rs` | Update — add text fallback in retrieve() when LanceDB fails |

## 5. Acceptance Criteria

1. `python -c "from plastic_promise.embedder import get_embedder; e=get_embedder(); v=e.embed('test'); assert len(v)==1024"` PASS
2. `memory_recall` handler returns valid ContextPack JSON with core/related/divergent layers
3. `memory_store` → `memory_recall` round-trip: store a test memory, recall it back with score > 0
4. All 16 handlers return valid JSON (no `pass`, no traceback)
5. `context_supply` embeds query text and passes vector to engine.supply()
6. HybridRetriever falls back to text match when LanceDB returns Err

## 6. Out of Scope

- LanceDB actual linking (waiting on VS BuildTools C++ workload)
- audit_run, audit_report, defense_trust (need LLM-based scoring)
- scarf_reflect, inertia_check (need SCARF engine)
- system_backup, system_migrate (need file I/O)
- Adaptive Retrieval, Noise Filter, Smart Extraction (Phase B)
- Cross-encoder Reranking (Phase B)
- Any P2 upper-layer Python modules
