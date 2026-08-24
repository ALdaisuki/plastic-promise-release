# 04 — 注入面与工具面全景

> 勘察日期：2026-08-23 · 仓库：plastic-promise-release · 工具面权威源：`plastic_promise/mcp/server.py`（58 暴露工具 = 51 派发 handler + 兼容别名）

## 一、注入面（自动注入触点）

### A1. session-init light 简报
- **代码**：`plastic_promise/skills/session_lifecycle.py`（`_context_mode()` 默认 light；`_light_context_status()` 有界预览）
- **触发**：Agent 显式调用 `session-init(task_description, context_mode)`；light 为默认档。
- **内容来源**：原则激活 + SCARF 基线 + 域/系统健康 + 信任分 + GC 预览 + chain_state + 1–2 条轻量记忆预览（adoption-audit 后 light 不再回显 code_memory）。完整上下文已从启动原子中移除，需按需显式 `context_supply`。

### A2. DSH profile 插件 pp-memory-cadence
- **配置**：`~/.dsh/profiles/web/cordis.patch.yml` → id `pp-memory-cadence`，url :9020/mcp，`every: 5` 步、timeoutMs 8000、maxInjectionChars 2400。
- **触发**：DSH 会话每完成 5 个 agent step 自动调用一次。
- **内容来源**：经 MCP 从记忆池取有界节拍注入（节拍器插件内联拼装），超预算截断。
- **状态**：随 DSH web profile 生效中。

### A3. UserPromptSubmit hook → auto_context_inject
- **触发**：Codex/DSH 客户端每次用户提交 prompt 时自动 Hook 调用。
- **内容来源**：`auto_context_inject(event="before_invoke")` 三层上下文包（核心/关联/发散）；同时在该 hook 的 additionalContext 中注入 official flow / 主链 / current-next stage 与 [user]/[model] 权限。canonical memory + proposal + 路由文本合并受总字符预算约束，路由 scope 优先保留。
- **注意**：注入返回的 project_id/stage_session_id/flow_line_id 必须原样回传 session-init 与 sp-stage。

### A4. Stop 被动捕获
- **触发**：会话 Stop hook；显式规则未产生 candidate 时进入被动路径。
- **内容来源**：原始用户文本写入 durable derived-work，异步批量（攒 20 条或 30s 窗口）交结构化切片 Provider，evidence 逐字 grounded；不重复写 canonical memory。
- **生效状态**：`PP_PASSIVE_SEMANTIC_CAPTURE=shadow` —— **今日已启用**（launchd 服务 org.plastic-promise.mac-canonical-runtime 环境实测确认），仅分类入 shadow 分区、不落 proposal；`PP_MEMORY_PROPOSAL_AUTO_ADOPT` 与 `PP_MEMORY_PROPOSALS` 仍为 off，晋升门禁关闭。

## 二、工具面：51 派发 handler 绑定普查

巡检手段：`python scripts/handler_binding_census.py` 机械提取 server.py 全部派发点与签名，输出绑定状态 JSON；历史基线见 `docs/audits/2026-08-23-handler-binding-census.md`。当前输出：**51 派发 / 22 无 runtime 绑定 / 签名-派发不一致 0**。

### Tier1（reviewer 门已上线，6）
| Handler | 面 |
|---|---|
| pack_import | replace 批量覆写记忆池 |
| market_install / market_remove / market_enable / disable | 插件执行面安装删除 |
| review_run | apply 触发信任分变动 + 自动建 fix 委托 |

### Tier2（已绑定 _runtime_context 并记录 actor 审计，9）
memory_gc(force) · memory_store · domain(merge/rebuild) · skill_session_start / complete / audit(auto_fix) · runtime_mode(set) · mgp_shadow_bridge(set_mode) · issue_create

### defense（reviewer 门）
信任分读写走 server-owned authority；adjust 受 PR #15 范式收紧，get/history 只读。

### 其余 22 无绑定（RQ 只读 / GOV 已治理 / HVY 重操作）
- RQ 只读：memory_recall/list、principle_activate/evaluate、context_supply/inject/graph、auto_context_inject、audit_run/pre_check、scarf_reflect、system(stats)、issue_list/transition、pack_export(数据出口)、skill_session_trace/auto_track、commercial_audit_export、knowledge_search、market_list/upgrade/status
- GOV：auto_context_inject 出站已经 core/memory_proposals.py 门控 ✓
- HVY 关注：pack_export 任意路径全池出口（建议单独限权）；market_upgrade 名实不符（实为版本检查）

## 深度优化候选

1. **注入命中率可观测性缺口**：A1–A3 四路注入均无「被模型实际引用率」遥测——建议在 auto_context_inject / cadence 输出打 span（复用 commercial_audit_export 的 call_spans），量化每层包的核心/关联命中，淘汰低价值注入。
2. **cadence 频率自适应**：固定 every=5 不分任务密度；可按 tool_usage_report 遥测做动态间隔（编码密集期降频、决策期升频），并让 maxInjectionChars 随上下文水位收缩。
3. **工具档案化 core profile 注册机制**：Tier1/Tier2 绑定状态目前散在审计 md；建议把 census 输出固化为机器可读 manifest（handler→tier→binding→actor 策略），server 启动时校验漂移，未注册新 handler fail-closed 报警。
4. **Stop shadow→on 决策数据化**：shadow 分类结果已有持久分区但无评估报告；补一个批量质量抽样脚本（grounded evidence 准确率）作为开启 on 门禁的证据。
5. **HVY 出口收敛**：pack_export 任意路径全池导出应并入 reviewer 门或限定 experience_packs/ 目录白名单，与 commercial_audit_export 共用出口审计通道。
