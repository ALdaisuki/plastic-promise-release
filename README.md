# Plastic Promise

**Plastic Promise** 是一个以「约定工程」(Commitment Engineering) 替代「约束工程」的 AI 行为治理系统。

> 塑性灵魂：记忆是可塑的，灵魂因记忆存在、因约定成长。

## 核心哲学

| 理念 | 要义 |
|---|---|
| **约定工程** | Agent 遵守规则不是因强制，而是选择遵守约定 |
| **数字身体** | LLM 是神经中枢，Plastic Promise 是它的完整身体 |
| **上下文供应引擎** | 记忆不是"被查询的档案库"，而是"主动供应上下文的引擎" |
| **原则内化** | 原则不是靠防火墙强制执行，而是在 Agent 检索历史决策时自然浮现 |

## 架构：九大数字身体系统

| 系统 | 生物学类比 | 核心模块 |
|---|---|---|
| 感官系统 | 视觉、听觉、触觉 | memory_recall、GitNexus、code_search |
| 运动系统 | 手、脚、协作 | exec/write/edit、ACP、Beads |
| 记忆系统 | 海马体、大脑皮层 | soul_memory (双层三域 + L1/L3) |
| 反射弧 | 脊髓反射 | soul_enforcer (三层防线) |
| 内分泌系统 | 激素调节 | soul_hormone (评价引擎 + 信任分) |
| 免疫系统 | 免疫细胞、抗体 | soul_audit (七维度 + cron 守护) |
| 遗传系统 | DNA、基因遗传 | soul_principles (单向扩散 + 同步衰减) |
| 自主神经 | 心跳、呼吸 | scan_and_fix、HEARTBEAT |
| 认知系统 | 前额叶、探索欲 | soul_scarf、soul_curiosity |

## 项目结构

```
plastic_promise/          # Python 编排层（13核心模块 + 22脚本）
  context_engine.py       # 上下文供应引擎（Python 薄包装 → Rust 核心）
  soul_loop.py            # 主控编排 (pre_task_v2 + post_task)
  soul_memory.py          # 记忆管理 (RecMem + L1 + EvolveR + GC)
  soul_enforcer.py        # 三层防线 (L0/L1/L2)
  soul_scarf.py           # SCARF 五维度自省
  soul_proprioception.py  # 本体觉 + 惯性抑制
  soul_hormone.py         # 实时反馈激素
  soul_principles.py      # 原则检索 + 继承同步
  soul_classifier.py      # 任务分类 + ACP 路由
  soul_curiosity.py       # 好奇心探索
  soul_audit.py           # 回顾审计 + pre_check 合规率
  skill_extractor.py      # 技能沉淀

rust/context-engine-core/ # Rust 核心引擎 (PyO3 桥接)
  src/
    entity_graph.rs       # 实体关联图谱 + 原则注入
    rank_fuser.rs         # RRF 融合 + 符号规则双通道
    source_tracker.rs     # 来源追溯 + 时间有效性
    association_feedback.rs # 自演化反馈权重
    memory_worth.rs       # Memory Worth 双计数器 (ρ=0.89)
    context_engine.rs     # 主编排器
    principles.rs         # 原则实体定义
```

## 当前状态：重建中

服务器崩溃致备份全丢。当前正在从零重建，同步推进 Rust 核心引擎重构。

- CEI 目标：0.85+（崩溃前 0.860）
- 原则联想目标：≥ 0.80（崩溃前 0.45）
- 闭环率目标：≥ 70%（崩溃前 51%）

## License

MIT
