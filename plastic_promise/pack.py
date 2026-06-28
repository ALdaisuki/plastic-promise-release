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
