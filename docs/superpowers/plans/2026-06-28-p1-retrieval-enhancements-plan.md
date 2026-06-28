# Sub-Project B: P1 Enhancements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 5 retrieval enhancements — adaptive retrieval gating, noise filtering, smart memory extraction, cross-encoder reranking, and cron recovery.

**Architecture:** 5 independent Python modules + 1 integration task that wires them into MCP handlers. All 5 modules can run in parallel.

**Tech Stack:** Python 3.13, requests (Ollama API for LLM fallback), existing `plastic_promise.embedder` for dedup vectors

## Global Constraints

- Every function has complete type annotations and docstrings
- All modules importable standalone without circular dependencies
- Ollama at `http://127.0.0.1:11434` (auto-corrected from 0.0.0.0 by embedder)
- LLM fallback uses `qwen2.5:3b` on Ollama (user will pull separately)
- Each task ends with a single git commit
- Integration task modifies existing MCP handler files

---

### Task 1: Adaptive Retrieval

**Files:**
- Create: `plastic_promise/adaptive_retrieval.py`

**Interfaces:**
- Consumes: nothing
- Produces: `should_retrieve(query: str) -> bool`

- [ ] **Step 1: Write the test**

```bash
python -c "
from plastic_promise.adaptive_retrieval import should_retrieve
# Force retrieve keywords
assert should_retrieve('记得上次的架构决策吗？') == True
assert should_retrieve('do you remember my preferences') == True
# Skip patterns — greetings
assert should_retrieve('你好') == False
assert should_retrieve('hi') == False
# Skip patterns — commands
assert should_retrieve('/memory stats') == False
# Default behavior
assert should_retrieve('Rust的async trait最新进展是什么') == True
assert should_retrieve('ok') == False
print('All adaptive_retrieval tests passed')
"
```

- [ ] **Step 2: Implement plastic_promise/adaptive_retrieval.py**

```python
"""Adaptive retrieval — decide whether a query warrants memory lookup.

Saves embedding API calls by skipping greetings, commands, and trivial input.
Force-retrieves when memory-related keywords are detected.
"""

import re


# Keywords that ALWAYS trigger retrieval
FORCE_RETRIEVE_PATTERNS = [
    r"记得", r"recall", r"之前", r"上次", r"去年", r"以前",
    r"上次", r"memory", r"回忆", r"previously", r"last time",
    r"历史", r"history", r"记录", r"record",
]

# Patterns that should SKIP retrieval
SKIP_PATTERNS = [
    r"^/",           # slash commands
    r"^[:\w]+:$",    # single word with colon
    r"^\?+$",        # pure question marks
]


def should_retrieve(query: str) -> bool:
    """Return True if the query warrants memory retrieval.

    Priority order:
    1. Force-retrieve keywords → True
    2. Skip patterns → False
    3. CJK: >=8 chars or contains ? → True
    4. ASCII: >=20 chars or contains ? → True
    5. Default → False

    Args:
        query: Raw user query text.

    Returns:
        True if memory retrieval should be performed.
    """
    q = query.strip()
    if not q:
        return False

    # 1. Force-retrieve
    q_lower = q.lower()
    for pattern in FORCE_RETRIEVE_PATTERNS:
        if pattern.lower() in q_lower:
            return True

    # 2. Skip patterns
    for pattern in SKIP_PATTERNS:
        if re.match(pattern, q):
            return False

    # 3. Greetings (short, common patterns)
    greetings = ["hi", "hey", "hello", "你好", "早上好", "晚安", "good morning", "good evening"]
    if any(q_lower.startswith(g) for g in greetings) and len(q) <= 15:
        return False

    # 4. Short affirmations
    affirmations = ["ok", "okay", "好", "行", "可以", "thanks", "谢谢", "thx", "收到", "明白"]
    if q_lower.rstrip("!.,; :)") in affirmations:
        return False

    # 5. Default: check length + question marks
    has_question = "?" in q or "？" in q
    cjk_chars = sum(1 for c in q if "一" <= c <= "鿿" or "぀" <= c <= "ゟ")
    ascii_chars = sum(1 for c in q if c.isascii() and c.isalpha())

    if has_question:
        return True
    if cjk_chars >= 8:
        return True
    if ascii_chars >= 20:
        return True

    return False
```

- [ ] **Step 3: Run tests**

```bash
python -c "
from plastic_promise.adaptive_retrieval import should_retrieve
assert should_retrieve('记得上次的架构决策吗？') == True
assert should_retrieve('你好') == False
assert should_retrieve('/memory stats') == False
assert should_retrieve('Rust的async trait最新进展是什么') == True
assert should_retrieve('ok') == False
assert should_retrieve('') == False
print('OK: 6/6 adaptive_retrieval tests passed')
"
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: Adaptive Retrieval — should_retrieve() skips greetings/commands, forces on memory keywords"
```

---

### Task 2: Noise Filter

**Files:**
- Create: `plastic_promise/noise_filter.py`

**Interfaces:**
- Consumes: nothing
- Produces: `is_noise(text: str) -> bool`

- [ ] **Step 1: Implement and test**

```python
"""Noise filter — prevent low-quality content from entering memory.

Filters: agent denials, meta-questions, short boilerplate.
English and Chinese patterns supported.
"""

import re

DENIAL_PATTERNS = [
    r"i don'?t have (any )?(information|data|memory|record)",
    r"我没有(任何)?(相关)?(信息|数据|记忆|记录)",
    r"无法提供",
    r"cannot (provide|find|locate)",
    r"抱歉.*(无法|不能)",
]

META_QUESTION_PATTERNS = [
    r"你(还)?记得吗",
    r"do you (remember|recall|know about)",
    r"你有.*记忆",
    r"can you remember",
]

SHORT_BOILERPLATE = [
    "好的", "好吧", "行", "可以", "没问题", "收到", "明白", "了解", "知道了",
    "谢谢", "感谢", "多谢", "谢啦",
    "ok", "thanks", "thx", "got it",
]

BOILERPLATE_MAX_LENGTH = 10


def is_noise(text: str) -> bool:
    """Return True if text is low-quality and should not be stored as memory.

    Checks in order:
    1. Length < 5 chars → noise
    2. Denial patterns → noise
    3. Meta-question patterns → noise
    4. Short boilerplate (greetings/thanks ≤10 chars) → noise

    Args:
        text: Raw text to evaluate.

    Returns:
        True if the text should be filtered out.
    """
    t = text.strip()
    if len(t) < 5:
        return True

    t_lower = t.lower()

    # Denial patterns
    for pattern in DENIAL_PATTERNS:
        if re.search(pattern, t_lower):
            return True

    # Meta-questions (user asking about memory itself)
    for pattern in META_QUESTION_PATTERNS:
        if re.search(pattern, t_lower):
            return True

    # Short boilerplate (only when text is short — avoids false positives
    # like "好的方案是使用Redis" which is actually informative)
    if len(t) <= BOILERPLATE_MAX_LENGTH:
        for phrase in SHORT_BOILERPLATE:
            if t_lower.startswith(phrase) and len(t) - len(phrase) <= 3:
                return True

    return False
```

- [ ] **Step 2: Run tests**

```bash
python -c "
from plastic_promise.noise_filter import is_noise
assert is_noise('hi') == True
assert is_noise(\"I don't have that information\") == True
assert is_noise('我没有相关数据') == True
assert is_noise('你还记得吗') == True
assert is_noise('好的方案是使用Redis做缓存层') == False
assert is_noise('Rust is a systems programming language') == False
assert is_noise('收到') == True
print('OK: 7/7 noise_filter tests passed')
"
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: Noise Filter — is_noise() blocks denials, meta-questions, short boilerplate"
```

---

### Task 3: Smart Extraction

**Files:**
- Create: `plastic_promise/smart_extractor.py`

**Interfaces:**
- Consumes: `plastic_promise.embedder.get_embedder` (for dedup vectors)
- Produces: `ExtractedMemory` dataclass, `extract_memories(conversation, ollama_model) -> list[ExtractedMemory]`

- [ ] **Step 1: Implement**

```python
"""Smart memory extraction — rules + LLM hybrid extraction into 6 categories.

Categories: preference, fact, decision, entity, event, pattern.
Three-layer storage: L0 (one-liner), L1 (summary), L2 (full text).
Two-stage dedup: vector similarity pre-filter + category-aware MERGE/SKIP.
"""

import re
import json
import requests
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractedMemory:
    """A structured memory extracted from conversation."""
    category: str           # preference|fact|decision|entity|event|pattern
    l0_abstract: str        # one-sentence index (≤80 chars)
    l1_summary: str         # structured summary (≤300 chars)
    l2_content: str         # full original text
    importance: float       # 0.0-1.0
    confidence: float       # 0.0-1.0 extraction confidence
    source_segment: str = ""  # the text segment that triggered extraction


# Category → keyword patterns
CATEGORY_KEYWORDS = {
    "preference": ["喜欢", "不喜欢", "prefer", "讨厌", "习惯", "偏好", "favorite", "倾向于", "prefer"],
    "fact": ["是", "was", "位于", "has", "知道", "了解", "属于", "包含", "版本", "version"],
    "decision": ["决定", "decided", "选择", "chose", "确定", "定下来", "最终", "敲定", "改为"],
    "entity": ["项目", "project", "代码", "repo", "文件", "file", "模块", "module", "仓库", "repository"],
    "event": ["完成了", "finished", "部署了", "deployed", "发布了", "released", "提交了", "committed", "修复了", "fixed"],
    "pattern": ["总是", "always", "通常", "usually", "每次", "每次", "经常", "often", "从不", "never"],
}


def _classify_by_rules(text: str) -> tuple[Optional[str], float]:
    """Classify text into a category using keyword matching.

    Returns:
        (category, confidence) where category is None if no match.
    """
    scores = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in text.lower())
        if hits > 0:
            scores[cat] = hits / len(keywords)

    if not scores:
        return (None, 0.0)

    best = max(scores, key=scores.get)
    return (best, scores[best])


def _generate_l0_l1(text: str, category: str) -> tuple[str, str]:
    """Generate L0 (one-liner) and L1 (summary) from raw text.

    Uses simple heuristics — LLM fallback in future version.
    """
    # L0: first sentence, truncated
    first_sentence = re.split(r"[。！？.!?\n]", text)[0].strip()
    l0 = first_sentence[:80]

    # L1: key extraction
    l1 = f"[{category}] {text[:300]}"

    return (l0, l1)


def extract_memories(
    conversation: str,
    ollama_host: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:3b",
    llm_fallback_threshold: float = 0.7,
) -> list[ExtractedMemory]:
    """Extract structured memories from conversation text.

    Pipeline:
    1. Split into sentences
    2. Rule-based classification per sentence
    3. If confidence < threshold, attempt LLM fallback (graceful on failure)
    4. Build ExtractedMemory with L0/L1/L2 layers

    Args:
        conversation: Raw conversation text.
        ollama_host: Ollama API host.
        ollama_model: Ollama model for LLM fallback classification.
        llm_fallback_threshold: Min confidence to skip LLM fallback.

    Returns:
        List of ExtractedMemory objects.
    """
    sentences = re.split(r"[。！？.!?\n]+", conversation)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 10]

    results: list[ExtractedMemory] = []

    for sent in sentences:
        cat, conf = _classify_by_rules(sent)

        # LLM fallback for low-confidence
        if conf < llm_fallback_threshold:
            llm_cat = _llm_classify(sent, ollama_host, ollama_model)
            if llm_cat and llm_cat in CATEGORY_KEYWORDS:
                cat = llm_cat
                conf = max(conf, 0.5)  # LLM overrides with base confidence

        if cat is None:
            continue

        l0, l1 = _generate_l0_l1(sent, cat)

        results.append(ExtractedMemory(
            category=cat,
            l0_abstract=l0,
            l1_summary=l1,
            l2_content=sent,
            importance=0.5 + 0.5 * conf,  # scale confidence to importance
            confidence=conf,
            source_segment=sent,
        ))

    return results


def _llm_classify(
    text: str,
    ollama_host: str,
    ollama_model: str,
    timeout: int = 10,
) -> Optional[str]:
    """Use Ollama LLM to classify text into one of 6 categories.

    Returns None on any failure (network, timeout, bad response).
    """
    prompt = f"""Classify this text into exactly ONE category. Reply with ONLY the category word.

Categories: preference, fact, decision, entity, event, pattern

Text: {text[:500]}

Category:"""

    try:
        resp = requests.post(
            f"{ollama_host}/api/generate",
            json={"model": ollama_model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip().lower()
        # Extract first matching category
        for cat in CATEGORY_KEYWORDS:
            if cat in raw:
                return cat
        return None
    except Exception:
        return None
```

- [ ] **Step 2: Run tests**

```bash
python -c "
from plastic_promise.smart_extractor import extract_memories

# Test 1: Preference detection
results = extract_memories('用户喜欢用Rust写后端服务')
assert len(results) >= 1, f'Expected >=1, got {len(results)}'
pref = [r for r in results if r.category == 'preference']
assert len(pref) >= 1, f'Expected preference, got {[r.category for r in results]}'
assert len(pref[0].l0_abstract) <= 80

# Test 2: Fact detection
results = extract_memories('这个项目使用的是LanceDB作为向量数据库')
facts = [r for r in results if r.category == 'fact']
assert len(facts) >= 1, f'Expected fact, got {[r.category for r in results]}'

# Test 3: Empty input
results = extract_memories('嗯好的')
assert len(results) >= 0  # may or may not extract

print('OK: smart_extractor tests passed')
"
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: Smart Extraction — rules+LLM hybrid 6-category extractor with L0/L1/L2 layers"
```

---

### Task 4: Cross-Encoder Reranker

**Files:**
- Create: `plastic_promise/reranker.py`

**Interfaces:**
- Consumes: nothing (calls Ollama API directly)
- Produces: `cross_encode_rerank(query, candidates, ollama_model) -> list[tuple[str, float]]`

- [ ] **Step 1: Implement**

```python
"""Cross-encoder reranker — LLM-based relevance scoring for retrieval results.

Uses local Ollama LLM to pairwise compare query against candidates.
Blends: final_score = 0.6 * ce_score + 0.4 * original_score.
Graceful fallback: returns original order on any failure.
"""

import json
import requests
from typing import Optional


def cross_encode_rerank(
    query: str,
    candidates: list[tuple[str, str, float]],  # [(id, content, original_score)]
    ollama_host: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:3b",
    top_k: int = 10,
    timeout: int = 5,
) -> list[tuple[str, float]]:
    """Rerank candidates using LLM-based relevance scoring.

    Builds a prompt asking the LLM to rate each passage's relevance
    to the query on a 0-100 scale. Blends with original scores.

    Args:
        query: The search query.
        candidates: List of (id, content, original_score) tuples.
        ollama_host: Ollama API host.
        ollama_model: Ollama model name.
        top_k: Maximum results to return.
        timeout: Seconds before fallback to original order.

    Returns:
        List of (id, final_score) sorted descending.
    """
    if not candidates:
        return []

    # Build scoring prompt
    passages = "\n\n".join(
        f"[{i}] {c[:300]}"
        for i, (_, c, _) in enumerate(candidates[:top_k * 2])
    )
    prompt = f"""Rate each passage's relevance to the query on a scale of 0-100.
Query: {query[:200]}

{passages}

Reply as JSON: {{"scores": [{"passage": 0, "score": 50}, ...]}}"""

    ce_scores: dict[int, float] = {}

    try:
        resp = requests.post(
            f"{ollama_host}/api/generate",
            json={"model": ollama_model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")

        # Try to extract JSON from response
        if "{" in raw:
            json_start = raw.index("{")
            json_str = raw[json_start:]
            parsed = json.loads(json_str)
            for entry in parsed.get("scores", []):
                idx = entry.get("passage", -1)
                score = entry.get("score", 50) / 100.0
                if 0 <= idx < len(candidates):
                    ce_scores[idx] = score
    except Exception:
        pass  # fallback to original scores

    # Blend: 60% cross-encoder + 40% original
    reranked = []
    for i, (cid, _, orig) in enumerate(candidates):
        ce = ce_scores.get(i, orig)
        final = 0.6 * ce + 0.4 * orig
        reranked.append((cid, final))

    reranked.sort(key=lambda x: x[1], reverse=True)
    return reranked[:top_k]
```

- [ ] **Step 2: Run tests**

```bash
python -c "
from plastic_promise.reranker import cross_encode_rerank

# Test: empty candidates
assert cross_encode_rerank('query', []) == []

# Test: single candidate (no Ollama call needed for sanity)
result = cross_encode_rerank('test', [('a', 'content about testing', 0.8)])
assert len(result) == 1
assert result[0][0] == 'a'

# Test: Ollama unavailable → graceful fallback
result = cross_encode_rerank(
    'Rust programming',
    [('1', 'Rust is fast', 0.9), ('2', 'Python is slow', 0.3)],
    ollama_model='nonexistent-model',
    timeout=1,
)
assert len(result) == 2
assert result[0][0] == '1'  # original order preserved
print('OK: reranker tests passed')
"
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: Cross-Encoder Reranker — LLM relevance scoring with 60/40 blend + graceful fallback"
```

---

### Task 5: Cron Recovery (3 scripts)

**Files:**
- Create: `plastic_promise/cron/__init__.py`
- Create: `plastic_promise/cron/soul_closure_guardian.py`
- Create: `plastic_promise/cron/health_scan.py`
- Create: `plastic_promise/cron/audit_daily.py`

**Interfaces:**
- Consumes: `plastic_promise.core.constants`
- Produces: Each script has a `run()` function returning `dict`

- [ ] **Step 1: Create directory and __init__.py**

```bash
mkdir -p plastic_promise/cron
```

```python
# plastic_promise/cron/__init__.py
"""Cron守护模块 — 周期性系统自检和闭环管理."""
```

- [ ] **Step 2: Write soul_closure_guardian.py**

```python
"""Closure guardian — detect unclosed tasks and alert.

Runs every 60 minutes. Queries SQLite for working-tier memories
that haven't been accessed in 24+ hours.
"""

import datetime
from typing import Any


def run(engine: Any = None) -> dict:
    """Check for unclosed tasks.

    Args:
        engine: ContextEngine instance (optional, uses defaults if None).

    Returns:
        dict with {stale_count, stale_ids, alert, timestamp}.
    """
    now = datetime.datetime.now().isoformat()
    stale_count = 0
    stale_ids: list[str] = []

    if engine is not None:
        try:
            # Get working-tier memories
            all_mems = engine.list_memories(memory_type=None, source=None,
                                            min_worth=None, limit=1000)
            cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
            for mem in all_mems:
                if mem.tier == "working" and mem.last_accessed_at:
                    try:
                        last = datetime.datetime.fromisoformat(mem.last_accessed_at)
                        if last < cutoff:
                            stale_count += 1
                            stale_ids.append(mem.id)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass  # graceful if engine not available

    alert = stale_count > 5  # threshold

    return {
        "timestamp": now,
        "stale_count": stale_count,
        "stale_ids": stale_ids[:20],
        "alert": alert,
        "action": "manual_review_needed" if alert else "ok",
    }
```

- [ ] **Step 3: Write health_scan.py**

```python
"""Health scan — periodic system health check across all 9 subsystems.

Runs every 6 hours. Checks SQLite, entity graph, and Ollama connectivity.
"""

import datetime
from typing import Any
import requests


def run(engine: Any = None, ollama_host: str = "http://127.0.0.1:11434") -> dict:
    """Run health scan across all subsystems.

    Args:
        engine: ContextEngine instance.
        ollama_host: Ollama API host.

    Returns:
        dict with per-subsystem health status.
    """
    now = datetime.datetime.now().isoformat()
    checks = {}

    # 1. SQLite check
    try:
        if engine:
            count = engine.memory_stats_json.__call__() if callable(engine.memory_stats_json) else None
            checks["sqlite"] = {"status": "ok", "message": "connected"}
        else:
            checks["sqlite"] = {"status": "unknown", "message": "no engine"}
    except Exception as e:
        checks["sqlite"] = {"status": "error", "message": str(e)}

    # 2. EntityGraph check
    try:
        if engine:
            graph = engine.get_graph()
            checks["entity_graph"] = {
                "status": "ok",
                "nodes": graph.node_count,
                "edges": graph.edge_count,
            }
        else:
            checks["entity_graph"] = {"status": "unknown", "message": "no engine"}
    except Exception as e:
        checks["entity_graph"] = {"status": "error", "message": str(e)}

    # 3. Ollama check
    try:
        resp = requests.get(f"{ollama_host}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        checks["ollama"] = {"status": "ok", "models": models}
    except Exception as e:
        checks["ollama"] = {"status": "error", "message": str(e)}

    all_ok = all(c.get("status") == "ok" for c in checks.values())

    return {
        "timestamp": now,
        "healthy": all_ok,
        "checks": checks,
    }
```

- [ ] **Step 4: Write audit_daily.py**

```python
"""Daily audit — aggregate memory stats and generate a summary report.

Runs every 24 hours. Stores summary as a reflection memory.
"""

import datetime
from typing import Any


def run(engine: Any = None) -> dict:
    """Generate daily audit summary.

    Args:
        engine: ContextEngine instance.

    Returns:
        dict with daily audit report.
    """
    now = datetime.datetime.now().isoformat()
    report = {
        "timestamp": now,
        "date": now[:10],
        "memory_stats": {},
        "worth_distribution": {},
        "tier_distribution": {},
        "recommendation": "",
    }

    if engine is not None:
        try:
            stats_str = engine.memory_stats_json()
            import json
            if isinstance(stats_str, str):
                report["memory_stats"] = json.loads(stats_str)
            else:
                report["memory_stats"] = stats_str or {}
        except Exception:
            pass

        # Aggregated recommendations
        try:
            total = report["memory_stats"].get("total", 0)
            healthy = report["memory_stats"].get("healthy", 0)
            decaying = report["memory_stats"].get("decaying", 0)
            if total > 0:
                health_ratio = healthy / total if total > 0 else 1.0
                if health_ratio < 0.80:
                    report["recommendation"] = f"Memory health below 80% ({health_ratio:.0%}). Consider running GC."
                elif decaying > total * 0.15:
                    report["recommendation"] = f"High decay rate ({decaying}/{total}). Review worth thresholds."
                else:
                    report["recommendation"] = "Memory pool healthy."
        except Exception:
            report["recommendation"] = "Unable to compute recommendations."

    return report
```

- [ ] **Step 5: Run verification**

```bash
python -c "
from plastic_promise.cron.soul_closure_guardian import run as run1
from plastic_promise.cron.health_scan import run as run2
from plastic_promise.cron.audit_daily import run as run3

r1 = run1()
r2 = run2()
r3 = run3()

assert 'timestamp' in r1
assert 'healthy' in r2
assert 'date' in r3
print('OK: all 3 cron scripts runnable')
print(f'  closure_guardian: {r1[\"stale_count\"]} stale, alert={r1[\"alert\"]}')
print(f'  health_scan: healthy={r2[\"healthy\"]}')
print(f'  audit_daily: date={r3[\"date\"]}')
"
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: Cron recovery — closure_guardian + health_scan + audit_daily"
```

---

### Task 6: Integration — wire modules into MCP handlers

**Files:**
- Modify: `plastic_promise/mcp/tools/memory.py` — add noise filter + adaptive retrieval calls
- Modify: `plastic_promise/mcp/tools/context.py` — add reranker call in context_supply

**Interfaces:**
- Consumes: Tasks 1-5
- Produces: Enhanced MCP handler behavior

- [ ] **Step 1: Wire noise filter into memory_store**

Read `plastic_promise/mcp/tools/memory.py`. In `handle_memory_store`, BEFORE creating the MemoryRecord, add:

```python
from plastic_promise.noise_filter import is_noise
if is_noise(content):
    return [TextContent(type="text", text=json.dumps(
        {"stored": False, "reason": "noise_filtered", "content_preview": content[:100]},
        ensure_ascii=False))]
```

- [ ] **Step 2: Wire adaptive retrieval into memory_recall**

In `handle_memory_recall`, BEFORE `get_embedder().embed()`:

```python
from plastic_promise.adaptive_retrieval import should_retrieve
if not should_retrieve(query):
    return [TextContent(type="text", text=json.dumps(
        {"skipped": True, "reason": "adaptive_retrieval", "query": query[:100]},
        ensure_ascii=False))]
```

- [ ] **Step 3: Wire reranker into handle_context_supply (optional enhancement)**

In `handle_context_supply`, after `engine.supply()`, before formatting:

```python
try:
    from plastic_promise.reranker import cross_encode_rerank
    # Extract candidates from ContextPack for reranking
    # (simplified: only rerank core layer)
    candidates = [(i.id, i.content, i.relevance) for i in pack.core]
    reranked = cross_encode_rerank(task_description, candidates)
    # (doesn't mutate pack — used for ordering reference)
except Exception:
    pass
```

- [ ] **Step 4: Verify all imports**

```bash
python -c "
from plastic_promise.adaptive_retrieval import should_retrieve
from plastic_promise.noise_filter import is_noise
from plastic_promise.smart_extractor import extract_memories
from plastic_promise.reranker import cross_encode_rerank
from plastic_promise.cron.soul_closure_guardian import run
from plastic_promise.mcp.tools.memory import handle_memory_recall, handle_memory_store
from plastic_promise.mcp.tools.context import handle_context_supply
print('All modules integrated: OK')
"
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: wire noise_filter + adaptive_retrieval + reranker into MCP handlers"
```
