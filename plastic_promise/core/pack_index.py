"""pack_tag_index — 独立于 DomainManager 的轻量倒排索引。
用于 pack_recall strict 模式，在 _dm_ok=False 时保底。
"""

import gzip
import json
import time
from typing import Any

from plastic_promise.core.context_engine import OrdinaryMemoryConflict
from plastic_promise.core.synthesis import synthesis_content_hash
from plastic_promise.core.synthesis_retrieval import (
    _source_is_available,
    engine_memory_is_governed_synthesis,
)

PACK_VERSION_MAP = {
    "1.0": {"domain": {"work": "governing", "life": "reflecting"}},
    "2.0": {},
}

_PACK_SOURCE_SNAPSHOT_FIELDS = (
    "access_count",
    "category",
    "created_at",
    "decay_multiplier",
    "effective_half_life",
    "embedding_hash",
    "last_accessed",
    "metadata_json",
    "tags",
    "tier",
    "worth_failure",
    "worth_success",
)


def _canonical_pack_memory(engine: Any, memory_id: str) -> dict | None:
    getter = getattr(engine, "get_memory_dict_for_review", None)
    if not callable(getter):
        getter = getattr(engine, "get_memory_dict", None)
    current = getter(memory_id) if callable(getter) else None
    return current if isinstance(current, dict) else None


def _ensure_pack_policy_preserves_availability(
    existing: dict, *, tags: list[str], domain: str
) -> None:
    candidate = dict(existing)
    candidate.update({"tags": list(tags), "domain": domain})
    try:
        changed = _source_is_available(existing) != _source_is_available(candidate)
    except Exception as exc:
        raise OrdinaryMemoryConflict("ordinary_patch_availability_invalid") from exc
    if changed:
        raise OrdinaryMemoryConflict("ordinary_patch_availability_change_requires_coordinator")


class PackIndex:
    """轻量倒排索引，不依赖 DomainManager。"""

    def __init__(self):
        self.tag_index: dict[str, set[str]] = {}  # tag → set[memory_id]
        self.memories: dict[str, dict] = {}  # mid → {content, tags, domain, ...}

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


def pack_export_streaming(
    name: str, output_path: str, engine: Any | None = None, tags: list | None = None
) -> dict:
    """流式写盘导出。逐条读取记忆，gzip 压缩，内存上限 50MB。

    Returns: {"path": output_path, "count": N}
    """
    count = 0
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('{"version":"2.0","name":"' + name + '","memories":[\n')
        first = True

        if engine:
            for mem in engine.iter_memories():
                memory_id = str(mem.get("id", ""))
                memory_type = mem.get("memory_type")
                if engine_memory_is_governed_synthesis(
                    engine,
                    memory_id,
                    memory_type=memory_type,
                ):
                    public_gate = getattr(type(engine), "_public_memory_ids", None)
                    if not callable(public_gate) or memory_id not in public_gate(
                        engine,
                        [memory_id],
                    ):
                        continue
                mem_tags = mem.get("tags", [])
                if tags and not (set(tags) & set(mem_tags)):
                    continue
                if not first:
                    f.write(",\n")
                else:
                    first = False
                json.dump(
                    {
                        "id": memory_id,
                        "content": mem.get("content", ""),
                        "memory_type": mem.get("memory_type", ""),
                        "source": mem.get("source", ""),
                        "tags": mem_tags,
                        "domain": mem.get("domain", ""),
                        "tier": mem.get("tier", ""),
                    },
                    f,
                    ensure_ascii=False,
                )
                count += 1

        f.write('\n],"count":' + str(count) + "}")

    return {"path": output_path, "count": count}


def pack_import_with_strategy(
    path: str, engine: Any, strategy: str = "skip", owner: str = ""
) -> dict:
    """导入经验包，支持策略选择 + 版本映射。

    strategy: skip|replace|merge
    merge 时 domain 冲突以包内 domain 为准（包是已知正确快照）。
    """
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            pack = json.load(f)
    else:
        with open(path, encoding="utf-8") as f:
            pack = json.load(f)

    pack_version = pack.get("version", "1.0")
    mapper = PACK_VERSION_MAP.get(pack_version, {})
    domain_map = mapper.get("domain", {})

    imported = 0
    skipped = 0
    merged = 0

    for mem in pack.get("memories", []):
        mid = mem["id"]
        # 版本域映射
        old_domain = mem.get("domain", "")
        new_domain = domain_map.get(old_domain, old_domain) if old_domain else ""

        existing = _canonical_pack_memory(engine, mid)
        if existing:
            if strategy == "skip":
                skipped += 1
                continue
            elif strategy == "replace":
                replacement_tags = list(mem.get("tags", []))
                replacement_domain = new_domain or str(existing.get("domain") or "")
                _ensure_pack_policy_preserves_availability(
                    existing,
                    tags=replacement_tags,
                    domain=replacement_domain,
                )
                if str(mem["content"]) == str(existing.get("content") or ""):
                    engine.patch_ordinary_memory(
                        mid,
                        replacements={"tags": replacement_tags, "domain": replacement_domain},
                        expected_project_id=str(existing.get("project_id") or ""),
                        expected_tags=list(existing.get("tags") or []),
                        require_source_available=True,
                    )
                else:
                    engine.mutate_ordinary_source(
                        mid,
                        operation="replace_content",
                        content=mem["content"],
                        reason="pack_import:replace",
                        actor="pack_import",
                        call_id=f"pack:replace:{time.time_ns()}:{mid}",
                        expected_project_id=str(existing.get("project_id") or ""),
                        expected_content_hash=synthesis_content_hash(existing.get("content")),
                        expected_source_snapshot={
                            field: existing.get(field) for field in _PACK_SOURCE_SNAPSHOT_FIELDS
                        },
                        require_source_available=True,
                        policy_replacements={
                            "category": str(existing.get("category") or "other"),
                            "domain": replacement_domain,
                            "tags": replacement_tags,
                            "tier": str(existing.get("tier") or "L1"),
                        },
                    )
                imported += 1
            elif strategy == "merge":
                old_tags = set(existing.get("tags", []))
                new_tags = set(mem.get("tags", []))
                merged_tags = sorted(old_tags | new_tags)
                merged_domain = new_domain or str(existing.get("domain") or "")
                _ensure_pack_policy_preserves_availability(
                    existing,
                    tags=merged_tags,
                    domain=merged_domain,
                )
                engine.patch_ordinary_memory(
                    mid,
                    replacements={
                        "tags": merged_tags,
                        "domain": merged_domain,
                    },
                    expected_project_id=str(existing.get("project_id") or ""),
                    expected_tags=list(existing.get("tags") or []),
                    require_source_available=True,
                )
                merged += 1
        else:
            engine.create_ordinary_if_absent(
                {
                    "id": mid,
                    "content": mem["content"],
                    "memory_type": mem.get("memory_type", "experience"),
                    "source": mem.get("source", "user"),
                    "tags": mem.get("tags", []),
                    "domain": new_domain,
                    "tier": mem.get("tier", "L1"),
                    "owner": owner or mem.get("owner", ""),
                }
            )
            imported += 1

    return {"imported": imported, "skipped": skipped, "merged": merged, "version": pack_version}
