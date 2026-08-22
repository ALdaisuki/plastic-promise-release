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

### Phase A 融合正确性 — 部分落地 (2026-08-22, commit 见 main)

实施中的认知修正: `weighted_rrf` (fusion_policy.py:278) 是完整正确的加权 RRF
(通道内排名投票 weight/(k+rank)、窗口截断、确定性排序), D1 的"非 RRF"表述撤回;
真正差距是 **启用条件苛刻** (需 candidate manifest hash + PP_RETRIEVAL_RRF_*_JSON 三件套,
默认 legacy-auto 落旧加权法) 与 **票值/旧分数币种错配** (RRF top≈1/(k+1)≈0.048 撞
HARD_MIN_SCORE=0.30 绝对门)。

已落地:
- `default_fusion_config(plan, env)`: 零配置等权 RRF 工厂 (k=PP_RETRIEVAL_RRF_K→RRF_K,
  等权 1.0, 窗口 min(plan,80)); legacy-auto 且 PP_FUSION_DEFAULT=rrf (新默认) 时生效,
  =legacy 一键回退; max-v1 显式策略不受影响。
- 派生 ceiling 归一: default 路径票值除以 Σweights/(k+1) 映射回 0..1 币种,
  下游绝对门语义保持; manifest 路径原始票值不变 (golden 契约不动)。
- 性质测试 ×5: 共识胜单臂 (两臂#3 > 单臂#1)、上界 arms/(k+1) 派生、env 覆盖、
  空通道 None、等权断言; tests/test_fusion_policy.py 43 passed。

Phase A 收尾 (ea96455): explain 白名单放行 fusion_algorithm/fusion_rrf_k/
fusion_channels/fusion_ceiling, dispatch 四分支就地捕获; fusion_shadow 纯函数对比
模块 (top-k overlap / Kendall tau / 派生 ceiling) + 确定性性质测试。

### Phase B 过道臂与 guarantees — 落地

- 第三臂 aisle: FUSION_CHANNELS_ALL 校验宇宙隔离 golden 三通道契约;
  planner has_aisle opt-in; engine _aisle_retrieval 按 domain/principle 元数据提名,
  per-domain 8 席防淹没, worth 排序, 分数仅作通道内序; PP_FUSION_AISLE=off 回退。
- Guarantees override ranker: principle/pinned/governance 记忆若入池但落在
  retention window(8) 外, 提升至窗口末位并记 fired 计数进 explain;
  hard-min 绝对门与层下限对 fired 项豁免, 保证最终交付层可见。
  principle 类本体仍走激活注入通道, 检索层 guarantee 针对 pinned/governance。
- 冒烟实证: m_pinned 从 rank 50 提至 8 (fired=1) 且 pinned_visible=true。

### Phase C (第一刀) — read both ends (D6 关闭, commit 见 main)

reranker.py 四个 provider (cloud/jina/siliconflow/ollama) 的候选文本全部是头部
截断 (`[:max_chars]`), 深埋中后段的关键行对重排器不可见——Monopoly 回合规则教训
的精确复刻。新增 `_both_ends_window(text, max_chars)`: 头+显式省略标记
\[…\]+尾, 短文原样、确定性、预算有界; 四处截断点统一替换。
测试 test_reranker_both_ends.py ×4 (头尾保留/joiner 显式/确定性单调/边界),
reranker 三套件 58 passed。

切片(chunking)本体对照结论: structure-v1 的结构感知/逐字保源/heading 上下文已优于
文章基线; 真实差距是 **超长块切分零重叠** (相邻片边界句腰斩)。因 Rust 端
context-engine-core/src/chunking.rs 为镜像实现且有 parity 测试, 单端改动会破坏
Python/Rust 一致性 —— overlap 需双端同步, 与 Phase D 合并执行。

待办: chunking overlap 双端同步(随 Phase D), D3 提名信号增强, D7 发牌定序,
D8 abstain, D9 占位实体治理。
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


## 六、全文逐项核对矩阵 (2026-08-22 全面审计)

| # | 文章要点 | 状态 | 证据/说明 |
|---|---|---|---|
| 1 | 词面臂 | ✅ | BM25 CJK-bigram (_tokenize :6810) + SQLite FTS5, 中文可用 |
| 2 | 语义臂 | ✅ | LanceDB + governed-node Qwen3 2560 维, 控制面路由 |
| 3 | 结构化过道臂 (每书8席/≤32候选) | ✅ B | aisle 通道 per-domain 8 席 + 窗口 32 (a55f582); PP_FUSION_AISLE 回退 |
| 4 | RRF 一臂一票 | ✅ A | weighted_rrf; k 默认 20 可调 —— k 为派生参数而非教条照抄 60 |
| 5 | consensus beats enthusiasm | ✅ | 性质测试: 两臂#3 > 单臂#1 |
| 6 | 提名算术上界 arms/(k+1) | ✅ | 派生 ceiling + 上界性质测试 |
| 7 | 漏斗 每臂≤80→融合截80 | ◐ | 结构等价数字不同: 通道窗 32, candidate_limit = Σ层预算×overfetch (MODE_BUDGETS 按模式派生, 非硬编码教条) |
| 8 | cross-encoder 成对重排 | ✅ | cloud API 天然 pair; 本地 governed-node llama.cpp :19132 |
| 9 | 出口 top 8 | ◐ | 层出口 core6/related10/divergent6 (global) ≈22, 按注意力层分配比单一 8 更细 —— 有意偏离 |
| 10 | read both ends (头595+尾600) | ✅ C | _both_ends_window 统一四 provider + governed node (env 1200), 显式省略标记 (64e2ef7 + 本次) |
| 11 | guarantees 覆盖 ranker | ✅ B | principle/pinned/governance 窗口保位 + hard-min/层下限豁免; 语义差异: 窗口末位保位而非 top1 (通用检索防查询意图扭曲, 有意偏离) |
| 12 | lost-in-the-middle 发牌 (1,3,5,4,2) | ❌ D7 | 未做 |
| 13 | 可派生常量律 | ✅ | ceiling 现场推导 + fusion_ceiling_formula 公式串(本次) + 硬编码上界反例测试 |
| 14 | abstain 纪律 | ❌ D8 | 未做 |
| 15 | wrench 面板显示 guarantee 触发 | ✅ | fusion_guarantee_fired 进 pipeline_stats/retrieval_explain_v1, MCP debug 输出可达 |
| 16 | confess-with-receipt 文化 | ✅ | step-closure 执行者四字段责任制 + audit_run + 信任分联动 (项目既有, 与文章同构) |

### 本轮核对新发现并修复

1. **governed node rerank documents 全量透传** (:5432): 生产主路径 WSL 重排
   收到未窗口化全文 —— 客户端不截断时服务端模型上下文成为隐性上限。
   已加 both_ends_window(env PP_RERANK_GOVERNED_DOC_CHARS=1200)。
2. **explain 缺公式串**: 数值 ceiling 已有但无推导过程 —— 补
   fusion_ceiling_formula ("N arms / (k + 1) = x") 落实"打印公式"要求。

### 核对后剩余欠账 (按优先级)

D7 发牌定序 → D8 abstain → chunking overlap 双端同步(Phase D) →
D9 占位实体治理 → Rust 热路径 explain 对齐。
