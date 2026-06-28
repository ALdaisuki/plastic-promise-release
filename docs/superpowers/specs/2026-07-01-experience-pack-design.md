# 经验包系统 — 随插随用的可分享领域记忆包

**日期**: 2026-07-01
**原则**: #4 上下文驱动, #2 可查可透明, #11 原则遗传

## 设计

### 文件格式

`experience_packs/{name}.json` — 每个包是一个 git 可追踪的 JSON 文件。

```json
{
  "pack": {
    "name": "operations", "version": "1.0.0",
    "author": "claude", "description": "...",
    "license": "MIT", "quality_score": 0.85,
    "provenance": [{"action":"created","agent":"claude","timestamp":"..."}],
    "memory_count": 3, "created": "..."
  },
  "memories": [
    {
      "id": "exp_<uuid8>", "content": "...",
      "type": "lesson|fact|procedure", "tags": [...],
      "source_memory_id": "mem_xxx", "distilled_by": "claude",
      "entity_ids": [...], "created_at": "...", "worth_score": 0.8
    }
  ]
}
```

### 三条提取铁律

1. SOURCE_ONLY — 每条结果必须有 source 字段指向具体记忆 ID
2. EMPTY_IS_OK — 0 匹配时返回 `{"found":0,"items":[],"note":"..."}` 不编造
3. ENRICH — 找到后沿 entity_ids 展开关联记忆

### MCP 工具

- `pack_export(name, tags, memory_ids)` — 导出 JSON
- `pack_import(path, owner)` — 导入并存储到主记忆池
- `pack_recall(query, pack?, strict=true)` — 严格从记忆提取

## 验证

- 导出 → 文件存在且 JSON 合法
- 导入 → memories 进入主池，可 recall
- strict 提取 → 无匹配返回空，有匹配返回带 source 的结果
