# Resilience System — 韧性专项设计

> 状态: 待评审 | 日期: 2026-06-29

## 一、问题诊断

域联邦系统上线后，对照三个韧性维度评估发现：

| 维度 | 得分 | 核心缺口 |
|------|------|----------|
| 灾难恢复 | 35% | 预定义域可恢复，自动发现域和候选域全丢；缺 rebuild_from_memories() |
| 跨版本兼容 | 10% | schema 无版本号，`try/except: pass` 静默吞错，旧版 pack 导入域映射断裂 |
| 静默失效 | 65% | Agent 主任务不依赖 Plastic Promise（大脑完好），但 DomainManager 故障→灵魂脑死亡 |

**根因：** 设计时只考虑正常路径，"万一坏了怎么修复"未覆盖。

## 二、目标

1. **灾难恢复**：从 863 条记忆的 tags 字段全量逆向重建域联邦图谱，含候选域进度和合并谱系
2. **跨版本兼容**：schema_version 迁移链 + 拒绝启动报错；pack 作为跨版本逃生舱
3. **静默失效**：DomainManager 不可用时快速降级，Agent 主任务不受影响
4. **工具精简**：39→29 个 MCP 工具，消除语义重叠
5. **经验包升级**：流式导出、版本映射、导入策略、pack 独立检索索引

## 三、基础设施

### schema_version 表

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
-- 当前代码期望版本: 2
```

### 迁移链

```python
SCHEMA_VERSION = 2

MIGRATION_CHAIN = {
    1: _migrate_v1_to_v2,
    # v1→v2: ALTER TABLE memories ADD tags, domain; CREATE domains, audit_log
}

# 启动序列:
#   current = SELECT MAX(version) FROM schema_version
#   if current == SCHEMA_VERSION: 正常启动
#   elif current < SCHEMA_VERSION: 依次执行迁移链
#   elif current > SCHEMA_VERSION: 拒绝启动 "DB too new for this code"
#   else: 新 DB，执行全部迁移，写入 SCHEMA_VERSION
```

### DomainManager 降级开关

```python
# ContextEngine.__init__:
try:
    self._dm = DomainManager(db_path=...)
    self._dm_ok = True
except Exception as e:
    logging.error(f"DomainManager init failed: {e} — domain features disabled")
    self._dm = None
    self._dm_ok = False

# 所有调用方前置检查:
# if getattr(engine, '_dm_ok', False): dm.assign(tags)
# else: return "uncategorized"
```

### pack_tag_index — 独立于 DomainManager 的检索索引

```python
# pack_recall 导入时构建，不依赖 DomainManager
pack_tag_index: dict[str, set[str]] = {}  # tag → set[memory_id]

# _dm_ok == False 时，pack_recall 完全基于此独立索引运行
```

## 四、灾难恢复 — rebuild_from_memories()

### 触发方式

| 方式 | 条件 |
|------|------|
| 自动 | DomainManager 初始化发现 domains 表为空但 memories 表有数据 |
| 手动 | `domain(action="rebuild")` MCP 工具 |
| 冷备份 | `pack_export` 全量导出 → 备份文件 → `pack_import` + rebuild |

### 重建流程（不依赖已有 domains 表）

```
Phase 1: 标签共现扫描
  从 SQLite memories 表逐条读取 tags
  → Counter 统计标签共现频次
  → 同一记忆中的标签对 → cooccur[tag_a,tag_b] += 1

Phase 2: 聚类 (cooccur >3 → 同域簇)
Phase 3: 与预定义域合并 (Jaccard >0.4)
Phase 4: 写入 domains 表 + 回填 memories.domain
Phase 5: 重建 tag_index
Phase 6: audit_log 写入 rebuild 事件
```

### 性能

| 规模 | 预计耗时 |
|------|----------|
| 863 条 | <2 秒 |
| 50000 条 | batch_size=1000, <30 秒 |

## 五、跨版本兼容

### schema_version 迁移链

- 硬编码迁移函数，按版本号顺序执行
- 不支持降级（current > expected → 拒绝启动）
- 迁移失败 → 拒绝启动 + 打印错误日志

### pack 跨版本逃生舱

```python
PACK_VERSION_MAP = {
    "1.0": {
        "domain": {"work": "governing", "life": "reflecting"},
    },
    "2.0": {},  # 当前版本
}

def pack_import(path, strategy="skip"):
    """
    strategy:
      "skip"    — 已存在的 memory_id 跳过 (默认)
      "replace" — 已存在的覆盖
      "merge"   — 同 ID 合并 tags 并集
    """
    # 1. 检测 pack version
    # 2. 应用 PACK_VERSION_MAP 域映射
    # 3. 按 strategy 写入
    # 4. 重建 pack_tag_index
```

### 流式导出（防 OOM）

```python
def pack_export_streaming(name, output_path, tags=None):
    """逐条 yield 记忆 + gzip 压缩，内存上限 50MB"""
    with gzip.open(output_path, 'wt') as f:
        f.write('{"version":"2.0","memories":[\n')
        for first, mid, mem in enumerate_streaming():
            if not first: f.write(',\n')
            json.dump(memory_to_dict(mid, mem), f)
        f.write('\n],"domains":')
        json.dump(domain_snapshot(), f)
        f.write('}')
```

## 六、工具合并方案

**39 → 29 个工具：**

| 域 | 旧工具 | 新工具 | 节省 |
|------|--------|--------|------|
| Domain | domain_stats, domain_merge, domain_unmerge, domain_rename, (domain_rebuild 新) | **domain**(action=stats\|merge\|unmerge\|rename\|rebuild) | -4 |
| Pipeline | fuzzy_status, fuzzy_process | **删除**，合并入 memory_stats + memory_store 自动触发 | -2 |
| Defense | defense_trust(已有 action), defense_status | defense_trust 内部吞并 status | -1 |
| Audit | audit_run, audit_report | audit_report → audit_run(action="report") | -1 |
| Reflection | scarf_reflect, inertia_check | inertia_check → scarf_reflect(mode="inertia") | -1 |
| System | system_backup, system_migrate | system(action="backup"\|"migrate"\|"stats") | -1 |

**保留不变的：** Memory 10 个 + Context 4 个 + Pack 3 个 + Principle 4 个 + Issue 3 个 = 24 个。

**合并后总计：24 + 5 个新 domain/system/defense/audit/reflection = 29 个。**

## 七、原则域过滤增强

### principle_activate 加 domain_hint

```python
def principle_activate(task_type, task_description="", domain_hint=None):
    """domain_hint: 可选，限定域。None=全部。all 域原则始终纳入。"""
    principles = filter_by_task_type(task_type)
    if domain_hint and domain_hint != "all":
        principles = [p for p in principles
                       if p["domain"] in (domain_hint, "all")]
    return principles
```

### principle_inherit 支持行为域

当前只支持 work→all, life→all。扩展为：

```python
# source_domain 可以是任意行为域: building, designing, reflecting, governing
# target_domain 默认 all
# 同域内原则直接激活，跨域通过联邦信号传递
```

## 八、audit_log 写入时机

| 操作 | 是否写入 | 理由 |
|------|----------|------|
| domain_merge | ✅ | 手动结构性变更 |
| domain_unmerge | ✅ | 手动逆向操作 |
| domain_rename | ✅ | 标签体系变更 |
| domain_create | ✅ | 候选域转正，自进化里程碑 |
| domain_decay | ✅ | 域衰减/萎缩 |
| domain_rebuild | ✅ | 灾难恢复事件 |
| assign() | ❌ | 高频操作，从 memories.domain 可追溯 |
| tag_alias 添加 | ❌ | 从 domains.aliases 列可追溯 |

## 九、新增/变更 MCP 工具

| 工具 | 说明 |
|------|------|
| `domain(action)` | 统一域操作入口: stats/merge/unmerge/rename/rebuild |
| `defense(action)` | 合并 defense_trust + defense_status: get/history/adjust/status |
| `audit_run(action)` | 合并 + report 模式 |
| `scarf_reflect(mode)` | 合并 + inertia 模式 |
| `system(action)` | 合并 backup/migrate/stats |
| `pack_import` | 新增 strategy 参数 ("skip"\|"replace"\|"merge") |
| `pack_export` | 流式写盘，支持 tags 过滤 |
| `principle_activate` | 新增 domain_hint 参数 |

## 十、改动面

| 文件 | 改动 |
|------|------|
| `core/context_engine.py` | `_dm_ok` 降级开关；`_init_schema()` 加 `schema_version` 建表 |
| `core/domain_manager.py` | `rebuild_from_memories()`；SCHEMA_VERSION 常量；启动迁移链 |
| `core/pack_index.py` (新) | 流式导出 + `pack_tag_index` 独立索引 |
| `mcp/tools/domain.py` | 合并为 `handle_domain(action=...)` 单入口 |
| `mcp/tools/principles.py` | `principle_activate` + `domain_hint`；`principle_inherit` 行为域支持 |
| `mcp/tools/memory.py` | `pack_import` + strategy + version_mapper；`pack_recall` + 内存索引 |
| `mcp/server.py` | 工具列表 10 处合并/删除；新路由 |
| `mcp/tools/management.py` | 删 `fuzzy_status`/`fuzzy_process` 独立处理函数 |
| `mcp/tools/audit_defense.py` | `audit_report` → `audit_run` 合并 |
| `mcp/tools/reflection.py` | `inertia_check` → `scarf_reflect` 合并 |

## 十一、不做什么

- 不拆读写锁（RLock 持锁 <1ms，当前规模不需要）
- 不用 LLM 生成联邦信号（模板拼接够用，原则 #1）
- 不引入 Alembic（硬编码迁移链足够透明）
- 不分片导出（gzip 流式写盘一条流，原则 #1）
- 不改变现有 12 条原则的域分配
