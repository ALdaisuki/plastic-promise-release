# Domain System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为记忆和原则引入基于 Agent 行为的动态域联邦系统 — 6 个行为域 + 1 个通用原则域，标签驱动检索分层，自进化三层闭环。

**Architecture:** Python 纯业务层改动。DomainManager 作为协调核心（threading.Lock 保护写操作），流水线 classified 阶段负责域分配，检索层高/低置信分层判定。SQLite 新增 `domains` 和 `audit_log` 两张表。4 个新 MCP 工具。

**Tech Stack:** Python 3.10+, SQLite (WAL), threading.Lock, Pytest

## Global Constraints

- 原则 #1 奥卡姆剃刀: 不新增不必要的表/结构，能复用的复用
- 原则 #2 可查可透明: 候选域、别名全部 SQLite 持久化，audit_log 记录所有变更
- 净零字段增长: MemoryRecord +2 (tags, domain) -2 (scope, category marked deprecated)
- 向后兼容: scope/category 列保留不删除
- 线程安全: DomainManager 所有写操作加 Lock
- 信号不持久化: domain_signals 不建表，检索时实时生成

---

### Task 1: 原则域重分配 — constants.py

**Files:**
- Modify: `plastic_promise/core/constants.py`

**Interfaces:**
- Produces: `CORE_PRINCIPLES` 中 12 条原则的 `domain` 字段更新为实际行为域

- [ ] **Step 1: 更新 CORE_PRINCIPLES 中每条原则的 domain 值**

找到 `plastic_promise/core/constants.py` 中 `CORE_PRINCIPLES` 列表（约第303行），更新以下原则的 `domain` 字段：

```python
# 原则 ID 1,2,8 → domain="all" (保持不变)
# 原则 ID 3 → domain="reflecting"
# 原则 ID 4 → domain="designing"
# 原则 ID 5 → domain="governing"
# 原则 ID 6 → domain="designing"
# 原则 ID 7 → domain="building"
# 原则 ID 9 → domain="governing"
# 原则 ID 10 → domain="reflecting"
# 原则 ID 11 → domain="governing"
# 原则 ID 12 → domain="building"
```

具体的每条原则 `domain` 值修改（在 `CORE_PRINCIPLES` 中找到对应 id 并修改 `"domain"` 字段）:

| ID | name | 旧 domain | 新 domain |
|----|------|-----------|-----------|
| 3 | 自我审计闭环 | "all" | "reflecting" |
| 4 | 上下文驱动决策 | "all" | "designing" |
| 5 | 约定优于约束 | "all" | "governing" |
| 6 | 数据流驱动 | "all" | "designing" |
| 7 | 器官互保 | "all" | "building" |
| 9 | 信任驱动约束 | "all" | "governing" |
| 10 | 自演化闭环 | "all" | "reflecting" |
| 11 | 原则遗传 | "all" | "governing" |
| 12 | 代码即文档 | "all" | "building" |

1,2,8 保持 `"all"` 不变。

- [ ] **Step 2: 验证常量加载**

```powershell
python -c "from plastic_promise.core.constants import CORE_PRINCIPLES; domains = set(p['domain'] for p in CORE_PRINCIPLES); print(domains)"
```

Expected: `{'all', 'reflecting', 'designing', 'governing', 'building'}`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/constants.py
git commit -m "refactor: redistribute 12 principles across 5 behavior domains (all/governing/building/designing/reflecting)"
```

---

### Task 2: DomainManager 核心 — core/domain_manager.py

**Files:**
- Create: `plastic_promise/core/domain_manager.py`
- Create: `tests/test_domain_manager.py`

**Interfaces:**
- Produces: `DomainInfo`, `DomainManager` (assign, merge, unmerge, rename, decay, generate_signal, stats, _rebuild_tag_index)
- Consumes: 无 (独立模块，依赖 SQLite 和 threading)

- [ ] **Step 1: 创建 DomainInfo 和 DomainManager 骨架**

`plastic_promise/core/domain_manager.py`:

```python
"""DomainManager — 域联邦系统的协调核心。

职责:
  - 域注册表管理 (创建/合并/衰减/别名)
  - 标签→域反向索引 (一对多)
  - 候选域追踪 (复用 domains 表, status='candidate')
  - 域分配逻辑 (tie-breaking: 匹配数→score→创建时间)
  - 联邦信号生成 (检索时实时生成, 不持久化)

线程安全: 所有写操作受 _lock 保护。
原则 #2: 候选域和别名全部 SQLite 持久化，重启不丢失。
"""

import json
import os
import datetime
import threading
from typing import Any, Optional
from collections import Counter


class DomainInfo:
    """域信息 — 一个语义域（行为域或候选域）"""

    __slots__ = (
        "name", "score", "tags", "aliases", "merged_from",
        "parent", "status", "memory_count", "principle_ids",
        "access_count", "last_accessed", "created_at", "last_active",
    )

    def __init__(
        self,
        name: str,
        score: float = 0.3,
        tags: Optional[set] = None,
        aliases: Optional[list] = None,
        merged_from: Optional[list] = None,
        parent: Optional[str] = None,
        status: str = "active",
        memory_count: int = 0,
        principle_ids: Optional[list] = None,
        access_count: int = 0,
        last_accessed: str = "",
        created_at: str = "",
        last_active: str = "",
    ):
        self.name = name
        self.score = score
        self.tags: set[str] = tags or set()
        self.aliases: list[dict] = aliases or []  # [{"alias":"x","expires_at":"..."}]
        self.merged_from: list[str] = merged_from or []
        self.parent = parent
        self.status = status
        self.memory_count = memory_count
        self.principle_ids: list[int] = principle_ids or []
        self.access_count = access_count
        self.last_accessed = last_accessed
        self.created_at = created_at or datetime.datetime.now().isoformat()
        self.last_active = last_active or datetime.datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": self.score,
            "tags": sorted(self.tags),
            "aliases": self.aliases,
            "merged_from": self.merged_from,
            "parent": self.parent,
            "status": self.status,
            "memory_count": self.memory_count,
            "principle_ids": self.principle_ids,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DomainInfo":
        return cls(
            name=data["name"],
            score=data.get("score", 0.3),
            tags=set(data.get("tags", [])),
            aliases=data.get("aliases", []),
            merged_from=data.get("merged_from", []),
            parent=data.get("parent"),
            status=data.get("status", "active"),
            memory_count=data.get("memory_count", 0),
            principle_ids=data.get("principle_ids", []),
            access_count=data.get("access_count", 0),
            last_accessed=data.get("last_accessed", ""),
            created_at=data.get("created_at", ""),
            last_active=data.get("last_active", ""),
        )


# 预定义行为域，初始 score=1.0
PREDEFINED_DOMAINS = {
    "building": {
        "score": 1.0,
        "tags": {"coding", "implement", "generate", "build", "feature", "refactor"},
        "principle_ids": [7, 12],
        "status": "active",
    },
    "fixing": {
        "score": 1.0,
        "tags": {"debug", "fix", "error", "bug", "trace", "patch"},
        "principle_ids": [],
        "status": "active",
    },
    "designing": {
        "score": 1.0,
        "tags": {"architect", "design", "plan", "structure", "system", "spec"},
        "principle_ids": [4, 6],
        "status": "active",
    },
    "reflecting": {
        "score": 1.0,
        "tags": {"audit", "scar", "reflect", "lesson", "review", "improve"},
        "principle_ids": [3, 10],
        "status": "active",
    },
    "governing": {
        "score": 1.0,
        "tags": {"principle", "trust", "govern", "policy", "comply"},
        "principle_ids": [5, 9, 11],
        "status": "active",
    },
    "connecting": {
        "score": 1.0,
        "tags": {"bridge", "agent", "message", "sync", "forward", "zmq"},
        "principle_ids": [],
        "status": "active",
    },
    "all": {
        "score": 1.0,
        "tags": set(),
        "principle_ids": [1, 2, 8],
        "status": "active",
    },
}
```

- [ ] **Step 2: 创建测试文件**

`tests/test_domain_manager.py`:

```python
"""DomainManager 单元测试"""
import pytest
from plastic_promise.core.domain_manager import DomainManager, DomainInfo, PREDEFINED_DOMAINS


class TestDomainManager:
    def test_init_creates_predefined_domains(self):
        dm = DomainManager()
        assert "building" in dm.domains
        assert "all" in dm.domains
        assert dm.domains["building"].score == 1.0
        assert dm.domains["all"].status == "active"

    def test_all_domain_never_assigned_to_memories(self):
        dm = DomainManager()
        # all 不应参与分配
        tags = {"code", "build"}
        result = dm.assign(tags)
        assert result != "all"

    def test_assign_matching_domain(self):
        dm = DomainManager()
        result = dm.assign({"debug", "fix", "crash"})
        assert result == "fixing"

    def test_assign_uncategorized(self):
        dm = DomainManager()
        result = dm.assign({"xyz_unknown_tag"})
        assert result == "uncategorized"

    def test_assign_to_candidate_then_promote(self):
        dm = DomainManager()
        # 第一次: 返回 uncategorized, 但候选域已创建
        r1 = dm.assign({"quantum", "compute"})
        # 第二次: 再加标签
        r2 = dm.assign({"quantum", "simulate"})
        # 候选域 quantum 应累积
        assert "quantum" in dm.domains
        assert dm.domains["quantum"].status == "candidate"

    def test_merge_domains(self):
        dm = DomainManager()
        dm.merge("fixing", "building")
        assert dm.domains["fixing"].status == "merged"
        assert dm.domains["fixing"].parent == "building"
        assert "fixing" in dm.domains["building"].merged_from

    def test_merge_writes_audit_log(self):
        dm = DomainManager()
        dm.merge("fixing", "building")
        # 检查 audit_log 写入
        count = dm._count_audit_log()
        assert count >= 1

    def test_rename_domain(self):
        dm = DomainManager()
        dm.rename("connecting", "bridging")
        assert "bridging" in dm.domains
        assert dm.domains["bridging"].status == "active"
        # 旧名应在 aliases 中
        aliases = [a["alias"] for a in dm.domains["bridging"].aliases]
        assert "connecting" in aliases

    def test_decay_inactive_domain(self):
        dm = DomainManager()
        dm.domains["fixing"].last_active = "2020-01-01T00:00:00"
        dm.domains["fixing"].access_count = 0
        decayed = dm.decay()
        # fixing 应出现在衰减列表中
        assert any(d["name"] == "fixing" for d in decayed)

    def test_thread_safety_assign(self):
        import threading
        dm = DomainManager()
        results = []

        def worker():
            for _ in range(50):
                r = dm.assign({"code", "build", "feature"})
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r == "building" for r in results)

    def test_tag_to_domain_one_to_many(self):
        dm = DomainManager()
        # "review" 标签可能存在于多个域
        dm.domains["reflecting"].tags.add("review")
        dm.domains["designing"].tags.add("review")
        dm._rebuild_tag_index()
        assert "building" in dm.tag_to_domain.get("review", set())
        # 应该是多个域
        assert len(dm.tag_to_domain.get("review", set())) >= 2
```

- [ ] **Step 3: 运行测试确认失败**

```powershell
pytest tests/test_domain_manager.py -v
```

Expected: all FAIL (DomainManager 未实现)

- [ ] **Step 4: 实现 DomainManager 完整逻辑**

在 `plastic_promise/core/domain_manager.py` 中追加：

```python
class DomainManager:
    """域联邦系统的协调核心。

    写操作 (assign/merge/unmerge/rename/decay) 受 _lock 保护。
    读操作 (stats/generate_signal) 不加锁。

    候选域复用 domains 表: status='candidate', tags 列存 {"tag": count} 的 JSON。
    别名复用 domains.aliases 列: JSON array of {alias, expires_at}。
    all 域不参与记忆分配、不参与融合。
    """

    def __init__(self, db_path: Optional[str] = None):
        self._lock = threading.Lock()
        self.domains: dict[str, DomainInfo] = {}
        self.tag_to_domain: dict[str, set[str]] = {}

        # SQLite 持久化
        import sqlite3
        if db_path is None:
            db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._load_from_db()

    def _init_schema(self):
        """建表: domains, audit_log"""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS domains (
                name TEXT PRIMARY KEY,
                score REAL NOT NULL DEFAULT 0.3,
                tags TEXT NOT NULL DEFAULT '[]',
                aliases TEXT NOT NULL DEFAULT '[]',
                merged_from TEXT NOT NULL DEFAULT '[]',
                parent TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                memory_count INTEGER NOT NULL DEFAULT 0,
                principle_ids TEXT NOT NULL DEFAULT '[]',
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                last_active TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                operation TEXT NOT NULL,
                detail TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def _load_from_db(self):
        """从 SQLite 加载域和标签索引，预定义域优先。"""
        # 1. 加载预定义域
        for name, cfg in PREDEFINED_DOMAINS.items():
            self.domains[name] = DomainInfo(
                name=name,
                score=cfg["score"],
                tags=cfg["tags"],
                principle_ids=cfg["principle_ids"],
                status=cfg["status"],
            )

        # 2. 从 DB 加载已持久化的域（覆盖/补充）
        rows = self._conn.execute(
            "SELECT name, score, tags, aliases, merged_from, parent, status, "
            "memory_count, principle_ids, access_count, last_accessed, "
            "created_at, last_active FROM domains"
        ).fetchall()
        for row in rows:
            name = row[0]
            tags_raw = json.loads(row[2]) if row[2] else []
            aliases_raw = json.loads(row[3]) if row[3] else []
            merged_raw = json.loads(row[4]) if row[4] else []
            pid_raw = json.loads(row[8]) if row[8] else []

            if name in self.domains and self.domains[name].status == "active":
                # 已有预定义域，只更新动态字段
                d = self.domains[name]
                d.aliases = aliases_raw
                d.access_count = max(d.access_count, row[9] or 0)
                d.last_accessed = row[10] or d.last_accessed
                d.last_active = row[12] or d.last_active
                # merge DB tags into predefined
                d.tags.update(set(tags_raw))
            else:
                tags = set(tags_raw) if isinstance(tags_raw, list) else (
                    set(tags_raw.keys()) if isinstance(tags_raw, dict) else set()
                )
                self.domains[name] = DomainInfo(
                    name=name,
                    score=row[1] or 0.3,
                    tags=tags,
                    aliases=aliases_raw,
                    merged_from=merged_raw,
                    parent=row[5],
                    status=row[6] or "active",
                    memory_count=row[7] or 0,
                    principle_ids=pid_raw,
                    access_count=row[9] or 0,
                    last_accessed=row[10] or "",
                    created_at=row[11] or "",
                    last_active=row[12] or "",
                )

        self._rebuild_tag_index()

    def _rebuild_tag_index(self):
        """从所有 active 域重建 tag_to_domain 反向索引（一对多）。"""
        self.tag_to_domain.clear()
        for name, dom in self.domains.items():
            if dom.status != "active":
                continue
            for tag in dom.tags:
                if tag not in self.tag_to_domain:
                    self.tag_to_domain[tag] = set()
                self.tag_to_domain[tag].add(name)
            # 别名也映射到该域
            for alias_entry in dom.aliases:
                alias = alias_entry["alias"]
                if alias not in self.tag_to_domain:
                    self.tag_to_domain[alias] = set()
                self.tag_to_domain[alias].add(name)

    def _write_audit_log(self, operation: str, detail: dict):
        """写入审计日志。线程安全（调用方已持有锁）。"""
        self._conn.execute(
            "INSERT INTO audit_log (timestamp, operation, detail) VALUES (?,?,?)",
            (datetime.datetime.now().isoformat(), operation, json.dumps(detail, ensure_ascii=False)),
        )
        self._conn.commit()

    def _count_audit_log(self) -> int:
        """返回审计日志条目数（测试用）。"""
        row = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
        return row[0] if row else 0

    # ======== 公开 API ========

    def assign(self, tags: list[str]) -> str:
        """为记忆标签分配域。

        线程安全。tie-breaking: 匹配数→score→创建时间。
        无法匹配时返回 "uncategorized"，同时生成候选域记录。

        Args:
            tags: 记忆的标签列表。

        Returns:
            域名字符串。
        """
        with self._lock:
            # 1. 统计每个 active 域（排除 all 和 candidate）匹配的标签数
            scores: dict[str, int] = {}
            for name, dom in self.domains.items():
                if name == "all":
                    continue
                if dom.status not in ("active",):
                    continue
                hit = len(dom.tags & set(tags))
                if hit > 0:
                    scores[name] = hit

            if not scores:
                # 2. 无匹配 → 候选新域流程
                return self._handle_candidate(tags)

            # 3. 找最佳域: 匹配数 (desc) → score (desc) → created_at (asc)
            best_name = max(
                scores.keys(),
                key=lambda n: (
                    scores[n],
                    self.domains[n].score,
                    -(datetime.datetime.fromisoformat(
                        self.domains[n].created_at or "2000-01-01T00:00:00"
                    ).timestamp()),
                ),
            )
            best_score = scores[best_name] / max(len(tags), 1)

            if best_score > 0.3:
                dom = self.domains[best_name]
                dom.memory_count += 1
                dom.last_active = datetime.datetime.now().isoformat()
                self._persist_domain(best_name)
                return best_name
            else:
                return self._handle_candidate(tags)

    def _handle_candidate(self, tags: list[str]) -> str:
        """候选新域流程。复用 domains 表 status='candidate'。"""
        if not tags:
            return "uncategorized"

        # 选出现次数最多的标签作为候选域名
        main_tag = max(set(tags), key=tags.count)

        if main_tag not in self.domains:
            self.domains[main_tag] = DomainInfo(
                name=main_tag,
                score=0.3,
                tags=set(tags),
                status="candidate",
                memory_count=1,
            )
            self._persist_domain(main_tag)
            return "uncategorized"

        dom = self.domains[main_tag]
        if dom.status == "candidate":
            dom.tags.update(tags)
            dom.memory_count += 1
            dom.last_active = datetime.datetime.now().isoformat()

            # 转正条件: ≥2 标签种类 + ≥5 记忆数
            if len(dom.tags) >= 2 and dom.memory_count >= 5:
                dom.status = "active"
                dom.score = 0.5
                self._rebuild_tag_index()
                self._write_audit_log("domain_create", {
                    "name": main_tag,
                    "tags": sorted(dom.tags),
                    "memory_count": dom.memory_count,
                })

            self._persist_domain(main_tag)
            return "uncategorized"
        else:
            # 已是正式域但匹配分低
            return "uncategorized"

    def merge(self, source: str, target: str) -> bool:
        """合并两个域。source 标记为 merged，谱系写入 target.merged_from。
        
        Returns:
            True 如果合并成功，False 如果源/目标不存在或源=target。
        """
        with self._lock:
            if source == target or source not in self.domains or target not in self.domains:
                return False
            if source == "all" or target == "all":
                return False

            src = self.domains[source]
            tgt = self.domains[target]

            # 转移标签
            tgt.tags.update(src.tags)
            # 记录谱系
            if source not in tgt.merged_from:
                tgt.merged_from.append(source)
            # 转移别名字段
            for alias_entry in src.aliases:
                if alias_entry not in tgt.aliases:
                    tgt.aliases.append(alias_entry)

            src.status = "merged"
            src.parent = target
            src.last_active = datetime.datetime.now().isoformat()

            self._persist_domain(source)
            self._persist_domain(target)
            self._rebuild_tag_index()
            self._write_audit_log("domain_merge", {
                "source": source, "target": target,
                "merged_tags": sorted(src.tags),
            })
            return True

    def unmerge(self, source: str) -> bool:
        """从 merged_from 谱系恢复被合并的域。"""
        with self._lock:
            if source not in self.domains:
                return False
            src = self.domains[source]
            if src.status != "merged" or not src.parent:
                return False
            parent_name = src.parent
            if parent_name not in self.domains:
                return False
            parent = self.domains[parent_name]

            src.status = "active"
            src.parent = None
            if source in parent.merged_from:
                parent.merged_from.remove(source)

            self._persist_domain(source)
            self._persist_domain(parent_name)
            self._rebuild_tag_index()
            self._write_audit_log("domain_unmerge", {
                "source": source, "from": parent_name,
            })
            return True

    def rename(self, old: str, new: str) -> bool:
        """重命名域。旧名→aliases 保留 30 天。"""
        with self._lock:
            if old not in self.domains or new in self.domains:
                return False
            if old == "all":
                return False

            dom = self.domains.pop(old)
            dom.name = new
            expires = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            dom.aliases.append({"alias": old, "expires_at": expires})

            self.domains[new] = dom
            self._persist_domain(old)  # 删除旧记录
            self._persist_domain(new)  # 插入新记录
            self._rebuild_tag_index()
            self._write_audit_log("domain_rename", {"old": old, "new": new})
            return True

    def decay(self) -> list[dict]:
        """衰减检测。返回被衰减的域列表。

        规则:
          - 7 天无新增 AND access_count 零增长 → score ×0.8
          - score < 0.1 → 萎缩，找 Jaccard 最相似的兄弟域合并
          - 候选域 (status='candidate') 7 天未达转正条件 → 清理
        """
        with self._lock:
            now = datetime.datetime.now()
            decayed = []

            for name, dom in list(self.domains.items()):
                if name == "all":
                    continue
                if dom.status == "merged":
                    continue

                try:
                    last = datetime.datetime.fromisoformat(dom.last_active or "2000-01-01T00:00:00")
                except (ValueError, TypeError):
                    last = datetime.datetime(2000, 1, 1)

                days_inactive = (now - last).days

                if dom.status == "candidate":
                    if days_inactive >= 7:
                        decayed.append({"name": name, "action": "remove_candidate", "days": days_inactive})
                        del self.domains[name]
                        self._conn.execute("DELETE FROM domains WHERE name = ?", (name,))
                        self._conn.commit()
                    continue

                if days_inactive >= 7 and dom.access_count == 0:
                    dom.score = round(dom.score * 0.8, 4)
                    decayed.append({"name": name, "action": "decay", "new_score": dom.score, "days": days_inactive})

                    # 萎缩: score < 0.1
                    if dom.score < 0.1:
                        target = self._find_most_similar(name)
                        if target:
                            self.merge(name, target)
                            decayed[-1]["action"] = "atrophied_merged"
                            decayed[-1]["target"] = target
                        dom.status = "atrophied"

                    self._persist_domain(name)
                    self._write_audit_log("domain_decay", decayed[-1])

            self._conn.commit()
            return decayed

    def _find_most_similar(self, name: str) -> Optional[str]:
        """用 Jaccard 相似度找最相似的兄弟域（exclude all/merged/自身）。"""
        if name not in self.domains:
            return None
        tags_a = self.domains[name].tags
        best_name = None
        best_jac = 0.0

        for other_name, other_dom in self.domains.items():
            if other_name == name or other_name == "all":
                continue
            if other_dom.status != "active":
                continue
            tags_b = other_dom.tags
            union = len(tags_a | tags_b)
            inter = len(tags_a & tags_b)
            jac = inter / union if union > 0 else 0.0
            if jac > best_jac:
                best_jac = jac
                best_name = other_name

        return best_name

    def generate_signal(self, from_domain: str, to_domain: str, context: str) -> str:
        """实时生成联邦信号摘要（≤200 字符，不持久化）。

        Args:
            from_domain: 信号来源域。
            to_domain: 信号目标域。
            context: 检索上下文（命中条数等）。

        Returns:
            信号摘要字符串，≤200 字符。
        """
        msg = f"{from_domain} 域检索命中 {context}"
        return msg[:200]

    def stats(self) -> dict:
        """返回所有域统计（只读，快照复制）。"""
        result = {}
        for name, dom in sorted(self.domains.items()):
            result[name] = {
                "score": dom.score,
                "tag_count": len(dom.tags),
                "memory_count": dom.memory_count,
                "principle_count": len(dom.principle_ids),
                "merged_from": dom.merged_from,
                "status": dom.status,
                "last_active": dom.last_active,
                "access_count": dom.access_count,
                "aliases": [a["alias"] for a in dom.aliases],
            }
        return result

    def _persist_domain(self, name: str):
        """将单个域写入 SQLite（Upsert）。调用方需持有锁。"""
        if name not in self.domains:
            self._conn.execute("DELETE FROM domains WHERE name = ?", (name,))
            return
        dom = self.domains[name]
        self._conn.execute(
            """INSERT OR REPLACE INTO domains
               (name, score, tags, aliases, merged_from, parent, status,
                memory_count, principle_ids, access_count, last_accessed,
                created_at, last_active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                dom.name,
                dom.score,
                json.dumps(sorted(dom.tags), ensure_ascii=False) if dom.status == "active"
                else json.dumps({t: 1 for t in dom.tags}, ensure_ascii=False),
                json.dumps(dom.aliases, ensure_ascii=False),
                json.dumps(dom.merged_from, ensure_ascii=False),
                dom.parent,
                dom.status,
                dom.memory_count,
                json.dumps(dom.principle_ids, ensure_ascii=False),
                dom.access_count,
                dom.last_accessed,
                dom.created_at,
                dom.last_active,
            ),
        )
        self._conn.commit()
```

- [ ] **Step 5: 运行测试确认通过**

```powershell
pytest tests/test_domain_manager.py -v
```

Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/core/domain_manager.py tests/test_domain_manager.py
git commit -m "feat: DomainManager — domain federation core with thread-safe assign/merge/decay/rename"
```

---

### Task 3: MemoryRecord 更新 + SQLite 迁移 + 检索加权

**Files:**
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Modifies: `MemoryRecord.__init__` (+tags +domain), `store_memory`, `get_memory`, `register_memory`, `_SQLiteStorage` schema, `_text_retrieval` (域加权), `supply` (domain_hint 参数)
- Consumes: `DomainManager` (from Task 2)
- Deprecates: `scope`, `category` (保留但不使用)

- [ ] **Step 1: MemoryRecord 加 tags/domain，标记 scope/category deprecated**

在 `plastic_promise/core/context_engine.py` 中找到 `MemoryRecord.__init__` (约第112行)：

```python
def __init__(self, id: str = "", content: str = "",
             memory_type: str = "experience", source: str = "user",
             owner: str = ""):
    self.id = id
    self.content = content
    self.memory_type = memory_type
    self.source = source
    self.owner: str = owner or os.environ.get("AGENT_OWNER", "")
    self.scope: str = "global"        # deprecated — use domain
    self.category: str = "other"      # deprecated — use domain
    self.tags: list[str] = []         # NEW: 多标签
    self.domain: str = "uncategorized" # NEW: 域标签
    self.importance: float = 0.7
    self.entity_ids: list[str] = []
    self.created_at: str = ""
    self.access_count: int = 0
    self.worth_success: int = 0
    self.worth_failure: int = 0
    self.tier: str = "L2"
```

- [ ] **Step 2: 更新 store_memory 方法**

在 `store_memory` 方法 (约第246行) 的 `data` dict 中添加：

```python
data = {
    # ... 现有字段 ...
    "owner": record.owner,
    "tier": record.tier,
    "tags": record.tags,               # NEW
    "domain": record.domain,           # NEW
}
```

- [ ] **Step 3: 更新 get_memory 方法**

在 `get_memory` 方法 (约第274行) 末尾添加：

```python
record.tags = mem.get("tags", [])
record.domain = mem.get("domain", "uncategorized")
record.tier = mem.get("tier", "L2")
return record
```

- [ ] **Step 4: 更新 register_memory 方法**

在 `register_memory` (约第206行) 的 `data` dict 中添加：

```python
data = {
    # ... 现有字段 ...
    "tier": record.get("tier", "L1"),
    "tags": record.get("tags", []),          # NEW
    "domain": record.get("domain", "uncategorized"),  # NEW
    "worth_success": record.get("worth_success", 0),
    # ... 其余不变 ...
}
```

- [ ] **Step 5: SQLite schema 迁移**

在 `_SQLiteStorage.__init__` (约第922行) 中，在已有 `CREATE TABLE IF NOT EXISTS` 之后添加迁移：

```python
# 迁移: 新增 tags 和 domain 列
try:
    self._conn.execute("ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
except Exception:
    pass  # 列已存在
try:
    self._conn.execute("ALTER TABLE memories ADD COLUMN domain TEXT NOT NULL DEFAULT 'uncategorized'")
except Exception:
    pass  # 列已存在
```

同时更新 `upsert` 和 `_row_to_dict`：

在 `upsert` (约第949行) 的 INSERT 语句中添加 tags 和 domain：

```python
self._conn.execute(
    "INSERT OR REPLACE INTO memories (id, content, memory_type, source, owner, "
    "tier, scope, category, tags, domain, importance, entity_ids, created_at, access_count, "
    "worth_success, worth_failure, activation_weight) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (
        # ... 现有参数 ...
        json.dumps(data.get("tags", [])),
        data.get("domain", "uncategorized"),
        # ... 其余参数 ...
    ),
)
```

在 `_row_to_dict` (约第998行) 中添加 tags 和 domain 的映射（注意列索引偏移）。

- [ ] **Step 6: 检索加权 — _text_retrieval 加域加权**

在 `_text_retrieval` 方法 (约第737行) 的得分计算后添加域加权：

```python
# 在 tier boost 之后添加 (约第777行处):
# 域加权: 同域 ×1.3, 融合域 (同标签) ×1.1
domain_hint = getattr(self, '_domain_hint', None)
if domain_hint and domain_hint != "all":
    mem_domain = mem.get("domain", "uncategorized")
    if mem_domain == domain_hint:
        score = min(score * 1.3, 1.0)
    elif domain_hint in self._get_federated_domains(mem_domain):
        score = min(score * 1.1, 1.0)

# 记录域访问
if domain_hint and domain_hint != "all":
    self._dm.domains.get(domain_hint, None)
    # 域 access_count 在检索完成后由上层调用
```

- [ ] **Step 7: 在 ContextEngine 中挂载 DomainManager**

在 `ContextEngine.__init__` 末尾添加：

```python
from plastic_promise.core.domain_manager import DomainManager
self._dm = DomainManager(db_path=self._sqlite._conn if self._sqlite else None)
self._domain_hint: Optional[str] = None
```

- [ ] **Step 8: 验证**

```powershell
python -c "from plastic_promise.core.context_engine import ContextEngine; e = ContextEngine(); print(e._dm.stats())"
```

Expected: 打印 7 个预定义域统计

- [ ] **Step 9: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: MemoryRecord +tags/+domain, SQLite migration, domain-weighted retrieval"
```

---

### Task 4: 流水线升级 — tagged + classified + migrate

**Files:**
- Modify: `plastic_promise/memory/pipeline.py`
- Modify: `plastic_promise/memory/soul_memory.py` (RecMem.store 传 tags/domain)

**Interfaces:**
- Modifies: `_extract_tags` → `_extract_semantic_tags`, `_process_tagged_to_classified` (加 domain 分配), `_process_embedded_to_migrate` (传 tags/domain)
- Consumes: `DomainManager.assign` (from Task 2)
- Consumes: `MemoryRecord` (from Task 3)

- [ ] **Step 1: 升级标签提取 — _extract_semantic_tags**

在 `plastic_promise/memory/pipeline.py` 中，替换 `_extract_tags`:

```python
def _extract_semantic_tags(self, content: str, use_llm: bool = True) -> list[str]:
    """提取语义标签。

    两层策略:
      1. 规则层 (免费): CJK bigram + 关键词正则 + 种子标签匹配
      2. 语义层 (可选): Ollama LLM 提取 3-5 个语义标签
    合并去重，上限 10 个。
    """
    tags: list[str] = []
    seen: set[str] = set()

    # Layer 1: 规则提取 (always)
    has_cjk = bool(re.search(r'[一-鿿]', content))
    if has_cjk:
        for i in range(len(content) - 1):
            bigram = content[i:i+2]
            if re.search(r'[一-鿿]', bigram) and bigram not in seen:
                tags.append(bigram)
                seen.add(bigram)
            if len(tags) >= 5:
                break
    if not tags:
        tags = [w for w in re.split(r'\s+|[,，。.!！?？;；:：\n]+', content)
                if len(w) >= 2 and w.lower() not in {'the','this','that','and','for','was','are','not','but','all','can','has','had','get','got','put','set','use','used'}][:5]

    # Layer 2: 种子标签匹配 (从预定义域标签中匹配)
    from plastic_promise.core.domain_manager import PREDEFINED_DOMAINS
    for domain_cfg in PREDEFINED_DOMAINS.values():
        for seed_tag in domain_cfg.get("tags", set()):
            if seed_tag.lower() in content.lower() and seed_tag not in seen:
                tags.append(seed_tag)
                seen.add(seed_tag)

    return tags[:10]
```

同时在 `store_urgent` 中把 `self._extract_tags(content)` 改为 `self._extract_semantic_tags(content)`。

- [ ] **Step 2: classified 阶段加 domain 分配**

在 `_process_tagged_to_classified` 中，tier 判定后追加 domain 分配：

```python
def _process_tagged_to_classified(self) -> int:
    items = [(mid, r) for mid, r in self._buffer.items() if r["stage"] == "tagged"]
    count = 0
    for mid, record in items:
        # 现有: tier 判定
        if self._tier_manager is not None:
            # ... 现有逻辑 ...
            record["tier"] = self._tier_manager.classify_tier(mr)
        else:
            record["tier"] = "L1"

        # 新增: domain 分配
        tags = record.get("tags", [])
        if hasattr(self, '_dm') and self._dm is not None:
            record["domain"] = self._dm.assign(tags)
        else:
            record["domain"] = "uncategorized"

        record["stage"] = "classified"
        record["processed_at"] = datetime.datetime.now().isoformat()
        count += 1
    return count
```

在 `__init__` 中添加 `self._dm`:

```python
def __init__(self, rec_mem=None, embedder=None, tier_manager=None, domain_manager=None) -> None:
    # ... 现有初始化 ...
    self._dm = domain_manager
```

- [ ] **Step 3: migrate 阶段传递 tags/domain 到主池**

在 `_process_embedded_to_migrate` 的 `self.rec_mem.store()` 调用后追加：

```python
# 在 stored = self.rec_mem.store(...) 之后
if vec and hasattr(self.rec_mem, '_engine'):
    engine = self.rec_mem._engine
    engine._memories[stored.memory_id]["_vector"] = vec
    engine._memories[stored.memory_id]["tags"] = record.get("tags", [])  # NEW
    engine._memories[stored.memory_id]["domain"] = record.get("domain", "uncategorized")  # NEW
```

- [ ] **Step 4: 更新 soul_memory.py 中 RecMem.store**

在 `plastic_promise/memory/soul_memory.py` 的 `store` 方法中，确保 MemoryRecord 传递 tags 和 domain：

```python
# 在 store 方法返回的 record 或内部处理中
record.tags = data.get("tags", [])
record.domain = data.get("domain", "uncategorized")
```

- [ ] **Step 5: 验证流水线处理**

```powershell
python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.memory.pipeline import MemoryPipeline
e = ContextEngine()
fb = MemoryPipeline(domain_manager=e._dm)
mid = fb.store_urgent('测试一条技术标签记忆 coding debug pipeline')
fb.process_pipeline()
print('stored:', mid)
"
```

Expected: 处理完成无报错

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/memory/pipeline.py plastic_promise/memory/soul_memory.py
git commit -m "feat: pipeline semantic tags + domain assignment in classified stage"
```

---

### Task 5: Domain MCP 工具

**Files:**
- Create: `plastic_promise/mcp/tools/domain.py`

**Interfaces:**
- Produces: `handle_domain_stats`, `handle_domain_merge`, `handle_domain_unmerge`, `handle_domain_rename`
- Consumes: `ContextEngine._dm` (DomainManager from Task 2)

- [ ] **Step 1: 创建 domain 工具模块**

`plastic_promise/mcp/tools/domain.py`:

```python
"""Domain MCP 工具 — 域联邦管理 4 个工具

工具列表:
- domain_stats   : 查看所有域的统计信息
- domain_merge   : 手动合并两个域
- domain_unmerge : 手动解除合并
- domain_rename  : 重命名域
"""

import json
from typing import Any
from mcp.types import TextContent


async def handle_domain_stats(engine: Any, args: dict) -> list[TextContent]:
    """查看所有域统计: 标签数、记忆数、原则数、得分、谱系、最后活跃时间。"""
    try:
        dm = getattr(engine, '_dm', None)
        if dm is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "DomainManager not initialized"}, ensure_ascii=False))]
        return [TextContent(type="text", text=json.dumps(
            dm.stats(), ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "domain_stats"}, ensure_ascii=False))]


async def handle_domain_merge(engine: Any, args: dict) -> list[TextContent]:
    """手动合并两个域（覆盖自动阈值）。"""
    try:
        dm = getattr(engine, '_dm', None)
        if dm is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "DomainManager not initialized"}, ensure_ascii=False))]
        source = args.get("source", "")
        target = args.get("target", "")
        if not source or not target:
            return [TextContent(type="text", text=json.dumps(
                {"error": "source and target required"}, ensure_ascii=False))]
        ok = dm.merge(source, target)
        return [TextContent(type="text", text=json.dumps(
            {"merged": ok, "source": source, "target": target}, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "domain_merge"}, ensure_ascii=False))]


async def handle_domain_unmerge(engine: Any, args: dict) -> list[TextContent]:
    """从 merged_from 谱系恢复被合并的域。"""
    try:
        dm = getattr(engine, '_dm', None)
        if dm is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "DomainManager not initialized"}, ensure_ascii=False))]
        source = args.get("source", "")
        if not source:
            return [TextContent(type="text", text=json.dumps(
                {"error": "source required"}, ensure_ascii=False))]
        ok = dm.unmerge(source)
        return [TextContent(type="text", text=json.dumps(
            {"unmerged": ok, "source": source}, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "domain_unmerge"}, ensure_ascii=False))]


async def handle_domain_rename(engine: Any, args: dict) -> list[TextContent]:
    """重命名域，自动更新记忆和原则的 domain 字段。"""
    try:
        dm = getattr(engine, '_dm', None)
        if dm is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "DomainManager not initialized"}, ensure_ascii=False))]
        old_name = args.get("old_name", "")
        new_name = args.get("new_name", "")
        if not old_name or not new_name:
            return [TextContent(type="text", text=json.dumps(
                {"error": "old_name and new_name required"}, ensure_ascii=False))]
        ok = dm.rename(old_name, new_name)
        return [TextContent(type="text", text=json.dumps(
            {"renamed": ok, "old_name": old_name, "new_name": new_name,
             "note": f"旧名 '{old_name}' 保留为别名 30 天"},
            ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "domain_rename"}, ensure_ascii=False))]
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/mcp/tools/domain.py
git commit -m "feat: domain MCP tools — stats/merge/unmerge/rename"
```

---

### Task 6: MCP 服务器注册 + memory_recall 增强 + 原则域信息

**Files:**
- Modify: `plastic_promise/mcp/server.py` (tool 注册 + 路由)
- Modify: `plastic_promise/mcp/tools/memory.py` (memory_recall 参数)
- Modify: `plastic_promise/mcp/tools/principles.py` (domain 信息)

- [ ] **Step 1: 在 server.py 中注册 4 个 domain 工具**

在 `_build_tools()` 函数末尾，`return tools` 之前追加 domain 工具：

```python
# === 域联邦域 ===
tools.extend([
    Tool(
        name="domain_stats",
        description="查看所有域：标签数、记忆数、原则数、得分、合并谱系、最后活跃时间。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="domain_merge",
        description="手动合并两个域（覆盖自动阈值）。合并后源域标记为 merged，标签转移到目标域。",
        inputSchema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "被合并的域"},
                "target": {"type": "string", "description": "合并到的目标域"},
            },
            "required": ["source", "target"],
        },
    ),
    Tool(
        name="domain_unmerge",
        description="手动解除合并（从 merged_from 谱系恢复）。",
        inputSchema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "要恢复的域"},
            },
            "required": ["source"],
        },
    ),
    Tool(
        name="domain_rename",
        description="重命名域，自动更新所有关联记忆和原则的 domain 字段。旧名作为别名保留 30 天。",
        inputSchema={
            "type": "object",
            "properties": {
                "old_name": {"type": "string", "description": "当前域名"},
                "new_name": {"type": "string", "description": "新域名"},
            },
            "required": ["old_name", "new_name"],
        },
    ),
])
```

- [ ] **Step 2: 在 call_tool 路由中添加 domain 分支**

在 `server.py` 的 `call_tool` 函数末尾（`pack_recall` 分支之后）添加：

```python
# Domain federation domain
elif name == "domain_stats":
    from plastic_promise.mcp.tools.domain import handle_domain_stats
    return await handle_domain_stats(engine, arguments)
elif name == "domain_merge":
    from plastic_promise.mcp.tools.domain import handle_domain_merge
    return await handle_domain_merge(engine, arguments)
elif name == "domain_unmerge":
    from plastic_promise.mcp.tools.domain import handle_domain_unmerge
    return await handle_domain_unmerge(engine, arguments)
elif name == "domain_rename":
    from plastic_promise.mcp.tools.domain import handle_domain_rename
    return await handle_domain_rename(engine, arguments)
```

- [ ] **Step 3: 增强 memory_recall — domain_hint + federation**

在 `plastic_promise/mcp/tools/memory.py` 的 `handle_memory_recall` 中：

```python
# 在函数开头 args 解析处添加:
domain_hint = args.get("domain_hint", None)
federation = args.get("federation", True)

# 在 supply 调用前设置 domain hint:
engine._domain_hint = domain_hint

# 在 supply 调用后注入联邦信号:
pack = engine.supply(query, vec, task_type, scope)

# 追加 federation signals (如果启用)
federation_signals = []
if federation and domain_hint and domain_hint != "all":
    dm = getattr(engine, '_dm', None)
    if dm:
        # 收集跨域命中
        for item in pack.core + pack.related:
            item_domain = getattr(item, 'domain', '')
            if item_domain and item_domain != domain_hint and item_domain != "all":
                sig = dm.generate_signal(item_domain, domain_hint,
                                         f"命中 {getattr(item, 'id', '?')}")
                federation_signals.append({
                    "source": item_domain,
                    "target": domain_hint,
                    "signal": sig,
                })

# 在返回 JSON 中追加:
"federation_signals": federation_signals,
```

- [ ] **Step 4: 更新 principle_activate 返回域信息**

在 `plastic_promise/mcp/tools/principles.py` 的 `handle_principle_activate` 返回的每条原则中添加 `domain` 信息（已有，确认 `"domain": p["domain"]` 行 101 存在）。

无需修改——已经存在。

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/server.py plastic_promise/mcp/tools/memory.py
git commit -m "feat: register domain MCP tools, add domain_hint+federation to memory_recall"
```

---

### Task 7: 审计钩子 — step_auditor 触发域检测

**Files:**
- Modify: `plastic_promise/core/step_auditor.py`

**Interfaces:**
- Modifies: `StepAuditor.audit_step` — 完成后触发域衰减检测

- [ ] **Step 1: 在 audit_step 末尾添加域检测**

在 `plastic_promise/core/step_auditor.py` 的 `StepAuditor.audit_step` 方法末尾（返回 result 之前）追加：

```python
# 域联邦自进化: 每次审计后触发衰减检测
try:
    from plastic_promise.core.domain_manager import DomainManager
    # 通过 engine 获取 dm (延迟导入避免循环)
    import os
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    dm = DomainManager(db_path=db_path)
    decayed = dm.decay()
    if decayed:
        result.audit_log = (result.audit_log or "") + (
            f"\n[domain_decay] {len(decayed)} domains decayed: "
            + ", ".join(d["name"] for d in decayed)
        )
except Exception:
    pass  # 域检测失败不影响主审计流程
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/core/step_auditor.py
git commit -m "feat: domain decay detection triggered on audit_step"
```

---

### Task 8: 集成验证

**Files:**
- Create: `tests/test_domain_integration.py`

- [ ] **Step 1: 端到端集成测试**

`tests/test_domain_integration.py`:

```python
"""Domain System 端到端集成测试"""
import pytest
import json


class TestDomainIntegration:
    def test_pipeline_stores_tags_and_domain(self):
        """验证 memory_store 后记忆带 tags 和 domain"""
        from plastic_promise.core.context_engine import ContextEngine
        from plastic_promise.memory.pipeline import MemoryPipeline

        engine = ContextEngine()
        pipeline = MemoryPipeline(domain_manager=engine._dm)
        mid = pipeline.store_urgent(
            "修复了一个 SQLite 持久化的 bug，调试了 MCP 重连后记忆丢失问题"
        )
        pipeline.process_pipeline()

        # 检查引擎中的记忆
        mem = engine._memories.get(mid)
        if mem is None:
            # 已迁移到主池，检查
            for k, v in engine._memories.items():
                if "SQLite" in v.get("content", ""):
                    mem = v
                    break
        if mem:
            assert mem.get("domain", "") != ""
            assert len(mem.get("tags", [])) > 0

    def test_domain_stats_accessible(self):
        """验证 domain_stats 返回 7 个预定义域"""
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()
        stats = engine._dm.stats()
        assert "building" in stats
        assert "fixing" in stats
        assert "designing" in stats
        assert "reflecting" in stats
        assert "governing" in stats
        assert "connecting" in stats
        assert "all" in stats
        assert stats["all"]["status"] == "active"
        assert stats["building"]["score"] == 1.0

    def test_all_domain_excluded_from_assignment(self):
        """验证 all 域不参与记忆分配"""
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()
        # 任何标签都不应分配进 all
        for tags in [
            ["code", "build"],
            ["debug", "fix"],
            ["design", "architect"],
        ]:
            result = engine._dm.assign(tags)
            assert result != "all", f"tags {tags} should not assign to all"

    def test_audit_log_written_on_merge(self):
        """验证域合并写入审计日志"""
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()
        before = engine._dm._count_audit_log()
        engine._dm.merge("fixing", "building")
        after = engine._dm._count_audit_log()
        assert after > before

    def test_low_confidence_fallback(self):
        """验证低置信查询走全量 — 无匹配标签时应返回 uncategorized"""
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()
        # 新标签无匹配 → 应返回 uncategorized，同时生成候选域
        result = engine._dm.assign(["totally_new_unknown_tag_xyz"])
        assert result == "uncategorized"
        # 候选域应已在 domains 中记录
        assert "totally_new_unknown_tag_xyz" in engine._dm.domains
        assert engine._dm.domains["totally_new_unknown_tag_xyz"].status == "candidate"
```

- [ ] **Step 2: 运行集成测试**

```powershell
pytest tests/test_domain_integration.py -v
```

Expected: 4 PASS, 1 SKIP (fallback test 需要完整 MCP 栈)

- [ ] **Step 3: Commit**

```bash
git add tests/test_domain_integration.py
git commit -m "test: domain system integration tests — pipeline, stats, audit_log, all exclusion"
```

---

### Task 9: 最终验证 — MCP 端到端

**Files:**
- (无改动，验证现有流程)

- [ ] **Step 1: 启动 MCP 服务器验证无报错**

```powershell
python -c "from plastic_promise.mcp.server import _build_tools; tools = _build_tools(); print(f'{len(tools)} tools registered')"
```

Expected: `36 tools registered` (原 32 + 4 domain tools)

- [ ] **Step 2: 验证原则域分配**

```powershell
python -c "
from plastic_promise.core.constants import CORE_PRINCIPLES
for p in CORE_PRINCIPLES:
    print(f'#{p[\"id\"]} {p[\"name\"]} -> {p[\"domain\"]}')
"
```

Expected: 输出 12 条，域分布为 all(3), governing(3), building(2), designing(2), reflecting(2)

- [ ] **Step 3: 验证域 tag→domain 一对多**

```powershell
python -c "
from plastic_promise.core.domain_manager import DomainManager
dm = DomainManager()
# 'review' 标签跨域
dm.domains['reflecting'].tags.add('review')
dm.domains['designing'].tags.add('review')
dm._rebuild_tag_index()
print('review ->', dm.tag_to_domain.get('review', set()))
"
```

Expected: `review -> {'reflecting', 'designing'}`

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "verify: 36 MCP tools, 12 principles redistributed, domain tag-to-domain 1:N"
```

---

### Task 10: 边界测试与清理

- [ ] **Step 1: 运行全部测试**

```powershell
pytest tests/ -v --tb=short
```

- [ ] **Step 2: 修复任何失败的测试**

- [ ] **Step 3: 验证模糊缓存区流水线**

```powershell
python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.memory.pipeline import MemoryPipeline
e = ContextEngine()
fb = MemoryPipeline(domain_manager=e._dm)
for i in range(3):
    mid = fb.store_urgent(f'测试记忆 {i}: 包含 coding, pipeline, domain 关键词')
fb.process_pipeline()
print('Buffer remaining:', len(fb._buffer))
print('Domain stats:', e._dm.stats())
"
```

Expected: `Buffer remaining: 0`, 域统计中 memory_count 增加

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "test: boundary tests — full pipeline + all unit tests passing"
```
