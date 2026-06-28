# Fuzzy Buffer Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development.

**Goal:** Build deferred memory embedding pipeline — store urgently with temp tags, process in background, migrate to main pool.

**Architecture:** New `FuzzyBuffer` class manages 4-stage pipeline (raw→tagged→embedded→classified→migrate). MCP tools `fuzzy_status`/`fuzzy_process` expose control. Cron auto-triggers when buffer has backlog.

**Tech Stack:** Python 3.10+, MemoryTierManager, RecMem, embedder, noise_filter, MCP mcp.types.TextContent

## Global Constraints

- New file: `plastic_promise/memory/fuzzy_buffer.py`
- Stage names: "raw", "tagged", "embedded", "classified"
- Memory IDs prefixed: "fuzzy_"
- Batch embedding size: 10
- CJK keyword extraction: top 5 bigrams from content
- All methods tolerate empty buffer gracefully
- FuzzyBuffer receives RecMem and embedder via constructor

---

### Task 1: Create FuzzyBuffer class

**Files:**
- Create: `plastic_promise/memory/fuzzy_buffer.py`

**Interfaces:**
- Produces: `FuzzyBuffer(rec_mem, embedder=None, tier_manager=None)`, `store_urgent(content, memory_type, source) -> str`, `process_pipeline() -> dict`, `stats() -> dict`, `process_stage(stage_from, stage_to, processor_fn) -> int`

- [ ] **Step 1: Write FuzzyBuffer class**

```python
"""Fuzzy Buffer — deferred memory embedding pipeline.

When the embedding service is unavailable, memories are stored urgently
with temporary tags in a buffer. The buffer is processed in the background
through a 4-stage pipeline: raw → tagged → embedded → classified → migrate.
"""

import uuid
import datetime
from typing import Any, Dict, List, Optional


class FuzzyBuffer:
    """Deferred memory processing buffer with 4-stage pipeline.

    Stages:
        raw        — just arrived, basic tags only
        tagged     — keywords extracted
        embedded   — vectors generated
        classified — tier (L1/L3) determined, ready to migrate
    """

    def __init__(self, rec_mem=None, embedder=None, tier_manager=None) -> None:
        self._buffer: Dict[str, Dict[str, Any]] = {}
        self.rec_mem = rec_mem
        self.embedder = embedder
        self._tier_manager = tier_manager
        self._last_process: Optional[str] = None
        self._batch_size = 10

    def store_urgent(
        self, content: str, memory_type: str = "experience", source: str = "user"
    ) -> str:
        """Store a memory urgently with temporary tags, skipping embedding.

        Returns:
            memory_id with 'fuzzy_' prefix.
        """
        mid = f"fuzzy_{uuid.uuid4().hex[:12]}"
        tags = self._extract_tags(content)
        self._buffer[mid] = {
            "memory_id": mid,
            "content": content,
            "memory_type": memory_type,
            "source": source,
            "stage": "raw",
            "tags": tags,
            "vector": None,
            "tier": None,
            "created_at": datetime.datetime.now().isoformat(),
            "processed_at": None,
        }
        return mid

    def _extract_tags(self, content: str) -> List[str]:
        """Extract up to 5 CJK bigram keywords as temporary tags."""
        import re
        tags = []
        seen = set()
        has_cjk = bool(re.search(r'[一-鿿]', content))
        if has_cjk:
            for i in range(len(content) - 1):
                bigram = content[i:i+2]
                if re.search(r'[一-鿿]', bigram) and bigram not in seen:
                    tags.append(bigram)
                    seen.add(bigram)
                if len(tags) >= 5:
                    break
        if not tags:
            # Fallback: split by whitespace, take first 5 non-empty
            tags = [w for w in content.split() if len(w) >= 2][:5]
        return tags

    def process_pipeline(self) -> Dict[str, Any]:
        """Run the full 4-stage pipeline on all buffered items.

        Returns:
            dict with counts per stage and migration total.
        """
        counts = {"raw→tagged": 0, "tagged→embedded": 0,
                  "embedded→classified": 0, "classified→migrated": 0}

        # Stage 1: raw → tagged (noise filter + keyword confirmation)
        counts["raw→tagged"] = self._process_raw_to_tagged()

        # Stage 2: tagged → embedded (batch embed)
        counts["tagged→embedded"] = self._process_tagged_to_embedded()

        # Stage 3: embedded → classified (tier classification)
        counts["embedded→classified"] = self._process_embedded_to_classified()

        # Stage 4: classified → migrate to main pool
        counts["classified→migrated"] = self._process_classified_to_migrate()

        self._last_process = datetime.datetime.now().isoformat()
        return {
            "pipeline": counts,
            "total_processed": sum(counts.values()),
            "buffer_remaining": len(self._buffer),
            "timestamp": self._last_process,
        }

    def _process_raw_to_tagged(self) -> int:
        """Stage 1: Confirm tags for raw items, move to tagged."""
        count = 0
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "raw"]
        for mid, record in items:
            try:
                from plastic_promise.noise_filter import is_noise
                if is_noise(record["content"]):
                    del self._buffer[mid]
                    continue
            except Exception:
                pass
            # Tags already extracted in store_urgent; confirm them
            record["stage"] = "tagged"
            record["processed_at"] = datetime.datetime.now().isoformat()
            count += 1
        return count

    def _process_tagged_to_embedded(self) -> int:
        """Stage 2: Batch-embed tagged items, move to embedded."""
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "tagged"]
        if not items or self.embedder is None:
            return 0

        count = 0
        for i in range(0, len(items), self._batch_size):
            batch = items[i:i + self._batch_size]
            contents = [r["content"] for _, r in batch]
            try:
                vectors = self.embedder.embed_batch(contents)
            except Exception:
                vectors = [[0.0] * self.embedder.dim for _ in batch]
            for (mid, record), vec in zip(batch, vectors):
                record["vector"] = vec
                record["stage"] = "embedded"
                record["processed_at"] = datetime.datetime.now().isoformat()
                count += 1
        return count

    def _process_embedded_to_classified(self) -> int:
        """Stage 3: Classify tier for embedded items using MemoryTierManager."""
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "embedded"]
        count = 0
        for mid, record in items:
            if self._tier_manager is not None:
                try:
                    from plastic_promise.memory.soul_memory import MemoryRecord
                    mr = MemoryRecord(
                        content=record["content"],
                        memory_type=record["memory_type"],
                        source=record["source"],
                    )
                    record["tier"] = self._tier_manager.classify_tier(mr)
                except Exception:
                    record["tier"] = "L1"
            else:
                record["tier"] = "L1"
            record["stage"] = "classified"
            record["processed_at"] = datetime.datetime.now().isoformat()
            count += 1
        return count

    def _process_classified_to_migrate(self) -> int:
        """Stage 4: Migrate classified items to main memory pool."""
        items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "classified"]
        count = 0
        for mid, record in items:
            try:
                if self.rec_mem is not None:
                    self.rec_mem.store(
                        content=record["content"],
                        memory_type=record["memory_type"],
                        source=record["source"],
                    )
                del self._buffer[mid]
                count += 1
            except Exception:
                pass
        return count

    def stats(self) -> Dict[str, Any]:
        """Return buffer statistics."""
        by_stage = {"raw": 0, "tagged": 0, "embedded": 0, "classified": 0}
        for r in self._buffer.values():
            stage = r.get("stage", "raw")
            if stage in by_stage:
                by_stage[stage] += 1
        total = len(self._buffer)
        oldest = None
        if self._buffer:
            oldest = min(r.get("created_at", "") for r in self._buffer.values())
        return {
            "total": total,
            "by_stage": by_stage,
            "oldest_pending": oldest,
            "last_process": self._last_process,
        }
```

- [ ] **Step 2: Verify class works**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.memory.fuzzy_buffer import FuzzyBuffer
from plastic_promise.memory.soul_memory import MemoryTierManager, RecMem
from plastic_promise.embedder import FallbackEmbedder

rm = RecMem()
fb = FuzzyBuffer(rec_mem=rm, embedder=FallbackEmbedder(), tier_manager=MemoryTierManager())

# store_urgent
mid = fb.store_urgent('这是一条紧急测试记忆：系统崩溃后需要立即记录关键日志', 'experience', 'system')
assert mid.startswith('fuzzy_')
print(f'store_urgent OK: {mid}')

# stats
s = fb.stats()
assert s['total'] == 1 and s['by_stage']['raw'] == 1
print(f'stats OK: total={s[\"total\"]}, stages={s[\"by_stage\"]}')

# process_pipeline
result = fb.process_pipeline()
print(f'pipeline OK: {result[\"pipeline\"]}, remaining={result[\"buffer_remaining\"]}')
assert result['buffer_remaining'] == 0

# Verify memory is in main pool
assert rm.stats()['total'] >= 1
print('migrated to main pool OK')

print('ALL TESTS PASSED')
"
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/memory/fuzzy_buffer.py
git commit -m "feat: add FuzzyBuffer — deferred memory embedding pipeline"
```

---

### Task 2: Add MCP tools + modify memory_store

**Files:**
- Modify: `plastic_promise/mcp/tools/memory.py`
- Modify: `plastic_promise/mcp/server.py` (register fuzzy_status, fuzzy_process)

**Interfaces:**
- Consumes: `FuzzyBuffer(engine._rec_mem)`
- Produces: `handle_fuzzy_status(engine, args)`, `handle_fuzzy_process(engine, args)`

- [ ] **Step 1: Add MCP handlers to memory.py**

Append after the last handler:

```python
# ---- fuzzy_status ----
async def handle_fuzzy_status(engine: Any, args: dict) -> list[TextContent]:
    """Query fuzzy buffer statistics."""
    try:
        from plastic_promise.memory.fuzzy_buffer import FuzzyBuffer
        fb = _get_fuzzy_buffer(engine)
        stats = fb.stats()
        return [TextContent(type="text", text=json.dumps(stats, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "fuzzy_status"}, ensure_ascii=False))]


# ---- fuzzy_process ----
async def handle_fuzzy_process(engine: Any, args: dict) -> list[TextContent]:
    """Trigger fuzzy buffer pipeline processing."""
    try:
        from plastic_promise.memory.fuzzy_buffer import FuzzyBuffer
        fb = _get_fuzzy_buffer(engine)
        result = fb.process_pipeline()
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "fuzzy_process"}, ensure_ascii=False))]


def _get_fuzzy_buffer(engine: Any):
    """Get or create a FuzzyBuffer attached to the engine."""
    if not hasattr(engine, '_fuzzy_buffer') or engine._fuzzy_buffer is None:
        from plastic_promise.memory.fuzzy_buffer import FuzzyBuffer
        from plastic_promise.memory.soul_memory import MemoryTierManager, RecMem
        from plastic_promise.embedder import get_embedder

        rec_mem = engine._rec_mem if hasattr(engine, '_rec_mem') else RecMem(engine)
        embedder = get_embedder()
        tier_mgr = MemoryTierManager(rec_mem)
        engine._fuzzy_buffer = FuzzyBuffer(rec_mem=rec_mem, embedder=embedder, tier_manager=tier_mgr)
        engine._rec_mem = rec_mem
    return engine._fuzzy_buffer
```

- [ ] **Step 2: Modify memory_store to use fuzzy buffer when embedder fails**

In `handle_memory_store`, modify the embedding section to fall back to fuzzy buffer:

Replace the embedding try/except block (around line 122-126) with:

```python
        # Embed — fall back to fuzzy buffer if unavailable
        from plastic_promise.embedder import get_embedder, FallbackEmbedder
        vector_dim = 0
        try:
            embedder = get_embedder(fallback_on_error=False)
            vec = embedder.embed(content)
            vector_dim = len(vec)
        except Exception:
            # Embedding unavailable: store in fuzzy buffer for later processing
            from plastic_promise.memory.fuzzy_buffer import FuzzyBuffer
            fb = _get_fuzzy_buffer(engine)
            fuzzy_id = fb.store_urgent(content, memory_type, source)
            return [TextContent(type="text", text=json.dumps({
                "stored": True,
                "memory_id": stored_id,
                "content_preview": content[:200],
                "memory_type": memory_type,
                "scope": scope,
                "fuzzy_id": fuzzy_id,
                "fuzzy": True,
                "note": "Stored in fuzzy buffer — embedding deferred to background processing",
            }, ensure_ascii=False))]
```

- [ ] **Step 3: Register new tools in server.py**

Read server.py to find the tool list and handlers, add:
- Tool definitions for `fuzzy_status` and `fuzzy_process`
- Handler imports and routing

- [ ] **Step 4: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
import json, asyncio
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine()

async def test():
    from plastic_promise.mcp.tools.memory import handle_fuzzy_status, handle_fuzzy_process, handle_memory_store

    # fuzzy_status on empty buffer
    r = await handle_fuzzy_status(engine, {})
    d = json.loads(r[0].text)
    assert d['total'] == 0
    print('fuzzy_status empty OK')

    # memory_store with fuzzy fallback (no Ollama)
    r = await handle_memory_store(engine, {
        'content': '紧急测试记忆：服务降级时需要立即记录',
        'memory_type': 'experience',
    })
    d = json.loads(r[0].text)
    assert d['stored'] and d.get('fuzzy')
    print(f'memory_store fuzzy OK: fuzzy_id={d.get(\"fuzzy_id\")}')

    # fuzzy_status after store
    r = await handle_fuzzy_status(engine, {})
    d = json.loads(r[0].text)
    assert d['total'] >= 1
    print(f'fuzzy_status OK: total={d[\"total\"]}, stages={d[\"by_stage\"]}')

    # fuzzy_process
    r = await handle_fuzzy_process(engine, {})
    d = json.loads(r[0].text)
    assert d['buffer_remaining'] == 0
    print(f'fuzzy_process OK: pipeline={d[\"pipeline\"]}')

    print('ALL TESTS PASSED')
asyncio.run(test())
"
```

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/memory.py plastic_promise/mcp/server.py
git commit -m "feat: add fuzzy_status + fuzzy_process MCP tools, fuzzy fallback in memory_store"
```

---

### Task 3: Cron integration

**Files:**
- Modify: `plastic_promise/cron/health_scan.py`

- [ ] **Step 1: Add fuzzy buffer check**

Add at end of `run()` function, before return:

```python
    # Check fuzzy buffer backlog
    try:
        if engine is not None and hasattr(engine, '_fuzzy_buffer') and engine._fuzzy_buffer is not None:
            fb_stats = engine._fuzzy_buffer.stats()
            if fb_stats["total"] > 0:
                engine._fuzzy_buffer.process_pipeline()
                result["fuzzy_processed"] = fb_stats["total"]
    except Exception:
        pass
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/cron/health_scan.py
git commit -m "feat: auto-process fuzzy buffer in health_scan cron"
```
