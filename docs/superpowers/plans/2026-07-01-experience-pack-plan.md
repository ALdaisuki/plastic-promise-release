# Experience Pack Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development.

**Goal:** Build plug-and-play experience packs: export domain memories as shareable JSON, import from JSON, strict memory-only recall.

**Architecture:** New module `plastic_promise/pack.py` + 3 MCP tools extending management.py. Git-tracked `experience_packs/` directory.

**Tech Stack:** Python 3.10+, json, uuid, datetime, existing ContextEngine + memory_store/recall

## Global Constraints
- Pack files live in `experience_packs/` (git tracked)
- Memory type: "lesson" | "fact" | "procedure"
- pack_recall strict mode: 0 matches → return empty, never fabricate
- Every extracted item has `source_memory_id` (traceable)
- Export overwrites existing pack file

---

### Task 1: pack.py module + pack_export MCP tool

**Files:**
- Create: `plastic_promise/pack.py`
- Modify: `plastic_promise/mcp/tools/management.py`
- Modify: `plastic_promise/mcp/server.py`

- [ ] **Step 1: Create pack.py**

```python
"""Experience Pack — 随插随用的可分享领域记忆包."""

import json, os, uuid, datetime
from typing import Any, Dict, List, Optional


def export_pack(
    engine: Any,
    name: str,
    tags: List[str] = None,
    memory_ids: List[str] = None,
    author: str = "claude",
    description: str = "",
) -> str:
    """Export memories matching tags/ids into a JSON pack file."""
    memories = engine.list_memories(limit=10000) if engine else []
    selected = []
    for m in memories:
        if memory_ids and m.id in memory_ids:
            selected.append(m)
        elif tags and hasattr(m, 'entity_ids'):
            mem_entity_ids = getattr(m, 'entity_ids', [])
            if any(t in mem_entity_ids or t in m.content for t in tags):
                selected.append(m)

    pack = {
        "pack": {
            "name": name, "version": "1.0.0", "author": author,
            "description": description, "license": "MIT",
            "quality_score": round(sum(getattr(m, 'worth_score', lambda: 0.5)() for m in selected) / max(len(selected), 1), 2),
            "provenance": [{"action": "exported", "agent": author, "timestamp": datetime.datetime.now().isoformat()}],
            "memory_count": len(selected), "created": datetime.datetime.now().isoformat(),
        },
        "memories": [
            {
                "id": f"exp_{uuid.uuid4().hex[:8]}",
                "content": m.content[:1000],
                "type": "fact" if m.memory_type == "knowledge" else "lesson" if m.memory_type == "reflection" else "procedure",
                "tags": getattr(m, 'entity_ids', []),
                "source_memory_id": m.id,
                "distilled_by": author,
                "distilled_at": datetime.datetime.now().isoformat(),
                "entity_ids": getattr(m, 'entity_ids', []),
                "created_at": getattr(m, 'created_at', ''),
                "worth_score": m.worth_score() if hasattr(m, 'worth_score') else 0.5,
            }
            for m in selected
        ],
    }

    os.makedirs("experience_packs", exist_ok=True)
    path = f"experience_packs/{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=2)
    return path


def import_pack(engine: Any, path: str, owner: str = "") -> dict:
    """Import a JSON pack file into the memory pool."""
    with open(path, "r", encoding="utf-8") as f:
        pack = json.load(f)

    imported = 0
    for mem in pack.get("memories", []):
        try:
            engine.register_memory({
                "id": mem.get("id", f"exp_{uuid.uuid4().hex[:8]}"),
                "content": mem["content"],
                "memory_type": "reflection" if mem.get("type") == "lesson" else "knowledge" if mem.get("type") == "fact" else "experience",
                "source": "pack_import",
                "owner": owner,
                "tier": "L3",
            })
            imported += 1
        except Exception:
            pass

    return {"imported": imported, "pack_name": pack["pack"]["name"], "total": len(pack.get("memories", []))}


def recall_pack(engine: Any, query: str, pack_name: str = None, strict: bool = True) -> dict:
    """Recall ONLY from stored memories. Strict mode: 0 matches → empty."""
    results = engine._text_retrieval(query) if engine else []
    memories = engine._memories if engine else {}

    items = []
    for mid, score, content, source in results:
        if mid not in memories or mid.startswith("principle:"):
            continue
        mem = memories[mid]
        owner = mem.get("owner", "")
        items.append({
            "source_memory_id": mid,
            "content": content[:500],
            "relevance": round(score, 3),
            "type": "reflection" if mem.get("memory_type") == "reflection" else "experience",
            "owner": owner,
            "tier": mem.get("tier", "L1"),
        })

    # ENRICH: expand via entity_ids
    enriched = []
    seen = {i["source_memory_id"] for i in items}
    for item in items[:]:
        mem = memories.get(item["source_memory_id"], {})
        for eid in mem.get("entity_ids", []):
            for edge in engine._graph_edges:
                if edge.get("to") == eid and edge.get("from") not in seen:
                    linked = memories.get(edge["from"], {})
                    if linked:
                        enriched.append({
                            "source_memory_id": edge["from"],
                            "content": linked.get("content", "")[:300],
                            "relation": f"linked via {eid}",
                        })
                        seen.add(edge["from"])

    if strict and not items:
        return {"found": 0, "items": [], "note": "无匹配记忆——按照约定#4，信息不足时不瞎编"}

    return {"found": len(items), "items": items, "enriched": enriched}
```

- [ ] **Step 2: Add MCP handlers to management.py**

```python
# ---- pack_export ----
async def handle_pack_export(engine: Any, args: dict) -> list[TextContent]:
    """Export memories as a shareable JSON experience pack."""
    try:
        from plastic_promise.pack import export_pack
        path = export_pack(
            engine, name=args["name"],
            tags=args.get("tags"), memory_ids=args.get("memory_ids"),
            author=args.get("author", "claude"),
            description=args.get("description", ""),
        )
        return [TextContent(type="text", text=json.dumps(
            {"exported": True, "path": path}, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "pack_export"}, ensure_ascii=False))]


# ---- pack_import ----
async def handle_pack_import(engine: Any, args: dict) -> list[TextContent]:
    """Import a JSON experience pack into the memory pool."""
    try:
        from plastic_promise.pack import import_pack
        result = import_pack(engine, path=args["path"], owner=args.get("owner", ""))
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "pack_import"}, ensure_ascii=False))]


# ---- pack_recall ----
async def handle_pack_recall(engine: Any, args: dict) -> list[TextContent]:
    """Recall ONLY from stored memories. Strict mode: never fabricate."""
    try:
        from plastic_promise.pack import recall_pack
        result = recall_pack(
            engine, query=args["query"],
            pack_name=args.get("pack"),
            strict=args.get("strict", True),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "pack_recall"}, ensure_ascii=False))]
```

- [ ] **Step 3: Register 3 tools in server.py** (tool definitions + routing)

- [ ] **Step 4: Verify all 3 tools**

```bash
cd "F:/Agent/Memory system" && python -c "
import json, asyncio
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.mcp.tools.management import handle_pack_export, handle_pack_import, handle_pack_recall

async def test():
    engine = ContextEngine()
    engine.register_memory({'id':'m1','content':'SQLite持久化配置：设置AGENT_USE_SQLITE=1，默认开启','memory_type':'reflection','source':'user'})
    engine.register_memory({'id':'m2','content':'MCP重连后记忆归零——需要SQLite持久化','memory_type':'experience','source':'user'})

    # Export
    r = await handle_pack_export(engine, {'name':'test_ops','tags':['SQLite','持久化'],'description':'运营测试'})
    d = json.loads(r[0].text)
    assert d['exported']; print(f'Export OK: {d[\"path\"]}')

    # Import
    r = await handle_pack_import(engine, {'path':'experience_packs/test_ops.json','owner':'pi'})
    d = json.loads(r[0].text)
    assert d['imported'] >= 1; print(f'Import OK: {d[\"imported\"]} memories')

    # Recall strict — match
    r = await handle_pack_recall(engine, {'query':'SQLite','strict':True})
    d = json.loads(r[0].text)
    assert d['found'] >= 1; print(f'Recall OK: found={d[\"found\"]}')

    # Recall strict — no match (must return empty, no fabrication)
    r = await handle_pack_recall(engine, {'query':'火星采矿技术','strict':True})
    d = json.loads(r[0].text)
    assert d['found'] == 0; assert '不瞎编' in d.get('note','')
    print(f'Strict no-match OK: {d[\"note\"][:50]}')

    print('ALL EXPERIENCE PACK TESTS PASSED')
asyncio.run(test())
"
```

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/pack.py plastic_promise/mcp/tools/management.py plastic_promise/mcp/server.py experience_packs/
git commit -m "feat: experience pack system — export/import/recall with strict no-fabrication"
```
