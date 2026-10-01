# T1 工具分档 — 46 个 40 天零调用工具

> 日期：2026-10-02 · 仓库：`plastic-promise-release` @ `feat/tool-core-profile`
> 基线：`docs/audits/2026-08-23-handler-binding-census.md`（51 派发 handler）；工具档案：`plastic_promise/mcp/tool_profile.py`
> 遥测证据：生产库 `tool_usage_events`（2026-08-24 → 2026-10-01，1078 次调用）中这 46 个工具均为 0 次
> 定位方法：`grep "def handle_<name>"` + 全仓反查非派发调用点（daemons/、hooks、.codex、CLI、skills 原子表）
> 下表 handler 路径前缀统一为 `plastic_promise/`（协作三工具为 server.py 内联派发，非独立 handler）

## 一、分档明细（46）

| # | 工具 | handler 文件:行 | 分档 | 依据 |
|---|---|---|---|---|
| 1 | memory_recall | mcp/tools/memory.py:804 | KEEP-CORE | 检索主干；技能/钩子内部复用 |
| 2 | memory_update | mcp/tools/memory.py:2374 | KEEP-CORE | 普通写入权限面；别名/MGP 归一入口 |
| 3 | memory_forget | mcp/tools/memory.py:2616 | KEEP-CORE | 公开遗忘面；MGP delete/expire 映射 |
| 4 | memory_list | mcp/tools/memory.py:2803 | KEEP-CORE | 只读列举面（含研究角色白名单） |
| 5 | memory_gc | mcp/tools/memory.py:2863 | INTERNAL-ONLY | session-init 原子（dry_run 预览） |
| 6 | memory_correct | mcp/tools/memory.py:3122 | ALIAS-MERGE | 与 memory_update 内容改写重叠 |
| 7 | memory_reclassify | mcp/tools/memory.py:3263 | INTERNAL-ONLY | maintenance_daemon 批量重分类 |
| 8 | memory_sync_files | mcp/tools/memory.py:3562 | INTERNAL-ONLY | 文件同步提案入池；tools/sync 垫片 |
| 9 | principle_activate | mcp/tools/principles.py:14 | INTERNAL-ONLY | 全部 Skill 首原子；技能内直调 |
| 10 | context_inject | mcp/tools/context.py:608 | INTERNAL-ONLY | 实体图边注入原子（技能生命周期） |
| 11 | context_graph | mcp/tools/context.py:732 | ARCHIVE-SAFE | 仅角色白名单声明，无调用点 |
| 12 | mgp_shadow_bridge | mcp/tools/mgp_shadow.py:15 | ARCHIVE-SAFE | P1 影子审计桥；env 默认 off |
| 13 | feedback_apply | mcp/tools/reflection.py:370 | INTERNAL-ONLY | 技能完成时更新 worth（内部调用） |
| 14 | system | mcp/tools/management.py:111 | INTERNAL-ONLY | session-init 健康原子（stats 等） |
| 15 | runtime_mode | mcp/tools/runtime.py:17 | ARCHIVE-SAFE | 只读 get 白名单；set 走 launcher |
| 16 | issue_transition | mcp/tools/management.py:441 | KEEP-CORE | Issue 状态机；daemon 自动关闭 |
| 17 | issue_list | mcp/tools/management.py:461 | KEEP-CORE | pi_worker 领单读取面 |
| 18 | pack_export | mcp/tools/management.py:492 | ARCHIVE-SAFE | 仅原子表声明；无技能真实引用 |
| 19 | pack_import | mcp/tools/management.py:512 | ARCHIVE-SAFE | 同上；CLI 无 pack 子命令 |
| 20 | domain | mcp/tools/domain.py:168 | INTERNAL-ONLY | session-init 域统计/合并原子 |
| 21 | task_enqueue | mcp/tools/task_queue.py:354 | KEEP-CORE | 工会派单；扫描器内部派单 |
| 22 | task_claim | mcp/tools/task_queue.py:653 | KEEP-CORE | 工会生命周期：揭榜 |
| 23 | task_complete | mcp/tools/task_queue.py:836 | KEEP-CORE | 工会生命周期：交付 |
| 24 | task_verify | mcp/tools/task_queue.py:1000 | KEEP-CORE | 工会验收；报告脚本依赖 |
| 25 | task_inbox | mcp/tools/task_queue.py:1271 | KEEP-CORE | 猎人委托板读取面 |
| 26 | task_heartbeat | mcp/tools/task_queue.py:1387 | KEEP-CORE | daemon 超时释放依赖心跳语义 |
| 27 | task_abandon | mcp/tools/task_queue.py:1500 | KEEP-CORE | 弃单 + 信任惩罚闭环 |
| 28 | collaboration_work_list | mcp/server.py:5139（内联） | KEEP-CORE | 协作 inbox；PR5 唯一有遥测者 |
| 29 | collaboration_lease_heartbeat | mcp/server.py:5148（内联） | KEEP-CORE | 租约续期；TTL 过期即释放 |
| 30 | collaboration_work_accept | mcp/server.py:5154（内联） | KEEP-CORE | 验收闭环终点（receipt 门控） |
| 31 | skill_session_start | mcp/tools/skill_tracking.py:989 | INTERNAL-ONLY | 每次技能调用自动建会话 |
| 32 | skill_session_complete | mcp/tools/skill_tracking.py:1213 | INTERNAL-ONLY | 技能收尾；daemon 弃单清理 |
| 33 | skill_session_trace | mcp/tools/skill_tracking.py:675 | INTERNAL-ONLY | soul_audit 评分 + daemon 巡检 |
| 34 | skill_session_audit | mcp/tools/skill_tracking.py:1652 | ARCHIVE-SAFE | 全仓无真实调用点（建议归档） |
| 35 | skill_auto_track | mcp/tools/skill_tracking.py:1847 | INTERNAL-ONLY | /api/skill-track HTTP 端点包装 |
| 36 | smart-remember | skills/memory_operations.py:439（SkillDef） | KEEP-CORE | 文档收尾主路径；别名对已折叠 |
| 37 | review_run | mcp/tools/review.py:17 | KEEP-CORE | pi_worker 评审链；受信提案路由 |
| 38 | knowledge_search | mcp/tools/knowledge.py:14 | ARCHIVE-SAFE | 门控默认 off，长期降级返回 |
| 39 | commercial_audit_export | mcp/tools/commercial_audit.py:152 | ARCHIVE-SAFE | 商业审计导出，无任何消费者 |
| 40 | market_install | mcp/tools/market.py:96 | ARCHIVE-SAFE | CLI 市场入口；远程安装未实现 |
| 41 | market_upgrade | mcp/tools/market.py:178 | ARCHIVE-SAFE | 名实不符，实为版本检查 |
| 42 | market_remove | mcp/tools/market.py:243 | ARCHIVE-SAFE | CLI 市场入口；能力保留于 CLI |
| 43 | market_enable | mcp/tools/market.py:302 | ALIAS-MERGE | 与 disable 成对，宜归一为启停 |
| 44 | market_disable | mcp/tools/market.py:344 | ALIAS-MERGE | 与 enable 成对，宜归一为启停 |
| 45 | market_status | mcp/tools/market.py:386 | ALIAS-MERGE | 与 market_list 信息重叠 |
| 46 | audit_rollover | core/tool_manifest.py:150 | INTERNAL-ONLY | 内部审计轮转能力名，非暴露工具 |

## 二、统计

| 分档 | 数量 | 工具 |
|---|---|---|
| KEEP-CORE | 18 | memory_recall / memory_update / memory_forget / memory_list / issue_transition / issue_list / task_enqueue / task_claim / task_complete / task_verify / task_inbox / task_heartbeat / task_abandon / collaboration_work_list / collaboration_lease_heartbeat / collaboration_work_accept / review_run / smart-remember |
| INTERNAL-ONLY | 13 | memory_gc / memory_reclassify / memory_sync_files / principle_activate / context_inject / feedback_apply / system / domain / skill_session_start / skill_session_complete / skill_session_trace / skill_auto_track / audit_rollover |
| ALIAS-MERGE | 4 | memory_correct / market_enable / market_disable / market_status |
| ARCHIVE-SAFE | 11 | context_graph / mgp_shadow_bridge / runtime_mode / pack_export / pack_import / knowledge_search / commercial_audit_export / market_install / market_upgrade / market_remove / skill_session_audit |
| **合计** | **46** | 4 档覆盖全部零调用工具 |

## 三、归档风险（一句话）

ARCHIVE-SAFE 的 11 个工具均无运行时消费者，仅从 `list_tools` 隐藏、派发层仍 fail-open 可显式调用，故不删代码即无副作用——唯一需同步处理的是声明式白名单引用（`research_reader` 的 context_graph、三角色的 runtime_mode get），否则该角色调用会落到未知工具；另需注意 market_* 归档后 CLI 市场子命令与 handler 将出现静默分叉，建议一并加 CLI 侧回归测试。
