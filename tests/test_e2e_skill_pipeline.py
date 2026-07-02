"""E2E 技能管线集成测试 — 完整日常使用场景覆盖

本文件是技能追踪 + 自动化上下文注入的规范化测试套件。

覆盖场景:
  S1: Claude Code 完整 SuperPowers 开发会话
  S2: Pi Agent 自治流水线 (Builder → Reviewer)
  S3: 自反馈循环 (跨会话上下文积累)
  S4: Bug 修复流程 (debugging → tdd → verification)
  S5: 链路完整性审计 (chain + orphan + gap 检测)
  S6: 优雅降级 (pre_task_v2 失败 + memory_store 失败)
  S7: 跨系统审计 (audit_run 第八维 skill_trace 评分)
"""

import json
import asyncio
import datetime
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import TextContent


# ═══════════════════════════════════════════════════════════
# 时间工具: 使用今天的真实日期，避免孤儿检测的时序问题
# ═══════════════════════════════════════════════════════════

_NOW = datetime.datetime.now(datetime.UTC)  # UTC timestamp used throughout tests
_TODAY = _NOW.strftime("%Y-%m-%d")
_T = lambda h, m, s=0: f"{_TODAY}T{h:02d}:{m:02d}:{s:02d}"
_OLD = lambda hours_ago: (_NOW - datetime.timedelta(hours=hours_ago)).isoformat()

# ═══════════════════════════════════════════════════════════
# Mock 工厂
# ═══════════════════════════════════════════════════════════


def _make_soul_loop_mock(core_items=None):
    """创建 SoulLoop.pre_task_v2 的 mock ContextPack 返回值。"""
    pack = MagicMock()
    core = []
    for item in core_items or []:
        i = MagicMock()
        i.id = item.get("id", "mem_default")
        i.content = item.get("content", "")
        i.relevance = item.get("relevance", 0.85)
        core.append(i)
    pack.core = core
    pack.related = []
    pack.divergent = []
    pack.to_prompt.return_value = "# Context"
    return pack


def _make_start_mock(skill_name, domain="reflecting", entity_id=None):
    eid = entity_id or f"skill:{skill_name}:{_T(10, 0)}"
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "entity_id": eid,
                    "skill_name": skill_name,
                    "status": "active",
                    "domain": domain,
                    "activated_principles": [
                        {"id": 2, "name": "全过程可查可透明"},
                        {"id": 4, "name": "上下文驱动决策"},
                    ],
                    "related_memories": [],
                    "chain_warning": None,
                }
            ),
        )
    ]


def _make_complete_mock(status="done"):
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "entity_id": "skill:test:2026-01-01T00:00:00",
                    "skill_name": "test",
                    "status": status,
                    "duration_ms": 5000,
                    "outcome": "完成",
                    "next_skills": [],
                    "worth_update": {"previous": 0.70, "delta": 0.02, "new": 0.72},
                }
            ),
        )
    ]


def _make_store_result(mid="mem_test"):
    """Build memory_store TextContent return value (synchronous helper)."""
    return [TextContent(type="text", text=json.dumps({"memory_id": mid, "stored": True}))]


# ═══════════════════════════════════════════════════════════
# S1: Claude Code 完整 SuperPowers 开发会话
# ═══════════════════════════════════════════════════════════


class TestS1ClaudeCodeFullSession:
    """日常开发流程: auto_context_inject → brainstorming → writing-plans → SDD → trace。"""

    def test_full_superpowers_development_session(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": f"skill_session:skill:auto_inject:claude_code:{_T(10, 0)}",
            "type": "skill_session",
            "is_new": True,
            "edges_created": 0,
        }
        engine._graph_nodes = {}
        engine._graph_edges = []
        engine._memories = {}

        # ── Phase 1: 会话启动 ──
        with patch("plastic_promise.loop.soul_loop.SoulLoop") as MockLoop:
            mock_loop = MagicMock()
            mock_loop.pre_task_v2.return_value = _make_soul_loop_mock()
            MockLoop.return_value = mock_loop

            with patch(
                "plastic_promise.mcp.tools.memory.handle_memory_store",
                return_value=_make_store_result(),
            ):
                with patch(
                    "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
                ) as mock_start:
                    mock_start.return_value = _make_start_mock("auto_inject:claude_code")
                    with patch(
                        "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
                    ) as mock_complete:
                        mock_complete.return_value = _make_complete_mock()

                        inject_result = asyncio.run(
                            handle_auto_context_inject(
                                engine,
                                {
                                    "task_description": "为 Plastic Promise 设计用户认证模块",
                                    "task_type": "architecture",
                                    "source": "claude_code",
                                    "scope": "agent:claude",
                                },
                            )
                        )

        inject_data = json.loads(inject_result[0].text)
        assert inject_data["skill_name"] == "auto_inject:claude_code"
        assert inject_data["inject_memory_id"] is not None

        # Manually add auto_inject node to graph (handler created it via register_entity mock)
        ai_eid = inject_data["entity_id"]
        engine._graph_nodes[f"skill_session:{ai_eid}"] = {
            "type": "skill_session",
            "name": "auto_inject:claude_code",
            "description": "会话启动",
        }
        engine._memories[f"mem_ai"] = {
            "id": "mem_ai",
            "content": f"[AUTO INJECT] 会话启动\n[SKILL DONE] outcome: 注入完成",
            "memory_type": "experience",
            "entity_ids": [ai_eid],
            "tags": ["auto_inject", "task:done", "skill:auto_inject:claude_code"],
            "worth_score": 0.70,
            "created_at": _T(10, 0),
            "last_accessed": _T(10, 1),
        }

        # ── Phase 2-4: 构建完整的 skill 链 ──
        for skill_name, ts, domain in [
            ("brainstorming", _T(10, 5), "designing"),
            ("writing-plans", _T(10, 35), "designing"),
            ("subagent-driven-development", _T(11, 5), "building"),
            ("finishing-a-development-branch", _T(12, 0), "governing"),
        ]:
            eid = f"skill:{skill_name}:{ts}"
            engine._graph_nodes[f"skill_session:{eid}"] = {
                "type": "skill_session",
                "name": skill_name,
                "description": f"Executing {skill_name}",
            }
            engine._memories[f"mem_{skill_name}"] = {
                "id": f"mem_{skill_name}",
                "content": f"[SKILL START] {skill_name}: test\n[SKILL DONE] outcome: ok",
                "memory_type": "experience",
                "entity_ids": [eid],
                "tags": ["task:done", f"skill:{skill_name}", f"domain:{domain}"],
                "worth_score": 0.72,
                "created_at": ts,
                "last_accessed": _T(12, 30),
            }

        engine._graph_edges = [
            {
                "from": f"skill_session:skill:brainstorming:{_T(10, 5)}",
                "to": f"skill_session:skill:writing-plans:{_T(10, 35)}",
                "relation": "parent_of",
                "weight": 0.8,
            },
            {
                "from": f"skill_session:skill:writing-plans:{_T(10, 35)}",
                "to": f"skill_session:skill:subagent-driven-development:{_T(11, 5)}",
                "relation": "parent_of",
                "weight": 0.8,
            },
            {
                "from": f"skill_session:skill:subagent-driven-development:{_T(11, 5)}",
                "to": f"skill_session:skill:finishing-a-development-branch:{_T(12, 0)}",
                "relation": "parent_of",
                "weight": 0.8,
            },
        ]

        # ── Phase 5: trace 默认过滤 auto_inject ──
        trace_result = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                    "include_auto_inject": False,
                },
            )
        )
        trace_data = json.loads(trace_result[0].text)
        sessions = trace_data["sessions"]

        # 4 个 SuperPowers skill，不含 auto_inject
        skill_names = {s["skill_name"] for s in sessions}
        assert "brainstorming" in skill_names
        assert "writing-plans" in skill_names
        assert "subagent-driven-development" in skill_names
        assert "finishing-a-development-branch" in skill_names
        assert "auto_inject:claude_code" not in skill_names

        # 完整合法链
        assert trace_data["chain_complete"] == True

        # ── Phase 6: trace 包含 auto_inject ──
        trace_all = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                    "include_auto_inject": True,
                },
            )
        )
        all_data = json.loads(trace_all[0].text)
        assert len(all_data["sessions"]) == 5  # 4 skills + 1 auto_inject

    def test_claude_startup_sequence(self):
        """CLAUDE.md 规定: auto_context_inject → system → defense。"""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:claude_code:2026-07-01T10:00:00",
            "type": "skill_session",
            "is_new": True,
            "edges_created": 0,
        }
        engine._graph_nodes = {}
        engine._memories = {}

        with patch("plastic_promise.loop.soul_loop.SoulLoop") as MockLoop:
            mock_loop = MagicMock()
            mock_loop.pre_task_v2.return_value = _make_soul_loop_mock()
            MockLoop.return_value = mock_loop

            with patch(
                "plastic_promise.mcp.tools.memory.handle_memory_store",
                return_value=_make_store_result(),
            ):
                with patch(
                    "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
                ) as mock_start:
                    mock_start.return_value = _make_start_mock("auto_inject:claude_code")
                    with patch(
                        "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
                    ) as mock_complete:
                        mock_complete.return_value = _make_complete_mock()

                        result = asyncio.run(
                            handle_auto_context_inject(
                                engine,
                                {
                                    "task_description": "会话启动",
                                    "source": "claude_code",
                                    "scope": "agent:claude",
                                },
                            )
                        )

        data = json.loads(result[0].text)
        assert data["entity_id"] is not None
        assert len(data["principles"]) >= 1
        assert data["context_pack"] is not None
        assert data["inject_memory_id"] is not None
        assert data.get("errors") is None


# ═══════════════════════════════════════════════════════════
# S2: Pi Agent 自治流水线
# ═══════════════════════════════════════════════════════════


class TestS2PiAgentPipeline:
    """Pi Agent (Builder → Reviewer) 的自动化注入 + skill 追踪。"""

    def test_pi_builder_inject(self):
        """Builder 检测到 task:pending → auto_context_inject → 执行。"""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:pi_agent:builder:2026-01-01T00:00:00",
            "type": "skill_session",
            "is_new": True,
        }

        with patch("plastic_promise.loop.soul_loop.SoulLoop") as MockLoop:
            MockLoop.return_value.pre_task_v2.return_value = _make_soul_loop_mock()

            with patch(
                "plastic_promise.mcp.tools.memory.handle_memory_store",
                return_value=_make_store_result(),
            ):
                with patch(
                    "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
                ) as mock_start:
                    mock_start.return_value = _make_start_mock("auto_inject:pi_agent:builder")
                    with patch(
                        "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
                    ) as mock_complete:
                        mock_complete.return_value = _make_complete_mock()

                        result = asyncio.run(
                            handle_auto_context_inject(
                                engine,
                                {
                                    "task_description": "创建 auth/jwt.py: login() + refresh()",
                                    "task_type": "code_generation",
                                    "source": "pi_agent:builder",
                                },
                            )
                        )

        data = json.loads(result[0].text)
        assert data["skill_name"] == "auto_inject:pi_agent:builder"

    def test_pi_reviewer_inject(self):
        """Builder 完成 → Reviewer 自动触发 → 上下文注入。"""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:pi_agent:reviewer:2026-01-01T00:00:00",
            "type": "skill_session",
            "is_new": True,
        }

        with patch("plastic_promise.loop.soul_loop.SoulLoop") as MockLoop:
            MockLoop.return_value.pre_task_v2.return_value = _make_soul_loop_mock()

            with patch(
                "plastic_promise.mcp.tools.memory.handle_memory_store",
                return_value=_make_store_result(),
            ):
                with patch(
                    "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
                ) as mock_start:
                    mock_start.return_value = _make_start_mock("auto_inject:pi_agent:reviewer")
                    with patch(
                        "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
                    ) as mock_complete:
                        mock_complete.return_value = _make_complete_mock()

                        result = asyncio.run(
                            handle_auto_context_inject(
                                engine,
                                {
                                    "task_description": "审查 auth/jwt.py — login() 逻辑",
                                    "task_type": "code_review",
                                    "source": "pi_agent:reviewer",
                                },
                            )
                        )

        data = json.loads(result[0].text)
        assert data["skill_name"] == "auto_inject:pi_agent:reviewer"


# ═══════════════════════════════════════════════════════════
# S3: 自反馈循环
# ═══════════════════════════════════════════════════════════


class TestS3SelfFeedbackLoop:
    """跨会话上下文积累 — 注入记录沉淀后下次可检索到。"""

    def test_second_inject_retrieves_first_record(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:claude_code:2026-01-01T00:00:00",
            "type": "skill_session",
            "is_new": True,
        }

        # 第一次注入留下的记忆
        first_record = {
            "id": "mem_first",
            "content": "[AUTO INJECT] 修复 JWT token 过期刷新问题\ncore_items: 3\nactivated_principles: 奥卡姆剃刀",
            "memory_type": "experience",
            "entity_ids": ["skill:auto_inject:claude_code:2026-01-01T00:00:00"],
            "tags": ["auto_inject", "source:claude_code", "task:done"],
            "worth_score": 0.72,
        }
        engine._memories = {"mem_first": first_record}

        with patch("plastic_promise.loop.soul_loop.SoulLoop") as MockLoop:
            mock_loop = MagicMock()
            # supply() 返回第一次记录（自反馈命中）
            mock_loop.pre_task_v2.return_value = _make_soul_loop_mock(
                core_items=[
                    {"id": "mem_first", "content": first_record["content"], "relevance": 0.88}
                ]
            )
            MockLoop.return_value = mock_loop

            with patch("plastic_promise.mcp.tools.memory.handle_memory_store") as mock_store:
                mock_store.return_value = _make_store_result("mem_second")
                with patch(
                    "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
                ) as mock_start:
                    mock_start.return_value = _make_start_mock("auto_inject:claude_code")
                    with patch(
                        "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
                    ) as mock_complete:
                        mock_complete.return_value = _make_complete_mock()

                        result = asyncio.run(
                            handle_auto_context_inject(
                                engine,
                                {
                                    "task_description": "修复 OAuth token 刷新竞态条件",
                                    "task_type": "debugging",
                                    "source": "claude_code",
                                    "scope": "agent:claude",
                                },
                            )
                        )

        data = json.loads(result[0].text)
        core = data["context_pack"]["core"]
        assert len(core) == 1
        assert core[0]["id"] == "mem_first"
        assert "JWT" in core[0]["content"]
        assert core[0]["relevance"] >= 0.85

    def test_multi_round_accumulation(self):
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:claude_code:2026-01-01T00:00:00",
            "type": "skill_session",
            "is_new": True,
        }

        # 两轮历史注入
        engine._memories = {
            "mem_r1": {
                "id": "mem_r1",
                "content": "[AUTO INJECT] 设计认证模块\ncore_items: 2",
                "memory_type": "experience",
                "tags": ["auto_inject"],
            },
            "mem_r2": {
                "id": "mem_r2",
                "content": "[AUTO INJECT] 实现 JWT 登录\ncore_items: 3",
                "memory_type": "experience",
                "tags": ["auto_inject"],
            },
        }

        with patch("plastic_promise.loop.soul_loop.SoulLoop") as MockLoop:
            mock_loop = MagicMock()
            mock_loop.pre_task_v2.return_value = _make_soul_loop_mock(
                core_items=[
                    {
                        "id": "mem_r2",
                        "content": engine._memories["mem_r2"]["content"],
                        "relevance": 0.90,
                    },
                    {
                        "id": "mem_r1",
                        "content": engine._memories["mem_r1"]["content"],
                        "relevance": 0.80,
                    },
                ]
            )
            MockLoop.return_value = mock_loop

            with patch("plastic_promise.mcp.tools.memory.handle_memory_store") as mock_store:
                mock_store.return_value = _make_store_result("mem_r3")
                with patch(
                    "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
                ) as mock_start:
                    mock_start.return_value = _make_start_mock("auto_inject:claude_code")
                    with patch(
                        "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
                    ) as mock_complete:
                        mock_complete.return_value = _make_complete_mock()

                        result = asyncio.run(
                            handle_auto_context_inject(
                                engine,
                                {
                                    "task_description": "审查认证模块安全性",
                                    "task_type": "code_review",
                                    "source": "claude_code",
                                },
                            )
                        )

        data = json.loads(result[0].text)
        core_ids = [c["id"] for c in data["context_pack"]["core"]]
        assert "mem_r2" in core_ids
        assert "mem_r1" in core_ids


# ═══════════════════════════════════════════════════════════
# S4: Bug 修复流程
# ═══════════════════════════════════════════════════════════


class TestS4BugFixWorkflow:
    """debugging → tdd → verification 完整追踪链。"""

    def test_bugfix_chain_trace(self):
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        engine._graph_nodes = {}
        engine._graph_edges = []
        engine._memories = {}

        chain = [
            ("systematic-debugging", _T(14, 5), "fixing"),
            ("test-driven-development", _T(14, 20), "building"),
            ("verification-before-completion", _T(14, 35), "reflecting"),
            ("finishing-a-development-branch", _T(15, 0), "governing"),
        ]

        for skill_name, ts, domain in chain:
            eid = f"skill:{skill_name}:{ts}"
            engine._graph_nodes[f"skill_session:{eid}"] = {
                "type": "skill_session",
                "name": skill_name,
            }
            engine._memories[f"mem_{skill_name}"] = {
                "id": f"mem_{skill_name}",
                "content": f"[SKILL START] {skill_name}: bug fix\n[SKILL DONE] outcome: ok",
                "memory_type": "experience",
                "entity_ids": [eid],
                "tags": ["task:done", f"skill:{skill_name}", f"domain:{domain}"],
                "worth_score": 0.73,
                "created_at": ts,
                "last_accessed": _T(14, 40),
            }

        # 合法 parent-child 边: debugging → tdd → verification → finishing
        engine._graph_edges = [
            {
                "from": f"skill_session:skill:systematic-debugging:{_T(14, 5)}",
                "to": f"skill_session:skill:test-driven-development:{_T(14, 20)}",
                "relation": "parent_of",
                "weight": 0.9,
            },
            {
                "from": f"skill_session:skill:test-driven-development:{_T(14, 20)}",
                "to": f"skill_session:skill:verification-before-completion:{_T(14, 35)}",
                "relation": "parent_of",
                "weight": 0.9,
            },
            {
                "from": f"skill_session:skill:verification-before-completion:{_T(14, 35)}",
                "to": f"skill_session:skill:finishing-a-development-branch:{_T(15, 0)}",
                "relation": "parent_of",
                "weight": 0.9,
            },
        ]

        trace_result = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                    "include_auto_inject": False,
                },
            )
        )

        trace_data = json.loads(trace_result[0].text)
        sessions = trace_data["sessions"]
        names = [s["skill_name"] for s in sessions]
        assert len(sessions) == 4
        assert "systematic-debugging" in names
        assert "test-driven-development" in names
        assert "verification-before-completion" in names
        assert "finishing-a-development-branch" in names
        assert trace_data["chain_valid"] == True


# ═══════════════════════════════════════════════════════════
# S5: 链路完整性审计
# ═══════════════════════════════════════════════════════════


class TestS5ChainIntegrityAudit:
    def test_detect_chain_broken(self):
        """brainstorming 完成但无 writing-plans → chain_broken warning。"""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        eid = f"skill:brainstorming:{_T(10, 0)}"
        engine._graph_nodes = {
            f"skill_session:{eid}": {"type": "skill_session", "name": "brainstorming"},
        }
        engine._graph_edges = []
        engine._memories = {
            "mem_b": {
                "id": "mem_b",
                "content": "[SKILL START] brainstorming: 设计 API\n[SKILL DONE] outcome: RESTful",
                "memory_type": "experience",
                "entity_ids": [eid],
                "tags": ["task:done", "skill:brainstorming"],
                "worth_score": 0.70,
                "created_at": _T(10, 0),
                "last_accessed": _T(10, 30),
            },
        }

        trace_result = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                    "include_auto_inject": False,
                },
            )
        )

        trace_data = json.loads(trace_result[0].text)
        chain_warnings = trace_data.get("chain_warnings", [])
        broken = [w for w in chain_warnings if w["type"] == "chain_broken"]
        assert len(broken) >= 1
        assert broken[0]["skill_name"] == "brainstorming"

    def test_detect_orphan_active(self):
        """skill 启动 45 分钟未 complete → orphan_active gap。"""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        eid = f"skill:brainstorming:{_OLD(0.75)}"  # 45 minutes ago
        engine._graph_nodes = {
            f"skill_session:{eid}": {"type": "skill_session", "name": "brainstorming"},
        }
        engine._graph_edges = []
        engine._memories = {
            "mem_orphan": {
                "id": "mem_orphan",
                "content": "[SKILL START] brainstorming: 设计用户系统（被中断）",
                "memory_type": "experience",
                "entity_ids": [eid],
                "tags": ["task:active", "skill:brainstorming"],  # 仍是 active!
                "worth_score": 0.65,
                "created_at": _OLD(0.75),
                "last_accessed": _OLD(0.75),  # 45 分钟前
            },
        }

        trace_result = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                    "include_auto_inject": False,
                },
            )
        )

        trace_data = json.loads(trace_result[0].text)
        gaps = trace_data.get("gaps", [])
        orphans = [g for g in gaps if g["type"] == "orphan_active"]
        assert len(orphans) >= 1, f"Expected orphan_active gap, gaps={gaps}"
        assert orphans[0]["skill_name"] == "brainstorming"

    def test_legal_chain_passes(self):
        """完整合法链: brainstorming → writing-plans → SDD → finish。"""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        engine._graph_nodes = {}
        engine._graph_edges = []
        engine._memories = {}

        skills = [
            ("brainstorming", _T(10, 0)),
            ("writing-plans", _T(10, 30)),
            ("subagent-driven-development", _T(11, 0)),
            ("finishing-a-development-branch", _T(12, 0)),
        ]

        for i, (name, ts) in enumerate(skills):
            eid = f"skill:{name}:{ts}"
            engine._graph_nodes[f"skill_session:{eid}"] = {
                "type": "skill_session",
                "name": name,
            }
            engine._memories[f"mem_{name}"] = {
                "id": f"mem_{name}",
                "content": f"[SKILL START] {name}\n[SKILL DONE] outcome: ok",
                "entity_ids": [eid],
                "tags": ["task:done", f"skill:{name}"],
                "worth_score": 0.72,
                "created_at": ts,
                "last_accessed": _T(12, 30),
            }
            # Parent edge (except first)
            if i > 0:
                prev_name, prev_ts = skills[i - 1]
                engine._graph_edges.append(
                    {
                        "from": f"skill_session:skill:{prev_name}:{prev_ts}",
                        "to": f"skill_session:{eid}",
                        "relation": "parent_of",
                    }
                )

        trace_result = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                    "include_auto_inject": False,
                },
            )
        )

        trace_data = json.loads(trace_result[0].text)
        assert trace_data["chain_complete"] == True
        assert len(trace_data["gaps"]) == 0


# ═══════════════════════════════════════════════════════════
# S6: 优雅降级
# ═══════════════════════════════════════════════════════════


class TestS6GracefulDegradation:
    def test_pre_task_failure_fallback(self):
        """pre_task_v2 失败 → principle_activate fallback 生效。"""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:claude_code:2026-01-01T00:00:00",
            "type": "skill_session",
            "is_new": True,
        }

        with patch("plastic_promise.loop.soul_loop.SoulLoop") as MockLoop:
            mock_loop = MagicMock()
            mock_loop.pre_task_v2.side_effect = Exception("Embedding down")
            MockLoop.return_value = mock_loop

            fallback = [{"id": 1, "name": "奥卡姆剃刀"}, {"id": 2, "name": "全过程可查可透明"}]
            with patch("plastic_promise.mcp.tools.principles.handle_principle_activate") as mock_pa:
                mock_pa.return_value = [
                    TextContent(type="text", text=json.dumps({"activated": fallback}))
                ]

                with patch(
                    "plastic_promise.mcp.tools.memory.handle_memory_store",
                    return_value=_make_store_result(),
                ):
                    with patch(
                        "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
                    ) as mock_start:
                        mock_start.return_value = _make_start_mock("auto_inject:claude_code")
                        with patch(
                            "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
                        ) as mock_complete:
                            mock_complete.return_value = _make_complete_mock()

                            result = asyncio.run(
                                handle_auto_context_inject(
                                    engine,
                                    {
                                        "task_description": "修复 bug",
                                        "source": "claude_code",
                                    },
                                )
                            )

        data = json.loads(result[0].text)
        assert data.get("partial") == True
        names = [p["name"] for p in data.get("principles", [])]
        assert "奥卡姆剃刀" in names
        assert data["entity_id"] is not None

    def test_memory_store_failure_partial(self):
        """memory_store 失败 → context_pack 和 entity 仍返回。"""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:claude_code:2026-01-01T00:00:00",
            "type": "skill_session",
            "is_new": True,
        }

        with patch("plastic_promise.loop.soul_loop.SoulLoop") as MockLoop:
            MockLoop.return_value.pre_task_v2.return_value = _make_soul_loop_mock()

            with patch(
                "plastic_promise.mcp.tools.memory.handle_memory_store",
                side_effect=Exception("DB down"),
            ):
                with patch(
                    "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start"
                ) as mock_start:
                    mock_start.return_value = _make_start_mock("auto_inject:claude_code")
                    with patch(
                        "plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete"
                    ) as mock_complete:
                        mock_complete.return_value = _make_complete_mock()

                        result = asyncio.run(
                            handle_auto_context_inject(
                                engine,
                                {
                                    "task_description": "修复 bug",
                                    "source": "claude_code",
                                },
                            )
                        )

        data = json.loads(result[0].text)
        assert data.get("partial") == True
        assert data["inject_memory_id"] is None
        assert data["context_pack"] is not None
        assert data["entity_id"] is not None


# ═══════════════════════════════════════════════════════════
# S7: 跨系统审计
# ═══════════════════════════════════════════════════════════


class TestS7CrossSystemAudit:
    def test_audit_weights_sum_to_one(self):
        from plastic_promise.core.constants import AUDIT_DIMENSIONS

        total = sum(d["weight"] for d in AUDIT_DIMENSIONS.values())
        assert abs(total - 1.00) < 0.001, f"Sum={total}"
        assert "skill_trace" in AUDIT_DIMENSIONS
        assert AUDIT_DIMENSIONS["skill_trace"]["weight"] == 0.10

    def test_auto_inject_doesnt_pollute_audit(self):
        """10 个 auto_inject + 1 个真实 skill → 默认 trace 只看到 1 个。"""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        engine._graph_nodes = {}
        engine._graph_edges = []
        engine._memories = {}

        for i in range(5):
            eid = f"skill:auto_inject:claude_code:2026-01-01T{i:02d}:00:00"
            engine._graph_nodes[f"skill_session:{eid}"] = {
                "type": "skill_session",
                "name": "auto_inject:claude_code",
            }
            engine._memories[f"mem_ai_{i}"] = {
                "id": f"mem_ai_{i}",
                "content": f"[AUTO INJECT] task{i}",
                "entity_ids": [eid],
                "tags": ["auto_inject", "task:done"],
                "worth_score": 0.70,
                "created_at": _T(i, 0),
                "last_accessed": _T(i, 1),
            }

        eid = f"skill:brainstorming:{_T(15, 0)}"
        engine._graph_nodes[f"skill_session:{eid}"] = {
            "type": "skill_session",
            "name": "brainstorming",
        }
        engine._memories["mem_real"] = {
            "id": "mem_real",
            "content": "[SKILL START] brainstorming: real\n[SKILL DONE] outcome: ok",
            "entity_ids": [eid],
            "tags": ["task:done", "skill:brainstorming"],
            "worth_score": 0.72,
            "created_at": _T(15, 0),
            "last_accessed": _T(15, 30),
        }

        trace = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                    "include_auto_inject": False,
                },
            )
        )
        data = json.loads(trace[0].text)
        assert data["total_count"] == 1

        trace_all = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                    "include_auto_inject": True,
                },
            )
        )
        all_data = json.loads(trace_all[0].text)
        assert all_data["total_count"] == 6  # 5 auto + 1 real

    def test_mcp_tool_count_40(self):
        from plastic_promise.mcp.server import list_tools

        tools = asyncio.run(list_tools())
        assert len(tools) == 40
        names = [t.name for t in tools]
        for expected in [
            "skill_session_start",
            "skill_session_complete",
            "skill_session_trace",
            "skill_session_audit",
            "skill_auto_track",
            "auto_context_inject",
            "memory_reclassify",
            "session-init",
            "smart-remember",
            "step-closure",
        ]:
            assert expected in names, f"Missing tool: {expected}"
