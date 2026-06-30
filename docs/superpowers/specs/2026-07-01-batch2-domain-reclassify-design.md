# Batch 2 — 域分配 + 存量重分类

## Context

MVP (Batch 1) 完成后管道全链路可用。两个遗留问题：

| # | 问题 | 现状 | 目标 |
|---|------|------|------|
| 1 | `domain:*` 标签未映射 | `domain:reflecting` → `uncategorized` | → `"reflecting"` |
| 2 | 存量记忆未分类 | 20+ 条记忆 tier=L1, domain=uncategorized, category=other | 全部重跑管线分类 |

注：中文"乱码"是终端 GBK 显示问题，SQLite 数据完整 (UTF-8)，本次不修。

## Fix 1: Domain 标签前缀映射

### 文件: `plastic_promise/core/domain_manager.py`

`DomainManager.assign(tags)` 目前通过预定义域标签匹配。添加前缀规则：

```
tag.startswith("domain:") → domain_name = tag.split(":")[1]
```

实现：在 `assign()` 方法开头加快速路径——扫描 tags，找到第一个 `domain:` 前缀的 tag，提取域名。若域名在 `self.domains` 中存在则直接返回；不存在则创建候选域。

### 文件: `plastic_promise/memory/pipeline.py`

`_process_tagged_to_classified` (line 274-278) 的 domain 分配已调 `self._dm.assign(tags)`，无需修改——改 `DomainManager` 即可生效。

## Fix 2: 存量重分类

### 新增: `plastic_promise/mcp/tools/memory.py` — `handle_memory_reclassify`

新 MCP 工具，遍历 SQLite 中所有记忆，重置分类字段并通过管线重分类：

```python
async def handle_memory_reclassify(engine, args):
    # 1. 遍历 engine._memories
    # 2. 对每条记忆: 清空 tier/domain/category
    # 3. 重新通过 MemoryPipeline.store_urgent() + process_pipeline()
    # 4. 返回 reclassified, skipped, errors 计数
```

或者更轻量：直接用现有 `handle_memory_update` 触发重分类（reset_worth 时同步触发 domain/tier 重算）。但当前 `update` 不改 domain，用独立工具更清晰。

### 重分类逻辑

```
for each memory in SQLite:
    1. 保留 content, entity_ids, tags, source
    2. 通过 MemoryPipeline 重新处理:
       store_urgent(content, tags=tags, entity_ids=entity_ids) 
       → process_pipeline()
       → 新记忆入池 (tier 分类 + domain 分配 + category 提取)
    3. 标记旧记忆为 replaced，保留 worth 历史
```

## 验收标准

1. `memory_store(tags=["domain:reflecting"])` → 存储后 domain=`"reflecting"` (非 `uncategorized`)
2. `memory_reclassify()` 执行后，存量记忆的 domain 不再全是 `uncategorized`
3. 全量测试通过 (252 tests)

## 不变更

- 终端 GBK 显示问题 (用户侧 `chcp 65001` 解决)
- SQLite/LanceDB 存储编码 (数据本身完好)
- 现有 MCP 工具签名
