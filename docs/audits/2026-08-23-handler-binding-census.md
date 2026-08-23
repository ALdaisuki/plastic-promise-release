# Handler Runtime-Binding Census — 2026-08-23

> 勘察工具：\`scripts/handler_binding_census.py\`（机械提取，零遗漏）；
> 敏感性分诊：两个独立分诊代理逐一读函数体。
> 背景：PR #15 发现 defense 面因缺少 \`_runtime_context\` 绑定出现无鉴权信任调整——本普查回答"还有哪些同类面"。

## 总量

- 派发 handler：51；**无 runtime 绑定：37**；签名与派发不一致：0（绑定约定自洽但覆盖窄）
- 分诊结果：**SM（敏感变更）16 · RQ 只读 15 · HVY 重操作 2 · GOV 已另有治理 2 · 数据出口 1 · 名实不符 1**

## SM 清单（绑定 _runtime_context 后必须收紧）

| Tier | Handler | 面 |
|---|---|---|
| 1 | pack_import | replace 批量覆写记忆池 |
| 1 | market_install / market_remove / market_enable / market_disable | 插件执行面安装/删除 |
| 1 | review_run | apply 触发信任分变动 + 自动建 fix 委托 |
| 2 | memory_gc (force) / memory_store | canonical 记忆删除合并/写入 |
| 2 | domain (merge/rebuild) | 域状态变更 |
| 2 | skill_session_start / complete / audit(auto_fix) | 会话实体与 worth/标签写入 |
| 2 | runtime_mode (set) | 热切执行面 + 重初始化 |
| 2 | mgp_shadow_bridge (set_mode) | 改运行 env 无守卫 |
| 2 | issue_create | 无守卫直写议题存储 |

## 其他发现

- \`pack_export\`（HVY）：任意路径全池数据出口——绑定后建议单独限权（导出面审计）
- \`market_upgrade\`：函数体实为版本检查，git 安装路径未实现——名实不符可降级
- \`auto_context_inject\`（GOV）：写仅经提案门控出站，约束在 core/memory_proposals.py ✓
- 内部路径默认 actor（_resolve_task_authority runtime=None → 'claude'）：受信边界已文档化，建议后续显式 allowlist

## 修复模式（沿用 PR #15 范式）

server 派发注入 \`_runtime_context={actor}\` → handler 签名加 keyword-only 参数 → 按 Tier 定策略：
Tier1 = reviewer-only；Tier2 = 绑定即可（记录 actor 审计）；HVY/RQ 保持现状。
分阶段落地：每 PR ≤6 个 handler，全量测试护航。
