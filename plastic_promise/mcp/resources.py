"""MCP Resources - 系统数据的只读视图

暴露为 MCP Resource 的系统数据:
- plastic-promise://principles - 11条核心原则
- plastic-promise://systems - 九大数字身体系统
- plastic-promise://trust-history - 信任分历史
- plastic-promise://audit-latest - 最新审计报告
- plastic-promise://memory-stats - 记忆池统计
"""

import json
from typing import List


def get_resource_list() -> List[dict]:
    """返回所有可用的 MCP Resource 定义。

    Returns:
        Resource 定义列表, 每项含 uri/name/description/mimeType。
    """
    return [
        {
            "uri": "plastic-promise://principles",
            "name": "核心原则列表",
            "description": "11 条核心原则的完整定义",
            "mimeType": "application/json",
        },
        {
            "uri": "plastic-promise://systems",
            "name": "九大数字身体系统",
            "description": "九大系统的名称、类比、成熟度和模块组成",
            "mimeType": "application/json",
        },
        {
            "uri": "plastic-promise://trust-history",
            "name": "信任分变化历史",
            "description": "信任分随时间变化的时序数据",
            "mimeType": "application/json",
        },
        {
            "uri": "plastic-promise://audit-latest",
            "name": "最新审计报告",
            "description": "最近一次七维度审计的完整报告",
            "mimeType": "application/json",
        },
        {
            "uri": "plastic-promise://memory-stats",
            "name": "记忆池统计",
            "description": "记忆总量、健康/衰退分布、类型分布、worth 分布",
            "mimeType": "application/json",
        },
    ]


def read_resource(uri: str) -> str:
    """读取指定 Resource 的当前数据。

    Args:
        uri: 资源 URI (如 plastic-promise://principles)。

    Returns:
        JSON 字符串格式的资源数据。
    """
    if uri == "plastic-promise://principles":
        from plastic_promise.core.constants import CORE_PRINCIPLES
        return json.dumps(CORE_PRINCIPLES, ensure_ascii=False, indent=2)

    elif uri == "plastic-promise://systems":
        from plastic_promise.core.constants import DIGITAL_BODY_SYSTEMS
        return json.dumps(DIGITAL_BODY_SYSTEMS, ensure_ascii=False, indent=2)

    elif uri == "plastic-promise://trust-history":
        return json.dumps({
            "trust_history": [],
            "current_trust": 0.60,
        }, ensure_ascii=False, indent=2)

    elif uri == "plastic-promise://audit-latest":
        return json.dumps({
            "message": "No audit run yet",
            "last_audit": None,
            "dimensions": {},
        }, ensure_ascii=False, indent=2)

    elif uri == "plastic-promise://memory-stats":
        return json.dumps({
            "total_memories": 0,
            "healthy": 0,
            "decaying": 0,
            "forgotten": 0,
            "by_type": {},
            "worth_distribution": {},
        }, ensure_ascii=False, indent=2)

    else:
        return json.dumps({
            "error": f"Unknown resource: {uri}",
        }, ensure_ascii=False)
