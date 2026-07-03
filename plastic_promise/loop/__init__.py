"""主控编排系统（神经中枢）

pre_task_v2 + post_task 完整编排：
上下文供应 -> SCARF 自省 -> 激素更新 -> 记忆演化 -> 审计记录。
"""

from plastic_promise.loop.soul_loop import SoulLoop, post_task, pre_task_v2

__all__ = ["SoulLoop", "pre_task_v2", "post_task"]
