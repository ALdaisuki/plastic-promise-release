"""rebuild_from_memories 恢复测试"""

import pytest
import json
from plastic_promise.core.domain_manager import DomainManager


class TestRebuild:
    def test_rebuild_from_scratch(self):
        """模拟 domains 表清空，从 memories 的 tags 重建"""
        dm = DomainManager(db_path=":memory:")

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
        dm = DomainManager(db_path=":memory:")
        result = dm.rebuild_from_memories(memories_source=[])
        stats = dm.stats()
        required = {
            "building",
            "fixing",
            "designing",
            "reflecting",
            "governing",
            "connecting",
            "all",
        }
        assert required.issubset(set(stats.keys()))

    def test_rebuild_writes_audit_log(self):
        """重建事件写入审计日志"""
        dm = DomainManager(db_path=":memory:")
        before = dm._count_audit_log()
        dm.rebuild_from_memories(
            memories_source=[{"id": "m1", "tags": ["code"], "domain": "building"}]
        )
        after = dm._count_audit_log()
        assert after > before
