# 系统全貌图谱（System Atlas）

> 2026-08-23 由代理集群分章测绘 + 人工执笔第 6 章。作为「自动化为主、显式为辅」
> 记忆架构深度优化的底图。各章文末自带深度优化候选，本章末尾汇总优先级。

| 章 | 文件 | 内容 | 优化候选数 |
|---|---|---|---|
| 01 | 01-packages.md | 包结构与模块地图（collaboration 30k / core 72k / mcp 27k…） | 5 |
| 02 | 02-data-stores.md | 84 张 SQLite 表 + LanceDB 三根目录 + 双写一致性 | 5 |
| 03 | 03-memory-pipeline.md | 显式写入/被动捕获/检索融合/GC 维护/注入 全链路 | 6 |
| 04 | 04-injection-and-tools.md | 四个自动注入触点 + 51 handler 绑定状态与 Tier | 5 |
| 05 | 05-runtime-topology.md | Mac launchd 服务群 + WSL 计算节点 + edge + 数据流图 | 4 |
| 06 | 06-dsh-integration.md | Day-0 DSH 集成契约（7 触点 + 双向检查清单 + 反模式） | — |

## 深度优化候选汇总（待排期）

| 优先级 | 候选 | 来源章节 | 一句话 |
|---|---|---|---|
| P0 | 被动捕获影子期评审 | 03 | 两周后决定 capture on；晋升恒经 evaluate_auto_promotion |
| P0 | 注入→引用率可观测 | 04 | 目前无法知道注入内容是否被采纳——telemetry 加引用标记 |
| P1 | collaboration 30k 行瘦身评估 | 01 | 单包过重，拆分线索待查 |
| P1 | 图谱 9088 节点 vs 记忆池 6 条倒挂治理 | 02/03 | graph 边缘清理或按需构建 |
| P2 | llama 容器 GPU 让渡/回收脚本化 | 05 | gpu-yield.sh / gpu-resume.sh 一键切换 |
| P2 | market_upgrade 名实不符降级 | 04 | 函数体仅版本检查 |
| P3 | pack_export 出口限权 | 03 | 全池数据出口单独审计 |

## 复测方式

    .venv/bin/python3 scripts/handler_binding_census.py .   # 绑定巡检
    python scripts/tool_usage_report.py --days 14           # 使用遥测基线
