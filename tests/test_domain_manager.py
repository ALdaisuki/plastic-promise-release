"""DomainManager 单元测试"""

import json
import sqlite3

from plastic_promise.core.domain_manager import DomainManager


class TestDomainManager:
    def test_init_creates_predefined_domains(self):
        dm = DomainManager(db_path=":memory:")
        assert "building" in dm.domains
        assert "all" in dm.domains
        assert dm.domains["building"].score == 1.0
        assert dm.domains["all"].status == "active"

    def test_all_domain_never_assigned_to_memories(self):
        dm = DomainManager(db_path=":memory:")
        # all 不应参与分配
        tags = {"code", "build"}
        result = dm.assign(tags)
        assert result != "all"

    def test_assign_matching_domain(self):
        dm = DomainManager(db_path=":memory:")
        result = dm.assign(["debug", "fix", "crash"])
        assert result == "fixing"

    def test_assign_uncategorized(self):
        dm = DomainManager(db_path=":memory:")
        result = dm.assign({"xyz_unknown_tag"})
        assert result == "uncategorized"

    def test_assign_to_candidate_then_promote(self):
        import time

        dm = DomainManager(db_path=":memory:")
        unique = f"ztag_{int(time.time() * 1000) % 100000}"
        # 第一次: 返回 uncategorized, 但候选域已创建
        dm.assign([unique, "compute"])
        # 第二次: 再加标签
        dm.assign([unique, "simulate"])
        # 候选域应累积 (用唯一标签避免DB残留干扰)
        assert unique in dm.domains, f"Expected '{unique}' in {list(dm.domains.keys())}"
        assert dm.domains[unique].status == "candidate"

    def test_candidate_promotion_uses_stable_automatic_active_name(self, tmp_path):
        db_path = tmp_path / "automatic-domain.db"
        dm = DomainManager(db_path=str(db_path), project_id="project:alpha")

        results = [
            dm.assign(
                ["vector-governance", "memory-orchestration"],
                project_id="project:alpha",
            )
            for _ in range(5)
        ]

        active_name = results[-1]
        assert active_name.startswith("emergent:")
        assert active_name != "vector-governance"
        assert dm.stats(project_id="project:alpha")[active_name]["status"] == "active"
        fresh = DomainManager(db_path=str(db_path), project_id="project:alpha")
        assert fresh.stats()[active_name]["status"] == "active"

    def test_candidate_counts_never_cross_project_scope(self, tmp_path):
        dm = DomainManager(db_path=str(tmp_path / "scoped-domains.db"))
        tags = ["tenant-isolation", "semantic-routing"]

        for _ in range(4):
            assert dm.assign(tags, project_id="project:alpha") == "uncategorized"
        assert dm.assign(tags, project_id="project:beta") == "uncategorized"

        alpha_name = dm.assign(tags, project_id="project:alpha")
        alpha_stats = dm.stats(project_id="project:alpha")
        beta_stats = dm.stats(project_id="project:beta")
        assert alpha_name.startswith("emergent:")
        assert alpha_stats[alpha_name]["memory_count"] == 5
        assert beta_stats["tenant-isolation"]["status"] == "candidate"
        assert beta_stats["tenant-isolation"]["memory_count"] == 1
        assert alpha_name not in beta_stats

    def test_v2_domain_rows_migrate_losslessly_to_legacy_project(self, tmp_path):
        db_path = tmp_path / "domain-v2.db"
        with sqlite3.connect(db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE domains (
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
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version VALUES (2);
                """
            )
            connection.execute(
                "INSERT INTO domains VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-emergent",
                    0.5,
                    json.dumps(["alpha", "beta"]),
                    "[]",
                    "[]",
                    None,
                    "active",
                    9,
                    "[]",
                    2,
                    "",
                    "2026-07-01T00:00:00",
                    "2026-07-02T00:00:00",
                ),
            )

        manager = DomainManager(db_path=str(db_path), project_id="project:legacy-global")

        assert manager.stats()["legacy-emergent"]["memory_count"] == 9
        assert "legacy-emergent" not in manager.stats(project_id="project:other")
        with sqlite3.connect(db_path) as connection:
            pk_columns = {
                row[1]: row[5]
                for row in connection.execute("PRAGMA table_info(domains)").fetchall()
            }
            row = connection.execute(
                "SELECT project_id, name, memory_count FROM domains WHERE name = 'legacy-emergent'"
            ).fetchone()
        assert pk_columns["project_id"] == 1
        assert pk_columns["name"] == 2
        assert row == ("project:legacy-global", "legacy-emergent", 9)

    def test_merge_domains(self):
        dm = DomainManager(db_path=":memory:")
        dm.merge("fixing", "building")
        assert dm.domains["fixing"].status == "merged"
        assert dm.domains["fixing"].parent == "building"
        assert "fixing" in dm.domains["building"].merged_from

    def test_merge_writes_audit_log(self):
        dm = DomainManager(db_path=":memory:")
        dm.merge("fixing", "building")
        # 检查 audit_log 写入
        count = dm._count_audit_log()
        assert count >= 1

    def test_rename_domain(self):
        dm = DomainManager(db_path=":memory:")
        dm.rename("connecting", "bridging")
        assert "bridging" in dm.domains
        assert dm.domains["bridging"].status == "active"
        # 旧名应在 aliases 中
        aliases = [a["alias"] for a in dm.domains["bridging"].aliases]
        assert "connecting" in aliases

    def test_decay_inactive_domain(self):
        dm = DomainManager(db_path=":memory:")
        dm.domains["fixing"].last_active = "2020-01-01T00:00:00"
        dm.domains["fixing"].access_count = 0
        decayed = dm.decay()
        # fixing 应出现在衰减列表中
        assert any(d["name"] == "fixing" for d in decayed)

    def test_thread_safety_assign(self):
        import threading

        dm = DomainManager(db_path=":memory:")
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
        dm = DomainManager(db_path=":memory:")
        # "review" 标签可能存在于多个域
        dm.domains["reflecting"].tags.add("review")
        dm.domains["designing"].tags.add("review")
        dm._rebuild_tag_index()
        assert "reflecting" in dm.tag_to_domain.get("review", set())
        # 应该是多个域
        assert len(dm.tag_to_domain.get("review", set())) >= 2

    def test_agent_id_param_accepted(self):
        """agent_id 参数接受非空值，行为不变（零行为变化）"""
        dm = DomainManager(db_path=":memory:")
        result = dm.stats(agent_id="agent_pi")
        assert "building" in result
        r = dm.assign(["debug", "fix", "crash"], agent_id="agent_pi")
        # :memory: DB is clean — predefined domains match correctly
        assert r in ("fixing", "building")  # 预定义域匹配确定
        ok = dm.merge("fixing", "building", agent_id="agent_pi")
        assert ok is True

    # ======== domain:xxx prefix mapping (Batch 2 Task 1) ========

    def test_assign_domain_prefix_direct(self):
        """domain:building tag → direct match 'building'"""
        dm = DomainManager(db_path=":memory:")
        result = dm.assign(["domain:building"])
        assert result == "building"

    def test_assign_domain_prefix_overrides_other_matches(self):
        """domain:xxx takes priority over tag-based matching"""
        dm = DomainManager(db_path=":memory:")
        # tags match "fixing" better, but domain:building overrides
        result = dm.assign(["domain:building", "debug", "fix", "crash"])
        assert result == "building"

    def test_assign_domain_prefix_unknown_falls_through(self):
        """domain:unknown (nonexistent domain) falls through to normal matching"""
        dm = DomainManager(db_path=":memory:")
        result = dm.assign(["domain:nonexistent", "debug", "fix"])
        assert result == "fixing"

    def test_assign_domain_prefix_all_still_excluded(self):
        """domain:all still excluded — returns uncategorized"""
        dm = DomainManager(db_path=":memory:")
        result = dm.assign(["domain:all", "code", "build"])
        assert result == "uncategorized"
