"""Tests for Safety-Net Daemon scanner functions (Tag Dispatch + Innovation Phase).

Requires MCP server running on localhost:9020 (integration tests).
"""

import sys
import os
import asyncio
import pytest

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)


class TestSafetyNetImports:
    """Verify all scan + dispatch functions are importable without side effects."""

    def test_import_dispatch_fix_task(self):
        from daemons.maintenance_daemon import dispatch_fix_task
        assert callable(dispatch_fix_task)

    def test_import_tag_for_redo(self):
        from daemons.maintenance_daemon import tag_for_redo
        assert callable(tag_for_redo)

    def test_import_tag_audit_finding(self):
        from daemons.maintenance_daemon import tag_audit_finding
        assert callable(tag_audit_finding)

    def test_import_store_tagged_memory(self):
        from daemons.maintenance_daemon import _store_tagged_memory
        assert callable(_store_tagged_memory)

    def test_import_scan_innovation_opportunities(self):
        from daemons.maintenance_daemon import scan_innovation_opportunities
        assert callable(scan_innovation_opportunities)

    def test_import_scan_redo_queue(self):
        from daemons.maintenance_daemon import scan_redo_queue
        assert callable(scan_redo_queue)

    def test_import_scan_duplicate_clusters(self):
        from daemons.maintenance_daemon import scan_duplicate_clusters
        assert callable(scan_duplicate_clusters)

    def test_import_scan_stale_worth(self):
        from daemons.maintenance_daemon import scan_stale_worth
        assert callable(scan_stale_worth)

    def test_import_scan_tier_migration(self):
        from daemons.maintenance_daemon import scan_tier_migration
        assert callable(scan_tier_migration)

    def test_import_scan_category_stuck(self):
        from daemons.maintenance_daemon import scan_category_stuck
        assert callable(scan_category_stuck)

    def test_import_scan_orphan_steps(self):
        from daemons.maintenance_daemon import scan_orphan_steps
        assert callable(scan_orphan_steps)

    def test_import_scan_unclosed_issues(self):
        from daemons.maintenance_daemon import scan_unclosed_issues
        assert callable(scan_unclosed_issues)

    def test_import_recover_stuck_tasks(self):
        from daemons.maintenance_daemon import recover_stuck_tasks
        assert callable(recover_stuck_tasks)

    def test_import_cleanup_old_tags(self):
        from daemons.maintenance_daemon import cleanup_old_tags
        assert callable(cleanup_old_tags)

    def test_import_scan_llm_classify(self):
        from daemons.maintenance_daemon import scan_llm_classify
        assert callable(scan_llm_classify)

    def test_dispatch_map_coverage(self):
        """_DISPATCH_MAP should cover all 4 agent types."""
        from daemons.maintenance_daemon import _DISPATCH_MAP
        assert set(_DISPATCH_MAP.keys()) == {"fixer", "reviewer", "builder", "claude"}
        for agent in ("fixer", "reviewer", "builder", "claude"):
            assert "assignee" in _DISPATCH_MAP[agent]
            assert "domain" in _DISPATCH_MAP[agent]


@pytest.mark.integration
class TestSafetyNetIntegration:
    """Smoke tests that require MCP server running."""

    @pytest.mark.asyncio
    async def test_scan_duplicate_clusters_no_crash(self):
        """scan_duplicate_clusters should not raise."""
        from daemons.maintenance_daemon import scan_duplicate_clusters
        try:
            await scan_duplicate_clusters()
        except Exception as e:
            pytest.fail(f"scan_duplicate_clusters raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_stale_worth_no_crash(self):
        """scan_stale_worth should not raise."""
        from daemons.maintenance_daemon import scan_stale_worth
        try:
            await scan_stale_worth()
        except Exception as e:
            pytest.fail(f"scan_stale_worth raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_tier_migration_no_crash(self):
        """scan_tier_migration should not raise."""
        from daemons.maintenance_daemon import scan_tier_migration
        try:
            await scan_tier_migration()
        except Exception as e:
            pytest.fail(f"scan_tier_migration raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_orphan_steps_no_crash(self):
        """scan_orphan_steps should not raise when MCP is available."""
        from daemons.maintenance_daemon import scan_orphan_steps
        try:
            await scan_orphan_steps()
        except Exception as e:
            pytest.fail(f"scan_orphan_steps raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_unclosed_issues_no_crash(self):
        """scan_unclosed_issues should not raise when MCP is available."""
        from daemons.maintenance_daemon import scan_unclosed_issues
        try:
            await scan_unclosed_issues()
        except Exception as e:
            pytest.fail(f"scan_unclosed_issues raised: {e}")

    @pytest.mark.asyncio
    async def test_dispatch_multi_agent(self):
        """Verify dispatch works for all 4 agent types."""
        from daemons.maintenance_daemon import dispatch_fix_task
        for agent in ("fixer", "reviewer", "builder", "claude"):
            try:
                await dispatch_fix_task(
                    task_type="test_multi_agent",
                    detail=f"Smoke test dispatch → {agent}",
                    target_id=f"test-{agent}",
                    assignee=agent,
                    severity="info",
                )
            except Exception as e:
                pytest.fail(f"dispatch → {agent} raised: {e}")

    @pytest.mark.asyncio
    async def test_tag_for_redo_no_crash(self):
        """tag_for_redo should not raise."""
        from daemons.maintenance_daemon import tag_for_redo
        try:
            await tag_for_redo(
                memory_id="test-dummy-id",
                reason="测试打回区标记",
                assignee="reviewer",
                severity="info",
            )
        except Exception as e:
            pytest.fail(f"tag_for_redo raised: {e}")

    @pytest.mark.asyncio
    async def test_tag_audit_finding_no_crash(self):
        """tag_audit_finding should not raise."""
        from daemons.maintenance_daemon import tag_audit_finding
        try:
            await tag_audit_finding(
                dimension="memory_quality",
                detail="测试审计发现",
                severity="info",
            )
        except Exception as e:
            pytest.fail(f"tag_audit_finding raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_innovation_opportunities_no_crash(self):
        """scan_innovation_opportunities should not raise."""
        from daemons.maintenance_daemon import scan_innovation_opportunities
        try:
            await scan_innovation_opportunities()
        except Exception as e:
            pytest.fail(f"scan_innovation_opportunities raised: {e}")

    @pytest.mark.asyncio
    async def test_scan_redo_queue_no_crash(self):
        """scan_redo_queue should not raise."""
        from daemons.maintenance_daemon import scan_redo_queue
        try:
            await scan_redo_queue()
        except Exception as e:
            pytest.fail(f"scan_redo_queue raised: {e}")
