# Batch 2 — 域分配 + 存量重分类 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 域标签正确映射 + 存量记忆全部重跑分类管线

**Architecture:** DomainManager.assign() 加 `domain:` 前缀快速路径 + 独立 `memory_reclassify` MCP 工具遍历 SQLite 重分类

**Tech Stack:** Python 3.13, SQLite, pyyaml

## Global Constraints

- 所有变更向后兼容
- worth 历史迁移到 metadata.worth_history
- 旧记忆用 metadata.replaced_by + tags:status:replaced 标记
- batch_size 默认 50
- 新增 memory_reclassify 注册到 MCP server

---

### Task 1: Domain 标签前缀映射

**Files:**
- Modify: `plastic_promise/core/domain_manager.py:373-422`
- Test: `tests/test_domain_manager.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_domain_manager.py (追加)
def test_domain_prefix_tag_maps_correctly():
    """domain:reflecting 标签直接映射到 reflecting 域"""
    from plastic_promise.core.domain_manager import DomainManager
    dm = DomainManager()
    # 确保 reflecting 域存在
    if "reflecting" not in dm.domains:
        dm.domains["reflecting"] = dm._make_domain("reflecting")
        dm.domains["reflecting"].status = "active"
    result = dm.assign(["task:done", "domain:reflecting", "skill:brainstorming"])
    assert result == "reflecting", f"Expected 'reflecting', got '{result}'"

def test_domain_prefix_unknown_creates_candidate():
    """domain:unknown 不存在 → 创建候选域，返回 uncategorized"""
    from plastic_promise.core.domain_manager import DomainManager
    dm = DomainManager()
    result = dm.assign(["domain:fantasy_realm"])
    assert result == "uncategorized"
    # 候选域应被创建
    assert "fantasy_realm" in dm.domains
    assert dm.domains["fantasy_realm"].status == "candidate"

def test_domain_prefix_empty_value_skipped():
    """domain: 空值 → 跳过，回退到常规匹配"""
    from plastic_promise.core.domain_manager import DomainManager
    dm = DomainManager()
    if "building" not in dm.domains:
        dm.domains["building"] = dm._make_domain("building")
        dm.domains["building"].status = "active"
    # 添加 building 域的种子标签以保证匹配
    dm.domains["building"].tags.add("skill:builder")
    result = dm.assign(["domain:", "skill:builder"])
    assert result == "building", f"Expected 'building' via regular match, got '{result}'"

def test_domain_prefix_first_wins():
    """多个 domain: 标签时取第一个有效值"""
    from plastic_promise.core.domain_manager import DomainManager
    dm = DomainManager()
    for name in ("reflecting", "building"):
        if name not in dm.domains:
            dm.domains[name] = dm._make_domain(name)
            dm.domains[name].status = "active"
    result = dm.assign(["domain:reflecting", "domain:building"])
    assert result == "reflecting"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_domain_manager.py::test_domain_prefix_tag_maps_correctly -v
# Expected: FAIL — result != "reflecting"
```

- [ ] **Step 3: 实现 DomainManager.assign() 快速路径**

在 `DomainManager.assign()` 方法中，`with self._lock:` 之下的最开始（`# 1. 统计...` 之前）插入：

```python
    with self._lock:
        # 0. Fast path: domain: 前缀标签直接指定域
        for tag in tags:
            if tag.startswith("domain:"):
                domain_name = tag.split(":", 1)[1].strip()
                if not domain_name:
                    continue  # 跳过空值 "domain:"
                if domain_name in self.domains:
                    dom = self.domains[domain_name]
                    dom.access_count += 1
                    dom.memory_count += 1
                    dom.last_accessed = datetime.datetime.now().isoformat()
                    dom.last_active = datetime.datetime.now().isoformat()
                    self._persist_domain(domain_name)
                    return domain_name
                else:
                    # 创建候选域
                    self._handle_candidate([tag])
                    return "uncategorized"
        
        # 1. 统计每个 active 域（排除 all 和 candidate）匹配的标签数
        scores: dict[str, int] = {}
```

原有的 `# 1. 统计...` 及之后代码不变。

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_domain_manager.py -v -k "domain_prefix" 
# Expected: 4 passed
```

- [ ] **Step 5: 运行全量测试确认无回归**

```bash
python -m pytest tests/ -q
# Expected: all pass
```

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/core/domain_manager.py tests/test_domain_manager.py
git commit -m "feat: domain prefix mapping — domain:xxx tag maps directly to domain"
```

---

### Task 2: memory_reclassify — 存量重分类

**Files:**
- Create: `plastic_promise/mcp/tools/reclassify.py`
- Modify: `plastic_promise/mcp/server.py` (注册新工具)
- Test: `tests/test_memory_reclassify.py`

**Interfaces:**
- Consumes: `_get_fuzzy_buffer(engine)` from `plastic_promise.mcp.tools.memory`
- Produces: `handle_memory_reclassify(engine, args) -> list[TextContent]`

- [ ] **Step 1: 写测试**

```python
# tests/test_memory_reclassify.py
import json, asyncio
from plastic_promise.core.context_engine import ContextEngine

async def _call(engine, args):
    from plastic_promise.mcp.tools.reclassify import handle_memory_reclassify
    r = await handle_memory_reclassify(engine, args)
    return json.loads(r[0].text)

class TestMemoryReclassify:
    def test_reclassify_empty_pool(self):
        """空记忆池返回 0 条重分类。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None  # 禁用 LanceDB 避免去重干扰
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer
            _get_fuzzy_buffer(engine)
            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] == 0
            assert result["remaining"] == 0
        asyncio.run(run())

    def test_reclassify_single_memory_preserves_content(self):
        """重分类后内容不变，domain 被正确分配。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer
            fb = _get_fuzzy_buffer(engine)
            # 先存一条带 domain:reflecting 标签的记忆
            fb.store_urgent(
                content="test content for reclassify",
                entity_ids=["skill:test:1"],
                custom_tags=["domain:reflecting", "task:done"],
                domain_hint="uncategorized",
            )
            fb.process_pipeline()
            # 确认旧 domain 是 uncategorized (当前行为)
            old_mem = list(engine._memories.values())[0]
            assert old_mem["domain"] == "uncategorized"
            
            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] >= 1
            
            # 验证新记忆 domain 被正确分配
            found_reflecting = False
            for mid, mem in engine._memories.items():
                if mem.get("domain") == "reflecting":
                    found_reflecting = True
                    break
            assert found_reflecting, "No memory found with domain=reflecting after reclassify"
        asyncio.run(run())

    def test_reclassify_preserves_worth_history(self):
        """重分类后旧记忆的 worth 历史保留在 metadata.worth_history。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer, handle_memory_store
            
            fb = _get_fuzzy_buffer(engine)
            r = await handle_memory_store(engine, {
                "content": "worth test content",
                "tags": ["domain:building"],
            })
            # 手动设 worth 值模拟历史
            for mid, mem in engine._memories.items():
                if "worth test" in mem.get("content", ""):
                    mem["worth_success"] = 5
                    mem["worth_failure"] = 2
            
            result = await _call(engine, {"batch_size": 10})
            assert result["reclassified"] >= 1
            
            # 找被标记为 replaced 的旧记忆
            found_history = False
            for mid, mem in engine._memories.items():
                meta = mem.get("metadata", {})
                if isinstance(meta, dict) and "worth_history" in meta:
                    wh = meta["worth_history"]
                    assert wh["previous"]["success"] == 5
                    assert wh["previous"]["failure"] == 2
                    found_history = True
                    break
            assert found_history, "worth_history not preserved in metadata"
        asyncio.run(run())

    def test_reclassify_batch_respects_limit(self):
        """batch_size 限制单次处理数量，remaining 反映剩余。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            engine._ldb = None
            from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer, handle_memory_store
            fb = _get_fuzzy_buffer(engine)
            # 存 5 条记忆
            for i in range(5):
                await handle_memory_store(engine, {
                    "content": f"batch test {i}",
                    "tags": ["domain:reflecting"],
                })
            result = await _call(engine, {"batch_size": 2})
            assert result["reclassified"] == 2
            assert result["remaining"] >= 3
        asyncio.run(run())
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_memory_reclassify.py -v
# Expected: ImportError (模块不存在)
```

- [ ] **Step 3: 实现 handle_memory_reclassify**

```python
# plastic_promise/mcp/tools/reclassify.py
"""MCP 工具: memory_reclassify — 存量记忆强制重跑分类管线"""

import json
import datetime
from typing import Any
from mcp.types import TextContent


async def handle_memory_reclassify(engine: Any, args: dict) -> list[TextContent]:
    """强制已有记忆重跑分类管线 (tier + domain + category)。

    遍历 engine._memories 中的记忆，提取 content/entity_ids/tags/source，
    通过 MemoryPipeline 重新处理，保留 worth 历史到新记忆的 metadata。

    Args:
        engine: ContextEngine 实例
        args:
            batch_size: int — 单次处理数量 (默认 50)
            resume_from: str | None — 从指定 memory_id 继续 (断点续传)

    Returns:
        list[TextContent]: reclassified, remaining, skipped, errors 计数
    """
    from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer

    batch_size = args.get("batch_size", 50)
    resume_from = args.get("resume_from", None)

    fb = _get_fuzzy_buffer(engine)
    now = datetime.datetime.utcnow().isoformat()

    reclassified = 0
    skipped = 0
    errors = 0
    remaining = 0

    # 收集待处理记忆 (排除已标记 replaced 的)
    pending = []
    for mid, mem in engine._memories.items():
        if not isinstance(mem, dict):
            continue
        tags = mem.get("tags", [])
        if "status:replaced" in tags:
            skipped += 1
            continue
        # resume_from: 跳过已处理的
        if resume_from and mid <= resume_from:
            skipped += 1
            continue
        pending.append((mid, dict(mem)))  # shallow copy

    remaining = max(0, len(pending) - batch_size)
    batch = pending[:batch_size]

    for mid, mem in batch:
        try:
            content = mem.get("content", "")
            if not content.strip():
                skipped += 1
                continue

            old_tags = list(mem.get("tags", []))
            old_eids = list(mem.get("entity_ids", []))
            old_source = mem.get("source", "user")
            old_worth_s = mem.get("worth_success", 0)
            old_worth_f = mem.get("worth_failure", 0)
            old_access = mem.get("access_count", 0)

            # 通过管线重分类
            fb.store_urgent(
                content=content,
                memory_type=mem.get("memory_type", "experience"),
                source=old_source,
                entity_ids=old_eids,
                custom_tags=old_tags,
                domain_hint=None,  # 让 DomainManager 重新分配
            )
            fb.process_pipeline()

            # 找新创建的记忆 (管线迁移后 engine._memories 中最新的)
            new_mid = None
            for check_mid in engine._memories:
                if (check_mid not in dict(batch)  # 不在本批次中
                        and "status:replaced" not in engine._memories[check_mid].get("tags", [])
                        and content[:50] in engine._memories[check_mid].get("content", "")):
                    new_mid = check_mid
                    break

            # 在新记忆上保留 worth 历史
            if new_mid and new_mid in engine._memories:
                new_mem = engine._memories[new_mid]
                if "metadata" not in new_mem or not isinstance(new_mem.get("metadata"), dict):
                    new_mem["metadata"] = {}
                new_mem["metadata"]["worth_history"] = {
                    "previous": {"success": old_worth_s, "failure": old_worth_f},
                    "previous_access_count": old_access,
                    "reclassified_at": now,
                }

            # 标记旧记忆为 replaced
            engine._memories[mid]["metadata"] = engine._memories[mid].get("metadata", {})
            if not isinstance(engine._memories[mid]["metadata"], dict):
                engine._memories[mid]["metadata"] = {}
            engine._memories[mid]["metadata"]["replaced_by"] = new_mid
            old_tags_replaced = list(engine._memories[mid].get("tags", []))
            if "status:replaced" not in old_tags_replaced:
                old_tags_replaced.append("status:replaced")
            engine._memories[mid]["tags"] = old_tags_replaced

            # SQLite 同步
            sqlite = getattr(engine, '_sqlite', None)
            if sqlite is not None:
                try:
                    sqlite._conn.execute(
                        "UPDATE memories SET tags = ?, metadata = ? WHERE id = ?",
                        (json.dumps(old_tags_replaced),
                         json.dumps(engine._memories[mid]["metadata"]), mid)
                    )
                    if new_mid:
                        import json as _json
                        sqlite._conn.execute(
                            "UPDATE memories SET metadata = ? WHERE id = ?",
                            (_json.dumps(new_mem["metadata"]), new_mid)
                        )
                    sqlite._conn.commit()
                except Exception:
                    pass

            reclassified += 1
        except Exception:
            errors += 1

    return [TextContent(type="text", text=json.dumps({
        "reclassified": reclassified,
        "remaining": remaining,
        "skipped": skipped,
        "errors": errors,
        "batch_size": batch_size,
        "last_id": batch[-1][0] if batch else None,
        "total": len(engine._memories),
    }, ensure_ascii=False))]
```

- [ ] **Step 4: 运行测试**

```bash
python -m pytest tests/test_memory_reclassify.py -v
# Expected: 4 passed
```

- [ ] **Step 5: 注册 MCP 工具**

在 `plastic_promise/mcp/server.py` 的 TOOLS 列表末尾添加：

```python
Tool(
    name="memory_reclassify",
    description="强制已有记忆重跑分类管线——重新分配 tier、domain、category。支持分批处理。",
    inputSchema={
        "type": "object",
        "properties": {
            "batch_size": {"type": "integer", "description": "单次处理数量 (默认 50)"},
            "resume_from": {"type": "string", "description": "从指定 memory_id 继续（断点续传）"},
        },
    },
),
```

并在 dispatch 部分添加：

```python
elif name == "memory_reclassify":
    from plastic_promise.mcp.tools.reclassify import handle_memory_reclassify
    return await handle_memory_reclassify(engine, arguments)
```

- [ ] **Step 6: 全量测试**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 7: 执行存量重分类**

```bash
python -c "
import asyncio, json
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.mcp.tools.reclassify import handle_memory_reclassify

async def main():
    engine = ContextEngine(use_sqlite=True)
    engine._ldb = None  # 禁用向量去重避免干扰
    r = await handle_memory_reclassify(engine, {'batch_size': 50})
    print(json.loads(r[0].text))

asyncio.run(main())
"
```

- [ ] **Step 8: Commit**

```bash
git add plastic_promise/mcp/tools/reclassify.py plastic_promise/mcp/server.py tests/test_memory_reclassify.py
git commit -m "feat: memory_reclassify — bulk re-run classification pipeline on existing memories"
```

---

### Task 3: 端到端验证

- [ ] **Step 1: 全量测试**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 2: 验证 domain 映射生效**

```bash
python -c "
import asyncio
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.mcp.tools.memory import handle_memory_store
import json

async def main():
    engine = ContextEngine(use_sqlite=False)
    engine._ldb = None
    from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer
    _get_fuzzy_buffer(engine)
    r = await handle_memory_store(engine, {
        'content': 'domain test',
        'tags': ['domain:reflecting', 'task:done'],
    })
    for mid, mem in engine._memories.items():
        if 'domain test' in mem.get('content', ''):
            assert mem['domain'] == 'reflecting', f'FAIL: {mem[\"domain\"]}'
            print(f'PASS: domain={mem[\"domain\"]}')
            break

asyncio.run(main())
"
```

- [ ] **Step 3: 重启 MCP 服务器**

```bash
python -c "import psutil; [p.terminate() or p.wait(timeout=5) for p in [psutil.Process(c.pid) for c in psutil.net_connections() if c.laddr.port==9020 and c.status=='LISTEN']]" 2>/dev/null
sleep 1
python -m plastic_promise.mcp.server --sse 9020 > /dev/null 2>&1 &
sleep 2
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read().decode())"
```

- [ ] **Step 4: Commit**

```bash
git add -A && git status
git commit -m "test: Batch 2 end-to-end verification — domain mapping + reclassify"
```
