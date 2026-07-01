"""pack_tag_index — 独立于 DomainManager 的轻量倒排索引。
用于 pack_recall strict 模式，在 _dm_ok=False 时保底。
"""
import json
import gzip
from typing import Any, Optional


PACK_VERSION_MAP = {
    "1.0": {"domain": {"work": "governing", "life": "reflecting"}},
    "2.0": {},
}


class PackIndex:
    """轻量倒排索引，不依赖 DomainManager。"""

    def __init__(self):
        self.tag_index: dict[str, set[str]] = {}  # tag → set[memory_id]
        self.memories: dict[str, dict] = {}        # mid → {content, tags, domain, ...}

    def build_from_pack(self, pack_data: dict):
        """从 pack JSON 数据构建索引。"""
        for mem in pack_data.get("memories", []):
            mid = mem["id"]
            tags = mem.get("tags", [])
            self.memories[mid] = mem
            for tag in tags:
                if tag not in self.tag_index:
                    self.tag_index[tag] = set()
                self.tag_index[tag].add(mid)

    def search(self, query_tags: list[str]) -> list[dict]:
        """按标签检索，返回匹配的记忆列表。"""
        candidates = set()
        for tag in query_tags:
            if tag in self.tag_index:
                if not candidates:
                    candidates = self.tag_index[tag].copy()
                else:
                    candidates &= self.tag_index[tag]
        if not candidates:
            # 无交集 → 返回并集
            for tag in query_tags:
                if tag in self.tag_index:
                    candidates |= self.tag_index[tag]
        return [self.memories[mid] for mid in candidates if mid in self.memories]


def pack_export_streaming(name: str, output_path: str,
                          engine: Optional[Any] = None,
                          tags: Optional[list] = None) -> dict:
    """流式写盘导出。逐条读取记忆，gzip 压缩，内存上限 50MB。

    Returns: {"path": output_path, "count": N}
    """
    count = 0
    with gzip.open(output_path, 'wt', encoding='utf-8') as f:
        f.write('{"version":"2.0","name":"' + name + '","memories":[\n')
        first = True

        if engine:
            for mem in engine.iter_memories():
                mem_tags = mem.get("tags", [])
                if tags and not (set(tags) & set(mem_tags)):
                    continue
                if not first:
                    f.write(',\n')
                else:
                    first = False
                json.dump({
                    "id": mem.get("id", ""), "content": mem.get("content", ""),
                    "memory_type": mem.get("memory_type", ""),
                    "source": mem.get("source", ""), "tags": mem_tags,
                    "domain": mem.get("domain", ""),
                    "tier": mem.get("tier", ""),
                }, f, ensure_ascii=False)
                count += 1

        f.write('\n],"count":' + str(count) + '}')

    return {"path": output_path, "count": count}


def pack_import_with_strategy(path: str, engine: Any,
                              strategy: str = "skip",
                              owner: str = "") -> dict:
    """导入经验包，支持策略选择 + 版本映射。

    strategy: skip|replace|merge
    merge 时 domain 冲突以包内 domain 为准（包是已知正确快照）。
    """
    if path.endswith('.gz'):
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            pack = json.load(f)
    else:
        with open(path, 'r', encoding='utf-8') as f:
            pack = json.load(f)

    pack_version = pack.get("version", "1.0")
    mapper = PACK_VERSION_MAP.get(pack_version, {})
    domain_map = mapper.get("domain", {})

    imported = 0; skipped = 0; merged = 0

    for mem in pack.get("memories", []):
        mid = mem["id"]
        # 版本域映射
        old_domain = mem.get("domain", "")
        new_domain = domain_map.get(old_domain, old_domain) if old_domain else ""

        existing = engine.get_memory_dict(mid)
        if existing:
            if strategy == "skip":
                skipped += 1
                continue
            elif strategy == "replace":
                engine.register_memory({
                    "id": mid, "content": mem["content"],
                    "memory_type": mem.get("memory_type", "experience"),
                    "source": mem.get("source", "user"),
                    "tags": mem.get("tags", []),
                    "domain": new_domain,
                })
                imported += 1
            elif strategy == "merge":
                old_tags = set(existing.get("tags", []))
                new_tags = set(mem.get("tags", []))
                engine.update_memory_fields(mid, tags=list(old_tags | new_tags), domain=new_domain)
                merged += 1
        else:
            engine.register_memory({
                "id": mid, "content": mem["content"],
                "memory_type": mem.get("memory_type", "experience"),
                "source": mem.get("source", "user"),
                "tags": mem.get("tags", []),
                "domain": new_domain,
                "tier": mem.get("tier", "L1"),
                "owner": owner or mem.get("owner", ""),
            })
            imported += 1

    return {"imported": imported, "skipped": skipped, "merged": merged,
            "version": pack_version}
