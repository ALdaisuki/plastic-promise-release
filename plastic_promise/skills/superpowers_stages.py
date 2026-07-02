"""域 3: SuperPowers 流水线阶段技能 — 按 SuperPowers 标准流程

SuperPowers 标准化流水线 (obra 定义):
    1. brainstorming              → 头脑风暴，澄清需求
    2. writing-plans              → 编写执行计划
    3. executing-plans            → 执行计划 (含子技能)
    4. subagent-driven-development → 子 Agent 驱动开发
    5. test-driven-development    → TDD 循环
    6. verification-before-completion → 完成前验证
    7. finishing-a-development-branch → 分支完成
    8. requesting-code-review     → 请求代码审查
    9. receiving-code-review      → 接收代码审查

辅助阶段:
    systematic-debugging, using-git-worktrees, dispatching-parallel-agents

每个阶段技能自动获得 SkillEngine 的 session_start/complete 追踪包装。
通过 Trae hooks (PreToolUse/PostToolUse) 触发 skill_auto_track。
"""

import json

from plastic_promise.skills.engine import SkillDef, SkillResult


# ═══════════════════════════════════════════════════════════════
# 阶段 → 域映射
# ═══════════════════════════════════════════════════════════════

STAGE_DOMAIN_MAP = {
    "brainstorming": "designing",
    "exemplar-research": "designing",
    "writing-plans": "designing",
    "executing-plans": "building",
    "subagent-driven-development": "building",
    "test-driven-development": "building",
    "verification-before-completion": "building",
    "finishing-a-development-branch": "governing",
    "requesting-code-review": "reflecting",
    "receiving-code-review": "reflecting",
    "systematic-debugging": "fixing",
    "using-git-worktrees": "building",
    "dispatching-parallel-agents": "building",
}

STAGE_TAGS_MAP = {
    "brainstorming": ["stage:brainstorming", "domain:designing"],
    "exemplar-research": ["stage:exemplar-research", "domain:designing", "task:research"],
    "writing-plans": ["stage:writing-plans", "domain:designing", "task:plan"],
    "executing-plans": ["stage:executing-plans", "domain:building", "task:active"],
    "subagent-driven-development": ["stage:subagent", "domain:building", "task:active"],
    "test-driven-development": ["stage:tdd", "domain:building", "task:active"],
    "verification-before-completion": ["stage:verify", "domain:building", "task:verify"],
    "finishing-a-development-branch": ["stage:finish", "domain:governing", "task:reviewed"],
    "requesting-code-review": ["stage:request-review", "domain:reflecting", "task:review"],
    "receiving-code-review": ["stage:receive-review", "domain:reflecting", "task:reviewed"],
    "systematic-debugging": ["stage:debug", "domain:fixing", "task:active"],
    "using-git-worktrees": ["stage:worktrees", "domain:building"],
    "dispatching-parallel-agents": ["stage:parallel", "domain:building"],
}

STAGE_DESCRIPTIONS = {
    "brainstorming": "SuperPowers 阶段: 头脑风暴 — 需求澄清、方案探索、Socratic 问答",
    "exemplar-research": "SuperPowers 阶段: 典范研究 — 搜索成熟实现、三问法分析、写分析文档、质量审核后入库",
    "writing-plans": "SuperPowers 阶段: 编写计划 — 将需求拆解为可执行的原子任务",
    "executing-plans": "SuperPowers 阶段: 执行计划 — 按计划逐步实施 (含子 Agent 派发、TDD)",
    "subagent-driven-development": "SuperPowers 阶段: 子 Agent 驱动开发 — 并行派发子 Agent 执行独立任务",
    "test-driven-development": "SuperPowers 阶段: TDD 循环 — 先写测试、再写代码、重构",
    "verification-before-completion": "SuperPowers 阶段: 完成前验证 — 三步验收 (Skill链 + 记忆质量 + 经验包)",
    "finishing-a-development-branch": "SuperPowers 阶段: 分支完成 — 最终验收、信任分调整、经验包导出",
    "requesting-code-review": "SuperPowers 阶段: 请求代码审查 — spec 合规检查 + 质量审查",
    "receiving-code-review": "SuperPowers 阶段: 接收代码审查 — 处理审查反馈",
    "systematic-debugging": "SuperPowers 阶段: 系统调试 — 科学调试流程 (假设→插桩→复现→分析→修复)",
    "using-git-worktrees": "SuperPowers 阶段: Git Worktree — 并行多分支开发",
    "dispatching-parallel-agents": "SuperPowers 阶段: 并行派发 — 同时派发多个子 Agent",
}


# ═══════════════════════════════════════════════════════════════
# 通用 Stage Handler
# ═══════════════════════════════════════════════════════════════


async def _stage_handler(ctx, params, atom_results, stage_name):
    """通用 SuperPowers 阶段处理器。

    组装 atom_results 并返回统一的阶段追踪结果。
    注意: ctx 是 ContextEngine 实例，atom handlers 直接调用 ctx 的方法。
    """
    task_desc = params.get("task_description", f"sp-{stage_name} execution")
    domain = STAGE_DOMAIN_MAP.get(stage_name, "building")
    tags = STAGE_TAGS_MAP.get(stage_name, [f"stage:{stage_name}"])

    def parse(result):
        if result and hasattr(result[0], "text"):
            try:
                return json.loads(result[0].text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": result[0].text}
        return {}

    principle_data = parse(atom_results.get("principle_activate"))
    # context_supply removed from atoms — its heavy retrieval was pure overhead
    # since the output was already trimmed from sp-stage response
    store_data = parse(atom_results.get("memory_store"))

    return SkillResult(
        skill_name=f"sp-{stage_name}",
        success=True,
        data={
            "stage": stage_name,
            "domain": domain,
            "tags": tags,
            "principles": principle_data.get("activated", []),
            "memory_id": store_data.get("memory_id", ""),
            "transition": f"→ {stage_name}",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )


# ═══════════════════════════════════════════════════════════════
# 按阶段生成 Handler
# ═══════════════════════════════════════════════════════════════


def _make_handler(stage_name):
    """为指定阶段创建闭包 handler。"""

    async def handler(ctx, params, atom_results):
        return await _stage_handler(ctx, params, atom_results, stage_name)

    return handler


# ═══════════════════════════════════════════════════════════════
# 审查阶段专用 Handlers
# ═══════════════════════════════════════════════════════════════


async def _request_review_handler(ctx, params, atom_results):
    """requesting-code-review 专用 handler — 调用 ReviewEngine.prepare()。

    1. 获取 git diff + 自动化预检
    2. 生成结构化审查 prompt
    3. 将审查请求存入记忆池供 Pi Reviewer 发现
    4. 返回 prompt 供 Claude Code 执行审查
    """
    import time
    import json as _json

    task_desc = params.get("task_description", "代码审查请求")
    commit_range = params.get("commit_range", "HEAD~1..HEAD")

    # 调用 ReviewEngine.prepare()
    review_data = None
    review_error = None
    try:
        from plastic_promise.core.review_engine import ReviewEngine

        # 获取 TrustManager (可选)
        trust_manager = None
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager
            from plastic_promise.defense.trust_store import TrustStore

            trust_manager = TrustManager(trust_store=TrustStore())
        except Exception:
            pass

        review_engine = ReviewEngine(
            trust_manager=trust_manager,
            context_engine=ctx,
        )
        prep = review_engine.prepare(commit_range)
        review_data = {
            "commit_range": commit_range,
            "files_changed": prep["changed_files"],
            "files_count": len(prep["changed_files"]),
            "pre_check": prep["pre_check_results"],
            "git_available": prep["git_available"],
            "prompt": prep["structured_prompt"],
            "prompt_length": len(prep["structured_prompt"]),
        }

        # 将审查请求存入记忆池 (供 Pi Reviewer 发现)
        try:
            ctx.register_memory(
                {
                    "id": f"review_req_{int(time.time())}",
                    "content": prep["structured_prompt"][:500],
                    "memory_type": "task",
                    "source": "claude_code",
                    "tags": [
                        "task:review",
                        "domain:reflecting",
                        f"commit:{commit_range}",
                        "assignee:pi_reviewer",
                        f"ts:{__import__('datetime').datetime.now().strftime('%Y%m%dT%H%M%S')}",
                    ],
                    "tier": "L1",
                }
            )
        except Exception:
            pass  # 记忆存储失败不阻塞审查流程

    except Exception as e:
        review_error = str(e)

    # 组装原则激活和记忆存储的原子结果
    def parse(result):
        if result and hasattr(result[0], "text"):
            try:
                return _json.loads(result[0].text)
            except (_json.JSONDecodeError, TypeError):
                return {"raw": result[0].text}
        return {}

    principle_data = parse(atom_results.get("principle_activate"))
    store_data = parse(atom_results.get("memory_store"))

    return SkillResult(
        skill_name="sp-requesting-code-review",
        success=True,
        data={
            "stage": "requesting-code-review",
            "domain": "reflecting",
            "tags": STAGE_TAGS_MAP.get("requesting-code-review", []),
            "principles": principle_data.get("activated", []),
            "memory_id": store_data.get("memory_id", ""),
            "review": review_data,
            "review_error": review_error,
            "prompt_ready": review_data is not None,
            "transition": "→ requesting-code-review",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[review_error] if review_error else [],
    )


async def _receive_review_handler(ctx, params, atom_results):
    """receiving-code-review 专用 handler — 调用 ReviewEngine.evaluate()+apply()。

    1. 解析 LLM 审查输出为 ReviewReport
    2. 应用审查结果 (信任分调整 + 发现入池 + fix 任务)
    3. 调用 post_task() 六联闭环
    """
    import json as _json

    task_desc = params.get("task_description", "接收代码审查结果")
    review_output = params.get("review_output", "")
    commit_range = params.get("commit_range", "HEAD~1..HEAD")
    author_target = params.get("author_target", "pi_builder")

    apply_result = None
    review_error = None
    report_data = None

    if review_output:
        try:
            from plastic_promise.core.review_engine import ReviewEngine

            trust_manager = None
            try:
                from plastic_promise.defense.soul_enforcer import TrustManager
                from plastic_promise.defense.trust_store import TrustStore

                trust_manager = TrustManager(trust_store=TrustStore())
            except Exception:
                pass

            review_engine = ReviewEngine(
                trust_manager=trust_manager,
                context_engine=ctx,
            )
            prep = review_engine.prepare(commit_range)
            report = review_engine.evaluate(
                diff_text=prep["diff_text"],
                changed_files=prep["changed_files"],
                pre_check=prep["pre_check_results"],
                review_output=review_output,
            )
            report_data = report.to_dict()
            apply_result = review_engine.apply(
                report,
                author_target=author_target,
                reviewer_target="pi_reviewer",
            )

            # 调用 post_task 六联闭环
            try:
                from plastic_promise.loop.soul_loop import post_task

                post_task(
                    task_description=f"审查完成: {report.status} — {report.summary[:100]}",
                    git_commit=commit_range,
                    mode="full",
                    lesson=review_engine._extract_lesson(report),
                    improvement=review_engine._extract_improvement(report),
                )
            except Exception:
                pass

        except Exception as e:
            review_error = str(e)

    def parse(result):
        if result and hasattr(result[0], "text"):
            try:
                return _json.loads(result[0].text)
            except (_json.JSONDecodeError, TypeError):
                return {"raw": result[0].text}
        return {}

    principle_data = parse(atom_results.get("principle_activate"))
    store_data = parse(atom_results.get("memory_store"))

    return SkillResult(
        skill_name="sp-receiving-code-review",
        success=True,
        data={
            "stage": "receiving-code-review",
            "domain": "reflecting",
            "tags": STAGE_TAGS_MAP.get("receiving-code-review", []),
            "principles": principle_data.get("activated", []),
            "memory_id": store_data.get("memory_id", ""),
            "review": {
                "report": report_data,
                "apply": apply_result,
                "error": review_error,
            },
            "transition": "→ receiving-code-review",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[review_error] if review_error else [],
    )


# ═══════════════════════════════════════════════════════════════
# 批量注册所有 SuperPowers 阶段技能
# ═══════════════════════════════════════════════════════════════

STAGE_ATOMS = {
    # 所有阶段统一: 原则激活 + 记忆存储 (context 已 trim，不再白算)
    "brainstorming": ["principle_activate", "memory_store"],
    "exemplar-research": ["principle_activate", "memory_store"],
    "writing-plans": ["principle_activate", "memory_store"],
    "executing-plans": ["principle_activate", "memory_store"],
    "subagent-driven-development": ["principle_activate", "memory_store"],
    "test-driven-development": ["principle_activate", "memory_store"],
    "verification-before-completion": ["principle_activate", "memory_store"],
    "using-git-worktrees": ["principle_activate", "memory_store"],
    "dispatching-parallel-agents": ["principle_activate", "memory_store"],
    # 审查阶段: + audit_run + memory_recall (真实审查管线)
    "requesting-code-review": ["principle_activate", "memory_recall", "audit_run", "memory_store"],
    "receiving-code-review": ["principle_activate", "memory_recall", "audit_run", "memory_store"],
    # 治理阶段: + defense
    "finishing-a-development-branch": ["principle_activate", "defense", "memory_store"],
    # 修复阶段
    "systematic-debugging": ["principle_activate", "memory_store"],
}

STAGE_DEGRADE = {
    "principle_activate": "skip",
    "memory_store": "warn",
    "audit_run": "skip",
    "defense": "warn",
}

SKILL_DEFS = {}

for _stage_name, _atoms in STAGE_ATOMS.items():
    # 审查阶段和 exemplar-research 使用专用 handler，其他阶段使用泛型 handler
    if _stage_name == "requesting-code-review":
        _handler = _request_review_handler
    elif _stage_name == "receiving-code-review":
        _handler = _receive_review_handler
    elif _stage_name == "exemplar-research":
        # Use dedicated handler from exemplar_research module
        try:
            from plastic_promise.skills.exemplar_research import _exemplar_research_handler

            _handler = _exemplar_research_handler
        except ImportError:
            _handler = _make_handler(_stage_name)
    else:
        _handler = _make_handler(_stage_name)

    SKILL_DEFS[_stage_name] = SkillDef(
        name=f"sp-{_stage_name}",
        domain="superpowers_stages",
        description=STAGE_DESCRIPTIONS.get(_stage_name, f"SuperPowers 阶段: {_stage_name}"),
        tier="P0",
        atoms=_atoms,
        degrade_map=STAGE_DEGRADE,
        handler=_handler,
        allowed_callers=["claude", "pi", "trae"],
    )

# 暴露为模块级变量，方便 SkillEngine 自动发现
brainstorming = SKILL_DEFS.get("brainstorming")
writing_plans = SKILL_DEFS.get("writing-plans")
executing_plans = SKILL_DEFS.get("executing-plans")
subagent_driven_development = SKILL_DEFS.get("subagent-driven-development")
test_driven_development = SKILL_DEFS.get("test-driven-development")
verification_before_completion = SKILL_DEFS.get("verification-before-completion")
finishing_a_development_branch = SKILL_DEFS.get("finishing-a-development-branch")
requesting_code_review = SKILL_DEFS.get("requesting-code-review")
receiving_code_review = SKILL_DEFS.get("receiving-code-review")
systematic_debugging = SKILL_DEFS.get("systematic-debugging")
using_git_worktrees = SKILL_DEFS.get("using-git-worktrees")
dispatching_parallel_agents = SKILL_DEFS.get("dispatching-parallel-agents")
exemplar_research = SKILL_DEFS.get("exemplar-research")
