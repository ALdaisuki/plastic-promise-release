"""Tests for Safety-Net Daemon scanner functions (Immune System Phase).

Requires MCP server running on localhost:9020 (integration tests).
"""

import sys
import os
import asyncio
import pytest

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)


class TestSafetyNetImports:
    """Verify all scan functions are importable without side effects."""

    def test_import_dispatch_fix_task(self):
        from daemons.maintenance_daemon import dispatch_fix_task
        assert callable(dispatch_fix_task)

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
    async def test_dispatch_fix_task_no_crash(self):
        """dispatch_fix_task should not raise (test with dummy type)."""
        from daemons.maintenance_daemon import dispatch_fix_task
        try:
            await dispatch_fix_task(
                "test_only",
                "Smoke test dispatch — should be ignored by Pi",
                target_id="test-dummy-id",
            )
        except Exception as e:
            pytest.fail(f"dispatch_fix_task raised: {e}")
