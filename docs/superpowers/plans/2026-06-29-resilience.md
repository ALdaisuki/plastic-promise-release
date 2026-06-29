# Resilience System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resilience upgrade — disaster recovery, cross-version compatibility, silent failure protection, plus tool consolidation 39→29.

**Architecture:** Three independent mechanisms sharing schema_version + _dm_ok infrastructure. Tool consolidation is a mapping pass over server.py. All Python, all in plastic_promise/.

**Tech Stack:** Python 3.10+, SQLite WAL, threading.RLock, gzip streaming

## Global Constraints

- 原则 #1 奥卡姆剃刀: 不引入新依赖（不装 Alembic），迁移链硬编码
- 原则 #2 可查可透明: audit_log 记录所有域变更和恢复事件
- 向后兼容: 现有 MCP 工具调用者不受影响（旧名称保留 30 天别名）
- 快速失败: DomainManager 不可用时所有域操作返回降级结果，不抛异常
- 线程安全: rebuild 操作获取写锁
- fuzzy 可见性: 积压计数保留在 system_stats 和 memory_stats 中

---

### Task 1: schema_version 基础设施 + _dm_ok 降级开关

**Files:**
- Modify: `plastic_promise/core/domain_manager.py`
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Produces: `DomainManager.SCHEMA_VERSION = 2`, `_run_migrations()`, auto-rebuild guard
- Produces: `ContextEngine._dm_ok: bool`

- [ ] **Step 1: 在 DomainManager._init_schema 中加 schema_version 表**

在 `_init_schema()` 的 `CREATE TABLE` 语句末尾追加：

```python
self._conn.executescript("""
    -- ... existing domains, audit_log tables ...
    
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL
    );
""")
self._conn.commit()
```

- [ ] **Step 2: 添加迁移链 + 启动检查**

在 DomainManager 中添加类变量和迁移方法：

```python
# 类级别常量
SCHEMA_VERSION = 2

MIGRATION_CHAIN = {
    1: "_migrate_v1_to_v2",
}
```

在 `__init__` 末尾（`_load_from_db()` 之后）添加：

```python
self._run_migrations()
```

实现 `_run_migrations()`:

```python
def _run_migrations(self):
    """检查 schema 版本并执行迁移链。"""
    try:
        row = self._conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        current = row[0] if row and row[0] else 0
    except Exception:
        current = 0  # 表不存在 = v0

    if current == self.SCHEMA_VERSION:
        return  # 最新，正常启动

    if current > self.SCHEMA_VERSION:
        raise RuntimeError(
            f"DB schema version {current} > code version {self.SCHEMA_VERSION}. "
            f"请升级 Plastic Promise 或使用旧版 DB。"
        )

    # 依次执行迁移链
    for v in range(current + 1, self.SCHEMA_VERSION + 1):
        method_name = self.MIGRATION_CHAIN.get(v)
        if method_name:
            getattr(self, method_name)()
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (v,)
            )
            self._conn.commit()

def _migrate_v1_to_v2(self):
    """v1→v2: 添加 tags/domain 列, 建 domains/audit_log 表"""
    for col, dtype, default in [
        ("tags", "TEXT", "'[]'"),
        ("domain", "TEXT", "'uncategorized'"),
    ]:
        try:
            self._conn.execute(
                f"ALTER TABLE memories ADD COLUMN {col} {dtype} NOT NULL DEFAULT {default}"
            )
        except Exception:
            pass  # 列已存在
    # domains 和 audit_log 在 _init_schema 中已用 IF NOT EXISTS 创建
```

- [ ] **Step 3: 加 auto-rebuild 守卫**

在 DomainManager.__init__ 中，_load_from_db() 之后，检查是否需要自动重建：

```python
# Auto-rebuild guard: domains 表空但 memories 有数据 → 自动重建
try:
    row = self._conn.execute("SELECT COUNT(*) FROM domains WHERE status != 'candidate'").fetchone()
    domain_count = row[0] if row else 0
    mem_count_row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    mem_count = mem_count_row[0] if mem_count_row else 0

    if domain_count == 0 and mem_count > 0:
        import time as _time
        logging.warning(
            f"domains 表为空但 memories 表有 {mem_count} 条记忆。"
            f"将在 5 秒后自动重建域图谱。按 Ctrl+C 取消。"
        )
        _time.sleep(5)
        self.rebuild_from_memories(memories_source="sqlite")
except Exception:
    pass
```

- [ ] **Step 4: 在 ContextEngine.__init__ 中加降级开关**

修改 `ContextEngine.__init__` 中的 DomainManager 初始化（约第206行）：

```python
# 当前:
from plastic_promise.core.domain_manager import DomainManager
self._dm = DomainManager()
self._domain_hint: Optional[str] = None

# 改为:
try:
    from plastic_promise.core.domain_manager import DomainManager
    self._dm = DomainManager()
    self._dm_ok = True
except Exception as e:
    logging.error(f"DomainManager init failed: {e} — domain features disabled")
    self._dm = None
    self._dm_ok = False
self._domain_hint: Optional[str] = None
```

- [ ] **Step 5: 验证**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" python -c "
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
print('_dm_ok:', e._dm_ok)
print('dm domains:', len(e._dm.stats()) if e._dm else 'N/A')
print('schema version:', e._dm._conn.execute('SELECT MAX(version) FROM schema_version').fetchone() if e._dm else 'N/A')
"
```

Expected: `_dm_ok: True, dm domains: >=7, schema version: 2`

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/core/domain_manager.py plastic_promise/core/context_engine.py
git commit -m "feat: schema_version migration chain + _dm_ok degradation switch + auto-rebuild guard"
```

---

### Task 2: rebuild_from_memories() 灾难恢复

**Files:**
- Modify: `plastic_promise/core/domain_manager.py`
- Create: `tests/test_rebuild.py`

**Interfaces:**
- Produces: `DomainManager.rebuild_from_memories(memories_source) -> dict`
- Produces: `handle_domain_rebuild` (part of domain tool consolidation in Task 4)

- [ ] **Step 1: 创建测试文件**

`tests/test_rebuild.py`:

```python
"""rebuild_from_memories 恢复测试"""
import pytest
import json
from plastic_promise.core.domain_manager import DomainManager


class TestRebuild:
    def test_rebuild_from_scratch(self):
        """模拟 domains 表清空，从 memories 的 tags 重建"""
        dm = DomainManager()
        
        # 模拟: 注入带 tags 的记忆到引擎
        test_memories = [
            {"id": "m1", "tags": ["coding", "python", "debug"], "domain": "building"},
            {"id": "m2", "tags": ["design", "architect", "system"], "domain": "designing"},
            {"id": "m3", "tags": ["audit", "reflect", "lesson"], "domain": "reflecting"},
        ]
        
        # 清空域表模拟损坏
        dm._conn.execute("DELETE FROM domains")
        dm._conn.commit()
        dm.domains.clear()
        
        # 重建
        result = dm.rebuild_from_memories(memories_source=test_memories)
        assert result["restored_domains"] >= 3
        # 预定义域应恢复
        assert "building" in dm.domains
        assert "designing" in dm.domains
        
    def test_rebuild_preserves_predefined_domains(self):
        """重建后预定义域仍存在"""
        dm = DomainManager()
        result = dm.rebuild_from_memories(memories_source=[])
        stats = dm.stats()
        required = {"building", "fixing", "designing", "reflecting", "governing", "connecting", "all"}
        assert required.issubset(set(stats.keys()))
        
    def test_rebuild_writes_audit_log(self):
        """重建事件写入审计日志"""
        dm = DomainManager()
        before = dm._count_audit_log()
        dm.rebuild_from_memories(memories_source=[
            {"id": "m1", "tags": ["code"], "domain": "building"}
        ])
        after = dm._count_audit_log()
        assert after > before
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_rebuild.py -v
```

Expected: FAIL (rebuild_from_memories 未实现)

- [ ] **Step 3: 实现 rebuild_from_memories**

在 `DomainManager` 类中添加：

```python
def rebuild_from_memories(self, memories_source=None) -> dict:
    """从记忆的 tags 字段全量逆向重建域联邦图谱。
    
    Args:
        memories_source: 可选 list[dict]，每项含 id, tags, domain。
                        None = 从 SQLite memories 表读取。
    Returns:
        {"restored_domains": int, "tags_indexed": int}
    """
    import json
    from collections import Counter
    
    with self._lock:
        if memories_source is None:
            rows = self._conn.execute(
                "SELECT id, tags FROM memories"
            ).fetchall()
            memories_source = [
                {"id": r[0], "tags": json.loads(r[1]) if isinstance(r[1], str) else (r[1] or [])}
                for r in rows
            ]
        
        # Phase 1: 标签共现统计
        tag_cooccur = Counter()
        tag_freq = Counter()
        all_tags = set()
        
        for mem in memories_source:
            tags = mem.get("tags", [])
            if isinstance(tags, str):
                tags = json.loads(tags) if tags else []
            for t in tags:
                tag_freq[t] += 1
                all_tags.add(t)
            for i, t1 in enumerate(tags):
                for t2 in tags[i+1:]:
                    key = tuple(sorted([t1, t2]))
                    tag_cooccur[key] += 1
        
        # Phase 2: 聚类 (cooccur > 3 → 同域)
        clusters = self._cluster_by_cooccurrence(tag_cooccur, tag_freq, all_tags)
        
        # Phase 3: 合并入预定义域
        merged_domains = {}
        for name, cfg in PREDEFINED_DOMAINS.items():
            if name == "all":
                continue
            merged_domains[name] = dict(cfg)
            merged_domains[name]["tags"] = set(cfg["tags"])
        
        for cluster_tags in list(clusters):
            best_name = None
            best_jac = 0.0
            for dname, dcfg in merged_domains.items():
                inter = len(cluster_tags & dcfg["tags"])
                union = len(cluster_tags | dcfg["tags"])
                jac = inter / union if union > 0 else 0.0
                if jac > best_jac:
                    best_jac = jac
                    best_name = dname
            if best_jac > 0.4 and best_name:
                merged_domains[best_name]["tags"].update(cluster_tags)
            else:
                name = max(cluster_tags, key=lambda t: tag_freq.get(t, 0))
                merged_domains[name] = {
                    "score": 0.5, "tags": cluster_tags,
                    "principle_ids": [], "status": "active",
                }
        
        # Phase 4: 写入
        self.domains.clear()
        for name, cfg in merged_domains.items():
            self.domains[name] = DomainInfo(
                name=name,
                score=cfg["score"],
                tags=cfg["tags"],
                principle_ids=cfg.get("principle_ids", []),
                status=cfg.get("status", "active"),
            )
            self._persist_domain(name)
        
        # Phase 5: 重建索引
        self._rebuild_tag_index()
        
        # Phase 6: 审计
        self._write_audit_log("domain_rebuild", {
            "source": "memories table",
            "domains_restored": len(merged_domains),
            "tags_total": len(all_tags),
        })
        
        return {"restored_domains": len(merged_domains), "tags_indexed": len(all_tags)}


def _cluster_by_cooccurrence(self, cooccur, tag_freq, all_tags):
    """基于标签共现频次聚类。cooccur > 3 → 认为属于同一域候选"""
    parent = {}
    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    
    for (t1, t2), count in cooccur.items():
        if count > 3:
            union(t1, t2)
    
    clusters = {}
    for tag in all_tags:
        root = find(tag)
        if root not in clusters:
            clusters[root] = set()
        clusters[root].add(tag)
    
    return [c for c in clusters.values() if len(c) >= 2]
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_rebuild.py -v
```

Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/domain_manager.py tests/test_rebuild.py
git commit -m "feat: rebuild_from_memories() — full domain graph recovery from memory tags"
```

---

### Task 3: 工具合并 — Domain + Defense + Audit + Reflection + System

**Files:**
- Modify: `plastic_promise/mcp/tools/domain.py`
- Modify: `plastic_promise/mcp/tools/audit_defense.py`
- Modify: `plastic_promise/mcp/tools/reflection.py`
- Modify: `plastic_promise/mcp/tools/management.py`
- Modify: `plastic_promise/mcp/tools/memory.py`
- Modify: `plastic_promise/mcp/server.py`

**Interfaces:**
- Produces: `handle_domain(action=...)` 单入口替代 4 个独立工具
- Produces: `handle_audit_run` 吞并 audit_report
- Produces: `handle_defense` 吞并 defense_status
- Produces: `handle_scarf_reflect` 吞并 inertia_check
- Produces: `handle_system` 吞并 system_backup/system_migrate
- Removes: fuzzy_status/fuzzy_process 独立入口（删工具注册和路由，handler 保留以备直接调用）

- [ ] **Step 1: 改写 domain.py — 统一入口**

用 `action` 参数路由替代 4 个独立函数：

```python
async def handle_domain(engine: Any, args: dict) -> list[TextContent]:
    """域联邦统一入口。action: stats|merge|unmerge|rename|rebuild"""
    action = args.get("action", "stats")
    dm = getattr(engine, '_dm', None)
    if dm is None:
        return [TextContent(type="text", text=json.dumps(
            {"error": "DomainManager not available (_dm_ok=False)"}, ensure_ascii=False))]
    
    try:
        if action == "stats":
            return [TextContent(type="text", text=json.dumps(dm.stats(), ensure_ascii=False, indent=2))]
        elif action == "merge":
            ok = dm.merge(args["source"], args["target"])
            return [TextContent(type="text", text=json.dumps({"merged": ok, "source": args["source"], "target": args["target"]}, ensure_ascii=False))]
        elif action == "unmerge":
            ok = dm.unmerge(args["source"])
            return [TextContent(type="text", text=json.dumps({"unmerged": ok, "source": args["source"]}, ensure_ascii=False))]
        elif action == "rename":
            ok = dm.rename(args["old_name"], args["new_name"])
            return [TextContent(type="text", text=json.dumps({"renamed": ok, "old_name": args["old_name"], "new_name": args["new_name"]}, ensure_ascii=False))]
        elif action == "rebuild":
            result = dm.rebuild_from_memories()
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown action: {action}"}, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]
```

- [ ] **Step 2: 改写 audit_defense.py — defense 统一入口**

在 audit_defense.py 末尾添加：

```python
async def handle_defense(engine: Any, args: dict) -> list[TextContent]:
    """防线统一入口。action: get|history|adjust|status"""
    action = args.get("action", "get")
    if action == "status":
        return await handle_defense_status(engine, args)
    else:
        return await handle_defense_trust(engine, args)
```

- [ ] **Step 3: 改写 audit_defense.py — audit_run 吞并 audit_report**

修改 `handle_audit_run`，添加 `action` 参数：

```python
async def handle_audit_run(engine: Any, args: dict) -> list[TextContent]:
    action = args.get("action", "full")
    if action == "report":
        return await handle_audit_report(engine, args)
    # 原有 full audit 逻辑不变
    ...
```

- [ ] **Step 4: 改写 reflection.py — scarf_reflect 吞并 inertia_check**

修改 `handle_scarf_reflect`，添加 `mode` 参数：

```python
async def handle_scarf_reflect(engine: Any, args: dict) -> list[TextContent]:
    mode = args.get("mode", "standard")
    if mode == "inertia":
        return await handle_inertia_check(engine, args)
    # 原有 SCARF 逻辑不变
    ...
```

- [ ] **Step 5: 改写 management.py — system 统一入口**

添加统一 system handler：

```python
async def handle_system(engine: Any, args: dict) -> list[TextContent]:
    """系统工具统一入口。action: stats|backup|migrate"""
    action = args.get("action", "stats")
    if action == "backup":
        return await handle_system_backup(engine, args)
    elif action == "migrate":
        return await handle_system_migrate(engine, args)
    else:
        # stats 模式: 合并 system_stats + fuzzy 积压计数
        result = await handle_system_stats(engine, args)
        # 追加 fuzzy buffer 积压信息
        try:
            fb = _get_fuzzy_buffer(engine)
            if fb:
                buf_stats = fb.stats()
                parsed = json.loads(result[0].text) if result else {}
                parsed["fuzzy_buffer"] = buf_stats
                result = [TextContent(type="text", text=json.dumps(parsed, ensure_ascii=False, indent=2))]
        except Exception:
            pass
        return result
```

- [ ] **Step 6: 更新 memory.py — fuzzy_status/fuzzy_process 处理器保留但标记内部**

保留 `handle_fuzzy_status` 和 `handle_fuzzy_process` 函数不删除（可能被内部调用），但在文件头部注释标记 `# internal — not exposed as MCP tool`。

在 `memory_stats` 返回中添加 pipeline 状态：

```python
# memory_stats handler 中添加 fuzz buffer 统计
try:
    from plastic_promise.memory.pipeline import MemoryPipeline
    fb = _get_fuzzy_buffer(engine)
    if fb:
        result["fuzzy_buffer"] = fb.stats()
except Exception:
    pass
```

- [ ] **Step 7: 更新 server.py — 工具声明 + 路由**

关键变更：
- **删除**: fuzzy_status, fuzzy_process, defense_status, audit_report, inertia_check, system_backup, system_migrate, domain_stats, domain_merge, domain_unmerge, domain_rename (11 个工具)
- **新增/修改**: domain(action=...), defense(action=...), audit_run(action=...), scarf_reflect(mode=...), system(action=...)

Tool 定义变更：

```python
# 删: fuzzy_status (line 171), fuzzy_process (line 179)
# 删: domain_stats/merge/unmerge/rename (在 domain 工具组)
# 删: defense_status (line 358)
# 删: audit_report (line 334)
# 删: inertia_check (line 382)
# 删: system_backup (line 417), system_migrate (line 428)

# 改: domain 工具组
Tool(
    name="domain",
    description="域联邦统一入口: action=stats|merge|unmerge|rename|rebuild",
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "stats|merge|unmerge|rename|rebuild"},
            "source": {"type": "string"},
            "target": {"type": "string"},
            "old_name": {"type": "string"},
            "new_name": {"type": "string"},
        },
        "required": ["action"],
    },
),

# 改: audit_run
Tool(
    name="audit_run",
    description="执行七维审计: action=full(默认)|report",
    inputSchema={
        "type": "object",
        "properties": {
            "scope": {"type": "string"},
            "time_range_hours": {"type": "integer"},
            "action": {"type": "string", "description": "full|report"},
        },
    },
),

# 改: defense -> 吞并 defense_status
Tool(
    name="defense",
    description="防线管理: action=get|history|adjust|status",
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "get|history|adjust|status"},
            "delta": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["action"],
    },
),

# 改: scarf_reflect -> 吞并 inertia_check
Tool(
    name="scarf_reflect",
    description="SCARF 五维自省: mode=standard|inertia",
    inputSchema={
        "type": "object",
        "properties": {
            "context": {"type": "string"},
            "dimensions": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "description": "standard|inertia"},
        },
        "required": ["context"],
    },
),

# 改: system -> 吞并 backup/migrate, 含 fuzz 积压
Tool(
    name="system",
    description="系统工具: action=stats|backup|migrate。stats 含模糊缓存积压计数。",
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "stats|backup|migrate"},
            "format": {"type": "string"},
            "source_path": {"type": "string"},
            "source_type": {"type": "string"},
        },
        "required": ["action"],
    },
),
```

路由变更（删除旧路由，添加新路由）：

```python
# 删除:
# elif name == "fuzzy_status": ...
# elif name == "fuzzy_process": ...
# elif name == "defense_status": ...
# elif name == "audit_report": ...
# elif name == "inertia_check": ...
# elif name == "system_backup": ...
# elif name == "system_migrate": ...

# 新增/修改路由:
elif name == "domain":
    from plastic_promise.mcp.tools.domain import handle_domain
    return await handle_domain(engine, arguments)
elif name == "defense":
    from plastic_promise.mcp.tools.audit_defense import handle_defense
    return await handle_defense(engine, arguments)
elif name == "scarf_reflect":
    from plastic_promise.mcp.tools.reflection import handle_scarf_reflect
    return await handle_scarf_reflect(engine, arguments)
elif name == "system":
    from plastic_promise.mcp.tools.management import handle_system
    return await handle_system(engine, arguments)

# audit_run 路由修改为:
elif name == "audit_run":
    from plastic_promise.mcp.tools.audit_defense import handle_audit_run
    return await handle_audit_run(engine, arguments)
```

- [ ] **Step 8: 验证工具数量**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" python -c "import asyncio; from plastic_promise.mcp.server import list_tools; tools = asyncio.run(list_tools()); print(f'{len(tools)} tools'); [print(f'  {t.name}') for t in sorted(tools, key=lambda t: t.name)]"
```

Expected: `29 tools`

- [ ] **Step 9: Commit**

```bash
git add plastic_promise/mcp/tools/domain.py plastic_promise/mcp/tools/audit_defense.py plastic_promise/mcp/tools/reflection.py plastic_promise/mcp/tools/management.py plastic_promise/mcp/tools/memory.py plastic_promise/mcp/server.py
git commit -m "refactor: tool consolidation 39→29 — domain/defense/audit/reflection/system unified, fuzzy internal"
```

---

### Task 4: pack 升级 — 流式导出 + strategy + version_mapper

**Files:**
- Modify: `plastic_promise/mcp/tools/management.py`
- Create: `plastic_promise/core/pack_index.py`

**Interfaces:**
- Produces: `pack_export_streaming(name, output_path, tags=None)` — 流式 gzip 写盘
- Produces: `pack_import(path, strategy="skip")` — strategy 支持 skip/replace/merge + version_mapper
- Produces: `pack_tag_index: dict[str, set[str]]` — 独立于 DomainManager 的检索索引

- [ ] **Step 1: 创建 pack_index.py**

`plastic_promise/core/pack_index.py`:

```python
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
        
        if engine and hasattr(engine, '_sqlite') and engine._sqlite:
            rows = engine._sqlite._conn.execute(
                "SELECT id, content, memory_type, source, tags, domain, tier FROM memories"
            ).fetchall()
            for row in rows:
                tags_list = json.loads(row[4]) if isinstance(row[4], str) else (row[4] or [])
                if tags and not (set(tags) & set(tags_list)):
                    continue  # 标签过滤
                if not first:
                    f.write(',\n')
                else:
                    first = False
                json.dump({
                    "id": row[0], "content": row[1], "memory_type": row[2],
                    "source": row[3], "tags": tags_list, "domain": row[5] or "",
                    "tier": row[6],
                }, f, ensure_ascii=False)
                count += 1
        elif engine:
            for mid, mem in engine._memories.items():
                mem_tags = mem.get("tags", [])
                if tags and not (set(tags) & set(mem_tags)):
                    continue
                if not first:
                    f.write(',\n')
                else:
                    first = False
                json.dump({
                    "id": mid, "content": mem.get("content", ""),
                    "memory_type": mem.get("memory_type", ""),
                    "source": mem.get("source", ""),
                    "tags": mem_tags,
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
        
        existing = engine._memories.get(mid)
        if existing:
            if strategy == "skip":
                skipped += 1
                continue
            elif strategy == "replace":
                engine._memories[mid] = {
                    "id": mid, "content": mem["content"],
                    "memory_type": mem.get("memory_type", "experience"),
                    "source": mem.get("source", "user"),
                    "tags": mem.get("tags", []),
                    "domain": new_domain,
                }
                imported += 1
            elif strategy == "merge":
                old_tags = set(existing.get("tags", []))
                new_tags = set(mem.get("tags", []))
                existing["tags"] = list(old_tags | new_tags)
                existing["domain"] = new_domain  # 包内 domain 为准
                merged += 1
        else:
            data = {
                "id": mid, "content": mem["content"],
                "memory_type": mem.get("memory_type", "experience"),
                "source": mem.get("source", "user"),
                "tags": mem.get("tags", []),
                "domain": new_domain,
                "tier": mem.get("tier", "L1"),
                "owner": owner or mem.get("owner", ""),
            }
            engine._memories[mid] = data
            if engine._sqlite:
                engine._sqlite.upsert(mid, data)
            imported += 1
    
    return {"imported": imported, "skipped": skipped, "merged": merged,
            "version": pack_version}
```

- [ ] **Step 2: 更新 management.py 中的 handle_pack_export/handle_pack_import**

修改 `handle_pack_export` 使用流式导出：

```python
async def handle_pack_export(engine: Any, args: dict) -> list[TextContent]:
    from plastic_promise.core.pack_index import pack_export_streaming
    name = args["name"]
    path = args.get("path", f"{name}.json.gz")
    tags = args.get("tags")
    result = pack_export_streaming(name, path, engine, tags)
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
```

修改 `handle_pack_import` 支持 strategy：

```python
async def handle_pack_import(engine: Any, args: dict) -> list[TextContent]:
    from plastic_promise.core.pack_index import pack_import_with_strategy
    path = args["path"]
    strategy = args.get("strategy", "skip")
    owner = args.get("owner", "")
    result = pack_import_with_strategy(path, engine, strategy, owner)
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
```

- [ ] **Step 3: 更新 server.py 中的 pack_import tool 声明**

在 `pack_import` 的 inputSchema 中添加 strategy：

```python
Tool(
    name="pack_import",
    description="导入经验包。strategy: skip(默认)|replace|merge。merge 时 domain 以包内为准。",
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "owner": {"type": "string"},
            "strategy": {"type": "string", "description": "skip|replace|merge"},
        },
        "required": ["path"],
    },
),
```

- [ ] **Step 4: 验证流式导出**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.pack_index import pack_export_streaming
e = ContextEngine()
result = pack_export_streaming('test', '/tmp/test_pack.json.gz', e)
print(result)
"
```

Expected: `{"path": "...", "count": N}`

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/pack_index.py plastic_promise/mcp/tools/management.py plastic_promise/mcp/server.py
git commit -m "feat: streaming pack_export + pack_import strategy/version_mapper + pack_index"
```

---

### Task 5: principle_activate domain_hint + principle_inherit 行为域

**Files:**
- Modify: `plastic_promise/mcp/tools/principles.py`
- Modify: `plastic_promise/mcp/server.py`

- [ ] **Step 1: principle_activate 加 domain_hint**

在 `handle_principle_activate` 的 args 解析中添加：

```python
domain_hint = args.get("domain_hint", None)

# 在过滤逻辑末尾 (max_p 截断之后) 添加:
if domain_hint and domain_hint != "all":
    principles = [p for p in principles if p["domain"] in (domain_hint, "all")]
    # all 域原则始终纳入 (通用型)
```

- [ ] **Step 2: principle_inherit 支持行为域**

在 `handle_principle_inherit` 中扩展 source_domain 验证：

```python
# 允许任意行为域作为 source (不仅 work/life):
# source_domain: building, designing, reflecting, governing, fixing, connecting
# 行为域 → all: 原则扩散到全域
```

- [ ] **Step 3: 更新 server.py tool 声明**

更新 `principle_activate` 的 inputSchema 添加 domain_hint：

```python
"domain_hint": {"type": "string", "description": "可选，限定域: building|fixing|designing|reflecting|governing|connecting|all"},
```

- [ ] **Step 4: 验证**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" python -c "
from plastic_promise.core.constants import CORE_PRINCIPLES
# 模拟 domain_hint='building'
result = [p for p in CORE_PRINCIPLES if p['domain'] in ('building', 'all')]
print([p['name'] for p in result])
"
```

Expected: `['奥卡姆剃刀', '全过程可查可透明', '器官互保', '工具即感官', '代码即文档']` (3 all + 2 building)

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/principles.py plastic_promise/mcp/server.py
git commit -m "feat: principle_activate domain_hint filter + principle_inherit behavior domain support"
```

---

### Task 6: E2E 回归 + 韧性集成测试

**Files:**
- Create: `tests/test_resilience_e2e.py`

- [ ] **Step 1: 韧性 E2E 测试**

```python
"""Resilience E2E — 灾难恢复 + 降级 + 版本迁移"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class TestResilienceE2E:
    def test_rebuild_and_recover(self):
        """全流程: 清空域 → rebuild → 检索可用"""
        from plastic_promise.core.context_engine import ContextEngine
        e = ContextEngine()
        
        # 模拟崩溃: 清空域
        e._dm._conn.execute("DELETE FROM domains")
        e._dm._conn.commit()
        e._dm.domains.clear()
        
        # 重建
        result = e._dm.rebuild_from_memories()
        assert result["restored_domains"] >= 7
        
        # 检索仍可用
        stats = e._dm.stats()
        assert "building" in stats
    
    def test_degradation_switch(self):
        """_dm_ok=False 时 assign 返回 uncategorized"""
        from plastic_promise.core.context_engine import ContextEngine
        e = ContextEngine()
        
        # 模拟降级
        old_dm = e._dm
        e._dm = None
        e._dm_ok = False
        
        # 检索不应抛异常
        try:
            # supply 不依赖 dm
            pack = e.supply("test query", [0.0]*1024, "general", "global")
            assert pack is not None
            result = "PASS: supply works without DM"
        except Exception as ex:
            result = f"FAIL: {ex}"
        
        e._dm = old_dm
        e._dm_ok = True
        assert "PASS" in result
    
    def test_schema_version_write(self):
        """schema_version 正确写入"""
        from plastic_promise.core.context_engine import ContextEngine
        e = ContextEngine()
        row = e._dm._conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        assert row[0] == 2
    
    def test_fuzzy_visible_in_stats(self):
        """模糊缓存积压计数保留在 system_stats 中"""
        from plastic_promise.core.context_engine import ContextEngine
        e = ContextEngine()
        
        # 向 pipeline 添加一条记忆
        from plastic_promise.memory.pipeline import MemoryPipeline
        fb = MemoryPipeline(domain_manager=e._dm)
        fb.store_urgent("test fuzzy visibility")
        
        buf_stats = fb.stats()
        assert buf_stats["total"] >= 1
```

- [ ] **Step 2: 运行韧性测试**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_resilience_e2e.py -v --tb=short
```

Expected: 4 PASS

- [ ] **Step 3: 运行全部回归**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_domain_manager.py tests/test_domain_e2e.py tests/test_resilience_e2e.py tests/test_rebuild.py -v --tb=line
```

Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_resilience_e2e.py
git commit -m "test: resilience E2E — rebuild, degradation, schema_version, fuzzy visibility"
```

---

### Task 7: CLAUDE.md 工作流更新

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 更新会话启动检查单**

将 `fuzzy_status` 替换为 `memory_stats`（已在 resilience spec 中完成，确认 CLAUDE.md v2 一致）

确认 `defense_trust(action="get")` → `defense(action="get")`

- [ ] **Step 2: 验证工具名称一致性**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" python -c "import asyncio; from plastic_promise.mcp.server import list_tools; tools = asyncio.run(list_tools()); names = {t.name for t in tools}; print('domain' in names, 'defense' in names, 'system' in names, 'scarf_reflect' in names, 'audit_run' in names)"
```

Expected: `True True True True True`

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md verify tool name consistency post-consolidation"
```
