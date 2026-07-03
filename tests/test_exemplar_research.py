"""Tests for exemplar_research skill handler and helpers."""
import json
import pytest
from plastic_promise.skills.exemplar_research import (
    _parse_atom_result,
    _extract_memory_items,
    _check_source_diversity,
    _analyze_gaps,
    _format_memory_section,
    _format_strategy_section,
    _build_closure_contract,
    _build_agent_templates,
    _build_execution_steps,
    _build_enhanced_instructions,
)


class TestParseAtomResult:
    def test_empty_result(self):
        assert _parse_atom_result(None) == {}
        assert _parse_atom_result([]) == {}

    def test_valid_json(self):
        class FakeText:
            def __init__(self, text):
                self.text = text
        result = [FakeText('{"core": [{"id": "1", "relevance": 0.9}]}')]
        parsed = _parse_atom_result(result)
        assert parsed["core"][0]["id"] == "1"

    def test_invalid_json(self):
        class FakeText:
            def __init__(self, text):
                self.text = text
        result = [FakeText("not json")]
        parsed = _parse_atom_result(result)
        assert parsed["raw"] == "not json"


class TestExtractMemoryItems:
    def test_empty_atom_results(self):
        items = _extract_memory_items({})
        assert items["has_results"] is False
        assert items["total_core"] == 0

    def test_filters_by_relevance(self):
        class FakeText:
            def __init__(self, text):
                self.text = text
        items = _extract_memory_items({
            "memory_recall": [
                FakeText(json.dumps({
                    "core": [
                        {"id": "1", "relevance": 0.95, "content": "high"},
                        {"id": "2", "relevance": 0.60, "content": "low"},
                    ],
                    "related": [
                        {"id": "3", "relevance": 0.50, "content": "mid"},
                        {"id": "4", "relevance": 0.30, "content": "vlow"},
                    ],
                }))
            ]
        })
        assert items["total_core"] == 1  # only 0.95 passes >=0.70
        assert items["total_related"] == 1  # only 0.50 passes >=0.45
        assert items["core"][0]["content"] == "high"


class TestSourceDiversity:
    def test_unique_sources(self):
        items = [
            {"id": "a", "source": "s1"},
            {"id": "b", "source": "s2"},
            {"id": "c", "source": "s1"},
        ]
        assert _check_source_diversity(items) == 2

    def test_single_source(self):
        items = [{"id": "a", "source": "s1"}, {"id": "b", "source": "s1"}]
        assert _check_source_diversity(items) == 1

    def test_empty(self):
        assert _check_source_diversity([]) == 0

    def test_fallback_to_id(self):
        items = [{"id": "a"}, {"id": "b"}]
        assert _check_source_diversity(items) == 2


class TestAnalyzeGaps:
    def test_full_search_when_empty(self):
        result = _analyze_gaps(
            {"core": [], "related": [], "total_core": 0},
            ["rust", "storage"],
        )
        assert result["strategy"] == "full_search"
        assert result["agent_count"] >= 2

    def test_fill_gaps_when_partial(self):
        result = _analyze_gaps(
            {
                "core": [{"id": "1", "relevance": 0.85, "content": "rust pattern"}],
                "total_core": 1,
            },
            ["rust", "storage"],
        )
        assert result["strategy"] == "fill_gaps"

    def test_verify_only_with_diverse_sources(self):
        core = [
            {"id": "1", "source": "proj_a", "content": "pattern 1"},
            {"id": "2", "source": "proj_b", "content": "pattern 2"},
            {"id": "3", "source": "proj_c", "content": "pattern 3"},
        ]
        result = _analyze_gaps({"core": core, "total_core": 3}, ["test"])
        assert result["strategy"] == "verify_only"

    def test_source_monoculture_downgrades_to_fill_gaps(self):
        core = [
            {"id": "1", "source": "same_project", "content": "p1"},
            {"id": "2", "source": "same_project", "content": "p2"},
            {"id": "3", "source": "same_project", "content": "p3"},
        ]
        result = _analyze_gaps({"core": core, "total_core": 3}, ["test"])
        assert result["strategy"] == "fill_gaps"
        assert "来源单一" in result["rationale"]


class TestFormatSections:
    def test_memory_section_with_results(self):
        items = {
            "core": [{"id": "m1", "relevance": 0.90, "content": "test memory"}],
            "related": [],
        }
        section = _format_memory_section(items)
        assert "[已有经验]" in section
        assert "test memory" in section
        assert "90%" in section

    def test_memory_section_empty(self):
        section = _format_memory_section({"core": [], "related": []})
        assert "暂无" in section

    def test_strategy_section(self):
        gap = {
            "strategy": "fill_gaps",
            "agent_count": 2,
            "rationale": "test",
            "missing_dims": ["rust"],
        }
        section = _format_strategy_section(gap)
        assert "fill_gaps" in section
        assert "rust" in section

    def test_closure_contract(self):
        contract = _build_closure_contract()
        assert "[闭环契约]" in contract
        assert "step-closure" in contract
        assert "不跳过" in contract


class TestAgentTemplates:
    def test_verify_only_no_agents(self):
        gap = {"strategy": "verify_only", "agent_count": 0, "missing_dims": []}
        result = _build_agent_templates(gap, {"core": [], "related": []}, "test query")
        assert "无需派发" in result

    def test_full_search_generates_templates(self):
        gap = {
            "strategy": "full_search",
            "agent_count": 2,
            "missing_dims": ["rust", "storage"],
            "covered_dims": [],
        }
        result = _build_agent_templates(
            gap, {"core": [], "related": []}, "rust storage"
        )
        assert "Agent 1" in result
        assert "Agent 2" in result
        assert "结果回流规则" in result
        assert "不直接写入记忆池" in result


class TestExecutionSteps:
    def test_steps_include_closure_checkpoints(self):
        gap = {"strategy": "full_search", "agent_count": 2}
        steps = _build_execution_steps(gap)
        assert "闭环·起点" in steps
        assert "闭环·产出" in steps
        assert "闭环·提交" in steps
        assert "闭环·完成" in steps

    def test_verify_only_skips_agent_step(self):
        gap = {"strategy": "verify_only", "agent_count": 0}
        steps = _build_execution_steps(gap)
        assert "派发子Agent" not in steps

    def test_high_severity_adds_urgency_banner(self):
        gap = {"strategy": "full_search", "agent_count": 2}
        steps = _build_execution_steps(gap, gap_severity="high")
        assert "高优先级" in steps

    def test_low_severity_no_banner(self):
        gap = {"strategy": "full_search", "agent_count": 2}
        steps = _build_execution_steps(gap, gap_severity="low")
        assert "高优先级" not in steps


class TestBuildEnhancedInstructions:
    def test_contains_all_sections(self):
        instructions = _build_enhanced_instructions(
            {"task_description": "Rust storage engine design"},
            {},
        )
        assert "[闭环契约]" in instructions
        assert "[已有经验]" in instructions
        assert "[搜索策略]" in instructions
        assert "[子Agent派发" in instructions
        assert "[执行步骤]" in instructions

    def test_with_gap_signal(self):
        instructions = _build_enhanced_instructions(
            {
                "task_description": "test",
                "gap_signal": {"suggested_search": ["distributed", "consensus"]},
            },
            {},
        )
        assert "distributed" in instructions.lower()
        assert "consensus" in instructions.lower()

    def test_gap_signal_severity_passed_through(self):
        """Verify gap_signal severity flows through to execution steps."""
        instructions = _build_enhanced_instructions(
            {
                "task_description": "critical architecture decision",
                "gap_signal": {
                    "suggested_search": ["raft", "paxos"],
                    "severity": "high",
                    "problem": "选择分布式一致性算法的核心架构决策",
                },
            },
            {},
        )
        assert "高优先级" in instructions
        assert "raft" in instructions.lower()
        assert "paxos" in instructions.lower()
