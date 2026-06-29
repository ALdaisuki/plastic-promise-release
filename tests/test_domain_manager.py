"""DomainManager 单元测试"""
import pytest
from plastic_promise.core.domain_manager import DomainManager, DomainInfo, PREDEFINED_DOMAINS


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
        r1 = dm.assign([unique, "compute"])
        # 第二次: 再加标签
        r2 = dm.assign([unique, "simulate"])
        # 候选域应累积 (用唯一标签避免DB残留干扰)
        assert unique in dm.domains, f"Expected '{unique}' in {list(dm.domains.keys())}"
        assert dm.domains[unique].status == "candidate"

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
        # stats
        result = dm.stats(agent_id="agent_pi")
        assert "building" in result
        # assign
        r = dm.assign(["debug", "fix"], agent_id="agent_pi")
        assert r == "fixing"
        # merge
        ok = dm.merge("fixing", "building", agent_id="agent_pi")
        assert ok is True
