"""MCP Review tool handler — review_run 统一审查入口。

支持四种 action:
  - prepare  : 获取 git diff + 预检 + 生成审查 prompt
  - evaluate : 解析 LLM 审查输出 → ReviewReport
  - apply    : 应用审查结果 (信任分 + 记忆 + fix 任务)
  - full     : prepare + evaluate + apply 完整管线
"""

import json
from typing import Any

from mcp.types import TextContent


async def handle_review_run(engine: Any, args: dict) -> list[TextContent]:
    """执行结构化代码审查 — 审查统一入口。

    三阶段管线:
      1. prepare  — 获取 diff + 预检 + 生成审查 prompt
      2. evaluate — 解析 LLM 审查输出为 ReviewReport
      3. apply    — 信任分调整 + 发现入池 + fix 任务

    Args:
        engine: ContextEngine 实例 (用作 context_engine)
        args: dict with keys:
            action: "prepare" | "evaluate" | "apply" | "full"
            commit_range: git commit 范围 (默认 "HEAD~1..HEAD")
            review_output: LLM 审查输出文本 (evaluate/full 时需要)
            author_target: 被审查 agent (默认 "pi_builder")
            reviewer_target: 审查 agent (默认 "pi_reviewer")
            spec_path: spec 文件路径 (可选)

    Returns:
        list[TextContent]: JSON 编码的审查结果
    """
    action = args.get("action", "full")
    commit_range = args.get("commit_range", "HEAD~1..HEAD")
    review_output = args.get("review_output", "")
    author_target = args.get("author_target", "pi_builder")
    reviewer_target = args.get("reviewer_target", "pi_reviewer")
    spec_path = args.get("spec_path")

    # Lazy import ReviewEngine
    from plastic_promise.core.review_engine import ReviewEngine

    # Lazy init TrustManager for trust integration
    trust_manager = None
    try:
        from plastic_promise.defense.soul_enforcer import TrustManager
        from plastic_promise.defense.trust_store import TrustStore

        trust_manager = TrustManager(trust_store=TrustStore())
    except Exception:
        pass  # trust 不可用时降级 — 审查仍可执行

    review_engine = ReviewEngine(
        trust_manager=trust_manager,
        context_engine=engine,
    )

    try:
        if action == "prepare":
            prep = review_engine.prepare(commit_range, spec_path=spec_path)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "action": "prepare",
                            "commit_range": commit_range,
                            "files_changed": prep["changed_files"],
                            "files_count": len(prep["changed_files"]),
                            "pre_check": prep["pre_check_results"],
                            "git_available": prep["git_available"],
                            "prompt": prep["structured_prompt"],
                            "prompt_length": len(prep["structured_prompt"]),
                            "context_memories_count": len(prep.get("context_memories", [])),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]

        elif action == "evaluate":
            if not review_output:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "review_output is required for evaluate action",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]

            # 需要先跑 prepare 获取 diff
            prep = review_engine.prepare(commit_range, spec_path=spec_path)

            report = review_engine.evaluate(
                diff_text=prep["diff_text"],
                changed_files=prep["changed_files"],
                pre_check=prep["pre_check_results"],
                review_output=review_output,
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "action": "evaluate",
                            "report": report.to_dict(),
                            "findings_count": len(report.findings),
                            "blocker_count": sum(
                                1 for f in report.findings if f.severity == "blocker"
                            ),
                            "major_count": sum(1 for f in report.findings if f.severity == "major"),
                            "trust_delta": report.trust_delta,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]

        elif action == "apply":
            # 需要完整的审查输出 + diff 上下文
            if not review_output:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "review_output is required for apply action",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]

            prep = review_engine.prepare(commit_range, spec_path=spec_path)
            report = review_engine.evaluate(
                diff_text=prep["diff_text"],
                changed_files=prep["changed_files"],
                pre_check=prep["pre_check_results"],
                review_output=review_output,
            )
            result = review_engine.apply(
                report,
                author_target=author_target,
                reviewer_target=reviewer_target,
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "action": "apply",
                            "status": report.status,
                            "recommendation": report.recommendation,
                            "trust_changes": result["trust_changes"],
                            "memory_ids": result["memory_ids"],
                            "fix_tasks": result["fix_tasks"],
                            "fix_tasks_count": len(result["fix_tasks"]),
                            "closure": result.get("closure"),
                            "summary": report.summary,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]

        elif action == "full":
            # 完整管线: prepare → evaluate → apply
            prep = review_engine.prepare(commit_range, spec_path=spec_path)

            if not review_output:
                # 仅返回 prepare 结果 + 提示
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "action": "full",
                                "phase": "prepare_only",
                                "message": "审查 prompt 已生成。请执行审查后将输出作为 review_output 参数传入以完成管线。",
                                "commit_range": commit_range,
                                "files_changed": prep["changed_files"],
                                "pre_check": prep["pre_check_results"],
                                "prompt": prep["structured_prompt"],
                                "prompt_length": len(prep["structured_prompt"]),
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                ]

            report = review_engine.evaluate(
                diff_text=prep["diff_text"],
                changed_files=prep["changed_files"],
                pre_check=prep["pre_check_results"],
                review_output=review_output,
            )
            result = review_engine.apply(
                report,
                author_target=author_target,
                reviewer_target=reviewer_target,
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "action": "full",
                            "report": report.to_dict(),
                            "trust_changes": result["trust_changes"],
                            "memory_ids": result["memory_ids"],
                            "fix_tasks": result["fix_tasks"],
                            "fix_tasks_count": len(result["fix_tasks"]),
                            "closure": result.get("closure"),
                            "stats": review_engine.get_stats(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]

        else:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": f"Unknown action: {action}. Valid: prepare, evaluate, apply, full",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": str(e),
                        "action": action,
                        "commit_range": commit_range,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
