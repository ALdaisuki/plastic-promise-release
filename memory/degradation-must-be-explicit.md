---
name: degradation-must-be-explicit
description: Silent degradation to defaults is worse than crashing — always annotate degradation cause
metadata:
  type: experience
---

降级必须显式标注原因，不能静默回退到默认值。典型案例：SCARF 在 FallbackEmbedder（零向量）时全部维度返回 0.65 + "无明显信号，维持默认评估"——用户以为系统正常，实际是 Ollama 嵌入服务不可用。

修复方案：(1) 添加 `component_health` 字段统一报告组件状态（healthy/degraded/no_init/fallback）；(2) 降级路径的 assessment 文本显式写原因（"嵌入服务不可用——可信度降低"）；(3) 用不同状态字符串区分降级程度（`degraded_vectors` vs `unavailable` vs `fallback_zero`）。

**Why:** 2026-07-03 session-init 的 SCARF 五维全 0.65，排查耗时远超修复耗时——因为降级是静默的，完全没有信号指出 Ollama 离线。

**How to apply:** 新增任何降级路径时：(1) 确定降级状态字符串；(2) 在 component_health 中注册；(3) 在日志/响应中显式标注降级原因和影响范围。
