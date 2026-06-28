# Phase 3: Soul System Integration — Design Spec

**Date**: 2026-06-28
**Status**: Approved
**Scope**: 深度集成 Plastic Promise 灵魂系统到 agent-interop 编排核心

---

## 1. 目标

将 Plastic Promise 的 7 个灵魂模块成为 agent-interop 的编排核心：
每次 `interop_delegate` 自动执行 pre_task/post_task 完整管线，
`interop_status` 显示 SCARF/Trust/Hormone 等灵魂状态。

## 2. 架构

```
interop_delegate(task)
    │
    ▼
SoulLoop.pre_task_v2(task, type)
    ├── context_supply → ContextPack
    ├── scarf_reflect → 五维度自省
    ├── hormone_regulate → 激素预调节
    ├── defense_pre_check → L0/L1/L2 防线
    └── audit_record → 审计快照
    │
    ▼
agent 执行任务 → 返回结果
    │
    ▼
SoulLoop.post_task(result)
    ├── scarf_reflect → 事后自省
    ├── hormone_update → 信任+情感更新
    ├── memory_evolve → 记忆演化
    ├── cei_recalculate → CEI 重算
    └── audit_record → 审计快照
```

## 3. 7 模块集成

| 模块 | agent-interop 集成点 | 效果 |
|------|---------------------|------|
| SoulLoop | `interop_delegate` 前后 | 完整认知管线 |
| SCARFReflector | `interop_soul` / `interop_status` | 五维度健康度显示 |
| TrustManager | `interop_delegate` 前检查 | 低信任分拒绝委派 |
| SoulHormone | `interop_soul` | 多巴胺/皮质醇状态 |
| SoulCuriosity | `interop_soul` | epsilon + 发现的话题 |
| SoulProprioception | `interop_soul` | 惯性检测 + 模式分析 |
| SoulEnforcer | `interop_delegate` 前 | L0 硬边界拦截 |

## 4. 新增/修改文件

```
bridge/soul_bridge.py        ← ★ SoulLoop Python 桥接层
.pi/extensions/interop-bridge.ts ← 修改：新增 interop_soul/trust/scarf 工具
```

## 5. 新增 Pi 工具

| 工具 | 功能 |
|------|------|
| `interop_soul` | 查看灵魂系统完整状态 |
| `interop_trust` | 查看/调整信任分 |

## 6. interop_delegate 增强

- pre: 调用 SoulLoop.pre_task_v2 → 获取 ContextPack + 防线检查
- defense: L0 硬边界触发时直接拒绝
- post: 调用 SoulLoop.post_task → 更新信任分/记忆/CEI
- 返回结果附带灵魂状态摘要

## 7. 配置

```bash
SOUL_ENABLED=1              # 启用灵魂系统
SOUL_TRUST_INITIAL=0.60     # 初始信任分
SOUL_EPSILON=0.10           # 探索率
```

## 8. 技术决策

- **Python 桥接而非 HTTP**：soul_bridge.py 直接 import Plastic Promise 模块（submodule 已链接），比 HTTP 调用更高效
- **SOUL_ENABLED 开关**：灵魂系统可关闭，回退到纯消息模式
- **防御优先**：L0 硬边界在 pre_task 阶段就拦截，不进入消息队列
