"""Commitment Engineering Integration — post_task mode + TrustManager multi-agent"""

import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCommitmentIntegration:
    def test_post_task_light_mode(self):
        """post_task(mode='light') 只做 alignment，不抛异常"""
        from plastic_promise.loop.soul_loop import SoulLoop

        loop = SoulLoop()
        result = loop.post_task("委派 Issue #12 给 pi_builder", mode="light", issue_id="issue_12")
        assert "alignment" in result
        assert "error" not in str(result.get("alignment", {}))
        assert result.get("issue_id") == "issue_12"
        # light mode 不应执行 SCARF
        assert result.get("scarf") is None

    def test_post_task_full_mode_backward_compat(self):
        """post_task(mode='full') 保持现有六联闭环行为"""
        from plastic_promise.loop.soul_loop import SoulLoop

        loop = SoulLoop()
        result = loop.post_task("验收通过", mode="full")
        assert "alignment" in result
        # full mode 应执行 SCARF
        assert "scarf" in result

    def test_trust_manager_multi_agent(self):
        """TrustManager 支持按 target 独立追踪信任分"""
        from plastic_promise.defense.soul_enforcer import TrustManager

        tm = TrustManager()

        # Claude 默认 trust = 0.60
        assert tm.get() == 0.60
        assert tm.get("pi_builder") == 0.60

        # Boost pi_builder
        tm.boost(0.02, "Issue #12 交付合格", target="pi_builder")
        assert tm.get("pi_builder") == 0.62
        # Claude trust 不变
        assert tm.get() == 0.60

        # Decay pi_fixer
        tm.decay(0.02, "缺少测试", target="pi_fixer")
        assert tm.get("pi_fixer") == 0.58

    def test_trust_tier_per_agent(self):
        """不同 Agent 有独立的 trust tier"""
        from plastic_promise.defense.soul_enforcer import TrustManager

        tm = TrustManager()
        tm.boost(0.25, target="pi_builder")  # 0.60 + 0.25 = 0.85 → high
        tm.decay(0.35, target="pi_fixer")  # 0.60 - 0.35 = 0.25 → critical

        assert tm.tier("pi_builder") == "high"
        assert tm.tier("pi_fixer") == "critical"
        # Claude 不受影响
        assert tm.tier() == "medium"
