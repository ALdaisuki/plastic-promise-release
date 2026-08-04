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

import datetime
import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter
from contextlib import suppress

from plastic_promise.core.paths import get_db_path

DEFAULT_DOMAIN_PROJECT_ID = "project:legacy-global"
_AUTOMATIC_DOMAIN_TAG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_NON_SEMANTIC_TAG_PREFIXES = (
    "cat:",
    "domain:",
    "lifecycle:",
    "llm_",
    "project:",
    "quality:",
    "status:",
    "visibility:",
)


class DomainInfo:
    """域信息 — 一个语义域（行为域或候选域）"""

    __slots__ = (
        "name",
        "score",
        "tags",
        "aliases",
        "merged_from",
        "parent",
        "status",
        "memory_count",
        "principle_ids",
        "access_count",
        "last_accessed",
        "created_at",
        "last_active",
    )

    def __init__(
        self,
        name: str,
        score: float = 0.3,
        tags: set | None = None,
        aliases: list | None = None,
        merged_from: list | None = None,
        parent: str | None = None,
        status: str = "active",
        memory_count: int = 0,
        principle_ids: list | None = None,
        access_count: int = 0,
        last_accessed: str = "",
        created_at: str = "",
        last_active: str = "",
    ):
        self.name = name
        self.score = score
        self.tags: set[str] = set(tags) if tags else set()  # defensive copy
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


class DomainManager:
    """域联邦系统的协调核心。

    写操作 (assign/merge/unmerge/rename/decay) 受 _lock 保护。
    读操作 (stats/generate_signal) 不加锁。

    候选域复用 domains 表: status='candidate', tags 列存 {"tag": count} 的 JSON。
    别名复用 domains.aliases 列: JSON array of {alias, expires_at}。
    all 域不参与记忆分配、不参与融合。
    """

    SCHEMA_VERSION = 3

    MIGRATION_CHAIN = {
        1: "_migrate_v1_to_v2",
        3: "_migrate_v2_to_v3",
    }

    def __init__(self, db_path: str | None = None, *, project_id: str | None = None):
        self._lock = threading.RLock()
        self.domains: dict[str, DomainInfo] = {}
        self.tag_to_domain: dict[str, set[str]] = {}
        self._scoped_managers: dict[str, DomainManager] = {}
        self.project_id = self._normalize_project_id(project_id)

        # SQLite 持久化
        import sqlite3

        if db_path is None:
            db_path = get_db_path()
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._run_migrations()
        self._load_from_db()

        # Auto-rebuild guard: domains 表空但 memories 有数据 → 自动重建
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM domains WHERE project_id = ? AND status != 'candidate'",
                (self.project_id,),
            ).fetchone()
            domain_count = row[0] if row else 0
            mem_count_row = self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE COALESCE(NULLIF(TRIM(project_id), ''), ?) = ?",
                (DEFAULT_DOMAIN_PROJECT_ID, self.project_id),
            ).fetchone()
            mem_count = mem_count_row[0] if mem_count_row else 0

            if domain_count == 0 and mem_count > 0:
                if hasattr(self, "rebuild_from_memories"):
                    import time as _time

                    logging.warning(
                        f"domains 表为空但 memories 表有 {mem_count} 条记忆。"
                        f"将在 5 秒后自动重建域图谱。按 Ctrl+C 取消。"
                    )
                    _time.sleep(5)
                    self.rebuild_from_memories(memories_source="sqlite")
                else:
                    logging.warning(
                        f"domains 表为空但 memories 表有 {mem_count} 条记忆，"
                        f"但 rebuild_from_memories 不可用 — 跳过自动重建"
                    )
        except Exception:
            pass

    def _init_schema(self):
        """建表: domains, audit_log"""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS domains (
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
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
                last_active TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, name)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                operation TEXT NOT NULL,
                detail TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            );
        """)
        self._conn.commit()

    def _run_migrations(self):
        """检查 schema 版本并执行迁移链。"""
        try:
            row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
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
                    "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (v,)
                )
                self._conn.commit()

        # 记录最终 schema 版本
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (self.SCHEMA_VERSION,)
        )
        self._conn.commit()

    def _migrate_v1_to_v2(self):
        """v1→v2: 添加 tags/domain 列, 建 domains/audit_log 表"""
        for col, dtype, default in [
            ("tags", "TEXT", "'[]'"),
            ("domain", "TEXT", "'uncategorized'"),
        ]:
            with suppress(Exception):
                self._conn.execute(
                    f"ALTER TABLE memories ADD COLUMN {col} {dtype} NOT NULL DEFAULT {default}"
                )
        # domains 和 audit_log 在 _init_schema 中已用 IF NOT EXISTS 创建

    def _migrate_v2_to_v3(self):
        """v2→v3: scope every persisted domain by project without rewriting memories.

        Legacy domain rows did not retain project provenance.  They are therefore
        preserved losslessly under ``project:legacy-global``.  Existing memory
        rows are intentionally not reclassified by this schema migration; a
        project-scoped reclassification remains an explicit governed operation.
        """
        columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(domains)").fetchall()
        }
        if "project_id" in columns:
            return
        required = {
            "name",
            "score",
            "tags",
            "aliases",
            "merged_from",
            "parent",
            "status",
            "memory_count",
            "principle_ids",
            "access_count",
            "last_accessed",
            "created_at",
            "last_active",
        }
        if not required.issubset(columns):
            raise RuntimeError("domains_v2_schema_invalid")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("ALTER TABLE domains RENAME TO domains_v2_legacy")
            self._conn.execute(
                """CREATE TABLE domains (
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
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
                    last_active TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (project_id, name)
                )"""
            )
            self._conn.execute(
                """INSERT INTO domains (
                    project_id, name, score, tags, aliases, merged_from, parent,
                    status, memory_count, principle_ids, access_count,
                    last_accessed, created_at, last_active
                )
                SELECT ?, name, score, tags, aliases, merged_from, parent,
                    status, memory_count, principle_ids, access_count,
                    last_accessed, created_at, last_active
                FROM domains_v2_legacy""",
                (DEFAULT_DOMAIN_PROJECT_ID,),
            )
            self._conn.execute("DROP TABLE domains_v2_legacy")
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @staticmethod
    def _normalize_project_id(project_id: str | None) -> str:
        candidate = str(
            project_id
            or os.environ.get("PLASTIC_PROJECT_ID")
            or os.environ.get("PP_PROJECT_ID")
            or DEFAULT_DOMAIN_PROJECT_ID
        ).strip()
        return candidate or DEFAULT_DOMAIN_PROJECT_ID

    def _manager_for_project(self, project_id: str | None) -> "DomainManager":
        if project_id is None:
            return self
        normalized = self._normalize_project_id(project_id)
        if normalized == self.project_id:
            return self
        with self._lock:
            manager = self._scoped_managers.get(normalized)
            if manager is None:
                manager = DomainManager(self._db_path, project_id=normalized)
                self._scoped_managers[normalized] = manager
            return manager

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
            "created_at, last_active FROM domains WHERE project_id = ?",
            (self.project_id,),
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
                tags = (
                    set(tags_raw)
                    if isinstance(tags_raw, list)
                    else (set(tags_raw.keys()) if isinstance(tags_raw, dict) else set())
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
        payload = {"project_id": self.project_id, **detail}
        self._conn.execute(
            "INSERT INTO audit_log (timestamp, operation, detail) VALUES (?,?,?)",
            (
                datetime.datetime.now().isoformat(),
                operation,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def _count_audit_log(self) -> int:
        """返回审计日志条目数（测试用）。"""
        row = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
        return row[0] if row else 0

    # ======== 公开 API ========

    def assign(
        self,
        tags: list[str],
        agent_id: str = "",
        *,
        project_id: str | None = None,
    ) -> str:
        """为记忆标签分配域。

        线程安全。tie-breaking: 匹配数→score→创建时间。
        无法匹配时返回 "uncategorized"，同时生成候选域记录。

        Args:
            tags: 记忆的标签列表。

        Returns:
            域名字符串。
        """
        manager = self._manager_for_project(project_id)
        if manager is not self:
            return manager.assign(tags, agent_id=agent_id)
        with self._lock:
            # Fast-path: domain:xxx tag prefix mapping
            for tag in tags:
                if tag.startswith("domain:"):
                    domain_name = tag[7:]  # strip "domain:" prefix
                    if domain_name == "all":
                        return "uncategorized"
                    if domain_name in self.domains and self.domains[domain_name].status == "active":
                        dom = self.domains[domain_name]
                        dom.access_count += 1
                        dom.last_accessed = datetime.datetime.now().isoformat()
                        dom.memory_count += 1
                        dom.last_active = datetime.datetime.now().isoformat()
                        self._persist_domain(domain_name)
                        return domain_name
                    # Non-existent or inactive domain: continue scanning
                    # for other domain:xxx tags before falling through

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
                    -(
                        datetime.datetime.fromisoformat(
                            self.domains[n].created_at or "2000-01-01T00:00:00"
                        ).timestamp()
                    ),
                ),
            )
            best_score = scores[best_name] / max(len(tags), 1)

            if best_score > 0.3:
                dom = self.domains[best_name]
                dom.access_count += 1
                dom.last_accessed = datetime.datetime.now().isoformat()
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
        main_tag = Counter(tags).most_common(1)[0][0]

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
                auto_name = self._automatic_domain_name(dom.tags)
                old_name = main_tag
                self.domains.pop(old_name)
                dom.name = auto_name
                dom.status = "active"
                dom.score = 0.5
                expires = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
                dom.aliases.append({"alias": old_name, "expires_at": expires})
                self.domains[auto_name] = dom
                self._persist_domain(old_name)
                self._rebuild_tag_index()
                self._write_audit_log(
                    "domain_create",
                    {
                        "name": auto_name,
                        "candidate_name": old_name,
                        "tags": sorted(dom.tags),
                        "memory_count": dom.memory_count,
                    },
                )
                self._persist_domain(auto_name)
                return auto_name

            self._persist_domain(main_tag)
            return "uncategorized"
        else:
            # 已是正式域但匹配分低
            return "uncategorized"

    def _automatic_domain_name(self, tags: set[str] | list[str]) -> str:
        """Build a stable, project-specific name for an emergent active domain."""

        normalized_tags = sorted({str(tag).strip() for tag in tags if str(tag).strip()})
        semantic_tags = [
            tag
            for tag in normalized_tags
            if not tag.casefold().startswith(_NON_SEMANTIC_TAG_PREFIXES)
        ]
        label_source = semantic_tags or normalized_tags or ["domain"]
        slugs: list[str] = []
        for tag in label_source:
            slug = _AUTOMATIC_DOMAIN_TAG_RE.sub("-", tag.casefold()).strip("-")
            if slug and slug not in slugs:
                slugs.append(slug[:32])
            if len(slugs) == 2:
                break
        label = "-".join(slugs) or "domain"
        fingerprint_material = "\0".join((self.project_id, *normalized_tags))
        fingerprint = hashlib.sha256(fingerprint_material.encode()).hexdigest()[:8]
        base = f"emergent:{label[:65]}-{fingerprint}"
        existing = self.domains.get(base)
        if existing is None or existing.tags == set(normalized_tags):
            return base
        suffix = 2
        while f"{base}-{suffix}" in self.domains:
            suffix += 1
        return f"{base}-{suffix}"

    def merge(
        self,
        source: str,
        target: str,
        agent_id: str = "",
        *,
        project_id: str | None = None,
    ) -> bool:
        """合并两个域。source 标记为 merged，谱系写入 target.merged_from。

        Returns:
            True 如果合并成功，False 如果源/目标不存在或源=target。
        """
        manager = self._manager_for_project(project_id)
        if manager is not self:
            return manager.merge(source, target, agent_id=agent_id)
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
            self._write_audit_log(
                "domain_merge",
                {
                    "source": source,
                    "target": target,
                    "merged_tags": sorted(src.tags),
                },
            )
            return True

    def unmerge(
        self,
        source: str,
        agent_id: str = "",
        *,
        project_id: str | None = None,
    ) -> bool:
        """从 merged_from 谱系恢复被合并的域。"""
        manager = self._manager_for_project(project_id)
        if manager is not self:
            return manager.unmerge(source, agent_id=agent_id)
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
            self._write_audit_log(
                "domain_unmerge",
                {
                    "source": source,
                    "from": parent_name,
                },
            )
            return True

    def rename(
        self,
        old: str,
        new: str,
        agent_id: str = "",
        *,
        project_id: str | None = None,
    ) -> bool:
        """重命名域。旧名→aliases 保留 30 天。"""
        manager = self._manager_for_project(project_id)
        if manager is not self:
            return manager.rename(old, new, agent_id=agent_id)
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

    def decay(self, agent_id: str = "", *, project_id: str | None = None) -> list[dict]:
        """衰减检测。返回被衰减的域列表。

        规则:
          - 7 天无新增 AND access_count 零增长 → score ×0.8
          - score < 0.1 → 萎缩，找 Jaccard 最相似的兄弟域合并
          - 候选域 (status='candidate') 7 天未达转正条件 → 清理
        """
        manager = self._manager_for_project(project_id)
        if manager is not self:
            return manager.decay(agent_id=agent_id)
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
                        decayed.append(
                            {"name": name, "action": "remove_candidate", "days": days_inactive}
                        )
                        del self.domains[name]
                        self._conn.execute(
                            "DELETE FROM domains WHERE project_id = ? AND name = ?",
                            (self.project_id, name),
                        )
                        self._conn.commit()
                    continue

                if days_inactive >= 7 and dom.access_count == 0:
                    dom.score = round(dom.score * 0.8, 4)
                    decayed.append(
                        {
                            "name": name,
                            "action": "decay",
                            "new_score": dom.score,
                            "days": days_inactive,
                        }
                    )

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

    def _find_most_similar(self, name: str) -> str | None:
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

    def generate_signal(
        self, from_domain: str, to_domain: str, context: str, agent_id: str = ""
    ) -> str:
        """实时生成联邦信号摘要（≤200 字符，不持久化）。

        Args:
            from_domain: 信号来源域。
            to_domain: 信号目标域。
            context: 检索上下文（命中条数等）。

        Returns:
            信号摘要字符串，≤200 字符。
        """
        msg = f"{from_domain} → {to_domain}: {context}"
        return msg[:200]

    def stats(self, agent_id: str = "", *, project_id: str | None = None) -> dict:
        # TODO(agent_id): 多 Agent 场景按 agent_id 过滤域可见性
        """返回所有域统计（只读，快照复制）。"""
        manager = self._manager_for_project(project_id)
        if manager is not self:
            return manager.stats(agent_id=agent_id)
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
                "project_id": self.project_id,
            }
        return result

    def _persist_domain(self, name: str):
        """将单个域写入 SQLite（Upsert）。调用方需持有锁。"""
        if name not in self.domains:
            self._conn.execute(
                "DELETE FROM domains WHERE project_id = ? AND name = ?",
                (self.project_id, name),
            )
            return
        dom = self.domains[name]
        self._conn.execute(
            """INSERT OR REPLACE INTO domains
               (project_id, name, score, tags, aliases, merged_from, parent, status,
                memory_count, principle_ids, access_count, last_accessed,
                created_at, last_active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.project_id,
                dom.name,
                dom.score,
                json.dumps(sorted(dom.tags), ensure_ascii=False)
                if dom.status == "active"
                else json.dumps(dict.fromkeys(dom.tags, 1), ensure_ascii=False),
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

    def rebuild_from_memories(
        self,
        memories_source=None,
        agent_id: str = "",
        *,
        project_id: str | None = None,
    ) -> dict:
        """从记忆的 tags 字段全量逆向重建域联邦图谱。

        Args:
            memories_source: 可选 list[dict]，每项含 id, tags, domain。
                            None = 从 SQLite memories 表读取。
        Returns:
            {"restored_domains": int, "tags_indexed": int}
        """
        import json as _json
        from collections import Counter

        manager = self._manager_for_project(project_id)
        if manager is not self:
            return manager.rebuild_from_memories(memories_source, agent_id=agent_id)
        with self._lock:
            if memories_source is None or memories_source == "sqlite":
                rows = self._conn.execute(
                    "SELECT id, tags FROM memories "
                    "WHERE COALESCE(NULLIF(TRIM(project_id), ''), ?) = ?",
                    (DEFAULT_DOMAIN_PROJECT_ID, self.project_id),
                ).fetchall()
                memories_source = [
                    {
                        "id": r[0],
                        "tags": _json.loads(r[1]) if isinstance(r[1], str) else (r[1] or []),
                    }
                    for r in rows
                ]

            # Phase 1: 标签共现统计
            tag_cooccur = Counter()
            tag_freq = Counter()
            all_tags = set()

            for mem in memories_source:
                tags = mem.get("tags", [])
                if isinstance(tags, str):
                    tags = _json.loads(tags) if tags else []
                for t in tags:
                    tag_freq[t] += 1
                    all_tags.add(t)
                for i, t1 in enumerate(tags):
                    for t2 in tags[i + 1 :]:
                        key = tuple(sorted([t1, t2]))
                        tag_cooccur[key] += 1

            # Phase 2: 聚类 (cooccur > 3 → 同域)
            clusters = self._cluster_by_cooccurrence(tag_cooccur, tag_freq, all_tags)

            # Phase 3: 合并入预定义域
            merged_domains = {}
            for name, cfg in PREDEFINED_DOMAINS.items():
                if name == "all":
                    # 保留 all 域但不参与聚类合并
                    merged_domains[name] = dict(cfg)
                    merged_domains[name]["tags"] = set(cfg["tags"])
                    continue
                merged_domains[name] = dict(cfg)
                merged_domains[name]["tags"] = set(cfg["tags"])

            for cluster_tags in list(clusters):
                best_name = None
                best_jac = 0.0
                for dname, dcfg in merged_domains.items():
                    if dname == "all":
                        continue
                    inter = len(cluster_tags & dcfg["tags"])
                    union = len(cluster_tags | dcfg["tags"])
                    jac = inter / union if union > 0 else 0.0
                    if jac > best_jac:
                        best_jac = jac
                        best_name = dname
                if best_jac > 0.4 and best_name:
                    merged_domains[best_name]["tags"].update(cluster_tags)
                else:
                    name = self._automatic_domain_name(cluster_tags)
                    merged_domains[name] = {
                        "score": 0.5,
                        "tags": cluster_tags,
                        "principle_ids": [],
                        "status": "active",
                    }

            # Phase 4: 写入。先清理旧 domains 行，避免重建后 stale candidate/merged 行重载复活。
            self.domains.clear()
            self._conn.execute("DELETE FROM domains WHERE project_id = ?", (self.project_id,))
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
            self._write_audit_log(
                "domain_rebuild",
                {
                    "source": "memories table",
                    "domains_restored": len(merged_domains),
                    "tags_total": len(all_tags),
                },
            )

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
