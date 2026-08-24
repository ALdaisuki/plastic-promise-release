# 03 — 记忆全生命周期管道图

> 勘察基线：发行仓 main；路径均相对 `plastic_promise/`（另注明者除外）。行号为当前工作区快照。

## 1. 显式写入（memory_store → LanceDB 双写）

| 环节 | 入口 | 关键变换 |
|---|---|---|
| MCP 工具层 | `mcp/tools/memory.py:1502` handle_memory_store → `:1593` 调 store_urgent | 参数校验、project_id/visibility 绑定 |
| 提案门禁 | `memory/pipeline.py:118-130` | proposal_mode=on 且受保护 source_class 非可信来源 → ProposalPolicyError("approval_required") |
| 智能提取 | `smart_extractor.py:158` extract_memories（store_urgent 内 `:133-147` 调用） | 6 类提取 + L0/L1/L2 分层，LLM fallback；0 结果+空白→None，0 结果+实质内容→raw 兜底 |
| 向量去重 | `memory/pipeline.py:980-982` check_duplicate | LanceDB ANN cos≥0.85 → 更新旧记录（access_count↑, worth↑, half_life↑） |
| 质量门控 | `core/quality_gate.py:22` QualityGate，`:29` score() | 4 维×0.25 等权：≥0.5 入库 / 0.3–0.5 low_quality / <0.3 丢弃 |
| 权威落库 | `memory/soul_memory.py:605` RecMem.store | SQLite canonical 写入 + decay_multiplier / effective_half_life 初始化 |
| 双写索引 | `memory/pipeline.py:1187-1207` | search_text + 向量写入 LanceDB（可重建派生索引，非真相源） |

## 2. 被动写入（Stop hook → proposal → 自动晋升）

链路：`passive_memory/codex_hook.py:1316` Stop 事件 → durable derived-work 落盘（Hook 不同步等云）
→ `passive_memory/coordinator.py:1089-1097` 双门禁判定（passive_memory_mode × proposal_mode；shadow 只记录不持久化）
→ `passive_memory/semantic_pipeline.py:546` enqueue_semantic_capture 入队
→ DurableSemanticMemoryWorker（`:143`）攒批：batch_size=20 / max_wait=30s（`:153-154`；env 可调 `:658/:660`）
→ GovernedNodeSemanticProvider.complete_json（`:86`）结构化切片 JSON Provider（按 project/visibility/config/provider 分区）
→ `:284` _validated_candidates 校验逐字 grounded evidence → `:412` MemoryProposalStore.create_many 建 proposal
→ ProposalAutomation 记录每 turn 评分证据 → `:485` enqueue_proposal_promotion_job。

门禁现状：
- `semantic_pipeline.py:522` semantic_capture_mode 读 `PP_PASSIVE_SEMANTIC_CAPTURE`，源码默认 off；
  **`launcher/default_environment.py:48` setdefault "shadow" —— 今日已启用 shadow**（只存分类结果，不转 proposal 正文入库）。
- 晋升唯一策略权威 `core/proposal_promotion.py:820` evaluate_auto_promotion：mode=shadow → 仅持久化
  would_promote 信号（`:864-873`）；mode=on 才调用既有原子晋升器。由 `PP_MEMORY_PROPOSAL_AUTO_ADOPT`（`:27`）控制，
  默认 off。三个门禁（capture/proposals/auto_adopt）默认均为 off。

## 3. 检索（三路融合 + rerank）

- 入口：`memory_recall` 工具与 `core/context_engine.py:3437` ContextEngine.supply。
- 融合通道：`core/fusion_policy.py:18` FUSION_CHANNEL_ORDER=("vector","bm25","fts")，RRF 归一合并（`:415-428`）；
  叠加 EntityGraph 图遍历双通道与符号规则 boost（context_engine `:7320-7333` security×1.5 等）。
- Rerank：`reranker.both_ends_window`（`context_engine.py:36` 引入），P3a MMR 多样性 + 可选 rerank 后重分层（`:5412`）；
  云端 governed rerank 有 diagnostics 不夸大声明（`:183`）。完整 supply 三路+Ollama rerank 耗时 5~60s。
- code_memory 回声降权（已落地）：`_enrich_pack_with_code_memory` 截断 code 结果至 top2（`:4580-4581`，
  adoption-audit 注释）；`_source_penalty_for` SOURCE_DOWNWEIGHT 表（`:7336-7361`）对 maintenance_daemon/
  step-closure/skill_session 等自产源降权 0.1–0.3；session-init light 预览亦排除 code echo（skills/session_lifecycle.py:277）。

## 4. 维护（MemoryGC 与 Daemon 周期）

- RecMem.collect（`memory/soul_memory.py:1442`）：mark_decaying（`:1518`）→ WeibullDecayCalculator 批量衰减
  （`core/decay_engine.py:17`；L1 beta=1.5/hl=3d，L2 1.2/7d，L3 0.7/90d；update_all_decay `:1219`）
  → merge_similar（`:1550`）cos≥0.70 按 composite_score 选幸存者 → forget（`:957`）清理 decayed+merged。
- memory_forget 为软删（标记衰退），约 7 天后 GC 清理。
- daemons/maintenance_daemon.py 任务注册表（`:2194-2201`）：heartbeat 10s、safety_net 600s、
  scan_data_quality 600s、scheduler_health 3600s、audit/governed_maintenance INTERVAL；
  AdaptiveThrottle 连续空转倍增间隔（max 8x，`:174`）。维护顺序固定：lifecycle → passive outbox replay
  → semantic jobs → promotion reconcile/process → proposal expiry → synthesis integrity → index replay → audit。

## 5. 注入

- session-init：`skills/session_lifecycle.py:183-187` context_mode 默认 light——只做 1–2 条有界轻量预览
  （偏好 curated、排除 code echo），完整 supply 必须显式调用；full 才运行 engine.supply。
- pp-memory-cadence：外部客户端侧 hook（~/.dsh/profiles/web/node_modules/），每步触发注入节奏；
  服务端不承载该循环（见 docs/research/three-librarians-rag-alignment.zh-CN.md:96）。

## 深度优化候选（自动化为主）

1. **被动捕获自动转正判据**：capture 已 shadow 运行，但 auto_adopt 仍需手工切 on；可基于 would-promote
   信号的 N 天命中率/人工驳回率设阈值，达标自动启用 promotion（保留回滚开关）。
2. **proposal ↔ 显式 store 去重汇合点缺失**：store_urgent 的 cos≥0.85 查重只在 fuzzy buffer 内生效，
   被动 proposal 经 create_many → 原子晋升器入库，两路无统一查重汇合；应在 promote 处复用同一向量查重，
   避免转正后与显式记忆重复。
3. **检索延迟预算未量化**：supply 三路融合 + rerank 5~60s 无分阶段预算；建议在 RetrievalPlan 中为
   vector/bm25/graph/rerank 各设超时与降级路径（如 rerank 超时直接用融合序），并输出耗时审计字段。
4. **worth 公式对 docstring 回声敏感**：calculate_worth（soul_memory.py:57）freshness 由 decay_multiplier
   推导、访问即强化；code echo 已在展示层截断 top2，但 worth/access 层未隔离——建议对 source=code_memory
   设 worth 增长上限或不计入 access 强化，防自产回声自我加权。
5. **去重/合并阈值静态**：cos≥0.85（去重）与 ≥0.70（merge）全局固定；可按 tier/domain 自适应
   （高价值域收紧、噪声域放宽），减少误合并与僵尸重复。
6. **语义 worker 未纳入 daemon 调度**：DurableSemanticMemoryWorker/promotion worker 仅活在 MCP 进程内线程，
   崩溃窗口靠 maintenance reconcile 补齐；可将 semantic jobs 与 promotion process 移入 maintenance_daemon
   固定顺序统一调度，缩短补账延迟。
