# Sub-Project B: P1 Retrieval Enhancements + Cron Recovery

> Date: 2026-06-28
> Status: draft
> Scope: Adaptive Retrieval, Noise Filter, Smart Extraction, Cross-Encoder Rerank, Cron 守护

## 1. Goal

在子项目 A 打通端到端链路后，增强检索质量和系统自治能力：
- 避免不必要的 embedding 调用（Adaptive Retrieval）
- 防止低质量记忆污染记忆池（Noise Filter）
- 自动从对话中提取结构化记忆（Smart Extraction）
- 提升检索精度（Cross-Encoder Rerank）
- 恢复周期性自检能力（Cron 守护）

## 2. Module Designs

### 2.1 Adaptive Retrieval

**File:** `plastic_promise/adaptive_retrieval.py`

```python
def should_retrieve(query: str) -> bool:
    """Return True if the query warrants memory retrieval."""
```

Rules (in priority order):
1. **Force-retrieve**: keywords `记得|recall|之前|上次|上次|memory|回忆|上次|previously|last time` → True
2. **Skip patterns**: greetings (`^hi\b|^hello|^你好|^hey`), slash commands (`^/`), pure emoji, ≤6 CJK chars non-question, ≤15 ASCII chars non-question → False
3. **Default**: question mark present → True, CJK ≥8 chars → True, else → False

Integration: called in `handle_memory_recall` BEFORE `get_embedder().embed()`.

### 2.2 Noise Filter

**File:** `plastic_promise/noise_filter.py`

```python
def is_noise(text: str) -> bool:
    """Return True if text is low-quality and should not be stored."""
```

Pattern groups:
- DENIAL: `I don't have|我没有|无法提供|没有相关` → noise
- META_QUESTION: `你还记得吗|do you remember|你知道.*吗` → noise
- SHORT_BOILERPLATE: greetings/thanks ≤10 chars → noise

Integration: called in `handle_memory_store` BEFORE storing.

### 2.3 Smart Extraction

**File:** `plastic_promise/smart_extractor.py`

```python
@dataclass
class ExtractedMemory:
    category: str           # preference|fact|decision|entity|event|pattern
    l0_abstract: str        # one-sentence index (≤80 chars)
    l1_summary: str         # structured summary (≤300 chars)
    l2_content: str         # full original text
    importance: float
    confidence: float       # 0.0-1.0 extraction confidence

def extract_memories(conversation: str, ollama_model: str = "qwen2.5:3b") -> list[ExtractedMemory]:
    """Extract structured memories from conversation text."""
```

Pipeline:
1. **Rule-based pre-filter**: keyword matching per category:
   - preference: `喜欢|不喜欢|prefer|讨厌|习惯`
   - fact: `是|was|位于|has|知道|了解`
   - decision: `决定|decided|选择|chose|确定`
   - entity: `项目|project|代码|repo|文件|file`
   - event: `完成了|finished|部署了|deployed|发布了`
   - pattern: `总是|always|通常|usually|每次|每次`
2. **Confidence scoring**: ratio of matched keywords to total category keywords
3. **LLM fallback**: if confidence < 0.7, send to Ollama for classification
4. **Dedup**: cosine similarity ≥0.7 against existing memories → MERGE or SKIP

Integration: called by a new MCP tool or by `memory_store` when `source="conversation_extract"`.

### 2.4 Cross-Encoder Rerank

**File:** `plastic_promise/reranker.py`

```python
def cross_encode_rerank(
    query: str,
    candidates: list[tuple[str, str, float]],  # [(id, content, score)]
    ollama_model: str = "qwen2.5:3b",
    top_k: int = 10,
) -> list[tuple[str, float]]:
    """Rerank candidates using local Ollama cross-encoding."""
```

Pipeline:
1. Build prompt: "Rate relevance 0-100 for query against each passage"
2. Call Ollama chat API with 5s timeout
3. Parse scores, blend: `final = 0.6 * ce_score + 0.4 * original_score`
4. On failure: return original order unchanged

Integration: called in `handle_memory_recall` between `engine.supply()` and JSON formatting.

### 2.5 Cron Recovery

**Files:**
- `plastic_promise/cron/soul_closure_guardian.py`
- `plastic_promise/cron/health_scan.py`
- `plastic_promise/cron/audit_daily.py`

**soul_closure_guardian** (every 60 min):
- Query SQLite: memories with `tier='working'` AND `last_accessed_at > 24h ago`
- Report: count of stale working memories → MCP `system_stats` or log

**health_scan** (every 6h):
- Check: SQLite connection, LanceDB stats, EntityGraph node/edge count
- Check: Ollama reachable via `/api/tags`
- Report: aggregated health JSON

**audit_daily** (every 24h):
- Aggregate: memory stats, worth distribution, tier distribution
- Generate: markdown summary
- Store: as a memory with `category='reflection'` and `source='audit_daily'`

All cron jobs run via Python scripts, invoked by system scheduler or Claude Code's cron mechanism.

## 3. Acceptance Criteria

1. `should_retrieve("你好")` → False, `should_retrieve("记得上次架构决策？")` → True
2. `is_noise("I don't have that information")` → True, `is_noise("Rust is fast")` → False
3. `extract_memories("用户喜欢用Rust写后端")` → at least 1 ExtractedMemory with category='preference'
4. `cross_encode_rerank("query", [("a","text",0.8)], "qwen2.5:3b")` → returns list, never panics
5. All 3 cron scripts importable, each has a `run()` function
6. `memory_store` handler calls `is_noise()` before writing
7. `memory_recall` handler calls `should_retrieve()` before embedding

## 4. Out of Scope

- Pulling Ollama generation model (qwen2.5:3b) — user handles separately
- Actual cron scheduling (system-level) — scripts only, scheduling via Claude Code or OS
- SCARF self-reflection (P2)
- 7-dimension full audit (P2)
- CEI index calculation (P2)
- Trust score system (P2)
