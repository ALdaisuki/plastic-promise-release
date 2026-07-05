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
通过 hook 系统触发 skill_auto_track。
"""

import json

from mcp.types import TextContent

from plastic_promise.skills.closure_runner import run_post_task_best_effort
from plastic_promise.skills.engine import SkillDef, SkillResult

# ═══════════════════════════════════════════════════════════════
# 阶段 → 域映射
# ═══════════════════════════════════════════════════════════════

STAGE_DOMAIN_MAP = {
    "audit": "governing",
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
    "audit": ["stage:audit", "domain:governing", "task:verify"],
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
    "audit": "SuperPowers 阶段: 审计 — 高风险PR完整审计 (10项检查 + audit_run)",
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
# Plastic Promise 治理注入: step_closure + code_memory handlers
# ═══════════════════════════════════════════════════════════════


STAGE_GUIDANCE_MAP = {
    "brainstorming": {
        "layer": "design",
        "summary": "Clarify requirements, compare approaches, and write the approved design spec.",
        "artifact": "Design spec under docs/superpowers/specs before implementation begins.",
        "handoff": "After the spec is reviewed, continue to exemplar-research.",
        "artifacts": [
            {
                "kind": "design_spec",
                "path": "docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md",
                "required": True,
            }
        ],
        "next_actions": [
            "Write or update the design spec.",
            "Run step-closure for the current brainstorming phase.",
            "Proceed to exemplar-research only after design intent is explicit.",
        ],
    },
    "exemplar-research": {
        "layer": "research",
        "summary": "Compare mature implementations and extract patterns worth adapting.",
        "artifact": "Exemplar analysis under docs/superpowers/specs/engineering-patterns.",
        "handoff": "After research findings are captured, continue to using-git-worktrees.",
        "artifacts": [
            {
                "kind": "exemplar_analysis",
                "path": "docs/superpowers/specs/engineering-patterns/YYYY-MM-DD-<topic>.md",
                "required": True,
            }
        ],
        "next_actions": [
            "Record the three-question analysis.",
            "Run step-closure for the current exemplar-research phase.",
            "Proceed to using-git-worktrees after findings are captured.",
        ],
    },
    "using-git-worktrees": {
        "layer": "isolation",
        "summary": "Confirm isolated branch/worktree state before implementation.",
        "artifact": "Branch/worktree status and baseline verification evidence.",
        "handoff": "After isolation is confirmed, continue to writing-plans.",
        "artifacts": [
            {"kind": "workspace_state", "path": "git status --short", "required": True}
        ],
        "next_actions": [
            "Confirm the current branch or worktree.",
            "Run step-closure if the workspace state changed.",
            "Proceed to writing-plans.",
        ],
    },
    "writing-plans": {
        "layer": "planning",
        "summary": "Turn the design into exact implementation steps and verification commands.",
        "artifact": "Implementation plan under docs/superpowers/plans before code changes.",
        "handoff": "After the plan is written, continue to executing-plans or subagent-driven-development.",
        "artifacts": [
            {
                "kind": "implementation_plan",
                "path": "docs/superpowers/plans/YYYY-MM-DD-<feature>.md",
                "required": True,
            }
        ],
        "next_actions": [
            "Write or update the implementation plan.",
            "Run step-closure for the current writing-plans phase.",
            "Proceed to executing-plans or subagent-driven-development.",
        ],
    },
    "executing-plans": {
        "layer": "implementation",
        "summary": "Apply the written plan step by step without skipping verification checkpoints.",
        "artifact": "Code changes and per-task verification notes tied to the plan.",
        "handoff": "After implementation tasks are complete, continue to test-driven-development.",
        "artifacts": [{"kind": "code_delta", "path": "git diff", "required": True}],
        "next_actions": [
            "Execute the plan checklist.",
            "Run step-closure after substantive outputs or commits.",
            "Proceed to test-driven-development.",
        ],
    },
    "subagent-driven-development": {
        "layer": "implementation",
        "summary": "Dispatch isolated implementation tasks and review each result before integration.",
        "artifact": "Subagent task prompts, returned diffs, and review notes.",
        "handoff": "After accepted subagent work, continue to test-driven-development.",
        "artifacts": [
            {"kind": "subagent_trace", "path": "task/subagent result log", "required": True}
        ],
        "next_actions": [
            "Dispatch only independent tasks.",
            "Review each result before applying it.",
            "Run step-closure after accepted work.",
        ],
    },
    "test-driven-development": {
        "layer": "testing",
        "summary": "Write failing tests first, implement minimally, then keep the suite green.",
        "artifact": "Red-green evidence from pytest or the relevant test runner.",
        "handoff": "After tests cover the behavior, continue to verification-before-completion.",
        "artifacts": [{"kind": "test_evidence", "path": "pytest <target> -q", "required": True}],
        "next_actions": [
            "Show the red test failure.",
            "Implement the minimal fix.",
            "Run step-closure after the TDD cycle.",
        ],
    },
    "verification-before-completion": {
        "layer": "verification",
        "summary": "Run fresh verification commands before making completion claims.",
        "artifact": "Fresh command output proving tests/build/smoke checks pass.",
        "handoff": "After verification passes, continue to finishing-a-development-branch.",
        "artifacts": [
            {"kind": "verification_log", "path": "fresh verification command output", "required": True}
        ],
        "next_actions": [
            "Run the full targeted verification command.",
            "Read the output and record failures if any.",
            "Run step-closure before finishing.",
        ],
    },
    "finishing-a-development-branch": {
        "layer": "integration",
        "summary": "Decide how the branch is integrated, then merge or prepare release handoff.",
        "artifact": "Commit, PR, merge, and release-sync evidence as requested.",
        "handoff": "After branch finish, stop unless the user requested deployment or release sync.",
        "artifacts": [
            {"kind": "integration_record", "path": "git log / PR / release-sync output", "required": True}
        ],
        "next_actions": [
            "Run final verification.",
            "Run step-closure with commit or PR evidence.",
            "Merge or release only when explicitly requested.",
        ],
    },
    "requesting-code-review": {
        "layer": "review",
        "summary": "Prepare a structured review request from the current diff.",
        "artifact": "Review prompt, changed-file list, and pre-check result.",
        "handoff": "After review is requested, continue to receiving-code-review when feedback arrives.",
        "artifacts": [{"kind": "review_request", "path": "review_run prepare output", "required": True}],
        "next_actions": [
            "Run review preparation.",
            "Run step-closure for the review request.",
            "Wait for review feedback.",
        ],
    },
    "receiving-code-review": {
        "layer": "review",
        "summary": "Evaluate review feedback technically and apply only verified fixes.",
        "artifact": "Review report, applied fixes, and re-verification evidence.",
        "handoff": "After feedback is resolved, continue to audit or finishing-a-development-branch.",
        "artifacts": [
            {"kind": "review_resolution", "path": "review_run evaluate/apply output", "required": True}
        ],
        "next_actions": [
            "Verify each review item against code reality.",
            "Apply fixes one at a time.",
            "Run step-closure after review resolution.",
        ],
    },
    "audit": {
        "layer": "audit",
        "summary": "Run structured self-audit before integration or release.",
        "artifact": "audit_run report and pass/block decision.",
        "handoff": "If audit passes, continue to finishing-a-development-branch.",
        "artifacts": [{"kind": "audit_report", "path": "audit_run(action='full')", "required": True}],
        "next_actions": [
            "Run audit_run or the local audit checklist.",
            "Record pass/block decision.",
            "Run step-closure for the audit phase.",
        ],
    },
    "systematic-debugging": {
        "layer": "debugging",
        "summary": "Find root cause before fixing unexpected behavior.",
        "artifact": "Root-cause notes, reproduction, hypothesis, and fix verification.",
        "handoff": "After root cause is known, continue to test-driven-development.",
        "artifacts": [
            {"kind": "debug_trace", "path": "root-cause investigation notes", "required": True}
        ],
        "next_actions": [
            "Reproduce and trace the failure.",
            "Write a failing regression test.",
            "Run step-closure after the fix is verified.",
        ],
    },
    "dispatching-parallel-agents": {
        "layer": "coordination",
        "summary": "Split independent work across agents with injected context.",
        "artifact": "Dispatch prompts and returned task summaries.",
        "handoff": "After agents return, review and integrate results through executing-plans.",
        "artifacts": [
            {"kind": "dispatch_record", "path": "agent dispatch prompts/results", "required": True}
        ],
        "next_actions": [
            "Inject context before dispatch.",
            "Review each returned result.",
            "Run step-closure after integration.",
        ],
    },
}


STAGE_ROUTE_MAP = {
    "normal-development": {
        "label": "Normal development",
        "summary": "Standard feature/change route from design through implementation, verification, and branch finishing.",
        "stages": [
            "brainstorming",
            "exemplar-research",
            "using-git-worktrees",
            "writing-plans",
            "executing-plans",
            "test-driven-development",
            "verification-before-completion",
            "finishing-a-development-branch",
        ],
    },
    "audit-review": {
        "label": "Audit/review",
        "summary": "Review route for structured code review, feedback handling, audit, and integration decisions.",
        "stages": [
            "requesting-code-review",
            "receiving-code-review",
            "audit",
            "finishing-a-development-branch",
        ],
    },
    "bug-hunt": {
        "label": "Bug hunt",
        "summary": "Debugging route that finds root cause before writing regression tests and verifying the fix.",
        "stages": [
            "systematic-debugging",
            "test-driven-development",
            "verification-before-completion",
            "finishing-a-development-branch",
        ],
    },
}

STAGE_DEFAULT_ROUTE_MAP = {
    "requesting-code-review": "audit-review",
    "receiving-code-review": "audit-review",
    "audit": "audit-review",
    "systematic-debugging": "bug-hunt",
}


def _closure_mode_for_stage(stage_name: str) -> str:
    atoms = STAGE_ATOMS.get(stage_name, [])
    if "step_closure_full" in atoms:
        return "full"
    if "step_closure_light" in atoms:
        return "light"
    return "as-needed"


def _infer_route_id(stage_name: str, route_id: str | None = None) -> str:
    requested = str(route_id or "").strip()
    if requested:
        return requested
    if stage_name in STAGE_DEFAULT_ROUTE_MAP:
        return STAGE_DEFAULT_ROUTE_MAP[stage_name]
    if stage_name in STAGE_ROUTE_MAP["normal-development"]["stages"]:
        return "normal-development"
    return "custom"


def _skill_authority_message(summary_kind: str, official_skill: str) -> str:
    return (
        f"This {summary_kind} summary is advisory only. The official SuperPowers SKILL "
        f"is {official_skill}; agents must load/read that SKILL before executing "
        "the current stage and must not begin development from sp-stage summaries alone."
    )


def _build_route_summary(stage_name: str, route_id: str | None = None) -> dict:
    resolved_route = _infer_route_id(stage_name, route_id)
    route = STAGE_ROUTE_MAP.get(
        resolved_route,
        {
            "label": "Custom route",
            "summary": "Caller-defined route; follow SKILL_CHAIN_MAP for valid stage transitions.",
            "stages": [stage_name],
        },
    )
    stages = list(route["stages"])
    official_skill = f"superpowers:{stage_name}"
    return {
        "route_id": resolved_route,
        "label": route["label"],
        "summary": route["summary"],
        "official_skill": official_skill,
        "skill_authority": _skill_authority_message("route", official_skill),
        "stages": stages,
        "current_stage": stage_name,
        "current_index": stages.index(stage_name) if stage_name in stages else None,
        "advisory_only": True,
        "session_isolation": (
            "Use stage_session_id plus flow_line_id to isolate concurrent "
            "SuperPowers flow lines within the same agent session."
        ),
    }


def build_stage_guidance(
    stage_name: str,
    closed: bool | None = None,
    route_id: str | None = None,
) -> dict:
    """Return stage-specific workflow guidance for sp-stage responses."""
    guidance = STAGE_GUIDANCE_MAP.get(
        stage_name,
        {
            "layer": "workflow",
            "summary": f"Execute SuperPowers stage {stage_name}.",
            "artifact": "Record durable evidence for this stage.",
            "handoff": "Continue to the next valid stage from SKILL_CHAIN_MAP.",
            "artifacts": [],
            "next_actions": [
                "Confirm the stage output.",
                "Run step-closure for substantive work.",
                "Continue through SKILL_CHAIN_MAP.",
            ],
        },
    )
    closure_mode = _closure_mode_for_stage(stage_name)
    official_skill = f"superpowers:{stage_name}"
    skill_authority = _skill_authority_message("stage", official_skill)
    return {
        "official_skill": official_skill,
        "stage_summary": {
            "stage": stage_name,
            "layer": guidance["layer"],
            "summary": guidance["summary"],
            "skill_authority": skill_authority,
        },
        "route_summary": _build_route_summary(stage_name, route_id=route_id),
        "artifact_summary": guidance["artifact"],
        "handoff_summary": guidance["handoff"],
        "required_artifacts": list(guidance["artifacts"]),
        "closure_reminder": {
            "tool": "step-closure",
            "mode": closure_mode,
            "current_stage": stage_name,
            "required": closure_mode != "as-needed",
            "sp_stage_closed": closed,
            "message": (
                f"Execute step-closure for current stage '{stage_name}' "
                f"with mode='{closure_mode}' after substantive output or commit."
            ),
        },
        "next_actions": list(guidance["next_actions"]),
    }


def attach_stage_guidance(
    data: dict,
    stage_name: str,
    closed: bool | None = None,
    route_id: str | None = None,
) -> dict:
    """Attach guidance to a SkillResult data dict without overwriting custom handlers."""
    if not isinstance(data, dict):
        data = {}
    data.setdefault(
        "stage_guidance",
        build_stage_guidance(stage_name, closed=closed, route_id=route_id),
    )
    return data


async def _governance_step_closure_light(ctx, params: dict):
    """轻量闭环 — 原则对齐检查 + 上下文注入（跳过 SCARF/激素/信任联动）。

    用于设计阶段 (brainstorming, exemplar-research, writing-plans)。
    无实质代码产出时使用，仅做原则对齐验证和上下文记录。
    """
    task_desc = params.get("task_description", "step-closure-light")

    closure = await run_post_task_best_effort(task_description=task_desc, mode="light")
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "closed": closure.completed,
                    "mode": "light",
                    "timed_out": closure.timed_out,
                    "skipped": closure.skipped,
                    "reason": closure.reason,
                },
                ensure_ascii=False,
            ),
        )
    ]


async def _governance_step_closure_full(ctx, params: dict):
    """完整六联闭环 — 原则对齐→SCARF→激素→信任→反思→CEI。

    用于实施/验证/治理阶段 (executing, TDD, verification, finishing)。
    每次有实质产出 (git commit / 设计决策 / 修复完成) 后必须执行。
    """
    task_desc = params.get("task_description", "step-closure-full")
    git_commit = params.get("git_commit", "")
    lesson = params.get("lesson", "")
    improvement = params.get("improvement", "")
    root_cause = params.get("root_cause", "")
    optimization = params.get("optimization", "")

    closure = await run_post_task_best_effort(
        task_description=task_desc,
        git_commit=git_commit,
        mode="full",
        lesson=lesson or f"sp-stage: {task_desc[:100]}",
        improvement=improvement or "下次遵循 SuperPowers 链约束，不跳步",
        root_cause=root_cause or "阶段执行完毕，正常闭环",
        optimization=optimization or "继续执行下一阶段",
    )
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "closed": closure.completed,
                    "mode": "full",
                    "timed_out": closure.timed_out,
                    "skipped": closure.skipped,
                    "reason": closure.reason,
                },
                ensure_ascii=False,
            ),
        )
    ]



# ═══════════════════════════════════════════════════════════════
# 通用 Stage Handler
# ═══════════════════════════════════════════════════════════════


async def _stage_handler(ctx, params, atom_results, stage_name):
    """通用 SuperPowers 阶段处理器。

    组装 atom_results 并返回统一的阶段追踪结果。
    注意: ctx 是 ContextEngine 实例，atom handlers 直接调用 ctx 的方法。
    """
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
    defense_data = parse(atom_results.get("defense"))
    closure_data = parse(
        atom_results.get("step_closure_light") or atom_results.get("step_closure_full")
    )

    return SkillResult(
        skill_name=f"sp-{stage_name}",
        success=True,
        data={
            "stage": stage_name,
            "domain": domain,
            "tags": tags,
            "principles": principle_data.get("activated", []),
            "memory_id": store_data.get("memory_id", ""),
            "trust": defense_data if defense_data else "unchecked",
            "closed": closure_data.get("closed", False) if closure_data else None,
            "stage_guidance": build_stage_guidance(
                stage_name,
                closed=closure_data.get("closed", False) if closure_data else None,
                route_id=params.get("route"),
            ),
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
    import json as _json
    import time

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
    # ── 设计阶段: 信任检查 + 原则激活 + 轻量闭环 ──
    # Retrieval is explicit (memory_recall/context_supply) so stage entry stays fast.
    "brainstorming": [
        "defense",
        "principle_activate",
        "step_closure_light",
    ],
    "exemplar-research": [
        "defense",
        "principle_activate",
        "step_closure_light",
    ],
    "writing-plans": [
        "defense",
        "principle_activate",
        "step_closure_light",
    ],
    # ── 实施阶段: 信任检查 + 原则激活 + 完整闭环 ──
    "executing-plans": ["defense", "principle_activate", "step_closure_full"],
    "subagent-driven-development": [
        "defense",
        "principle_activate",
        "step_closure_full",
    ],
    "test-driven-development": [
        "defense",
        "principle_activate",
        "step_closure_full",
    ],
    "verification-before-completion": [
        "defense",
        "principle_activate",
        "memory_gc",
        "step_closure_full",
    ],
    "using-git-worktrees": ["defense", "principle_activate"],
    "dispatching-parallel-agents": [
        "defense",
        "principle_activate",
    ],
    # ── 审查阶段: + audit_run；retrieval remains explicit ──
    "requesting-code-review": [
        "defense",
        "principle_activate",
        "audit_run",
        "step_closure_full",
    ],
    "receiving-code-review": [
        "defense",
        "principle_activate",
        "audit_run",
        "step_closure_full",
    ],
    # ── 治理阶段: + defense(adjust) + 审计 + GC + 经验包 ──
    "audit": [
        "defense",
        "principle_activate",
        "audit_run",
        "step_closure_full",
    ],
    "finishing-a-development-branch": [
        "defense",
        "principle_activate",
        "audit_run",
        "memory_gc",
        "step_closure_full",
        "pack_export",
    ],
    # ── 修复阶段: 信任检查 + 原则激活 + 完整闭环 ──
    "systematic-debugging": [
        "defense",
        "principle_activate",
        "step_closure_full",
    ],
}

STAGE_DEGRADE = {
    "principle_activate": "skip",
    "memory_store": "warn",
    "memory_recall": "skip",
    "context_supply": "skip",
    "audit_run": "fallback:audit_run_light",
    "memory_gc": "skip",
    "defense": "warn",
    "step_closure_light": "skip",
    "step_closure_full": "warn",
    "pack_export": "skip",
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
    elif _stage_name == "audit":
        try:
            from plastic_promise.skills.audit_handler import _audit_handler

            _handler = _audit_handler
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
        atom_timeout_seconds=5.0,
        track_start_memory=False,
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
audit = SKILL_DEFS.get("audit")


# ═══════════════════════════════════════════════════════════════
# Plugin hook triggering — called by stage handlers
# ═══════════════════════════════════════════════════════════════


def trigger_plugin_hooks(stage_name: str, params: dict) -> list[dict]:
    """Trigger plugin hooks for a stage transition.

    Called by sp-stage handlers after chain validation passes
    and before entering the target stage.

    Args:
        stage_name: Target stage name (e.g. "executing-plans")
        params: Stage parameters including task_description

    Returns:
        List of hook result dicts. Empty list if no hooks registered.
    """
    try:
        from plastic_promise.extensions.loader import PluginLoader

        loader = PluginLoader()
        loader.discover()
        loader.activate_all()  # populate hooks + tools from discovered packs

        task_desc = params.get("task_description", "")
        slot_name = f"on_before_{stage_name.replace('-', '_')}"

        context = {
            "task_description": task_desc,
            "to_stage": stage_name,
        }

        results = loader.trigger_hooks(slot_name, context)
        return [r for r in results if r]  # filter empty results
    except Exception:
        return []  # plugin hooks never block stage execution


def transition_plugin_hooks(from_stage: str, to_stage: str, params: dict) -> list[dict]:
    """Trigger plugin hooks for a transition between two stages.

    Args:
        from_stage: Current stage name
        to_stage: Target stage name
        params: Stage parameters including task_description

    Returns:
        List of hook result dicts. Empty list if no hooks registered.
    """
    try:
        from plastic_promise.extensions.loader import PluginLoader

        loader = PluginLoader()
        loader.discover()
        loader.activate_all()  # populate hooks + tools from discovered packs

        from_key = from_stage.replace("-", "_")
        to_key = to_stage.replace("-", "_")
        slot_name = f"on_transition_{from_key}_{to_key}"
        task_desc = params.get("task_description", "")

        context = {
            "task_description": task_desc,
            "from_stage": from_stage,
            "to_stage": to_stage,
        }

        results = loader.trigger_hooks(slot_name, context)
        return [r for r in results if r]
    except Exception:
        return []
