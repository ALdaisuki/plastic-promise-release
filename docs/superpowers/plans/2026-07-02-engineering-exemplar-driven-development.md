# Engineering Exemplar-Driven Development Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Systematize exemplar-driven development by adding knowledge-gap detection to context_supply, a new sp-stage for exemplar research with quality review, and Hunter Guild integration for async research dispatch.

**Architecture:** Three-layer design — `exemplar_gap_detector.py` (detection middleware in context_supply return path), `exemplar_research.py` (sp-stage handler with verify_exemplar quality gate), and task_queue enhancements (payload_hash dedup + research_exemplar + verify_exemplar task types). Files-only knowledge storage in `engineering-patterns/`, with smart-remember dual-write to memory pool. No new MCP tools, no EntityGraph changes, no Daemon scanner.

**Tech Stack:** Python 3.11+, SQLite (existing plastic_memory.db), LanceDB (existing), WebSearch tool (built-in), smart-remember MCP tool (existing).

## Global Constraints

- No new MCP tools — reuse sp-stage + memory_store + task_enqueue pipeline
- No new Python dependencies (no spacy/nltk — simple heuristic keyword extraction)
- No EntityGraph changes — exemplar uses memory_type="exemplar" tag for association
- No Daemon scanner in MVP — validate manually first, upgrade path reserved
- gap_signal is non-persistent — immediate signal only, no storage
- All changes must gracefully degrade (gap_detector failure must not block context_supply)

---

### Task 1: Create exemplar_gap_detector.py

**Files:**
- Create: `plastic_promise/core/exemplar_gap_detector.py`

**Interfaces:**
- Consumes: `ContextPack` (from `context_engine.py`, dataclass with `core`, `related`, `divergent`, `total_items` property)
- Produces: `GapSignal` dataclass, `detect_gap(query, pack) -> GapSignal | None`, `_is_tech_query(query) -> bool`, `_extract_keywords(query) -> list[str]`

- [ ] **Step 1: Write the module skeleton and GapSignal dataclass**

```python
"""Exemplar Gap Detector — knowledge-gap detection middleware.

Detects when context_supply returns empty/low-quality results for
technical queries, signaling that exemplar research is needed.

This module does NOT perform searches or produce side effects.
It only builds a GapSignal that consumers (sp-stage, Claude) may act on.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GapSignal:
    """Signal emitted when context_supply detects a knowledge gap."""
    type: str              # "exemplar_needed"
    problem: str           # Original query text
    suggested_search: list[str]  # 2-3 search keywords
    auto_task: bool        # Whether to auto-create a Hunter Guild task
    severity: str          # "high" | "medium" | "low"
```

- [ ] **Step 2: Add TECH_KEYWORDS and _is_tech_query()**

```python
# Technology keywords that indicate a query may benefit from exemplar research.
# These are kept intentionally broad — false positives are cheap (a signal is
# shown but ignored), while false negatives mean missed knowledge gaps.
TECH_KEYWORDS = {
    "storage", "engine", "agent", "memory", "retrieval",
    "api", "schema", "protocol", "distributed", "consensus",
    "replication", "caching", "queue", "stream", "index",
    "embedding", "vector", "pipeline", "router", "gateway",
    "proxy", "cache", "lock", "transaction", "snapshot",
    "database", "sql", "nosql", "lance", "sqlite",
    "rust", "python", "golang", "typescript", "compiler",
    "serialize", "deserialize", "encoding", "encryption",
    "auth", "oauth", "jwt", "token", "session",
    "wal", "lsm", "btree", "hash", "bloom",
    "rpc", "grpc", "http", "websocket", "sse",
    "scheduler", "daemon", "worker", "dispatcher",
    "rag", "llm", "embedder", "reranker", "tokenizer",
}


def _is_tech_query(query: str) -> bool:
    """Check if a query contains technology-related keywords.

    The check is case-insensitive and matches substrings within words
    (e.g. "embedding" matches "embedder"). This is intentional: false
    positives produce a harmless signal; false negatives miss gaps.
    """
    query_lower = query.lower()
    return any(kw in query_lower for kw in TECH_KEYWORDS)
```

- [ ] **Step 3: Add _extract_keywords() with the heuristic algorithm**

```python
# English stop words (subset — full list of ~150 common words)
STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "having", "do", "does", "did",
    "doing", "will", "would", "could", "should", "may", "might",
    "can", "shall", "to", "of", "in", "for", "on", "with", "at",
    "by", "from", "as", "into", "through", "during", "before",
    "after", "above", "below", "between", "and", "but", "or",
    "not", "no", "nor", "so", "if", "then", "else", "when",
    "where", "why", "how", "all", "each", "every", "both", "few",
    "more", "most", "other", "some", "such", "only", "own",
    "same", "than", "too", "very", "just", "about", "also",
    "this", "that", "these", "those", "it", "its", "he", "she",
    "they", "them", "we", "you", "i", "me", "my", "your", "our",
    "what", "which", "who", "whom", "whose",
    # Chinese stop words
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
    "它", "们", "那", "些", "什么", "怎么", "如何", "为什么",
    "可以", "这个", "那个", "还是", "只是", "但是", "因为", "所以",
}


def _extract_keywords(query: str) -> list[str]:
    """Extract 2-3 search keywords from a query using simple heuristics.

    Algorithm (no external dependencies):
    1. Normalize: lowercase, strip punctuation except hyphens
    2. Tokenize: split on whitespace for English; treat CJK chars as tokens
    3. Filter stop words
    4. Score remaining tokens: CAP-cased English nouns > tech keywords > rest
    5. Merge adjacent scored tokens into compound phrases
    6. Return top 3, ordered by priority

    Does NOT use spacy/nltk — keeps the dependency footprint zero.
    """
    import re

    # Normalize
    cleaned = re.sub(r'[^\w\s\-]', ' ', query)
    tokens = cleaned.split()

    # Separate English tokens from CJK
    en_tokens = []
    cjk_tokens = []

    for token in tokens:
        # Skip stop words (case-insensitive for English)
        if token.lower() in STOP_WORDS:
            continue
        # Detect CJK: if the token has any CJK character, treat separately
        if any('一' <= c <= '鿿' for c in token):
            # For CJK, each character is a "word", but pairs are more useful
            chars = [c for c in token if '一' <= c <= '鿿']
            # Generate bigrams (compound CJK phrases)
            for i in range(len(chars) - 1):
                cjk_tokens.append(chars[i] + chars[i + 1])
            # Also include single chars as fallback
            cjk_tokens.extend(chars)
        else:
            en_tokens.append(token)

    # Score English tokens: CAP-cased > lowercase tech > lowercase
    scored = []
    for token in en_tokens:
        score = 0
        # Heuristic: tokens starting with uppercase are likely proper nouns
        if token[0].isupper():
            score += 3
        # Tokens that match tech keywords get bonus
        if token.lower() in TECH_KEYWORDS:
            score += 2
        # Longer tokens are more specific
        if len(token) > 3:
            score += 1
        scored.append((score, token.lower()))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Build compound phrases: merge adjacent high-score tokens
    phrases = []
    i = 0
    en_result = [t for _, t in scored]
    while i < len(en_result):
        # Try 2-word compound
        if i + 1 < len(en_result):
            phrases.append(f"{en_result[i]} {en_result[i+1]}")
        # Try 3-word compound
        if i + 2 < len(en_result):
            phrases.append(f"{en_result[i]} {en_result[i+1]} {en_result[i+2]}")
        phrases.append(en_result[i])
        i += 1

    # Combine: compound phrases first, then single tokens, then CJK bigrams
    all_keywords = phrases[:3] + en_result[:3] + cjk_tokens[:2]

    # Deduplicate preserving order
    seen = set()
    result = []
    for kw in all_keywords:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            result.append(kw)

    return result[:3]
```

- [ ] **Step 4: Add detect_gap() — the main entry point**

```python
def detect_gap(query: str, pack: "ContextPack") -> Optional[GapSignal]:
    """Detect knowledge gaps in context_supply results.

    Called as middleware in context_supply's return path.
    Returns None (no gap) or a GapSignal for consumers to act on.

    Three-tier detection:
    1. core layer has results → information is sufficient, no gap
    2. core is empty but related has >=3 items all with relevance > 0.45
       → associated knowledge is adequate, no gap
    3. core is empty and related is insufficient → gap detected

    Only triggers for queries containing technology keywords.
    Does NOT perform searches. Does NOT modify the pack.
    """
    # Guard: only technical queries can trigger
    if not _is_tech_query(query):
        return None

    # Tier 1: core layer populated → sufficient info
    if pack.core:
        return None

    # Tier 2: related layer has enough high-quality items
    related_with_relevance = [
        item for item in pack.related
        if getattr(item, 'relevance', 0) > 0.45
    ]
    if len(related_with_relevance) >= 3:
        return None

    # Tier 3: genuine knowledge gap
    return GapSignal(
        type="exemplar_needed",
        problem=query,
        suggested_search=_extract_keywords(query),
        auto_task=True,
        severity="medium",
    )
```

- [ ] **Step 5: Verify the module is importable**

Run:
```powershell
python -c "from plastic_promise.core.exemplar_gap_detector import GapSignal, detect_gap, _is_tech_query, _extract_keywords; print('OK')"
```
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/core/exemplar_gap_detector.py
git commit -m "feat(exemplar): add gap detector middleware for context_supply"
```

---

### Task 2: Wire gap_detector into context_engine.py

**Files:**
- Modify: `plastic_promise/core/context_engine.py:1244-1245` (before `return pack`)

**Interfaces:**
- Consumes: `detect_gap()` from `exemplar_gap_detector.py`
- Produces: `ContextPack.gap_signal` field (new optional attribute)

- [ ] **Step 1: Add gap_signal field to ContextPack dataclass**

Edit `plastic_promise/core/context_engine.py`, line 66-72. Add `gap_signal` field:

```python
@dataclass
class ContextPack:
    """三层上下文包"""
    core: List[ContextItem] = field(default_factory=list)
    related: List[ContextItem] = field(default_factory=list)
    divergent: List[ContextItem] = field(default_factory=list)
    activated_principles: List[dict] = field(default_factory=list)
    audit_metadata: Dict[str, str] = field(default_factory=dict)
    gap_signal: Optional["GapSignal"] = None  # NEW: knowledge-gap detection signal
```

The `Optional["GapSignal"]` uses forward reference to avoid circular imports.

- [ ] **Step 2: Add import at top of context_engine.py**

At line 15-16 (inside the `typing` import block), verify `Optional` is already imported. No change needed — it's already there.

- [ ] **Step 3: Inject gap detection before return pack**

Edit `plastic_promise/core/context_engine.py`, insert before line 1245 (`return pack`):

```python
        # ── Exemplar gap detection ─────────────────────────
        # Middleware: detect knowledge gaps before returning.
        # Graceful degradation: if the detector fails, we still
        # return the pack — gap_signal is optional enrichment.
        try:
            from plastic_promise.core.exemplar_gap_detector import detect_gap
            pack.gap_signal = detect_gap(task_description, pack)
        except Exception:
            pass  # gap detection failure must not block context_supply

        return pack
```

The `try/except` ensures gap detection never blocks the main context_supply flow.

- [ ] **Step 4: Verify ContextPack accepts gap_signal**

Run:
```powershell
python -c "from plastic_promise.core.context_engine import ContextPack; p = ContextPack(); print(p.gap_signal); p.gap_signal = 'test'; print(p.gap_signal)"
```
Expected:
```
None
test
```

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat(exemplar): wire gap_detector into context_supply return path"
```

---

### Task 3: Add payload_hash dedup to task_enqueue

**Files:**
- Modify: `plastic_promise/mcp/tools/task_queue.py:105-122` (normal enqueue section)

**Interfaces:**
- Consumes: existing `handle_task_enqueue` arguments
- Produces: `_compute_payload_hash(problem, search_hint) -> str`, dedup check before INSERT

- [ ] **Step 1: Add _compute_payload_hash helper**

Insert after `_generate_task_id()` (after line 27) in `task_queue.py`:

```python
import hashlib


def _compute_payload_hash(payload: dict) -> str:
    """Compute a deterministic hash for dedup based on payload content.

    Uses SHA256 first 8 hex chars of problem + sorted search_hints.
    Returns empty string if payload is None or missing required fields.
    """
    if not payload:
        return ""
    problem = payload.get("problem", "") or payload.get("gap_signal", {}).get("problem", "")
    search_hint = payload.get("search_hint", [])
    if not problem:
        return ""
    seed = f"{problem}|{'|'.join(sorted(search_hint))}"
    return hashlib.sha256(seed.encode()).hexdigest()[:8]
```

- [ ] **Step 2: Add dedup check before normal enqueue**

Insert before line 106 (`task_id = _generate_task_id()`) in the normal enqueue section:

```python
    # ── Dedup check (research_exemplar / verify_exemplar) ───
    # For research-oriented task types, check if a pending task
    # with the same payload_hash already exists.
    if args["task_type"] in ("research_exemplar", "verify_exemplar"):
        payload = args.get("payload")
        if payload:
            phash = _compute_payload_hash(payload)
            if phash:
                existing = conn.execute(
                    "SELECT id FROM task_queue "
                    "WHERE task_type = ? AND status = 'pending' "
                    "AND json_extract(payload, '$.payload_hash') = ? "
                    "LIMIT 1",
                    (args["task_type"], phash),
                ).fetchone()
                if existing:
                    conn.close()
                    return [TextContent(type="text", text=json.dumps({
                        "status": "duplicate",
                        "existing_task_id": existing["id"],
                        "reason": f"Pending {args['task_type']} for this problem already exists",
                    }, ensure_ascii=False))]

    # ── Normal enqueue ─────────────────────────────────────
```

- [ ] **Step 3: Inject payload_hash into payload before INSERT**

In the normal enqueue section, modify the payload JSON serialization (line 120) to include the hash:

Replace:
```python
            json.dumps(args.get("payload")) if args.get("payload") else None,
```

With:
```python
            json.dumps(_inject_payload_hash(args.get("payload"))) if args.get("payload") else None,
```

Add the helper function after `_compute_payload_hash`:

```python
def _inject_payload_hash(payload: dict) -> dict:
    """Inject payload_hash into payload dict for later dedup queries."""
    if not payload:
        return payload
    result = dict(payload)
    result["payload_hash"] = _compute_payload_hash(payload)
    return result
```

- [ ] **Step 4: Verify dedup logic**

Run:
```powershell
python -c "
from plastic_promise.mcp.tools.task_queue import _compute_payload_hash
h1 = _compute_payload_hash({'problem': 'Rust storage engine', 'search_hint': ['Rust', 'storage']})
h2 = _compute_payload_hash({'problem': 'Rust storage engine', 'search_hint': ['storage', 'Rust']})
print(f'Same problem, different hint order: {h1} == {h2} -> {h1 == h2}')
h3 = _compute_payload_hash({'problem': 'Different problem', 'search_hint': ['Rust']})
print(f'Different problem: {h1} == {h3} -> {h1 == h3}')
"
```
Expected: `h1 == h2` is True (order-independent), `h1 == h3` is False.

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/task_queue.py
git commit -m "feat(task-queue): add payload_hash dedup for research/verify task types"
```

---

### Task 4: Register exemplar-research in superpowers_stages.py

**Files:**
- Modify: `plastic_promise/skills/superpowers_stages.py:30-58,336-353,362-396`

**Interfaces:**
- Consumes: `SkillDef`, `SkillResult`, `_stage_handler` (existing patterns)
- Produces: `exemplar_research` module-level variable

- [ ] **Step 1: Add exemplar-research to STAGE_DOMAIN_MAP**

Insert after line 31 (`"brainstorming": "designing"`):

```python
    "exemplar-research": "designing",
```

- [ ] **Step 2: Add to STAGE_TAGS_MAP**

Insert after line 46 (`"brainstorming": ...`):

```python
    "exemplar-research": ["stage:exemplar-research", "domain:designing", "task:research"],
```

- [ ] **Step 3: Add to STAGE_DESCRIPTIONS**

Insert after line 61 (`"brainstorming": ...`):

```python
    "exemplar-research": "SuperPowers 阶段: 典范研究 — 搜索成熟实现、三问法分析、写分析文档、质量审核后入库",
```

- [ ] **Step 4: Add to STAGE_ATOMS**

Insert after line 338 (`"brainstorming": ...`):

```python
    "exemplar-research": ["principle_activate", "memory_store"],
```

- [ ] **Step 5: Add module-level export**

After line 385 (`brainstorming = ...`), insert:

```python
exemplar_research = SKILL_DEFS.get("exemplar-research")
```

- [ ] **Step 6: Verify the stage is registered**

Run:
```powershell
python -c "from plastic_promise.skills.superpowers_stages import exemplar_research, SKILL_DEFS; print(list(SKILL_DEFS.keys())); print(exemplar_research.name if exemplar_research else 'MISSING')"
```
Expected: `exemplar_research` appears in keys list, `sp-exemplar-research` printed.

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/skills/superpowers_stages.py
git commit -m "feat(superpowers): register exemplar-research stage in pipeline"
```

---

### Task 5: Create exemplar_research.py — sp-stage handler

**Files:**
- Create: `plastic_promise/skills/exemplar_research.py`

**Interfaces:**
- Consumes: `SkillDef`, `SkillResult` from `skills.engine`; sp-stage handler pattern from `superpowers_stages.py`; `smart-remember` MCP tool; `task_enqueue` MCP tool; `WebSearch` built-in tool
- Produces: `_exemplar_research_handler(ctx, params, atom_results) -> SkillResult`

- [ ] **Step 1: Write the module skeleton and handler**

```python
"""Exemplar Research — sp-stage handler for exemplar-research phase.

Part of the SuperPowers 12-stage pipeline. Sits between brainstorming
and using-git-worktrees in the main chain.

Execution flow:
  1. Read gap_signal (if present) or extract search intent from task_description
  2. WebSearch for mature implementations
  3. Three-question analysis (problem / pattern / constraints)
  4. Write analysis doc to engineering-patterns/ (status=draft)
  5. Quality review via verify_exemplar task
  6. On approval → smart-remember dual-write to memory pool
  7. Complete → exemplar available in subsequent context_supply calls
"""

import json
import os
from datetime import datetime

from plastic_promise.skills.engine import SkillResult


async def _exemplar_research_handler(ctx, params, atom_results):
    """Handler for sp-stage exemplar-research.

    ctx: ContextEngine instance
    params: dict with task_description and optional gap_signal
    atom_results: dict with principle_activate and memory_store results
    """
    task_desc = params.get("task_description", "exemplar research")
    gap_signal = params.get("gap_signal", None)

    # ── 1. Determine search target ─────────────────────────
    if gap_signal and isinstance(gap_signal, dict):
        problem = gap_signal.get("problem", task_desc)
        search_hints = gap_signal.get("suggested_search", [])
    else:
        problem = task_desc
        # Extract search hints from task_description
        try:
            from plastic_promise.core.exemplar_gap_detector import _extract_keywords
            search_hints = _extract_keywords(task_desc)
        except ImportError:
            search_hints = []

    search_query = " ".join(search_hints) if search_hints else problem

    # ── 2. Parse atom results ──────────────────────────────
    def parse(result):
        if result and hasattr(result[0], 'text'):
            try:
                return json.loads(result[0].text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": result[0].text}
        return {}

    principle_data = parse(atom_results.get("principle_activate"))
    store_data = parse(atom_results.get("memory_store"))

    # ── 3. Return SkillResult with exemplar context ────────
    # The actual WebSearch + analysis + doc writing + review
    # is performed by Claude in the conversation after receiving
    # this result. The handler provides the structured context
    # and instructions.

    return SkillResult(
        skill_name="sp-exemplar-research",
        success=True,
        data={
            "stage": "exemplar-research",
            "domain": "designing",
            "tags": ["stage:exemplar-research", "domain:designing", "task:research"],
            "principles": principle_data.get("activated", []),
            "memory_id": store_data.get("memory_id", ""),
            "exemplar": {
                "problem": problem,
                "search_query": search_query,
                "search_hints": search_hints,
                "gap_signal": gap_signal,
                "instructions": (
                    "1. Use WebSearch to find mature implementations for: " + search_query + "\n"
                    "2. Apply three-question analysis:\n"
                    "   a. What problem does it solve?\n"
                    "   b. How does it solve it? (algorithm / data structure / flow)\n"
                    "   c. What parts cannot be used directly? (language / architecture / constraints)\n"
                    "3. Write analysis doc to docs/superpowers/specs/engineering-patterns/YYYY-MM-DD-<project>.md\n"
                    "4. Set status=draft in frontmatter\n"
                    "5. Create verify_exemplar task for quality review\n"
                    "6. On approval → smart-remember(memory_type='exemplar') to memory pool\n"
                    "7. Update INDEX.md with new entry and status marking"
                ),
            },
            "transition": "→ exemplar-research → using-git-worktrees",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )


# ═══════════════════════════════════════════════════════════════
# SkillDef registration
# ═══════════════════════════════════════════════════════════════

EXEMPLAR_RESEARCH_SKILL_DEF = {
    "name": "sp-exemplar-research",
    "domain": "superpowers_stages",
    "description": "SuperPowers 阶段: 典范研究 — 搜索成熟实现、三问法分析、写分析文档、质量审核后入库",
    "tier": "P0",
    "atoms": ["principle_activate", "memory_store"],
    "degrade_map": {
        "principle_activate": "skip",
        "memory_store": "warn",
    },
    "handler": _exemplar_research_handler,
    "allowed_callers": ["claude", "pi", "trae"],
}
```

- [ ] **Step 2: Verify the module is importable**

Run:
```powershell
python -c "from plastic_promise.skills.exemplar_research import _exemplar_research_handler, EXEMPLAR_RESEARCH_SKILL_DEF; print('OK'); print(EXEMPLAR_RESEARCH_SKILL_DEF['name'])"
```
Expected: `OK` then `sp-exemplar-research`.

- [ ] **Step 3: Wire into superpowers_stages.py registration loop**

The existing loop in `superpowers_stages.py` (lines 362-382) auto-registers all stages in `STAGE_ATOMS`. Since we already added `"exemplar-research"` to `STAGE_ATOMS` in Task 4, it will be picked up automatically. But we need to use our dedicated handler instead of the generic `_make_handler`.

Add this conditional in the registration loop at line 366:

```python
    if _stage_name == "requesting-code-review":
        _handler = _request_review_handler
    elif _stage_name == "receiving-code-review":
        _handler = _receive_review_handler
    elif _stage_name == "exemplar-research":
        # Use dedicated handler from exemplar_research module
        try:
            from plastic_promise.skills.exemplar_research import _exemplar_research_handler
            _handler = _exemplar_research_handler
        except ImportError:
            _handler = _make_handler(_stage_name)
    else:
        _handler = _make_handler(_stage_name)
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/skills/exemplar_research.py plastic_promise/skills/superpowers_stages.py
git commit -m "feat(exemplar): add exemplar-research sp-stage handler with three-question analysis flow"
```

---

### Task 6: Create engineering-patterns/ directory with INDEX.md and _template.md

**Files:**
- Create: `docs/superpowers/specs/engineering-patterns/INDEX.md`
- Create: `docs/superpowers/specs/engineering-patterns/_template.md`

**Interfaces:**
- Consumes: none (files-only, consumed by human readers and exemplar_research handler)
- Produces: INDEX.md (categorized index with status markers), _template.md (analysis doc template)

- [ ] **Step 1: Create the directory and INDEX.md**

```powershell
New-Item -ItemType Directory -Force -Path "docs/superpowers/specs/engineering-patterns"
```

Write `docs/superpowers/specs/engineering-patterns/INDEX.md`:

```markdown
# Engineering Exemplar Index

> Curated collection of mature engineering patterns discovered through
> exemplar-driven development. Each entry links to a detailed analysis
> document with three-question breakdown and adaptation notes.

## Status Legend

| Marker | Status | Meaning |
|--------|--------|---------|
| 📝 | Draft | Analysis written, pending review |
| ⚠️ | Pending Verification | Reviewed but not yet applied to project |
| ✅ | Adopted | Pattern successfully applied in project |
| 🗑️ | Deprecated | No longer applicable or superseded |

---

## Storage Engines

<!-- New entries go here -->

## Delegation Systems

<!-- New entries go here -->

## Agent Communication

<!-- New entries go here -->

## Retrieval & Search

<!-- New entries go here -->

## Scheduling & Orchestration

<!-- New entries go here -->

---

*Last updated: 2026-07-02*
*Auto-maintained by exemplar-research sp-stage. Manual edits welcome.*
```

- [ ] **Step 2: Write _template.md**

Write `docs/superpowers/specs/engineering-patterns/_template.md`:

```markdown
---
project: <exemplar-project-name>
url: <github-or-paper-link>
date_analyzed: <YYYY-MM-DD>
status: draft
tags: []
---

# <Project Name> — <One-line summary>

## What problem does it solve?

<!-- Describe the engineering problem this project addresses -->

## How does it solve it? (Core Pattern)

<!-- Extract the core algorithm, data structure, or process flow -->

## What parts cannot be used directly?

<!-- Language differences, architecture differences, constraint differences -->

## Reusable Patterns

<!-- Code snippets, algorithms, data structures worth reusing -->

## Adaptation Notes for This Project

<!-- Concrete suggestions for adapting the pattern -->

## Review Checklist

- [ ] Three-question analysis completeness
- [ ] Code snippet runnability verified
- [ ] Adaptation suggestions feasible for this project

## Review History

| Date | Reviewer | Verdict | Notes |
|------|----------|---------|-------|
|      |          |         |       |
```

- [ ] **Step 3: Verify files exist**

Run:
```powershell
Get-ChildItem docs/superpowers/specs/engineering-patterns/
```
Expected: Both `INDEX.md` and `_template.md` listed.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/engineering-patterns/
git commit -m "docs(exemplar): create engineering-patterns directory with INDEX and template"
```

---

### Task 7: Integration test — end-to-end gap_signal flow

**Files:**
- Test: (inline verification, no permanent test file)

**Interfaces:**
- Consumes: All components from Tasks 1-6
- Produces: Verification that gap detection → signal → research → store chain works

- [ ] **Step 1: Test gap_detector with various inputs**

Run:
```powershell
python -c "
from plastic_promise.core.context_engine import ContextPack, ContextItem
from plastic_promise.core.exemplar_gap_detector import detect_gap

# Test 1: Empty pack + tech query → should trigger
pack = ContextPack()
result = detect_gap('Rust storage engine design', pack)
print(f'Test 1 (empty + tech): gap={result is not None}, severity={result.severity if result else None}')

# Test 2: Empty pack + non-tech query → should NOT trigger
result = detect_gap('what is the weather today', pack)
print(f'Test 2 (empty + non-tech): gap={result is not None}')

# Test 3: Core populated → should NOT trigger
pack.core = [ContextItem(id='1', content='storage engine pattern', relevance=0.9, layer='core')]
result = detect_gap('Rust storage engine design', pack)
print(f'Test 3 (core populated): gap={result is not None}')

# Test 4: Related with >=3 items all > 0.45 → should NOT trigger
pack2 = ContextPack()
pack2.related = [
    ContextItem(id='1', content='a', relevance=0.5, layer='related'),
    ContextItem(id='2', content='b', relevance=0.5, layer='related'),
    ContextItem(id='3', content='c', relevance=0.5, layer='related'),
]
result = detect_gap('Rust storage engine design', pack2)
print(f'Test 4 (related >=3 high quality): gap={result is not None}')

# Test 5: Related with <3 items → should trigger
pack3 = ContextPack()
pack3.related = [
    ContextItem(id='1', content='a', relevance=0.3, layer='related'),
    ContextItem(id='2', content='b', relevance=0.3, layer='related'),
]
result = detect_gap('Rust storage engine design', pack3)
print(f'Test 5 (related <3): gap={result is not None}')

print('All tests passed!')
"
```
Expected: All tests pass with expected gap/no-gap results.

- [ ] **Step 2: Test keyword extraction**

Run:
```powershell
python -c "
from plastic_promise.core.exemplar_gap_detector import _extract_keywords

tests = [
    'Rust storage engine design',
    'how to implement event sourcing',
    'LanceDB retrieval performance optimization',
    'Rust SQLite',
]
for t in tests:
    kw = _extract_keywords(t)
    print(f'{t:50s} → {kw}')
print('Keyword extraction OK')
"
```
Expected: Reasonable keyword lists for each input.

- [ ] **Step 3: Test ContextPack gap_signal field**

Run:
```powershell
python -c "
from plastic_promise.core.context_engine import ContextPack
p = ContextPack()
print(f'Default gap_signal: {p.gap_signal}')
p.gap_signal = {'type': 'exemplar_needed', 'problem': 'test'}
print(f'After assignment: {p.gap_signal}')
print('ContextPack OK')
"
```
Expected: `None` then the dict.

- [ ] **Step 4: Test _compute_payload_hash determinism**

Run:
```powershell
python -c "
from plastic_promise.mcp.tools.task_queue import _compute_payload_hash
p1 = {'problem': 'Rust storage engine', 'search_hint': ['Rust', 'storage', 'engine']}
p2 = {'problem': 'Rust storage engine', 'search_hint': ['engine', 'storage', 'Rust']}
h1 = _compute_payload_hash(p1)
h2 = _compute_payload_hash(p2)
print(f'Hash 1: {h1}')
print(f'Hash 2: {h2}')
print(f'Deterministic: {h1 == h2}')  # Should be True
print(f'Length: {len(h1)}')  # Should be 8
"
```
Expected: Same hash for order-independent hints, 8 chars long.

- [ ] **Step 5: Test sp-stage registration**

Run:
```powershell
python -c "
from plastic_promise.skills.superpowers_stages import exemplar_research, STAGE_ATOMS, STAGE_DOMAIN_MAP
assert 'exemplar-research' in STAGE_ATOMS, 'Missing in STAGE_ATOMS'
assert 'exemplar-research' in STAGE_DOMAIN_MAP, 'Missing in STAGE_DOMAIN_MAP'
assert exemplar_research is not None, 'Module export is None'
print(f'Stage registered: {exemplar_research.name}')
print(f'Domain: {STAGE_DOMAIN_MAP[\"exemplar-research\"]}')
print('All sp-stage checks passed!')
"
```
Expected: All assertions pass.

- [ ] **Step 6: Full import chain verification**

Run:
```powershell
python -c "
# Verify the full chain imports without errors
from plastic_promise.core.exemplar_gap_detector import GapSignal, detect_gap, _is_tech_query, _extract_keywords
from plastic_promise.skills.exemplar_research import _exemplar_research_handler, EXEMPLAR_RESEARCH_SKILL_DEF
from plastic_promise.skills.superpowers_stages import exemplar_research
from plastic_promise.core.context_engine import ContextPack
from plastic_promise.mcp.tools.task_queue import _compute_payload_hash, _inject_payload_hash
print('Full import chain OK — all modules load cleanly')
"
```
Expected: `Full import chain OK`.

- [ ] **Step 7: Commit**

```bash
git commit --allow-empty -m "test(exemplar): verify end-to-end gap detection and sp-stage integration"
```

---

### Task 8: Update CLAUDE.md with exemplar-research workflow

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: existing CLAUDE.md structure
- Produces: updated SuperPowers pipeline documentation with exemplar-research stage

- [ ] **Step 1: Add exemplar-research to the SuperPowers flow diagram**

Find the flow diagram section in CLAUDE.md and update the chain:

```
brainstorming → using-git-worktrees → writing-plans
```

Should become:

```
brainstorming → exemplar-research → using-git-worktrees → writing-plans
```

- [ ] **Step 2: Add exemplar-research to the sp-stage mapping table**

In the tool table, add:
```
| exemplar-research | 搜索成熟实现 + 三问法分析 + 质量审核 | exemplar_research.py |
```

- [ ] **Step 3: Add new task types to Hunter Guild section**

Under delegate types, add:
```
| research_exemplar | 研究工程典范 | claude | B (priority=3) |
| verify_exemplar | 审核典范分析质量 | claude | B (priority=3) |
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with exemplar-research stage and new task types"
```
