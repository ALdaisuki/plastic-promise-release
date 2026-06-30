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
    """
    domain = STAGE_DOMAIN_MAP.get(stage_name, "building")
    tags = STAGE_TAGS_MAP.get(stage_name, [f"stage:{stage_name}"])

    def parse(result):
        if result and hasattr(result[0], 'text'):
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
# 批量注册所有 SuperPowers 阶段技能
# ═══════════════════════════════════════════════════════════════

STAGE_ATOMS = {
    # 所有阶段统一: 原则激活 + 记忆存储 (context 已 trim，不再白算)
    "brainstorming": ["principle_activate", "memory_store"],
    "writing-plans": ["principle_activate", "memory_store"],
    "executing-plans": ["principle_activate", "memory_store"],
    "subagent-driven-development": ["principle_activate", "memory_store"],
    "test-driven-development": ["principle_activate", "memory_store"],
    "verification-before-completion": ["principle_activate", "memory_store"],
    "using-git-worktrees": ["principle_activate", "memory_store"],
    "dispatching-parallel-agents": ["principle_activate", "memory_store"],
    # 审查阶段: + audit_run
    "requesting-code-review": ["principle_activate", "audit_run", "memory_store"],
    "receiving-code-review": ["principle_activate", "audit_run", "memory_store"],
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
    SKILL_DEFS[_stage_name] = SkillDef(
        name=f"sp-{_stage_name}",
        domain="superpowers_stages",
        description=STAGE_DESCRIPTIONS.get(_stage_name, f"SuperPowers 阶段: {_stage_name}"),
        tier="P0",
        atoms=_atoms,
        degrade_map=STAGE_DEGRADE,
        handler=_make_handler(_stage_name),
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