# Three Librarians 对照：检索栈差距分析与改造计划

> 来源: https://research.strata2signal.com/three-librarians/ (strata→signal, 2026-08-18/21)
> 状态: 研究基线 (2026-08-22)。对照分析, 非已完成改造声明。行号以 main 6744841 为准。

## 一、RuleSage 参考架构要点

1. 三检索臂: 词面 FTS (精确词强/语义盲)、语义向量 (意义强/近距模糊/专名雾)、
   结构化"过道臂" (按当前 edition+errata 限定, 每书 8 席 ≤4 文档 ≤32 候选)。
2. RRF 选举 k=60, 一臂一票 1/(k+rank): consensus beats enthusiasm —— 两臂 #3 (≈0.0317)
   稳胜单臂 #1 (≈0.0164); 提名有算术上界 arms/(k+1)。
3. 漏斗: 每臂≤80 → 融合截 80 → cross-encoder 成对重排 (读窗口 头595+尾600+joiner,
   "read both ends" 实测教训) → top 8。
4. Guarantees 覆盖 ranker: 规则书 top1 保位; errata top 永不丢。
5. Lost-in-the-middle 发牌: 最强置两端最弱埋中 (演示序 1,3,5,4,2)。
6. 可派生常量律: fused ceiling = arms/(k+1) 现场推导; "a hardcoded truth is a lie with a delay"。
7. Abstain 纪律: 书中无据明说 not-in-the-book。

## 二、现状映射

已对齐: 词面双通道 (BM25 _text_retrieval + SQLite FTS _fts_retrieval)、语义臂
(LanceDB + governed-node Qwen3 2560 维)、RRF_K 常量 (constants.py:295, 默认 20)、
重排器 (WSL rerank :19132)、debug 投影 (core/retrieval_explain.py retrieval_explain_v1)、
结构化素材 (domain/tier/project/source)。

差距 (按影响排序):

| # | 差距 | 证据 | 对照 |
|---|---|---|---|
| D1 | 主融合非 RRF: _hybrid_fuse (context_engine.py:6941) 跨币种加权 vector×0.7+text×0.3 | 温度与价格平均 | 纯排名票 |
| D2 | max 合并无共识 + BM25 bypass 魔法数 0.75/0.90、FTS 0.85 | 单通道高分即碾压 | consensus 选举; 特例表达为 guarantee |
| D3 | 无第三臂: project/domain/tier 仅做过滤; _layered_fuse (:7049) graph 臂硬顶 0.50 且已有即跳过 | 提名权形同虚设 | 过道臂按活跃域提名 top-k |
| D4 | RRF_K 定义未用于主路径 (死常量) | 比无常量更危险 | 进函数签名 + explain 报告 |
| D5 | fused ceiling 无处派生; explain 无 arms/k/ceiling 字段 | 无法回答理论上限 | 现场计算+公式串 |
| D6 | rerank 读窗口未证实 read both ends (:19132 输入构造) | Monopoly 教训 | 头N+尾N+joiner 强制 |
| D7 | context_supply 组装无 lost-in-the-middle 发牌 | 最强记忆可能落入中段雾区 | 1st,3rd,…,4th,2nd 定序 |
| D8 | 无显式 abstain: 全低分仍返回 | 低分噪音占预算 | 相对 ceiling 比例阈值 → abstain |

## 三、分阶段改造计划

Phase A 融合正确性 (Python): _hybrid_fuse 加 RRF 路径 (PP_FUSION_MODE=rrf|weighted,
shadow 对比先行); bypass 退役为 guarantee; explain 加 arms/rrf_k/fused_ceiling;
recall_quality 回归 + 共识/单臂上界性质测试。
Phase B 第三臂与 guarantees: 会话活跃 domain/project 过道臂 (每源限额防淹没);
principle/governance 与 pin 记忆保位, explain 标注 guarantee 触发。
Phase C 重排与组装: rerank 输入头尾拼接; 注入文本 lost-in-the-middle 定序; abstain。
Phase D Rust 热路径对齐同语义与 explain 字段。

## 四、DSH 节拍器关联

pp-memory-cadence (~/.dsh/profiles/web/node_modules/) 每 every 步调
auto_context_inject(before_invoke), 其质量直接受益 Phase A/B; 已实现客户端命中门控
(minHits/minContentChars, EntityGraph 占位节点不计入); metadata.aisle_hints 可作
Phase B 第三臂提名信号。

## 五、原则对齐

原则 6 数据流驱动 (全部差距引用现行代码位置); 原则 12 代码即文档 (改造落地后回写);
RuleSage confess-with-receipt 文化与原则 3 同构。

## 附: 实弹案例 (2026-08-22)

DSH 节拍注入曾返回 status=degraded、context_pack.core 全部为 content="graph"、
worth_score=0.0 的 EntityGraph 占位节点 (memory_ids=principle:1..4), 无任何真实记忆。
教训: (a) 占位实体不得进入 before_invoke 结果 (服务端待修, 记 D9);
(b) 客户端必须解包 MCP text 内嵌 JSON 再判断 (cadence 已修);
(c) 命中门控是注入质量的最后防线。
