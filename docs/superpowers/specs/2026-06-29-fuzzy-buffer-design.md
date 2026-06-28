# 模糊缓存区（Fuzzy Buffer）设计

**日期**: 2026-06-29
**类型**: 新功能

## 背景

当本地嵌入服务（Ollama）不可用时，记忆存储会因 FallbackEmbedder 使用零向量而丢失语义精度。模糊缓存区提供"先存后补"机制：紧急存储时只打临时标签放入缓存，空闲时后台完成嵌入和分类后迁移到主记忆池。

## 架构

```
新文件: plastic_promise/memory/fuzzy_buffer.py

FuzzyBuffer (作为 ContextEngine 的附属模块)
├── 细分区（按 stage 字段在同一个内部 dict 中追踪）
│   ├── raw        — 原始内容 + 临时标签
│   ├── tagged     — 关键词提取完成
│   ├── embedded   — 向量已生成
│   └── classified — 分层判定完成，等待迁移
```

### 触发方式（混合）

- **Cron**: `health_scan.py` 每次运行时检查缓存积压，有则自动处理
- **MCP 手动**: `fuzzy_process` 工具立即触发全流水线
- **MCP 查询**: `fuzzy_status` 查看各区统计

## 数据模型

每条缓存记录：
```python
{
    "memory_id": "fuzzy_<uuid8>",
    "content": str,
    "memory_type": str,
    "source": str,
    "stage": "raw" | "tagged" | "embedded" | "classified",
    "tags": [str],          # CJK 关键词
    "vector": None | [float],  # 嵌入后填充
    "tier": None | "L1" | "L3",  # 分类后填充
    "created_at": str,
    "processed_at": None | str,
}
```

## 四阶段流水线

### raw → tagged
- noise_filter 检查，过滤纯噪音
- CJK 大五元提取前 5 个关键词作为临时标签

### tagged → embedded
- 批量模式：攒够 10 条或距上次处理超 5 分钟
- 调用 `embedder.embed_batch()` 一次性嵌入
- 嵌入失败继续留在 tagged 区

### embedded → classified
- 调用 `MemoryTierManager.classify_tier()` 判定 L1/L3
- 设置 tier 字段

### classified → 迁移
- 调用 `RecMem.store()` 写入主记忆池
- 从 fuzzy buffer 中移除记录
- 统计本次迁移数量

## MCP 工具

### fuzzy_status
返回 `{total, by_stage: {raw, tagged, embedded, classified}, oldest_pending}`

### fuzzy_process
触发全流水线（raw→tagged→embedded→classified→迁移），返回处理统计。

## Cron 集成

`health_scan.py` 追加 fuzzy buffer 检查：有积压时打印日志但不阻塞。

## 验证
- Ollama 离线时 memory_store → 进入 raw 区
- fuzzy_process → 逐步推进各阶段
- 最终迁移到主池后 memory_recall 可检索到
