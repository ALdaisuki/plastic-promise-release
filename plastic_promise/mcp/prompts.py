"""MCP Prompts - 标准操作流程模板

暴露为 MCP Prompt 的标准操作流程:
- run-full-audit - 执行完整的七维度审计
- check-principle-alignment - 检查决策与原则对齐
- daily-reflection - 每日 SCARF 自省 + 记忆演化检查
"""

from typing import List, Dict, Optional


def get_prompt_list() -> List[dict]:
    """返回所有可用的 MCP Prompt 定义。

    Returns:
        Prompt 定义列表, 每项含 name/description/arguments。
    """
    return [
        {
            "name": "run-full-audit",
            "description": "执行完整的七维度审计流程",
            "arguments": [
                {"name": "scope", "description": "审计范围: full/quick"},
            ],
        },
        {
            "name": "check-principle-alignment",
            "description": "检查当前决策是否与核心原则对齐",
            "arguments": [
                {"name": "decision", "description": "当前决策描述"},
            ],
        },
        {
            "name": "daily-reflection",
            "description": "每日 SCARF 自省 + 记忆演化检查",
            "arguments": [],
        },
    ]


def get_prompt(
    name: str,
    arguments: Optional[Dict[str, str]] = None,
) -> dict:
    """获取指定 Prompt 模板的内容。

    Args:
        name: Prompt 名称。
        arguments: Prompt 参数。

    Returns:
        {"messages": [{"role": str, "content": str}]} 格式的 Prompt 结果。
    """
    args = arguments or {}

    if name == "run-full-audit":
        scope = args.get("scope", "full")
        return {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"请执行{scope}范围的七维度审计。\n\n"
                        f"审计维度：原则联想/记忆供应/约束合规/反馈闭环/信任校准/原则继承/安全追溯。\n"
                        f"返回每个维度的评分（0.0-1.0）、发现的问题、建议的修复措施。\n"
                        f"如果评分低于 0.60，标记为 P0 并立即告警。"
                    ),
                }
            ]
        }

    elif name == "check-principle-alignment":
        decision = args.get("decision", "")
        return {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"对于以下决策，逐一检查是否与 11 条核心原则对齐：\n\n"
                        f"决策: {decision}\n\n"
                        f"对每条原则给出：✅ 对齐 / ⚠️ 部分对齐 / ❌ 冲突。\n"
                        f"如果冲突，说明「如果违反会怎样」的反事实预演。"
                    ),
                }
            ]
        }

    elif name == "daily-reflection":
        return {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "执行每日 SCARF 自省。\n\n"
                        "1. 对过去 24 小时的行为进行五维度评分（Status/Certainty/Autonomy/Relatedness/Fairness）\n"
                        "2. 检查记忆池健康度：新增/衰退/GC 数量\n"
                        "3. 检查信任分变化趋势\n"
                        "4. 如有维度低于 0.50，给出改进建议"
                    ),
                }
            ]
        }

    else:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": f"Unknown prompt: {name}",
                }
            ]
        }
